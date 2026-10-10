"""SQLite library, folder roots, albums, tags, ratings, and edit lists."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from lumen.imaging import INDEX_EXTS, clamp_tone, neutral_tone, tone_is_neutral
from lumen.paths import Paths

THUMB_SIZE_MIN = 96
THUMB_SIZE_MAX = 320
THUMB_SIZE_DEFAULT = 160


def clamp_thumb_size(value) -> int:
    """Nearest integer inside the thumbnail slider range."""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return THUMB_SIZE_DEFAULT
    return max(THUMB_SIZE_MIN, min(THUMB_SIZE_MAX, number))

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    root TEXT NOT NULL,
    folder TEXT NOT NULL,
    filename TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    rating INTEGER NOT NULL DEFAULT 0 CHECK (rating >= 0 AND rating <= 5)
);
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS album_photos (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    PRIMARY KEY (album_id, photo_id)
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS photo_tags (
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (photo_id, tag_id)
);
CREATE TABLE IF NOT EXISTS edits (
    id INTEGER PRIMARY KEY,
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    op TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_photos_root ON photos(root);
CREATE INDEX IF NOT EXISTS idx_photos_folder ON photos(folder);
CREATE INDEX IF NOT EXISTS idx_photos_rating ON photos(rating);
"""


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Store:
    def __init__(self, paths: Paths | None = None) -> None:
        self.paths = paths or Paths()
        self.paths.ensure()
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.paths.db, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def get_roots(self) -> list[str]:
        with self.lock:
            return list(self._read_roots())

    def add_root(self, path: str) -> str:
        resolved = str(Path(path).expanduser().resolve())
        if not Path(resolved).is_dir():
            raise NotADirectoryError(resolved)
        with self.lock:
            roots = self._read_roots()
            if resolved not in roots:
                roots.append(resolved)
                self._write_roots(roots)
        return resolved

    def remove_root(self, path: str) -> None:
        resolved = str(Path(path).expanduser().resolve())
        with self.lock:
            roots = [r for r in self._read_roots() if r != resolved]
            self._write_roots(roots)
            self.conn.execute("DELETE FROM photos WHERE root = ?", (resolved,))
            self.conn.commit()

    def _read_config(self) -> dict:
        cfg = self.paths.config_file
        if not cfg.is_file():
            return {}
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_config(self, data: dict) -> None:
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2) + "\n"
        tmp = self.paths.config_file.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.paths.config_file)

    def _read_roots(self) -> list[str]:
        roots = self._read_config().get("roots") or []
        return [str(r) for r in roots if isinstance(r, str)]

    def _write_roots(self, roots: list[str]) -> None:
        data = self._read_config()
        data["roots"] = roots
        self._write_config(data)

    def get_theme(self) -> str:
        """system, light, or dark. Unknown values fall back to system."""
        with self.lock:
            value = self._read_config().get("theme", "system")
        if value not in ("system", "light", "dark"):
            return "system"
        return str(value)

    def set_theme(self, theme: str) -> None:
        if theme not in ("system", "light", "dark"):
            raise ValueError(theme)
        with self.lock:
            data = self._read_config()
            data["theme"] = theme
            self._write_config(data)

    def get_thumb_size(self) -> int:
        """Thumbnail tile width in pixels. Unknown values fall back to the default."""
        with self.lock:
            value = self._read_config().get("thumb_size", THUMB_SIZE_DEFAULT)
        return clamp_thumb_size(value)

    def set_thumb_size(self, size: int) -> None:
        size = clamp_thumb_size(size)
        with self.lock:
            data = self._read_config()
            data["thumb_size"] = size
            self._write_config(data)

    def get_backup_dir(self) -> str:
        """Last backup folder, or empty when none has been chosen."""
        with self.lock:
            value = self._read_config().get("backup_dir", "")
        return value if isinstance(value, str) else ""

    def set_backup_dir(self, path: str) -> str:
        resolved = str(Path(path).expanduser().resolve())
        with self.lock:
            data = self._read_config()
            data["backup_dir"] = resolved
            self._write_config(data)
        return resolved

    def export_database(self, dest: Path) -> None:
        """Write a consistent snapshot of the library database to dest.

        Uses the SQLite backup API so the open library file, its WAL, and
        the photo originals are left as they are. Refuses to replace the
        live database.
        """
        dest = Path(dest)
        live = self.paths.db.resolve()
        if dest.exists():
            try:
                if dest.resolve() == live or os.path.samefile(dest, live):
                    raise ValueError("Refusing to overwrite the live library database")
            except OSError:
                if dest.resolve() == live:
                    raise ValueError("Refusing to overwrite the live library database")
        elif dest.resolve() == live:
            raise ValueError("Refusing to overwrite the live library database")
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".partial")
        if partial.exists():
            try:
                if partial.resolve() == live or (
                    partial.exists() and os.path.samefile(partial, live)
                ):
                    raise ValueError("Refusing to overwrite the live library database")
            except OSError:
                pass
            partial.unlink()
        with self.lock:
            src = sqlite3.connect(self.paths.db)
            try:
                dst = sqlite3.connect(partial)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            if partial.resolve() == live:
                raise ValueError("Refusing to overwrite the live library database")
            os.replace(partial, dest)

    def scan_root(self, root: str) -> int:
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir():
            return 0
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                if Path(name).suffix.lower() not in INDEX_EXTS:
                    continue
                path = Path(dirpath) / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                resolved = path.resolve()
                self.upsert_photo(resolved, root_path, st.st_mtime_ns)
                found.append(str(resolved))
        self.prune_root(str(root_path), found)
        return len(found)

    def upsert_photo(self, path: Path, root: Path, mtime_ns: int) -> None:
        path_s = str(path)
        root_s = str(root)
        folder_s = str(path.parent)
        with self.lock:
            row = self.conn.execute(
                "SELECT id, mtime_ns, folder, filename, root FROM photos WHERE path = ?",
                (path_s,),
            ).fetchone()
            if row is None:
                self.conn.execute(
                    """INSERT INTO photos (path, root, folder, filename, mtime_ns, rating)
                       VALUES (?, ?, ?, ?, ?, 0)""",
                    (path_s, root_s, folder_s, path.name, int(mtime_ns)),
                )
            elif (
                row["mtime_ns"] != int(mtime_ns)
                or row["folder"] != folder_s
                or row["filename"] != path.name
                or row["root"] != root_s
            ):
                self.conn.execute(
                    """UPDATE photos SET root = ?, folder = ?, filename = ?, mtime_ns = ?
                       WHERE id = ?""",
                    (root_s, folder_s, path.name, int(mtime_ns), row["id"]),
                )
            self.conn.commit()

    def prune_root(self, root: str, found: list[str]) -> None:
        keep = set(found)
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, path FROM photos WHERE root = ?", (root,)
            ).fetchall()
            for row in rows:
                if row["path"] not in keep:
                    self.conn.execute("DELETE FROM photos WHERE id = ?", (row["id"],))
            self.conn.commit()

    def get_photo(self, photo_id: int) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        return dict(row) if row else None

    def get_photo_by_path(self, path: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM photos WHERE path = ?", (str(path),)).fetchone()
        return dict(row) if row else None

    def folders(self) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT DISTINCT folder FROM photos ORDER BY folder COLLATE NOCASE"
            ).fetchall()
        return [r["folder"] for r in rows]

    def count_photos(self, *, in_any_album: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM photos p"
        if in_any_album:
            sql += " WHERE EXISTS (SELECT 1 FROM album_photos ap WHERE ap.photo_id = p.id)"
        with self.lock:
            row = self.conn.execute(sql).fetchone()
        return int(row["n"])

    def grouped_counts(self, column: str) -> dict[str, int]:
        """Photo counts keyed by root or folder path."""
        if column not in ("root", "folder"):
            raise ValueError(column)
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {column} AS key, COUNT(*) AS n FROM photos GROUP BY {column}"
            ).fetchall()
        return {str(row["key"]): int(row["n"]) for row in rows}

    def query_photos(
        self,
        *,
        root: str | None = None,
        folder: str | None = None,
        album_id: int | None = None,
        in_any_album: bool = False,
        min_rating: int = 0,
        text: str = "",
    ) -> list[dict]:
        where: list[str] = []
        params: list[object] = []
        if root is not None:
            where.append("p.root = ?")
            params.append(root)
        if folder is not None:
            where.append("p.folder = ?")
            params.append(folder)
        if album_id is not None:
            where.append(
                """EXISTS (
                    SELECT 1 FROM album_photos ap
                    WHERE ap.photo_id = p.id AND ap.album_id = ?
                )"""
            )
            params.append(album_id)
        if in_any_album:
            where.append(
                """EXISTS (
                    SELECT 1 FROM album_photos ap
                    WHERE ap.photo_id = p.id
                )"""
            )
        if min_rating:
            where.append("p.rating >= ?")
            params.append(int(min_rating))
        needle = text.strip()
        if needle:
            like = "%" + _escape_like(needle) + "%"
            where.append(
                """(
                    p.filename LIKE ? ESCAPE '\\'
                    OR p.folder LIKE ? ESCAPE '\\'
                    OR p.root LIKE ? ESCAPE '\\'
                    OR EXISTS (
                        SELECT 1 FROM album_photos ap
                        JOIN albums a ON a.id = ap.album_id
                        WHERE ap.photo_id = p.id AND a.name LIKE ? ESCAPE '\\'
                    )
                    OR EXISTS (
                        SELECT 1 FROM photo_tags pt
                        JOIN tags t ON t.id = pt.tag_id
                        WHERE pt.photo_id = p.id AND t.name LIKE ? ESCAPE '\\'
                    )
                )"""
            )
            params.extend([like, like, like, like, like])
        sql = "SELECT p.* FROM photos p"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY p.filename COLLATE NOCASE, p.path COLLATE NOCASE"
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def set_rating(self, photo_id: int, rating: int) -> None:
        rating = int(rating)
        if rating < 0 or rating > 5:
            raise ValueError("rating must be between 0 and 5")
        with self.lock:
            cur = self.conn.execute(
                "UPDATE photos SET rating = ? WHERE id = ?", (rating, photo_id)
            )
            self.conn.commit()
            if cur.rowcount != 1:
                raise KeyError(photo_id)

    def get_tags(self, photo_id: int) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                """SELECT t.name FROM tags t
                   JOIN photo_tags pt ON pt.tag_id = t.id
                   WHERE pt.photo_id = ?
                   ORDER BY t.name COLLATE NOCASE""",
                (photo_id,),
            ).fetchall()
        return [r["name"] for r in rows]

    def set_tags(self, photo_id: int, text: str) -> None:
        names: list[str] = []
        seen: set[str] = set()
        for part in text.split(","):
            name = part.strip()
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
        with self.lock:
            self.conn.execute("DELETE FROM photo_tags WHERE photo_id = ?", (photo_id,))
            for name in names:
                row = self.conn.execute(
                    "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)
                ).fetchone()
                if row:
                    tag_id = row["id"]
                else:
                    cur = self.conn.execute("INSERT INTO tags (name) VALUES (?)", (name,))
                    tag_id = cur.lastrowid
                self.conn.execute(
                    "INSERT OR IGNORE INTO photo_tags (photo_id, tag_id) VALUES (?, ?)",
                    (photo_id, tag_id),
                )
            self.conn.execute(
                "DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM photo_tags)"
            )
            self.conn.commit()

    def albums(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                """SELECT a.id, a.name, COUNT(ap.photo_id) AS n
                   FROM albums a
                   LEFT JOIN album_photos ap ON ap.album_id = a.id
                   GROUP BY a.id
                   ORDER BY a.name COLLATE NOCASE"""
            ).fetchall()
        return [dict(r) for r in rows]

    def create_album(self, name: str) -> int:
        name = name.strip()
        if not name:
            raise ValueError("Album name is empty")
        with self.lock:
            row = self.conn.execute(
                "SELECT id FROM albums WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone()
            if row:
                return int(row["id"])
            cur = self.conn.execute("INSERT INTO albums (name) VALUES (?)", (name,))
            self.conn.commit()
            return int(cur.lastrowid)

    def delete_album(self, album_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
            self.conn.commit()

    def album_ids_for_photo(self, photo_id: int) -> set[int]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT album_id FROM album_photos WHERE photo_id = ?", (photo_id,)
            ).fetchall()
        return {int(r["album_id"]) for r in rows}

    def set_membership(self, photo_id: int, album_id: int, member: bool) -> None:
        with self.lock:
            if member:
                self.conn.execute(
                    "INSERT OR IGNORE INTO album_photos (album_id, photo_id) VALUES (?, ?)",
                    (album_id, photo_id),
                )
            else:
                self.conn.execute(
                    "DELETE FROM album_photos WHERE album_id = ? AND photo_id = ?",
                    (album_id, photo_id),
                )
            self.conn.commit()

    def get_edits(self, photo_id: int) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT op, params FROM edits WHERE photo_id = ? ORDER BY seq, id",
                (photo_id,),
            ).fetchall()
        ops = []
        for row in rows:
            params = json.loads(row["params"] or "{}")
            if not isinstance(params, dict):
                params = {}
            ops.append({"op": row["op"], **params})
        return ops

    def add_edit(self, photo_id: int, op: str, params: dict | None = None) -> None:
        params = params or {}
        with self.lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM edits WHERE photo_id = ?",
                (photo_id,),
            ).fetchone()
            self.conn.execute(
                "INSERT INTO edits (photo_id, seq, op, params) VALUES (?, ?, ?, ?)",
                (photo_id, int(row["n"]), op, json.dumps(params)),
            )
            self.conn.commit()

    def clear_edits(self, photo_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM edits WHERE photo_id = ?", (photo_id,))
            self.conn.commit()

    def get_tone(self, photo_id: int) -> dict[str, float]:
        """Current adjustment sliders. Missing or neutral tone is all zeros."""
        tone = neutral_tone()
        for op in self.get_edits(photo_id):
            if op.get("op") == "tone":
                tone = clamp_tone(op)
        return tone

    def set_tone(self, photo_id: int, values: dict | None) -> None:
        """Replace the single tone edit. Neutral values remove it.

        Rotate, crop, and enhance rows are left where they are. A tone row
        keeps its place in the list so later rotates still happen after it.
        """
        clean = clamp_tone(values)
        payload = json.dumps(clean)
        with self.lock:
            rows = self.conn.execute(
                "SELECT id FROM edits WHERE photo_id = ? AND op = 'tone' ORDER BY seq, id",
                (photo_id,),
            ).fetchall()
            if tone_is_neutral(clean):
                if rows:
                    self.conn.execute(
                        "DELETE FROM edits WHERE photo_id = ? AND op = 'tone'",
                        (photo_id,),
                    )
                    self.conn.commit()
                return
            if rows:
                keep = int(rows[0]["id"])
                self.conn.execute(
                    "UPDATE edits SET params = ? WHERE id = ?",
                    (payload, keep),
                )
                if len(rows) > 1:
                    self.conn.execute(
                        "DELETE FROM edits WHERE photo_id = ? AND op = 'tone' AND id != ?",
                        (photo_id, keep),
                    )
            else:
                row = self.conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM edits WHERE photo_id = ?",
                    (photo_id,),
                ).fetchone()
                self.conn.execute(
                    "INSERT INTO edits (photo_id, seq, op, params) VALUES (?, ?, 'tone', ?)",
                    (photo_id, int(row["n"]), payload),
                )
            self.conn.commit()
