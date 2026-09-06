#!/usr/bin/env bash
set -euo pipefail

# The real requirement is NOT a particular Python version -- it is a Python that can
# "import gi". PyGObject is a compiled distribution package built against the system
# GLib/GObject and its introspection typelibs, so it cannot be pip-installed. That means
# the venv must be built from the SYSTEM interpreter that ships PyGObject, with
# --system-site-packages, whatever version that happens to be on this distro
# (3.12 on Ubuntu 24.04, 3.13 on Debian 13/Fedora, 3.14 on Ubuntu 26.04, ...).

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_dir="$repo_root/.venv"
uv_cache_dir="${UV_CACHE_DIR:-/tmp/cul-uv-cache}"
mkdir -p "$uv_cache_dir"

has_gi() { "$1" -c 'import gi' >/dev/null 2>&1; }

find_system_python() {
    # Honour an explicit override first, then look for any system python with gi.
    if [[ -n "${CUL_PYTHON:-}" ]]; then
        if has_gi "$CUL_PYTHON"; then echo "$CUL_PYTHON"; return 0; fi
        echo "CUL_PYTHON=$CUL_PYTHON cannot 'import gi'" >&2
        return 1
    fi
    local candidate
    for candidate in /usr/bin/python3 /usr/bin/python3.1[0-9] /usr/bin/python3.9; do
        [[ -x "$candidate" ]] || continue
        if has_gi "$candidate"; then echo "$candidate"; return 0; fi
    done
    return 1
}

if [[ ! -x "$venv_dir/bin/python" ]]; then
    command -v uv >/dev/null 2>&1 || { echo "uv is required to bootstrap .venv" >&2; exit 1; }
    if ! system_python="$(find_system_python)"; then
        cat >&2 <<'MSG'
No system Python with PyGObject was found.

PyGObject cannot be installed from PyPI for this project: 'gi' is compiled against your
distribution's GLib and loads its introspection typelibs. Install it from your package
manager, then re-run this script:

  Debian/Ubuntu  sudo apt install python3-gi gir1.2-atspi-2.0 gstreamer1.0-pipewire
  Fedora/RHEL    sudo dnf install python3-gobject gstreamer1-plugins-base
  Arch           sudo pacman -S python-gobject at-spi2-core gst-plugin-pipewire
  openSUSE       sudo zypper install python3-gobject gstreamer-plugins-base

If your system Python is at an unusual path, point at it explicitly:
  CUL_PYTHON=/usr/bin/python3.12 ./scripts/bootstrap.sh
MSG
        exit 1
    fi
    echo "using system interpreter: $system_python ($("$system_python" -V 2>&1))"
    uv venv --python "$system_python" --system-site-packages "$venv_dir"
else
    # The existing environment is user state. Validate it, but never replace it.
    if ! has_gi "$venv_dir/bin/python"; then
        echo ".venv exists but cannot 'import gi'." >&2
        echo "It was probably built from a non-system interpreter or without --system-site-packages." >&2
        echo "Remove it and re-run this script to rebuild: rm -rf '$venv_dir'" >&2
        exit 1
    fi
fi

# Installed into the existing environment. No apt command belongs here: PyGObject comes
# from system site-packages, and GstApp is optional at runtime.
UV_CACHE_DIR="$uv_cache_dir" VIRTUAL_ENV="$venv_dir" uv pip install --python "$venv_dir/bin/python" numpy 'mcp>=2.1' pillow python-xlib pytest pytest-asyncio ruff

"$venv_dir/bin/python" -c 'import gi, numpy, mcp, PIL, sys; print("ready: python {}.{}; gi {}; numpy {}; PIL {}".format(sys.version_info.major, sys.version_info.minor, gi.__version__, numpy.__version__, PIL.__version__))'
