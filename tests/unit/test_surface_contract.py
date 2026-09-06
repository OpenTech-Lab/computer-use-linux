from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from computer_use_linux.config import load_config
from computer_use_linux.errors import BackendUnavailable
from computer_use_linux.session import Session
from computer_use_linux.types import Capability, Frame, HeldState, Surface


def test_named_surface_screenshot_rejects_a_frame_with_wrong_dimensions(tmp_path) -> None:
    requested = Surface("monitor:HDMI-1", "monitor", 1920, 1080, (0, 0), 1.0, "HDMI-1")

    class WrongSurfaceBackend:
        capabilities = Capability.CAPTURE
        primary_surface_id = requested.id

        def list_surfaces(self) -> list[Surface]:
            return [requested]

        def grab(self, surface_id: str, *, timeout: float) -> Frame:
            del timeout
            return Frame(surface_id, 1280, 720, np.zeros((720, 1280, 3), dtype=np.uint8), 0.0)

        def held(self) -> HeldState:
            return HeldState()

        def release_all(self) -> None:
            pass

        def close(self) -> None:
            pass

    config = load_config(
        tmp_path / "config.toml",
        environ={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )
    window_source = SimpleNamespace(sensitive_focused=lambda: None, list_windows=lambda: [])
    session = Session(backend=WrongSurfaceBackend(), config=config, window_source=window_source)
    try:
        with pytest.raises(BackendUnavailable, match=r"monitor:HDMI-1.*1920x1080.*1280x720"):
            session.screenshot(surface_id=requested.id, max_width=0)
    finally:
        session.close()
