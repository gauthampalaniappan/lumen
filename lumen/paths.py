"""Where Lumen keeps its config, library database and caches.

Linux follows the XDG base directory spec. Windows and macOS use their usual
per-user folders. On every platform the XDG_* variables still win when they
are set, so the headless checks can point Lumen at temporary folders.
No fixed home directory is baked in.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _env_path(env: str) -> Path | None:
    value = os.environ.get(env, "").strip()
    return Path(value) if value else None


def _platform_defaults(home: Path) -> tuple[Path, Path, Path]:
    """(data root, config root, cache root) before the "lumen" folder is added."""
    if sys.platform == "win32":
        roaming = _env_path("APPDATA") or home / "AppData" / "Roaming"
        local = _env_path("LOCALAPPDATA") or home / "AppData" / "Local"
        # Library and caches stay on this machine; settings may roam.
        return local, roaming, local / "Lumen" / "Cache"
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        return support, support, home / "Library" / "Caches"
    return home / ".local" / "share", home / ".config", home / ".cache"


class Paths:
    def __init__(self) -> None:
        home = Path.home()
        data_root, config_root, cache_root = _platform_defaults(home)
        native = sys.platform in ("win32", "darwin")
        name = "Lumen" if native else "lumen"

        xdg_data = _env_path("XDG_DATA_HOME")
        xdg_config = _env_path("XDG_CONFIG_HOME")
        xdg_cache = _env_path("XDG_CACHE_HOME")

        self.data = xdg_data / "lumen" if xdg_data else data_root / name
        self.config_dir = xdg_config / "lumen" if xdg_config else config_root / name
        if xdg_cache:
            self.cache = xdg_cache / "lumen"
        elif sys.platform == "win32":
            self.cache = cache_root  # already ...\Lumen\Cache
        else:
            self.cache = cache_root / name
        self.db = self.data / "library.db"
        self.config_file = self.config_dir / "config.json"
        self.thumbs = self.cache / "thumbs"
        self.edit_export = self.cache / "edit-export"

    def ensure(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.thumbs.mkdir(parents=True, exist_ok=True)
