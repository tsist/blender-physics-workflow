"""Residual-driven sphere-proxy contact graph.

The graph runs a user-facing base sweep budget and then a bounded residual
continuation. Candidate records are rebuilt at every sweep. Each sweep
interleaves sphere pairs, the existing mesh collider, and an accurate floor
response before the residual is measured.

This module only builds nodes; it does not run Blender.
"""

from __future__ import annotations

import uuid

import bpy


MAX_EXTRA_SWEEPS = 32
INTERNAL_RESIDUAL_TOL = 1e-5

# Sleep support is deliberately looser than the collision tolerance (1e-6) but
# still scaled by the sphere radius.  A supporter must be close enough to be a
# current resting contact and must not be a coincident fallback-normal pair.
SUPPORT_GAP_RATIO = 0.02
SUPPORT_MIN_DIST_RATIO = 0.5
SUPPORT_NORMAL_MIN_Z = 0.1


def _socket(tree, name, in_out, socket_type, default=None):
    sock = tree.interface.new_socket(name=name, in_out=in_out, socket_type=socket_type)
    if default is not None:
        sock.default_value = default
    return sock


def _node(tree, type_name, loc=(0.0, 0.0)):
    node = tree.nodes.new(type_name)
    node.location = loc
    return node


def _set(tree, socket, value):
    if value is None:
        return
    if isinstance(value, bpy.types.NodeSocket):
        tree.links.new(value, socket)
    else:
        socket.default_value = value


def _math(tree, op, a=None, b=None, loc=(0.0, 0.0), c=None):
    # Allow unary calls such as _math("ABSOLUTE", value, (x, y)).
    if isinstance(b, tuple) and len(b) == 2 and loc == (0.0, 0.0):
        loc, b = b, None
    node = _node(tree, "ShaderNodeMath", loc)
    node.operation = op
    _set(tree, node.inputs[0], a)
    if len(node.inputs) > 1:
        _set(tree, node.inputs[1], b)
    if len(node.inputs) > 2:
        _set(tree, node.inputs[2], c)
    return node


def _vmath(tree, op, a=None, b=None, loc=(0.0, 0.0)):
    node = _node(tree, "ShaderNodeVectorMath", loc)
    node.operation = op
    _set(tree, node.inputs[0], a)
    if b is not None:
        _set(tree, node.inputs["Scale"] if op == "SCALE" else node.inputs[1], b)
    return node


def _bool(tree, op, a=None, b=None, loc=(0.0, 0.0)):
    node = _node(tree, "FunctionNodeBooleanMath", loc)
    node.operation = op
    _set(tree, node.inputs[0], a)
    if len(node.inputs) > 1:
        _set(tree, node.inputs[1], b)
    return node


def _named(tree, name, data_type="FLOAT_VECTOR", loc=(0.0, 0.0)):
    node = _node(tree, "GeometryNodeInputNamedAttribute", loc)
    node.data_type = data_type
    node.inputs["Name"].default_value = name
    return node


def _sample_vec(tree, geometry, value, index, loc=(0.0, 0.0)):
    node = _node(tree, "GeometryNodeSampleIndex", loc)
    node.data_type = "FLOAT_VECTOR"
    node.domain = "POINT"
    node.clamp = True
    _set(tree, node.inputs["Geometry"], geometry)
    _set(tree, node.inputs["Value"], value)
    _set(tree, node.inputs["Index"], index)
    return node


def _sample_int(tree, geometry, value, index, loc=(0.0, 0.0)):
    node = _node(tree, "GeometryNodeSampleIndex", loc)
    node.data_type = "INT"
    node.domain = "POINT"
    node.clamp = True
    _set(tree, node.inputs["Geometry"], geometry)
    _set(tree, node.inputs["Value"], value)
    _set(tree, node.inputs["Index"], index)
    return node


def _sample_bool(tree, geometry, value, index, loc=(0.0, 0.0)):
    node = _node(tree, "GeometryNodeSampleIndex", loc)
    node.data_type = "BOOLEAN"
    node.domain = "POINT"
    node.clamp = True
    _set(tree, node.inputs["Geometry"], geometry)
    _set(tree, node.inputs["Value"], value)
    _set(tree, node.inputs["Index"], index)
    return node


def _switch_vec(tree, switch, false_value=None, true_value=None, loc=(0.0, 0.0)):
    node = _node(tree, "GeometryNodeSwitch", loc)
    node.input_type = "VECTOR"
    _set(tree, node.inputs["Switch"], switch)
    _set(tree, node.inputs["False"], false_value)
    _set(tree, node.inputs["True"], true_value)
    return node


def _switch_float(tree, switch, false_value=None, true_value=None, loc=(0.0, 0.0)):
    node = _node(tree, "GeometryNodeSwitch", loc)
    node.input_type = "FLOAT"
    _set(tree, node.inputs["Switch"], switch)
    _set(tree, node.inputs["False"], false_value)
    _set(tree, node.inputs["True"], true_value)
    return node


def _add_residual(tree, geometry, radius, loc=(0.0, 0.0)):
    """Return the maximum nearest-neighbour penetration field."""
    pos = _node(tree, "GeometryNodeInputPosition", (loc[0], loc[1]))
    nearest = _node(tree, "GeometryNodeIndexOfNearest", (loc[0] + 180, loc[1]))
    tree.links.new(pos.outputs["Position"], nearest.inputs["Position"])
    neighbor = _sample_vec(
        tree,
        geometry,
        pos.outputs["Position"],
        nearest.outputs["Index"],
        (loc[0] + 360, loc[1]),
    )
    sep = _vmath(tree, "SUBTRACT", pos.outputs["Position"], neighbor.outputs["Value"], (loc[0] + 540, loc[1]))
    dist = _vmath(tree, "LENGTH", sep.outputs["Vector"], loc=(loc[0] + 720, loc[1]))
    radius2 = _math(tree, "MULTIPLY", radius, 2.0, (loc[0] + 720, loc[1] - 140))
    pen_raw = _math(tree, "SUBTRACT", radius2.outputs["Value"], dist.outputs["Value"], (loc[0] + 900, loc[1]))
    pen = _math(tree, "MAXIMUM", pen_raw.outputs["Value"], 0.0, (loc[0] + 1080, loc[1]))
    pen_valid = _math(
        tree,
        "MULTIPLY",
        pen.outputs["Value"],
        nearest.outputs["Has Neighbor"],
        (loc[0] + 1260, loc[1]),
    )
    stat = _node(tree, "GeometryNodeAttributeStatistic", (loc[0] + 1440, loc[1]))
    stat.data_type = "FLOAT"
    stat.domain = "POINT"
    tree.links.new(geometry, stat.inputs["Geometry"])
    tree.links.new(pen_valid.outputs["Value"], stat.inputs["Attribute"])
    return stat.outputs["Max"]


