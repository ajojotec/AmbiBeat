#!/usr/bin/env bash
set -euo pipefail

PACKAGES=(
    gir1.2-glib-2.0
    pulseaudio-utils
    python3-dbus
    python3-gi
    python3-numpy
    python3-setuptools
    python3-venv
)

DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        -h|--help)
            echo "Usage: sudo ./scripts/install-dependencies-debian13.sh [--dry-run]"
            echo "Install the system packages required by AmbiBeat on Debian 13."
            echo "  --dry-run  Show the apt commands without changing the system"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 1
            ;;
    esac
done

if [ "$DRY_RUN" = true ]; then
    printf 'apt-get update\napt-get install -y'
    printf ' %q' "${PACKAGES[@]}"
    printf '\n'
    exit 0
fi

if [ "$EUID" -ne 0 ]; then
    echo "Run this dependency installer as root: sudo $0" >&2
    exit 1
fi

if [ ! -r /etc/os-release ]; then
    echo "Cannot identify the operating system; expected Debian 13." >&2
    exit 1
fi

. /etc/os-release
if [ "${ID:-}" != "debian" ] || [ "${VERSION_ID:-}" != "13" ]; then
    echo "This script is intended for Debian 13 (detected ${PRETTY_NAME:-unknown})." >&2
    exit 1
fi

apt-get update
apt-get install -y "${PACKAGES[@]}"

echo "Debian 13 dependencies installed."
