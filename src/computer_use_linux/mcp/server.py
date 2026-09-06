from __future__ import annotations

import argparse
import asyncio
import json
from functools import wraps
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, ImageContent, TextContent

from ..adapters import create_adapters, mcp_tool_function
from ..errors import ComputerUseError
from ..session import Session
from ..types import Capability


def _json_result(value: Any) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False))])


def _adapter_result(value: Any) -> CallToolResult:
    """Convert adapter values to MCP content, keeping PNGs as real image blocks."""

    if isinstance(value, CallToolResult):
        return value
    if isinstance(value, dict) and value.get("png_base64"):
        encoded = str(value["png_base64"])
        image = ImageContent(type="image", data=encoded, mime_type=str(value.get("mime_type", "image/png")))
        metadata = {key: item for key, item in value.items() if key != "png_base64"}
        return CallToolResult(content=[image, TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False))])
    return _json_result(value)


def create_server(session: Session | None = None, *, isolated: bool | None = None) -> MCPServer:
    """Create the stdio MCP server over one shared Session."""

    session = session or Session(isolated=isolated)
    server = MCPServer(
        name="computer-use-linux",
        version="0.1.0",
        description="Safe, local Linux desktop capture and input",
        instructions="Coordinates are local to the selected capture/input surface; screenshot metadata describes image scaling.",
    )

    def guarded(function: Any) -> Any:
        @wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return function(*args, **kwargs)
            except Exception:
                session.release_all()
                raise

        return wrapper

    @guarded
    def screenshot(
        surface_id: str | None = None,
        region: dict[str, int] | None = None,
        max_width: int = 1280,
        cursor: bool = True,
    ) -> CallToolResult:
        result = session.screenshot(surface_id=surface_id, region=region, max_width=max_width, cursor=cursor)
        image = ImageContent(type="image", data=__import__("base64").b64encode(result.png).decode("ascii"), mime_type="image/png")
        metadata = TextContent(type="text", text=json.dumps(result.metadata, separators=(",", ":")))
        return CallToolResult(content=[image, metadata])

    @guarded
    def list_surfaces() -> CallToolResult:
        return _json_result([surface.to_dict() for surface in session.surfaces])

    @guarded
    def select_surface(surface_id: str) -> CallToolResult:
        return _json_result(session.select_surface(surface_id).to_dict())

    @guarded
    def move(
        x: float,
        y: float,
        surface_id: str | None = None,
        coord_space: str = "image",
    ) -> CallToolResult:
        return _json_result(session.move(x, y, surface_id=surface_id, coord_space=coord_space))

    @guarded
    def click(
        x: float,
        y: float,
        button: str = "left",
        count: int = 1,
        modifiers: list[str] | None = None,
        surface_id: str | None = None,
        coord_space: str = "image",
    ) -> CallToolResult:
        button_number = {"left": 1, "middle": 2, "right": 3}.get(button, 0)
        if not button_number:
            raise ValueError("button must be left, middle, or right")
        return _json_result(session.click(x, y, surface_id=surface_id, coord_space=coord_space, button=button_number, count=count, modifiers=modifiers))

    @guarded
    def drag(
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        button: str = "left",
        steps: int = 20,
        surface_id: str | None = None,
        coord_space: str = "image",
    ) -> CallToolResult:
        button_number = {"left": 1, "middle": 2, "right": 3}.get(button, 0)
        if not button_number:
            raise ValueError("button must be left, middle, or right")
        return _json_result(session.drag(from_x, from_y, to_x, to_y, surface_id=surface_id, button=button_number, steps=steps, coord_space=coord_space))

    @guarded
    def scroll(
        x: float,
        y: float,
        dx: float = 0,
        dy: float = 0,
        surface_id: str | None = None,
        coord_space: str = "image",
    ) -> CallToolResult:
        return _json_result(session.scroll(x, y, dx=dx, dy=dy, surface_id=surface_id, coord_space=coord_space))

    @guarded
    def key(keys: str | list[str], confirm: bool = False) -> CallToolResult:
        return _json_result(session.key(keys, confirm=confirm))

    @guarded
    def type_text(text: str, mode: str = "auto", confirm: bool = False) -> CallToolResult:
        return _json_result(session.type_text(text, mode=mode, confirm=confirm))

    @guarded
    def list_windows() -> CallToolResult:
        positions_available = bool(getattr(session.backend, "capabilities", Capability(0)) & Capability.WINDOW_GEOMETRY)
        return _json_result(
            {
                "windows": [window.to_dict() for window in session.list_windows()],
                "position_available": positions_available,
                "position_reason": None
                if positions_available
                else "the selected backend does not expose reliable global window positions",
            }
        )

    @guarded
    def focus_window(window_id: str) -> CallToolResult:
        return _json_result({"ok": session.focus_window(window_id), "window_id": window_id})

    @guarded
    def window_tree(window_id: str, max_depth: int = 6) -> CallToolResult:
        return _json_result(session.window_tree(window_id, max_depth))

    @guarded
    def wait_for_change(
        surface_id: str | None = None,
        timeout: float = 5.0,
        threshold: float = 0.002,
    ) -> CallToolResult:
        return _json_result(session.wait_for_change(surface_id=surface_id, timeout=timeout, threshold=threshold))

    @guarded
    def panic() -> CallToolResult:
        return _json_result(session.panic())

    tools = (
        (screenshot, "screenshot", "Capture a PNG frame and return its geometry metadata."),
        (list_surfaces, "list_surfaces", "List capture/input surfaces, including virtual surfaces when available."),
        (select_surface, "select_surface", "Select the default capture/input surface."),
        (move, "move", "Move the pointer in image or native surface pixels."),
        (click, "click", "Click at a coordinate on the selected surface."),
        (drag, "drag", "Drag between two coordinates on the selected surface."),
        (scroll, "scroll", "Scroll at a coordinate on the selected surface."),
        (key, "key", "Press and release keysym-based key chords."),
        (type_text, "type_text", "Type text using the layout-safe keysym path when possible."),
        (list_windows, "list_windows", "List accessible windows with geometry when the backend can report it."),
        (focus_window, "focus_window", "Focus an accessible window."),
        (window_tree, "window_tree", "Return a bounded AT-SPI semantic tree."),
        (wait_for_change, "wait_for_change", "Wait for a measurable screenshot change."),
        (panic, "panic", "Release all input and tear down the session."),
    )
    for function, name, description in tools:
        server.add_tool(function, name=name, description=description, structured_output=False)
    adapters = create_adapters(session=session, config=session.config)
    for adapter_name, adapter in adapters.items():
        for spec in adapter.actions():
            exposed_actions = (spec.name, *spec.aliases)
            for exposed_action in exposed_actions:
                function = mcp_tool_function(adapter, spec, action_name=exposed_action)
                original = function

                @wraps(original)
                def guarded_adapter_tool(*args: Any, _original: Any = original, **kwargs: Any) -> CallToolResult:
                    del args
                    try:
                        return _adapter_result(_original(**kwargs))
                    except Exception:
                        session.release_all()
                        raise

                guarded_adapter_tool.__name__ = f"app_{adapter_name}_{exposed_action.replace('-', '_')}"
                guarded_adapter_tool.__signature__ = original.__signature__
                server.add_tool(
                    guarded_adapter_tool,
                    name=guarded_adapter_tool.__name__,
                    description=spec.description,
                    structured_output=False,
                )
    # Keep the owner alive for the whole MCP transport lifetime and make it available to tests.
    server._cul_session = session  # type: ignore[attr-defined]
    server._cul_adapters = adapters  # type: ignore[attr-defined]
    return server


