#!/usr/bin/env python3
"""Write a photobook PDF with Gtk.PrintOperation, the same path the app uses.

No print dialog. Safe to run without a window. Does not modify photo files.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("GTK_A11Y", "none")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lumen.imaging import IMAGE_EXTS
from lumen.photobook import load_pages, plan_pages, run_print


def list_photos(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() not in IMAGE_EXTS:
                continue
            found.append(Path(dirpath) / name)
    found.sort(key=lambda p: (p.name.casefold(), str(p).casefold()))
    return found


def main() -> int:
    assert plan_pages(0) == []
    assert plan_pages(1) == [("one", 1)]
    assert plan_pages(2) == [("one", 1), ("two", 1)]
    assert plan_pages(3) == [("one", 1), ("two", 2)]
    assert plan_pages(5) == [("one", 1), ("two", 2), ("one", 1), ("two", 1)]
    assert sum(n for _layout, n in plan_pages(13)) == 13
    assert len(plan_pages(13)) == 9

    sample = ROOT / "sample-photos"
    photos = list_photos(sample)
    print(f"PHOTOS {len(photos)}")
    for path in photos:
        print(f"  {path.relative_to(sample)}")
    items = [(str(path), []) for path in photos]
    pages = load_pages(items, single=False)
    assert [layout for layout, _images in pages] == [layout for layout, _n in plan_pages(len(photos))]
    assert len(pages) == 9

    dest = ROOT / "lumen-photobook.pdf"
    if dest.exists():
        dest.unlink()
    result = run_print(None, pages, str(dest))
    print(f"EXPORT {result} {dest} bytes={dest.stat().st_size}")
    header = dest.read_bytes()[:5]
    if header != b"%PDF-":
        print("NOT_A_PDF", header)
        return 1

    # Leftover two-up page: 2 photos -> page 2 has one photo, not dropped.
    leftover = Path("/tmp/lumen-photobook-leftover.pdf")
    if leftover.exists():
        leftover.unlink()
    two = load_pages(items[:2], single=False)
    assert len(two) == 2 and two[1][0] == "two" and len(two[1][1]) == 1
    run_print(None, two, str(leftover))
    print(f"LEFTOVER {leftover}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
