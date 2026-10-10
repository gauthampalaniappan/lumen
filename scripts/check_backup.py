#!/usr/bin/env python3
"""Back up sample-photos into a temp folder. Does not touch a real library."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(folder: Path) -> dict[str, tuple[int, int, int, str]]:
    found: dict[str, tuple[int, int, int, str]] = {}
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        st = path.stat()
        found[str(path)] = (st.st_ino, st.st_size, st.st_mtime_ns, sha(path))
    return found


def main() -> int:
    samples = ROOT / "sample-photos"
    before = snapshot(samples)
    with tempfile.TemporaryDirectory(prefix="lumen-backup-", dir=os.environ.get("LUMEN_CHECK_TMP", str(ROOT))) as tmp:
        base = Path(tmp)
        os.environ["XDG_CONFIG_HOME"] = str(base / "config")
        os.environ["XDG_DATA_HOME"] = str(base / "data")
        os.environ["XDG_CACHE_HOME"] = str(base / "cache")
        from lumen.backup import backup_library, photo_destination
        from lumen.store import Store

        store = Store()
        root = store.add_root(str(samples))
        counted = store.scan_root(root)
        photos = store.query_photos()
        assert counted == len(photos) > 0, (counted, len(photos))
        photo = photos[0]
        store.set_rating(int(photo["id"]), 4)
        album_id = store.create_album("Trip")
        store.set_membership(int(photo["id"]), album_id, True)
        store.set_tags(int(photo["id"]), "harbor, test")

        dest = base / "backup-dest"
        first = backup_library(store, dest)
        assert first.copied == len(photos), first
        assert first.skipped == 0, first
        assert first.failed == 0, first
        assert first.database_ok, first
        db_path = dest / "library.db"
        assert db_path.is_file()
        for item in photos:
            target = photo_destination(item, dest, [root])
            source = Path(item["path"])
            assert target.is_file(), target
            assert target.stat().st_size == source.stat().st_size
            assert sha(target) == sha(source)
        nested = dest / "photos" / "sample-photos" / "trips" / "2024" / "green-trail.jpg"
        assert nested.is_file(), nested
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT rating FROM photos WHERE path = ?", (photo["path"],)
            ).fetchone()
            assert row is not None and int(row[0]) == 4, row
            album = conn.execute(
                "SELECT name FROM albums WHERE name = 'Trip'"
            ).fetchone()
            assert album is not None
            tag = conn.execute(
                "SELECT name FROM tags WHERE name = 'harbor'"
            ).fetchone()
            assert tag is not None

        after_first = snapshot(samples)
        assert after_first == before, "originals changed on first backup"

        second = backup_library(store, dest)
        assert second.copied == 0, second
        assert second.skipped == len(photos), second
        assert second.failed == 0, second
        assert second.database_ok, second
        assert snapshot(samples) == before, "originals changed on second backup"

        newer = nested
        newer_bytes = b"newer-than-source-not-the-photo"
        newer.write_bytes(newer_bytes)
        future = time.time() + 3600
        os.utime(newer, (future, future))
        kept = backup_library(store, dest)
        assert kept.copied == 0, kept
        assert kept.skipped == len(photos), kept
        assert newer.read_bytes() == newer_bytes
        assert snapshot(samples) == before

        older = dest / "photos" / "sample-photos" / "blue-door.jpg"
        assert older.is_file()
        older.write_bytes(b"short")
        os.utime(older, (1, 1))
        replaced = backup_library(store, dest)
        assert replaced.copied == 1, replaced
        assert replaced.skipped == len(photos) - 1, replaced
        assert replaced.failed == 0, replaced
        original = samples / "blue-door.jpg"
        assert sha(older) == sha(original)
        assert snapshot(samples) == before

        missing = root + "/does-not-exist.jpg"
        store.conn.execute(
            """INSERT INTO photos (path, root, folder, filename, mtime_ns, rating)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (missing, root, root, "does-not-exist.jpg", 0),
        )
        store.conn.commit()
        failed = backup_library(store, dest)
        assert failed.failed == 1, failed
        assert failed.copied == 0, failed
        assert any("does-not-exist.jpg" in err for err in failed.errors), failed.errors
        assert snapshot(samples) == before
        assert not list(dest.rglob("*.lumen-partial"))
        assert not list(dest.rglob("*.partial"))

        remembered = store.set_backup_dir(str(dest))
        assert store.get_backup_dir() == remembered
        assert Path(remembered) == dest.resolve()
        config = Path(os.environ["XDG_CONFIG_HOME"]) / "lumen" / "config.json"
        assert '"backup_dir"' in config.read_text(encoding="utf-8")

        live = store.paths.db.resolve()
        try:
            store.export_database(live)
        except ValueError:
            pass
        else:
            raise SystemExit("export_database replaced the live database")
        assert store.paths.db.is_file()

    print("BACKUP_OK", len(before), "sample files unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
