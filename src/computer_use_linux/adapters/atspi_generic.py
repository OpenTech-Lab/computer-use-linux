"""Generic semantic adapter for GTK, Qt, Electron and other AT-SPI applications."""

from __future__ import annotations

from typing import Any

from ..errors import BackendUnavailable
from ..windows.atspi import AtspiWindowSource, atspi_probe
from . import ActionSpec, AdapterBase, register


@register
class AtspiGenericAdapter(AdapterBase):
    """Expose AT-SPI identity, tree, text and ``Action.do_action`` operations.

    This adapter intentionally has no coordinate-click operation.  On the target Wayland
    session AT-SPI reports window origins as ``(0, 0)``; semantic actions are safer and more
    useful than pretending those extents are global desktop geometry.
    """

    name = "atspi_generic"
    # A shared Session supplies the compositor-safe activation fallback for focus. All tree and
    # action operations still go through AT-SPI and never translate extents into coordinates.
    needs_session = True

    def __init__(self, *, session: Any | None = None, config: Any | None = None):
        super().__init__(session=session, config=config)
        self._source: AtspiWindowSource | None = None

    def _window_source(self) -> AtspiWindowSource:
        if self._source is not None:
            return self._source
        if self.session is not None:
            source = self.session._get_window_source(required=True)
        else:
            source = AtspiWindowSource()
        self._source = source
        return source

    def detect(self) -> bool:
        available, _count, _detail = atspi_probe()
        return available

    def actions(self) -> list[ActionSpec]:
        return [
            ActionSpec(
                "windows",
                "List accessible application windows. Wayland positions are always null.",
            ),
            ActionSpec(
                "tree",
                "Return a bounded AT-SPI semantic tree, including native actions.",
                parameters={
                    "window_id": {"type": "string", "required": True, "description": "Stable AT-SPI window id."},
                    "max_depth": {"type": "integer", "default": 6, "description": "Maximum child depth."},
                },
            ),
            ActionSpec(
                "read_text",
                "Read the longest text node exposed by an accessible window.",
                parameters={
                    "window_id": {"type": "string", "required": True, "description": "Stable AT-SPI window id."},
                },
            ),
            ActionSpec(
                "focus",
                "Focus an accessible window and its first editable descendant.",
                parameters={
                    "window_id": {"type": "string", "required": True, "description": "Stable AT-SPI window id."},
                },
            ),
            ActionSpec(
                "actions",
                "List semantic AT-SPI actions exposed by a window and its descendants.",
                parameters={
                    "window_id": {"type": "string", "required": True, "description": "Stable AT-SPI window id."},
                    "max_depth": {"type": "integer", "default": 6, "description": "Maximum child depth."},
                },
            ),
            ActionSpec(
                "invoke_action",
                "Invoke an AT-SPI Action.do_action operation; no screen coordinates are used.",
                parameters={
                    "window_id": {"type": "string", "required": True, "description": "Stable AT-SPI window id."},
                    "name": {"type": "string", "required": True, "description": "Action name, description, keybinding, or numeric index."},
                    "path": {"type": "array", "default": [], "description": "Child-index path from the window root."},
                },
            ),
        ]

    def _invoke(self, action: str, **kwargs: Any) -> Any:
        source = self._window_source()
        if action == "windows":
            return {
                "windows": [window.to_dict() for window in source.list_windows()],
                "position_available": False,
                "position_reason": "Wayland AT-SPI does not expose reliable global window positions",
            }
        if action == "tree":
            return source.tree(str(kwargs["window_id"]), int(kwargs.get("max_depth", 6)))
        if action == "read_text":
            return {"window_id": str(kwargs["window_id"]), "text": source.window_text(str(kwargs["window_id"]))}
        if action == "focus":
            window_id = str(kwargs["window_id"])
            focused = self.session.focus_window(window_id) if self.session is not None else source.focus(window_id)
            return {"ok": bool(focused), "window_id": window_id}
        if action == "actions":
            return source.actions(str(kwargs["window_id"]), int(kwargs.get("max_depth", 6)))
        if action == "invoke_action":
            window_id = str(kwargs["window_id"])
            name: str | int
            raw_name = kwargs["name"]
            try:
                name = int(raw_name)
            except (TypeError, ValueError):
                name = str(raw_name)
            path = kwargs.get("path") or []
            return source.invoke_action(window_id, name, path=path)
        raise BackendUnavailable(f"unsupported AT-SPI adapter action: {action}")
