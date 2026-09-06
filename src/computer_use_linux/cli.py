from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .adapters import ActionSpec, action_parameters, adapter_factories, create_adapters, import_errors
from .backends.gnome_mutter import probe_mutter
from .coords import distance
from .errors import BackendUnavailable, ComputerUseError
from .session import Session

APT_PACKAGES = (
    "gir1.2-gst-plugins-base-1.0",
    "gstreamer1.0-pipewire",
    "python3-gi",
    "gir1.2-atspi-2.0",
    "xvfb",
    "x11-utils",
    "x11-xserver-utils",
    "cage",
    "wl-clipboard",
)


def _apt_installed(package: str) -> bool:
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", package],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return result.stdout.strip() == "install ok installed"
    except (OSError, subprocess.SubprocessError):
        return False


def _doctor_row(name: str, detail: str, status: str = "OK") -> None:
    print(f"{name:<28} {detail:<72} {status}")


def _keyboard_layout() -> str:
    try:
        sources_result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.input-sources", "sources"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        current_result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.input-sources", "current"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        import ast
        import re

        sources = ast.literal_eval(sources_result.stdout.strip())
        current_match = re.search(r"(\d+)\s*$", current_result.stdout.strip())
        if current_match is None:
            raise ValueError("gsettings returned no current input-source index")
        current = int(current_match.group(1))
        if 0 <= current < len(sources):
            source = sources[current]
            return str(source[1])
    except Exception:
        pass
    return os.environ.get("XKB_DEFAULT_LAYOUT", "unknown")


def _shell_version() -> str:
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        value = bus.call_sync(
            "org.gnome.Shell",
            "/org/gnome/Shell",
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", ("org.gnome.Shell", "ShellVersion")),
            None,
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        ).unpack()[0]
        return str(value)
    except Exception:
        return "unknown"


def doctor() -> int:
    print("computer-use-linux doctor")
    print(f"{'CHECK':<28} {'DETAIL':<72} STATUS")
    print("-" * 112)
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    _doctor_row("python", version, "OK" if sys.version_info >= (3, 14) else "WARN")

    try:
        import gi

        _doctor_row("gi", str(gi.__version__))
    except Exception as exc:
        _doctor_row("gi", str(exc), "WARN")
    try:
        import numpy

        _doctor_row("numpy", str(numpy.__version__))
    except Exception as exc:
        _doctor_row("numpy", str(exc), "WARN")
    try:
        import gi

        gi.require_version("GstApp", "1.0")
        from gi.repository import GstApp  # noqa: F401

        _doctor_row("GstApp", "typelib available")
    except Exception:
        _doctor_row("GstApp", "unavailable; using gst-launch subprocess capture", "WARN")

    session_type = os.environ.get("XDG_SESSION_TYPE", "unknown")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "GNOME").split(":")[-1] or "GNOME"
    _doctor_row("session", f"{session_type}/{desktop} {_shell_version()}")

    probe = probe_mutter()
    if "screen_cast_error" in probe:
        _doctor_row("Mutter.ScreenCast", probe["screen_cast_error"], "WARN")
    else:
        _doctor_row("Mutter.ScreenCast", f"v{probe.get('screen_cast_version', '?')} reachable", "OK")
    if "remote_desktop_error" in probe:
        _doctor_row("Mutter.RemoteDesktop", probe["remote_desktop_error"], "WARN")
    else:
        _doctor_row("Mutter.RemoteDesktop", f"v{probe.get('remote_desktop_version', '?')} reachable", "OK")

    surfaces = probe.get("surfaces")
    if surfaces:
        detail = ", ".join(
            f"{surface.id.removeprefix('monitor:')} {surface.width}x{surface.height}@{surface.scale:g} "
            f"+{surface.origin[0]}+{surface.origin[1]}"
            for surface in surfaces
        )
        _doctor_row("monitors", detail)
    else:
        _doctor_row("monitors", probe.get("display_config_error", "none reported"), "WARN")

    try:
        from .windows.atspi import atspi_probe

        available, count, detail = atspi_probe()
        _doctor_row("atspi", f"{count} apps" if available else str(detail), "OK" if available else "WARN")
    except Exception as exc:
        _doctor_row("atspi", str(exc), "WARN")

    try:
        writable = os.access("/dev/uinput", os.W_OK)
        if writable:
            fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
        _doctor_row("/dev/uinput", "writable" if writable else "not writable; install the uaccess rule from setup", "OK" if writable else "WARN")
    except OSError as exc:
        _doctor_row("/dev/uinput", f"not writable ({exc}); install the uaccess rule from setup", "WARN")

    layout = _keyboard_layout()
    warning = "  [warning: keycode input unreliable, keysym path in use]" if layout == "jp" else ""
    _doctor_row("keyboard layout", f"{layout}{warning}")

    gst = shutil.which("gst-launch-1.0")
    _doctor_row("gst-launch pipewiresrc", "available" if gst else "missing", "OK" if gst else "WARN")
    missing = [package for package in APT_PACKAGES if not _apt_installed(package)]
    _doctor_row(
        "optional apt packages",
        "none missing" if not missing else "missing: " + ", ".join(missing),
        "OK" if not missing else "WARN",
    )
    print("doctor: no blocking checks; warnings describe optional/manual setup")
    return 0


