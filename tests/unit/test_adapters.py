from __future__ import annotations

from typing import Any

import pytest

from computer_use_linux.adapters import ActionSpec, AdapterBase, mcp_tool_function, registered_names
from computer_use_linux.config import Config
from computer_use_linux.errors import SafetyRefusal


def _config(tmp_path, *, confirm_mode: str = "destructive") -> Config:
    return Config(
        config_file=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        panic_file=tmp_path / "PANIC",
        actions_log=tmp_path / "actions.jsonl",
        confirm_mode=confirm_mode,
    )


def test_builtin_registry_contains_all_five_adapters() -> None:
    assert registered_names() == ["atspi_generic", "blender", "browser", "godot", "vscode"]


class _DangerousAdapter(AdapterBase):
    name = "test"

    def actions(self) -> list[ActionSpec]:
        return [
            ActionSpec(
                "run_python",
                "Run code.",
                parameters={"expr": {"type": "string", "required": True}},
            )
        ]

    def _invoke(self, action: str, **kwargs: Any) -> Any:
        return {"action": action, **kwargs}


def test_code_actions_are_confirmation_gated(tmp_path) -> None:
    adapter = _DangerousAdapter(config=_config(tmp_path))
    with pytest.raises(SafetyRefusal, match="confirmation required"):
        adapter.invoke("run-python", expr="import os")
    assert adapter.invoke("run_python", expr="1", confirm=True) == {"action": "run_python", "expr": "1"}


def test_mcp_tool_function_exposes_action_schema_and_alias() -> None:
    adapter = _DangerousAdapter(config=_config(__import__("pathlib").Path("/tmp")))
    spec = adapter.actions()[0]
    function = mcp_tool_function(adapter, spec, action_name="run_python")
    import inspect

    signature = inspect.signature(function)
    assert list(signature.parameters) == ["expr", "confirm"]
    assert signature.parameters["expr"].default is inspect.Parameter.empty
    assert signature.parameters["confirm"].default is False
    assert function(expr="1", confirm=True) == {"action": "run_python", "expr": "1"}


def test_all_confirmation_mode_is_exposed_for_every_action(tmp_path) -> None:
    class SafeAdapter(AdapterBase):
        name = "safe"

        def actions(self) -> list[ActionSpec]:
            return [ActionSpec("status", "Return status.")]

        def _invoke(self, action: str, **kwargs: Any) -> Any:
            return {"action": action}

    adapter = SafeAdapter(config=_config(tmp_path, confirm_mode="all"))
    function = mcp_tool_function(adapter, adapter.actions()[0])
    import inspect

    assert "confirm" in inspect.signature(function).parameters
    with pytest.raises(SafetyRefusal, match="confirmation required"):
        function(confirm=False)
    assert function(confirm=True) == {"action": "status"}
