"""Probe the XDG ScreenCast portal handshake WITHOUT calling Start()
(Start is the only step that pops a user-visible consent dialog)."""
import os, secrets
from jeepney import DBusAddress, new_method_call, MessageType
from jeepney.io.blocking import open_dbus_connection

PORTAL = DBusAddress('/org/freedesktop/portal/desktop',
                     bus_name='org.freedesktop.portal.Desktop',
                     interface='org.freedesktop.portal.ScreenCast')

conn = open_dbus_connection(bus='SESSION')
unique = conn.unique_name[1:].replace('.', '_')
token = 'cu_' + secrets.token_hex(4)

# --- version + capability properties (no dialog) ---
props = DBusAddress('/org/freedesktop/portal/desktop',
                    bus_name='org.freedesktop.portal.Desktop',
                    interface='org.freedesktop.DBus.Properties')
for p in ('version', 'AvailableSourceTypes', 'AvailableCursorModes'):
    try:
        r = conn.send_and_get_reply(new_method_call(props, 'Get', 'ss',
                                    ('org.freedesktop.portal.ScreenCast', p)))
        print(f"ScreenCast.{p} = {r.body[0][1]}")
    except Exception as e:
        print(f"ScreenCast.{p} ERR {e}")

# --- CreateSession (no dialog) ---
reply = conn.send_and_get_reply(new_method_call(
    PORTAL, 'CreateSession', 'a{sv}',
    ({'handle_token': ('s', token), 'session_handle_token': ('s', token)},)))
if reply.header.message_type is MessageType.error:
    print("CreateSession FAILED:", reply.header.fields, reply.body); raise SystemExit(1)
print("CreateSession -> request handle:", reply.body[0])
print()
print("RESULT: portal reachable, ScreenCast interface responds, session request created.")
print("Start() deliberately NOT called (that is the step that prompts the user).")
