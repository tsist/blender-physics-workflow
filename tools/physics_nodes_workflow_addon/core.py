"""Core Geometry Node tree assembly (PNW_Wrapper + PNW_Core), parameter updates, and manual-edit protection."""

from __future__ import annotations

import json
import uuid
from typing import Any

import bpy

from . import bake as pnw_bake
from . import box_tipping as pnw_box_tipping
from . import bridge as pnw_bridge
from . import softbody as pnw_softbody
from . import subgroups as pnw_subgroups
from .capabilities import IMPLEMENTATION_VERSION


ROLE_KEY = "pnw_role"
STATE_PROP = "pnw_state_json"
FINGERPRINT_KEY = "pnw_last_fingerprint"

# Public socket definitions shared by Core and Wrapper node groups.
STANDARD_INPUT_SOCKETS: tuple[tuple[str, str, Any], ...] = (
    ("Geometry", "NodeSocketGeometry", None),
    ("Substeps", "NodeSocketInt", 4),
    ("Contact Iterations", "NodeSocketInt", 3),
    ("Constraint Iterations", "NodeSocketInt", 12),
    ("Seed", "NodeSocketInt", 42),
    ("Activation Frame", "NodeSocketInt", 0),
    ("Gravity", "NodeSocketVector", (0.0, 0.0, -9.81)),
    ("Drag", "NodeSocketFloat", 0.04),
    ("Wind Vector", "NodeSocketVector", (0.0, 0.0, 0.0)),
    ("Wind Turbulence", "NodeSocketFloat", 0.0),
    ("Stiffness", "NodeSocketFloat", 0.7),
    ("Restitution", "NodeSocketFloat", 0.35),
    ("Friction", "NodeSocketFloat", 0.45),
    ("Sleep Velocity", "NodeSocketFloat", 0.05),
    ("Contact Thickness", "NodeSocketFloat", 0.08),
    ("Particle Radius", "NodeSocketFloat", 0.12),
    ("Ground Z", "NodeSocketFloat", 0.0),
    ("Box Half Extents", "NodeSocketVector", (0.2, 0.3, 1.0)),
    ("Box Initial Tilt", "NodeSocketFloat", 0.3),
    ("Scatter Count", "NodeSocketInt", 48),
    ("Angular Spin", "NodeSocketFloat", 1.2),
    ("Anchor", "NodeSocketVector", (0.0, 0.0, 2.5)),
    ("Rest Length", "NodeSocketFloat", 0.0),
    ("Emit Rate", "NodeSocketInt", 24),
    ("Emit Speed", "NodeSocketFloat", 6.0),
    ("Emit End Frame", "NodeSocketInt", 90),
    ("Lifetime", "NodeSocketInt", 120),
    ("Buoyancy", "NodeSocketFloat", 0.0),
    ("Curve Resolution", "NodeSocketInt", 64),
    ("SDF Voxel Size", "NodeSocketFloat", 0.08),
    ("Collider", "NodeSocketObject", None),
    ("Collider Collection", "NodeSocketCollection", None),
    ("Instance Collection", "NodeSocketCollection", None),
    ("Normalize Instances", "NodeSocketBool", True),
    ("Bending", "NodeSocketBool", False),
    ("Bend Softness", "NodeSocketFloat", 0.1),
    ("Tearing", "NodeSocketBool", False),
    ("Tear Threshold", "NodeSocketFloat", 1.2),
)


def _socket_value_from_request(request: dict[str, Any], socket_name: str, obj: bpy.types.Object) -> Any:
    p = request["parameters"]
    mapping: dict[str, Any] = {
        "Substeps": int(p["substeps"]),
        "Contact Iterations": int(p["contact_iterations"]),
        "Constraint Iterations": int(p["constraint_iterations"]),
        "Seed": int(p["seed"]),
        "Activation Frame": int(p["activation_frame"]),
        "Normalize Instances": bool(p["instance_normalize"]),
        "Bending": p["bending"],
        "Bend Softness": p["bend_softness"],
        "Tearing": p["tearing"],
        "Tear Threshold": p["tear_threshold"],
        "Gravity": tuple(float(x) for x in p["gravity"]),
        "Drag": float(p["drag"]),
        "Wind Vector": tuple(float(x) for x in p["wind_vector"]),
        "Wind Turbulence": float(p["wind_turbulence"]),
        "Stiffness": float(p["stiffness"]),
        "Restitution": float(p["restitution"]),
        "Friction": float(p["friction"]),
        "Sleep Velocity": float(p["sleep_velocity"]),
        "Contact Thickness": float(p["contact_thickness"]),
        "Particle Radius": float(p["particle_radius"]),
        "Ground Z": float(p["ground_z"]),
        "Box Half Extents": tuple(float(x) for x in p.get("box_half_extents", (0.2, 0.3, 1.0))),
        "Box Initial Tilt": float(p.get("box_initial_tilt", 0.3)),
        "Scatter Count": int(p["scatter_count"]),
        "Angular Spin": float(p["angular_spin"]),
        "Anchor": tuple(float(x) for x in p["anchor_pos"]),
        "Rest Length": float(p["rest_length"]),
        "Emit Rate": int(p["emit_rate"]),
        "Emit Speed": float(p["emit_speed"]),
        "Emit End Frame": int(p["emit_end_frame"]),
        "Lifetime": int(p["lifetime"]),
        "Buoyancy": float(p["buoyancy"]),
        "Curve Resolution": int(p["curve_resolution"]),
        "SDF Voxel Size": float(p["sdf_voxel_size"]),
    }
    if socket_name == "Collider":
        cname = request.get("collider_object")
        if not cname:
            return None
        col_obj = bpy.data.objects.get(cname)
        if col_obj is None:
            raise ValueError(f"Collider object not found: {cname}")
        if col_obj == obj:
            raise ValueError("Collider object cannot reference target object itself")
        if col_obj.type != "MESH":
            raise ValueError("Collider must be a mesh surface")
        return col_obj
    if socket_name in {"Instance Collection", "Collider Collection"}:
        coll_name = request.get("instance_collection" if socket_name == "Instance Collection" else "collider_collection")
        if not coll_name:
            return None
        coll = bpy.data.collections.get(coll_name)
        if coll is None:
            raise ValueError(f"Collection not found: {coll_name}")
        if obj in list(coll.all_objects):
            raise ValueError("Referenced collection cannot contain the simulation target (dependency cycle)")
        return coll
    return mapping.get(socket_name)


def _set_modifier_socket(mod: bpy.types.Modifier, identifier: str, value: Any) -> None:
    try:
        sock_prop = getattr(mod.properties.inputs, identifier)
    except AttributeError:
        sock_prop = mod.properties.inputs[identifier]
    if hasattr(sock_prop, "value"):
        sock_prop.value = value
    else:
        sock_prop["value"] = value


