# Coordinates

Every input point is `(surface_id, x, y)`. A surface is one Mutter monitor stream and defines one
native pixel space. There is no public global 3840-pixel desktop plane, so a point on `HDMI-1`
cannot accidentally be interpreted as a point on `DP-2`.

Screenshots default to `max_width=1280`. For a 1920-wide monitor, the image scale is `1280/1920`;
an image-space input is divided by that scale and rounded once at the boundary. `coord_space="surface"`
uses native pixels directly. A screenshot response includes both image and surface dimensions,
the scale, surface ID, and capture timestamp. A cropped screenshot also records its native region.

`Surface.scale` is reported from `Mutter.DisplayConfig.GetCurrentState` for diagnostics. It does not
enter the conversion: the Mutter stream and `NotifyPointerMotionAbsolute` use the same physical
pixel space. This remains correct for a synthetic 2.0-scale surface and avoids applying HiDPI twice.

The live acceptance round-trip moves the real pointer to `(400,300)`, `(100,100)`, and `(1800,1000)`
on `DP-2` and reads the exact local cursor position from a `cursor-mode=2` PipeWire metadata stream.
It asserts every observed position is within two pixels. This avoids confusing application animation
with cursor pixels. The companion `cul selftest monitors` command moves to fixed points on both
`DP-2` and `HDMI-1`, requiring a cursor observation on the commanded stream and no observation on
the other stream; this is the regression test for monitor targeting.
