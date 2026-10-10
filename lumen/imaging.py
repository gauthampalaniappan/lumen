"""Non-destructive image rendering. Original files are never written."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff"}

# Camera raw is copied and indexed only. Lumen does not decode it.
RAW_EXTS = {
    ".3fr", ".arw", ".cr2", ".cr3", ".dcr", ".dng", ".erf", ".fff",
    ".iiq", ".kdc", ".mef", ".mos", ".mrw", ".nef", ".nrw", ".orf",
    ".pef", ".raf", ".raw", ".rw2", ".rwl", ".sr2", ".srf", ".srw",
    ".x3f",
}
# Stills the import dialog lists. GIF stays in the library scanner only.
IMPORT_STILLS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
IMPORT_EXTS = IMPORT_STILLS | RAW_EXTS
INDEX_EXTS = IMAGE_EXTS | RAW_EXTS


def is_raw_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in RAW_EXTS

EXPORT_FORMATS = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
    ".tif": "TIFF",
    ".tiff": "TIFF",
}


def open_image(path: str | Path) -> Image.Image:
    """Load the first frame, applying EXIF orientation for display only."""
    with Image.open(path) as im:
        try:
            im.seek(0)
        except EOFError:
            pass
        im.load()
        frame = im.copy()
    try:
        frame = ImageOps.exif_transpose(frame)
    except Exception:
        pass
    return frame


def flatten_rgb(im: Image.Image) -> Image.Image:
    if im.mode == "RGB":
        return im
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return im.convert("RGB")


def _enhance(im: Image.Image) -> Image.Image:
    """Autocontrast plus a mild color and contrast bump (I'm Feeling Lucky-ish)."""
    alpha = None
    if im.mode == "RGBA":
        alpha = im.getchannel("A")
        base = im.convert("RGB")
    elif im.mode != "RGB":
        base = im.convert("RGB")
    else:
        base = im
    base = ImageOps.autocontrast(base, cutoff=0.8)
    base = ImageEnhance.Color(base).enhance(1.18)
    base = ImageEnhance.Contrast(base).enhance(1.12)
    base = ImageEnhance.Brightness(base).enhance(1.04)
    if alpha is not None:
        base = base.convert("RGBA")
        base.putalpha(alpha)
    return base


TONE_KEYS = (
    "exposure",
    "contrast",
    "highlights",
    "shadows",
    "whites",
    "blacks",
    "saturation",
)

# Exposure is in stops. The rest are percentages around zero.
TONE_RANGES = {
    "exposure": (-2.0, 2.0),
    "contrast": (-100.0, 100.0),
    "highlights": (-100.0, 100.0),
    "shadows": (-100.0, 100.0),
    "whites": (-100.0, 100.0),
    "blacks": (-100.0, 100.0),
    "saturation": (-100.0, 100.0),
}


def neutral_tone() -> dict[str, float]:
    return {key: 0.0 for key in TONE_KEYS}


def clamp_tone(values: dict | None) -> dict[str, float]:
    """Copy a tone dict into range. Unknown keys are ignored."""
    out = neutral_tone()
    if not isinstance(values, dict):
        return out
    for key, (lo, hi) in TONE_RANGES.items():
        raw = values.get(key, 0.0)
        try:
            number = float(raw)
        except (TypeError, ValueError):
            number = 0.0
        if number != number or number in (float("inf"), float("-inf")):
            number = 0.0
        out[key] = max(lo, min(hi, number))
    return out


def tone_is_neutral(values: dict | None) -> bool:
    tone = clamp_tone(values)
    return all(abs(tone[key]) < 1e-6 for key in TONE_KEYS)


# Longer side of the picture shown while a slider is moving.
# Full resolution is rendered once the drag stops.
PREVIEW_LIVE_EDGE = 1200


def ops_without_tone(ops: list[dict]) -> list[dict]:
    """Edit list with the tone row removed. Rotate, crop, and enhance stay in order."""
    return [op for op in ops if op.get("op") != "tone"]


def open_image_fitted(path: str | Path, edge: int) -> Image.Image:
    """Load a display copy whose longer side is at most edge.

    JPEG may decode below full size. The original file is not written.
    """
    edge = max(1, int(edge))
    with Image.open(path) as im:
        try:
            im.draft("RGB", (edge, edge))
        except Exception:
            pass
        try:
            im.seek(0)
        except EOFError:
            pass
        im.load()
        frame = im.copy()
    try:
        frame = ImageOps.exif_transpose(frame)
    except Exception:
        pass
    return fit_inside(flatten_rgb(frame), edge)


def preview_base_image(path: str | Path, ops: list[dict], edge: int = PREVIEW_LIVE_EDGE) -> Image.Image:
    """Rotate, crop, and enhance, then fit. Tone sliders are applied afterwards."""
    geometric = ops_without_tone(ops)
    if not geometric:
        return open_image_fitted(path, edge)
    return fit_inside(flatten_rgb(render(path, geometric)), edge)


def render_tone_on(base: Image.Image, values: dict | None) -> Image.Image:
    """Apply slider values to a copy of base. base itself is left unchanged."""
    tone = clamp_tone(values)
    if tone_is_neutral(tone):
        return base.copy()
    return flatten_rgb(_apply_tone(base, {"op": "tone", **tone}))


def _clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _smoothstep(value: float) -> float:
    value = _clamp01(value)
    return value * value * (3.0 - 2.0 * value)


def _tone_lut(tone: dict[str, float]) -> list[int] | None:
    """256-entry curve for exposure through blacks. None when that part is neutral.

    Saturation is not in the curve. It is applied afterwards on color.
    """
    if all(abs(tone[key]) < 1e-6 for key in TONE_KEYS if key != "saturation"):
        return None
    exposure = tone["exposure"]
    contrast = tone["contrast"] / 100.0
    highlights = tone["highlights"] / 100.0
    shadows = tone["shadows"] / 100.0
    whites = tone["whites"] / 100.0
    blacks = tone["blacks"] / 100.0
    gain = 2.0 ** exposure
    lut: list[int] = []
    for index in range(256):
        x = index / 255.0
        if exposure != 0.0:
            # Approximate a linear-light exposure change, then encode again.
            linear = x * x
            x = _clamp01((linear * gain) ** 0.5)
        if blacks != 0.0 and x < 0.35:
            weight = _smoothstep((0.35 - x) / 0.35)
            if blacks > 0.0:
                x = x + blacks * 0.45 * weight * (1.0 - x)
            else:
                x = x * (1.0 + blacks * 0.95 * weight)
        if whites != 0.0 and x > 0.65:
            weight = _smoothstep((x - 0.65) / 0.35)
            if whites > 0.0:
                x = x + whites * 0.55 * weight * (1.0 - x)
            else:
                x = x + whites * weight * (x - 0.5) * 0.9
        if shadows != 0.0 and x < 0.55:
            weight = _smoothstep((0.55 - x) / 0.55)
            if shadows > 0.0:
                x = x + shadows * 0.50 * weight * (1.0 - x)
            else:
                x = x * (1.0 + shadows * 0.80 * weight)
        if highlights != 0.0 and x > 0.45:
            weight = _smoothstep((x - 0.45) / 0.55)
            if highlights > 0.0:
                x = x + highlights * 0.50 * weight * (1.0 - x)
            else:
                x = x + highlights * 0.70 * weight * x
        if contrast != 0.0:
            x = (x - 0.5) * (1.0 + contrast) + 0.5
        lut.append(int(round(_clamp01(x) * 255.0)))
    return lut


def _apply_tone(im: Image.Image, op: dict) -> Image.Image:
    tone = clamp_tone(op)
    if tone_is_neutral(tone):
        return im
    alpha = None
    if im.mode == "RGBA":
        alpha = im.getchannel("A")
        base = im.convert("RGB")
    elif im.mode != "RGB":
        base = im.convert("RGB")
    else:
        base = im
    lut = _tone_lut(tone)
    if lut is not None:
        # Pillow applies one table per band. The same curve goes to R, G, and B.
        base = base.point(lut * len(base.getbands()))
    saturation = tone["saturation"]
    if abs(saturation) >= 1e-6:
        factor = max(0.0, 1.0 + saturation / 100.0)
        base = ImageEnhance.Color(base).enhance(factor)
    if alpha is not None:
        base = base.convert("RGBA")
        base.putalpha(alpha)
    return base


def _clamp_crop(im: Image.Image, op: dict) -> Image.Image:
    width, height = im.size
    x = int(op.get("x", 0))
    y = int(op.get("y", 0))
    w = int(op.get("w", width))
    h = int(op.get("h", height))
    x = max(0, min(x, max(0, width - 1)))
    y = max(0, min(y, max(0, height - 1)))
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))
    return im.crop((x, y, x + w, y + h))


