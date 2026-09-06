"""Small Linux process identity helpers for managed adapter lifecycles."""

from __future__ import annotations

from pathlib import Path


def start_ticks(pid: int) -> int | None:
    """Return Linux ``/proc/<pid>/stat`` start time, or ``None`` off Linux/unreadable."""

    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # The command name may contain spaces/parentheses; split only after its final ')'.
        fields = value[value.rfind(")") + 2 :].split()
        return int(fields[19])  # field 22 in proc(5), with pid/comm removed
    except (OSError, IndexError, ValueError):
        return None
