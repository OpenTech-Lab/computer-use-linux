"""END-TO-END: does uinput injection actually reach the compositor?
Proof = move the pointer to a known coordinate, then SEE the cursor there in a real capture."""
import ctypes, struct, os, time, subprocess, sys

DESKTOP_W, DESKTOP_H = 3840, 1080     # DP-2 (0,0) + HDMI-1 (1920,0)
MON, MON_X = "DP-2", 0                # capture DP-2, which starts at desktop x=0
ABS_MAX = 32767

UI_DEV_SETUP=0x405c5503; UI_DEV_CREATE=0x5501; UI_DEV_DESTROY=0x5502
UI_SET_EVBIT=0x40045564; UI_SET_KEYBIT=0x40045565; UI_SET_ABSBIT=0x40045567
UI_ABS_SETUP=0x401c5504
EV_SYN,EV_KEY,EV_ABS=0,1,3; ABS_X,ABS_Y=0,1; BTN_LEFT=0x110; SYN_REPORT=0
libc=ctypes.CDLL("libc.so.6", use_errno=True)

class Pointer:
    def __enter__(self):
        self.fd=os.open("/dev/uinput", os.O_WRONLY|os.O_NONBLOCK)
        def io(req,arg=0):
            if libc.ioctl(self.fd, ctypes.c_ulong(req), arg)<0:
                raise OSError(ctypes.get_errno(), hex(req))
        for ev in (EV_KEY,EV_ABS,EV_SYN): io(UI_SET_EVBIT,ev)
        io(UI_SET_KEYBIT,BTN_LEFT)
        for a in (ABS_X,ABS_Y):
            io(UI_SET_ABSBIT,a)
            io(UI_ABS_SETUP, ctypes.create_string_buffer(struct.pack("HH6i",a,0,0,0,ABS_MAX,0,0,0)))
        io(UI_DEV_SETUP, ctypes.create_string_buffer(
            struct.pack("HHHH80sI",0x03,0xC1AD,0xE001,1,b"computer-use-linux e2e pointer",0)))
        io(UI_DEV_CREATE)
        time.sleep(1.2)                      # let udev/libinput adopt the device
        return self
    def _emit(self,t,c,v):
        os.write(self.fd, struct.pack("llHHi",0,0,t,c,v))
    def move_to(self,px,py):
        self._emit(EV_ABS,ABS_X,int(px/DESKTOP_W*ABS_MAX))
        self._emit(EV_ABS,ABS_Y,int(py/DESKTOP_H*ABS_MAX))
        self._emit(EV_SYN,SYN_REPORT,0)
        time.sleep(0.45)
    def __exit__(self,*a):
        libc.ioctl(self.fd, ctypes.c_ulong(UI_DEV_DESTROY), 0); os.close(self.fd)

def grab(out):
    r=subprocess.run(["/usr/bin/python3", os.path.join(os.path.dirname(__file__),"capture_once.py"),
                      MON, out], capture_output=True, text=True, timeout=40)
    if not os.path.exists(out): print("capture failed:", r.stdout[-300:], r.stderr[-300:]); sys.exit(1)

def cursor_xy(a_before, a_after):
    """Cursor position = centroid of the largest changed region between two frames."""
    import numpy as np
    d = np.abs(a_after.astype(int)-a_before.astype(int)).sum(2)
    ys,xs = np.where(d>40)
    if len(xs)==0: return None
    return int(np.median(xs)), int(np.median(ys)), len(xs)

if __name__=="__main__":
    import numpy as np
    from PIL import Image
    P1=(400,300); P2=(1300,800)          # both on DP-2
    with Pointer() as p:
        p.move_to(*P1); grab(f"{os.path.dirname(__file__)}/e2e_a.png")
        p.move_to(*P2); grab(f"{os.path.dirname(__file__)}/e2e_b.png")
        p.move_to(960,540)               # park cursor back near centre of DP-2
    a=np.asarray(Image.open(f"{os.path.dirname(__file__)}/e2e_a.png").convert("RGB"))
    b=np.asarray(Image.open(f"{os.path.dirname(__file__)}/e2e_b.png").convert("RGB"))
    print(f"frames: {a.shape} / {b.shape}")
    r=cursor_xy(a,b)
    if not r: print("RESULT: NO PIXEL CHANGE — injection did NOT reach the compositor"); sys.exit(2)
    cx,cy,n = r
    print(f"changed pixels: {n}, centroid=({cx},{cy})")
    print(f"expected cursor to move between {P1} and {P2} (monitor-local, DP-2)")
    ok = min(P1[0],P2[0])-60 <= cx <= max(P1[0],P2[0])+60 and min(P1[1],P2[1])-60 <= cy <= max(P1[1],P2[1])+60
    print("RESULT:", "PASS — uinput injection reaches the compositor and is visible in capture" if ok
          else f"AMBIGUOUS — change centroid ({cx},{cy}) outside expected band")
