# computer-use-linux

**Give AI agents eyes and hands on a real Linux desktop.**

`computer-use-linux` lets coding agents — Claude Code, Codex, or anything that speaks
[MCP](https://modelcontextprotocol.io) — *see* the screen and *drive* the mouse and keyboard
of a Linux machine. The goal is not a browser-only sandbox: it is the whole desktop, so an
agent can operate a browser, **Godot**, **Blender**, **VSCode**, a terminal, or any other
GUI application the way a person would.

Current release: **v0.1.0** · [Release notes](docs/version/0.1.0.md)

> **Status: Phases 0–6 plus virtual-pointer, X11, and Xvfb backend work are implemented.** The
> project includes the GNOME vertical slice, browser CDP, Godot, Blender, VSCode, and generic
> AT-SPI adapters. Each adapter is exposed through both `cul app ...` and dynamically generated
> MCP tools.

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
- **`/dev/uinput` is reserved for the portable backend.** It is root-only on stock systems;
  if it already works, commonly Steam's `60-steam-input.rules` supplied the `uaccess` rule.
- **Not all of GNOME's D-Bus surface is closed — check which.** `org.gnome.Shell.Screenshot`
  and `org.gnome.Shell.Introspect` still *introspect* successfully on GNOME 50 but return
  `Access denied` on every call, so anything built on them looks correct until it runs.
  `org.gnome.Mutter.ScreenCast` is a **different** bus name, and it works for the session user
  with no prompt at all. Capture prefers it on GNOME; the XDG portal remains the planned portable
  capture fallback and is not yet implemented.

## Architecture

Two tiers of control, because pixel-pushing alone is fragile:

**Tier 1 — pixel level (universal).** Screenshot, move, click, drag, scroll, key, type.
Works against any application, including ones with no automation API. This is the fallback
that is always available.

**Tier 2 — app adapters (reliable).** Where an application exposes a real API, use it.
Clicking coordinates in a 3D viewport is a coin flip; calling the application's own
scripting interface is deterministic.

Backends are pluggable and selected by runtime capability detection:

- `gnome_mutter` — the primary Wayland path: bound Mutter RemoteDesktop + ScreenCast sessions
- `x11` — XTEST input and XGetImage capture on an X11/Xvfb root with real window geometry
- `headless` — a private Xvfb display for full pointer, keyboard, and window-stack isolation
- `portal` — not yet implemented; use the explicit backends above

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

## Setup

### 1. Install PyGObject from your package manager

This is the only step that differs between distributions, and it cannot be skipped: `gi` is
compiled against your system's GLib and loads its introspection typelibs, so **it cannot be
installed from PyPI**.

| Distribution | Command |
| --- | --- |
| Debian / Ubuntu | `sudo apt install python3-gi gir1.2-atspi-2.0 gstreamer1.0-pipewire` |
| Fedora / RHEL | `sudo dnf install python3-gobject gstreamer1-plugins-base` |
| Arch | `sudo pacman -S python-gobject at-spi2-core gst-plugin-pipewire` |
| openSUSE | `sudo zypper install python3-gobject gstreamer-plugins-base` |

### 2. Clone and bootstrap

```bash
git clone https://github.com/OpenTech-Lab/computer-use-linux.git
cd computer-use-linux
./scripts/bootstrap.sh
./.venv/bin/cul doctor
```

`bootstrap.sh` locates the system interpreter that can `import gi` and builds `.venv` from it with
`--system-site-packages`. Any Python 3.11+ works — the version is whatever your distribution ships.
It never replaces an existing `.venv` and never installs system packages.

**`cul doctor` is the source of truth.** It prints one row per precondition — session type, Mutter
ScreenCast and RemoteDesktop, monitors, AT-SPI, keyboard layout, `/dev/uinput`, X11, Xvfb — and
tells you exactly what is usable on that machine. Read every row before connecting an agent.

### 3. Connect an agent

```bash
claude mcp add computer-use -- "$PWD/.venv/bin/cul-mcp"
```

Add `--isolated` to the command to give the agent its own pointer instead of sharing your cursor:

```bash
claude mcp add computer-use -- "$PWD/.venv/bin/cul-mcp" --isolated
```

### 4. First commands

Surface names are per-machine, so start with `surfaces` and use the ids it prints.

```bash
.venv/bin/cul surfaces                                   # what can I see?
.venv/bin/cul shot --surface monitor:<ID> -o /tmp/s.png  # capture a screen
.venv/bin/cul selftest coords --surface monitor:<ID>     # prove clicks land where asked
.venv/bin/cul app list                                   # which applications are drivable
```

### If something goes wrong

**`cannot import PyGObject ('gi')`** — the venv was built from the wrong interpreter. If `python3`
resolves to mise, pyenv, asdf, conda or Homebrew, it has no `gi` and no amount of `pip install` will
fix it. Do step 1, then `rm -rf .venv && ./scripts/bootstrap.sh`. Point at a specific interpreter
with `CUL_PYTHON=/usr/bin/python3.12 ./scripts/bootstrap.sh`.

**Screen capture unavailable** — outside GNOME the `Mutter.*` APIs do not exist and the XDG portal
path is used instead, which prompts for consent once. `doctor` says which path is active.

**`GStreamer capture timed out`** — PipeWire only emits frames when the screen *changes*, and a
fullscreen application can take direct scanout, which bypasses composition and stops the stream for
that monitor entirely. A longer timeout does not help; this is not a slow capture, it is no capture.
Use a different surface (`cul surfaces`), leave fullscreen on that monitor, or raise
`CUL_CAPTURE_TIMEOUT` if your display really is just idle.

**`/dev/uinput` not writable** — expected. It is needed only by the portable input backend, not by
GNOME, X11 XTEST or Xvfb. [docs/INSTALL.md](docs/INSTALL.md) has the udev rule if you want it.

Full details, including the optional packages and the headless backend, are in
[docs/INSTALL.md](docs/INSTALL.md).

## Requirements

- **Linux** with Wayland (GNOME 50+ verified) or X11
- **Python 3.11+** — specifically the system interpreter that ships PyGObject; `bootstrap.sh` finds it
- **PipeWire** and GStreamer `pipewiresrc` for GNOME screen capture
- On GNOME, **nothing further** — Mutter's RemoteDesktop and ScreenCast APIs need no consent and no setup
- Optional: `xvfb` for the fully isolated headless backend; `gir1.2-gst-plugins-base-1.0` for the
  in-process appsink path (ordinary screenshots use a `gst-launch-1.0` subprocess without it)

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

### Virtual pointer and full isolation

On a single physical seat, the normal Wayland and X11 surfaces still provide **one logical pointer**.
When the agent selects a physical monitor, moving it moves *your* mouse. This is a property of the
display server, not a limitation of this tool.

There are three practical choices:

- **Prefer adapters.** `cul app ...` actions drive applications through their own APIs and
  `do_action()`, so they never touch the pointer at all. Most useful work needs no cursor.
- **Use `virtual:0` for an independent pointer and capture stream.** `Mutter.ScreenCast.RecordVirtual`
  is opt-in: `--isolated` starts a virtual-only session, and a CLI request such as
  `cul shot --surface virtual:0` selects that mode up front. A normal session lists and creates only
  physical monitor streams. Do not mix physical and virtual surfaces in one Mutter session; start
  MCP with `--isolated` when the virtual surface is needed. A live probe moved the virtual pointer
  while the physical cursor stayed put, and no monitor was added to DisplayConfig. The virtual
  surface starts empty: it is an input-and-capture target, not a second desktop.
  Ordinary application windows cannot be placed there by normal X11/GTK operations; those operations
  remain on a physical desktop surface.
- **Use the headless backend for full isolation.** `CUL_BACKEND=headless` starts a private Xvfb
  server, so target applications have a separate pointer, keyboard, window stack, and capture root.
  It is capability-gated and requires the optional `xvfb` package; see [docs/INSTALL.md](docs/INSTALL.md).

Run it on a machine you are willing to let an agent control.

## Roadmap

- [x] Core input injection (Mutter RemoteDesktop) and screen capture (Mutter ScreenCast)
- [x] MCP server + CLI surface
- [x] Minimal AT-SPI window/focus/readback foundation
- [x] App adapters: browser, VSCode, Godot, Blender, generic AT-SPI
- [x] Independent agent pointer via a virtual surface; full isolation via nested/headless backend
- [x] X11 and nested/headless backends
- [x] Packaging and install docs

## Releasing

```bash
./scripts/bump-version.sh            # auto-increment the patch version
./scripts/bump-version.sh 0.2.0      # explicit version
./scripts/bump-version.sh --tag-only # tag the current version, no file changes
```

Flags: `--force` (allow a non-increasing version), `--no-tag`, `--no-push`.

The version has **one** source of truth, `__version__` in
`src/computer_use_linux/__init__.py`. `pyproject.toml` declares `dynamic = ["version"]` and reads
that attribute, and the CLI's `--version` imports it, so a single edit propagates to the package
metadata and the CLI together. The script refuses to run if that arrangement is broken, and
`tests/unit/test_version_single_source.py` enforces it.

Each release writes `docs/version/<version>.md` from the commits since the previous tag, commits,
tags, pushes, and creates a GitHub release when `gh` is available.

## License

[MIT](LICENSE) © 2026 OpenTech-Lab