def apply_ops(im: Image.Image, ops: list[dict]) -> Image.Image:
    for op in ops:
        kind = op.get("op")
        if kind == "rotate":
            direction = op.get("direction")
            if direction == "left":
                im = im.transpose(Image.Transpose.ROTATE_90)
            elif direction == "right":
                im = im.transpose(Image.Transpose.ROTATE_270)
            else:
                raise ValueError(f"unknown rotate direction: {direction}")
        elif kind == "crop":
            im = _clamp_crop(im, op)
        elif kind == "enhance":
            im = _enhance(im)
        elif kind == "tone":
            im = _apply_tone(im, op)
        else:
            raise ValueError(f"unknown edit: {kind}")
    return im


def render(path: str | Path, ops: list[dict]) -> Image.Image:
    return apply_ops(open_image(path), ops)


def current_size(path: str | Path, ops: list[dict]) -> tuple[int, int]:
    return render(path, ops).size


def crop_fits(size: tuple[int, int], x: int, y: int, w: int, h: int) -> bool:
    width, height = size
    return w >= 1 and h >= 1 and x >= 0 and y >= 0 and x + w <= width and y + h <= height


def edit_sig(ops: list[dict]) -> str:
    blob = json.dumps(ops, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha1(blob).hexdigest()[:16]


# Longest side of a cached thumbnail. Matches the largest gallery slider
# step so the UI can scale down without decoding the original again.
THUMB_CACHE_EDGE = 320


def fit_inside(im: Image.Image, edge: int) -> Image.Image:
    """Return a copy that fits in an edge×edge box. Keeps aspect ratio, never crops or upscales."""
    edge = max(1, int(edge))
    fitted = im.copy()
    fitted.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    return fitted


def thumb_file(cache_thumbs: Path, photo_path: str, mtime_ns: int, ops: list[dict]) -> Path:
    # "contain" distinguishes this from the old center-cropped 240×160 cache.
    key = hashlib.sha1(
        f"{photo_path}|{mtime_ns}|{edit_sig(ops)}|contain{THUMB_CACHE_EDGE}".encode()
    ).hexdigest()
    return cache_thumbs / f"{key}.jpg"


def ensure_thumb(cache_thumbs: Path, photo_path: str, mtime_ns: int, ops: list[dict]) -> Path:
    cache_thumbs.mkdir(parents=True, exist_ok=True)
    dest = thumb_file(cache_thumbs, photo_path, mtime_ns, ops)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    im = fit_inside(flatten_rgb(render(photo_path, ops)), THUMB_CACHE_EDGE)
    tmp = dest.with_suffix(".jpg.part")
    im.save(tmp, "JPEG", quality=82)
    tmp.replace(dest)
    return dest


def scaled_thumb_bytes(path: str | Path, edge: int, quality: int = 85) -> bytes:
    """JPEG of path scaled so its longer side is at most edge. Does not crop or upscale."""
    with Image.open(path) as im:
        im.load()
        frame = im.copy()
    fitted = fit_inside(flatten_rgb(frame), edge)
    buf = io.BytesIO()
    fitted.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def rgb_histogram(im: Image.Image, edge: int = 512) -> tuple[list[int], list[int], list[int]]:
    """Red, green, and blue counts for a preview. Large images are scaled down first."""
    rgb = flatten_rgb(im)
    if max(rgb.size) > edge:
        rgb = fit_inside(rgb, edge)
    counts = rgb.histogram()
    if len(counts) < 768:
        counts = (list(counts) + [0] * 768)[:768]
    return list(counts[0:256]), list(counts[256:512]), list(counts[512:768])


def jpeg_bytes(im: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    flatten_rgb(im).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def export_copy(src: str | Path, ops: list[dict], dest: str | Path) -> None:
    """Write a new file. Refuses to replace the original."""
    src_path = Path(src)
    dest_path = Path(dest)
    if _same_file(src_path, dest_path):
        raise ValueError("Refusing to overwrite the original file.")
    ext = dest_path.suffix.lower()
    fmt = EXPORT_FORMATS.get(ext)
    if fmt is None:
        allowed = ", ".join(sorted(EXPORT_FORMATS))
        raise ValueError(f"Use one of these extensions: {allowed}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    im = render(src_path, ops)
    if fmt == "JPEG":
        im = flatten_rgb(im)
        im.save(dest_path, "JPEG", quality=92)
    elif fmt == "PNG":
        im.save(dest_path, "PNG")
    elif fmt == "WEBP":
        im.save(dest_path, "WEBP", quality=90)
    elif fmt == "TIFF":
        im.save(dest_path, "TIFF")
    else:
        raise ValueError(fmt)


def prepare_editor_file(src: str | Path, ops: list[dict], export_dir: str | Path) -> Path:
    """Path to open in an external editor.

    No edits: the original path, unread and unwritten.
    Edits: a new file under export_dir with the stored edits applied.
    Never replaces the original.
    """
    src_path = Path(src)
    if not ops:
        return src_path
    folder = Path(export_dir)
    folder.mkdir(parents=True, exist_ok=True)
    ext = src_path.suffix.lower()
    if ext not in EXPORT_FORMATS:
        ext = ".jpg"
    try:
        identity = str(src_path.resolve())
    except OSError:
        identity = str(src_path)
    key = hashlib.sha1(f"{identity}|{edit_sig(ops)}".encode()).hexdigest()[:12]
    dest = folder / f"{src_path.stem}-{key}{ext}"
    export_copy(src_path, ops, dest)
    return dest
