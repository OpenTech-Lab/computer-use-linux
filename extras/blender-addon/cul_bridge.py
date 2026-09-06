"""Loopback Blender bridge loaded with ``blender --python cul_bridge.py``.

The socket listener never touches bpy.  Requests are queued and drained by a bpy timer on
Blender's main thread, which is the only safe place for most Blender API operations.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import socket
import threading
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import bpy  # type: ignore


_PORT = int(os.environ.get("CUL_BLENDER_PORT", "9876"))
_REQUESTS: queue.Queue[tuple[dict[str, Any], queue.Queue[dict[str, Any]]]] = queue.Queue()
_STOP = threading.Event()
_SERVER: socket.socket | None = None


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
    global _SERVER
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _SERVER = server
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", _PORT))
    server.listen(8)
    server.settimeout(0.5)
    while not _STOP.is_set():
        try:
            connection, _address = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        with connection:
            connection.settimeout(10.0)
            file = connection.makefile("rb")
            try:
                for line in file:
                    try:
                        request = json.loads(line.decode("utf-8"))
                        if not isinstance(request, dict):
                            raise ValueError("request must be an object")
                    except Exception as exc:
                        response = {"ok": False, "error": f"invalid request: {exc}"}
                    else:
                        responses: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
                        _REQUESTS.put((request, responses))
                        try:
                            response = responses.get(timeout=30.0)
                        except queue.Empty:
                            response = {"ok": False, "error": "Blender main thread did not answer in 30 seconds"}
                    connection.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
            finally:
                file.close()
    with contextlib.suppress(OSError):
        server.close()


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


def _start() -> None:
    thread = threading.Thread(target=_listener, name="cul-blender-bridge", daemon=True)
    thread.start()
    bpy.app.timers.register(_pump, first_interval=0.05, persistent=True)


_start()