def apply_request_parameters(obj: bpy.types.Object, mod: bpy.types.Modifier, request: dict[str, Any]) -> None:
    tree = mod.node_group
    if tree is None:
        raise ValueError("Modifier has no node_group assigned")
    for item in tree.interface.items_tree:
        if item.item_type != "SOCKET" or item.in_out != "INPUT" or item.socket_type == "NodeSocketGeometry":
            continue
        if item.name in {s[0] for s in STANDARD_INPUT_SOCKETS}:
            val = _socket_value_from_request(request, item.name, obj)
            _set_modifier_socket(mod, item.identifier, val)
    tree.interface_update(bpy.context)
    obj.update_tag()


def _surface_area_hint(obj: bpy.types.Object) -> float:
    if obj.type == "MESH" and obj.data is not None and len(obj.data.polygons) > 0:
        area = sum(p.area for p in obj.data.polygons)
        return max(float(area), 0.05)
    return 1.0


def _ensure_standard_interface(tree: bpy.types.NodeTree) -> None:
    existing_in = {
        item.name
        for item in tree.interface.items_tree
        if item.item_type == "SOCKET" and item.in_out == "INPUT"
    }
    for name, sock_type, default_val in STANDARD_INPUT_SOCKETS:
        if name not in existing_in:
            pnw_subgroups.add_socket(tree, name, "INPUT", sock_type, default_val)
    existing_out = {
        item.name
        for item in tree.interface.items_tree
        if item.item_type == "SOCKET" and item.in_out == "OUTPUT"
    }
    if "Geometry" not in existing_out:
        pnw_subgroups.add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")


