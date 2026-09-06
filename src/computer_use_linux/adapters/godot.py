"""Godot 4 adapter with a headless script path and optional editor-plugin bridge."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from ..errors import BackendUnavailable
from . import ActionSpec, AdapterBase, register
from ._process import start_ticks


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _record(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


@register
class GodotAdapter(AdapterBase):
    """Run Godot scripts headlessly or talk to a project editor plugin."""

    name = "godot"
    needs_session = False

    def __init__(self, *, session: Any | None = None, config: Any | None = None):
        super().__init__(session=session, config=config)
        self._state_file = self.config.state_dir / "godot.json"
        self._plugin = Path(__file__).resolve().parents[3] / "extras" / "godot-plugin" / "addons" / "cul_bridge"

    def _binary(self, requested: str | None = None) -> str:
        candidate = requested or os.environ.get("CUL_GODOT_BINARY")
        if candidate:
            path = shutil.which(candidate) or candidate
            if Path(path).is_file() and os.access(path, os.X_OK):
                return str(Path(path).resolve())
            raise BackendUnavailable(f"Godot executable is unavailable: {candidate}")
        preferred = Path.home() / ".local" / "bin" / "godot"
        if preferred.is_file() and os.access(preferred, os.X_OK):
            return str(preferred)
        path = shutil.which("godot")
        if path:
            return path
        raise BackendUnavailable("Godot executable not found; set CUL_GODOT_BINARY")

    def _managed(self, record: dict[str, Any] | None = None) -> bool:
        record = record or _record(self._state_file)
        if not record:
            return False
        try:
            pid = int(record["pid"])
            int(record["port"])
            os.kill(pid, 0)
        except (KeyError, TypeError, ValueError, OSError):
            return False
        expected_start = record.get("start_ticks")
        if expected_start is not None and start_ticks(pid) != int(expected_start):
            return False
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            return False
        project = str(record.get("project") or "")
        return "--editor" in command and bool(project) and project in command

    def _request(self, action: str, **payload: Any) -> dict[str, Any]:
        record = _record(self._state_file)
        if not record or not self._managed(record):
            raise BackendUnavailable("no managed Godot editor bridge is running; use headless mode or `cul app godot launch`")
        try:
            with socket.create_connection(("127.0.0.1", int(record["port"])), timeout=8.0) as connection:
                connection.settimeout(8.0)
                connection.sendall((json.dumps({"action": action, **payload}, separators=(",", ":")) + "\n").encode("utf-8"))
                data = b""
                while not data.endswith(b"\n"):
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > 4 * 1024 * 1024:
                        raise BackendUnavailable("Godot bridge response is too large")
        except OSError as exc:
            raise BackendUnavailable(f"Godot bridge connection failed: {exc}") from exc
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackendUnavailable("Godot bridge returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise BackendUnavailable("Godot bridge returned a non-object response")
        if not value.get("ok", False):
            raise BackendUnavailable(str(value.get("error") or "Godot bridge action failed"))
        return value

    def detect(self) -> bool:
        try:
            self._request("status")
            return True
        except BackendUnavailable:
            return False

    def actions(self) -> list[ActionSpec]:
        return [
            ActionSpec(
                "launch",
                "Launch a Godot editor with the optional CUL bridge plugin.",
                parameters={
                    "project": {"type": "string", "required": True, "description": "Godot project directory."},
                    "godot": {"type": "string", "default": "", "description": "Godot executable override."},
                    "port": {"type": "integer", "default": 9877, "description": "Loopback editor bridge port."},
                    "headless": {"type": "boolean", "default": False, "description": "Launch the editor without a visible window."},
                },
            ),
            ActionSpec("status", "Return status for the managed Godot editor bridge."),
            ActionSpec("stop", "Stop the Godot process owned by this adapter."),
            ActionSpec(
                "run_scene",
                "Run a Godot scene headlessly or through the live editor bridge.",
                parameters={
                    "project": {"type": "string", "required": True, "description": "Godot project directory."},
                    "scene": {"type": "string", "default": "", "description": "Optional project-relative scene path."},
                    "timeout": {"type": "number", "default": 20.0, "description": "Maximum seconds."},
                },
            ),
            ActionSpec(
                "eval_gdscript",
                "Evaluate GDScript in a headless SceneTree or live editor. This action is confirmation-gated.",
                aliases=("eval",),
                parameters={
                    "expr": {"type": "string", "required": True, "description": "GDScript expression or statements."},
                    "project": {"type": "string", "default": "", "description": "Optional Godot project directory."},
                    "timeout": {"type": "number", "default": 20.0, "description": "Maximum seconds."},
                    "headless": {"type": "boolean", "default": False, "description": "Force a one-shot headless script."},
                },
            ),
            ActionSpec(
                "editor_command",
                "Invoke a small named command in the live Godot editor bridge.",
                parameters={
                    "id": {"type": "string", "required": True, "description": "Command id, such as play_main_scene or stop_playing_scene."},
                },
            ),
        ]

    def launch(self, **kwargs: Any) -> dict[str, Any]:
        if self.detect():
            record = _record(self._state_file) or {}
            return {"ok": True, "already_running": True, "pid": record.get("pid"), "port": record.get("port"), "project": record.get("project")}
        project = Path(str(kwargs.get("project") or "")).expanduser().resolve()
        if not project.is_dir():
            raise BackendUnavailable(f"Godot project directory does not exist: {project}")
        binary = self._binary(str(kwargs.get("godot") or "") or None)
        port = int(kwargs.get("port", os.environ.get("CUL_GODOT_PORT", 9877)))
        if not self._plugin.is_dir():
            raise BackendUnavailable(f"Godot bridge plugin is missing: {self._plugin}")
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        command = [binary]
        if _bool(kwargs.get("headless", False)):
            command.append("--headless")
        command.extend(["--editor", "--path", str(project)])
        env = os.environ.copy()
        env["CUL_GODOT_PORT"] = str(port)
        log_path = self.config.state_dir / "godot.log"
        log_handle = log_path.open("ab")
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        finally:
            log_handle.close()
        record = {"pid": process.pid, "port": port, "project": str(project), "binary": binary, "plugin": str(self._plugin), "start_ticks": start_ticks(process.pid)}
        self._state_file.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        # The plugin is project-local and must be enabled in project.godot.  A launch that does
        # not expose a socket is still useful as a normal editor launch, so return its state
        # instead of misreporting a ready bridge.
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise BackendUnavailable(f"Godot exited during launch; see {log_path}")
            if self.detect():
                return {"ok": True, **record, "bridge": "ready"}
            time.sleep(0.1)
        return {"ok": True, **record, "bridge": "not detected", "warning": "enable addons/cul_bridge in the Godot project for live editor actions"}

    @staticmethod
    def _script_body(expr: str) -> str:
        stripped = expr.strip()
        if stripped.startswith(("print(", "push_error(", "push_warning(")) or "\n" in expr or ";" in expr:
            body = expr
        else:
            body = f"print({expr})"
        return "extends SceneTree\n\nfunc _init():\n    " + body.replace("\n", "\n    ") + "\n    quit()\n"

    def _headless_eval(self, expression: str, *, project: str = "", timeout: float = 20.0) -> dict[str, Any]:
        binary = self._binary()
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        script = self.config.state_dir / f"godot-eval-{os.getpid()}-{time.time_ns()}.gd"
        script.write_text(self._script_body(expression), encoding="utf-8")
        command = [binary, "--headless"]
        if project:
            command.extend(["--path", str(Path(project).expanduser().resolve())])
        command.extend(["--script", str(script)])
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=float(timeout),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendUnavailable(f"Godot headless evaluation timed out after {timeout}s") from exc
        finally:
            with contextlib.suppress(OSError):
                script.unlink()
        return {"ok": completed.returncode == 0, "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}

    def _headless_scene(self, project: str, scene: str, timeout: float) -> dict[str, Any]:
        binary = self._binary()
        project_path = Path(project).expanduser().resolve()
        if not project_path.is_dir():
            raise BackendUnavailable(f"Godot project directory does not exist: {project_path}")
        command = [binary, "--headless", "--path", str(project_path)]
        if scene:
            command.extend([str(scene), "--quit-after", "1"])
        else:
            command.extend(["--editor", "--quit-after", "1"])
        try:
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=float(timeout), check=False)
        except subprocess.TimeoutExpired as exc:
            raise BackendUnavailable(f"Godot scene run timed out after {timeout}s") from exc
        return {"ok": completed.returncode == 0, "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}

    def _stop(self) -> dict[str, Any]:
        record = _record(self._state_file)
        if not record or not self._managed(record):
            if record:
                with contextlib.suppress(OSError):
                    self._state_file.unlink()
            return {"ok": True, "running": False}
        pid = int(record["pid"])
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.05)
        else:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            self._state_file.unlink()
        return {"ok": True, "running": False, "pid": pid}

    def _invoke(self, action: str, **kwargs: Any) -> Any:
        if action == "launch":
            return self.launch(**kwargs)
        if action == "status":
            record = _record(self._state_file)
            return {
                "running": self.detect(),
                "managed": bool(record and self._managed(record)),
                "pid": record.get("pid") if record else None,
                "port": record.get("port") if record else None,
                "project": record.get("project") if record else None,
            }
        if action == "stop":
            return self._stop()
        if action == "eval_gdscript":
            expression = str(kwargs["expr"])
            if not _bool(kwargs.get("headless", False)) and self.detect():
                return self._request("eval_gdscript", expr=expression)
            return self._headless_eval(expression, project=str(kwargs.get("project") or ""), timeout=float(kwargs.get("timeout", 20.0)))
        if action == "run_scene":
            if self.detect():
                return self._request("run_scene", scene=str(kwargs.get("scene") or ""))
            return self._headless_scene(str(kwargs["project"]), str(kwargs.get("scene") or ""), float(kwargs.get("timeout", 20.0)))
        if action == "editor_command":
            if not self.detect():
                raise BackendUnavailable("editor_command requires a running Godot bridge; use `cul app godot launch --project ...`")
            return self._request("editor_command", id=str(kwargs["id"]))
        raise BackendUnavailable(f"unsupported Godot adapter action: {action}")
