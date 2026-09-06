import ctypes, struct, os, time
# Minimal uinput smoke test: create an absolute pointer + keyboard, verify device registers.
UI_DEV_SETUP=0x405c5503; UI_DEV_CREATE=0x5501; UI_DEV_DESTROY=0x5502
UI_SET_EVBIT=0x40045564; UI_SET_KEYBIT=0x40045565; UI_SET_ABSBIT=0x40045567
UI_ABS_SETUP=0x401c5504
EV_SYN,EV_KEY,EV_ABS=0x00,0x01,0x03
ABS_X,ABS_Y=0x00,0x01
BTN_LEFT=0x110; KEY_A=30
libc=ctypes.CDLL("libc.so.6", use_errno=True)
fd=os.open("/dev/uinput", os.O_WRONLY|os.O_NONBLOCK)
def ioctl(req,arg=0):
    r=libc.ioctl(fd,ctypes.c_ulong(req),arg)
    if r<0: raise OSError(ctypes.get_errno(), f"ioctl {hex(req)}")
    return r
for ev in (EV_KEY,EV_ABS,EV_SYN): ioctl(UI_SET_EVBIT,ev)
ioctl(UI_SET_KEYBIT,BTN_LEFT); ioctl(UI_SET_KEYBIT,KEY_A)
for a in (ABS_X,ABS_Y): ioctl(UI_SET_ABSBIT,a)
# uinput_abs_setup: __u16 code; input_absinfo(value,min,max,fuzz,flat,res) as s32 x6
for a in (ABS_X,ABS_Y):
    buf=struct.pack("HH6i", a,0, 0,0,32767,0,0,0)  # pad to align
    ioctl(UI_ABS_SETUP, ctypes.create_string_buffer(buf))
# uinput_setup: input_id(bustype,vendor,product,version) + name[80] + ff_effects_max u32
setup=struct.pack("HHHH80sI", 0x03,0x1234,0x5678,1, b"claude-computer-use-probe", 0)
ioctl(UI_DEV_SETUP, ctypes.create_string_buffer(setup))
ioctl(UI_DEV_CREATE)
time.sleep(0.4)
print("UINPUT DEVICE CREATED OK")
os.system("grep -A2 -B2 'claude-computer-use-probe' /proc/bus/input/devices | head -20")
ioctl(UI_DEV_DESTROY); os.close(fd)
print("DESTROYED CLEANLY")
