"""X11 capture and input backend.

The X11 path deliberately talks to the server directly.  It does not shell out to xdotool or
import a desktop-specific window manager helper, which keeps it useful under Xvfb and XWayland.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from typing import Any

from ..errors import BackendUnavailable, CaptureTimeout, InputError, SurfaceNotFound
from ..types import Capability, Frame, HeldState, Surface, SurfaceSpec, WindowInfo


def _xlib() -> tuple[Any, Any, Any]:
    try:
        from Xlib import X, display
        from Xlib.ext import xtest

        return X, display, xtest
    except Exception as exc:  # pragma: no cover - depends on the host Python environment
        raise BackendUnavailable("python-xlib is unavailable; install the python-xlib package") from exc


def probe_x11(display_name: str | None = None) -> dict[str, Any]:
    """Return X11/XTEST capability diagnostics without retaining a server connection."""

    result: dict[str, Any] = {"display": display_name or os.environ.get("DISPLAY", "")}
    if not result["display"]:
        result["error"] = "DISPLAY is unset"
        result["xtest_available"] = False
        result["xgetimage_available"] = False
        result["available"] = False
        return result
    connection = None
    try:
        X, display, _ = _xlib()
        connection = display.Display(display_name)
        result["xtest_available"] = connection.query_extension("XTEST") is not None
        if not result["xtest_available"]:
            result["error"] = "the XTEST extension is unavailable"
        else:
            screen = connection.screen()
            result["size"] = f"{screen.width_in_pixels}x{screen.height_in_pixels}"
            try:
                root = screen.root
                depth = int(screen.root_depth)
                root.get_image(0, 0, 1, 1, X.ZPixmap, (1 << depth) - 1)
                result["xgetimage_available"] = True
            except Exception as exc:
                result["xgetimage_available"] = False
                result["error"] = f"XGetImage is unavailable: {exc}"
        result["available"] = bool(result["xtest_available"] and result.get("xgetimage_available"))
    except Exception as exc:
        result["xtest_available"] = False
        result["xgetimage_available"] = False
        result["available"] = False
        result["error"] = str(exc)
    finally:
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()
    return result


def _attribute(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _connection_info(connection: Any) -> Any:
    # python-xlib's public Display wrapper keeps the connection setup under ``display.info``;
    # the small fake connections used by unit tests expose it directly as ``info``.
    return _attribute(connection, "info", None) or _attribute(_attribute(connection, "display", None), "info", None)


def _root_visual_info(connection: Any, screen: Any) -> Any:
    for depth in _attribute(screen, "allowed_depths", ()):
        if int(_attribute(depth, "depth", -1)) != int(screen.root_depth):
            continue
        for visual in _attribute(depth, "visuals", ()):
            if int(_attribute(visual, "visual_id", 0)) == int(screen.root_visual):
                return visual
    raise BackendUnavailable("the X11 root visual could not be inspected")


def _root_pixmap_format(connection: Any, depth: int) -> Any:
    for pixmap_format in _attribute(_connection_info(connection), "pixmap_formats", ()):
        if int(_attribute(pixmap_format, "depth", -1)) == depth:
            return pixmap_format
    raise BackendUnavailable(f"the X11 server has no pixmap format for root depth {depth}")


def _decode_ximage(connection: Any, image: Any, width: int, height: int, visual: Any, bits_per_pixel: int) -> Any:
    """Decode a TrueColor XImage into an RGB numpy array using the server masks."""

    import numpy as np

    raw = np.frombuffer(bytes(image.data), dtype=np.uint8)
    if height <= 0 or width <= 0 or raw.size < height:
        raise CaptureTimeout("XGetImage returned an empty image")
    row_stride = raw.size // height
    pixel_bytes = (int(bits_per_pixel) + 7) // 8
    required = width * pixel_bytes
    if pixel_bytes <= 0 or row_stride < required:
        raise CaptureTimeout(f"unsupported XImage stride: {row_stride} bytes for {width} pixels")
    pixels = raw[: row_stride * height].reshape((height, row_stride))[:, :required]
    pixels = pixels.reshape((height, width, pixel_bytes))
    image_byte_order = int(_attribute(_connection_info(connection), "image_byte_order", 0))
    if image_byte_order == 1:  # X.MSBFirst; constants are stable protocol values.
        pixels = pixels[..., ::-1]
    values = np.zeros((height, width), dtype=np.uint64)
    for index in range(pixel_bytes):
        values |= pixels[..., index].astype(np.uint64) << (8 * index)

    def channel(mask: int) -> Any:
        mask = int(mask)
        if not mask:
            return np.zeros((height, width), dtype=np.uint8)
        shift = (mask & -mask).bit_length() - 1
        maximum = mask >> shift
        value = ((values & mask) >> shift) * 255 // maximum
        return value.astype(np.uint8)

    red = channel(_attribute(visual, "red_mask", 0))
    green = channel(_attribute(visual, "green_mask", 0))
    blue = channel(_attribute(visual, "blue_mask", 0))
    return np.stack((red, green, blue), axis=2)


class X11WindowSource:
    """Small EWMH-independent X11 window source with real root-relative positions."""

    def __init__(self, connection: Any, surface_id: str):
        self._display = connection
        self._X = _xlib()[0]
        self._root = connection.screen().root
        self._surface_id = surface_id
        self._refs: dict[str, Any] = {}
        self._lock = threading.RLock()

    def _property_text(self, window: Any, name: str) -> str:
        try:
            atom = self._display.intern_atom(name)
            property_value = window.get_full_property(atom, self._X.AnyPropertyType)
            if property_value is None:
                return ""
            value = property_value.value
            if isinstance(value, bytes):
                return value.rstrip(b"\0").decode("utf-8", "replace")
            if isinstance(value, (list, tuple)):
                return bytes(value).rstrip(b"\0").decode("utf-8", "replace")
            return str(value)
        except Exception:
            return ""

    def _info(self, window: Any) -> WindowInfo | None:
        try:
            geometry = window.get_geometry()
            translated = window.translate_coords(self._root, 0, 0)
            position = (int(translated.x), int(translated.y))
            title = self._property_text(window, "_NET_WM_NAME") or self._property_text(window, "WM_NAME")
            wm_class = window.get_wm_class() or ()
            app = str(wm_class[1] if len(wm_class) > 1 else wm_class[0] if wm_class else "X11")
            window_id = f"window:{int(window.id):x}"
            focus = self._root.get_input_focus().focus
            active = int(getattr(focus, "id", 0)) == int(window.id)
            return WindowInfo(
                id=window_id,
                title=title,
                app=app,
                role="window",
                width=int(geometry.width),
                height=int(geometry.height),
                position=position,
                active=active,
                surface_id=self._surface_id,
            )
        except Exception:
            return None

    def list_windows(self) -> list[WindowInfo]:
        result: list[WindowInfo] = []
        refs: dict[str, Any] = {}
        try:
            children = self._root.query_tree().children
        except Exception as exc:
            raise BackendUnavailable(f"X11 window enumeration failed: {exc}") from exc
        for window in children:
            info = self._info(window)
            if info is not None:
                result.append(info)
                refs[info.id] = window
        with self._lock:
            self._refs = refs
        return result

    def _ref(self, window_id: str) -> Any:
        self.list_windows()
        with self._lock:
            window = self._refs.get(window_id)
        if window is None:
            raise SurfaceNotFound(f"unknown window: {window_id}")
        return window

    def focus(self, window_id: str) -> bool:
        window = self._ref(window_id)
        try:
            window.set_input_focus(self._X.RevertToParent, self._X.CurrentTime)
            window.configure(stack_mode=self._X.Above)
            self._display.sync()
            return self.is_active(window_id)
        except Exception as exc:
            raise BackendUnavailable(f"X11 could not focus {window_id}: {exc}") from exc

    def is_active(self, window_id: str) -> bool:
        self._ref(window_id)
        focus = self._root.get_input_focus().focus
        return int(getattr(focus, "id", 0)) == int(window_id.removeprefix("window:"), 16)

    def application_search_name(self, window_id: str) -> str:
        info = next((item for item in self.list_windows() if item.id == window_id), None)
        if info is None:
            raise SurfaceNotFound(f"unknown window: {window_id}")
        return info.title or info.app

    def activate(self, window_id: str) -> bool:
        return self.focus(window_id)

    def focused_text(self) -> tuple[WindowInfo, str] | None:
        for info in self.list_windows():
            if info.active:
                return info, ""
        return None

    def sensitive_focused(self) -> WindowInfo | None:
        from ..safety import is_sensitive_window

        return next((info for info in self.list_windows() if info.active and is_sensitive_window(info)), None)

    def window_text(self, window_id: str) -> str:
        self._ref(window_id)
        return ""

    def actions(self, window_id: str, max_depth: int = 6) -> list[dict[str, Any]]:
        del window_id, max_depth
        return []

    def invoke_action(self, window_id: str, action_name: str | int, **kwargs: Any) -> dict[str, Any]:
        del window_id, action_name, kwargs
        raise BackendUnavailable("X11 windows do not expose AT-SPI actions through this backend")

    def tree(self, window_id: str, max_depth: int = 6) -> dict[str, Any]:
        root_window = self._ref(window_id)

        def build(window: Any, depth: int) -> dict[str, Any]:
            info = self._info(window)
            result: dict[str, Any] = {
                "name": info.title if info else "",
                "role": "window",
                "interfaces": [],
                "text": "",
                "actions": [],
            }
            if depth < max_depth:
                with contextlib.suppress(Exception):
                    result["children"] = [build(child, depth + 1) for child in window.query_tree().children]
            return result

        return build(root_window, 0)


class X11Backend:
    name = "x11"
    capabilities = (
        Capability.CAPTURE
        | Capability.INPUT_POINTER
        | Capability.INPUT_KEYBOARD
        | Capability.WINDOW_LIST
        | Capability.WINDOW_FOCUS
        | Capability.WINDOW_GEOMETRY
    )

    def __init__(self, *, display_name: str | None = None, surface_id: str = "monitor:x11-0"):
        X, display, xtest = _xlib()
        self._X = X
        self._xtest = xtest
        self._display = None
        self._lock = threading.RLock()
        self._held_keys: set[int] = set()
        self._held_buttons: set[int] = set()
        self._key_plans: dict[int, tuple[int, tuple[int, ...]]] = {}
        try:
            self._display = display.Display(display_name)
            if self._display.query_extension("XTEST") is None:
                raise BackendUnavailable("the XTEST extension is unavailable on this X11 server")
            screen = self._display.screen()
            geometry = screen.root.get_geometry()
            width, height = int(geometry.width), int(geometry.height)
            self._root = screen.root
            self._plane_mask = (1 << int(screen.root_depth)) - 1
            self._visual = _root_visual_info(self._display, screen)
            self._pixmap_format = _root_pixmap_format(self._display, int(screen.root_depth))
            try:
                self._root.get_image(0, 0, 1, 1, self._X.ZPixmap, self._plane_mask)
            except Exception as exc:
                raise BackendUnavailable(f"XGetImage is unavailable on this X11 display: {exc}") from exc
            self._surface = Surface(
                id=surface_id,
                kind="monitor",
                width=width,
                height=height,
                origin=(0, 0),
                scale=1.0,
                label=f"X11 root ({width}x{height})",
            )
            self._surfaces = [self._surface]
            self.primary_surface_id = surface_id
            self.isolated_surface_id = None
            self.window_source = X11WindowSource(self._display, surface_id)
        except Exception:
            if self._display is not None:
                with contextlib.suppress(Exception):
                    self._display.close()
            raise

    def list_surfaces(self) -> list[Surface]:
        return list(self._surfaces)

    def open_surface(self, spec: SurfaceSpec) -> Surface:
        if spec.id and spec.id != self._surface.id:
            raise SurfaceNotFound(f"unknown surface: {spec.id}")
        if spec.kind != "monitor":
            raise InputError("X11 supports one monitor root surface")
        return self._surface

    def close_surface(self, surface_id: str) -> None:
        self._surface_for_id(surface_id)

    def _surface_for_id(self, surface_id: str) -> Surface:
        if surface_id != self._surface.id:
            raise SurfaceNotFound(f"unknown surface: {surface_id}")
        return self._surface

    def grab(self, surface_id: str, *, timeout: float = 2.0) -> Frame:
        del timeout
        self._surface_for_id(surface_id)
        try:
            geometry = self._root.get_geometry()
            width, height = int(geometry.width), int(geometry.height)
            image = self._root.get_image(0, 0, width, height, self._X.ZPixmap, self._plane_mask)
            data = _decode_ximage(
                self._display,
                image,
                width,
                height,
                self._visual,
                int(_attribute(self._pixmap_format, "bits_per_pixel", 0)),
            )
            return Frame(surface_id, width, height, data, time.time())
        except CaptureTimeout:
            raise
        except Exception as exc:
            raise CaptureTimeout(f"XGetImage failed for {surface_id}: {exc}") from exc

    def move(self, surface_id: str, x: float, y: float) -> None:
        self._surface_for_id(surface_id)
        self._xtest.fake_input(self._display, self._X.MotionNotify, root=self._root.id, x=int(x), y=int(y))
        self._display.sync()

    def cursor_position(self, surface_id: str, *, timeout: float = 2.0) -> tuple[int, int]:
        del timeout
        self._surface_for_id(surface_id)
        try:
            pointer = self._root.query_pointer()
            return int(pointer.root_x), int(pointer.root_y)
        except Exception as exc:
            raise CaptureTimeout(f"X11 pointer query failed for {surface_id}: {exc}") from exc

    def button(self, button: int, pressed: bool) -> None:
        logical_button = int(button)
        if logical_button not in {1, 2, 3}:
            raise InputError("X11 supports pointer buttons 1, 2, and 3")
        event = self._X.ButtonPress if pressed else self._X.ButtonRelease
        self._xtest.fake_input(self._display, event, detail=logical_button)
        self._display.sync()
        with self._lock:
            if pressed:
                self._held_buttons.add(logical_button)
            else:
                self._held_buttons.discard(logical_button)

    def _wheel(self, button: int, amount: float) -> None:
        count = abs(int(round(amount)))
        if count == 0 and amount:
            count = 1
        for _ in range(count):
            self._xtest.fake_input(self._display, self._X.ButtonPress, detail=button)
            self._xtest.fake_input(self._display, self._X.ButtonRelease, detail=button)

    def scroll(self, dx: float, dy: float, *, discrete: bool = True) -> None:
        del discrete
        if dy > 0:
            self._wheel(4, dy)
        elif dy < 0:
            self._wheel(5, dy)
        if dx > 0:
            self._wheel(6, dx)
        elif dx < 0:
            self._wheel(7, dx)
        self._display.sync()

    def _key_plan(self, value: int) -> tuple[int, tuple[int, ...]]:
        """Resolve a keysym to a keycode and any X11 level modifiers it needs."""

        value = int(value)
        keycode = int(self._display.keysym_to_keycode(value))
        if keycode <= 0:
            raise InputError(f"X11 has no keycode for keysym {value}")
        # XTest accepts keycodes, while the public backend protocol accepts keysyms.  The
        # server's first mapping for ``A``/``!`` is commonly the unshifted key, so inspect the
        # keymap column and synthesize Shift for level-one symbols.  This keeps ``type_text``
        # and key chords meaningful on X11 instead of silently producing lowercase/base keys.
        column = 0
        keycode_to_keysym = getattr(self._display, "keycode_to_keysym", None)
        if callable(keycode_to_keysym):
            matches = []
            for candidate_column in range(8):
                with contextlib.suppress(Exception):
                    if int(keycode_to_keysym(keycode, candidate_column) or 0) == value:
                        matches.append(candidate_column)
            if matches:
                column = matches[0]
        group, level = divmod(column, 2)
        if group:
            raise InputError(f"X11 keysym {value} requires an unsupported keyboard group")
        if level == 0 or value in {0xFFE1, 0xFFE2, 0xFFE3, 0xFFE4, 0xFFE9, 0xFFEA, 0xFFEB, 0xFFEC}:
            return keycode, ()
        if any(keysym in self._held_keys for keysym in (0xFFE1, 0xFFE2)):
            return keycode, ()
        shift_keycode = int(self._display.keysym_to_keycode(0xFFE1))
        if shift_keycode <= 0:
            raise InputError("X11 has no keycode for Shift")
        return keycode, (shift_keycode,)

    def _fake_key(self, keycode: int, pressed: bool) -> None:
        event = self._X.KeyPress if pressed else self._X.KeyRelease
        self._xtest.fake_input(self._display, event, detail=int(keycode))

    def keysym(self, value: int, pressed: bool) -> None:
        value = int(value)
        if pressed:
            keycode, modifiers = self._key_plan(value)
            for modifier in modifiers:
                self._fake_key(modifier, True)
            self._fake_key(keycode, True)
            self._key_plans[value] = (keycode, modifiers)
        else:
            plan = self._key_plans.pop(value, None)
            if plan is None:
                plan = self._key_plan(value)
            keycode, modifiers = plan
            self._fake_key(keycode, False)
            for modifier in reversed(modifiers):
                self._fake_key(modifier, False)
        self._display.sync()
        with self._lock:
            if pressed:
                self._held_keys.add(value)
            else:
                self._held_keys.discard(value)

    def held(self) -> HeldState:
        with self._lock:
            return HeldState(frozenset(self._held_keys), frozenset(self._held_buttons))

    def release_all(self) -> None:
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

    def close(self) -> None:
        self.release_all()
        if self._display is not None:
            with contextlib.suppress(Exception):
                self._display.close()
            self._display = None


__all__ = ["X11Backend", "X11WindowSource", "probe_x11"]
