"""A volume-only evaluated target must not be written as a fake Alembic mesh."""
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
    mesh = bpy.data.meshes.new("VolumeOnlyEmitter")
    mesh.from_pydata([(0, 0, 0), (0.1, 0, 0), (0, 0.1, 0)], [], [])
    mesh.update()
    obj = bpy.data.objects.new("VolumeOnlyEmitter", mesh)
    scene.collection.objects.link(obj)
    tree = bpy.data.node_groups.new("VolumeOnly", "GeometryNodeTree")
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    inp = tree.nodes.new("NodeGroupInput")
    out = tree.nodes.new("NodeGroupOutput")
    points = tree.nodes.new("GeometryNodeMeshToPoints")
    volume = tree.nodes.new("GeometryNodePointsToVolume")
    volume.inputs["Resolution Mode"].default_value = "Size"
    volume.inputs["Voxel Size"].default_value = 0.1
    volume.inputs["Radius"].default_value = 0.2
    tree.links.new(inp.outputs["Geometry"], points.inputs["Mesh"])
    tree.links.new(points.outputs["Points"], volume.inputs["Points"])
    tree.links.new(volume.outputs["Volume"], out.inputs["Geometry"])
    mod = obj.modifiers.new("VolumeOnly", "NODES")
    mod.node_group = tree
    scene.frame_start, scene.frame_end = 1, 2
    path = workdir / "volume_only.abc"
    request = contract.normalize_request({
        "operation": "export",
        "preset_id": "smoke_plume_volume",
        "target_object": obj.name,
        "export_format": "ALEMBIC",
        "export_path": str(path),
        "parameters": {"frame_start": 1, "frame_end": 2},
    })
    error = None
    try:
        common.pnw_export.export_workflow_result(obj, request)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    payload = {
        "ok": bool(error and "EMPTY_EXPORT" in error and not path.exists()),
        "error": error,
        "path_exists": path.exists(),
    }
    common.write_json(Path(args.out), payload)
    print("ALEMBIC_VOLUME_NEGATIVE_BEGIN")
    print(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True))
    print("ALEMBIC_VOLUME_NEGATIVE_END")


main()
