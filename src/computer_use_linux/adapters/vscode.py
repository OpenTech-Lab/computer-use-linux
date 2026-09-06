"""VSCode adapter using the ``code`` CLI, AT-SPI, and an optional command extension."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from ..errors import BackendUnavailable
from ..windows.atspi import AtspiWindowSource, atspi_probe
from . import ActionSpec, AdapterBase, register


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


@register
class VscodeAdapter(AdapterBase):
    """Control VSCode through stable CLI and accessibility APIs."""

    name = "vscode"
    needs_session = True

    def __init__(self, *, session: Any | None = None, config: Any | None = None):
        super().__init__(session=session, config=config)
        self._state_file = self.config.state_dir / "vscode.json"
        self._extension = Path(__file__).resolve().parents[3] / "extras" / "vscode-extension"

    def _binary(self, requested: str | None = None) -> str:
        candidate = requested or os.environ.get("CUL_VSCODE_BINARY") or "code"
        resolved = shutil.which(candidate) or candidate
        if not Path(resolved).is_file() or not os.access(resolved, os.X_OK):
            raise BackendUnavailable(f"VSCode CLI is unavailable: {candidate}")
        return str(Path(resolved).resolve())

    def _source(self) -> AtspiWindowSource:
        if self.session is not None:
            return self.session._get_window_source(required=True)
        return AtspiWindowSource()

    def _windows(self) -> list[Any]:
        source = self._source()
        return [window for window in source.list_windows() if window.app.casefold() in {"code", "visual studio code"} or window.app.casefold().startswith("code")]

    def _socket_path(self) -> Path:
        configured = os.environ.get("CUL_VSCODE_SOCKET")
        if configured:
            return Path(configured).expanduser()
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        return Path(runtime).expanduser() / "cul-vscode.sock" if runtime else self.config.state_dir / "vscode.sock"

    def _bridge_request(self, action: str, **payload: Any) -> dict[str, Any]:
        path = self._socket_path()
        if not path.exists():
            raise BackendUnavailable(f"VSCode bridge socket is not available: {path}")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(8.0)
                connection.connect(str(path))
                connection.sendall((json.dumps({"action": action, **payload}, separators=(",", ":")) + "\n").encode("utf-8"))
                data = b""
                while not data.endswith(b"\n"):
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    data += chunk
        except OSError as exc:
            raise BackendUnavailable(f"VSCode bridge connection failed: {exc}") from exc
        try:
            result = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackendUnavailable("VSCode bridge returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise BackendUnavailable("VSCode bridge returned a non-object response")
        if not result.get("ok", False):
            raise BackendUnavailable(str(result.get("error") or "VSCode bridge action failed"))
        return result

    def detect(self) -> bool:
        try:
            if self._bridge_request("status").get("ok"):
                return True
        except BackendUnavailable:
            pass
        try:
            return bool(self._windows())
        except Exception:
            available, _count, _detail = atspi_probe()
            return available and bool(shutil.which(os.environ.get("CUL_VSCODE_BINARY", "code")))

    def actions(self) -> list[ActionSpec]:
        return [
            ActionSpec(
                "launch",
                "Launch or focus VSCode through its CLI.",
                parameters={
                    "path": {"type": "string", "default": "", "description": "Optional file or folder to open."},
                    "new_window": {"type": "boolean", "default": False, "description": "Open a new VSCode window."},
                    "with_bridge": {"type": "boolean", "default": False, "description": "Load the optional command bridge in extension development mode."},
                },
            ),
            ActionSpec("status", "Return VSCode CLI, AT-SPI and optional bridge status."),
            ActionSpec(
                "open",
                "Open a file or folder through the VSCode CLI.",
                parameters={"path": {"type": "string", "required": True, "description": "File or folder path."}},
            ),
            ActionSpec(
                "goto",
                "Open a file at a line and column through the VSCode CLI.",
                parameters={"target": {"type": "string", "required": True, "description": "VSCode target file:line:column."}},
            ),
            ActionSpec(
                "diff",
                "Open a two-file diff through the VSCode CLI.",
                parameters={
                    "left": {"type": "string", "required": True, "description": "Left file."},
                    "right": {"type": "string", "required": True, "description": "Right file."},
                },
            ),
            ActionSpec(
                "command",
                "Execute a VSCode command through the optional extension or a safe known key chord.",
                parameters={
                    "id": {"type": "string", "required": True, "description": "VSCode command id."},
                    "args": {"type": "array", "default": [], "description": "Optional JSON command arguments; extension bridge only."},
                },
            ),
            ActionSpec("windows", "List VSCode windows discovered through AT-SPI."),
            ActionSpec(
                "tree",
                "Return a VSCode AT-SPI tree including semantic actions.",
                parameters={
                    "window_id": {"type": "string", "required": True, "description": "AT-SPI window id."},
                    "max_depth": {"type": "integer", "default": 6, "description": "Maximum child depth."},
                },
            ),
            ActionSpec(
                "read_text",
                "Read text from a VSCode window through AT-SPI.",
                parameters={"window_id": {"type": "string", "required": True, "description": "AT-SPI window id."}},
            ),
            ActionSpec(
                "actions",
                "List VSCode AT-SPI actions without using coordinates.",
                parameters={
                    "window_id": {"type": "string", "required": True, "description": "AT-SPI window id."},
                    "max_depth": {"type": "integer", "default": 6, "description": "Maximum child depth."},
                },
            ),
            ActionSpec(
                "invoke_action",
                "Invoke a VSCode AT-SPI action by name and child path.",
                parameters={
                    "window_id": {"type": "string", "required": True, "description": "AT-SPI window id."},
                    "name": {"type": "string", "required": True, "description": "AT-SPI action name or index."},
                    "path": {"type": "array", "default": [], "description": "Child-index path."},
                },
            ),
        ]

    @staticmethod
    def _run(command: list[str], *, timeout: float = 20.0) -> dict[str, Any]:
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise BackendUnavailable(f"VSCode CLI timed out after {timeout}s") from exc
        return {"ok": completed.returncode == 0, "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr, "command": command}

    def launch(self, **kwargs: Any) -> dict[str, Any]:
        binary = self._binary()
        command = [binary, "--new-window" if _bool(kwargs.get("new_window", False)) else "--reuse-window"]
        path = str(kwargs.get("path") or "")
        if path:
            command.append(str(Path(path).expanduser()))
        env = os.environ.copy()
        with_bridge = _bool(kwargs.get("with_bridge", False))
        if with_bridge:
            socket_path = self._socket_path()
            env["CUL_VSCODE_SOCKET"] = str(socket_path)
            if self._extension.is_dir():
                command.append(f"--extensionDevelopmentPath={self._extension}")
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps({"pid": process.pid, "command": command, "bridge": with_bridge, "socket": str(self._socket_path())}, sort_keys=True), encoding="utf-8")
        time.sleep(0.5)
        return {"ok": True, "pid": process.pid, "command": command, "bridge": self._socket_path().exists() if with_bridge else False}

    def _focus_one(self) -> str:
        windows = self._windows()
        if not windows:
            raise BackendUnavailable("no VSCode window is exposed through AT-SPI; run `cul app vscode launch`")
        selected = next((window for window in windows if window.active), windows[0])
        if self.session is not None:
            with contextlib.suppress(Exception):
                self.session.focus_window(selected.id)
        return selected.id

    def _invoke(self, action: str, **kwargs: Any) -> Any:
        if action == "launch":
            return self.launch(**kwargs)
        if action == "status":
            cli = shutil.which(os.environ.get("CUL_VSCODE_BINARY", "code"))
            bridge = False
            with contextlib.suppress(BackendUnavailable):
                bridge = self._bridge_request("status").get("ok", False)
            windows = []
            with contextlib.suppress(Exception):
                windows = [window.to_dict() for window in self._windows()]
            return {"cli": cli, "bridge": bridge, "socket": str(self._socket_path()), "windows": windows}
        if action == "open":
            return self._run([self._binary(), "--reuse-window", str(Path(str(kwargs["path"])).expanduser())])
        if action == "goto":
            return self._run([self._binary(), "--reuse-window", "--goto", str(kwargs["target"])])
        if action == "diff":
            return self._run([self._binary(), "--reuse-window", "--diff", str(Path(str(kwargs["left"])).expanduser()), str(Path(str(kwargs["right"])).expanduser())])
        if action == "command":
            command_id = str(kwargs["id"])
            with contextlib.suppress(BackendUnavailable):
                return self._bridge_request("command", id=command_id, args=kwargs.get("args") or [])
            known_chords = {
                "workbench.action.showCommands": "ctrl+shift+p",
                "workbench.action.quickOpen": "ctrl+p",
                "workbench.action.files.openFile": "ctrl+o",
                "workbench.action.closeActiveEditor": "ctrl+w",
                "workbench.action.togglePanel": "ctrl+j",
            }
            chord = known_chords.get(command_id)
            if chord is None:
                raise BackendUnavailable(
                    f"VSCode command {command_id!r} needs the companion extension; launch with --with-bridge "
                    f"or install {self._extension}"
                )
            self._focus_one()
            if self.session is None:
                raise BackendUnavailable("VSCode keyboard fallback requires the shared desktop session")
            result = self.session.key(chord)
            return {"ok": True, "command": command_id, "mechanism": "keyboard-fallback", "chord": chord, "input": result}
        source = self._source()
        if action == "windows":
            return {"windows": [window.to_dict() for window in self._windows()], "position_available": False}
        if action == "tree":
            return source.tree(str(kwargs["window_id"]), int(kwargs.get("max_depth", 6)))
        if action == "read_text":
            window_id = str(kwargs["window_id"])
            return {"window_id": window_id, "text": source.window_text(window_id)}
        if action == "actions":
            return source.actions(str(kwargs["window_id"]), int(kwargs.get("max_depth", 6)))
        if action == "invoke_action":
            raw_name = kwargs["name"]
            try:
                action_name: str | int = int(raw_name)
            except (TypeError, ValueError):
                action_name = str(raw_name)
            return source.invoke_action(str(kwargs["window_id"]), action_name, path=kwargs.get("path") or [])
        raise BackendUnavailable(f"unsupported VSCode adapter action: {action}")

