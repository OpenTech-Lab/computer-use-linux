# Application adapters

The adapters are the primary interaction path for applications that expose a native control
surface. Pixel input remains available through the core session, but on GNOME/Wayland it is the
least reliable tier because AT-SPI cannot provide absolute window positions.

The order is explicit:

1. Use the application-native API: CDP for Chromium, GDScript for Godot, `bpy` for Blender, and
   the `code` CLI or VSCode command bridge for VSCode.
2. If the target is an accessible control, invoke its AT-SPI `Action.do_action()` directly. This
   needs no coordinate and does not depend on window geometry.
3. Only when neither native API nor semantic action applies, derive a pixel coordinate from the
   captured screenshot. Never derive a global click coordinate from AT-SPI extents.

## Registry and surfaces

`computer_use_linux.adapters` discovers modules in its own package and registers classes with the
`@register` decorator. The current independent modules are:

| Adapter | Native path | Actions |
| --- | --- | --- |
| `browser` | Chrome DevTools Protocol over a loopback WebSocket | `launch`, `status`, `stop`, `navigate`, `eval`, `click_selector`, `text`, `screenshot`, `wait_for` |
| `godot` | one-shot `--headless --script`, or an editor plugin socket | `launch`, `status`, `stop`, `run_scene`, `eval_gdscript`/`eval`, `editor_command` |
| `blender` | absolute Blender executable with `--python-expr`, or a `bpy` main-thread socket addon | `launch`, `status`, `stop`, `run_python`, `scene_info`, `viewport_screenshot` |
| `vscode` | `code` CLI, AT-SPI, or optional Unix-socket extension | `launch`, `status`, `open`, `goto`, `diff`, `command`, `windows`, `tree`, `read_text`, `actions`, `invoke_action` |
| `atspi_generic` | AT-SPI tree and `Action.do_action()` | `windows`, `tree`, `read_text`, `focus`, `actions`, `invoke_action` |

The CLI exposes these as `cul app <adapter> <action>`. The MCP server creates the same action
surface at startup as `app_<adapter>_<action>`; for example,
`app_browser_navigate`, `app_blender_run_python`, and `app_atspi_generic_invoke_action`.

Adding an adapter means adding a module and decorating its class. Core session, CLI, and MCP code
do not contain an application-specific switch.

## Browser isolation

`browser.launch` always supplies all of these flags:

```text
--remote-debugging-port=<managed port>
--remote-debugging-address=127.0.0.1
--user-data-dir=~/.local/state/computer-use-linux/browser-profile
--force-renderer-accessibility
```

The adapter records the PID, port, and managed profile, and refuses to connect to an arbitrary
DevTools endpoint. It never adopts an already-running browser or the user's profile. `stop` kills
only the process whose command line contains the recorded managed profile and CDP port; the
profile itself is retained for later managed runs. On unpacked images where Chrome's setuid
sandbox helper is not root-owned with mode `4755`, the adapter adds `--no-sandbox` and reports that
fact in the launch result. A normal correctly-installed browser keeps its sandbox enabled.

## Code execution safety

Blender `run_python`, Godot `eval_gdscript`/`eval`, and browser `eval` are confirmation-gated by the
same `confirm=true` / `--confirm` policy used by the core. The gate runs before a bridge or
headless subprocess is reached. A failed or omitted confirmation never executes the expression.

The Blender and Godot sockets bind only to `127.0.0.1`; the VSCode companion uses a Unix-domain
socket. Socket bridges are optional and their headless/native fallbacks remain usable without
installing Python packages.

## Managed bridge lifecycle

`launch` records a small state file under the configured state directory so separate CLI invocations
can reconnect to a managed process. Adapter `close()` does not kill an application; use the
adapter's explicit `stop` action during tests or when the managed process is no longer wanted.

For Godot, copy `extras/godot-plugin/addons/cul_bridge` into a project and enable it in the
project's editor plugins before expecting live `editor_command` or `eval_gdscript` calls. The
headless path does not need that project change.

For VSCode, `--with-bridge` starts the extension-development path from
`extras/vscode-extension`. Without it, `command` supports a small audited set of standard command
ids through keyboard chords and asks for the bridge for arbitrary command ids.
