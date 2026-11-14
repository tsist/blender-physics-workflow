"""Produce real OpenVDB exports, Blender Volume imports and explicit negative cases."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys

import bpy
import numpy as np
import openvdb
from mathutils import Vector

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import export_test_common as common  # noqa: E402
from physics_nodes_workflow_addon import contract, vdb_export  # noqa: E402


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cube_object(name="Cube"):
    mesh = bpy.data.meshes.new(name)
    verts = [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
             (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)]
    faces = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def request_for(obj, path, *, voxel=0.08, preset_id="fluid_fountain_sdf", frame=1):
    return contract.normalize_request({
        "operation": "export",
        "preset_id": preset_id,
        "target_object": obj.name,
        "export_format": "OPENVDB",
        "export_path": str(path),
        "export_frame": frame,
        "parameters": {"sdf_voxel_size": voxel, "frame_start": 1, "frame_end": max(1, frame)},
    })


def read_summary(path):
    grids, metadata = openvdb.readAll(str(path))
    if len(grids) != 1:
        return {"count": len(grids)}
    grid = grids[0]
    lo, hi = grid.evalActiveVoxelBoundingBox()
    shape = tuple(int(hi[i] - lo[i] + 1) for i in range(3))
    data = np.zeros(shape, np.float32)
    grid.copyToArray(data, ijk=tuple(int(v) for v in lo))
    return {
        "count": len(grids),
        "type": type(grid).__name__,
        "name": grid.name,
        "grid_class": str(grid.gridClass),
        "active_voxels": int(grid.activeVoxelCount()),
        "bbox": [list(lo), list(hi)],
        "world_bbox": [
            [round(float(v), 9) for v in grid.transform.indexToWorld(tuple(int(v) for v in lo))],
            [round(float(v), 9) for v in grid.transform.indexToWorld(tuple(int(v) for v in hi))],
        ],
        "transform": [
            [round(float(v), 12) for v in grid.transform.indexToWorld(i)]
            for i in [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
        ],
        "background": round(float(grid.background), 9),
        "value_min": round(float(data.min()), 9),
        "value_max": round(float(data.max()), 9),
        "finite": bool(np.isfinite(data).all()),
        "negative_voxels": int((data < 0).sum()),
        "positive_voxels": int((data > 0).sum()),
        "metadata": metadata,
    }


def sample_world_value(path, world_point):
    grids, _ = openvdb.readAll(str(path))
    grid = grids[0]
    lo, hi = grid.evalActiveVoxelBoundingBox()
    shape = tuple(int(hi[i] - lo[i] + 1) for i in range(3))
    data = np.zeros(shape, np.float32)
    grid.copyToArray(data, ijk=tuple(int(v) for v in lo))
    index = grid.transform.worldToIndex(tuple(float(v) for v in world_point))
    nearest = tuple(int(round(float(v))) for v in index)
    value = None
    if all(int(lo[i]) <= nearest[i] <= int(hi[i]) for i in range(3)):
        value = float(data[nearest[0] - int(lo[0]), nearest[1] - int(lo[1]), nearest[2] - int(lo[2])])
    return {
        "world": [round(float(v), 9) for v in world_point],
        "index": [round(float(v), 9) for v in index],
        "nearest": list(nearest),
        "value": value,
    }


def success_case(workdir):
    common.reset_scene()
    scene = bpy.context.scene
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=1.0, location=(0, 0, 0))
    obj = bpy.context.object
    obj.name = "TransformedSphere"
    obj.rotation_euler = (0.0, 0.0, math.radians(30.0))
    obj.scale = (2.0, 1.0, 1.5)
    obj.location = (1.0, 2.0, 3.0)
    bpy.context.view_layer.update()
    world = [obj.matrix_world @ vert.co for vert in obj.data.vertices]
    expected_min = [min(float(v[i]) for v in world) for i in range(3)]
    expected_max = [max(float(v[i]) for v in world) for i in range(3)]

    out = workdir / "transformed_sphere.vdb"
    result = common.pnw_export.export_workflow_result(obj, request_for(obj, out))
    readback = read_summary(out)
    volume_data = bpy.data.volumes.new("VdbSurface")
    volume_data.filepath = str(out)
    volume_obj = bpy.data.objects.new("VdbSurface", volume_data)
    scene.collection.objects.link(volume_obj)
    bpy.context.view_layer.update()
    evaluated = volume_obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    evaluated.data.grids.load()
    volume_info = {
        "grids_error": evaluated.data.grids.error_message,
        "frame_filepath": str(evaluated.data.grids.frame_filepath),
        "grids": [[item.name, item.data_type] for item in evaluated.data.grids],
    }
    blend = workdir / "volume_import.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    surface_world = [
        obj.matrix_world @ Vector((1.0, 0.0, 0.0)),
        obj.matrix_world @ Vector((0.0, 1.0, 0.0)),
        obj.matrix_world @ Vector((0.0, 0.0, 1.0)),
    ]
    return {
        "ok": True,
        "path": str(out),
        "sha256": sha256(out),
        "expected_world_min": expected_min,
        "expected_world_max": expected_max,
        "result": result,
        "readback": readback,
        "blender_volume": volume_info,
        "blend": str(blend),
        "world_probes": {
            "surface": [sample_world_value(out, point) for point in surface_world],
            "center": sample_world_value(out, obj.matrix_world @ Vector((0.0, 0.0, 0.0))),
        },
        "temp_files": sorted(path.name for path in workdir.iterdir() if path.suffix == ".tmp"),
    }


def fluid_case(workdir):
    common.reset_scene()
    scene = bpy.context.scene
    mesh = common.grid_mesh("FluidEmitter", 2.0, 2.0, 2, 2, 0.0)
    obj = bpy.data.objects.new("FluidEmitter", mesh)
    scene.collection.objects.link(obj)
    obj.location = (0.0, 0.0, 2.0)
    scene.frame_start, scene.frame_end = 1, 6
    built = common.execute_request({
        "operation": "build",
        "preset_id": "fluid_fountain_sdf",
        "target_object": obj.name,
        "parameters": {
            "frame_start": 1,
            "frame_end": 6,
            "emit_rate": 6,
            "emit_speed": 5.0,
            "emit_end_frame": 6,
            "particle_radius": 0.16,
            "sdf_voxel_size": 0.08,
            "seed": 303,
        },
    })
    if not built.get("ok"):
        return {"ok": False, "error": f"FLUID_BUILD_FAILED: {built}"}
    scene.frame_set(6)
    realized, vertex_count, face_count = common.realized_world_vertices(obj)
    out = workdir / "fluid_surface.vdb"
    request = contract.normalize_request({
        "operation": "export",
        "preset_id": "fluid_fountain_sdf",
        "target_object": obj.name,
        "export_format": "OPENVDB",
        "export_path": str(out),
        "export_frame": 6,
        "parameters": {
            "frame_start": 1,
            "frame_end": 6,
            "emit_rate": 6,
            "emit_speed": 5.0,
            "emit_end_frame": 6,
            "particle_radius": 0.16,
            "sdf_voxel_size": 0.08,
            "seed": 303,
        },
    })
    result = common.pnw_export.export_workflow_result(obj, request)
    readback = read_summary(out)
    return {
        "ok": True,
        "path": str(out),
        "sha256": sha256(out),
        "source_vertex_count": vertex_count,
        "source_face_count": face_count,
        "source_bbox": common.bbox(realized),
        "result": result,
        "readback": readback,
        "temp_files": sorted(path.name for path in workdir.iterdir() if path.suffix == ".tmp"),
    }


def smoke_case(workdir):
    common.reset_scene()
    scene = bpy.context.scene
    mesh = common.grid_mesh("SmokeEmitter", 2.0, 2.0, 2, 2, 0.0)
    obj = bpy.data.objects.new("SmokeEmitter", mesh)
    scene.collection.objects.link(obj)
    obj.location = (0.0, 0.0, 1.0)
    scene.frame_start, scene.frame_end = 1, 6
    parameters = {
        "frame_start": 1,
        "frame_end": 6,
        "emit_rate": 6,
        "emit_speed": 2.0,
        "emit_end_frame": 6,
        "sdf_voxel_size": 0.1,
        "seed": 505,
    }
    built = common.execute_request({
        "operation": "build",
        "preset_id": "smoke_plume_volume",
        "target_object": obj.name,
        "parameters": parameters,
    })
    if not built.get("ok"):
        return {"ok": False, "error": f"SMOKE_BUILD_FAILED: {built}"}
    scene.frame_set(6)
    realized, vertex_count, face_count = common.realized_world_vertices(obj)
    out = workdir / "smoke_surface.vdb"
    request = contract.normalize_request({
        "operation": "export",
        "preset_id": "smoke_plume_volume",
        "target_object": obj.name,
        "export_format": "OPENVDB",
        "export_path": str(out),
        "export_frame": 6,
        "parameters": parameters,
    })
    result = common.pnw_export.export_workflow_result(obj, request)
    readback = read_summary(out)
    return {
        "ok": True,
        "path": str(out),
        "sha256": sha256(out),
        "source_vertex_count": vertex_count,
        "source_face_count": face_count,
        "source_bbox": common.bbox(realized),
        "result": result,
        "readback": readback,
        "temp_files": sorted(path.name for path in workdir.iterdir() if path.suffix == ".tmp"),
    }


def error_of(call):
    try:
        call()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def negative_cases(workdir):
    results = {}

    common.reset_scene()
    obj = cube_object("ExistingPathCube")
    path = workdir / "existing.vdb"
    path.write_bytes(b"SENTINEL-DO-NOT-OVERWRITE")
    before = sha256(path)
    error = error_of(lambda: common.pnw_export.export_workflow_result(obj, request_for(obj, path)))
    results["existing_path"] = {
        "error": error,
        "sha_before": before,
        "sha_after": sha256(path),
        "bytes_unchanged": path.read_bytes() == b"SENTINEL-DO-NOT-OVERWRITE",
        "ok": bool(error and "already exists" in error and path.read_bytes() == b"SENTINEL-DO-NOT-OVERWRITE"),
    }

    common.reset_scene()
    mesh = common.grid_mesh("OpenCloth", 2.0, 2.0, 4, 4, 0.0)
    obj = bpy.data.objects.new("OpenCloth", mesh)
    bpy.context.scene.collection.objects.link(obj)
    path = workdir / "open_cloth.vdb"
    error = error_of(lambda: common.pnw_export.export_workflow_result(obj, request_for(obj, path)))
    results["open_surface"] = {
        "error": error,
        "path_exists": path.exists(),
        "ok": bool(error and "UNSUPPORTED_EXPORT_TOPOLOGY" in error and not path.exists()),
    }

    common.reset_scene()
    mesh = bpy.data.meshes.new("NonManifold")
    mesh.from_pydata(
        [(0, 0, 0), (0, 1, 0), (0, 0, 1), (1, 0, 0), (0, 0, -1)],
        [],
        [(0, 1, 2), (0, 1, 3), (0, 1, 4)],
    )
    mesh.update()
    obj = bpy.data.objects.new("NonManifold", mesh)
    bpy.context.scene.collection.objects.link(obj)
    path = workdir / "nonmanifold.vdb"
    error = error_of(lambda: common.pnw_export.export_workflow_result(obj, request_for(obj, path)))
    results["nonmanifold"] = {
        "error": error,
        "path_exists": path.exists(),
        "ok": bool(error and "non-manifold" in error and not path.exists()),
    }

    common.reset_scene()
    empty_mesh = bpy.data.meshes.new("EmptyMesh")
    obj = bpy.data.objects.new("EmptyMesh", empty_mesh)
    bpy.context.scene.collection.objects.link(obj)
    path = workdir / "empty.vdb"
    error = error_of(lambda: common.pnw_export.export_workflow_result(obj, request_for(obj, path)))
    results["empty_mesh"] = {
        "error": error,
        "path_exists": path.exists(),
        "ok": bool(error and "EMPTY_EXPORT" in error and not path.exists()),
    }

    common.reset_scene()
    obj = cube_object("NonFiniteCube")
    coords = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", coords)
    coords[0] = np.nan
    obj.data.vertices.foreach_set("co", coords)
    path = workdir / "nonfinite.vdb"
    error = error_of(lambda: common.pnw_export.export_workflow_result(obj, request_for(obj, path)))
    results["nonfinite"] = {
        "error": error,
        "path_exists": path.exists(),
        "ok": bool(error and "NONFINITE_EXPORT" in error and not path.exists()),
    }

    common.reset_scene()
    obj = cube_object("HugeCube")
    obj.scale = (5000.0, 5000.0, 5000.0)
    bpy.context.view_layer.update()
    path = workdir / "resource_limit.vdb"
    error = error_of(lambda: common.pnw_export.export_workflow_result(
        obj, request_for(obj, path, voxel=0.01)
    ))
    results["resource_limit"] = {
        "error": error,
        "path_exists": path.exists(),
        "ok": bool(error and "RESOURCE_LIMIT" in error and not path.exists()),
    }

    common.reset_scene()
    obj = cube_object("InjectedVerifyCube")
    path = workdir / "injected_verify.vdb"
    original = vdb_export._verify_written_grid

    def fail_verify(*args, **kwargs):
        raise RuntimeError("injected verification failure")

    vdb_export._verify_written_grid = fail_verify
    try:
        error = error_of(lambda: common.pnw_export.export_workflow_result(obj, request_for(obj, path)))
    finally:
        vdb_export._verify_written_grid = original
    results["verification_gate"] = {
        "error": error,
        "path_exists": path.exists(),
        "temp_files": sorted(item.name for item in workdir.iterdir() if item.suffix == ".tmp"),
        "ok": bool(
            error
            and "injected verification failure" in error
            and not path.exists()
            and not [item for item in workdir.iterdir() if item.suffix == ".tmp"]
        ),
    }

    common.reset_scene()
    obj = cube_object("SourcePathCube")
    source = workdir / "source.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(source))
    source_sha = sha256(source)
    error = error_of(lambda: common.pnw_export.export_workflow_result(obj, request_for(obj, source)))
    results["source_blend"] = {
        "error": error,
        "source_sha_before": source_sha,
        "source_sha_after": sha256(source),
        "ok": bool(
            error
            and "source blend" in error
            and sha256(source) == source_sha
        ),
    }
    return results


def main() -> None:
    args = common.parse_args()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "success": success_case(workdir),
        "fluid": fluid_case(workdir),
        "smoke": smoke_case(workdir),
        "negative": negative_cases(workdir),
    }
    common.write_json(Path(args.out) if args.out else workdir / "produce.json", payload)
    print("OPENVDB_PRODUCE_BEGIN")
    print(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True))
    print("OPENVDB_PRODUCE_END")


main()
