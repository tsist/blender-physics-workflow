"""Isolated planar box-on-floor tipping mechanism.

This module is deliberately separate from the historical rigid-sphere solver.
It implements one real contact point per box point for a fixed horizontal
ground plane, with persistent angular velocity and a normal/tangent impulse
response.  Box-box contact, arbitrary mesh collision and non-planar motion are
out of scope for this first mechanism revision.

Read-only design reference: PBD ``PositionBasedRigidBodyDynamics.cpp``
(upstream 10a70bc146a97873dc3c8fef372f5217e010542e).  The equations are
implemented independently in Geometry Nodes; no upstream source is copied.
"""

from __future__ import annotations

import math
from typing import Any

import bpy

from .subgroups import (
    _finalize_subgroup,
    _math,
    _prepare_subgroup,
    _vmath,
    add_socket,
)


PRESET_ID = "rigid_box_tipping"
SOLVER_GROUP = "PN_Solver_BoxFloorStep"
POST_GROUP = "PN_Post_InstanceBox"
HALF_EXTENTS_KEY = "box_half_extents"
INITIAL_TILT_KEY = "box_initial_tilt"

# Fixed mass and fixed ground normal are part of this isolated mechanism.
BOX_MASS = 1.0
GROUND_NORMAL = (0.0, 0.0, 1.0)


def _combine(tree: bpy.types.NodeTree, x, y, z, loc: tuple[float, float]) -> bpy.types.Node:
    node = tree.nodes.new("ShaderNodeCombineXYZ")
    node.location = loc
    for socket, value in (("X", x), ("Y", y), ("Z", z)):
        if value is None:
            continue
        if isinstance(value, (int, float)):
            node.inputs[socket].default_value = float(value)
        else:
            tree.links.new(value, node.inputs[socket])
    return node


def _separate(tree: bpy.types.NodeTree, vector, loc: tuple[float, float]) -> bpy.types.Node:
    node = tree.nodes.new("ShaderNodeSeparateXYZ")
    node.location = loc
    tree.links.new(vector, node.inputs["Vector"])
    return node


def _store(
    tree: bpy.types.NodeTree,
    geometry,
    name: str,
    data_type: str,
    loc: tuple[float, float],
    *,
    value=None,
    default: Any = None,
) -> bpy.types.Node:
    node = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    node.data_type = data_type
    node.domain = "POINT"
    node.inputs["Name"].default_value = name
    node.location = loc
    tree.links.new(geometry, node.inputs["Geometry"])
    if value is not None:
        tree.links.new(value, node.inputs["Value"])
    elif default is not None:
        node.inputs["Value"].default_value = default
    return node


def _named_vector(tree: bpy.types.NodeTree, name: str, loc: tuple[float, float]) -> bpy.types.Node:
    node = tree.nodes.new("GeometryNodeInputNamedAttribute")
    node.data_type = "FLOAT_VECTOR"
    node.inputs["Name"].default_value = name
    node.location = loc
    return node


def _capture_state(tree: bpy.types.NodeTree, geometry) -> tuple[bpy.types.Node, dict[str, Any]]:
    """Capture the complete solver state before any store can overwrite it."""

    cap = tree.nodes.new("GeometryNodeCaptureAttribute")
    cap.domain = "POINT"
    cap.location = (-1420, 0)
    tree.links.new(geometry, cap.inputs["Geometry"])

    position = tree.nodes.new("GeometryNodeInputPosition")
    position.location = (-1600, 120)
    velocity = _named_vector(tree, "pnw_velocity", (-1600, -40))
    omega = _named_vector(tree, "pnw_omega", (-1600, -200))
    rotation = _named_vector(tree, "pnw_rotation", (-1600, -360))

    items = (
        ("P", position.outputs["Position"]),
        ("V", velocity.outputs["Attribute"]),
        ("W", omega.outputs["Attribute"]),
        ("R", rotation.outputs["Attribute"]),
    )
    captured: dict[str, Any] = {}
    for index, (name, field) in enumerate(items):
        cap.capture_items.new("VECTOR", name)
        tree.links.new(field, cap.inputs[name])
        captured[name] = cap.outputs[name]
    return cap, captured


