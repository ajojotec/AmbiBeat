#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ASSET_DIR="$PROJECT_DIR/src/ambibeat/assets"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
LEGACY_DESKTOP_FILE="$APP_DIR/bt-audio-companion.desktop"

for arg in "$@"; do
    case "$arg" in
        -h|--help)
            echo "Usage: ./scripts/install.sh"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 1
            ;;
    esac
done

if ! command -v python3 >/dev/null; then
    echo "python3 is required." >&2
    exit 1
fi

mkdir -p "$BIN_DIR" "$APP_DIR"

if [ -e "$LEGACY_DESKTOP_FILE" ]; then
    rm -- "$LEGACY_DESKTOP_FILE"
fi

if [ ! -d "$PROJECT_DIR/.venv" ]; then
    python3 -m venv --system-site-packages "$PROJECT_DIR/.venv"
else
    python3 -m venv --upgrade --system-site-packages "$PROJECT_DIR/.venv"
fi

if grep -q '^include-system-site-packages = false$' "$PROJECT_DIR/.venv/pyvenv.cfg"; then
    sed -i 's/^include-system-site-packages = false$/include-system-site-packages = true/' \
        "$PROJECT_DIR/.venv/pyvenv.cfg"
elif ! grep -qx 'include-system-site-packages = true' "$PROJECT_DIR/.venv/pyvenv.cfg"; then
    echo "Cannot confirm that .venv can access Debian's system Python packages." >&2
    exit 1
fi

VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
if ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
    if ! "$VENV_PYTHON" -m ensurepip --upgrade; then
        PYTHON_VERSION="$("$VENV_PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
        echo "pip and ensurepip are unavailable in the project virtual environment." >&2
        echo "On Debian/Ubuntu, install the venv package with: sudo apt install python${PYTHON_VERSION}-venv" >&2
        exit 1
    fi
fi

if "$VENV_PYTHON" -m pip show bt-audio-companion >/dev/null 2>&1; then
    "$VENV_PYTHON" -m pip uninstall --yes bt-audio-companion
fi

"$VENV_PYTHON" -m pip install --editable "$PROJECT_DIR"

ln -sfn "$PROJECT_DIR/.venv/bin/ambibeat" "$BIN_DIR/ambibeat"
ln -sfn "$BIN_DIR/ambibeat" "$BIN_DIR/bt-audio-companion"
ln -sfn "$PROJECT_DIR/.venv/bin/ambibeat-qt" "$BIN_DIR/ambibeat-qt"

cat > "$BIN_DIR/ambibeat-native" <<EOF
#!/usr/bin/env bash
exec "$BIN_DIR/ambibeat-qt" "\$@"
EOF
chmod 0755 "$BIN_DIR/ambibeat-native"

ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
mkdir -p "$ICON_DIR/32x32/apps" "$ICON_DIR/192x192/apps" "$ICON_DIR/512x512/apps"
install -m 0644 "$ASSET_DIR/ambibeat-icon-32.png" "$ICON_DIR/32x32/apps/ambibeat.png"
install -m 0644 "$ASSET_DIR/ambibeat-icon-192.png" "$ICON_DIR/192x192/apps/ambibeat.png"
install -m 0644 "$ASSET_DIR/ambibeat-icon.png" "$ICON_DIR/512x512/apps/ambibeat.png"
cat > "$ICON_DIR/index.theme" <<EOF
[Icon Theme]
Name=AmbiBeat
Directories=32x32/apps,192x192/apps,512x512/apps

[32x32/apps]
Size=32
Context=Apps
Type=Fixed

[192x192/apps]
Size=192
Context=Apps
Type=Fixed

[512x512/apps]
Size=512
Context=Apps
Type=Fixed
EOF
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true
fi

cat > "$APP_DIR/ambibeat.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AmbiBeat
Comment=Show music metadata and audio visualisation
Exec=$BIN_DIR/ambibeat-qt
Icon=$ASSET_DIR/ambibeat-icon.png
Terminal=false
Categories=AudioVideo;Audio;Player;
StartupNotify=true
StartupWMClass=AmbiBeat
EOF

cat > "$APP_DIR/ambibeat-qt.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AmbiBeat Native
Comment=Native Qt audio visualisation frontend
Exec=$BIN_DIR/ambibeat-qt
Icon=$ASSET_DIR/ambibeat-icon.png
Terminal=false
Categories=AudioVideo;Audio;Player;
StartupNotify=true
StartupWMClass=AmbiBeat
NoDisplay=true
EOF

echo "Installation complete. Launch AmbiBeat from the application menu or with $BIN_DIR/ambibeat."
