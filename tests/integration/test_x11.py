from __future__ import annotations

import pytest

from computer_use_linux.backends.x11 import X11Backend, probe_x11

pytestmark = [pytest.mark.requires_session, pytest.mark.disruptive]


def test_x11_backend_captures_and_injects_on_the_selected_root() -> None:
    probe = probe_x11()
    if not probe["available"]:
        pytest.skip(str(probe.get("error", "X11/XTEST/XGetImage is unavailable")))
    backend = X11Backend()
    try:
        surface = backend.list_surfaces()[0]
        frame = backend.grab(surface.id)
        assert (frame.width, frame.height) == (surface.width, surface.height)
        backend.move(surface.id, 17, 23)
        assert backend.cursor_position(surface.id) == (17, 23)
    finally:
        backend.close()
