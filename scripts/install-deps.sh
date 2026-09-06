#!/usr/bin/env bash
set -euo pipefail

# The app adapters use standard-library sockets plus the already-installed browser/Godot/Blender/
# VSCode executables. Missing packages below only affect optional desktop backends or capture
# enhancements; adapter imports remain lazy and do not hard-fail.
# Minimal full-isolation setup: sudo apt install xvfb cage
apt_packages=(
    gir1.2-gst-plugins-base-1.0
    gstreamer1.0-pipewire
    python3-gi
    gir1.2-atspi-2.0
    xvfb
    x11-utils
    x11-xserver-utils
    cage
    wl-clipboard
)

missing=()
for package in "${apt_packages[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q '^install ok installed$'; then
        missing+=("$package")
    fi
done

if ((${#missing[@]} == 0)); then
    echo "all optional system packages are already installed"
    exit 0
fi

echo "The following system packages are missing: ${missing[*]}"
read -r -p "Install them with sudo apt-get install? [y/N] " answer
case "$answer" in
    y|Y|yes|YES)
        sudo apt-get install -y "${missing[@]}"
        ;;
    *)
        echo "no packages installed"
        ;;
esac
