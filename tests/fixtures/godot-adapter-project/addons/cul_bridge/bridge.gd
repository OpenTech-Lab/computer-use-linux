@tool
extends Node

var server := TCPServer.new()
var peer: StreamPeerTCP
var buffer := ""
var port := 9877
var state_file := ""
var token := ""
var crypto := Crypto.new()

const OWNER_ONLY_PERMISSIONS := FileAccess.UNIX_READ_OWNER | FileAccess.UNIX_WRITE_OWNER


func start() -> bool:
	port = int(OS.get_environment("CUL_GODOT_PORT"))
	if port <= 0:
		port = 9877
	state_file = OS.get_environment("CUL_GODOT_STATE_FILE")
	if state_file.is_empty():
		state_file = ProjectSettings.globalize_path("user://cul-godot.json")
	token = crypto.generate_random_bytes(32).hex_encode()
	var listen_error := server.listen(port, "127.0.0.1")
	if listen_error != OK:
		push_error("CUL Godot bridge could not bind its loopback listener")
		token = ""
		return false
	if not _write_state():
		server.stop()
		token = ""
		return false
	set_process(true)
	return true


func stop() -> void:
	_close_peer()
	server.stop()
	_remove_state_if_owned()


func _close_peer() -> void:
	if peer:
		peer.disconnect_from_host()
	peer = null
	buffer = ""


func _read_state() -> Dictionary:
	if not FileAccess.file_exists(state_file):
		return {}
	var file := FileAccess.open(state_file, FileAccess.READ)
	if file == null:
		return {}
	var parsed = JSON.parse_string(file.get_as_text())
	file.close()
	return parsed if parsed is Dictionary else {}


func _write_state() -> bool:
	var parent := state_file.get_base_dir()
	if not DirAccess.dir_exists_absolute(parent):
		if DirAccess.make_dir_recursive_absolute(parent) != OK:
			push_error("CUL Godot bridge could not create its state directory")
			return false
	var state := _read_state()
	state["pid"] = OS.get_process_id()
	state["port"] = port
	state["project"] = ProjectSettings.globalize_path("res://")
	state["token"] = token
	var file := FileAccess.open(state_file, FileAccess.WRITE)
	if file == null:
		push_error("CUL Godot bridge could not write its state file")
		return false
	file.store_string(JSON.stringify(state))
	file.flush()
	file.close()
	if FileAccess.set_unix_permissions(state_file, OWNER_ONLY_PERMISSIONS) != OK:
		push_error("CUL Godot bridge state file permissions could not be restricted")
		return false
	if FileAccess.get_unix_permissions(state_file) != OWNER_ONLY_PERMISSIONS:
		push_error("CUL Godot bridge state file is not owner-only")
		return false
	return true


func _remove_state_if_owned() -> void:
	var state := _read_state()
	if int(state.get("pid", -1)) == OS.get_process_id() and FileAccess.file_exists(state_file):
		DirAccess.remove_absolute(state_file)


func _authorized(request: Dictionary) -> bool:
	var supplied = request.get("token", "")
	if typeof(supplied) != TYPE_STRING:
		supplied = ""
	return crypto.constant_time_compare(token.to_utf8_buffer(), supplied.to_utf8_buffer())


func _process(_delta: float) -> void:
	if peer == null and server.is_connection_available():
		peer = server.take_connection()
	if peer == null:
		return
	if peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
		_close_peer()
		return
	var available := peer.get_available_bytes()
	if available > 0:
		buffer += peer.get_utf8_string(available)
	while buffer.find("\n") >= 0:
		var newline := buffer.find("\n")
		var line := buffer.substr(0, newline)
		buffer = buffer.substr(newline + 1)
		var parsed = JSON.parse_string(line)
		if typeof(parsed) != TYPE_DICTIONARY:
			_close_peer()
			return
		var request: Dictionary = parsed
		if not _authorized(request):
			_close_peer()
			return
		request.erase("token")
		var response = _handle(request)
		peer.put_data((JSON.stringify(response) + "\n").to_utf8_buffer())
		_close_peer()


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
