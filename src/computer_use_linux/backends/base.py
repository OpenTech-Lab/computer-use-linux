from __future__ import annotations

from collections.abc import Protocol

from ..types import Capability, Frame, HeldState, Surface, SurfaceSpec


class CaptureBackend(Protocol):
    name: str
    capabilities: Capability

    def list_surfaces(self) -> list[Surface]: ...

    def open_surface(self, spec: SurfaceSpec) -> Surface: ...

    def close_surface(self, surface_id: str) -> None: ...

    def grab(self, surface_id: str, *, timeout: float = 2.0) -> Frame: ...


class InputBackend(Protocol):
    name: str
    capabilities: Capability

    def move(self, surface_id: str, x: float, y: float) -> None: ...

    def button(self, button: int, pressed: bool) -> None: ...

    def scroll(self, dx: float, dy: float, *, discrete: bool = True) -> None: ...

    def keysym(self, keysym: int, pressed: bool) -> None: ...

    def held(self) -> HeldState: ...

    def release_all(self) -> None: ...


class Backend(CaptureBackend, InputBackend, Protocol):
    pass

