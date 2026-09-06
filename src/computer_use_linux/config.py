from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _default_config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "computer-use-linux"


def _default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "computer-use-linux"


@dataclass(frozen=True)
class Config:
    config_file: Path
    state_dir: Path
    panic_file: Path
    actions_log: Path
    backend: str | None = None
    isolated: bool = False
    virtual_width: int = 1280
    virtual_height: int = 720
    virtual_refresh: float = 30.0
    confirm_mode: str = "destructive"
    max_hold_seconds: float = 5.0
    redact_regions: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    redact_window_titles: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_file": str(self.config_file),
            "state_dir": str(self.state_dir),
            "panic_file": str(self.panic_file),
            "actions_log": str(self.actions_log),
            "backend": self.backend,
            "isolated": self.isolated,
            "virtual_width": self.virtual_width,
            "virtual_height": self.virtual_height,
            "virtual_refresh": self.virtual_refresh,
            "confirm_mode": self.confirm_mode,
            "max_hold_seconds": self.max_hold_seconds,
            "redact_regions": [dict(region) for region in self.redact_regions],
            "redact_window_titles": list(self.redact_window_titles),
        }


def _file_value(raw: Mapping[str, Any], key: str, default: Any) -> Any:
    if key in raw:
        return raw[key]
    for section_name in ("session", "safety"):
        section = raw.get(section_name)
        if isinstance(section, Mapping) and key in section:
            return section[key]
    return default


def _as_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().casefold() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(value, str) and value.strip().casefold() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be a boolean")


def load_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Config:
    """Load config file values, then apply CUL_* environment overrides."""

    env = os.environ if environ is None else environ
    config_home = Path(env.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    state_home = Path(env.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    config_dir = config_home / "computer-use-linux"
    config_file = Path(path or env.get("CUL_CONFIG_FILE", config_dir / "config.toml")).expanduser()
    raw: dict[str, Any] = {}
    if config_file.exists():
        with config_file.open("rb") as handle:
            parsed = tomllib.load(handle)
        if not isinstance(parsed, dict):  # pragma: no cover - tomllib always returns a dict
            raise ValueError(f"configuration root must be a table: {config_file}")
        raw = parsed

    state_dir = Path(env.get("CUL_STATE_DIR", state_home / "computer-use-linux")).expanduser()
    # Use the XDG config location by default even when a custom state directory is selected.
    panic_file = Path(env.get("CUL_PANIC_FILE", config_dir / "PANIC")).expanduser()
    actions_log = Path(env.get("CUL_ACTIONS_LOG", state_dir / "actions.jsonl")).expanduser()

    backend = env.get("CUL_BACKEND", _file_value(raw, "backend", None))
    isolated = _as_bool(env.get("CUL_ISOLATED", _file_value(raw, "isolated", False)), "isolated")

    virtual_width = int(env.get("CUL_VIRTUAL_WIDTH", _file_value(raw, "virtual_width", 1280)))
    virtual_height = int(env.get("CUL_VIRTUAL_HEIGHT", _file_value(raw, "virtual_height", 720)))
    virtual_refresh = float(env.get("CUL_VIRTUAL_REFRESH", _file_value(raw, "virtual_refresh", 30.0)))
    if virtual_width <= 0 or virtual_height <= 0:
        raise ValueError("virtual_width and virtual_height must be positive")
    if virtual_refresh <= 0:
        raise ValueError("virtual_refresh must be positive")

    confirm_mode = str(env.get("CUL_CONFIRM_MODE", _file_value(raw, "confirm_mode", "destructive"))).lower()
    if confirm_mode not in {"off", "destructive", "all"}:
        raise ValueError("confirm_mode must be one of off, destructive, all")

    max_hold_raw = env.get("CUL_MAX_HOLD_SECONDS", _file_value(raw, "max_hold_seconds", 5.0))
    max_hold_seconds = float(max_hold_raw)
    if max_hold_seconds <= 0:
        raise ValueError("max_hold_seconds must be positive")

    regions_raw = _file_value(raw, "redact_regions", [])
    titles_raw = _file_value(raw, "redact_window_titles", [])
    if not isinstance(regions_raw, list) or not isinstance(titles_raw, list):
        raise ValueError("redact_regions and redact_window_titles must be arrays")

    return Config(
        config_file=config_file,
        state_dir=state_dir,
        panic_file=panic_file,
        actions_log=actions_log,
        backend=str(backend) if backend else None,
        isolated=isolated,
        virtual_width=virtual_width,
        virtual_height=virtual_height,
        virtual_refresh=virtual_refresh,
        confirm_mode=confirm_mode,
        max_hold_seconds=max_hold_seconds,
        redact_regions=tuple(dict(region) for region in regions_raw),
        redact_window_titles=tuple(str(title) for title in titles_raw),
    )
