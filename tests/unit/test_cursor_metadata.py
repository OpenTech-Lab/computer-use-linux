from __future__ import annotations

import threading

import computer_use_linux.backends.gnome_mutter as gnome_mutter
from computer_use_linux.backends.gnome_mutter import GnomeMutterBackend
from computer_use_linux.types import Surface


def test_cursor_position_retries_identical_sample_after_pointer_move(monkeypatch) -> None:
    surface_id = "monitor:test"

    class FakeCapture:
        node_id = 7

        def __init__(self) -> None:
            self.positions = iter(((40, 30), (100, 100)))
            self.calls = 0

        def grab(self, *, timeout: float) -> tuple[object, tuple[int, int]]:
            assert timeout > 0
            self.calls += 1
            return object(), next(self.positions)

        def close(self) -> None:
            pass

    capture = FakeCapture()
    monkeypatch.setattr(gnome_mutter, "GstCursorMetadataCapture", lambda _node, _surface: capture)
    backend = GnomeMutterBackend.__new__(GnomeMutterBackend)
    backend._closed = False
    backend._surfaces = [Surface(surface_id, "monitor", 1920, 1080, (0, 0), 1.0, "test")]
    backend._metadata_nodes = {surface_id: capture.node_id}
    backend._lock = threading.RLock()
    backend._cursor_move_generation = 2
    backend._cursor_read_generations = {surface_id: 1}
    backend._cursor_last_positions = {surface_id: (40, 30)}

    assert backend.cursor_position(surface_id, timeout=1.0) == (100, 100)
    assert capture.calls == 2
