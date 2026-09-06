# Reference probes

Throwaway scripts that were **executed on the target machine** to verify the environment
assumptions in `docs/ENVIRONMENT.md`. They are not part of the package and are not imported
by it — they exist so the implementation can be checked against something known to work.

| Probe | Proves |
| --- | --- |
| `uinput_probe.py` | A virtual ABS pointer + keyboard can be created and destroyed via pure `ctypes`, unprivileged. |
| `portal_probe.py` | The XDG ScreenCast portal handshake (the portable fallback path), via pure-Python `jeepney`. |
| `capture_once.py` | Mutter ScreenCast → PipeWire node id → `gst-launch-1.0 pipewiresrc` → PNG. |
| `e2e_probe.py` | The closed loop: inject pointer motion, capture, and find the cursor where it was commanded. |
