#!/usr/bin/env python3
"""Import from a fake card without touching a real photo library."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


DETECT = """Model                          Port
----------------------------------------------------------
Canon EOS 5D                   usb:001,012
"""

LISTING = """There is no file in folder '/'.
There are 3 files in folder '/store_00010001/DCIM/100CANON'.
#1     IMG_0001.JPG               rd  10 KB image/jpeg
#2     IMG_0002.CR2               rd  20 KB image/x-canon-cr2
#3     notes.TXT                  rd   1 KB text/plain
"""


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lumen-import-", dir=os.environ.get("LUMEN_CHECK_TMP", str(ROOT))) as tmp:
        base = Path(tmp)
        os.environ["XDG_CONFIG_HOME"] = str(base / "config")
        os.environ["XDG_DATA_HOME"] = str(base / "data")
        os.environ["XDG_CACHE_HOME"] = str(base / "cache")
        from lumen.importing import (
            CAMERA_NOTE,
            camera_download_note,
            copy_without_overwrite,
            detect_cameras,
            discover_volumes,
            import_local_files,
            list_folder_photos,
            parse_gphoto2_detect,
            parse_gphoto2_list,
        )
        from lumen.store import Store

        samples = ROOT / "sample-photos"
        media = base / "run-media" / "tester"
        media.mkdir(parents=True)
        first = discover_volumes(media_dirs=[media], query_udisks=False, query_gio=False)
        assert first == [], first

        card = media / "CARD"
        folder = card / "DCIM" / "100CANON"
        folder.mkdir(parents=True)
        jpg = folder / "harbor-morning.jpg"
        other = folder / "blue-door.jpg"
        raw = folder / "shot.CR2"
        shutil.copy(samples / "harbor-morning.jpg", jpg)
        shutil.copy(samples / "blue-door.jpg", other)
        raw.write_bytes(b"not-a-real-raw-just-bytes")
        (folder / "notes.txt").write_text("skip me\n", encoding="utf-8")
        (card / ".secret.jpg").write_bytes(b"hidden")
        hidden = card / ".hidden"
        hidden.mkdir()
        (hidden / "nope.jpg").write_bytes(b"nope")
        shutil.copy(samples / "formats" / "tiny.gif", folder / "tiny.gif")

        appeared = discover_volumes(media_dirs=[media], query_udisks=False, query_gio=False)
        assert len(appeared) == 1, appeared
        assert appeared[0].path == str(card.resolve())
        assert appeared[0].title == "CARD"
        assert appeared[0].kind == "volume"

        listed = list_folder_photos(card)
        names = {item.name for item in listed}
        assert names == {"harbor-morning.jpg", "blue-door.jpg", "shot.CR2"}, names
        assert all(item.path and Path(item.path).is_file() for item in listed)

        before = {path: sha(path) for path in (jpg, other, raw)}
        store = Store()
        album_id = store.create_album("Trip")
        dest = base / "library" / "Lumen"
        # A name collision must stay byte-for-byte.
        dest.mkdir(parents=True)
        kept = dest / "harbor-morning.jpg"
        kept.write_bytes(b"already here")
        kept_hash = sha(kept)

        result = import_local_files(
            store,
            [Path(item.path) for item in listed],
            dest,
            album_id,
            source_roots=[card],
        )
        assert result.errors == [], result.errors
        assert result.root == str(dest.resolve())
        assert sorted(result.copied) == ["blue-door.jpg", "shot.CR2"]
        assert result.skipped == ["harbor-morning.jpg"]
        assert sha(kept) == kept_hash
        assert kept.read_bytes() == b"already here"
        for path, digest in before.items():
            assert sha(path) == digest, path
        assert raw.read_bytes() == b"not-a-real-raw-just-bytes"
        copied_raw = dest / "shot.CR2"
        assert copied_raw.read_bytes() == b"not-a-real-raw-just-bytes"
        assert (dest / "blue-door.jpg").is_file()
        assert not (dest / "tiny.gif").exists()
        assert not (dest / "notes.txt").exists()

        members = store.query_photos(album_id=album_id)
        member_names = sorted(p["filename"] for p in members)
        assert member_names == ["blue-door.jpg", "shot.CR2"], member_names
        assert store.get_roots() == [str(dest.resolve())]
        # The skipped original is indexed because it was already in the new folder,
        # but it was not added to the album.
        assert store.get_photo_by_path(str(kept.resolve())) is not None

        # A second import of the same names must not overwrite the copies.
        door_hash = sha(dest / "blue-door.jpg")
        again = import_local_files(
            store,
            [other, raw],
            dest,
            album_id,
            source_roots=[card],
        )
        assert again.copied == []
        assert set(again.skipped) == {"blue-door.jpg", "shot.CR2"}
        assert sha(dest / "blue-door.jpg") == door_hash
        assert sha(other) == before[other]

        clash = dest / "blue-door.jpg"
        assert copy_without_overwrite(other, clash) is False
        assert sha(clash) == door_hash

        inside = import_local_files(store, [other], card / "DCIM", None, source_roots=[card])
        assert inside.copied == []
        assert inside.errors

        assert camera_download_note(binary="") == CAMERA_NOTE
        cameras, note = detect_cameras(binary="")
        assert cameras == [] and note == CAMERA_NOTE
        parsed = parse_gphoto2_detect(DETECT)
        assert parsed == [("Canon EOS 5D", "usb:001,012")], parsed
        listed_cam = parse_gphoto2_list(LISTING)
        assert [p.name for p in listed_cam] == ["IMG_0001.JPG", "IMG_0002.CR2"]
        assert listed_cam[0].folder.endswith("100CANON")
        assert listed_cam[1].raw

        print("IMPORT_CHECK ok")
        print(f"copied {result.copied} skipped {result.skipped} album {member_names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
