"""Copy photos from a card, camera, or folder into the library.

Files are copied, never moved. An existing destination file is skipped.
Nothing here overwrites a photo that is already on disk.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from lumen.imaging import IMPORT_EXTS, is_raw_path
from lumen.store import Store

CAMERA_NOTE = "Camera download needs gphoto2."
_GPHOTO_PORT = re.compile(r"^(usb|serial|disk):")
_LIST_FOLDER = re.compile(r"in folder '([^']*)'")
_LIST_FILE = re.compile(r"^#(\d+)\s+(\S+)")
_MOUNT_AT = re.compile(r"\bat\s+(\S+)")
_DEVICE = re.compile(r"^/dev/[A-Za-z0-9._/-]+$")
_SYSTEM_PREFIXES = (
    "/boot",
    "/usr",
    "/var",
    "/opt",
    "/sys",
    "/proc",
    "/dev",
    "/tmp",
    "/run/user",
    "/run/credentials",
    "/run/systemd",
    "/home",
)
_SYSTEM_EXACT = {"/", "/boot", "/boot/efi", "/home", "/var", "/usr", "/opt", "/tmp", "/run"}


@dataclass
class ImportSource:
    """A place the import dialog can browse. path is discovered, never invented."""

    key: str
    title: str
    kind: str
    path: str | None = None
    device: str | None = None
    port: str | None = None
    model: str | None = None
    mounted: bool = True


@dataclass
class ListedPhoto:
    name: str
    label: str
    path: str | None = None
    folder: str | None = None
    index: int | None = None
    size: int | None = None

    @property
    def raw(self) -> bool:
        return is_raw_path(self.name)


@dataclass
class ImportResult:
    dest: str
    root: str | None = None
    copied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    album_id: int | None = None


def default_import_dir() -> Path:
    """Library folder created on import if it does not exist yet."""
    return Path.home() / "Pictures" / "Lumen"


def default_media_dirs() -> list[Path]:
    """Fedora mounts a user's removable disks here. The names underneath are real."""
    user = os.environ.get("USER", "").strip() or getpass.getuser()
    if not user:
        return []
    return [Path("/run/media") / user, Path("/media") / user]


def camera_download_note(binary: str | None = None) -> str | None:
    """None when gphoto2 can be used. Otherwise the sentence shown in the dialog."""
    if binary is None:
        binary = shutil.which("gphoto2")
    if binary:
        return None
    return CAMERA_NOTE


def _is_system_path(path: str) -> bool:
    norm = os.path.normpath(path)
    if norm in _SYSTEM_EXACT:
        return True
    for prefix in _SYSTEM_PREFIXES:
        if norm == prefix or norm.startswith(prefix + os.sep):
            # /run/media and /media are the user mount trees, not system disks.
            if norm.startswith("/run/media/") or norm.startswith("/media/"):
                return False
            return True
    return False


def _decode_bytes(value) -> str:
    if isinstance(value, str):
        return value.split("\x00", 1)[0]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).split(b"\x00", 1)[0].decode("utf-8", "replace")
    if isinstance(value, (list, tuple)):
        try:
            return bytes(value).split(b"\x00", 1)[0].decode("utf-8", "replace")
        except (TypeError, ValueError):
            return ""
    return ""


def _decode_paths(value) -> list[str]:
    if not value:
        return []
    paths: list[str] = []
    for entry in value:
        text = _decode_bytes(entry).strip()
        if text:
            paths.append(text)
    return paths


def volumes_from_media_dirs(media_dirs: list[Path]) -> list[ImportSource]:
    """Each child of /run/media/$USER or /media/$USER is a mounted volume."""
    found: list[ImportSource] = []
    seen: set[str] = set()
    for base in media_dirs:
        try:
            if not base.is_dir():
                continue
            children = list(base.iterdir())
        except OSError:
            continue
        for child in sorted(children, key=lambda p: p.name.lower()):
            if child.name.startswith(".") or not child.is_dir():
                continue
            try:
                resolved = str(child.resolve())
            except OSError:
                resolved = str(child)
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(
                ImportSource(
                    key="vol:" + resolved,
                    title=child.name or resolved,
                    kind="volume",
                    path=resolved,
                    mounted=True,
                )
            )
    return found


