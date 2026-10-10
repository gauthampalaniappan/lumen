#!/usr/bin/env bash
# User-level install of Lumen on Fedora.
# Package names were checked on 2026-09-30 against
# https://packages.fedoraproject.org (Fedora 43, 44, 45, and Rawhide).
# dnf needs privileges. The app, icon, and launcher go under $PREFIX
# (default: ~/.local) and do not need root.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"

case "$PREFIX" in
  *" "*)
    echo "PREFIX must not contain spaces: $PREFIX" >&2
    exit 1
    ;;
esac

if [[ "${SKIP_DEPS:-0}" != "1" ]]; then
  if ! command -v dnf >/dev/null 2>&1; then
    echo "dnf was not found. On Fedora, install the runtime packages with:" >&2
    echo "  sudo dnf install python3-gobject python3-gobject-base gtk4 libadwaita python3-pillow" >&2
    echo "Then re-run: SKIP_DEPS=1 $0" >&2
    exit 1
  fi
  sudo dnf install -y python3-gobject python3-gobject-base gtk4 libadwaita python3-pillow
fi

install -d \
  "$PREFIX/bin" \
  "$PREFIX/share/lumen" \
  "$PREFIX/share/applications" \
  "$PREFIX/share/icons/hicolor/scalable/apps"

rm -rf "$PREFIX/share/lumen/lumen"
cp -a "$ROOT/lumen" "$PREFIX/share/lumen/lumen"
install -m 0644 "$ROOT/lumen.py" "$PREFIX/share/lumen/lumen.py"
find "$PREFIX/share/lumen" -type d -name '__pycache__' -exec rm -rf {} + || true
install -m 0644 "$ROOT/share/org.lumen.Lumen.svg" \
  "$PREFIX/share/icons/hicolor/scalable/apps/org.lumen.Lumen.svg"

# Write the launcher without a nested heredoc so this file stays easy to copy.
printf '%s\n' \
  '#!/bin/sh' \
  'export GTK_A11Y="${GTK_A11Y:-none}"' \
  "exec python3 \"$PREFIX/share/lumen/lumen.py\" \"\$@\"" \
  > "$PREFIX/bin/lumen"
chmod 0755 "$PREFIX/bin/lumen"

sed "s|^Exec=.*|Exec=$PREFIX/bin/lumen|" "$ROOT/share/org.lumen.Lumen.desktop" \
  > "$PREFIX/share/applications/org.lumen.Lumen.desktop"
chmod 0644 "$PREFIX/share/applications/org.lumen.Lumen.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$PREFIX/share/applications" || true
fi
if command -v gtk4-update-icon-cache >/dev/null 2>&1; then
  gtk4-update-icon-cache -f -t "$PREFIX/share/icons/hicolor" || true
fi

echo "Installed Lumen for this user."
echo "Launcher: $PREFIX/bin/lumen"
echo "If $PREFIX/bin is not on PATH, run that path directly."
