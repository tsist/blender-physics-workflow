"""Managed Mantaflow caches for explicitly selected native fluid domains."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
from time import perf_counter
import uuid

import bpy

from . import animation_identity, identity
from .bake import _atomic_write_json, _sha256_file


STATE_PROP = "pnw_native_fluid_state"
SCHEMA = "pnw.native-fluid/1"
_DOMAIN_TYPES = {"native_liquid": "LIQUID", "native_gas": "GAS"}
_TRANSFORM_PROPS = {
    "location", "rotation_euler", "rotation_quaternion", "scale",
    "delta_location", "delta_rotation_euler", "delta_rotation_quaternion", "delta_scale",
}
_SUPPORTED_MESH_MODIFIERS = {
    "BEVEL", "SOLIDIFY", "SUBSURF", "TRIANGULATE", "WEIGHTED_NORMAL",
}
_CACHE_SETTINGS = {
    "cache_type", "cache_frame_start", "cache_frame_end", "cache_frame_offset",
    "cache_data_format", "cache_noise_format", "cache_mesh_format",
    "cache_resumable",
}
_CACHE_COMPONENTS = ("data", "mesh", "noise", "particles", "guide")
_BAKED_FLAGS = tuple("has_cache_baked_" + item for item in _CACHE_COMPONENTS)
_PAUSE_FRAMES = tuple("cache_frame_pause_" + item for item in _CACHE_COMPONENTS)
_FRAME_NAME = re.compile(r"^(?:config|fluid_[A-Za-z_]+)_([0-9]+)\.")
_UUID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class NativeFluidError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code: str, message: str):
    raise NativeFluidError(code, message)


def resolve_domain(obj, req):
    """Return the explicit native modifier and domain settings without changing the scene."""
    if bpy.app.version < (5, 3, 0) or not bpy.app.build_options.fluid:
        _fail("NATIVE_FLUID_HOST", "Managed Mantaflow requires Blender 5.3+ with fluid support.")
    context = bpy.context
    if obj.type != "MESH" or context.view_layer.objects.get(obj.name) != obj:
        _fail("NATIVE_FLUID_DOMAIN", "The fluid domain must be a mesh in the current view layer.")
    mod = obj.modifiers.get(req["modifier_name"])
    if mod is None or mod.type != "FLUID" or mod.fluid_type != "DOMAIN" or not mod.domain_settings:
        _fail("NATIVE_FLUID_DOMAIN", f"'{req['modifier_name']}' is not a native FLUID domain on '{obj.name}'.")
    settings = mod.domain_settings
    if _DOMAIN_TYPES.get(req["preset_id"]) != settings.domain_type:
        _fail("NATIVE_FLUID_DOMAIN", "The native preset and selected domain type must match.")
    if not mod.show_viewport or obj.hide_viewport or obj.hide_get():
        _fail("NATIVE_FLUID_DOMAIN", "The selected native domain must be enabled in the current view layer.")
    return mod, settings


def _scalar_payload(rna, prefix, animated, *, domain=False, ignore=()):
    result = {}
    for prop in rna.bl_rna.properties:
        name = prop.identifier
        if (prop.is_readonly or name == "rna_type" or name in ignore
                or prop.type not in {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"}):
            continue
        if domain:
            if name.startswith(("has_cache_", "is_cache_")):
                continue
            if name.startswith("cache_") and name not in _CACHE_SETTINGS:
                continue
        value = getattr(rna, name)
        if getattr(prop, "is_array", False):
            result[name] = {str(i): v for i, v in enumerate(value) if (prefix + name, i) not in animated}
        elif (prefix + name, 0) not in animated:
            result[name] = sorted(value) if isinstance(value, set) else value
    return result


def _animation(obj, settings):
    data = obj.animation_data
    if not data:
        return {}, set()
    if data.drivers or data.nla_tracks:
        _fail("NATIVE_FLUID_DEPENDENCY", f"Drivers and NLA on '{obj.name}' are outside managed native baking.")
    if not data.action:
        return {}, set()
    role_prefix = settings.path_from_id() + "."
    fcurves = []
    if data.action.is_action_layered:
        if not data.action_slot or len(data.action.layers) != 1 or len(data.action.layers[0].strips) != 1:
            _fail("NATIVE_FLUID_DEPENDENCY", f"'{obj.name}' requires one action layer and one keyframe strip.")
        bag = data.action.layers[0].strips[0].channelbag(data.action_slot)
        if not bag:
            _fail("NATIVE_FLUID_DEPENDENCY", f"'{obj.name}' action has no channel bag for its bound slot.")
        fcurves = bag.fcurves
    else:
        fcurves = data.action.fcurves
    channels = set()
    curves = []
    for curve in fcurves:
        path = curve.data_path
        if path not in _TRANSFORM_PROPS and path != role_prefix + "use_inflow":
            _fail("NATIVE_FLUID_DEPENDENCY", f"Unsupported native animation channel on '{obj.name}': {path}")
        if curve.modifiers or curve.sampled_points:
            _fail("NATIVE_FLUID_DEPENDENCY", f"Keyframe curve modifiers and sampled animation on '{obj.name}' require a separate bake.")
        key = (path, curve.array_index)
        if key in channels or curve.mute or (curve.group and curve.group.mute):
            _fail("NATIVE_FLUID_DEPENDENCY", f"'{obj.name}' has a muted or repeated native animation channel.")
        channels.add(key)
        curves.append({
            "path": path,
            "index": curve.array_index,
            "extrapolation": curve.extrapolation,
            "keys": [
                (tuple(point.co), point.interpolation, tuple(point.handle_left),
                 tuple(point.handle_right), point.easing, point.amplitude, point.back, point.period)
                for point in curve.keyframe_points
            ],
        })
    return curves, channels


def _object_payload(obj, role_modifier, settings):
    if obj.type != "MESH" or obj.data.shape_keys or obj.constraints or obj.rigid_body:
        _fail("NATIVE_FLUID_DEPENDENCY", f"Use a static mesh or keyframed transform for fluid input '{obj.name}'.")
    if obj.data.animation_data:
        _fail("NATIVE_FLUID_DEPENDENCY", f"Mesh animation on '{obj.name}' requires a separate bake.")
    parent_reasons = animation_identity.static_parent_reasons(obj)
    if parent_reasons:
        _fail("NATIVE_FLUID_DEPENDENCY", f"Unsupported fluid parent on '{obj.name}': {', '.join(parent_reasons)}")
    curves, animated = _animation(obj, settings)
    static_transform = {
        name: {str(i): value for i, value in enumerate(getattr(obj, name)) if (name, i) not in animated}
        for name in sorted(_TRANSFORM_PROPS)
    }
    trailing = False
    modifiers = []
    for mod in obj.modifiers:
        if mod == role_modifier:
            trailing = True
            continue
        if trailing or not (mod.show_viewport or mod.show_render):
            continue
        if mod.type not in _SUPPORTED_MESH_MODIFIERS:
            _fail("NATIVE_FLUID_DEPENDENCY", f"Apply or isolate the '{mod.type}' modifier on fluid input '{obj.name}'.")
        modifiers.append({"name": mod.name, "type": mod.type, "rna": _scalar_payload(mod, "", set())})
    return {
        "object": obj.name, "modifier": role_modifier.name, "mesh_sha256": identity.base_geometry_sha256(obj),
        "parents": animation_identity.static_parent_chain_payload(obj),
        "matrix_parent_inverse": obj.matrix_parent_inverse, "rotation_mode": obj.rotation_mode,
        "transform": static_transform, "animation": curves, "modifiers": modifiers,
        "enabled": (role_modifier.show_viewport, role_modifier.show_render, obj.hide_viewport),
        "settings": _scalar_payload(settings, settings.path_from_id() + ".", animated,
                                    domain=role_modifier.fluid_type == "DOMAIN",
                                    ignore=("name",)),
    }


def _dependency_payload(obj, mod, settings, req):
    scene = bpy.context.scene
    if scene.animation_data and (scene.animation_data.action or scene.animation_data.drivers
                                 or scene.animation_data.nla_tracks):
        _fail("NATIVE_FLUID_DEPENDENCY", "Animated scene settings are outside managed native baking.")
    if settings.use_guide and settings.guide_source == "DOMAIN":
        _fail("NATIVE_FLUID_DEPENDENCY", "A second FLUID domain used as a solver guide requires separate cache verification.")
    flows, effectors = [], []
    roles = (
        ("FLOW", settings.fluid_group, flows),
        ("EFFECTOR", settings.effector_group, effectors),
    )
    for role, group, results in roles:
        pool = group.all_objects if group else scene.objects
        for source in sorted(pool, key=lambda item: item.name):
            if source == obj:
                continue
            for source_mod in source.modifiers:
                if source_mod.type != "FLUID" or source_mod.fluid_type != role:
                    continue
                if not (source_mod.show_viewport or source_mod.show_render):
                    continue
                if role == "FLOW":
                    source_settings = source_mod.flow_settings
                    if not source_settings or ((source_settings.flow_type == "LIQUID")
                                               != (settings.domain_type == "LIQUID")):
                        continue
                    if source_settings.flow_source != "MESH" or source_settings.use_texture:
                        _fail("NATIVE_FLUID_DEPENDENCY", f"Use a mesh flow source without texture on '{source.name}'.")
                else:
                    source_settings = source_mod.effector_settings
                results.append(_object_payload(source, source_mod, source_settings))
    if not flows or not any(source["settings"].get("flow_behavior") != "OUTFLOW" for source in flows):
        _fail("NATIVE_FLUID_FLOW", "No compatible mesh inflow or volume source was found for this domain.")
    collider_name = req.get("collider_object")
    if collider_name and not any(item["object"] == collider_name for item in effectors):
        _fail("NATIVE_FLUID_COLLIDER", f"'{collider_name}' is not an enabled effector of the domain.")
    if req.get("collider_collection") and (not settings.effector_group
                                           or req["collider_collection"] != settings.effector_group.name):
        _fail("NATIVE_FLUID_COLLIDER", "Requested collider collection must match the domain's native effector collection.")
    weights = settings.effector_weights
    field_pool = weights.collection.all_objects if weights.collection else scene.objects
    for field in field_pool:
        if field.field and field.field.type != "NONE":
            _fail("NATIVE_FLUID_DEPENDENCY", f"Native force field '{field.name}' requires separate dependency verification.")
    return {
        "schema": SCHEMA, "host": (bpy.app.version_string, bpy.app.build_hash.decode()),
        "scene": scene.name, "gravity": scene.gravity, "use_gravity": scene.use_gravity,
        "fps": scene.render.fps, "fps_base": scene.render.fps_base,
        "domain": _object_payload(obj, mod, settings),
        "effector_weights": _scalar_payload(weights, "", set()),
        "fluid_group": settings.fluid_group.name if settings.fluid_group else None,
        "effector_group": settings.effector_group.name if settings.effector_group else None,
        "force_group": weights.collection.name if weights.collection else None,
        "flows": flows, "effectors": effectors,
    }


def _dependencies(obj, mod, settings, req):
    payload = _dependency_payload(obj, mod, settings, req)
    digest = hashlib.sha256(identity.canonical_json(payload).encode("utf-8")).hexdigest()
    return {"sha256": digest, "flows": [item["object"] for item in payload["flows"]],
            "effectors": [item["object"] for item in payload["effectors"]]}


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _fail("NATIVE_FLUID_CACHE_MANIFEST", f"Could not read native cache metadata: {path}")


def _read_state(obj, mod):
    raw = obj.get(STATE_PROP)
    if not raw:
        return None
    try:
        state = json.loads(raw)
        commit = state["commit"]
        valid_commit = commit is None or (
            _UUID.fullmatch(commit["generation"]) and _DIGEST.fullmatch(commit["manifest_sha256"])
        )
        if (state["schema"] == SCHEMA and state["object"] == obj.name and state["modifier"] == mod.name
                and _UUID.fullmatch(state["owner_uuid"]) and Path(state["owner_blend"]).is_absolute()
                and valid_commit and isinstance(state["detached"], bool)):
            return state
    except (TypeError, KeyError, ValueError):
        pass
    _fail("NATIVE_FLUID_OWNER", "The native cache binding must match the original object and modifier names.")


def _safe_path(path):
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NativeFluidError("NATIVE_FLUID_CACHE_PATH", f"Cannot inspect cache path: {part}") from exc
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            _fail("NATIVE_FLUID_CACHE_PATH", f"Native cache path may not contain links or reparse points: {part}")
    return path.resolve()


def _native_path(settings):
    raw = settings.cache_directory
    return _safe_path(Path(bpy.path.abspath(raw))) if raw else None


def assert_isolated_output(path):
    """Resolve an output path that cannot reside inside a native cache or generation."""
    path = _safe_path(Path(path))
    if path == path.parent:
        _fail("NATIVE_FLUID_CACHE_PATH", "A dedicated native output directory is required.")
    for ancestor in (path, *path.parents):
        if ancestor.name.lower().startswith("txn-") and _UUID.fullmatch(ancestor.name[4:].lower()):
            _fail("NATIVE_FLUID_CACHE_PATH", f"Cannot write within an existing native generation: {ancestor}")
        marker = ancestor / "owner.json"
        if marker.is_file() and marker.stat().st_size < 16384:
            try:
                owner = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                owner = None
            if isinstance(owner, dict) and owner.get("schema") == SCHEMA:
                _fail("NATIVE_FLUID_CACHE_PATH", f"Cannot write within an existing native generation: {ancestor}")
    seen = set()
    for scene in bpy.data.scenes:
        for other in scene.objects:
            if other.as_pointer() in seen:
                continue
            seen.add(other.as_pointer())
            for modifier in other.modifiers:
                if modifier.type != "FLUID" or modifier.fluid_type != "DOMAIN":
                    continue
                settings = modifier.domain_settings
                if not settings or not settings.cache_directory:
                    continue
                active_path = Path(bpy.path.abspath(settings.cache_directory, library=other.library)).resolve()
                if path.is_relative_to(active_path):
                    _fail("NATIVE_FLUID_CACHE_PATH", f"Cannot write inside the active FLUID cache of '{other.name}'.")
    return path


def _root(req):
    if not bpy.data.filepath or not bpy.data.is_saved:
        _fail("NATIVE_FLUID_WORK_COPY", "Save an editable work copy of the blend before managing a native cache.")
    blend = Path(bpy.data.filepath).resolve()
    raw = req.get("bake_directory")
    root = Path(bpy.path.abspath(raw)) if raw else blend.parent / (blend.stem + ".pnw-fluid")
    return assert_isolated_output(root), blend


def _require_editable(obj, settings):
    if obj.library or obj.override_library or obj.mode != "OBJECT":
        _fail("NATIVE_FLUID_WORK_COPY", "Use a local, editable native domain in Object Mode.")
    if settings.is_cache_baking_any:
        _fail("NATIVE_FLUID_BAKE", "Wait for the active Blender fluid cache job to finish.")


def _preflight_write(obj, mod, settings, req):
    _require_editable(obj, settings)
    if settings.cache_type != "ALL":
        _fail("NATIVE_FLUID_CACHE_TYPE", "Managed native baking currently requires Blender's All cache type.")
    if settings.cache_frame_offset != 0:
        _fail("NATIVE_FLUID_CACHE_RANGE", "Set native cache frame offset to 0 before managed baking.")
    if settings.domain_type == "LIQUID" and not settings.use_mesh:
        _fail("NATIVE_FLUID_LIQUID_MESH", "Enable the native liquid surface mesh before managed baking.")
    if settings.domain_type == "GAS" and settings.cache_data_format != "OPENVDB":
        _fail("NATIVE_FLUID_GAS_FORMAT", "Use OpenVDB data caching for the managed gas density workflow.")
    start = settings.cache_frame_start
    end = settings.cache_frame_end
    if not 1 <= start <= end or (start, end) != (req["parameters"]["frame_start"], req["parameters"]["frame_end"]):
        _fail("NATIVE_FLUID_CACHE_RANGE", "Use the domain's positive native cache start and end frames.")
    state = _read_state(obj, mod)
    path = _native_path(settings)
    if state and state.get("commit"):
        _committed(obj, mod, settings, state)
    elif _has_native_files(path) or (settings.has_cache_baked_data and not state):
        _fail("NATIVE_FLUID_EXTERNAL_CACHE", "Existing unmanaged native caches are read-only to Physics Nodes Workbench.")
    return state


def _has_native_files(path):
    return bool(path and any(any((path / name).glob("*")) for name in ("config", "data", "mesh", "noise")))


def _new_state(obj, mod, blend):
    return {"schema": SCHEMA, "object": obj.name, "modifier": mod.name, "owner_uuid": uuid.uuid4().hex,
            "owner_blend": str(blend), "commit": None, "detached": False}


def _save_state(obj, state):
    obj[STATE_PROP] = json.dumps(state, sort_keys=True, separators=(",", ":"))


def _record_file(file, cache):
    path = _safe_path(file)
    if not path.is_file() or not path.is_relative_to(cache):
        _fail("NATIVE_FLUID_CACHE_FILE", f"Unsafe or missing native cache file: {file}")
    size = path.stat().st_size
    if not size:
        _fail("NATIVE_FLUID_CACHE_FILE", f"Empty native cache file: {file}")
    return {"path": path.relative_to(cache).as_posix(), "size": size, "sha256": _sha256_file(path)}


def _frame_files(cache, settings):
    start, end = settings.cache_frame_start, settings.cache_frame_end
    by_frame = {frame: {"files": []} for frame in range(start, end + 1)}
    main_component = "mesh" if settings.domain_type == "LIQUID" else "data"
    for file in sorted(cache.rglob("*")):
        match = _FRAME_NAME.match(file.name)
        if not match or not file.is_file():
            continue
        frame = int(match.group(1))
        if frame not in by_frame:
            continue
        row = by_frame[frame]
        item = _record_file(file, cache)
        row["files"].append(item)
        if (file.parent.name == main_component
                and file.name.startswith("fluid_mesh_" if main_component == "mesh" else "fluid_data_")):
            row["primary"] = item["path"]
    for frame, row in by_frame.items():
        prefixes = {entry["path"].split("/", 1)[0] for entry in row["files"]}
        if "primary" not in row or not {"config", "data"} <= prefixes:
            _fail("NATIVE_FLUID_CACHE_FRAME", f"Native operator returned incomplete output at frame {frame}.")
        if settings.domain_type == "GAS" and not row["primary"].endswith(".vdb"):
            _fail("NATIVE_FLUID_GAS_FORMAT", "The managed gas cache did not produce a VDB for each frame.")
    return by_frame


def _archive_files(cache, archive, frames):
    """Preserve native output independently of the directory Blender can clear."""
    archive.mkdir()
    size = 0
    count = 0
    for row in frames.values():
        for entry in row["files"]:
            target = archive / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            source = _safe_path(cache / entry["path"])
            shutil.copyfile(source, target)
            if source.samefile(target) or _record_file(target, archive) != entry:
                _fail("NATIVE_FLUID_ARCHIVE", f"Native cache archive verification failed: {entry['path']}")
            size += entry["size"]
            count += 1
    return {"directory": "archive", "bytes": size, "files": count}


def _checked_frames(directory, frames, start, end):
    files = {}
    for frame in range(start, end + 1):
        row = frames.get(str(frame))
        if not row or not row.get("files"):
            _fail("NATIVE_FLUID_CACHE_FRAME", f"No complete committed native cache for frame {frame}.")
        for entry in row["files"]:
            file = directory / entry["path"]
            if _record_file(file, directory) != entry:
                _fail("NATIVE_FLUID_CACHE_FILE", f"Native cache file changed at frame {frame}: {entry['path']}")
        if not any(entry["path"] == row.get("primary") for entry in row["files"]):
            _fail("NATIVE_FLUID_CACHE_FRAME", f"No primary native cache for frame {frame}.")
        files[frame] = str(directory / row["primary"])
    return files


def _committed(obj, mod, settings, state):
    commit = state.get("commit") if state else None
    if not commit:
        _fail("NATIVE_FLUID_CACHE_REQUIRED", "A committed managed cache is required for this native fluid operation.")
    cache = _native_path(settings)
    generation = commit.get("generation")
    if not cache or cache.name != "cache" or cache.parent.name != f"txn-{generation}":
        _fail("NATIVE_FLUID_CACHE_PATH", "The domain cache directory does not match its committed generation.")
    owner = _read_json(_safe_path(cache.parent / "owner.json"))
    expected_owner = {
        "schema": SCHEMA, "object": obj.name, "modifier": mod.name, "domain_type": settings.domain_type,
        "owner_uuid": state["owner_uuid"], "owner_blend": state["owner_blend"], "generation": generation,
    }
    if owner != expected_owner:
        _fail("NATIVE_FLUID_OWNER", "The generation is not owned by this native fluid binding.")
    manifest_path = _safe_path(cache.parent / "manifest.json")
    if not manifest_path.is_file() or _sha256_file(manifest_path) != commit.get("manifest_sha256"):
        _fail("NATIVE_FLUID_CACHE_MANIFEST", "The committed native cache manifest is missing or changed.")
    manifest = _read_json(manifest_path)
    if manifest.get("owner") != owner or not isinstance(manifest.get("frames"), dict):
        _fail("NATIVE_FLUID_CACHE_MANIFEST", "The committed native cache manifest does not match its owner.")
    return cache, manifest


def _validated_sequence(obj, mod, settings, req, start, end, domain_type, *, binding=None, signature=None):
    if domain_type and settings.domain_type != domain_type:
        _fail("NATIVE_FLUID_DOMAIN", "Selected fluid domain has the wrong type for this export.")
    if not settings.cache_frame_start <= start <= end <= settings.cache_frame_end:
        _fail("NATIVE_FLUID_CACHE_RANGE", "Requested frames are outside the native cache range.")
    if binding is None:
        state = _read_state(obj, mod)
        cache, manifest = _committed(obj, mod, settings, state)
    else:
        state, cache, manifest = binding
    if (not settings.has_cache_baked_data
            or (settings.domain_type == "LIQUID" and not settings.has_cache_baked_mesh)):
        _fail("NATIVE_FLUID_CACHE_REQUIRED", "Blender has not loaded the expected native cache components.")
    evidence = signature or _dependencies(obj, mod, settings, req)
    if evidence["sha256"] != manifest.get("dependency_sha256"):
        _fail("NATIVE_FLUID_CACHE_STALE", "Native fluid settings, source animation, or geometry changed after baking.")
    return {
        "cache_directory": str(cache), "frame_files": _checked_frames(cache, manifest["frames"], start, end),
        "domain_type": settings.domain_type,
        "generation": state["commit"]["generation"], "manifest_sha256": state["commit"]["manifest_sha256"],
    }


def require_cached_sequence(obj, req, start, end, domain_type=None):
    """Verify settings, ownership and each file in a subset of a committed cache."""
    mod, settings = resolve_domain(obj, req)
    return _validated_sequence(obj, mod, settings, req, start, end, domain_type)


def require_cached_frame(obj, req, frame, domain_type=None):
    proof = require_cached_sequence(obj, req, frame, frame, domain_type)
    proof["frame_file"] = proof["frame_files"][frame]
    proof["frame"] = frame
    return proof


def _describe(obj, mod, settings, req):
    state = _read_state(obj, mod)
    cache = _native_path(settings)
    result = {
        "domain_type": settings.domain_type, "cache_type": settings.cache_type,
        "frame_start": settings.cache_frame_start, "frame_end": settings.cache_frame_end,
        "resolution": settings.resolution_max, "cache_directory": str(cache) if cache else None,
        "cache_state": "unbaked", "cache_valid": False, "generation": None,
        "dependency_error": None, "flows": [], "effectors": [], "archive_directory": None,
    }
    if state and state.get("retained"):
        result["retained"] = state["retained"]
    signature = None
    try:
        signature = _dependencies(obj, mod, settings, req)
        result.update(flows=signature["flows"], effectors=signature["effectors"])
    except NativeFluidError as exc:
        result["dependency_error"] = {"code": exc.code, "message": exc.message}
    if state and state.get("commit"):
        result["generation"] = state["commit"]["generation"]
        try:
            live, manifest = _committed(obj, mod, settings, state)
            archive = manifest.get("archive")
            if archive and archive.get("directory") == "archive":
                result["archive_directory"] = str(_safe_path(live.parent / "archive"))
                result["archive_bytes"] = archive["bytes"]
            result.update(_validated_sequence(obj, mod, settings, req, settings.cache_frame_start,
                                              settings.cache_frame_end, settings.domain_type,
                                              binding=(state, live, manifest), signature=signature))
            result.update(cache_state="valid", cache_valid=True)
        except NativeFluidError as exc:
            result.update(cache_state="stale", cache_error={"code": exc.code, "message": exc.message})
    elif state and state.get("detached"):
        result["cache_state"] = "detached"
    elif _has_native_files(cache) or settings.has_cache_baked_data:
        result["cache_state"] = "external"
    return result


def _redirect(settings, cache):
    settings.cache_directory = str(cache)
    if _native_path(settings) != cache:
        _fail("NATIVE_FLUID_CACHE_PATH", "Blender did not accept the new native cache path.")


def _set_flags(settings, flags):
    for key, value in flags.items():
        setattr(settings, key, value)


def _cache_progress(settings):
    return {name: getattr(settings, name) for name in (*_BAKED_FLAGS, *_PAUSE_FRAMES)}


def _bake(obj, mod, settings, req):
    previous = _preflight_write(obj, mod, settings, req)
    root, blend = _root(req)
    source_state = previous if previous and previous["owner_blend"] == str(blend) else _new_state(obj, mod, blend)
    if previous and previous.get("retained"):
        source_state = {**source_state, "retained": previous["retained"]}
    signature = _dependencies(obj, mod, settings, req)
    generation = uuid.uuid4().hex
    transaction = root / f"txn-{generation}"
    cache = transaction / "cache"
    owner = {key: source_state[key] for key in ("schema", "object", "modifier", "owner_uuid", "owner_blend")}
    owner.update(generation=generation, domain_type=settings.domain_type)
    try:
        root.mkdir(parents=True, exist_ok=True)
        transaction.mkdir()
        cache.mkdir()
        _atomic_write_json(transaction / "owner.json", owner)
    except OSError as exc:
        _fail("NATIVE_FLUID_CACHE_PATH", f"Cannot allocate native cache generation: {exc}")
    context = bpy.context
    scene = context.scene
    view_layer = context.view_layer
    before_path = settings.cache_directory
    before_flags = _cache_progress(settings)
    before_frame = scene.frame_current
    before_subframe = scene.frame_subframe
    before_active = view_layer.objects.active
    before_selected = obj.select_get()
    started = perf_counter()
    try:
        _redirect(settings, cache)
        # Native Bake All uses the pause counter even if the baked flags have
        # been cleared. A new generation always starts at cache_frame_start.
        _set_flags(settings, dict.fromkeys(_PAUSE_FRAMES, 0))
        view_layer.objects.active = obj
        obj.select_set(True)
        with context.temp_override(object=obj, active_object=obj, selected_objects=[obj],
                                   selected_editable_objects=[obj]):
            if settings.has_cache_baked_data or settings.has_cache_baked_mesh:
                # Blender 5.3 carries baked flags across cache-directory changes.
                # Clear these only while pointed at the new, empty owned generation.
                if any(cache.iterdir()) or not bpy.ops.fluid.free_all.poll():
                    _fail("NATIVE_FLUID_BAKE", "Native bake flags cannot be reset in the newly owned cache.")
                if bpy.ops.fluid.free_all("EXEC_DEFAULT") != {"FINISHED"} or _native_path(settings) != cache:
                    _fail("NATIVE_FLUID_BAKE", "Blender failed to reset native bake flags.")
            scene.frame_set(settings.cache_frame_start)
            if not bpy.ops.fluid.bake_all.poll() or bpy.ops.fluid.bake_all("EXEC_DEFAULT") != {"FINISHED"}:
                _fail("NATIVE_FLUID_BAKE", "Blender did not complete the native Bake All operation.")
        if not settings.has_cache_baked_data or (settings.domain_type == "LIQUID" and not settings.has_cache_baked_mesh):
            _fail("NATIVE_FLUID_BAKE", "Blender did not mark the expected native cache components as baked.")
        frames = _frame_files(cache, settings)
        if _dependencies(obj, mod, settings, req)["sha256"] != signature["sha256"]:
            _fail("NATIVE_FLUID_CACHE_STALE", "Native fluid dependencies changed while baking.")
        archive = transaction / "archive"
        archive_info = _archive_files(cache, archive, frames)
        manifest = transaction / "manifest.json"
        _atomic_write_json(manifest, {"owner": owner, "dependency_sha256": signature["sha256"],
                                      "frames": frames, "archive": archive_info})
        manifest_hash = _sha256_file(manifest)
        state = {**source_state, "commit": {"generation": generation, "manifest_sha256": manifest_hash},
                 "detached": False}
        _save_state(obj, state)
    except Exception as exc:
        settings.cache_directory = before_path
        _set_flags(settings, before_flags)
        if isinstance(exc, NativeFluidError):
            raise
        raise NativeFluidError("NATIVE_FLUID_BAKE", str(exc)) from exc
    finally:
        view_layer.objects.active = before_active
        obj.select_set(before_selected)
        scene.frame_set(before_frame, subframe=before_subframe)
    return {"cache_directory": str(cache), "archive_directory": str(archive),
            "archive_bytes": archive_info["bytes"], "generation": generation, "manifest_sha256": manifest_hash,
            "duration_seconds": perf_counter() - started, "frames": len(frames), "saved": False}


def _retune(obj, mod, settings, req):
    start, end = settings.cache_frame_start, settings.cache_frame_end
    prior_request = {**req, "parameters": {"frame_start": start, "frame_end": end}}
    state = _preflight_write(obj, mod, settings, prior_request)
    _dependencies(obj, mod, settings, prior_request)
    edits = {**req.get("native_settings", {}), "cache_frame_start": req["parameters"]["frame_start"],
             "cache_frame_end": req["parameters"]["frame_end"]}
    if edits.get("timesteps_min", settings.timesteps_min) > edits.get("timesteps_max", settings.timesteps_max):
        _fail("NATIVE_FLUID_PARAMETERS", "Minimum time steps cannot exceed the maximum")
    saved = {name: getattr(settings, name) for name in edits}
    root, blend = _root(req)
    current = state or _new_state(obj, mod, blend)
    changed = any(abs(saved[name] - value) > 1e-6 if isinstance(value, float) else saved[name] != value
                  for name, value in edits.items())
    previous_path, progress = settings.cache_directory, _cache_progress(settings)
    previous_state = obj.get(STATE_PROP)
    destination = root / f"detached-{uuid.uuid4().hex}" if changed else None
    try:
        if changed:
            if current.get("commit"):
                cache, manifest = _committed(obj, mod, settings, state)
                retained = {**state["commit"], "cache_directory": str(cache)}
                if (manifest.get("archive") or {}).get("directory") == "archive":
                    retained["archive_directory"] = str(_safe_path(cache.parent / "archive"))
                current = {**current, "retained": retained}
            _redirect(settings, destination)
            _set_flags(settings, dict.fromkeys(_BAKED_FLAGS, False))
            _set_flags(settings, dict.fromkeys(_PAUSE_FRAMES, 0))
            for name, value in edits.items():
                setattr(settings, name, value)
            actual = {name: getattr(settings, name) for name in edits}
            rejected = [name for name, value in edits.items() if
                        (abs(actual[name] - value) > 1e-6 if isinstance(value, float) else actual[name] != value)]
            if rejected:
                _fail("NATIVE_FLUID_PARAMETERS", f"Blender did not accept these settings: {', '.join(rejected)}")
            _dependencies(obj, mod, settings, req)
            current = {**current, "commit": None, "detached": True}
        _save_state(obj, current)
        inspection = _describe(obj, mod, settings, req)
    except Exception:
        try:
            if changed and settings.cache_directory != previous_path:
                try:
                    for name, value in saved.items():
                        if getattr(settings, name) != value:
                            setattr(settings, name, value)
                finally:
                    settings.cache_directory = previous_path
                    _set_flags(settings, progress)
        finally:
            if previous_state is not None:
                obj[STATE_PROP] = previous_state
            elif STATE_PROP in obj:
                del obj[STATE_PROP]
        raise
    return {"workflow": {"adopted": True, "owner_uuid": current["owner_uuid"],
                         "settings": {name: getattr(settings, name) for name in edits}},
            "inspection": inspection}


def execute(obj, req):
    """Dispatch a normalized, explicitly selected native fluid operation."""
    mod, settings = resolve_domain(obj, req)
    op = req["operation"]
    if op == "inspect":
        return {"inspection": _describe(obj, mod, settings, req)}
    if op == "bake":
        result = _bake(obj, mod, settings, req)
        return {"bake": result, "inspection": _describe(obj, mod, settings, req)}
    if op == "parameters":
        return _retune(obj, mod, settings, req)
    if op in {"build", "adopt", "update"}:
        state = _preflight_write(obj, mod, settings, req)
        _dependencies(obj, mod, settings, req)
        _, blend = _root(req)
        if not state:
            state = _new_state(obj, mod, blend)
            _save_state(obj, state)
        return {"workflow": {"adopted": True, "domain_type": settings.domain_type,
                             "owner_uuid": state["owner_uuid"]},
                "inspection": _describe(obj, mod, settings, req)}
    if op == "invalidate_cache":
        _require_editable(obj, settings)
        state = _read_state(obj, mod)
        cache, manifest = _committed(obj, mod, settings, state)
        root, _ = _root(req)
        destination = root / f"detached-{uuid.uuid4().hex}"
        previous = settings.cache_directory
        previous_flags = _cache_progress(settings)
        try:
            _redirect(settings, destination)
            _set_flags(settings, dict.fromkeys(_BAKED_FLAGS, False))
            _set_flags(settings, dict.fromkeys(_PAUSE_FRAMES, 0))
            _save_state(obj, {**state, "commit": None, "detached": True})
        except Exception:
            settings.cache_directory = previous
            _set_flags(settings, previous_flags)
            raise
        archive = manifest.get("archive")
        return {"invalidate": {"detached": True, "retained_directory": str(cache),
                               "archive_directory": str(cache.parent / "archive") if archive else None},
                "inspection": _describe(obj, mod, settings, req)}
    _fail("NATIVE_FLUID_OPERATION", f"Operation '{op}' is unsupported for managed native fluid domains.")
