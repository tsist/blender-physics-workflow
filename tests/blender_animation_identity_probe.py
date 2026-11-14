"""In-process Blender probe for simple keyframed collider cache identities.

Run with:
  blender.exe --background --factory-startup --disable-autoexec \
    --python-exit-code 1 --python tests/blender_animation_identity_probe.py \
    -- <addon_parent> <out_json>
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import bpy


def _socket_identifier(tree, name: str, socket_type: str) -> str:
    for item in tree.interface.items_tree:
        if (
            getattr(item, "item_type", None) == "SOCKET"
            and getattr(item, "in_out", None) == "INPUT"
            and getattr(item, "name", None) == name
            and getattr(item, "socket_type", None) == socket_type
        ):
            return str(item.identifier)
    raise AssertionError(f"missing socket {name}/{socket_type}")


def _set_input(mod, tree, name: str, socket_type: str, value) -> None:
    identifier = _socket_identifier(tree, name, socket_type)
    prop = getattr(mod.properties.inputs, identifier)
    prop.value = value


def _mesh(name: str, z: float = 0.0):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(
        [(-1.0, -1.0, z), (1.0, -1.0, z), (1.0, 1.0, z), (-1.0, 1.0, z)],
        [],
        [(0, 1, 2), (0, 2, 3)],
    )
    mesh.update()
    return mesh


def _new_object(scene, name: str, z: float = 0.0):
    obj = bpy.data.objects.new(name, _mesh(name + "Mesh", z))
    scene.collection.objects.link(obj)
    return obj


def _make_action(obj, slot_name_a: str = "SlotAAA", slot_name_b: str = "SlotBBB"):
    action = bpy.data.actions.new(obj.name + "Action")
    slot_a = action.slots.new(id_type="OBJECT", name=slot_name_a)
    slot_b = action.slots.new(id_type="OBJECT", name=slot_name_b)
    layer = action.layers.new("Layer")
    strip = layer.strips.new(type="KEYFRAME")
    bag_a = strip.channelbag(slot_a, ensure=True)
    curve = bag_a.fcurves.new(data_path="location", index=0)
    for frame, value in ((1, 0.0), (12, 2.0), (24, 5.0)):
        point = curve.keyframe_points.insert(frame, value)
        point.interpolation = "BEZIER"
        point.handle_left_type = "FREE"
        point.handle_right_type = "FREE"
        point.handle_left = (frame - 1.0, value - 0.25)
        point.handle_right = (frame + 1.0, value + 0.25)
    rotation_curve = bag_a.fcurves.new(data_path="rotation_euler", index=2)
    for frame, value in ((1, 0.0), (24, math.pi / 2.0)):
        point = rotation_curve.keyframe_points.insert(frame, value)
        point.interpolation = "LINEAR"
    bag_b = strip.channelbag(slot_b, ensure=True)
    other = bag_b.fcurves.new(data_path="location", index=1)
    for frame, value in ((1, 10.0), (24, 20.0)):
        other.keyframe_points.insert(frame, value)
    obj.animation_data_create()
    obj.animation_data.action = action
    obj.animation_data.action_slot = slot_a
    return action, slot_a, slot_b, bag_a, bag_b, curve, rotation_curve


def _snapshot_scene_state():
    scene = bpy.context.scene
    return {
        "frame_current": int(scene.frame_current),
        "frame_subframe": float(scene.frame_subframe),
        "selected": sorted(obj.name for obj in bpy.context.view_layer.objects if obj.select_get()),
        "active": bpy.context.view_layer.objects.active.name
        if bpy.context.view_layer.objects.active is not None
        else None,
    }


def _animation_snapshot(obj):
    action = obj.animation_data.action
    slot = obj.animation_data.action_slot
    curves = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    curves.append(
                        {
                            "slot": bag.slot.identifier,
                            "path": curve.data_path,
                            "index": curve.array_index,
                            "extrapolation": curve.extrapolation,
                            "mute": bool(curve.mute),
                            "keyframes": [
                                {
                                    "co": list(point.co),
                                    "handle_left": list(point.handle_left),
                                    "handle_right": list(point.handle_right),
                                    "handle_left_type": point.handle_left_type,
                                    "handle_right_type": point.handle_right_type,
                                    "interpolation": point.interpolation,
                                    "easing": point.easing,
                                }
                                for point in curve.keyframe_points
                            ],
                        }
                    )
    return {
        "action": action.name,
        "slot": slot.identifier,
        "slot_handle": slot.handle,
        "curves": curves,
    }


def main() -> None:
    addon_parent, out_json = sys.argv[sys.argv.index("--") + 1 :]
    out_path = Path(out_json).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(addon_parent).resolve()))
    from physics_nodes_workflow_addon import identity  # noqa: PLC0415

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 24
    scene.render.fps = 24

    target = _new_object(scene, "Target", z=0.0)
    target.location = (0.0, 0.0, 2.0)
    single = _new_object(scene, "ColliderSingle", z=1.0)
    member = _new_object(scene, "ColliderMember", z=2.0)
    parent = bpy.data.objects.new("StaticParent", None)
    parent.location = (3.0, 4.0, 5.0)
    scene.collection.objects.link(parent)
    single.parent = parent
    single.matrix_parent_inverse.identity()

    collider_collection = bpy.data.collections.new("ColliderCollection")
    scene.collection.children.link(collider_collection)
    collider_collection.objects.link(member)
    instance_collection = bpy.data.collections.new("InstanceCollection")
    scene.collection.children.link(instance_collection)
    instance_member = _new_object(scene, "InstanceMember", z=3.0)
    instance_collection.objects.link(instance_member)

    tree = bpy.data.node_groups.new("PNW_IdentityTest", "GeometryNodeTree")
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Collider", in_out="INPUT", socket_type="NodeSocketObject")
    tree.interface.new_socket(
        name="Collider Collection", in_out="INPUT", socket_type="NodeSocketCollection"
    )
    tree.interface.new_socket(
        name="Instance Collection", in_out="INPUT", socket_type="NodeSocketCollection"
    )
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    tree.links.new(gin.outputs["Geometry"], gout.inputs["Geometry"])

    mod = target.modifiers.new("PhysicsNodes", "NODES")
    mod.node_group = tree
    bpy.context.view_layer.objects.active = target
    target.select_set(True)
    bpy.context.view_layer.update()
    _set_input(mod, tree, "Collider", "NodeSocketObject", single)
    _set_input(mod, tree, "Collider Collection", "NodeSocketCollection", collider_collection)
    _set_input(mod, tree, "Instance Collection", "NodeSocketCollection", None)

    action_single, slot_a, slot_b, bag_a, bag_b, curve, rotation_curve = _make_action(single)
    _make_action(member)

    results: dict = {"scenarios": {}, "checks": []}
    baseline_snapshot = _animation_snapshot(single)

    def build():
        return identity.build_cache_identity(target, mod, frame_start=1, frame_end=24)

    def hash_at(frame: int) -> str:
        scene.frame_set(frame)
        payload = build()
        assert payload["reliability"]["reliable"], payload["reliability"]["reasons"]
        return str(payload["identity_hash"])

    # Frame stability, including out-of-order and repeated frames.
    hashes = {}
    for frame in (24, 1, 12, 24, 1):
        hashes[str(frame)] = hash_at(frame)
    results["scenarios"]["frame_hashes"] = hashes
    results["scenarios"]["frame_stable"] = len(set(hashes.values())) == 1

    scene.frame_set(7)
    scene.frame_subframe = 0.25
    single.select_set(True)
    member.select_set(False)
    before_state = _snapshot_scene_state()
    baseline = build()
    after_state = _snapshot_scene_state()
    results["scenarios"]["no_mutation"] = {
        "before": before_state,
        "after": after_state,
        "equal": before_state == after_state,
    }
    assert baseline["reliability"]["reliable"], baseline["reliability"]["reasons"]
    baseline_hash = str(baseline["identity_hash"])
    results["scenarios"]["baseline_hash"] = baseline_hash

    def check(label: str, mutate, restore, *, reliable: bool = True):
        mutate()
        payload = build()
        actual_reliable = bool(payload["reliability"]["reliable"])
        changed = str(payload["identity_hash"]) != baseline_hash
        reasons = list(payload["reliability"]["reasons"])
        restore()
        restored = build()
        restored_reliable = bool(restored["reliability"]["reliable"])
        restored_hash = str(restored["identity_hash"])
        results["checks"].append(
            {
                "label": label,
                "reliable": actual_reliable,
                "expected_reliable": reliable,
                "identity_changed": changed,
                "reasons": reasons,
                "restored_reliable": restored_reliable,
                "restored_to_baseline": restored_hash == baseline_hash,
            }
        )

    # Keyframe value, handles, interpolation, easing, extrapolation and mute.
    point = curve.keyframe_points[0]

    def mutate_keyframe():
        point.co[1] += 0.25

    def restore_keyframe():
        point.co[1] -= 0.25

    check("keyframe_value", mutate_keyframe, restore_keyframe)

    def mutate_handle():
        point.handle_left[0] -= 0.5

    def restore_handle():
        point.handle_left[0] += 0.5

    check("keyframe_handle", mutate_handle, restore_handle)

    def mutate_handle_type():
        point.handle_left_type = "VECTOR"

    def restore_handle_type():
        point.handle_left_type = "FREE"

    check("keyframe_handle_type", mutate_handle_type, restore_handle_type)

    def mutate_interpolation():
        point.interpolation = "LINEAR"

    def restore_interpolation():
        point.interpolation = "BEZIER"

    check("keyframe_interpolation", mutate_interpolation, restore_interpolation)

    def mutate_easing():
        point.interpolation = "SINE"
        point.easing = "EASE_IN"

    def restore_easing():
        point.easing = "AUTO"
        point.interpolation = "BEZIER"

    check("keyframe_easing", mutate_easing, restore_easing)

    def mutate_extrapolation():
        curve.extrapolation = "LINEAR"

    def restore_extrapolation():
        curve.extrapolation = "CONSTANT"

    check("fcurve_extrapolation", mutate_extrapolation, restore_extrapolation)

    def mutate_mute():
        curve.mute = True

    def restore_mute():
        curve.mute = False

    check("fcurve_mute", mutate_mute, restore_mute)

    # A non-animated transform component is retained in the signature.
    def mutate_non_animated_channel():
        single.rotation_euler[1] += 0.125

    def restore_non_animated_channel():
        single.rotation_euler[1] -= 0.125

    check("non_animated_channel", mutate_non_animated_channel, restore_non_animated_channel)

    # A static parent is part of the signed transform.
    def mutate_static_parent():
        parent.location.x += 1.0
        bpy.context.view_layer.update()

    def restore_static_parent():
        parent.location.x -= 1.0
        bpy.context.view_layer.update()

    check("static_parent", mutate_static_parent, restore_static_parent)

    # Even without an explicit depsgraph update, static parent transform
    # channels are signed directly and must invalidate.
    def mutate_static_parent_channels():
        parent.location.x += 0.5

    def restore_static_parent_channels():
        parent.location.x -= 0.5

    check(
        "static_parent_channels_without_update",
        mutate_static_parent_channels,
        restore_static_parent_channels,
    )

    def mutate_static_parent_nan():
        parent.scale[0] = float("nan")

    def restore_static_parent_nan():
        parent.scale[0] = 1.0

    check("static_parent_nan", mutate_static_parent_nan, restore_static_parent_nan, reliable=False)

    def mutate_matrix_parent_inverse_nan():
        single.matrix_parent_inverse[0][0] = float("nan")

    def restore_matrix_parent_inverse_nan():
        single.matrix_parent_inverse[0][0] = 1.0

    check(
        "matrix_parent_inverse_nan",
        mutate_matrix_parent_inverse_nan,
        restore_matrix_parent_inverse_nan,
        reliable=False,
    )

    def mutate_keyframe_nan():
        curve.keyframe_points[0].co[1] = float("nan")

    def restore_keyframe_nan():
        curve.keyframe_points[0].co[1] = 0.0

    check("keyframe_nan", mutate_keyframe_nan, restore_keyframe_nan, reliable=False)

    original_frame_start = float(action_single.frame_start)
    original_frame_end = float(action_single.frame_end)

    def mutate_action_frame_start_nan():
        action_single.frame_start = float("nan")

    def restore_action_frame_start_nan():
        action_single.frame_start = original_frame_start
        action_single.frame_end = original_frame_end

    check(
        "action_frame_start_nan",
        mutate_action_frame_start_nan,
        restore_action_frame_start_nan,
        reliable=False,
    )

    # Switching to a same-name-length slot must invalidate even when its curves
    # are identical to the bound slot.
    def mutate_slot_switch():
        single.animation_data.action_slot = slot_b

    def restore_slot_switch():
        single.animation_data.action_slot = slot_a

    check("slot_switch_same_length", mutate_slot_switch, restore_slot_switch)

    # Add a third slot with exactly the same curve data as slot A.
    identical_slot = action_single.slots.new(id_type="OBJECT", name="SlotCCC")
    layer = action_single.layers[0]
    strip = layer.strips[0]
    identical_bag = strip.channelbag(identical_slot, ensure=True)
    identical_curve = identical_bag.fcurves.new(data_path="location", index=0)
    for frame, value in ((1, 0.0), (12, 2.0), (24, 5.0)):
        new_point = identical_curve.keyframe_points.insert(frame, value)
        new_point.interpolation = "BEZIER"
        new_point.handle_left_type = "FREE"
        new_point.handle_right_type = "FREE"
        new_point.handle_left = (frame - 1.0, value - 0.25)
        new_point.handle_right = (frame + 1.0, value + 0.25)

    def mutate_identical_slot():
        single.animation_data.action_slot = identical_slot

    def restore_identical_slot():
        single.animation_data.action_slot = slot_a

    check("slot_switch_identical_curves", mutate_identical_slot, restore_identical_slot)
    strip.channelbags.remove(identical_bag)
    action_single.slots.remove(identical_slot)

    # Unsupported mechanisms must be explicit and must restore cleanly.
    def mutate_nla():
        single.animation_data.nla_tracks.new()

    def restore_nla():
        single.animation_data.nla_tracks.remove(single.animation_data.nla_tracks[0])

    check("nla_track", mutate_nla, restore_nla, reliable=False)

    def mutate_driver():
        single.animation_data.drivers.new(data_path="location", index=0)

    def restore_driver():
        single.animation_data.drivers.remove(single.animation_data.drivers[0])

    check("driver", mutate_driver, restore_driver, reliable=False)

    def mutate_constraint():
        single.constraints.new("COPY_LOCATION")

    def restore_constraint():
        single.constraints.remove(single.constraints[0])

    check("constraint", mutate_constraint, restore_constraint, reliable=False)

    def mutate_curve_modifier():
        curve.modifiers.new("NOISE")

    def restore_curve_modifier():
        curve.modifiers.remove(curve.modifiers[0])

    check("fcurve_modifier", mutate_curve_modifier, restore_curve_modifier, reliable=False)

    unsupported_curve = bag_a.fcurves.new(data_path="delta_location", index=0)
    unsupported_curve.keyframe_points.insert(1, 0.0)
    unsupported_curve.keyframe_points.insert(24, 1.0)

    def mutate_unsupported_channel():
        pass

    def restore_unsupported_channel():
        bag_a.fcurves.remove(unsupported_curve)

    check("unsupported_channel", mutate_unsupported_channel, restore_unsupported_channel, reliable=False)

    def mutate_unbound_slot():
        single.animation_data.action_slot = None

    def restore_unbound_slot():
        single.animation_data.action_slot = slot_a

    check("unbound_slot", mutate_unbound_slot, restore_unbound_slot, reliable=False)

    # Animated collection member support and member keyframe invalidation.
    member_action = member.animation_data.action
    member_curve = member_action.layers[0].strips[0].channelbags[0].fcurves[0]

    def mutate_member_keyframe():
        member_curve.keyframe_points[0].co[1] += 1.0

    def restore_member_keyframe():
        member_curve.keyframe_points[0].co[1] -= 1.0

    check("collection_member_keyframe", mutate_member_keyframe, restore_member_keyframe)

    def mutate_member_constraint():
        member.constraints.new("COPY_LOCATION")

    def restore_member_constraint():
        member.constraints.remove(member.constraints[0])

    check("collection_member_constraint", mutate_member_constraint, restore_member_constraint, reliable=False)

    # Animated instance collections are intentionally still unsupported.
    _make_action(instance_member)
    _set_input(mod, tree, "Instance Collection", "NodeSocketCollection", instance_collection)
    instance_payload = build()
    results["scenarios"]["instance_collection_animated"] = {
        "reliable": bool(instance_payload["reliability"]["reliable"]),
        "reasons": list(instance_payload["reliability"]["reasons"]),
    }
    _set_input(mod, tree, "Instance Collection", "NodeSocketCollection", None)

    # Target animation remains unsupported (scope is collider dependencies only).
    _make_action(target)
    target_payload = build()
    results["scenarios"]["target_animated"] = {
        "reliable": bool(target_payload["reliability"]["reliable"]),
        "reasons": list(target_payload["reliability"]["reasons"]),
    }
    target.animation_data.action = None

    # Parent animation invalidates the static-parent precondition.
    parent.animation_data_create()
    parent_action = bpy.data.actions.new("ParentMove")
    parent_slot = parent_action.slots.new(id_type="OBJECT", name="ParentSlot")
    parent_layer = parent_action.layers.new("Layer")
    parent_strip = parent_layer.strips.new(type="KEYFRAME")
    parent_bag = parent_strip.channelbag(parent_slot, ensure=True)
    parent_curve = parent_bag.fcurves.new(data_path="location", index=0)
    parent_curve.keyframe_points.insert(1, 0.0)
    parent_curve.keyframe_points.insert(24, 4.0)
    parent.animation_data.action = parent_action
    parent.animation_data.action_slot = parent_slot
    parent_payload = build()
    results["scenarios"]["parent_animated"] = {
        "reliable": bool(parent_payload["reliability"]["reliable"]),
        "reasons": list(parent_payload["reliability"]["reasons"]),
    }
    parent.animation_data.action = None

    # Non-mesh animated collider is outside the supported subset.
    animated_empty = bpy.data.objects.new("AnimatedEmpty", None)
    scene.collection.objects.link(animated_empty)
    _make_action(animated_empty)
    _set_input(mod, tree, "Collider", "NodeSocketObject", animated_empty)
    empty_payload = build()
    results["scenarios"]["animated_empty_collider"] = {
        "reliable": bool(empty_payload["reliability"]["reliable"]),
        "reasons": list(empty_payload["reliability"]["reasons"]),
    }
    _set_input(mod, tree, "Collider", "NodeSocketObject", single)

    # Geometry Nodes dynamics on a collider are not part of the signed subset.
    gn_modifier = single.modifiers.new("GNCheck", "NODES")
    gn_payload = build()
    results["scenarios"]["gn_modifier_collider"] = {
        "reliable": bool(gn_payload["reliability"]["reliable"]),
        "reasons": list(gn_payload["reliability"]["reasons"]),
    }
    single.modifiers.remove(gn_modifier)

    # Animated mesh data is rejected even when the object transform is the
    # supported simple keyframe subset.
    data_animated = _new_object(scene, "DataAnimatedCollider", z=4.0)
    _make_action(data_animated)
    data_action = bpy.data.actions.new("MeshDataAction")
    data_slot = data_action.slots.new(id_type="MESH", name="MeshDataSlot")
    data_animated.data.animation_data_create()
    data_animated.data.animation_data.action = data_action
    data_animated.data.animation_data.action_slot = data_slot
    _set_input(mod, tree, "Collider", "NodeSocketObject", data_animated)
    data_payload = build()
    results["scenarios"]["data_animated_collider"] = {
        "reliable": bool(data_payload["reliability"]["reliable"]),
        "reasons": list(data_payload["reliability"]["reasons"]),
    }
    _set_input(mod, tree, "Collider", "NodeSocketObject", single)

    # Animated shape keys are rejected.
    shape_animated = _new_object(scene, "ShapeAnimatedCollider", z=5.0)
    bpy.context.view_layer.objects.active = shape_animated
    shape_animated.shape_key_add(name="Basis")
    shape_animated.shape_key_add(name="Key1")
    _make_action(shape_animated)
    shape_action = bpy.data.actions.new("ShapeKeyAction")
    shape_slot = shape_action.slots.new(id_type="KEY", name="ShapeKeySlot")
    shape_keys = shape_animated.data.shape_keys
    shape_keys.animation_data_create()
    shape_keys.animation_data.action = shape_action
    shape_keys.animation_data.action_slot = shape_slot
    _set_input(mod, tree, "Collider", "NodeSocketObject", shape_animated)
    shape_payload = build()
    results["scenarios"]["shape_keys_animated_collider"] = {
        "reliable": bool(shape_payload["reliability"]["reliable"]),
        "reasons": list(shape_payload["reliability"]["reasons"]),
    }
    _set_input(mod, tree, "Collider", "NodeSocketObject", single)
    bpy.context.view_layer.objects.active = target

    # Quaternion rotation and scale are part of the supported channel set.
    quaternion_collider = _new_object(scene, "QuaternionCollider", z=6.0)
    quaternion_collider.rotation_mode = "QUATERNION"
    quaternion_action = bpy.data.actions.new("QuaternionAction")
    quaternion_slot = quaternion_action.slots.new(id_type="OBJECT", name="QuaternionSlot")
    quaternion_layer = quaternion_action.layers.new("Layer")
    quaternion_strip = quaternion_layer.strips.new(type="KEYFRAME")
    quaternion_bag = quaternion_strip.channelbag(quaternion_slot, ensure=True)
    quaternion_curve = quaternion_bag.fcurves.new(data_path="rotation_quaternion", index=0)
    for frame, value in ((1, 1.0), (12, 0.8), (24, 0.5)):
        quaternion_curve.keyframe_points.insert(frame, value)
    scale_curve = quaternion_bag.fcurves.new(data_path="scale", index=0)
    for frame, value in ((1, 1.0), (24, 2.0)):
        scale_curve.keyframe_points.insert(frame, value)
    quaternion_collider.animation_data_create()
    quaternion_collider.animation_data.action = quaternion_action
    quaternion_collider.animation_data.action_slot = quaternion_slot
    _set_input(mod, tree, "Collider", "NodeSocketObject", quaternion_collider)
    quaternion_hashes = {}
    for frame in (24, 1, 12):
        scene.frame_set(frame)
        quaternion_payload = build()
        quaternion_hashes[str(frame)] = quaternion_payload["identity_hash"]
        results["scenarios"]["quaternion_scale_animated"] = {
            "reliable": bool(quaternion_payload["reliability"]["reliable"]),
            "reasons": list(quaternion_payload["reliability"]["reasons"]),
            "frame_hashes": quaternion_hashes,
            "frame_stable": len(set(quaternion_hashes.values())) == 1,
        }
    quaternion_baseline = quaternion_hashes["1"]
    quaternion_curve.keyframe_points[1].co[1] += 0.1
    scene.frame_set(1)
    changed_quaternion = build()
    results["scenarios"]["quaternion_scale_animated"]["keyframe_change_invalidates"] = (
        changed_quaternion["identity_hash"] != quaternion_baseline
    )
    quaternion_curve.keyframe_points[1].co[1] -= 0.1
    _set_input(mod, tree, "Collider", "NodeSocketObject", single)

    # A static collider with a non-finite transform channel must also be
    # rejected; otherwise different NaN values could sign the same identity.
    static_nan_collider = _new_object(scene, "StaticNanCollider", z=7.0)
    static_nan_collider.parent = parent
    static_nan_collider.matrix_parent_inverse.identity()
    _set_input(mod, tree, "Collider", "NodeSocketObject", static_nan_collider)
    static_nan_baseline = build()
    static_nan_hash = str(static_nan_baseline["identity_hash"])
    static_nan_baseline_reliable = bool(static_nan_baseline["reliability"]["reliable"])
    static_nan_collider.scale[0] = float("nan")
    static_nan_payload = build()
    static_nan_reliable = bool(static_nan_payload["reliability"]["reliable"])
    static_nan_reasons = list(static_nan_payload["reliability"]["reasons"])
    static_nan_collider.scale[0] = 1.0
    static_nan_restored = build()
    static_nan_restored_reliable = bool(static_nan_restored["reliability"]["reliable"])
    static_nan_restored_baseline = str(static_nan_restored["identity_hash"]) == static_nan_hash
    results["scenarios"]["static_collider_nan"] = {
        "baseline_reliable": static_nan_baseline_reliable,
        "reliable": static_nan_reliable,
        "reasons": static_nan_reasons,
        "restored_reliable": static_nan_restored_reliable,
        "restored_to_baseline": static_nan_restored_baseline,
    }

    parent.scale[0] = float("nan")
    static_parent_nan_payload = build()
    results["scenarios"]["static_collider_parent_nan"] = {
        "reliable": bool(static_parent_nan_payload["reliability"]["reliable"]),
        "reasons": list(static_parent_nan_payload["reliability"]["reasons"]),
    }
    parent.scale[0] = 1.0
    static_parent_nan_restored = build()
    results["scenarios"]["static_collider_parent_nan"].update(
        {
            "restored_reliable": bool(static_parent_nan_restored["reliability"]["reliable"]),
            "restored_to_baseline": str(static_parent_nan_restored["identity_hash"]) == static_nan_hash,
        }
    )
    _set_input(mod, tree, "Collider", "NodeSocketObject", single)

    # Source animation, frame/subframe and selection must be unchanged by all
    # identity builds above.
    scene.frame_set(7)
    scene.frame_subframe = 0.25
    final_snapshot = _animation_snapshot(single)
    results["source_unchanged"] = final_snapshot == baseline_snapshot
    results["final_scene_state"] = _snapshot_scene_state()
    results["frame_subframe_selection_unchanged"] = results["final_scene_state"] == before_state
    results["version"] = bpy.app.version_string
    results["build_hash"] = (
        bpy.app.build_hash.decode("ascii", "replace")
        if isinstance(bpy.app.build_hash, (bytes, bytearray))
        else str(bpy.app.build_hash)
    )

    results["pass"] = (
        results["scenarios"]["frame_stable"]
        and results["scenarios"]["no_mutation"]["equal"]
        and results["source_unchanged"]
        and all(
            item["reliable"] == item["expected_reliable"]
            and (item["expected_reliable"] is False or item["identity_changed"])
            and item["restored_reliable"]
            and item["restored_to_baseline"]
            for item in results["checks"]
        )
        and not results["scenarios"]["instance_collection_animated"]["reliable"]
        and not results["scenarios"]["target_animated"]["reliable"]
        and not results["scenarios"]["parent_animated"]["reliable"]
        and not results["scenarios"]["animated_empty_collider"]["reliable"]
        and not results["scenarios"]["gn_modifier_collider"]["reliable"]
        and not results["scenarios"]["data_animated_collider"]["reliable"]
        and not results["scenarios"]["shape_keys_animated_collider"]["reliable"]
        and results["scenarios"]["quaternion_scale_animated"]["reliable"]
        and results["scenarios"]["quaternion_scale_animated"]["frame_stable"]
        and results["scenarios"]["quaternion_scale_animated"]["keyframe_change_invalidates"]
        and results["scenarios"]["static_collider_nan"]["baseline_reliable"]
        and not results["scenarios"]["static_collider_nan"]["reliable"]
        and results["scenarios"]["static_collider_nan"]["restored_reliable"]
        and results["scenarios"]["static_collider_nan"]["restored_to_baseline"]
        and not results["scenarios"]["static_collider_parent_nan"]["reliable"]
        and results["scenarios"]["static_collider_parent_nan"]["restored_reliable"]
        and results["scenarios"]["static_collider_parent_nan"]["restored_to_baseline"]
    )
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print("ANIMATION_IDENTITY_PROBE_PASS" if results["pass"] else "ANIMATION_IDENTITY_PROBE_FAIL", out_path)
    if not results["pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