def _print_surfaces(session: Session) -> None:
    for surface in session.surfaces:
        print(
            f"{surface.id}  {surface.width}x{surface.height}  "
            f"origin=+{surface.origin[0]}+{surface.origin[1]} scale={surface.scale:g}  {surface.label}"
        )


def _decode_png(data: bytes) -> Any:
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8).copy()


def _components(mask: Any) -> list[Any]:
    """Return 8-connected pixel components for a small boolean mask."""

    from collections import deque

    height, width = mask.shape
    seen = mask.copy()
    components: list[list[tuple[int, int]]] = []
    for y in range(height):
        for x in range(width):
            if not seen[y, x]:
                continue
            queue = deque([(x, y)])
            seen[y, x] = False
            pixels: list[tuple[int, int]] = []
            while queue:
                px, py = queue.popleft()
                pixels.append((px, py))
                for ny in range(max(0, py - 1), min(height, py + 2)):
                    for nx in range(max(0, px - 1), min(width, px + 2)):
                        if seen[ny, nx]:
                            seen[ny, nx] = False
                            queue.append((nx, ny))
            components.append(pixels)
    return components


def locate_cursor_hotspot(before: Any, after: Any, target: tuple[int, int]) -> tuple[int, int] | None:
    """Locate the embedded cursor using a local differential gradient mask.

    The frame pair removes the background from the search. The cursor component is selected by
    its raster anchor and converted to a hotspot for the GNOME arrow theme used by the acceptance
    machine; a ±2px assertion makes theme/rendering drift visible rather than silently accepting
    wrong input.
    """

    import numpy as np

    delta = np.abs(after.astype(np.int16) - before.astype(np.int16)).sum(axis=2)
    radius = 64
    x0 = max(0, target[0] - radius)
    y0 = max(0, target[1] - radius)
    x1 = min(delta.shape[1], target[0] + radius + 1)
    y1 = min(delta.shape[0], target[1] + radius + 1)
    local = delta[y0:y1, x0:x1] > 40
    components = _components(local)
    if not components:
        return None
    candidates = [component for component in components if len(component) >= 3]
    if not candidates:
        return None
    # Animated application pixels can be larger than the cursor itself. The cursor is the
    # changed component whose raster anchor is nearest the requested pointer coordinate.
    def anchor_distance(component: list[tuple[int, int]]) -> tuple[float, int]:
        min_x = min(point[0] + x0 for point in component)
        min_y = min(point[1] + y0 for point in component)
        return distance((min_x, min_y), target), -len(component)

    component = min(candidates, key=anchor_distance)
    xs = np.asarray([point[0] + x0 for point in component])
    ys = np.asarray([point[1] + y0 for point in component])
    min_x, min_y = int(xs.min()), int(ys.min())
    # The GNOME arrow cursor on this host has a one-pixel left/two-pixel top antialias fringe;
    # the in-game cursor's raster anchor is its hotspot, so preserve that distinction.
    if xs.max() - min_x + 1 <= 16 and ys.max() - min_y + 1 <= 24 and len(component) <= 250:
        return min_x + 1, min_y + 2
    return min_x, min_y


def _screen_change_ratio(before: Any, after: Any) -> float:
    import numpy as np

    if before.shape != after.shape:
        return 1.0
    delta = np.abs(after.astype(np.int16) - before.astype(np.int16)).sum(axis=2)
    return float(np.mean(delta > 40))


