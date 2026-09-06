from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from computer_use_linux.adapters._bridge_security import (
    ensure_private_directory,
    public_record,
    write_private_json,
)
from computer_use_linux.adapters.blender import BlenderAdapter
from computer_use_linux.adapters.godot import GodotAdapter
from computer_use_linux.adapters.vscode import VscodeAdapter
from computer_use_linux.config import Config


def _config(tmp_path: Path) -> Config:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    return Config(
        config_file=tmp_path / "config.toml",
        state_dir=state_dir,
        panic_file=tmp_path / "PANIC",
        actions_log=tmp_path / "actions.jsonl",
        confirm_mode="off",
    )


def _one_shot_server(family: socket.AddressFamily, address: Any, captured: list[dict[str, Any]]) -> threading.Thread:
    ready = threading.Event()

    def serve() -> None:
        with socket.socket(family, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(address)
            if family == socket.AF_UNIX:
                os.chmod(address, 0o600)
            server.listen(1)
            ready.set()
            with server.accept()[0] as connection:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                captured.append(json.loads(data.decode("utf-8")))
                connection.sendall(b'{"ok":true,"version":"test"}\n')

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(2)
    return thread


def test_private_state_files_are_created_owner_only_and_public_records_drop_tokens(tmp_path: Path) -> None:
    state_file = tmp_path / "state" / "bridge.json"
    write_private_json(state_file, {"pid": 1, "token": "do-not-expose"})

    metadata = state_file.stat()
    assert metadata.st_uid == os.geteuid()
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert public_record({"pid": 1, "token": "do-not-expose"}) == {"pid": 1}


def test_private_directory_refuses_existing_loose_directory(tmp_path: Path) -> None:
    directory = tmp_path / "loose"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o755)
    with pytest.raises(PermissionError, match="owner-only"):
        ensure_private_directory(directory)


@pytest.mark.parametrize(
    ("adapter_type", "family", "record_key"),
    [
        (BlenderAdapter, socket.AF_UNIX, "socket"),
        (GodotAdapter, socket.AF_INET, "port"),
        (VscodeAdapter, socket.AF_UNIX, "socket"),
    ],
)
def test_adapters_send_the_state_token_on_every_bridge_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_type: type[Any],
    family: socket.AddressFamily,
    record_key: str,
) -> None:
    config = _config(tmp_path)
    adapter = adapter_type(config=config)
    token = "test-token"
    captured: list[dict[str, Any]] = []
    if family == socket.AF_UNIX:
        endpoint = tmp_path / f"{adapter_type.__name__}.sock"
        address: Any = str(endpoint)
        if adapter_type is VscodeAdapter:
            monkeypatch.setenv("CUL_VSCODE_SOCKET", str(endpoint))
    else:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        address = probe.getsockname()
        port = int(address[1])
        probe.close()
    thread = _one_shot_server(family, address, captured)
    record: dict[str, Any] = {"token": token, record_key: address if record_key == "socket" else port, "pid": os.getpid()}
    if adapter_type is BlenderAdapter:
        record.update({"bridge": str(adapter._bridge), "start_ticks": None})
    if adapter_type is GodotAdapter:
        record.update({"project": str(tmp_path)})
    write_private_json(adapter._state_file, record)
    if adapter_type is not VscodeAdapter:
        monkeypatch.setattr(adapter, "_managed", lambda _record=None: True)

    if adapter_type is BlenderAdapter:
        result = adapter._request("status")
    elif adapter_type is GodotAdapter:
        result = adapter._request("status")
    else:
        result = adapter._bridge_request("status")

    thread.join(timeout=2)
    assert result["ok"] is True
    assert captured == [{"action": "status", "token": token}]