def ensure_box_floor_step(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Build the independent box-floor impulse solver.

    One point is one box centre of mass.  The support point is the box centre
    plus the world offset of the deepest corner.  A normal impulse is applied
    only while that support point is below the ground and the contact point is
    approaching.  The angular velocity update is driven only by that impulse;
    linear velocity is never converted into spin.
    """

    tree, need_build = _prepare_subgroup(SOLVER_GROUP, "solver", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Geometry", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Delta Time", "INPUT", "NodeSocketFloat", 0.005208333333333333)
    add_socket(tree, "Gravity", "INPUT", "NodeSocketVector", (0.0, 0.0, -9.81))
    add_socket(tree, "Ground Z", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Box Half Extents", "INPUT", "NodeSocketVector", (0.2, 0.3, 1.0))
    add_socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.0)
    add_socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.45)
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-1780, 0)
    gout.location = (1900, 0)

    cap, state = _capture_state(tree, gin.outputs["Geometry"])
    position = state["P"]
    velocity = state["V"]
    omega = state["W"]
    rotation = state["R"]

    # Semi-implicit translational integration and explicit planar orientation
    # integration.  Only the Y component of omega is generated below.
    gravity_dt = _vmath(tree, "SCALE", (-1240, 220))
    tree.links.new(gin.outputs["Gravity"], gravity_dt.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], gravity_dt.inputs["Scale"])

    velocity_1 = _vmath(tree, "ADD", (-1060, 220))
    tree.links.new(velocity, velocity_1.inputs[0])
    tree.links.new(gravity_dt.outputs["Vector"], velocity_1.inputs[1])

    omega_dt = _vmath(tree, "SCALE", (-1240, 40))
    tree.links.new(omega, omega_dt.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], omega_dt.inputs["Scale"])

    rotation_1 = _vmath(tree, "ADD", (-1060, 40))
    tree.links.new(rotation, rotation_1.inputs[0])
    tree.links.new(omega_dt.outputs["Vector"], rotation_1.inputs[1])

    step = _vmath(tree, "SCALE", (-1240, -160))
    tree.links.new(velocity_1.outputs["Vector"], step.inputs[0])
    tree.links.new(gin.outputs["Delta Time"], step.inputs["Scale"])

    position_1 = _vmath(tree, "ADD", (-1060, -160))
    tree.links.new(position, position_1.inputs[0])
    tree.links.new(step.outputs["Vector"], position_1.inputs[1])

    # R = Euler(0, theta, 0), with Blender's real EulerToRotation node.  The
    # support direction is -sign(R^-1 n) * h, component-wise; SIGN(0) == 0 so a
    # perfectly flat box selects the bottom face centre instead of an arbitrary
    # corner.
    rot_1 = tree.nodes.new("FunctionNodeEulerToRotation")
    rot_1.location = (-880, 40)
    tree.links.new(rotation_1.outputs["Vector"], rot_1.inputs["Euler"])

    rot_inv = tree.nodes.new("FunctionNodeInvertRotation")
    rot_inv.location = (-700, 40)
    tree.links.new(rot_1.outputs["Rotation"], rot_inv.inputs["Rotation"])

    normal = _combine(tree, *GROUND_NORMAL, (-880, -100))
    normal_local = tree.nodes.new("FunctionNodeRotateVector")
    normal_local.location = (-700, -100)
    tree.links.new(normal.outputs["Vector"], normal_local.inputs["Vector"])
    tree.links.new(rot_inv.outputs["Rotation"], normal_local.inputs["Rotation"])

    n_loc = _separate(tree, normal_local.outputs["Vector"], (-520, -100))
    sign_x = _math(tree, "SIGN", (-340, -20))
    sign_y = _math(tree, "SIGN", (-340, -100))
    sign_z = _math(tree, "SIGN", (-340, -180))
    tree.links.new(n_loc.outputs["X"], sign_x.inputs[0])
    tree.links.new(n_loc.outputs["Y"], sign_y.inputs[0])
    tree.links.new(n_loc.outputs["Z"], sign_z.inputs[0])

    sign_vec = _combine(tree, sign_x.outputs["Value"], sign_y.outputs["Value"], sign_z.outputs["Value"], (-160, -100))
    signed_half = _vmath(tree, "MULTIPLY", (20, -100))
    tree.links.new(sign_vec.outputs["Vector"], signed_half.inputs[0])
    tree.links.new(gin.outputs["Box Half Extents"], signed_half.inputs[1])
    r_local = _vmath(tree, "SCALE", (200, -100))
    tree.links.new(signed_half.outputs["Vector"], r_local.inputs[0])
    r_local.inputs["Scale"].default_value = -1.0

    r_world = tree.nodes.new("FunctionNodeRotateVector")
    r_world.location = (380, -100)
    tree.links.new(r_local.outputs["Vector"], r_world.inputs["Vector"])
    tree.links.new(rot_1.outputs["Rotation"], r_world.inputs["Rotation"])
    r = r_world.outputs["Vector"]
    r_xyz = _separate(tree, r, (560, -100))
    r_x = r_xyz.outputs["X"]
    r_z = r_xyz.outputs["Z"]

    # Predicted contact point and signed penetration.
    contact_point = _vmath(tree, "ADD", (560, -280))
    tree.links.new(position_1.outputs["Vector"], contact_point.inputs[0])
    tree.links.new(r, contact_point.inputs[1])
    contact_sep = _separate(tree, contact_point.outputs["Vector"], (740, -280))
    depth = _math(tree, "SUBTRACT", (920, -280))
    tree.links.new(contact_sep.outputs["Z"], depth.inputs[0])
    tree.links.new(gin.outputs["Ground Z"], depth.inputs[1])
    contact = _math(tree, "LESS_THAN", (1100, -280))
    contact.inputs[1].default_value = 0.0
    tree.links.new(depth.outputs["Value"], contact.inputs[0])

    # Contact-point velocity u = v + omega x r.
    omega_x_r = _vmath(tree, "CROSS_PRODUCT", (380, -460))
    tree.links.new(omega, omega_x_r.inputs[0])
    tree.links.new(r, omega_x_r.inputs[1])
    u = _vmath(tree, "ADD", (560, -460))
    tree.links.new(velocity_1.outputs["Vector"], u.inputs[0])
    tree.links.new(omega_x_r.outputs["Vector"], u.inputs[1])
    u_sep = _separate(tree, u.outputs["Vector"], (740, -460))

    approaching = _math(tree, "LESS_THAN", (920, -460))
    tree.links.new(u_sep.outputs["Z"], approaching.inputs[0])
    approaching.inputs[1].default_value = 0.0

    active = _math(tree, "MULTIPLY", (1100, -460))
    tree.links.new(contact.outputs["Value"], active.inputs[0])
    tree.links.new(approaching.outputs["Value"], active.inputs[1])

    # I_yy = m (hx^2 + hz^2) / 3.  This is exact for rotations about Y and is
    # constant as the box turns around that axis.
    h_sep = _separate(tree, gin.outputs["Box Half Extents"], (-520, 320))
    hx2 = _math(tree, "POWER", (-340, 320))
    tree.links.new(h_sep.outputs["X"], hx2.inputs[0])
    hx2.inputs[1].default_value = 2.0
    hz2 = _math(tree, "POWER", (-340, 240))
    tree.links.new(h_sep.outputs["Z"], hz2.inputs[0])
    hz2.inputs[1].default_value = 2.0
    inertia_sum = _math(tree, "ADD", (-160, 280))
    tree.links.new(hx2.outputs["Value"], inertia_sum.inputs[0])
    tree.links.new(hz2.outputs["Value"], inertia_sum.inputs[1])
    inertia = _math(tree, "DIVIDE", (20, 280))
    tree.links.new(inertia_sum.outputs["Value"], inertia.inputs[0])
    inertia.inputs[1].default_value = 3.0 / BOX_MASS
    inv_inertia = _math(tree, "DIVIDE", (200, 280))
    inv_inertia.inputs[0].default_value = 1.0
    tree.links.new(inertia.outputs["Value"], inv_inertia.inputs[1])

    rx2 = _math(tree, "POWER", (740, -640))
    tree.links.new(r_x, rx2.inputs[0])
    rx2.inputs[1].default_value = 2.0
    rx2_inv_i = _math(tree, "MULTIPLY", (920, -640))
    tree.links.new(rx2.outputs["Value"], rx2_inv_i.inputs[0])
    tree.links.new(inv_inertia.outputs["Value"], rx2_inv_i.inputs[1])
    denom = _math(tree, "ADD", (1100, -640))
    denom.inputs[0].default_value = 1.0 / BOX_MASS
    tree.links.new(rx2_inv_i.outputs["Value"], denom.inputs[1])

    one_plus_e = _math(tree, "ADD", (920, -760))
    tree.links.new(gin.outputs["Restitution"], one_plus_e.inputs[0])
    one_plus_e.inputs[1].default_value = 1.0
    negative_one_plus_e = _math(tree, "MULTIPLY", (1100, -760))
    tree.links.new(one_plus_e.outputs["Value"], negative_one_plus_e.inputs[0])
    negative_one_plus_e.inputs[1].default_value = -1.0
    numerator = _math(tree, "MULTIPLY", (1280, -760))
    tree.links.new(negative_one_plus_e.outputs["Value"], numerator.inputs[0])
    tree.links.new(u_sep.outputs["Z"], numerator.inputs[1])
    j_raw = _math(tree, "DIVIDE", (1460, -760))
    tree.links.new(numerator.outputs["Value"], j_raw.inputs[0])
    tree.links.new(denom.outputs["Value"], j_raw.inputs[1])

    j_contact = _math(tree, "MULTIPLY", (1640, -760))
    tree.links.new(j_raw.outputs["Value"], j_contact.inputs[0])
    tree.links.new(active.outputs["Value"], j_contact.inputs[1])
    j = j_contact.outputs["Value"]

    j_vec = _combine(tree, 0.0, 0.0, j, (1460, 120))
    velocity_2 = _vmath(tree, "ADD", (1640, 220))
    tree.links.new(velocity_1.outputs["Vector"], velocity_2.inputs[0])
    tree.links.new(j_vec.outputs["Vector"], velocity_2.inputs[1])

    # Angular impulse from the normal impulse: I^-1 (r x j n).  For n=+Z this
    # is (0, -r_x j / I, 0).
    rx_j = _math(tree, "MULTIPLY", (1280, 40))
    tree.links.new(r_x, rx_j.inputs[0])
    tree.links.new(j, rx_j.inputs[1])
    omega_y_normal = _math(tree, "MULTIPLY", (1460, 40))
    tree.links.new(rx_j.outputs["Value"], omega_y_normal.inputs[0])
    tree.links.new(inv_inertia.outputs["Value"], omega_y_normal.inputs[1])
    omega_y_normal_neg = _math(tree, "MULTIPLY", (1640, 40))
    tree.links.new(omega_y_normal.outputs["Value"], omega_y_normal_neg.inputs[0])
    omega_y_normal_neg.inputs[1].default_value = -1.0
    omega_sep = _separate(tree, omega, (-340, 600))
    omega_y_2 = _math(tree, "ADD", (1820, 120))
    tree.links.new(omega_sep.outputs["Y"], omega_y_2.inputs[0])
    tree.links.new(omega_y_normal_neg.outputs["Value"], omega_y_2.inputs[1])
    omega_2 = _combine(tree, omega_sep.outputs["X"], omega_y_2.outputs["Value"], omega_sep.outputs["Z"], (2000, 120))

    # Tangential (Coulomb) impulse, using the post-normal contact velocity.
    omega2_x_r = _vmath(tree, "CROSS_PRODUCT", (1640, -460))
    tree.links.new(omega_2.outputs["Vector"], omega2_x_r.inputs[0])
    tree.links.new(r, omega2_x_r.inputs[1])
    u2 = _vmath(tree, "ADD", (1820, -460))
    tree.links.new(velocity_2.outputs["Vector"], u2.inputs[0])
    tree.links.new(omega2_x_r.outputs["Vector"], u2.inputs[1])
    u2_sep = _separate(tree, u2.outputs["Vector"], (2000, -460))

    rz2 = _math(tree, "POWER", (740, -860))
    tree.links.new(r_z, rz2.inputs[0])
    rz2.inputs[1].default_value = 2.0
    rz2_inv_i = _math(tree, "MULTIPLY", (920, -860))
    tree.links.new(rz2.outputs["Value"], rz2_inv_i.inputs[0])
    tree.links.new(inv_inertia.outputs["Value"], rz2_inv_i.inputs[1])
    denom_t = _math(tree, "ADD", (1100, -860))
    denom_t.inputs[0].default_value = 1.0 / BOX_MASS
    tree.links.new(rz2_inv_i.outputs["Value"], denom_t.inputs[1])
    jt_raw = _math(tree, "DIVIDE", (1280, -860))
    # jt_raw = -u_x / denom_t
    jt_neg = _math(tree, "MULTIPLY", (1280, -940))
    tree.links.new(u2_sep.outputs["X"], jt_neg.inputs[0])
    jt_neg.inputs[1].default_value = -1.0
    tree.links.new(jt_neg.outputs["Value"], jt_raw.inputs[0])
    tree.links.new(denom_t.outputs["Value"], jt_raw.inputs[1])

    friction_limit = _math(tree, "MULTIPLY", (1460, -940))
    tree.links.new(gin.outputs["Friction"], friction_limit.inputs[0])
    tree.links.new(j, friction_limit.inputs[1])
    upper = _math(tree, "MINIMUM", (1640, -940))
    tree.links.new(jt_raw.outputs["Value"], upper.inputs[0])
    tree.links.new(friction_limit.outputs["Value"], upper.inputs[1])
    negative_limit = _math(tree, "MULTIPLY", (1640, -1020))
    tree.links.new(friction_limit.outputs["Value"], negative_limit.inputs[0])
    negative_limit.inputs[1].default_value = -1.0
    jt = _math(tree, "MAXIMUM", (1820, -940))
    tree.links.new(upper.outputs["Value"], jt.inputs[0])
    tree.links.new(negative_limit.outputs["Value"], jt.inputs[1])

    jt_vec = _combine(tree, jt.outputs["Value"], 0.0, 0.0, (1640, -1160))
    velocity_3 = _vmath(tree, "ADD", (1820, -1160))
    tree.links.new(velocity_2.outputs["Vector"], velocity_3.inputs[0])
    tree.links.new(jt_vec.outputs["Vector"], velocity_3.inputs[1])

    rz_jt = _math(tree, "MULTIPLY", (1640, -1280))
    tree.links.new(r_z, rz_jt.inputs[0])
    tree.links.new(jt.outputs["Value"], rz_jt.inputs[1])
    omega_y_tangent = _math(tree, "MULTIPLY", (1820, -1280))
    tree.links.new(rz_jt.outputs["Value"], omega_y_tangent.inputs[0])
    tree.links.new(inv_inertia.outputs["Value"], omega_y_tangent.inputs[1])
    omega_y_3 = _math(tree, "ADD", (2000, -1280))
    tree.links.new(omega_y_2.outputs["Value"], omega_y_3.inputs[0])
    tree.links.new(omega_y_tangent.outputs["Value"], omega_y_3.inputs[1])
    omega_3 = _combine(
        tree,
        omega_sep.outputs["X"],
        omega_y_3.outputs["Value"],
        omega_sep.outputs["Z"],
        (2180, -1160),
    )

    # Penetration correction is a position projection, independent of the
    # velocity impulse.  It moves the centre so the support point rests exactly
    # on the plane and does not alter orientation or angular velocity.
    corrected_z = _math(tree, "SUBTRACT", (2000, -280))
    tree.links.new(gin.outputs["Ground Z"], corrected_z.inputs[0])
    tree.links.new(r_xyz.outputs["Z"], corrected_z.inputs[1])
    p_sep = _separate(tree, position_1.outputs["Vector"], (1820, -280))
    z_out = tree.nodes.new("GeometryNodeSwitch")
    z_out.input_type = "FLOAT"
    z_out.location = (2180, -280)
    tree.links.new(contact.outputs["Value"], z_out.inputs["Switch"])
    tree.links.new(p_sep.outputs["Z"], z_out.inputs["False"])
    tree.links.new(corrected_z.outputs["Value"], z_out.inputs["True"])
    position_out = _combine(tree, p_sep.outputs["X"], p_sep.outputs["Y"], z_out.outputs["Output"], (2360, -280))

    set_position = tree.nodes.new("GeometryNodeSetPosition")
    set_position.location = (1340, 420)
    tree.links.new(cap.outputs["Geometry"], set_position.inputs["Geometry"])
    tree.links.new(position_out.outputs["Vector"], set_position.inputs["Position"])

    stored_rotation = _store(
        tree, set_position.outputs["Geometry"], "pnw_rotation", "FLOAT_VECTOR", (1520, 420),
        value=rotation_1.outputs["Vector"],
    )
    stored_velocity = _store(
        tree, stored_rotation.outputs["Geometry"], "pnw_velocity", "FLOAT_VECTOR", (1700, 420),
        value=velocity_3.outputs["Vector"],
    )
    stored_omega = _store(
        tree, stored_velocity.outputs["Geometry"], "pnw_omega", "FLOAT_VECTOR", (1880, 420),
        value=omega_3.outputs["Vector"],
    )
    stored_j = _store(
        tree, stored_omega.outputs["Geometry"], "pnw_box_last_normal_impulse", "FLOAT", (2060, 420),
        value=j,
    )
    stored_jt = _store(
        tree, stored_j.outputs["Geometry"], "pnw_box_last_tangent_impulse", "FLOAT", (2240, 420),
        value=jt.outputs["Value"],
    )
    normal_angular_impulse = _math(tree, "MULTIPLY", (1640, -1500))
    tree.links.new(rx_j.outputs["Value"], normal_angular_impulse.inputs[0])
    normal_angular_impulse.inputs[1].default_value = -1.0
    angular_impulse = _math(tree, "ADD", (2000, -1500))
    tree.links.new(normal_angular_impulse.outputs["Value"], angular_impulse.inputs[0])
    tree.links.new(rz_jt.outputs["Value"], angular_impulse.inputs[1])
    stored_angular = _store(
        tree, stored_jt.outputs["Geometry"], "pnw_box_last_angular_impulse", "FLOAT", (2420, 420),
        value=angular_impulse.outputs["Value"],
    )
    stored_contact = _store(
        tree, stored_angular.outputs["Geometry"], "pnw_box_contact", "FLOAT", (2600, 420),
        value=contact.outputs["Value"],
    )
    stored_offset = _store(
        tree, stored_contact.outputs["Geometry"], "pnw_box_support_offset", "FLOAT_VECTOR", (2780, 420),
        value=r,
    )

    tree.links.new(stored_offset.outputs["Geometry"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)


def ensure_post_instance_box(force_rebuild: bool = False) -> bpy.types.NodeTree:
    """Instance a true-size cube (dimensions 2*half_extents) at each box point."""

    tree, need_build = _prepare_subgroup(POST_GROUP, "post", force_rebuild)
    if not need_build:
        return tree

    add_socket(tree, "Points", "INPUT", "NodeSocketGeometry")
    add_socket(tree, "Box Half Extents", "INPUT", "NodeSocketVector", (0.2, 0.3, 1.0))
    add_socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    gin = tree.nodes.new("NodeGroupInput")
    gout = tree.nodes.new("NodeGroupOutput")
    gin.location = (-620, 0)
    gout.location = (620, 0)

    size = _vmath(tree, "SCALE", (-400, 180))
    tree.links.new(gin.outputs["Box Half Extents"], size.inputs[0])
    size.inputs["Scale"].default_value = 2.0
    cube = tree.nodes.new("GeometryNodeMeshCube")
    cube.location = (-200, 180)
    tree.links.new(size.outputs["Vector"], cube.inputs["Size"])

    rot_attr = _named_vector(tree, "pnw_rotation", (-400, -120))
    rot = tree.nodes.new("FunctionNodeEulerToRotation")
    rot.location = (-200, -120)
    tree.links.new(rot_attr.outputs["Attribute"], rot.inputs["Euler"])

    instance = tree.nodes.new("GeometryNodeInstanceOnPoints")
    instance.location = (100, 0)
    tree.links.new(gin.outputs["Points"], instance.inputs["Points"])
    tree.links.new(cube.outputs["Mesh"], instance.inputs["Instance"])
    tree.links.new(rot.outputs["Rotation"], instance.inputs["Rotation"])
    tree.links.new(instance.outputs["Instances"], gout.inputs["Geometry"])
    return _finalize_subgroup(tree)


def ensure_box_subgroups(force_rebuild: bool = False) -> dict[str, bpy.types.NodeTree]:
    return {
        SOLVER_GROUP: ensure_box_floor_step(force_rebuild),
        POST_GROUP: ensure_post_instance_box(force_rebuild),
    }


def validate_box_parameters(params: dict[str, Any]) -> tuple[list[float], float]:
    """Validate the isolated box parameters independently of the contract."""

    half_extents = params.get(HALF_EXTENTS_KEY)
    if not isinstance(half_extents, (list, tuple)) or len(half_extents) != 3:
        raise ValueError("box_half_extents must be a 3-element vector")
    try:
        hx, hy, hz = (float(value) for value in half_extents)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("box_half_extents must contain finite numbers") from exc
    if not all(math.isfinite(value) for value in (hx, hy, hz)):
        raise ValueError("box_half_extents must be finite")
    if min(hx, hy, hz) <= 0.0:
        raise ValueError("box_half_extents components must be strictly positive")

    tilt = params.get(INITIAL_TILT_KEY)
    try:
        tilt_value = float(tilt)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("box_initial_tilt must be a finite number") from exc
    if not math.isfinite(tilt_value):
        raise ValueError("box_initial_tilt must be finite")
    if abs(tilt_value) >= math.pi / 2.0:
        raise ValueError("box_initial_tilt must be strictly between -pi/2 and pi/2 radians")

    gravity = params.get("gravity")
    if not isinstance(gravity, (list, tuple)) or len(gravity) != 3:
        raise ValueError("gravity must be a 3-element vector")
    try:
        gravity_values = [float(value) for value in gravity]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("gravity must contain finite numbers") from exc
    if not all(math.isfinite(value) for value in gravity_values):
        raise ValueError("gravity must be finite")
    if gravity_values[1] != 0.0:
        raise ValueError(
            "rigid_box_tipping supports XZ-plane motion with Y-axis rotation; gravity Y must be 0"
        )
    return [hx, hy, hz], tilt_value


def preflight_box_tipping(obj: bpy.types.Object, request: dict[str, Any]) -> dict[str, Any]:
    """Reject unsupported inputs before any graph is created or changed.

    The first mechanism revision supports a single fixed horizontal ground
    plane and one box per target vertex.  Unsupported collision sources are
    rejected instead of being silently ignored.
    """

    params = request["parameters"]
    half_extents, tilt = validate_box_parameters(params)
    if request.get("collider_object"):
        raise ValueError("rigid_box_tipping uses only the fixed horizontal ground plane; collider_object is unsupported")
    if request.get("collider_collection"):
        raise ValueError("rigid_box_tipping uses only the fixed horizontal ground plane; collider_collection is unsupported")
    if request.get("instance_collection"):
        raise ValueError("rigid_box_tipping instances true-size cubes internally; instance_collection is unsupported")
    if obj.type != "MESH":
        raise ValueError("rigid_box_tipping requires a mesh point source")
    modifier_name = str(request.get("modifier_name") or "PhysicsNodes")
    modifiers = list(obj.modifiers)
    managed = obj.modifiers.get(modifier_name)
    if managed is None:
        preceding = modifiers
        managed_index = None
    else:
        managed_index = next(
            (index for index, modifier in enumerate(modifiers) if modifier.as_pointer() == managed.as_pointer()),
            None,
        )
        preceding = modifiers[:managed_index] if managed_index is not None else modifiers
    blockers = [
        f"{index}:{modifier.name}({modifier.type})"
        for index, modifier in enumerate(preceding)
        if bool(getattr(modifier, "show_viewport", False)) or bool(getattr(modifier, "show_render", False))
    ]
    if blockers:
        raise ValueError(
            "rigid_box_tipping cannot evaluate its source boundary when an enabled modifier "
            f"precedes '{modifier_name}'; disable or move these modifiers after it: {', '.join(blockers)}"
        )
    if int(params.get("scatter_count", 0)) != 0:
        raise ValueError("rigid_box_tipping consumes mesh vertices directly; scatter_count must be 0")
    mesh = obj.data
    if mesh is None or len(mesh.vertices) < 1:
        raise ValueError("rigid_box_tipping requires at least one source vertex (one box per vertex)")
    if len(mesh.vertices) > 64:
        raise ValueError("rigid_box_tipping supports at most 64 sparse boxes in this revision")
    return {
        "preset_id": PRESET_ID,
        "box_half_extents": half_extents,
        "box_initial_tilt": tilt,
        "box_count": len(mesh.vertices),
        "managed_modifier": modifier_name,
        "managed_modifier_index": managed_index,
        "preceding_modifiers": [f"{index}:{modifier.name}" for index, modifier in enumerate(preceding)],
        "preceding_enabled_modifiers": [],
        "ground": "fixed_horizontal_plane",
        "box_box_contact": False,
        "arbitrary_mesh_collision": False,
        "non_planar_motion": False,
    }
