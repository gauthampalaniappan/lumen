"""Copy the indexed library to a folder. Originals are never modified."""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from lumen.store import Store


@dataclass
class BackupResult:
    copied: int = 0
    skipped: int = 0
    failed: int = 0
    total: int = 0
    destination: str = ""
    database_ok: bool = False
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"Copied {self.copied}, skipped {self.skipped}, failed {self.failed}."
        if self.database_ok:
            text += " Library database saved."
        elif self.errors:
            text += " Library database was not saved."
        return text


def format_progress(done: int, total: int, copied: int, skipped: int, failed: int) -> str:
    return f"{done} / {total} · copied {copied}, skipped {skipped}, failed {failed}"


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def root_folder_name(root: str, roots: list[str]) -> str:
    """Folder name for one library root. Duplicate basenames get a stable suffix."""
    name = Path(root).name or "photos"
    basenames = [(Path(item).name or "photos") for item in roots]
    if basenames.count(name) > 1:
        digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:8]
        return f"{name}-{digest}"
    return name


def photo_destination(photo: dict, destination: Path, roots: list[str]) -> Path:
    """Path under destination/photos that mirrors the file inside its root."""
    root = str(photo.get("root") or "")
    source = Path(str(photo.get("path") or ""))
    try:
        relative = source.resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        relative = Path(source.name)
    parts = [part for part in relative.parts if part not in ("", ".", "..")]
    if not parts:
        parts = [source.name or "photo"]
    folder = root_folder_name(root, roots) if root else "photos"
    return destination / "photos" / folder / Path(*parts)


def _decide(source: Path, dest: Path) -> str:
    """Return copied, skipped, or raise. Never writes to source."""
    if not source.is_file():
        raise FileNotFoundError(source)
    if _same_file(source, dest):
        return "skipped"
    if dest.exists() or dest.is_symlink():
        if not dest.is_file():
            raise IsADirectoryError(str(dest))
        source_stat = source.stat()
        dest_stat = dest.stat()
        if dest_stat.st_size == source_stat.st_size:
            return "skipped"
        if dest_stat.st_mtime_ns > source_stat.st_mtime_ns:
            return "skipped"
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".lumen-partial")
    if partial.exists() and _same_file(partial, source):
        raise ValueError(f"Refusing to write {source} onto itself")
    try:
        if partial.exists():
            partial.unlink()
        shutil.copy2(source, partial)
        if _same_file(partial, source):
            raise ValueError(f"Refusing to write {source} onto itself")
        os.replace(partial, dest)
    except Exception:
        try:
            if partial.exists() and not _same_file(partial, source):
                partial.unlink()
        except OSError:
            pass
        raise
    return "copied"


def backup_library(store: Store, destination: str | Path, progress=None) -> BackupResult:
    """Copy every indexed photo and a snapshot of the library database.

    Files already at the destination with the same size are skipped. A
    destination file that is newer than the original is left in place even
    when the size differs. Nothing in this function deletes or overwrites
    an original photo.
    """
    dest = Path(destination).expanduser().resolve()
    result = BackupResult(destination=str(dest))
    if dest.exists() and not dest.is_dir():
        raise NotADirectoryError(str(dest))
    live_db = store.paths.db.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    photos = store.query_photos()
    roots = sorted({str(photo.get("root") or "") for photo in photos if photo.get("root")})
    result.total = len(photos)
    if progress is not None:
        progress(0, result.total, 0, 0, 0)
    for index, photo in enumerate(photos, start=1):
        source = Path(str(photo.get("path") or ""))
        try:
            target = photo_destination(photo, dest, roots)
            if not _inside(target, dest):
                raise ValueError("Backup path escaped the destination")
            if target.resolve() == live_db:
                raise ValueError("Refusing to overwrite the live library database")
            outcome = _decide(source, target)
        except Exception as exc:
            result.failed += 1
            if len(result.errors) < 8:
                result.errors.append(f"{source}: {exc}")
        else:
            if outcome == "copied":
                result.copied += 1
            else:
                result.skipped += 1
        if progress is not None:
            progress(index, result.total, result.copied, result.skipped, result.failed)
    db_path = dest / "library.db"
    try:
        if db_path.exists() and _same_file(db_path, live_db):
            raise ValueError("Refusing to overwrite the live library database")
        store.export_database(db_path)
    except Exception as exc:
        result.database_ok = False
        if len(result.errors) < 8:
            result.errors.append(f"library database: {exc}")
    else:
        result.database_ok = True
    return result
