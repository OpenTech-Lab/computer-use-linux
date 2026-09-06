from __future__ import annotations

import contextlib
import os
import threading
import time
from typing import Any

from ..errors import BackendUnavailable, CaptureTimeout, InputError, SurfaceNotFound
from ..pipewire.gst_capture import GstCursorMetadataCapture, GstSubprocessCapture
from ..types import Capability, Frame, HeldState, Surface, SurfaceSpec

SC = "org.gnome.Mutter.ScreenCast"
SC_SESSION = f"{SC}.Session"
SC_STREAM = f"{SC}.Stream"
RD = "org.gnome.Mutter.RemoteDesktop"
RD_SESSION = f"{RD}.Session"
PROPERTIES = "org.freedesktop.DBus.Properties"
DISPLAY_CONFIG = "org.gnome.Mutter.DisplayConfig"


def _gio():
    try:
        import gi

        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib

        return Gio, GLib
    except Exception as exc:  # pragma: no cover - depends on the host interpreter
        raise BackendUnavailable(
            "PyGObject/Gio is unavailable; use the prepared CPython 3.14 .venv with system site-packages"
        ) from exc


def _variant(GLib: Any, signature: str, value: Any) -> Any:
    return GLib.Variant(signature, value)


def _unpack_property(value: Any) -> Any:
    if hasattr(value, "unpack"):
        return value.unpack()
    return value


def _call(bus: Any, destination: str, path: str, interface: str, method: str, parameters: Any = None) -> Any:
    Gio, _ = _gio()
    try:
        return bus.call_sync(destination, path, interface, method, parameters, None, Gio.DBusCallFlags.NONE, 8000, None)
    except Exception as exc:
        raise BackendUnavailable(f"{destination}.{method} failed: {exc}") from exc


def _get_property(bus: Any, destination: str, path: str, interface: str, property_name: str) -> Any:
    _, GLib = _gio()
    reply = _call(
        bus,
        destination,
        path,
        PROPERTIES,
        "Get",
        _variant(GLib, "(ss)", (interface, property_name)),
    )
    return _unpack_property(reply.unpack()[0])


def _version(bus: Any, destination: str, interface: str) -> int:
    return int(_get_property(bus, destination, "/org/gnome/Mutter/" + interface.rsplit(".", 1)[-1], interface, "Version"))


def parse_display_config(state: tuple[Any, ...]) -> list[Surface]:
    """Convert Mutter's GetCurrentState result into monitor-local surfaces."""

    if len(state) < 3:
        raise BackendUnavailable("Mutter.DisplayConfig.GetCurrentState returned an incomplete state")
    monitor_records = state[1]
    logical_records = state[2]
    monitor_by_connector: dict[str, tuple[int, int]] = {}
    for monitor_record in monitor_records:
        spec, modes, _monitor_properties = monitor_record
        connector = str(spec[0])
        chosen = None
        for mode in modes:
            if len(mode) >= 7 and isinstance(mode[6], dict) and mode[6].get("is-current"):
                chosen = mode
                break
        if chosen is None:
            for mode in modes:
                if len(mode) >= 7 and isinstance(mode[6], dict) and mode[6].get("is-preferred"):
                    chosen = mode
                    break
        if chosen is None and modes:
            chosen = modes[0]
        if chosen is not None:
            monitor_by_connector[connector] = (int(chosen[1]), int(chosen[2]))

    geometry: dict[str, tuple[int, int, float]] = {}
    for logical in logical_records:
        x, y, scale, _transform, _primary, connectors, _properties = logical
        for connector_spec in connectors:
            geometry[str(connector_spec[0])] = (int(x), int(y), float(scale))

    surfaces: list[Surface] = []
    for connector, (width, height) in monitor_by_connector.items():
        x, y, scale = geometry.get(connector, (0, 0, 1.0))
        surfaces.append(
            Surface(
                id=f"monitor:{connector}",
                kind="monitor",
                width=width,
                height=height,
                origin=(x, y),
                scale=scale,
                label=f"{connector} ({width}x{height})",
            )
        )
    surfaces.sort(key=lambda surface: (surface.origin[1], surface.origin[0], surface.id))
    return surfaces


