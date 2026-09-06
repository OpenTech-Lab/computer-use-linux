@tool
extends EditorPlugin

var bridge: Node


func _enter_tree() -> void:
	bridge = preload("res://addons/cul_bridge/bridge.gd").new()
	add_child(bridge)
	bridge.start()


func _exit_tree() -> void:
	if is_instance_valid(bridge):
		bridge.stop()
		bridge.queue_free()
