"""Build one real rigid/cloth scene and export it through the plugin Alembic path."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sys

import bpy

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import export_test_common as common  # noqa: E402
from physics_nodes_workflow_addon import contract, core  # noqa: E402


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def error_of(call):
    try:
        call()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def build_rigid(scene):
    mesh = common.grid_mesh("Emitter", 3.0, 3.0, 2, 2, 0.0)
    obj = bpy.data.objects.new("Emitter", mesh)
    scene.collection.objects.link(obj)
    obj.location = (0.0, 0.0, 2.2)
    scene.frame_start, scene.frame_end = 1, 8
    build_request = contract.normalize_request({
        "operation": "build",
        "preset_id": "rigid_drop_pack",
        "target_object": obj.name,
        "parameters": {
            "scatter_count": 5,
            "frame_start": 1,
            "frame_end": 8,
            "seed": 7,
            "sdf_voxel_size": 0.08,
        },
    })
    # Build the graph directly: execute_request's inspection would pre-roll
    # the simulation and mask whether the export path prepares frames itself.
    core.build_or_update_workflow(obj, build_request)
    export_parameters = {
        "scatter_count": 5,
        "frame_start": 3,
        "frame_end": 6,
        "seed": 7,
        "sdf_voxel_size": 0.08,
    }
    return obj, export_parameters


def build_cloth(scene):
    mesh = common.grid_mesh("Cloth", 2.0, 2.0, 10, 8, 0.0)
    obj = bpy.data.objects.new("Cloth", mesh)
    scene.collection.objects.link(obj)
    obj.location = (0.0, 0.0, 2.5)
    scene.frame_start, scene.frame_end = 1, 8
    build_request = contract.normalize_request({
        "operation": "build",
        "preset_id": "cloth_banner_wind",
        "target_object": obj.name,
        "parameters": {
            "frame_start": 1,
            "frame_end": 8,
            "seed": 3,
            "wind_vector": [3.0, 1.0, 0.0],
            "wind_turbulence": 1.5,
        },
    })
    core.build_or_update_workflow(obj, build_request)
    export_parameters = {
        "frame_start": 3,
        "frame_end": 6,
        "seed": 3,
        "wind_vector": [3.0, 1.0, 0.0],
        "wind_turbulence": 1.5,
    }
    return obj, export_parameters


def main() -> None:
    args = common.parse_args()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    scene = common.reset_scene()
    if args.case == "rigid":
        obj, export_parameters = build_rigid(scene)
        preset_id = "rigid_drop_pack"
    elif args.case == "cloth":
        obj, export_parameters = build_cloth(scene)
        preset_id = "cloth_banner_wind"
    else:
        raise ValueError(f"unknown case: {args.case}")

    old_frame = scene.frame_current
    old_subframe = scene.frame_subframe
    old_scene_range = [scene.frame_start, scene.frame_end]
    old_selection = sorted(item.name for item in bpy.context.selected_objects)
    old_active = bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None
    old_modifiers = [mod.name for mod in obj.modifiers]
    old_objects = {item.name for item in bpy.data.objects}
    old_groups = {group.name for group in bpy.data.node_groups}

    abc = workdir / f"{args.case}.abc"
    request = contract.normalize_request({
        "operation": "export",
        "preset_id": preset_id,
        "target_object": obj.name,
        "export_format": "ALEMBIC",
        "export_path": str(abc),
        "parameters": export_parameters,
    })
    result = common.pnw_export.export_workflow_result(obj, request)

    after_objects = {item.name for item in bpy.data.objects}
    after_groups = {group.name for group in bpy.data.node_groups}
    restored = {
        "frame": [scene.frame_current, scene.frame_subframe],
        "expected_frame": [old_frame, old_subframe],
        "scene_range": [scene.frame_start, scene.frame_end],
        "expected_scene_range": old_scene_range,
        "selection": sorted(item.name for item in bpy.context.selected_objects),
        "expected_selection": old_selection,
        "active": bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
        "expected_active": old_active,
        "modifiers": [mod.name for mod in obj.modifiers],
        "expected_modifiers": old_modifiers,
        "leaked_objects": sorted(after_objects - old_objects),
        "leaked_groups": sorted(after_groups - old_groups),
    }
    # Sample only after the unprimed export.  If the exporter had jumped over
    # simulation frames, these correctly stepped states would expose it.
    source_rows = common.sample_managed_frames(scene, obj, list(range(1, 9)))
    payload = {
        "ok": True,
        "case": args.case,
        "preset_id": preset_id,
        "target": obj.name,
        "target_matrix_world": [[round(float(v), 9) for v in row] for row in obj.matrix_world],
        "export_range": [int(export_parameters["frame_start"]), int(export_parameters["frame_end"])],
        "source_frames": source_rows,
        "export_result": result,
        "abc": str(abc),
        "abc_bytes": abc.stat().st_size if abc.is_file() else 0,
        "restore": restored,
    }
    existing_hash = sha256(abc)
    payload["existing_path_guard"] = {
        "error": error_of(lambda: common.pnw_export.export_workflow_result(obj, request)),
        "sha_before": existing_hash,
        "sha_after": sha256(abc),
    }
    payload["existing_path_guard"]["ok"] = bool(
        payload["existing_path_guard"]["error"]
        and "already exists" in payload["existing_path_guard"]["error"]
        and payload["existing_path_guard"]["sha_after"] == existing_hash
    )

    invalid = contract.normalize_request({
        "operation": "export",
        "preset_id": preset_id,
        "target_object": obj.name,
        "export_format": "ALEMBIC",
        "export_path": str(workdir / f"{args.case}_invalid_range.abc"),
        "parameters": {**export_parameters, "frame_start": 7, "frame_end": 9},
    })
    invalid_path = Path(invalid["export_path"])
    payload["invalid_range_guard"] = {
        "error": error_of(lambda: common.pnw_export.export_workflow_result(obj, invalid)),
        "path_exists": invalid_path.exists(),
    }
    payload["invalid_range_guard"]["ok"] = bool(
        payload["invalid_range_guard"]["error"]
        and "INVALID_EXPORT_RANGE" in payload["invalid_range_guard"]["error"]
        and not invalid_path.exists()
    )

    empty_mesh = bpy.data.meshes.new("EmptyAlembic")
    empty_obj = bpy.data.objects.new("EmptyAlembic", empty_mesh)
    scene.collection.objects.link(empty_obj)
    empty_request = contract.normalize_request({
        "operation": "export",
        "preset_id": preset_id,
        "target_object": empty_obj.name,
        "export_format": "ALEMBIC",
        "export_path": str(workdir / f"{args.case}_empty.abc"),
        "parameters": {"frame_start": 1, "frame_end": 2},
    })
    empty_path = Path(empty_request["export_path"])
    payload["empty_geometry_guard"] = {
        "error": error_of(lambda: common.pnw_export.export_workflow_result(empty_obj, empty_request)),
        "path_exists": empty_path.exists(),
    }
    payload["empty_geometry_guard"]["ok"] = bool(
        payload["empty_geometry_guard"]["error"]
        and "EMPTY_EXPORT" in payload["empty_geometry_guard"]["error"]
        and not empty_path.exists()
    )
    if args.out:
        common.write_json(Path(args.out), payload)
    print("ALEMBIC_EXPORT_CASE_BEGIN")
    print(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True))
    print("ALEMBIC_EXPORT_CASE_END")


main()
