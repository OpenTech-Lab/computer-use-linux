"""Ground truth for cursor position, immune to screen animation.
Two streams on the SAME monitor: one with cursor embedded, one without.
Their difference is the cursor glyph and nothing else."""
import gi, subprocess, sys, time
gi.require_version("Gio","2.0"); from gi.repository import Gio, GLib
import numpy as np; from PIL import Image
SP="/tmp/claude-1000/-home-toyofumi-projects-computer-use-linux/593d1348-9f85-4607-95a5-a6d4a0d0a400/scratchpad"
MON=sys.argv[1]
bus=Gio.bus_get_sync(Gio.BusType.SESSION,None)
SC="org.gnome.Mutter.ScreenCast"; RD="org.gnome.Mutter.RemoteDesktop"
c=lambda d,p,i,m,v=None: bus.call_sync(d,p,i,m,v,None,Gio.DBusCallFlags.NONE,5000,None)
rd=c(RD,"/org/gnome/Mutter/RemoteDesktop",RD,"CreateSession").unpack()[0]
sid=bus.call_sync(RD,rd,"org.freedesktop.DBus.Properties","Get",
    GLib.Variant("(ss)",(RD+".Session","SessionId")),None,Gio.DBusCallFlags.NONE,5000,None).unpack()[0]
sc=c(SC,"/org/gnome/Mutter/ScreenCast",SC,"CreateSession",
     GLib.Variant("(a{sv})",({"remote-desktop-session-id":GLib.Variant("s",sid)},))).unpack()[0]
streams={}
for mode in (1,0):
    st=c(SC,sc,SC+".Session","RecordMonitor",
         GLib.Variant("(sa{sv})",(MON,{"cursor-mode":GLib.Variant("u",mode)}))).unpack()[0]
    streams[mode]=st
nodes={}
bus.signal_subscribe(None,SC+".Stream","PipeWireStreamAdded",None,None,Gio.DBusSignalFlags.NONE,
    lambda conn,s,path,i,sig,params: nodes.__setitem__(path,int(params.unpack()[0])))
c(RD,rd,RD+".Session","Start")
l=GLib.MainLoop(); GLib.timeout_add(2000, lambda:(l.quit(),False)[1]); l.run()

def grab(node,out):
    subprocess.run(["gst-launch-1.0","-q","pipewiresrc",f"path={node}","always-copy=true",
      "num-buffers=6","!","videoconvert","!","pngenc","snapshot=true","!","filesink",
      f"location={out}"],capture_output=True,timeout=25)

for tx,ty in [(1500,900),(300,200),(960,540)]:
    c(RD,rd,RD+".Session","NotifyPointerMotionAbsolute",
      GLib.Variant("(sdd)",(streams[1],float(tx),float(ty))))
    time.sleep(0.9)
    grab(nodes[streams[1]], f"{SP}/ct_on.png")
    grab(nodes[streams[0]], f"{SP}/ct_off.png")
    on=np.asarray(Image.open(f"{SP}/ct_on.png").convert("RGB")).astype(int)
    off=np.asarray(Image.open(f"{SP}/ct_off.png").convert("RGB")).astype(int)
    d=(np.abs(on-off).sum(2)>40); ys,xs=np.where(d)
    if len(xs)==0: print(f"  target=({tx},{ty})  observed=NONE  changed={d.sum()}"); continue
    # tightest cluster = cursor
    cx,cy=int(np.median(xs)),int(np.median(ys))
    err=((cx-tx)**2+(cy-ty)**2)**0.5
    print(f"  target=({tx},{ty})  observed=({cx},{cy})  err={err:.1f}px  changed={d.sum()}")
c(RD,rd,RD+".Session","Stop")
