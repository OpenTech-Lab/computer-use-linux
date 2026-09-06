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
_VIRTUAL_CAPTURE_ATTEMPTS = 4
_PHYSICAL_CAPTURE_ATTEMPTS = 2
_VIRTUAL_CAPTURE_RETRY_DELAY = 0.35


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


def virtual_mode_properties(GLib: Any, width: int, height: int, refresh: float) -> dict[str, Any]:
    """Build the undocumented-but-supported RecordVirtual mode description.

    Mutter's public XML only documents ``cursor-mode`` and ``is-platform``.  GNOME 50 also
    accepts ``modes``; putting the preferred mode on the D-Bus request prevents the first
    PipeWire negotiation from choosing the 1x1 fallback used by an unconstrained consumer.
    """

    mode = {
        "size": _variant(GLib, "(uu)", (int(width), int(height))),
        "refresh-rate": _variant(GLib, "d", float(refresh)),
        "is-preferred": _variant(GLib, "b", True),
    }
    return {
        "is-platform": _variant(GLib, "b", True),
        "modes": _variant(GLib, "aa{sv}", [mode]),
    }


def probe_virtual_surface(
    bus: Any,
    *,
    width: int = 1280,
    height: int = 720,
    refresh: float = 30.0,
) -> tuple[bool, str]:
    """Probe RecordVirtual without starting a stream or changing the monitor layout."""

    _, GLib = _gio()
    rd_path = None
    sc_path = None
    try:
        rd_path = _call(bus, RD, "/org/gnome/Mutter/RemoteDesktop", RD, "CreateSession").unpack()[0]
        session_id = str(_get_property(bus, RD, rd_path, RD_SESSION, "SessionId"))
        sc_path = _call(
            bus,
            SC,
            "/org/gnome/Mutter/ScreenCast",
            SC,
            "CreateSession",
            _variant(GLib, "(a{sv})", ({"remote-desktop-session-id": _variant(GLib, "s", session_id)},)),
        ).unpack()[0]
        try:
            _call(
                bus,
                SC,
                sc_path,
                SC_SESSION,
                "RecordVirtual",
                _variant(GLib, "(a{sv})", (virtual_mode_properties(GLib, width, height, refresh),)),
            )
            return True, f"RecordVirtual available ({width}x{height}@{refresh:g})"
        except BackendUnavailable:
            # Older Mutter versions may have RecordVirtual but not the preferred-mode extension.
            _call(
                bus,
                SC,
                sc_path,
                SC_SESSION,
                "RecordVirtual",
                _variant(
                    GLib,
                    "(a{sv})",
                    (
                        {
                            "is-platform": _variant(GLib, "b", True),
                            "cursor-mode": _variant(GLib, "u", 2),
                        },
                    ),
                ),
            )
            return True, "RecordVirtual available (PipeWire mode negotiation)"
    except Exception as exc:
        return False, str(exc)
    finally:
        # A probe must not leave a remote-control or virtual-monitor session alive.  In
        # particular, an un-stopped RecordVirtual object can outlive this function on the
        # session bus and perturb the next real backend session.
        if sc_path is not None:
            with contextlib.suppress(Exception):
                _call(bus, SC, sc_path, SC_SESSION, "Stop")
        if rd_path is not None:
            with contextlib.suppress(Exception):
                _call(bus, RD, rd_path, RD_SESSION, "Stop")


