@tool
extends EditorPlugin

var bridge: Node


func _enter_tree() -> void:
	bridge = preload("res://addons/cul_bridge/bridge.gd").new()
	add_child(bridge)
	if not bridge.start():
		push_error("CUL Godot bridge refused to start")
		remove_child(bridge)
		bridge.queue_free()
		bridge = null


func _exit_tree() -> void:
	if is_instance_valid(bridge):
		bridge.stop()
		bridge.queue_free()
