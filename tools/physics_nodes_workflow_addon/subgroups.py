"""Modular, transparent, user-editable Geometry Node subgroups for Physics Nodes Workbench."""

from __future__ import annotations

import os
import time
from typing import Any

import bpy

from . import identity as solver_identity


SUBGROUP_ROLE_KEY = "pnw_subgroup_role"
SUBGROUP_FINGERPRINT_KEY = "pnw_last_fingerprint"
SUBGROUP_LOGICAL_KEY = "pnw_template_logical_name"
SUBGROUP_REVISION_KEY = "pnw_template_revision"
SUBGROUP_TEMPLATE_KEY = "pnw_template_semantic_fingerprint"
SUBGROUP_GENERATION_KEY = "pnw_template_generation"
SUBGROUP_REVISION = 7
_ACTIVE_TEMPLATES: dict[str, bpy.types.NodeTree] = {}
_BUILTIN_DYNAMICS_ASSET = "geometry_nodes_dynamics_assets.blend"
_BUILTIN_DYNAMICS_CACHE: dict[str, bpy.types.NodeTree] = {}


def _is_live_group(tree) -> bool:
    # Opening another file invalidates bpy RNA references kept by the addon.
    try:
        return tree is not None and bpy.data.node_groups.get(tree.name) == tree
    except ReferenceError:
        return False


def add_socket(tree: bpy.types.NodeTree, name: str, in_out: str, socket_type: str, default_value: Any = None):
    sock = tree.interface.new_socket(name=name, in_out=in_out, socket_type=socket_type)
    if default_value is not None and hasattr(sock, "default_value"):
        sock.default_value = default_value
    return sock


def tree_fingerprint(tree: bpy.types.NodeTree) -> str:
    """Thin wrapper over the formal layout-independent semantic identity."""
    return solver_identity.semantic_tree_fingerprint(tree)


def is_tree_manually_edited(tree: bpy.types.NodeTree | None) -> bool:
    if tree is None:
        return False
    saved = tree.get(SUBGROUP_FINGERPRINT_KEY)
    if not saved:
        return False
    return str(saved) != tree_fingerprint(tree)


def _template_generation(tree: bpy.types.NodeTree) -> int:
    try:
        return int(tree.get(SUBGROUP_GENERATION_KEY, 0))
    except Exception:
        return 0


def _is_unmodified_template(tree: bpy.types.NodeTree, logical_name: str, role: str) -> bool:
    if tree.get(SUBGROUP_LOGICAL_KEY) != logical_name:
        return False
    if tree.get(SUBGROUP_ROLE_KEY) != role:
        return False
    if tree.get(SUBGROUP_REVISION_KEY) != SUBGROUP_REVISION:
        return False
    expected = tree.get(SUBGROUP_TEMPLATE_KEY)
    if not expected:
        return False
    return tree_fingerprint(tree) == str(expected)


def _find_reusable_template(logical_name: str, role: str) -> bpy.types.NodeTree | None:
    active = _ACTIVE_TEMPLATES.get(logical_name)
    if _is_live_group(active):
        if _is_unmodified_template(active, logical_name, role):
            return active
    candidates: list[bpy.types.NodeTree] = []
    for group in bpy.data.node_groups:
        # Per-object copies created by the core are named ``<template>.001``.
        # Treat dotted names as copies, never as canonical templates.
        if "." in group.name:
            continue
        if _is_unmodified_template(group, logical_name, role):
            candidates.append(group)
    if not candidates:
        return None
    candidates.sort(key=lambda group: (_template_generation(group), group.name), reverse=True)
    return candidates[0]


def _unique_template_name(logical_name: str) -> str:
    if bpy.data.node_groups.get(logical_name) is None:
        return logical_name
    base = f"{logical_name}_rev{SUBGROUP_REVISION}"
    candidate = base
    suffix = 1
    while bpy.data.node_groups.get(candidate) is not None:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _prepare_subgroup(name: str, role: str, force_rebuild: bool = False) -> tuple[bpy.types.NodeTree, bool]:
    """Reuse only an unmodified template; otherwise create a new revision.

    Old or user-edited groups are never cleared or overwritten. ``force_rebuild``
    always creates a new uniquely named template and preserves the previous one.
    """
    if not force_rebuild:
        reusable = _find_reusable_template(name, role)
        if reusable is not None:
            _ACTIVE_TEMPLATES[name] = reusable
            return reusable, False

    for group in list(bpy.data.node_groups):
        if group.get(SUBGROUP_LOGICAL_KEY) == name and group.get(SUBGROUP_ROLE_KEY) == role:
            try:
                group.use_fake_user = True
            except Exception:
                pass

    tree = bpy.data.node_groups.new(_unique_template_name(name), "GeometryNodeTree")
    tree[SUBGROUP_ROLE_KEY] = role
    tree[SUBGROUP_LOGICAL_KEY] = name
    tree[SUBGROUP_REVISION_KEY] = SUBGROUP_REVISION
    tree[SUBGROUP_GENERATION_KEY] = int(time.time())
    _ACTIVE_TEMPLATES[name] = tree
    return tree, True


def _finalize_subgroup(tree: bpy.types.NodeTree) -> bpy.types.NodeTree:
    fingerprint = tree_fingerprint(tree)
    tree[SUBGROUP_FINGERPRINT_KEY] = fingerprint
    tree[SUBGROUP_TEMPLATE_KEY] = fingerprint
    return tree


def _vmath(tree: bpy.types.NodeTree, op: str, loc: tuple[float, float]):
    n = tree.nodes.new("ShaderNodeVectorMath")
    n.operation = op
    n.location = loc
    return n


def _math(tree: bpy.types.NodeTree, op: str, loc: tuple[float, float]):
    n = tree.nodes.new("ShaderNodeMath")
    n.operation = op
    n.location = loc
    return n


def _load_builtin_dynamics(names: list[str]) -> dict[str, bpy.types.NodeTree]:
    """Load Blender's bundled 5.2 dynamics node assets without relying on names.

    The returned mapping is keyed by the asset's original node-group name. The
    actual datablock may be renamed by Blender when a name collision exists, so
    callers must use the returned pointers instead of looking groups up by name.
    """
    result: dict[str, bpy.types.NodeTree] = {}
    for name in names:
        cached = _BUILTIN_DYNAMICS_CACHE.get(name)
        if _is_live_group(cached):
            result[name] = cached

    missing = [name for name in names if name not in result]
    if not missing:
        return result

    asset_path = os.path.join(
        bpy.utils.resource_path("LOCAL"),
        "datafiles",
        "assets",
        "nodes",
        _BUILTIN_DYNAMICS_ASSET,
    )
    if not os.path.isfile(asset_path):
        return result

    available: list[str] = []
    with bpy.data.libraries.load(asset_path, link=False) as (src, dst):
        available = [name for name in missing if name in src.node_groups]
        # Blender mutates the assigned list in-place into loaded datablocks.
        # Keep our own list of requested names for the post-load mapping.
        dst.node_groups = list(available)

    for name, group in zip(available, dst.node_groups):
        if group is None:
            continue
        _BUILTIN_DYNAMICS_CACHE[name] = group
        result[name] = group
    return result


def ensure_force_gravity_drag(force_rebuild: bool = False) -> bpy.types.NodeTree:
    tree, need_build = _prepare_subgroup("PN_Force_GravityDrag", "force", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Velocity", "INPUT", "NodeSocketVector", (0.0, 0.0, 0.0))
    add_socket(tree, "Gravity", "INPUT", "NodeSocketVector", (0.0, 0.0, -9.81))
    add_socket(tree, "Drag", "INPUT", "NodeSocketFloat", 0.04)
    add_socket(tree, "Sleep Velocity", "INPUT", "NodeSocketFloat", 0.02)
    add_socket(tree, "Delta Time", "INPUT", "NodeSocketFloat", 0.01)
    add_socket(tree, "Velocity", "OUTPUT", "NodeSocketVector")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-720, 0)
    gout.location = (740, 0)

    g_dt = _vmath(tree, "SCALE", (-480, 120))
    tree.links.new(gin.outputs["Gravity"], g_dt.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], g_dt.inputs["Scale"])

    v_add = _vmath(tree, "ADD", (-280, 140))
    tree.links.new(gin.outputs["Velocity"], v_add.inputs[0])
    tree.links.new(g_dt.outputs["Vector"], v_add.inputs[1])

    drag_dt = _math(tree, "MULTIPLY", (-480, -80))
    tree.links.new(gin.outputs["Drag"], drag_dt.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], drag_dt.inputs[1])

    one_minus = _math(tree, "SUBTRACT", (-280, -80))
    one_minus.inputs[0].default_value = 1.0
    tree.links.new(drag_dt.outputs["Value"], one_minus.inputs[1])

    clamp_d = _math(tree, "MAXIMUM", (-100, -80))
    clamp_d.inputs[1].default_value = 0.0
    tree.links.new(one_minus.outputs["Value"], clamp_d.inputs[0])

    v_damp = _vmath(tree, "SCALE", (100, 120))
    tree.links.new(v_add.outputs["Vector"], v_damp.inputs[0])
    tree.links.new(clamp_d.outputs["Value"], v_damp.inputs["Scale"])

    # Note: Sleep velocity suppression belongs strictly in contact/collision steps,
    # never in gravity integration, otherwise high substeps trap stationary objects at t=0.
    tree.links.new(v_damp.outputs["Vector"], gout.inputs["Velocity"])
    return _finalize_subgroup(tree)


def ensure_force_wind_turbulence(force_rebuild: bool = False) -> bpy.types.NodeTree:
    tree, need_build = _prepare_subgroup("PN_Force_WindTurbulence", "force", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Position", "INPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "INPUT", "NodeSocketVector")
    add_socket(tree, "Wind Vector", "INPUT", "NodeSocketVector", (0.0, 0.0, 0.0))
    add_socket(tree, "Turbulence", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Time", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Delta Time", "INPUT", "NodeSocketFloat", 0.01)
    add_socket(tree, "Velocity", "OUTPUT", "NodeSocketVector")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-760, 0)
    gout.location = (640, 0)

    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.noise_dimensions = "4D"
    noise.inputs["Scale"].default_value = 1.6
    noise.inputs["Detail"].default_value = 2.0
    noise.location = (-520, -140)
    tree.links.new(gin.outputs["Position"], noise.inputs["Vector"])
    tree.links.new(gin.outputs["Time"], noise.inputs["W"])

    # Center noise color from [0, 1] -> [-0.5, 0.5]
    sub_half = _vmath(tree, "SUBTRACT", (-300, -140))
    sub_half.inputs[1].default_value = (0.5, 0.5, 0.5)
    tree.links.new(noise.outputs["Color"], sub_half.inputs[0])

    turb_scale = _vmath(tree, "SCALE", (-100, -140))
    tree.links.new(sub_half.outputs["Vector"], turb_scale.inputs[0])
    tree.links.new(gin.outputs["Turbulence"], turb_scale.inputs["Scale"])

    wind_total = _vmath(tree, "ADD", (100, -40))
    tree.links.new(gin.outputs["Wind Vector"], wind_total.inputs[0])
    tree.links.new(turb_scale.outputs["Vector"], wind_total.inputs[1])

    dv = _vmath(tree, "SCALE", (280, -40))
    tree.links.new(wind_total.outputs["Vector"], dv.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], dv.inputs["Scale"])

    v_out = _vmath(tree, "ADD", (460, 40))
    tree.links.new(gin.outputs["Velocity"], v_out.inputs[0])
    tree.links.new(dv.outputs["Vector"], v_out.inputs[1])
    tree.links.new(v_out.outputs["Vector"], gout.inputs["Velocity"])
    return _finalize_subgroup(tree)


