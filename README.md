# computer-use-linux

**Give AI agents eyes and hands on a real Linux desktop.**

`computer-use-linux` lets coding agents — Claude Code, Codex, or anything that speaks
[MCP](https://modelcontextprotocol.io) — *see* the screen and *drive* the mouse and keyboard
of a Linux machine. The goal is not a browser-only sandbox: it is the whole desktop, so an
agent can operate a browser, **Godot**, **Blender**, **VSCode**, a terminal, or any other
GUI application the way a person would.

> **Status: Phases 0–6 implemented for the adapter scope.** The project includes the GNOME
> vertical slice, browser CDP, Godot, Blender, VSCode, and generic AT-SPI adapters. Each adapter
> is exposed through both `cul app ...` and dynamically generated MCP tools.

---

## Why this is not just `xdotool`

Most "computer use" recipes assume X11 and reach for `xdotool`, `wmctrl`, or `scrot`. On a
modern Linux desktop that is increasingly wrong: **Ubuntu, Fedora, and most GNOME/KDE
installs now default to Wayland**, where those tools cannot see or touch native Wayland
windows at all. They silently degrade to XWayland-only, which usually means they appear to
work and then don't.

This project targets the Wayland-first reality directly, and keeps X11 as a supported
backend rather than the assumption.

### How input and capture actually work here

| Concern | Approach | Requires root? | Requires consent? |
| --- | --- | --- | --- |
| Keyboard & mouse | **Mutter RemoteDesktop** D-Bus (keysym-based) on GNOME | No | No |
| Screen capture | **Mutter ScreenCast** D-Bus + `gst-launch-1.0 pipewiresrc` | No | None |
| Window & widget info | **AT-SPI2** accessibility tree | No | No |
| App-level control | Native APIs per app (see [adapters](#app-adapters)) | No | No |

Two findings drive that design, both confirmed by probing a live GNOME 50 / Wayland system:

- **Input goes in by *keysym*, not keycode.** `org.gnome.Mutter.RemoteDesktop` accepts
  `NotifyKeyboardKeysym`, so Mutter itself does the keysym→keycode mapping and text entry is
  immune to the active keyboard layout. This matters more than it sounds: on a Japanese (jp106)
  layout, naive keycode injection silently types the wrong characters for `@ [ ] : _`. Pointer
  coordinates are **stream-relative**, so the screenshot's pixel space and the click coordinate
  space are the *same space* — which removes an entire class of multi-monitor bugs.
- **`/dev/uinput` is reserved for the later portable backend.** It is root-only on stock systems;
  if it already works, commonly Steam's `60-steam-input.rules` supplied the `uaccess` rule.
- **Not all of GNOME's D-Bus surface is closed — check which.** `org.gnome.Shell.Screenshot`
  and `org.gnome.Shell.Introspect` still *introspect* successfully on GNOME 50 but return
  `Access denied` on every call, so anything built on them looks correct until it runs.
  `org.gnome.Mutter.ScreenCast` is a **different** bus name, and it works for the session user
  with no prompt at all. Capture prefers it on GNOME and falls back to the portable XDG portal
  elsewhere.

## Architecture

Two tiers of control, because pixel-pushing alone is fragile:

**Tier 1 — pixel level (universal).** Screenshot, move, click, drag, scroll, key, type.
Works against any application, including ones with no automation API. This is the fallback
that is always available.

**Tier 2 — app adapters (reliable).** Where an application exposes a real API, use it.
Clicking coordinates in a 3D viewport is a coin flip; calling the application's own
scripting interface is deterministic.

Backends are pluggable and selected by runtime capability detection:

- `gnome_mutter` — the current primary path: bound Mutter RemoteDesktop + ScreenCast sessions
- `portal`, `x11`, and `headless` — planned portable backends, outside this adapter-focused run

### App adapters

| Application | Control path |
| --- | --- |
| Chrome / Chromium | Chrome DevTools Protocol |
| VSCode | `code` CLI + companion extension / AT-SPI |
| Godot 4 | `--headless` / `--script` + editor plugin socket |
| Blender | Python API over a socket addon |
| GTK / Qt apps | AT-SPI2 semantic tree |

New applications are added by implementing the adapter interface, without touching core.
See [docs/ADAPTERS.md](docs/ADAPTERS.md) for the precedence rule, action list, isolation, and
bridge lifecycle.

## Quickstart

The supplied `.venv` must be CPython 3.14 with system site-packages. `make setup` validates
an existing environment and installs the Python package; it never installs apt packages or
replaces an existing `.venv`.

```bash
make setup
.venv/bin/cul doctor
.venv/bin/cul surfaces
.venv/bin/cul shot --surface monitor:DP-2 -o /tmp/desktop.png
.venv/bin/cul selftest coords --surface monitor:DP-2
.venv/bin/cul selftest monitors
.venv/bin/cul app list
.venv/bin/cul-mcp
```

The screenshot is a PNG returned directly by MCP. The default 1280-pixel image width is a
deliberate context-budget limit; use the CLI/native shot or MCP `max_width` when reading small text.
The coordinate selftest reads PipeWire cursor metadata, so it remains reliable while a game or video
is animating. If the metadata path is unavailable, the legacy frame-difference fallback only reports
`SKIPPED` when the screen is busy; it never reports a false pass.

## Requirements

- Linux with Wayland (GNOME 50+ verified) or X11
- On GNOME, nothing further — Mutter's RemoteDesktop and ScreenCast APIs need no consent and no setup
- For the later portable (non-GNOME / X11) backend, write access to `/dev/uinput` via a one-time udev rule:
  ```
  KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"
  ```
- PipeWire and `xdg-desktop-portal` for screen capture
- Python 3.14 at `/usr/bin/python3.14`, with `.venv` created using `--system-site-packages`
- `gir1.2-gst-plugins-base-1.0` is optional here: it enables the in-process appsink path used for
  cursor metadata when available. Ordinary screenshots still use the verified `gst-launch-1.0`
  subprocess path because the GstApp/GstVideo typelibs are absent on the target machine. Run
  `scripts/install-deps.sh` manually if desired.

## Safety

This tool drives a **real** desktop with **real** input devices. It is built with that in mind:

- **Killing the process is a complete kill switch, by construction.** Mutter destroys the session
  the instant the D-Bus connection drops, releasing every held key and button. `pkill` is always safe.
- A concrete command is `pkill -f cul-mcp`; the long-lived D-Bus connection then disappears and
  Mutter releases its input state.
- A `panic` command, plus a filesystem trip-wire a human can touch from another terminal
- Virtual input devices are released on crash, so a stuck modifier key cannot wedge the session
- Opt-in confirmation for destructive actions
- Screenshot redaction for regions that should never reach a model

Run it on a machine you are willing to let an agent control.

## Roadmap

- [x] Core input injection (Mutter RemoteDesktop) and screen capture (Mutter ScreenCast)
- [x] MCP server + CLI surface
- [x] Minimal AT-SPI window/focus/readback foundation
- [x] App adapters: browser, VSCode, Godot, Blender, generic AT-SPI
- [ ] X11 and nested/headless backends
- [ ] Packaging and install docs

## License

Not yet specified.
