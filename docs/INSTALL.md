# Installation

This project controls the desktop that runs it. Start with `cul doctor`, read every row, and
choose the backend deliberately before connecting an agent.

## Python runtime

Use the system CPython 3.14 and create the virtual environment with system site-packages:

```bash
./scripts/bootstrap.sh
make setup
./.venv/bin/cul doctor
```

`pyproject.toml` requires Python 3.14 or newer. The supported bootstrap command is equivalent to:

```bash
uv venv --python /usr/bin/python3.14 --system-site-packages .venv
```

PyGObject is not a normal pip-only dependency for this project. Its `gi` bindings are compiled
against the distribution's GLib/GObject libraries and load the matching installed introspection
typelibs (including Gio, GStreamer, and AT-SPI). The prepared runtime therefore gets PyGObject
from the operating system through `--system-site-packages`; do not try to repair a missing `gi`
import with `pip install PyGObject`. Use the matching distribution packages and recreate the venv
with `/usr/bin/python3.14` if necessary. `python-xlib`, NumPy, Pillow, and MCP are installed into
the venv by the project bootstrap.

## Optional system packages

The core GNOME path needs the existing Mutter, PipeWire, and GStreamer services. The optional
packages checked by `cul doctor` are:

```text
gir1.2-gst-plugins-base-1.0  gstreamer1.0-pipewire  python3-gi
gir1.2-atspi-2.0            xvfb                  x11-utils
x11-xserver-utils           cage                  wl-clipboard
```

The repository helper lists missing packages and asks before installing them:

```bash
./scripts/install-deps.sh
```

For the isolated CI/headless backend, install the display packages explicitly when they are
missing:

```bash
sudo apt install xvfb cage
```

`cage` is reported as an optional nested Wayland compositor. The implemented headless backend
uses Xvfb directly, which is sufficient to give target applications a separate X server, pointer,
keyboard, and window hierarchy.

## `/dev/uinput`

The uinput device is needed only by a future/portable input backend. GNOME Mutter RemoteDesktop,
X11 XTEST, and the Xvfb backend do not require this rule. `/dev/uinput` is not present with the
required user access by default; on the development machine the access happened to come from
Steam's `60-steam-input.rules`, not from this project.

If a portable backend is enabled, install an owner-approved rule such as:

```text
# /etc/udev/rules.d/70-computer-use-uinput.rules
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"
```

Then reload the rules and start a new login session:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger /dev/uinput
```

Do not add broad `MODE="0666"` permissions for an agent input device.

## Surfaces and isolation

On GNOME, a normal session lists and creates only physical monitor surfaces. Virtual input/capture
is opt-in and must be selected when the session starts:

```bash
./.venv/bin/cul surfaces
./.venv/bin/cul --isolated surfaces
./.venv/bin/cul shot --surface virtual:0 -o /tmp/isolated.png
# MCP: start `cul-mcp --isolated`, then select_surface({"surface_id":"virtual:0"})
```

Mutter cannot safely keep physical and virtual streams in the same bound session. A normal physical
session and an isolated virtual session are therefore separate choices; attempting to select a
virtual surface in an already-started physical MCP session returns an error.

Use isolated mode when the agent must not move the user's physical cursor:

```bash
./.venv/bin/cul --isolated surfaces
./.venv/bin/cul-mcp --isolated
```

The same setting can be made persistent:

```toml
# ~/.config/computer-use-linux/config.toml
[session]
isolated = true
virtual_width = 1280
virtual_height = 720
virtual_refresh = 30
```

`CUL_ISOLATED=1`, `CUL_VIRTUAL_WIDTH`, `CUL_VIRTUAL_HEIGHT`, and `CUL_VIRTUAL_REFRESH` override
the file. `RecordVirtual` gives the agent an independent pointer and capture stream, but it is
not a second desktop: ordinary application windows cannot be placed on it by normal X11/GTK
window operations. The verified experiment requested a window at the virtual monitor's apparent
offset; X11 clamped it back into the physical root, and GTK selected a physical monitor. Use the
headless backend for full application/window-stack isolation:

```bash
CUL_BACKEND=headless ./.venv/bin/cul surfaces
CUL_BACKEND=headless ./.venv/bin/cul-mcp
```

Headless mode starts Xvfb, sets `DISPLAY`/X11 toolkit variables for child applications, and
restores the parent process environment when the session closes. It is capability-gated and fails
with an actionable error when Xvfb is not installed.

## Register the MCP server

Use an absolute path to the venv entry point. Claude Code:

```bash
claude mcp add computer-use -- /abs/path/.venv/bin/cul-mcp
```

For a GNOME virtual-pointer session, add `--isolated` after the server command:

```bash
claude mcp add computer-use -- /abs/path/.venv/bin/cul-mcp --isolated
```

Codex CLI uses its MCP subcommand:

```bash
codex mcp add computer-use -- /abs/path/.venv/bin/cul-mcp
codex mcp list
```

For full Xvfb isolation in Codex, persist the backend environment variable on the stdio server
configuration, or put `backend = "headless"` at the top level of `config.toml`:

```bash
codex mcp add computer-use --env CUL_BACKEND=headless -- /abs/path/.venv/bin/cul-mcp
```

See the [official Codex MCP documentation](https://developers.openai.com/codex/mcp) for current
CLI and configuration details.

## Troubleshooting `cul doctor`

The diagnostic rows are intentionally actionable:

| Row | Meaning and next step |
| --- | --- |
| `python` | Must be CPython 3.14+. Recreate `.venv` with `/usr/bin/python3.14 --system-site-packages`. |
| `gi` | Install the distribution PyGObject packages and use the system-site-packages venv. |
| `GstApp` | Optional warning. Screenshots use the `gst-launch-1.0` subprocess path when this typelib is absent. |
| `Mutter.ScreenCast` / `Mutter.RemoteDesktop` | The GNOME private APIs are unavailable, blocked, or the session is not usable. Try X11 or headless. |
| `virtual surface` | `RecordVirtual` is unavailable. Normal physical surfaces can still work; isolated GNOME mode cannot. Install Xvfb and use `CUL_BACKEND=headless` for full isolation. |
| `X11/XTEST+XGetImage` | Both XTEST and direct root capture must be available. XWayland may expose XTEST but reject root `XGetImage`; use the GNOME backend for the full Wayland desktop or Xvfb for headless work. |
| `headless Xvfb` | Install `xvfb` (and optionally `cage`) with `sudo apt install xvfb cage`; tests skip cleanly while it is absent. |
| `atspi` | Install `gir1.2-atspi-2.0` for window/safety context. Core pointer capture can still be probed separately. |
| `/dev/uinput` | Relevant only to the portable uinput backend. Check the udev rule and Steam's rule if that backend is enabled. |
| `keyboard layout` | `jp` uses the keysym path; this warning is expected and does not require root. |
| `gst-launch pipewiresrc` | Install `gstreamer1.0-pipewire` and the GStreamer base plugins. |
| `optional apt packages` | Informational aggregate of the packages above; run `scripts/install-deps.sh` if desired. |

If a GNOME cursor selftest reports `SKIPPED`, it means cursor metadata was starved or the screen
was busy, not that a coordinate assertion passed. A fullscreen game holding a pointer grab on
`DP-2` can starve cursor metadata there; `monitor:HDMI-1` is the reliable coordinate-check
surface on the verified machine.