def monitor_surfaces(bus: Any) -> list[Surface]:
    state = _call(bus, "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig", DISPLAY_CONFIG, "GetCurrentState")
    return parse_display_config(state.unpack())


def primary_monitor_id(bus: Any) -> str | None:
    """Return the connector marked primary by Mutter's logical monitor state."""

    state = _call(bus, "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig", DISPLAY_CONFIG, "GetCurrentState")
    logical_records = state.unpack()[2]
    for logical in logical_records:
        if len(logical) < 6 or not bool(logical[4]):
            continue
        connectors = logical[5]
        if connectors:
            return f"monitor:{connectors[0][0]}"
    return None


def probe_mutter() -> dict[str, Any]:
    """Probe only the permitted Mutter APIs and return printable diagnostics."""

    Gio, GLib = _gio()
    result: dict[str, Any] = {}
    bus = None
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result["screen_cast_version"] = _version(bus, SC, SC)
        sc_session = _call(
            bus,
            SC,
            "/org/gnome/Mutter/ScreenCast",
            SC,
            "CreateSession",
            _variant(GLib, "(a{sv})", ({},)),
        ).unpack()[0]
        result["screen_cast_session"] = sc_session
    except Exception as exc:
        result["screen_cast_error"] = str(exc)
    try:
        if bus is None:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result["remote_desktop_version"] = _version(bus, RD, RD)
        result["remote_desktop_session"] = _call(
            bus,
            RD,
            "/org/gnome/Mutter/RemoteDesktop",
            RD,
            "CreateSession",
        ).unpack()[0]
    except Exception as exc:
        result["remote_desktop_error"] = str(exc)
    try:
        result["surfaces"] = monitor_surfaces(bus or Gio.bus_get_sync(Gio.BusType.SESSION, None))
    except Exception as exc:
        result["display_config_error"] = str(exc)
    return result


