from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import computer_use_linux.backends as backends
import computer_use_linux.backends.gnome_mutter as gnome_mutter
from computer_use_linux.backends import _gnome_backend
from computer_use_linux.backends.gnome_mutter import GnomeMutterBackend
from computer_use_linux.cli import _build_parser
from computer_use_linux.config import load_config
from computer_use_linux.errors import BackendUnavailable
from computer_use_linux.session import Session
from computer_use_linux.types import Capability, Surface


class _Variant:
    def __init__(self, signature: str, value: object):
        self.signature = signature
        self.value = value


class _GLib:
    @staticmethod
    def Variant(signature: str, value: object) -> _Variant:
        return _Variant(signature, value)


def test_record_virtual_mode_pins_size_and_refresh() -> None:
    properties = gnome_mutter.virtual_mode_properties(_GLib, 1600, 900, 24.0)

    assert properties["is-platform"].value is True
    assert properties["modes"].signature == "aa{sv}"
    mode = properties["modes"].value[0]
    assert mode["size"].value == (1600, 900)
    assert mode["refresh-rate"].value == 24.0
    assert mode["is-preferred"].value is True


def test_virtual_grab_retries_capture_timeout_before_returning_a_frame(monkeypatch) -> None:
    surface = Surface("virtual:0", "virtual", 1280, 720, (0, 0), 1.0, "virtual")
    frame = object()

    class FakeCapture:
        node_id = 41

        def __init__(self, _node: int, _surface: str, **_options: object) -> None:
            self.calls = 0

        def grab(self, *, timeout: float) -> object:
            self.calls += 1
            assert timeout == 0.5
            if self.calls < 3:
                raise gnome_mutter.CaptureTimeout("empty virtual stream")
            return frame

    capture: FakeCapture | None = None

    def make_capture(*args: object, **kwargs: object) -> FakeCapture:
        nonlocal capture
        capture = FakeCapture(*args, **kwargs)
        return capture

    monkeypatch.setattr(gnome_mutter, "GstSubprocessCapture", make_capture)
    monkeypatch.setattr(gnome_mutter.time, "sleep", lambda _delay: None)
    backend = GnomeMutterBackend.__new__(GnomeMutterBackend)
    backend._closed = False
    backend._surfaces = [surface]
    backend._nodes = {surface.id: 41}
    backend._captures = {}
    backend._virtual_width = 1280
    backend._virtual_height = 720
    backend._virtual_refresh = 30.0

    assert backend.grab(surface.id, timeout=0.5) is frame
    assert capture is not None and capture.calls == 3


def test_unknown_physical_stream_is_recreated_after_virtual_stream_changes(monkeypatch) -> None:
    surface_id = "monitor:test"
    surface = Surface(surface_id, "monitor", 1920, 1080, (0, 0), 1.0, "test")
    backend = GnomeMutterBackend.__new__(GnomeMutterBackend)
    backend._closed = False
    backend._bus = object()
    backend._rd_path = "/rd/session"
    backend._sc_path = "/sc/session"
    backend._Gio = object()
    backend._GLib = _GLib
    backend._streams = {surface_id: "/stream/old-pixels"}
    backend._metadata_streams = {surface_id: "/stream/old-metadata"}
    backend._nodes = {surface_id: 11}
    backend._metadata_nodes = {surface_id: 12}
    backend._captures = {}
    backend._subscriptions = []
    backend._surfaces = [surface]
    subscribed: list[str] = []
    backend._subscribe_stream = subscribed.append
    backend._wait_for_nodes = lambda _timeout, **_kwargs: None
    calls: list[tuple[str, str]] = []
    next_stream = iter(("/stream/new-pixels", "/stream/new-metadata"))

    def fake_call(_bus: object, _destination: str, _path: str, _interface: str, method: str, parameters: object = None) -> object:
        stream_path = getattr(parameters, "value", (None,))[0] if parameters is not None else None
        calls.append((method, str(stream_path)))
        if method == "NotifyPointerMotionAbsolute" and stream_path == "/stream/old-pixels":
            raise BackendUnavailable("GDBus.Error: org.gnome.Mutter.RemoteDesktop: Unknown stream (0)")
        if method == "RecordMonitor":
            return SimpleNamespace(unpack=lambda: (next(next_stream),))
        return SimpleNamespace(unpack=lambda: ())

    monkeypatch.setattr(gnome_mutter, "_call", fake_call)
    backend._notify_motion(surface, 150, 120)

    assert backend._streams[surface_id] == "/stream/new-pixels"
    assert backend._metadata_streams[surface_id] == "/stream/new-metadata"
    assert subscribed == ["/stream/new-pixels", "/stream/new-metadata"]
    assert calls[-2:] == [
        ("NotifyPointerMotionAbsolute", "/stream/new-pixels"),
        ("NotifyPointerMotionAbsolute", "/stream/new-metadata"),
    ]


