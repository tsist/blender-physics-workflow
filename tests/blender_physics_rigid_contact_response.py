"""Regression for a rigid collider whose nearest face changes after projection.

Run with Blender --background --factory-startup --disable-autoexec
--python-exit-code 1 --python THIS_FILE -- OUTPUT_DIRECTORY.
"""
from pathlib import Path
import json
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from physics_nodes_workflow_addon import subgroups


def main():
    out = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mesh = bpy.data.meshes.new("FloorAndWall")
    mesh.from_pydata(
        [(-5, -5, 0), (5, -5, 0), (5, 5, 0), (-5, 5, 0),
         (0, -5, -5), (0, 5, -5), (0, 5, 5), (0, -5, 5)],
        [], [(0, 1, 2, 3), (4, 5, 6, 7)],
    )
    mesh.update()
    collider = bpy.data.objects.new("FloorAndWall", mesh)
    bpy.context.scene.collection.objects.link(collider)

    tree = bpy.data.node_groups.new("RigidContactHarness", "GeometryNodeTree")
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gin, gout = tree.nodes.new("NodeGroupInput"), tree.nodes.new("NodeGroupOutput")
    contact = tree.nodes.new("GeometryNodeGroup")
    contact.node_tree = subgroups.ensure_collide_mesh_geometry(force_rebuild=True)
    info = tree.nodes.new("GeometryNodeObjectInfo")
    info.inputs["Object"].default_value = collider
    info.transform_space = "ORIGINAL"
    tree.links.new(gin.outputs["Geometry"], contact.inputs["Geometry"])
    tree.links.new(info.outputs["Geometry"], contact.inputs["Collider Geometry"])
    tree.links.new(contact.outputs["Geometry"], gout.inputs["Geometry"])
    contact.inputs["Velocity"].default_value = (-1, 0, -1)
    contact.inputs["Radius"].default_value = 0.04
    contact.inputs["Restitution"].default_value = 0.35
    contact.inputs["Friction"].default_value = 0.0
    contact.inputs["Sleep Velocity"].default_value = 0.0

    points = [(0.01, 0, 0.005), (0.5, 0, 0.005), (0.5, 0, 0.2)]
    mesh = bpy.data.meshes.new("ContactPoints")
    mesh.from_pydata(points, [], [])
    mesh.update()
    velocity = mesh.attributes.new("pnw_velocity", "FLOAT_VECTOR", "POINT")
    for item in velocity.data:
        item.vector = (-1, 0, -1)
    obj = bpy.data.objects.new("ContactPoints", mesh)
    bpy.context.scene.collection.objects.link(obj)
    mod = obj.modifiers.new("ContactHarness", "NODES")
    mod.node_group = tree
    bpy.context.view_layer.update()
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    result_mesh = evaluated.to_mesh()
    try:
        rows = [
            {"position": list(vertex.co),
             "velocity": list(result_mesh.attributes["pnw_velocity"].data[index].vector)}
            for index, vertex in enumerate(result_mesh.vertices)
        ]
    finally:
        evaluated.to_mesh_clear()
    expected = [
        {"position": (0.01, 0, 0.04), "velocity": (-1, 0, 0.35)},
        {"position": (0.5, 0, 0.04), "velocity": (-1, 0, 0.35)},
        {"position": (0.5, 0, 0.2), "velocity": (-1, 0, -1)},
    ]
    names = ["nearest_face_switch", "single_floor", "no_contact"]
    checks = {
        name: all(abs(a - b) < 1e-5 for key in ("position", "velocity")
                  for a, b in zip(actual[key], target[key]))
        for name, actual, target in zip(names, rows, expected)
    }
    report = {
        "passed": all(checks.values()), "checks": checks, "cases": dict(zip(names, rows)),
        "expected": dict(zip(names, expected)), "blender_version": bpy.app.version_string,
        "module": str(Path(subgroups.__file__).resolve()),
        "template_revision": subgroups.SUBGROUP_REVISION,
        "scope": "Single contact response; not dense-contact convergence or continuous collision detection",
    }
    (out / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("RIGID_CONTACT_RESPONSE " + json.dumps(report))
    if not report["passed"]:
        raise SystemExit("Rigid contact response regression failed")


if __name__ == "__main__":
    main()
