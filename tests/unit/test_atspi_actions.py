from __future__ import annotations

from types import SimpleNamespace

from computer_use_linux.types import WindowInfo
from computer_use_linux.windows.atspi import AtspiWindowSource, _actions, _WindowRef


class FakeAction:
    def __init__(self):
        self.invoked: list[int] = []

    def get_n_actions(self) -> int:
        return 1

    def get_action_name(self, index: int) -> str:
        assert index == 0
        return "doDefault"

    def get_action_description(self, index: int) -> str:
        assert index == 0
        return "activate"

    def get_key_binding(self, index: int) -> str:
        assert index == 0
        return ""

    def do_action(self, index: int) -> bool:
        self.invoked.append(index)
        return True


class FakeNode:
    def __init__(self, action: FakeAction):
        self.action = action

    def get_action(self) -> FakeAction:
        return self.action

    def get_name(self) -> str:
        return "Button"

    def get_role_name(self) -> str:
        return "push button"

    def get_child_count(self) -> int:
        return 0


def test_actions_use_atspi_action_names_and_indices() -> None:
    action = FakeAction()
    assert _actions(FakeNode(action)) == [{"index": 0, "name": "doDefault", "description": "activate", "keybinding": ""}]


def test_invoke_action_calls_do_action_without_coordinates() -> None:
    action = FakeAction()
    node = FakeNode(action)
    source = object.__new__(AtspiWindowSource)
    source._atspi = SimpleNamespace()
    source._refs = {
        "window:test:0": _WindowRef(
            app=SimpleNamespace(),
            window=node,
            app_name="test",
            index=0,
            info=WindowInfo("window:test:0", "Test", "test", "frame", 100, 100, None, False),
        )
    }
    source._lock = __import__("threading").RLock()
    source.list_windows = lambda: [source._refs["window:test:0"].info]
    result = source.invoke_action("window:test:0", "doDefault")
    assert result["ok"] is True
    assert result["path"] == []
    assert action.invoked == [0]
