from __future__ import annotations

import contextlib
import ctypes
import os
import shutil
import struct
import subprocess
import tempfile
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..errors import BackendUnavailable, CaptureTimeout
from ..types import Frame


def _refresh_caps(value: float) -> str:
    fraction = Fraction(float(value)).limit_denominator(1000)
    return f"{fraction.numerator}/{fraction.denominator}"


def _capture_timeout(explicit: float | None) -> float:
    """Resolve the capture deadline.

    2s is too tight in practice: PipeWire emits frames only on damage, so a quiet or
    directly-scanned-out monitor can legitimately take longer than that to produce one.
    """
    if explicit is not None:
        return float(explicit)
    raw = os.environ.get("CUL_CAPTURE_TIMEOUT")
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return 6.0


class GstSubprocessCapture:
    """Pull one PipeWire frame through the installed gst-launch executable.

    The in-process appsink binding is intentionally not used: this machine does not have the
    GstApp typelib. A short-lived pipeline is slower than appsink, but it is the verified path
    that remains available without installing an apt package.
    """

    def __init__(
        self,
        node_id: int,
        surface_id: str,
        *,
        width: int | None = None,
        height: int | None = None,
        refresh: float | None = None,
    ):
        self.node_id = int(node_id)
        self.surface_id = surface_id
        self.width = width
        self.height = height
        self.refresh = refresh
        self.launcher = shutil.which("gst-launch-1.0")
        if not self.launcher:
            raise BackendUnavailable("gst-launch-1.0 is missing; install GStreamer and gstreamer1.0-pipewire")

    def grab(self, *, timeout: float | None = None) -> Frame:
        import numpy as np
        from PIL import Image

        timeout = _capture_timeout(timeout)
        fd, raw_path = tempfile.mkstemp(prefix="cul-frame-", suffix=".png")
        os.close(fd)
        output_path = Path(raw_path)
        command = [
            self.launcher,
            "-q",
            "pipewiresrc",
            f"path={self.node_id}",
            "always-copy=true",
            "num-buffers=1" if self.width is not None and self.height is not None and self.refresh is not None else "num-buffers=8",
        ]
        if self.width is not None and self.height is not None and self.refresh is not None:
            command.extend(
                [
                    "!",
                    (
                        "video/x-raw,format=BGRx,"
                        f"max-framerate={_refresh_caps(self.refresh)},width={int(self.width)},height={int(self.height)}"
                    ),
                ]
            )
        command.extend(
            [
                "!",
                "videoconvert",
                "!",
                "pngenc",
                "snapshot=true",
                "!",
                "filesink",
                f"location={output_path}",
            ]
        )
        try:
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=max(3.0, timeout + 1.0),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise CaptureTimeout(
                    f"GStreamer capture timed out after {timeout:.1f}s for {self.surface_id}. "
                    "PipeWire only emits frames when the screen changes, and a fullscreen "
                    "application can take direct scanout, which bypasses composition and stops "
                    "the stream entirely. Try another surface (see `cul surfaces`), leave "
                    "fullscreen on that monitor, or raise CUL_CAPTURE_TIMEOUT (seconds)."
                ) from exc
            if completed.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
                detail = (completed.stderr or completed.stdout or "no pipeline output").strip()[-500:]
                raise CaptureTimeout(f"GStreamer did not produce a frame: {detail}")
            try:
                with Image.open(output_path) as image:
                    data = np.asarray(image.convert("RGB"), dtype=np.uint8).copy()
            except Exception as exc:
                raise CaptureTimeout(f"GStreamer produced an unreadable PNG: {exc}") from exc
            if data.ndim != 3 or data.shape[2] != 3:
                raise CaptureTimeout(f"unexpected captured frame shape: {data.shape}")
            return Frame(
                surface_id=self.surface_id,
                width=int(data.shape[1]),
                height=int(data.shape[0]),
                data=data,
                captured_at=time.time(),
            )
        finally:
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass


