from __future__ import annotations

import os

import pytest

from computer_use_linux.backends.headless import HeadlessBackend, probe_headless

pytestmark = pytest.mark.requires_session


def test_headless_backend_is_a_real_private_x_server() -> None:
    probe = probe_headless()
    if not probe["available"]:
        pytest.skip(str(probe.get("error", "Xvfb is unavailable")))
    previous_display = os.environ.get("DISPLAY")
    backend = HeadlessBackend(width=320, height=200)
    try:
        surface = backend.list_surfaces()[0]
        assert (surface.width, surface.height) == (320, 200)
        frame = backend.grab(surface.id)
        assert (frame.width, frame.height) == (320, 200)
        backend.move(surface.id, 17, 23)
        assert backend.cursor_position(surface.id) == (17, 23)
    finally:
        backend.close()
    assert os.environ.get("DISPLAY") == previous_display
