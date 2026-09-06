from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from ..errors import BackendUnavailable, SurfaceNotFound
from ..types import WindowInfo


def _load_atspi() -> Any:
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        return Atspi
    except Exception as exc:  # pragma: no cover - host dependency
        raise BackendUnavailable(
            "AT-SPI is unavailable; install gir1.2-atspi-2.0 and use the prepared CPython 3.14 .venv"
        ) from exc


def _state_contains(accessible: Any, atspi: Any, state: Any) -> bool:
    try:
        state_set = accessible.get_state_set()
        return bool(state_set and state_set.contains(state))
    except Exception:
        return False


def _component_rect(accessible: Any) -> tuple[int, int, int, int] | None:
    try:
        component = accessible.get_component()
        if component is None:
            return None
        rect = component.get_extents(_load_atspi().CoordType.SCREEN)
        return int(rect.x), int(rect.y), int(rect.width), int(rect.height)
    except Exception:
        return None


def _text(accessible: Any, atspi: Any, limit: int = 4000) -> str:
    try:
        if "Text" not in accessible.get_interfaces():
            return ""
        count = int(atspi.Text.get_character_count(accessible))
        if count <= 0:
            return ""
        return str(atspi.Text.get_text(accessible, 0, min(count, limit)))
    except Exception:
        return ""


def _actions(accessible: Any, limit: int = 32) -> list[dict[str, Any]]:
    """Return the semantic actions exposed by one accessible object.

    AT-SPI action indices are local to the object.  Keeping the index in the result lets callers
    invoke the action without ever translating it into a screen coordinate.
    """

    try:
        action = accessible.get_action()
        if action is None:
            return []
        count = min(int(action.get_n_actions()), limit)
        result: list[dict[str, Any]] = []
        for index in range(count):
            get_name = getattr(action, "get_action_name", None) or getattr(action, "get_name")
            get_description = getattr(action, "get_action_description", None) or getattr(action, "get_description")
            result.append(
                {
                    "index": index,
                    "name": str(get_name(index) or ""),
                    "description": str(get_description(index) or ""),
                    "keybinding": str(action.get_key_binding(index) or ""),
                }
            )
        return result
    except Exception:
        return []


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", value.casefold()).strip("-") or "app"


@dataclass
class _WindowRef:
    app: Any
    window: Any
    app_name: str
    index: int
    info: WindowInfo


def atspi_probe() -> tuple[bool, int, str | None]:
    try:
        atspi = _load_atspi()
        atspi.init()
        desktop = atspi.get_desktop(0)
        if desktop is None:
            return False, 0, "AT-SPI desktop is unavailable"
        return True, int(desktop.get_child_count()), None
    except Exception as exc:
        return False, 0, str(exc)


