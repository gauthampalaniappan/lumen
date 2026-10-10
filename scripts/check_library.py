#!/usr/bin/env python3
"""Exercise the library and edit pipeline without opening a window."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.make_samples import make_samples  # noqa: E402


def main() -> int:
    samples = make_samples()
    with tempfile.TemporaryDirectory(prefix="lumen-check-") as tmp:
        base = Path(tmp)
        os.environ["XDG_CONFIG_HOME"] = str(base / "config")
        os.environ["XDG_DATA_HOME"] = str(base / "data")
        os.environ["XDG_CACHE_HOME"] = str(base / "cache")
        from lumen.imaging import apply_ops, current_size, export_copy, open_image, render, tone_is_neutral
        from lumen.store import Store
        from PIL import Image

        store = Store()
        assert store.paths.db == base / "data" / "lumen" / "library.db"
        assert store.paths.config_file == base / "config" / "lumen" / "config.json"
        root = store.add_root(str(samples))
        count = store.scan_root(root)
        photos = store.query_photos()
        names = {p["filename"] for p in photos}
        print(f"indexed {count} files")
        for required in (
            "harbor-morning.jpg",
            "green-trail.jpg",
            "tiny.png",
            "tiny.webp",
            "tiny.gif",
            "tiny.tiff",
            "Upper.JPG",
            "rotated-exif.jpg",
        ):
            assert required in names, required
        assert "notes.txt" not in names
        assert ".secret.jpg" not in names
        assert any(p["folder"].endswith(str(Path("trips") / "2024")) for p in photos)

        harbor = next(p for p in photos if p["filename"] == "harbor-morning.jpg")
        store.set_rating(harbor["id"], 4)
        again = store.get_photo(harbor["id"])
        assert again["rating"] == 4
        starred = store.query_photos(min_rating=1)
        assert any(p["id"] == harbor["id"] for p in starred)
        assert store.query_photos(min_rating=5) == []

        store.set_tags(harbor["id"], "vacation, Harbor, vacation")
        assert store.get_tags(harbor["id"]) == ["Harbor", "vacation"] or set(store.get_tags(harbor["id"])) == {
            "Harbor",
            "vacation",
        }
        found = store.query_photos(text="vacation")
        assert len(found) == 1 and found[0]["id"] == harbor["id"]
        by_folder = store.query_photos(text="2024")
        assert any(p["filename"] == "green-trail.jpg" for p in by_folder)

        album_id = store.create_album("Summer")
        store.set_membership(harbor["id"], album_id, True)
        in_album = store.query_photos(text="Summer")
        assert any(p["id"] == harbor["id"] for p in in_album)
        assert store.query_photos(album_id=album_id)[0]["id"] == harbor["id"]

        original = hashlib.sha256((samples / "harbor-morning.jpg").read_bytes()).hexdigest()
        with Image.open(samples / "rotated-exif.jpg") as raw:
            raw_size = raw.size
        oriented = open_image(samples / "rotated-exif.jpg")
        assert oriented.size == (raw_size[1], raw_size[0]), (raw_size, oriented.size)

        base_im = open_image(samples / "harbor-morning.jpg")
        rotated = apply_ops(base_im.copy(), [{"op": "rotate", "direction": "left"}])
        assert rotated.size == (base_im.size[1], base_im.size[0])
        cropped = apply_ops(base_im.copy(), [{"op": "crop", "x": 10, "y": 20, "w": 30, "h": 40}])
        assert cropped.size == (30, 40)
        enhanced = apply_ops(base_im.copy(), [{"op": "enhance"}])
        assert list(enhanced.getdata()) != list(base_im.convert("RGB").getdata())

        store.add_edit(harbor["id"], "rotate", {"direction": "right"})
        store.add_edit(harbor["id"], "crop", {"x": 0, "y": 0, "w": 100, "h": 80})
        ops = store.get_edits(harbor["id"])
        assert current_size(harbor["path"], ops) == (100, 80)
        dest = base / "export.jpg"
        export_copy(harbor["path"], ops, dest)
        assert dest.is_file() and dest.stat().st_size > 0
        after = hashlib.sha256(Path(harbor["path"]).read_bytes()).hexdigest()
        assert after == original
        try:
            export_copy(harbor["path"], ops, harbor["path"])
            raise SystemExit("export should have refused to overwrite the original")
        except ValueError as exc:
            assert "original" in str(exc)
        store.clear_edits(harbor["id"])
        assert store.get_edits(harbor["id"]) == []
        plain = render(harbor["path"], [])
        assert plain.size == base_im.size

        # Tone sliders are one stored op. They change pixels and never the file.
        gradient = Image.new("RGB", (256, 8))
        for x in range(256):
            for y in range(8):
                gradient.putpixel((x, y), (x, 40 if x < 128 else 200, 255 - x))
        before_px = list(gradient.getdata())

        def mean_luma(im: Image.Image) -> float:
            rgb = im.convert("RGB")
            total = 0
            count = 0
            for pixel in rgb.getdata():
                total += pixel[0] + pixel[1] + pixel[2]
                count += 3
            return total / count

        exposed = apply_ops(gradient.copy(), [{"op": "tone", "exposure": 1.0}])
        darkened = apply_ops(gradient.copy(), [{"op": "tone", "exposure": -1.0}])
        assert mean_luma(exposed) > mean_luma(gradient) + 8
        assert mean_luma(darkened) < mean_luma(gradient) - 8
        neutral = apply_ops(gradient.copy(), [{"op": "tone"}])
        assert list(neutral.getdata()) == before_px
        for key, amount in (
            ("contrast", 80),
            ("highlights", 80),
            ("shadows", 80),
            ("whites", 80),
            ("blacks", -80),
            ("saturation", -100),
        ):
            changed = apply_ops(gradient.copy(), [{"op": "tone", key: amount}])
            assert list(changed.getdata()) != before_px, key
        gray = apply_ops(gradient.copy(), [{"op": "tone", "saturation": -100}])
        sample = gray.getpixel((200, 2))
        assert abs(sample[0] - sample[1]) <= 1 and abs(sample[1] - sample[2]) <= 1, sample

        file_bytes = Path(harbor["path"]).read_bytes()
        store.set_tone(harbor["id"], {"exposure": 1.0, "contrast": 25})
        store.set_tone(harbor["id"], {"exposure": -0.5, "saturation": 10})
        tone_ops = [op for op in store.get_edits(harbor["id"]) if op["op"] == "tone"]
        assert len(tone_ops) == 1
        assert abs(tone_ops[0]["exposure"] + 0.5) < 1e-6
        adjusted = render(harbor["path"], store.get_edits(harbor["id"]))
        assert mean_luma(adjusted) != mean_luma(plain)
        store.set_tone(harbor["id"], {})
        assert tone_is_neutral(store.get_tone(harbor["id"]))
        assert store.get_edits(harbor["id"]) == []
        restored = render(harbor["path"], store.get_edits(harbor["id"]))
        assert list(restored.getdata()) == list(plain.getdata())
        assert Path(harbor["path"]).read_bytes() == file_bytes

        # Second scan keeps the rating and does not duplicate.
        store.scan_root(root)
        assert store.get_photo(harbor["id"])["rating"] == 4
        assert len(store.query_photos()) == count

        cfg = store.paths.config_file.read_text(encoding="utf-8")
        assert str(samples.resolve()) in cfg
        print("CHECK_OK", store.paths.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