class GnomeMutterBackend:
    name = "gnome_mutter"
    capabilities = (
        Capability.CAPTURE
        | Capability.INPUT_POINTER
        | Capability.INPUT_KEYBOARD
        | Capability.CLIPBOARD
        | Capability.BOUND_CAPTURE_INPUT
    )

    def __init__(self, *, bus: Any | None = None, startup_timeout: float = 10.0):
        Gio, GLib = _gio()
        self._Gio = Gio
        self._GLib = GLib
        self._bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._closed = False
        self._lock = threading.RLock()
        self._held_keys: set[int] = set()
        self._held_buttons: set[int] = set()
        self._streams: dict[str, str] = {}
        self._nodes: dict[str, int] = {}
        self._metadata_streams: dict[str, str] = {}
        self._metadata_nodes: dict[str, int] = {}
        self._subscriptions: list[int] = []
        self._captures: dict[str, GstSubprocessCapture] = {}
        self._metadata_captures: dict[str, GstCursorMetadataCapture] = {}
        self._clipboard_enabled = False
        self._clipboard_text = ""
        self._clipboard_subscription: int | None = None
        self._surfaces = monitor_surfaces(self._bus)
        if not self._surfaces:
            raise BackendUnavailable("Mutter reported no connected monitors")
        try:
            self.primary_surface_id = primary_monitor_id(self._bus) or self._surfaces[0].id
        except Exception:
            self.primary_surface_id = self._surfaces[0].id
        try:
            self._create_bound_session()
            self._create_monitor_streams()
            self._enable_clipboard()
            _call(self._bus, RD, self._rd_path, RD_SESSION, "Start")
            self._wait_for_nodes(startup_timeout)
        except Exception:
            self.close()
            raise

    def _create_bound_session(self) -> None:
        _, GLib = self._gio()
        self._rd_path = _call(
            self._bus,
            RD,
            "/org/gnome/Mutter/RemoteDesktop",
            RD,
            "CreateSession",
        ).unpack()[0]
        session_id = str(_get_property(self._bus, RD, self._rd_path, RD_SESSION, "SessionId"))
        self._sc_path = _call(
            self._bus,
            SC,
            "/org/gnome/Mutter/ScreenCast",
            SC,
            "CreateSession",
            _variant(
                GLib,
                "(a{sv})",
                (
                    {
                        "remote-desktop-session-id": _variant(GLib, "s", session_id),
                        "disable-animations": _variant(GLib, "b", True),
                    },
                ),
            ),
        ).unpack()[0]

    def _create_monitor_streams(self) -> None:
        Gio, GLib = self._gio()
        for surface in self._surfaces:
            connector = surface.id.removeprefix("monitor:")
            stream = _call(
                self._bus,
                SC,
                self._sc_path,
                SC_SESSION,
                "RecordMonitor",
                _variant(
                    GLib,
                    "(sa{sv})",
                    (connector, {"cursor-mode": _variant(GLib, "u", 1)}),
                ),
            ).unpack()[0]
            self._streams[surface.id] = stream
            self._nodes[surface.id] = 0
            metadata_stream = _call(
                self._bus,
                SC,
                self._sc_path,
                SC_SESSION,
                "RecordMonitor",
                _variant(
                    GLib,
                    "(sa{sv})",
                    (connector, {"cursor-mode": _variant(GLib, "u", 2)}),
                ),
            ).unpack()[0]
            self._metadata_streams[surface.id] = metadata_stream
            self._metadata_nodes[surface.id] = 0
            for stream_path in (stream, metadata_stream):
                subscription = self._bus.signal_subscribe(
                    SC,
                    SC_STREAM,
                    "PipeWireStreamAdded",
                    stream_path,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    self._on_pipewire_stream_added,
                )
                self._subscriptions.append(subscription)
        self._subscriptions.append(
            self._bus.signal_subscribe(
                RD,
                RD_SESSION,
                "Closed",
                self._rd_path,
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_session_closed,
            )
        )
        self._subscriptions.append(
            self._bus.signal_subscribe(
                SC,
                SC_SESSION,
                "Closed",
                self._sc_path,
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_session_closed,
            )
        )

    def _enable_clipboard(self) -> None:
        _, GLib = self._gio()
        try:
            _call(self._bus, RD, self._rd_path, RD_SESSION, "EnableClipboard", _variant(GLib, "(a{sv})", ({},)))
            self._clipboard_enabled = True
            self._clipboard_subscription = self._bus.signal_subscribe(
                RD,
                RD_SESSION,
                "SelectionTransfer",
                self._rd_path,
                None,
                self._Gio.DBusSignalFlags.NONE,
                self._on_selection_transfer,
            )
            self._subscriptions.append(self._clipboard_subscription)
        except Exception:
            # Clipboard is an optional extension of the Phase 1 keysym path.
            self._clipboard_enabled = False

    def _gio(self):
        return self._Gio, self._GLib

    def _on_pipewire_stream_added(self, _connection: Any, _sender: str, object_path: str, *_args: Any) -> None:
        try:
            params = _args[-1]
            node_id = int(params.unpack()[0])
        except Exception:
            return
        for surface_id, stream_path in self._streams.items():
            if stream_path == object_path:
                self._nodes[surface_id] = node_id
                return
        for surface_id, stream_path in self._metadata_streams.items():
            if stream_path == object_path:
                self._metadata_nodes[surface_id] = node_id
                return

    def _on_session_closed(self, *_args: Any) -> None:
        self._closed = True

    def _on_selection_transfer(self, *_args: Any) -> None:
        # The complete clipboard protocol is intentionally kept behind this callback. It is
        # only used by explicit clipboard mode; keysym mode never touches clipboard state.
        if not self._clipboard_text:
            return
        try:
            params = _args[-1].unpack()
            serial = int(params[1])
            fd = _call(self._bus, RD, self._rd_path, RD_SESSION, "SelectionWrite", _variant(self._GLib, "(u)", (serial,))).unpack()[0]
            os.write(int(fd), self._clipboard_text.encode("utf-8"))
            os.close(int(fd))
            _call(self._bus, RD, self._rd_path, RD_SESSION, "SelectionWriteDone", _variant(self._GLib, "(ub)", (serial, True)))
        except Exception:
            return

    def _wait_for_nodes(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        context = self._GLib.MainContext.default()
        while time.monotonic() < deadline and not all((*self._nodes.values(), *self._metadata_nodes.values())):
            while context.pending():
                context.iteration(False)
            time.sleep(0.01)
        missing = [surface_id for surface_id, node in self._nodes.items() if not node]
        missing.extend(f"{surface_id} (cursor metadata)" for surface_id, node in self._metadata_nodes.items() if not node)
        if missing:
            raise CaptureTimeout(f"Mutter did not publish PipeWire nodes for: {', '.join(missing)}")

    def list_surfaces(self) -> list[Surface]:
        return list(self._surfaces)

    def open_surface(self, spec: SurfaceSpec) -> Surface:
        if spec.id:
            return self._surface(spec.id)
        if spec.kind != "monitor":
            raise InputError("Phase 1 Mutter backend only supports monitor surfaces")
        if len(self._surfaces) != 1:
            raise InputError("surface_id is required when more than one monitor is connected")
        return self._surfaces[0]

    def close_surface(self, surface_id: str) -> None:
        # Streams belong to the bound session and are torn down together. This method is
        # intentionally idempotent so callers can use a common backend protocol.
        self._surface(surface_id)

    def _surface(self, surface_id: str) -> Surface:
        for surface in self._surfaces:
            if surface.id == surface_id:
                return surface
        raise SurfaceNotFound(f"unknown surface: {surface_id}")

    def grab(self, surface_id: str, *, timeout: float = 2.0) -> Frame:
        self._surface(surface_id)
        if self._closed:
            raise BackendUnavailable("Mutter session is closed")
        node = self._nodes.get(surface_id, 0)
        if not node:
            raise CaptureTimeout(f"no PipeWire node is available for {surface_id}")
        capture = self._captures.get(surface_id)
        if capture is None or capture.node_id != node:
            capture = GstSubprocessCapture(node, surface_id)
            self._captures[surface_id] = capture
        return capture.grab(timeout=timeout)

    def cursor_position(self, surface_id: str, *, timeout: float = 2.0) -> tuple[int, int] | None:
        """Return the exact monitor-local cursor position from SPA metadata."""

        self._surface(surface_id)
        if self._closed:
            raise BackendUnavailable("Mutter session is closed")
        node = self._metadata_nodes.get(surface_id, 0)
        if not node:
            raise CaptureTimeout(f"no cursor metadata node is available for {surface_id}")
        capture = self._metadata_captures.get(surface_id)
        if capture is None or capture.node_id != node:
            capture = GstCursorMetadataCapture(node, surface_id)
            self._metadata_captures[surface_id] = capture
        for _ in range(3):
            _frame, position = capture.grab(timeout=timeout)
            if position is not None:
                return position
        return None

    def move(self, surface_id: str, x: float, y: float) -> None:
        self._surface(surface_id)
        if self._closed:
            raise BackendUnavailable("Mutter session is closed")
        _call(
            self._bus,
            RD,
            self._rd_path,
            RD_SESSION,
            "NotifyPointerMotionAbsolute",
            _variant(self._GLib, "(sdd)", (self._streams[surface_id], float(x), float(y))),
        )

    @staticmethod
    def _button_code(button: int) -> int:
        return {1: 0x110, 2: 0x112, 3: 0x111}.get(int(button), int(button))

    def button(self, button: int, pressed: bool) -> None:
        logical_button = int(button)
        code = self._button_code(logical_button)
        _call(
            self._bus,
            RD,
            self._rd_path,
            RD_SESSION,
            "NotifyPointerButton",
            _variant(self._GLib, "(ib)", (code, bool(pressed))),
        )
        with self._lock:
            if pressed:
                self._held_buttons.add(logical_button)
            else:
                self._held_buttons.discard(logical_button)

    def scroll(self, dx: float, dy: float, *, discrete: bool = True) -> None:
        if discrete:
            if dy:
                self._axis_discrete(0, dy)
            if dx:
                self._axis_discrete(1, dx)
            return
        _call(
            self._bus,
            RD,
            self._rd_path,
            RD_SESSION,
            "NotifyPointerAxis",
            _variant(self._GLib, "(dd u)".replace(" ", ""), (float(dx), float(dy), 0)),
        )

    def _axis_discrete(self, axis: int, amount: float) -> None:
        steps = int(round(amount))
        if steps == 0:
            steps = 1 if amount > 0 else -1
        _call(
            self._bus,
            RD,
            self._rd_path,
            RD_SESSION,
            "NotifyPointerAxisDiscrete",
            _variant(self._GLib, "(ui)", (int(axis), steps)),
        )

    def keysym(self, value: int, pressed: bool) -> None:
        value = int(value)
        _call(
            self._bus,
            RD,
            self._rd_path,
            RD_SESSION,
            "NotifyKeyboardKeysym",
            _variant(self._GLib, "(ub)", (value, bool(pressed))),
        )
        with self._lock:
            if pressed:
                self._held_keys.add(value)
            else:
                self._held_keys.discard(value)

    def held(self) -> HeldState:
        with self._lock:
            return HeldState(frozenset(self._held_keys), frozenset(self._held_buttons))

    def release_all(self) -> None:
        # Never raise: this is called from signal handlers and atexit.
        with self._lock:
            keys = list(self._held_keys)
            buttons = list(self._held_buttons)
        for value in reversed(keys):
            with contextlib.suppress(Exception):
                self.keysym(value, False)
        for button in reversed(buttons):
            with contextlib.suppress(Exception):
                self.button(button, False)
        with self._lock:
            self._held_keys.clear()
            self._held_buttons.clear()

    def type_clipboard(self, text: str) -> None:
        if not self._clipboard_enabled:
            raise BackendUnavailable(
                "clipboard typing is unavailable in this Mutter session; use short ASCII keysym mode "
                "or install/enable a clipboard provider"
            )
        self._clipboard_text = text
        _, GLib = self._gio()
        _call(
            self._bus,
            RD,
            self._rd_path,
            RD_SESSION,
            "SetSelection",
            _variant(GLib, "(a{sv})", ({"mime-types": _variant(GLib, "as", (["text/plain", "text/plain;charset=utf-8"],))},)),
        )
        from ..keys import parse_chord

        for value in parse_chord("ctrl+v"):
            self.keysym(value, True)
        for value in reversed(parse_chord("ctrl+v")):
            self.keysym(value, False)

    def close(self) -> None:
        if self._closed and not hasattr(self, "_rd_path"):
            return
        self.release_all()
        self._closed = True
        for subscription in getattr(self, "_subscriptions", []):
            with contextlib.suppress(Exception):
                self._bus.signal_unsubscribe(subscription)
        self._subscriptions = []
        for capture in getattr(self, "_metadata_captures", {}).values():
            with contextlib.suppress(Exception):
                capture.close()
        self._metadata_captures = {}
        if hasattr(self, "_rd_path"):
            with contextlib.suppress(Exception):
                _call(self._bus, RD, self._rd_path, RD_SESSION, "Stop")
        if hasattr(self, "_sc_path"):
            with contextlib.suppress(Exception):
                _call(self._bus, SC, self._sc_path, SC_SESSION, "Stop")