async def _selftest_async(isolated: bool | None = None) -> int:
    session = Session(isolated=isolated)
    server = create_server(session)
    try:
        tools = await server.list_tools()
        names = [tool.name for tool in tools]
        required = [
            "screenshot",
            "list_surfaces",
            "select_surface",
            "move",
            "click",
            "drag",
            "scroll",
            "key",
            "type_text",
            "list_windows",
            "focus_window",
            "window_tree",
            "wait_for_change",
            "panic",
            "app_browser_launch",
            "app_browser_navigate",
            "app_godot_eval_gdscript",
            "app_godot_eval",
            "app_blender_run_python",
            "app_vscode_command",
            "app_atspi_generic_windows",
        ]
        missing = [name for name in required if name not in names]
        if missing:
            raise RuntimeError(f"missing tools: {missing}")
        print("tools: " + ", ".join(names) + "  PASS")

        surface_id = "monitor:DP-2" if any(surface.id == "monitor:DP-2" for surface in session.surfaces) else session.active_surface_id
        result = await server.call_tool("screenshot", {"surface_id": surface_id, "max_width": 1280, "cursor": True})
        first, second = result.content[0], result.content[1]
        metadata = json.loads(second.text)  # type: ignore[attr-defined]
        mime = getattr(first, "mime_type", None)
        expected_scale = 1280 / metadata["surface_width"]
        if first.type != "image" or mime != "image/png":
            raise RuntimeError(f"unexpected screenshot content: {first}")
        if metadata["image_width"] != 1280 or metadata["scale"] != expected_scale:
            raise RuntimeError(f"unexpected screenshot metadata: {metadata}")
        print(
            f"screenshot: type=image mimeType=image/png image_width={metadata['image_width']} "
            f"surface_width={metadata['surface_width']} scale={metadata['scale']}  PASS"
        )

        await server.call_tool("click", {"surface_id": surface_id, "x": 1, "y": 1, "coord_space": "surface"})
        await server.call_tool("type_text", {"text": "", "mode": "keysym"})
        # Park the pointer back on the primary test surface before closing.
        await server.call_tool("move", {"surface_id": surface_id, "x": 960, "y": 540, "coord_space": "surface"})
        print("click: PASS")
        print("type_text: PASS")
        print("mcp selftest: PASS")
        return 0
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cul-mcp")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--isolated", action="store_true", help="default the session to an isolated-capable surface")
    args = parser.parse_args(argv)
    isolated = True if args.isolated else None
    if args.selftest:
        try:
            return asyncio.run(_selftest_async(isolated=isolated))
        except (ComputerUseError, OSError, ValueError, RuntimeError) as exc:
            print(f"mcp selftest: FAIL: {exc}")
            return 1

    session = Session(isolated=isolated)
    server = create_server(session)
    try:
        server.run("stdio")
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