def ensure_collide_ground_plane(force_rebuild: bool = False) -> bpy.types.NodeTree:
    tree, need_build = _prepare_subgroup("PN_Collide_GroundPlane", "collider", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Position", "INPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "INPUT", "NodeSocketVector")
    add_socket(tree, "Ground Z", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Radius", "INPUT", "NodeSocketFloat", 0.05)
    add_socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.35)
    add_socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.45)
    add_socket(tree, "Sleep Velocity", "INPUT", "NodeSocketFloat", 0.05)
    add_socket(tree, "Position", "OUTPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "OUTPUT", "NodeSocketVector")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-860, 0)
    gout.location = (860, 0)

    floor_z = _math(tree, "ADD", (-620, 40))
    tree.links.new(gin.outputs["Ground Z"], floor_z.inputs[0])
    tree.links.new(gin.outputs["Radius"], floor_z.inputs[1])

    sep_p = tree.nodes.new("ShaderNodeSeparateXYZ")
    sep_p.location = (-620, 200)
    tree.links.new(gin.outputs["Position"], sep_p.inputs["Vector"])

    hit = _math(tree, "LESS_THAN", (-380, 120))
    tree.links.new(sep_p.outputs["Z"], hit.inputs[0])
    tree.links.new(floor_z.outputs["Value"], hit.inputs[1])

    comb_p = tree.nodes.new("ShaderNodeCombineXYZ")
    comb_p.location = (-360, 300)
    tree.links.new(sep_p.outputs["X"], comb_p.inputs["X"])
    tree.links.new(sep_p.outputs["Y"], comb_p.inputs["Y"])
    tree.links.new(floor_z.outputs["Value"], comb_p.inputs["Z"])

    sw_p = tree.nodes.new("GeometryNodeSwitch")
    sw_p.input_type = "VECTOR"
    sw_p.location = (520, 220)
    tree.links.new(hit.outputs["Value"], sw_p.inputs["Switch"])
    tree.links.new(gin.outputs["Position"], sw_p.inputs["False"])
    tree.links.new(comb_p.outputs["Vector"], sw_p.inputs["True"])

    sep_v = tree.nodes.new("ShaderNodeSeparateXYZ")
    sep_v.location = (-620, -200)
    tree.links.new(gin.outputs["Velocity"], sep_v.inputs["Vector"])

    # Tangential friction damp factor = max(0, 1 - Friction)
    fric_damp = _math(tree, "SUBTRACT", (-400, -120))
    fric_damp.inputs[0].default_value = 1.0
    tree.links.new(gin.outputs["Friction"], fric_damp.inputs[1])

    vx = _math(tree, "MULTIPLY", (-180, -100))
    tree.links.new(sep_v.outputs["X"], vx.inputs[0])
    tree.links.new(fric_damp.outputs["Value"], vx.inputs[1])

    vy = _math(tree, "MULTIPLY", (-180, -220))
    tree.links.new(sep_v.outputs["Y"], vy.inputs[0])
    tree.links.new(fric_damp.outputs["Value"], vy.inputs[1])

    # Only bounce if downward velocity magnitude > Sleep Velocity; otherwise clamp vz=0 (resting stability!)
    vz_abs = _math(tree, "ABSOLUTE", (-400, -340))
    tree.links.new(sep_v.outputs["Z"], vz_abs.inputs[0])

    gt_sleep = _math(tree, "GREATER_THAN", (-220, -440))
    tree.links.new(vz_abs.outputs["Value"], gt_sleep.inputs[0])
    tree.links.new(gin.outputs["Sleep Velocity"], gt_sleep.inputs[1])

    vz_bounce = _math(tree, "MULTIPLY", (-180, -340))
    tree.links.new(vz_abs.outputs["Value"], vz_bounce.inputs[0])
    tree.links.new(gin.outputs["Restitution"], vz_bounce.inputs[1])

    vz_final = _math(tree, "MULTIPLY", (20, -360))
    tree.links.new(vz_bounce.outputs["Value"], vz_final.inputs[0])
    tree.links.new(gt_sleep.outputs["Value"], vz_final.inputs[1])

    # Only reflect if vz < 0 (moving into floor), else keep existing vz
    vz_neg = _math(tree, "LESS_THAN", (20, -480))
    vz_neg.inputs[1].default_value = 0.0
    tree.links.new(sep_v.outputs["Z"], vz_neg.inputs[0])

    sw_vz = tree.nodes.new("GeometryNodeSwitch")
    sw_vz.input_type = "FLOAT"
    sw_vz.location = (180, -400)
    tree.links.new(vz_neg.outputs["Value"], sw_vz.inputs["Switch"])
    tree.links.new(sep_v.outputs["Z"], sw_vz.inputs["False"])
    tree.links.new(vz_final.outputs["Value"], sw_vz.inputs["True"])

    comb_v = tree.nodes.new("ShaderNodeCombineXYZ")
    comb_v.location = (340, -220)
    tree.links.new(vx.outputs["Value"], comb_v.inputs["X"])
    tree.links.new(vy.outputs["Value"], comb_v.inputs["Y"])
    tree.links.new(sw_vz.outputs["Output"], comb_v.inputs["Z"])

    sw_v = tree.nodes.new("GeometryNodeSwitch")
    sw_v.input_type = "VECTOR"
    sw_v.location = (560, -140)
    tree.links.new(hit.outputs["Value"], sw_v.inputs["Switch"])
    tree.links.new(gin.outputs["Velocity"], sw_v.inputs["False"])
    tree.links.new(comb_v.outputs["Vector"], sw_v.inputs["True"])

    tree.links.new(sw_p.outputs["Output"], gout.inputs["Position"])
    tree.links.new(sw_v.outputs["Output"], gout.inputs["Velocity"])
    return _finalize_subgroup(tree)


def ensure_collide_mesh_surface(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Collider response against an arbitrary Mesh Object using Proximity + SampleNearestSurface normal."""
    tree, need_build = _prepare_subgroup("PN_Collide_MeshSurface", "collider", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Position", "INPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "INPUT", "NodeSocketVector")
    add_socket(tree, "Collider", "INPUT", "NodeSocketObject")
    add_socket(tree, "Contact Thickness", "INPUT", "NodeSocketFloat", 0.08)
    add_socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.35)
    add_socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.45)
    add_socket(tree, "Collider Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Position", "OUTPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "OUTPUT", "NodeSocketVector")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-1040, 0)
    gout.location = (1040, 0)

    obj_info = tree.nodes.new("GeometryNodeObjectInfo")
    obj_info.transform_space = "ORIGINAL"
    obj_info.location = (-840, 260)
    tree.links.new(gin.outputs["Collider"], obj_info.inputs["Object"])

    realize = tree.nodes.new("GeometryNodeRealizeInstances")
    realize.location = (-660, 260)
    tree.links.new(obj_info.outputs["Geometry"], realize.inputs["Geometry"])

    # Object fallback and supplied collection geometry share one world-space
    # collider slot. The collection path arrives already realized from core.
    obj_world = tree.nodes.new("GeometryNodeTransform")
    obj_world.inputs["Mode"].default_value = "Matrix"
    obj_world.location = (-460, 480)
    tree.links.new(realize.outputs["Geometry"], obj_world.inputs["Geometry"])
    tree.links.new(obj_info.outputs["Transform"], obj_world.inputs["Transform"])

    supplied_count = tree.nodes.new("GeometryNodeAttributeDomainSize")
    supplied_count.component = "MESH"
    supplied_count.location = (-660, 620)
    tree.links.new(gin.outputs["Collider Geometry"], supplied_count.inputs["Geometry"])
    has_supplied = _math(tree, "GREATER_THAN", (-460, 620))
    has_supplied.inputs[1].default_value = 0.0
    tree.links.new(supplied_count.outputs["Face Count"], has_supplied.inputs[0])
    collider_switch = tree.nodes.new("GeometryNodeSwitch")
    collider_switch.input_type = "GEOMETRY"
    collider_switch.location = (-460, 520)
    tree.links.new(has_supplied.outputs["Value"], collider_switch.inputs["Switch"])
    tree.links.new(obj_world.outputs["Geometry"], collider_switch.inputs["False"])
    tree.links.new(gin.outputs["Collider Geometry"], collider_switch.inputs["True"])

    prox = tree.nodes.new("GeometryNodeProximity")
    prox.target_element = "FACES"
    prox.location = (-460, 320)
    tree.links.new(collider_switch.outputs["Output"], prox.inputs["Geometry"])
    tree.links.new(gin.outputs["Position"], prox.inputs["Sample Position"])

    normal_in = tree.nodes.new("GeometryNodeInputNormal")
    normal_in.location = (-660, 80)

    samp_norm = tree.nodes.new("GeometryNodeSampleNearestSurface")
    samp_norm.data_type = "FLOAT_VECTOR"
    samp_norm.location = (-460, 80)
    tree.links.new(collider_switch.outputs["Output"], samp_norm.inputs["Mesh"])
    tree.links.new(normal_in.outputs["Normal"], samp_norm.inputs["Value"])
    tree.links.new(gin.outputs["Position"], samp_norm.inputs["Sample Position"])

    n_unit = _vmath(tree, "NORMALIZE", (-260, 80))
    tree.links.new(samp_norm.outputs["Value"], n_unit.inputs[0])

    # Delta vector from surface closest point to particle: d = P - P_surf
    d_vec = _vmath(tree, "SUBTRACT", (-260, 260))
    tree.links.new(gin.outputs["Position"], d_vec.inputs[0])
    tree.links.new(prox.outputs["Position"], d_vec.inputs[1])

    signed_dist = _vmath(tree, "DOT_PRODUCT", (-80, 260))
    tree.links.new(d_vec.outputs["Vector"], signed_dist.inputs[0])
    tree.links.new(n_unit.outputs["Vector"], signed_dist.inputs[1])

    # Hit condition:
    # Must be geometrically close to surface (prox.Distance < Contact Thickness * 2.5)
    # AND inside/within contact shell (signed_dist < Contact Thickness), avoiding infinite plane extension outside mesh bounds.
    max_reach = _math(tree, "MULTIPLY", (-80, 380))
    max_reach.inputs[1].default_value = 2.5
    tree.links.new(gin.outputs["Contact Thickness"], max_reach.inputs[0])

    near_surf = _math(tree, "LESS_THAN", (100, 380))
    tree.links.new(prox.outputs["Distance"], near_surf.inputs[0])
    tree.links.new(max_reach.outputs["Value"], near_surf.inputs[1])

    lt_thick = _math(tree, "LESS_THAN", (100, 240))
    tree.links.new(signed_dist.outputs["Value"], lt_thick.inputs[0])
    tree.links.new(gin.outputs["Contact Thickness"], lt_thick.inputs[1])

    cond_depth = _math(tree, "MULTIPLY", (260, 280))
    tree.links.new(lt_thick.outputs["Value"], cond_depth.inputs[0])
    tree.links.new(near_surf.outputs["Value"], cond_depth.inputs[1])

    cond_valid = _math(tree, "MULTIPLY", (420, 280))
    tree.links.new(cond_depth.outputs["Value"], cond_valid.inputs[0])
    tree.links.new(prox.outputs["Is Valid"], cond_valid.inputs[1])

    # Push position out along normal: push_dist = Contact Thickness - signed_dist
    push_dist = _math(tree, "SUBTRACT", (100, 60))
    tree.links.new(gin.outputs["Contact Thickness"], push_dist.inputs[0])
    tree.links.new(signed_dist.outputs["Value"], push_dist.inputs[1])

    push_vec = _vmath(tree, "SCALE", (280, 60))
    tree.links.new(n_unit.outputs["Vector"], push_vec.inputs[0])
    tree.links.new(push_dist.outputs["Value"], push_vec.inputs["Scale"])

    p_pushed = _vmath(tree, "ADD", (460, 60))
    tree.links.new(gin.outputs["Position"], p_pushed.inputs[0])
    tree.links.new(push_vec.outputs["Vector"], p_pushed.inputs[1])

    sw_p = tree.nodes.new("GeometryNodeSwitch")
    sw_p.input_type = "VECTOR"
    sw_p.location = (760, 180)
    tree.links.new(cond_valid.outputs["Value"], sw_p.inputs["Switch"])
    tree.links.new(gin.outputs["Position"], sw_p.inputs["False"])
    tree.links.new(p_pushed.outputs["Vector"], sw_p.inputs["True"])

    # Velocity reflection against surface normal when v_n = dot(V, N) < 0
    v_n = _vmath(tree, "DOT_PRODUCT", (-80, -140))
    tree.links.new(gin.outputs["Velocity"], v_n.inputs[0])
    tree.links.new(n_unit.outputs["Vector"], v_n.inputs[1])

    v_n_vec = _vmath(tree, "SCALE", (100, -140))
    tree.links.new(n_unit.outputs["Vector"], v_n_vec.inputs[0])
    tree.links.new(v_n.outputs["Value"], v_n_vec.inputs["Scale"])

    v_t = _vmath(tree, "SUBTRACT", (280, -100))
    tree.links.new(gin.outputs["Velocity"], v_t.inputs[0])
    tree.links.new(v_n_vec.outputs["Vector"], v_t.inputs[1])

    fric_factor = _math(tree, "SUBTRACT", (280, -240))
    fric_factor.inputs[0].default_value = 1.0
    tree.links.new(gin.outputs["Friction"], fric_factor.inputs[1])

    v_t_damp = _vmath(tree, "SCALE", (460, -100))
    tree.links.new(v_t.outputs["Vector"], v_t_damp.inputs[0])
    tree.links.new(fric_factor.outputs["Value"], v_t_damp.inputs["Scale"])

    neg_rest = _math(tree, "MULTIPLY", (280, -360))
    neg_rest.inputs[0].default_value = -1.0
    tree.links.new(gin.outputs["Restitution"], neg_rest.inputs[1])

    v_n_bounce = _vmath(tree, "SCALE", (460, -300))
    tree.links.new(v_n_vec.outputs["Vector"], v_n_bounce.inputs[0])
    tree.links.new(neg_rest.outputs["Value"], v_n_bounce.inputs["Scale"])

    v_col = _vmath(tree, "ADD", (620, -200))
    tree.links.new(v_t_damp.outputs["Vector"], v_col.inputs[0])
    tree.links.new(v_n_bounce.outputs["Vector"], v_col.inputs[1])

    # Only alter velocity if colliding AND moving toward surface (v_n < 0)
    moving_in = _math(tree, "LESS_THAN", (460, -460))
    moving_in.inputs[1].default_value = 0.0
    tree.links.new(v_n.outputs["Value"], moving_in.inputs[0])

    vel_cond = _math(tree, "MULTIPLY", (640, -420))
    tree.links.new(cond_valid.outputs["Value"], vel_cond.inputs[0])
    tree.links.new(moving_in.outputs["Value"], vel_cond.inputs[1])

    sw_v = tree.nodes.new("GeometryNodeSwitch")
    sw_v.input_type = "VECTOR"
    sw_v.location = (800, -180)
    tree.links.new(vel_cond.outputs["Value"], sw_v.inputs["Switch"])
    tree.links.new(gin.outputs["Velocity"], sw_v.inputs["False"])
    tree.links.new(v_col.outputs["Vector"], sw_v.inputs["True"])

    tree.links.new(sw_p.outputs["Output"], gout.inputs["Position"])
    tree.links.new(sw_v.outputs["Output"], gout.inputs["Velocity"])
    return _finalize_subgroup(tree)


def _capture_contact_response(tree, geometry, collide):
    # Evaluate both responses against incoming positions. Evaluating velocity
    # after Set Position would retest contact on the corrected surface and
    # discard the bounce/friction impulse.
    capture = tree.nodes.new("GeometryNodeCaptureAttribute")
    capture.domain = "POINT"
    capture.capture_items.new("VECTOR", "Position")
    capture.capture_items.new("VECTOR", "Velocity")
    tree.links.new(geometry, capture.inputs["Geometry"])
    tree.links.new(collide.outputs["Position"], capture.inputs["Position"])
    tree.links.new(collide.outputs["Velocity"], capture.inputs["Velocity"])
    return capture


def ensure_collide_mesh_surface_geo(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Geometry-domain wrapper around the legacy mesh surface collider.

    Child-group field inputs can lose their intended geometry context when mixed
    with Repeat Zones. This wrapper makes the geometry explicit and stores the
    corrected position/velocity back onto the same geometry.
    """
    sg_mesh = ensure_collide_mesh_surface(force_rebuild)
    tree, need_build = _prepare_subgroup("PN_Collide_MeshSurface_Geo", "collider", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Collider", "INPUT", "NodeSocketObject")
    add_socket(tree, "Contact Thickness", "INPUT", "NodeSocketFloat", 0.08)
    add_socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.35)
    add_socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.45)
    add_socket(tree, "Collider Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-720, 0)
    gout.location = (720, 0)
    pos = tree.nodes.new("GeometryNodeInputPosition")
    pos.location = (-520, -160)
    vel = tree.nodes.new("GeometryNodeInputNamedAttribute")
    vel.data_type = "FLOAT_VECTOR"
    vel.inputs["Name"].default_value = "pnw_velocity"
    vel.location = (-520, -320)
    collide = tree.nodes.new("GeometryNodeGroup")
    collide.node_tree = sg_mesh
    collide.location = (-300, 0)
    tree.links.new(pos.outputs["Position"], collide.inputs["Position"])
    tree.links.new(vel.outputs["Attribute"], collide.inputs["Velocity"])
    tree.links.new(gin.outputs["Collider"], collide.inputs["Collider"])
    tree.links.new(gin.outputs["Contact Thickness"], collide.inputs["Contact Thickness"])
    tree.links.new(gin.outputs["Restitution"], collide.inputs["Restitution"])
    tree.links.new(gin.outputs["Friction"], collide.inputs["Friction"])
    tree.links.new(gin.outputs["Collider Geometry"], collide.inputs["Collider Geometry"])
    response = _capture_contact_response(tree, gin.outputs["Geometry"], collide)
    set_pos = tree.nodes.new("GeometryNodeSetPosition")
    set_pos.location = (-40, 120)
    tree.links.new(response.outputs["Geometry"], set_pos.inputs["Geometry"])
    tree.links.new(response.outputs["Position"], set_pos.inputs["Position"])
    store_v = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store_v.data_type = "FLOAT_VECTOR"
    store_v.domain = "POINT"
    store_v.inputs["Name"].default_value = "pnw_velocity"
    store_v.location = (220, 120)
    tree.links.new(set_pos.outputs["Geometry"], store_v.inputs["Geometry"])
    tree.links.new(response.outputs["Velocity"], store_v.inputs["Value"])
    tree.links.new(store_v.outputs["Geometry"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)


def ensure_collide_ground_plane_geo(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Geometry-domain wrapper around the legacy ground plane collider."""
    sg_ground = ensure_collide_ground_plane(force_rebuild)
    tree, need_build = _prepare_subgroup("PN_Collide_GroundPlane_Geo", "collider", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Ground Z", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Radius", "INPUT", "NodeSocketFloat", 0.05)
    add_socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.35)
    add_socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.45)
    add_socket(tree, "Sleep Velocity", "INPUT", "NodeSocketFloat", 0.05)
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-720, 0)
    gout.location = (720, 0)
    pos = tree.nodes.new("GeometryNodeInputPosition")
    pos.location = (-520, -160)
    vel = tree.nodes.new("GeometryNodeInputNamedAttribute")
    vel.data_type = "FLOAT_VECTOR"
    vel.inputs["Name"].default_value = "pnw_velocity"
    vel.location = (-520, -320)
    collide = tree.nodes.new("GeometryNodeGroup")
    collide.node_tree = sg_ground
    collide.location = (-300, 0)
    tree.links.new(pos.outputs["Position"], collide.inputs["Position"])
    tree.links.new(vel.outputs["Attribute"], collide.inputs["Velocity"])
    tree.links.new(gin.outputs["Ground Z"], collide.inputs["Ground Z"])
    tree.links.new(gin.outputs["Radius"], collide.inputs["Radius"])
    tree.links.new(gin.outputs["Restitution"], collide.inputs["Restitution"])
    tree.links.new(gin.outputs["Friction"], collide.inputs["Friction"])
    tree.links.new(gin.outputs["Sleep Velocity"], collide.inputs["Sleep Velocity"])
    response = _capture_contact_response(tree, gin.outputs["Geometry"], collide)
    set_pos = tree.nodes.new("GeometryNodeSetPosition")
    set_pos.location = (-40, 120)
    tree.links.new(response.outputs["Geometry"], set_pos.inputs["Geometry"])
    tree.links.new(response.outputs["Position"], set_pos.inputs["Position"])
    store_v = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store_v.data_type = "FLOAT_VECTOR"
    store_v.domain = "POINT"
    store_v.inputs["Name"].default_value = "pnw_velocity"
    store_v.location = (220, 120)
    tree.links.new(set_pos.outputs["Geometry"], store_v.inputs["Geometry"])
    tree.links.new(response.outputs["Velocity"], store_v.inputs["Value"])
    tree.links.new(store_v.outputs["Geometry"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)


def ensure_collide_mesh_geometry(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """World-space mesh collider with sphere-proxy radius and velocity response."""
    tree, need_build = _prepare_subgroup("PN_Collide_MeshGeometry", "collider", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Collider Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Velocity", "INPUT", "NodeSocketVector", (0.0, 0.0, 0.0))
    add_socket(tree, "Radius", "INPUT", "NodeSocketFloat", 0.12)
    add_socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.35)
    add_socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.45)
    add_socket(tree, "Sleep Velocity", "INPUT", "NodeSocketFloat", 0.08)
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-1100, 0)
    gout.location = (1100, 0)

    pos_field = tree.nodes.new("GeometryNodeInputPosition")
    pos_field.location = (-900, -160)
    prox = tree.nodes.new("GeometryNodeProximity")
    prox.target_element = "FACES"
    prox.location = (-700, 200)
    tree.links.new(gin.outputs["Collider Geometry"], prox.inputs["Geometry"])
    tree.links.new(pos_field.outputs["Position"], prox.inputs["Sample Position"])

    normal_in = tree.nodes.new("GeometryNodeInputNormal")
    normal_in.location = (-900, 40)
    samp_norm = tree.nodes.new("GeometryNodeSampleNearestSurface")
    samp_norm.data_type = "FLOAT_VECTOR"
    samp_norm.location = (-700, 20)
    tree.links.new(gin.outputs["Collider Geometry"], samp_norm.inputs["Mesh"])
    tree.links.new(normal_in.outputs["Normal"], samp_norm.inputs["Value"])
    tree.links.new(pos_field.outputs["Position"], samp_norm.inputs["Sample Position"])
    n_unit = _vmath(tree, "NORMALIZE", (-500, 20))
    tree.links.new(samp_norm.outputs["Value"], n_unit.inputs[0])

    # Finite mesh boundary query. A point beyond an open face edge/vertex must
    # use the true Euclidean distance to the nearest feature, not the face
    # plane's signed distance; otherwise the face is extrapolated infinitely.
    prox_edge = tree.nodes.new("GeometryNodeProximity")
    prox_edge.target_element = "EDGES"
    prox_edge.location = (-700, 560)
    tree.links.new(gin.outputs["Collider Geometry"], prox_edge.inputs["Geometry"])
    tree.links.new(pos_field.outputs["Position"], prox_edge.inputs["Sample Position"])
    prox_point = tree.nodes.new("GeometryNodeProximity")
    prox_point.target_element = "POINTS"
    prox_point.location = (-700, 720)
    tree.links.new(gin.outputs["Collider Geometry"], prox_point.inputs["Geometry"])
    tree.links.new(pos_field.outputs["Position"], prox_point.inputs["Sample Position"])
    boundary_min = _math(tree, "MINIMUM", (-500, 640))
    tree.links.new(prox_edge.outputs["Distance"], boundary_min.inputs[0])
    tree.links.new(prox_point.outputs["Distance"], boundary_min.inputs[1])
    use_edge = _math(tree, "LESS_THAN", (-500, 760))
    tree.links.new(prox_edge.outputs["Distance"], use_edge.inputs[0])
    tree.links.new(prox_point.outputs["Distance"], use_edge.inputs[1])
    boundary_pos = tree.nodes.new("GeometryNodeSwitch")
    boundary_pos.input_type = "VECTOR"
    boundary_pos.location = (-300, 700)
    tree.links.new(use_edge.outputs["Value"], boundary_pos.inputs["Switch"])
    tree.links.new(prox_point.outputs["Position"], boundary_pos.inputs["False"])
    tree.links.new(prox_edge.outputs["Position"], boundary_pos.inputs["True"])
    boundary_vec = _vmath(tree, "SUBTRACT", (-100, 700))
    tree.links.new(pos_field.outputs["Position"], boundary_vec.inputs[0])
    tree.links.new(boundary_pos.outputs["Output"], boundary_vec.inputs[1])
    boundary_dist = _vmath(tree, "LENGTH", (80, 700))
    tree.links.new(boundary_vec.outputs["Vector"], boundary_dist.inputs[0])
    boundary_norm_raw = _vmath(tree, "NORMALIZE", (260, 700))
    tree.links.new(boundary_vec.outputs["Vector"], boundary_norm_raw.inputs[0])
    boundary_tiny = _math(tree, "GREATER_THAN", (260, 580))
    boundary_tiny.inputs[1].default_value = 0.000001
    tree.links.new(boundary_dist.outputs["Value"], boundary_tiny.inputs[0])
    boundary_norm = tree.nodes.new("GeometryNodeSwitch")
    boundary_norm.input_type = "VECTOR"
    boundary_norm.location = (440, 700)
    tree.links.new(boundary_tiny.outputs["Value"], boundary_norm.inputs["Switch"])
    tree.links.new(n_unit.outputs["Vector"], boundary_norm.inputs["False"])
    tree.links.new(boundary_norm_raw.outputs["Vector"], boundary_norm.inputs["True"])
    boundary_feature_valid = _math(tree, "MAXIMUM", (80, 580))
    tree.links.new(prox_edge.outputs["Is Valid"], boundary_feature_valid.inputs[0])
    tree.links.new(prox_point.outputs["Is Valid"], boundary_feature_valid.inputs[1])
    boundary_face_delta = _math(tree, "ADD", (80, 460))
    tree.links.new(prox.outputs["Distance"], boundary_face_delta.inputs[0])
    boundary_face_delta.inputs[1].default_value = 0.00001
    boundary_near = _math(tree, "LESS_THAN", (260, 460))
    tree.links.new(boundary_min.outputs["Value"], boundary_near.inputs[0])
    tree.links.new(boundary_face_delta.outputs["Value"], boundary_near.inputs[1])
    boundary_is = _math(tree, "MULTIPLY", (440, 520))
    tree.links.new(boundary_near.outputs["Value"], boundary_is.inputs[0])
    tree.links.new(boundary_feature_valid.outputs["Value"], boundary_is.inputs[1])
    boundary_radius = _math(tree, "ADD", (80, 820))
    tree.links.new(gin.outputs["Radius"], boundary_radius.inputs[0])
    boundary_radius.inputs[1].default_value = 0.0001
    boundary_within = _math(tree, "LESS_THAN", (260, 820))
    tree.links.new(boundary_dist.outputs["Value"], boundary_within.inputs[0])
    tree.links.new(boundary_radius.outputs["Value"], boundary_within.inputs[1])
    boundary_valid = _math(tree, "MULTIPLY", (440, 820))
    tree.links.new(boundary_is.outputs["Value"], boundary_valid.inputs[0])
    tree.links.new(boundary_within.outputs["Value"], boundary_valid.inputs[1])

    d_vec = _vmath(tree, "SUBTRACT", (-500, 200))
    tree.links.new(pos_field.outputs["Position"], d_vec.inputs[0])
    tree.links.new(prox.outputs["Position"], d_vec.inputs[1])
    signed_dist = _vmath(tree, "DOT_PRODUCT", (-300, 200))
    tree.links.new(d_vec.outputs["Vector"], signed_dist.inputs[0])
    tree.links.new(n_unit.outputs["Vector"], signed_dist.inputs[1])

    reach = _math(tree, "MULTIPLY", (-300, 360))
    reach.inputs[1].default_value = 2.5
    tree.links.new(gin.outputs["Radius"], reach.inputs[0])
    reach_eps = _math(tree, "ADD", (-120, 360))
    reach_eps.inputs[1].default_value = 0.0001
    tree.links.new(reach.outputs["Value"], reach_eps.inputs[0])
    near_surf = _math(tree, "LESS_THAN", (-100, 360))
    tree.links.new(prox.outputs["Distance"], near_surf.inputs[0])
    tree.links.new(reach_eps.outputs["Value"], near_surf.inputs[1])
    inside_eps = _math(tree, "ADD", (-300, 100))
    inside_eps.inputs[1].default_value = 0.0001
    tree.links.new(gin.outputs["Radius"], inside_eps.inputs[0])
    inside = _math(tree, "LESS_THAN", (-100, 220))
    tree.links.new(signed_dist.outputs["Value"], inside.inputs[0])
    tree.links.new(inside_eps.outputs["Value"], inside.inputs[1])
    valid = _math(tree, "MULTIPLY", (100, 300))
    tree.links.new(near_surf.outputs["Value"], valid.inputs[0])
    tree.links.new(inside.outputs["Value"], valid.inputs[1])
    valid_surface = _math(tree, "MULTIPLY", (280, 300))
    tree.links.new(valid.outputs["Value"], valid_surface.inputs[0])
    tree.links.new(prox.outputs["Is Valid"], valid_surface.inputs[1])
    contact_normal = tree.nodes.new("GeometryNodeSwitch")
    contact_normal.input_type = "VECTOR"
    contact_normal.location = (620, 680)
    tree.links.new(boundary_is.outputs["Value"], contact_normal.inputs["Switch"])
    tree.links.new(n_unit.outputs["Vector"], contact_normal.inputs["False"])
    tree.links.new(boundary_norm.outputs["Output"], contact_normal.inputs["True"])
    face_push = _math(tree, "SUBTRACT", (-100, 80))
    tree.links.new(gin.outputs["Radius"], face_push.inputs[0])
    tree.links.new(signed_dist.outputs["Value"], face_push.inputs[1])
    boundary_push = _math(tree, "SUBTRACT", (80, 880))
    tree.links.new(gin.outputs["Radius"], boundary_push.inputs[0])
    tree.links.new(boundary_dist.outputs["Value"], boundary_push.inputs[1])
    contact_push = tree.nodes.new("GeometryNodeSwitch")
    contact_push.input_type = "FLOAT"
    contact_push.location = (440, 880)
    tree.links.new(boundary_is.outputs["Value"], contact_push.inputs["Switch"])
    tree.links.new(face_push.outputs["Value"], contact_push.inputs["False"])
    tree.links.new(boundary_push.outputs["Value"], contact_push.inputs["True"])
    contact_valid = tree.nodes.new("GeometryNodeSwitch")
    contact_valid.input_type = "FLOAT"
    contact_valid.location = (800, 300)
    tree.links.new(boundary_is.outputs["Value"], contact_valid.inputs["Switch"])
    tree.links.new(valid_surface.outputs["Value"], contact_valid.inputs["False"])
    tree.links.new(boundary_valid.outputs["Value"], contact_valid.inputs["True"])

    push_vec = _vmath(tree, "SCALE", (100, 80))
    tree.links.new(contact_normal.outputs["Output"], push_vec.inputs[0])
    tree.links.new(contact_push.outputs["Output"], push_vec.inputs["Scale"])
    pushed_pos = _vmath(tree, "ADD", (300, 80))
    tree.links.new(pos_field.outputs["Position"], pushed_pos.inputs[0])
    tree.links.new(push_vec.outputs["Vector"], pushed_pos.inputs[1])
    pos_switch = tree.nodes.new("GeometryNodeSwitch")
    pos_switch.input_type = "VECTOR"
    pos_switch.location = (500, 160)
    tree.links.new(contact_valid.outputs["Output"], pos_switch.inputs["Switch"])
    tree.links.new(pos_field.outputs["Position"], pos_switch.inputs["False"])
    tree.links.new(pushed_pos.outputs["Vector"], pos_switch.inputs["True"])
    set_pos = tree.nodes.new("GeometryNodeSetPosition")
    set_pos.location = (700, 280)
    tree.links.new(gin.outputs["Geometry"], set_pos.inputs["Geometry"])
    tree.links.new(pos_switch.outputs["Output"], set_pos.inputs["Position"])

    v_n = _vmath(tree, "DOT_PRODUCT", (-500, -300))
    tree.links.new(gin.outputs["Velocity"], v_n.inputs[0])
    tree.links.new(contact_normal.outputs["Output"], v_n.inputs[1])
    v_t = _vmath(tree, "SUBTRACT", (-300, -260))
    tree.links.new(gin.outputs["Velocity"], v_t.inputs[0])
    v_n_vec = _vmath(tree, "SCALE", (-300, -420))
    tree.links.new(contact_normal.outputs["Output"], v_n_vec.inputs[0])
    tree.links.new(v_n.outputs["Value"], v_n_vec.inputs["Scale"])
    tree.links.new(v_n_vec.outputs["Vector"], v_t.inputs[1])
    fric_factor = _math(tree, "SUBTRACT", (-100, -420))
    fric_factor.inputs[0].default_value = 1.0
    tree.links.new(gin.outputs["Friction"], fric_factor.inputs[1])
    v_t_damped = _vmath(tree, "SCALE", (-100, -260))
    tree.links.new(v_t.outputs["Vector"], v_t_damped.inputs[0])
    tree.links.new(fric_factor.outputs["Value"], v_t_damped.inputs["Scale"])
    # Low-energy restitution target: e*|v_n| is the outgoing normal speed.
    # If it is below Sleep Velocity, use e_eff=0 (fully inelastic normal
    # response) while preserving tangential velocity.  Do not zero the whole
    # velocity vector on a low-speed contact here; persistent sleep is handled
    # later by the per-point support/speed state machine.
    vn_abs = _math(tree, "ABSOLUTE", (-500, -620))
    tree.links.new(v_n.outputs["Value"], vn_abs.inputs[0])
    rebound = _math(tree, "MULTIPLY", (-300, -620))
    tree.links.new(vn_abs.outputs["Value"], rebound.inputs[0])
    tree.links.new(gin.outputs["Restitution"], rebound.inputs[1])
    low_rebound = _math(tree, "LESS_THAN", (-100, -620))
    tree.links.new(rebound.outputs["Value"], low_rebound.inputs[0])
    tree.links.new(gin.outputs["Sleep Velocity"], low_rebound.inputs[1])
    keep_restitution = tree.nodes.new("FunctionNodeBooleanMath")
    keep_restitution.operation = "NOT"
    keep_restitution.location = (80, -620)
    tree.links.new(low_rebound.outputs["Value"], keep_restitution.inputs[0])
    e_eff = _math(tree, "MULTIPLY", (260, -620))
    tree.links.new(gin.outputs["Restitution"], e_eff.inputs[0])
    tree.links.new(keep_restitution.outputs["Boolean"], e_eff.inputs[1])
    bounce = _math(tree, "MULTIPLY", (-300, -560))
    bounce.inputs[1].default_value = -1.0
    tree.links.new(e_eff.outputs["Value"], bounce.inputs[0])
    v_n_bounce = _vmath(tree, "SCALE", (-100, -560))
    tree.links.new(contact_normal.outputs["Output"], v_n_bounce.inputs[0])
    tree.links.new(v_n.outputs["Value"], v_n_bounce.inputs["Scale"])
    v_n_bounce_scaled = _vmath(tree, "SCALE", (100, -560))
    tree.links.new(v_n_bounce.outputs["Vector"], v_n_bounce_scaled.inputs[0])
    tree.links.new(bounce.outputs["Value"], v_n_bounce_scaled.inputs["Scale"])
    v_collide = _vmath(tree, "ADD", (100, -300))
    tree.links.new(v_t_damped.outputs["Vector"], v_collide.inputs[0])
    tree.links.new(v_n_bounce_scaled.outputs["Vector"], v_collide.inputs[1])
    moving_in = _math(tree, "LESS_THAN", (100, -420))
    moving_in.inputs[1].default_value = 0.0
    tree.links.new(v_n.outputs["Value"], moving_in.inputs[0])
    velocity_valid = _math(tree, "MULTIPLY", (300, -420))
    tree.links.new(contact_valid.outputs["Output"], velocity_valid.inputs[0])
    tree.links.new(moving_in.outputs["Value"], velocity_valid.inputs[1])
    vel_switch = tree.nodes.new("GeometryNodeSwitch")
    vel_switch.input_type = "VECTOR"
    vel_switch.location = (500, -240)
    tree.links.new(velocity_valid.outputs["Value"], vel_switch.inputs["Switch"])
    tree.links.new(gin.outputs["Velocity"], vel_switch.inputs["False"])
    tree.links.new(v_collide.outputs["Vector"], vel_switch.inputs["True"])

    # Pushing a point away from one face can make another face nearest.
    # Freeze the contact normal response and selection on the incoming geometry.
    response = tree.nodes.new("GeometryNodeCaptureAttribute")
    response.domain = "POINT"
    response.capture_items.new("VECTOR", "Velocity")
    response.capture_items.new("BOOLEAN", "Contact")
    response.location = (680, 480)
    tree.links.new(gin.outputs["Geometry"], response.inputs["Geometry"])
    tree.links.new(vel_switch.outputs["Output"], response.inputs["Velocity"])
    tree.links.new(contact_valid.outputs["Output"], response.inputs["Contact"])
    tree.links.new(response.outputs["Geometry"], set_pos.inputs["Geometry"])
    store_v = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store_v.data_type = "FLOAT_VECTOR"
    store_v.domain = "POINT"
    store_v.inputs["Name"].default_value = "pnw_velocity"
    store_v.location = (900, 280)
    tree.links.new(set_pos.outputs["Geometry"], store_v.inputs["Geometry"])
    tree.links.new(response.outputs["Velocity"], store_v.inputs["Value"])
    tree.links.new(response.outputs["Contact"], store_v.inputs["Selection"])
    tree.links.new(store_v.outputs["Geometry"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)
def ensure_constraint_pin_anchor(force_rebuild: bool = False) -> bpy.types.NodeTree:
    tree, need_build = _prepare_subgroup("PN_Constraint_PinAnchor", "constraint", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Position", "INPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "INPUT", "NodeSocketVector")
    add_socket(tree, "Rest Position", "INPUT", "NodeSocketVector")
    add_socket(tree, "Pin Weight", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Position", "OUTPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "OUTPUT", "NodeSocketVector")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-520, 0)
    gout.location = (520, 0)

    is_pinned = _math(tree, "GREATER_THAN", (-240, 60))
    is_pinned.inputs[1].default_value = 0.5
    tree.links.new(gin.outputs["Pin Weight"], is_pinned.inputs[0])

    sw_p = tree.nodes.new("GeometryNodeSwitch")
    sw_p.input_type = "VECTOR"
    sw_p.location = (180, 100)
    tree.links.new(is_pinned.outputs["Value"], sw_p.inputs["Switch"])
    tree.links.new(gin.outputs["Position"], sw_p.inputs["False"])
    tree.links.new(gin.outputs["Rest Position"], sw_p.inputs["True"])

    sw_v = tree.nodes.new("GeometryNodeSwitch")
    sw_v.input_type = "VECTOR"
    sw_v.location = (180, -100)
    sw_v.inputs["True"].default_value = (0.0, 0.0, 0.0)
    tree.links.new(is_pinned.outputs["Value"], sw_v.inputs["Switch"])
    tree.links.new(gin.outputs["Velocity"], sw_v.inputs["False"])

    tree.links.new(sw_p.outputs["Output"], gout.inputs["Position"])
    tree.links.new(sw_v.outputs["Output"], gout.inputs["Velocity"])
    return _finalize_subgroup(tree)


def ensure_constraint_distance_chain(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Elastic distance tether to an anchor point (activated when Stiffness > 0 and Rest Length > 0)."""
    tree, need_build = _prepare_subgroup("PN_Constraint_DistanceChain", "constraint", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Position", "INPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "INPUT", "NodeSocketVector")
    add_socket(tree, "Anchor", "INPUT", "NodeSocketVector", (0.0, 0.0, 2.5))
    add_socket(tree, "Rest Length", "INPUT", "NodeSocketFloat", 1.5)
    add_socket(tree, "Stiffness", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Position", "OUTPUT", "NodeSocketVector")
    add_socket(tree, "Velocity", "OUTPUT", "NodeSocketVector")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-780, 0)
    gout.location = (780, 0)

    diff = _vmath(tree, "SUBTRACT", (-520, 120))
    tree.links.new(gin.outputs["Position"], diff.inputs[0])
    tree.links.new(gin.outputs["Anchor"], diff.inputs[1])

    dist = _vmath(tree, "LENGTH", (-320, 40))
    tree.links.new(diff.outputs["Vector"], dist.inputs[0])

    dir_v = _vmath(tree, "NORMALIZE", (-320, 200))
    tree.links.new(diff.outputs["Vector"], dir_v.inputs[0])

    # Excess stretch = max(0, dist - Rest Length)
    excess = _math(tree, "SUBTRACT", (-140, 40))
    tree.links.new(dist.outputs["Value"], excess.inputs[0])
    tree.links.new(gin.outputs["Rest Length"], excess.inputs[1])

    pos_excess = _math(tree, "MAXIMUM", (20, 40))
    pos_excess.inputs[1].default_value = 0.0
    tree.links.new(excess.outputs["Value"], pos_excess.inputs[0])

    corr_mag = _math(tree, "MULTIPLY", (180, 40))
    tree.links.new(pos_excess.outputs["Value"], corr_mag.inputs[0])
    tree.links.new(gin.outputs["Stiffness"], corr_mag.inputs[1])

    corr_vec = _vmath(tree, "SCALE", (340, 120))
    tree.links.new(dir_v.outputs["Vector"], corr_vec.inputs[0])
    tree.links.new(corr_mag.outputs["Value"], corr_vec.inputs["Scale"])

    p_new = _vmath(tree, "SUBTRACT", (520, 120))
    tree.links.new(gin.outputs["Position"], p_new.inputs[0])
    tree.links.new(corr_vec.outputs["Vector"], p_new.inputs[1])

    # Active only when Rest Length > 0 and Stiffness > 0
    has_len = _math(tree, "GREATER_THAN", (-140, -180))
    has_len.inputs[1].default_value = 1e-4
    tree.links.new(gin.outputs["Rest Length"], has_len.inputs[0])

    has_stiff = _math(tree, "GREATER_THAN", (-140, -300))
    has_stiff.inputs[1].default_value = 1e-4
    tree.links.new(gin.outputs["Stiffness"], has_stiff.inputs[0])

    active = _math(tree, "MULTIPLY", (80, -240))
    tree.links.new(has_len.outputs["Value"], active.inputs[0])
    tree.links.new(has_stiff.outputs["Value"], active.inputs[1])

    sw_p = tree.nodes.new("GeometryNodeSwitch")
    sw_p.input_type = "VECTOR"
    sw_p.location = (640, 60)
    tree.links.new(active.outputs["Value"], sw_p.inputs["Switch"])
    tree.links.new(gin.outputs["Position"], sw_p.inputs["False"])
    tree.links.new(p_new.outputs["Vector"], sw_p.inputs["True"])

    # Dampen outward radial velocity softly via position correction difference
    v_corr = _vmath(tree, "SCALE", (340, -140))
    tree.links.new(corr_vec.outputs["Vector"], v_corr.inputs[0])
    v_corr.inputs["Scale"].default_value = 8.0

    v_new = _vmath(tree, "SUBTRACT", (520, -140))
    tree.links.new(gin.outputs["Velocity"], v_new.inputs[0])
    tree.links.new(v_corr.outputs["Vector"], v_new.inputs[1])

    sw_v = tree.nodes.new("GeometryNodeSwitch")
    sw_v.input_type = "VECTOR"
    sw_v.location = (640, -140)
    tree.links.new(active.outputs["Value"], sw_v.inputs["Switch"])
    tree.links.new(gin.outputs["Velocity"], sw_v.inputs["False"])
    tree.links.new(v_new.outputs["Vector"], sw_v.inputs["True"])

    tree.links.new(sw_p.outputs["Output"], gout.inputs["Position"])
    tree.links.new(sw_v.outputs["Output"], gout.inputs["Velocity"])
    return _finalize_subgroup(tree)


def _index_equals(tree: bpy.types.NodeTree, iteration_socket, loc: tuple[float, float]):
    """Return a 0/1 field that is true where the current point index equals iteration."""
    index = tree.nodes.new("GeometryNodeInputIndex")
    index.location = (loc[0] - 160, loc[1])
    compare = _math(tree, "COMPARE", loc)
    tree.links.new(index.outputs["Index"], compare.inputs[0])
    tree.links.new(iteration_socket, compare.inputs[1])
    compare.inputs[2].default_value = 0.00001
    return compare


def ensure_clamp_ground_position(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Position-only floor clamp used before contact solving."""
    tree, need_build = _prepare_subgroup("PN_Collide_GroundClamp", "collider", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Position", "INPUT", "NodeSocketVector")
    add_socket(tree, "Ground Z", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Radius", "INPUT", "NodeSocketFloat", 0.05)
    add_socket(tree, "Position", "OUTPUT", "NodeSocketVector")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-620, 0)
    gout.location = (620, 0)

    sep = tree.nodes.new("ShaderNodeSeparateXYZ")
    sep.location = (-420, 120)
    tree.links.new(gin.outputs["Position"], sep.inputs["Vector"])

    floor_z = _math(tree, "ADD", (-420, -80))
    tree.links.new(gin.outputs["Ground Z"], floor_z.inputs[0])
    tree.links.new(gin.outputs["Radius"], floor_z.inputs[1])
    floor_eps = _math(tree, "ADD", (-240, -80))
    floor_eps.inputs[1].default_value = 0.0001
    tree.links.new(floor_z.outputs["Value"], floor_eps.inputs[0])

    hit = _math(tree, "LESS_THAN", (-220, 80))
    tree.links.new(sep.outputs["Z"], hit.inputs[0])
    tree.links.new(floor_eps.outputs["Value"], hit.inputs[1])

    comb = tree.nodes.new("ShaderNodeCombineXYZ")
    comb.location = (-40, 140)
    tree.links.new(sep.outputs["X"], comb.inputs["X"])
    tree.links.new(sep.outputs["Y"], comb.inputs["Y"])
    tree.links.new(floor_z.outputs["Value"], comb.inputs["Z"])

    sw = tree.nodes.new("GeometryNodeSwitch")
    sw.input_type = "VECTOR"
    sw.location = (240, 80)
    tree.links.new(hit.outputs["Value"], sw.inputs["Switch"])
    tree.links.new(gin.outputs["Position"], sw.inputs["False"])
    tree.links.new(comb.outputs["Vector"], sw.inputs["True"])
    tree.links.new(sw.outputs["Output"], gout.inputs["Position"])
    return _finalize_subgroup(tree)


def ensure_collide_sphere_proxies(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Build the fixed-candidate, two-sided sphere proxy contact solver."""
    mesh_group = ensure_collide_mesh_geometry(force_rebuild)
    tree, need_build = _prepare_subgroup("PN_Collide_SphereProxies", "collider", force_rebuild)
    required_sockets = {"Ground Z", "Collider Geometry"}
    if not need_build and not required_sockets.issubset(
        {
            getattr(item, "name", None)
            for item in tree.interface.items_tree
            if getattr(item, "item_type", None) == "SOCKET"
        }
    ):
        # A pre-0.8 template has no Ground Z contact socket. Never silently
        # reuse the legacy one-point/BVH graph.
        tree, need_build = _prepare_subgroup("PN_Collide_SphereProxies", "collider", True)
    if not need_build:
        return tree
    from . import sphere_contacts

    sphere_contacts.build_sphere_proxy_contact(tree, mesh_group)
    return _finalize_subgroup(tree)


def ensure_constraint_chain_links(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Adjacent point-index chain constraints.

    The first point is tethered to ``Anchor`` and every following point is
    constrained to the previous point with ``Rest Length``. This replaces the
    old single-anchor "all points within radius" tether. Point insertion order is
    the chain topology; scattered non-chain clouds should leave ``Rest Length``
    at zero to disable it.
    """
    tree, need_build = _prepare_subgroup("PN_Constraint_ChainLinks", "constraint", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Anchor", "INPUT", "NodeSocketVector", (0.0, 0.0, 2.5))
    add_socket(tree, "Rest Length", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Stiffness", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-1200, 0)
    gout.location = (1200, 0)

    point_count = tree.nodes.new("GeometryNodeAttributeDomainSize")
    point_count.component = "POINTCLOUD"
    point_count.location = (-1000, -260)
    tree.links.new(gin.outputs["Geometry"], point_count.inputs["Geometry"])

    active_len = _math(tree, "GREATER_THAN", (-1000, -420))
    active_len.inputs[1].default_value = 0.0
    tree.links.new(gin.outputs["Rest Length"], active_len.inputs[0])
    active_stiff = _math(tree, "GREATER_THAN", (-1000, -540))
    active_stiff.inputs[1].default_value = 0.0
    tree.links.new(gin.outputs["Stiffness"], active_stiff.inputs[0])
    active = _math(tree, "MULTIPLY", (-820, -480))
    tree.links.new(active_len.outputs["Value"], active.inputs[0])
    tree.links.new(active_stiff.outputs["Value"], active.inputs[1])
    active_bool = _math(tree, "GREATER_THAN", (-640, -480))
    active_bool.inputs[1].default_value = 0.5
    tree.links.new(active.outputs["Value"], active_bool.inputs[0])
    iter_switch = tree.nodes.new("GeometryNodeSwitch")
    iter_switch.input_type = "INT"
    iter_switch.location = (-600, -180)
    tree.links.new(active_bool.outputs["Value"], iter_switch.inputs["Switch"])
    tree.links.new(point_count.outputs["Point Count"], iter_switch.inputs["True"])
    iter_switch.inputs["False"].default_value = 0

    repeat_in = tree.nodes.new("GeometryNodeRepeatInput")
    repeat_out = tree.nodes.new("GeometryNodeRepeatOutput")
    repeat_in.pair_with_output(repeat_out)
    repeat_in.location = (-760, 0)
    repeat_out.location = (1020, 0)
    tree.links.new(iter_switch.outputs["Output"], repeat_in.inputs["Iterations"])
    tree.links.new(gin.outputs["Geometry"], repeat_in.inputs["Geometry"])

    sel_i = _index_equals(tree, repeat_in.outputs["Iteration"], (-560, 360))

    has_prev = _math(tree, "GREATER_THAN", (-560, 220))
    has_prev.inputs[1].default_value = 0.0
    tree.links.new(repeat_in.outputs["Iteration"], has_prev.inputs[0])
    prev_index = _math(tree, "SUBTRACT", (-740, 120))
    tree.links.new(repeat_in.outputs["Iteration"], prev_index.inputs[0])
    prev_index.inputs[1].default_value = 1.0
    prev_index_clamped = _math(tree, "MAXIMUM", (-560, 120))
    prev_index_clamped.inputs[1].default_value = 0.0
    tree.links.new(prev_index.outputs["Value"], prev_index_clamped.inputs[0])
    sel_prev = _index_equals(tree, prev_index_clamped.outputs["Value"], (-380, 120))
    sel_prev_active = _math(tree, "MULTIPLY", (-180, 120))
    tree.links.new(sel_prev.outputs["Value"], sel_prev_active.inputs[0])
    tree.links.new(has_prev.outputs["Value"], sel_prev_active.inputs[1])

    p_i = tree.nodes.new("GeometryNodeSampleIndex")
    p_i.data_type = "FLOAT_VECTOR"
    p_i.domain = "POINT"
    p_i.location = (-560, -120)
    tree.links.new(repeat_in.outputs["Geometry"], p_i.inputs["Geometry"])
    position_field = tree.nodes.new("GeometryNodeInputPosition")
    position_field.location = (-760, -120)
    tree.links.new(position_field.outputs["Position"], p_i.inputs["Value"])
    tree.links.new(repeat_in.outputs["Iteration"], p_i.inputs["Index"])

    p_prev_sample = tree.nodes.new("GeometryNodeSampleIndex")
    p_prev_sample.data_type = "FLOAT_VECTOR"
    p_prev_sample.domain = "POINT"
    p_prev_sample.location = (-380, -120)
    tree.links.new(repeat_in.outputs["Geometry"], p_prev_sample.inputs["Geometry"])
    tree.links.new(position_field.outputs["Position"], p_prev_sample.inputs["Value"])
    tree.links.new(prev_index_clamped.outputs["Value"], p_prev_sample.inputs["Index"])

    p_prev = tree.nodes.new("GeometryNodeSwitch")
    p_prev.input_type = "VECTOR"
    p_prev.location = (-180, -120)
    tree.links.new(has_prev.outputs["Value"], p_prev.inputs["Switch"])
    tree.links.new(gin.outputs["Anchor"], p_prev.inputs["False"])
    tree.links.new(p_prev_sample.outputs["Value"], p_prev.inputs["True"])

    vel_field = tree.nodes.new("GeometryNodeInputNamedAttribute")
    vel_field.data_type = "FLOAT_VECTOR"
    vel_field.inputs["Name"].default_value = "pnw_velocity"
    vel_field.location = (-760, -300)
    v_i = tree.nodes.new("GeometryNodeSampleIndex")
    v_i.data_type = "FLOAT_VECTOR"
    v_i.domain = "POINT"
    v_i.location = (-560, -300)
    tree.links.new(repeat_in.outputs["Geometry"], v_i.inputs["Geometry"])
    tree.links.new(vel_field.outputs["Attribute"], v_i.inputs["Value"])
    tree.links.new(repeat_in.outputs["Iteration"], v_i.inputs["Index"])
    v_prev_sample = tree.nodes.new("GeometryNodeSampleIndex")
    v_prev_sample.data_type = "FLOAT_VECTOR"
    v_prev_sample.domain = "POINT"
    v_prev_sample.location = (-380, -300)
    tree.links.new(repeat_in.outputs["Geometry"], v_prev_sample.inputs["Geometry"])
    tree.links.new(vel_field.outputs["Attribute"], v_prev_sample.inputs["Value"])
    tree.links.new(prev_index_clamped.outputs["Value"], v_prev_sample.inputs["Index"])
    v_prev_zero = tree.nodes.new("ShaderNodeCombineXYZ")
    v_prev_zero.location = (-380, -460)
    v_prev = tree.nodes.new("GeometryNodeSwitch")
    v_prev.input_type = "VECTOR"
    v_prev.location = (-180, -300)
    tree.links.new(has_prev.outputs["Value"], v_prev.inputs["Switch"])
    tree.links.new(v_prev_zero.outputs["Vector"], v_prev.inputs["False"])
    tree.links.new(v_prev_sample.outputs["Value"], v_prev.inputs["True"])

    delta = _vmath(tree, "SUBTRACT", (20, -120))
    tree.links.new(p_i.outputs["Value"], delta.inputs[0])
    tree.links.new(p_prev.outputs["Output"], delta.inputs[1])
    distance = _vmath(tree, "LENGTH", (200, -120))
    tree.links.new(delta.outputs["Vector"], distance.inputs[0])
    direction_raw = _vmath(tree, "NORMALIZE", (200, 20))
    tree.links.new(delta.outputs["Vector"], direction_raw.inputs[0])
    not_tiny = _math(tree, "GREATER_THAN", (200, 200))
    not_tiny.inputs[1].default_value = 0.000001
    tree.links.new(distance.outputs["Value"], not_tiny.inputs[0])
    fallback = tree.nodes.new("ShaderNodeCombineXYZ")
    fallback.inputs["Z"].default_value = -1.0
    fallback.location = (380, 200)
    direction = tree.nodes.new("GeometryNodeSwitch")
    direction.input_type = "VECTOR"
    direction.location = (560, 120)
    tree.links.new(not_tiny.outputs["Value"], direction.inputs["Switch"])
    tree.links.new(fallback.outputs["Vector"], direction.inputs["False"])
    tree.links.new(direction_raw.outputs["Vector"], direction.inputs["True"])

    error = _math(tree, "SUBTRACT", (380, -260))
    tree.links.new(distance.outputs["Value"], error.inputs[0])
    tree.links.new(gin.outputs["Rest Length"], error.inputs[1])
    error_stretch = _math(tree, "MAXIMUM", (560, -220))
    error_stretch.inputs[1].default_value = 0.0
    tree.links.new(error.outputs["Value"], error_stretch.inputs[0])
    error_compress = _math(tree, "MINIMUM", (560, -380))
    error_compress.inputs[1].default_value = 0.0
    tree.links.new(error.outputs["Value"], error_compress.inputs[0])
    compress_scale = _math(tree, "MULTIPLY", (740, -380))
    tree.links.new(error_compress.outputs["Value"], compress_scale.inputs[0])
    compress_scale.inputs[1].default_value = 0.25
    error_total = _math(tree, "ADD", (920, -280))
    tree.links.new(error_stretch.outputs["Value"], error_total.inputs[0])
    tree.links.new(compress_scale.outputs["Value"], error_total.inputs[1])
    stiffness_corr = _math(tree, "MULTIPLY", (1100, -280))
    tree.links.new(error_total.outputs["Value"], stiffness_corr.inputs[0])
    tree.links.new(gin.outputs["Stiffness"], stiffness_corr.inputs[1])
    corr = _math(tree, "MULTIPLY", (1280, -280))
    tree.links.new(stiffness_corr.outputs["Value"], corr.inputs[0])
    tree.links.new(active.outputs["Value"], corr.inputs[1])

    is_first = _math(tree, "COMPARE", (-180, 320))
    tree.links.new(repeat_in.outputs["Iteration"], is_first.inputs[0])
    is_first.inputs[1].default_value = 0.0
    is_first.inputs[2].default_value = 0.00001
    weight_i = _math(tree, "MULTIPLY", (20, 320))
    tree.links.new(is_first.outputs["Value"], weight_i.inputs[0])
    weight_i.inputs[1].default_value = -0.5
    weight_i_add = _math(tree, "ADD", (200, 320))
    tree.links.new(weight_i.outputs["Value"], weight_i_add.inputs[0])
    weight_i_add.inputs[1].default_value = 1.0

    corr_i = _math(tree, "MULTIPLY", (920, -120))
    tree.links.new(corr.outputs["Value"], corr_i.inputs[0])
    tree.links.new(weight_i_add.outputs["Value"], corr_i.inputs[1])
    corr_prev = _math(tree, "MULTIPLY", (920, -20))
    tree.links.new(corr.outputs["Value"], corr_prev.inputs[0])
    corr_prev.inputs[1].default_value = 0.5

    shift_i = _vmath(tree, "SCALE", (1100, 20))
    tree.links.new(direction.outputs["Output"], shift_i.inputs[0])
    tree.links.new(corr_i.outputs["Value"], shift_i.inputs["Scale"])
    new_i = _vmath(tree, "SUBTRACT", (1280, 20))
    tree.links.new(p_i.outputs["Value"], new_i.inputs[0])
    tree.links.new(shift_i.outputs["Vector"], new_i.inputs[1])

    shift_prev = _vmath(tree, "SCALE", (1100, -180))
    tree.links.new(direction.outputs["Output"], shift_prev.inputs[0])
    tree.links.new(corr_prev.outputs["Value"], shift_prev.inputs["Scale"])
    new_prev = _vmath(tree, "ADD", (1280, -180))
    tree.links.new(p_prev.outputs["Output"], new_prev.inputs[0])
    tree.links.new(shift_prev.outputs["Vector"], new_prev.inputs[1])

    # Constraint-space velocity projection: remove relative radial velocity
    # without injecting correction/dt energy (stable for large initial overlap).
    rel_vel = _vmath(tree, "SUBTRACT", (1100, -360))
    tree.links.new(v_i.outputs["Value"], rel_vel.inputs[0])
    tree.links.new(v_prev.outputs["Output"], rel_vel.inputs[1])
    v_rel = _vmath(tree, "DOT_PRODUCT", (1280, -360))
    tree.links.new(rel_vel.outputs["Vector"], v_rel.inputs[0])
    tree.links.new(direction.outputs["Output"], v_rel.inputs[1])
    v_rel_active = _math(tree, "MULTIPLY", (1460, -520))
    tree.links.new(v_rel.outputs["Value"], v_rel_active.inputs[0])
    tree.links.new(active.outputs["Value"], v_rel_active.inputs[1])
    v_i_scale = _math(tree, "MULTIPLY", (1280, -520))
    tree.links.new(v_rel_active.outputs["Value"], v_i_scale.inputs[0])
    tree.links.new(weight_i_add.outputs["Value"], v_i_scale.inputs[1])
    v_i_shift = _vmath(tree, "SCALE", (1460, -360))
    tree.links.new(direction.outputs["Output"], v_i_shift.inputs[0])
    tree.links.new(v_i_scale.outputs["Value"], v_i_shift.inputs["Scale"])
    vel_i_new = _vmath(tree, "SUBTRACT", (1640, -360))
    tree.links.new(v_i.outputs["Value"], vel_i_new.inputs[0])
    tree.links.new(v_i_shift.outputs["Vector"], vel_i_new.inputs[1])

    v_prev_scale = _math(tree, "MULTIPLY", (1280, -700))
    tree.links.new(v_rel_active.outputs["Value"], v_prev_scale.inputs[0])
    v_prev_scale.inputs[1].default_value = 0.5
    v_prev_shift = _vmath(tree, "SCALE", (1460, -700))
    tree.links.new(direction.outputs["Output"], v_prev_shift.inputs[0])
    tree.links.new(v_prev_scale.outputs["Value"], v_prev_shift.inputs["Scale"])
    vel_prev_new = _vmath(tree, "ADD", (1640, -700))
    tree.links.new(v_prev.outputs["Output"], vel_prev_new.inputs[0])
    tree.links.new(v_prev_shift.outputs["Vector"], vel_prev_new.inputs[1])

    position_i = tree.nodes.new("GeometryNodeSwitch")
    position_i.input_type = "VECTOR"
    position_i.location = (1460, 120)
    tree.links.new(sel_i.outputs["Value"], position_i.inputs["Switch"])
    tree.links.new(position_field.outputs["Position"], position_i.inputs["False"])
    tree.links.new(new_i.outputs["Vector"], position_i.inputs["True"])
    position_prev = tree.nodes.new("GeometryNodeSwitch")
    position_prev.input_type = "VECTOR"
    position_prev.location = (1640, 120)
    tree.links.new(sel_prev_active.outputs["Value"], position_prev.inputs["Switch"])
    tree.links.new(position_i.outputs["Output"], position_prev.inputs["False"])
    tree.links.new(new_prev.outputs["Vector"], position_prev.inputs["True"])

    set_position = tree.nodes.new("GeometryNodeSetPosition")
    set_position.location = (1820, 260)
    tree.links.new(repeat_in.outputs["Geometry"], set_position.inputs["Geometry"])
    tree.links.new(position_prev.outputs["Output"], set_position.inputs["Position"])

    velocity_i = tree.nodes.new("GeometryNodeSwitch")
    velocity_i.input_type = "VECTOR"
    velocity_i.location = (1460, -540)
    tree.links.new(sel_i.outputs["Value"], velocity_i.inputs["Switch"])
    tree.links.new(vel_field.outputs["Attribute"], velocity_i.inputs["False"])
    tree.links.new(vel_i_new.outputs["Vector"], velocity_i.inputs["True"])
    velocity_prev = tree.nodes.new("GeometryNodeSwitch")
    velocity_prev.input_type = "VECTOR"
    velocity_prev.location = (1640, -540)
    tree.links.new(sel_prev_active.outputs["Value"], velocity_prev.inputs["Switch"])
    tree.links.new(velocity_i.outputs["Output"], velocity_prev.inputs["False"])
    tree.links.new(vel_prev_new.outputs["Vector"], velocity_prev.inputs["True"])
    store_velocity = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store_velocity.data_type = "FLOAT_VECTOR"
    store_velocity.domain = "POINT"
    store_velocity.inputs["Name"].default_value = "pnw_velocity"
    store_velocity.location = (1820, -260)
    tree.links.new(set_position.outputs["Geometry"], store_velocity.inputs["Geometry"])
    tree.links.new(velocity_prev.outputs["Output"], store_velocity.inputs["Value"])

    tree.links.new(store_velocity.outputs["Geometry"], repeat_out.inputs["Geometry"])
    tree.links.new(repeat_out.outputs["Geometry"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)


def ensure_solver_rigid_scatter_step(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Substep solver: forces, adjacent chain links, sphere contacts, colliders, rest."""
    sg_chain = ensure_constraint_chain_links(force_rebuild)
    sg_ground_clamp = ensure_clamp_ground_position(force_rebuild)
    sg_contact = ensure_collide_sphere_proxies(force_rebuild)
    from . import sphere_contacts

    tree, need_build = _prepare_subgroup("PN_Solver_RigidScatterStep", "solver", force_rebuild)
    if not need_build:
        return tree

    def _bool_node(op, loc):
        node = tree.nodes.new("FunctionNodeBooleanMath")
        node.operation = op
        node.location = loc
        return node

    add_socket(tree, "Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Delta Time", "INPUT", "NodeSocketFloat", 0.01)
    add_socket(tree, "Time", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Gravity", "INPUT", "NodeSocketVector", (0.0, 0.0, -9.81))
    add_socket(tree, "Drag", "INPUT", "NodeSocketFloat", 0.04)
    add_socket(tree, "Wind Vector", "INPUT", "NodeSocketVector", (0.0, 0.0, 0.0))
    add_socket(tree, "Wind Turbulence", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Collider", "INPUT", "NodeSocketObject")
    add_socket(tree, "Contact Thickness", "INPUT", "NodeSocketFloat", 0.08)
    add_socket(tree, "Ground Z", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Radius", "INPUT", "NodeSocketFloat", 0.12)
    add_socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.35)
    add_socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.45)
    add_socket(tree, "Sleep Velocity", "INPUT", "NodeSocketFloat", 0.08)
    add_socket(tree, "Angular Spin", "INPUT", "NodeSocketFloat", 1.2)
    add_socket(tree, "Anchor", "INPUT", "NodeSocketVector", (0.0, 0.0, 2.5))
    add_socket(tree, "Rest Length", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Stiffness", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Contact Iterations", "INPUT", "NodeSocketInt", 3)
    add_socket(tree, "Collider Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-1200, 0)
    gout.location = (1280, 0)

    pos_in = tree.nodes.new("GeometryNodeInputPosition")
    pos_in.location = (-1000, -180)
    vel_in = tree.nodes.new("GeometryNodeInputNamedAttribute")
    vel_in.data_type = "FLOAT_VECTOR"
    vel_in.inputs["Name"].default_value = "pnw_velocity"
    vel_in.location = (-1000, -340)
    rot_in = tree.nodes.new("GeometryNodeInputNamedAttribute")
    rot_in.data_type = "FLOAT_VECTOR"
    rot_in.inputs["Name"].default_value = "pnw_rotation"
    rot_in.location = (-1000, -500)

    # Resolve the collider and capture the current static support before the
    # integration gate.  A stale sleep flag must never suppress gravity when
    # the floor/mesh support has moved away in this substep.
    collider_info = tree.nodes.new("GeometryNodeObjectInfo")
    collider_info.transform_space = "ORIGINAL"
    collider_info.location = (840, -520)
    tree.links.new(gin.outputs["Collider"], collider_info.inputs["Object"])
    collider_world = tree.nodes.new("GeometryNodeTransform")
    collider_world.inputs["Mode"].default_value = "Matrix"
    collider_world.location = (1040, -520)
    tree.links.new(collider_info.outputs["Geometry"], collider_world.inputs["Geometry"])
    tree.links.new(collider_info.outputs["Transform"], collider_world.inputs["Transform"])
    supplied_collider_count = tree.nodes.new("GeometryNodeAttributeDomainSize")
    supplied_collider_count.component = "MESH"
    supplied_collider_count.location = (840, -720)
    tree.links.new(gin.outputs["Collider Geometry"], supplied_collider_count.inputs["Geometry"])
    has_supplied_collider = _math(tree, "GREATER_THAN", (1040, -720))
    has_supplied_collider.inputs[1].default_value = 0.0
    tree.links.new(supplied_collider_count.outputs["Face Count"], has_supplied_collider.inputs[0])
    collider_switch = tree.nodes.new("GeometryNodeSwitch")
    collider_switch.input_type = "GEOMETRY"
    collider_switch.location = (1240, -520)
    tree.links.new(has_supplied_collider.outputs["Value"], collider_switch.inputs["Switch"])
    tree.links.new(collider_world.outputs["Geometry"], collider_switch.inputs["False"])
    tree.links.new(gin.outputs["Collider Geometry"], collider_switch.inputs["True"])

    gravity_sep = tree.nodes.new("ShaderNodeSeparateXYZ")
    gravity_sep.location = (1260, -300)
    tree.links.new(gin.outputs["Gravity"], gravity_sep.inputs["Vector"])
    gravity_ax = _math(tree, "ABSOLUTE", (1440, -300))
    tree.links.new(gravity_sep.outputs["X"], gravity_ax.inputs[0])
    gravity_ay = _math(tree, "ABSOLUTE", (1440, -380))
    tree.links.new(gravity_sep.outputs["Y"], gravity_ay.inputs[0])
    gravity_x0 = _math(tree, "LESS_THAN", (1620, -300))
    tree.links.new(gravity_ax.outputs["Value"], gravity_x0.inputs[0])
    gravity_x0.inputs[1].default_value = 1e-6
    gravity_y0 = _math(tree, "LESS_THAN", (1620, -380))
    tree.links.new(gravity_ay.outputs["Value"], gravity_y0.inputs[0])
    gravity_y0.inputs[1].default_value = 1e-6
    gravity_xy0 = _bool_node("AND", (1800, -340))
    tree.links.new(gravity_x0.outputs["Value"], gravity_xy0.inputs[0])
    tree.links.new(gravity_y0.outputs["Value"], gravity_xy0.inputs[1])
    gravity_down_z = _math(tree, "LESS_THAN", (1620, -460))
    tree.links.new(gravity_sep.outputs["Z"], gravity_down_z.inputs[0])
    gravity_down_z.inputs[1].default_value = -1e-9
    gravity_down = _bool_node("AND", (1980, -400))
    tree.links.new(gravity_xy0.outputs["Boolean"], gravity_down.inputs[0])
    tree.links.new(gravity_down_z.outputs["Value"], gravity_down.inputs[1])

    # Sleep roots require the conservative default downward gravity.  Support
    # projection stays valid for lateral/upward gravity; only sleep is gated.
    st_root_enabled = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_root_enabled.data_type = "BOOLEAN"
    st_root_enabled.domain = "POINT"
    st_root_enabled.inputs["Name"].default_value = "pnw_sleep_root_enabled"
    st_root_enabled.location = (2160, -400)
    tree.links.new(gin.outputs["Geometry"], st_root_enabled.inputs["Geometry"])
    tree.links.new(gravity_down.outputs["Boolean"], st_root_enabled.inputs["Value"])

    prev_norm_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    prev_norm_attr.data_type = "FLOAT_VECTOR"
    prev_norm_attr.inputs["Name"].default_value = "pnw_support_normal"
    prev_norm_attr.location = (2340, -400)
    capture_prev_norm = tree.nodes.new("GeometryNodeCaptureAttribute")
    capture_prev_norm.domain = "POINT"
    capture_prev_norm.capture_items.new("VECTOR", "PrevSupportNormal")
    capture_prev_norm.location = (2520, -400)
    tree.links.new(st_root_enabled.outputs["Geometry"], capture_prev_norm.inputs["Geometry"])
    tree.links.new(prev_norm_attr.outputs["Attribute"], capture_prev_norm.inputs["PrevSupportNormal"])
    prev_root_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    prev_root_attr.data_type = "BOOLEAN"
    prev_root_attr.inputs["Name"].default_value = "pnw_sleep_root_valid"
    prev_root_attr.location = (2340, -520)
    capture_prev_root = tree.nodes.new("GeometryNodeCaptureAttribute")
    capture_prev_root.domain = "POINT"
    capture_prev_root.capture_items.new("BOOLEAN", "PrevSleepRootValid")
    capture_prev_root.location = (2700, -400)
    tree.links.new(capture_prev_norm.outputs["Geometry"], capture_prev_root.inputs["Geometry"])
    tree.links.new(prev_root_attr.outputs["Attribute"], capture_prev_root.inputs["PrevSleepRootValid"])

    pre_support = sphere_contacts._add_support_capture(
        tree,
        capture_prev_root.outputs["Geometry"],
        collider_switch.outputs["Output"],
        gin.outputs["Radius"],
        gin.outputs["Ground Z"],
        (-2600, -1000),
        root_enabled=gravity_down.outputs["Boolean"],
    )
    cur_norm_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    cur_norm_attr.data_type = "FLOAT_VECTOR"
    cur_norm_attr.inputs["Name"].default_value = "pnw_support_normal"
    cur_norm_attr.location = (-2600, -1200)
    capture_cur_norm = tree.nodes.new("GeometryNodeCaptureAttribute")
    capture_cur_norm.domain = "POINT"
    capture_cur_norm.capture_items.new("VECTOR", "CurSupportNormal")
    capture_cur_norm.location = (-2440, -1200)
    tree.links.new(pre_support, capture_cur_norm.inputs["Geometry"])
    tree.links.new(cur_norm_attr.outputs["Attribute"], capture_cur_norm.inputs["CurSupportNormal"])
    cur_root_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    cur_root_attr.data_type = "BOOLEAN"
    cur_root_attr.inputs["Name"].default_value = "pnw_sleep_root_valid"
    cur_root_attr.location = (-2600, -1320)
    capture_cur_root = tree.nodes.new("GeometryNodeCaptureAttribute")
    capture_cur_root.domain = "POINT"
    capture_cur_root.capture_items.new("BOOLEAN", "CurSleepRootValid")
    capture_cur_root.location = (-2280, -1200)
    tree.links.new(capture_cur_norm.outputs["Geometry"], capture_cur_root.inputs["Geometry"])
    tree.links.new(cur_root_attr.outputs["Attribute"], capture_cur_root.inputs["CurSleepRootValid"])
    cur_root_delete_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    cur_root_delete_attr.data_type = "BOOLEAN"
    cur_root_delete_attr.inputs["Name"].default_value = "pnw_sleep_root_valid"
    cur_root_delete_attr.location = (-2600, -1440)
    not_cur_root = _bool_node("NOT", (-2440, -1440))
    tree.links.new(cur_root_delete_attr.outputs["Attribute"], not_cur_root.inputs[0])
    delete_nonroot = tree.nodes.new("GeometryNodeDeleteGeometry")
    delete_nonroot.domain = "POINT"
    delete_nonroot.location = (-2280, -1440)
    tree.links.new(pre_support, delete_nonroot.inputs["Geometry"])
    tree.links.new(not_cur_root.outputs["Boolean"], delete_nonroot.inputs["Selection"])
    root_count = tree.nodes.new("GeometryNodeAttributeDomainSize")
    root_count.component = "POINTCLOUD"
    root_count.location = (-2120, -1440)
    tree.links.new(delete_nonroot.outputs["Geometry"], root_count.inputs["Geometry"])
    prev_root_count_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    prev_root_count_attr.data_type = "FLOAT"
    prev_root_count_attr.inputs["Name"].default_value = "pnw_sleep_root_count"
    prev_root_count_attr.location = (-2280, -1560)
    root_count_drop = _math(tree, "SUBTRACT", (-2120, -1560))
    tree.links.new(prev_root_count_attr.outputs["Attribute"], root_count_drop.inputs[0])
    tree.links.new(root_count.outputs["Point Count"], root_count_drop.inputs[1])
    root_lost = _math(tree, "GREATER_THAN", (-1940, -1560))
    tree.links.new(root_count_drop.outputs["Value"], root_lost.inputs[0])
    root_lost.inputs[1].default_value = 0.5
    capture_start = tree.nodes.new("GeometryNodeCaptureAttribute")
    capture_start.domain = "POINT"
    capture_start.capture_items.new("VECTOR", "StartPosition")
    capture_start.location = (-1100, -180)
    tree.links.new(capture_cur_root.outputs["Geometry"], capture_start.inputs["Geometry"])
    start_pos_field = tree.nodes.new("GeometryNodeInputPosition")
    start_pos_field.location = (-1200, -260)
    tree.links.new(start_pos_field.outputs["Position"], capture_start.inputs["StartPosition"])
    st_start_pos = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_start_pos.data_type = "FLOAT_VECTOR"
    st_start_pos.domain = "POINT"
    st_start_pos.inputs["Name"].default_value = "pnw_substep_start_pos"
    st_start_pos.location = (-1000, -180)
    tree.links.new(capture_start.outputs["Geometry"], st_start_pos.inputs["Geometry"])
    tree.links.new(capture_start.outputs["StartPosition"], st_start_pos.inputs["Value"])

    # Conservative moving-support detector.  A translated collider changes its
    # bounding-box centre; a changing Ground Z is caught separately.  While the
    # support moves, sleeping is disabled and held poses are released so the
    # current contact solve (not the old pose) decides the result.
    collider_bbox = tree.nodes.new("GeometryNodeBoundBox")
    collider_bbox.location = (1440, -520)
    tree.links.new(collider_switch.outputs["Output"], collider_bbox.inputs["Geometry"])
    bbox_sum = _vmath(tree, "ADD", (1620, -520))
    tree.links.new(collider_bbox.outputs["Min"], bbox_sum.inputs[0])
    tree.links.new(collider_bbox.outputs["Max"], bbox_sum.inputs[1])
    bbox_center = _vmath(tree, "SCALE", (1800, -520))
    tree.links.new(bbox_sum.outputs["Vector"], bbox_center.inputs[0])
    bbox_center.inputs["Scale"].default_value = 0.5
    collider_face_count = tree.nodes.new("GeometryNodeAttributeDomainSize")
    collider_face_count.component = "MESH"
    collider_face_count.location = (1440, -700)
    tree.links.new(collider_switch.outputs["Output"], collider_face_count.inputs["Geometry"])
    collider_present = _math(tree, "GREATER_THAN", (1620, -700))
    collider_present.inputs[1].default_value = 0.0
    tree.links.new(collider_face_count.outputs["Face Count"], collider_present.inputs[0])
    last_center_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    last_center_attr.data_type = "FLOAT_VECTOR"
    last_center_attr.inputs["Name"].default_value = "pnw_collider_center"
    last_center_attr.location = (1440, -860)
    capture_prev_center = tree.nodes.new("GeometryNodeCaptureAttribute")
    capture_prev_center.domain = "POINT"
    capture_prev_center.capture_items.new("VECTOR", "PreviousCenter")
    capture_prev_center.location = (1620, -760)
    tree.links.new(st_start_pos.outputs["Geometry"], capture_prev_center.inputs["Geometry"])
    tree.links.new(last_center_attr.outputs["Attribute"], capture_prev_center.inputs["PreviousCenter"])
    center_delta_vec = _vmath(tree, "SUBTRACT", (1620, -860))
    tree.links.new(bbox_center.outputs["Vector"], center_delta_vec.inputs[0])
    tree.links.new(capture_prev_center.outputs["PreviousCenter"], center_delta_vec.inputs[1])
    center_delta = _vmath(tree, "LENGTH", (1800, -860))
    tree.links.new(center_delta_vec.outputs["Vector"], center_delta.inputs[0])
    support_eps = _math(tree, "MULTIPLY", (1620, -960))
    tree.links.new(gin.outputs["Radius"], support_eps.inputs[0])
    support_eps.inputs[1].default_value = sphere_contacts.SUPPORT_GAP_RATIO
    center_moved_raw = _math(tree, "GREATER_THAN", (1800, -860))
    tree.links.new(center_delta.outputs["Value"], center_moved_raw.inputs[0])
    tree.links.new(support_eps.outputs["Value"], center_moved_raw.inputs[1])
    center_moved = _bool_node("AND", (1980, -860))
    tree.links.new(collider_present.outputs["Value"], center_moved.inputs[0])
    tree.links.new(center_moved_raw.outputs["Value"], center_moved.inputs[1])
    last_ground_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    last_ground_attr.data_type = "FLOAT"
    last_ground_attr.inputs["Name"].default_value = "pnw_last_ground_z"
    last_ground_attr.location = (1440, -1020)
    capture_prev_ground = tree.nodes.new("GeometryNodeCaptureAttribute")
    capture_prev_ground.domain = "POINT"
    capture_prev_ground.capture_items.new("FLOAT", "PreviousGroundZ")
    capture_prev_ground.location = (1620, -940)
    tree.links.new(capture_prev_center.outputs["Geometry"], capture_prev_ground.inputs["Geometry"])
    tree.links.new(last_ground_attr.outputs["Attribute"], capture_prev_ground.inputs["PreviousGroundZ"])
    ground_delta = _math(tree, "SUBTRACT", (1620, -1020))
    tree.links.new(gin.outputs["Ground Z"], ground_delta.inputs[0])
    tree.links.new(capture_prev_ground.outputs["PreviousGroundZ"], ground_delta.inputs[1])
    ground_delta_abs = _math(tree, "ABSOLUTE", (1800, -1020))
    tree.links.new(ground_delta.outputs["Value"], ground_delta_abs.inputs[0])
    ground_moved = _math(tree, "GREATER_THAN", (1980, -1020))
    tree.links.new(ground_delta_abs.outputs["Value"], ground_moved.inputs[0])
    tree.links.new(support_eps.outputs["Value"], ground_moved.inputs[1])
    normal_dot = _vmath(tree, "DOT_PRODUCT", (2160, -1180))
    tree.links.new(capture_prev_norm.outputs["PrevSupportNormal"], normal_dot.inputs[0])
    tree.links.new(capture_cur_norm.outputs["CurSupportNormal"], normal_dot.inputs[1])
    normal_changed_raw = _math(tree, "LESS_THAN", (2340, -1180))
    tree.links.new(normal_dot.outputs["Value"], normal_changed_raw.inputs[0])
    normal_changed_raw.inputs[1].default_value = 0.999999
    normal_changed_rooted = _bool_node("AND", (2520, -1180))
    tree.links.new(capture_prev_root.outputs["PrevSleepRootValid"], normal_changed_rooted.inputs[0])
    tree.links.new(capture_cur_root.outputs["CurSleepRootValid"], normal_changed_rooted.inputs[1])
    normal_changed = _bool_node("AND", (2700, -1180))
    tree.links.new(normal_changed_rooted.outputs["Boolean"], normal_changed.inputs[0])
    tree.links.new(normal_changed_raw.outputs["Value"], normal_changed.inputs[1])
    support_moved_center = _bool_node("OR", (2160, -960))
    tree.links.new(center_moved.outputs["Boolean"], support_moved_center.inputs[0])
    tree.links.new(ground_moved.outputs["Value"], support_moved_center.inputs[1])
    support_moved = _bool_node("OR", (2880, -960))
    tree.links.new(support_moved_center.outputs["Boolean"], support_moved.inputs[0])
    tree.links.new(normal_changed.outputs["Boolean"], support_moved.inputs[1])
    support_moved_roots = _bool_node("OR", (3060, -960))
    tree.links.new(support_moved.outputs["Boolean"], support_moved_roots.inputs[0])
    tree.links.new(root_lost.outputs["Value"], support_moved_roots.inputs[1])
    support_moved = support_moved_roots
    # Freeze the comparison against the previous substep on the input geometry
    # before any attributes are updated for this substep.  Reading the raw
    # collider-centre attribute later can otherwise resolve against the value
    # just written downstream in the same evaluation.
    st_support_moved = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_support_moved.data_type = "BOOLEAN"
    st_support_moved.domain = "POINT"
    st_support_moved.inputs["Name"].default_value = "pnw_support_moved"
    st_support_moved.location = (2520, -960)
    tree.links.new(capture_prev_ground.outputs["Geometry"], st_support_moved.inputs["Geometry"])
    tree.links.new(support_moved.outputs["Boolean"], st_support_moved.inputs["Value"])
    moved_count_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    moved_count_attr.data_type = "FLOAT"
    moved_count_attr.inputs["Name"].default_value = "pnw_support_moved_frames"
    moved_count_attr.location = (2340, -1120)
    moved_minus = _math(tree, "SUBTRACT", (2520, -1120))
    tree.links.new(moved_count_attr.outputs["Attribute"], moved_minus.inputs[0])
    moved_minus.inputs[1].default_value = 1.0
    moved_decay = _math(tree, "MAXIMUM", (2700, -1120))
    tree.links.new(moved_minus.outputs["Value"], moved_decay.inputs[0])
    moved_decay.inputs[1].default_value = 0.0
    moved_count = tree.nodes.new("GeometryNodeSwitch")
    moved_count.input_type = "FLOAT"
    moved_count.location = (2880, -1120)
    tree.links.new(support_moved.outputs["Boolean"], moved_count.inputs["Switch"])
    tree.links.new(moved_decay.outputs["Value"], moved_count.inputs["False"])
    moved_count.inputs["True"].default_value = 32.0
    support_unstable = _math(tree, "GREATER_THAN", (3060, -1120))
    tree.links.new(moved_count.outputs["Output"], support_unstable.inputs[0])
    support_unstable.inputs[1].default_value = 0.5
    support_stable = _bool_node("NOT", (3240, -1120))
    tree.links.new(support_unstable.outputs["Value"], support_stable.inputs[0])

    captured_velocity = tree.nodes.new("GeometryNodeCaptureAttribute")
    captured_velocity.domain = "POINT"
    captured_velocity.capture_items.new("VECTOR", "Velocity")
    captured_velocity.location = (-900, -340)
    tree.links.new(st_support_moved.outputs["Geometry"], captured_velocity.inputs["Geometry"])
    tree.links.new(vel_in.outputs["Attribute"], captured_velocity.inputs["Velocity"])
    sleep_state_in = tree.nodes.new("GeometryNodeInputNamedAttribute")
    sleep_state_in.data_type = "BOOLEAN"
    sleep_state_in.inputs["Name"].default_value = "pnw_sleep"
    sleep_state_in.location = (-900, -520)
    zero_sleep_vec = tree.nodes.new("ShaderNodeCombineXYZ")
    zero_sleep_vec.location = (-740, -620)
    v_start = tree.nodes.new("GeometryNodeSwitch")
    v_start.input_type = "VECTOR"
    v_start.location = (-740, -340)
    tree.links.new(captured_velocity.outputs["Velocity"], v_start.inputs["False"])
    tree.links.new(zero_sleep_vec.outputs["Vector"], v_start.inputs["True"])

    # Force/drag/wind are built from raw nodes here. Keeping this chain local
    # avoids field-context ambiguity when it feeds geometry-domain Repeat Zones.
    gravity_dt = _vmath(tree, "SCALE", (-740, -160))
    tree.links.new(gin.outputs["Gravity"], gravity_dt.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], gravity_dt.inputs["Scale"])
    velocity_gravity = _vmath(tree, "ADD", (-560, -160))
    tree.links.new(v_start.outputs["Output"], velocity_gravity.inputs[0])
    tree.links.new(gravity_dt.outputs["Vector"], velocity_gravity.inputs[1])

    drag_dt = _math(tree, "MULTIPLY", (-740, -320))
    tree.links.new(gin.outputs["Drag"], drag_dt.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], drag_dt.inputs[1])
    drag_keep = _math(tree, "SUBTRACT", (-560, -320))
    drag_keep.inputs[0].default_value = 1.0
    tree.links.new(drag_dt.outputs["Value"], drag_keep.inputs[1])
    drag_clamp = _math(tree, "MAXIMUM", (-380, -320))
    drag_clamp.inputs[1].default_value = 0.0
    tree.links.new(drag_keep.outputs["Value"], drag_clamp.inputs[0])
    velocity_drag = _vmath(tree, "SCALE", (-380, -160))
    tree.links.new(velocity_gravity.outputs["Vector"], velocity_drag.inputs[0])
    tree.links.new(drag_clamp.outputs["Value"], velocity_drag.inputs["Scale"])

    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.noise_dimensions = "4D"
    noise.inputs["Scale"].default_value = 1.6
    noise.inputs["Detail"].default_value = 2.0
    noise.location = (-740, -520)
    tree.links.new(pos_in.outputs["Position"], noise.inputs["Vector"])
    tree.links.new(gin.outputs["Time"], noise.inputs["W"])
    noise_center = _vmath(tree, "SUBTRACT", (-560, -520))
    tree.links.new(noise.outputs["Color"], noise_center.inputs[0])
    noise_center.inputs[1].default_value = (0.5, 0.5, 0.5)
    turbulence = _vmath(tree, "SCALE", (-380, -520))
    tree.links.new(noise_center.outputs["Vector"], turbulence.inputs[0])
    tree.links.new(gin.outputs["Wind Turbulence"], turbulence.inputs["Scale"])
    wind_total = _vmath(tree, "ADD", (-200, -400))
    tree.links.new(gin.outputs["Wind Vector"], wind_total.inputs[0])
    tree.links.new(turbulence.outputs["Vector"], wind_total.inputs[1])
    wind_dt = _vmath(tree, "SCALE", (-20, -400))
    tree.links.new(wind_total.outputs["Vector"], wind_dt.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], wind_dt.inputs["Scale"])
    velocity_final = _vmath(tree, "ADD", (160, -240))
    tree.links.new(velocity_drag.outputs["Vector"], velocity_final.inputs[0])
    tree.links.new(wind_dt.outputs["Vector"], velocity_final.inputs[1])
    v_final_gate = tree.nodes.new("GeometryNodeSwitch")
    v_final_gate.input_type = "VECTOR"
    v_final_gate.location = (260, -300)
    tree.links.new(velocity_final.outputs["Vector"], v_final_gate.inputs["False"])
    tree.links.new(zero_sleep_vec.outputs["Vector"], v_final_gate.inputs["True"])

    step_dx = _vmath(tree, "SCALE", (340, -200))
    tree.links.new(velocity_final.outputs["Vector"], step_dx.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], step_dx.inputs["Scale"])
    p_int = _vmath(tree, "ADD", (520, -160))
    tree.links.new(pos_in.outputs["Position"], p_int.inputs[0])
    tree.links.new(step_dx.outputs["Vector"], p_int.inputs[1])

    # Geometry-domain stage for adjacent chain links and proxy sphere contacts.
    st_integrated_velocity = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_integrated_velocity.data_type = "FLOAT_VECTOR"
    st_integrated_velocity.domain = "POINT"
    st_integrated_velocity.inputs["Name"].default_value = "pnw_velocity"
    st_integrated_velocity.location = (-100, 260)
    tree.links.new(captured_velocity.outputs["Geometry"], st_integrated_velocity.inputs["Geometry"])
    tree.links.new(v_final_gate.outputs["Output"], st_integrated_velocity.inputs["Value"])
    set_pred = tree.nodes.new("GeometryNodeSetPosition")
    set_pred.location = (40, 120)
    tree.links.new(st_integrated_velocity.outputs["Geometry"], set_pred.inputs["Geometry"])
    p_gate = tree.nodes.new("GeometryNodeSwitch")
    p_gate.input_type = "VECTOR"
    p_gate.location = (260, -160)
    tree.links.new(p_int.outputs["Vector"], p_gate.inputs["False"])
    tree.links.new(pos_in.outputs["Position"], p_gate.inputs["True"])
    tree.links.new(p_gate.outputs["Output"], set_pred.inputs["Position"])

    n_chain = tree.nodes.new("GeometryNodeGroup")
    n_chain.node_tree = sg_chain
    n_chain.location = (240, 120)
    tree.links.new(set_pred.outputs["Geometry"], n_chain.inputs["Geometry"])
    tree.links.new(gin.outputs["Anchor"], n_chain.inputs["Anchor"])
    tree.links.new(gin.outputs["Rest Length"], n_chain.inputs["Rest Length"])
    tree.links.new(gin.outputs["Stiffness"], n_chain.inputs["Stiffness"])

    # Clamp the supported bottom sphere before contacts so stacks have a base.
    chain_pos = tree.nodes.new("GeometryNodeInputPosition")
    chain_pos.location = (240, -40)
    n_ground_clamp = tree.nodes.new("GeometryNodeGroup")
    n_ground_clamp.node_tree = sg_ground_clamp
    n_ground_clamp.location = (440, 120)
    tree.links.new(chain_pos.outputs["Position"], n_ground_clamp.inputs["Position"])
    tree.links.new(gin.outputs["Ground Z"], n_ground_clamp.inputs["Ground Z"])
    tree.links.new(gin.outputs["Radius"], n_ground_clamp.inputs["Radius"])
    set_pre = tree.nodes.new("GeometryNodeSetPosition")
    set_pre.location = (640, 120)
    tree.links.new(n_chain.outputs["Geometry"], set_pre.inputs["Geometry"])
    tree.links.new(n_ground_clamp.outputs["Position"], set_pre.inputs["Position"])

    # Pairwise sphere proxy contact (F07): every point is a sphere of Radius.
    n_contact = tree.nodes.new("GeometryNodeGroup")
    n_contact.node_tree = sg_contact
    n_contact.location = (840, 120)
    tree.links.new(set_pre.outputs["Geometry"], n_contact.inputs["Geometry"])
    tree.links.new(gin.outputs["Radius"], n_contact.inputs["Radius"])
    tree.links.new(gin.outputs["Restitution"], n_contact.inputs["Restitution"])
    tree.links.new(gin.outputs["Friction"], n_contact.inputs["Friction"])
    tree.links.new(gin.outputs["Sleep Velocity"], n_contact.inputs["Sleep Velocity"])
    tree.links.new(gin.outputs["Ground Z"], n_contact.inputs["Ground Z"])
    tree.links.new(gin.outputs["Contact Iterations"], n_contact.inputs["Contact Iterations"])

    tree.links.new(collider_switch.outputs["Output"], n_contact.inputs["Collider Geometry"])

    vel_after_contact = tree.nodes.new("GeometryNodeInputNamedAttribute")
    vel_after_contact.data_type = "FLOAT_VECTOR"
    vel_after_contact.inputs["Name"].default_value = "pnw_velocity"
    vel_after_contact.location = (1260, -260)

    # Persistent low-speed sleep state. Sleep is per point, requires current
    # support, and is disabled while chain/wind is active. The velocity is
    # zeroed before rotation so a sleeping point cannot keep accumulating spin.
    support_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    support_attr.data_type = "BOOLEAN"
    support_attr.inputs["Name"].default_value = "pnw_sleep_root_valid"
    support_attr.location = (1260, -400)
    contact_support_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    contact_support_attr.data_type = "BOOLEAN"
    contact_support_attr.inputs["Name"].default_value = "pnw_contact_support"
    contact_support_attr.location = (1260, -470)
    support_any_raw = _bool_node("OR", (1440, -470))
    tree.links.new(support_attr.outputs["Attribute"], support_any_raw.inputs[0])
    tree.links.new(contact_support_attr.outputs["Attribute"], support_any_raw.inputs[1])
    support_any = _bool_node("AND", (1620, -470))
    tree.links.new(support_any_raw.outputs["Boolean"], support_any.inputs[0])
    tree.links.new(gravity_down.outputs["Boolean"], support_any.inputs[1])
    frames_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    frames_attr.data_type = "FLOAT"
    frames_attr.inputs["Name"].default_value = "pnw_sleep_frames"
    frames_attr.location = (1260, -540)
    speed = _vmath(tree, "LENGTH", (1440, -300))
    tree.links.new(vel_after_contact.outputs["Attribute"], speed.inputs[0])
    low_speed = _math(tree, "LESS_THAN", (1620, -300))
    tree.links.new(speed.outputs["Value"], low_speed.inputs[0])
    tree.links.new(gin.outputs["Sleep Velocity"], low_speed.inputs[1])
    support_low = _bool_node("AND", (1800, -360))
    tree.links.new(support_any.outputs["Boolean"], support_low.inputs[0])
    tree.links.new(low_speed.outputs["Value"], support_low.inputs[1])
    wind_len = _vmath(tree, "LENGTH", (1440, -620))
    tree.links.new(gin.outputs["Wind Vector"], wind_len.inputs[0])
    wind_calm = _math(tree, "LESS_THAN", (1620, -620))
    wind_calm.inputs[1].default_value = 0.000001
    tree.links.new(wind_len.outputs["Value"], wind_calm.inputs[0])
    turb_calm = _math(tree, "LESS_THAN", (1620, -700))
    turb_calm.inputs[1].default_value = 0.000001
    tree.links.new(gin.outputs["Wind Turbulence"], turb_calm.inputs[0])
    wind_ok = _bool_node("AND", (1800, -660))
    tree.links.new(wind_calm.outputs["Value"], wind_ok.inputs[0])
    tree.links.new(turb_calm.outputs["Value"], wind_ok.inputs[1])
    chain_len = _math(tree, "GREATER_THAN", (1440, -820))
    chain_len.inputs[1].default_value = 0.0
    tree.links.new(gin.outputs["Rest Length"], chain_len.inputs[0])
    chain_stiff = _math(tree, "GREATER_THAN", (1440, -900))
    chain_stiff.inputs[1].default_value = 0.0
    tree.links.new(gin.outputs["Stiffness"], chain_stiff.inputs[0])
    chain_active = _bool_node("OR", (1620, -860))
    tree.links.new(chain_len.outputs["Value"], chain_active.inputs[0])
    tree.links.new(chain_stiff.outputs["Value"], chain_active.inputs[1])
    chain_ok = _bool_node("NOT", (1800, -860))
    tree.links.new(chain_active.outputs["Boolean"], chain_ok.inputs[0])
    sleep_enabled = _bool_node("AND", (1980, -660))
    tree.links.new(wind_ok.outputs["Boolean"], sleep_enabled.inputs[0])
    tree.links.new(chain_ok.outputs["Boolean"], sleep_enabled.inputs[1])
    sleep_enabled_stable = _bool_node("AND", (2160, -660))
    tree.links.new(sleep_enabled.outputs["Boolean"], sleep_enabled_stable.inputs[0])
    tree.links.new(support_stable.outputs["Boolean"], sleep_enabled_stable.inputs[1])

    # The integration gate is re-validated every substep.  It uses the freshly
    # captured static support, the previous contact-support state, the current
    # wind/chain inputs, and the current speed.  If any of these changed, the
    # old sleep flag must not suppress gravity or the contact solve.
    input_speed = _vmath(tree, "LENGTH", (1440, -2040))
    tree.links.new(captured_velocity.outputs["Velocity"], input_speed.inputs[0])
    input_slow = _math(tree, "LESS_THAN", (1620, -2040))
    tree.links.new(input_speed.outputs["Value"], input_slow.inputs[0])
    tree.links.new(gin.outputs["Sleep Velocity"], input_slow.inputs[1])
    hold_support_raw = _bool_node("OR", (1440, -2160))
    tree.links.new(support_attr.outputs["Attribute"], hold_support_raw.inputs[0])
    tree.links.new(contact_support_attr.outputs["Attribute"], hold_support_raw.inputs[1])
    hold_support = _bool_node("AND", (1620, -2160))
    tree.links.new(hold_support_raw.outputs["Boolean"], hold_support.inputs[0])
    tree.links.new(gravity_down.outputs["Boolean"], hold_support.inputs[1])
    sleep_hold = _bool_node("AND", (1620, -2160))
    tree.links.new(sleep_state_in.outputs["Attribute"], sleep_hold.inputs[0])
    tree.links.new(hold_support.outputs["Boolean"], sleep_hold.inputs[1])
    sleep_hold_wind = _bool_node("AND", (1800, -2160))
    tree.links.new(sleep_hold.outputs["Boolean"], sleep_hold_wind.inputs[0])
    tree.links.new(wind_ok.outputs["Boolean"], sleep_hold_wind.inputs[1])
    sleep_hold_chain = _bool_node("AND", (1980, -2160))
    tree.links.new(sleep_hold_wind.outputs["Boolean"], sleep_hold_chain.inputs[0])
    tree.links.new(chain_ok.outputs["Boolean"], sleep_hold_chain.inputs[1])
    sleep_hold_stable = _bool_node("AND", (2160, -2160))
    tree.links.new(sleep_hold_chain.outputs["Boolean"], sleep_hold_stable.inputs[0])
    tree.links.new(support_stable.outputs["Boolean"], sleep_hold_stable.inputs[1])
    sleep_hold_slow = _bool_node("AND", (2340, -2160))
    tree.links.new(sleep_hold_stable.outputs["Boolean"], sleep_hold_slow.inputs[0])
    tree.links.new(input_slow.outputs["Value"], sleep_hold_slow.inputs[1])
    tree.links.new(sleep_hold_slow.outputs["Boolean"], v_start.inputs["Switch"])
    tree.links.new(sleep_hold_slow.outputs["Boolean"], v_final_gate.inputs["Switch"])
    tree.links.new(sleep_hold_slow.outputs["Boolean"], p_gate.inputs["Switch"])

    contact_converged_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    contact_converged_attr.data_type = "BOOLEAN"
    contact_converged_attr.inputs["Name"].default_value = "pnw_contact_converged"
    contact_converged_attr.location = (1800, -980)
    sleep_enabled_contact = _bool_node("AND", (2160, -660))
    tree.links.new(sleep_enabled_stable.outputs["Boolean"], sleep_enabled_contact.inputs[0])
    tree.links.new(contact_converged_attr.outputs["Attribute"], sleep_enabled_contact.inputs[1])
    current_pos = tree.nodes.new("GeometryNodeInputPosition")
    current_pos.location = (1980, -800)
    start_pos_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    start_pos_attr.data_type = "FLOAT_VECTOR"
    start_pos_attr.inputs["Name"].default_value = "pnw_substep_start_pos"
    start_pos_attr.location = (1440, -1200)
    correction_vec = _vmath(tree, "SUBTRACT", (1620, -1200))
    tree.links.new(current_pos.outputs["Position"], correction_vec.inputs[0])
    tree.links.new(start_pos_attr.outputs["Attribute"], correction_vec.inputs[1])
    correction = _vmath(tree, "LENGTH", (1800, -1200))
    tree.links.new(correction_vec.outputs["Vector"], correction.inputs[0])
    correction_eps = _math(tree, "MULTIPLY", (1620, -1300))
    tree.links.new(gin.outputs["Radius"], correction_eps.inputs[0])
    correction_eps.inputs[1].default_value = sphere_contacts.SUPPORT_GAP_RATIO
    correction_small = _math(tree, "LESS_THAN", (1800, -1300))
    tree.links.new(correction.outputs["Value"], correction_small.inputs[0])
    tree.links.new(correction_eps.outputs["Value"], correction_small.inputs[1])
    low = _bool_node("AND", (1980, -360))
    tree.links.new(support_low.outputs["Boolean"], low.inputs[0])
    tree.links.new(sleep_enabled_contact.outputs["Boolean"], low.inputs[1])
    low_corrected = _bool_node("AND", (2160, -360))
    tree.links.new(low.outputs["Boolean"], low_corrected.inputs[0])
    tree.links.new(correction_small.outputs["Value"], low_corrected.inputs[1])
    frames_plus = _math(tree, "ADD", (1440, -540))
    tree.links.new(frames_attr.outputs["Attribute"], frames_plus.inputs[0])
    frames_plus.inputs[1].default_value = 1.0
    frames_next = _math(tree, "MULTIPLY", (1620, -540))
    tree.links.new(frames_plus.outputs["Value"], frames_next.inputs[0])
    tree.links.new(low_corrected.outputs["Boolean"], frames_next.inputs[1])
    sleep_new = _math(tree, "GREATER_THAN", (1800, -540))
    sleep_new.inputs[1].default_value = 2.5
    tree.links.new(frames_next.outputs["Value"], sleep_new.inputs[0])
    sleep_rooted_next = _bool_node("AND", (1980, -620))
    tree.links.new(sleep_new.outputs["Value"], sleep_rooted_next.inputs[0])
    tree.links.new(support_any.outputs["Boolean"], sleep_rooted_next.inputs[1])
    set_sleep_pos = tree.nodes.new("GeometryNodeSetPosition")
    set_sleep_pos.location = (1980, 160)
    tree.links.new(n_contact.outputs["Geometry"], set_sleep_pos.inputs["Geometry"])
    # Never restore an old sleep pose over the current contact solution.  The
    # contact solve already produced the valid pose; storing it is enough.
    tree.links.new(current_pos.outputs["Position"], set_sleep_pos.inputs["Position"])
    zero_velocity = tree.nodes.new("ShaderNodeCombineXYZ")
    zero_velocity.location = (1980, -540)
    sleep_velocity = tree.nodes.new("GeometryNodeSwitch")
    sleep_velocity.input_type = "VECTOR"
    sleep_velocity.location = (2160, -360)
    tree.links.new(sleep_new.outputs["Value"], sleep_velocity.inputs["Switch"])
    tree.links.new(vel_after_contact.outputs["Attribute"], sleep_velocity.inputs["False"])
    tree.links.new(zero_velocity.outputs["Vector"], sleep_velocity.inputs["True"])
    store_frames = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store_frames.data_type = "FLOAT"
    store_frames.domain = "POINT"
    store_frames.inputs["Name"].default_value = "pnw_sleep_frames"
    store_frames.location = (1980, 40)
    tree.links.new(set_sleep_pos.outputs["Geometry"], store_frames.inputs["Geometry"])
    tree.links.new(frames_next.outputs["Value"], store_frames.inputs["Value"])
    store_sleep = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store_sleep.data_type = "BOOLEAN"
    store_sleep.domain = "POINT"
    store_sleep.inputs["Name"].default_value = "pnw_sleep"
    store_sleep.location = (2160, 40)
    tree.links.new(store_frames.outputs["Geometry"], store_sleep.inputs["Geometry"])
    tree.links.new(sleep_new.outputs["Value"], store_sleep.inputs["Value"])
    store_sleep_pos = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store_sleep_pos.data_type = "FLOAT_VECTOR"
    store_sleep_pos.domain = "POINT"
    store_sleep_pos.inputs["Name"].default_value = "pnw_sleep_pos"
    store_sleep_pos.location = (2340, 120)
    tree.links.new(store_sleep.outputs["Geometry"], store_sleep_pos.inputs["Geometry"])
    # The stored pose is always the current post-contact pose; no old pose is
    # ever restored over a new contact solution.
    tree.links.new(current_pos.outputs["Position"], store_sleep_pos.inputs["Value"])
    store_sleep_velocity = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store_sleep_velocity.data_type = "FLOAT_VECTOR"
    store_sleep_velocity.domain = "POINT"
    store_sleep_velocity.inputs["Name"].default_value = "pnw_velocity"
    store_sleep_velocity.location = (2340, 40)
    tree.links.new(store_sleep_pos.outputs["Geometry"], store_sleep_velocity.inputs["Geometry"])
    tree.links.new(sleep_velocity.outputs["Output"], store_sleep_velocity.inputs["Value"])

    # Update angular rotation vector proportional to linear velocity (rolling spin) when above sleep speed
    sep_v = tree.nodes.new("ShaderNodeSeparateXYZ")
    sep_v.location = (480, -480)
    tree.links.new(vel_after_contact.outputs["Attribute"], sep_v.inputs["Vector"])

    comb_omega = tree.nodes.new("ShaderNodeCombineXYZ")
    comb_omega.location = (660, -480)
    # Roll around Y from vx and X from -vy, plus z spin
    neg_vy = _math(tree, "MULTIPLY", (560, -560))
    neg_vy.inputs[1].default_value = -1.0
    tree.links.new(sep_v.outputs["Y"], neg_vy.inputs[0])
    tree.links.new(neg_vy.outputs["Value"], comb_omega.inputs["X"])
    tree.links.new(sep_v.outputs["X"], comb_omega.inputs["Y"])
    tree.links.new(sep_v.outputs["Z"], comb_omega.inputs["Z"])

    spin_dt = _math(tree, "MULTIPLY", (660, -640))
    tree.links.new(gin.outputs["Angular Spin"], spin_dt.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], spin_dt.inputs[1])

    d_rot = _vmath(tree, "SCALE", (820, -480))
    tree.links.new(comb_omega.outputs["Vector"], d_rot.inputs[0])
    tree.links.new(spin_dt.outputs["Value"], d_rot.inputs["Scale"])
    rot_gate = tree.nodes.new("GeometryNodeSwitch")
    rot_gate.input_type = "VECTOR"
    rot_gate.location = (900, -560)
    tree.links.new(sleep_new.outputs["Value"], rot_gate.inputs["Switch"])
    tree.links.new(d_rot.outputs["Vector"], rot_gate.inputs["False"])
    tree.links.new(zero_sleep_vec.outputs["Vector"], rot_gate.inputs["True"])

    rot_next = _vmath(tree, "ADD", (960, -440))
    tree.links.new(rot_in.outputs["Attribute"], rot_next.inputs[0])
    tree.links.new(rot_gate.outputs["Output"], rot_next.inputs[1])

    st_rot = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_rot.data_type = "FLOAT_VECTOR"
    st_rot.domain = "POINT"
    st_rot.inputs["Name"].default_value = "pnw_rotation"
    st_rot.location = (1080, 40)
    tree.links.new(store_sleep_velocity.outputs["Geometry"], st_rot.inputs["Geometry"])
    tree.links.new(rot_next.outputs["Vector"], st_rot.inputs["Value"])

    # Persist the support environment and the sampled correction for the next
    # substep's moving-support and sleep-entry gates.  These are point-domain
    # attributes so the solver keeps a single geometry contract.
    st_collider = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_collider.data_type = "FLOAT_VECTOR"
    st_collider.domain = "POINT"
    st_collider.inputs["Name"].default_value = "pnw_collider_center"
    st_collider.location = (1200, 40)
    tree.links.new(st_rot.outputs["Geometry"], st_collider.inputs["Geometry"])
    tree.links.new(bbox_center.outputs["Vector"], st_collider.inputs["Value"])
    st_ground = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_ground.data_type = "FLOAT"
    st_ground.domain = "POINT"
    st_ground.inputs["Name"].default_value = "pnw_last_ground_z"
    st_ground.location = (1200, 160)
    tree.links.new(st_collider.outputs["Geometry"], st_ground.inputs["Geometry"])
    tree.links.new(gin.outputs["Ground Z"], st_ground.inputs["Value"])
    st_correction = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_correction.data_type = "FLOAT"
    st_correction.domain = "POINT"
    st_correction.inputs["Name"].default_value = "pnw_sleep_correction"
    st_correction.location = (1200, 280)
    tree.links.new(st_ground.outputs["Geometry"], st_correction.inputs["Geometry"])
    tree.links.new(correction.outputs["Value"], st_correction.inputs["Value"])
    st_moved_count = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_moved_count.data_type = "FLOAT"
    st_moved_count.domain = "POINT"
    st_moved_count.inputs["Name"].default_value = "pnw_support_moved_frames"
    st_moved_count.location = (1200, 400)
    tree.links.new(st_correction.outputs["Geometry"], st_moved_count.inputs["Geometry"])
    tree.links.new(moved_count.outputs["Output"], st_moved_count.inputs["Value"])
    st_sleep_rooted = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_sleep_rooted.data_type = "BOOLEAN"
    st_sleep_rooted.domain = "POINT"
    st_sleep_rooted.inputs["Name"].default_value = "pnw_sleep_rooted"
    st_sleep_rooted.location = (1200, 520)
    tree.links.new(st_moved_count.outputs["Geometry"], st_sleep_rooted.inputs["Geometry"])
    tree.links.new(sleep_rooted_next.outputs["Boolean"], st_sleep_rooted.inputs["Value"])
    st_root_count = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_root_count.data_type = "FLOAT"
    st_root_count.domain = "POINT"
    st_root_count.inputs["Name"].default_value = "pnw_sleep_root_count"
    st_root_count.location = (1200, 640)
    tree.links.new(st_sleep_rooted.outputs["Geometry"], st_root_count.inputs["Geometry"])
    tree.links.new(root_count.outputs["Point Count"], st_root_count.inputs["Value"])

    tree.links.new(st_root_count.outputs["Geometry"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)


def ensure_solver_cloth_rope_step(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Cloth & rope XPBD substep using Blender 5.2's bundled dynamics assets.

    Edge-length constraints are solved by the native XPBD Solver node. The
    outer core Simulation/Repeat zones provide substeps, as documented by
    Blender for external substep loops. Mesh/ground collision and the final
    explicit pin clamp remain in this addon so the existing socket contract and
    one-collider design stay unchanged.
    """
    sg_wind = ensure_force_wind_turbulence(force_rebuild)
    sg_mesh = ensure_collide_mesh_surface_geo(force_rebuild)
    sg_ground = ensure_collide_ground_plane_geo(force_rebuild)
    builtin = _load_builtin_dynamics(
        [
            "XPBD Solver",
            "Evaluate Effector Attributes",
            "Evaluate Forces",
            "Gravity",
            "Edge Length Constraint",
            "Store Edge Length",
            "Damping",
            "Cross Edge Length Constraint",
            "Tearing Effector",
            "Evaluate Custom Effectors",
        ]
    )
    required = (
        "XPBD Solver",
        "Evaluate Effector Attributes",
        "Evaluate Forces",
        "Gravity",
        "Edge Length Constraint",
        "Store Edge Length",
        "Damping",
        "Cross Edge Length Constraint",
        "Tearing Effector",
        "Evaluate Custom Effectors",
    )
    missing = [name for name in required if name not in builtin]
    if missing:
        raise RuntimeError(
            "Blender 5.2 bundled dynamics assets are unavailable; cloth/rope "
            f"XPBD edge constraints cannot be built. Missing: {missing}"
        )
    edge_length_dependency = next(
        (
            node.node_tree
            for node in builtin["Store Edge Length"].nodes
            if node.bl_idname == "GeometryNodeGroup"
            and node.node_tree is not None
            and "Edge Length" in node.node_tree.name
        ),
        None,
    )
    if edge_length_dependency is None:
        raise RuntimeError("Bundled 'Store Edge Length' asset has no Edge Length dependency")

    tree, need_build = _prepare_subgroup("PN_Solver_ClothRopeStep", "solver", force_rebuild)
    if not need_build:
        has_elastic_socket = any(
            item.item_type == "SOCKET" and item.name == "Elastic Shell"
            for item in tree.interface.items_tree
        )
        if not has_elastic_socket:
            # The 0.8 template predates the internal softbody edge branch.
            # Rebuild a new revision while keeping the old authored group.
            tree, need_build = _prepare_subgroup(
                "PN_Solver_ClothRopeStep", "solver", force_rebuild=True
            )
    if not need_build:
        return tree

    add_socket(tree, "Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Delta Time", "INPUT", "NodeSocketFloat", 0.008)
    add_socket(tree, "Time", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Gravity", "INPUT", "NodeSocketVector", (0.0, 0.0, -9.81))
    add_socket(tree, "Drag", "INPUT", "NodeSocketFloat", 0.12)
    add_socket(tree, "Wind Vector", "INPUT", "NodeSocketVector", (4.0, 1.0, 0.2))
    add_socket(tree, "Wind Turbulence", "INPUT", "NodeSocketFloat", 1.8)
    add_socket(tree, "Stiffness", "INPUT", "NodeSocketFloat", 0.72)
    add_socket(tree, "Elastic Shell", "INPUT", "NodeSocketBool", False)
    add_socket(tree, "Collider", "INPUT", "NodeSocketObject")
    add_socket(tree, "Contact Thickness", "INPUT", "NodeSocketFloat", 0.04)
    add_socket(tree, "Ground Z", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.1)
    add_socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.4)
    add_socket(tree, "Sleep Velocity", "INPUT", "NodeSocketFloat", 0.01)
    add_socket(tree, "Constraint Iterations", "INPUT", "NodeSocketInt", 12)
    add_socket(tree, "Rope Resample Count", "INPUT", "NodeSocketInt", 12)
    add_socket(tree, "Collider Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-1500, 0)
    gout.location = (1700, 0)

    pin_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    pin_attr.data_type = "FLOAT"
    pin_attr.inputs["Name"].default_value = "pnw_pinned"
    pin_attr.location = (-1300, -420)
    pin_exists = tree.nodes.new("GeometryNodeInputNamedAttribute")
    pin_exists.data_type = "FLOAT"
    pin_exists.inputs["Name"].default_value = "pnw_pinned"
    pin_exists.location = (-1300, -560)
    # One shared pin predicate is used for native mass, wind gating and the
    # pre/post collision safety clamps.
    pin_selected = _math(tree, "GREATER_THAN", (-1300, -840))
    pin_selected.inputs[1].default_value = 0.5
    tree.links.new(pin_attr.outputs["Attribute"], pin_selected.inputs[0])
    zero_velocity = tree.nodes.new("ShaderNodeCombineXYZ")
    zero_velocity.location = (-1300, -980)
    rest_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    rest_attr.data_type = "FLOAT_VECTOR"
    rest_attr.inputs["Name"].default_value = "pnw_rest_pos"
    rest_attr.location = (-1300, -700)

    # Optional resampling for a raw 2-point POLY rope (core converts curves to
    # an edge mesh without resampling). Interior pins are intentionally cleared.
    mesh_count = tree.nodes.new("GeometryNodeAttributeDomainSize")
    mesh_count.component = "MESH"
    mesh_count.location = (-1300, 240)
    tree.links.new(gin.outputs["Geometry"], mesh_count.inputs["Geometry"])
    is_two = _math(tree, "COMPARE", (-1120, 240))
    tree.links.new(mesh_count.outputs["Point Count"], is_two.inputs[0])
    is_two.inputs[1].default_value = 2.0
    is_two.inputs[2].default_value = 0.00001

    mesh_to_curve = tree.nodes.new("GeometryNodeMeshToCurve")
    mesh_to_curve.location = (-1300, 80)
    tree.links.new(gin.outputs["Geometry"], mesh_to_curve.inputs["Mesh"])
    resample = tree.nodes.new("GeometryNodeResampleCurve")
    resample.location = (-1120, 80)
    resample.inputs["Mode"].default_value = "Count"
    tree.links.new(mesh_to_curve.outputs["Curve"], resample.inputs["Curve"])
    tree.links.new(gin.outputs["Rope Resample Count"], resample.inputs["Count"])
    curve_to_mesh = tree.nodes.new("GeometryNodeCurveToMesh")
    curve_to_mesh.location = (-940, 80)
    tree.links.new(resample.outputs["Curve"], curve_to_mesh.inputs["Curve"])

    pin0 = tree.nodes.new("GeometryNodeSampleIndex")
    pin0.data_type = "FLOAT"
    pin0.domain = "POINT"
    pin0.location = (-1120, -420)
    tree.links.new(gin.outputs["Geometry"], pin0.inputs["Geometry"])
    tree.links.new(pin_attr.outputs["Attribute"], pin0.inputs["Value"])
    pin0.inputs["Index"].default_value = 0
    pin1 = tree.nodes.new("GeometryNodeSampleIndex")
    pin1.data_type = "FLOAT"
    pin1.domain = "POINT"
    pin1.location = (-940, -420)
    tree.links.new(gin.outputs["Geometry"], pin1.inputs["Geometry"])
    tree.links.new(pin_attr.outputs["Attribute"], pin1.inputs["Value"])
    pin1.inputs["Index"].default_value = 1

    resampled_count = tree.nodes.new("GeometryNodeAttributeDomainSize")
    resampled_count.component = "MESH"
    resampled_count.location = (-760, 240)
    tree.links.new(curve_to_mesh.outputs["Mesh"], resampled_count.inputs["Geometry"])
    last_index = _math(tree, "SUBTRACT", (-580, 240))
    tree.links.new(resampled_count.outputs["Point Count"], last_index.inputs[0])
    last_index.inputs[1].default_value = 1.0
    current_index = tree.nodes.new("GeometryNodeInputIndex")
    current_index.location = (-760, -160)
    first = _math(tree, "COMPARE", (-580, -160))
    tree.links.new(current_index.outputs["Index"], first.inputs[0])
    first.inputs[1].default_value = 0.0
    first.inputs[2].default_value = 0.00001
    last = _math(tree, "COMPARE", (-580, -300))
    tree.links.new(current_index.outputs["Index"], last.inputs[0])
    tree.links.new(last_index.outputs["Value"], last.inputs[1])
    last.inputs[2].default_value = 0.00001
    pin_value_a = tree.nodes.new("GeometryNodeSwitch")
    pin_value_a.input_type = "FLOAT"
    pin_value_a.location = (-380, -240)
    tree.links.new(first.outputs["Value"], pin_value_a.inputs["Switch"])
    pin_value_a.inputs["False"].default_value = 0.0
    tree.links.new(pin0.outputs["Value"], pin_value_a.inputs["True"])
    pin_value_b = tree.nodes.new("GeometryNodeSwitch")
    pin_value_b.input_type = "FLOAT"
    pin_value_b.location = (-200, -240)
    tree.links.new(last.outputs["Value"], pin_value_b.inputs["Switch"])
    tree.links.new(pin_value_a.outputs["Output"], pin_value_b.inputs["False"])
    tree.links.new(pin1.outputs["Value"], pin_value_b.inputs["True"])
    store_pin = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store_pin.data_type = "FLOAT"
    store_pin.domain = "POINT"
    store_pin.inputs["Name"].default_value = "pnw_pinned"
    store_pin.location = (0, -160)
    tree.links.new(curve_to_mesh.outputs["Mesh"], store_pin.inputs["Geometry"])
    tree.links.new(pin_value_b.outputs["Output"], store_pin.inputs["Value"])
    resample_switch = tree.nodes.new("GeometryNodeSwitch")
    resample_switch.input_type = "GEOMETRY"
    resample_switch.location = (180, 80)
    tree.links.new(is_two.outputs["Value"], resample_switch.inputs["Switch"])
    tree.links.new(gin.outputs["Geometry"], resample_switch.inputs["False"])
    tree.links.new(store_pin.outputs["Geometry"], resample_switch.inputs["True"])

    # Store rest_length once, on edges, before the first XPBD step.
    rest_len_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    rest_len_attr.data_type = "FLOAT"
    rest_len_attr.inputs["Name"].default_value = "rest_length"
    rest_len_attr.location = (180, -280)
    rest_len_missing = _math(tree, "SUBTRACT", (380, -280))
    rest_len_missing.inputs[0].default_value = 1.0
    tree.links.new(rest_len_attr.outputs["Exists"], rest_len_missing.inputs[1])
    edge_length_group = tree.nodes.new("GeometryNodeGroup")
    edge_length_group.node_tree = edge_length_dependency
    edge_length_group.location = (380, -120)
    store_rest_len = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store_rest_len.data_type = "FLOAT"
    store_rest_len.domain = "EDGE"
    store_rest_len.inputs["Name"].default_value = "rest_length"
    store_rest_len.location = (580, 80)
    tree.links.new(resample_switch.outputs["Output"], store_rest_len.inputs["Geometry"])
    tree.links.new(edge_length_group.outputs["Length"], store_rest_len.inputs["Value"])
    tree.links.new(rest_len_missing.outputs["Value"], store_rest_len.inputs["Selection"])

    # Store mass once.  A pinned point becomes a native infinite-mass point
    # (mass=0) for the XPBD solve, while authored non-pin mass values are kept
    # verbatim.  Missing mass still defaults to 1.0 for free points.
    mass_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    mass_attr.data_type = "FLOAT"
    mass_attr.inputs["Name"].default_value = "mass"
    mass_attr.location = (580, -280)
    existing_mass = tree.nodes.new("GeometryNodeSwitch")
    existing_mass.input_type = "FLOAT"
    existing_mass.location = (760, -280)
    tree.links.new(mass_attr.outputs["Exists"], existing_mass.inputs["Switch"])
    existing_mass.inputs["False"].default_value = 1.0
    tree.links.new(mass_attr.outputs["Attribute"], existing_mass.inputs["True"])
    mass_for_solver = tree.nodes.new("GeometryNodeSwitch")
    mass_for_solver.input_type = "FLOAT"
    mass_for_solver.location = (940, -280)
    tree.links.new(pin_selected.outputs["Value"], mass_for_solver.inputs["Switch"])
    mass_for_solver.inputs["True"].default_value = 0.0
    tree.links.new(existing_mass.outputs["Output"], mass_for_solver.inputs["False"])
    store_mass = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store_mass.data_type = "FLOAT"
    store_mass.domain = "POINT"
    store_mass.inputs["Name"].default_value = "mass"
    store_mass.location = (940, 80)
    store_mass.inputs["Selection"].default_value = True
    tree.links.new(store_rest_len.outputs["Geometry"], store_mass.inputs["Geometry"])
    tree.links.new(mass_for_solver.outputs["Output"], store_mass.inputs["Value"])

    pnw_velocity = tree.nodes.new("GeometryNodeInputNamedAttribute")
    pnw_velocity.data_type = "FLOAT_VECTOR"
    pnw_velocity.inputs["Name"].default_value = "pnw_velocity"
    pnw_velocity.location = (760, -420)
    position_field = tree.nodes.new("GeometryNodeInputPosition")
    position_field.location = (760, -560)
    n_wind = tree.nodes.new("GeometryNodeGroup")
    n_wind.node_tree = sg_wind
    n_wind.location = (940, -420)
    tree.links.new(position_field.outputs["Position"], n_wind.inputs["Position"])
    tree.links.new(gin.outputs["Wind Vector"], n_wind.inputs["Wind Vector"])
    tree.links.new(gin.outputs["Wind Turbulence"], n_wind.inputs["Turbulence"])
    tree.links.new(gin.outputs["Time"], n_wind.inputs["Time"])
    tree.links.new(gin.outputs["Delta Time"], n_wind.inputs["Delta Time"])
    wind_velocity = _vmath(tree, "ADD", (1120, -420))
    tree.links.new(pnw_velocity.outputs["Attribute"], wind_velocity.inputs[0])
    tree.links.new(n_wind.outputs["Velocity"], wind_velocity.inputs[1])
    # Pinned points must never receive a wind impulse.  The native mass=0 pin
    # is the primary fix; this explicit gate also keeps the input velocity
    # clean for the collision path.
    velocity_for_solver = tree.nodes.new("GeometryNodeSwitch")
    velocity_for_solver.input_type = "VECTOR"
    velocity_for_solver.location = (1300, -420)
    tree.links.new(pin_selected.outputs["Value"], velocity_for_solver.inputs["Switch"])
    tree.links.new(zero_velocity.outputs["Vector"], velocity_for_solver.inputs["True"])
    tree.links.new(wind_velocity.outputs["Vector"], velocity_for_solver.inputs["False"])
    store_velocity = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store_velocity.data_type = "FLOAT_VECTOR"
    store_velocity.domain = "POINT"
    store_velocity.inputs["Name"].default_value = "velocity"
    store_velocity.location = (1300, 80)
    tree.links.new(store_mass.outputs["Geometry"], store_velocity.inputs["Geometry"])
    tree.links.new(velocity_for_solver.outputs["Output"], store_velocity.inputs["Value"])

    # Native XPBD world: one flat cloth sub-bundle with structural constraints.
    rest_position = tree.nodes.new("GeometryNodeInputNamedAttribute")
    rest_position.data_type = "FLOAT_VECTOR"
    rest_position.inputs["Name"].default_value = "pnw_rest_pos"
    rest_position.location = (1300, -420)
    rest_position_exists = tree.nodes.new("GeometryNodeInputNamedAttribute")
    rest_position_exists.data_type = "FLOAT_VECTOR"
    rest_position_exists.inputs["Name"].default_value = "pnw_rest_pos"
    rest_position_exists.location = (1300, -560)
    pin_position = tree.nodes.new("GeometryNodeSwitch")
    pin_position.input_type = "VECTOR"
    pin_position.location = (1480, -420)
    tree.links.new(rest_position_exists.outputs["Exists"], pin_position.inputs["Switch"])
    tree.links.new(position_field.outputs["Position"], pin_position.inputs["False"])
    tree.links.new(rest_position.outputs["Attribute"], pin_position.inputs["True"])

    one_minus_stiff = _math(tree, "SUBTRACT", (1300, -820))
    one_minus_stiff.inputs[0].default_value = 1.0
    tree.links.new(gin.outputs["Stiffness"], one_minus_stiff.inputs[1])
    legacy_softness = _math(tree, "MULTIPLY", (1480, -900))
    tree.links.new(one_minus_stiff.outputs["Value"], legacy_softness.inputs[0])
    legacy_softness.inputs[1].default_value = 0.0001
    # Keep the frozen cloth/rope mapping above.  Softbody shells opt into
    # native Softness = 1 - Stiffness, which the bundled asset converts to
    # compliance with its own Softness-to-Compliance curve.
    elastic_softness = tree.nodes.new("GeometryNodeSwitch")
    elastic_softness.input_type = "FLOAT"
    elastic_softness.label = "Elastic Shell edge softness branch"
    elastic_softness.location = (1480, -760)
    tree.links.new(gin.outputs["Elastic Shell"], elastic_softness.inputs["Switch"])
    tree.links.new(legacy_softness.outputs["Value"], elastic_softness.inputs["False"])
    tree.links.new(one_minus_stiff.outputs["Value"], elastic_softness.inputs["True"])
    edge_constraint = tree.nodes.new("GeometryNodeGroup")
    edge_constraint.node_tree = builtin["Edge Length Constraint"]
    edge_constraint.location = (1660, -240)
    tree.links.new(elastic_softness.outputs["Output"], edge_constraint.inputs["Softness"])

    rest_len_stat = tree.nodes.new("GeometryNodeAttributeStatistic")
    rest_len_stat.data_type = "FLOAT"
    rest_len_stat.domain = "EDGE"
    rest_len_stat.location = (1660, -500)
    tree.links.new(store_rest_len.outputs["Geometry"], rest_len_stat.inputs["Geometry"])
    rest_len_field = tree.nodes.new("GeometryNodeInputNamedAttribute")
    rest_len_field.data_type = "FLOAT"
    rest_len_field.inputs["Name"].default_value = "rest_length"
    rest_len_field.location = (1480, -640)
    tree.links.new(rest_len_field.outputs["Attribute"], rest_len_stat.inputs["Attribute"])
    edge_tolerance = _math(tree, "MULTIPLY", (1840, -500))
    tree.links.new(rest_len_stat.outputs["Min"], edge_tolerance.inputs[0])
    edge_tolerance.inputs[1].default_value = 0.001
    tree.links.new(edge_tolerance.outputs["Value"], edge_constraint.inputs["Error Threshold"])

    gravity_group = tree.nodes.new("GeometryNodeGroup")
    gravity_group.node_tree = builtin["Gravity"]
    gravity_group.location = (1480, 80)
    tree.links.new(gin.outputs["Gravity"], gravity_group.inputs["Gravity"])
    damping_group = tree.nodes.new("GeometryNodeGroup")
    damping_group.node_tree = builtin["Damping"]
    damping_group.location = (1480, -80)
    tree.links.new(gin.outputs["Drag"], damping_group.inputs["Linear"])
    tree.links.new(gin.outputs["Drag"], damping_group.inputs["Angular"])

    cloth_bundle = tree.nodes.new("NodeCombineBundle")
    for socket_type, name in (
        ("GEOMETRY", "Geometry"),
        ("BUNDLE", "Gravity"),
        ("BUNDLE", "Edge Length"),
        ("BUNDLE", "Damping"),
    ):
        cloth_bundle.bundle_items.new(socket_type, name)
    cloth_bundle.location = (1660, 80)
    tree.links.new(store_velocity.outputs["Geometry"], cloth_bundle.inputs["Geometry"])
    tree.links.new(gravity_group.outputs["Gravity"], cloth_bundle.inputs["Gravity"])
    tree.links.new(edge_constraint.outputs["Constraint"], cloth_bundle.inputs["Edge Length"])
    tree.links.new(damping_group.outputs["Damping"], cloth_bundle.inputs["Damping"])

    world = tree.nodes.new("NodeCombineBundle")
    world.bundle_items.new("BUNDLE", "Cloth")
    world.location = (1880, 80)
    tree.links.new(cloth_bundle.outputs["Bundle"], world.inputs["Cloth"])

    effector_attrs = tree.nodes.new("GeometryNodeGroup")
    effector_attrs.node_tree = builtin["Evaluate Effector Attributes"]
    effector_attrs.location = (2080, 80)
    tree.links.new(world.outputs["Bundle"], effector_attrs.inputs["World"])
    forces = tree.nodes.new("GeometryNodeGroup")
    forces.node_tree = builtin["Evaluate Forces"]
    forces.inputs["Name"].default_value = "external_force"
    forces.location = (2280, 80)
    tree.links.new(effector_attrs.outputs["World"], forces.inputs["World"])
    solver = tree.nodes.new("GeometryNodeGroup")
    solver.node_tree = builtin["XPBD Solver"]
    solver.location = (2480, 80)
    tree.links.new(forces.outputs["World"], solver.inputs["World"])
    tree.links.new(gin.outputs["Delta Time"], solver.inputs["Delta Time"])
    solver.inputs["Substeps"].default_value = 1
    tree.links.new(gin.outputs["Constraint Iterations"], solver.inputs["Constraint Iterations"])
    get_geometry = tree.nodes.new("NodeGetBundleItem")
    get_geometry.socket_type = "GEOMETRY"
    get_geometry.inputs["Path"].default_value = "Cloth/Geometry"
    get_geometry.location = (2680, 80)
    tree.links.new(solver.outputs["World"], get_geometry.inputs["Bundle"])

    from .deformation import extend_cloth_step
    extend_cloth_step(tree, gin, cloth_bundle, solver, get_geometry,
                      rest_position.outputs["Attribute"], builtin, add_socket)

    solved_velocity = tree.nodes.new("GeometryNodeInputNamedAttribute")
    solved_velocity.data_type = "FLOAT_VECTOR"
    solved_velocity.inputs["Name"].default_value = "velocity"
    solved_velocity.location = (2480, -120)
    store_solved_velocity = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store_solved_velocity.data_type = "FLOAT_VECTOR"
    store_solved_velocity.domain = "POINT"
    store_solved_velocity.inputs["Name"].default_value = "pnw_velocity"
    store_solved_velocity.location = (2680, 80)
    tree.links.new(get_geometry.outputs["Item"], store_solved_velocity.inputs["Geometry"])
    tree.links.new(solved_velocity.outputs["Attribute"], store_solved_velocity.inputs["Value"])

    # Pre-collision pin protection.  The native mass=0 pin should already hold
    # these points, but collision/ground are separate node groups after the
    # solver; restore position and zero velocity before they run so they can
    # never consume or inject a pinned-point impulse.
    pre_pin_position = tree.nodes.new("GeometryNodeSwitch")
    pre_pin_position.input_type = "VECTOR"
    pre_pin_position.location = (2680, -160)
    tree.links.new(pin_selected.outputs["Value"], pre_pin_position.inputs["Switch"])
    tree.links.new(pin_position.outputs["Output"], pre_pin_position.inputs["True"])
    tree.links.new(position_field.outputs["Position"], pre_pin_position.inputs["False"])
    pre_pin_set = tree.nodes.new("GeometryNodeSetPosition")
    pre_pin_set.location = (2680, 120)
    tree.links.new(store_solved_velocity.outputs["Geometry"], pre_pin_set.inputs["Geometry"])
    tree.links.new(pre_pin_position.outputs["Output"], pre_pin_set.inputs["Position"])
    pre_pin_velocity_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    pre_pin_velocity_attr.data_type = "FLOAT_VECTOR"
    pre_pin_velocity_attr.inputs["Name"].default_value = "pnw_velocity"
    pre_pin_velocity_attr.location = (2680, -320)
    pre_pin_velocity = tree.nodes.new("GeometryNodeSwitch")
    pre_pin_velocity.input_type = "VECTOR"
    pre_pin_velocity.location = (2860, -320)
    tree.links.new(pin_selected.outputs["Value"], pre_pin_velocity.inputs["Switch"])
    tree.links.new(zero_velocity.outputs["Vector"], pre_pin_velocity.inputs["True"])
    tree.links.new(pre_pin_velocity_attr.outputs["Attribute"], pre_pin_velocity.inputs["False"])
    pre_pin_store = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    pre_pin_store.data_type = "FLOAT_VECTOR"
    pre_pin_store.domain = "POINT"
    pre_pin_store.inputs["Name"].default_value = "pnw_velocity"
    pre_pin_store.location = (2680, 220)
    tree.links.new(pre_pin_set.outputs["Geometry"], pre_pin_store.inputs["Geometry"])
    tree.links.new(pre_pin_velocity.outputs["Output"], pre_pin_store.inputs["Value"])

    n_mesh = tree.nodes.new("GeometryNodeGroup")
    n_mesh.node_tree = sg_mesh
    n_mesh.location = (2880, 80)
    tree.links.new(pre_pin_store.outputs["Geometry"], n_mesh.inputs["Geometry"])
    tree.links.new(gin.outputs["Collider"], n_mesh.inputs["Collider"])
    tree.links.new(gin.outputs["Collider Geometry"], n_mesh.inputs["Collider Geometry"])
    tree.links.new(gin.outputs["Contact Thickness"], n_mesh.inputs["Contact Thickness"])
    tree.links.new(gin.outputs["Restitution"], n_mesh.inputs["Restitution"])
    tree.links.new(gin.outputs["Friction"], n_mesh.inputs["Friction"])

    n_ground = tree.nodes.new("GeometryNodeGroup")
    n_ground.node_tree = sg_ground
    n_ground.location = (3080, 80)
    tree.links.new(n_mesh.outputs["Geometry"], n_ground.inputs["Geometry"])
    tree.links.new(gin.outputs["Ground Z"], n_ground.inputs["Ground Z"])
    tree.links.new(gin.outputs["Contact Thickness"], n_ground.inputs["Radius"])
    tree.links.new(gin.outputs["Restitution"], n_ground.inputs["Restitution"])
    tree.links.new(gin.outputs["Friction"], n_ground.inputs["Friction"])
    tree.links.new(gin.outputs["Sleep Velocity"], n_ground.inputs["Sleep Velocity"])

    # Final explicit pin clamp; pinned points always resolve to their rest pose.
    current_pos = tree.nodes.new("GeometryNodeInputPosition")
    current_pos.location = (3080, -160)
    current_vel = tree.nodes.new("GeometryNodeInputNamedAttribute")
    current_vel.data_type = "FLOAT_VECTOR"
    current_vel.inputs["Name"].default_value = "pnw_velocity"
    current_vel.location = (3080, -320)
    pin_pos_sw = tree.nodes.new("GeometryNodeSwitch")
    pin_pos_sw.input_type = "VECTOR"
    pin_pos_sw.location = (3260, -160)
    tree.links.new(pin_selected.outputs["Value"], pin_pos_sw.inputs["Switch"])
    tree.links.new(current_pos.outputs["Position"], pin_pos_sw.inputs["False"])
    tree.links.new(pin_position.outputs["Output"], pin_pos_sw.inputs["True"])
    pin_vel_sw = tree.nodes.new("GeometryNodeSwitch")
    pin_vel_sw.input_type = "VECTOR"
    pin_vel_sw.location = (3260, -320)
    tree.links.new(pin_selected.outputs["Value"], pin_vel_sw.inputs["Switch"])
    tree.links.new(current_vel.outputs["Attribute"], pin_vel_sw.inputs["False"])
    tree.links.new(zero_velocity.outputs["Vector"], pin_vel_sw.inputs["True"])

    set_pin = tree.nodes.new("GeometryNodeSetPosition")
    set_pin.location = (3460, 80)
    tree.links.new(n_ground.outputs["Geometry"], set_pin.inputs["Geometry"])
    tree.links.new(pin_pos_sw.outputs["Output"], set_pin.inputs["Position"])

    store_v = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    store_v.data_type = "FLOAT_VECTOR"
    store_v.domain = "POINT"
    store_v.inputs["Name"].default_value = "pnw_velocity"
    store_v.location = (3660, 80)
    tree.links.new(set_pin.outputs["Geometry"], store_v.inputs["Geometry"])
    tree.links.new(pin_vel_sw.outputs["Output"], store_v.inputs["Value"])
    tree.links.new(store_v.outputs["Geometry"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)


def ensure_solver_fluid_particle_step(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Particle advection + mesh obstacle deflection + ground splash + age advancement per substep."""
    sg_force = ensure_force_gravity_drag(force_rebuild)
    sg_wind = ensure_force_wind_turbulence(force_rebuild)
    sg_mesh = ensure_collide_mesh_surface(force_rebuild)
    sg_ground = ensure_collide_ground_plane(force_rebuild)

    tree, need_build = _prepare_subgroup("PN_Solver_FluidParticleStep", "solver", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Delta Time", "INPUT", "NodeSocketFloat", 0.01)
    add_socket(tree, "Time", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Gravity", "INPUT", "NodeSocketVector", (0.0, 0.0, -9.81))
    add_socket(tree, "Drag", "INPUT", "NodeSocketFloat", 0.04)
    add_socket(tree, "Wind Vector", "INPUT", "NodeSocketVector", (0.3, 0.0, 0.0))
    add_socket(tree, "Wind Turbulence", "INPUT", "NodeSocketFloat", 0.8)
    add_socket(tree, "Collider", "INPUT", "NodeSocketObject")
    add_socket(tree, "Contact Thickness", "INPUT", "NodeSocketFloat", 0.05)
    add_socket(tree, "Ground Z", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.25)
    add_socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.15)
    add_socket(tree, "Collider Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-1160, 0)
    gout.location = (1180, 0)

    pos_in = tree.nodes.new("GeometryNodeInputPosition")
    pos_in.location = (-960, -180)
    vel_in = tree.nodes.new("GeometryNodeInputNamedAttribute")
    vel_in.data_type = "FLOAT_VECTOR"
    vel_in.inputs["Name"].default_value = "pnw_velocity"
    vel_in.location = (-960, -340)
    age_in = tree.nodes.new("GeometryNodeInputNamedAttribute")
    age_in.data_type = "FLOAT"
    age_in.inputs["Name"].default_value = "pnw_age"
    age_in.location = (-960, -500)

    n_force = tree.nodes.new("GeometryNodeGroup")
    n_force.node_tree = sg_force
    n_force.location = (-700, -180)
    tree.links.new(vel_in.outputs["Attribute"], n_force.inputs["Velocity"])
    tree.links.new(gin.outputs["Gravity"], n_force.inputs["Gravity"])
    tree.links.new(gin.outputs["Drag"], n_force.inputs["Drag"])
    n_force.inputs["Sleep Velocity"].default_value = 0.0
    tree.links.new(gin.outputs["Delta Time"], n_force.inputs["Delta Time"])

    n_wind = tree.nodes.new("GeometryNodeGroup")
    n_wind.node_tree = sg_wind
    n_wind.location = (-480, -180)
    tree.links.new(pos_in.outputs["Position"], n_wind.inputs["Position"])
    tree.links.new(n_force.outputs["Velocity"], n_wind.inputs["Velocity"])
    tree.links.new(gin.outputs["Wind Vector"], n_wind.inputs["Wind Vector"])
    tree.links.new(gin.outputs["Wind Turbulence"], n_wind.inputs["Turbulence"])
    tree.links.new(gin.outputs["Time"], n_wind.inputs["Time"])
    tree.links.new(gin.outputs["Delta Time"], n_wind.inputs["Delta Time"])

    step_dx = _vmath(tree, "SCALE", (-260, -180))
    tree.links.new(n_wind.outputs["Velocity"], step_dx.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], step_dx.inputs["Scale"])

    p_next = _vmath(tree, "ADD", (-80, -180))
    tree.links.new(pos_in.outputs["Position"], p_next.inputs[0])
    tree.links.new(step_dx.outputs["Vector"], p_next.inputs[1])

    n_mesh = tree.nodes.new("GeometryNodeGroup")
    n_mesh.node_tree = sg_mesh
    n_mesh.location = (120, -180)
    tree.links.new(p_next.outputs["Vector"], n_mesh.inputs["Position"])
    tree.links.new(n_wind.outputs["Velocity"], n_mesh.inputs["Velocity"])
    tree.links.new(gin.outputs["Collider"], n_mesh.inputs["Collider"])
    tree.links.new(gin.outputs["Collider Geometry"], n_mesh.inputs["Collider Geometry"])
    tree.links.new(gin.outputs["Contact Thickness"], n_mesh.inputs["Contact Thickness"])
    tree.links.new(gin.outputs["Restitution"], n_mesh.inputs["Restitution"])
    tree.links.new(gin.outputs["Friction"], n_mesh.inputs["Friction"])

    n_ground = tree.nodes.new("GeometryNodeGroup")
    n_ground.node_tree = sg_ground
    n_ground.location = (340, -180)
    tree.links.new(n_mesh.outputs["Position"], n_ground.inputs["Position"])
    tree.links.new(n_mesh.outputs["Velocity"], n_ground.inputs["Velocity"])
    tree.links.new(gin.outputs["Ground Z"], n_ground.inputs["Ground Z"])
    tree.links.new(gin.outputs["Contact Thickness"], n_ground.inputs["Radius"])
    tree.links.new(gin.outputs["Restitution"], n_ground.inputs["Restitution"])
    tree.links.new(gin.outputs["Friction"], n_ground.inputs["Friction"])
    n_ground.inputs["Sleep Velocity"].default_value = 0.0

    response = _capture_contact_response(tree, gin.outputs["Geometry"], n_ground)
    set_p = tree.nodes.new("GeometryNodeSetPosition")
    set_p.location = (560, 40)
    tree.links.new(response.outputs["Geometry"], set_p.inputs["Geometry"])
    tree.links.new(response.outputs["Position"], set_p.inputs["Position"])

    st_vel = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_vel.data_type = "FLOAT_VECTOR"
    st_vel.domain = "POINT"
    st_vel.inputs["Name"].default_value = "pnw_velocity"
    st_vel.location = (760, 40)
    tree.links.new(set_p.outputs["Geometry"], st_vel.inputs["Geometry"])
    tree.links.new(response.outputs["Velocity"], st_vel.inputs["Value"])

    age_add = _math(tree, "ADD", (560, -440))
    tree.links.new(age_in.outputs["Attribute"], age_add.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], age_add.inputs[1])

    st_age = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    st_age.data_type = "FLOAT"
    st_age.domain = "POINT"
    st_age.inputs["Name"].default_value = "pnw_age"
    st_age.location = (960, 40)
    tree.links.new(st_vel.outputs["Geometry"], st_age.inputs["Geometry"])
    tree.links.new(age_add.outputs["Value"], st_age.inputs["Value"])

    tree.links.new(st_age.outputs["Geometry"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)


def ensure_post_instance_rigid(force_rebuild: bool = False) -> bpy.types.NodeTree:
    tree, need_build = _prepare_subgroup("PN_Post_InstanceRigid", "post", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Points", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Instance Collection", "INPUT", "NodeSocketCollection")
    add_socket(tree, "Radius", "INPUT", "NodeSocketFloat", 0.12)
    add_socket(tree, "Normalize Instances", "INPUT", "NodeSocketBool", True)
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-1280, 0)
    gout.location = (1320, 0)

    # Historical path: reset children to their local geometry and scale every
    # prototype by 2r (unit-diameter convention). Kept bit-for-bit for
    # Normalize Instances = False.
    col_info = tree.nodes.new("GeometryNodeCollectionInfo")
    col_info.inputs["Separate Children"].default_value = True
    col_info.inputs["Reset Children"].default_value = True
    col_info.location = (-1060, -220)
    tree.links.new(gin.outputs["Instance Collection"], col_info.inputs["Collection"])

    legacy_sphere = tree.nodes.new("GeometryNodeMeshUVSphere")
    legacy_sphere.inputs["Segments"].default_value = 24
    legacy_sphere.inputs["Rings"].default_value = 12
    legacy_sphere.inputs["Radius"].default_value = 0.5
    legacy_sphere.location = (-1060, -420)

    legacy_fallback = tree.nodes.new("GeometryNodeGeometryToInstance")
    legacy_fallback.location = (-860, -420)
    tree.links.new(legacy_sphere.outputs["Mesh"], legacy_fallback.inputs["Geometry"])

    # Check collection size via DomainSize(INSTANCES) > 0 to pick CollectionInfo
    # vs. the fallback sphere. Both collection paths have the same count.
    dom = tree.nodes.new("GeometryNodeAttributeDomainSize")
    dom.component = "INSTANCES"
    dom.location = (-860, -300)
    tree.links.new(col_info.outputs["Instances"], dom.inputs["Geometry"])

    gt0 = _math(tree, "GREATER_THAN", (-680, -300))
    gt0.inputs[1].default_value = 0.0
    tree.links.new(dom.outputs["Instance Count"], gt0.inputs[0])

    sw_legacy = tree.nodes.new("GeometryNodeSwitch")
    sw_legacy.input_type = "GEOMETRY"
    sw_legacy.location = (-500, -220)
    tree.links.new(gt0.outputs["Value"], sw_legacy.inputs["Switch"])
    tree.links.new(legacy_fallback.outputs["Instances"], sw_legacy.inputs["False"])
    tree.links.new(col_info.outputs["Instances"], sw_legacy.inputs["True"])

    # Per-prototype normalization. Reset Children is disabled here so source
    # object rotation/scale are part of the measured world-space prototype; the
    # source datablocks stay untouched. For Each (INSTANCE) keeps every child
    # separate instead of collapsing the collection into one bounding box.
    norm_col = tree.nodes.new("GeometryNodeCollectionInfo")
    norm_col.inputs["Separate Children"].default_value = True
    norm_col.inputs["Reset Children"].default_value = False
    norm_col.location = (-1060, 240)
    tree.links.new(gin.outputs["Instance Collection"], norm_col.inputs["Collection"])

    foreach_in = tree.nodes.new("GeometryNodeForeachGeometryElementInput")
    foreach_out = tree.nodes.new("GeometryNodeForeachGeometryElementOutput")
    foreach_in.pair_with_output(foreach_out)
    foreach_out.domain = "INSTANCE"
    foreach_in.location = (-860, 240)
    foreach_out.location = (620, 240)
    tree.links.new(norm_col.outputs["Instances"], foreach_in.inputs["Geometry"])

    realize = tree.nodes.new("GeometryNodeRealizeInstances")
    realize.location = (-660, 140)
    tree.links.new(foreach_in.outputs["Element"], realize.inputs["Geometry"])

    bbox = tree.nodes.new("GeometryNodeBoundBox")
    bbox.location = (-480, 140)
    tree.links.new(realize.outputs["Geometry"], bbox.inputs["Geometry"])

    center_add = _vmath(tree, "ADD", (-300, 340))
    tree.links.new(bbox.outputs["Min"], center_add.inputs[0])
    tree.links.new(bbox.outputs["Max"], center_add.inputs[1])
    center = _vmath(tree, "SCALE", (-120, 340))
    tree.links.new(center_add.outputs["Vector"], center.inputs[0])
    center.inputs["Scale"].default_value = 0.5

    size = _vmath(tree, "SUBTRACT", (-300, 160))
    tree.links.new(bbox.outputs["Max"], size.inputs[0])
    tree.links.new(bbox.outputs["Min"], size.inputs[1])
    half_diagonal = _vmath(tree, "LENGTH", (-120, 160))
    tree.links.new(size.outputs["Vector"], half_diagonal.inputs[0])
    bbox_radius = _math(tree, "MULTIPLY", (60, 160))
    tree.links.new(half_diagonal.outputs["Value"], bbox_radius.inputs[0])
    bbox_radius.inputs[1].default_value = 0.5
    safe_radius = _math(tree, "MAXIMUM", (240, 160))
    safe_radius.inputs[1].default_value = 0.0001
    tree.links.new(bbox_radius.outputs["Value"], safe_radius.inputs[0])

    pos = tree.nodes.new("GeometryNodeInputPosition")
    pos.location = (-480, -80)
    offset = _vmath(tree, "SUBTRACT", (-300, -40))
    tree.links.new(pos.outputs["Position"], offset.inputs[0])
    tree.links.new(center.outputs["Vector"], offset.inputs[1])
    centered = tree.nodes.new("GeometryNodeSetPosition")
    centered.location = (60, 40)
    tree.links.new(realize.outputs["Geometry"], centered.inputs["Geometry"])
    tree.links.new(offset.outputs["Vector"], centered.inputs["Position"])

    inv_radius = _math(tree, "DIVIDE", (240, -40))
    inv_radius.inputs[0].default_value = 1.0
    tree.links.new(safe_radius.outputs["Value"], inv_radius.inputs[1])
    norm_scale = tree.nodes.new("ShaderNodeCombineXYZ")
    norm_scale.location = (420, -40)
    for axis in ("X", "Y", "Z"):
        tree.links.new(inv_radius.outputs["Value"], norm_scale.inputs[axis])
    normalize = tree.nodes.new("GeometryNodeTransform")
    normalize.inputs["Mode"].default_value = "Components"
    normalize.location = (600, 40)
    tree.links.new(centered.outputs["Geometry"], normalize.inputs["Geometry"])
    tree.links.new(norm_scale.outputs["Vector"], normalize.inputs["Scale"])

    normalized_instance = tree.nodes.new("GeometryNodeGeometryToInstance")
    normalized_instance.location = (780, 40)
    tree.links.new(normalize.outputs["Geometry"], normalized_instance.inputs["Geometry"])
    tree.links.new(normalized_instance.outputs["Instances"], foreach_out.inputs["Geometry"])
    generated = next(sock for sock in foreach_out.outputs if sock.identifier == "Generation_0")

    normalized_sphere = tree.nodes.new("GeometryNodeMeshUVSphere")
    normalized_sphere.inputs["Segments"].default_value = 24
    normalized_sphere.inputs["Rings"].default_value = 12
    normalized_sphere.inputs["Radius"].default_value = 1.0
    normalized_sphere.location = (-1060, 600)
    normalized_fallback = tree.nodes.new("GeometryNodeGeometryToInstance")
    normalized_fallback.location = (-860, 600)
    tree.links.new(normalized_sphere.outputs["Mesh"], normalized_fallback.inputs["Geometry"])

    sw_normalized = tree.nodes.new("GeometryNodeSwitch")
    sw_normalized.input_type = "GEOMETRY"
    sw_normalized.location = (-500, 600)
    tree.links.new(gt0.outputs["Value"], sw_normalized.inputs["Switch"])
    tree.links.new(normalized_fallback.outputs["Instances"], sw_normalized.inputs["False"])
    tree.links.new(generated, sw_normalized.inputs["True"])

    mode_switch = tree.nodes.new("GeometryNodeSwitch")
    mode_switch.input_type = "GEOMETRY"
    mode_switch.location = (920, -220)
    tree.links.new(gin.outputs["Normalize Instances"], mode_switch.inputs["Switch"])
    tree.links.new(sw_legacy.outputs["Output"], mode_switch.inputs["False"])
    tree.links.new(sw_normalized.outputs["Output"], mode_switch.inputs["True"])

    legacy_scale = _math(tree, "MULTIPLY", (920, -440))
    legacy_scale.inputs[1].default_value = 2.0
    tree.links.new(gin.outputs["Radius"], legacy_scale.inputs[0])
    scale_switch = tree.nodes.new("GeometryNodeSwitch")
    scale_switch.input_type = "FLOAT"
    scale_switch.location = (1100, -360)
    tree.links.new(gin.outputs["Normalize Instances"], scale_switch.inputs["Switch"])
    tree.links.new(legacy_scale.outputs["Value"], scale_switch.inputs["False"])
    tree.links.new(gin.outputs["Radius"], scale_switch.inputs["True"])
    comb_s = tree.nodes.new("ShaderNodeCombineXYZ")
    comb_s.location = (1280, -360)
    for axis in ("X", "Y", "Z"):
        tree.links.new(scale_switch.outputs["Output"], comb_s.inputs[axis])

    rot_attr = tree.nodes.new("GeometryNodeInputNamedAttribute")
    rot_attr.data_type = "FLOAT_VECTOR"
    rot_attr.inputs["Name"].default_value = "pnw_rotation"
    rot_attr.location = (920, 180)

    idx_in = tree.nodes.new("GeometryNodeInputIndex")
    idx_in.location = (920, 320)

    inst_pts = tree.nodes.new("GeometryNodeInstanceOnPoints")
    inst_pts.inputs["Pick Instance"].default_value = True
    inst_pts.location = (1120, 80)
    tree.links.new(gin.outputs["Points"], inst_pts.inputs["Points"])
    tree.links.new(mode_switch.outputs["Output"], inst_pts.inputs["Instance"])
    tree.links.new(idx_in.outputs["Index"], inst_pts.inputs["Instance Index"])
    tree.links.new(rot_attr.outputs["Attribute"], inst_pts.inputs["Rotation"])
    tree.links.new(comb_s.outputs["Vector"], inst_pts.inputs["Scale"])

    tree.links.new(inst_pts.outputs["Instances"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)


def ensure_post_fluid_sdf_mesh(force_rebuild: bool = False) -> bpy.types.NodeTree:
    tree, need_build = _prepare_subgroup("PN_Post_FluidSDFMesh", "post", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Points", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Particle Radius", "INPUT", "NodeSocketFloat", 0.16)
    add_socket(tree, "Voxel Size", "INPUT", "NodeSocketFloat", 0.08)
    add_socket(tree, "Mesh", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-460, 0)
    gout.location = (460, 0)

    p2sdf = tree.nodes.new("GeometryNodePointsToSDFGrid")
    p2sdf.location = (-140, 0)
    tree.links.new(gin.outputs["Points"], p2sdf.inputs["Points"])
    tree.links.new(gin.outputs["Particle Radius"], p2sdf.inputs["Radius"])
    tree.links.new(gin.outputs["Voxel Size"], p2sdf.inputs["Voxel Size"])

    g2m = tree.nodes.new("GeometryNodeGridToMesh")
    g2m.location = (120, 0)
    tree.links.new(p2sdf.outputs["SDF Grid"], g2m.inputs["Grid"])

    smooth = tree.nodes.new("GeometryNodeSetShadeSmooth")
    smooth.location = (280, 0)
    tree.links.new(g2m.outputs["Mesh"], smooth.inputs[0])
    tree.links.new(smooth.outputs[0], gout.inputs["Mesh"])
    return _finalize_subgroup(tree)


def ensure_post_smoke_volume(force_rebuild: bool = False) -> bpy.types.NodeTree:
    tree, need_build = _prepare_subgroup("PN_Post_SmokeVolume", "post", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Points", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Particle Radius", "INPUT", "NodeSocketFloat", 0.24)
    add_socket(tree, "Voxel Size", "INPUT", "NodeSocketFloat", 0.1)
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-480, 0)
    gout.location = (520, 0)

    p2v = tree.nodes.new("GeometryNodePointsToVolume")
    p2v.inputs["Resolution Mode"].default_value = "Size"
    p2v.location = (-160, 100)
    tree.links.new(gin.outputs["Points"], p2v.inputs["Points"])
    tree.links.new(gin.outputs["Particle Radius"], p2v.inputs["Radius"])
    tree.links.new(gin.outputs["Voxel Size"], p2v.inputs["Voxel Size"])

    # Also build preview mesh alongside volume so Workbench/wireframe & mesh exporters see smoke geometry
    v2m = tree.nodes.new("GeometryNodeVolumeToMesh")
    v2m.inputs["Resolution Mode"].default_value = "Grid"
    v2m.inputs["Threshold"].default_value = 0.08
    v2m.location = (80, -80)
    tree.links.new(p2v.outputs["Volume"], v2m.inputs["Volume"])

    j = tree.nodes.new("GeometryNodeJoinGeometry")
    j.location = (300, 20)
    tree.links.new(p2v.outputs["Volume"], j.inputs["Geometry"])
    tree.links.new(v2m.outputs["Mesh"], j.inputs["Geometry"])
    tree.links.new(j.outputs["Geometry"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)


def ensure_all_subgroups(force_rebuild: bool = False) -> dict[str, bpy.types.NodeTree]:
    return {
        "PN_Force_GravityDrag": ensure_force_gravity_drag(force_rebuild),
        "PN_Force_WindTurbulence": ensure_force_wind_turbulence(force_rebuild),
        "PN_Collide_GroundPlane": ensure_collide_ground_plane(force_rebuild),
        "PN_Collide_GroundClamp": ensure_clamp_ground_position(force_rebuild),
        "PN_Collide_MeshSurface": ensure_collide_mesh_surface(force_rebuild),
        "PN_Collide_MeshGeometry": ensure_collide_mesh_geometry(force_rebuild),
        "PN_Collide_MeshSurface_Geo": ensure_collide_mesh_surface_geo(force_rebuild),
        "PN_Collide_GroundPlane_Geo": ensure_collide_ground_plane_geo(force_rebuild),
        "PN_Collide_SphereProxies": ensure_collide_sphere_proxies(force_rebuild),
        "PN_Constraint_PinAnchor": ensure_constraint_pin_anchor(force_rebuild),
        "PN_Constraint_DistanceChain": ensure_constraint_distance_chain(force_rebuild),
        "PN_Constraint_ChainLinks": ensure_constraint_chain_links(force_rebuild),
        "PN_Solver_RigidScatterStep": ensure_solver_rigid_scatter_step(force_rebuild),
        "PN_Solver_ClothRopeStep": ensure_solver_cloth_rope_step(force_rebuild),
        "PN_Solver_FluidParticleStep": ensure_solver_fluid_particle_step(force_rebuild),
        "PN_Post_InstanceRigid": ensure_post_instance_rigid(force_rebuild),
        "PN_Post_FluidSDFMesh": ensure_post_fluid_sdf_mesh(force_rebuild),
        "PN_Post_SmokeVolume": ensure_post_smoke_volume(force_rebuild),
    }