class GstCursorMetadataCapture:
    """Read PipeWire cursor metadata through GStreamer without GstApp bindings.

    ``pipewiresrc`` turns SPA cursor metadata into a
    ``GstVideoRegionOfInterestMeta`` whose region is named ``cursor``.  The
    GstApp typelib is not needed: appsink is created by the normal GStreamer
    element factory and samples are pulled through its signal API.  The
    metadata stream is used only by the coordinate selftest; ordinary screen
    captures continue to use the embedded-cursor subprocess path above.
    """

    def __init__(
        self,
        node_id: int,
        surface_id: str,
        *,
        width: int | None = None,
        height: int | None = None,
        refresh: float | None = None,
    ):
        self.node_id = int(node_id)
        self.surface_id = surface_id
        self.width = width
        self.height = height
        self.refresh = refresh
        self._Gst, self._GObject = self._load_gstreamer()
        self._pipeline: Any | None = None
        self._sink: Any | None = None
        self._roi_api: Any | None = None
        try:
            caps = "video/x-raw,format=BGRx"
            if self.width is not None and self.height is not None and self.refresh is not None:
                caps += f",max-framerate={_refresh_caps(self.refresh)},width={int(self.width)},height={int(self.height)}"
            self._pipeline = self._Gst.parse_launch(
                " ! ".join(
                    (
                        f"pipewiresrc path={self.node_id} always-copy=true",
                        caps,
                        "appsink name=cul_sink sync=false max-buffers=1 drop=true",
                    )
                )
            )
            self._sink = self._pipeline.get_by_name("cul_sink")
            if self._sink is None:
                raise BackendUnavailable("GStreamer did not create an appsink for cursor metadata")
            state_result = self._pipeline.set_state(self._Gst.State.PLAYING)
            if state_result == self._Gst.StateChangeReturn.FAILURE:
                raise BackendUnavailable("GStreamer could not start the cursor metadata pipeline")
            # The GstVideo metadata types are registered lazily when the first
            # sample is negotiated; resolve the API in _cursor_position().
            self._roi_api = None
        except BackendUnavailable:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise BackendUnavailable(f"GStreamer cursor metadata is unavailable: {exc}") from exc

    @staticmethod
    def _load_gstreamer() -> tuple[Any, Any]:
        try:
            import gi

            gi.require_version("GObject", "2.0")
            gi.require_version("Gst", "1.0")
            from gi.repository import GObject, Gst

            Gst.init(None)
            return Gst, GObject
        except Exception as exc:  # pragma: no cover - depends on the host interpreter
            raise BackendUnavailable(
                "PyGObject/GStreamer is unavailable; cursor metadata selftest requires the prepared runtime"
            ) from exc

    def _cursor_position(self, buffer: Any) -> tuple[int, int] | None:
        if self._roi_api is None:
            with contextlib.suppress(Exception):
                self._roi_api = self._GObject.type_from_name("GstVideoRegionOfInterestMetaAPI")
            if self._roi_api is None:
                return None
        try:
            meta = buffer.get_meta(self._roi_api)
            if meta is None:
                return None
            # GstVideoRegionOfInterestMeta has a stable ABI on the supported
            # GStreamer 1.x stack: GstMeta (16 bytes), roi_type (4), id (8),
            # then x/y/w/h.  The ROI struct keeps the id immediately after
            # roi_type, so x/y begin at offsets 28/32.
            # pipewiresrc emits only the cursor ROI, so no string lookup is
            # required and this remains usable without the GstVideo typelib.
            address = hash(meta)
            x, y = struct.unpack_from("II", ctypes.string_at(address + 28, 8))
            return int(x), int(y)
        except (OSError, TypeError, ValueError, struct.error):
            return None

    def grab(self, *, timeout: float = 2.0) -> tuple[Frame, tuple[int, int] | None]:
        import numpy as np

        if self._sink is None:
            raise CaptureTimeout(f"cursor metadata pipeline is closed for {self.surface_id}")
        sample = self._sink.emit("try-pull-sample", int(max(0.1, timeout) * self._Gst.SECOND))
        if sample is None:
            raise CaptureTimeout(f"GStreamer produced no cursor metadata frame for {self.surface_id}")
        buffer = sample.get_buffer()
        caps = sample.get_caps()
        structure = caps.get_structure(0)
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))
        success, mapped = buffer.map(self._Gst.MapFlags.READ)
        if not success:
            raise CaptureTimeout(f"GStreamer could not map cursor metadata frame for {self.surface_id}")
        try:
            # Read metadata while the GstBuffer is still mapped and owned by
            # the sample.  The ROI metadata belongs to the buffer, not the
            # returned Frame, so do not inspect it after unmapping.
            position = self._cursor_position(buffer)
            raw = np.frombuffer(mapped.data, dtype=np.uint8)
            expected = width * height * 4
            if raw.size < expected:
                raise CaptureTimeout(f"unexpected cursor metadata frame size: {raw.size} < {expected}")
            # BGRx is explicitly negotiated above. Copy before unmapping the
            # GstBuffer so the returned Frame owns its bytes.
            data = raw[:expected].reshape((height, width, 4))[..., 2::-1].copy()
        finally:
            buffer.unmap(mapped)
        return (
            Frame(
                surface_id=self.surface_id,
                width=width,
                height=height,
                data=data,
                captured_at=time.time(),
            ),
            position,
        )

    def close(self) -> None:
        if self._pipeline is not None:
            with contextlib.suppress(Exception):
                self._pipeline.set_state(self._Gst.State.NULL)
        self._sink = None
        self._pipeline = None