def _add_ground_response(tree, geometry, ground_z, radius, restitution, friction, sleep, loc=(0.0, 0.0)):
    """Accurate <= floor response, replacing the strict-< legacy boundary."""
    pos = _node(tree, "GeometryNodeInputPosition", (loc[0], loc[1]))
    vel = _named(tree, "pnw_velocity", "FLOAT_VECTOR", (loc[0], loc[1] - 180))
    z_sep = _node(tree, "ShaderNodeSeparateXYZ", (loc[0] + 180, loc[1]))
    tree.links.new(pos.outputs["Position"], z_sep.inputs["Vector"])
    floor = _math(tree, "ADD", ground_z, radius, (loc[0] + 180, loc[1] - 160))
    hit_bound = _math(tree, "ADD", floor.outputs["Value"], 1e-6, (loc[0] + 360, loc[1] - 160))
    hit = _math(tree, "LESS_THAN", z_sep.outputs["Z"], hit_bound.outputs["Value"], (loc[0] + 540, loc[1]))
    pos_comb = _node(tree, "ShaderNodeCombineXYZ", (loc[0] + 540, loc[1] - 320))
    tree.links.new(z_sep.outputs["X"], pos_comb.inputs["X"])
    tree.links.new(z_sep.outputs["Y"], pos_comb.inputs["Y"])
    tree.links.new(floor.outputs["Value"], pos_comb.inputs["Z"])
    pos_sw = _switch_vec(
        tree,
        hit.outputs["Value"],
        pos.outputs["Position"],
        pos_comb.outputs["Vector"],
        (loc[0] + 720, loc[1]),
    )

    v_sep = _node(tree, "ShaderNodeSeparateXYZ", (loc[0] + 360, loc[1] - 500))
    tree.links.new(vel.outputs["Attribute"], v_sep.inputs["Vector"])
    vz_abs = _math(tree, "ABSOLUTE", v_sep.outputs["Z"], (loc[0] + 540, loc[1] - 500))
    # Restitution is a target outgoing speed, not a licence to keep bouncing
    # forever.  If e*|vz| is below the sleep threshold, the normal target is
    # zero (inelastic) while the non-penetration position clamp remains exact.
    # The threshold is applied to the outgoing rebound, not the incoming
    # gravity speed, so a resting ball cannot sustain a low-energy limit cycle.
    rebound = _math(tree, "MULTIPLY", vz_abs.outputs["Value"], restitution, (loc[0] + 720, loc[1] - 660))
    low_rebound = _math(tree, "LESS_THAN", rebound.outputs["Value"], sleep, (loc[0] + 900, loc[1] - 660))
    keep_restitution = _bool(tree, "NOT", low_rebound.outputs["Value"], loc=(loc[0] + 1080, loc[1] - 660))
    bounce = _math(tree, "MULTIPLY", rebound.outputs["Value"], keep_restitution.outputs["Boolean"], (loc[0] + 1260, loc[1] - 660))
    moving_down = _math(tree, "LESS_THAN", v_sep.outputs["Z"], 0.0, (loc[0] + 900, loc[1] - 500))
    vz_hit = _switch_float(
        tree,
        moving_down.outputs["Value"],
        v_sep.outputs["Z"],
        bounce.outputs["Value"],
        (loc[0] + 1080, loc[1] - 580),
    )
    friction_apply = _math(tree, "MULTIPLY", friction, moving_down.outputs["Value"], (loc[0] + 540, loc[1] - 760))
    friction_keep = _math(tree, "SUBTRACT", 1.0, friction_apply.outputs["Value"], (loc[0] + 540, loc[1] - 820))
    vx = _math(tree, "MULTIPLY", v_sep.outputs["X"], friction_keep.outputs["Value"], (loc[0] + 720, loc[1] - 820))
    vy = _math(tree, "MULTIPLY", v_sep.outputs["Y"], friction_keep.outputs["Value"], (loc[0] + 900, loc[1] - 820))
    vel_comb = _node(tree, "ShaderNodeCombineXYZ", (loc[0] + 1080, loc[1] - 760))
    tree.links.new(vx.outputs["Value"], vel_comb.inputs["X"])
    tree.links.new(vy.outputs["Value"], vel_comb.inputs["Y"])
    tree.links.new(vz_hit.outputs["Output"], vel_comb.inputs["Z"])
    vel_sw = _switch_vec(
        tree,
        hit.outputs["Value"],
        vel.outputs["Attribute"],
        vel_comb.outputs["Vector"],
        (loc[0] + 1260, loc[1] - 580),
    )

    response = _node(tree, "GeometryNodeCaptureAttribute", (loc[0] + 900, loc[1] + 240))
    response.domain = "POINT"
    response.capture_items.new("BOOLEAN", "pnw_ground_hit")
    response.capture_items.new("VECTOR", "pnw_ground_velocity")
    tree.links.new(geometry, response.inputs["Geometry"])
    tree.links.new(hit.outputs["Value"], response.inputs["pnw_ground_hit"])
    tree.links.new(vel_sw.outputs["Output"], response.inputs["pnw_ground_velocity"])
    set_pos = _node(tree, "GeometryNodeSetPosition", (loc[0] + 1080, loc[1] + 240))
    tree.links.new(response.outputs["Geometry"], set_pos.inputs["Geometry"])
    tree.links.new(pos_sw.outputs["Output"], set_pos.inputs["Position"])
    store_vel = _node(tree, "GeometryNodeStoreNamedAttribute", (loc[0] + 1260, loc[1] + 240))
    store_vel.data_type = "FLOAT_VECTOR"
    store_vel.domain = "POINT"
    store_vel.inputs["Name"].default_value = "pnw_velocity"
    tree.links.new(set_pos.outputs["Geometry"], store_vel.inputs["Geometry"])
    tree.links.new(response.outputs["pnw_ground_velocity"], store_vel.inputs["Value"])
    tree.links.new(response.outputs["pnw_ground_hit"], store_vel.inputs["Selection"])
    return store_vel.outputs["Geometry"]


