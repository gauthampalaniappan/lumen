"""Find Darktable and GIMP: PATH, Flatpak exports on Linux, and the default
install folders on Windows and macOS."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Desktop launchers do not put Flatpak export dirs on PATH. The programs are
# still installed: the export bin is a wrapper that runs `flatpak run <app-id>`.
FLATPAK_APP_IDS = {
    "darktable": "org.darktable.Darktable",
    "gimp": "org.gimp.GIMP",
}

# A distro package sometimes installs a versioned name next to `gimp`.
EXTRA_NAMES = {
    "gimp": ("gimp-3.2", "gimp-3.0", "gimp-2.10"),
}


def _candidate_dirs() -> list[Path]:
    home = Path.home()
    ordered: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = os.path.normpath(str(path))
        if not key or key in seen:
            return
        seen.add(key)
        ordered.append(Path(key))

    for part in os.environ.get("PATH", "").split(os.pathsep):
        if part:
            add(Path(part))

    for part in os.environ.get("XDG_DATA_DIRS", "").split(os.pathsep):
        if not part:
            continue
        data = Path(part)
        # .../flatpak/exports/share -> .../flatpak/exports/bin
        if data.name == "share":
            add(data.parent / "bin")

    for path in (
        Path("/usr/bin"),
        Path("/usr/local/bin"),
        Path("/bin"),
        home / ".local" / "bin",
        home / "bin",
        Path("/var/lib/flatpak/exports/bin"),
        home / ".local/share/flatpak/exports/bin",
    ):
        add(path)
    return ordered


def _names_for(command: str) -> list[str]:
    names = [command, *EXTRA_NAMES.get(command, ())]
    app_id = FLATPAK_APP_IDS.get(command)
    if app_id:
        names.append(app_id)
    return names


def _executable(path: Path) -> str | None:
    try:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
    except OSError:
        return None
    return None


def _windows_installs(command: str) -> list[Path]:
    """Default installer locations, e.g. C:\\Program Files\\GIMP 3\\bin\\gimp-3.exe."""
    roots: list[Path] = []
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
        value = os.environ.get(env, "").strip()
        if value:
            roots.append(Path(value))
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        roots.append(Path(local) / "Programs")
    found: list[Path] = []
    for root in roots:
        if command == "gimp":
            matches = sorted(root.glob("GIMP*/bin/gimp*.exe"), reverse=True)
            found.extend(
                m for m in matches if "console" not in m.name.lower() and "debug" not in m.name.lower()
            )
        else:
            found.extend(root.glob(f"{command}/bin/{command}.exe"))
    return found


def _macos_installs(command: str) -> list[Path]:
    """App bundles in /Applications or ~/Applications."""
    found: list[Path] = []
    for apps in (Path("/Applications"), Path.home() / "Applications"):
        if command == "gimp":
            for bundle in sorted(apps.glob("GIMP*.app"), reverse=True):
                found.extend(sorted(bundle.glob("Contents/MacOS/gimp*")))
        else:
            found.extend(apps.glob(f"{command}.app/Contents/MacOS/{command}"))
    return found


def _platform_installs(command: str) -> list[Path]:
    if sys.platform == "win32":
        return _windows_installs(command)
    if sys.platform == "darwin":
        return _macos_installs(command)
    return []


def resolve_editor(command: str) -> str | None:
    """Absolute executable for `command`, or None if it is not installed.

    Checked on each call so an editor installed after Lumen started is noticed
    the next time the Edit in menu opens.
    """
    names = _names_for(command)
    for directory in _candidate_dirs():
        for name in names:
            found = _executable(directory / name)
            if found:
                return found

    search = os.pathsep.join(str(directory) for directory in _candidate_dirs())
    for name in names:
        found = shutil.which(name, path=search)
        if found:
            return str(Path(found).resolve())

    try:
        for candidate in _platform_installs(command):
            found = _executable(candidate)
            if found:
                return found
    except OSError:
        return None
    return None
