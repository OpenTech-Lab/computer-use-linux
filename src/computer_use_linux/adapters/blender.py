"""Blender 5 adapter using ``--python-expr`` or a loopback main-thread bridge."""

from __future__ import annotations

import base64
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
from ._bridge_security import ensure_private_directory, public_record, read_json, write_private_json
from ._process import start_ticks


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _read_record(path: Path) -> dict[str, Any] | None:
    return read_json(path)


@register
class BlenderAdapter(AdapterBase):
    """Control Blender without relying on viewport coordinates."""

    name = "blender"
    needs_session = False

    def __init__(self, *, session: Any | None = None, config: Any | None = None):
        super().__init__(session=session, config=config)
        self._state_file = self.config.state_dir / "blender.json"
        self._bridge = Path(__file__).resolve().parents[3] / "extras" / "blender-addon" / "cul_bridge.py"

    def _binary(self, requested: str | None = None) -> str:
        candidate = requested or os.environ.get("CUL_BLENDER_BINARY") or "/opt/blender/blender"
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
        # An explicit command may intentionally be a PATH name; the configured/default path above
        # remains absolute and is the only implicit location we promise.
        if requested or os.environ.get("CUL_BLENDER_BINARY"):
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        raise BackendUnavailable(
            f"Blender executable is unavailable at {candidate}; set CUL_BLENDER_BINARY or pass --blender "
            "with an absolute executable path"
        )

    def _record(self) -> dict[str, Any] | None:
        return _read_record(self._state_file)

    @staticmethod
    def _socket_path(identifier: str) -> Path:
        configured = os.environ.get("CUL_BLENDER_SOCKET")
        if configured:
            path = Path(configured).expanduser()
            ensure_private_directory(path.parent)
            return path
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        base = Path(runtime).expanduser() if runtime else Path.home() / ".local" / "state" / "computer-use-linux"
        ensure_private_directory(base)
        return base / f"cul-blender-{identifier}.sock"

    def _write_record(self, record: dict[str, Any]) -> dict[str, Any]:
        current = self._record() or {}
        merged = {**record}
        token = current.get("token")
        if isinstance(token, str) and token:
            merged["token"] = token
        write_private_json(self._state_file, merged)
        return merged

    @staticmethod
    def _cleanup_socket(record: dict[str, Any] | None) -> None:
        if not record:
            return
        socket_name = record.get("socket")
        if socket_name:
            with contextlib.suppress(OSError):
                Path(str(socket_name)).unlink()

    def _managed_process(self, record: dict[str, Any] | None = None) -> bool:
        record = record or self._record()
        if not record:
            return False
        try:
            pid = int(record["pid"])
            socket_name = str(record["socket"])
            os.kill(pid, 0)
            if not socket_name:
                return False
        except (KeyError, TypeError, ValueError, OSError):
            return False
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            return False
        expected_start = record.get("start_ticks")
        if expected_start is not None and start_ticks(pid) != int(expected_start):
            return False
        return str(self._bridge) in command and socket_name == str(record.get("socket", ""))

    def _managed(self, record: dict[str, Any] | None = None) -> bool:
        record = record or self._record()
        if not self._managed_process(record):
            return False
        token = record.get("token") if record else None
        return isinstance(token, str) and bool(token)

    def _request(self, action: str, *, timeout: float = 8.0, **payload: Any) -> dict[str, Any]:
        record = self._record()
        if not record or not self._managed(record):
            raise BackendUnavailable("no managed Blender bridge is running; run `cul app blender launch` first")
        token = record.get("token")
        if not isinstance(token, str) or not token:
            raise BackendUnavailable("Blender bridge authentication state is unavailable")
        socket_name = str(record["socket"])
        request = {"action": action, **payload, "token": token}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout)
                connection.connect(socket_name)
                connection.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
                line = b""
                while not line.endswith(b"\n"):
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    line += chunk
                    if len(line) > 4 * 1024 * 1024:
                        raise BackendUnavailable("Blender bridge response is too large")
        except OSError as exc:
            raise BackendUnavailable(f"Blender bridge connection failed: {exc}") from exc
        if not line:
            raise BackendUnavailable("Blender bridge closed the connection")
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackendUnavailable("Blender bridge returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise BackendUnavailable("Blender bridge returned a non-object response")
        if not value.get("ok", False):
            raise BackendUnavailable(str(value.get("error") or "Blender bridge action failed"))
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
                "Launch Blender with the computer-use bridge addon.",
                parameters={
                    "blender": {"type": "string", "default": "", "description": "Absolute Blender executable override."},
                    "port": {"type": "integer", "default": 9876, "description": "Deprecated compatibility option; the bridge uses a private Unix socket."},
                    "background": {"type": "boolean", "default": False, "description": "Launch without a visible Blender window."},
                    "file": {"type": "string", "default": "", "description": "Optional .blend file to open."},
                },
            ),
            ActionSpec("status", "Return status for the managed Blender bridge."),
            ActionSpec("stop", "Stop the Blender process owned by this adapter."),
            ActionSpec(
                "run_python",
                "Execute Python in Blender's main thread. This action is confirmation-gated.",
                parameters={
                    "expr": {"type": "string", "required": True, "description": "Python statements to execute with bpy available."},
                    "blender": {"type": "string", "default": "", "description": "Absolute Blender executable override for headless mode."},
                    "timeout": {"type": "number", "default": 20.0, "description": "Headless execution timeout in seconds."},
                    "headless": {"type": "boolean", "default": False, "description": "Force a one-shot headless process instead of the bridge."},
                },
            ),
            ActionSpec("scene_info", "Return Blender scene and object information through bpy."),
            ActionSpec(
                "viewport_screenshot",
                "Render the current Blender scene to a PNG through bpy; no viewport click is used.",
                parameters={
                    "output": {"type": "string", "default": "", "description": "Optional PNG output path."},
                    "blender": {"type": "string", "default": "", "description": "Absolute Blender executable override for headless mode."},
                    "timeout": {"type": "number", "default": 60.0, "description": "Maximum render time in seconds."},
                },
            ),
        ]

    def launch(self, **kwargs: Any) -> dict[str, Any]:
        if self.detect():
            record = self._record() or {}
            return {"ok": True, "already_running": True, **public_record(record)}
        if not self._bridge.is_file():
            raise BackendUnavailable(f"Blender bridge addon is missing: {self._bridge}")
        binary = self._binary(str(kwargs.get("blender") or "") or None)
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        stale_record = self._record()
        self._cleanup_socket(stale_record)
        with contextlib.suppress(OSError):
            self._state_file.unlink()
        socket_name = self._socket_path(f"{os.getpid()}-{time.time_ns()}")
        command = [binary, "--factory-startup"]
        if _bool(kwargs.get("background", False)):
            command.append("--background")
        file = str(kwargs.get("file") or "")
        if file:
            command.append(str(Path(file).expanduser()))
        command.extend(["--python", str(self._bridge)])
        env = os.environ.copy()
        env["CUL_BLENDER_SOCKET"] = str(socket_name)
        env["CUL_BLENDER_STATE_FILE"] = str(self._state_file)
        log_path = self.config.state_dir / "blender.log"
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
        record = {"pid": process.pid, "socket": str(socket_name), "binary": binary, "bridge": str(self._bridge), "start_ticks": start_ticks(process.pid)}
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._cleanup_socket(record)
                raise BackendUnavailable(f"Blender exited during bridge launch; see {log_path}")
            current = self._record()
            token = current.get("token") if current else None
            if isinstance(token, str) and token:
                merged = {**record, "token": token}
                self._write_record(merged)
                try:
                    self._request("status")
                    return {"ok": True, **public_record(self._record() or merged), "bridge": "ready"}
                except BackendUnavailable:
                    pass
            time.sleep(0.1)
        self._cleanup_socket(record)
        with contextlib.suppress(OSError):
            self._state_file.unlink()
        raise BackendUnavailable(f"timed out waiting for Blender bridge; see {log_path}")

    def _run_headless(self, expression: str, *, timeout: float = 20.0, binary: str | None = None) -> dict[str, Any]:
        executable = self._binary(binary)
        try:
            completed = subprocess.run(
                [executable, "--background", "--factory-startup", "--python-expr", expression],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=float(timeout),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendUnavailable(f"Blender headless action timed out after {timeout}s") from exc
        return {"ok": completed.returncode == 0, "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}

    @staticmethod
    def _png_result(path: Path, *, output: str = "") -> dict[str, Any]:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise BackendUnavailable(f"Blender did not produce a render at {path}: {exc}") from exc
        if output:
            destination = Path(output).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            return {"saved": str(destination), "bytes": len(data), "mime_type": "image/png"}
        return {"png_base64": base64.b64encode(data).decode("ascii"), "bytes": len(data), "mime_type": "image/png"}

    def _invoke(self, action: str, **kwargs: Any) -> Any:
        if action == "launch":
            return self.launch(**kwargs)
        if action == "status":
            record = self._record()
            return {
                "running": self.detect(),
                "managed": bool(record and self._managed(record)),
                "pid": record.get("pid") if record else None,
                "socket": record.get("socket") if record else None,
                "binary": record.get("binary") if record else None,
            }
        if action == "stop":
            record = self._record()
            if not record or not self._managed_process(record):
                if record:
                    self._cleanup_socket(record)
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
            self._cleanup_socket(record)
            with contextlib.suppress(OSError):
                self._state_file.unlink()
            return {"ok": True, "running": False, "pid": pid}
        if action == "run_python":
            expression = str(kwargs["expr"])
            if not _bool(kwargs.get("headless", False)) and self.detect():
                return self._request("run_python", expr=expression)
            return self._run_headless(
                expression,
                timeout=float(kwargs.get("timeout", 20.0)),
                binary=str(kwargs.get("blender") or "") or None,
            )
        if action == "scene_info":
            if self.detect():
                return self._request("scene_info")
            expression = (
                "import bpy, json; print(json.dumps({'objects':[{'name':o.name,'type':o.type} "
                "for o in bpy.context.scene.objects], 'scene': bpy.context.scene.name}))"
            )
            result = self._run_headless(expression)
            return result
        if action == "viewport_screenshot":
            output = str(kwargs.get("output") or "")
            timeout = float(kwargs.get("timeout", 60.0))
            path = Path(output).expanduser() if output else self.config.state_dir / "blender-render.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            if self.detect():
                result = self._request("viewport_screenshot", timeout=max(8.0, timeout + 5.0), path=str(path))
                if result.get("path"):
                    path = Path(str(result["path"]))
            else:
                expression = (
                    f"import bpy; bpy.context.scene.render.filepath={str(str(path)).__repr__()}; "
                    "bpy.ops.render.render(write_still=True); print(bpy.context.scene.render.filepath)"
                )
                result = self._run_headless(
                    expression,
                    timeout=timeout,
                    binary=str(kwargs.get("blender") or "") or None,
                )
                if not result.get("ok"):
                    return result
            return self._png_result(path, output=output)
        raise BackendUnavailable(f"unsupported Blender adapter action: {action}")
