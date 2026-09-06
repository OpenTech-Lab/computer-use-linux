from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .errors import SafetyRefusal
from .keys import chord_is_destructive
from .types import Frame, Surface, WindowInfo

SENSITIVE_APP_NAMES = {
    "gcr-prompter",
    "polkit-gnome-authentication-agent",
    "xdg-desktop-portal-gtk",
}


def is_sensitive_window(window: WindowInfo) -> bool:
    app = window.app.casefold()
    title = window.title.casefold()
    if any(name in app for name in SENSITIVE_APP_NAMES):
        return True
    if app == "gnome-shell" and window.active:
        return title not in {"main stage", ""}
    return window.role.casefold() == "password text"


def focused_sensitive_window(windows: Iterable[WindowInfo]) -> WindowInfo | None:
    for window in windows:
        if window.active and is_sensitive_window(window):
            return window
    return None


def require_confirmation(
    *,
    action: str,
    confirm: bool,
    confirm_mode: str,
    chord: str | None = None,
    focused_window: WindowInfo | None = None,
) -> None:
    destructive = confirm_mode == "all"
    reason = action
    if confirm_mode == "destructive":
        if action == "key" and chord is not None:
            destructive = chord_is_destructive(chord)
            reason = f"key chord {chord!r}"
        elif action in {"run_python", "eval"}:
            destructive = True
        elif action == "type_text" and focused_window is not None:
            destructive = True
            reason = f"typing into {focused_window.title!r}"
    if destructive and not confirm:
        raise SafetyRefusal(f"confirmation required before {reason}; repeat with confirm=true")


def redact_frame(
    frame: Frame,
    surface: Surface,
    *,
    redact_regions: Iterable[Mapping[str, Any]] = (),
    redact_window_titles: Iterable[str] = (),
    windows: Iterable[WindowInfo] = (),
) -> Frame:
    """Apply all static redactions to a copy of the native RGB frame."""

    import numpy as np

    data = np.array(frame.data, copy=True)
    for region in redact_regions:
        if str(region.get("surface_id", surface.id)) != surface.id:
            continue
        _fill(data, int(region.get("x", 0)), int(region.get("y", 0)), int(region.get("w", 0)), int(region.get("h", 0)))

    patterns = [re.compile(pattern, re.IGNORECASE) for pattern in redact_window_titles]
    if patterns:
        for window in windows:
            if not any(pattern.search(window.title) for pattern in patterns):
                continue
            if window.position is None:
                raise SafetyRefusal(
                    f"refusing screenshot: redaction title {window.title!r} has no reliable Wayland bounds"
                )
            if window.width is None or window.height is None:
                raise SafetyRefusal(f"refusing screenshot: redaction title {window.title!r} has no bounds")
            x = window.position[0] - surface.origin[0]
            y = window.position[1] - surface.origin[1]
            _fill(data, x, y, window.width, window.height)

    return Frame(frame.surface_id, frame.width, frame.height, data, frame.captured_at)


def _fill(data: Any, x: int, y: int, width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        return
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(data.shape[1], x + width)
    y1 = min(data.shape[0], y + height)
    if x0 < x1 and y0 < y1:
        data[y0:y1, x0:x1, :3] = 0