def _add_support_capture(
    tree,
    geometry,
    collider,
    radius,
    ground_z,
    loc=(0.0, 0.0),
    root_enabled=None,
):
    """Store the current static support normal per point (mesh or ground).

    The mesh gate mirrors the rigid mesh collider's finite-boundary rule: a
    nearest face inside the face uses its oriented signed distance, while a
    nearest point on an open edge/vertex uses the true Euclidean feature
    distance.  A mesh contact only counts as sleep/support when its normal has
    an upward component; a vertical wall is collision geometry, not a floor.
    Ground support is the exact horizontal floor. Mesh support wins when it
    has the deeper penetration.
    """
    pos = _node(tree, "GeometryNodeInputPosition", (loc[0], loc[1]))
    prox = _node(tree, "GeometryNodeProximity", (loc[0] + 180, loc[1] + 120))
    prox.target_element = "FACES"
    tree.links.new(collider, prox.inputs["Geometry"])
    tree.links.new(pos.outputs["Position"], prox.inputs["Sample Position"])
    normal_in = _node(tree, "GeometryNodeInputNormal", (loc[0], loc[1] - 80))
    surface_n = _node(tree, "GeometryNodeSampleNearestSurface", (loc[0] + 180, loc[1] - 80))
    surface_n.data_type = "FLOAT_VECTOR"
    tree.links.new(collider, surface_n.inputs["Mesh"])
    tree.links.new(normal_in.outputs["Normal"], surface_n.inputs["Value"])
    tree.links.new(pos.outputs["Position"], surface_n.inputs["Sample Position"])
    n_unit = _vmath(tree, "NORMALIZE", surface_n.outputs["Value"], loc=(loc[0] + 360, loc[1] - 80))

    # Finite-feature query, matching ensure_collide_mesh_geometry.
    prox_edge = _node(tree, "GeometryNodeProximity", (loc[0] + 180, loc[1] + 460))
    prox_edge.target_element = "EDGES"
    tree.links.new(collider, prox_edge.inputs["Geometry"])
    tree.links.new(pos.outputs["Position"], prox_edge.inputs["Sample Position"])
    prox_point = _node(tree, "GeometryNodeProximity", (loc[0] + 180, loc[1] + 620))
    prox_point.target_element = "POINTS"
    tree.links.new(collider, prox_point.inputs["Geometry"])
    tree.links.new(pos.outputs["Position"], prox_point.inputs["Sample Position"])
    boundary_min = _math(tree, "MINIMUM", prox_edge.outputs["Distance"], prox_point.outputs["Distance"], (loc[0] + 360, loc[1] + 520))
    use_edge = _math(tree, "LESS_THAN", prox_edge.outputs["Distance"], prox_point.outputs["Distance"], (loc[0] + 360, loc[1] + 660))
    boundary_pos = _switch_vec(
        tree,
        use_edge.outputs["Value"],
        prox_point.outputs["Position"],
        prox_edge.outputs["Position"],
        (loc[0] + 540, loc[1] + 620),
    )
    boundary_vec = _vmath(tree, "SUBTRACT", pos.outputs["Position"], boundary_pos.outputs["Output"], (loc[0] + 720, loc[1] + 620))
    boundary_dist = _vmath(tree, "LENGTH", boundary_vec.outputs["Vector"], loc=(loc[0] + 900, loc[1] + 620))
    boundary_norm_raw = _vmath(tree, "NORMALIZE", boundary_vec.outputs["Vector"], loc=(loc[0] + 1080, loc[1] + 620))
    boundary_tiny = _math(tree, "GREATER_THAN", boundary_dist.outputs["Value"], 1e-6, (loc[0] + 1080, loc[1] + 520))
    boundary_norm = _switch_vec(
        tree,
        boundary_tiny.outputs["Value"],
        n_unit.outputs["Vector"],
        boundary_norm_raw.outputs["Vector"],
        (loc[0] + 1260, loc[1] + 620),
    )
    boundary_feature_valid = _math(tree, "MAXIMUM", prox_edge.outputs["Is Valid"], prox_point.outputs["Is Valid"], (loc[0] + 900, loc[1] + 480))
    boundary_face_delta = _math(tree, "ADD", prox.outputs["Distance"], 1e-5, (loc[0] + 900, loc[1] + 380))
    boundary_near = _math(tree, "LESS_THAN", boundary_min.outputs["Value"], boundary_face_delta.outputs["Value"], (loc[0] + 1080, loc[1] + 380))
    boundary_is = _math(tree, "MULTIPLY", boundary_near.outputs["Value"], boundary_feature_valid.outputs["Value"], (loc[0] + 1260, loc[1] + 480))
    boundary_radius = _math(tree, "ADD", radius, 1e-4, (loc[0] + 1080, loc[1] + 760))
    boundary_within = _math(tree, "LESS_THAN", boundary_dist.outputs["Value"], boundary_radius.outputs["Value"], (loc[0] + 1260, loc[1] + 760))
    boundary_valid = _math(tree, "MULTIPLY", boundary_is.outputs["Value"], boundary_within.outputs["Value"], (loc[0] + 1440, loc[1] + 620))

    d_vec = _vmath(tree, "SUBTRACT", pos.outputs["Position"], prox.outputs["Position"], (loc[0] + 360, loc[1] + 120))
    signed = _vmath(tree, "DOT_PRODUCT", d_vec.outputs["Vector"], n_unit.outputs["Vector"], (loc[0] + 540, loc[1] + 120))
    reach = _math(tree, "MULTIPLY", radius, 2.5, (loc[0] + 360, loc[1] + 300))
    near = _math(tree, "LESS_THAN", prox.outputs["Distance"], reach.outputs["Value"], (loc[0] + 540, loc[1] + 300))
    inside_bound = _math(tree, "ADD", radius, 1e-6, (loc[0] + 540, loc[1] + 180))
    inside_face = _math(tree, "LESS_THAN", signed.outputs["Value"], inside_bound.outputs["Value"], (loc[0] + 720, loc[1] + 180))
    effective_inside = _switch_float(
        tree,
        boundary_is.outputs["Value"],
        inside_face.outputs["Value"],
        boundary_valid.outputs["Value"],
        (loc[0] + 900, loc[1] + 240),
    )
    mesh_near = _bool(tree, "AND", near.outputs["Value"], effective_inside.outputs["Output"], (loc[0] + 1080, loc[1] + 240))
    mesh_valid = _bool(tree, "AND", prox.outputs["Is Valid"], mesh_near.outputs["Boolean"], (loc[0] + 1260, loc[1] + 240))
    effective_dist = _switch_float(
        tree,
        boundary_is.outputs["Value"],
        signed.outputs["Value"],
        boundary_dist.outputs["Value"],
        (loc[0] + 900, loc[1] + 120),
    )
    effective_n = _switch_vec(
        tree,
        boundary_is.outputs["Value"],
        n_unit.outputs["Vector"],
        boundary_norm.outputs["Output"],
        (loc[0] + 1440, loc[1] + 120),
    )
    pos_sep = _node(tree, "ShaderNodeSeparateXYZ", (loc[0] + 360, loc[1] - 260))
    tree.links.new(pos.outputs["Position"], pos_sep.inputs["Vector"])
    floor = _math(tree, "ADD", ground_z, radius, (loc[0] + 360, loc[1] - 400))
    ground_bound = _math(tree, "ADD", floor.outputs["Value"], 1e-6, (loc[0] + 540, loc[1] - 400))
    ground_valid = _math(tree, "LESS_THAN", pos_sep.outputs["Z"], ground_bound.outputs["Value"], (loc[0] + 720, loc[1] - 260))
    mesh_pen = _math(tree, "SUBTRACT", radius, effective_dist.outputs["Output"], (loc[0] + 1080, loc[1] + 120))
    ground_pen = _math(tree, "SUBTRACT", floor.outputs["Value"], pos_sep.outputs["Z"], (loc[0] + 720, loc[1] - 400))
    not_ground = _bool(tree, "NOT", ground_valid.outputs["Value"], loc=(loc[0] + 900, loc[1] - 260))
    up_sep = _node(tree, "ShaderNodeSeparateXYZ", (loc[0] + 1440, loc[1] - 40))
    tree.links.new(effective_n.outputs["Output"], up_sep.inputs["Vector"])
    mesh_up = _math(tree, "GREATER_THAN", up_sep.outputs["Z"], SUPPORT_NORMAL_MIN_Z, (loc[0] + 1620, loc[1] - 40))
    # Support projection is valid for every current static contact surface,
    # including vertical walls.  The upward component is only required for the
    # separate sleep-root flag below, never for position constraint projection.
    mesh_any = _bool(tree, "AND", mesh_valid.outputs["Boolean"], not_ground.outputs["Boolean"], (loc[0] + 1800, loc[1] + 80))
    both_mesh = _bool(tree, "AND", mesh_valid.outputs["Boolean"], ground_valid.outputs["Value"], (loc[0] + 1800, loc[1] - 100))
    mesh_deeper = _math(tree, "GREATER_THAN", mesh_pen.outputs["Value"], ground_pen.outputs["Value"], (loc[0] + 900, loc[1] - 160))
    both_mesh_deeper = _bool(tree, "AND", both_mesh.outputs["Boolean"], mesh_deeper.outputs["Value"], (loc[0] + 1980, loc[1] - 100))
    use_mesh = _bool(tree, "OR", mesh_any.outputs["Boolean"], both_mesh_deeper.outputs["Boolean"], (loc[0] + 1980, loc[1] + 20))
    support_valid = _bool(tree, "OR", use_mesh.outputs["Boolean"], ground_valid.outputs["Value"], (loc[0] + 2160, loc[1] - 280))
    ground_n = _node(tree, "ShaderNodeCombineXYZ", (loc[0] + 540, loc[1] - 560))
    ground_n.inputs["Z"].default_value = 1.0
    support_n = _switch_vec(tree, use_mesh.outputs["Boolean"], ground_n.outputs["Vector"], effective_n.outputs["Output"], (loc[0] + 2160, loc[1] + 120))
    store_valid = _node(tree, "GeometryNodeStoreNamedAttribute", (loc[0] + 2340, loc[1] + 20))
    store_valid.data_type = "BOOLEAN"
    store_valid.domain = "POINT"
    store_valid.inputs["Name"].default_value = "pnw_support_valid"
    tree.links.new(geometry, store_valid.inputs["Geometry"])
    tree.links.new(support_valid.outputs["Boolean"], store_valid.inputs["Value"])
    store_n = _node(tree, "GeometryNodeStoreNamedAttribute", (loc[0] + 2520, loc[1] + 20))
    store_n.data_type = "FLOAT_VECTOR"
    store_n.domain = "POINT"
    store_n.inputs["Name"].default_value = "pnw_support_normal"
    tree.links.new(store_valid.outputs["Geometry"], store_n.inputs["Geometry"])
    tree.links.new(support_n.outputs["Output"], store_n.inputs["Value"])
    mesh_root = _bool(tree, "AND", mesh_valid.outputs["Boolean"], mesh_up.outputs["Value"], (loc[0] + 1800, loc[1] - 180))
    sleep_root_raw = _bool(tree, "OR", ground_valid.outputs["Value"], mesh_root.outputs["Boolean"], (loc[0] + 1980, loc[1] - 180))
    if root_enabled is None:
        root_enabled_node = _named(tree, "pnw_sleep_root_enabled", "BOOLEAN", (loc[0] + 2160, loc[1] - 180))
        root_enabled_socket = root_enabled_node.outputs["Attribute"]
    else:
        root_enabled_socket = root_enabled
    sleep_root_valid = _bool(
        tree,
        "AND",
        sleep_root_raw.outputs["Boolean"],
        root_enabled_socket,
        (loc[0] + 2340, loc[1] - 180),
    )
    store_sleep_root = _node(tree, "GeometryNodeStoreNamedAttribute", (loc[0] + 2700, loc[1] + 20))
    store_sleep_root.data_type = "BOOLEAN"
    store_sleep_root.domain = "POINT"
    store_sleep_root.inputs["Name"].default_value = "pnw_sleep_root_valid"
    tree.links.new(store_n.outputs["Geometry"], store_sleep_root.inputs["Geometry"])
    tree.links.new(sleep_root_valid.outputs["Boolean"], store_sleep_root.inputs["Value"])
    return store_sleep_root.outputs["Geometry"]


