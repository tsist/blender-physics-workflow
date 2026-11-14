"""Material assignment and bounded static-target image dependency coverage."""
from __future__ import annotations

from pathlib import Path
import bpy

from . import export_delivery


def check_prototype_materials(obj, request):
    """Reject a reproduced loss at GN Realize, before signing faithful delivery.

    The managed normalization graph can realize prototypes before the final
    target evaluation; inspect its actual collection input as well as instances.
    """
    prototypes = {}
    mod = obj.modifiers.get(request.get("modifier_name", "PhysicsNodes"))
    if mod and mod.type == "NODES" and mod.node_group:
        for item in mod.node_group.interface.items_tree:
            if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name == "Instance Collection":
                slot = mod.properties.inputs[item.identifier]
                coll = slot.value if hasattr(slot, "value") else slot.get("value")
                if coll:
                    prototypes.update({o.as_pointer(): o for o in coll.all_objects})
    dg = bpy.context.evaluated_depsgraph_get()
    for inst in dg.object_instances:
        if inst.is_instance and inst.parent and inst.parent.original == obj and inst.object.original != obj:
            original = inst.object.original
            prototypes[original.as_pointer()] = original
    for prototype in prototypes.values():
        for index, slot in enumerate(prototype.material_slots):
            data_mat = prototype.data.materials[index] if index < len(prototype.data.materials) else None
            if slot.link == "OBJECT" and slot.material != data_mat:
                raise ValueError("EXPORT_PROTOTYPE_MATERIAL_UNSUPPORTED: "
                                 f"{prototype.name} has an OBJECT material override lost by instance realization; "
                                 "use a separate mesh with DATA materials for this prototype")


def materialize_slots(mesh, evaluated):
    # new_from_object copies Mesh.materials, which omits OBJECT link overrides.
    if len(mesh.materials) != len(evaluated.material_slots):
        raise ValueError("EXPORT_MATERIAL_MAPPING_INVALID: evaluated slots do not match realized mesh")
    for index, slot in enumerate(evaluated.material_slots):
        material = slot.material.original if slot.material else None
        mesh.materials[index] = material


def static_preservation(mesh):
    files, visited, images, visited_images = {}, set(), {}, set()

    def local(block):
        if block.library:
            raise ValueError(f"EXPORT_DEPENDENCY_UNSUPPORTED: linked material dependency {block.name}")

    def walk(tree):
        if tree is None or tree.as_pointer() in visited:
            return
        visited.add(tree.as_pointer())
        local(tree)
        for node in tree.nodes:
            child = getattr(node, "node_tree", None)
            if child:
                walk(child)
            if node.bl_idname == "ShaderNodeScript" and node.mode == "EXTERNAL":
                raise ValueError("EXPORT_DEPENDENCY_UNSUPPORTED: external shader scripts")
            if node.bl_idname == "ShaderNodeTexIES" and node.mode == "EXTERNAL":
                raise ValueError("EXPORT_DEPENDENCY_UNSUPPORTED: external IES profiles")
            image = getattr(node, "image", None)
            if image is None:
                continue
            local(image)
            if image.as_pointer() in visited_images:
                continue
            visited_images.add(image.as_pointer())
            if image.source not in {"FILE", "GENERATED"}:
                raise ValueError(f"EXPORT_DEPENDENCY_UNSUPPORTED: image source {image.source}: {image.name}")
            if image.is_dirty:
                raise ValueError(f"EXPORT_DEPENDENCY_UNSAVED: save image edits before export: {image.name}")
            if image.packed_file:
                images[image.name] = {"storage": "PACKED"}
                continue
            if image.source != "FILE":
                raise ValueError(f"EXPORT_DEPENDENCY_UNSAVED: save or pack image before export: {image.name}")
            path = Path(bpy.path.abspath(image.filepath, library=image.library)).resolve()
            record = export_delivery.file_record(path, "material_image")
            files[str(path)] = record
            images[image.name] = {"storage": "EXTERNAL", "path": str(path)}

    for material in mesh.materials:
        if material:
            local(material)
            walk(material.node_tree)
    preservation = {
        "scope": "target_static_mesh_and_material_images_only",
        "geometry": "realized_mesh", "materials": "effective_slots_and_shader_graphs",
        "material_slots": [m.name if m else None for m in mesh.materials],
        "uv_layers": [{"name": u.name, "active_render": bool(u.active_render)} for u in mesh.uv_layers],
        "active_uv": mesh.uv_layers.active.name if mesh.uv_layers.active else None,
        "images": images,
        "limitations": ["Full-scene copy: dependencies of other objects and simulation caches are not covered.",
                        "External image files remain at their recorded paths; this is not a portable packed bundle.",
                        "Custom graphs that already realized prototypes are checked only as evaluated target geometry."],
    }
    return list(files.values()), preservation