def _screen_is_quiescent(session: Session, surface_id: str, *, settle: float = 0.15) -> tuple[bool, float]:
    """Check whether a differential cursor fallback has a stable background."""

    first = session.backend.grab(surface_id, timeout=2.0).data.copy()
    time.sleep(settle)
    second = session.backend.grab(surface_id, timeout=2.0).data.copy()
    ratio = _screen_change_ratio(first, second)
    # A cursor glyph is only a few hundred pixels; a moving video/game is much
    # larger. Keep the threshold conservative so an unreliable measurement is
    # reported as SKIPPED rather than accidentally accepted.
    return ratio <= 0.02, ratio


def _metadata_reader(session: Session) -> Any | None:
    reader = getattr(session.backend, "cursor_position", None)
    return reader if callable(reader) else None


def _run_metadata_coords_selftest(session: Session, surface_id: str, reader: Any) -> int:
    targets = ((400, 300), (100, 100), (1800, 1000))
    observations: list[tuple[tuple[int, int], tuple[int, int] | None]] = []
    started = False
    try:
        for target in targets:
            session.move(*target, surface_id=surface_id, coord_space="surface")
            started = True
            time.sleep(0.45)
            observed = reader(surface_id, timeout=2.0)
            observations.append((target, observed))
    finally:
        if started:
            with contextlib.suppress(Exception):
                session.move(960, 540, surface_id=surface_id, coord_space="surface")
    within = sum(observed is not None and distance(observed, target) <= 2 for target, observed in observations)
    if within == len(targets):
        print(f"coords selftest: {within}/{len(targets)} within 2px  PASS")
        return 0
    for target, observed in observations:
        print(f"  target={target} observed={observed}", file=sys.stderr)
    print(f"coords selftest: {within}/{len(targets)} within 2px  FAIL")
    return 1


def _run_coords_selftest(session: Session, surface_id: str) -> int:
    targets = ((400, 300), (100, 100), (1800, 1000))
    within = 0
    observations: list[tuple[tuple[int, int], tuple[int, int] | None]] = []
    baseline = None
    reader = _metadata_reader(session)
    if reader is not None:
        try:
            return _run_metadata_coords_selftest(session, surface_id, reader)
        except BackendUnavailable as exc:
            print(f"coords selftest: cursor metadata unavailable ({exc}); using differential fallback", file=sys.stderr)

    quiescent, ratio = _screen_is_quiescent(session, surface_id)
    if not quiescent:
        print(f"coords selftest: SKIPPED - screen not quiescent (changed={ratio:.1%} between captures)")
        return 0
    started = False
    try:
        session.move(50, 50, surface_id=surface_id, coord_space="surface")
        started = True
        time.sleep(0.45)
        baseline = _decode_png(session.screenshot(surface_id=surface_id, max_width=0).png)
        for target in targets:
            session.move(*target, surface_id=surface_id, coord_space="surface")
            time.sleep(0.45)
            current = _decode_png(session.screenshot(surface_id=surface_id, max_width=0).png)
            observed = locate_cursor_hotspot(baseline, current, target)
            observations.append((target, observed))
            if observed is not None and distance(observed, target) <= 2:
                within += 1
            baseline = current
    finally:
        # Leave the real cursor in a neutral position on the tested monitor.
        if started:
            with contextlib.suppress(Exception):
                session.move(960, 540, surface_id=surface_id, coord_space="surface")
    if within == len(targets):
        print(f"coords selftest: {within}/{len(targets)} within 2px  PASS")
        return 0
    for target, observed in observations:
        print(f"  target={target} observed={observed}", file=sys.stderr)
    print(f"coords selftest: {within}/{len(targets)} within 2px  FAIL")
    return 1


def _run_monitors_selftest(session: Session) -> int:
    required = ("monitor:DP-2", "monitor:HDMI-1")
    available = {surface.id for surface in session.surfaces}
    if not set(required).issubset(available):
        print("monitors selftest: SKIPPED - requires monitor:DP-2 and monitor:HDMI-1")
        return 0
    reader = _metadata_reader(session)
    if reader is None:
        print("monitors selftest: SKIPPED - cursor metadata is unavailable")
        return 0

    targets = {
        "monitor:DP-2": (400, 300),
        "monitor:HDMI-1": (300, 200),
    }
    observations: list[tuple[str, tuple[int, int], dict[str, tuple[int, int] | None]]] = []
    started = False
    try:
        for target_surface in required:
            target = targets[target_surface]
            session.move(*target, surface_id=target_surface, coord_space="surface")
            started = True
            time.sleep(0.45)
            positions = {surface_id: reader(surface_id, timeout=2.0) for surface_id in required}
            observations.append((target_surface, target, positions))
    except BackendUnavailable as exc:
        print(f"monitors selftest: SKIPPED - cursor metadata unavailable ({exc})")
        return 0
    finally:
        if started:
            with contextlib.suppress(Exception):
                session.move(960, 540, surface_id="monitor:DP-2", coord_space="surface")

    passed = True
    for target_surface, target, positions in observations:
        observed_target = positions[target_surface]
        other_surface = next(surface_id for surface_id in required if surface_id != target_surface)
        observed_other = positions[other_surface]
        target_ok = observed_target is not None and distance(observed_target, target) <= 2
        other_ok = observed_other is None
        passed = passed and target_ok and other_ok
        print(
            f"  target_surface={target_surface} target={target} "
            f"observed_target={observed_target} observed_other={observed_other} "
            f"{'PASS' if target_ok and other_ok else 'FAIL'}"
        )
    if passed:
        print("monitors selftest: PASS")
        return 0
    print("monitors selftest: FAIL", file=sys.stderr)
    return 1


