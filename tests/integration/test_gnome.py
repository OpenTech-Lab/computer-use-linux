from __future__ import annotations

import os
import time

import pytest


@pytest.mark.requires_session
@pytest.mark.disruptive
def test_gnome_session_is_available() -> None:
    if not os.environ.get("WAYLAND_DISPLAY"):
        pytest.skip("no Wayland session")
    from computer_use_linux.session import Session

    session = Session()
    try:
        assert {surface.id for surface in session.surfaces} >= {"monitor:DP-2"}
    finally:
        session.close()


@pytest.mark.requires_session
@pytest.mark.disruptive
def test_gnome_multimonitor_pointer_targeting(capsys) -> None:
    if not os.environ.get("WAYLAND_DISPLAY"):
        pytest.skip("no Wayland session")
    from computer_use_linux.cli import _run_monitors_selftest
    from computer_use_linux.session import Session

    session = Session()
    try:
        required = {"monitor:DP-2", "monitor:HDMI-1"}
        if not required.issubset({surface.id for surface in session.surfaces}):
            pytest.skip("the decisive monitor test requires DP-2 and HDMI-1")
        assert callable(getattr(session.backend, "cursor_position", None))
        result = _run_monitors_selftest(session)
        output = capsys.readouterr()
        assert result == 0
        assert "monitors selftest: PASS" in output.out
    finally:
        session.close()


@pytest.mark.requires_session
@pytest.mark.disruptive
def test_gnome_cursor_metadata_observations_are_distinct() -> None:
    if not os.environ.get("WAYLAND_DISPLAY"):
        pytest.skip("no Wayland session")
    from computer_use_linux.session import Session

    session = Session()
    try:
        surface_id = "monitor:DP-2"
        if surface_id not in {surface.id for surface in session.surfaces}:
            pytest.skip("the cursor metadata regression test requires DP-2")
        targets = ((400, 300), (100, 100), (1800, 1000))
        observed: list[tuple[int, int] | None] = []
        try:
            for target in targets:
                session.move(*target, surface_id=surface_id, coord_space="surface")
                time.sleep(0.45)
                observed.append(session.backend.cursor_position(surface_id, timeout=2.0))
        finally:
            session.move(960, 540, surface_id=surface_id, coord_space="surface")
        assert all(position is not None for position in observed)
        assert len(set(observed)) == len(targets)
    finally:
        session.close()
