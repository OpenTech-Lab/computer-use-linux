import gi, subprocess, sys, os
gi.require_version("Gio","2.0")
from gi.repository import Gio, GLib
mon, out = sys.argv[1], sys.argv[2]
bus=Gio.bus_get_sync(Gio.BusType.SESSION,None); SC="org.gnome.Mutter.ScreenCast"
c=lambda p,i,m,v=None: bus.call_sync(SC,p,i,m,v,None,Gio.DBusCallFlags.NONE,5000,None)
sess=c("/org/gnome/Mutter/ScreenCast",SC,"CreateSession",GLib.Variant("(a{sv})",({},))).unpack()[0]
stream=c(sess,SC+".Session","RecordMonitor",
    GLib.Variant("(sa{sv})",(mon,{"cursor-mode":GLib.Variant("u",1)}))).unpack()[0]  # 1 = EMBEDDED
node=[]
bus.signal_subscribe(None,SC+".Stream","PipeWireStreamAdded",stream,None,
    Gio.DBusSignalFlags.NONE, lambda *a: node.append(a[5].unpack()[0]))
c(sess,SC+".Session","Start")
loop=GLib.MainLoop(); GLib.timeout_add(1500, lambda:(loop.quit(),False)[1]); loop.run()
if not node: print("NO NODE"); sys.exit(1)
subprocess.run(["gst-launch-1.0","-q","pipewiresrc",f"path={node[0]}","num-buffers=8","!",
  "videoconvert","!","pngenc","snapshot=true","!","filesink",f"location={out}"],
  capture_output=True,text=True,timeout=30)
c(sess,SC+".Session","Stop")