def _build_core_tree(
    core_name: str,
    obj: bpy.types.Object,
    request: dict[str, Any],
    subgroups: dict[str, bpy.types.NodeTree],
    force_rebuild: bool = False,
) -> bpy.types.NodeTree:
    domain = request["domain"]
    preset_id = request["preset_id"]

    # Never rewrite a datablock selected by a display name. Explicit rebuilds
    # allocate a replacement; the previous graph remains available for recovery.
    tree = bpy.data.node_groups.new(core_name, "GeometryNodeTree")

    tree[ROLE_KEY] = "core"
    _ensure_standard_interface(tree)

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-1600, 0)
    gout.location = (1800, 0)

    # Pre-simulation setup per domain
    area_hint = _surface_area_hint(obj)

    if domain == "rigid_scatter":
        # Convert mesh faces -> distributed points if Scatter Count > 0 and mesh has faces, else mesh->points
        dom_sz = tree.nodes.new("GeometryNodeAttributeDomainSize")
        dom_sz.component = "MESH"
        dom_sz.location = (-1350, 220)
        tree.links.new(gin.outputs["Geometry"], dom_sz.inputs["Geometry"])

        has_faces = tree.nodes.new("ShaderNodeMath")
        has_faces.operation = "GREATER_THAN"
        has_faces.inputs[1].default_value = 0.0
        has_faces.location = (-1160, 240)
        tree.links.new(dom_sz.outputs["Face Count"], has_faces.inputs[0])

        want_scatter = tree.nodes.new("ShaderNodeMath")
        want_scatter.operation = "GREATER_THAN"
        want_scatter.inputs[1].default_value = 0.0
        want_scatter.location = (-1160, 100)
        tree.links.new(gin.outputs["Scatter Count"], want_scatter.inputs[0])

        use_dist = tree.nodes.new("ShaderNodeMath")
        use_dist.operation = "MULTIPLY"
        use_dist.location = (-980, 180)
        tree.links.new(has_faces.outputs["Value"], use_dist.inputs[0])
        tree.links.new(want_scatter.outputs["Value"], use_dist.inputs[1])

        dens = tree.nodes.new("ShaderNodeMath")
        dens.operation = "DIVIDE"
        dens.inputs[1].default_value = area_hint
        dens.location = (-1160, -40)
        tree.links.new(gin.outputs["Scatter Count"], dens.inputs[0])

        dist = tree.nodes.new("GeometryNodeDistributePointsOnFaces")
        dist.distribute_method = "RANDOM"
        dist.location = (-980, -20)
        tree.links.new(gin.outputs["Geometry"], dist.inputs["Mesh"])
        tree.links.new(dens.outputs["Value"], dist.inputs["Density"])
        tree.links.new(gin.outputs["Seed"], dist.inputs["Seed"])

        m2p = tree.nodes.new("GeometryNodeMeshToPoints")
        m2p.location = (-980, -220)
        tree.links.new(gin.outputs["Geometry"], m2p.inputs["Mesh"])

        sw_pts = tree.nodes.new("GeometryNodeSwitch")
        sw_pts.input_type = "GEOMETRY"
        sw_pts.location = (-780, 20)
        tree.links.new(use_dist.outputs["Value"], sw_pts.inputs["Switch"])
        tree.links.new(m2p.outputs["Points"], sw_pts.inputs["False"])
        tree.links.new(dist.outputs["Points"], sw_pts.inputs["True"])
        if obj.type in {"CURVE", "CURVES"}:
            curve_points = tree.nodes.new("GeometryNodeCurveToPoints")
            tree.links.new(gin.outputs["Geometry"], curve_points.inputs["Curve"])
            tree.links.new(gin.outputs["Curve Resolution"], curve_points.inputs["Count"])
            tree.links.new(curve_points.outputs["Points"], sw_pts.inputs["False"])

        st_vel = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_vel.data_type = "FLOAT_VECTOR"
        st_vel.domain = "POINT"
        st_vel.inputs["Name"].default_value = "pnw_velocity"
        st_vel.inputs["Value"].default_value = (0.0, 0.0, 0.0)
        st_vel.location = (-580, 20)
        tree.links.new(sw_pts.outputs["Output"], st_vel.inputs["Geometry"])

        st_rot = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_rot.data_type = "FLOAT_VECTOR"
        st_rot.domain = "POINT"
        st_rot.inputs["Name"].default_value = "pnw_rotation"
        st_rot.location = (-400, 20)
        tree.links.new(st_vel.outputs["Geometry"], st_rot.inputs["Geometry"])
        if preset_id == pnw_box_tipping.PRESET_ID:
            # Isolated planar preset: deterministic initial tilt about Y.  The
            # explicit persistent angular velocity starts at zero; it is only
            # changed by real contact impulses in PN_Solver_BoxFloorStep.
            initial_tilt = tree.nodes.new("ShaderNodeCombineXYZ")
            initial_tilt.location = (-780, -200)
            initial_tilt.inputs["X"].default_value = 0.0
            tree.links.new(gin.outputs["Box Initial Tilt"], initial_tilt.inputs["Y"])
            initial_tilt.inputs["Z"].default_value = 0.0
            tree.links.new(initial_tilt.outputs["Vector"], st_rot.inputs["Value"])

            st_omega = tree.nodes.new("GeometryNodeStoreNamedAttribute")
            st_omega.data_type = "FLOAT_VECTOR"
            st_omega.domain = "POINT"
            st_omega.inputs["Name"].default_value = "pnw_omega"
            st_omega.inputs["Value"].default_value = (0.0, 0.0, 0.0)
            st_omega.location = (-220, 20)
            tree.links.new(st_rot.outputs["Geometry"], st_omega.inputs["Geometry"])
            pre_sim_geo = st_omega.outputs["Geometry"]
        else:
            # Seeded random initial rotation for the historical rigid presets.
            rv_rot = tree.nodes.new("FunctionNodeRandomValue")
            rv_rot.data_type = "FLOAT_VECTOR"
            rv_rot.inputs["Min"].default_value = (-1.5, -1.5, -1.5)
            rv_rot.inputs["Max"].default_value = (1.5, 1.5, 1.5)
            rv_rot.location = (-780, -200)
            tree.links.new(gin.outputs["Seed"], rv_rot.inputs["Seed"])
            tree.links.new(rv_rot.outputs["Value"], st_rot.inputs["Value"])
            pre_sim_geo = st_rot.outputs["Geometry"]

    elif domain == "cloth_rope":
        # Convert Curve to Mesh edges automatically if object is a curve; leave Mesh untouched
        is_curve_src = obj.type in {"CURVE", "CURVES"}
        if is_curve_src:
            resample = tree.nodes.new("GeometryNodeResampleCurve")
            tree.links.new(gin.outputs["Curve Resolution"], resample.inputs["Count"])
            tree.links.new(gin.outputs["Geometry"], resample.inputs["Curve"])
            c2m = tree.nodes.new("GeometryNodeCurveToMesh")
            c2m.location = (-1250, 20)
            tree.links.new(resample.outputs["Curve"], c2m.inputs["Curve"])
            base_geo_out = c2m.outputs["Mesh"]
        else:
            base_geo_out = gin.outputs["Geometry"]

        pos_pre = tree.nodes.new("GeometryNodeInputPosition")
        pos_pre.location = (-1250, -160)

        st_rest = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_rest.data_type = "FLOAT_VECTOR"
        st_rest.domain = "POINT"
        st_rest.inputs["Name"].default_value = "pnw_rest_pos"
        st_rest.location = (-1040, 20)
        tree.links.new(base_geo_out, st_rest.inputs["Geometry"])
        tree.links.new(pos_pre.outputs["Position"], st_rest.inputs["Value"])

        st_vel = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_vel.data_type = "FLOAT_VECTOR"
        st_vel.domain = "POINT"
        st_vel.inputs["Name"].default_value = "pnw_velocity"
        st_vel.inputs["Value"].default_value = (0.0, 0.0, 0.0)
        st_vel.location = (-840, 20)
        tree.links.new(st_rest.outputs["Geometry"], st_vel.inputs["Geometry"])

        # Read authored pins; the switch below uses zero when the attribute is absent.
        pin_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
        pin_attr.data_type = "FLOAT"
        pin_attr.inputs["Name"].default_value = "pnw_pinned"
        pin_attr.location = (-1040, -280)

        bbox = tree.nodes.new("GeometryNodeBoundBox")
        bbox.location = (-1040, -440)
        tree.links.new(base_geo_out, bbox.inputs["Geometry"])

        sep_max = tree.nodes.new("ShaderNodeSeparateXYZ")
        sep_max.location = (-860, -440)
        tree.links.new(bbox.outputs["Max"], sep_max.inputs["Vector"])

        band_z = tree.nodes.new("ShaderNodeMath")
        band_z.operation = "SUBTRACT"
        band_z.inputs[1].default_value = 0.08
        band_z.location = (-680, -440)
        tree.links.new(sep_max.outputs["Z"], band_z.inputs[0])

        sep_p = tree.nodes.new("ShaderNodeSeparateXYZ")
        sep_p.location = (-860, -280)
        tree.links.new(pos_pre.outputs["Position"], sep_p.inputs["Vector"])

        auto_pin = tree.nodes.new("ShaderNodeMath")
        auto_pin.operation = "GREATER_THAN"
        auto_pin.location = (-520, -340)
        tree.links.new(sep_p.outputs["Z"], auto_pin.inputs[0])
        tree.links.new(band_z.outputs["Value"], auto_pin.inputs[1])

        sw_pin = tree.nodes.new("GeometryNodeSwitch")
        sw_pin.input_type = "FLOAT"
        sw_pin.location = (-520, -160)
        tree.links.new(pin_attr.outputs["Exists"], sw_pin.inputs["Switch"])
        # Pinning is an authored attribute; a horizontal cloth must not become
        # completely frozen just because every point lies in the top Z band.
        sw_pin.inputs["False"].default_value = 0.0
        tree.links.new(pin_attr.outputs["Attribute"], sw_pin.inputs["True"])

        st_pin = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_pin.data_type = "FLOAT"
        st_pin.domain = "POINT"
        st_pin.inputs["Name"].default_value = "pnw_pinned"
        st_pin.location = (-400, 20)
        tree.links.new(st_vel.outputs["Geometry"], st_pin.inputs["Geometry"])
        tree.links.new(sw_pin.outputs["Output"], st_pin.inputs["Value"])
        pre_sim_geo = st_pin.outputs["Geometry"]

    else:  # fluid_emission
        # Start with empty point cloud; Simulation Zone spawns particles each frame <= Emit End Frame
        m2p_init = tree.nodes.new("GeometryNodeMeshToPoints")
        m2p_init.location = (-820, 40)
        tree.links.new(gin.outputs["Geometry"], m2p_init.inputs["Mesh"])

        del_all = tree.nodes.new("GeometryNodeDeleteGeometry")
        del_all.inputs["Selection"].default_value = True
        del_all.location = (-640, 40)
        tree.links.new(m2p_init.outputs["Points"], del_all.inputs["Geometry"])

        st_v0 = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_v0.data_type = "FLOAT_VECTOR"
        st_v0.domain = "POINT"
        st_v0.inputs["Name"].default_value = "pnw_velocity"
        st_v0.inputs["Value"].default_value = (0.0, 0.0, 0.0)
        st_v0.location = (-460, 40)
        tree.links.new(del_all.outputs["Geometry"], st_v0.inputs["Geometry"])

        st_a0 = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_a0.data_type = "FLOAT"
        st_a0.domain = "POINT"
        st_a0.inputs["Name"].default_value = "pnw_age"
        st_a0.inputs["Value"].default_value = 0.0
        st_a0.location = (-300, 40)
        tree.links.new(st_v0.outputs["Geometry"], st_a0.inputs["Geometry"])
        pre_sim_geo = st_a0.outputs["Geometry"]

    # Root Simulation Zone
    sim_in = tree.nodes.new("GeometryNodeSimulationInput")
    sim_out = tree.nodes.new("GeometryNodeSimulationOutput")
    sim_in.location = (-120, 0)
    sim_out.location = (1180, 0)
    sim_in.pair_with_output(sim_out)
    tree.links.new(pre_sim_geo, sim_in.inputs["Geometry"])

    scene_time = tree.nodes.new("GeometryNodeInputSceneTime")
    scene_time.location = (-120, -260)

    if domain == "rigid_scatter":
        # Native Skip preserves the entire previous simulation state, including
        # rotation and zero initial velocity, without advancing any solver step.
        # Activation is a whole-workflow gate; points are not separate bodies
        # coupled to other modifiers while waiting.
        waiting = tree.nodes.new("ShaderNodeMath")
        waiting.name = "PNW_ActivationWaiting"
        waiting.operation = "LESS_THAN"
        waiting.location = (-120, -820)
        tree.links.new(scene_time.outputs["Frame"], waiting.inputs[0])
        tree.links.new(gin.outputs["Activation Frame"], waiting.inputs[1])
        tree.links.new(waiting.outputs["Value"], sim_out.inputs["Skip"])
        active = tree.nodes.new("FunctionNodeBooleanMath")
        active.name = "PNW_ActivationEnabled"
        active.operation = "NOT"
        active.location = (80, -820)
        tree.links.new(waiting.outputs["Value"], active.inputs[0])
        initial_active = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        initial_active.name = "PNW_InitialActive"
        initial_active.data_type = "BOOLEAN"
        initial_active.domain = "POINT"
        initial_active.inputs["Name"].default_value = "pnw_active"
        tree.links.new(pre_sim_geo, initial_active.inputs["Geometry"])
        tree.links.new(active.outputs["Boolean"], initial_active.inputs["Value"])
        tree.links.new(initial_active.outputs["Geometry"], sim_in.inputs["Geometry"])

    # For fluid_emission, emit new particles per frame inside Simulation Zone and cull expired ones
    if domain == "fluid_emission":
        can_emit = tree.nodes.new("ShaderNodeMath")
        can_emit.operation = "LESS_THAN"
        can_emit.location = (40, 320)
        tree.links.new(scene_time.outputs["Frame"], can_emit.inputs[0])
        # Frame <= Emit End Frame -> Frame < Emit End Frame + 1
        end_plus = tree.nodes.new("ShaderNodeMath")
        end_plus.operation = "ADD"
        end_plus.inputs[1].default_value = 1.0
        end_plus.location = (-120, 340)
        tree.links.new(gin.outputs["Emit End Frame"], end_plus.inputs[0])
        tree.links.new(end_plus.outputs["Value"], can_emit.inputs[1])

        emit_seed = tree.nodes.new("ShaderNodeMath")
        emit_seed.operation = "ADD"
        emit_seed.location = (40, 180)
        tree.links.new(gin.outputs["Seed"], emit_seed.inputs[0])
        tree.links.new(scene_time.outputs["Frame"], emit_seed.inputs[1])

        emit_dens = tree.nodes.new("ShaderNodeMath")
        emit_dens.operation = "DIVIDE"
        emit_dens.inputs[1].default_value = area_hint
        emit_dens.location = (-120, 200)
        tree.links.new(gin.outputs["Emit Rate"], emit_dens.inputs[0])

        dist_emit = tree.nodes.new("GeometryNodeDistributePointsOnFaces")
        dist_emit.distribute_method = "RANDOM"
        dist_emit.location = (220, 220)
        tree.links.new(gin.outputs["Geometry"], dist_emit.inputs["Mesh"])
        tree.links.new(emit_dens.outputs["Value"], dist_emit.inputs["Density"])
        tree.links.new(emit_seed.outputs["Value"], dist_emit.inputs["Seed"])

        # Seeded cone velocity around +Z * Emit Speed
        rv_spray = tree.nodes.new("FunctionNodeRandomValue")
        rv_spray.data_type = "FLOAT_VECTOR"
        rv_spray.inputs["Min"].default_value = (-1.2, -1.2, 0.7)
        rv_spray.inputs["Max"].default_value = (1.2, 1.2, 1.3)
        rv_spray.location = (220, 420)
        tree.links.new(emit_seed.outputs["Value"], rv_spray.inputs["Seed"])

        v_emit = tree.nodes.new("ShaderNodeVectorMath")
        v_emit.operation = "SCALE"
        v_emit.location = (400, 420)
        tree.links.new(rv_spray.outputs["Value"], v_emit.inputs[0])
        tree.links.new(gin.outputs["Emit Speed"], v_emit.inputs["Scale"])

        st_ev = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_ev.data_type = "FLOAT_VECTOR"
        st_ev.domain = "POINT"
        st_ev.inputs["Name"].default_value = "pnw_velocity"
        st_ev.location = (420, 220)
        tree.links.new(dist_emit.outputs["Points"], st_ev.inputs["Geometry"])
        tree.links.new(v_emit.outputs["Vector"], st_ev.inputs["Value"])

        st_ea = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        st_ea.data_type = "FLOAT"
        st_ea.domain = "POINT"
        st_ea.inputs["Name"].default_value = "pnw_age"
        st_ea.inputs["Value"].default_value = 0.0
        st_ea.location = (600, 220)
        tree.links.new(st_ev.outputs["Geometry"], st_ea.inputs["Geometry"])

        sw_emit = tree.nodes.new("GeometryNodeSwitch")
        sw_emit.input_type = "GEOMETRY"
        sw_emit.location = (760, 220)
        tree.links.new(can_emit.outputs["Value"], sw_emit.inputs["Switch"])
        tree.links.new(st_ea.outputs["Geometry"], sw_emit.inputs["True"])

        # Join existing simulated particles with newly spawned ones
        join_sim = tree.nodes.new("GeometryNodeJoinGeometry")
        join_sim.location = (920, 120)
        tree.links.new(sim_in.outputs["Geometry"], join_sim.inputs["Geometry"])
        tree.links.new(sw_emit.outputs["Output"], join_sim.inputs["Geometry"])

        # Age is stored in seconds. Delta Time is one simulation frame and
        # follows the scene FPS/FPS Base rather than a fixed 24 fps assumption.
        age_read = tree.nodes.new("GeometryNodeInputNamedAttribute")
        age_read.data_type = "FLOAT"
        age_read.inputs["Name"].default_value = "pnw_age"
        age_read.location = (600, 420)

        life_sec = tree.nodes.new("ShaderNodeMath")
        life_sec.operation = "MULTIPLY"
        life_sec.location = (600, 560)
        tree.links.new(gin.outputs["Lifetime"], life_sec.inputs[0])
        tree.links.new(sim_in.outputs["Delta Time"], life_sec.inputs[1])

        expired = tree.nodes.new("ShaderNodeMath")
        expired.operation = "GREATER_THAN"
        expired.location = (780, 460)
        tree.links.new(age_read.outputs["Attribute"], expired.inputs[0])
        tree.links.new(life_sec.outputs["Value"], expired.inputs[1])

        cull_old = tree.nodes.new("GeometryNodeDeleteGeometry")
        cull_old.location = (920, 320)
        tree.links.new(join_sim.outputs["Geometry"], cull_old.inputs["Geometry"])
        tree.links.new(expired.outputs["Value"], cull_old.inputs["Selection"])
        sim_body_in_geo = cull_old.outputs["Geometry"]
    else:
        sim_body_in_geo = sim_in.outputs["Geometry"]

    # Inner Repeat Zone for Substeps
    rep_in = tree.nodes.new("GeometryNodeRepeatInput")
    rep_out = tree.nodes.new("GeometryNodeRepeatOutput")
    rep_in.location = (220, -40)
    rep_out.location = (920, -40)
    rep_in.pair_with_output(rep_out)
    tree.links.new(gin.outputs["Substeps"], rep_in.inputs["Iterations"])
    tree.links.new(sim_body_in_geo, rep_in.inputs["Geometry"])

    dt_sub = tree.nodes.new("ShaderNodeMath")
    dt_sub.operation = "DIVIDE"
    dt_sub.location = (180, -300)
    tree.links.new(sim_in.outputs["Delta Time"], dt_sub.inputs[0])
    tree.links.new(gin.outputs["Substeps"], dt_sub.inputs[1])

    # Convert world Ground Z into object-local Z so objects placed above world Z=0 fall all the way to the world ground
    self_obj = tree.nodes.new("GeometryNodeSelfObject")
    self_obj.location = (-20, -480)
    self_info = tree.nodes.new("GeometryNodeObjectInfo")
    self_info.transform_space = "ORIGINAL"
    self_info.location = (140, -480)
    tree.links.new(self_obj.outputs["Self Object"], self_info.inputs["Object"])

    sep_self = tree.nodes.new("ShaderNodeSeparateXYZ")
    sep_self.location = (300, -480)
    tree.links.new(self_info.outputs["Location"], sep_self.inputs["Vector"])

    local_ground_z = tree.nodes.new("ShaderNodeMath")
    local_ground_z.operation = "SUBTRACT"
    local_ground_z.location = (440, -480)
    tree.links.new(gin.outputs["Ground Z"], local_ground_z.inputs[0])
    tree.links.new(sep_self.outputs["Z"], local_ground_z.inputs[1])

    local_anchor = tree.nodes.new("ShaderNodeVectorMath")
    local_anchor.operation = "SUBTRACT"
    local_anchor.location = (440, -640)
    tree.links.new(gin.outputs["Anchor"], local_anchor.inputs[0])
    tree.links.new(self_info.outputs["Location"], local_anchor.inputs[1])

    # Call the domain solver subgroup inside Repeat Zone
    solver = tree.nodes.new("GeometryNodeGroup")
    solver.location = (560, -100)
    if domain == "rigid_scatter" and preset_id == pnw_box_tipping.PRESET_ID:
        # Independent box-floor impulse solver.  The historical sphere solver
        # is not placed in this graph at all.
        solver.node_tree = subgroups["PN_Solver_BoxFloorStep"]
        tree.links.new(rep_in.outputs["Geometry"], solver.inputs["Geometry"])
        tree.links.new(dt_sub.outputs["Value"], solver.inputs["Delta Time"])
        tree.links.new(gin.outputs["Gravity"], solver.inputs["Gravity"])
        tree.links.new(local_ground_z.outputs["Value"], solver.inputs["Ground Z"])
        tree.links.new(gin.outputs["Box Half Extents"], solver.inputs["Box Half Extents"])
        tree.links.new(gin.outputs["Restitution"], solver.inputs["Restitution"])
        tree.links.new(gin.outputs["Friction"], solver.inputs["Friction"])
    elif domain == "rigid_scatter":
        solver.node_tree = subgroups["PN_Solver_RigidScatterStep"]
        tree.links.new(rep_in.outputs["Geometry"], solver.inputs["Geometry"])
        tree.links.new(dt_sub.outputs["Value"], solver.inputs["Delta Time"])
        tree.links.new(scene_time.outputs["Seconds"], solver.inputs["Time"])
        tree.links.new(gin.outputs["Gravity"], solver.inputs["Gravity"])
        tree.links.new(gin.outputs["Drag"], solver.inputs["Drag"])
        tree.links.new(gin.outputs["Wind Vector"], solver.inputs["Wind Vector"])
        tree.links.new(gin.outputs["Wind Turbulence"], solver.inputs["Wind Turbulence"])
        tree.links.new(gin.outputs["Collider"], solver.inputs["Collider"])
        tree.links.new(gin.outputs["Contact Thickness"], solver.inputs["Contact Thickness"])
        tree.links.new(local_ground_z.outputs["Value"], solver.inputs["Ground Z"])
        tree.links.new(gin.outputs["Particle Radius"], solver.inputs["Radius"])
        tree.links.new(gin.outputs["Restitution"], solver.inputs["Restitution"])
        tree.links.new(gin.outputs["Friction"], solver.inputs["Friction"])
        tree.links.new(gin.outputs["Sleep Velocity"], solver.inputs["Sleep Velocity"])
        tree.links.new(gin.outputs["Angular Spin"], solver.inputs["Angular Spin"])
        tree.links.new(local_anchor.outputs["Vector"], solver.inputs["Anchor"])
        tree.links.new(gin.outputs["Rest Length"], solver.inputs["Rest Length"])
        tree.links.new(gin.outputs["Stiffness"], solver.inputs["Stiffness"])
    elif domain == "cloth_rope":
        solver.node_tree = subgroups["PN_Solver_ClothRopeStep"]
        solver.inputs["Elastic Shell"].default_value = preset_id == "softbody_elastic_shell"
        tree.links.new(rep_in.outputs["Geometry"], solver.inputs["Geometry"])
        tree.links.new(dt_sub.outputs["Value"], solver.inputs["Delta Time"])
        tree.links.new(scene_time.outputs["Seconds"], solver.inputs["Time"])
        tree.links.new(gin.outputs["Gravity"], solver.inputs["Gravity"])
        tree.links.new(gin.outputs["Drag"], solver.inputs["Drag"])
        tree.links.new(gin.outputs["Wind Vector"], solver.inputs["Wind Vector"])
        tree.links.new(gin.outputs["Wind Turbulence"], solver.inputs["Wind Turbulence"])
        tree.links.new(gin.outputs["Stiffness"], solver.inputs["Stiffness"])
        tree.links.new(gin.outputs["Collider"], solver.inputs["Collider"])
        tree.links.new(gin.outputs["Contact Thickness"], solver.inputs["Contact Thickness"])
        tree.links.new(local_ground_z.outputs["Value"], solver.inputs["Ground Z"])
        tree.links.new(gin.outputs["Restitution"], solver.inputs["Restitution"])
        tree.links.new(gin.outputs["Friction"], solver.inputs["Friction"])
        tree.links.new(gin.outputs["Sleep Velocity"], solver.inputs["Sleep Velocity"])
    else:  # fluid_emission
        solver.node_tree = subgroups["PN_Solver_FluidParticleStep"]
        tree.links.new(rep_in.outputs["Geometry"], solver.inputs["Geometry"])
        tree.links.new(dt_sub.outputs["Value"], solver.inputs["Delta Time"])
        tree.links.new(scene_time.outputs["Seconds"], solver.inputs["Time"])
        tree.links.new(gin.outputs["Gravity"], solver.inputs["Gravity"])
        tree.links.new(gin.outputs["Drag"], solver.inputs["Drag"])
        tree.links.new(gin.outputs["Wind Vector"], solver.inputs["Wind Vector"])
        tree.links.new(gin.outputs["Wind Turbulence"], solver.inputs["Wind Turbulence"])
        tree.links.new(gin.outputs["Collider"], solver.inputs["Collider"])
        tree.links.new(gin.outputs["Contact Thickness"], solver.inputs["Contact Thickness"])
        tree.links.new(local_ground_z.outputs["Value"], solver.inputs["Ground Z"])
        tree.links.new(gin.outputs["Restitution"], solver.inputs["Restitution"])
        tree.links.new(gin.outputs["Friction"], solver.inputs["Friction"])

    tree.links.new(solver.outputs["Geometry"], rep_out.inputs["Geometry"])
    tree.links.new(rep_out.outputs["Geometry"], sim_out.inputs["Geometry"])
    if domain == "rigid_scatter":
        # This state update is skipped together with the solver while waiting.
        # Store the observed state before instancing and disk bake.
        active_state = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        active_state.name = "PNW_SimulatedActive"
        active_state.data_type = "BOOLEAN"
        active_state.domain = "POINT"
        active_state.inputs["Name"].default_value = "pnw_active"
        active_state.inputs["Value"].default_value = True
        tree.links.new(rep_out.outputs["Geometry"], active_state.inputs["Geometry"])
        tree.links.new(active_state.outputs["Geometry"], sim_out.inputs["Geometry"])

    # Post-simulation stage outside Simulation Zone
    if domain == "rigid_scatter" and "Contact Iterations" in solver.inputs:
        tree.links.new(gin.outputs["Contact Iterations"], solver.inputs["Contact Iterations"])
    if domain == "cloth_rope" and "Constraint Iterations" in solver.inputs:
        tree.links.new(gin.outputs["Constraint Iterations"], solver.inputs["Constraint Iterations"])
    if domain == "cloth_rope" and "Rope Resample Count" in solver.inputs:
        tree.links.new(gin.outputs["Curve Resolution"], solver.inputs["Rope Resample Count"])
    if domain == "cloth_rope":
        for name in ("Bending", "Bend Softness", "Tearing", "Tear Threshold"):
            tree.links.new(gin.outputs[name], solver.inputs[name])
    if domain == "rigid_scatter":
        post = tree.nodes.new("GeometryNodeGroup")
        post.location = (1460, 0)
        if preset_id == pnw_box_tipping.PRESET_ID:
            # True-size cube instancing: dimensions are 2 * box_half_extents.
            # No 2r sphere normalization is involved.
            post.node_tree = subgroups["PN_Post_InstanceBox"]
            tree.links.new(sim_out.outputs["Geometry"], post.inputs["Points"])
            tree.links.new(gin.outputs["Box Half Extents"], post.inputs["Box Half Extents"])
        else:
            post.node_tree = subgroups["PN_Post_InstanceRigid"]
            tree.links.new(sim_out.outputs["Geometry"], post.inputs["Points"])
            tree.links.new(gin.outputs["Instance Collection"], post.inputs["Instance Collection"])
            tree.links.new(gin.outputs["Particle Radius"], post.inputs["Radius"])
            tree.links.new(gin.outputs["Normalize Instances"], post.inputs["Normalize Instances"])
        tree.links.new(post.outputs["Geometry"], gout.inputs["Geometry"])
    elif domain == "cloth_rope":
        if obj.type in {"CURVE", "CURVES"}:
            # Convert simulated polyline edges back to smooth tubular rope mesh
            m2c = tree.nodes.new("GeometryNodeMeshToCurve")
            m2c.location = (1380, 0)
            tree.links.new(sim_out.outputs["Geometry"], m2c.inputs["Mesh"])

            prof = tree.nodes.new("GeometryNodeCurvePrimitiveCircle")
            prof.inputs["Resolution"].default_value = 8
            prof.location = (1380, -180)
            tree.links.new(gin.outputs["Particle Radius"], prof.inputs["Radius"])

            rc2m = tree.nodes.new("GeometryNodeCurveToMesh")
            rc2m.inputs["Fill Caps"].default_value = True
            rc2m.location = (1580, 0)
            tree.links.new(m2c.outputs["Curve"], rc2m.inputs["Curve"])
            tree.links.new(prof.outputs["Curve"], rc2m.inputs["Profile Curve"])
            post_out_sock = rc2m.outputs["Mesh"]
        else:
            smooth = tree.nodes.new("GeometryNodeSetShadeSmooth")
            smooth.location = (1460, 0)
            tree.links.new(sim_out.outputs["Geometry"], smooth.inputs[0])
            post_out_sock = smooth.outputs[0]

        if hasattr(obj.data, "materials") and len(obj.data.materials) > 0 and obj.data.materials[0] is not None:
            set_mat = tree.nodes.new("GeometryNodeSetMaterial")
            set_mat.location = (1680, 0)
            set_mat.inputs["Material"].default_value = obj.data.materials[0]
            tree.links.new(post_out_sock, set_mat.inputs["Geometry"])
            tree.links.new(set_mat.outputs["Geometry"], gout.inputs["Geometry"])
        else:
            tree.links.new(post_out_sock, gout.inputs["Geometry"])
    else:  # fluid_emission
        post = tree.nodes.new("GeometryNodeGroup")
        post.location = (1460, 0)
        if preset_id == "smoke_plume_volume":
            post.node_tree = subgroups["PN_Post_SmokeVolume"]
            tree.links.new(sim_out.outputs["Geometry"], post.inputs["Points"])
            tree.links.new(gin.outputs["Particle Radius"], post.inputs["Particle Radius"])
            tree.links.new(gin.outputs["SDF Voxel Size"], post.inputs["Voxel Size"])
            post_out_sock = post.outputs["Geometry"]
        else:
            post.node_tree = subgroups["PN_Post_FluidSDFMesh"]
            tree.links.new(sim_out.outputs["Geometry"], post.inputs["Points"])
            tree.links.new(gin.outputs["Particle Radius"], post.inputs["Particle Radius"])
            tree.links.new(gin.outputs["SDF Voxel Size"], post.inputs["Voxel Size"])
            post_out_sock = post.outputs["Mesh"]

        # Report the actual simulated particle population on the reconstructed
        # surface. Mesh vertices are not particle counts, and this statistic
        # does not claim to expose or compare the complete solver state.
        particle_count = tree.nodes.new("GeometryNodeAttributeDomainSize")
        particle_count.name = "PNW_ParticlePopulation"
        particle_count.component = "POINTCLOUD"
        tree.links.new(sim_out.outputs["Geometry"], particle_count.inputs["Geometry"])
        population = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        population.name = "PNW_OutputParticleCount"
        population.data_type = "INT"
        population.domain = "POINT"
        population.inputs["Name"].default_value = "pnw_particle_count"
        tree.links.new(post_out_sock, population.inputs["Geometry"])
        tree.links.new(particle_count.outputs["Point Count"], population.inputs["Value"])
        post_out_sock = population.outputs["Geometry"]

        if hasattr(obj.data, "materials") and len(obj.data.materials) > 0 and obj.data.materials[0] is not None:
            set_mat = tree.nodes.new("GeometryNodeSetMaterial")
            set_mat.location = (1680, 0)
            set_mat.inputs["Material"].default_value = obj.data.materials[0]
            tree.links.new(post_out_sock, set_mat.inputs["Geometry"])
            tree.links.new(set_mat.outputs["Geometry"], gout.inputs["Geometry"])
        else:
            tree.links.new(post_out_sock, gout.inputs["Geometry"])

    # Forces, radii, ground and anchors are defined in world space. Transform
    # the entire input geometry (including instance transforms) into world
    # space before simulation; invert the full matrix only at the output.
    world_in = tree.nodes.new("GeometryNodeTransform")
    world_in.name = "PNW_InputToWorld"
    world_in.inputs["Mode"].default_value = "Matrix"
    input_consumers = [l.to_socket for l in gin.outputs["Geometry"].links]
    for socket in input_consumers:
        tree.links.new(world_in.outputs["Geometry"], socket)
    tree.links.new(gin.outputs["Geometry"], world_in.inputs["Geometry"])
    tree.links.new(self_info.outputs["Transform"], world_in.inputs["Transform"])
    # Density is defined against the evaluated world-space emitting surface.
    # A baked-in local area estimate changes the count when an object is scaled.
    if obj.type == "MESH":
        face_area = tree.nodes.new("GeometryNodeInputMeshFaceArea")
        area_sum = tree.nodes.new("GeometryNodeAttributeStatistic")
        area_sum.data_type = "FLOAT"
        area_sum.domain = "FACE"
        tree.links.new(world_in.outputs["Geometry"], area_sum.inputs["Geometry"])
        tree.links.new(face_area.outputs["Area"], area_sum.inputs["Attribute"])
        if domain == "rigid_scatter":
            tree.links.new(area_sum.outputs["Sum"], dens.inputs[1])
        elif domain == "fluid_emission":
            tree.links.new(area_sum.outputs["Sum"], emit_dens.inputs[1])
    inverse = tree.nodes.new("FunctionNodeInvertMatrix")
    tree.links.new(self_info.outputs["Transform"], inverse.inputs["Matrix"])
    world_out = tree.nodes.new("GeometryNodeTransform")
    world_out.name = "PNW_OutputToLocal"
    world_out.inputs["Mode"].default_value = "Matrix"
    output_source = gout.inputs["Geometry"].links[0].from_socket
    tree.links.new(output_source, world_out.inputs["Geometry"])
    tree.links.new(inverse.outputs["Matrix"], world_out.inputs["Transform"])
    tree.links.new(world_out.outputs["Geometry"], gout.inputs["Geometry"])
    tree.links.new(gin.outputs["Ground Z"], solver.inputs["Ground Z"])
    if "Collider Geometry" in solver.inputs:
        coll_info = tree.nodes.new("GeometryNodeCollectionInfo")
        coll_info.name = "PNW_ColliderCollectionWorld"
        coll_info.transform_space = "ORIGINAL"
        tree.links.new(gin.outputs["Collider Collection"], coll_info.inputs["Collection"])
        coll_realize = tree.nodes.new("GeometryNodeRealizeInstances")
        tree.links.new(coll_info.outputs["Instances"], coll_realize.inputs["Geometry"])
        tree.links.new(coll_realize.outputs["Geometry"], solver.inputs["Collider Geometry"])
    elif request.get("collider_collection"):
        raise ValueError("Collider collections require the updated contact solver")
    if "Anchor" in solver.inputs:
        tree.links.new(gin.outputs["Anchor"], solver.inputs["Anchor"])
    if domain == "fluid_emission":
        buoyancy = tree.nodes.new("ShaderNodeCombineXYZ")
        tree.links.new(gin.outputs["Buoyancy"], buoyancy.inputs["Z"])
        acceleration = tree.nodes.new("ShaderNodeVectorMath")
        acceleration.operation = "ADD"
        tree.links.new(gin.outputs["Gravity"], acceleration.inputs[0])
        tree.links.new(buoyancy.outputs["Vector"], acceleration.inputs[1])
        tree.links.new(acceleration.outputs["Vector"], solver.inputs["Gravity"])
    tree[FINGERPRINT_KEY] = pnw_subgroups.tree_fingerprint(tree)
    return tree


