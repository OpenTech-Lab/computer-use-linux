from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .types import HeldState


class ActionLogger:
    def __init__(self, path: Path):
        self.path = path

    def log(self, tool: str, args: dict[str, Any], *, surface: str | None, held: HeldState) -> None:
        safe_args: dict[str, Any] = {}
        for key, value in args.items():
            if key in {"text", "type_text"} and isinstance(value, str):
                safe_args[key] = {"length": len(value)}
            else:
                safe_args[key] = value
        record = {
            "timestamp": time.time(),
            "tool": tool,
            "args": safe_args,
            "surface": surface,
            "held": held.to_dict(),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        except OSError:
            # Logging must never turn an input action into a stuck-input failure.
            return