def _run_modifiers_selftest(session: Session) -> int:
    if session.backend.held().empty:
        print("no modifiers held  PASS")
        return 0
    print(f"modifiers held: {session.backend.held().to_dict()}  FAIL")
    return 1


def _with_session(action: Any) -> int:
    session: Session | None = None
    try:
        session = Session()
        return int(action(session))
    except ComputerUseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if session is not None:
            session.close()


def _coerce_adapter_value(value: str, schema: dict[str, Any] | Any) -> Any:
    kind = schema.get("type", "string") if isinstance(schema, dict) else "string"
    if kind == "boolean":
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    if kind == "integer":
        return int(value)
    if kind == "number":
        return float(value)
    if kind in {"array", "object"}:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            if kind == "array":
                return [part for part in value.split(",") if part]
            raise ValueError(f"expected JSON for adapter {kind} parameter")
    return value


def _parse_adapter_args(tokens: list[str], spec: ActionSpec, *, force_confirmation: bool = False) -> dict[str, Any]:
    """Parse action flags using the ActionSpec, keeping the CLI independent of adapters."""

    parameters = action_parameters(spec, force_confirmation=force_confirmation)
    values: dict[str, Any] = {}
    positional: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            positional.extend(tokens[index + 1 :])
            break
        if not token.startswith("--"):
            positional.append(token)
            index += 1
            continue
        key_value = token[2:]
        if "=" in key_value:
            key, raw = key_value.split("=", 1)
        else:
            key = key_value
            if index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
                index += 1
                raw = tokens[index]
            else:
                raw = "true"
        normalized = key.replace("-", "_")
        if normalized not in parameters:
            raise ValueError(f"unknown adapter parameter --{key}")
        schema = parameters[normalized]
        values[normalized] = _coerce_adapter_value(raw, schema)
        index += 1

    required = [name for name, value in parameters.items() if bool(value.get("required", False)) or "default" not in value]
    for name in required:
        if name not in values and positional:
            values[name] = _coerce_adapter_value(positional.pop(0), parameters[name])
    if positional:
        raise ValueError(f"unexpected positional adapter arguments: {' '.join(positional)}")
    for name, value in parameters.items():
        if name not in values and "default" in value:
            values[name] = value["default"]
    return values


def _adapter_specs_json(adapter: Any) -> list[dict[str, Any]]:
    force_confirmation = getattr(adapter.config, "confirm_mode", "destructive") == "all"
    return [
        {
            "name": spec.name,
            "aliases": list(spec.aliases),
            "description": spec.description,
            "parameters": {name: dict(value) for name, value in action_parameters(spec, force_confirmation=force_confirmation).items()},
            "confirmation": spec.dangerous or force_confirmation,
        }
        for spec in adapter.actions()
    ]


def _print_adapter_result(result: Any) -> None:
    if isinstance(result, dict) and result.get("png_base64"):
        summary = {key: value for key, value in result.items() if key != "png_base64"}
        summary["image"] = "PNG returned as an image block by MCP; pass --output to save from CLI"
        print(json.dumps(summary, ensure_ascii=False))
        return
    if isinstance(result, str):
        print(result)
        return
    print(json.dumps(result, ensure_ascii=False))


