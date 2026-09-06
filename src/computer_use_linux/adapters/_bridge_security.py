"""Small, shared helpers for adapter bridge state files.

Bridge state files contain bearer credentials.  Keep their creation and mode handling in one
place so a normal adapter launch cannot accidentally reintroduce a write-then-chmod race.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def ensure_private_directory(path: Path) -> Path:
    """Create ``path`` if needed and require that it belongs only to this uid."""

    path = path.expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE:
        raise PermissionError(f"bridge directory is not owner-only: {path}")
    return path


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write JSON through a newly-created/truncated 0600 descriptor and verify the result."""

    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
    descriptor = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, PRIVATE_FILE_MODE)
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE:
        raise PermissionError(f"bridge state file is not owner-only: {path}")


def public_record(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return bridge metadata without ever exposing the bearer token."""

    if not record:
        return {}
    return {key: value for key, value in record.items() if key != "token"}
