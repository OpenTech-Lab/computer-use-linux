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
   │             │
RemoteDesktop  ScreenCast
   │             │
keysym input   PipeWire node -> gst-launch-1.0 -> PNG -> numpy
```

The Mutter backend creates RemoteDesktop first, reads its `SessionId`, and creates a ScreenCast
session bound to that ID. It adds every monitor stream before calling RemoteDesktop `Start()`.
The Gio connection stays alive until `Session.close()`; Mutter destroying the session when that
connection drops is also the process-death kill switch.

Capture and input coordinates are both relative to the selected monitor stream. `Surface.origin`
is diagnostic metadata only and is never used to turn the public API into a global desktop plane.
The two current monitors therefore expose two independent surfaces.

`GstApp` is not imported anywhere. `GstSubprocessCapture` follows the executed reference probe:
it invokes `gst-launch-1.0 -q pipewiresrc ... ! videoconvert ! pngenc ! filesink` and decodes the
result with Pillow. This is intentionally a per-frame subprocess until the missing typelib can be
installed by a human.

The AT-SPI source supplies window identity, title, role, state, size, focus activation, text
readback, and semantic actions. It returns `position: null` on this Wayland session; no position
is fabricated from AT-SPI's `(0, 0)` extents. The adapter order is native API, AT-SPI action, then
screenshot-derived pixel coordinates.
