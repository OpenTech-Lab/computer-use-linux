# Architecture

The current implementation uses one long-lived `Session` for desktop operations and passes that
session to adapters that need semantic focus or AT-SPI. The session owns a capability-detected
backend, monitor-local surfaces, coordinate/image-scale state, the held-input set, the safety
watchdog, and the optional AT-SPI window source. Native app adapters are the preferred path for
application-specific work.

```text
CLI / stdio MCP ──> Session ──> desktop backend
       │                 │
       └───────────────> adapter registry
                         ├─ browser: CDP
                         ├─ godot: headless GDScript / editor socket
                         ├─ blender: --python-expr / bpy socket
                         ├─ vscode: code CLI / AT-SPI / Unix socket
                         └─ atspi_generic: Action.do_action()

GnomeMutterBackend
   ├─ normal session: physical monitor streams ── ScreenCast/PipeWire ──> gst-launch-1.0 -> PNG -> numpy
   └─ --isolated session: virtual:0 ─────────── ScreenCast/PipeWire ──> gst-launch-1.0 -> PNG -> numpy
      └─ RemoteDesktop NotifyPointerMotionAbsolute / keysym input

X11Backend
   └─ XTEST input + XGetImage root capture + X11 TranslateCoords window geometry

HeadlessBackend
   └─ private Xvfb ──> X11Backend (separate pointer, keyboard, windows, and root capture)
```

The Mutter backend creates RemoteDesktop first, reads its `SessionId`, and creates a ScreenCast
session bound to that ID. It adds every monitor stream before calling RemoteDesktop `Start()`.
The Gio connection stays alive until `Session.close()`; Mutter destroying the session when that
connection drops is also the process-death kill switch.

Capture and input coordinates are both relative to the selected surface. `Surface.origin` is
diagnostic metadata for physical monitor layout; it is `(0, 0)` for the virtual stream and the X11
root. It is never used to turn the GNOME public API into a global desktop plane. A virtual stream
has an independent pointer and capture target, but GNOME does not expose it as a desktop output on
which ordinary windows can be placed. Full application/window-stack isolation is provided by the
private Xvfb backend instead.

Physical and virtual surfaces are mutually exclusive within a Mutter session. Normal sessions do
not probe or create a virtual stream; `--isolated` selects a virtual-only session before streams
are recorded. This avoids Mutter's mixed-stream invalidation, where a subsequent
`NotifyPointerMotionAbsolute` can return `Unknown stream (0)` for a physical stream. Virtual grabs
retry a bounded number of times because an empty `RecordVirtual` stream can stop emitting PipeWire
buffers. A virtual selection made after a physical MCP session has started is rejected; restart the
MCP server with `--isolated` instead.

`GstApp` is not imported anywhere. `GstSubprocessCapture` follows the executed reference probe:
it invokes `gst-launch-1.0 -q pipewiresrc ... ! videoconvert ! pngenc ! filesink` and decodes the
result with Pillow. This is intentionally a per-frame subprocess until the missing typelib can be
installed by a human.

The AT-SPI source supplies window identity, title, role, state, size, focus activation, text
readback, and semantic actions. It returns `position: null` on this Wayland session; no position
is fabricated from AT-SPI's `(0, 0)` extents. The adapter order is native API, AT-SPI action, then
screenshot-derived pixel coordinates. X11 uses a direct window source with real root-relative
positions and advertises `WINDOW_GEOMETRY`; the Wayland AT-SPI source keeps positions null.
