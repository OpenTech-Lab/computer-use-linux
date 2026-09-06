#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_dir="$repo_root/.venv"
uv_cache_dir="${UV_CACHE_DIR:-/tmp/cul-uv-cache}"
mkdir -p "$uv_cache_dir"

if [[ ! -x "$venv_dir/bin/python" ]]; then
    command -v uv >/dev/null 2>&1 || {
        echo "uv is required to bootstrap .venv" >&2
        exit 1
    }
    uv venv --python /usr/bin/python3.14 --system-site-packages "$venv_dir"
else
    # The existing environment is user state. Validate it, but never replace it.
    if ! "$venv_dir/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 14) else 1)' ; then
        echo ".venv exists but is not the required CPython 3.14 environment; refusing to replace it" >&2
        exit 1
    fi
fi

# These are deliberately installed into the existing environment. No apt command belongs here:
# PyGObject is supplied by the system site-packages and GstApp is optional at runtime.
UV_CACHE_DIR="$uv_cache_dir" VIRTUAL_ENV="$venv_dir" uv pip install --python "$venv_dir/bin/python" numpy 'mcp>=2.1' pillow pytest pytest-asyncio ruff

"$venv_dir/bin/python" -c 'import gi, numpy, mcp, PIL, sys; print("ready: python {}.{}; gi {}; numpy {}; PIL {}".format(sys.version_info.major, sys.version_info.minor, gi.__version__, numpy.__version__, PIL.__version__))'
