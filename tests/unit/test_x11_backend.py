from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np

import computer_use_linux.backends as backends
import computer_use_linux.backends.headless as headless
import computer_use_linux.backends.x11 as x11


def test_xgetimage_truecolor_decode_uses_visual_masks() -> None:
    # Little-endian 0x00112233: red=0x11, green=0x22, blue=0x33.
    image = SimpleNamespace(data=bytes((0x33, 0x22, 0x11, 0x00)))
    connection = SimpleNamespace(info=SimpleNamespace(image_byte_order=0))
    visual = SimpleNamespace(red_mask=0x00FF0000, green_mask=0x0000FF00, blue_mask=0x000000FF)

    result = x11._decode_ximage(connection, image, 1, 1, visual, 32)

    assert result.dtype == np.uint8
    assert result.tolist() == [[[0x11, 0x22, 0x33]]]


def test_x11_probe_reports_missing_display_without_importing_a_server(monkeypatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)

    result = x11.probe_x11()

    assert result["xtest_available"] is False
    assert result["available"] is False
    assert result["error"] == "DISPLAY is unset"


def test_auto_detection_chooses_x11_when_xtest_is_available(monkeypatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(backends, "probe_x11", lambda: {"available": True})

    class FakeX11:
        pass

    monkeypatch.setattr(backends, "X11Backend", FakeX11)

    from computer_use_linux.config import load_config

    config = load_config(environ={"XDG_CONFIG_HOME": "/tmp/cul-test-config", "XDG_STATE_HOME": "/tmp/cul-test-state"})
    assert isinstance(backends.detect(config=config), FakeX11)


def test_x11_window_source_keeps_root_relative_position() -> None:
    class FakeX:
        AnyPropertyType = 0
        RevertToParent = 0
        CurrentTime = 0
        Above = 0

    class FakeDisplay:
        def intern_atom(self, _name: str) -> int:
            return 1

    window = SimpleNamespace(
        id=0x42,
        get_geometry=lambda: SimpleNamespace(width=640, height=480),
        translate_coords=lambda _root, _x, _y: SimpleNamespace(x=120, y=80),
        get_full_property=lambda _atom, _property_type: SimpleNamespace(value=b"Demo\0"),
        get_wm_class=lambda: ("demo", "Demo"),
    )
    root = SimpleNamespace(
        query_tree=lambda: SimpleNamespace(children=[window]),
        get_input_focus=lambda: SimpleNamespace(focus=window),
    )
    source = x11.X11WindowSource.__new__(x11.X11WindowSource)
    source._display = FakeDisplay()
    source._X = FakeX
    source._root = root
    source._surface_id = "monitor:x11-0"
    source._refs = {}
    source._lock = threading.RLock()

    values = source.list_windows()

    assert len(values) == 1
    # Keep the assertion on the public model rather than treating None as the only valid
    # geometry value. X11 supplies the real root-relative origin.
    assert values[0].position == (120, 80)
    assert values[0].surface_id == "monitor:x11-0"


def test_x11_keysym_injection_synthesizes_shift_for_level_one_symbols() -> None:
    class FakeX:
        KeyPress = 2
        KeyRelease = 3

    class FakeDisplay:
        def keysym_to_keycode(self, value: int) -> int:
            return {ord("A"): 38, 0xFFE1: 50}[value]

        def keycode_to_keysym(self, keycode: int, column: int) -> int:
            return {(38, 0): ord("a"), (38, 1): ord("A"), (50, 0): 0xFFE1}.get((keycode, column), 0)

        def sync(self) -> None:
            pass

    events: list[tuple[int, int]] = []

    class FakeXTest:
        @staticmethod
        def fake_input(_display: object, event: int, detail: int) -> None:
            events.append((event, detail))

    backend = x11.X11Backend.__new__(x11.X11Backend)
    backend._X = FakeX
    backend._xtest = FakeXTest
    backend._display = FakeDisplay()
    backend._lock = threading.RLock()
    backend._held_keys = set()
    backend._held_buttons = set()
    backend._key_plans = {}

    backend.keysym(ord("A"), True)
    backend.keysym(ord("A"), False)

    assert events == [(2, 50), (2, 38), (3, 38), (3, 50)]
    assert backend.held().empty


def test_headless_probe_reports_missing_xvfb_as_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(headless.shutil, "which", lambda _name: None)

    result = headless.probe_headless()

    assert result["available"] is False
    assert result["error"] == "Xvfb is missing; install xvfb"
