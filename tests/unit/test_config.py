from __future__ import annotations

from pathlib import Path

from computer_use_linux.config import load_config


def test_config_file_then_environment_precedence(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """[safety]\nconfirm_mode = 'all'\nmax_hold_seconds = 9\nredact_window_titles = ['secret']\n""",
        encoding="utf-8",
    )
    config = load_config(
        config_file,
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config-home"),
            "XDG_STATE_HOME": str(tmp_path / "state-home"),
            "CUL_CONFIRM_MODE": "off",
            "CUL_MAX_HOLD_SECONDS": "3",
        },
    )
    assert config.confirm_mode == "off"
    assert config.max_hold_seconds == 3
    assert config.redact_window_titles == ("secret",)
    assert config.state_dir == tmp_path / "state-home" / "computer-use-linux"
    assert config.panic_file == tmp_path / "config-home" / "computer-use-linux" / "PANIC"

