"""Editable Blender 5.3 GAS density guides and independently readable Fog VDB."""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import stat
import uuid

import bpy
import numpy as np

from .host_compat import is_supported_host
from .vdb_export import VdbExportError, _openvdb, _sha256_file


_ROLE = "pnw_native_density_role"
_DOMAIN = "pnw_native_density_domain"
_SOURCE = "pnw_native_density_source"
_BYPASS = "pnw_native_density_bypass"
_EXIT = "pnw_native_density_exit"
_INSPECTION = "pnw_native_density_inspection"
_STRENGTH = "pnw_native_density_strength"
_MODIFIER = "PNW Density Guide"
_MIN_DENSITY = 1e-6
_MAX_FRAME_VOXELS = 4_000_000
_MAX_BOX_VOXELS = 16_777_216
_MAX_FRAMES = 500
_SEQUENCE_PROPERTIES = ("filepath", "is_sequence", "frame_start", "frame_duration",
                        "frame_offset", "sequence_mode")


class NativeDensityError(ValueError):
    """An operation that could not produce a verified density result."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _error(code, message):
    raise NativeDensityError(code, message)


def _bounds(obj):
    matrix = obj.matrix_world
    identity_limit = 1e-6
    if (not all(math.isfinite(v) for row in matrix for v in row)
            or any(matrix[i][i] <= 0 for i in range(3))
            or any(abs(matrix[i][j]) > identity_limit for i in range(3) for j in range(3) if i != j)
            or obj.parent is not None or bool(obj.constraints) or bool(obj.animation_data)):
        _error("DENSITY_UNSUPPORTED_TRANSFORM", "Density guides require a static axis-aligned GAS domain")
    offsets = np.array(obj.bound_box, dtype=np.float64)
    scale = np.array([matrix[i][i] for i in range(3)])
    bounds = offsets.min(axis=0) * scale + np.array(matrix.translation)
    extent = (offsets.max(axis=0) - offsets.min(axis=0)) * scale
    if not np.isfinite(extent).all() or np.any(extent <= identity_limit):
        _error("DENSITY_UNSUPPORTED_TRANSFORM", "GAS domain bounds are empty or invalid")
    return bounds, extent


def _check_grid_transform(transform, *, raw=False):
    origin = np.array(transform.indexToWorld((0, 0, 0)), dtype=np.float64)
    basis = np.array([transform.indexToWorld(tuple(float(i == axis) for i in range(3)))
                      for axis in range(3)], dtype=np.float64) - origin
    steps = basis.diagonal()
    if (not transform.isLinear or not np.isfinite(basis).all() or not np.isfinite(origin).all()
            or np.any(steps <= 1e-8) or not np.allclose(basis, np.diag(steps), atol=1e-8, rtol=0)
            or not np.allclose(steps, steps[0], atol=1e-8, rtol=1e-6)
            or (raw and not np.allclose(origin, 0, atol=1e-8, rtol=0))):
        _error("DENSITY_UNSUPPORTED_TRANSFORM", "Expected an axis-aligned uniform affine density grid")
    return origin, steps


def _manifest_transform(grid):
    origin, step = _check_grid_transform(grid.transform)
    return {"origin": origin.tolist(), "voxel_size": step.tolist()}


def _volume_grid(vdb, path, *, delivery=False):
    if not path.is_file():
        _error("DENSITY_CACHE_MISSING", f"Density frame is missing: {path}")
    try:
        grid = vdb.read(str(path), "density")
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        _error("DENSITY_GRID_INVALID", f"Cannot read density from {path.name}: {exc}")
    if not isinstance(grid, vdb.FloatGrid):
        _error("DENSITY_GRID_INVALID", f"{path.name} requires a float density grid")
    if delivery and grid.gridClass != vdb.GridClass.FOG_VOLUME:
        _error("DENSITY_GRID_INVALID", f"{path.name} has no float Fog Volume density")
    if grid.activeVoxelCount() > _MAX_FRAME_VOXELS:
        _error("DENSITY_RESOURCE_LIMIT", f"{path.name} exceeds the guide voxel limit")
    return grid


def _verify_grid_values(grid):
    values = [(item.value, item.count) for item in grid.iterOnValues()]
    if not values:
        return 0.0
    numbers, counts = np.array(values, dtype=np.float64).T
    if not np.isfinite(numbers).all() or np.any(numbers < 0):
        _error("DENSITY_GRID_INVALID", "Float density must contain finite nonnegative values")
    return float(numbers @ counts)


def _density_file(frame):
    return f"density_{frame:04d}.vdb"


def _raw_density_file(directory, frame):
    return directory / f"fluid_data_{frame:04d}.vdb"


def _absolute_directory(value, label):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        _error("DENSITY_PATH_REQUIRED", f"{label} requires a dedicated absolute directory")
    path = Path(value).expanduser()
    if not path.is_absolute():
        _error("DENSITY_PATH_REQUIRED", f"{label} requires a dedicated absolute directory")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            _error("DENSITY_PATH_UNSAFE", f"{label} cannot traverse links or Windows reparse points: {part}")
    return path.resolve()


@contextmanager
def _new_directory(path):
    """Only remove files claimed by this operation, leaving foreign entries alone."""
    try:
        path.mkdir()
    except FileExistsError:
        _error("DENSITY_OUTPUT_EXISTS", f"Density target already exists: {path}")
    except FileNotFoundError:
        _error("DENSITY_PATH_REQUIRED", f"Density output parent directory does not exist: {path.parent}")
    written = []
    try:
        yield written
    except BaseException:
        for item in reversed(written):
            if item.is_file():
                item.unlink()
        try:
            path.rmdir()
        except OSError:
            pass
        raise


def _claim(path, written):
    try:
        with path.open("xb"):
            pass
    except FileExistsError:
        _error("DENSITY_OUTPUT_EXISTS", f"Density target already exists: {path}")
    written.append(path)


def _write_manifest(path, payload, written):
    _claim(path, written)
    path.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _write_grid(vdb, path, grid, written):
    _claim(path, written)
    grid.name = "density"
    grid.gridClass = vdb.GridClass.FOG_VOLUME
    vdb.write(str(path), grids=[grid])
    checked = _volume_grid(vdb, path, delivery=True)
    if checked.activeVoxelCount() != grid.activeVoxelCount() or _manifest_transform(checked) != _manifest_transform(grid):
        _error("DENSITY_GRID_INVALID", f"OpenVDB readback changed {path.name}")
    return {
        "path": path.name,
        "sha256": _sha256_file(path),
        "byte_count": path.stat().st_size,
        "active_voxels": checked.activeVoxelCount(),
        "density_sum": _verify_grid_values(checked),
        "transform": _manifest_transform(checked),
    }


def _output_path(options, cache):
    from .native_fluid import NativeFluidError, assert_isolated_output

    value = options.get("output_directory")
    if value:
        path = _absolute_directory(value, "output_directory")
    else:
        current = Path(bpy.data.filepath)
        if not bpy.data.filepath or not current.is_absolute():
            _error("DENSITY_UNSAVED_BLEND", "Save the working .blend before creating a guide")
        path = _absolute_directory(current.with_suffix(".pnw-density") / uuid.uuid4().hex, "Guide directory")
    if path.is_relative_to(cache.parent):
        _error("DENSITY_OUTPUT_INVALID", "Store density results outside the source cache tree")
    try:
        path = assert_isolated_output(path)
    except NativeFluidError as exc:
        _error("DENSITY_OUTPUT_INVALID", exc.message)
    if not value:
        path.parent.mkdir(exist_ok=True)
    return path


def _lineage(path, directory):
    try:
        return {"kind": "relative", "path": Path(os.path.relpath(path, directory)).as_posix()}
    except ValueError:
        return {"kind": "absolute", "path": path.as_posix()}


def _source_file_and_manifest(guide):
    source = guide.get(_SOURCE)
    if not isinstance(source, bpy.types.Object) or source.type != "VOLUME" or source.get(_ROLE) != "source":
        _error("DENSITY_GUIDE_INVALID", "Guide source volume has been removed or replaced")
    source_path = Path(bpy.path.abspath(source.data.filepath)).resolve()
    path = source_path.parent / "source-density.json"
    if not path.is_file():
        _error("DENSITY_CACHE_MISSING", f"Guide source manifest is missing: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _error("DENSITY_GRID_INVALID", f"Guide source manifest cannot be read: {path}")
    if not isinstance(manifest, dict) or manifest.get("schema") != "pnw.native-density-source/1":
        _error("DENSITY_GUIDE_INVALID", "Guide source manifest does not describe this workflow")
    files = manifest.get("frames")
    if not isinstance(files, list) or not files or not all(isinstance(item, dict) for item in files):
        _error("DENSITY_GUIDE_INVALID", "Guide source manifest contains no frame records")
    start = files[0].get("frame")
    if (type(start) is not int or source_path.name != _density_file(start)
            or not source.data.is_sequence or source.data.frame_start != start
            or source.data.frame_duration != len(files) or source.data.frame_offset != start - 1
            or source.data.sequence_mode != "CLIP"):
        _error("DENSITY_GUIDE_INVALID", "Guide source sequence path or timing was edited; restore it or create a new guide")
    return source, path, manifest


def _check_source(guide, cache, frames):
    source, manifest_path, manifest = _source_file_and_manifest(guide)
    lineage = manifest.get("source_cache", {})
    root = lineage.get("path") if isinstance(lineage, dict) else None
    if not isinstance(root, str) or (manifest_path.parent / root).resolve() != cache:
        _error("DENSITY_STALE_SOURCE", "The density guide uses an older bake. Rebuild its source using Guide Density.")
    records = {line.get("frame"): line for line in manifest["frames"] if type(line.get("frame")) is int}
    for frame in frames:
        record = records.get(frame)
        if record is None or record.get("path") != _density_file(frame):
            _error("DENSITY_CACHE_MISSING", f"The guide does not cover density frame {frame}")
        baked = _raw_density_file(cache, frame)
        normalized = manifest_path.with_name(_density_file(frame))
        if not baked.is_file() or not normalized.is_file():
            _error("DENSITY_CACHE_MISSING", f"Density at frame {frame} is missing")
        if _sha256_file(baked) != record.get("source_sha256"):
            _error("DENSITY_STALE_SOURCE", f"The bake for density frame {frame} changed")
        if _sha256_file(normalized) != record.get("sha256"):
            _error("DENSITY_GRID_INVALID", f"The guide source for frame {frame} failed checksum verification")
    return source, manifest_path, manifest


def _rig(bounds, size):
    x, y, z = bounds
    dx, dy, dz = size
    layout = {
        "bypass_anchor": (x + 0.40 * dx, y + 0.5 * dy, z + 0.39 * dz),
        "exit_anchor": (x + 0.40 * dx, y + 0.5 * dy, z + 0.68 * dz),
        "rise": (z + 0.13 * dz, z + 0.33 * dz),
        "fall": (z + 0.41 * dz, z + 0.60 * dz),
        "edge": (x + 0.41 * dx, x + 0.52 * dx),
        "exit": (z + 0.53 * dz, z + 0.71 * dz),
    }
    layout["bypass_position"] = (x + 0.27 * dx, *layout["bypass_anchor"][1:])
    layout["exit_position"] = (x + 0.61 * dx, *layout["exit_anchor"][1:])
    return layout


def _control(name, label, position, collection, created):
    if name:
        obj = collection.all_objects.get(name)
        if obj is None:
            _error("DENSITY_CONTROL_INVALID", f"Guide control is not in the active scene: {name}")
        return obj
    obj = bpy.data.objects.new(label, None)
    created.append(obj)
    obj.location = position
    obj.empty_display_type = "SPHERE"
    obj.empty_display_size = 0.2
    collection.objects.link(obj)
    return obj


def _source_volume(obj, directory, frames, collection, created):
    data = bpy.data.volumes.new(f"{obj.name} density source")
    created.append(data)
    source = bpy.data.objects.new(data.name, data)
    created.append(source)
    source[_ROLE] = "source"
    collection.objects.link(source)
    source.hide_render = True
    source.hide_set(True)
    _set_source_sequence(source, directory, frames)
    return source


def _set_source_sequence(source, directory, frames):
    data = source.data
    data.filepath = bpy.path.relpath(str(directory / _density_file(frames[0])))
    data.is_sequence = True
    data.frame_start = frames[0]
    data.frame_duration = len(frames)
    data.frame_offset = frames[0] - 1
    data.sequence_mode = "CLIP"
    source.update_tag()


def _replace_source_sequence(source, directory, frames):
    before = {key: getattr(source.data, key) for key in _SEQUENCE_PROPERTIES}
    try:
        _set_source_sequence(source, directory, frames)
    except Exception as exc:
        for key, value in before.items():
            setattr(source.data, key, value)
        source.update_tag()
        _error("DENSITY_GUIDE_REFRESH", f"Guide source update was rolled back: {exc}")


def _material(obj, created):
    if obj.data.materials and obj.data.materials[0]:
        return obj.data.materials[0]
    material = bpy.data.materials.new("PNW Smoke density")
    created.append(material)
    material.use_nodes = True
    volume = material.node_tree.nodes.new("ShaderNodeVolumePrincipled")
    volume.inputs["Density"].default_value = 1
    volume.inputs["Density Attribute"].default_value = "density"
    output = material.node_tree.nodes.get("Material Output")
    material.node_tree.links.new(volume.outputs["Volume"], output.inputs["Volume"])
    for node in list(material.node_tree.nodes):
        if node.type == "BSDF_PRINCIPLED":
            material.node_tree.nodes.remove(node)
    return material


def _find_guide(obj, name):
    if name:
        guide = bpy.data.objects.get(name)
        if guide is not None and (guide.get(_ROLE) != "guide" or guide.get(_DOMAIN) != obj
                                  or guide.name not in bpy.context.scene.objects):
            _error("DENSITY_GUIDE_INVALID", f"Name belongs to a different scene object: {name}")
        return guide
    guides = [item for item in bpy.context.scene.objects if item.get(_ROLE) == "guide" and item.get(_DOMAIN) == obj]
    if len(guides) > 1:
        _error("DENSITY_GUIDE_INVALID", "Several guides use this GAS domain. Select a guide by object name.")
    return guides[0] if guides else None


def _modifier_inputs(guide):
    mod = guide.modifiers.get(_MODIFIER)
    if mod is None or mod.type != "NODES" or mod.node_group is None:
        _error("DENSITY_GUIDE_INVALID", "Guide's Geometry Nodes modifier is missing")
    try:
        return getattr(mod.properties.inputs, guide[_STRENGTH]), getattr(mod.properties.inputs, guide[_INSPECTION])
    except (AttributeError, KeyError):
        _error("DENSITY_GUIDE_INVALID", "Guide's modifier inputs were removed or renamed")


def _validate_guide_identity(guide):
    if not np.allclose(np.array(guide.matrix_world), np.eye(4), atol=1e-6, rtol=0):
        _error("DENSITY_UNSUPPORTED_TRANSFORM", "Move the guide controls and keep the density guide object at identity")
    return _modifier_inputs(guide)

def _make_guide(name, source, around, exit_guide, material, layout, collection, created):
    tree = bpy.data.node_groups.new("PNW Native Density Guide", "GeometryNodeTree")
    created.append(tree)
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    mix = tree.interface.new_socket(name="Guide Strength", in_out="INPUT", socket_type="NodeSocketFloat")
    mix.default_value, mix.min_value, mix.max_value = 1.0, 0.0, 1.0
    inspect = tree.interface.new_socket(name="Inspection Mesh", in_out="INPUT", socket_type="NodeSocketBool")
    inspect.default_value = False
    link = tree.links.new

    def node(identifier, label, x, y):
        item = tree.nodes.new(identifier)
        item.label = label
        item.location = (x, y)
        return item

    def assign(socket, source_socket_or_value):
        if isinstance(source_socket_or_value, bpy.types.NodeSocket):
            link(source_socket_or_value, socket)
        else:
            socket.default_value = source_socket_or_value

    def math_node(operation, label, x, y, a, b):
        item = node("ShaderNodeMath", label, x, y)
        item.operation = operation
        assign(item.inputs[0], a)
        assign(item.inputs[1], b)
        return item.outputs[0]

    def vector(operation, label, x, y, a, b):
        item = node("ShaderNodeVectorMath", label, x, y)
        item.operation = operation
        assign(item.inputs[0], a)
        assign(item.inputs["Scale"] if operation == "SCALE" else item.inputs[1], b)
        return item.outputs["Vector"]

    def smooth(label, x, y, value, lower, upper):
        item = node("ShaderNodeMapRange", label, x, y)
        item.clamp = True
        item.interpolation_type = "SMOOTHSTEP"
        link(value, item.inputs["Value"])
        item.inputs["From Min"].default_value = lower
        item.inputs["From Max"].default_value = upper
        return item.outputs["Result"]

    def object_info(obj, label, x, y):
        item = node("GeometryNodeObjectInfo", label, x, y)
        item.transform_space = "RELATIVE"
        item.inputs["Object"].default_value = obj
        return item

    group_in = node("NodeGroupInput", "Preview and inspect", -1680, 80)
    group_out = node("NodeGroupOutput", "Editable guided smoke", 1660, 720)
    obj = object_info(source, "Original gas density sequence", -1680, 760)
    grid = node("GeometryNodeGetNamedGrid", "Read density", -1440, 760)
    grid.inputs["Name"].default_value = "density"
    link(obj.outputs["Geometry"], grid.inputs["Volume"])
    info = node("GeometryNodeGridInfo", "Reuse input grid transform", -1220, 1020)
    link(grid.outputs["Grid"], info.inputs["Grid"])
    expand = node("GeometryNodeGridVoxelize", "Expand tiles into voxels", -1220, 760)
    link(grid.outputs["Grid"], expand.inputs["Grid"])
    points = node("GeometryNodeGridToPoints", "One point per active voxel", -1000, 760)
    link(expand.outputs["Grid"], points.inputs["Grid"])

    position = node("GeometryNodeInputPosition", "World point position", -1680, 400)
    axes = node("ShaderNodeSeparateXYZ", "Guide by height and side", -1460, 400)
    link(position.outputs["Position"], axes.inputs["Vector"])
    z = axes.outputs["Z"]
    x = axes.outputs["X"]
    ease_in = smooth("Build bend approaching capsule", -1230, 415, z, *layout["rise"])
    ease_out = smooth("Release bend above capsule", -1230, 90, z, *layout["fall"])
    left_ease = smooth("Keep right-side smoke", -1230, -240, x, *layout["edge"])
    band = math_node("SUBTRACT", "Bypass height mask", -990, 410, ease_in, ease_out)
    left_side = math_node("SUBTRACT", "Left lobe mask", -990, -25, 1, left_ease)
    bypass_weight = math_node("MULTIPLY", "Blend by side and height", -770, 380, band, left_side)
    exit_weight = smooth("Ramp exit above ceramic", -1000, -380, z, *layout["exit"])
    around_info = object_info(around, "Move the bypass control", -1000, 50 - 850)
    exit_info = object_info(exit_guide, "Move the exit control", -1000, 50 - 1110)
    bypass_offset = vector("SUBTRACT", "Bypass movement", -770, -740, around_info.outputs["Location"], layout["bypass_anchor"])
    exit_offset = vector("SUBTRACT", "Exit movement", -770, -1010, exit_info.outputs["Location"], layout["exit_anchor"])
    bypass = vector("SCALE", "Apply bypass mask", -540, 315, bypass_offset, bypass_weight)
    head = vector("SCALE", "Apply exit mask", -540, -65, exit_offset, exit_weight)
    offset = vector("ADD", "Combine both control volumes", -325, 310, bypass, head)
    strength = vector("SCALE", "Overall guide blend", -115, 280, offset, group_in.outputs["Guide Strength"])
    target = vector("ADD", "Bend sample positions", 105, 390, position.outputs["Position"], strength)

    raster = node("GeometryNodeRasterizePoints", "5.3 · transfer density at guided positions", 345, 760)
    raster.rasterize_items.new("Density")
    raster.inputs["Grid Transform Mode"].default_value = "Matrix"
    raster.inputs["Kernel Type"].default_value = "Linear"
    link(info.outputs["Transform"], raster.inputs["Matrix"])
    link(points.outputs["Points"], raster.inputs["Points"])
    link(points.outputs["Value"], raster.inputs["Density"])
    link(target, raster.inputs["Position"])

    store = node("GeometryNodeStoreNamedGrid", "Volume grid for shader", 620, 820)
    store.inputs["Name"].default_value = "density"
    link(raster.outputs["Density"], store.inputs["Grid"])
    assign_material = node("GeometryNodeSetMaterial", "Same density material as A", 860, 850)
    assign_material.inputs["Material"].default_value = material
    link(store.outputs["Volume"], assign_material.inputs["Geometry"])

    export_expand = node("GeometryNodeGridVoxelize", "Expand output tiles", 575, 460)
    export_points = node("GeometryNodeGridToPoints", "Read back rasterized values", 800, 465)
    attr = node("GeometryNodeStoreNamedAttribute", "Transfer density to readback mesh", 1020, 450)
    attr.domain = "POINT"
    attr.data_type = "FLOAT"
    attr.inputs["Name"].default_value = "density_vdb"
    vertices = node("GeometryNodePointsToVertices", "Readback mesh for VDB writer", 1270, 470)
    link(raster.outputs["Density"], export_expand.inputs["Grid"])
    link(export_expand.outputs["Grid"], export_points.inputs["Grid"])
    link(export_points.outputs["Points"], attr.inputs["Geometry"])
    link(export_points.outputs["Value"], attr.inputs["Value"])
    link(attr.outputs["Geometry"], vertices.inputs["Points"])

    switch = node("GeometryNodeSwitch", "Volume or independent readback", 1440, 760)
    switch.input_type = "GEOMETRY"
    link(group_in.outputs["Inspection Mesh"], switch.inputs["Switch"])
    link(assign_material.outputs["Geometry"], switch.inputs["False"])
    link(vertices.outputs["Mesh"], switch.inputs["True"])
    link(switch.outputs["Output"], group_out.inputs["Geometry"])

    mesh = bpy.data.meshes.new("Smoke node host")
    created.append(mesh)
    obj = bpy.data.objects.new(name, mesh)
    created.append(obj)
    collection.objects.link(obj)
    modifier = obj.modifiers.new(_MODIFIER, "NODES")
    modifier.node_group = tree
    return obj, modifier, mix.identifier, inspect.identifier


def _extract_source(vdb, directory, cache, frames, bounds, written):
    entries = []
    for frame in frames:
        baked = _raw_density_file(cache, frame)
        if not baked.is_file():
            _error("DENSITY_CACHE_MISSING", f"Density frame is missing: {baked}")
        fingerprint = _sha256_file(baked)
        source = _volume_grid(vdb, baked)
        _verify_grid_values(source)
        transform = source.transform
        _, step = _check_grid_transform(transform, raw=True)
        # The solver stores index zero, while its first world sample is centered in the first cell.
        transform.postTranslate(tuple(bounds + 0.5 * step))
        source.transform = transform
        entry = _write_grid(vdb, directory / _density_file(frame), source, written)
        if _sha256_file(baked) != fingerprint:
            _error("DENSITY_STALE_SOURCE", f"Density at frame {frame} changed during extraction")
        entries.append({"frame": frame, "source_sha256": fingerprint, **entry})
    _write_manifest(directory / "source-density.json", {
        "schema": "pnw.native-density-source/1",
        "blender_version": bpy.app.version_string,
        "blender_build": bpy.app.build_hash.decode(),
        "source_cache": _lineage(cache, directory),
        "frames": entries,
    }, written)


def _guide_payload(guide, source, manifest, frames, reused):
    return {
        "guide_object": guide.name,
        "source_object": source.name,
        "source_manifest": str(manifest),
        "bypass_object": guide[_BYPASS].name,
        "exit_object": guide[_EXIT].name,
        "frame_start": frames[0],
        "frame_end": frames[-1],
        "reused": reused,
    }


def _apply_strength(guide, strength, inputs):
    if strength is not None:
        inputs[0].value = strength
        guide.update_tag()


def _ensure_guide(vdb, obj, guide, options, cache, frames, bounds, extent, strength):
    created = []
    if guide is not None:
        inputs = _validate_guide_identity(guide)
        for name, key in (("bypass_object", _BYPASS), ("exit_object", _EXIT)):
            control = guide.get(key)
            if not isinstance(control, bpy.types.Object) or control.name not in bpy.context.scene.objects:
                _error("DENSITY_CONTROL_INVALID", f"The existing {name} has no control object in this scene")
            if options.get(name) and options[name] != control.name:
                _error("DENSITY_CONTROL_INVALID", f"Select the existing {name} or create a differently named guide")
        source, manifest, _ = _source_file_and_manifest(guide)
        try:
            _check_source(guide, cache, frames)
        except NativeDensityError as exc:
            if exc.code not in ("DENSITY_STALE_SOURCE", "DENSITY_CACHE_MISSING"):
                raise
            if source.data.users != 1:
                _error("DENSITY_GUIDE_INVALID", "Make the shared density source Volume single user before refreshing")
            directory = _output_path(options, cache)
            with _new_directory(directory) as written:
                _extract_source(vdb, directory, cache, frames, bounds, written)
                _replace_source_sequence(source, directory, frames)
                manifest = directory / "source-density.json"
        _apply_strength(guide, strength, inputs)
        return _guide_payload(guide, source, manifest, frames, True)

    name = options.get("guide_name") or f"{obj.name} Density Guide"
    directory = _output_path(options, cache)
    collection = bpy.context.scene.collection
    layout = _rig(bounds, extent)
    with _new_directory(directory) as written:
        try:
            _extract_source(vdb, directory, cache, frames, bounds, written)
            material = _material(obj, created)
            source = _source_volume(obj, directory, frames, collection, created)
            bypass = _control(options.get("bypass_object"), f"{name} Bypass", layout["bypass_position"], collection, created)
            exit_guide = _control(options.get("exit_object"), f"{name} Exit", layout["exit_position"], collection, created)
            if bypass == exit_guide:
                _error("DENSITY_CONTROL_INVALID", "Bypass and Exit must refer to distinct objects")
            guide, _, strength_key, inspection_key = _make_guide(
                name, source, bypass, exit_guide, material, layout, collection, created
            )
            guide[_ROLE] = "guide"
            guide[_DOMAIN] = obj
            guide[_SOURCE] = source
            guide[_BYPASS] = bypass
            guide[_EXIT] = exit_guide
            guide[_STRENGTH] = strength_key
            guide[_INSPECTION] = inspection_key
            _apply_strength(guide, strength, _modifier_inputs(guide))
            return _guide_payload(guide, source, directory / "source-density.json", frames, False)
        except BaseException:
            bpy.data.batch_remove(ids=created)
            raise


def _guided_grid(vdb, guide, source_grid):
    evaluated = guide.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.data
    transform = source_grid.transform
    grid = vdb.FloatGrid()
    grid.transform = transform
    if not isinstance(mesh, bpy.types.Mesh):
        _error("DENSITY_GUIDE_INVALID", "The guide's Inspection Mesh output was disconnected or replaced")
    count = len(mesh.vertices)
    if count == 0 and source_grid.activeVoxelCount() == 0:
        return grid
    if count > _MAX_FRAME_VOXELS:
        _error("DENSITY_RESOURCE_LIMIT", "Guided density exceeds the output voxel limit")
    attribute = mesh.attributes.get("density_vdb")
    if count == 0 or attribute is None or attribute.data_type != "FLOAT" or attribute.domain != "POINT":
        _error("DENSITY_GUIDE_INVALID", "The guide does not expose one float density per inspection point")
    coords = np.empty(count * 3, dtype=np.float32)
    density = np.empty(count, dtype=np.float32)
    mesh.vertices.foreach_get("co", coords)
    attribute.data.foreach_get("value", density)
    if not np.isfinite(coords).all() or not np.isfinite(density).all() or np.any(density < 0):
        _error("DENSITY_GRID_INVALID", "Guided density contains invalid coordinates or values")
    active = density > _MIN_DENSITY
    if not np.any(active):
        return grid
    origin, step = _check_grid_transform(transform)
    cell_coords = (coords.reshape((-1, 3))[active] - origin) / step
    rounded = np.rint(cell_coords)
    if np.any(np.abs(rounded) > 100_000_000) or not np.allclose(cell_coords, rounded, atol=1e-4, rtol=0):
        _error("DENSITY_GRID_INVALID", "Inspection points no longer correspond to the source grid transform")
    ijk = rounded.astype(np.int32)
    lo = ijk.min(axis=0)
    shape = tuple(int(v) for v in (ijk.max(axis=0) - lo + 1))
    if math.prod(shape) > _MAX_BOX_VOXELS:
        _error("DENSITY_RESOURCE_LIMIT", "Guided density occupies too large a voxel box")
    pixels = tuple((ijk - lo).T)
    flattened = np.ravel_multi_index(pixels, shape)
    if np.unique(flattened).size != flattened.size:
        _error("DENSITY_GRID_INVALID", "Several inspection points occupy the same density voxel")
    data = np.zeros(shape, dtype=np.float32)
    data[pixels] = density[active]
    grid.copyFromArray(data, ijk=tuple(int(v) for v in lo), tolerance=_MIN_DENSITY)
    return grid


def _export(vdb, guide, options, cache, frames):
    if guide is None:
        _error("DENSITY_GUIDE_INVALID", "Create a density guide before exporting guided density")
    _, inspection_input = _validate_guide_identity(guide)
    source, manifest_path, _ = _check_source(guide, cache, frames)
    if not np.allclose(np.array(source.matrix_world), np.eye(4), atol=1e-6, rtol=0):
        _error("DENSITY_UNSUPPORTED_TRANSFORM", "Keep the guide source volume at identity")
    if not options.get("output_directory"):
        _error("DENSITY_PATH_REQUIRED", "Export requires a dedicated absolute output_directory")
    directory = _output_path(options, cache)
    scene = bpy.context.scene
    original_frame = (scene.frame_current, scene.frame_subframe)
    original_inspection = inspection_input.value
    entries = []
    with _new_directory(directory) as written:
        try:
            inspection_input.value = True
            guide.update_tag()
            for frame in frames:
                scene.frame_set(frame)
                bpy.context.view_layer.update()
                original = _volume_grid(vdb, manifest_path.with_name(_density_file(frame)), delivery=True)
                guided = _guided_grid(vdb, guide, original)
                entries.append({"frame": frame, **_write_grid(
                    vdb, directory / _density_file(frame), guided, written
                )})
        finally:
            inspection_input.value = original_inspection
            guide.update_tag()
            scene.frame_set(original_frame[0], subframe=original_frame[1])
            bpy.context.view_layer.update()
        manifest = directory / "density-sequence.json"
        _write_manifest(manifest, {
            "schema": "pnw.native-density-sequence/1",
            "blender_version": bpy.app.version_string,
            "blender_build": bpy.app.build_hash.decode(),
            "grid": {"name": "density", "type": "FloatGrid", "grid_class": "FOG_VOLUME"},
            "guide_object": guide.name,
            "source_manifest": _lineage(manifest_path, directory),
            "frame_start": frames[0],
            "frame_end": frames[-1],
            "blender_sequence": {
                "filepath": entries[0]["path"], "frame_start": frames[0],
                "frame_offset": frames[0] - 1, "frame_duration": len(entries), "mode": "CLIP",
            },
            "frames": entries,
        }, written)
    return {
        "guide_object": guide.name,
        "manifest": str(manifest),
        "output_directory": str(directory),
        "frame_start": frames[0],
        "frame_end": frames[-1],
        "frame_count": len(entries),
        "frames": entries,
    }


def execute_request(obj, request):
    """Build or reuse the scene guide, or export it using the current art edits."""
    operation = request.get("operation")
    if not is_supported_host(bpy.app.version) or operation not in ("guide_density", "export_density"):
        _error("DENSITY_UNSUPPORTED", "This operation requires the Blender 5.3 native density guide")
    options = request.get("native_density") or {}
    if not isinstance(options, dict):
        _error("DENSITY_REQUEST_INVALID", "native_density must contain named options")
    domain = obj.modifiers.get(request.get("modifier_name")) if request.get("modifier_name") else None
    if domain is None:
        domains = [mod for mod in obj.modifiers if mod.type == "FLUID" and mod.fluid_type == "DOMAIN"]
        domain = domains[0] if len(domains) == 1 else None
    if domain is None or domain.type != "FLUID" or domain.fluid_type != "DOMAIN":
        _error("DENSITY_DOMAIN_INVALID", "Select one native GAS domain")
    settings = domain.domain_settings
    if (settings is None or settings.domain_type != "GAS" or settings.use_adaptive_domain
            or settings.cache_data_format != "OPENVDB"):
        _error("DENSITY_DOMAIN_INVALID", "Use a fixed native GAS domain baked as OpenVDB")
    parameters = request.get("parameters") or {}
    if not isinstance(parameters, dict):
        _error("DENSITY_REQUEST_INVALID", "parameters must contain named options")
    start = parameters.get("frame_start", settings.cache_frame_start)
    end = parameters.get("frame_end", settings.cache_frame_end)
    if type(start) is not int or type(end) is not int or not (
        max(settings.cache_frame_start, 0) <= start <= end <= settings.cache_frame_end
        and end - start + 1 <= _MAX_FRAMES
    ):
        _error("DENSITY_REQUEST_INVALID", "Choose a valid density frame range within the domain bake")
    frames = range(start, end + 1)
    for key in ("guide_name", "bypass_object", "exit_object"):
        if options.get(key) is not None and not isinstance(options[key], str):
            _error("DENSITY_REQUEST_INVALID", f"{key} must be an object name")
    strength = options.get("guide_strength")
    if strength is not None and (type(strength) not in (float, int) or not math.isfinite(strength) or not 0 <= strength <= 1):
        _error("DENSITY_REQUEST_INVALID", "Guide strength must be between zero and one")
    cache = _absolute_directory(options.get("source_directory"), "source_directory")
    if not cache.is_dir():
        _error("DENSITY_CACHE_MISSING", f"Native density data directory is missing: {cache}")
    bounds, extent = _bounds(obj)
    guide = _find_guide(obj, options.get("guide_name"))
    try:
        vdb = _openvdb()
        if operation == "guide_density":
            return _ensure_guide(vdb, obj, guide, options, cache, frames, bounds, extent, strength)
        return _export(vdb, guide, options, cache, frames)
    except VdbExportError as exc:
        _error(exc.code, exc.message)
    except OSError as exc:
        _error("DENSITY_IO_ERROR", str(exc))



