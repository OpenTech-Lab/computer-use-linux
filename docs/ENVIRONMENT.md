# Environment Recon — verified 2026-09-06 on the target box

## Host
- Ubuntu 26.04 LTS (Resolute Raccoon), kernel 7.0.0-15-generic, x86_64
- **GNOME Shell 50.1 on Wayland** (XWayland up: DISPLAY=:0, WAYLAND_DISPLAY=wayland-0)
- libei/libeis 1.5.0 · PipeWire 1.6.2 · gnome-remote-desktop 50.0
- Portals (xdg-desktop-portal 1.21.1 + -gnome 50.0): ScreenCast **v5**, RemoteDesktop **v2**, Screenshot **v2**
- AT-SPI2 2.60.4 installed, gir1.2-atspi-2.0 present, but `org.gnome.desktop.interface toolkit-accessibility = false`

## VERIFIED BY EXECUTION (not assumed)
| Probe | Result |
|---|---|
| `/dev/uinput` open for write as user | **YES** — ACL grants `user:toyofumi:rw-` |
| Create uinput ABS pointer + keyboard, register, destroy | **YES** — appeared as `/devices/virtual/input/input23`, clean teardown |
| `org.gnome.Shell.Screenshot.Screenshot` | **Access denied** (introspects, but call refused) |
| `org.gnome.Shell.Introspect.GetWindows` | **Access denied** |

=> Input needs **no root, no daemon, no consent**. GNOME's private D-Bus APIs are closed on 50.x; do not build on them.

## Installed / missing
- Present: `google-chrome`, `firefox`, `code` 1.134.0, `godot` 4.6.2.stable, `ffmpeg`, ImageMagick `import`, `xrandr`, `busctl`, `snap`
- **Missing**: `blender` (apt candidate 5.0.1+dfsg-1ubuntu1), xdotool, wmctrl, ydotool, grim, slurp, scrot, maim, wl-clipboard, xclip, Xvfb, Xephyr, weston, labwc, sway, cage, mutter(standalone), x11vnc, wayvnc, flatpak
- Python: **default `python3` is mise 3.11.15** (has PIL+numpy; NO gi/dbus). System `/usr/bin/python3` is **3.14.4** and HAS `gi` + `dbus`.
  - Consequence: a venv built on mise python cannot `import gi`. Either use `/usr/bin/python3` for the portal/D-Bus layer, or talk D-Bus over the raw socket without PyGObject.
- `node` 26.1.0, `npm`, `uv` available.

## Capability matrix (what a computer-use tool can actually do here)
| Need | Path | Consent |
|---|---|---|
| Keyboard/mouse injection | **uinput virtual devices** (ABS pointer + keyboard + wheel) | none |
| Input (fallback) | RemoteDesktop portal + libei | one-time |
| Screen capture | **ScreenCast portal + PipeWire**, `persist_mode=2` + saved `restore_token` | one-time, then persistent |
| Screen capture (fallback) | Screenshot portal | dialog per call — unsuitable for automation |
| Window list/geometry | GNOME Introspect DENIED → AT-SPI, or ship our own GNOME Shell extension | none |
| Semantic UI tree | AT-SPI2 (needs toolkit-accessibility=true; Chrome needs `--force-renderer-accessibility`) | none |
| Deterministic / CI | nested compositor (cage/labwc/sway) or Xvfb — must be installed | none |

