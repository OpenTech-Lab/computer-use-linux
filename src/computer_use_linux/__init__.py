"""Core package for computer-use-linux."""

from __future__ import annotations

import sys

if sys.version_info < (3, 14):
    raise RuntimeError(
        "computer-use-linux requires the system CPython 3.14 (/usr/bin/python3.14) with "
        "--system-site-packages; PyGObject cannot be installed from PyPI. Run scripts/bootstrap.sh."
    )

__version__ = "0.1.0"

