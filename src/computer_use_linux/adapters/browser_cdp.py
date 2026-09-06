"""Chrome DevTools Protocol adapter.

The implementation deliberately uses only Python's standard library.  ``httpx`` and
``websockets`` are convenient, but making them optional means importing the project never fails
on a prepared desktop that only has the core dependencies.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..errors import BackendUnavailable
from . import ActionSpec, AdapterBase, register
from ._process import start_ticks


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


class _WebSocket:
    """Small RFC 6455 client for the localhost CDP endpoint."""

    def __init__(self, url: str, *, timeout: float = 8.0):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise BackendUnavailable(f"unsupported CDP websocket URL: {url}")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise BackendUnavailable("refusing a non-local CDP websocket endpoint")
        port = parsed.port or 80
        self._socket = socket.create_connection((parsed.hostname, port), timeout=timeout)
        self._socket.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        request = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self._socket.sendall(request)
        response = self._read_until(b"\r\n\r\n")
        status = response.split(b"\r\n", 1)[0]
        if b" 101 " not in status and not status.endswith(b" 101"):
            self.close()
            raise BackendUnavailable(f"CDP websocket handshake failed: {status.decode('latin1', 'replace')}")
        headers = {}
        for line in response.split(b"\r\n")[1:]:
            if b":" in line:
                name, value = line.split(b":", 1)
                headers[name.decode("latin1").casefold()] = value.strip().decode("latin1")
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            self.close()
            raise BackendUnavailable("CDP websocket handshake returned an invalid accept key")

    def _read_until(self, marker: bytes) -> bytes:
        data = bytearray()
        while marker not in data:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise BackendUnavailable("CDP websocket closed during handshake")
            data.extend(chunk)
            if len(data) > 64 * 1024:
                raise BackendUnavailable("CDP websocket handshake is too large")
        return bytes(data)

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self._socket.recv(size - len(data))
            if not chunk:
                raise BackendUnavailable("CDP websocket closed")
            data.extend(chunk)
        return bytes(data)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        mask = secrets.token_bytes(4)
        masked = bytes(payload[index] ^ mask[index % 4] for index in range(length))
        self._socket.sendall(header + mask + masked)

    def send_json(self, value: MappingLike) -> None:
        self._send_frame(0x1, json.dumps(value, separators=(",", ":")).encode("utf-8"))

    def recv_json(self) -> dict[str, Any]:
        fragments = bytearray()
        first_opcode: int | None = None
        while True:
            first, second = struct.unpack("!BB", self._read_exact(2))
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(payload[index] ^ mask[index % 4] for index in range(length))
            if opcode == 0x8:
                raise BackendUnavailable("CDP websocket closed")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in {0x1, 0x0}:
                if first_opcode is None:
                    first_opcode = opcode
                fragments.extend(payload)
                if first & 0x80:
                    break
        try:
            value = json.loads(fragments.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackendUnavailable("CDP returned a non-JSON websocket message") from exc
        if not isinstance(value, dict):
            raise BackendUnavailable("CDP returned a non-object websocket message")
        return value

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = int(time.monotonic_ns() & 0x7FFFFFFF)
        self.send_json({"id": request_id, "method": method, "params": params or {}})
        while True:
            value = self.recv_json()
            if value.get("id") != request_id:
                continue
            error = value.get("error")
            if error:
                raise BackendUnavailable(f"CDP {method} failed: {error}")
            return value

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._send_frame(0x8, b"")
        with contextlib.suppress(Exception):
            self._socket.close()

    def __enter__(self) -> _WebSocket:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


MappingLike = dict[str, Any]


def _http_json(url: str, *, timeout: float = 2.0) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        raise BackendUnavailable(f"CDP HTTP request failed for {url}: {exc}") from exc


@register
class BrowserAdapter(AdapterBase):
    """Control a browser instance launched with a private, managed Chrome profile."""

    name = "browser"
    needs_session = False

    def __init__(self, *, session: Any | None = None, config: Any | None = None):
        super().__init__(session=session, config=config)
        self._state_file = self.config.state_dir / "browser.json"

    @property
    def _profile_dir(self) -> Path:
        return self.config.state_dir / "browser-profile"

    def _binary(self, requested: str | None = None) -> str:
        candidate = requested or os.environ.get("CUL_BROWSER_BINARY")
        if candidate:
            path = shutil.which(candidate) or candidate
            if Path(path).is_file() and os.access(path, os.X_OK):
                return str(Path(path).resolve())
            raise BackendUnavailable(f"browser executable is not available: {candidate}")
        for name in ("google-chrome", "brave-browser", "chromium", "chromium-browser"):
            path = shutil.which(name)
            if path:
                return path
        raise BackendUnavailable("no Chrome-compatible browser found (tried google-chrome, brave-browser, chromium)")

    @staticmethod
    def _sandbox_is_usable(binary: str) -> bool:
        """Check the setuid helper without requiring root or mutating the installation."""

        candidates = [
            Path(binary).parent / "chrome-sandbox",
            Path(binary).parent / "brave-sandbox",
            Path("/opt/google/chrome/chrome-sandbox"),
            Path("/opt/brave.com/brave/brave-sandbox"),
        ]
        for candidate in candidates:
            try:
                stat = candidate.stat()
            except OSError:
                continue
            if stat.st_uid == 0 and stat.st_mode & 0o4000:
                return True
        return False

    def _record(self) -> dict[str, Any] | None:
        return _read_json(self._state_file)

    def _managed_process(self, record: dict[str, Any] | None = None) -> bool:
        record = record or self._record()
        if not record:
            return False
        try:
            pid = int(record["pid"])
            os.kill(pid, 0)
        except (KeyError, TypeError, ValueError, OSError):
            return False
        proc_cmdline = Path(f"/proc/{pid}/cmdline")
        try:
            command = proc_cmdline.read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            return False
        expected_start = record.get("start_ticks")
        if expected_start is not None and start_ticks(pid) != int(expected_start):
            return False
        profile = str(self._profile_dir)
        port = str(int(record["port"]))
        return f"--user-data-dir={profile}" in command and f"--remote-debugging-port={port}" in command

    def _base_url(self, record: dict[str, Any] | None = None) -> str:
        record = record or self._record()
        if not record or not self._managed_process(record):
            raise BackendUnavailable("no managed browser is running; run `cul app browser launch` first")
        return f"http://127.0.0.1:{int(record['port'])}"

    def _pages(self) -> list[dict[str, Any]]:
        values = _http_json(self._base_url() + "/json/list")
        if not isinstance(values, list):
            raise BackendUnavailable("CDP /json/list returned an invalid page list")
        return [value for value in values if isinstance(value, dict) and value.get("type") == "page" and value.get("webSocketDebuggerUrl")]

    def _page(self) -> dict[str, Any]:
        pages = self._pages()
        if not pages:
            raise BackendUnavailable("managed browser has no controllable page target")
        record = self._record() or {}
        target_id = record.get("target_id")
        page = next((item for item in pages if item.get("id") == target_id), pages[0])
        if page.get("id") != target_id:
            record["target_id"] = page.get("id")
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(record), encoding="utf-8")
        return page

    def _with_page(self, callback: Callable[[_WebSocket], Any]) -> Any:
        page = self._page()
        url = str(page["webSocketDebuggerUrl"])
        with _WebSocket(url) as socket_client:
            return callback(socket_client)

    @staticmethod
    def _remote_value(response: dict[str, Any]) -> Any:
        result = response.get("result", {}).get("result", {})
        exception = response.get("result", {}).get("exceptionDetails")
        if exception:
            description = exception.get("exception", {}).get("description") or exception.get("text") or "JavaScript exception"
            raise BackendUnavailable(f"CDP evaluation failed: {description}")
        if result.get("unserializableValue") is not None:
            return result["unserializableValue"]
        return result.get("value")

    def _evaluate(self, expression: str) -> Any:
        def call(socket_client: _WebSocket) -> Any:
            response = socket_client.call(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": True,
                    "userGesture": True,
                },
            )
            return self._remote_value(response)

        return self._with_page(call)

    def detect(self) -> bool:
        try:
            self._pages()
            return True
        except BackendUnavailable:
            return False

    def actions(self) -> list[ActionSpec]:
        return [
            ActionSpec(
                "launch",
                "Launch a managed Chrome-compatible browser with an isolated profile.",
                parameters={
                    "browser": {"type": "string", "default": "", "description": "Executable name/path; defaults to google-chrome, then Brave."},
                    "port": {"type": "integer", "default": 9222, "description": "Loopback CDP port; a free port is chosen if this one is occupied."},
                    "headless": {"type": "boolean", "default": False, "description": "Use Chrome headless mode."},
                    "url": {"type": "string", "default": "about:blank", "description": "Initial URL."},
                },
            ),
            ActionSpec("status", "Return managed browser and CDP target status."),
            ActionSpec("stop", "Stop the browser process owned by this adapter."),
            ActionSpec(
                "navigate",
                "Navigate the selected CDP page without using pixel coordinates.",
                parameters={"url": {"type": "string", "required": True, "description": "URL to load."}},
            ),
            ActionSpec(
                "eval",
                "Evaluate JavaScript in the selected page. This is confirmation-gated.",
                parameters={"expr": {"type": "string", "required": True, "description": "JavaScript expression or async expression."}},
            ),
            ActionSpec(
                "click_selector",
                "Click a DOM element by CSS selector through CDP.",
                parameters={"selector": {"type": "string", "required": True, "description": "CSS selector."}},
            ),
            ActionSpec(
                "text",
                "Read page text or the text of a selected DOM element.",
                parameters={"selector": {"type": "string", "default": "", "description": "Optional CSS selector."}},
            ),
            ActionSpec(
                "screenshot",
                "Capture the selected page as a PNG through CDP.",
                parameters={"output": {"type": "string", "default": "", "description": "Optional local output path."}},
            ),
            ActionSpec(
                "wait_for",
                "Wait until a selector exists and/or page text contains a value.",
                parameters={
                    "selector": {"type": "string", "default": "", "description": "Optional CSS selector."},
                    "text": {"type": "string", "default": "", "description": "Optional required text."},
                    "timeout": {"type": "number", "default": 10.0, "description": "Maximum seconds."},
                },
            ),
        ]

    def launch(self, **kwargs: Any) -> dict[str, Any]:
        if self.detect():
            record = self._record() or {}
            return {"ok": True, "already_running": True, "pid": record.get("pid"), "port": record.get("port"), "profile": str(self._profile_dir)}
        binary = self._binary(str(kwargs.get("browser") or "") or None)
        requested_port = _as_int(kwargs.get("port"), 9222)
        port = requested_port
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            busy = probe.connect_ex(("127.0.0.1", port)) == 0
        finally:
            probe.close()
        if busy:
            reserve = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                reserve.bind(("127.0.0.1", 0))
                port = int(reserve.getsockname()[1])
            finally:
                reserve.close()
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        command = [
            binary,
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self._profile_dir}",
            "--force-renderer-accessibility",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
        ]
        if _as_bool(kwargs.get("headless", False)):
            command.append("--headless=new")
        sandbox_warning = False
        if _as_bool(os.environ.get("CUL_BROWSER_NO_SANDBOX", "false")) or not self._sandbox_is_usable(binary):
            # Some unpacked/system images ship Chrome's helper without root ownership or the
            # setuid bit.  We cannot repair that installation without sudo; keep the managed
            # profile boundary and degrade with an explicit, reported no-sandbox launch.
            command.append("--no-sandbox")
            sandbox_warning = True
        url = str(kwargs.get("url") or "about:blank")
        if url:
            command.append(url)
        log_path = self.config.state_dir / "browser.log"
        log_handle = log_path.open("ab")
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_handle.close()
        record = {"pid": process.pid, "port": port, "profile": str(self._profile_dir), "binary": binary, "start_ticks": start_ticks(process.pid)}
        self._state_file.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        deadline = time.monotonic() + 10.0
        last_error = ""
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise BackendUnavailable(f"managed browser exited during launch; see {log_path}")
            try:
                _http_json(f"http://127.0.0.1:{port}/json/version", timeout=0.5)
                pages = self._pages()
                if pages:
                    record["target_id"] = pages[0].get("id")
                    self._state_file.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
                    return {
                        "ok": True,
                        "pid": process.pid,
                        "port": port,
                        "profile": str(self._profile_dir),
                        "binary": binary,
                        "force_renderer_accessibility": True,
                        "sandbox": "disabled" if sandbox_warning else "enabled",
                    }
            except BackendUnavailable as exc:
                last_error = str(exc)
            time.sleep(0.1)
        raise BackendUnavailable(f"timed out waiting for managed browser CDP endpoint: {last_error}")

    def _stop_managed(self) -> dict[str, Any]:
        record = self._record()
        if not record or not self._managed_process(record):
            if record:
                with __import__("contextlib").suppress(OSError):
                    self._state_file.unlink()
            return {"ok": True, "running": False}
        pid = int(record["pid"])
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
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
            record = self._record()
            running = self.detect()
            return {
                "running": running,
                "managed": bool(record and self._managed_process(record)),
                "pid": record.get("pid") if record else None,
                "port": record.get("port") if record else None,
                "profile": str(self._profile_dir),
            }
        if action == "stop":
            return self._stop_managed()
        if action == "navigate":
            url = str(kwargs["url"])

            def navigate(socket_client: _WebSocket) -> Any:
                socket_client.call("Page.enable")
                result = socket_client.call("Page.navigate", {"url": url})
                return result.get("result", {})

            result = self._with_page(navigate)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    state = self._evaluate("document.readyState")
                    if state in {"interactive", "complete"}:
                        break
                except BackendUnavailable:
                    pass
                time.sleep(0.05)
            return {"ok": True, "url": url, "navigation": result}
        if action == "eval":
            return {"value": self._evaluate(str(kwargs["expr"]))}
        if action == "click_selector":
            selector = json.dumps(str(kwargs["selector"]))
            value = self._evaluate(
                f"(() => {{ const e = document.querySelector({selector}); if (!e) return {{ok:false}}; "
                "e.scrollIntoView({block:'center',inline:'center'}); e.click(); "
                "return {ok:true,tag:e.tagName,text:(e.innerText||e.textContent||'').slice(0,500)}; })()"
            )
            if not isinstance(value, dict) or not value.get("ok"):
                raise BackendUnavailable(f"CSS selector did not match an element: {kwargs['selector']}")
            return value
        if action == "text":
            selector = str(kwargs.get("selector") or "")
            if selector:
                expression = (
                    f"(() => {{ const e = document.querySelector({json.dumps(selector)}); "
                    "return e ? (e.innerText ?? e.textContent ?? '') : null; }})()"
                )
                value = self._evaluate(expression)
                if value is None:
                    raise BackendUnavailable(f"CSS selector did not match an element: {selector}")
            else:
                value = self._evaluate("document.body ? (document.body.innerText || document.body.textContent || '') : ''")
            return {"text": str(value or ""), "url": self._page().get("url", "")}
        if action == "screenshot":
            def capture(socket_client: _WebSocket) -> Any:
                return socket_client.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}).get("result", {})

            result = self._with_page(capture)
            data = base64.b64decode(str(result.get("data", "")), validate=True)
            output = str(kwargs.get("output") or "")
            if output:
                path = Path(output).expanduser()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                return {"saved": str(path), "bytes": len(data), "mime_type": "image/png"}
            return {"png_base64": base64.b64encode(data).decode("ascii"), "bytes": len(data), "mime_type": "image/png"}
        if action == "wait_for":
            selector = str(kwargs.get("selector") or "")
            text = str(kwargs.get("text") or "")
            timeout = float(kwargs.get("timeout", 10.0))
            if not selector and not text:
                raise ValueError("wait_for requires selector and/or text")
            selector_js = "true" if not selector else f"Boolean(document.querySelector({json.dumps(selector)}))"
            text_js = "true" if not text else f"(document.body?.innerText || '').includes({json.dumps(text)})"
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                found = bool(self._evaluate(f"({selector_js}) && ({text_js})"))
                if found:
                    return {"ok": True, "selector": selector, "text": text, "elapsed": max(0.0, timeout - max(0.0, deadline - time.monotonic()))}
                if time.monotonic() >= deadline:
                    return {"ok": False, "selector": selector, "text": text, "timeout": timeout}
                time.sleep(0.1)
        raise BackendUnavailable(f"unsupported browser adapter action: {action}")