def probe_mutter(*, probe_virtual: bool = False) -> dict[str, Any]:
    """Probe Mutter capabilities without creating a virtual stream by default."""

    Gio, GLib = _gio()
    result: dict[str, Any] = {}
    bus = None
    sc_session = None
    rd_session = None
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result["screen_cast_version"] = _version(bus, SC, SC)
        sc_session = result["screen_cast_session"] = _call(
            bus,
            SC,
            "/org/gnome/Mutter/ScreenCast",
            SC,
            "CreateSession",
            _variant(GLib, "(a{sv})", ({},)),
        ).unpack()[0]
    except Exception as exc:
        result["screen_cast_error"] = str(exc)
    try:
        if bus is None:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result["remote_desktop_version"] = _version(bus, RD, RD)
        rd_session = result["remote_desktop_session"] = _call(
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
    if bus is not None and probe_virtual:
        available, detail = probe_virtual_surface(bus)
        result["virtual_surface_available"] = available
        if available:
            result["virtual_surface"] = detail
        else:
            result["virtual_surface_error"] = detail
    if bus is not None:
        if sc_session is not None:
            with contextlib.suppress(Exception):
                _call(bus, SC, sc_session, SC_SESSION, "Stop")
        if rd_session is not None:
            with contextlib.suppress(Exception):
                _call(bus, RD, rd_session, RD_SESSION, "Stop")
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

    def __init__(
        self,
        *,
        bus: Any | None = None,
        startup_timeout: float = 10.0,
        include_virtual: bool = False,
        virtual_only: bool = False,
        virtual_width: int = 1280,
        virtual_height: int = 720,
        virtual_refresh: float = 30.0,
    ):
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
        self._cursor_move_generation = 0
        self._cursor_read_generations: dict[str, int] = {}
        self._cursor_last_positions: dict[str, tuple[int, int]] = {}
        self._subscriptions: list[int] = []
        self._captures: dict[str, GstSubprocessCapture] = {}
        self._clipboard_enabled = False
        self._clipboard_text = ""
        self._clipboard_subscription: int | None = None
        self._virtual_backend: GnomeMutterBackend | None = None
        self._active_input_surface_id: str | None = None
        self._physical_surfaces = monitor_surfaces(self._bus)
        self._include_virtual = bool(include_virtual or virtual_only)
        self._virtual_only = bool(virtual_only)
        if self._include_virtual and not self._virtual_only:
            raise BackendUnavailable(
                "Mutter physical and virtual surfaces cannot share one session; use --isolated "
                "for a virtual-only session"
            )
        self._virtual_width = int(virtual_width)
        self._virtual_height = int(virtual_height)
        self._virtual_refresh = float(virtual_refresh)
        self.capabilities = type(self).capabilities | (Capability.VIRTUAL_SURFACE if self._include_virtual else Capability(0))
        self._virtual_surface = Surface(
            id="virtual:0",
            kind="virtual",
            width=self._virtual_width,
            height=self._virtual_height,
            origin=(0, 0),
            scale=1.0,
            label=f"Virtual remote monitor ({self._virtual_width}x{self._virtual_height}@{self._virtual_refresh:g})",
        )
        self._surfaces = [self._virtual_surface] if self._virtual_only else list(self._physical_surfaces)
        if self._include_virtual and not self._virtual_only:
            self._surfaces.append(self._virtual_surface)
        if not self._surfaces:
            raise BackendUnavailable("Mutter reported no connected monitors")
        if self._virtual_only:
            self.primary_surface_id = self._virtual_surface.id
        else:
            try:
                self.primary_surface_id = primary_monitor_id(self._bus) or self._physical_surfaces[0].id
            except Exception:
                self.primary_surface_id = self._surfaces[0].id
        self._active_input_surface_id = self.primary_surface_id
        self.isolated_surface_id = self._virtual_surface.id if self._include_virtual else None
        try:
            self._create_bound_session()
            if not self._virtual_only:
                self._create_monitor_streams()
            if self._include_virtual and self._virtual_only:
                self._create_virtual_stream()
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
        _, GLib = self._gio()
        for surface in self._physical_surfaces:
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
                self._subscribe_stream(stream_path)
        self._subscriptions.append(
            self._bus.signal_subscribe(
                RD,
                RD_SESSION,
                "Closed",
                self._rd_path,
                None,
                self._Gio.DBusSignalFlags.NONE,
                self._on_session_closed,
            )
        )

    def _subscribe_stream(self, stream_path: str) -> None:
        subscription = self._bus.signal_subscribe(
            SC,
            SC_STREAM,
            "PipeWireStreamAdded",
            stream_path,
            None,
            self._Gio.DBusSignalFlags.NONE,
            self._on_pipewire_stream_added,
        )
        self._subscriptions.append(subscription)

    def _create_virtual_stream(self) -> None:
        _, GLib = self._gio()
        properties = virtual_mode_properties(GLib, self._virtual_width, self._virtual_height, self._virtual_refresh)
        try:
            stream = _call(
                self._bus,
                SC,
                self._sc_path,
                SC_SESSION,
                "RecordVirtual",
                _variant(GLib, "(a{sv})", (properties | {"cursor-mode": _variant(GLib, "u", 2)},)),
            ).unpack()[0]
        except BackendUnavailable:
            # Keep the backend usable on Mutter releases that predate preferred modes.  The
            # virtual capture reader still supplies fixed caps, so it can negotiate the size.
            stream = _call(
                self._bus,
                SC,
                self._sc_path,
                SC_SESSION,
                "RecordVirtual",
                _variant(
                    GLib,
                    "(a{sv})",
                    (
                        {
                            "is-platform": _variant(GLib, "b", True),
                            "cursor-mode": _variant(GLib, "u", 2),
                        },
                    ),
                ),
            ).unpack()[0]
        self._streams[self._virtual_surface.id] = stream
        self._nodes[self._virtual_surface.id] = 0
        # A virtual stream carries cursor metadata and pixels in the same stream. Do not create
        # a second RecordVirtual stream: each such call creates another virtual monitor.
        self._metadata_streams[self._virtual_surface.id] = stream
        self._metadata_nodes[self._virtual_surface.id] = 0
        self._subscribe_stream(stream)
        self._subscriptions.append(
            self._bus.signal_subscribe(
                SC,
                SC_SESSION,
                "Closed",
                self._sc_path,
                None,
                self._Gio.DBusSignalFlags.NONE,
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
        matched = False
        for surface_id, stream_path in self._streams.items():
            if stream_path == object_path:
                self._nodes[surface_id] = node_id
                matched = True
        for surface_id, stream_path in self._metadata_streams.items():
            if stream_path == object_path:
                self._metadata_nodes[surface_id] = node_id
                matched = True
        if not matched:
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

    def _wait_for_nodes(self, timeout: float, *, required_surface_ids: set[str] | None = None) -> None:
        surface_ids = tuple(required_surface_ids or self._nodes.keys())
        deadline = time.monotonic() + timeout
        context = self._GLib.MainContext.default()
        while time.monotonic() < deadline and not all(
            self._nodes.get(surface_id, 0) and self._metadata_nodes.get(surface_id, 0) for surface_id in surface_ids
        ):
            while context.pending():
                context.iteration(False)
            time.sleep(0.01)
        missing = [surface_id for surface_id in surface_ids if not self._nodes.get(surface_id, 0)]
        missing.extend(f"{surface_id} (cursor metadata)" for surface_id in surface_ids if not self._metadata_nodes.get(surface_id, 0))
        if missing:
            raise CaptureTimeout(f"Mutter did not publish PipeWire nodes for: {', '.join(missing)}")

    def list_surfaces(self) -> list[Surface]:
        return list(self._surfaces)

    def open_surface(self, spec: SurfaceSpec) -> Surface:
        if spec.id:
            return self._surface(spec.id)
        if spec.kind not in {"monitor", "virtual"}:
            raise InputError("Mutter supports monitor and virtual surfaces")
        matching = [surface for surface in self._surfaces if surface.kind == spec.kind]
        if len(matching) != 1:
            raise InputError("surface_id is required when more than one surface matches the requested kind")
        return matching[0]

    def close_surface(self, surface_id: str) -> None:
        # Streams belong to the bound session and are torn down together. This method is
        # intentionally idempotent so callers can use a common backend protocol.
        self._surface(surface_id)

    def _surface(self, surface_id: str) -> Surface:
        for surface in self._surfaces:
            if surface.id == surface_id:
                return surface
        raise SurfaceNotFound(f"unknown surface: {surface_id}")

    def _backend_for_surface(self, surface: Surface) -> GnomeMutterBackend:
        if surface.kind == "virtual":
            return getattr(self, "_virtual_backend", None) or self
        return self

    def set_active_surface(self, surface_id: str) -> None:
        surface = self._surface(surface_id)
        self._active_input_surface_id = surface.id
        target = self._backend_for_surface(surface)
        if target is not self:
            target.set_active_surface(surface.id)

    def _active_backend(self) -> GnomeMutterBackend:
        surface_id = self._active_input_surface_id or self.primary_surface_id
        return self._backend_for_surface(self._surface(surface_id))

    def _capture_options(self, surface: Surface) -> dict[str, int | float]:
        if surface.kind != "virtual":
            return {}
        return {
            "width": self._virtual_width,
            "height": self._virtual_height,
            "refresh": self._virtual_refresh,
        }

    def _recreate_monitor_streams(self, surface: Surface) -> None:
        """Rebind a physical monitor after a virtual-monitor topology change.

        Mutter 50 can invalidate physical stream objects when a virtual monitor is negotiated.
        The remote desktop session then reports ``Unknown stream (0)`` for the old object path.
        Re-recording the affected monitor gives it fresh stream paths; this is deliberately
        bounded to one surface and is only used as a recovery path.
        """

        if surface.kind != "monitor":
            raise BackendUnavailable(f"cannot recreate a {surface.kind} stream")
        _, GLib = self._gio()
        connector = surface.id.removeprefix("monitor:")
        for cursor_mode, stream_map, node_map in (
            (1, self._streams, self._nodes),
            (2, self._metadata_streams, self._metadata_nodes),
        ):
            stream = _call(
                self._bus,
                SC,
                self._sc_path,
                SC_SESSION,
                "RecordMonitor",
                _variant(
                    GLib,
                    "(sa{sv})",
                    (connector, {"cursor-mode": _variant(GLib, "u", cursor_mode)}),
                ),
            ).unpack()[0]
            stream_map[surface.id] = stream
            node_map[surface.id] = 0
            self._subscribe_stream(stream)
        self._captures.pop(surface.id, None)
        self._wait_for_nodes(5.0, required_surface_ids={surface.id})

    def _ensure_monitor_streams(self, surface: Surface) -> None:
        if surface.kind != "monitor" or not getattr(self, "_include_virtual", False) or getattr(self, "_virtual_only", False):
            return
        if not self._nodes.get(surface.id, 0) or not self._metadata_nodes.get(surface.id, 0):
            self._recreate_monitor_streams(surface)

    @staticmethod
    def _is_unknown_stream(exc: Exception) -> bool:
        return "Unknown stream" in str(exc)

    def _notify_motion(self, surface: Surface, x: float, y: float) -> None:
        self._ensure_monitor_streams(surface)
        stream_paths = [self._streams[surface.id]]
        metadata_stream = self._metadata_streams.get(surface.id)
        if metadata_stream and metadata_stream not in stream_paths:
            stream_paths.append(metadata_stream)
        try:
            for stream_path in stream_paths:
                _call(
                    self._bus,
                    RD,
                    self._rd_path,
                    RD_SESSION,
                    "NotifyPointerMotionAbsolute",
                    _variant(self._GLib, "(sdd)", (stream_path, float(x), float(y))),
                )
        except BackendUnavailable as exc:
            if not self._is_unknown_stream(exc) or surface.kind != "monitor":
                raise
            self._recreate_monitor_streams(surface)
            recovered_paths = [self._streams[surface.id]]
            recovered_metadata = self._metadata_streams.get(surface.id)
            if recovered_metadata and recovered_metadata not in recovered_paths:
                recovered_paths.append(recovered_metadata)
            for stream_path in recovered_paths:
                _call(
                    self._bus,
                    RD,
                    self._rd_path,
                    RD_SESSION,
                    "NotifyPointerMotionAbsolute",
                    _variant(self._GLib, "(sdd)", (stream_path, float(x), float(y))),
                )

    def grab(self, surface_id: str, *, timeout: float | None = None) -> Frame:
        surface = self._surface(surface_id)
        target = self._backend_for_surface(surface)
        if target is not self:
            return target.grab(surface_id, timeout=timeout)
        if self._closed:
            raise BackendUnavailable("Mutter session is closed")
        self._ensure_monitor_streams(surface)
        node = self._nodes.get(surface_id, 0)
        if not node:
            raise CaptureTimeout(f"no PipeWire node is available for {surface_id}")
        capture = self._captures.get(surface_id)
        options = self._capture_options(surface)
        if capture is None or capture.node_id != node:
            capture = GstSubprocessCapture(node, surface_id, **options)
            self._captures[surface_id] = capture
        last_timeout: CaptureTimeout | None = None
        attempts = _VIRTUAL_CAPTURE_ATTEMPTS if surface.kind == "virtual" else _PHYSICAL_CAPTURE_ATTEMPTS
        for attempt in range(attempts):
            try:
                return capture.grab(timeout=timeout)
            except CaptureTimeout as exc:
                last_timeout = exc
                if attempt + 1 < attempts:
                    # An empty RecordVirtual surface, or a temporarily starved physical
                    # stream, may not publish a buffer immediately. Match the bounded retry
                    # discipline used by the cursor selftest; a caller can still turn a final
                    # timeout into SKIPPED.
                    time.sleep(_VIRTUAL_CAPTURE_RETRY_DELAY)
        assert last_timeout is not None
        raise last_timeout

    def cursor_position(self, surface_id: str, *, timeout: float | None = None) -> tuple[int, int] | None:
        """Return the exact monitor-local cursor position from SPA metadata."""

        from ..pipewire.gst_capture import _capture_timeout

        timeout = _capture_timeout(timeout)
        surface = self._surface(surface_id)
        target = self._backend_for_surface(surface)
        if target is not self:
            return target.cursor_position(surface_id, timeout=timeout)
        if self._closed:
            raise BackendUnavailable("Mutter session is closed")
        self._ensure_monitor_streams(surface)
        node = self._metadata_nodes.get(surface_id, 0)
        if not node:
            raise CaptureTimeout(f"no cursor metadata node is available for {surface_id}")
        with self._lock:
            move_generation = self._cursor_move_generation
            last_read_generation = self._cursor_read_generations.get(surface_id, move_generation)
            previous_position = self._cursor_last_positions.get(surface_id)
        deadline = time.monotonic() + max(0.1, timeout)
        last_timeout: CaptureTimeout | None = None
        options = self._capture_options(surface)
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            capture = GstCursorMetadataCapture(node, surface_id, **options)
            try:
                _frame, position = capture.grab(timeout=remaining)
            except CaptureTimeout as exc:
                last_timeout = exc
                continue
            finally:
                capture.close()
            if position is None:
                continue

            # A move between two reads invalidates an identical coordinate:
            # it is the signature of a cached pre-move buffer. Keep creating
            # one-shot readers until a new coordinate is observed or the
            # caller's timeout expires.
            if move_generation > last_read_generation and previous_position == position:
                continue

            with self._lock:
                self._cursor_last_positions[surface_id] = position
                self._cursor_read_generations[surface_id] = move_generation
            return position
        if last_timeout is not None:
            raise last_timeout
        return None

    def move(self, surface_id: str, x: float, y: float) -> None:
        surface = self._surface(surface_id)
        self.set_active_surface(surface.id)
        target = self._backend_for_surface(surface)
        if target is not self:
            target.move(surface_id, x, y)
            return
        if self._closed:
            raise BackendUnavailable("Mutter session is closed")
        self._notify_motion(surface, x, y)
        with self._lock:
            self._cursor_move_generation += 1

    @staticmethod
    def _button_code(button: int) -> int:
        return {1: 0x110, 2: 0x112, 3: 0x111}.get(int(button), int(button))

    def button(self, button: int, pressed: bool) -> None:
        target = self._active_backend()
        if target is not self:
            target.button(button, pressed)
            return
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
        target = self._active_backend()
        if target is not self:
            target.scroll(dx, dy, discrete=discrete)
            return
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
        target = self._active_backend()
        if target is not self:
            target.keysym(value, pressed)
            return
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
            local = HeldState(frozenset(self._held_keys), frozenset(self._held_buttons))
        child = getattr(self, "_virtual_backend", None)
        if child is None:
            return local
        child_state = child.held()
        return HeldState(local.keysyms | child_state.keysyms, local.buttons | child_state.buttons)

    def release_all(self) -> None:
        # Never raise: this is called from signal handlers and atexit.
        child = getattr(self, "_virtual_backend", None)
        if child is not None:
            with contextlib.suppress(Exception):
                child.release_all()
        with self._lock:
            keys = list(self._held_keys)
            buttons = list(self._held_buttons)
            active_surface_id = self._active_input_surface_id
            self._active_input_surface_id = self.primary_surface_id
        for value in reversed(keys):
            with contextlib.suppress(Exception):
                self.keysym(value, False)
        for button in reversed(buttons):
            with contextlib.suppress(Exception):
                self.button(button, False)
        with self._lock:
            self._held_keys.clear()
            self._held_buttons.clear()
            self._active_input_surface_id = active_surface_id

    def type_clipboard(self, text: str) -> None:
        target = self._active_backend()
        if target is not self:
            target.type_clipboard(text)
            return
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
        child = self._virtual_backend
        self._virtual_backend = None
        if child is not None:
            with contextlib.suppress(Exception):
                child.close()
        for subscription in getattr(self, "_subscriptions", []):
            with contextlib.suppress(Exception):
                self._bus.signal_unsubscribe(subscription)
        self._subscriptions = []
        # Stop the ScreenCast session first. A bound ScreenCast object can disappear as soon
        # as the RemoteDesktop session stops, which otherwise produces noisy UnknownMethod
        # errors during normal teardown.
        if hasattr(self, "_sc_path"):
            with contextlib.suppress(Exception):
                _call(self._bus, SC, self._sc_path, SC_SESSION, "Stop")
        if hasattr(self, "_rd_path"):
            with contextlib.suppress(Exception):
                _call(self._bus, RD, self._rd_path, RD_SESSION, "Stop")
