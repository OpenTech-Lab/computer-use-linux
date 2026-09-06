from __future__ import annotations

import os

from ..config import Config, load_config
from ..errors import BackendUnavailable
from .gnome_mutter import GnomeMutterBackend, probe_mutter
from .headless import HeadlessBackend, probe_headless
from .x11 import X11Backend, probe_x11


def detect(*, name: str | None = None, config: Config | None = None):
    """Select the currently usable backend from explicit and session capabilities.

    GNOME's private APIs are preferred on Wayland, XTEST is used on X11/XWayland, and an explicit
    isolated request uses a private Xvfb display when Mutter's virtual surface is unavailable.
    """

    config = config or load_config()
    requested = name or config.backend or os.environ.get("CUL_BACKEND")
    requested = requested.casefold() if isinstance(requested, str) else requested
    if requested and requested not in {"gnome", "gnome_mutter", "mutter", "x11", "xwayland", "headless"}:
        raise BackendUnavailable(
            f"unknown backend {requested!r}; available backends: gnome_mutter, x11, headless"
        )
    if requested in {"gnome", "gnome_mutter", "mutter"}:
        return _gnome_backend(config)
    if requested in {"x11", "xwayland"}:
        if config.isolated:
            return _headless_backend(config)
        return X11Backend()
    if requested == "headless":
        return _headless_backend(config)

    session_type = os.environ.get("XDG_SESSION_TYPE", "").casefold()
    if session_type == "wayland":
        probe = probe_mutter(probe_virtual=config.isolated)
        if "screen_cast_error" not in probe and "remote_desktop_error" not in probe:
            try:
                return _gnome_backend(config, probe=probe)
            except BackendUnavailable:
                if config.isolated:
                    return _headless_backend(config)
                raise
        if config.isolated:
            return _headless_backend(config)
        x11_probe = probe_x11()
        if x11_probe.get("available"):
            return X11Backend()
        raise BackendUnavailable("Mutter ScreenCast/RemoteDesktop is unavailable on this session")

    if session_type == "x11" or os.environ.get("DISPLAY"):
        if config.isolated:
            return _headless_backend(config)
        x11_probe = probe_x11()
        if x11_probe.get("available"):
            return X11Backend()
        raise BackendUnavailable(f"X11 is unavailable: {x11_probe.get('error', 'XTEST is unavailable')}")
    raise BackendUnavailable("no GNOME Wayland or X11 backend is available on this session")


def _headless_backend(config: Config) -> HeadlessBackend:
    probe = probe_headless()
    if not probe.get("available"):
        detail = str(probe.get("error", "Xvfb is unavailable"))
        raise BackendUnavailable(f"isolated headless backend is unavailable: {detail}")
    return HeadlessBackend(width=config.virtual_width, height=config.virtual_height)


def _gnome_backend(config: Config, *, probe: dict[str, object] | None = None) -> GnomeMutterBackend:
    """Construct Mutter with virtual-surface policy decided before streams are recorded."""

    probe = probe or probe_mutter(probe_virtual=config.isolated)
    if "screen_cast_error" in probe or "remote_desktop_error" in probe:
        raise BackendUnavailable("Mutter ScreenCast/RemoteDesktop is unavailable on this session")
    virtual_available = bool(probe.get("virtual_surface_available", False))
    if config.isolated and not virtual_available:
        detail = str(probe.get("virtual_surface_error", "RecordVirtual is unavailable"))
        raise BackendUnavailable(f"isolated mode requires Mutter RecordVirtual: {detail}")
    include_virtual = bool(config.isolated)
    return GnomeMutterBackend(
        include_virtual=include_virtual,
        virtual_only=include_virtual,
        virtual_width=config.virtual_width,
        virtual_height=config.virtual_height,
        virtual_refresh=config.virtual_refresh,
    )


__all__ = [
    "GnomeMutterBackend",
    "HeadlessBackend",
    "X11Backend",
    "detect",
    "probe_headless",
    "probe_mutter",
    "probe_x11",
]
