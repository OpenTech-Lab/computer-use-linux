"""Isolated Xvfb backend for CI and for agents that must not touch the host desktop."""

from __future__ import annotations

import contextlib
import os
import selectors
import shutil
import subprocess
import time
from typing import Any

from ..errors import BackendUnavailable
from ..types import Capability, Frame, HeldState, Surface, SurfaceSpec
from .x11 import X11Backend


def probe_headless() -> dict[str, Any]:
    """Report whether the Xvfb implementation can be started on this host.

    Cage is reported separately because it is an optional nested Wayland compositor; Xvfb alone
    already gives this backend a separate X server, pointer, keyboard, and window hierarchy.
    """

    xvfb = shutil.which("Xvfb")
    cage = shutil.which("cage")
    try:
        import Xlib  # noqa: F401

        xlib_available = True
    except Exception:
        xlib_available = False
    result: dict[str, Any] = {
        "xvfb": xvfb,
        "cage": cage,
        "xlib": xlib_available,
        "available": bool(xvfb and xlib_available),
    }
    if not xvfb:
        result["error"] = "Xvfb is missing; install xvfb"
    elif not xlib_available:
        result["error"] = "python-xlib is missing; install the Python dependency"
    else:
        result["detail"] = "Xvfb isolated display available" + ("; cage also installed" if cage else "")
    return result


class HeadlessBackend:
    """Run X11Backend against a private Xvfb display and restore the caller's environment."""

    name = "headless"
    capabilities = (
        Capability.CAPTURE
        | Capability.INPUT_POINTER
        | Capability.INPUT_KEYBOARD
        | Capability.WINDOW_LIST
        | Capability.WINDOW_FOCUS
        | Capability.WINDOW_GEOMETRY
        | Capability.BOUND_CAPTURE_INPUT
    )

    def __init__(
        self,
        *,
        width: int = 1280,
        height: int = 720,
        startup_timeout: float = 5.0,
    ):
        if width <= 0 or height <= 0:
            raise ValueError("headless width and height must be positive")
        probe = probe_headless()
        if not probe["available"]:
            raise BackendUnavailable(str(probe.get("error", "headless Xvfb is unavailable")))
        self._process: subprocess.Popen[str] | None = None
        self._x11: X11Backend | None = None
        self._display_name: str | None = None
        self._previous_environment: dict[str, str | None] = {}
        try:
            self._display_name = self._start_xvfb(str(probe["xvfb"]), width, height, startup_timeout)
            self._set_child_environment(self._display_name)
            deadline = time.monotonic() + startup_timeout
            last_error: Exception | None = None
            while time.monotonic() < deadline:
                try:
                    self._x11 = X11Backend(display_name=self._display_name, surface_id="monitor:headless-0")
                    break
                except Exception as exc:
                    last_error = exc
                    if self._process is not None and self._process.poll() is not None:
                        break
                    time.sleep(0.05)
            if self._x11 is None:
                raise BackendUnavailable(f"Xvfb display {self._display_name} did not become usable: {last_error}")
            self.primary_surface_id = self._x11.primary_surface_id
            self.isolated_surface_id = self.primary_surface_id
            self.window_source = self._x11.window_source
        except Exception:
            self.close()
            raise

    def _start_xvfb(self, binary: str, width: int, height: int, timeout: float) -> str:
        process = subprocess.Popen(
            [
                binary,
                "-displayfd",
                "1",
                "-screen",
                "0",
                f"{width}x{height}x24",
                "-nolisten",
                "tcp",
                "-ac",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self._process = process
        if process.stdout is None:
            raise BackendUnavailable("Xvfb did not expose its displayfd")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                ready = selector.select(max(0.01, deadline - time.monotonic()))
                if not ready:
                    continue
                line = process.stdout.readline().strip()
                if line:
                    try:
                        return f":{int(line)}"
                    except ValueError as exc:
                        raise BackendUnavailable(f"Xvfb returned an invalid display number: {line!r}") from exc
                if process.poll() is not None:
                    detail = process.stderr.read().strip() if process.stderr is not None else ""
                    raise BackendUnavailable(f"Xvfb exited before publishing a display number: {detail}")
            detail = ""
            if process.poll() is not None and process.stderr is not None:
                detail = process.stderr.read().strip()
            raise BackendUnavailable(f"Xvfb did not publish a display number within {timeout:.1f}s: {detail}")
        finally:
            selector.close()

    def _set_child_environment(self, display_name: str) -> None:
        for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "GDK_BACKEND", "QT_QPA_PLATFORM", "SDL_VIDEODRIVER"):
            self._previous_environment[key] = os.environ.get(key)
        os.environ["DISPLAY"] = display_name
        os.environ.pop("WAYLAND_DISPLAY", None)
        os.environ["XDG_SESSION_TYPE"] = "x11"
        os.environ["GDK_BACKEND"] = "x11"
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        os.environ["SDL_VIDEODRIVER"] = "x11"

    def list_surfaces(self) -> list[Surface]:
        assert self._x11 is not None
        return self._x11.list_surfaces()

    def open_surface(self, spec: SurfaceSpec) -> Surface:
        assert self._x11 is not None
        return self._x11.open_surface(spec)

    def close_surface(self, surface_id: str) -> None:
        assert self._x11 is not None
        self._x11.close_surface(surface_id)

    def grab(self, surface_id: str, *, timeout: float = 2.0) -> Frame:
        assert self._x11 is not None
        return self._x11.grab(surface_id, timeout=timeout)

    def cursor_position(self, surface_id: str, *, timeout: float = 2.0) -> tuple[int, int]:
        assert self._x11 is not None
        return self._x11.cursor_position(surface_id, timeout=timeout)

    def move(self, surface_id: str, x: float, y: float) -> None:
        assert self._x11 is not None
        self._x11.move(surface_id, x, y)

    def button(self, button: int, pressed: bool) -> None:
        assert self._x11 is not None
        self._x11.button(button, pressed)

    def scroll(self, dx: float, dy: float, *, discrete: bool = True) -> None:
        assert self._x11 is not None
        self._x11.scroll(dx, dy, discrete=discrete)

    def keysym(self, value: int, pressed: bool) -> None:
        assert self._x11 is not None
        self._x11.keysym(value, pressed)

    def held(self) -> HeldState:
        assert self._x11 is not None
        return self._x11.held()

    def release_all(self) -> None:
        if self._x11 is not None:
            self._x11.release_all()

    def close(self) -> None:
        if self._x11 is not None:
            with contextlib.suppress(Exception):
                self._x11.close()
            self._x11 = None
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            with contextlib.suppress(Exception):
                process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                process.wait(timeout=1.0)
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    process.wait(timeout=1.0)
        for key, previous in self._previous_environment.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        self._previous_environment = {}


__all__ = ["HeadlessBackend", "probe_headless"]
