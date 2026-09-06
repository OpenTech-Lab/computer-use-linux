from __future__ import annotations

import os

from ..config import Config, load_config
from ..errors import BackendUnavailable
from .gnome_mutter import GnomeMutterBackend, probe_mutter


def detect(*, name: str | None = None, config: Config | None = None):
    """Select the currently usable Phase 1 backend.

    The fallback backends are intentionally not selected before their later phases are built.
    A requested non-GNOME backend gets a clear error instead of a deceptive partial implementation.
    """

    config = config or load_config()
    requested = name or config.backend or os.environ.get("CUL_BACKEND")
    if requested and requested not in {"gnome", "gnome_mutter", "mutter"}:
        raise BackendUnavailable(
            f"backend {requested!r} is not implemented in Phases 0-2; use gnome_mutter on this GNOME session"
        )
    if requested in {"gnome", "gnome_mutter", "mutter"}:
        return GnomeMutterBackend()
    if os.environ.get("XDG_SESSION_TYPE") != "wayland":
        raise BackendUnavailable("no Phase 0-2 backend is available outside a Wayland session")
    probe = probe_mutter()
    if "screen_cast_error" in probe or "remote_desktop_error" in probe:
        raise BackendUnavailable("Mutter ScreenCast/RemoteDesktop is unavailable on this session")
    return GnomeMutterBackend()


__all__ = ["GnomeMutterBackend", "detect", "probe_mutter"]