def _build_or_preserve_wrapper(
    wrapper_name: str,
    core: bpy.types.NodeTree,
    force_rebuild: bool = False,
) -> tuple[bpy.types.NodeTree, bool]:
    """Create wrapper tree or safely rebind Core inside existing wrapper while preserving user edits."""
    existing = bpy.data.node_groups.get(wrapper_name)
    if existing is None or force_rebuild:
        wrapper = bpy.data.node_groups.new(wrapper_name, "GeometryNodeTree")
        wrapper[ROLE_KEY] = "wrapper"
        _ensure_standard_interface(wrapper)

        win = wrapper.nodes.new("NodeGroupInput")
        wcore = wrapper.nodes.new("GeometryNodeGroup")
        wcore.name = "PNW_CoreNode"
        wcore.node_tree = core
        wout = wrapper.nodes.new("NodeGroupOutput")
        win.location = (-360, 0)
        wcore.location = (0, 0)
        wout.location = (360, 0)

        for name, _, _ in STANDARD_INPUT_SOCKETS:
            wrapper.links.new(win.outputs[name], wcore.inputs[name])
        wrapper.links.new(wcore.outputs["Geometry"], wout.inputs["Geometry"])
        wrapper[FINGERPRINT_KEY] = pnw_subgroups.tree_fingerprint(wrapper)
        return wrapper, False

    # The absence of a link is also an author edit. Preserve it literally.
    manual_edited = pnw_subgroups.is_tree_manually_edited(existing)
    return existing, manual_edited


