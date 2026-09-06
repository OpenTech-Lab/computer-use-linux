# Architecture

Phases 0–2 use one long-lived `Session`. The session owns a capability-detected backend,
monitor-local surfaces, coordinate/image-scale state, the held-input set, the safety watchdog,
and the optional AT-SPI window source.

```text
CLI / stdio MCP
       |
    Session
       |
GnomeMutterBackend
   |             |
RemoteDesktop  ScreenCast
   |             |
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

The Phase 1 AT-SPI source supplies window identity, title, role, state, size, focus activation,
and text readback. It always returns `position: null` on this Wayland session; no position is
fabricated from AT-SPI's `(0, 0)` extents.