def volumes_from_udisks() -> list[ImportSource]:
    """Read mounted (and removable unmounted) filesystems from udisks2.

    Mount paths come from the MountPoints property. This function does not
    guess a path when udisks has not mounted the disk.
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception:
        return []
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        reply = bus.call_sync(
            "org.freedesktop.UDisks2",
            "/org/freedesktop/UDisks2",
            "org.freedesktop.DBus.ObjectManager",
            "GetManagedObjects",
            None,
            GLib.VariantType.new("(a{oa{sa{sv}}})"),
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
        objects = reply.unpack()[0]
    except Exception:
        return []
    found: list[ImportSource] = []
    for _path, ifaces in objects.items():
        if not isinstance(ifaces, dict):
            continue
        block = ifaces.get("org.freedesktop.UDisks2.Block") or {}
        fs = ifaces.get("org.freedesktop.UDisks2.Filesystem") or {}
        if not block or not fs:
            continue
        usage = str(block.get("IdUsage") or "")
        if usage and usage != "filesystem":
            continue
        if block.get("HintIgnore"):
            continue
        drive_path = str(block.get("Drive") or "")
        drive = {}
        if drive_path and drive_path != "/":
            drive = (objects.get(drive_path) or {}).get("org.freedesktop.UDisks2.Drive") or {}
        removable = bool(drive.get("Removable") or drive.get("MediaRemovable"))
        bus_name = str(drive.get("ConnectionBus") or "")
        device = _decode_bytes(block.get("Device") or block.get("PreferredDevice") or "")
        label = str(block.get("IdLabel") or "").strip()
        user = os.environ.get("USER", "").strip() or getpass.getuser()
        mounts = []
        for mount in _decode_paths(fs.get("MountPoints")):
            if not mount or _is_system_path(mount):
                continue
            under_media = mount.startswith(f"/run/media/{user}/") or mount.startswith(f"/media/{user}/")
            hot = removable or bus_name in {"usb", "sdio", "ieee1394", "mmc"}
            # HintSystem disks are ignored unless Fedora mounted them for this user.
            if block.get("HintSystem") and not under_media:
                continue
            if hot or under_media:
                mounts.append(mount)
        if not mounts and not (removable and device and not block.get("HintSystem")):
            continue
        if mounts:
            for mount in mounts:
                if _is_system_path(mount):
                    continue
                title = label or Path(mount).name or device or "Card"
                found.append(
                    ImportSource(
                        key="vol:" + mount,
                        title=title,
                        kind="volume",
                        path=mount,
                        device=device or None,
                        mounted=True,
                    )
                )
        elif removable and device and _DEVICE.match(device):
            title = label or (str(drive.get("Model") or "").strip()) or Path(device).name
            found.append(
                ImportSource(
                    key="dev:" + device,
                    title=title or "Removable disk",
                    kind="volume",
                    path=None,
                    device=device,
                    mounted=False,
                )
            )
    return found


def volumes_from_gio() -> list[ImportSource]:
    """Removable mounts Gio already knows about (gvfs / udisks on Fedora)."""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
    except Exception:
        return []
    try:
        monitor = Gio.VolumeMonitor.get()
        mounts = monitor.get_mounts()
    except Exception:
        return []
    found: list[ImportSource] = []
    for mount in mounts or []:
        try:
            root = mount.get_root()
            path = root.get_path() if root is not None else None
            if not path or _is_system_path(path):
                continue
            drive = mount.get_drive()
            volume = mount.get_volume()
            removable = False
            if drive is not None and (drive.is_removable() or drive.is_media_removable()):
                removable = True
            if volume is not None and volume.can_eject():
                removable = True
            if mount.can_eject():
                removable = True
            user = os.environ.get("USER", "").strip() or getpass.getuser()
            under_media = path.startswith(f"/run/media/{user}/") or path.startswith(f"/media/{user}/")
            if not (removable or under_media):
                continue
            title = mount.get_name() or Path(path).name
            found.append(
                ImportSource(
                    key="vol:" + path,
                    title=title,
                    kind="volume",
                    path=path,
                    mounted=True,
                )
            )
        except Exception:
            continue
    return found


def discover_volumes(
    *,
    media_dirs: list[Path] | None = None,
    query_udisks: bool = True,
    query_gio: bool = True,
) -> list[ImportSource]:
    """Removable volumes that are actually present.

    media_dirs defaults to /run/media/$USER and /media/$USER. Tests pass a
    fake directory instead of reading a real card.
    """
    dirs = default_media_dirs() if media_dirs is None else list(media_dirs)
    merged: dict[str, ImportSource] = {}

    def add(source: ImportSource) -> None:
        if source.path:
            try:
                if Path(source.path).exists():
                    source.path = str(Path(source.path).resolve())
            except OSError:
                pass
            if _is_system_path(source.path):
                return
            source.key = "vol:" + source.path
        key = source.key
        prev = merged.get(key)
        if prev is None:
            merged[key] = source
            return
        if source.title and prev.title == Path(prev.path or "").name:
            prev.title = source.title
        if source.device and not prev.device:
            prev.device = source.device

    for source in volumes_from_media_dirs(dirs):
        add(source)
    if query_udisks:
        for source in volumes_from_udisks():
            add(source)
    if query_gio:
        for source in volumes_from_gio():
            add(source)
    return sorted(merged.values(), key=lambda s: (s.title or "").lower())


def parse_gphoto2_detect(text: str) -> list[tuple[str, str]]:
    """(model, port) rows from `gphoto2 --auto-detect`."""
    cameras: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("model") or set(stripped) <= {"-"}:
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        port = parts[-1]
        if not _GPHOTO_PORT.match(port):
            continue
        model = line[: line.rfind(port)].strip()
        if model:
            cameras.append((model, port))
    return cameras


def parse_gphoto2_list(text: str) -> list[ListedPhoto]:
    """Files from `gphoto2 --list-files`, limited to importable extensions."""
    folder = ""
    photos: list[ListedPhoto] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        folder_match = _LIST_FOLDER.search(line)
        if folder_match and "folder" in line.lower():
            folder = folder_match.group(1)
            continue
        file_match = _LIST_FILE.match(line.strip())
        if not file_match:
            continue
        name = file_match.group(2)
        if Path(name).suffix.lower() not in IMPORT_EXTS:
            continue
        index = int(file_match.group(1))
        label = name if not folder else f"{folder.rstrip('/')}/{name}"
        photos.append(
            ListedPhoto(
                name=name,
                label=label,
                folder=folder,
                index=index,
            )
        )
    return photos


def detect_cameras(binary: str | None = None, runner=None) -> tuple[list[ImportSource], str | None]:
    """Cameras from gphoto2. Missing binary returns the dialog note and no cameras."""
    if binary is None:
        binary = shutil.which("gphoto2")
    if not binary:
        return [], CAMERA_NOTE
    if runner is None:
        def runner(argv, timeout):
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env={**os.environ, "LC_ALL": "C"},
            )

    try:
        proc = runner([binary, "--auto-detect"], 20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], str(exc)
    text = ""
    if hasattr(proc, "stdout"):
        text = (proc.stdout or "") + "\n" + (getattr(proc, "stderr", "") or "")
        code = getattr(proc, "returncode", 0)
    else:
        text = str(proc)
        code = 0
    if code not in (0, None) and not parse_gphoto2_detect(text):
        return [], (text.strip() or "gphoto2 could not look for a camera.")
    sources = []
    for model, port in parse_gphoto2_detect(text):
        sources.append(
            ImportSource(
                key="cam:" + port,
                title=model,
                kind="camera",
                port=port,
                model=model,
                mounted=True,
            )
        )
    return sources, None


def list_folder_photos(root: Path) -> list[ListedPhoto]:
    """Importable files under root. Hidden names are skipped. Links are not followed."""
    root = Path(root)
    photos: list[ListedPhoto] = []
    if not root.is_dir():
        return photos
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() not in IMPORT_EXTS:
                continue
            path = Path(dirpath) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                size = path.stat().st_size
                resolved = path.resolve()
            except OSError:
                continue
            try:
                label = str(resolved.relative_to(root.resolve()))
            except ValueError:
                label = name
            photos.append(
                ListedPhoto(
                    name=name,
                    label=label,
                    path=str(resolved),
                    size=size,
                )
            )
    return photos


def list_camera_photos(port: str, binary: str | None = None, runner=None) -> list[ListedPhoto]:
    if binary is None:
        binary = shutil.which("gphoto2")
    if not binary:
        raise RuntimeError(CAMERA_NOTE)
    argv = [binary, "--port", port, "--list-files"]
    if runner is None:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
        if proc.returncode != 0 and not proc.stdout:
            raise RuntimeError((proc.stderr or proc.stdout or "Could not list the camera.").strip())
        return parse_gphoto2_list(proc.stdout or "")
    proc = runner(argv, 90)
    text = (getattr(proc, "stdout", "") or "")
    return parse_gphoto2_list(text)


def copy_without_overwrite(src: Path, dest: Path) -> bool:
    """Copy src to dest. Return False if dest already exists. Never replace it."""
    src = Path(src)
    dest = Path(dest)
    if src.is_symlink() or not src.is_file():
        return False
    if dest.exists() or dest.is_symlink():
        return False
    try:
        if src.resolve() == dest:
            return False
    except OSError:
        return False
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(dest, flags, 0o644)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
            shutil.copyfileobj(inp, out, length=1024 * 1024)
        try:
            shutil.copystat(src, dest)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise
    return True


def _safe_name(name: str) -> str | None:
    base = Path(name).name
    if not base or base in {".", ".."} or base.startswith(".") or base != name:
        return None
    if Path(base).suffix.lower() not in IMPORT_EXTS:
        return None
    return base


def _containing_root(store: Store, dest: Path) -> str | None:
    dest_r = dest.resolve()
    best: str | None = None
    for root in store.get_roots():
        try:
            root_p = Path(root).resolve()
        except OSError:
            continue
        if dest_r == root_p or root_p in dest_r.parents:
            if best is None or len(str(root_p)) > len(best):
                best = str(root_p)
    return best


def _destination_conflicts(dest: Path, source_roots: list[Path]) -> str | None:
    try:
        dest_r = dest.resolve()
    except OSError:
        return "The destination folder is not available."
    for root in source_roots:
        try:
            root_r = root.resolve()
        except OSError:
            continue
        if dest_r == root_r or root_r in dest_r.parents:
            return "Pick a destination that is not on the card or inside the folder you are importing."
    return None


def import_local_files(
    store: Store,
    files: list[Path],
    dest_dir: Path,
    album_id: int | None = None,
    source_roots: list[Path] | None = None,
) -> ImportResult:
    """Copy files into dest_dir, index them, and optionally add them to an album."""
    dest_dir = Path(dest_dir).expanduser()
    result = ImportResult(dest=str(dest_dir), album_id=album_id)
    conflict = _destination_conflicts(dest_dir, source_roots or [])
    if conflict:
        result.errors.append(conflict)
        return result
    created = not dest_dir.exists()
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.errors.append(str(exc))
        return result
    if not dest_dir.is_dir():
        result.errors.append("Destination is not a folder.")
        return result

    copied_paths: list[Path] = []
    for src in files:
        src = Path(src)
        name = _safe_name(src.name)
        if name is None:
            result.skipped.append(src.name)
            continue
        dest = dest_dir / name
        if dest.exists():
            result.skipped.append(name)
            continue
        try:
            wrote = copy_without_overwrite(src, dest)
        except OSError as exc:
            result.errors.append(f"{name}: {exc}")
            continue
        if not wrote:
            result.skipped.append(name)
            continue
        copied_paths.append(dest)
        result.copied.append(name)

    if not copied_paths:
        if created:
            try:
                dest_dir.rmdir()
            except OSError:
                pass
        return result

    try:
        root = _containing_root(store, dest_dir)
        fresh = root is None
        if fresh:
            root = store.add_root(str(dest_dir))
            store.scan_root(root)
        else:
            for path in copied_paths:
                st = path.stat()
                store.upsert_photo(path.resolve(), Path(root), st.st_mtime_ns)
        result.root = root
        if album_id is not None:
            for path in copied_paths:
                row = store.get_photo_by_path(str(path.resolve()))
                if row is None:
                    result.errors.append(f"{path.name}: copied, but not in the library index")
                    continue
                store.set_membership(int(row["id"]), int(album_id), True)
    except Exception as exc:
        result.errors.append(str(exc))
    return result


def mount_device(device: str) -> str:
    """Mount with udisksctl and return the path it reports. Does not invent one."""
    if not _DEVICE.match(device or ""):
        raise RuntimeError("Refusing to mount an unexpected device path.")
    binary = shutil.which("udisksctl")
    if not binary:
        raise RuntimeError("This card is not mounted, and udisksctl is not available.")
    proc = subprocess.run(
        [binary, "mount", "-b", device],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    match = _MOUNT_AT.search(text)
    if proc.returncode != 0 or not match:
        raise RuntimeError(text or "Could not mount this card.")
    path = match.group(1).rstrip(".")
    if not path.startswith("/"):
        raise RuntimeError(text or "udisksctl did not report a mount path.")
    return path


def download_camera_file(
    port: str,
    folder: str,
    index: int,
    dest: Path,
    binary: str | None = None,
) -> bool:
    """Download one camera file. Return False if dest already exists."""
    dest = Path(dest)
    if dest.exists() or dest.is_symlink():
        return False
    if binary is None:
        binary = shutil.which("gphoto2")
    if not binary:
        raise RuntimeError(CAMERA_NOTE)
    partial = dest.with_name(dest.name + ".lumen-partial")
    if partial.exists():
        try:
            partial.unlink()
        except OSError:
            pass
    argv = [
        binary,
        "--port",
        port,
        "--folder",
        folder or "/",
        "--filename",
        str(partial),
        "--get-file",
        str(int(index)),
    ]
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    if not partial.is_file():
        message = (proc.stderr or proc.stdout or "Camera download failed.").strip()
        raise RuntimeError(message)
    if proc.returncode != 0:
        partial.unlink(missing_ok=True)
        message = (proc.stderr or proc.stdout or "Camera download failed.").strip()
        raise RuntimeError(message)
    try:
        try:
            os.link(partial, dest)
        except FileExistsError:
            return False
        except OSError:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(dest, flags, 0o644)
            with os.fdopen(fd, "wb") as out, partial.open("rb") as inp:
                shutil.copyfileobj(inp, out, length=1024 * 1024)
    finally:
        partial.unlink(missing_ok=True)
    return dest.is_file()


def import_camera_files(
    store: Store,
    photos: list[ListedPhoto],
    dest_dir: Path,
    port: str,
    album_id: int | None = None,
) -> ImportResult:
    """Download selected camera files into dest_dir, then index them like a card."""
    dest_dir = Path(dest_dir).expanduser()
    result = ImportResult(dest=str(dest_dir), album_id=album_id)
    if camera_download_note():
        result.errors.append(CAMERA_NOTE)
        return result
    created = not dest_dir.exists()
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.errors.append(str(exc))
        return result
    staged: list[Path] = []
    for photo in photos:
        name = _safe_name(photo.name)
        if name is None or photo.index is None:
            result.skipped.append(photo.name)
            continue
        dest = dest_dir / name
        if dest.exists():
            result.skipped.append(name)
            continue
        try:
            wrote = download_camera_file(port, photo.folder or "/", photo.index, dest)
        except Exception as exc:
            result.errors.append(f"{name}: {exc}")
            continue
        if not wrote:
            result.skipped.append(name)
            continue
        staged.append(dest)
        result.copied.append(name)
    if not staged:
        if created:
            try:
                dest_dir.rmdir()
            except OSError:
                pass
        return result
    # Files are already in dest. Index without copying them again.
    try:
        root = _containing_root(store, dest_dir)
        fresh = root is None
        if fresh:
            root = store.add_root(str(dest_dir))
            store.scan_root(root)
        else:
            for path in staged:
                st = path.stat()
                store.upsert_photo(path.resolve(), Path(root), st.st_mtime_ns)
        result.root = root
        if album_id is not None:
            for path in staged:
                row = store.get_photo_by_path(str(path.resolve()))
                if row is None:
                    result.errors.append(f"{path.name}: downloaded, but not in the library index")
                    continue
                store.set_membership(int(row["id"]), int(album_id), True)
    except Exception as exc:
        result.errors.append(str(exc))
    return result
