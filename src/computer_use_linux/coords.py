from __future__ import annotations

import math
from typing import Literal

from .errors import InputError
from .types import Surface

CoordSpace = Literal["image", "surface"]


def image_dimensions(surface: Surface, max_width: int | None) -> tuple[int, int, float]:
    """Return image width, height and image/native scale for a surface."""

    if max_width is None or max_width <= 0 or surface.width <= max_width:
        return surface.width, surface.height, 1.0
    scale = max_width / surface.width
    return max_width, max(1, round(surface.height * scale)), scale


def _check_number(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise InputError(f"{name} must be finite")
    return value


def surface_coordinates(
    x: float,
    y: float,
    *,
    surface: Surface,
    coord_space: CoordSpace = "image",
    image_scale: float = 1.0,
    image_origin: tuple[int, int] = (0, 0),
) -> tuple[int, int]:
    """Convert one API point to native stream pixels and round exactly once."""

    x = _check_number(x, "x")
    y = _check_number(y, "y")
    if coord_space not in {"image", "surface"}:
        raise InputError("coord_space must be 'image' or 'surface'")
    if image_scale <= 0 or not math.isfinite(image_scale):
        raise InputError("image_scale must be positive and finite")
    if coord_space == "image":
        x = image_origin[0] + x / image_scale
        y = image_origin[1] + y / image_scale
    if x < 0 or y < 0:
        raise InputError(f"coordinates ({x}, {y}) are outside {surface.id}")
    # A pointer can address the last pixel, not the exclusive image edge.
    native_x = min(surface.width - 1, round(x))
    native_y = min(surface.height - 1, round(y))
    if native_x < 0 or native_y < 0:
        raise InputError(f"coordinates are outside {surface.id}")
    return native_x, native_y


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