def _copy_graph(tree, copies=None):
    """Detach nested node datablocks without losing any author edits."""
    copies = {} if copies is None else copies
    key = tree.as_pointer()
    if key in copies:
        return copies[key]
    clone = tree.copy()
    copies[key] = clone
    for node in clone.nodes:
        nested = getattr(node, "node_tree", None)
        if nested is not None:
            node.node_tree = _copy_graph(nested, copies)
    return clone


def _validate_target(obj, request):
    if obj.library or (obj.data and obj.data.library):
        raise ValueError("Linked targets are read-only; create a local working copy first")
    if obj.mode != "OBJECT":
        raise ValueError("Physics workflow requires Object Mode")
    if obj.type not in {"MESH", "CURVE", "CURVES"}:
        raise ValueError(f"Unsupported target type: {obj.type}; expected Mesh or Curves")
    if abs(obj.matrix_world.determinant()) < 1e-12:
        raise ValueError("Target transform is singular; simulation requires an invertible matrix")
    if request["domain"] == "fluid_emission" and obj.type != "MESH":
        raise ValueError("Particle emission currently requires a mesh surface")
    if request["bridge_mode"] != "GN_NATIVE":
        raise ValueError("UNSUPPORTED_BRIDGE: native bridge interfaces are reserved; no complete bake adapter is available")
    if obj.rigid_body or any(m.type == "FLUID" or m.name.endswith("_XPBD") for m in obj.modifiers):
        raise ValueError("Target already has a native physics bridge; use a clean working copy to avoid double simulation")
    # Validate references before creating/changing any graph.
    if obj.type != "MESH" and (request["parameters"]["bending"] or request["parameters"]["tearing"]):
        raise ValueError("Cloth bending/tearing requires mesh faces; curve rod bend/twist is not implemented")
    for socket in ("Collider", "Instance Collection", "Collider Collection"):
        _socket_value_from_request(request, socket, obj)
    if request["preset_id"] == pnw_box_tipping.PRESET_ID:
        pnw_box_tipping.preflight_box_tipping(obj, request)


