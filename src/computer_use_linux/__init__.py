"""Core package for computer-use-linux."""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    raise RuntimeError(
        f"computer-use-linux requires Python 3.11 or newer; this is {sys.version.split()[0]}."
    )

# The binding that actually constrains the runtime is PyGObject, not a Python version.
# `gi` is a compiled distribution package built against the system GLib and its typelibs,
# so it cannot come from PyPI -- the venv must be built from the system interpreter that
# ships it, with --system-site-packages. Whichever version that is varies by distro, so
# check the capability rather than pinning a version and locking out other distributions.
try:  # pragma: no cover - exercised by the import itself
    import gi  # noqa: F401
except ImportError as _exc:  # pragma: no cover
    raise RuntimeError(
        "computer-use-linux cannot import PyGObject ('gi'). It is a system package and cannot "
        "be pip-installed: build .venv from the system interpreter with --system-site-packages, "
        f"e.g. `./scripts/bootstrap.sh` (running {sys.executable})."
    ) from _exc

__version__ = "0.1.0"

