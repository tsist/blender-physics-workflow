"""Minimal regression check for the unchanged static mesh export path."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import bpy

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import export_test_common as common  # noqa: E402
from physics_nodes_workflow_addon import contract  # noqa: E402


def main() -> None:
    args = common.parse_args()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    scene = common.reset_scene()
    mesh = bpy.data.meshes.new("StaticCube")
    mesh.from_pydata(
        [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
         (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)],
        [],
        [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)],
    )
    mesh.update()
    obj = bpy.data.objects.new("StaticCube", mesh)
    scene.collection.objects.link(obj)
    obj.location = (1.0, 2.0, 3.0)
    scene.frame_set(1)
    old_selection = sorted(item.name for item in bpy.context.selected_objects)
    old_active = bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None
    old_objects = {item.name for item in bpy.data.objects}
    old_groups = {group.name for group in bpy.data.node_groups}
    out = workdir / "static_copy.blend"
    request = contract.normalize_request({
        "operation": "export",
        "preset_id": "rigid_drop_pack",
        "target_object": obj.name,
        "export_format": "STATIC_MESH",
        "export_path": str(out),
        "export_frame": 1,
        "parameters": {"frame_start": 1, "frame_end": 1},
    })
    result = common.pnw_export.export_workflow_result(obj, request)
    payload = {
        "ok": True,
        "result": result,
        "path": str(out),
        "path_size": out.stat().st_size if out.is_file() else 0,
        "restore": {
            "frame": [scene.frame_current, scene.frame_subframe],
            "selection": sorted(item.name for item in bpy.context.selected_objects),
            "expected_selection": old_selection,
            "active": bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
            "expected_active": old_active,
            "leaked_objects": sorted({item.name for item in bpy.data.objects} - old_objects),
            "leaked_groups": sorted({group.name for group in bpy.data.node_groups} - old_groups),
        },
    }
    opened = []
    open_result = []
    if out.is_file():
        open_result = list(bpy.ops.wm.open_mainfile(filepath=str(out)))
        opened = sorted(item.name for item in bpy.context.scene.objects)
    payload["open_result"] = open_result
    payload["opened_objects"] = opened
    payload["ok"] = bool(
        result.get("exported")
        and result.get("vertex_count") == 8
        and result.get("face_count") == 6
        and payload["path_size"] > 0
        and "FINISHED" in open_result
        and "StaticCube" in opened
        and "StaticCube_StaticF1" in opened
        and payload["restore"]["frame"] == [1, 0]
        and payload["restore"]["selection"] == old_selection
        and payload["restore"]["active"] == old_active
        and not payload["restore"]["leaked_objects"]
        and not payload["restore"]["leaked_groups"]
    )
    common.write_json(Path(args.out), payload)
    print("STATIC_MESH_CASE_BEGIN")
    print(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True))
    print("STATIC_MESH_CASE_END")


main()
