#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$#" -gt 1 ]; then
    echo "Usage: $0 [output-directory]" >&2
    exit 1
fi
case "${1:-}" in
    -h|--help)
        echo "Usage: $0 [output-directory]"
        echo "Build a Debian package in the output directory (default: dist/)."
        exit 0
        ;;
    -*)
        echo "Unknown option: $1" >&2
        exit 1
        ;;
esac

for tool in python3 dpkg dpkg-deb; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "$tool is required to build the Debian package." >&2
        exit 1
    fi
done

if ! VERSION="$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' \
    "$PROJECT_DIR/pyproject.toml")"; then
    echo "Cannot read the project version; Python 3.11 or later is required." >&2
    exit 1
fi
if [[ ! "$VERSION" =~ ^[0-9][A-Za-z0-9.+~:-]*$ ]] || ! dpkg --validate-version "$VERSION"; then
    echo "Invalid Debian package version: $VERSION" >&2
    exit 1
fi

OUTPUT_DIR="${1:-$PROJECT_DIR/dist}"
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ambibeat-deb.XXXXXXXX")"
trap 'rm -rf -- "$BUILD_DIR"' EXIT
STAGING="$BUILD_DIR/package"

install -d "$STAGING/DEBIAN" "$STAGING/usr/bin" \
    "$STAGING/usr/lib/python3/dist-packages/ambibeat/assets" \
    "$STAGING/usr/share/applications" "$STAGING/usr/share/doc/ambibeat"
install -m 0644 "$PROJECT_DIR"/src/ambibeat/*.py \
    "$STAGING/usr/lib/python3/dist-packages/ambibeat/"
install -m 0644 "$PROJECT_DIR"/src/ambibeat/assets/*.png \
    "$PROJECT_DIR/src/ambibeat/assets/ATTRIBUTION.txt" \
    "$STAGING/usr/lib/python3/dist-packages/ambibeat/assets/"

for size in 32 192 512; do
    icon="$PROJECT_DIR/src/ambibeat/assets/ambibeat-icon-$size.png"
    if [ "$size" = 512 ]; then
        icon="$PROJECT_DIR/src/ambibeat/assets/ambibeat-icon.png"
    fi
    install -D -m 0644 "$icon" "$STAGING/usr/share/icons/hicolor/${size}x${size}/apps/ambibeat.png"
done

cat > "$STAGING/usr/bin/ambibeat" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 -m ambibeat "$@"
EOF
chmod 0755 "$STAGING/usr/bin/ambibeat"
ln -s ambibeat "$STAGING/usr/bin/ambibeat-qt"

cat > "$STAGING/usr/share/applications/ambibeat.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=AmbiBeat
Comment=Show music metadata and audio visualisation
Exec=/usr/bin/ambibeat
Icon=ambibeat
Terminal=false
Categories=AudioVideo;Audio;Player;
StartupNotify=true
StartupWMClass=AmbiBeat
EOF
chmod 0644 "$STAGING/usr/share/applications/ambibeat.desktop"

{
    printf 'AmbiBeat source code is licensed under GPL-3.0-or-later.\n\n'
    cat "$PROJECT_DIR/src/ambibeat/assets/ATTRIBUTION.txt"
    printf '\n\n'
    cat "$PROJECT_DIR/LICENSE"
} > "$STAGING/usr/share/doc/ambibeat/copyright"
chmod 0644 "$STAGING/usr/share/doc/ambibeat/copyright"

cat > "$STAGING/DEBIAN/control" <<EOF
Package: ambibeat
Version: $VERSION
Architecture: all
Maintainer: AmbiBeat contributors <noreply@github.com>
Section: sound
Priority: optional
Depends: python3 (>= 3.10), python3-dbus, python3-gi, gir1.2-glib-2.0, python3-numpy (>= 1.24), python3-pyside6.qtcore (>= 6.8), python3-pyside6.qtgui (>= 6.8), python3-pyside6.qtwidgets (>= 6.8), python3-pyside6.qtopengl (>= 6.8), python3-pyside6.qtopenglwidgets (>= 6.8), qt6-qpa-plugins, qt6-wayland, pulseaudio-utils
Homepage: https://github.com/ajojotec/AmbiBeat
Description: Audio visualiser and tempo-synced edge lighting
 AmbiBeat is a Qt music visualiser that reads media-player metadata and
 captures PulseAudio or PipeWire monitor audio.
EOF
chmod 0644 "$STAGING/DEBIAN/control"

PACKAGE="$OUTPUT_DIR/ambibeat_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$STAGING" "$BUILD_DIR/ambibeat.deb"
mv -f -- "$BUILD_DIR/ambibeat.deb" "$PACKAGE"
echo "Built $PACKAGE"
