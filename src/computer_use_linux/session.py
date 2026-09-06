from __future__ import annotations

import atexit
import contextlib
import io
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any

from .backends import detect
from .config import Config, load_config
from .coords import image_dimensions, surface_coordinates
from .errors import BackendUnavailable, SafetyRefusal, SurfaceNotFound
from .keys import parse_chord, text_to_keysyms
from .logging import ActionLogger
from .safety import redact_frame, require_confirmation
from .types import Frame, Surface, WindowInfo


@dataclass(frozen=True)
class ScreenshotResult:
    png: bytes
    metadata: dict[str, Any]


class Session:
    """Own one backend session and all safety state around it."""

    def __init__(
        self,
        *,
        backend: Any | None = None,
        config: Config | None = None,
        window_source: Any | None = None,
    ):
        self.config = config or load_config()
        self.backend = backend or detect(config=self.config)
        self.surfaces: list[Surface] = list(self.backend.list_surfaces())
        if not self.surfaces:
            raise BackendUnavailable("the selected backend reported no surfaces")
        self._surface_by_id = {surface.id: surface for surface in self.surfaces}
        self.active_surface_id = getattr(self.backend, "primary_surface_id", self.surfaces[0].id)
        if self.active_surface_id not in self._surface_by_id:
            self.active_surface_id = self.surfaces[0].id
        self._last_image_scale: dict[str, float] = {surface.id: 1.0 for surface in self.surfaces}
        self._last_image_origin: dict[str, tuple[int, int]] = {surface.id: (0, 0) for surface in self.surfaces}
        self._window_source = window_source
        self._action_logger = ActionLogger(self.config.actions_log)
        self._lock = threading.RLock()
        self._held_since: dict[tuple[str, int], float] = {}
        self._panic_active = False
        self._closed = False
        self._stop_watchdog = threading.Event()
        self._old_signal_handlers: dict[int, Any] = {}
        self._install_guards()
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="cul-safety-watchdog", daemon=True)
        self._watchdog.start()

    def _install_guards(self) -> None:
        atexit.register(self.close)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                self._old_signal_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._signal_handler)
            except (ValueError, OSError):
                # A Session can be constructed in a worker thread; the watchdog remains active.
                continue

    def _signal_handler(self, signum: int, _frame: Any) -> None:
        self.release_all()
        self.close()
        raise SystemExit(128 + signum)

    def _watchdog_loop(self) -> None:
        while not self._stop_watchdog.wait(0.25):
            try:
                panic_present = self.config.panic_file.exists()
            except OSError:
                panic_present = True
            if panic_present:
                self._panic_active = True
                self.release_all()
            else:
                self._panic_active = False
            now = time.monotonic()
            with self._lock:
                expired = [
                    key for key, started in self._held_since.items() if now - started > self.config.max_hold_seconds
                ]
            if expired:
                self.release_all()

    def _check_input_allowed(self) -> None:
        if self._closed:
            raise BackendUnavailable("session is closed")
        if self._panic_active or self.config.panic_file.exists():
            self.release_all()
            self._panic_active = True
            raise SafetyRefusal(f"input refused while panic trip-wire exists: {self.config.panic_file}")

    def _surface(self, surface_id: str | None) -> Surface:
        chosen = surface_id or self.active_surface_id
        try:
            return self._surface_by_id[chosen]
        except KeyError as exc:
            raise SurfaceNotFound(f"unknown surface: {chosen}") from exc

    def select_surface(self, surface_id: str) -> Surface:
        surface = self._surface(surface_id)
        self.active_surface_id = surface.id
        return surface

    def _point(self, surface: Surface, x: float, y: float, coord_space: str | None) -> tuple[int, int]:
        selected_space = coord_space or "image"
        return surface_coordinates(
            x,
            y,
            surface=surface,
            coord_space=selected_space,  # type: ignore[arg-type]
            image_scale=self._last_image_scale[surface.id],
            image_origin=self._last_image_origin[surface.id],
        )

    def _track_key(self, value: int, pressed: bool) -> None:
        with self._lock:
            marker = ("key", int(value))
            if pressed:
                self._held_since.setdefault(marker, time.monotonic())
            else:
                self._held_since.pop(marker, None)

    def _track_button(self, value: int, pressed: bool) -> None:
        with self._lock:
            marker = ("button", int(value))
            if pressed:
                self._held_since.setdefault(marker, time.monotonic())
            else:
                self._held_since.pop(marker, None)

    def _log(self, tool: str, args: dict[str, Any], surface: str | None = None) -> None:
        with contextlib.suppress(Exception):
            self._action_logger.log(tool, args, surface=surface, held=self.backend.held())

    def move(self, x: float, y: float, *, surface_id: str | None = None, coord_space: str | None = None) -> dict[str, Any]:
        self._check_input_allowed()
        surface = self._surface(surface_id)
        native_x, native_y = self._point(surface, x, y, coord_space)
        result: dict[str, Any] | None = None
        try:
            self.backend.move(surface.id, native_x, native_y)
            result = {"ok": True, "surface_id": surface.id, "x": native_x, "y": native_y}
            self._log("move", {"x": x, "y": y, "coord_space": coord_space}, surface.id)
            return result
        except Exception:
            self.release_all()
            raise

    def click(
        self,
        x: float,
        y: float,
        *,
        surface_id: str | None = None,
        coord_space: str | None = None,
        button: int = 1,
        count: int = 1,
        modifiers: list[str] | None = None,
    ) -> dict[str, Any]:
        self._check_input_allowed()
        if count < 1:
            raise ValueError("count must be at least 1")
        surface = self._surface(surface_id)
        modifier_values = tuple(value for modifier in (modifiers or []) for value in parse_chord(modifier))
        native_x, native_y = self._point(surface, x, y, coord_space)
        try:
            for value in modifier_values:
                self.backend.keysym(value, True)
                self._track_key(value, True)
            self.backend.move(surface.id, native_x, native_y)
            for _ in range(count):
                self.backend.button(button, True)
                self._track_button(button, True)
                self.backend.button(button, False)
                self._track_button(button, False)
            result = {"ok": True, "surface_id": surface.id, "x": native_x, "y": native_y, "button": button, "count": count}
        except Exception:
            self.release_all()
            raise
        finally:
            for value in reversed(modifier_values):
                with contextlib.suppress(Exception):
                    self.backend.keysym(value, False)
                self._track_key(value, False)
        assert result is not None
        self._log("click", {"x": x, "y": y, "button": button, "count": count, "modifiers": modifiers or []}, surface.id)
        return result

    def drag(
        self,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        *,
        surface_id: str | None = None,
        button: int = 1,
        steps: int = 20,
        coord_space: str | None = None,
    ) -> dict[str, Any]:
        self._check_input_allowed()
        if steps < 1:
            raise ValueError("steps must be at least 1")
        surface = self._surface(surface_id)
        start = self._point(surface, from_x, from_y, coord_space)
        end = self._point(surface, to_x, to_y, coord_space)
        try:
            self.backend.move(surface.id, *start)
            self.backend.button(button, True)
            self._track_button(button, True)
            for index in range(1, steps + 1):
                fraction = index / steps
                self.backend.move(
                    surface.id,
                    start[0] + (end[0] - start[0]) * fraction,
                    start[1] + (end[1] - start[1]) * fraction,
                )
            self.backend.button(button, False)
            self._track_button(button, False)
            result = {"ok": True, "surface_id": surface.id, "from": list(start), "to": list(end)}
            self._log("drag", {"from_x": from_x, "from_y": from_y, "to_x": to_x, "to_y": to_y}, surface.id)
            return result
        except Exception:
            self.release_all()
            raise

    def scroll(
        self,
        x: float,
        y: float,
        *,
        dx: float = 0,
        dy: float = 0,
        surface_id: str | None = None,
        coord_space: str | None = None,
    ) -> dict[str, Any]:
        self._check_input_allowed()
        surface = self._surface(surface_id)
        native = self._point(surface, x, y, coord_space)
        try:
            self.backend.move(surface.id, *native)
            self.backend.scroll(dx, dy, discrete=True)
            result = {"ok": True, "surface_id": surface.id, "x": native[0], "y": native[1], "dx": dx, "dy": dy}
            self._log("scroll", {"x": x, "y": y, "dx": dx, "dy": dy}, surface.id)
            return result
        except Exception:
            self.release_all()
            raise

    def key(self, keys: str | list[str], *, confirm: bool = False) -> dict[str, Any]:
        self._check_input_allowed()
        chords = [keys] if isinstance(keys, str) else list(keys)
        try:
            for chord in chords:
                require_confirmation(
                    action="key",
                    confirm=confirm,
                    confirm_mode=self.config.confirm_mode,
                    chord=chord,
                )
                values = parse_chord(chord)
                for value in values:
                    self.backend.keysym(value, True)
                    self._track_key(value, True)
                for value in reversed(values):
                    self.backend.keysym(value, False)
                    self._track_key(value, False)
            result = {"ok": True, "keys": chords}
            self._log("key", {"keys": chords})
            return result
        except Exception:
            self.release_all()
            raise

    def _focused_window(self) -> WindowInfo | None:
        source = self._get_window_source(required=False)
        if source is None:
            return None
        try:
            focused = source.focused_text()
            return focused[0] if focused else None
        except Exception:
            return None

    def type_text(self, text: str, *, mode: str = "auto", confirm: bool = False) -> dict[str, Any]:
        self._check_input_allowed()
        if mode not in {"auto", "keysym", "clipboard"}:
            raise ValueError("mode must be auto, keysym, or clipboard")
        focused = self._focused_window()
        if focused is not None:
            for pattern in self.config.redact_window_titles:
                import re

                if re.search(pattern, focused.title):
                    require_confirmation(
                        action="type_text",
                        confirm=confirm,
                        confirm_mode=self.config.confirm_mode,
                        focused_window=focused,
                    )
                    break
        selected = mode
        if selected == "auto":
            selected = "keysym" if len(text) <= 40 and text.isascii() else "clipboard"
        try:
            warning = None
            if selected == "keysym":
                values = text_to_keysyms(text)
                for value in values:
                    self.backend.keysym(value, True)
                    self._track_key(value, True)
                    self.backend.keysym(value, False)
                    self._track_key(value, False)
            else:
                type_clipboard = getattr(self.backend, "type_clipboard", None)
                if type_clipboard is None:
                    raise BackendUnavailable("selected backend has no clipboard typing support")
                type_clipboard(text)
                if getattr(self.backend, "name", "") != "gnome_mutter":
                    warning = "clipboard fallback used; keycode typing is unreliable on the Japanese layout"
            result: dict[str, Any] = {"ok": True, "mode": selected, "length": len(text)}
            if warning:
                result["warning"] = warning
            self._log("type_text", {"text": text, "mode": selected})
            return result
        except Exception:
            self.release_all()
            raise

    def hold_key(self, key: str) -> None:
        self._check_input_allowed()
        values = parse_chord(key)
        if len(values) != 1:
            raise ValueError("hold-key accepts one key")
        self.backend.keysym(values[0], True)
        self._track_key(values[0], True)
        self._log("hold_key", {"key": key})

    def release_all(self) -> None:
        with contextlib.suppress(Exception):
            self.backend.release_all()
        with self._lock:
            self._held_since.clear()

    def panic(self) -> dict[str, Any]:
        self.release_all()
        self._panic_active = True
        self.close()
        return {"ok": True, "panic": True}

    def _get_window_source(self, *, required: bool) -> Any | None:
        if self._window_source is None:
            try:
                from .windows.atspi import AtspiWindowSource

                self._window_source = AtspiWindowSource()
            except Exception:
                if required:
                    raise SafetyRefusal("AT-SPI is unavailable; refusing a desktop operation that needs window state")
                return None
        return self._window_source

    def list_windows(self) -> list[WindowInfo]:
        source = self._get_window_source(required=True)
        return source.list_windows()

    def focus_window(self, window_id: str) -> bool:
        source = self._get_window_source(required=True)
        try:
            focused = bool(source.focus(window_id))
            activated = False
            if not focused or not source.is_active(window_id):
                activate = getattr(source, "activate", None)
                if activate is not None:
                    with contextlib.suppress(Exception):
                        activated = bool(activate(window_id))
                        focused = focused or activated
            if (not focused or not source.is_active(window_id)) and not activated:
                # Wayland prevents a foreign accessibility client from raising a window with
                # Component.grab_focus(). Use the compositor's normal, user-visible overview
                # search instead; it does not depend on invented window coordinates.
                query = source.application_search_name(window_id)
                super_key = 0xFFEB
                return_key = 0xFF0D
                self.backend.keysym(super_key, True)
                self.backend.keysym(super_key, False)
                time.sleep(0.35)
                for value in text_to_keysyms(query):
                    self.backend.keysym(value, True)
                    self.backend.keysym(value, False)
                self.backend.keysym(return_key, True)
                self.backend.keysym(return_key, False)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if source.is_active(window_id):
                        focused = True
                        break
                    time.sleep(0.05)
            self._last_focused_window_id = window_id
            return focused
        except Exception:
            self.release_all()
            raise

    def window_tree(self, window_id: str, max_depth: int = 6) -> dict[str, Any]:
        source = self._get_window_source(required=True)
        return source.tree(window_id, max_depth)

    def window_text(self, window_id: str) -> str:
        source = self._get_window_source(required=True)
        return source.window_text(window_id)

    def _safe_frame(self, surface: Surface) -> Frame:
        source = self._get_window_source(required=True)
        sensitive = source.sensitive_focused()
        if sensitive is not None:
            raise SafetyRefusal(f"refusing screenshot while sensitive window is focused: {sensitive.title or sensitive.app}")
        frame = self.backend.grab(surface.id, timeout=2.0)
        sensitive = source.sensitive_focused()
        if sensitive is not None:
            raise SafetyRefusal(f"refusing screenshot while sensitive window is focused: {sensitive.title or sensitive.app}")
        windows = source.list_windows()
        return redact_frame(
            frame,
            surface,
            redact_regions=self.config.redact_regions,
            redact_window_titles=self.config.redact_window_titles,
            windows=windows,
        )

    def screenshot(
        self,
        *,
        surface_id: str | None = None,
        region: dict[str, int] | None = None,
        max_width: int = 1280,
        cursor: bool = True,
    ) -> ScreenshotResult:
        surface = self._surface(surface_id)
        frame = self._safe_frame(surface)
        region_origin = (0, 0)
        if region is not None:
            x = int(region.get("x", 0))
            y = int(region.get("y", 0))
            width = int(region.get("w", 0))
            height = int(region.get("h", 0))
            if width <= 0 or height <= 0 or x < 0 or y < 0 or x + width > frame.width or y + height > frame.height:
                raise ValueError("region must be a positive rectangle inside the surface")
            frame = Frame(frame.surface_id, width, height, frame.data[y : y + height, x : x + width], frame.captured_at)
            region_origin = (x, y)
        image_width, image_height, scale = image_dimensions(
            Surface(surface.id, surface.kind, frame.width, frame.height, surface.origin, surface.scale, surface.label),
            max_width,
        )
        from PIL import Image

        image = Image.fromarray(frame.data, mode="RGB")
        if (image_width, image_height) != image.size:
            image = image.resize((image_width, image_height), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        self._last_image_scale[surface.id] = scale
        self._last_image_origin[surface.id] = region_origin
        metadata = {
            "surface_id": surface.id,
            "image_width": image_width,
            "image_height": image_height,
            "surface_width": surface.width,
            "surface_height": surface.height,
            "scale": scale,
            "coord_space": "image",
            "captured_at": frame.captured_at,
            "cursor": bool(cursor),
        }
        if region is not None:
            metadata["region"] = {"x": region_origin[0], "y": region_origin[1], "w": frame.width, "h": frame.height}
        return ScreenshotResult(output.getvalue(), metadata)

    def wait_for_change(
        self,
        *,
        surface_id: str | None = None,
        timeout: float = 5.0,
        threshold: float = 0.002,
    ) -> dict[str, Any]:
        import numpy as np
        from PIL import Image

        surface = self._surface(surface_id)
        started = time.monotonic()
        first = self._safe_frame(surface)
        baseline = np.asarray(Image.fromarray(first.data).convert("L").resize((320, 180)), dtype=np.float32) / 255
        changed = False
        while time.monotonic() - started < timeout:
            current = self._safe_frame(surface)
            sample = np.asarray(Image.fromarray(current.data).convert("L").resize((320, 180)), dtype=np.float32) / 255
            if float(np.mean(np.abs(sample - baseline))) >= threshold:
                changed = True
                break
            time.sleep(0.05)
        return {"changed": changed, "elapsed": time.monotonic() - started}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_watchdog.set()
        self.release_all()
        with contextlib.suppress(Exception):
            self.backend.close()
        for signum, handler in self._old_signal_handlers.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signum, handler)