def _build_sweep_group(mesh_group):
    """One bounded contact sweep: sphere pairs -> mesh -> accurate ground."""
    tree = bpy.data.node_groups.new(f"PN_ContactSweep_{uuid.uuid4().hex[:10]}", "GeometryNodeTree")
    _socket(tree, "Geometry", "INPUT", "NodeSocketGeometry")
    _socket(tree, "Collider Geometry", "INPUT", "NodeSocketGeometry")
    _socket(tree, "Radius", "INPUT", "NodeSocketFloat", 0.12)
    _socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.35)
    _socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.45)
    _socket(tree, "Sleep Velocity", "INPUT", "NodeSocketFloat", 0.08)
    _socket(tree, "Ground Z", "INPUT", "NodeSocketFloat", 0.0)
    _socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")
    gin = _node(tree, "NodeGroupInput", (-2000, 0))
    gout = _node(tree, "NodeGroupOutput", (3000, 0))
    sim = _add_support_capture(
        tree,
        gin.outputs["Geometry"],
        gin.outputs["Collider Geometry"],
        gin.outputs["Radius"],
        gin.outputs["Ground Z"],
        (-2600, -1000),
    )
    pos = _node(tree, "GeometryNodeInputPosition", (-1760, -220))
    input_index = _node(tree, "GeometryNodeInputIndex", (-1760, -360))
    radius2 = _math(tree, "MULTIPLY", gin.outputs["Radius"], 2.0, (-800, -300))

    nearest = _node(tree, "GeometryNodeIndexOfNearest", (-1540, -120))
    tree.links.new(pos.outputs["Position"], nearest.inputs["Position"])
    neighbor_pos = _sample_vec(tree, sim, pos.outputs["Position"], nearest.outputs["Index"], (-1340, -80))
    delta = _vmath(tree, "SUBTRACT", pos.outputs["Position"], neighbor_pos.outputs["Value"], (-1140, -80))
    dist = _vmath(tree, "LENGTH", delta.outputs["Vector"], loc=(-960, -80))
    neighbor_nearest = _sample_int(tree, sim, nearest.outputs["Index"], nearest.outputs["Index"], (-1340, -280))

    captured = sim
    for name, data_type, value in (
        ("pnw_ci", "INT", input_index.outputs["Index"]),
        ("pnw_cj", "INT", nearest.outputs["Index"]),
        ("pnw_cj_nearest", "INT", neighbor_nearest.outputs["Value"]),
        ("pnw_cdist", "FLOAT", dist.outputs["Value"]),
        ("pnw_has_neighbor", "BOOLEAN", nearest.outputs["Has Neighbor"]),
    ):
        store = _node(tree, "GeometryNodeStoreNamedAttribute", (-1140, 180))
        store.data_type = data_type
        store.domain = "POINT"
        store.inputs["Name"].default_value = name
        tree.links.new(captured, store.inputs["Geometry"])
        tree.links.new(value, store.inputs["Value"])
        captured = store.outputs["Geometry"]
    contact_clear = _node(tree, "GeometryNodeStoreNamedAttribute", (-940, 180))
    contact_clear.data_type = "FLOAT"
    contact_clear.domain = "POINT"
    contact_clear.inputs["Name"].default_value = "pnw_contact"
    contact_clear.inputs["Value"].default_value = 0.0
    tree.links.new(captured, contact_clear.inputs["Geometry"])
    support_clear = _node(tree, "GeometryNodeStoreNamedAttribute", (-760, 180))
    support_clear.data_type = "BOOLEAN"
    support_clear.domain = "POINT"
    support_clear.inputs["Name"].default_value = "pnw_contact_support"
    support_clear.inputs["Value"].default_value = False
    tree.links.new(contact_clear.outputs["Geometry"], support_clear.inputs["Geometry"])

    candidate_eps = _math(tree, "ADD", radius2.outputs["Value"], 1e-6, (-960, -300))
    near = _math(tree, "LESS_THAN", dist.outputs["Value"], candidate_eps.outputs["Value"], (-780, -240))
    i_lt_j = _math(tree, "LESS_THAN", input_index.outputs["Index"], nearest.outputs["Index"], (-780, -400))
    j_eq_i = _math(
        tree,
        "COMPARE",
        neighbor_nearest.outputs["Value"],
        input_index.outputs["Index"],
        loc=(-780, -520),
        c=0.5,
    )
    j_ne_i = _bool(tree, "NOT", j_eq_i.outputs["Value"], loc=(-600, -520))
    keep_pair = _bool(tree, "OR", i_lt_j.outputs["Value"], j_ne_i.outputs["Boolean"], (-420, -440))
    has_near = _bool(tree, "AND", nearest.outputs["Has Neighbor"], near.outputs["Value"], (-420, -300))
    active = _bool(tree, "AND", has_near.outputs["Boolean"], keep_pair.outputs["Boolean"], (-240, -360))
    inactive = _bool(tree, "NOT", active.outputs["Boolean"], loc=(-60, -360))

    candidates = _node(tree, "GeometryNodeDeleteGeometry", (120, 180))
    candidates.domain = "POINT"
    tree.links.new(support_clear.outputs["Geometry"], candidates.inputs["Geometry"])
    tree.links.new(inactive.outputs["Boolean"], candidates.inputs["Selection"])
    cand_flag = _node(tree, "GeometryNodeStoreNamedAttribute", (300, 180))
    cand_flag.data_type = "BOOLEAN"
    cand_flag.domain = "POINT"
    cand_flag.inputs["Name"].default_value = "pnw_contact_candidate"
    cand_flag.inputs["Value"].default_value = True
    tree.links.new(candidates.outputs["Geometry"], cand_flag.inputs["Geometry"])
    sim_flag = _node(tree, "GeometryNodeStoreNamedAttribute", (300, 360))
    sim_flag.data_type = "BOOLEAN"
    sim_flag.domain = "POINT"
    sim_flag.inputs["Name"].default_value = "pnw_contact_candidate"
    sim_flag.inputs["Value"].default_value = False
    tree.links.new(support_clear.outputs["Geometry"], sim_flag.inputs["Geometry"])
    sim_count = _node(tree, "GeometryNodeAttributeDomainSize", (480, 420))
    sim_count.component = "POINTCLOUD"
    cand_count = _node(tree, "GeometryNodeAttributeDomainSize", (480, 240))
    cand_count.component = "POINTCLOUD"
    tree.links.new(sim_flag.outputs["Geometry"], sim_count.inputs["Geometry"])
    tree.links.new(cand_flag.outputs["Geometry"], cand_count.inputs["Geometry"])
    joined = _node(tree, "GeometryNodeJoinGeometry", (660, 300))
    tree.links.new(cand_flag.outputs["Geometry"], joined.inputs[0])
    tree.links.new(sim_flag.outputs["Geometry"], joined.inputs[0])

    inner_in = _node(tree, "GeometryNodeRepeatInput", (840, 300))
    inner_out = _node(tree, "GeometryNodeRepeatOutput", (2600, 300))
    inner_in.pair_with_output(inner_out)
    tree.links.new(cand_count.outputs["Point Count"], inner_in.inputs["Iterations"])
    tree.links.new(joined.outputs["Geometry"], inner_in.inputs["Geometry"])
    cand_index = _node(tree, "FunctionNodeIntegerMath", (840, 520))
    cand_index.operation = "ADD"
    tree.links.new(sim_count.outputs["Point Count"], cand_index.inputs[0])
    tree.links.new(inner_in.outputs["Iteration"], cand_index.inputs[1])
    ci = _sample_int(
        tree,
        inner_in.outputs["Geometry"],
        _named(tree, "pnw_ci", "INT", (660, 520)).outputs["Attribute"],
        cand_index.outputs[0],
        (1020, 520),
    )
    cj = _sample_int(
        tree,
        inner_in.outputs["Geometry"],
        _named(tree, "pnw_cj", "INT", (660, 640)).outputs["Attribute"],
        cand_index.outputs[0],
        (1020, 640),
    )
    support_valid_attr = _named(tree, "pnw_support_valid", "BOOLEAN", (660, 820))
    sleep_root_valid_attr = _named(tree, "pnw_sleep_root_valid", "BOOLEAN", (660, 870))
    support_n_attr = _named(tree, "pnw_support_normal", "FLOAT_VECTOR", (660, 920))
    support_valid_i = _sample_bool(
        tree,
        inner_in.outputs["Geometry"],
        support_valid_attr.outputs["Attribute"],
        ci.outputs["Value"],
        (1020, 820),
    )
    support_valid_j = _sample_bool(
        tree,
        inner_in.outputs["Geometry"],
        support_valid_attr.outputs["Attribute"],
        cj.outputs["Value"],
        (1020, 920),
    )
    sleep_root_i = _sample_bool(
        tree,
        inner_in.outputs["Geometry"],
        sleep_root_valid_attr.outputs["Attribute"],
        ci.outputs["Value"],
        (1020, 870),
    )
    sleep_root_j = _sample_bool(
        tree,
        inner_in.outputs["Geometry"],
        sleep_root_valid_attr.outputs["Attribute"],
        cj.outputs["Value"],
        (1020, 970),
    )
    support_n_i = _sample_vec(
        tree,
        inner_in.outputs["Geometry"],
        support_n_attr.outputs["Attribute"],
        ci.outputs["Value"],
        (1020, 1020),
    )
    support_n_j = _sample_vec(
        tree,
        inner_in.outputs["Geometry"],
        support_n_attr.outputs["Attribute"],
        cj.outputs["Value"],
        (1020, 1120),
    )
    # Only rooted sleep can propagate support.  A stale/legacy free-air sleep
    # flag is deliberately not a root.
    sleep_support_attr = _named(tree, "pnw_sleep_rooted", "BOOLEAN", (660, 1220))
    contact_support_attr = _named(tree, "pnw_contact_support", "BOOLEAN", (660, 1320))
    sleep_i = _sample_bool(
        tree,
        inner_in.outputs["Geometry"],
        sleep_support_attr.outputs["Attribute"],
        ci.outputs["Value"],
        (1020, 1220),
    )
    sleep_j = _sample_bool(
        tree,
        inner_in.outputs["Geometry"],
        sleep_support_attr.outputs["Attribute"],
        cj.outputs["Value"],
        (1020, 1320),
    )
    contact_support_i = _sample_bool(
        tree,
        inner_in.outputs["Geometry"],
        contact_support_attr.outputs["Attribute"],
        ci.outputs["Value"],
        (1020, 1420),
    )
    contact_support_j = _sample_bool(
        tree,
        inner_in.outputs["Geometry"],
        contact_support_attr.outputs["Attribute"],
        cj.outputs["Value"],
        (1020, 1520),
    )
    p_i = _sample_vec(tree, inner_in.outputs["Geometry"], pos.outputs["Position"], ci.outputs["Value"], (1200, 120))
    p_j = _sample_vec(tree, inner_in.outputs["Geometry"], pos.outputs["Position"], cj.outputs["Value"], (1200, 0))
    velocity = _named(tree, "pnw_velocity", "FLOAT_VECTOR", (840, -160))
    v_i = _sample_vec(tree, inner_in.outputs["Geometry"], velocity.outputs["Attribute"], ci.outputs["Value"], (1200, -120))
    v_j = _sample_vec(tree, inner_in.outputs["Geometry"], velocity.outputs["Attribute"], cj.outputs["Value"], (1200, -240))

    sep = _vmath(tree, "SUBTRACT", p_i.outputs["Value"], p_j.outputs["Value"], (1380, 60))
    current_dist = _vmath(tree, "LENGTH", sep.outputs["Vector"], loc=(1560, 60))
    pen_raw = _math(tree, "SUBTRACT", radius2.outputs["Value"], current_dist.outputs["Value"], (1740, 60))
    pen = _math(tree, "MAXIMUM", pen_raw.outputs["Value"], 0.0, (1920, 60))
    contact_eps = _math(tree, "ADD", radius2.outputs["Value"], 1e-6, (1920, 160))
    contact_valid = _math(tree, "LESS_THAN", current_dist.outputs["Value"], contact_eps.outputs["Value"], (2100, 160))

    normal_raw = _vmath(tree, "NORMALIZE", sep.outputs["Vector"], loc=(1560, -160))
    tiny = _math(tree, "GREATER_THAN", current_dist.outputs["Value"], 1e-6, (1560, -300))
    min_index = _math(tree, "MINIMUM", ci.outputs["Value"], cj.outputs["Value"], (1380, -360))
    angle = _math(tree, "MULTIPLY", min_index.outputs["Value"], 2.399963229728653, (1560, -360))
    cos_a = _math(tree, "COSINE", angle.outputs["Value"], loc=(1740, -320))
    sin_a = _math(tree, "SINE", angle.outputs["Value"], loc=(1740, -440))
    fallback_vec = _node(tree, "ShaderNodeCombineXYZ", (1920, -360))
    tree.links.new(cos_a.outputs["Value"], fallback_vec.inputs["X"])
    tree.links.new(sin_a.outputs["Value"], fallback_vec.inputs["Y"])
    fallback_vec.inputs["Z"].default_value = 0.37
    fallback_norm = _vmath(tree, "NORMALIZE", fallback_vec.outputs["Vector"], loc=(2100, -360))
    i_lt_j_inner = _math(tree, "LESS_THAN", ci.outputs["Value"], cj.outputs["Value"], (1380, -540))
    sign_tmp = _math(tree, "MULTIPLY", i_lt_j_inner.outputs["Value"], -2.0, (1560, -540))
    sign = _math(tree, "ADD", sign_tmp.outputs["Value"], 1.0, (1740, -540))
    fallback_signed = _vmath(tree, "SCALE", fallback_norm.outputs["Vector"], sign.outputs["Value"], (2100, -500))
    normal_sw = _switch_vec(
        tree,
        tiny.outputs["Value"],
        fallback_signed.outputs["Vector"],
        normal_raw.outputs["Vector"],
        (2280, -260),
    )
    normal = normal_sw.outputs["Output"]

    normal_sep = _node(tree, "ShaderNodeSeparateXYZ", (2280, 120))
    tree.links.new(normal, normal_sep.inputs["Vector"])
    p_i_sep = _node(tree, "ShaderNodeSeparateXYZ", (1380, 320))
    p_j_sep = _node(tree, "ShaderNodeSeparateXYZ", (1380, 240))
    tree.links.new(p_i.outputs["Value"], p_i_sep.inputs["Vector"])
    tree.links.new(p_j.outputs["Value"], p_j_sep.inputs["Vector"])
    half_pen = _math(tree, "MULTIPLY", pen.outputs["Value"], 0.5, (2100, 60))
    floor = _math(tree, "ADD", gin.outputs["Ground Z"], gin.outputs["Radius"], (1740, 400))
    nz_half = _math(tree, "MULTIPLY", normal_sep.outputs["Z"], half_pen.outputs["Value"], (2280, 320))
    prospective_i = _math(tree, "ADD", p_i_sep.outputs["Z"], nz_half.outputs["Value"], (2280, 440))
    prospective_j = _math(tree, "SUBTRACT", p_j_sep.outputs["Z"], nz_half.outputs["Value"], (2280, 240))
    i_below = _math(tree, "LESS_THAN", prospective_i.outputs["Value"], floor.outputs["Value"], (2460, 440))
    j_below = _math(tree, "LESS_THAN", prospective_j.outputs["Value"], floor.outputs["Value"], (2460, 240))
    not_j = _bool(tree, "NOT", j_below.outputs["Value"], loc=(2640, 240))
    not_i = _bool(tree, "NOT", i_below.outputs["Value"], loc=(2640, 440))
    only_i = _bool(tree, "AND", i_below.outputs["Value"], not_j.outputs["Boolean"], (2820, 440))
    only_j = _bool(tree, "AND", j_below.outputs["Value"], not_i.outputs["Boolean"], (2820, 240))
    weight_i_first = _switch_float(tree, only_i.outputs["Boolean"], 0.5, 0.0, (3000, 400))
    weight_i = _switch_float(tree, only_j.outputs["Boolean"], weight_i_first.outputs["Output"], 1.0, (3180, 400))
    weight_j = _math(tree, "SUBTRACT", 1.0, weight_i.outputs["Output"], (3360, 400))

    # Static-support projected inverse mass for position correction.
    support_dot_i = _vmath(tree, "DOT_PRODUCT", normal, support_n_i.outputs["Value"], (3360, 540))
    enter_i = _bool(
        tree,
        "AND",
        support_valid_i.outputs["Value"],
        _math(tree, "LESS_THAN", support_dot_i.outputs["Value"], -1e-9, (3540, 500)).outputs["Value"],
        (3720, 540),
    )
    proj_i = _vmath(
        tree,
        "SUBTRACT",
        normal,
        _vmath(tree, "SCALE", support_n_i.outputs["Value"], support_dot_i.outputs["Value"], (3540, 620)).outputs["Vector"],
        (3900, 540),
    )
    a_i = _switch_vec(tree, enter_i.outputs["Boolean"], normal, proj_i.outputs["Vector"], (4080, 540))
    neg_normal = _vmath(tree, "SCALE", normal, -1.0, (3360, 660))
    support_dot_j = _vmath(tree, "DOT_PRODUCT", neg_normal.outputs["Vector"], support_n_j.outputs["Value"], (3540, 660))
    enter_j = _bool(
        tree,
        "AND",
        support_valid_j.outputs["Value"],
        _math(tree, "LESS_THAN", support_dot_j.outputs["Value"], -1e-9, (3720, 620)).outputs["Value"],
        (3900, 660),
    )
    support_dot_j_proj = _vmath(tree, "DOT_PRODUCT", normal, support_n_j.outputs["Value"], (3720, 780))
    proj_j = _vmath(
        tree,
        "SUBTRACT",
        normal,
        _vmath(
            tree,
            "SCALE",
            support_n_j.outputs["Value"],
            support_dot_j_proj.outputs["Value"],
            (3720, 740),
        ).outputs["Vector"],
        (4080, 700),
    )
    a_j = _switch_vec(tree, enter_j.outputs["Boolean"], normal, proj_j.outputs["Vector"], (4260, 660))
    denom_sum = _vmath(tree, "ADD", a_i.outputs["Output"], a_j.outputs["Output"], (4440, 600))
    denom = _vmath(tree, "DOT_PRODUCT", normal, denom_sum.outputs["Vector"], (4620, 600))
    denom_safe = _math(tree, "MAXIMUM", denom.outputs["Value"], 1e-8, (4800, 600))
    denom_ok = _math(tree, "GREATER_THAN", denom.outputs["Value"], 1e-8, (4800, 480))
    lambda_raw = _math(tree, "DIVIDE", pen.outputs["Value"], denom_safe.outputs["Value"], (4980, 600))
    lambda_pos = _switch_float(tree, denom_ok.outputs["Value"], 0.0, lambda_raw.outputs["Value"], (5160, 600))
    delta_i = _vmath(tree, "SCALE", a_i.outputs["Output"], lambda_pos.outputs["Output"], (5340, 600))
    lambda_neg = _math(tree, "MULTIPLY", lambda_pos.outputs["Output"], -1.0, (5160, 720))
    delta_j = _vmath(tree, "SCALE", a_j.outputs["Output"], lambda_neg.outputs["Value"], (5340, 720))

    v_i_sep = _node(tree, "ShaderNodeSeparateXYZ", (2100, -620))
    v_j_sep = _node(tree, "ShaderNodeSeparateXYZ", (2100, -700))
    tree.links.new(v_i.outputs["Value"], v_i_sep.inputs["Vector"])
    tree.links.new(v_j.outputs["Value"], v_j_sep.inputs["Vector"])
    vi_z_floor = _math(tree, "MAXIMUM", v_i_sep.outputs["Z"], 0.0, (2280, -620))
    vj_z_floor = _math(tree, "MAXIMUM", v_j_sep.outputs["Z"], 0.0, (2280, -700))
    v_i_floor = _node(tree, "ShaderNodeCombineXYZ", (2460, -620))
    v_j_floor = _node(tree, "ShaderNodeCombineXYZ", (2460, -700))
    tree.links.new(v_i_sep.outputs["X"], v_i_floor.inputs["X"])
    tree.links.new(v_i_sep.outputs["Y"], v_i_floor.inputs["Y"])
    tree.links.new(vi_z_floor.outputs["Value"], v_i_floor.inputs["Z"])
    tree.links.new(v_j_sep.outputs["X"], v_j_floor.inputs["X"])
    tree.links.new(v_j_sep.outputs["Y"], v_j_floor.inputs["Y"])
    tree.links.new(vj_z_floor.outputs["Value"], v_j_floor.inputs["Z"])
    v_i_eff = _switch_vec(tree, only_i.outputs["Boolean"], v_i.outputs["Value"], v_i_floor.outputs["Vector"], (2640, -620))
    v_j_eff = _switch_vec(tree, only_j.outputs["Boolean"], v_j.outputs["Value"], v_j_floor.outputs["Vector"], (2640, -700))
    rel_vel = _vmath(tree, "SUBTRACT", v_i_eff.outputs["Output"], v_j_eff.outputs["Output"], (2820, -720))
    vn = _vmath(tree, "DOT_PRODUCT", rel_vel.outputs["Vector"], normal, (3000, -720))
    approaching = _math(tree, "LESS_THAN", vn.outputs["Value"], 0.0, (3180, -720))
    velocity_gate = _math(tree, "MULTIPLY", contact_valid.outputs["Value"], approaching.outputs["Value"], (3360, -720))
    # Low-energy pair collisions use an inelastic target (e_eff = 0) while
    # still applying the full non-penetration impulse.  This preserves total
    # momentum and relative tangential motion; it is not a global freeze.
    vn_abs = _math(tree, "ABSOLUTE", vn.outputs["Value"], (3180, -940))
    rebound = _math(tree, "MULTIPLY", vn_abs.outputs["Value"], gin.outputs["Restitution"], (3360, -940))
    low_rebound = _math(tree, "LESS_THAN", rebound.outputs["Value"], gin.outputs["Sleep Velocity"], (3540, -940))
    keep_restitution = _bool(tree, "NOT", low_rebound.outputs["Value"], loc=(3720, -940))
    e_eff = _math(tree, "MULTIPLY", gin.outputs["Restitution"], keep_restitution.outputs["Boolean"], (3900, -940))
    one_plus_e = _math(tree, "ADD", 1.0, e_eff.outputs["Value"], (3180, -860))
    normal_delta_raw = _math(tree, "MULTIPLY", one_plus_e.outputs["Value"], vn.outputs["Value"], (3360, -860))
    normal_delta = _math(tree, "MULTIPLY", normal_delta_raw.outputs["Value"], -1.0, (3540, -860))
    normal_delta = _math(tree, "MULTIPLY", normal_delta.outputs["Value"], velocity_gate.outputs["Value"], (3720, -860))
    normal_i = _vmath(tree, "SCALE", normal, normal_delta.outputs["Value"], (3900, -860))
    normal_i = _vmath(tree, "SCALE", normal_i.outputs["Vector"], weight_i.outputs["Output"], (4080, -860))
    normal_j = _vmath(tree, "SCALE", normal, normal_delta.outputs["Value"], (3900, -940))
    normal_j = _vmath(tree, "SCALE", normal_j.outputs["Vector"], weight_j.outputs["Value"], (4080, -940))
    vn_vec = _vmath(tree, "SCALE", normal, vn.outputs["Value"], (3180, -1000))
    tangent = _vmath(tree, "SUBTRACT", rel_vel.outputs["Vector"], vn_vec.outputs["Vector"], (3360, -1000))
    tangent_scale_i = _math(tree, "MULTIPLY", -1.0, gin.outputs["Friction"], (3540, -1000))
    tangent_scale_i = _math(tree, "MULTIPLY", tangent_scale_i.outputs["Value"], velocity_gate.outputs["Value"], (3720, -1000))
    tangent_scale_i = _math(tree, "MULTIPLY", tangent_scale_i.outputs["Value"], weight_i.outputs["Output"], (3900, -1000))
    tangent_i = _vmath(tree, "SCALE", tangent.outputs["Vector"], tangent_scale_i.outputs["Value"], (4080, -1000))
    tangent_scale_j = _math(tree, "MULTIPLY", gin.outputs["Friction"], velocity_gate.outputs["Value"], (3540, -1080))
    tangent_scale_j = _math(tree, "MULTIPLY", tangent_scale_j.outputs["Value"], weight_j.outputs["Value"], (3720, -1080))
    tangent_j = _vmath(tree, "SCALE", tangent.outputs["Vector"], tangent_scale_j.outputs["Value"], (4080, -1080))
    dv_i = _vmath(tree, "ADD", normal_i.outputs["Vector"], tangent_i.outputs["Vector"], (4260, -900))
    normal_j_neg = _vmath(tree, "SCALE", normal_j.outputs["Vector"], -1.0, (4260, -1000))
    dv_j = _vmath(tree, "ADD", normal_j_neg.outputs["Vector"], tangent_j.outputs["Vector"], (4440, -940))

    idx_inner = _node(tree, "GeometryNodeInputIndex", (3000, 180))
    is_i = _math(tree, "COMPARE", idx_inner.outputs["Index"], ci.outputs["Value"], loc=(3180, 220), c=0.5)
    is_j = _math(tree, "COMPARE", idx_inner.outputs["Index"], cj.outputs["Value"], loc=(3180, 100), c=0.5)
    # Root-connected pair support.  A point can support the point above it only
    # when it is itself touching the static world, already asleep with a
    # validated root, or already root-connected earlier in this sweep.  Free
    # clusters therefore never validate each other.  The distance and
    # relative-speed gates keep moving or deeply coincident pairs out of the
    # support graph.
    support_gap = _math(tree, "MULTIPLY", gin.outputs["Radius"], SUPPORT_GAP_RATIO, (2280, 520))
    support_dist = _math(tree, "ADD", radius2.outputs["Value"], support_gap.outputs["Value"], (2460, 520))
    support_near = _math(
        tree,
        "LESS_THAN",
        current_dist.outputs["Value"],
        support_dist.outputs["Value"],
        (2640, 520),
    )
    support_min = _math(tree, "MULTIPLY", gin.outputs["Radius"], SUPPORT_MIN_DIST_RATIO, (2280, 620))
    support_separated = _math(
        tree,
        "GREATER_THAN",
        current_dist.outputs["Value"],
        support_min.outputs["Value"],
        (2640, 620),
    )
    rel_speed_vec = _vmath(
        tree,
        "SUBTRACT",
        v_i.outputs["Value"],
        v_j.outputs["Value"],
        (1560, -460),
    )
    rel_speed = _vmath(tree, "LENGTH", rel_speed_vec.outputs["Vector"], loc=(1740, -460))
    rel_slow = _math(
        tree,
        "LESS_THAN",
        rel_speed.outputs["Value"],
        gin.outputs["Sleep Velocity"],
        (1920, -460),
    )
    support_dist_ok = _bool(tree, "AND", support_near.outputs["Value"], support_separated.outputs["Value"], (2820, 560))
    support_dist_ok = _bool(tree, "AND", support_dist_ok.outputs["Boolean"], rel_slow.outputs["Value"], (3000, 560))
    rooted_i = _bool(tree, "OR", sleep_root_i.outputs["Value"], sleep_i.outputs["Value"], (2460, 1220))
    rooted_i = _bool(
        tree,
        "OR",
        rooted_i.outputs["Boolean"],
        contact_support_i.outputs["Value"],
        (2640, 1220),
    )
    rooted_j = _bool(tree, "OR", sleep_root_j.outputs["Value"], sleep_j.outputs["Value"], (2460, 1320))
    rooted_j = _bool(
        tree,
        "OR",
        rooted_j.outputs["Boolean"],
        contact_support_j.outputs["Value"],
        (2640, 1320),
    )
    support_i_geom = _math(tree, "GREATER_THAN", normal_sep.outputs["Z"], SUPPORT_NORMAL_MIN_Z, (3180, 380))
    support_j_geom = _math(tree, "LESS_THAN", normal_sep.outputs["Z"], -SUPPORT_NORMAL_MIN_Z, (3180, 460))
    support_i_sel = _bool(tree, "AND", is_i.outputs["Value"], support_i_geom.outputs["Value"], (3360, 380))
    support_i_sel = _bool(tree, "AND", support_i_sel.outputs["Boolean"], rooted_j.outputs["Boolean"], (3540, 380))
    support_j_sel = _bool(tree, "AND", is_j.outputs["Value"], support_j_geom.outputs["Value"], (3360, 460))
    support_j_sel = _bool(tree, "AND", support_j_sel.outputs["Boolean"], rooted_i.outputs["Boolean"], (3540, 460))
    support_pair = _bool(tree, "OR", support_i_sel.outputs["Boolean"], support_j_sel.outputs["Boolean"], (3720, 420))
    support_pair = _bool(
        tree,
        "AND",
        support_pair.outputs["Boolean"],
        support_dist_ok.outputs["Boolean"],
        (3900, 420),
    )
    support_valid_pair = _bool(tree, "AND", contact_valid.outputs["Value"], support_pair.outputs["Boolean"], (4080, 420))
    support_existing = _named(tree, "pnw_contact_support", "BOOLEAN", (3540, 560))
    support_acc = _bool(tree, "OR", support_existing.outputs["Attribute"], support_valid_pair.outputs["Boolean"], (3900, 420))
    pos_now = _node(tree, "GeometryNodeInputPosition", (3000, -40))
    pos_i_new = _vmath(tree, "ADD", pos_now.outputs["Position"], delta_i.outputs["Vector"], (3360, 160))
    pos_j_new = _vmath(tree, "ADD", pos_now.outputs["Position"], delta_j.outputs["Vector"], (3360, 40))
    pos_sw_i = _switch_vec(tree, is_i.outputs["Value"], pos_now.outputs["Position"], pos_i_new.outputs["Vector"], (3540, 160))
    pos_sw_j = _switch_vec(tree, is_j.outputs["Value"], pos_sw_i.outputs["Output"], pos_j_new.outputs["Vector"], (3720, 160))
    set_pos = _node(tree, "GeometryNodeSetPosition", (3900, 300))
    tree.links.new(inner_in.outputs["Geometry"], set_pos.inputs["Geometry"])
    tree.links.new(pos_sw_j.outputs["Output"], set_pos.inputs["Position"])
    vel_now = _named(tree, "pnw_velocity", "FLOAT_VECTOR", (3360, -180))
    vel_i_new = _vmath(tree, "ADD", v_i_eff.outputs["Output"], dv_i.outputs["Vector"], (3900, -120))
    vel_j_new = _vmath(tree, "ADD", v_j_eff.outputs["Output"], dv_j.outputs["Vector"], (3900, -240))
    vel_sw_i = _switch_vec(tree, is_i.outputs["Value"], vel_now.outputs["Attribute"], vel_i_new.outputs["Vector"], (4080, -120))
    vel_sw_j = _switch_vec(tree, is_j.outputs["Value"], vel_sw_i.outputs["Output"], vel_j_new.outputs["Vector"], (4260, -120))
    store_vel = _node(tree, "GeometryNodeStoreNamedAttribute", (4440, 300))
    store_vel.data_type = "FLOAT_VECTOR"
    store_vel.domain = "POINT"
    store_vel.inputs["Name"].default_value = "pnw_velocity"
    tree.links.new(set_pos.outputs["Geometry"], store_vel.inputs["Geometry"])
    tree.links.new(vel_sw_j.outputs["Output"], store_vel.inputs["Value"])
    contact_pair = _bool(tree, "OR", is_i.outputs["Value"], is_j.outputs["Value"], (4440, -360))
    contact_sel = _bool(tree, "AND", contact_valid.outputs["Value"], contact_pair.outputs["Boolean"], (4620, -360))
    store_contact = _node(tree, "GeometryNodeStoreNamedAttribute", (4620, 300))
    store_contact.data_type = "FLOAT"
    store_contact.domain = "POINT"
    store_contact.inputs["Name"].default_value = "pnw_contact"
    store_contact.inputs["Value"].default_value = 1.0
    tree.links.new(store_vel.outputs["Geometry"], store_contact.inputs["Geometry"])
    tree.links.new(contact_sel.outputs["Boolean"], store_contact.inputs["Selection"])
    store_support = _node(tree, "GeometryNodeStoreNamedAttribute", (4800, 300))
    store_support.data_type = "BOOLEAN"
    store_support.domain = "POINT"
    store_support.inputs["Name"].default_value = "pnw_contact_support"
    tree.links.new(store_contact.outputs["Geometry"], store_support.inputs["Geometry"])
    tree.links.new(support_acc.outputs["Boolean"], store_support.inputs["Value"])
    tree.links.new(contact_pair.outputs["Boolean"], store_support.inputs["Selection"])
    tree.links.new(store_support.outputs["Geometry"], inner_out.inputs["Geometry"])

    candidate_tag = _named(tree, "pnw_contact_candidate", "BOOLEAN", (4620, 700))
    remove_candidates = _node(tree, "GeometryNodeDeleteGeometry", (4800, 700))
    remove_candidates.domain = "POINT"
    tree.links.new(inner_out.outputs["Geometry"], remove_candidates.inputs["Geometry"])
    tree.links.new(candidate_tag.outputs["Attribute"], remove_candidates.inputs["Selection"])

    mesh_node = _node(tree, "GeometryNodeGroup", (5000, 300))
    mesh_node.node_tree = mesh_group
    tree.links.new(remove_candidates.outputs["Geometry"], mesh_node.inputs["Geometry"])
    tree.links.new(gin.outputs["Collider Geometry"], mesh_node.inputs["Collider Geometry"])
    tree.links.new(velocity.outputs["Attribute"], mesh_node.inputs["Velocity"])
    tree.links.new(gin.outputs["Radius"], mesh_node.inputs["Radius"])
    tree.links.new(gin.outputs["Restitution"], mesh_node.inputs["Restitution"])
    tree.links.new(gin.outputs["Friction"], mesh_node.inputs["Friction"])
    tree.links.new(gin.outputs["Sleep Velocity"], mesh_node.inputs["Sleep Velocity"])
    ground_out = _add_ground_response(
        tree,
        mesh_node.outputs["Geometry"],
        gin.outputs["Ground Z"],
        gin.outputs["Radius"],
        gin.outputs["Restitution"],
        gin.outputs["Friction"],
        gin.outputs["Sleep Velocity"],
        (5200, 300),
    )
    tree.links.new(ground_out, gout.inputs["Geometry"])
    return tree


