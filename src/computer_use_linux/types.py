from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Literal

SurfaceKind = Literal["monitor", "area", "virtual", "window"]


@dataclass(frozen=True)
class Surface:
    """One capture stream and therefore one native coordinate space."""

    id: str
    kind: SurfaceKind
    width: int
    height: int
    origin: tuple[int, int]
    scale: float
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "width": self.width,
            "height": self.height,
            "origin": list(self.origin),
            "scale": self.scale,
            "label": self.label,
        }


@dataclass
class Frame:
    surface_id: str
    width: int
    height: int
    data: Any
    captured_at: float


@dataclass(frozen=True)
class WindowInfo:
    id: str
    title: str
    app: str
    role: str
    width: int | None
    height: int | None
    position: tuple[int, int] | None
    active: bool
    surface_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "app": self.app,
            "role": self.role,
            "width": self.width,
            "height": self.height,
            "position": list(self.position) if self.position is not None else None,
            "active": self.active,
            "surface_id": self.surface_id,
        }


@dataclass(frozen=True)
class HeldState:
    keysyms: frozenset[int] = field(default_factory=frozenset)
    buttons: frozenset[int] = field(default_factory=frozenset)

    def to_dict(self) -> dict[str, list[int]]:
        return {
            "keysyms": sorted(self.keysyms),
            "buttons": sorted(self.buttons),
        }

    @property
    def empty(self) -> bool:
        return not self.keysyms and not self.buttons


class Capability(enum.Flag):
    CAPTURE = enum.auto()
    INPUT_POINTER = enum.auto()
    INPUT_KEYBOARD = enum.auto()
    CLIPBOARD = enum.auto()
    WINDOW_LIST = enum.auto()
    WINDOW_FOCUS = enum.auto()
    WINDOW_GEOMETRY = enum.auto()
    VIRTUAL_SURFACE = enum.auto()
    BOUND_CAPTURE_INPUT = enum.auto()


@dataclass(frozen=True)
class SurfaceSpec:
    kind: SurfaceKind = "monitor"
    id: str | None = None
    width: int | None = None
    height: int | None = None
    origin: tuple[int, int] = (0, 0)