def test_blender_bridge_source_has_peer_credentials_socket_auth_and_safe_setup() -> None:
    source = (Path(__file__).resolve().parents[2] / "extras" / "blender-addon" / "cul_bridge.py").read_text(encoding="utf-8")
    assert "socket.AF_UNIX" in source
    assert "secrets.token_urlsafe(32)" in source
    assert "hmac.compare_digest" in source
    assert "socket.SO_PEERCRED" in source
    assert "server.bind(str(_SOCKET_PATH))" in source
    assert source.index("server.bind(str(_SOCKET_PATH))") < source.index("os.chmod(_SOCKET_PATH, 0o600)") < source.index("server.listen(8)")
    assert "request.pop(\"token\", None)" in source


def test_blender_bridge_rejects_missing_and_wrong_tokens_and_accepts_the_right_one(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    socket_path = runtime / "cul-blender-test.sock"
    state_path = tmp_path / "state" / "blender.json"
    script = r'''
import runpy
import sys
import threading
import time
import types

bpy = types.ModuleType("bpy")

def register(callback, *args, **kwargs):
    def pump():
        while True:
            callback()
            time.sleep(0.05)
    threading.Thread(target=pump, daemon=True).start()

bpy.app = types.SimpleNamespace(
    version_string="test",
    timers=types.SimpleNamespace(register=register),
)
sys.modules["bpy"] = bpy
runpy.run_path(sys.argv[1], run_name="__main__")
while True:
    time.sleep(1)
'''
    environment = os.environ.copy()
    environment.update(
        {
            "CUL_BRIDGE": str(Path(__file__).resolve().parents[2] / "extras" / "blender-addon" / "cul_bridge.py"),
            "CUL_BLENDER_SOCKET": str(socket_path),
            "CUL_BLENDER_STATE_FILE": str(state_path),
            "XDG_RUNTIME_DIR": str(runtime),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, environment["CUL_BRIDGE"]],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    def wait_for_state() -> dict[str, Any]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if state.get("token"):
                    return state
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        error = process.stderr.read() if process.poll() is not None and process.stderr else ""
        raise AssertionError(f"Blender bridge state file did not appear: {error}")

    def request(payload: dict[str, Any]) -> bytes:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(3)
                connection.connect(str(socket_path))
                connection.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                return connection.recv(65536)
        except (ConnectionResetError, OSError):
            return b""

    try:
        state = wait_for_state()
        assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
        assert request({"action": "status"}) == b""
        assert request({"action": "status", "token": "wrong-token"}) == b""
        response = json.loads(request({"action": "status", "token": state["token"]}).decode("utf-8"))
        assert response["ok"] is True
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def test_godot_bridge_source_rejects_unauthenticated_requests() -> None:
    root = Path(__file__).resolve().parents[2]
    for relative in (
        "extras/godot-plugin/addons/cul_bridge/bridge.gd",
        "tests/fixtures/godot-adapter-project/addons/cul_bridge/bridge.gd",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert 'server.listen(port, "127.0.0.1")' in source
        assert "generate_random_bytes(32)" in source
        assert "constant_time_compare" in source
        assert 'request.get("token", "")' in source
        assert 'request.erase("token")' in source
        assert "OWNER_ONLY_PERMISSIONS" in source
        assert "_close_peer()" in source


def test_vscode_bridge_accepts_only_the_token_and_sets_private_modes(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    socket_path = runtime / "cul-vscode.sock"
    state_path = tmp_path / "state" / "vscode.json"
    script = r"""
const fs = require('fs');
const Module = require('module');
const originalLoad = Module._load;
let commandCount = 0;
Module._load = function(request, parent, isMain) {
  if (request === 'vscode') {
    return {
      version: 'test',
      env: { appName: 'test' },
      commands: { executeCommand: async () => { commandCount += 1; return 'ok'; } },
    };
  }
  return originalLoad(request, parent, isMain);
};
const bridge = require(process.env.CUL_EXTENSION);
const socketPath = process.env.CUL_SOCKET;
const statePath = process.env.CUL_STATE;
bridge.activate({ subscriptions: [] });

function waitForFiles() {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + 5000;
    const poll = () => {
      if (fs.existsSync(socketPath) && fs.existsSync(statePath)) return resolve();
      if (Date.now() > deadline) return reject(new Error('bridge did not start'));
      setTimeout(poll, 20);
    };
    poll();
  });
}

function request(value) {
  return new Promise((resolve) => {
    let data = '';
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(data); } };
    const client = require('net').createConnection(socketPath, () => client.write(JSON.stringify(value) + '\n'));
    client.setEncoding('utf8');
    client.on('data', (chunk) => { data += chunk; });
    client.on('end', finish);
    client.on('close', finish);
    client.on('error', finish);
  });
}

(async () => {
  await waitForFiles();
  const state = JSON.parse(fs.readFileSync(statePath, 'utf8'));
  const missing = await request({ action: 'command', id: 'missing' });
  const wrong = await request({ action: 'command', id: 'wrong', token: 'wrong-token' });
  const right = await request({ action: 'command', id: 'right', token: state.token });
  const result = {
    missingBytes: missing.length,
    wrongBytes: wrong.length,
    right: JSON.parse(right).ok === true,
    commandCount,
    socketMode: fs.statSync(socketPath).mode & 0o777,
    stateMode: fs.statSync(statePath).mode & 0o777,
    runtimeMode: fs.statSync(require('path').dirname(socketPath)).mode & 0o777,
  };
  bridge.deactivate();
  process.stdout.write(JSON.stringify(result));
})().catch((error) => { process.stderr.write(String(error)); process.exit(1); });
"""
    environment = os.environ.copy()
    environment.update(
        {
            "CUL_EXTENSION": str(Path(__file__).resolve().parents[2] / "extras" / "vscode-extension" / "extension.js"),
            "CUL_SOCKET": str(socket_path),
            "CUL_STATE": str(state_path),
            "CUL_VSCODE_SOCKET": str(socket_path),
            "CUL_VSCODE_STATE_FILE": str(state_path),
            "XDG_RUNTIME_DIR": str(runtime),
        }
    )
    completed = subprocess.run([node, "-e", script], env=environment, capture_output=True, text=True, timeout=15, check=False)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {
        "missingBytes": 0,
        "wrongBytes": 0,
        "right": True,
        "commandCount": 1,
        "socketMode": 0o600,
        "stateMode": 0o600,
        "runtimeMode": 0o700,
    }


def test_godot_bridge_rejects_missing_and_wrong_tokens_and_accepts_the_right_one(tmp_path: Path) -> None:
    godot = shutil.which("godot") or str(Path.home() / ".local" / "bin" / "godot")
    if not Path(godot).is_file():
        pytest.skip("Godot is unavailable")
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    state_path = tmp_path / "state" / "godot.json"
    port_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    port_probe.bind(("127.0.0.1", 0))
    port = int(port_probe.getsockname()[1])
    port_probe.close()
    environment = os.environ.copy()
    environment.update(
        {
            "CUL_GODOT_PORT": str(port),
            "CUL_GODOT_STATE_FILE": str(state_path),
            "XDG_RUNTIME_DIR": str(runtime),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        }
    )
    process = subprocess.Popen(
        [godot, "--headless", "--editor", "--path", str(Path(__file__).resolve().parents[1] / "fixtures" / "godot-adapter-project")],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def wait_for_state() -> dict[str, Any]:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if state.get("token"):
                    return state
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        raise AssertionError("Godot bridge state file did not appear")

    def request(payload: dict[str, Any]) -> bytes:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
                connection.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                connection.settimeout(3)
                return connection.recv(65536)
        except (ConnectionResetError, OSError):
            return b""

    try:
        state = wait_for_state()
        assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
        assert request({"action": "status"}) == b""
        assert request({"action": "status", "token": "wrong-token"}) == b""
        response = json.loads(request({"action": "status", "token": state["token"]}).decode("utf-8"))
        assert response["ok"] is True
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
