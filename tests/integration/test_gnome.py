from __future__ import annotations

import os

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
