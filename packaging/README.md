# Packaging and releases

Releases are built by GitHub Actions (`.github/workflows/release.yml`).

| Output | Built on | How |
| --- | --- | --- |
| `lumen-<ver>-1.fcNN.noarch.rpm` | Fedora container | `packaging/linux/build-packages.sh rpm` |
| `lumen_<ver>-1_all.deb` | Debian 13 container | `packaging/linux/build-packages.sh deb` |
| `Lumen-<ver>-windows-x64-setup.exe` and `-portable.zip` | Windows, MSYS2 UCRT64 | PyInstaller (`packaging/lumen.spec`), then Inno Setup (`packaging/windows/lumen.iss`) |
| `Lumen-<ver>-macos-arm64.dmg` / `-x86_64.dmg` | macOS (Apple Silicon and Intel) | Homebrew GTK, PyInstaller (`packaging/lumen.spec`), `hdiutil` |

Every package is installed or launched with `--self-check` in CI before it is
uploaded, so a build that cannot start never reaches a release.

## Making a release

1. Bump `__version__` in `lumen/__init__.py`.
2. Add a line for the new version to `share/org.lumen.Lumen.metainfo.xml`, e.g.
   `<release version="0.2.0" date="2026-11-01"/>` (newest first).
3. Commit, then tag and push:

   ```bash
   git tag v0.2.0
   git push origin main v0.2.0
   ```

4. When the workflow finishes, open **Releases** on GitHub. A draft release
   holds every installer plus `SHA256SUMS.txt`. Check it, then press **Publish**.

To try the builds without releasing, run the workflow by hand
(**Actions → Release → Run workflow**). The packages appear as artifacts on
that run.

## Building locally

Linux packages (needs `dpkg-deb` and/or `rpmbuild`):

```bash
packaging/linux/build-packages.sh        # writes release/*.deb and/or release/*.rpm
```

Windows (in an MSYS2 UCRT64 shell with the packages listed in the workflow) or
macOS (with Homebrew `gtk4 libadwaita pygobject3 librsvg adwaita-icon-theme`
and `pip install pyinstaller pillow`):

```bash
pyinstaller --noconfirm --clean packaging/lumen.spec   # dist/Lumen or dist/Lumen.app
```

## Code signing (optional, later)

The Windows and macOS builds are unsigned. They work, but users see a warning
the first time they open Lumen:

- **Windows** — SmartScreen: *More info → Run anyway*. Signing needs a code
  signing certificate (or Azure Trusted Signing); sign `dist/Lumen/Lumen.exe`
  and the setup `.exe` with `signtool` in the Windows job.
- **macOS** — *System Settings → Privacy & Security → Open Anyway*. Signing and
  notarization need an Apple Developer account (US$99/year): replace the
  ad-hoc `codesign --sign -` step with a Developer ID identity, then run
  `xcrun notarytool submit` and `xcrun stapler staple` on the `.dmg`.

## Platform notes

- Lumen needs libadwaita 1.7 or newer (`Adw.WrapBox`). The `.deb` declares this,
  so apt refuses it on older systems such as Ubuntu 24.04.
- On Windows and macOS, camera import (gphoto2) and Linux card detection
  (udisks) are unavailable; importing by choosing a folder works everywhere.
  Settings and the library live in `%APPDATA%\Lumen` / `%LOCALAPPDATA%\Lumen`
  on Windows and `~/Library/Application Support/Lumen` on macOS.
