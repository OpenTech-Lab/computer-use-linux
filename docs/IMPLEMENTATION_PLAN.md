# computer-use-linux — Implementation Plan

**Target repo:** `/home/toyofumi/projects/computer-use-linux` (greenfield; contains only `README.md`)
**Executor:** Codex `gpt-5.6-luna`, `codex exec --full-auto`, reasoning effort max.
**Status of this document:** every environment claim below was verified by executing a probe on the
target machine on 2026-09-06. Where a claim is *unverified*, it is explicitly labelled OPEN.

**Current implementation status:** Phases 0–6 are committed at `fce5759`. The current working
tree also implements the virtual-pointer surface, X11/Xvfb backends, and installation docs; this
document remains the historical design record for those phases. The XDG portal and portable
uinput backend are still not implemented.

The original Phase 5 acceptance text below predates the current backend implementation. Use
`docs/INSTALL.md`, `docs/COORDINATES.md`, and `docs/ARCHITECTURE.md` for the current contracts and
capability-gated behavior.

---

## 0. Read this first: corrections to the prior recon

A previous recon pass (`RECON.md`) drove several design assumptions that turned out to be **wrong**.
Do not design around `RECON.md`. Design around this section.

| Prior claim | Reality (verified by execution) |
|---|---|
| Capture needs the **ScreenCast portal** with `persist_mode=2` + a persisted `restore_token`, and a one-time human consent dialog | **False.** `org.gnome.Mutter.ScreenCast` (Mutter's own D-Bus API, version 4) is callable by any session-bus client with **zero consent, zero dialog, zero token**. Verified end to end: created a session, `RecordMonitor("HDMI-1")`, `Start`, got PipeWire node 89, pulled a real 1920x1080 PNG of the live desktop. |
| Input must go through **`/dev/uinput`** | **Superseded.** `org.gnome.Mutter.RemoteDesktop` (version 1) is likewise callable with **zero consent** and is strictly better here — see §2. uinput still works and stays as the *portable* backend, but it is not the primary path. |
| AT-SPI requires `toolkit-accessibility=true` | **False.** With that gsetting still `false`, AT-SPI enumerated 12 live applications (Brave, VSCode, gnome-text-editor, Terminal…) including window titles, roles and active-state. |
| Blender is **not installed** | **False.** Blender **5.1.2** is at `/opt/blender/blender` (not on `PATH`). |
| — (not mentioned) | **Dual monitor.** `DP-2` 1920x1080 at `+0+0`, `HDMI-1` 1920x1080 at `+1920+0` (primary). Both scale `1.0`. Logical desktop 3840x1080. |
| — (not mentioned) | **`org.gnome.Mutter.DisplayConfig.GetCurrentState` works.** Full monitor layout, connector names, modes and scale factors, no consent. This replaces the denied `org.gnome.Shell.Introspect` for *monitor* geometry. |
| — (not mentioned) | **The active keyboard layout is Japanese.** `org.gnome.desktop.input-sources sources = [('xkb','jp'), ('xkb','us'), ('ibus','mozc-jp'), ('ibus','chewing')]`, `current = 0` → `xkb:jp::jpn`. Naive **keycode** injection produces wrong ASCII on a jp106 layout. This is a correctness bug waiting to happen; §2 eliminates it. |
| `/usr/bin/python3` "has gi + dbus" (implied: ready to use) | Partly. It has `gi` 3.56.2, `dbus`, PIL 12.1.1 — but **no numpy**, and it is **PEP 668 externally-managed** (`/usr/lib/python3.14/EXTERNALLY-MANAGED`). §1 gives the verified fix. |

**Confirmed from the prior recon** (re-verified, still true): GNOME Shell 50.1 on Wayland, Ubuntu 26.04,
kernel 7.0.0-15; `org.gnome.Shell.Screenshot.Screenshot` → `AccessDenied`;
`org.gnome.Shell.Introspect.GetWindows` → `AccessDenied`; `org.gnome.Shell.Eval` → disabled (`(false, '')`);
`/dev/uinput` writable by `toyofumi` via ACL; portal versions ScreenCast 5 / RemoteDesktop 2 / Screenshot 2;
xdotool, wmctrl, ydotool, grim, Xvfb, cage, weston all absent.

### New hard constraints discovered

1. **`RecordWindow` is unusable.** Its signature is `RecordWindow(a{sv} properties) → o` — the window is
   selected by a `window-id` property, and the only source of mutter window ids is
   `org.gnome.Shell.Introspect.GetWindows`, which is **denied**. Per-window capture therefore requires
   either a bespoke GNOME Shell extension (Phase 7) or `RecordArea` over a computed rectangle.
   **Do not attempt `RecordWindow` before Phase 7.**
2. **AT-SPI reports every window position as `(0, 0)`.** Verified: sizes and titles are correct and
   distinct (1498x1015, 1702x943, 820x563…), positions are uniformly zero — Wayland clients do not know
   their global position. AT-SPI is a source of *identity, size, role, state and actions*, **never of
   screen position**.
3. **Session lifetime is bound to the D-Bus connection.** A Mutter ScreenCast/RemoteDesktop session is
   destroyed the moment the creating client's connection drops. Verified: `gdbus call` created a session
   and it vanished before the next command. This is a *feature* — see §7 (kill switch) — but it means the
   daemon must hold one long-lived `Gio.DBusConnection` for the whole session.

---

## 1. Language, interpreter and packaging — non-negotiable

**Language: Python 3.14.** Justification: the entire critical path is GLib/GObject —  D-Bus with a main
loop, GStreamer, AT-SPI. PyGObject (`gi`) is the only mature binding, it is already installed
system-wide as a distro C extension, and the MCP Python SDK (2.1.1) installs cleanly alongside it. Rust
(`zbus` + `ashpd` + `pipewire-rs`) would buy nothing: the hot path is GStreamer's C code either way, and
it would forfeit `gi`'s AT-SPI binding.

**The interpreter trap — this will silently break the build if ignored.** The default `python3` on
`PATH` is **mise 3.11.15**, which has PIL and numpy but **cannot `import gi`**. `gi` is a Debian C
extension built for 3.14 and cannot be pip-installed. The project **must** be built on
`/usr/bin/python3.14` with system site-packages visible.

Verified working bootstrap (this exact sequence was executed and confirmed):

```bash
uv venv --python /usr/bin/python3.14 --system-site-packages .venv
VIRTUAL_ENV=.venv uv pip install numpy mcp pillow
.venv/bin/python -c "import gi, numpy, mcp, PIL; print(gi.__version__, numpy.__version__)"
# -> 3.56.2 2.5.2
```

Put this in `scripts/bootstrap.sh` and make `make setup` call it. Add a hard guard in
`src/computer_use_linux/__init__.py`:

```python
import sys
if sys.version_info < (3, 14):
    raise RuntimeError(
        "computer-use-linux requires the system CPython 3.14 (/usr/bin/python3.14) with "
        "--system-site-packages; PyGObject cannot be installed from PyPI. Run scripts/bootstrap.sh."
    )
```

**Do not add a `.python-version` file** — mise would hijack it. Pin the interpreter in
`scripts/bootstrap.sh` and in `pyproject.toml` (`requires-python = ">=3.14"`).

### APT packages to install (`scripts/install-deps.sh`, idempotent, prompts before `sudo`)

| Package | Why | State |
|---|---|---|
| `gir1.2-gst-plugins-base-1.0` | `gi.require_version("GstApp", "1.0")` for `appsink` — **required**, currently missing | install |
| `gstreamer1.0-pipewire` | `pipewiresrc` | already installed (1.6.2) |
| `python3-gi`, `gir1.2-atspi-2.0` | `gi`, AT-SPI | already installed |
| `xvfb`, `x11-utils`, `x11-xserver-utils` | CI / nested backend (Phase 5) | install |
| `cage` | nested Wayland compositor for deterministic tests (Phase 5) | install |
| `wl-clipboard` | clipboard fallback outside GNOME | install |

PyPI (into `.venv`): `mcp>=2.1`, `numpy`, `pillow`, `evdev` (uinput backend), `python-xlib` (x11 backend),
`websockets` + `httpx` (CDP adapter), `pytest`, `pytest-asyncio`, `ruff`.

---

## 2. The core architectural decision: Mutter RemoteDesktop as the primary input path

This is the single most important design choice, and it resolves three problems at once.

**Verified behaviour.** A `org.gnome.Mutter.RemoteDesktop` session, with a
`org.gnome.Mutter.ScreenCast` session bound to it via the `remote-desktop-session-id` property, provides:

- `NotifyKeyboardKeysym(u keysym, b pressed)` — **injection by X keysym, not keycode.** Mutter performs
  the keysym→keycode mapping itself. This makes text entry **completely immune to the active xkb layout**,
  which on this machine is Japanese. Verified OK.
- `NotifyPointerMotionAbsolute(s stream_path, d x, d y)` — coordinates are **relative to the named
  screencast stream**. Verified with a decisive test: requested `(400, 300)` on the `DP-2` stream, captured
  a frame with the cursor embedded, and the cursor hotspot landed at **(400, 299)** in that frame's own
  pixel space. **The screenshot's coordinate space and the input coordinate space are the same space.**
- `NotifyPointerButton`, `NotifyPointerAxis`, `NotifyPointerAxisDiscrete`, `NotifyTouch*`,
  `EnableClipboard`/`SetSelection`/`SelectionWrite` (clipboard verified enabled — must be called
  **before** `Session.Start`), `ConnectToEIS`, `SetKeymap`.

**Why this beats uinput here:**

| | Mutter RemoteDesktop | uinput |
|---|---|---|
| Text on a jp layout | Correct — keysym-based | **Wrong** — keycodes go through the jp106 layout |
| Multi-monitor coordinates | Stream-relative; no offset math | Global ABS range; must map monitor offsets by hand |
| Stuck modifier on crash | Impossible — mutter drops all state when the connection closes | **Real risk** — device outlives the process holding a key down |
| Consent | None | None |
| Portability | GNOME only | Any compositor, and X11 |

uinput therefore stays in the codebase as the **portable** input backend (sway, KDE, non-GNOME, and
nested compositors), not as the primary.

**The exact D-Bus call order matters and is easy to get wrong.** Two orderings were tried and failed.
This is the verified-correct sequence:

```python
# 1. RemoteDesktop session first, and read its SessionId property
rd_path = call(RD, "/org/gnome/Mutter/RemoteDesktop", RD, "CreateSession")
session_id = get_prop(RD, rd_path, RD + ".Session", "SessionId")

# 2. ScreenCast session BOUND to it
sc_path = call(SC, "/org/gnome/Mutter/ScreenCast", SC, "CreateSession",
               GLib.Variant("(a{sv})", ({"remote-desktop-session-id": GLib.Variant("s", session_id),
                                         "disable-animations":       GLib.Variant("b", True)},)))

# 3. Add streams BEFORE starting
stream_path = call(SC, sc_path, SC + ".Session", "RecordMonitor",
                   GLib.Variant("(sa{sv})", (connector, {"cursor-mode": GLib.Variant("u", 1)})))

# 4. Subscribe to Stream.PipeWireStreamAdded to learn the PipeWire node id
# 5. Clipboard, if wanted, must be enabled BEFORE Start
call(RD, rd_path, RD + ".Session", "EnableClipboard", ...)

# 6. Start the REMOTE DESKTOP session ONLY.
#    Calling ScreenCast.Session.Start on a bound session fails with
#    "Must be started from remote desktop session". Starting RD starts both.
call(RD, rd_path, RD + ".Session", "Start")
```

`cursor-mode`: `0` = hidden, `1` = embedded in the frame, `2` = metadata only. Default to **1** — an
agent that can see the cursor debugs its own misclicks.

**Frame pull.** The stream's PipeWire node is consumed with GStreamer:
`pipewiresrc path=<node> always-copy=true ! videoconvert ! video/x-raw,format=RGBx ! appsink name=sink max-buffers=1 drop=true sync=false`.
Verified working via a `gst-launch-1.0` subprocess producing a valid 1920x1080 PNG; the in-process
`appsink` variant needs `gir1.2-gst-plugins-base-1.0` (see §1). Keep the pipeline alive across
screenshots — do **not** rebuild it per call; latency matters and PipeWire renegotiation is slow.

---

## 3. Architecture

### 3.1 Layering

```
  MCP server  (mcp/server.py)          CLI  (cli.py)
                    \                  /
                     v                v
                    Session  (session.py)
        holds: backend, surfaces, press-state, safety policy, adapters
                     |
        +------------+--------------+---------------+
        v            v              v               v
   CaptureBackend  InputBackend  WindowSource   AppAdapter registry
        |            |              |               |
  gnome_mutter   gnome_mutter    atspi          browser_cdp
  portal         uinput          (+ shell ext,  vscode / godot
  x11            x11              Phase 7)      blender / atspi_generic
  headless       portal
```

Capture and input are **separate protocols** deliberately: a valid configuration is *portal capture +
uinput input* on a non-GNOME compositor. `gnome_mutter` is the one backend that implements both and
requires them bound to a single session; model that as a backend that advertises
`Capability.BOUND_CAPTURE_INPUT` and exposes both protocols from one object.

### 3.2 Core types (`types.py`)

```python
@dataclass(frozen=True)
class Surface:                 # one capture stream = one coordinate space
    id: str                    # "monitor:DP-2", "area:0,0,800,600", "virtual:0"
    kind: Literal["monitor", "area", "virtual", "window"]
    width: int                 # native pixels of the stream
    height: int
    origin: tuple[int, int]    # position in the compositor's logical desktop, for reference only
    scale: float               # compositor scale factor of the underlying monitor
    label: str                 # "DP-2 (1920x1080)"

@dataclass
class Frame:
    surface_id: str
    width: int                 # native
    height: int
    data: np.ndarray           # HxWx3 uint8 RGB
    captured_at: float

@dataclass(frozen=True)
class WindowInfo:
    id: str                    # stable synthetic id
    title: str
    app: str                   # AT-SPI application name
    role: str
    width: int | None          # from AT-SPI; reliable
    height: int | None
    position: tuple[int, int] | None   # ALWAYS None on Wayland/AT-SPI. Do not fake it.
    active: bool
    surface_id: str | None     # best-effort monitor attribution, may be None

class Capability(enum.Flag):
    CAPTURE = auto(); INPUT_POINTER = auto(); INPUT_KEYBOARD = auto()
    CLIPBOARD = auto(); WINDOW_LIST = auto(); WINDOW_FOCUS = auto()
    WINDOW_GEOMETRY = auto()       # position known — false on the Wayland backend
    VIRTUAL_SURFACE = auto()       # RecordVirtual / Xvfb
    BOUND_CAPTURE_INPUT = auto()
```

### 3.3 Backend protocols (`backends/base.py`)

```python
class CaptureBackend(Protocol):
    name: str
    capabilities: Capability
    def list_surfaces(self) -> list[Surface]: ...
    def open_surface(self, spec: SurfaceSpec) -> Surface: ...   # monitor / area / virtual
    def close_surface(self, surface_id: str) -> None: ...
    def grab(self, surface_id: str, *, timeout: float = 2.0) -> Frame: ...

class InputBackend(Protocol):
    name: str
    capabilities: Capability
    def move(self, surface_id: str, x: float, y: float) -> None: ...
    def button(self, button: int, pressed: bool) -> None: ...       # 1=L 2=M 3=R (BTN_* mapped internally)
    def scroll(self, dx: float, dy: float, *, discrete: bool = True) -> None: ...
    def keysym(self, keysym: int, pressed: bool) -> None: ...
    def held(self) -> HeldState: ...                                # keysyms + buttons currently down
    def release_all(self) -> None: ...                              # MUST be idempotent and never raise

class WindowSource(Protocol):
    def list_windows(self) -> list[WindowInfo]: ...
    def focus(self, window_id: str) -> bool: ...
    def tree(self, window_id: str, max_depth: int = 6) -> dict: ...  # AT-SPI semantic tree
```

`backends/__init__.py` exposes `detect() -> Backend`, ordered:

1. `gnome_mutter` — if `XDG_SESSION_TYPE=wayland` **and** `org.gnome.Mutter.ScreenCast` is
   name-owned **and** `CreateSession` succeeds. (Probe it; do not infer from `XDG_CURRENT_DESKTOP`.)
2. `x11` — if `XDG_SESSION_TYPE=x11` or `$DISPLAY` is set and XTEST is present.
3. `portal` — Wayland, non-GNOME, or Mutter's private API refused. Requires consent; caches the
   `restore_token` in `~/.local/state/computer-use-linux/portal-token`.
4. `headless` — explicit `--backend headless`; never auto-selected.

Selection is overridable with `CUL_BACKEND=<name>` and `--backend`.

---

## 4. Coordinates — pin this down or everything else is guesswork

The brief correctly identifies this as the #1 source of bugs in computer-use tools. The verified
stream-relative behaviour of `NotifyPointerMotionAbsolute` lets us make it almost trivial.

**Rules, to be implemented in `coords.py` and documented in `docs/COORDINATES.md`:**

1. **There is no global desktop coordinate space in the public API.** Ever. `Surface.origin` exists for
   diagnostics and is never accepted as input. A dual-monitor desktop is two surfaces, not one 3840-wide
   plane. This is what removes the whole class of "clicked on the wrong monitor" bugs.
2. **Every coordinate is `(surface_id, x, y)`.** If `surface_id` is omitted it defaults to the session's
   *active surface* (the primary monitor at startup; settable via `select_surface`).
3. **The model clicks in the pixel space of the image it was shown.** `screenshot` may downscale for
   token cost (`max_width`, default 1280). When it does, the `Session` records
   `surface.last_image_scale = image_width / surface.width`. Input tools default to
   `coord_space="image"` and divide by that factor before calling the backend. `coord_space="surface"`
   opts into native pixels. A screenshot must be taken before an image-space click; if none has been,
   scale is 1.0 and the two spaces coincide.
4. **Every screenshot response states its own geometry** — `image_width`, `image_height`,
   `surface_width`, `surface_height`, `scale`, `coord_space` — so a stateless or restarted agent can
   always recompute. Never rely solely on server-side memory.
5. **HiDPI:** `Surface.scale` comes from `Mutter.DisplayConfig.GetCurrentState`. The screencast stream is
   in *physical* pixels; input coordinates are in that same stream space. Therefore **the scale factor
   never enters the arithmetic** — it is reported for the agent's benefit only. Both monitors here are
   scale 1.0, so this path is currently untestable on real hardware; cover it with a unit test over a
   synthetic 2.0-scale `Surface`.
6. Rounding: `round()` at the boundary, once, in `coords.py`. Never in call sites.

Acceptance test for this section is the cursor round-trip in Phase 1 — it is not optional.

---

## 5. App adapters — the reliable tier

Pixel control is the universal fallback and it is unreliable. Where a real API exists, use it.

```python
class AppAdapter(Protocol):
    name: str                                  # "browser", "blender", "godot", "vscode"
    def detect(self) -> bool: ...              # is a controllable instance reachable right now?
    def launch(self, **kw) -> None: ...        # start one we control
    def actions(self) -> list[ActionSpec]: ... # name, JSON schema, description
    def invoke(self, action: str, **kw) -> Any: ...
    def surface_hint(self) -> str | None: ...  # which surface its window is on, if knowable
```

Adapters register in `adapters/__init__.py` via a decorator; the MCP server enumerates them at startup
and exposes each as `app_<name>_<action>`. **Adding an app must never require touching core.**

| App | Version on box | Mechanism |
|---|---|---|
| Browser | Brave 151.1.93.137, Chrome 151.0.7922.173, Firefox 154 | **Chrome DevTools Protocol.** Launch `brave-browser --remote-debugging-port=9222 --user-data-dir=~/.local/state/computer-use-linux/browser-profile`. Talk HTTP `/json` + WebSocket. Actions: `navigate`, `eval`, `click_selector`, `text`, `screenshot`, `wait_for`. Nothing is listening on 9222 today — we always launch our own instance rather than adopting the user's. Firefox falls back to the pixel tier. |
| Blender | **5.1.2 at `/opt/blender/blender`** | Launch with `--python <extras/blender-addon/cul_bridge.py>`; the addon opens an owner-only Unix socket under `XDG_RUNTIME_DIR`, verifies `SO_PEERCRED`, and requires a per-launch token before dispatching on Blender's main thread via `bpy.app.timers`. Actions: `run_python`, `scene_info`, `viewport_screenshot`. |
| Godot | 4.6.2.stable at `~/.local/bin/godot` | Two modes: headless (`godot --headless --script res://...`) for scripted runs, and an `EditorPlugin` in `extras/godot-plugin/addons/cul_bridge/` opening an explicitly loopback TCP socket with a per-launch token stored in a verified `0600` state file. Actions: `run_scene`, `eval_gdscript`, `editor_command`. |
| VSCode | 1.134.0 | `code` CLI for open/goto/diff; AT-SPI for the live tree (verified: `code` exposes two frames with correct titles). A companion extension (`extras/vscode-extension/`) listening on a unix socket for `vscode.commands.executeCommand` is the full-control path — **Phase 6, not earlier.** |
| Generic GTK/Qt | — | `adapters/atspi_generic.py`: enumerate, focus, read text, invoke AT-SPI actions. Chromium-family apps need `--force-renderer-accessibility` for a full render tree; add it to our managed browser launch. |

---

## 6. Agent-facing surface

**Decision: an MCP server (stdio) as the primary surface, plus a CLI that shares the same `Session`.**
MCP is how Claude Code and Codex integrate natively; the CLI exists for scripting, for the acceptance
checks in this plan, and because every acceptance check must be runnable without an agent in the loop.

Entry points in `pyproject.toml`:
`cul = computer_use_linux.cli:main`, `cul-mcp = computer_use_linux.mcp.server:main`.

### Tools

| Tool | Params | Returns |
|---|---|---|
| `screenshot` | `surface_id?`, `region?` `{x,y,w,h}`, `max_width?=1280`, `cursor?=true` | `ImageContent` (base64 PNG) **+** `TextContent` JSON: `{surface_id, image_width, image_height, surface_width, surface_height, scale, coord_space:"image", captured_at}` |
| `list_surfaces` | — | JSON array of `Surface` |
| `select_surface` | `surface_id` | ok |
| `move` | `x`, `y`, `surface_id?`, `coord_space?` | ok |
| `click` | `x`, `y`, `button?="left"`, `count?=1`, `modifiers?=[]`, `surface_id?`, `coord_space?` | ok |
| `drag` | `from_x`, `from_y`, `to_x`, `to_y`, `button?`, `steps?=20`, `surface_id?` | ok |
| `scroll` | `x`, `y`, `dx?=0`, `dy`, `surface_id?` | ok |
| `key` | `keys: str \| list[str]` — chords like `"ctrl+shift+p"`, `"Return"`, `"super"` | ok |
| `type_text` | `text`, `mode?="auto"` (`auto` \| `keysym` \| `clipboard`) | ok |
| `list_windows` | — | JSON array of `WindowInfo`; `position` is `null` and the response carries `"position_available": false` with a one-line reason |
| `focus_window` | `window_id` | ok |
| `window_tree` | `window_id`, `max_depth?=6` | AT-SPI semantic tree JSON |
| `wait_for_change` | `surface_id?`, `timeout?=5.0`, `threshold?=0.002` | `{changed: bool, elapsed: float}` — mean-abs-diff on downscaled greyscale |
| `panic` | — | releases all input, tears down the session |
| `app_<name>_<action>` | per adapter | per adapter |

**Screenshot return format matters.** Return real `ImageContent` with `mimeType="image/png"` — never a
file path, never a data URI inside text. Default `max_width=1280` keeps a 1920-wide monitor around
~450KB of base64; expose `max_width` so an agent can ask for native resolution when reading small text.

**`type_text` mode selection** (`auto`, the default):
- ≤ 40 chars, pure ASCII → `keysym`, one `NotifyKeyboardKeysym` press/release pair per character.
- longer, or containing non-ASCII → `clipboard`: `EnableClipboard` + `SetSelection`, then synthesise
  `ctrl+v`. Faster and immune to both layout and IME state.
- On the uinput backend, `keysym` is unavailable; `auto` must fall back to clipboard + a loud warning in
  the tool result, because keycode typing on the jp layout **will** produce wrong characters.

---

## 7. Safety

This drives the user's real desktop. Treat every item here as a requirement, not a nicety.

1. **Kill switch — three independent mechanisms, all required.**
   - `panic` MCP tool and `cul panic` CLI command.
   - **Process death is a complete kill switch, by construction.** Mutter destroys the RemoteDesktop
     session when the D-Bus connection drops, releasing every held key and button. `pkill -f cul-mcp`
     is therefore *guaranteed* safe on the GNOME backend. Document this prominently in `README.md` and
     `docs/SAFETY.md` — it is the single most valuable safety property of this design.
   - A filesystem trip-wire: the session polls `~/.config/computer-use-linux/PANIC` every 250 ms; if it
     exists, `release_all()` and refuse all further input until it is removed. This is the mechanism a
     panicking human can reach from a second terminal. A global hotkey is **not** achievable on Wayland
     without a shell extension — do not promise one before Phase 7.
2. **Held-state tracking is mandatory.** `InputBackend.held()` returns every keysym and button currently
   down. `release_all()` is invoked from `atexit`, from `signal` handlers for SIGINT/SIGTERM/SIGHUP, from
   every `except` path in the tool dispatcher, and from a watchdog that fires if any key has been held
   for more than `max_hold_seconds` (default 5). The uinput backend additionally issues `UI_DEV_DESTROY`
   in a `finally`. A stuck virtual modifier is the worst failure mode this tool has; it gets four
   independent guards.
3. **Sensitive-window detection.** Before returning any frame, check the AT-SPI tree for a focused
   window belonging to `gcr-prompter`, `polkit-gnome-authentication-agent`, `xdg-desktop-portal-gtk`,
   `gnome-shell` modal prompts, or any window containing a widget with role `password text`. If found:
   redact the whole window region if its bounds are known, otherwise **refuse the screenshot** and return
   an explanatory error. Never silently return a frame containing a password prompt.
4. **Static redaction.** `config.py` accepts `redact_regions: list[{surface_id, x, y, w, h}]` and
   `redact_window_titles: list[regex]`; matched areas are filled with solid black **before** encoding,
   never after. Redaction happens in `safety.py`, on the numpy array, in one place.
5. **Confirmation gating.** `config.confirm_mode` ∈ `off` | `destructive` | `all`. In `destructive`
   (the default), these require an explicit `confirm=true` argument on the tool call, and otherwise
   return a structured refusal describing what would happen: `key` chords containing `super`, `alt+F4`,
   `ctrl+alt+*`, or `Delete`; any `type_text` while the focused window matches
   `redact_window_titles`; any `app_*_run_python` / `eval` action. This is deliberately a small,
   auditable list — not a heuristic classifier.
6. **Never adopt the user's browser profile.** The CDP adapter always launches with its own
   `--user-data-dir`, so automation cannot reach the user's logged-in sessions and cookies.
7. **Logging.** Every input action logs to `~/.local/state/computer-use-linux/actions.jsonl`
   (timestamp, tool, args with `type_text` content redacted to a length, surface, resulting held-state).
   Screenshots are not logged to disk by default.

---

## 8. Repo tree

```
computer-use-linux/
├── README.md                       # rewrite: what it is, quickstart, the pkill kill-switch
├── pyproject.toml                  # requires-python = ">=3.14"; entry points cul, cul-mcp
├── Makefile                        # setup / test / test-unit / test-integration / lint / doctor
├── scripts/
│   ├── bootstrap.sh                # uv venv --python /usr/bin/python3.14 --system-site-packages
│   └── install-deps.sh             # apt packages from §1
├── src/computer_use_linux/
│   ├── __init__.py                 # 3.14 guard
│   ├── __main__.py
│   ├── cli.py                      # argparse; subcommands mirror the MCP tools
│   ├── config.py                   # ~/.config/computer-use-linux/config.toml + CUL_* env
│   ├── errors.py                   # BackendUnavailable, SurfaceNotFound, SafetyRefusal, CaptureTimeout
│   ├── logging.py
│   ├── types.py                    # §3.2
│   ├── coords.py                   # §4 transforms — pure, fully unit-tested
│   ├── keys.py                     # name→keysym table, chord parser, text→keysym sequence
│   ├── session.py                  # lifecycle, surfaces, held-state, watchdog, panic file poll
│   ├── safety.py                   # redaction, sensitive-window detection, confirm gating
│   ├── backends/
│   │   ├── __init__.py             # registry + detect()
│   │   ├── base.py                 # protocols from §3.3
│   │   ├── gnome_mutter.py         # PRIMARY — the §2 sequence
│   │   ├── portal.py               # xdg-desktop-portal + restore_token
│   │   ├── uinput.py               # evdev; portable input
│   │   ├── x11.py                  # XTEST + XGetImage via python-xlib
│   │   └── headless.py             # RecordVirtual, and cage/Xvfb spawning
│   ├── pipewire/
│   │   ├── __init__.py
│   │   └── gst_capture.py          # persistent pipewiresrc→appsink pipeline → numpy
│   ├── windows/
│   │   ├── __init__.py
│   │   ├── atspi.py                # tree, focus, roles; NEVER returns a position
│   │   └── model.py
│   ├── adapters/
│   │   ├── __init__.py             # AppAdapter protocol + @register decorator
│   │   ├── browser_cdp.py
│   │   ├── atspi_generic.py
│   │   ├── blender.py
│   │   ├── godot.py
│   │   └── vscode.py
│   └── mcp/
│       ├── __init__.py
│       └── server.py               # tool defs from §6
├── extras/
│   ├── blender-addon/cul_bridge.py
│   ├── godot-plugin/addons/cul_bridge/{plugin.cfg,plugin.gd,bridge.gd}
│   ├── vscode-extension/           # Phase 6
│   └── gnome-extension/computer-use@local/  # Phase 7
├── tests/
│   ├── conftest.py
│   ├── unit/                       # coords, keys, safety, config — no display needed
│   ├── contract/test_backend_contract.py    # every backend passes the same suite
│   └── integration/                # real backend; marked, skipped without a session
└── docs/
    ├── ARCHITECTURE.md
    ├── COORDINATES.md              # §4, verbatim, with the cursor round-trip evidence
    ├── SAFETY.md                   # §7
    └── ENVIRONMENT.md              # §0 corrections + how to re-run the probes
```

---

## 9. Phases

Each phase ends with a **runnable** acceptance check and stated expected output. Because the Mutter
path needs no consent, **Phases 1–3 are fully automatable with no human present** — a significant
improvement over the portal-based design the recon implied.

### Phase 0 — Scaffold and doctor
Repo skeleton, `pyproject.toml`, `scripts/bootstrap.sh`, `scripts/install-deps.sh`, `config.py`,
`errors.py`, `types.py`, and `cul doctor`. `doctor` probes and reports: interpreter version, `gi`/numpy/
GstApp importability, session type, `Mutter.ScreenCast` reachability, `Mutter.RemoteDesktop`
reachability, `DisplayConfig` monitor list, AT-SPI reachability, `/dev/uinput` writability, active
keyboard layout, and the missing apt packages.

**Acceptance**
```bash
make setup && .venv/bin/cul doctor
```
Expect exit 0 and a table whose rows include `python 3.14.4 OK`, `gi 3.56.2 OK`, `numpy OK`,
`GstApp OK`, `session wayland/GNOME 50.1`, `Mutter.ScreenCast v4 OK`, `Mutter.RemoteDesktop v1 OK`,
`monitors: DP-2 1920x1080@1.0 +0+0, HDMI-1 1920x1080@1.0 +1920+0`, `atspi OK (12 apps)`,
`keyboard layout: jp  [warning: keycode input unreliable, keysym path in use]`.

### Phase 1 — GNOME backend: see, click, type (the vertical slice)
`backends/gnome_mutter.py`, `pipewire/gst_capture.py`, `coords.py`, `keys.py`, `session.py`, and the
CLI subcommands `surfaces`, `shot`, `move`, `click`, `type`, `key`, `panic`.

**Acceptance A — capture**
```bash
.venv/bin/cul surfaces
.venv/bin/cul shot --surface monitor:DP-2 -o /tmp/a.png && identify /tmp/a.png
```
Expect `surfaces` to list exactly two monitor surfaces, and `identify` to report `PNG 1920x1080`.

**Acceptance B — the coordinate round-trip (the critical one)**
```bash
.venv/bin/cul selftest coords --surface monitor:DP-2
```
This must: move the pointer to a set of targets `[(400,300), (100,100), (1800,1000)]`, read the exact
cursor position from a `cursor-mode=2` PipeWire metadata stream, and assert every result is within
**2 px** of its target. It must be repeatable on an animated desktop; it must not difference frames
captured at different times. Expect `coords selftest: 3/3 within 2px  PASS` and exit 0.

The decisive multi-monitor check is:
```bash
.venv/bin/cul selftest monitors
```
It moves to a point on each of `DP-2` and `HDMI-1` and verifies that metadata appears on the target
stream and is absent from the other stream. A missing metadata capability is reported as `SKIPPED`;
an incorrect or cross-monitor observation is a non-zero failure. If metadata is unavailable for the
coordinate fallback, the fallback first checks that the screen is quiescent and reports `SKIPPED`
instead of accepting an unreliable measurement.

**Acceptance C — typing on the Japanese layout**
```bash
gnome-text-editor & sleep 2
.venv/bin/cul windows | grep gnome-text-editor
.venv/bin/cul focus-window <id>
.venv/bin/cul type 'Hello @[]:_ 123'
```
Expect the editor to contain exactly `Hello @[]:_ 123`. `@ [ ] : _` are precisely the characters a jp106
keycode mapping gets wrong, so this string is the test. Automate the readback through AT-SPI
(`windows/atspi.py` text extraction) so the check is self-verifying, not eyeballed.

**Acceptance D — kill switch**
```bash
.venv/bin/cul hold-key shift &   # deliberately holds Shift_L
sleep 1; pkill -f "cul hold-key"; sleep 1
.venv/bin/cul selftest modifiers
```
Expect `no modifiers held  PASS`.

### Phase 2 — MCP server
`mcp/server.py` over the same `Session`. All tools from §6.

**Acceptance**
```bash
.venv/bin/cul-mcp --selftest        # in-process client: list tools, call screenshot, click, type
```
Expect a tool list containing all of §6, and a `screenshot` result whose first content block has
`type=image`, `mimeType=image/png`, and whose second block parses as JSON with
`image_width == 1280`, `surface_width == 1920`, `scale == 1280/1920`. Then register with Claude Code
(`claude mcp add computer-use -- /abs/path/.venv/bin/cul-mcp`) and confirm the tools appear.

### Phase 3 — Windows and semantics
`windows/atspi.py`, `list_windows`, `focus_window`, `window_tree`, `wait_for_change`, sensitive-window
detection in `safety.py`.

**Acceptance**
```bash
.venv/bin/cul windows --json | jq '[.[] | select(.app=="code")] | length'   # -> 2
.venv/bin/cul windows --json | jq '[.[] | .position] | unique'              # -> [null]
.venv/bin/cul window-tree <code-window-id> --depth 3 | head -40
```
`position` must be uniformly `null` — an assertion that we did not invent geometry. Add a unit test
that fails if any backend ever returns a non-null position without also setting
`Capability.WINDOW_GEOMETRY`.

### Phase 4 — Browser adapter (proves the adapter interface)
`adapters/__init__.py` + `adapters/browser_cdp.py`.

**Acceptance**
```bash
.venv/bin/cul app browser launch
.venv/bin/cul app browser navigate --url https://example.com
.venv/bin/cul app browser text | grep -q "Example Domain" && echo PASS
.venv/bin/cul shot -o /tmp/b.png     # pixel tier still works on the same window
```
Expect `PASS`, and `/tmp/b.png` to show the rendered page. Launch flags must include
`--remote-debugging-port=9222`, a dedicated `--user-data-dir`, and `--force-renderer-accessibility`.

### Phase 5 — Additional backends, contract tests, CI
`backends/uinput.py`, `backends/x11.py`, `backends/headless.py`, `backends/portal.py`, and
`tests/contract/test_backend_contract.py` — one parametrised suite every backend must pass.

**Acceptance (fully headless, no display, CI-safe)**
```bash
xvfb-run -s "-screen 0 1024x768x24" .venv/bin/pytest tests/contract -v -k x11
```
Expect all contract tests green: `list_surfaces` returns one 1024x768 surface; `grab` returns a
1024x768x3 array; a click at `(512, 384)` is observed by a test GTK window at `(512, 384) ± 2`; typed
text round-trips. This is the answer to "how do you test input injection in CI" — the x11 backend under
Xvfb is deterministic and needs no compositor, no consent and no hardware.

Portal backend acceptance is the **only** step needing a human (one consent dialog); mark its test
`@pytest.mark.requires_consent` and exclude it from CI.

### Phase 6 — Godot, Blender, VSCode adapters
`extras/blender-addon/cul_bridge.py`, `extras/godot-plugin/`, `extras/vscode-extension/`.

**Acceptance**
```bash
.venv/bin/cul app blender launch --blender /opt/blender/blender
.venv/bin/cul app blender run-python --expr "import bpy; print(len(bpy.data.objects))" --confirm   # -> 3
.venv/bin/cul app godot eval --expr 'print(Engine.get_version_info().string)' --confirm      # -> 4.6.2.stable
.venv/bin/cul app vscode command --id workbench.action.showCommands
```

### Phase 7 — GNOME Shell extension (optional; unblocks window geometry)
`extras/gnome-extension/computer-use@local/` exposing a private D-Bus API that returns mutter window
ids, frame rects and monitor attribution — restoring what `org.gnome.Shell.Introspect` denies, and
unblocking `RecordWindow` and true per-window capture.

**OPEN QUESTION — do not guess.** Whether GNOME 50 loads a newly-installed extension without a session
restart is unverified, and on Wayland the shell cannot be restarted without logging out. Determine it
empirically before building on it:
```bash
gnome-extensions install --force extras/gnome-extension/computer-use@local.shell-extension.zip
gnome-extensions enable computer-use@local
gdbus call --session -d org.gnome.Shell -o /org/gnome/Shell/Extensions/ComputerUse \
  -m org.gnome.Shell.Extensions.ComputerUse.GetWindows
```
If that returns window data, live install works. If it errors, the extension requires a logout and
Phase 7 must be gated behind an explicit, documented user action. **Everything in Phases 1–6 must work
without this extension** — treat window geometry as genuinely unavailable, not as deferred.

---

## 10. Testing strategy

| Layer | What | Where it runs |
|---|---|---|
| **Unit** | `coords.py` transforms (image↔surface, downscale, synthetic 2.0-scale HiDPI, rounding at boundaries); `keys.py` chord parsing and text→keysym; `safety.py` redaction and confirm gating; `config.py` precedence | Anywhere, no display, no D-Bus. Must be the bulk of the suite. |
| **Contract** | One parametrised suite (`tests/contract/test_backend_contract.py`) that every backend must satisfy: surface enumeration, grab dimensions match `Surface`, click lands within 2 px, typed text round-trips, `release_all()` clears held state, `close_surface` is idempotent | Against `x11` under Xvfb in CI; against `gnome_mutter` locally |
| **Integration** | Real GNOME session: the Phase 1 acceptance checks, promoted into pytest and marked `@pytest.mark.requires_session` | Locally; skipped when `WAYLAND_DISPLAY` is unset |
| **Adapter** | CDP against a launched browser and `example.com`; Blender/Godot against a scripted headless launch | CI-capable — all three run headless |

**CI without a display, concretely:** `xvfb-run -s "-screen 0 1024x768x24" pytest tests/unit tests/contract`.
The x11 backend under Xvfb gives a real X server with working XTEST and XGetImage, so input injection
and capture are genuinely exercised — not mocked. The target for click assertions is a ~40-line GTK3
test window in `tests/integration/fixtures/click_target.py` that records event coordinates to stdout.

The GNOME `RecordVirtual` mode (verified callable on this box) is the *local* deterministic pointer
option: a virtual monitor is invisible to the user, so pointer tests can run without hijacking the
real pointer. The implemented `headless` backend uses a private Xvfb display for the stronger
application/window-stack isolation guarantee. Prefer either isolated path over the real monitor for
anything that clicks.

**Golden rule for this repo:** a test that asserts a *screenshot looks right* is not a test. Assert
geometry, assert the target app observed the event, assert text round-tripped.

---

## 11. Risks and fallout

| Risk | Mitigation |
|---|---|
| **Mutter's private D-Bus API is unstable across GNOME versions and is not a supported public interface.** It could be locked down like `Shell.Introspect` was. | This is the main strategic risk and the reason the backend abstraction is not over-engineering. The `portal` backend is the sanctioned fallback and must be implemented in Phase 5, not skipped. `doctor` must report which backend is active so a regression is diagnosed in seconds. |
| Integration tests hijack the real pointer and disturb the user | Default integration tests to the `headless`/`RecordVirtual` surface. Anything touching the real desktop is marked `@pytest.mark.disruptive` and excluded by default in `pyproject.toml`'s `addopts`. |
| Codex builds against mise `python3` and `import gi` fails | The 3.14 guard in `__init__.py` and `scripts/bootstrap.sh`. This is called out in §1 because it is the most likely way this build fails on the first attempt. |
| `gir1.2-gst-plugins-base-1.0` missing → no `appsink` | `doctor` checks it; `install-deps.sh` installs it. Interim fallback: the verified `gst-launch-1.0` subprocess path, which needs no typelib. |
| jp keyboard layout corrupts typed text | Keysym injection (§2) plus the Phase 1 Acceptance C test string `Hello @[]:_ 123`, chosen specifically because those glyphs move on a jp106 layout. |
| PipeWire renegotiation stalls after monitor hotplug or suspend/resume | `Stream.Closed` and `Session.Closed` signal handlers tear down and rebuild the session; `grab()` has a 2 s timeout and one automatic retry, then raises `CaptureTimeout`. |
| Screenshot base64 blows the context budget | `max_width` default 1280, PNG. Document the token cost in `README.md`. |
| Nothing to migrate | Greenfield repo — no existing callers, saved formats or docs to invalidate. The only existing file is `README.md`, which gets rewritten. |

---

## 12. Out of scope

- Video/streaming output. Single frames only. (The pipeline could do video; the agent interface should not.)
- Audio capture or injection.
- Remote/network access. stdio MCP and a local CLI only; no listening socket beyond the adapters'
  loopback control ports.
- Multi-seat, multi-user, or multiple concurrent sessions against one desktop.
- OCR, element detection, or any vision model. The agent has its own vision; this tool ships pixels and
  semantics, not interpretations.
- Recording/replaying macros.
- Packaging (deb/flatpak/PyPI release). `uv` + a git checkout for now.
- Windows/macOS.
- A GNOME Shell extension before Phase 7, and `RecordWindow` before then (§0, constraint 1).
