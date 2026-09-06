# Safety

This project controls the user's real desktop.

* `cul panic`, the MCP `panic` tool, and the `PANIC` file at
  `~/.config/computer-use-linux/PANIC` release all held input. The watchdog polls that file every
  250 ms and refuses further input until it is removed.
* `pkill -f cul-mcp` is a complete kill switch on the GNOME backend. The long-lived Gio D-Bus
  connection drops with the process, and Mutter destroys the bound RemoteDesktop session,
  releasing its keys and buttons.
* Every input path tracks held keysyms/buttons, releases in `finally`/`except` paths, handles
  SIGINT/SIGTERM/SIGHUP, and has a five-second hold watchdog. `release_all()` is idempotent and
  never raises.
* Screenshots are checked against focused sensitive windows (authentication agents, portal
  prompts, gcr-prompter, GNOME Shell modal prompts, and password-text roles). Configured regions
  are blacked out in the numpy frame before PNG encoding. A title redaction without reliable
  Wayland bounds fails closed rather than returning an unredacted frame.
* The default `destructive` confirmation policy gates Super, Alt+F4, Ctrl+Alt chords, Delete,
  and configured sensitive-window text entry. Use explicit `confirm=true` only when the action is
  intended.

The implementation never calls `org.gnome.Shell.Screenshot`, `org.gnome.Shell.Introspect`, or
`org.gnome.Shell.Eval`, and never uses `RecordWindow` before the Phase 7 shell extension.

