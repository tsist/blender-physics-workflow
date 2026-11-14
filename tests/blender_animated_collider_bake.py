"""Real geometry-nodes bake probe for simple keyframed collider transforms.

Run with:
  blender.exe --background --factory-startup --disable-autoexec \
    --python-exit-code 1 --python tests/blender_animated_collider_bake.py \
    -- <mode> <addon_parent> <evidence_dir>

Modes:
  single_animated | single_static | collection_animated | collection_static |
  cold_single | cold_collection
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import bpy


def _mesh(name: str, vertices, faces):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    return mesh


def _make_collider_action(plane):
    action = bpy.data.actions.new(plane.name + "AuthoredMove")
    slot_a = action.slots.new(id_type="OBJECT", name="ColliderSlotA")
    slot_b = action.slots.new(id_type="OBJECT", name="ColliderSlotB")
    layer = action.layers.new("Layer")
    strip = layer.strips.new(type="KEYFRAME")
    bag_a = strip.channelbag(slot_a, ensure=True)
    curve = bag_a.fcurves.new(data_path="location", index=2)
    for frame, value in ((1, 1.0), (24, 3.0)):
        point = curve.keyframe_points.insert(frame, value)
        point.interpolation = "LINEAR"
    bag_b = strip.channelbag(slot_b, ensure=True)
    other = bag_b.fcurves.new(data_path="location", index=2)
    for frame, value in ((1, 1.0), (24, 5.0)):
        point = other.keyframe_points.insert(frame, value)
        point.interpolation = "LINEAR"
    plane.animation_data_create()
    plane.animation_data.action = action
    plane.animation_data.action_slot = slot_a
    plane.location.z = 1.0
    return action, slot_a, slot_b, curve


def _origins(obj, frame: int):
    bpy.context.scene.frame_set(frame)
    graph = bpy.context.evaluated_depsgraph_get()
    return [
        [round(float(value), 9) for value in instance.matrix_world.translation]
        for instance in graph.object_instances
        if instance.is_instance
        and instance.parent
        and instance.parent.original == obj
    ]


def _evaluated_vertices(obj, frame: int):
    bpy.context.scene.frame_set(frame)
    graph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(graph)
    data = evaluated.data
    if not hasattr(data, "vertices"):
        return []
    matrix = evaluated.matrix_world
    return [
        [round(float(value), 9) for value in (matrix @ vertex.co)]
        for vertex in data.vertices
    ]


def _animation_snapshot(plane):
    animation_data = plane.animation_data
    action = animation_data.action if animation_data else None
    slot = animation_data.action_slot if animation_data else None
    curves = []
    if action is not None:
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
        "action": action.name if action else None,
        "slot": slot.identifier if slot else None,
        "slot_handle": slot.handle if slot else None,
        "nla_tracks": [track.name for track in animation_data.nla_tracks]
        if animation_data
        else [],
        "drivers": len(animation_data.drivers) if animation_data else 0,
        "curves": curves,
    }


def _scene_state():
    scene = bpy.context.scene
    return {
        "frame": int(scene.frame_current),
        "subframe": float(scene.frame_subframe),
        "selected": sorted(obj.name for obj in bpy.context.view_layer.objects if obj.select_get()),
        "active": bpy.context.view_layer.objects.active.name
        if bpy.context.view_layer.objects.active is not None
        else None,
    }


def _save_blend(path: Path) -> None:
    prefs = bpy.context.preferences.filepaths
    prefs.save_version = 0
    if hasattr(prefs, "file_preview_type"):
        prefs.file_preview_type = "NONE"
    bpy.ops.wm.save_as_mainfile(filepath=str(path), compress=False, check_existing=False)


def _build_scene(addon_parent: str, scenario: str):
    sys.path.insert(0, str(Path(addon_parent).resolve()))
    import physics_nodes_workflow_addon as addon  # noqa: PLC0415
    from physics_nodes_workflow_addon import bake, contract, geometry_evidence  # noqa: PLC0415

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = 24

    target_mesh = _mesh("AnimatedColliderTargetMesh", [(0.0, 0.0, 2.0), (0.6, 0.0, 2.0)], [])
    target = bpy.data.objects.new("AnimatedColliderTarget", target_mesh)
    scene.collection.objects.link(target)
    bpy.context.view_layer.objects.active = target
    target.select_set(True)

    plane_mesh = _mesh(
        "AnimatedColliderPlaneMesh",
        [(-2.0, -2.0, 0.0), (2.0, -2.0, 0.0), (2.0, 2.0, 0.0), (-2.0, 2.0, 0.0)],
        [(0, 1, 2, 3)],
    )
    plane = bpy.data.objects.new("AnimatedColliderPlane", plane_mesh)
    parent = bpy.data.objects.new("AnimatedColliderStaticParent", None)
    parent.location = (0.0, 0.0, 0.0)
    scene.collection.objects.link(parent)
    plane.parent = parent
    plane.matrix_parent_inverse.identity()
    plane.location = (0.0, 0.0, 1.0)

    animated = scenario.endswith("_animated")
    collection = scenario.startswith("collection_")
    collider_collection = None
    if collection:
        collider_collection = bpy.data.collections.new("AnimatedColliderCollection")
        scene.collection.children.link(collider_collection)
        collider_collection.objects.link(plane)
    else:
        scene.collection.objects.link(plane)

    action = slot_a = slot_b = curve = None
    if animated:
        action, slot_a, slot_b, curve = _make_collider_action(plane)
    else:
        if plane.animation_data is not None:
            plane.animation_data_clear()
        plane.location = (0.0, 0.0, 1.0)

    evidence_dir = Path(sys.argv[sys.argv.index("--") + 3]).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    bake_dir = evidence_dir / ("cache_" + scenario)
    request = {
        "target_object": target.name,
        "preset_id": "rigid_drop_pack",
        "bake": True,
        "bake_directory": str(bake_dir),
        "collider_object": None if collection else plane.name,
        "collider_collection": collider_collection.name if collection else None,
        "parameters": {
            "frame_start": 1,
            "frame_end": 24,
            "substeps": 1,
            "contact_iterations": 1,
            "constraint_iterations": 1,
            "seed": 42,
            "scatter_count": 0,
            "particle_radius": 0.2,
            "gravity": [0.0, 0.0, -9.81],
            "drag": 0.0,
            "restitution": 0.0,
            "friction": 0.0,
            "sleep_velocity": 0.0,
            "contact_thickness": 0.08,
            "ground_z": -10.0,
            "angular_spin": 0.0,
            "stiffness": 0.0,
            "anchor_pos": [0.0, 0.0, 2.5],
            "rest_length": 0.0,
            "wind_vector": [0.0, 0.0, 0.0],
            "wind_turbulence": 0.0,
        },
    }
    return (
        addon,
        bake,
        contract,
        geometry_evidence,
        scene,
        target,
        plane,
        parent,
        action,
        slot_a,
        slot_b,
        curve,
        request,
        evidence_dir,
    )


def run_bake(scenario: str, addon_parent: str, evidence_dir_arg: str) -> None:
    (
        addon,
        bake,
        contract,
        geometry_evidence,
        scene,
        target,
        plane,
        parent,
        action,
        slot_a,
        slot_b,
        curve,
        request,
        evidence_dir,
    ) = _build_scene(addon_parent, scenario)

    result = addon.execute_request(request)
    if not result.get("ok") or not (result.get("bake") or {}).get("baked"):
        raise RuntimeError(f"bake failed: {json.dumps(result, indent=2)[:12000]}")
    mod = target.modifiers["PhysicsNodes"]
    normalized = contract.normalize_request(request)
    params_hash = normalized["parameters_hash"]
    graph_fingerprint = result["workflow"]["graph_fingerprint"]
    valid, cache_reason, cache_path = bake.is_cache_valid(
        target, mod, params_hash, graph_fingerprint
    )
    if not valid:
        raise RuntimeError(f"new bake is not cache-valid: {cache_reason}")

    invalidation = []
    source_snapshot = _animation_snapshot(plane) if plane.animation_data else None
    state_before_checks = _scene_state()

    def cache_check(label: str, mutate, restore, expected_valid: bool):
        mutate()
        if label == "static_parent":
            bpy.context.view_layer.update()
        mutated_valid, mutated_reason, _ = bake.is_cache_valid(
            target, mod, params_hash, graph_fingerprint
        )
        mutated_state = _scene_state()
        restore()
        if label in {"static_parent", "non_animated_channel"}:
            bpy.context.view_layer.update()
        restored_valid, restored_reason, _ = bake.is_cache_valid(
            target, mod, params_hash, graph_fingerprint
        )
        invalidation.append(
            {
                "label": label,
                "valid_after_mutation": bool(mutated_valid),
                "reason": mutated_reason,
                "expected_valid": expected_valid,
                "restored_valid": bool(restored_valid),
                "restored_reason": restored_reason,
                "state_during_check": mutated_state,
            }
        )

    if plane.animation_data is not None:
        point = curve.keyframe_points[0]

        def mutate_keyframe():
            point.co[1] += 0.25

        def restore_keyframe():
            point.co[1] -= 0.25

        cache_check("keyframe_value", mutate_keyframe, restore_keyframe, False)

        def mutate_handle():
            point.handle_left[0] -= 0.5

        def restore_handle():
            point.handle_left[0] += 0.5

        cache_check("keyframe_handle", mutate_handle, restore_handle, False)

        def mutate_non_animated_channel():
            plane.location.x += 0.25

        def restore_non_animated_channel():
            plane.location.x -= 0.25

        cache_check(
            "non_animated_channel",
            mutate_non_animated_channel,
            restore_non_animated_channel,
            False,
        )

        def mutate_static_parent():
            parent.location.x += 0.5

        def restore_static_parent():
            parent.location.x -= 0.5

        cache_check("static_parent", mutate_static_parent, restore_static_parent, False)

        def mutate_slot():
            plane.animation_data.action_slot = slot_b

        def restore_slot():
            plane.animation_data.action_slot = slot_a

        cache_check("slot_switch_same_length", mutate_slot, restore_slot, False)

        def mutate_nla():
            plane.animation_data.nla_tracks.new()

        def restore_nla():
            plane.animation_data.nla_tracks.remove(plane.animation_data.nla_tracks[0])

        cache_check("nla_track", mutate_nla, restore_nla, False)

        def mutate_driver():
            plane.animation_data.drivers.new(data_path="location", index=0)

        def restore_driver():
            plane.animation_data.drivers.remove(plane.animation_data.drivers[0])

        cache_check("driver", mutate_driver, restore_driver, False)

        def mutate_constraint():
            plane.constraints.new("COPY_LOCATION")

        def restore_constraint():
            plane.constraints.remove(plane.constraints[0])

        cache_check("constraint", mutate_constraint, restore_constraint, False)

    source_unchanged = (
        source_snapshot is None
        or _animation_snapshot(plane) == source_snapshot
    )
    checks_clean = all(
        item["valid_after_mutation"] == item["expected_valid"]
        and item["restored_valid"]
        and item["state_during_check"] == state_before_checks
        for item in invalidation
    )

    samples = []
    for frame in (24, 1, 12, 24):
        samples.append({"frame": frame, "summary": geometry_evidence.sample_frame(target, frame)})
    f24_origins = _origins(target, 24)
    f24_vertices = _evaluated_vertices(target, 24)

    # Validate at an unrelated frame/subframe, then save the animated output.
    scene.frame_set(13)
    scene.frame_subframe = 0.5
    state_before = _scene_state()
    valid_at_13, reason_at_13, _ = bake.is_cache_valid(
        target, mod, params_hash, graph_fingerprint
    )
    state_after = _scene_state()
    if not valid_at_13:
        raise RuntimeError(f"cache invalid at frame 13: {reason_at_13}")
    if state_before != state_after:
        raise RuntimeError(f"cache validation changed scene state: {state_before} -> {state_after}")

    output_blend = evidence_dir / ("output_" + scenario + ".blend")
    _save_blend(output_blend)

    payload = {
        "pass": bool(
            valid
            and valid_at_13
            and source_unchanged
            and checks_clean
            and state_before == state_after
        ),
        "scenario": scenario,
        "version": bpy.app.version_string,
        "build_hash": bpy.app.build_hash.decode("ascii", "replace")
        if isinstance(bpy.app.build_hash, (bytes, bytearray))
        else str(bpy.app.build_hash),
        "module": addon.__file__,
        "request": request,
        "parameters_hash": params_hash,
        "graph_fingerprint": graph_fingerprint,
        "bake": result["bake"],
        "cache_valid_after_bake": valid,
        "cache_reason_after_bake": cache_reason,
        "cache_path": str(cache_path),
        "cache_valid_at_frame_13": valid_at_13,
        "cache_reason_at_frame_13": reason_at_13,
        "scene_state_before_validation": state_before,
        "scene_state_after_validation": state_after,
        "invalidation": invalidation,
        "source_animation_unchanged": source_unchanged,
        "state_before_checks": state_before_checks,
        "invalidation_checks_clean": checks_clean,
        "samples": samples,
        "f24_origins": f24_origins,
        "f24_vertices": f24_vertices,
        "output_blend": str(output_blend),
        "output_blend_size": output_blend.stat().st_size,
        "blend_backup_exists": (output_blend.parent / (output_blend.name + "1")).exists(),
    }
    (evidence_dir / "build.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        "ANIMATED_COLLIDER_BAKE_PASS" if payload["pass"] else "ANIMATED_COLLIDER_BAKE_FAIL",
        scenario,
        json.dumps(payload["f24_origins"]),
    )
    if not payload["pass"]:
        raise SystemExit(1)


def run_cold(scenario: str, addon_parent: str, evidence_dir_arg: str) -> None:
    evidence_dir = Path(evidence_dir_arg).resolve()
    build = json.loads((evidence_dir / "build.json").read_text(encoding="utf-8"))
    sys.path.insert(0, str(Path(addon_parent).resolve()))
    import physics_nodes_workflow_addon as addon  # noqa: PLC0415
    from physics_nodes_workflow_addon import bake, contract, geometry_evidence  # noqa: PLC0415

    bpy.ops.wm.open_mainfile(filepath=str(evidence_dir / ("output_" + scenario + ".blend")), load_ui=False, use_scripts=False)
    target = bpy.data.objects[build["request"]["target_object"]]
    mod = target.modifiers["PhysicsNodes"]
    normalized = contract.normalize_request(build["request"])
    scene = bpy.context.scene
    scene.frame_set(13)
    scene.frame_subframe = 0.5
    state_before = _scene_state()
    valid, reason, cache_path = bake.is_cache_valid(
        target,
        mod,
        normalized["parameters_hash"],
        build["graph_fingerprint"],
    )
    state_after = _scene_state()

    samples = []
    matches = []
    full_matches = []
    validity_by_frame = []
    for index, frame in enumerate((24, 1, 12, 24)):
        scene.frame_set(frame)
        frame_state_before = _scene_state()
        frame_valid, frame_reason, _ = bake.is_cache_valid(
            target,
            mod,
            normalized["parameters_hash"],
            build["graph_fingerprint"],
        )
        frame_state_after = _scene_state()
        actual = geometry_evidence.sample_frame(target, frame)
        expected = build["samples"][index]
        validity_by_frame.append(
            {
                "frame": frame,
                "valid": bool(frame_valid),
                "reason": frame_reason,
                "state_preserved": frame_state_before == frame_state_after,
            }
        )
        samples.append({"frame": frame, "summary": actual})
        matches.append(actual["geometry_sha256"] == expected["summary"]["geometry_sha256"])
        full_matches.append(actual == expected["summary"])

    payload = {
        "pass": bool(
            valid
            and all(matches)
            and all(full_matches)
            and state_before == state_after
            and all(item["valid"] and item["state_preserved"] for item in validity_by_frame)
        ),
        "scenario": scenario,
        "cold_open": True,
        "version": bpy.app.version_string,
        "module": addon.__file__,
        "cache_valid": bool(valid),
        "cache_reason": reason,
        "cache_path": str(cache_path),
        "frames": [24, 1, 12, 24],
        "cache_validity_by_frame": validity_by_frame,
        "geometry_matches": matches,
        "full_geometry_matches": full_matches,
        "samples": samples,
        "f24_origins": _origins(target, 24),
        "scene_state_before_validation": state_before,
        "scene_state_after_validation": state_after,
        "scene_state_preserved": bool(
            state_before == state_after
            and all(item["state_preserved"] for item in validity_by_frame)
        ),
    }
    (evidence_dir / "cold.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        "ANIMATED_COLLIDER_COLD_PASS" if payload["pass"] else "ANIMATED_COLLIDER_COLD_FAIL",
        scenario,
        json.dumps(matches),
    )
    if not payload["pass"]:
        raise SystemExit(1)


def main() -> None:
    mode, addon_parent, evidence_dir = sys.argv[sys.argv.index("--") + 1 :]
    if mode.startswith("cold_"):
        scenario = mode[len("cold_") :]
        if scenario == "single":
            scenario = "single_animated"
        elif scenario == "collection":
            scenario = "collection_animated"
        run_cold(scenario, addon_parent, evidence_dir)
    else:
        run_bake(mode, addon_parent, evidence_dir)


if __name__ == "__main__":
    main()
