"""Realized mesh exports, native Alembic and verified OpenVDB surface exports."""
from __future__ import annotations

from pathlib import Path
import json
from typing import Any

import bpy

from . import vdb_export, export_delivery, export_fidelity


def _prepare_export_frame(obj, request, frame):
    """Never mistake a skipped unbaked simulation frame for its solved state."""
    from . import bake, subgroups
    mod = obj.modifiers.get(request.get("modifier_name", "PhysicsNodes"))
    scene = bpy.context.scene
    if mod is None or mod.type != "NODES" or not mod.node_group:
        scene.frame_set(frame)
        return "DIRECT_STATIC"
    node = mod.node_group.nodes.get("PNW_CoreNode")
    if node is None or not node.node_tree:
        raise ValueError("EXPORT_STATE_INVALID: managed Core is missing")
    try:
        state = json.loads(obj.get(f"pnw_state_json__{mod.name}", "{}"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("EXPORT_STATE_INVALID: managed workflow state is not valid JSON") from exc
    params_hash = state.get("parameters_hash", "")
    valid, reason, directory = bake.is_cache_valid(
        obj,
        mod,
        params_hash,
        subgroups.tree_fingerprint(node.node_tree),
    )
    if valid:
        scene.frame_set(frame)
        return "CACHED_DIRECT"
    stats = bake.cache_stats(directory)
    if mod.bake_directory or stats["meta_files"] or stats["blob_files"]:
        raise ValueError(f"CACHE_INVALID: rebake before exporting ({reason})")
    start = int(state.get("parameters", {}).get("frame_start", request["parameters"]["frame_start"]))
    if frame < start:
        raise ValueError("Export frame is before simulation start")
    for current in range(start, frame + 1):
        scene.frame_set(current)
        dg = bpy.context.evaluated_depsgraph_get()
        obj.evaluated_get(dg)
    return "SEQUENTIAL_UNBAKED"


def _prepare_export_sequence(obj, request):
    """Validate identity and pre-roll every frame before a dynamic export.

    The Alembic exporter steps frames itself, but an unbaked Geometry Nodes
    simulation must first be evaluated in order.  A skipped frame cannot be
    detected from the output archive after the fact, so validate the complete
    requested window and let ``_prepare_export_frame`` build the sequential
    simulation state.
    """
    p = request["parameters"]
    start, end = int(p["frame_start"]), int(p["frame_end"])
    if end < start:
        raise ValueError("INVALID_EXPORT_RANGE: frame_end precedes frame_start")

    mod = obj.modifiers.get(request.get("modifier_name", "PhysicsNodes"))
    managed = mod is not None and mod.type == "NODES" and mod.node_group is not None
    if managed:
        node = mod.node_group.nodes.get("PNW_CoreNode")
        if node is not None and node.node_tree is not None:
            try:
                state = json.loads(obj.get(f"pnw_state_json__{mod.name}", "{}"))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("EXPORT_STATE_INVALID: managed workflow state is not valid JSON") from exc
            sim_start = int(state.get("parameters", {}).get("frame_start", start))
            sim_end = int(state.get("parameters", {}).get("frame_end", end))
            if start < sim_start or end > sim_end:
                raise ValueError(
                    "INVALID_EXPORT_RANGE: "
                    f"requested {start}..{end} is outside the managed simulation {sim_start}..{sim_end}"
                )

    # A single call validates the cache/manifest and, for an unbaked workflow,
    # evaluates frames start..end in order.  It never samples the end frame
    # directly from a clean state.
    return _prepare_export_frame(obj, request, end)


def _new_export_path(value):
    path = Path(bpy.path.abspath(value)).resolve()
    source = Path(bpy.data.filepath).resolve() if bpy.data.filepath else None
    if source and (path == source or (path.exists() and path.samefile(source))):
        raise ValueError("Export must not overwrite the open source blend")
    if path.exists():
        raise ValueError(f"Export path already exists; choose a new path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _realize_modifier(obj):
    """Realize on the target so its OBJECT material overrides remain available."""
    group = bpy.data.node_groups.new("PNW_ExportRealize", "GeometryNodeTree")
    group.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    inp = group.nodes.new("NodeGroupInput")
    out = group.nodes.new("NodeGroupOutput")
    realize = group.nodes.new("GeometryNodeRealizeInstances")
    group.links.new(inp.outputs["Geometry"], realize.inputs["Geometry"])
    group.links.new(realize.outputs["Geometry"], out.inputs["Geometry"])
    mod = obj.modifiers.new("PNW_ExportRealize", "NODES")
    mod.node_group = group
    return mod, group


def _realized_mesh(obj, *, realize_instances=True):
    """Realize downstream without changing authored trees or simulation identity."""
    mod, group = _realize_modifier(obj) if realize_instances else (None, None)
    try:
        bpy.context.view_layer.update()
        dg = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(dg)
        mesh = bpy.data.meshes.new_from_object(
            evaluated,
            preserve_all_data_layers=True,
            depsgraph=dg,
        )
        if mesh is not None:
            try:
                export_fidelity.materialize_slots(mesh, evaluated)
            except Exception:
                bpy.data.meshes.remove(mesh)
                raise
        return mesh
    finally:
        if mod is not None:
            obj.modifiers.remove(mod)
            bpy.data.node_groups.remove(group)
            bpy.context.view_layer.update()


def _native_liquid_surface(obj, request, frame):
    from . import native_fluid

    evidence = native_fluid.require_cached_frame(obj, request, frame, domain_type="LIQUID")
    bpy.context.scene.frame_set(frame)
    mesh = _realized_mesh(obj, realize_instances=False)
    if mesh is None:
        raise native_fluid.NativeFluidError("NATIVE_LIQUID_SURFACE_MISSING", f"No liquid surface at frame {frame}")
    # When Blender cannot read a mesh cache it can evaluate the original domain.
    domain = obj.data
    is_domain_shape = (
        len(mesh.vertices) == len(domain.vertices)
        and len(mesh.polygons) == len(domain.polygons)
        and all((a.co - b.co).length_squared < 1e-14 for a, b in zip(mesh.vertices, domain.vertices))
    )
    if not mesh.polygons or is_domain_shape:
        bpy.data.meshes.remove(mesh)
        raise native_fluid.NativeFluidError(
            "NATIVE_LIQUID_SURFACE_MISSING", f"Frame {frame} evaluates to empty geometry or to the domain boundary",
        )
    return mesh, evidence


def _evaluated_mesh_counts(obj):
    dg = bpy.context.evaluated_depsgraph_get()
    try:
        mesh = bpy.data.meshes.new_from_object(
            obj.evaluated_get(dg),
            preserve_all_data_layers=False,
            depsgraph=dg,
        )
    except Exception as exc:
        raise ValueError(
            "EMPTY_EXPORT: evaluated Alembic geometry cannot be realized as mesh "
            "(volume grids must use OPENVDB)"
        ) from exc
    try:
        return len(mesh.vertices), len(mesh.polygons)
    finally:
        bpy.data.meshes.remove(mesh)


def _export_alembic(obj, request, path):
    scene = bpy.context.scene
    p = request["parameters"]
    sampling_mode = _prepare_export_sequence(obj, request)
    export_fidelity.check_prototype_materials(obj, request)
    old_frame_step = int(scene.frame_step)
    mod = group = None
    try:
        scene.frame_step = 1
        mod, group = _realize_modifier(obj)
        bpy.context.view_layer.update()
        wrapper = obj
        vertex_count, face_count = _evaluated_mesh_counts(wrapper)
        evaluated_frame = scene.frame_current
        if vertex_count == 0 or face_count == 0:
            # Emission and tearing can produce valid sequences that end empty.
            # Reject only when the entire requested frame range has no mesh.
            for frame in range(int(p["frame_start"]), int(p["frame_end"])):
                scene.frame_set(frame)
                vertex_count, face_count = _evaluated_mesh_counts(wrapper)
                evaluated_frame = frame
                if vertex_count and face_count:
                    break
        if vertex_count == 0 or face_count == 0:
            raise ValueError(
                "EMPTY_EXPORT: no frame in the Alembic range has mesh vertices/faces; "
                "volume grids must use OPENVDB"
            )
        dg = bpy.context.evaluated_depsgraph_get()
        instance_count = 0
        for inst in dg.object_instances:
            if inst.is_instance and inst.parent is not None and inst.parent.original == obj:
                instance_count += 1
        bpy.ops.object.select_all(action="DESELECT")
        wrapper.select_set(True)
        bpy.context.view_layer.objects.active = wrapper
        with bpy.context.temp_override(scene=scene, active_object=wrapper, selected_objects=[wrapper]):
            status = bpy.ops.wm.alembic_export(
                filepath=str(path),
                start=int(p["frame_start"]),
                end=int(p["frame_end"]),
                selected=True,
                as_background_job=False,
                evaluation_mode="RENDER",
                use_instancing=False,
                init_scene_frame_range=False,
                uvs=True,
                face_sets=True,
                packuv=False,
            )
        if "FINISHED" not in status or not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Alembic exporter did not produce a nonempty output")
        result = {
            "exported": True,
            "export_format": "ALEMBIC",
            "export_path": str(path),
            "size_bytes": path.stat().st_size,
            "sampling_mode": sampling_mode,
            "frame_start": int(p["frame_start"]),
            "frame_end": int(p["frame_end"]),
            "evaluated_vertex_count": vertex_count,
            "evaluated_face_count": face_count,
            "evaluated_geometry_frame": evaluated_frame,
            "instance_policy": "REALIZED_SINGLE_MESH",
            "instance_count_at_export_frame": instance_count,
            "instance_geometry_retained": False,
            "realized_geometry": True,
            "volume_grids_exported": False,
        }
        result["preservation"] = {
            "scope": "realized_alembic_mesh_sequence",
            "uv_layers": "blender_multiple_uv_maps",
            "materials": "face_sets_and_slot_names_only",
            "shader_graphs": "not_exported", "images": "not_exported",
            "limitations": ["Alembic does not deliver Blender shader graphs or texture files.",
                            "Multiple UV maps use Blender's Alembic extension; other readers are not verified.",
                            "Active editing/render UV selection is not guaranteed on import.",
                            "Custom graphs that already realized prototypes are checked only as evaluated target geometry."],
        }
        result["delivery_manifest"] = export_delivery.write_manifest(path, "ALEMBIC", [], result["preservation"])
        return result
    finally:
        scene.frame_step = old_frame_step
        if mod is not None:
            obj.modifiers.remove(mod)
        if group is not None:
            bpy.data.node_groups.remove(group)
        bpy.context.view_layer.update()


def export_workflow_result(obj: bpy.types.Object, request: dict[str, Any]) -> dict[str, Any]:
    fmt = request.get("export_format", "NONE")
    if fmt == "NONE":
        return {"exported": False, "export_format": "NONE"}
    if fmt not in {"STATIC_MESH", "ALEMBIC", "OPENVDB"}:
        raise ValueError(f"Unsupported export_format: {fmt}")
    value = request.get("export_path")
    if fmt in {"ALEMBIC", "OPENVDB"} and not value:
        raise ValueError(f"export_path is required for {fmt}")
    if value and request.get("preset_id") == "native_liquid":
        from . import native_fluid

        value = str(native_fluid.assert_isolated_output(bpy.path.abspath(value)))
    path = _new_export_path(value) if value else None
    if path:
        export_delivery.ensure_new_manifest(path)
    frame = int(request.get("export_frame", request["parameters"]["frame_end"]))
    scene = bpy.context.scene
    old_frame, old_subframe = scene.frame_current, scene.frame_subframe
    old_frame_start, old_frame_end = scene.frame_start, scene.frame_end
    selected = list(bpy.context.selected_objects)
    active = bpy.context.view_layer.objects.active
    try:
        if fmt == "STATIC_MESH":
            native_evidence = None
            if request.get("preset_id") == "native_liquid":
                mesh, native_evidence = _native_liquid_surface(obj, request, frame)
                sampling_mode = "NATIVE_CACHED_DIRECT"
            else:
                sampling_mode = _prepare_export_frame(obj, request, frame)
                export_fidelity.check_prototype_materials(obj, request)
                mesh = _realized_mesh(obj)
            if mesh is None or not mesh.vertices:
                if mesh is not None:
                    bpy.data.meshes.remove(mesh)
                raise ValueError("EMPTY_EXPORT: evaluated geometry contains no mesh vertices")
            mesh.name = f"{obj.name}_StaticF{frame}"
            try:
                files, preservation = export_fidelity.static_preservation(mesh) if path else ([], {
                    "scope": "in_scene_static_mesh", "materials": "effective_slots_and_shader_graphs",
                    "limitations": ["No disk delivery or external dependency validation was requested."],
                })
            except Exception:
                bpy.data.meshes.remove(mesh)
                raise
            static = bpy.data.objects.new(mesh.name, mesh)
            static.matrix_world = obj.matrix_world.copy()
            result = {
                "exported": True,
                "export_format": fmt,
                "export_frame": frame,
                "static_object_name": static.name,
                "vertex_count": len(mesh.vertices),
                "face_count": len(mesh.polygons),
                "saved_path": str(path) if path else None,
            }
            result["sampling_mode"] = sampling_mode
            result["preservation"] = preservation
            if native_evidence is not None:
                result["native_cache"] = native_evidence
                preservation["native_cache_generation"] = native_evidence["generation"]
            preservation["static_object_name"] = static.name
            if path:
                # save_as_mainfile(copy=True) is the supported full-scene export
                # path. Partial library writes of a new Scene crash in the
                # verified 5.2.1 build while copying its uninitialized ViewLayer.
                scene.collection.objects.link(static)
                try:
                    status = bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True, relative_remap=True)
                    if "FINISHED" not in status:
                        raise RuntimeError("Static blend copy was not saved")
                    result["delivery_manifest"] = export_delivery.write_manifest(path, fmt, files, preservation)
                finally:
                    bpy.data.objects.remove(static, do_unlink=True)
                    bpy.data.meshes.remove(mesh)
            else:
                scene.collection.objects.link(static)
            return result
        if fmt == "OPENVDB":
            sampling_mode = _prepare_export_frame(obj, request, frame)
            mesh = _realized_mesh(obj)
            try:
                if mesh is None or not mesh.vertices:
                    raise ValueError("EMPTY_EXPORT: evaluated geometry contains no mesh vertices")
                p = request["parameters"]
                source_semantics = (
                    "realized_surface_mesh_of_smoke_volume_output; "
                    "the exported grid is a surface level set, not native smoke density"
                    if request.get("preset_id") == "smoke_plume_volume"
                    else "realized_surface_mesh_world_level_set"
                )
                result = vdb_export.export_surface_vdb(
                    obj,
                    mesh,
                    path,
                    float(p["sdf_voxel_size"]),
                    frame=frame,
                    sampling_mode=sampling_mode,
                    source_semantics=source_semantics,
                )
                result["preservation"] = {
                    "scope": "surface_level_set", "materials": "not_applicable", "uv_layers": "not_applicable",
                    "limitations": ["Surface level set only; native smoke density and velocity grids are not exported."],
                }
                result["delivery_manifest"] = export_delivery.write_manifest(path, fmt, [], result["preservation"])
                return result
            finally:
                if mesh is not None:
                    bpy.data.meshes.remove(mesh)
        for item in selected:
            item.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        return _export_alembic(obj, request, path)
    finally:
        scene.frame_start, scene.frame_end = old_frame_start, old_frame_end
        scene.frame_set(old_frame, subframe=old_subframe)
        for item in bpy.context.selected_objects:
            item.select_set(False)
        for item in selected:
            item.select_set(True)
        bpy.context.view_layer.objects.active = active