class AtspiWindowSource:
    """AT-SPI application/window source.

    On Wayland the client-reported screen origin is deliberately discarded. X11 callers can opt
    into the same extents with ``include_position=True`` because the X server supplies a real root
    coordinate space there.
    """

    def __init__(self, *, include_position: bool = False):
        self._atspi = _load_atspi()
        self._atspi.init()
        self._desktop = self._atspi.get_desktop(0)
        if self._desktop is None:
            raise BackendUnavailable("AT-SPI desktop is unavailable")
        self._refs: dict[str, _WindowRef] = {}
        self._lock = threading.RLock()
        self._include_position = bool(include_position)

    def _applications(self) -> list[Any]:
        apps: list[Any] = []
        for index in range(int(self._desktop.get_child_count())):
            try:
                app = self._desktop.get_child_at_index(index)
                if app is not None:
                    apps.append(app)
            except Exception:
                continue
        return apps

    def list_windows(self) -> list[WindowInfo]:
        refs: dict[str, _WindowRef] = {}
        result: list[WindowInfo] = []
        for app in self._applications():
            try:
                app_name = str(app.get_name() or "")
                count = int(app.get_child_count())
            except Exception:
                continue
            window_index = 0
            for child_index in range(count):
                try:
                    window = app.get_child_at_index(child_index)
                    if window is None:
                        continue
                    role = str(window.get_role_name() or "")
                    if role.casefold() not in {"window", "frame", "dialog", "alert"}:
                        continue
                    title = str(window.get_name() or "")
                    rect = _component_rect(window)
                    width = rect[2] if rect else None
                    height = rect[3] if rect else None
                    active = _state_contains(window, self._atspi, self._atspi.StateType.FOCUSED) or _state_contains(
                        window, self._atspi, self._atspi.StateType.ACTIVE
                    )
                    window_id = f"window:{_slug(app_name)}:{window_index}"
                    info = WindowInfo(
                        id=window_id,
                        title=title,
                        app=app_name,
                        role=role,
                        width=width,
                        height=height,
                        position=rect[:2] if rect is not None and self._include_position else None,
                        active=active,
                        surface_id=None,
                    )
                    refs[window_id] = _WindowRef(app, window, app_name, window_index, info)
                    result.append(info)
                    window_index += 1
                except Exception:
                    continue
        with self._lock:
            self._refs = refs
        return result

    def _ref(self, window_id: str) -> _WindowRef:
        self.list_windows()
        with self._lock:
            ref = self._refs.get(window_id)
        if ref is None:
            raise SurfaceNotFound(f"unknown window: {window_id}")
        return ref

    def focus(self, window_id: str) -> bool:
        ref = self._ref(window_id)
        focused = False
        try:
            component = ref.window.get_component()
            focused = bool(component and component.grab_focus())
        except Exception:
            focused = False

        # A frame focus is not always enough to give a GTK4 editor its caret. Prefer the first
        # editable text descendant and place its caret at the end for the Phase 1 type/readback path.
        editable = self._find_editable(ref.window)
        if editable is not None:
            try:
                component = editable.get_component()
                if component is not None:
                    focused = bool(component.grab_focus()) or focused
                count = int(self._atspi.Text.get_character_count(editable))
                self._atspi.Text.set_caret_offset(editable, count)
            except Exception:
                pass
        return focused

    def is_active(self, window_id: str) -> bool:
        return any(info.id == window_id and info.active for info in self.list_windows())

    def application_search_name(self, window_id: str) -> str:
        """Return a human-facing GNOME overview query for an application's window."""

        ref = self._ref(window_id)
        try:
            app_id = str(ref.app.get_accessible_id() or "")
        except Exception:
            app_id = ""
        names = {
            "org.gnome.TextEditor": "Text Editor",
            "org.gnome.Terminal": "Terminal",
            "code": "Visual Studio Code",
        }
        return names.get(app_id, ref.app_name.replace("-", " "))

    def activate(self, window_id: str) -> bool:
        """Raise an application through its own GTK D-Bus activation interface.

        Wayland intentionally rejects a foreign AT-SPI client's request to raise a window. GTK
        applications still expose the normal application activation contract, which is the same
        user-visible path used by a desktop launcher. Text Editor's existing window can be stale
        on another workspace in this test environment, so its ``new-window`` action is the final
        app-local fallback; no GNOME Shell private API is involved.
        """

        ref = self._ref(window_id)
        try:
            app_id = str(ref.app.get_accessible_id() or "")
        except Exception:
            app_id = ""
        if not app_id:
            return False
        try:
            import gi

            gi.require_version("Gio", "2.0")
            gi.require_version("GLib", "2.0")
            from gi.repository import Gio, GLib

            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            path = "/" + app_id.replace(".", "/")
            bus.call_sync(
                app_id,
                path,
                "org.gtk.Application",
                "Activate",
                GLib.Variant("(a{sv})", ({},)),
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            time.sleep(0.25)
            if self.is_active(window_id):
                return True
            if app_id == "org.gnome.TextEditor":
                bus.call_sync(
                    app_id,
                    path,
                    "org.gtk.Actions",
                    "Activate",
                    GLib.Variant("(sava{sv})", ("new-window", [], {})),
                    None,
                    Gio.DBusCallFlags.NONE,
                    3000,
                    None,
                )
                time.sleep(0.35)
                return any(info.app == ref.app_name and info.active for info in self.list_windows())
        except Exception:
            return False
        return False

    def _find_editable(self, root: Any) -> Any | None:
        try:
            if "EditableText" in root.get_interfaces():
                return root
            for index in range(int(root.get_child_count())):
                child = root.get_child_at_index(index)
                if child is None:
                    continue
                found = self._find_editable(child)
                if found is not None:
                    return found
        except Exception:
            return None
        return None

    def _find_text_nodes(self, root: Any, result: list[Any]) -> None:
        try:
            if "Text" in root.get_interfaces():
                result.append(root)
            for index in range(int(root.get_child_count())):
                child = root.get_child_at_index(index)
                if child is not None:
                    self._find_text_nodes(child, result)
        except Exception:
            return

    def window_text(self, window_id: str) -> str:
        ref = self._ref(window_id)
        nodes: list[Any] = []
        self._find_text_nodes(ref.window, nodes)
        editable = [node for node in nodes if "EditableText" in node.get_interfaces()]
        candidates = editable or nodes
        values = [_text(node, self._atspi) for node in candidates]
        value = max(values, key=len, default="")
        # GTK's empty TextView is represented by a non-breaking-space placeholder.
        if value in {"\xa0", "\u200b"}:
            return ""
        value = value.replace("\xa0", "")
        # GNOME Text Editor exposes its empty-document placeholder in the same AT-SPI Text
        # node as user content. It is presentation text, not part of the document.
        if ref.app_name == "gnome-text-editor" and value.startswith("Text Editor\n"):
            value = value.removeprefix("Text Editor\n")
        return value

    def actions(self, window_id: str, max_depth: int = 6) -> list[dict[str, Any]]:
        """List AT-SPI actions with a child path for deterministic invocation."""

        ref = self._ref(window_id)
        result: list[dict[str, Any]] = []

        def visit(node: Any, path: tuple[int, ...], depth: int) -> None:
            for action in _actions(node):
                action.update(
                    {
                        "path": list(path),
                        "role": str(node.get_role_name() or ""),
                        "node_name": str(node.get_name() or ""),
                        "text": _text(node, self._atspi, limit=300),
                    }
                )
                result.append(action)
            if depth >= max_depth:
                return
            try:
                count = int(node.get_child_count())
                for index in range(count):
                    child = node.get_child_at_index(index)
                    if child is not None:
                        visit(child, path + (index,), depth + 1)
            except Exception:
                return

        visit(ref.window, (), 0)
        return result

    def invoke_action(
        self,
        window_id: str,
        action_name: str | int,
        *,
        path: list[int] | tuple[int, ...] | None = None,
        max_depth: int = 6,
    ) -> dict[str, Any]:
        """Invoke an AT-SPI action by name or index, without pointer coordinates."""

        ref = self._ref(window_id)
        node = ref.window
        selected_path = tuple(int(value) for value in (path or ()))
        try:
            for child_index in selected_path:
                node = node.get_child_at_index(child_index)
                if node is None:
                    raise SurfaceNotFound(f"AT-SPI child path does not exist: {list(selected_path)}")
        except SurfaceNotFound:
            raise
        except Exception as exc:
            raise SurfaceNotFound(f"AT-SPI child path does not exist: {list(selected_path)}") from exc

        available = _actions(node)
        chosen_index: int | None = None
        if isinstance(action_name, int):
            chosen_index = action_name if any(item["index"] == action_name for item in available) else None
        else:
            wanted = action_name.casefold()
            for item in available:
                if wanted in {
                    str(item["name"]).casefold(),
                    str(item["description"]).casefold(),
                    str(item["keybinding"]).casefold(),
                }:
                    chosen_index = int(item["index"])
                    break
        if chosen_index is None:
            raise SurfaceNotFound(
                f"AT-SPI action {action_name!r} was not found on {str(node.get_name() or '')!r}; "
                f"available={[item['name'] for item in available]}"
            )
        try:
            action = node.get_action()
            invoked = bool(action.do_action(chosen_index))
        except Exception as exc:
            raise BackendUnavailable(f"AT-SPI action {action_name!r} failed: {exc}") from exc
        if not invoked:
            raise BackendUnavailable(f"AT-SPI action {action_name!r} declined by the application")
        return {
            "ok": True,
            "window_id": window_id,
            "action": action_name,
            "index": chosen_index,
            "path": list(selected_path),
            "max_depth": max_depth,
        }

    def focused_text(self) -> tuple[WindowInfo, str] | None:
        windows = self.list_windows()
        fallback: tuple[WindowInfo, str] | None = None
        shell_fallback: tuple[WindowInfo, str] | None = None
        for info in windows:
            if info.active:
                try:
                    value = self.window_text(info.id)
                    ref = self._refs.get(info.id)
                    if ref is not None and self._find_editable(ref.window) is not None and info.app != "gnome-shell":
                        return info, value
                    if info.app == "gnome-shell":
                        shell_fallback = shell_fallback or (info, value)
                    else:
                        fallback = fallback or (info, value)
                except Exception:
                    continue
        return fallback or shell_fallback

    def tree(self, window_id: str, max_depth: int = 6) -> dict[str, Any]:
        ref = self._ref(window_id)

        def build(node: Any, depth: int) -> dict[str, Any]:
            try:
                interfaces = list(node.get_interfaces() or [])
            except Exception:
                interfaces = []
            result: dict[str, Any] = {
                "name": str(node.get_name() or ""),
                "role": str(node.get_role_name() or ""),
                "interfaces": interfaces,
                "text": _text(node, self._atspi, limit=800),
                "actions": _actions(node),
            }
            if depth < max_depth:
                children = []
                try:
                    for index in range(int(node.get_child_count())):
                        child = node.get_child_at_index(index)
                        if child is not None:
                            children.append(build(child, depth + 1))
                except Exception:
                    pass
                result["children"] = children
            return result

        return build(ref.window, 0)

    def sensitive_focused(self) -> WindowInfo | None:
        from ..safety import is_sensitive_window

        windows = self.list_windows()
        for info in windows:
            if not info.active:
                continue
            if is_sensitive_window(info):
                return info
            ref = self._refs.get(info.id)
            if ref is not None and self._tree_has_password(ref.window):
                return info
        return None

    def _tree_has_password(self, node: Any) -> bool:
        try:
            if str(node.get_role_name() or "").casefold() == "password text":
                return True
            return any(
                child is not None and self._tree_has_password(child)
                for child in (node.get_child_at_index(index) for index in range(int(node.get_child_count())))
            )
        except Exception:
            return False
