# PyInstaller recipe for the Windows and macOS builds of Lumen.
#
#   pyinstaller --noconfirm --clean packaging/lumen.spec
#
# Output: dist/Lumen/ (Windows folder with Lumen.exe) or dist/Lumen.app (macOS).
# GTK 4, libadwaita, PyGObject, pycairo and Pillow must be importable by the
# Python that runs PyInstaller (MSYS2 UCRT64 on Windows, Homebrew on macOS).
# Linux uses the native RPM/.deb packages instead.

import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821  (SPECPATH is provided by PyInstaller)
VERSION = re.search(
    r'__version__\s*=\s*"([^"]+)"', (ROOT / "lumen" / "__init__.py").read_text(encoding="utf-8")
).group(1)

is_win = sys.platform == "win32"
is_mac = sys.platform == "darwin"
icon = str(ROOT / "packaging" / "icons" / ("lumen.ico" if is_win else "lumen.icns"))

a = Analysis(  # noqa: F821
    [str(ROOT / "lumen.py")],
    pathex=[str(ROOT)],
    hiddenimports=[
        "lumen.ui",
        "lumen.import_dialog",
        "lumen.backup_dialog",
        "lumen.photobook",
        "gi.repository.Adw",
        "gi.repository.Gtk",
        "gi.repository.Gdk",
        "gi.repository.Gio",
        "gi.repository.GLib",
        "gi.repository.Pango",
        "gi.repository.Graphene",
        "cairo",
    ],
    hooksconfig={
        "gi": {
            "module-versions": {"Gtk": "4.0", "Gdk": "4.0"},
            "icons": ["Adwaita", "hicolor"],
            "themes": [],
            "languages": ["en_US", "en_GB"],
        },
    },
    excludes=["tkinter", "unittest", "pydoc_data", "test"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Lumen",
    console=False,
    icon=icon,
    upx=False,
    # Builds are unsigned unless signing is set up later (see packaging/README.md).
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    upx=False,
    name="Lumen",
)

if is_mac:
    app = BUNDLE(  # noqa: F821
        coll,
        name="Lumen.app",
        icon=icon,
        bundle_identifier="org.lumen.Lumen",
        version=VERSION,
        info_plist={
            "CFBundleName": "Lumen",
            "CFBundleDisplayName": "Lumen",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "© 2026 Gautham Palaniappan. MIT License.",
            "LSApplicationCategoryType": "public.app-category.photography",
            "NSPhotoLibraryUsageDescription": "Lumen browses photo folders you choose.",
            "NSRemovableVolumesUsageDescription": "Lumen imports photos from memory cards.",
        },
    )
