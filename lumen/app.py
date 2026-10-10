"""Command line entry for Lumen."""

from __future__ import annotations

import argparse
import os
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lumen",
        description="Lumen — a small photo organizer for local folders.",
    )
    parser.add_argument(
        "--library",
        action="append",
        default=[],
        metavar="DIR",
        help="Photo folder to remember and scan (repeatable). Saved in the config file.",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="After the first scan, rate the first photo 4 stars and print SELF_CHECK. For smoke tests.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("GTK_A11Y", "none")
    args = build_parser().parse_args(argv)
    for folder in args.library:
        expanded = os.path.expanduser(folder)
        if not os.path.isdir(expanded):
            print(f"lumen: not a directory: {folder}", file=sys.stderr)
            return 2

    from lumen.ui import LumenApp

    app = LumenApp(libraries=args.library, self_check=args.self_check)
    return int(app.run([sys.argv[0]]))
