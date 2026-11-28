"""Managed Blender Cloth and Bullet adapters for the public physics request."""

from __future__ import annotations

import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import random
import shutil
import struct

import bpy

from .bake import _sha256_file


STATE = "pnw_native_point_cache"
DEFORM = {"ARMATURE", "CORRECTIVE_SMOOTH", "SUBSURF"}


def point_cache_path(path, *, blend=None):
    parent = Path(blend or bpy.data.filepath).parent
    _check(Path(path).anchor.lower() == parent.anchor.lower(), "Put the Cloth cache and .blend on the same drive")
    return "//" + Path(os.path.relpath(path, parent)).as_posix()


def resolved_point_cache(path):
    return Path(bpy.path.abspath(path)).resolve()


def load_cell_fracture():
    """Register the bundled upstream extension in the current Blender session."""
    from . import cell_fracture

    registered = hasattr(bpy.types, "OBJECT_OT_add_fracture_cell_objects")
    if not registered:
        cell_fracture.register()
    _check(hasattr(bpy.types, "OBJECT_OT_add_fracture_cell_objects"), "Could not register the Cell Fracture operator")
    return cell_fracture, not registered


def _check(condition, message):
    if not condition:
        raise ValueError(message)


def scene_object(name, *, mesh=True):
    _check(isinstance(name, str) and name, "Expected a scene object name")
    obj = bpy.context.view_layer.objects.get(name)
    _check(obj is not None and not obj.library and (not mesh or obj.type == "MESH"),
           f"Unavailable local {'mesh' if mesh else 'object'}: {name}")
    return obj


def _mapping(value, keys, label):
    _check(isinstance(value, dict) and value.keys() <= keys, f"Invalid {label} fields")
    return value


def _records(setup, key, default=()):
    value = setup.get(key, list(default))
    _check(isinstance(value, list), f"native_setup.{key} must be an array")
    return value


def _options(raw, target, fields):
    for key in raw.keys() & fields:
        prop = target.bl_rna.properties[key]
        value = raw[key]
        if prop.type == "BOOLEAN":
            good = type(value) is bool
        elif prop.type == "INT":
            good = type(value) is int and prop.hard_min <= value <= prop.hard_max
        elif prop.type == "FLOAT":
            good = type(value) in {int, float} and isfinite(value) and prop.hard_min <= value <= prop.hard_max
        else:
            good = isinstance(value, str) and value in prop.enum_items.keys()
        _check(good, f"Invalid {key}: {value!r}")
        setattr(target, key, value)


def reorder_after_deformation(obj, modifier):
    """Put a managed solver after deformation without reordering other modifiers."""
    stack = list(obj.modifiers)
    armatures = [mod for mod in stack if mod.type == "ARMATURE"]
    post = [mod for mod in stack if mod.type in DEFORM - {"ARMATURE"}]
    for armature in armatures:
        if post:
            first = min(obj.modifiers.find(mod.name) for mod in post)
            current = obj.modifiers.find(armature.name)
            if current > first:
                obj.modifiers.move(current, first)
    last = max((i for i, mod in enumerate(obj.modifiers) if mod.type in DEFORM), default=-1)
    current = obj.modifiers.find(modifier.name)
    if current < last:
        obj.modifiers.move(current, last)


def _solver_modifier(obj, name, kind):
    mod = obj.modifiers.get(name)
    if mod is None:
        matches = [item for item in obj.modifiers if item.type == kind]
        _check(not matches, f"{obj.name} already has {kind} modifier {matches[0].name if matches else ''}. Name it explicitly.")
        mod = obj.modifiers.new(name, kind)
    _check(mod.type == kind, f"{obj.name}/{name} is not a {kind} modifier")
    reorder_after_deformation(obj, mod)
    return mod


def _require_mesh_visibility(obj):
    _check(not obj.hide_viewport and not obj.hide_get(), f"Enable '{obj.name}' in the source view layer")


