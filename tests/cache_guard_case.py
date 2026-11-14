"""Verify our export paths still enforce the existing cache identity guard."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import bpy

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import export_test_common as common  # noqa: E402
from physics_nodes_workflow_addon import contract  # noqa: E402


def error_of(call):
    try:
        call()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def main() -> None:
    args = common.parse_args()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    scene = common.reset_scene()
    mesh = common.grid_mesh("CachedEmitter", 2.0, 2.0, 2, 2, 0.0)
    obj = bpy.data.objects.new("CachedEmitter", mesh)
    scene.collection.objects.link(obj)
    obj.location = (0.0, 0.0, 2.0)
    scene.frame_start, scene.frame_end = 1, 4
    bake_dir = workdir / "cache"
    built = common.execute_request({
        "operation": "build",
        "preset_id": "rigid_drop_pack",
        "target_object": obj.name,
        "bake": True,
        "bake_directory": str(bake_dir),
        "parameters": {
            "scatter_count": 3,
            "frame_start": 1,
            "frame_end": 4,
            "seed": 11,
            "sdf_voxel_size": 0.1,
        },
    })
    payload = {
        "build_ok": bool(built.get("ok")),
        "build_error": built.get("error"),
        "bake": built.get("bake"),
    }
    if not built.get("ok"):
        common.write_json(Path(args.out), payload)
        print("CACHE_GUARD_BEGIN")
        print(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True))
        print("CACHE_GUARD_END")
        return

    parameters = {
        "scatter_count": 3,
        "frame_start": 1,
        "frame_end": 4,
        "seed": 11,
        "sdf_voxel_size": 0.1,
    }
    first = workdir / "cached_surface.vdb"
    request = contract.normalize_request({
        "operation": "export",
        "preset_id": "rigid_drop_pack",
        "target_object": obj.name,
        "export_format": "OPENVDB",
        "export_path": str(first),
        "export_frame": 4,
        "parameters": parameters,
    })
    payload["cached_export"] = common.pnw_export.export_workflow_result(obj, request)

    cached_abc = workdir / "cached_sequence.abc"
    alembic_request = contract.normalize_request({
        "operation": "export",
        "preset_id": "rigid_drop_pack",
        "target_object": obj.name,
        "export_format": "ALEMBIC",
        "export_path": str(cached_abc),
        "parameters": parameters,
    })
    payload["cached_alembic"] = common.pnw_export.export_workflow_result(obj, alembic_request)

    # The cache identity captures the target matrix.  Changing it after the
    # bake must invalidate the cache and block a new export.
    obj.scale = (1.1, 1.1, 1.1)
    bpy.context.view_layer.update()
    second = workdir / "invalid_surface.vdb"
    request = contract.normalize_request({
        "operation": "export",
        "preset_id": "rigid_drop_pack",
        "target_object": obj.name,
        "export_format": "OPENVDB",
        "export_path": str(second),
        "export_frame": 4,
        "parameters": parameters,
    })
    error = error_of(lambda: common.pnw_export.export_workflow_result(obj, request))
    payload["invalid_cache"] = {
        "error": error,
        "path_exists": second.exists(),
        "ok": bool(error and "CACHE_INVALID" in error and not second.exists()),
    }
    invalid_abc = workdir / "invalid_sequence.abc"
    invalid_alembic_request = contract.normalize_request({
        "operation": "export",
        "preset_id": "rigid_drop_pack",
        "target_object": obj.name,
        "export_format": "ALEMBIC",
        "export_path": str(invalid_abc),
        "parameters": parameters,
    })
    alembic_error = error_of(lambda: common.pnw_export.export_workflow_result(obj, invalid_alembic_request))
    payload["invalid_alembic_cache"] = {
        "error": alembic_error,
        "path_exists": invalid_abc.exists(),
        "ok": bool(alembic_error and "CACHE_INVALID" in alembic_error and not invalid_abc.exists()),
    }
    common.write_json(Path(args.out), payload)
    print("CACHE_GUARD_BEGIN")
    print(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True))
    print("CACHE_GUARD_END")


main()
