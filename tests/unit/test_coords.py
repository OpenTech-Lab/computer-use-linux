from __future__ import annotations

import pytest

from computer_use_linux.coords import image_dimensions, surface_coordinates
from computer_use_linux.errors import InputError
from computer_use_linux.types import Surface


def surface(scale: float = 1.0) -> Surface:
    return Surface("monitor:test", "monitor", 1920, 1080, (300, 20), scale, "test")


def test_downscale_records_native_image_scale() -> None:
    assert image_dimensions(surface(), 1280) == (1280, 720, 1280 / 1920)


def test_image_coordinates_are_converted_once_and_crop_origin_is_honoured() -> None:
    assert surface_coordinates(640, 360, surface=surface(), image_scale=2 / 3) == (960, 540)
    assert surface_coordinates(10, 20, surface=surface(), image_scale=0.5, image_origin=(100, 40)) == (120, 80)


def test_hidpi_surface_scale_is_reported_not_applied() -> None:
    assert surface_coordinates(100, 200, surface=surface(scale=2.0), coord_space="surface") == (100, 200)


def test_rounding_and_edges() -> None:
    assert surface_coordinates(10.49, 20.51, surface=surface(), coord_space="surface") == (10, 21)
    assert surface_coordinates(99999, 99999, surface=surface(), coord_space="surface") == (1919, 1079)
    with pytest.raises(InputError):
        surface_coordinates(-1, 0, surface=surface(), coord_space="surface")
