#!/usr/bin/env python3
"""Run Lumen from a source checkout. Also the entry point for packaged builds."""

import os
import sys

os.environ.setdefault("GTK_A11Y", "none")
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _log_to_file_without_console() -> None:
    """Windowed Windows builds have no console, so stdout/stderr are None.

    Send them to lumen.log in the cache folder instead, so errors (and the
    --self-check line used by release smoke tests) are not lost.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        from lumen.paths import Paths

        paths = Paths()
        paths.cache.mkdir(parents=True, exist_ok=True)
        log = open(paths.cache / "lumen.log", "w", encoding="utf-8", buffering=1)
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


_log_to_file_without_console()

from lumen.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