def _point_inventory(directory):
    _check(directory.is_dir(), f"Missing PointCache directory: {directory}")
    files = {}
    for path in sorted(directory.glob("*.bphys")):
        _check(path.is_file() and not path.is_symlink(), f"Invalid PointCache file: {path}")
        with path.open("rb") as stream:
            files[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return files


def _cloth(obj, request, start, end):
    setup = request["native_setup"]
    settings_fields = {"quality", "mass", "tension_stiffness", "compression_stiffness", "shear_stiffness",
                       "bending_stiffness", "use_sewing_springs", "sewing_force_max", "use_pressure",
                       "uniform_pressure_force"}
    collision_fields = {"use_self_collision", "distance_min", "self_distance_min", "collision_quality",
                        "self_friction", "friction"}
    layers = _records(setup, "layers", ({"object": obj.name},))
    _check(bool(layers), "Cloth requires at least one layer")
    names = []
    colliders = []
    for name in _records(setup, "colliders"):
        collider = scene_object(name)
        _require_mesh_visibility(collider)
        existing = [mod for mod in collider.modifiers if mod.type == "COLLISION"]
        _check(len(existing) <= 1, f"Use a single Collision modifier on '{name}'")
        mod = existing[0] if existing else collider.modifiers.new("PNW Native Collision", "COLLISION")
        reorder_after_deformation(collider, mod)
        colliders.append(collider)

    for index, raw in enumerate(layers):
        layer = _mapping(raw, {"object", "modifier_name", "pin_group"} | settings_fields | collision_fields, "cloth layer")
        garment = scene_object(layer.get("object"))
        _check(garment.name not in names, f"Duplicate cloth layer '{garment.name}'")
        _require_mesh_visibility(garment)
        mod = _solver_modifier(garment, layer.get("modifier_name", request["modifier_name"]), "CLOTH")
        cache = mod.point_cache
        _check(not cache.is_baked and not cache.use_external, f"Use an unbaked Cloth modifier on '{garment.name}'")
        _options(layer, mod.settings, settings_fields)
        _options({"use_self_collision": True, **layer}, mod.collision_settings, collision_fields)
        group = bpy.data.collections.new(f"PNW Cloth Contacts {garment.name}")
        bpy.context.scene.collection.children.link(group)
        for collider in colliders:
            _check(collider != garment, f"Cloth layer '{garment.name}' cannot be its own collider")
            group.objects.link(collider)
        mod.collision_settings.collection = group
        if "pin_group" in layer:
            _check(layer["pin_group"] in garment.vertex_groups, f"Missing pin group on '{garment.name}'")
            mod.settings.vertex_group_mass = layer["pin_group"]
        cache.frame_start, cache.frame_end, cache.frame_step = start, end, 1
        if request["operation"] == "bake":
            _bake_cloth(garment, mod, request, index, start, end)
        if index + 1 < len(layers):
            contacts = [entry for entry in garment.modifiers if entry.type == "COLLISION"]
            _check(len(contacts) <= 1, f"Use a single Collision modifier on '{garment.name}'")
            collision = contacts[0] if contacts else garment.modifiers.new("PNW Layer Collision", "COLLISION")
            from_index = garment.modifiers.find(collision.name)
            cloth_index = garment.modifiers.find(mod.name)
            if from_index < cloth_index:
                garment.modifiers.move(from_index, cloth_index)
            colliders.append(garment)
        names.append(garment.name)
    return names


def _bake_cloth(obj, mod, request, index, start, end):
    cache = mod.point_cache
    blend = Path(bpy.data.filepath)
    _check(blend.is_file(), "Save a working copy before Cloth baking")
    root = Path(request["bake_directory"])
    _check(root.is_absolute(), "Cloth bake requires a new absolute cache directory")
    root.mkdir(exist_ok=True)
    directory = root / f"cloth-{index:03}"
    directory.mkdir()
    native_directory = blend.parent / f"blendcache_{blend.stem}"
    cache.name = f"PNWCloth{index:03}"
    cache.use_disk_cache = True
    bpy.context.scene.frame_set(start)
    with bpy.context.temp_override(object=obj, active_object=obj, point_cache=cache):
        result = bpy.ops.ptcache.bake(bake=True)
    _check(result == {"FINISHED"} and cache.is_baked, f"Cloth bake failed on '{obj.name}'")
    files = sorted(native_directory.glob(f"{cache.name}_*.bphys"))
    _check(len(files) == end - start + 1, f"Cloth bake did not produce one disk file per frame: {obj.name}")
    for source in files:
        try:
            source.replace(directory / source.name)
        except OSError:
            shutil.copy2(source, directory / source.name)
    cache.filepath = point_cache_path(directory)
    cache.use_external = True


def _focus(obj, *, selected=None):
    for old in bpy.context.selected_objects:
        old.select_set(False)
    for member in selected if selected is not None else (obj,):
        member.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _fracture(raw):
    _mapping(raw, {"object", "source_limit", "recursion", "mass", "seed"}, "Cell Fracture")
    obj = scene_object(raw.get("object"))
    _require_mesh_visibility(obj)
    extension, registered = load_cell_fracture()
    before = set(bpy.context.scene.objects)
    _focus(obj)
    options = {key: raw[key] for key in ("source_limit", "recursion") if key in raw}
    state = random.getstate()
    try:
        random.seed(raw.get("seed", 0))
        _check(bpy.ops.object.add_fracture_cell_objects(source={"VERT_OWN"}, use_remove_original=False, **options) == {"FINISHED"},
               "Cell Fracture did not complete")
    finally:
        random.setstate(state)
        if registered:
            extension.unregister()
    shards = sorted((part for part in set(bpy.context.scene.objects) - before if part.type == "MESH"),
                    key=lambda part: part.name)
    _check(len(shards) >= 2, "Cell Fracture produced fewer than two mesh shards")
    obj.hide_render = True
    obj.hide_set(True)
    return [{"object": shard.name, "mass": raw.get("mass", 1.0), "collision_shape": "CONVEX_HULL"} for shard in shards]


def _rigid_body(obj, request, start, end):
    setup = request["native_setup"]
    bodies = _records(setup, "bodies", ({"object": obj.name},) if "fracture" not in setup else ())
    constraints = _records(setup, "constraints")
    if "fracture" in setup:
        bodies = [*bodies, *_fracture(setup["fracture"])]
    _check(bool(bodies), "Bullet requires at least one named body")
    body_fields = {"type", "collision_shape", "mesh_source", "mass", "friction", "restitution",
                   "use_margin", "collision_margin", "kinematic", "linear_damping", "angular_damping"}
    names, active = [], []
    for raw in bodies:
        raw = _mapping(raw, {"object"} | body_fields, "Bullet body")
        body = scene_object(raw.get("object"))
        _check(body.name not in names, f"Duplicate Bullet body '{body.name}'")
        _require_mesh_visibility(body)
        if not body.rigid_body:
            _focus(body)
            _check(bpy.ops.rigidbody.object_add() == {"FINISHED"}, f"Could not add Bullet body '{body.name}'")
        _options(raw, body.rigid_body, body_fields)
        names.append(body.name)
        if body.rigid_body.type == "ACTIVE" and not body.rigid_body.kinematic:
            active.append(body)
    for raw in constraints:
        limit = {"use_limit_ang_z", "limit_ang_z_lower", "limit_ang_z_upper"}
        fields = {"type", "use_breaking", "breaking_threshold", "disable_collisions"} | limit
        raw = _mapping(raw, {"object", "object1", "object2"} | fields, "Bullet constraint")
        anchor = scene_object(raw.get("object"), mesh=False)
        body_a, body_b = scene_object(raw.get("object1")), scene_object(raw.get("object2"))
        _check(body_a != body_b and set((body_a.name, body_b.name)) <= set(names),
               "Constraint endpoints must be distinct managed bodies")
        _check(raw.get("type", "FIXED") in {"FIXED", "HINGE"}, "Use FIXED or HINGE for a managed constraint")
        if not anchor.rigid_body_constraint:
            _focus(anchor)
            _check(bpy.ops.rigidbody.constraint_add() == {"FINISHED"}, f"Could not add constraint '{anchor.name}'")
        constraint = anchor.rigid_body_constraint
        constraint.object1, constraint.object2 = body_a, body_b
        _options(raw, constraint, fields)
    cache = bpy.context.scene.rigidbody_world.point_cache
    _check(not cache.is_baked, "Use an unbaked Bullet world")
    cache.frame_start, cache.frame_end = start, end
    if request["operation"] == "bake":
        _check(bool(active), "Bullet bake requires a dynamic body")
        _bake_bullet(active, start, end)
        if setup.get("alembic"):
            output = Path(request["bake_directory"]) / "bullet.abc"
            output.parent.mkdir(exist_ok=True)
            _check(not output.exists() and bpy.ops.wm.alembic_export(
                filepath=str(output), start=start, end=end, selected=True, as_background_job=False
            ) == {"FINISHED"} and output.is_file(), "Bullet Alembic export failed")
    return names


def _bake_bullet(bodies, start, end):
    """Read the Bullet cache in order and key transforms in background Blender."""
    scene = bpy.context.scene
    for body in bodies:
        _check(not body.parent and not body.constraints and not body.animation_data,
               f"Use an unparented simulation proxy without existing transform animation for '{body.name}'")
    sampled = []
    for frame in range(start, end + 1):
        scene.frame_set(frame)
        graph = bpy.context.evaluated_depsgraph_get()
        sampled.append([body.evaluated_get(graph).matrix_world.copy() for body in bodies])
    _focus(bodies[0], selected=bodies)
    _check(bpy.ops.rigidbody.objects_remove() == {"FINISHED"}, "Could not detach the baked Bullet bodies")
    for body in bodies:
        body.rotation_mode = "QUATERNION"
    for frame, matrices in enumerate(sampled, start):
        for body, matrix in zip(bodies, matrices):
            location, rotation, scale = matrix.decompose()
            rotation.make_compatible(body.rotation_quaternion)
            body.location, body.rotation_quaternion, body.scale = location, rotation, scale
            for channel in ("location", "rotation_quaternion", "scale"):
                _check(body.keyframe_insert(data_path=channel, frame=frame),
                       f"Could not key bake channel {body.name}/{channel}")


def sample(scene, names, start, end, *, cloth):
    observations = []
    for frame in (end, start, (start + end) // 2, end):
        scene.frame_set(frame)
        dg = bpy.context.evaluated_depsgraph_get()
        row = {}
        for name in names:
            evaluated = scene.objects[name].evaluated_get(dg)
            coords = hashlib.sha256()
            for values in evaluated.matrix_world:
                coords.update(struct.pack("<4f", *values))
            if cloth:
                for vertex in evaluated.data.vertices:
                    coords.update(struct.pack("<3f", *vertex.co))
            row[name] = coords.hexdigest()
        observations.append({"frame": frame, "samples": row})
    _check(observations[0] == observations[-1], "Native point-cache end frame changes on repeated seek")
    return observations


def verify(obj, request):
    state = json.loads(obj.get(STATE, "null"))
    _check(isinstance(state, dict) and state.get("bridge_mode") == request["bridge_mode"],
           f"No managed {request['bridge_mode']} setup on '{obj.name}'")
    cloth = state["bridge_mode"] == "CLOTH"
    scene = bpy.context.scene
    for name, expected in state["modifier_stacks"].items():
        _check(name in scene.objects and [mod.type for mod in scene.objects[name].modifiers] == expected,
               f"Saved modifier stack changed on '{name}'")
    if state["operation"] == "bake":
        for directory, files in state["files"].items():
            _check(_point_inventory(resolved_point_cache(directory)) == files, f"Changed native point cache: {directory}")
        actual = sample(bpy.context.scene, state["objects"], *state["frame_range"], cloth=cloth)
        _check(actual == state["observations"], "Cold-open evaluated native frame values differ from the bake")
        if "alembic_artifact" in state:
            abc = state["alembic_artifact"]
            path = Path(abc["path"])
            _check(path.is_file() and _sha256_file(path) == abc["sha256"], "Bullet Alembic delivery changed")
    return state


def apply_bridge(obj, request, collider_obj=None):
    mode = request["bridge_mode"]
    if mode == "GN_NATIVE":
        return {"bridge": {"bridge_mode": mode, "applied": False}}
    if request["operation"] == "inspect":
        return {"inspection": verify(obj, request)}

    scene = bpy.context.scene
    params = request["parameters"]
    start, end = params["frame_start"], params["frame_end"]
    solver = {"CLOTH": _cloth, "RIGID_BODY": _rigid_body}[mode]
    scene.frame_start, scene.frame_end = start, end
    scene.frame_set(start)
    names = solver(obj, request, start, end)
    stack_objects = names + (request["native_setup"].get("colliders", []) if mode == "CLOTH" else [])
    state = {"objects": names, "frame_range": [start, end], "bridge_mode": mode, "operation": request["operation"],
             "modifier_stacks": {name: [mod.type for mod in scene.objects[name].modifiers] for name in stack_objects}}
    if request["operation"] == "bake":
        root = Path(request["bake_directory"])
        state["files"] = ({point_cache_path(path): _point_inventory(path) for path in sorted(root.glob("cloth-*"))}
                          if mode == "CLOTH" else {})
        state["observations"] = sample(scene, names, start, end, cloth=mode == "CLOTH")
        abc = root / "bullet.abc"
        if abc.exists():
            state["alembic_artifact"] = {"path": str(abc), "sha256": _sha256_file(abc)}
    obj[STATE] = json.dumps(state, sort_keys=True)
    return {"workflow": state}