def test_session_isolated_mode_selects_backend_virtual_surface(tmp_path) -> None:
    class FakeBackend:
        name = "fake"
        capabilities = Capability.CAPTURE | Capability.INPUT_POINTER | Capability.INPUT_KEYBOARD
        primary_surface_id = "monitor:primary"
        isolated_surface_id = "virtual:0"

        def __init__(self) -> None:
            self.surfaces = [
                Surface("monitor:primary", "monitor", 100, 80, (0, 0), 1.0, "primary"),
                Surface("virtual:0", "virtual", 100, 80, (0, 0), 1.0, "virtual"),
            ]

        def list_surfaces(self) -> list[Surface]:
            return self.surfaces

        def held(self):
            from computer_use_linux.types import HeldState

            return HeldState()

        def release_all(self) -> None:
            pass

        def close(self) -> None:
            pass

    config = load_config(
        tmp_path / "config.toml",
        environ={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )
    session = Session(backend=FakeBackend(), config=config, isolated=True)
    try:
        assert session.config.isolated is True
        assert session.active_surface_id == "virtual:0"
    finally:
        session.close()


def test_session_isolated_mode_refuses_a_backend_without_an_isolated_surface(tmp_path) -> None:
    class PhysicalOnlyBackend:
        name = "physical-only"
        capabilities = Capability.CAPTURE | Capability.INPUT_POINTER | Capability.INPUT_KEYBOARD
        primary_surface_id = "monitor:primary"

        def list_surfaces(self) -> list[Surface]:
            return [Surface("monitor:primary", "monitor", 100, 80, (0, 0), 1.0, "primary")]

        def held(self):
            from computer_use_linux.types import HeldState

            return HeldState()

        def release_all(self) -> None:
            pass

        def close(self) -> None:
            pass

    config = load_config(
        tmp_path / "config.toml",
        environ={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )
    with pytest.raises(BackendUnavailable, match="no isolated surface"):
        Session(backend=PhysicalOnlyBackend(), config=config, isolated=True)


def test_mutter_virtual_surface_routes_all_input_to_the_bound_virtual_backend() -> None:
    class VirtualBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def set_active_surface(self, surface_id: str) -> None:
            self.calls.append(("select", surface_id))

        def move(self, surface_id: str, x: float, y: float) -> None:
            self.calls.append(("move", surface_id, x, y))

        def button(self, button: int, pressed: bool) -> None:
            self.calls.append(("button", button, pressed))

        def scroll(self, dx: float, dy: float, *, discrete: bool = True) -> None:
            self.calls.append(("scroll", dx, dy, discrete))

        def keysym(self, value: int, pressed: bool) -> None:
            self.calls.append(("keysym", value, pressed))

        def type_clipboard(self, text: str) -> None:
            self.calls.append(("clipboard", text))

    physical = Surface("monitor:primary", "monitor", 100, 80, (0, 0), 1.0, "primary")
    virtual = Surface("virtual:0", "virtual", 100, 80, (0, 0), 1.0, "virtual")
    child = VirtualBackend()
    backend = GnomeMutterBackend.__new__(GnomeMutterBackend)
    backend._surfaces = [physical, virtual]
    backend._virtual_backend = child
    backend.primary_surface_id = physical.id
    backend._active_input_surface_id = physical.id

    backend.set_active_surface(virtual.id)
    backend.move(virtual.id, 12, 13)
    backend.button(1, True)
    backend.scroll(0, -1)
    backend.keysym(ord("x"), True)
    backend.type_clipboard("hello")

    assert child.calls == [
        ("select", "virtual:0"),
        ("select", "virtual:0"),
        ("move", "virtual:0", 12, 13),
        ("button", 1, True),
        ("scroll", 0, -1, True),
        ("keysym", ord("x"), True),
        ("clipboard", "hello"),
    ]


def test_cli_accepts_isolated_before_or_after_command() -> None:
    parser = _build_parser()

    assert parser.parse_args(["--isolated", "surfaces"]).isolated is True
    assert parser.parse_args(["surfaces", "--isolated"]).isolated is True


def test_gnome_detection_passes_virtual_dimensions_and_isolated_policy(monkeypatch, tmp_path) -> None:
    config = load_config(
        tmp_path / "config.toml",
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
    )
    config = replace(
        config,
        isolated=True,
        virtual_width=1600,
        virtual_height=900,
        virtual_refresh=24.0,
    )
    captured: dict[str, object] = {}

    class FakeBackend:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(backends, "GnomeMutterBackend", FakeBackend)
    backend = _gnome_backend(
        config,
        probe={"screen_cast_version": 5, "remote_desktop_version": 2, "virtual_surface_available": True},
    )

    assert isinstance(backend, FakeBackend)
    assert captured == {
        "include_virtual": True,
        "virtual_only": True,
        "virtual_width": 1600,
        "virtual_height": 900,
        "virtual_refresh": 24.0,
    }


def test_gnome_detection_keeps_virtual_surface_opt_in(monkeypatch, tmp_path) -> None:
    config = load_config(
        tmp_path / "config.toml",
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
    )
    captured: dict[str, object] = {}

    class FakeBackend:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(backends, "GnomeMutterBackend", FakeBackend)
    backend = _gnome_backend(
        config,
        probe={"screen_cast_version": 5, "remote_desktop_version": 2, "virtual_surface_available": True},
    )

    assert isinstance(backend, FakeBackend)
    assert captured == {
        "include_virtual": False,
        "virtual_only": False,
        "virtual_width": 1280,
        "virtual_height": 720,
        "virtual_refresh": 30.0,
    }
