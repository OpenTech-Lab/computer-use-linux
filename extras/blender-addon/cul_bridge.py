"""Loopback Blender bridge loaded with ``blender --python cul_bridge.py``.

The socket listener never touches bpy.  Requests are queued and drained by a bpy timer on
Blender's main thread, which is the only safe place for most Blender API operations.
"""

from __future__ import annotations

import atexit
import contextlib
import hmac
import io
import json
import os
import queue
import secrets
import socket
import stat
import struct
import threading
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import bpy  # type: ignore

_REQUESTS: queue.Queue[tuple[dict[str, Any], queue.Queue[dict[str, Any]]]] = queue.Queue()
_STOP = threading.Event()
_SERVER: socket.socket | None = None
_SOCKET_PATH: Path | None = None
_STATE_FILE: Path | None = None
_TOKEN: str | None = None


def _safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    return str(value)


def _scene_info() -> dict[str, Any]:
    return {
        "scene": bpy.context.scene.name if bpy.context.scene else None,
        "objects": [
            {
                "name": obj.name,
                "type": obj.type,
                "location": [float(value) for value in obj.location],
            }
            for obj in bpy.context.scene.objects
        ],
    }


def _ensure_private_directory(path: Path) -> Path:
    path = path.expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RuntimeError(f"Blender bridge directory is not owner-only: {path}")
    return path


def _socket_path() -> Path:
    configured = os.environ.get("CUL_BLENDER_SOCKET")
    if configured:
        path = Path(configured).expanduser()
    else:
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        base = Path(runtime).expanduser() if runtime else Path.home() / ".local" / "state" / "computer-use-linux"
        path = base / f"cul-blender-{os.getpid()}.sock"
    _ensure_private_directory(path.parent)
    return path


def _state_file() -> Path:
    configured = os.environ.get("CUL_BLENDER_STATE_FILE")
    if configured:
        return Path(configured).expanduser()
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")).expanduser()
    return state_home / "computer-use-linux" / "blender.json"


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_state(path: Path, values: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))
    descriptor = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RuntimeError("Blender bridge state file is not owner-only")


def _remove_stale_socket(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(metadata.st_mode):
        raise RuntimeError(f"Blender bridge path is not a socket: {path}")
    path.unlink()


def _peer_uid_matches(connection: socket.socket) -> bool:
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
    except (AttributeError, OSError, struct.error):
        return False
    return uid == os.geteuid()


def _authorized(request: dict[str, Any]) -> bool:
    supplied = request.get("token")
    if not isinstance(supplied, str):
        supplied = ""
    expected = _TOKEN or ""
    return hmac.compare_digest(supplied, expected)


def _handle(request: dict[str, Any]) -> dict[str, Any]:
    action = str(request.get("action", ""))
    if action == "status":
        return {"ok": True, "version": bpy.app.version_string, "pid": os.getpid()}
    if action == "scene_info":
        return {"ok": True, **_scene_info()}
    if action == "run_python":
        expression = str(request.get("expr", ""))
        output = io.StringIO()
        namespace: dict[str, Any] = {"bpy": bpy, "__name__": "__cul_blender__"}
        try:
            with redirect_stdout(output):
                exec(expression, namespace, namespace)
            return {"ok": True, "stdout": output.getvalue(), "result": _safe(namespace.get("_cul_result"))}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "stdout": output.getvalue(), "traceback": traceback.format_exc()}
    if action == "viewport_screenshot":
        path = Path(str(request.get("path") or (Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "computer-use-linux" / "blender-render.png"))).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            bpy.context.scene.render.filepath = str(path)
            bpy.ops.render.render(write_still=True)
            return {"ok": True, "path": str(path)}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
    return {"ok": False, "error": f"unknown bridge action: {action}"}


def _listener() -> None:
    server = _SERVER
    if server is None:
        return
    while not _STOP.is_set():
        try:
            connection, _address = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        with connection:
            if not _peer_uid_matches(connection):
                continue
            connection.settimeout(10.0)
            file = connection.makefile("rb")
            try:
                for line in file:
                    try:
                        request = json.loads(line.decode("utf-8"))
                        if not isinstance(request, dict):
                            break
                    except (UnicodeDecodeError, ValueError, TypeError):
                        break
                    if not _authorized(request):
                        break
                    request.pop("token", None)
                    responses: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
                    _REQUESTS.put((request, responses))
                    try:
                        response = responses.get(timeout=30.0)
                    except queue.Empty:
                        response = {"ok": False, "error": "Blender main thread did not answer in 30 seconds"}
                    connection.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
            finally:
                file.close()


def _pump() -> float:
    while True:
        try:
            request, responses = _REQUESTS.get_nowait()
        except queue.Empty:
            break
        try:
            responses.put_nowait(_handle(request))
        except Exception as exc:
            responses.put_nowait({"ok": False, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()})
    return 0.05


def _shutdown() -> None:
    _STOP.set()
    if _SERVER is not None:
        with contextlib.suppress(OSError):
            _SERVER.close()
    if _SOCKET_PATH is not None:
        with contextlib.suppress(OSError):
            _SOCKET_PATH.unlink()
    if _STATE_FILE is not None:
        state = _read_state(_STATE_FILE)
        if state.get("pid") == os.getpid():
            with contextlib.suppress(OSError):
                _STATE_FILE.unlink()


def _start() -> None:
    global _SERVER, _SOCKET_PATH, _STATE_FILE, _TOKEN
    _SOCKET_PATH = _socket_path()
    _STATE_FILE = _state_file()
    _TOKEN = secrets.token_urlsafe(32)
    _remove_stale_socket(_SOCKET_PATH)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(_SOCKET_PATH))
        os.chmod(_SOCKET_PATH, 0o600)
        server.listen(8)
    except Exception:
        with contextlib.suppress(OSError):
            server.close()
        with contextlib.suppress(OSError):
            _SOCKET_PATH.unlink()
        raise
    server.settimeout(0.5)
    _SERVER = server
    state = _read_state(_STATE_FILE)
    state.update({"pid": os.getpid(), "socket": str(_SOCKET_PATH), "token": _TOKEN})
    _write_state(_STATE_FILE, state)
    atexit.register(_shutdown)
    thread = threading.Thread(target=_listener, name="cul-blender-bridge", daemon=True)
    thread.start()
    bpy.app.timers.register(_pump, first_interval=0.05, persistent=True)


_start()