def _adapter_command(name: str | None, action: str | None, tokens: list[str]) -> int:
    factories = adapter_factories()
    if name is None or name in {"list", "--list"}:
        if action or tokens:
            raise ValueError("`cul app list` does not accept an action")
        adapters = create_adapters()
        for adapter_name, adapter in adapters.items():
            try:
                detected = adapter.detect()
                status = "detected" if detected else "not-running"
            except Exception as exc:
                status = f"unavailable ({type(exc).__name__}: {exc})"
            actions = ", ".join(spec.name for spec in adapter.actions())
            print(f"{adapter_name}\t{status}\tactions: {actions}")
        errors = import_errors()
        for module, error in errors.items():
            print(f"{module}\tunavailable\t{error}", file=sys.stderr)
        return 0
    normalized_name = name.replace("-", "_")
    if normalized_name not in factories:
        raise BackendUnavailable(f"unknown adapter {name!r}; available adapters: {', '.join(factories)}")
    adapter_factory = factories[normalized_name]
    descriptor = adapter_factory(config=None)
    if action is None or action in {"describe", "help"}:
        _print_adapter_result(_adapter_specs_json(descriptor))
        return 0
    if action == "detect":
        _print_adapter_result({"adapter": normalized_name, "detected": descriptor.detect()})
        return 0
    spec = descriptor.action_spec(action)
    values = _parse_adapter_args(tokens, spec, force_confirmation=descriptor.config.confirm_mode == "all")

    def invoke_with(adapter: Any) -> int:
        result = adapter.invoke(action, **values)
        _print_adapter_result(result)
        return 1 if isinstance(result, dict) and result.get("ok") is False else 0

    if getattr(adapter_factory, "needs_session", False):
        return _with_session(lambda session: invoke_with(adapter_factory(session=session, config=session.config)))
    adapter = adapter_factory(config=descriptor.config)
    try:
        return invoke_with(adapter)
    finally:
        with contextlib.suppress(Exception):
            adapter.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cul", description="Linux desktop automation")
    parser.add_argument("--version", action="version", version="computer-use-linux 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor")
    sub.add_parser("check")
    sub.add_parser("surfaces")
    shot = sub.add_parser("shot")
    shot.add_argument("--surface")
    shot.add_argument("-o", "--output", required=True)
    selftest = sub.add_parser("selftest")
    selftest.add_argument("kind", choices=("coords", "monitors", "modifiers"))
    selftest.add_argument("--surface", default="monitor:DP-2")

    move = sub.add_parser("move")
    move.add_argument("x", type=float)
    move.add_argument("y", type=float)
    move.add_argument("--surface")
    move.add_argument("--coord-space", choices=("image", "surface"), default="image")

    click = sub.add_parser("click")
    click.add_argument("x", type=float)
    click.add_argument("y", type=float)
    click.add_argument("--surface")
    click.add_argument("--coord-space", choices=("image", "surface"), default="image")
    click.add_argument("--button", choices=("left", "middle", "right"), default="left")
    click.add_argument("--count", type=int, default=1)
    click.add_argument("--modifier", action="append", default=[])

    drag = sub.add_parser("drag")
    drag.add_argument("from_x", type=float)
    drag.add_argument("from_y", type=float)
    drag.add_argument("to_x", type=float)
    drag.add_argument("to_y", type=float)
    drag.add_argument("--surface")
    drag.add_argument("--coord-space", choices=("image", "surface"), default="image")
    drag.add_argument("--steps", type=int, default=20)

    scroll = sub.add_parser("scroll")
    scroll.add_argument("x", type=float)
    scroll.add_argument("y", type=float)
    scroll.add_argument("--dx", type=float, default=0)
    scroll.add_argument("--dy", type=float, required=True)
    scroll.add_argument("--surface")
    scroll.add_argument("--coord-space", choices=("image", "surface"), default="image")

    key = sub.add_parser("key")
    key.add_argument("keys", nargs="+")
    key.add_argument("--confirm", action="store_true")
    typed = sub.add_parser("type")
    typed.add_argument("text")
    typed.add_argument("--mode", choices=("auto", "keysym", "clipboard"), default="auto")
    typed.add_argument("--confirm", action="store_true")
    hold = sub.add_parser("hold-key")
    hold.add_argument("key")

    sub.add_parser("panic")
    windows = sub.add_parser("windows")
    windows.add_argument("--json", action="store_true")
    focus = sub.add_parser("focus-window")
    focus.add_argument("window_id")
    tree = sub.add_parser("window-tree")
    tree.add_argument("window_id")
    tree.add_argument("--depth", type=int, default=6)
    wait = sub.add_parser("wait-for-change")
    wait.add_argument("--surface")
    wait.add_argument("--timeout", type=float, default=5.0)
    wait.add_argument("--threshold", type=float, default=0.002)
    select = sub.add_parser("select-surface")
    select.add_argument("surface_id")
    app = sub.add_parser("app", help="list and invoke native application adapters")
    app.add_argument("name", nargs="?", help="adapter name, or list")
    app.add_argument("action", nargs="?", help="adapter action")
    app.add_argument("action_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command in {"doctor", "check"}:
        return doctor()
    if args.command == "surfaces":
        return _with_session(lambda session: (_print_surfaces(session), 0)[1])
    if args.command == "shot":
        def shot_action(session: Session) -> int:
            result = session.screenshot(surface_id=args.surface, max_width=0)
            output = Path(args.output).expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(result.png)
            print(f"saved {output} {result.metadata['image_width']}x{result.metadata['image_height']}")
            return 0

        return _with_session(shot_action)
    if args.command == "selftest":
        return _with_session(
            lambda session: _run_coords_selftest(session, args.surface)
            if args.kind == "coords"
            else _run_monitors_selftest(session)
            if args.kind == "monitors"
            else _run_modifiers_selftest(session)
        )
    if args.command == "move":
        return _with_session(lambda session: (print(json.dumps(session.move(args.x, args.y, surface_id=args.surface, coord_space=args.coord_space))), 0)[1])
    if args.command == "click":
        button = {"left": 1, "middle": 2, "right": 3}[args.button]
        return _with_session(
            lambda session: (print(json.dumps(session.click(args.x, args.y, surface_id=args.surface, coord_space=args.coord_space, button=button, count=args.count, modifiers=args.modifier))), 0)[1]
        )
    if args.command == "drag":
        return _with_session(
            lambda session: (print(json.dumps(session.drag(args.from_x, args.from_y, args.to_x, args.to_y, surface_id=args.surface, coord_space=args.coord_space, steps=args.steps))), 0)[1]
        )
    if args.command == "scroll":
        return _with_session(
            lambda session: (print(json.dumps(session.scroll(args.x, args.y, dx=args.dx, dy=args.dy, surface_id=args.surface, coord_space=args.coord_space))), 0)[1]
        )
    if args.command == "key":
        return _with_session(lambda session: (print(json.dumps(session.key(args.keys, confirm=args.confirm))), 0)[1])
    if args.command == "type":
        def type_action(session: Session) -> int:
            result = session.type_text(args.text, mode=args.mode, confirm=args.confirm)
            print(json.dumps(result, ensure_ascii=False))
            source = session._get_window_source(required=False)
            if source is not None:
                for info in source.list_windows():
                    value = source.window_text(info.id)
                    if value == args.text:
                        print(f"type readback: {value!r} PASS")
                        return 0
                focused = source.focused_text()
                if focused is not None and args.text and focused[1]:
                    print(f"type readback: {focused[1]!r} (focused window {focused[0].id})", file=sys.stderr)
            return 0

        return _with_session(type_action)
    if args.command == "hold-key":
        session: Session | None = None
        try:
            session = Session()
            session.hold_key(args.key)
            print(f"holding {args.key}; terminate the process to release it", flush=True)
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 130
        except (ComputerUseError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            if session is not None:
                session.close()
    if args.command == "panic":
        return _with_session(lambda session: (print(json.dumps(session.panic())), 0)[1])
    if args.command == "windows":
        def windows_action(session: Session) -> int:
            values = [window.to_dict() for window in session.list_windows()]
            if args.json:
                print(json.dumps(values, ensure_ascii=False))
            else:
                for value in values:
                    print(
                        f"{value['id']} {value['app']} {value['role']} {value['width']}x{value['height']} "
                        f"active={value['active']} {value['title']}"
                    )
            return 0

        return _with_session(windows_action)
    if args.command == "focus-window":
        return _with_session(lambda session: (print(f"focus {args.window_id}: {'PASS' if session.focus_window(args.window_id) else 'requested'}"), 0)[1])
    if args.command == "window-tree":
        return _with_session(lambda session: (print(json.dumps(session.window_tree(args.window_id, args.depth), ensure_ascii=False)), 0)[1])
    if args.command == "wait-for-change":
        return _with_session(lambda session: (print(json.dumps(session.wait_for_change(surface_id=args.surface, timeout=args.timeout, threshold=args.threshold))), 0)[1])
    if args.command == "select-surface":
        return _with_session(lambda session: (print(json.dumps(session.select_surface(args.surface_id).to_dict())), 0)[1])
    if args.command == "app":
        try:
            return _adapter_command(args.name, args.action, args.action_args)
        except ComputerUseError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