def build_or_update_workflow(obj: bpy.types.Object, request: dict[str, Any]) -> dict[str, Any]:
    _validate_target(obj, request)
    pnw_softbody.validate_softbody_input(obj, request)
    op, mod_name = request["operation"], request["modifier_name"]
    force_rebuild = bool(request.get("force_rebuild", False))
    key = f"{STATE_PROP}__{mod_name}"
    try:
        previous = json.loads(obj.get(key, "{}"))
    except (TypeError, json.JSONDecodeError):
        raise ValueError("Workflow state is damaged; preserve this object and build a new working copy")
    mod = obj.modifiers.get(mod_name)
    if mod and (mod.type != "NODES" or mod.node_group is None):
        raise ValueError(f"Modifier '{mod_name}' is not a managed physics node workflow")
    if mod and mod.node_group.get(ROLE_KEY) != "wrapper":
        raise ValueError("Refusing to overwrite an unowned node graph; use a new modifier name")
    if mod and previous.get("implementation_version") != IMPLEMENTATION_VERSION and not force_rebuild:
        raise ValueError("LEGACY_WORKFLOW: keep the authored graph or explicitly rebuild into a new retained copy with force_rebuild=True")
    if op == "parameters" and mod is None:
        raise ValueError(f"Physics modifier '{mod_name}' not found on '{obj.name}'")
    schema_changed = bool(previous) and any(previous.get(k) != request[k] for k in ("domain", "preset_id", "bridge_mode"))
    if schema_changed and not force_rebuild:
        raise ValueError("Preset/domain changes require an explicit force_rebuild; the authored graph is unchanged")
    if op == "parameters" and force_rebuild:
        raise ValueError("Use build with force_rebuild for an explicit graph replacement")

    old_wrapper = mod.node_group if mod else None
    preserved = old_wrapper is not None and not force_rebuild
    if preserved:
        wrapper = old_wrapper
        # Parameters belong to the modifier, while all nested authored graphs
        # belong to this workflow. Copy on shared ownership before any update.
        if wrapper.users > 1:
            wrapper = _copy_graph(wrapper)
            mod.node_group = wrapper
        core_node = wrapper.nodes.get("PNW_CoreNode")
        core = core_node.node_tree if core_node is not None else None
        if core is None:
            raise ValueError("Managed Core reference is missing; preserving graph without parameter changes")
    else:
        # Fresh names are display labels only. Datablock references establish
        # identity, so spaces, underscores, renames and duplicate objects cannot
        # alias one another's core.
        token = uuid.uuid4().hex[:12]
        groups = pnw_subgroups.ensure_all_subgroups(force_rebuild=False)
        if request["preset_id"] == pnw_box_tipping.PRESET_ID:
            groups.update(pnw_box_tipping.ensure_box_subgroups(force_rebuild=False))
        core = _build_core_tree(f"PNW_Core_{token}", obj, request, groups)
        copies = {}
        for node in core.nodes:
            nested = getattr(node, "node_tree", None)
            if nested is not None:
                node.node_tree = _copy_graph(nested, copies)
        wrapper, _ = _build_or_preserve_wrapper(f"PNW_Wrapper_{token}", core)
        if mod is None:
            mod = obj.modifiers.new(mod_name, "NODES")
        if old_wrapper is not None:
            old_wrapper.use_fake_user = True
        mod.node_group = wrapper

    previous_hash = previous.get("parameters_hash")
    changed = force_rebuild or (previous_hash is not None and previous_hash != request["parameters_hash"])
    if changed:
        pnw_bake.invalidate_cache(obj, mod, None)
    apply_request_parameters(obj, mod, request)
    graph_fp = pnw_subgroups.tree_fingerprint(core)
    obj[key] = json.dumps({
        "schema_version": request["schema_version"],
        "domain": request["domain"], "preset_id": request["preset_id"],
        "bridge_mode": request["bridge_mode"],
        "parameters": request["parameters"], "parameters_hash": request["parameters_hash"],
        "graph_fingerprint": graph_fp, "core_name": core.name, "wrapper_name": wrapper.name,
        "implementation_version": IMPLEMENTATION_VERSION,
    }, ensure_ascii=False, allow_nan=False)
    return {
        "mode": "parameters_only" if op == "parameters" else op,
        "wrapper_name": wrapper.name, "core_name": core.name,
        "manual_edits_preserved": preserved,
        "previous_wrapper_retained": old_wrapper.name if force_rebuild and old_wrapper else None,
        "cache_invalidated_on_hash_change": changed,
        "bridge": {"bridge_mode": "GN_NATIVE", "applied": False},
        "parameters_hash": request["parameters_hash"], "graph_fingerprint": graph_fp,
    }