def _add_sweep_call(tree, sweep_group, geometry, collider_geometry, gin, loc=(0.0, 0.0)):
    node = _node(tree, "GeometryNodeGroup", loc)
    node.node_tree = sweep_group
    tree.links.new(geometry, node.inputs["Geometry"])
    tree.links.new(collider_geometry, node.inputs["Collider Geometry"])
    tree.links.new(gin.outputs["Radius"], node.inputs["Radius"])
    tree.links.new(gin.outputs["Restitution"], node.inputs["Restitution"])
    tree.links.new(gin.outputs["Friction"], node.inputs["Friction"])
    tree.links.new(gin.outputs["Sleep Velocity"], node.inputs["Sleep Velocity"])
    tree.links.new(gin.outputs["Ground Z"], node.inputs["Ground Z"])
    return node


def build_sphere_proxy_contact(tree, mesh_group):
    """Build the base + bounded residual continuation contact graph."""
    _socket(tree, "Geometry", "INPUT", "NodeSocketGeometry")
    _socket(tree, "Collider Geometry", "INPUT", "NodeSocketGeometry")
    _socket(tree, "Radius", "INPUT", "NodeSocketFloat", 0.12)
    _socket(tree, "Restitution", "INPUT", "NodeSocketFloat", 0.35)
    _socket(tree, "Friction", "INPUT", "NodeSocketFloat", 0.45)
    _socket(tree, "Sleep Velocity", "INPUT", "NodeSocketFloat", 0.08)
    _socket(tree, "Ground Z", "INPUT", "NodeSocketFloat", 0.0)
    _socket(tree, "Contact Iterations", "INPUT", "NodeSocketInt", 3)
    _socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")
    gin = _node(tree, "NodeGroupInput", (-1400, 0))
    gout = _node(tree, "NodeGroupOutput", (1800, 0))
    sweep_group = _build_sweep_group(mesh_group)

    base_in = _node(tree, "GeometryNodeRepeatInput", (-1200, 300))
    base_out = _node(tree, "GeometryNodeRepeatOutput", (200, 300))
    base_in.pair_with_output(base_out)
    tree.links.new(gin.outputs["Contact Iterations"], base_in.inputs["Iterations"])
    tree.links.new(gin.outputs["Geometry"], base_in.inputs["Geometry"])
    base_sweep = _add_sweep_call(
        tree,
        sweep_group,
        base_in.outputs["Geometry"],
        gin.outputs["Collider Geometry"],
        gin,
        (-900, 300),
    )
    tree.links.new(base_sweep.outputs["Geometry"], base_out.inputs["Geometry"])

    base_residual = _add_residual(tree, base_out.outputs["Geometry"], gin.outputs["Radius"], (400, 500))
    tol = _math(tree, "ADD", INTERNAL_RESIDUAL_TOL, 1e-12, (400, 340))
    needs_extra = _math(tree, "GREATER_THAN", base_residual, tol.outputs["Value"], (580, 500))
    extra_iterations = _node(tree, "GeometryNodeSwitch", (760, 500))
    extra_iterations.input_type = "INT"
    tree.links.new(needs_extra.outputs["Value"], extra_iterations.inputs["Switch"])
    extra_iterations.inputs["False"].default_value = 0
    extra_iterations.inputs["True"].default_value = MAX_EXTRA_SWEEPS

    extra_in = _node(tree, "GeometryNodeRepeatInput", (760, 300))
    extra_out = _node(tree, "GeometryNodeRepeatOutput", (1500, 300))
    extra_in.pair_with_output(extra_out)
    tree.links.new(extra_iterations.outputs["Output"], extra_in.inputs["Iterations"])
    tree.links.new(base_out.outputs["Geometry"], extra_in.inputs["Geometry"])
    current_residual = _add_residual(tree, extra_in.outputs["Geometry"], gin.outputs["Radius"], (900, 700))
    converged = _math(tree, "LESS_THAN", current_residual, tol.outputs["Value"], (1100, 700))
    extra_sweep = _add_sweep_call(
        tree,
        sweep_group,
        extra_in.outputs["Geometry"],
        gin.outputs["Collider Geometry"],
        gin,
        (1100, 300),
    )
    bypass = _node(tree, "GeometryNodeSwitch", (1300, 300))
    bypass.input_type = "GEOMETRY"
    tree.links.new(converged.outputs["Value"], bypass.inputs["Switch"])
    tree.links.new(extra_sweep.outputs["Geometry"], bypass.inputs["False"])
    tree.links.new(extra_in.outputs["Geometry"], bypass.inputs["True"])
    tree.links.new(bypass.outputs["Output"], extra_out.inputs["Geometry"])

    final_geometry = extra_out.outputs["Geometry"]
    final_residual = _add_residual(tree, final_geometry, gin.outputs["Radius"], (1500, 700))
    final_converged = _math(tree, "LESS_THAN", final_residual, tol.outputs["Value"], (1500, 500))
    store_residual = _node(tree, "GeometryNodeStoreNamedAttribute", (1650, 300))
    store_residual.data_type = "FLOAT"
    store_residual.domain = "POINT"
    store_residual.inputs["Name"].default_value = "pnw_contact_residual"
    tree.links.new(final_geometry, store_residual.inputs["Geometry"])
    tree.links.new(final_residual, store_residual.inputs["Value"])
    store_converged = _node(tree, "GeometryNodeStoreNamedAttribute", (1800, 300))
    store_converged.data_type = "BOOLEAN"
    store_converged.domain = "POINT"
    store_converged.inputs["Name"].default_value = "pnw_contact_converged"
    tree.links.new(store_residual.outputs["Geometry"], store_converged.inputs["Geometry"])
    tree.links.new(final_converged.outputs["Value"], store_converged.inputs["Value"])
    tree.links.new(store_converged.outputs["Geometry"], gout.inputs["Geometry"])
    return tree
