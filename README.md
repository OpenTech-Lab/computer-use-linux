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

All five are implemented and exposed as both `cul app <name> <action>` and MCP
`app_<name>_<action>` tools.

| Adapter | Control path | Actions |
| --- | --- | --- |
| `browser` | Chrome DevTools Protocol | `launch` `navigate` `eval` `click_selector` `text` `screenshot` `wait_for` `status` `stop` |
| `godot` | `--headless --script` + editor socket | `launch` `run_scene` `eval_gdscript` `editor_command` `status` `stop` |
| `blender` | `bpy` on Blender's main thread | `launch` `run_python` `scene_info` `viewport_screenshot` `status` `stop` |
| `vscode` | `code` CLI + AT-SPI | `launch` `open` `goto` `diff` `command` `windows` `tree` `read_text` `invoke_action` |
| `atspi_generic` | AT-SPI2 tree + `do_action()` | `windows` `tree` `read_text` `focus` `actions` `invoke_action` |

Adapters register through a decorator, so **adding an application requires no change to core**.
Actions that execute arbitrary code (`eval`, `run_python`, `eval_gdscript`) are confirmation-gated.

```bash
cul app list
cul app browser launch
cul app browser navigate --url https://example.com
cul app browser text
cul app blender scene_info
cul app godot eval_gdscript --confirm --expr "Engine.get_version_info().string"
cul app vscode windows
```
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

### The agent shares your cursor

On a single seat, Wayland and X11 provide **one logical pointer**. When the agent moves the mouse,
it moves *your* mouse — you cannot comfortably use the machine at the same time. This is a property
of the display server, not a limitation of this tool.

Two things reduce it:

- **Prefer adapters.** `cul app ...` actions drive applications through their own APIs and
  `do_action()`, so they never touch the pointer at all. Most useful work needs no cursor.
- **A virtual surface gives the agent its own pointer.** `Mutter.ScreenCast.RecordVirtual`, bound to
  a RemoteDesktop session, yields a surface whose pointer is genuinely independent — verified: the
  virtual pointer moved to its commanded coordinate while the real cursor stayed put, and no monitor
  was added to the desktop layout. It is not yet wired into the public API because placing ordinary
  application windows onto that surface is still an open question; a nested compositor is the
  fallback for full isolation.

Run it on a machine you are willing to let an agent control.

## Roadmap

- [x] Core input injection (Mutter RemoteDesktop) and screen capture (Mutter ScreenCast)
- [x] MCP server + CLI surface
- [x] Minimal AT-SPI window/focus/readback foundation
- [x] App adapters: browser, VSCode, Godot, Blender, generic AT-SPI
- [ ] Independent agent pointer via a virtual surface or nested compositor
- [ ] X11 and nested/headless backends
- [ ] Packaging and install docs

## License

Not yet specified.
