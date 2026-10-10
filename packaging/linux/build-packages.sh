#!/usr/bin/env bash
# Build Linux packages for Lumen into ./release/.
#
#   packaging/linux/build-packages.sh          # .deb and .rpm (whichever tools exist)
#   packaging/linux/build-packages.sh deb      # only the .deb (needs dpkg-deb)
#   packaging/linux/build-packages.sh rpm      # only the .rpm (needs rpmbuild)
#
# The version comes from lumen/__init__.py. In CI, RELEASE_TAG (e.g. v0.1.0)
# must match it, so a tag can never ship a package with the wrong version.
set -euo pipefail

APP=lumen
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$APP/__init__.py")"
if [[ -z "$VERSION" ]]; then
  echo "Could not read __version__ from $APP/__init__.py" >&2
  exit 1
fi
if [[ -n "${RELEASE_TAG:-}" && "${RELEASE_TAG#v}" != "$VERSION" ]]; then
  echo "Tag $RELEASE_TAG does not match __version__ $VERSION in $APP/__init__.py" >&2
  exit 1
fi
if ! grep -q "<release version=\"$VERSION\"" share/org.lumen.Lumen.metainfo.xml; then
  echo "Add <release version=\"$VERSION\" date=\"YYYY-MM-DD\"/> to share/org.lumen.Lumen.metainfo.xml" >&2
  exit 1
fi

want=("$@")
if [[ ${#want[@]} -eq 0 ]]; then
  command -v dpkg-deb >/dev/null && want+=(deb)
  command -v rpmbuild >/dev/null && want+=(rpm)
fi
if [[ ${#want[@]} -eq 0 ]]; then
  echo "Neither dpkg-deb nor rpmbuild is installed." >&2
  exit 1
fi

OUT="$ROOT/release"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"

build_deb() {
  local stage="$WORK/deb"
  install -d "$stage/DEBIAN" "$stage/usr/share/$APP" "$stage/usr/share/doc/$APP"
  cp -a "$APP" "$stage/usr/share/$APP/$APP"
  find "$stage/usr/share/$APP" -name '__pycache__' -prune -exec rm -rf {} +
  install -Dm0755 packaging/linux/lumen.sh "$stage/usr/bin/$APP"
  install -Dm0644 share/org.lumen.Lumen.desktop "$stage/usr/share/applications/org.lumen.Lumen.desktop"
  install -Dm0644 share/org.lumen.Lumen.svg "$stage/usr/share/icons/hicolor/scalable/apps/org.lumen.Lumen.svg"
  install -Dm0644 share/org.lumen.Lumen.metainfo.xml "$stage/usr/share/metainfo/org.lumen.Lumen.metainfo.xml"
  install -m0644 README.md "$stage/usr/share/doc/$APP/README.md"
  install -m0644 LICENSE "$stage/usr/share/doc/$APP/copyright"

  local size
  size="$(du -sk --exclude=DEBIAN "$stage" | cut -f1)"
  sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$size/" packaging/linux/debian-control.in \
    > "$stage/DEBIAN/control"
  printf '%s\n' '#!/bin/sh' 'set -e' \
    "if command -v py3compile >/dev/null 2>&1; then py3compile /usr/share/$APP/$APP || true; fi" \
    > "$stage/DEBIAN/postinst"
  printf '%s\n' '#!/bin/sh' 'set -e' \
    "if command -v py3clean >/dev/null 2>&1; then py3clean /usr/share/$APP/$APP || true; fi" \
    "find /usr/share/$APP -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true" \
    > "$stage/DEBIAN/prerm"
  chmod 0755 "$stage/DEBIAN/postinst" "$stage/DEBIAN/prerm"

  local deb="$OUT/${APP}_${VERSION}-1_all.deb"
  dpkg-deb --root-owner-group -Zxz --build "$stage" "$deb" >/dev/null
  echo "Built $deb"
}

build_rpm() {
  local top="$WORK/rpm" name="$APP-$VERSION"
  mkdir -p "$top"/{SOURCES,SPECS,BUILD,RPMS,SRPMS} "$WORK/src/$name"
  cp -a "$APP" share README.md LICENSE "$WORK/src/$name/"
  install -Dm0755 packaging/linux/lumen.sh "$WORK/src/$name/packaging/linux/lumen.sh"
  find "$WORK/src/$name" -name '__pycache__' -prune -exec rm -rf {} +
  tar -C "$WORK/src" -czf "$top/SOURCES/$name.tar.gz" "$name"

  local date
  date="$(LC_ALL=C date -u +'%a %b %d %Y')"
  sed -e "s/@VERSION@/$VERSION/g" -e "s/@DATE@/$date/" packaging/linux/lumen.spec.in \
    > "$top/SPECS/$APP.spec"

  local nodeps=()
  # On a non-RPM system (e.g. Ubuntu) the build dependencies cannot be checked.
  [[ -f /etc/redhat-release || -f /etc/fedora-release ]] || nodeps=(--nodeps)
  rpmbuild -bb "${nodeps[@]}" --define "_topdir $top" "$top/SPECS/$APP.spec" >"$WORK/rpmbuild.log" 2>&1 || {
    cat "$WORK/rpmbuild.log" >&2
    exit 1
  }
  local rpm
  for rpm in "$top"/RPMS/*/*.rpm; do
    cp "$rpm" "$OUT/"
    echo "Built $OUT/$(basename "$rpm")"
  done
}

for kind in "${want[@]}"; do
  case "$kind" in
    deb) build_deb ;;
    rpm) build_rpm ;;
    *) echo "Unknown package type: $kind (use deb or rpm)" >&2; exit 1 ;;
  esac
done
