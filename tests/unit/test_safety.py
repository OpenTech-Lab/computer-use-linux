from __future__ import annotations

import numpy as np
import pytest

from computer_use_linux.errors import SafetyRefusal
from computer_use_linux.safety import redact_frame, require_confirmation
from computer_use_linux.types import Frame, Surface, WindowInfo


def test_static_redaction_is_applied_to_the_frame_copy() -> None:
    surface = Surface("monitor:test", "monitor", 20, 10, (0, 0), 1.0, "test")
    data = np.full((10, 20, 3), 255, dtype=np.uint8)
    frame = Frame(surface.id, 20, 10, data, 1.0)
    redacted = redact_frame(frame, surface, redact_regions=[{"surface_id": surface.id, "x": 2, "y": 3, "w": 4, "h": 2}])
    assert np.all(redacted.data[3:5, 2:6] == 0)
    assert np.all(frame.data == 255)


def test_title_redaction_fails_closed_when_wayland_bounds_are_unknown() -> None:
    surface = Surface("monitor:test", "monitor", 20, 10, (0, 0), 1.0, "test")
    frame = Frame(surface.id, 20, 10, np.ones((10, 20, 3), dtype=np.uint8), 1.0)
    window = WindowInfo("w", "Password prompt", "app", "dialog", 10, 5, None, False)
    with pytest.raises(SafetyRefusal):
        redact_frame(frame, surface, redact_window_titles=["password"], windows=[window])


def test_confirmation_default_only_gates_audited_actions() -> None:
    require_confirmation(action="key", chord="ctrl+shift+p", confirm=False, confirm_mode="destructive")
    with pytest.raises(SafetyRefusal):
        require_confirmation(action="key", chord="alt+F4", confirm=False, confirm_mode="destructive")
    require_confirmation(action="key", chord="super", confirm=True, confirm_mode="destructive")