## App-control notes (prefer real APIs over pixel-poking)
- **Browser**: Chrome DevTools Protocol via `--remote-debugging-port` is far more reliable than clicking pixels.
- **VSCode**: `code` CLI, plus Electron exposes a good AT-SPI tree; a companion extension gives full control.
- **Godot 4.6**: `--headless`, `--script`, and an EditorPlugin can expose a control socket.
- **Blender 5.0**: `--python` / `--python-expr`; the standard pattern is a small addon holding a socket (this machine's operator already uses a Blender-MCP addon of that shape).

---
# ROUND 2 — additional verified findings

## sudo is INTERACTIVE (`sudo -n true` fails)
Codex under `--full-auto` **cannot install apt packages**. Core must need zero new system packages;
anything requiring apt goes in a documented manual step and must degrade gracefully, never hard-fail on import.

## Pure-Python D-Bus works — the `gi` trap is solved
`jeepney==0.9.0` installs and imports fine on the **mise python 3.11.15** (verified in a uv venv).
No PyGObject, no `--system-site-packages`, no interpreter pinning needed.

## ScreenCast portal handshake VERIFIED (executed, no dialog triggered)
```
ScreenCast.version            = 5
ScreenCast.AvailableSourceTypes = 7   # MONITOR|WINDOW|VIRTUAL  (all three)
ScreenCast.AvailableCursorModes = 7   # HIDDEN|EMBEDDED|METADATA (all three)
CreateSession -> /org/freedesktop/portal/desktop/request/1_228/cu_...
```
- `EMBEDDED` cursor mode is available → captures can include the pointer (agent sees its own cursor).
- `VIRTUAL` source type is available → a virtual monitor is possible.
- Only `Start()` prompts the user. `CreateSession`/`SelectSources` are silent.
- Working reference: `portal_probe.py` in this scratchpad.

## Capture transport
- ffmpeg 8.0.1 has **no `pipewiregrab`**; `x11grab` is XWayland-only; `kmsgrab` needs caps on root:video `/dev/dri/card1`.
- **`gst-launch-1.0` + `pipewiresrc` (rank primary+1) IS available**, `gstreamer1.0-pipewire` installed.
  → chain is: portal → PipeWire node id → `pipewiresrc`.

## Proven reference implementations in this scratchpad
- `uinput_probe.py`  — creates/destroys a virtual ABS pointer + keyboard via pure `ctypes`. Ran clean.
- `portal_probe.py`  — jeepney ScreenCast portal handshake. Ran clean.

## App adapter paths — VERIFIED by execution
| App | Probe | Result |
|---|---|---|
| Chrome | `--headless=new --remote-debugging-port=9333`, then `GET /json/version` | **WORKS** — Chrome/151.0.7922.173, CDP protocol 1.3, websocket URL returned |
| Godot | `godot --headless --script probe.gd` (SceneTree `_init`) | **WORKS** — 4.6.2-stable, `DisplayServer.get_name()=headless`, clean exit 0 |
| VSCode | `code --version` | present, 1.134.0 |
| Blender | — | **NOT INSTALLED**, needs `sudo apt install blender` (5.0.1) → manual user step |

Godot probe reference: `godot-probe/probe.gd` in this scratchpad.

---
# ROUND 3 — CORRECTION to Round 1 (important)

Round 1 said capture "must" go through the XDG portal with a consent dialog. **That was too strong.**

`org.gnome.Mutter.ScreenCast` is a SEPARATE bus name from the denied `org.gnome.Shell.Screenshot`,
and it is **reachable by the session user with NO consent dialog**. Independently verified:

```
$ busctl --user call org.gnome.Mutter.ScreenCast /org/gnome/Mutter/ScreenCast \
        org.gnome.Mutter.ScreenCast CreateSession "a{sv}" 0
o "/org/gnome/Mutter/ScreenCast/Session/u10"          # exit 0, no prompt
```
`org.gnome.Mutter.RemoteDesktop` is likewise reachable (`.CreateSession` present) — that is
GNOME's own input-injection API, also unprompted.

A real 1920x1080 frame was captured through this path (`frame.png`: std dev 33.1, 1417 unique
colors sampled → genuine desktop content, not a blank buffer).

## Revised capture/input matrix
| Path | Consent | Portability | Notes |
|---|---|---|---|
| **`org.gnome.Mutter.ScreenCast`** | **none** | GNOME only, private API | fastest path, no dialog; may change across GNOME versions |
| XDG ScreenCast portal | one-time (`persist_mode=2` + restore_token) | cross-desktop standard | the portable, supported option |
| `org.gnome.Shell.Screenshot` | — | — | **DENIED on GNOME 50, do not use** |

| Input path | Consent | Portability |
|---|---|---|
| **uinput via ctypes** | none | Wayland + X11, compositor-independent — **preferred** |
| `org.gnome.Mutter.RemoteDesktop` | none | GNOME only |
| RemoteDesktop portal + libei | one-time | cross-desktop |

**Design implication:** do NOT hard-code one path. Implement capture as a strategy with runtime
probing, preferring Mutter ScreenCast on GNOME for zero friction and falling back to the XDG
portal for portability/other desktops. Keep uinput as the input path since it is the most portable.

---
# ROUND 4 — FULL END-TO-END VALIDATION (the foundation is proven)

Ran a closed-loop test: **inject pointer motion via uinput → capture the screen → find the cursor.**

```
uinput ABS pointer  ->  move to (400,300)  -> capture DP-2  (frame A)
                    ->  move to (1300,800) -> capture DP-2  (frame B)
diff(A,B) > 40:  832 changed px total
   125 px within 48px of (400,300)    <- cursor glyph REMOVED from old position
   129 px within 48px of (1300,800)   <- cursor glyph DRAWN at new position
   578 px elsewhere                    <- unrelated desktop activity (clock etc.)
```
**PASS.** uinput injection genuinely reaches the compositor and moves the real pointer, and
Mutter ScreenCast + `pipewiresrc` captures it with the cursor embedded. Both halves of the
tool are validated together, with no consent dialog anywhere in the loop.

Reference implementations (working, extend these rather than starting over):
- `e2e_probe.py`    — uinput ABS pointer class (ctypes, context-managed, guaranteed teardown) + the diff assertion
- `capture_once.py` — Mutter ScreenCast → PipeWire node id → `gst-launch-1.0 pipewiresrc` → PNG

## MULTI-MONITOR — a coordinate-space trap Codex must handle
`org.gnome.Mutter.DisplayConfig.GetCurrentState` reports:
```
DP-2   1920x1080@60  logical (0,0)     scale 1.0
HDMI-1 1920x1080@60  logical (1920,0)  scale 1.0
```
The desktop is **3840x1080 across two monitors**, but a *capture* of DP-2 is only 1920x1080.
uinput ABS axes (0..32767) map across the **entire 3840-wide desktop**, NOT one monitor.
=> `abs_x = desktop_px / 3840 * 32767`. Confusing monitor-local with desktop-global coordinates
is the #1 bug risk here. The API must be explicit about which space every coordinate is in, and
screenshots must carry their monitor origin + size so the agent can convert.

---
# ROUND 5 — AT-SPI works, but with a HARD Wayland limitation

AT-SPI is reachable **even though `toolkit-accessibility` is `false`** (that gsetting only gates
legacy GTK3 bridging; modern GTK4/Electron apps always bridge). 12 apps were enumerated:

```
gnome-shell   Main stage                          window   197x56+0+0
Brave Browser OpenTech-Lab/computer-use-linux …   frame   1498x1015+0+0
code          screen2.png - godot-3d-mmorpg-game  frame   1702x943+0+0
code          computer-use-linux - VS Code        frame   1330x761+0+0
gnome-text-editor / openusage / org.gnome.Terminal / mutter-x11-frames …
```

## What AT-SPI GIVES you
- Application list, window titles, roles, **window sizes**, and the full semantic widget tree
- VSCode (`code`) and browsers bridge properly → good adapter surface
- No consent, no gsettings change required

## What AT-SPI DOES NOT give you — **every window reports origin `+0+0`**
On Wayland a client is never told its own absolute position, so `Atspi.Component.get_extents(SCREEN)`
returns correct **width/height** but a meaningless **x/y of 0,0** for every window. The uniformity
across all 10 windows on a 3840x1080 two-monitor desktop confirms this is "unsupported", not real data.

**Therefore: you CANNOT derive click coordinates from AT-SPI extents on Wayland.**
And `org.gnome.Shell.Introspect.GetWindows` (which would have given real positions) is DENIED.

## Consequences the design must absorb
1. Absolute click targeting must come from the **captured pixels**, not from window geometry —
   i.e. screenshot + vision/template-matching, or an app-native API.
2. This is a strong argument for making **Tier-2 app adapters the primary path**, not a nice-to-have:
   CDP gives real element boxes in the browser; Godot/Blender scripting bypasses coordinates entirely.
3. AT-SPI remains valuable for *what exists* (titles, roles, tree, focus, actions) and for invoking
   actions via `Atspi.Action.do_action()`, which needs no coordinates at all — prefer that over clicking.
4. Window *raising/focusing* also cannot be done via AT-SPI position; use keyboard (Alt+Tab / Super),
   `gtk-launch`/app activation, or per-app CLI.
