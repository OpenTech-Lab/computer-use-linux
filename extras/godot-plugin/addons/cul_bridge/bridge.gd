@tool
extends Node

var server := TCPServer.new()
var peer: StreamPeerTCP
var buffer := ""
var port := 9877


func start() -> void:
	port = int(OS.get_environment("CUL_GODOT_PORT"))
	if port <= 0:
		port = 9877
	server.listen(port, "127.0.0.1")
	set_process(true)


func stop() -> void:
	if peer:
		peer.disconnect_from_host()
	peer = null
	server.stop()


func _process(_delta: float) -> void:
	if peer == null and server.is_connection_available():
		peer = server.take_connection()
	if peer == null:
		return
	if peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
		peer = null
		return
	var available := peer.get_available_bytes()
	if available > 0:
		buffer += peer.get_utf8_string(available)
	while buffer.find("\n") >= 0:
		var newline := buffer.find("\n")
		var line := buffer.substr(0, newline)
		buffer = buffer.substr(newline + 1)
		var request = JSON.parse_string(line)
		var response = _handle(request if request is Dictionary else {})
		peer.put_data((JSON.stringify(response) + "\n").to_utf8_buffer())
		peer.disconnect_from_host()
		peer = null
		buffer = ""


func _handle(request: Dictionary) -> Dictionary:
	var action := str(request.get("action", ""))
	if action == "status":
		return {"ok": true, "version": Engine.get_version_info().string, "pid": OS.get_process_id()}
	if action == "run_scene":
		EditorInterface.play_main_scene()
		return {"ok": true, "action": action}
	if action == "editor_command":
		var command_id := str(request.get("id", ""))
		if command_id == "play_main_scene":
			EditorInterface.play_main_scene()
		elif command_id == "play_current_scene":
			EditorInterface.play_current_scene()
		elif command_id == "stop_playing_scene":
			EditorInterface.stop_playing_scene()
		else:
			return {"ok": false, "error": "unsupported editor command: " + command_id}
		return {"ok": true, "action": command_id}
	if action == "eval_gdscript":
		return _eval_gdscript(str(request.get("expr", "")))
	return {"ok": false, "error": "unknown bridge action: " + action}


func _eval_gdscript(source: String) -> Dictionary:
	var body := source.strip_edges()
	if not (body.begins_with("print(") or body.begins_with("push_error(") or body.begins_with("push_warning(") or body.contains("\n") or body.contains(";")):
		body = "return " + body
	var script := GDScript.new()
	script.source_code = "extends RefCounted\nfunc _cul_run():\n    " + body.replace("\n", "\n    ") + "\n"
	var error := script.reload()
	if error != OK:
		return {"ok": false, "error": "GDScript parse error: " + str(error)}
	var instance = script.new()
	var value = instance._cul_run()
	return {"ok": true, "result": value}
