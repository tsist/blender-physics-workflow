"""Regression test: contact response velocity is captured before SetPosition."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import bpy


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from physics_nodes_workflow_addon import subgroups  # noqa: E402


def set_inputs(mod, values):
    tree = mod.node_group
    for item in tree.interface.items_tree:
        if item.item_type != "SOCKET" or item.in_out != "INPUT":
            continue
        if item.name in values:
            getattr(mod.properties.inputs, item.identifier).value = values[item.name]


def build_fluid_harness():
    solver = subgroups.ensure_solver_fluid_particle_step(force_rebuild=True)
    tree = bpy.data.node_groups.new("FluidVelocityHarness", "GeometryNodeTree")
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    node = tree.nodes.new("GeometryNodeGroup")
    node.node_tree = solver
    tree.links.new(gin.outputs["Geometry"], node.inputs["Geometry"])
    node.inputs["Delta Time"].default_value = 0.01
    node.inputs["Time"].default_value = 0.0
    node.inputs["Gravity"].default_value = (0.0, 0.0, 0.0)
    node.inputs["Drag"].default_value = 0.0
    node.inputs["Wind Vector"].default_value = (0.0, 0.0, 0.0)
    node.inputs["Wind Turbulence"].default_value = 0.0
    node.inputs["Contact Thickness"].default_value = 0.05
    node.inputs["Ground Z"].default_value = 0.0
    node.inputs["Restitution"].default_value = 0.35
    node.inputs["Friction"].default_value = 0.0
    tree.links.new(node.outputs["Geometry"], gout.inputs["Geometry"])
    return tree


def run_case(name, tree, collider, z, velocity):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0.0, 0.0, z)], [], [])
    mesh.update()
    vel = mesh.attributes.new("pnw_velocity", "FLOAT_VECTOR", "POINT")
    vel.data[0].vector = velocity
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    mod = obj.modifiers.new(name, "NODES")
    mod.node_group = tree
    set_inputs(
        mod,
        {
            "Collider": collider,
            "Contact Thickness": 0.04,
            "Radius": 0.04,
            "Restitution": 0.35,
            "Friction": 0.0,
            "Sleep Velocity": 0.0,
        },
    )
    obj.update_tag()
    bpy.context.view_layer.update()
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    evaluated_mesh = evaluated.to_mesh()
    position = list(evaluated_mesh.vertices[0].co)
    result_velocity = list(evaluated_mesh.attributes["pnw_velocity"].data[0].vector)
    evaluated.to_mesh_clear()
    return {"position": position, "velocity": result_velocity}


def run_fluid_case(name, tree, z, velocity):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0.0, 0.0, z)], [], [])
    mesh.update()
    vel = mesh.attributes.new("pnw_velocity", "FLOAT_VECTOR", "POINT")
    vel.data[0].vector = velocity
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    mod = obj.modifiers.new(name, "NODES")
    mod.node_group = tree
    obj.update_tag()
    bpy.context.view_layer.update()
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    evaluated_mesh = evaluated.to_mesh()
    position = list(evaluated_mesh.vertices[0].co)
    result_velocity = list(evaluated_mesh.attributes["pnw_velocity"].data[0].vector)
    evaluated.to_mesh_clear()
    return {"position": position, "velocity": result_velocity}


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.render.fps = 24

    plane_mesh = bpy.data.meshes.new("ContactPlane")
    plane_mesh.from_pydata(
        [(-5.0, -5.0, 0.0), (5.0, -5.0, 0.0), (5.0, 5.0, 0.0), (-5.0, 5.0, 0.0)],
        [],
        [(0, 1, 2, 3)],
    )
    plane_mesh.update()
    plane = bpy.data.objects.new("ContactPlane", plane_mesh)
    bpy.context.scene.collection.objects.link(plane)

    trees = {
        "mesh": subgroups.ensure_collide_mesh_surface_geo(force_rebuild=True),
        "ground": subgroups.ensure_collide_ground_plane_geo(force_rebuild=True),
    }
    cases = {}
    for kind, tree in trees.items():
        cases[kind] = {
            "penetrating": run_case(f"{kind}_pen", tree, plane, 0.01, (0.0, 0.0, -1.0)),
            "fast": run_case(f"{kind}_fast", tree, plane, 0.02, (0.0, 0.0, -3.0)),
            "no_contact": run_case(f"{kind}_free", tree, plane, 0.20, (0.0, 0.0, -1.0)),
        }
    fluid_tree = build_fluid_harness()
    cases["fluid_step"] = {
        "penetrating": run_fluid_case("fluid_pen", fluid_tree, 0.01, (0.0, 0.0, -1.0)),
        "fast": run_fluid_case("fluid_fast", fluid_tree, 0.02, (0.0, 0.0, -3.0)),
        "no_contact": run_fluid_case("fluid_free", fluid_tree, 0.20, (0.0, 0.0, -1.0)),
    }

    checks = {}
    tolerance = 1e-5
    for kind in ("mesh", "ground"):
        pen = cases[kind]["penetrating"]
        fast = cases[kind]["fast"]
        free = cases[kind]["no_contact"]
        checks[f"{kind}_position_clamped"] = abs(pen["position"][2] - 0.04) < tolerance
        checks[f"{kind}_bounce_velocity"] = abs(pen["velocity"][2] - 0.35) < tolerance
        checks[f"{kind}_fast_bounce_velocity"] = abs(fast["velocity"][2] - 1.05) < tolerance
        checks[f"{kind}_no_contact_position"] = abs(free["position"][2] - 0.20) < tolerance
        checks[f"{kind}_no_contact_velocity"] = abs(free["velocity"][2] + 1.0) < tolerance

    # The repaired graphs must keep the capture node in the executable path.
    checks["mesh_capture_present"] = any(
        node.bl_idname == "GeometryNodeCaptureAttribute" for node in trees["mesh"].nodes
    )
    checks["ground_capture_present"] = any(
        node.bl_idname == "GeometryNodeCaptureAttribute" for node in trees["ground"].nodes
    )
    fluid = cases["fluid_step"]
    checks["fluid_position_clamped"] = abs(fluid["penetrating"]["position"][2] - 0.05) < tolerance
    checks["fluid_bounce_velocity"] = abs(fluid["penetrating"]["velocity"][2] - 0.35) < tolerance
    checks["fluid_fast_bounce_velocity"] = abs(fluid["fast"]["velocity"][2] - 1.05) < tolerance
    # One advection step at dt=0.01: 0.20 + (-1.0 * 0.01) = 0.19.
    checks["fluid_no_contact_position"] = abs(fluid["no_contact"]["position"][2] - 0.19) < tolerance
    checks["fluid_no_contact_velocity"] = abs(fluid["no_contact"]["velocity"][2] + 1.0) < tolerance
    checks["fluid_capture_present"] = any(
        node.bl_idname == "GeometryNodeCaptureAttribute"
        for node in subgroups.ensure_solver_fluid_particle_step().nodes
    )
    report = {
        "cases": cases,
        "checks": checks,
        "passed": all(checks.values()),
    }
    out = os.environ.get("PNW_TEST_OUT")
    if out:
        Path(out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("PNW_CONTACT_VELOCITY_TEST_BEGIN")
    print(json.dumps(report, indent=2))
    print("PNW_CONTACT_VELOCITY_TEST_END")
    if not report["passed"]:
        raise SystemExit("PNW contact velocity checks failed")


if __name__ == "__main__":
    main()
