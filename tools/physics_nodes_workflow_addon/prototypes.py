"""Explicit, editable prototype snapshots; never mutate the source collection."""
from __future__ import annotations

import json
import math
import uuid

import bpy

SCHEMA = "pnw.prototype.snapshot/1"


def _preflight(source, target):
    if source is None:
        raise ValueError("INSTANCE_PREPARATION_INPUT: choose an instance collection")
    if source.library or source.override_library or getattr(source, "animation_data", None):
        raise ValueError("INSTANCE_PREPARATION_UNSUPPORTED: collection must be local and static")
    if source.children:
        raise ValueError("INSTANCE_PREPARATION_UNSUPPORTED: nested collections require a separate hierarchy adapter")
    objects = list(source.objects)
    if not objects:
        raise ValueError("INSTANCE_PREPARATION_INPUT: collection is empty")
    if target in objects:
        raise ValueError("INSTANCE_PREPARATION_INPUT: collection contains the workflow target")
    if not all(math.isfinite(float(v)) for v in source.instance_offset):
        raise ValueError("INSTANCE_PREPARATION_INPUT: non-finite collection offset")
    for obj in objects:
        if (obj.type != "MESH" or obj.library or obj.override_library or obj.data.library or obj.data.override_library
                or obj.mode != "OBJECT" or obj.parent or obj.modifiers or obj.constraints
                or obj.animation_data or obj.data.animation_data or obj.data.shape_keys
                or obj.rigid_body or obj.rigid_body_constraint or obj.instance_type != "NONE"):
            raise ValueError(f"INSTANCE_PREPARATION_UNSUPPORTED: {obj.name}: "
                             "requires local static Mesh without parenting, modifiers, constraints, animation, shape keys or instancing")
        if (not all(math.isfinite(float(v)) for row in obj.matrix_basis for v in row)
                or not all(math.isfinite(float(v)) for vertex in obj.data.vertices for v in vertex.co)):
            raise ValueError(f"INSTANCE_PREPARATION_INPUT: non-finite geometry/transform: {obj.name}")
        if any(slot.material and slot.material.library for slot in obj.material_slots):
            raise ValueError(f"INSTANCE_PREPARATION_UNSUPPORTED: linked material: {obj.name}")
    return objects


def _collection_info_order(source, objects):
    """Read native instance rows, not Python's different name sort order.

    Collection Info sorts names with Blender's natural comparator. Its reference
    table itself is NOT sorted; .reference_index maps the actual ordered rows.
    Read borrowed geometry before cleanup or any further dependency evaluation.
    """
    mesh = probe = group = None
    try:
        mesh = bpy.data.meshes.new("PNW_OrderProbe")
        probe = bpy.data.objects.new("PNW_OrderProbe", mesh)
        group = bpy.data.node_groups.new("PNW_OrderProbe", "GeometryNodeTree")
        group.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        output = group.nodes.new("NodeGroupOutput")
        info = group.nodes.new("GeometryNodeCollectionInfo")
        info.inputs["Collection"].default_value = source
        info.inputs["Separate Children"].default_value = True
        group.links.new(info.outputs["Instances"], output.inputs["Geometry"])
        probe.modifiers.new("PNW_OrderProbe", "NODES").node_group = group
        bpy.context.scene.collection.objects.link(probe)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        geometry = probe.evaluated_get(depsgraph).evaluated_geometry()
        points = geometry.instances_pointcloud()
        indices = points.attributes.get(".reference_index") if points is not None else None
        if indices is None or len(indices.data) != len(objects):
            raise ValueError("INSTANCE_PREPARATION_ORDER: native instance rows unavailable")
        references = list(geometry.instance_references())
        ordered = []
        for item in indices.data:
            reference = references[int(item.value)]
            if not isinstance(reference, bpy.types.Object):
                raise ValueError("INSTANCE_PREPARATION_ORDER: unsupported native reference")
            ordered.append(reference.original)
        if len(set(ordered)) != len(objects) or set(ordered) != set(objects):
            raise ValueError("INSTANCE_PREPARATION_ORDER: native object mapping differs from source")
        return ordered
    finally:
        if probe is not None:
            bpy.data.objects.remove(probe, do_unlink=True)
        if group is not None:
            bpy.data.node_groups.remove(group)
        if mesh is not None:
            bpy.data.meshes.remove(mesh)


def prepare_instances(target, request):
    """Freeze geometry and slot assignments into a NEW collection on every call.

    Material datablocks remain shared. Source edits do not update the snapshot;
    editing the snapshot is allowed and invalidates its consumers normally.
    No modifier is rebound and no author graph is rebuilt by this operation.
    """
    from . import identity

    source = bpy.data.collections.get(request.get("instance_collection") or "")
    objects = _preflight(source, target)
    objects = _collection_info_order(source, objects)
    token = uuid.uuid4().hex[:12]
    collection = bpy.data.collections.new(f"PNW_Instances_{token}")
    created_objects, created_meshes, mapping = [], [], []
    try:
        collection.instance_offset = source.instance_offset.copy()
        collection.hide_viewport = source.hide_viewport
        collection.hide_render = source.hide_render
        for index, original in enumerate(objects):
            materials = [slot.material for slot in original.material_slots]
            mesh = original.data.copy()
            created_meshes.append(mesh)
            clone = original.copy()
            created_objects.append(clone)
            clone.data = mesh
            for key in list(clone.keys()):
                if key in {"pnw_working_copy_source", "pnw_state_json"} or key.startswith((
                        "pnw_state_json__", "pnw_cache_owner_binding__", "pnw_cache_last_root__")):
                    del clone[key]
            # Keep relative prototype ordering even when source names collide
            # with existing copies. Object.copy retains authored basis/deltas.
            clone.name = f"PNW_{token}_{index:08d}_{original.name}"
            mesh.name = clone.name + "_Mesh"
            for slot_index, material in enumerate(materials):
                clone.material_slots[slot_index].link = "DATA"
                mesh.materials[slot_index] = material
            collection.objects.link(clone)
            mapping.append({
                "source_object": original.name, "source_mesh": original.data.name,
                "prepared_object": clone.name, "prepared_mesh": mesh.name,
                "source_geometry_sha256": identity.base_geometry_sha256(original),
                "prepared_geometry_sha256": identity.base_geometry_sha256(clone),
                "effective_materials": [m.name if m else None for m in materials],
            })
        evidence = {
            "schema": SCHEMA, "generation": token, "source_collection": source.name,
            "prepared_collection": collection.name, "objects": mapping,
            "source_link": "provenance_only_not_live", "geometry": "independent_editable_mesh_copies",
            "materials": "slot_assignments_copied_shader_datablocks_shared",
            "order": "native_collection_info_instance_rows",
        }
        collection["pnw_prototype_snapshot"] = json.dumps(evidence, ensure_ascii=False, allow_nan=False)
        # Retain even an initially unlinked source across save/reopen. This is
        # provenance ownership only; the snapshot's geometry does not read it.
        collection["pnw_prototype_source"] = source
        # The collection is intentionally outside the visible Scene hierarchy.
        # Fake user persists a prepared result even before a Build references it.
        collection.use_fake_user = True
        return evidence
    except Exception:
        # Only objects created in this call are rolled back. Prior generations
        # and the author source are never removed or relinked.
        for obj in created_objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in created_meshes:
            bpy.data.meshes.remove(mesh)
        bpy.data.collections.remove(collection)
        raise
