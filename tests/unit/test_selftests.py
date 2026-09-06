from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from computer_use_linux import cli
from computer_use_linux.types import Frame, HeldState, Surface


class FakeSession:
    def __init__(self, *, positions: dict[str, tuple[int, int] | None], held: HeldState | None = None):
        self.backend = SimpleNamespace(
            cursor_position=lambda surface_id, **_kwargs: positions.get(surface_id),
            held=lambda: held or HeldState(),
        )
        self.surfaces = [
            Surface("monitor:DP-2", "monitor", 1920, 1080, (0, 0), 1.0, "DP-2"),
            Surface("monitor:HDMI-1", "monitor", 1920, 1080, (1920, 0), 1.0, "HDMI-1"),
        ]
        self.moves: list[tuple[int, int, str]] = []

    def move(self, x: int, y: int, *, surface_id: str, coord_space: str) -> None:
        self.moves.append((x, y, surface_id))


def test_coords_selftest_returns_nonzero_when_metadata_measurement_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    session = FakeSession(positions={"monitor:DP-2": None})

    result = cli._run_coords_selftest(session, "monitor:DP-2")

    captured = capsys.readouterr()
    assert result == 1
    assert "coords selftest: 0/3" in captured.out
    assert "FAIL" in captured.out


def test_monitors_selftest_returns_nonzero_when_pointer_is_seen_on_both_monitors(capsys) -> None:
    session = FakeSession(positions={"monitor:DP-2": (300, 200), "monitor:HDMI-1": (300, 200)})

    result = cli._run_monitors_selftest(session)

    captured = capsys.readouterr()
    assert result == 1
    assert "monitors selftest: FAIL" in captured.err


def test_modifiers_selftest_returns_nonzero_when_a_key_is_held(capsys) -> None:
    session = FakeSession(positions={}, held=HeldState(frozenset({42}), frozenset()))

    result = cli._run_modifiers_selftest(session)

    captured = capsys.readouterr()
    assert result == 1
    assert "modifiers held:" in captured.out
    assert "FAIL" in captured.out


def test_coords_selftest_skips_unreliable_differential_measurement(monkeypatch, capsys) -> None:
    class DifferentialOnlyBackend:
        def grab(self, _surface_id: str, *, timeout: float) -> Frame:
            del timeout
            return Frame("monitor:DP-2", 2, 2, np.zeros((2, 2, 3), dtype=np.uint8), 0.0)

    session = SimpleNamespace(backend=DifferentialOnlyBackend(), surfaces=[])
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    # Replace the second frame with a visibly different desktop so the
    # differential path is forced into its explicit SKIPPED result.
    frames = iter(
        (
            Frame("monitor:DP-2", 2, 2, np.zeros((2, 2, 3), dtype=np.uint8), 0.0),
            Frame("monitor:DP-2", 2, 2, np.full((2, 2, 3), 255, dtype=np.uint8), 0.0),
        )
    )
    session.backend.grab = lambda _surface_id, *, timeout: next(frames)

    result = cli._run_coords_selftest(session, "monitor:DP-2")

    captured = capsys.readouterr()
    assert result == 0
    assert "SKIPPED - screen not quiescent" in captured.out
