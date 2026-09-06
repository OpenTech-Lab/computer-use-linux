# Coordinates

Every input point is `(surface_id, x, y)`. A surface is one backend capture/input target and defines
one native pixel space. GNOME physical surfaces are Mutter monitor streams; `virtual:0` is a
`RecordVirtual` stream; X11 and headless expose an X root. There is no public global 3840-pixel
desktop plane in the GNOME API, so a point on `HDMI-1` cannot accidentally be interpreted as a point
on `DP-2`.

The virtual surface uses the configured negotiated size (1280x720 at 30 Hz by default) and has a
local origin of `(0, 0)`. Its independent pointer does not imply that application windows can be
placed there. Normal window placement remains on the physical desktop; use the headless backend for
a separate application/window stack.

The X11 backend exposes the root as one surface with real root coordinates. Its `WindowInfo.position`
values are root-relative when the server supports XGetGeometry/TranslateCoords. The headless backend
uses the same X11 contract inside its private Xvfb display, where the root origin is `(0, 0)`.

Screenshots default to `max_width=1280`. For a 1920-wide monitor, the image scale is `1280/1920`;
an image-space input is divided by that scale and rounded once at the boundary. `coord_space="surface"`
uses native pixels directly. A screenshot response includes both image and surface dimensions,
the scale, surface ID, and capture timestamp. A cropped screenshot also records its native region.

`Surface.scale` is reported from `Mutter.DisplayConfig.GetCurrentState` for diagnostics. It does not
enter the conversion: the Mutter stream and `NotifyPointerMotionAbsolute` use the same physical
pixel space. This remains correct for a synthetic 2.0-scale surface and avoids applying HiDPI twice.

The live acceptance round-trip moves a pointer to `(400,300)`, `(100,100)`, and `(1800,1000)` on
`HDMI-1` and reads the exact local cursor position from a `cursor-mode=2` PipeWire metadata stream.
The same check can target `virtual:0` without moving the physical cursor. It asserts every observed
position is within two pixels. This avoids confusing application animation with cursor pixels. The
companion `cul selftest monitors` command moves to fixed points on both physical monitors, requiring
a cursor observation on the commanded stream and no observation on the other stream; this is the
regression test for monitor targeting.
