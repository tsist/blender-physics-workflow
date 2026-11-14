"""Cache identity v2 for Physics Nodes Workflow disk caches.

The old cache identity was a name/location based node-tree hash plus a parameter
hash.  That is not enough to decide whether a bake still represents the actual
solver inputs.  This module builds a deterministic, layout-independent identity
from:

* recursive node semantics (operations, defaults, mute, interfaces, subgroups),
* the actual modifier socket values used by the solver,
* the target object's matrix and base geometry/topology/attributes,
* the target object's effective material-slot bindings (slot order, DATA/OBJECT
  link source and the real material ID, without hashing material contents),
* collider and instance-collection dependencies (including their content),
* scene time settings (fps, fps_base, frame range/step) and related settings.

It also reports why an identity cannot be trusted (animation, drivers,
constraints, or prior modifiers that are not covered).  Callers must refuse to
sign a cache as valid when ``reliable`` is false.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
from typing import Any, Iterable

import bpy

from . import animation_identity as pnw_animation_identity


IDENTITY_SCHEMA = "pnw-cache-identity/2.0"
GEOMETRY_SCHEMA = "pnw-base-geometry/2.0"
MATERIAL_SLOTS_SCHEMA = "pnw-effective-material-slots/1.0"

# Node properties that only describe the editor representation.  Names and
# locations are intentionally excluded: renaming/moving a node must not
# invalidate a bake.
_UI_NODE_PROPERTIES = frozenset(
    {
        "rna_type",
        "name",
        "label",
        "location",
        "location_absolute",
        "width",
        "height",
        "color",
        "use_custom_color",
        "select",
        "show_options",
        "show_preview",
        "show_texture",
        "hide",
        "parent",
        "dimensions",
        "type",
        "bl_idname",
        "bl_label",
        "bl_description",
        "bl_icon",
        "bl_static_type",
        "bl_width_default",
        "bl_width_min",
        "bl_width_max",
        "bl_height_default",
        "bl_height_min",
        "bl_height_max",
        "warning_propagation",
        "color_tag",
        "active_index",
        "inspection_index",
        "inputs",
        "outputs",
        "internal_links",
        "node_tree",
    }
)

_SCALAR_PROPERTY_TYPES = frozenset({"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM", "FLOAT_ARRAY", "INT_ARRAY"})

# Pointer properties whose contents are either captured explicitly, resolved
# through the node-pairing table, or are editor-only selections.
_HANDLED_STRUCT_POINTERS = frozenset({"color_ramp", "mapping", "texture_mapping", "color_mapping"})
_IGNORED_STRUCT_POINTERS = frozenset(
    {
        "parent",
        "node_tree",
        "paired_output",
        "active_item",
        "enum_definition",
        # Blender 5.2 multiline string editor viewport; the actual string is
        # already captured as a scalar node property. No evaluation state.
        "textbox_state",
    }
)

_DYNAMIC_ITEM_COLLECTIONS = frozenset(
    {
        "capture_items",
        "repeat_items",
        "state_items",
        "index_switch_items",
        "enum_items",
        "bundle_items",
    }
)

_UI_MODIFIER_PROPERTIES = frozenset(
    {
        "execution_time",
        "persistent_uid",
        "is_active",
        "show_expanded",
        "use_pin_to_last",
        "is_override_data",
        "open_output_attributes_panel",
        "open_manage_panel",
        "open_bake_panel",
        "open_named_attributes_panel",
        "open_bake_data_blocks_panel",
        "open_warnings_panel",
        "show_in_editmode",
        "show_on_cage",
        "show_group_selector",
        "show_manage_panel",
        "node_warnings",
        "panels",
        "bakes",
        "properties",
        "bake_directory",
        "node_group",
        "name",
        "type",
        "show_viewport",
        "show_render",
        "bake_target",
    }
)

# Modifiers that deterministically depend only on their own settings plus the
# incoming geometry.  Modifiers with object/collection/texture pointers are not
# considered covered because their external content is not part of the mesh
# digest.  Geometry Nodes and simulation modifiers are deliberately excluded.
_COVERED_PRIOR_MODIFIER_TYPES = frozenset(
    {
        "SUBSURF",
        "TRIANGULATE",
        "DECIMATE",
        "SOLIDIFY",
        "WELD",
        "EDGE_SPLIT",
        "ARRAY",
        "MIRROR",
    }
)


class CacheIdentityError(RuntimeError):
    """Raised when a reliable cache identity cannot be produced."""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _round_float(value: float, digits: int = 9) -> float:
    if not math.isfinite(value):
        raise CacheIdentityError("non_finite_cache_input", "Cache identity cannot sign NaN or Infinity as zero")
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0.0 else rounded


def _id_ref(id_block: Any) -> dict[str, Any] | None:
    if id_block is None:
        return None
    try:
        id_type = id_block.bl_rna.identifier
    except Exception:
        id_type = type(id_block).__name__
    return {"id_type": id_type, "name": getattr(id_block, "name", None)}


def _library_ref(library: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Return a stable reference for a library datablock, including its path."""
    if library is None:
        return None, []
    errors: list[str] = []
    try:
        name = library.name
    except Exception as exc:
        name = None
        errors.append(f"library_name_unreadable:{type(exc).__name__}")
    try:
        filepath = library.filepath
    except Exception as exc:
        filepath = None
        errors.append(f"library_filepath_unreadable:{type(exc).__name__}")
    resolved_filepath = None
    if isinstance(filepath, str) and filepath:
        try:
            resolved_filepath = bpy.path.abspath(filepath)
        except Exception as exc:
            errors.append(f"library_filepath_unresolvable:{type(exc).__name__}")
        else:
            try:
                resolved_filepath = os.path.normpath(str(resolved_filepath))
            except Exception as exc:
                errors.append(f"library_filepath_not_normalizable:{type(exc).__name__}")
                resolved_filepath = None
    if not isinstance(name, str) or not name:
        errors.append("library_name_invalid")
    if not isinstance(resolved_filepath, str) or not resolved_filepath:
        errors.append("library_filepath_invalid")
    return {
        "name": name if isinstance(name, str) and name else None,
        "filepath": resolved_filepath if isinstance(resolved_filepath, str) and resolved_filepath else None,
    }, errors


def _material_id_ref(material: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Return a material reference that distinguishes equal names by library.

    Deliberately excludes material node trees, shader settings and images:
    editing material content must not look like a geometry/solver-input change.
    """
    if material is None:
        return None, []
    errors: list[str] = []
    try:
        if not isinstance(material, bpy.types.Material):
            return None, [f"material_pointer_invalid:{type(material).__name__}"]
    except Exception as exc:
        return None, [f"material_pointer_unreadable:{type(exc).__name__}"]
    try:
        id_type = material.bl_rna.identifier
    except Exception as exc:
        id_type = None
        errors.append(f"material_id_type_unreadable:{type(exc).__name__}")
    try:
        name = material.name
    except Exception as exc:
        name = None
        errors.append(f"material_name_unreadable:{type(exc).__name__}")
    if not isinstance(name, str) or not name:
        errors.append("material_name_invalid")
    try:
        library = material.library
    except Exception as exc:
        library = None
        errors.append(f"material_library_unreadable:{type(exc).__name__}")
    library_payload, library_errors = _library_ref(library)
    errors.extend(library_errors)
    return {
        "id_type": id_type if isinstance(id_type, str) and id_type else None,
        "name": name if isinstance(name, str) and name else None,
        "library": library_payload,
    }, errors


def _serialize_value(value: Any) -> Any:
    """Convert Blender/RNA values to canonical JSON-friendly primitives."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return _round_float(value)
    if isinstance(value, bpy.types.ID):
        return _id_ref(value)
    if isinstance(value, dict):
        return {str(k): _serialize_value(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_serialize_value(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    # bpy_prop_array, Vector, Color, Matrix and similar sequences.
    if hasattr(value, "to_tuple"):
        try:
            return _serialize_value(value.to_tuple())
        except Exception:
            pass
    if hasattr(value, "__iter__") and not isinstance(value, (str, bytes, bytearray)):
        try:
            return [_serialize_value(v) for v in value]
        except Exception:
            pass
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        _serialize_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _matrix_payload(matrix: Any) -> list[list[float]]:
    try:
        return [[_round_float(float(v)) for v in row] for row in matrix]
    except Exception:
        return []


def _vector_payload(vector: Any) -> list[float]:
    try:
        return [_round_float(float(v)) for v in vector]
    except Exception:
        return []


def _socket_default_payload(socket: Any) -> Any:
    try:
        if hasattr(socket, "default_value"):
            return _serialize_value(socket.default_value)
    except Exception:
        pass
    return None


def _interface_payload(tree: bpy.types.NodeTree) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        iterable = list(tree.interface.items_tree)
    except Exception:
        return items
    for item in iterable:
        if getattr(item, "item_type", None) != "SOCKET":
            # Panels and other UI-only items are editor layout.
            continue
        payload: dict[str, Any] = {
            "name": getattr(item, "name", ""),
            "identifier": getattr(item, "identifier", ""),
            "in_out": getattr(item, "in_out", ""),
            "socket_type": getattr(item, "socket_type", ""),
            "default_value": _socket_default_payload(item),
        }
        for attr in ("min_value", "max_value"):
            if hasattr(item, attr):
                try:
                    payload[attr] = _serialize_value(getattr(item, attr))
                except Exception:
                    pass
        items.append(payload)
    items.sort(
        key=lambda item: (
            str(item.get("in_out", "")),
            str(item.get("name", "")),
            str(item.get("socket_type", "")),
            str(item.get("identifier", "")),
        )
    )
    return items


def _animation_summary(id_block: Any) -> dict[str, Any]:
    empty = {"animated": False, "action": None, "drivers": [], "nla_tracks": []}
    if id_block is None:
        return empty
    try:
        animation_data = getattr(id_block, "animation_data", None)
    except Exception:
        animation_data = None
    if animation_data is None:
        return empty

    action = getattr(animation_data, "action", None)
    drivers = list(getattr(animation_data, "drivers", []) or [])
    nla_tracks = list(getattr(animation_data, "nla_tracks", []) or [])
    action_slot = getattr(animation_data, "action_slot", None)
    animated = bool(action is not None or drivers or nla_tracks or action_slot is not None)

    driver_payload = []
    for driver in drivers:
        driver_payload.append(
            {
                "data_path": getattr(driver, "data_path", ""),
                "array_index": getattr(driver, "array_index", 0),
                "is_valid": bool(getattr(driver, "is_valid", True)),
            }
        )
    driver_payload.sort(key=lambda item: (item["data_path"], item["array_index"]))
    return {
        "animated": animated,
        "action": getattr(action, "name", None) if action is not None else None,
        "drivers": driver_payload,
        "nla_tracks": sorted(str(getattr(track, "name", "")) for track in nla_tracks),
    }


def _is_animated(id_block: Any) -> bool:
    return bool(_animation_summary(id_block).get("animated"))


def _constraint_payload(owner: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    try:
        constraints = list(getattr(owner, "constraints", []) or [])
    except Exception:
        return result
    for constraint in constraints:
        result.append(
            {
                "name": getattr(constraint, "name", ""),
                "type": getattr(constraint, "type", ""),
                "mute": bool(getattr(constraint, "mute", False)),
                "influence": _round_float(float(getattr(constraint, "influence", 1.0))),
                "target": _id_ref(getattr(constraint, "target", None)),
            }
        )
    result.sort(key=lambda item: str(item["name"]))
    return result


def _active_constraints(owner: Any) -> list[str]:
    active: list[str] = []
    try:
        constraints = list(getattr(owner, "constraints", []) or [])
    except Exception:
        return active
    for constraint in constraints:
        if bool(getattr(constraint, "mute", False)):
            continue
        try:
            influence = float(getattr(constraint, "influence", 1.0))
        except Exception:
            influence = 1.0
        if influence > 0.0:
            active.append(str(getattr(constraint, "name", "")))
    return sorted(active)


def _simple_rna_properties(owner: Any, *, extra_deny: Iterable[str] = ()) -> dict[str, Any]:
    deny = _UI_NODE_PROPERTIES | frozenset(extra_deny)
    result: dict[str, Any] = {}
    try:
        properties = owner.bl_rna.properties
    except Exception:
        return result
    for prop in properties:
        identifier = getattr(prop, "identifier", None)
        if not identifier or identifier in deny:
            continue
        prop_type = getattr(prop, "type", "")
        if prop_type not in _SCALAR_PROPERTY_TYPES and prop_type != "POINTER":
            continue
        try:
            value = getattr(owner, identifier)
        except Exception:
            continue
        if prop_type == "POINTER":
            # Object/collection/texture pointers are dependencies, not simple
            # scalar settings.  They are recorded separately where relevant.
            continue
        if isinstance(value, bpy.types.ID):
            continue
        result[identifier] = _serialize_value(value)
    return result


def _node_pointer_dependencies(node: Any) -> list[dict[str, Any]]:
    deps: list[dict[str, Any]] = []
    try:
        properties = node.bl_rna.properties
    except Exception:
        return deps
    for prop in properties:
        identifier = getattr(prop, "identifier", None)
        if not identifier or identifier in {
            "rna_type",
            "node_tree",
            "parent",
            "inputs",
            "outputs",
            "internal_links",
            "panel_states",
        }:
            continue
        if getattr(prop, "type", "") != "POINTER":
            continue
        try:
            value = getattr(node, identifier)
        except Exception:
            continue
        if value is None:
            continue
        if isinstance(value, bpy.types.ID):
            ref = _id_ref(value)
            if ref is not None:
                ref["property"] = identifier
                deps.append(ref)
        elif identifier not in _HANDLED_STRUCT_POINTERS and identifier not in _IGNORED_STRUCT_POINTERS:
            # Unknown sub-struct pointers cannot be summarized reliably.  They
            # are reported so _tree_reliability can fail closed.
            deps.append(
                {
                    "property": identifier,
                    "struct_type": getattr(getattr(value, "bl_rna", None), "identifier", type(value).__name__),
                    "unverified": True,
                }
            )
    deps.sort(
        key=lambda item: (
            str(item.get("property", "")),
            str(item.get("name", "")),
            str(item.get("struct_type", "")),
        )
    )
    return deps


def _socket_semantics(socket: Any, *, linked: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": getattr(socket, "name", ""),
        "identifier": getattr(socket, "identifier", ""),
        "type": getattr(socket, "type", ""),
        "linked": bool(linked),
    }
    if not linked:
        payload["default_value"] = _socket_default_payload(socket)
    if hasattr(socket, "enabled"):
        try:
            payload["enabled"] = bool(socket.enabled)
        except Exception:
            pass
    return payload


def _color_ramp_payload(ramp: Any) -> dict[str, Any] | None:
    if ramp is None:
        return None
    payload: dict[str, Any] = {
        "interpolation": getattr(ramp, "interpolation", None),
        "hue_interpolation": getattr(ramp, "hue_interpolation", None),
        "color_mode": getattr(ramp, "color_mode", None),
        "elements": [],
    }
    try:
        elements = list(ramp.elements)
    except Exception:
        elements = []
    for element in elements:
        payload["elements"].append(
            {
                "position": _round_float(float(getattr(element, "position", 0.0))),
                "color": _vector_payload(getattr(element, "color", ())),
                "alpha": _round_float(float(getattr(element, "alpha", 1.0))),
            }
        )
    return payload


def _curve_mapping_payload(mapping: Any) -> dict[str, Any] | None:
    if mapping is None:
        return None
    payload: dict[str, Any] = {
        "tone": getattr(mapping, "tone", None),
        "use_clip": bool(getattr(mapping, "use_clip", False)),
        "clip_min_x": _serialize_value(getattr(mapping, "clip_min_x", 0.0)),
        "clip_min_y": _serialize_value(getattr(mapping, "clip_min_y", 0.0)),
        "clip_max_x": _serialize_value(getattr(mapping, "clip_max_x", 1.0)),
        "clip_max_y": _serialize_value(getattr(mapping, "clip_max_y", 1.0)),
        "extend": getattr(mapping, "extend", None),
        "black_level": _serialize_value(getattr(mapping, "black_level", 0.0)),
        "white_level": _serialize_value(getattr(mapping, "white_level", 1.0)),
        "curves": [],
    }
    try:
        curves = list(mapping.curves)
    except Exception:
        curves = []
    for curve in curves:
        points: list[dict[str, Any]] = []
        try:
            curve_points = list(getattr(curve, "points", []) or [])
        except Exception:
            curve_points = []
        for point in curve_points:
            point_payload: dict[str, Any] = {
                "location": _vector_payload(getattr(point, "location", ())),
                "handle_type": getattr(point, "handle_type", None),
            }
            for attr in ("handle_left", "handle_right", "handle_left_type", "handle_right_type", "radius"):
                if hasattr(point, attr):
                    value = getattr(point, attr, None)
                    if value is not None:
                        point_payload[attr] = _serialize_value(value)
            points.append(point_payload)
        payload["curves"].append({"points": points})
    return payload


def _tex_mapping_payload(mapping: Any) -> dict[str, Any] | None:
    if mapping is None:
        return None
    return _simple_rna_properties(mapping)


def _color_mapping_payload(mapping: Any) -> dict[str, Any] | None:
    if mapping is None:
        return None
    payload = _simple_rna_properties(mapping)
    color_ramp = getattr(mapping, "color_ramp", None)
    if color_ramp is not None:
        payload["color_ramp"] = _color_ramp_payload(color_ramp)
    return payload


def _item_semantic_properties(item: Any) -> dict[str, Any]:
    """Capture semantic dynamic-item fields (name/type/domain/identifier)."""
    result: dict[str, Any] = {}
    try:
        properties = item.bl_rna.properties
    except Exception:
        return result
    deny = {"rna_type", "color", "select", "description", "label"}
    for prop in properties:
        identifier = getattr(prop, "identifier", None)
        if not identifier or identifier in deny:
            continue
        if getattr(prop, "type", "") not in _SCALAR_PROPERTY_TYPES:
            continue
        try:
            value = getattr(item, identifier)
        except Exception:
            continue
        result[identifier] = _serialize_value(value)
    return result


def _dynamic_item_collections_payload(node: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    try:
        properties = node.bl_rna.properties
    except Exception:
        return payload
    for prop in properties:
        identifier = getattr(prop, "identifier", None)
        if not identifier or getattr(prop, "type", "") != "COLLECTION":
            continue
        if identifier in {"inputs", "outputs", "internal_links", "panel_states"}:
            continue
        if identifier not in _DYNAMIC_ITEM_COLLECTIONS and not identifier.endswith("_items"):
            continue
        try:
            items = list(getattr(node, identifier))
        except Exception:
            continue
        payload[identifier] = [_item_semantic_properties(item) for item in items]
    return payload


def _node_semantics(node: Any, stack: tuple[int, ...]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "bl_idname": getattr(node, "bl_idname", ""),
        "mute": bool(getattr(node, "mute", False)),
        "properties": _simple_rna_properties(node),
        "inputs": [],
        "outputs": [],
        "dynamic_items": _dynamic_item_collections_payload(node),
        "pointer_dependencies": _node_pointer_dependencies(node),
    }
    color_ramp = getattr(node, "color_ramp", None)
    if color_ramp is not None:
        payload["color_ramp"] = _color_ramp_payload(color_ramp)
    mapping = getattr(node, "mapping", None)
    if mapping is not None:
        payload["mapping"] = _curve_mapping_payload(mapping)
    texture_mapping = getattr(node, "texture_mapping", None)
    if texture_mapping is not None:
        payload["texture_mapping"] = _tex_mapping_payload(texture_mapping)
    color_mapping = getattr(node, "color_mapping", None)
    if color_mapping is not None:
        payload["color_mapping"] = _color_mapping_payload(color_mapping)
    if hasattr(node, "is_active_output"):
        try:
            payload["is_active_output"] = bool(node.is_active_output)
        except Exception:
            pass

    try:
        inputs = list(node.inputs)
    except Exception:
        inputs = []
    for socket in inputs:
        try:
            links = list(getattr(socket, "links", []) or [])
            linked = any(not bool(getattr(link, "is_muted", False)) for link in links)
            if not links and bool(getattr(socket, "is_linked", False)):
                linked = True
        except Exception:
            linked = bool(getattr(socket, "is_linked", False))
        payload["inputs"].append(_socket_semantics(socket, linked=linked))

    try:
        outputs = list(node.outputs)
    except Exception:
        outputs = []
    for socket in outputs:
        payload["outputs"].append(
            {
                "name": getattr(socket, "name", ""),
                "identifier": getattr(socket, "identifier", ""),
                "type": getattr(socket, "type", ""),
            }
        )

    node_tree = getattr(node, "node_tree", None)
    if node_tree is not None:
        try:
            pointer = int(node_tree.as_pointer())
        except Exception:
            pointer = 0
        if pointer and pointer in stack:
            payload["nested_tree_cycle"] = True
        else:
            payload["nested_tree_fingerprint"] = _tree_payload(node_tree, stack=stack + (pointer,))
    return payload


def _link_semantics(tree: bpy.types.NodeTree, node_keys: dict[int, str]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    try:
        iterable = list(tree.links)
    except Exception:
        return links
    for link in iterable:
        try:
            from_pointer = int(link.from_node.as_pointer())
            to_pointer = int(link.to_node.as_pointer())
        except Exception:
            continue
        links.append(
            {
                "from_node": node_keys.get(from_pointer, ""),
                "from_socket": getattr(getattr(link, "from_socket", None), "identifier", ""),
                "to_node": node_keys.get(to_pointer, ""),
                "to_socket": getattr(getattr(link, "to_socket", None), "identifier", ""),
                "is_muted": bool(getattr(link, "is_muted", False)),
                "is_valid": bool(getattr(link, "is_valid", True)),
                "multi_input": bool(getattr(link, "multi_input", False)),
            }
        )
    links.sort(
        key=lambda item: (
            str(item["from_node"]),
            str(item["from_socket"]),
            str(item["to_node"]),
            str(item["to_socket"]),
            str(item["is_muted"]),
        )
    )
    return links


def _tree_payload(tree: bpy.types.NodeTree, *, stack: tuple[int, ...] = ()) -> dict[str, Any]:
    node_descriptors: list[dict[str, Any]] = []
    node_keys: dict[int, str] = {}
    try:
        nodes = list(tree.nodes)
    except Exception:
        nodes = []
    for node in nodes:
        try:
            pointer = int(node.as_pointer())
        except Exception:
            pointer = id(node)
        descriptor = _node_semantics(node, stack)
        node_descriptors.append(
            {
                "pointer": pointer,
                "descriptor": descriptor,
                "name": getattr(node, "name", ""),
                "node": node,
            }
        )

    # Node names are editor labels.  Sort by semantics and only use names as a
    # deterministic tie-breaker for otherwise identical nodes.
    node_descriptors.sort(
        key=lambda item: (
            canonical_json(item["descriptor"]),
            str(item["name"]),
        )
    )
    canonical_nodes: list[dict[str, Any]] = []
    canonical_by_key: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(node_descriptors):
        key = f"n{index}"
        node_keys[int(item["pointer"])] = key
        canonical = {"key": key, **item["descriptor"]}
        canonical_nodes.append(canonical)
        canonical_by_key[key] = canonical

    # Simulation/Repeat Input nodes store their output pairing as a Node
    # pointer.  Resolve it to the canonical node key so the pairing is part of
    # the semantic identity without leaking runtime pointers or node names.
    for item in node_descriptors:
        node = item["node"]
        paired = getattr(node, "paired_output", None)
        if paired is None:
            continue
        try:
            paired_pointer = int(paired.as_pointer())
        except Exception:
            paired_pointer = 0
        key = node_keys.get(int(item["pointer"]))
        if key is not None:
            canonical_by_key[key]["paired_output"] = node_keys.get(paired_pointer, "")

    payload = {
        "nodes": canonical_nodes,
        "links": _link_semantics(tree, node_keys),
        "interface": _interface_payload(tree),
        "animation": _animation_summary(tree),
    }
    return payload


def semantic_tree_payload(tree: bpy.types.NodeTree | None) -> dict[str, Any]:
    """Return the recursive, layout-independent semantic payload of a node tree."""
    if tree is None:
        return {"schema": IDENTITY_SCHEMA, "tree": None}
    try:
        pointer = int(tree.as_pointer())
    except Exception:
        pointer = 0
    return {
        "schema": IDENTITY_SCHEMA,
        "tree": _tree_payload(tree, stack=(pointer,) if pointer else ()),
    }


def semantic_tree_fingerprint(tree: bpy.types.NodeTree | None) -> str:
    """SHA256 of :func:`semantic_tree_payload`.

    This is the drop-in replacement for ``subgroups.tree_fingerprint``.  It
    ignores node locations, labels/names, colors and panel layout, while
    including nested subgroup trees, node operations, unlinked socket defaults,
    mute state and interface sockets/defaults.
    """
    return _sha256_text(canonical_json(semantic_tree_payload(tree)))


def _tree_reliability(
    tree: bpy.types.NodeTree | None,
    *,
    prefix: str,
    seen: set[int],
) -> list[str]:
    reasons: list[str] = []
    if tree is None:
        return reasons
    try:
        pointer = int(tree.as_pointer())
    except Exception:
        pointer = id(tree)
    if pointer in seen:
        return reasons
    seen.add(pointer)

    if _is_animated(tree):
        reasons.append(f"{prefix}:node_tree_animated")
    try:
        nodes = list(tree.nodes)
    except Exception:
        nodes = []
    for node in nodes:
        try:
            node_pointer = int(node.as_pointer())
        except Exception:
            node_pointer = id(node)
        if hasattr(node, "animation_data") and _is_animated(node):
            reasons.append(f"{prefix}:node_animated:{getattr(node, 'name', '')}")
        for dep in _node_pointer_dependencies(node):
            reasons.append(
                f"{prefix}:node_pointer_unverified:{dep.get('property', '')}:{dep.get('id_type', '')}:{dep.get('name', '')}"
            )
        nested = getattr(node, "node_tree", None)
        if nested is not None:
            try:
                nested_pointer = int(nested.as_pointer())
            except Exception:
                nested_pointer = id(nested)
            if nested_pointer not in seen:
                reasons.extend(
                    _tree_reliability(
                        nested,
                        prefix=f"{prefix}/{getattr(nested, 'name', 'subgroup')}",
                        seen=seen,
                    )
                )
    return reasons


def _attribute_value(attribute: Any, element: Any) -> Any:
    data_type = getattr(attribute, "data_type", "")
    if data_type == "FLOAT_VECTOR":
        return _vector_payload(getattr(element, "vector", ()))
    if data_type == "FLOAT_COLOR":
        return _vector_payload(getattr(element, "color", ()))
    if data_type == "BYTE_COLOR":
        return _vector_payload(getattr(element, "color", ()))
    if data_type == "STRING":
        return getattr(element, "value", "")
    if data_type == "BOOLEAN":
        return bool(getattr(element, "value", False))
    if data_type in {"INT", "INT8", "INT16", "INT32"}:
        return int(getattr(element, "value", 0))
    if data_type in {"FLOAT", "FLOAT2"}:
        value = getattr(element, "value", 0.0)
        return _serialize_value(value)
    if hasattr(element, "value"):
        return _serialize_value(element.value)
    if hasattr(element, "vector"):
        return _vector_payload(element.vector)
    return None


def _hash_mesh(hasher: "hashlib._Hash", mesh: bpy.types.Mesh) -> None:
    hasher.update(struct.pack("<I", len(mesh.vertices)))
    for vertex in mesh.vertices:
        co = vertex.co
        hasher.update(struct.pack("<3d", _round_float(co.x), _round_float(co.y), _round_float(co.z)))

    hasher.update(struct.pack("<I", len(mesh.edges)))
    for edge in mesh.edges:
        verts = tuple(edge.vertices)
        hasher.update(struct.pack("<2i", int(verts[0]), int(verts[1])))

    hasher.update(struct.pack("<I", len(mesh.loops)))
    for loop in mesh.loops:
        hasher.update(struct.pack("<2i", int(loop.vertex_index), int(loop.edge_index)))

    hasher.update(struct.pack("<I", len(mesh.polygons)))
    for polygon in mesh.polygons:
        hasher.update(
            struct.pack(
                "<3i?",
                int(polygon.loop_start),
                int(polygon.loop_total),
                int(polygon.material_index),
                bool(polygon.use_smooth),
            )
        )

    try:
        attributes = sorted(list(mesh.attributes), key=lambda attr: (str(attr.name), str(attr.domain), str(attr.data_type)))
    except Exception:
        attributes = []
    hasher.update(struct.pack("<I", len(attributes)))
    for attribute in attributes:
        hasher.update(
            canonical_json(
                {
                    "name": getattr(attribute, "name", ""),
                    "domain": getattr(attribute, "domain", ""),
                    "data_type": getattr(attribute, "data_type", ""),
                }
            ).encode("utf-8")
        )
        try:
            data = list(attribute.data)
        except Exception:
            data = []
        hasher.update(struct.pack("<I", len(data)))
        for element in data:
            hasher.update(canonical_json(_attribute_value(attribute, element)).encode("utf-8"))

    shape_keys = getattr(mesh, "shape_keys", None)
    if shape_keys is not None:
        key_blocks = list(shape_keys.key_blocks)
        hasher.update(struct.pack("<I", len(key_blocks)))
        for block in key_blocks:
            hasher.update(
                canonical_json(
                    {
                        "name": getattr(block, "name", ""),
                        "value": _round_float(float(getattr(block, "value", 0.0))),
                        "mute": bool(getattr(block, "mute", False)),
                        "relative_key": getattr(getattr(block, "relative_key", None), "name", None),
                    }
                ).encode("utf-8")
            )
            for point in block.data:
                co = point.co
                hasher.update(struct.pack("<3d", _round_float(co.x), _round_float(co.y), _round_float(co.z)))

    vertex_groups = list(getattr(mesh, "vertices", []))
    hasher.update(struct.pack("<I", len(vertex_groups)))
    for vertex in vertex_groups:
        groups = []
        for group in getattr(vertex, "groups", []):
            groups.append((int(getattr(group, "group", -1)), _round_float(float(getattr(group, "weight", 0.0)))))
        groups.sort()
        hasher.update(canonical_json(groups).encode("utf-8"))

    uv_layers = list(getattr(mesh, "uv_layers", []) or [])
    hasher.update(struct.pack("<I", len(uv_layers)))
    for layer in uv_layers:
        hasher.update(canonical_json({"name": getattr(layer, "name", "")}).encode("utf-8"))
        try:
            uv_data = list(layer.data)
        except Exception:
            uv_data = []
        hasher.update(struct.pack("<I", len(uv_data)))
        for uv in uv_data:
            hasher.update(struct.pack("<2d", _round_float(uv.uv.x), _round_float(uv.uv.y)))

    color_attributes = list(getattr(mesh, "color_attributes", []) or [])
    hasher.update(struct.pack("<I", len(color_attributes)))
    for attribute in color_attributes:
        hasher.update(
            canonical_json(
                {
                    "name": getattr(attribute, "name", ""),
                    "domain": getattr(attribute, "domain", ""),
                    "data_type": getattr(attribute, "data_type", ""),
                }
            ).encode("utf-8")
        )
        try:
            color_data = list(attribute.data)
        except Exception:
            color_data = []
        hasher.update(struct.pack("<I", len(color_data)))
        for element in color_data:
            hasher.update(canonical_json(_vector_payload(getattr(element, "color", ()))).encode("utf-8"))

    hasher.update(struct.pack("<I", len(getattr(mesh, "materials", []) or [])))
    for material in getattr(mesh, "materials", []) or []:
        hasher.update(canonical_json(_id_ref(material)).encode("utf-8"))


def _hash_curve(hasher: "hashlib._Hash", curve: bpy.types.Curve) -> None:
    for attr in (
        "dimensions",
        "resolution_u",
        "render_resolution_u",
        "bevel_depth",
        "bevel_resolution",
        "extrude",
        "offset",
        "fill_mode",
        "twist_mode",
        "use_fill_caps",
    ):
        if hasattr(curve, attr):
            try:
                hasher.update(canonical_json({attr: getattr(curve, attr)}).encode("utf-8"))
            except Exception:
                pass
    splines = list(curve.splines)
    hasher.update(struct.pack("<I", len(splines)))
    for spline in splines:
        hasher.update(canonical_json({"type": getattr(spline, "type", "")}).encode("utf-8"))
        points = list(getattr(spline, "points", []) or [])
        hasher.update(struct.pack("<I", len(points)))
        for point in points:
            co = point.co
            hasher.update(
                canonical_json(
                    {
                        "co": [_round_float(co.x), _round_float(co.y), _round_float(co.z), _round_float(co.w)],
                        "radius": _round_float(float(getattr(point, "radius", 1.0))),
                        "tilt": _round_float(float(getattr(point, "tilt", 0.0))),
                    }
                ).encode("utf-8")
            )
        bezier_points = list(getattr(spline, "bezier_points", []) or [])
        hasher.update(struct.pack("<I", len(bezier_points)))
        for point in bezier_points:
            hasher.update(
                canonical_json(
                    {
                        "co": _vector_payload(point.co),
                        "handle_left": _vector_payload(point.handle_left),
                        "handle_right": _vector_payload(point.handle_right),
                        "radius": _round_float(float(getattr(point, "radius", 1.0))),
                        "tilt": _round_float(float(getattr(point, "tilt", 0.0))),
                    }
                ).encode("utf-8")
            )
    if hasattr(curve, "attributes"):
        try:
            attributes = sorted(list(curve.attributes), key=lambda attr: str(attr.name))
        except Exception:
            attributes = []
        for attribute in attributes:
            hasher.update(
                canonical_json(
                    {
                        "name": getattr(attribute, "name", ""),
                        "domain": getattr(attribute, "domain", ""),
                        "data_type": getattr(attribute, "data_type", ""),
                    }
                ).encode("utf-8")
            )
            try:
                data = list(attribute.data)
            except Exception:
                data = []
            for element in data:
                hasher.update(canonical_json(_attribute_value(attribute, element)).encode("utf-8"))
    hasher.update(struct.pack("<I", len(getattr(curve, "materials", []) or [])))
    for material in getattr(curve, "materials", []) or []:
        hasher.update(canonical_json(_id_ref(material)).encode("utf-8"))


def _material_slots_payload(obj: bpy.types.Object) -> dict[str, Any]:
    """Describe the effective material slots in their real slot order.

    ``Object.material_slots`` is the only RNA view that exposes both the
    ``DATA``/``OBJECT`` link source and the material that is actually used for
    the slot.  A shared Mesh can therefore have different effective material
    bindings per Object.  Only slot identity is signed here; changing a
    material's nodes, settings or images is intentionally not a change.

    Unreadable or inconsistent RNA is reported as ``reliable: false`` with
    explicit reasons.  It is never collapsed into a convincing empty slot list.
    """
    payload: dict[str, Any] = {
        "schema": MATERIAL_SLOTS_SCHEMA,
        "source": "object.material_slots",
        "available": False,
        "reliable": True,
        "slot_count": None,
        "slots": [],
        "reasons": [],
    }
    reasons = payload["reasons"]
    try:
        slots = obj.material_slots
    except Exception as exc:
        reasons.append(f"material_slots_unreadable:{type(exc).__name__}")
        payload["reliable"] = False
        return payload
    if slots is None:
        reasons.append("material_slots_none")
        payload["reliable"] = False
        return payload
    try:
        slot_count = len(slots)
    except Exception as exc:
        reasons.append(f"material_slot_count_unreadable:{type(exc).__name__}")
        payload["reliable"] = False
        return payload
    payload["available"] = True
    payload["slot_count"] = slot_count

    data_materials: Any = None
    try:
        object_type = str(getattr(obj, "type", ""))
    except Exception:
        object_type = ""
    try:
        data = getattr(obj, "data", None)
    except Exception as exc:
        data = None
        reasons.append(f"material_slot_data_unreadable:{type(exc).__name__}")
    if data is not None:
        try:
            data_materials = getattr(data, "materials", None)
        except Exception as exc:
            reasons.append(f"material_data_materials_unreadable:{type(exc).__name__}")
        if data_materials is not None:
            try:
                data_material_count = len(data_materials)
            except Exception as exc:
                reasons.append(f"material_data_material_count_unreadable:{type(exc).__name__}")
            else:
                if data_material_count != slot_count:
                    reasons.append(
                        f"material_slot_count_mismatch:{slot_count}:{data_material_count}"
                    )
        elif object_type in {"MESH", "CURVE"}:
            reasons.append(f"material_data_materials_unavailable:{object_type}")

    for index in range(slot_count):
        record: dict[str, Any] = {"index": index, "link": None, "material": None}
        try:
            slot = slots[index]
        except Exception as exc:
            record["unreliable"] = True
            reasons.append(f"material_slot_unreadable:{index}:{type(exc).__name__}")
            payload["slots"].append(record)
            continue
        try:
            link = slot.link
        except Exception as exc:
            record["unreliable"] = True
            reasons.append(f"material_slot_link_unreadable:{index}:{type(exc).__name__}")
        else:
            link_text = str(link)
            if link_text not in {"DATA", "OBJECT"}:
                record["unreliable"] = True
                reasons.append(f"material_slot_link_invalid:{index}:{link_text}")
            else:
                record["link"] = link_text
        try:
            material = slot.material
        except Exception as exc:
            record["unreliable"] = True
            record["material_unreadable"] = type(exc).__name__
            reasons.append(f"material_slot_material_unreadable:{index}:{type(exc).__name__}")
        else:
            material_ref, material_errors = _material_id_ref(material)
            record["material"] = material_ref
            if material_errors:
                record["unreliable"] = True
                record["material_errors"] = material_errors
                for material_error in material_errors:
                    reasons.append(f"material_slot_material_invalid:{index}:{material_error}")
        payload["slots"].append(record)

    payload["reliable"] = not reasons
    return payload


def base_geometry_sha256(obj: bpy.types.Object) -> str:
    """Stable digest of base geometry, topology, attributes and material slots.

    The mesh/curve material list is hashed at DATA level here.  Effective
    ``Object.material_slots`` bindings, including OBJECT overrides, are hashed
    separately below so they also participate in the standalone input digest.
    Material contents are never part of this digest.
    """
    hasher = hashlib.sha256()
    hasher.update(GEOMETRY_SCHEMA.encode("utf-8"))
    hasher.update(str(getattr(obj, "type", "")).encode("utf-8"))
    hasher.update(canonical_json(_material_slots_payload(obj)).encode("utf-8"))
    data = getattr(obj, "data", None)
    if obj.type == "MESH" and isinstance(data, bpy.types.Mesh):
        _hash_mesh(hasher, data)
    elif obj.type == "CURVE" and isinstance(data, bpy.types.Curve):
        _hash_curve(hasher, data)
    else:
        hasher.update(canonical_json(_id_ref(data)).encode("utf-8"))
        if data is not None and hasattr(data, "attributes"):
            try:
                attributes = sorted(list(data.attributes), key=lambda attr: str(attr.name))
            except Exception:
                attributes = []
            for attribute in attributes:
                hasher.update(canonical_json(_id_ref(attribute)).encode("utf-8"))
    return hasher.hexdigest()


def geometry_summary(obj: bpy.types.Object) -> dict[str, Any]:
    data = getattr(obj, "data", None)
    summary: dict[str, Any] = {"type": getattr(obj, "type", ""), "data_type": getattr(getattr(data, "bl_rna", None), "identifier", "")}
    if obj.type == "MESH" and isinstance(data, bpy.types.Mesh):
        summary.update(
            {
                "vertices": len(data.vertices),
                "edges": len(data.edges),
                "loops": len(data.loops),
                "polygons": len(data.polygons),
                "attributes": len(getattr(data, "attributes", []) or []),
                "materials": len(getattr(data, "materials", []) or []),
            }
        )
    elif obj.type == "CURVE" and isinstance(data, bpy.types.Curve):
        summary.update(
            {
                "splines": len(data.splines),
                "points": sum(len(spline.points) + len(spline.bezier_points) for spline in data.splines),
            }
        )
    return summary


def _modifier_static_payload(modifier: bpy.types.Modifier) -> dict[str, Any]:
    return {
        "name": getattr(modifier, "name", ""),
        "type": getattr(modifier, "type", ""),
        "show_viewport": bool(getattr(modifier, "show_viewport", True)),
        "show_render": bool(getattr(modifier, "show_render", True)),
        "properties": _simple_rna_properties(
            modifier,
            extra_deny=_UI_MODIFIER_PROPERTIES,
        ),
        "node_group_fingerprint": semantic_tree_fingerprint(getattr(modifier, "node_group", None)),
    }


def _modifier_pointer_dependencies(modifier: bpy.types.Modifier) -> list[dict[str, Any]]:
    deps: list[dict[str, Any]] = []
    try:
        properties = modifier.bl_rna.properties
    except Exception:
        return deps
    for prop in properties:
        identifier = getattr(prop, "identifier", None)
        if not identifier or identifier in {"rna_type", "node_group", "bakes", "properties", "panels"}:
            continue
        if getattr(prop, "type", "") != "POINTER":
            continue
        try:
            value = getattr(modifier, identifier)
        except Exception:
            continue
        if isinstance(value, bpy.types.ID):
            ref = _id_ref(value)
            if ref is not None:
                ref["property"] = identifier
                deps.append(ref)
    return deps


def _prior_modifier_covered(modifier: bpy.types.Modifier) -> tuple[bool, str]:
    if not bool(getattr(modifier, "show_viewport", True)):
        return True, "disabled"
    modifier_type = str(getattr(modifier, "type", ""))
    if modifier_type == "NODES":
        return False, "geometry_nodes_prior_modifier"
    if modifier_type not in _COVERED_PRIOR_MODIFIER_TYPES:
        return False, f"type_not_covered:{modifier_type}"
    pointer_deps = _modifier_pointer_dependencies(modifier)
    if pointer_deps:
        return False, "external_pointer_dependency"
    return True, "deterministic_local_settings"


def _prior_modifiers(obj: bpy.types.Object, modifier: bpy.types.Modifier) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    try:
        modifiers = list(obj.modifiers)
    except Exception:
        return result
    try:
        target_index = next(index for index, item in enumerate(modifiers) if item == modifier)
    except StopIteration:
        target_index = len(modifiers)
    for index, prior in enumerate(modifiers[:target_index]):
        covered, reason = _prior_modifier_covered(prior)
        result.append(
            {
                "index": index,
                **_modifier_static_payload(prior),
                "pointer_dependencies": _modifier_pointer_dependencies(prior),
                "covered": covered,
                "coverage_reason": reason,
            }
        )
    return result


def _parent_chain(obj: bpy.types.Object) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    current = getattr(obj, "parent", None)
    while current is not None:
        try:
            pointer = int(current.as_pointer())
        except Exception:
            pointer = id(current)
        if pointer in seen:
            break
        seen.add(pointer)
        chain.append(
            {
                "name": getattr(current, "name", ""),
                "type": getattr(current, "type", ""),
                "matrix_world": _matrix_payload(getattr(current, "matrix_world", [])),
                "animation": _animation_summary(current),
                "constraints": _constraint_payload(current),
            }
        )
        current = getattr(current, "parent", None)
    return chain


def _object_reliability_reasons(
    obj: bpy.types.Object | None,
    *,
    prefix: str,
    seen: set[int],
    skip_modifier: bpy.types.Modifier | None = None,
    allow_empty: bool = True,
    allow_simple_transform_animation: bool = False,
) -> list[str]:
    reasons: list[str] = []
    if obj is None:
        return reasons
    try:
        pointer = int(obj.as_pointer())
    except Exception:
        pointer = id(obj)
    if pointer in seen:
        return reasons
    seen.add(pointer)

    object_type = str(getattr(obj, "type", ""))
    if object_type not in {"MESH", "CURVE"} and not (allow_empty and object_type == "EMPTY"):
        reasons.append(f"{prefix}:unsupported_geometry_type:{object_type}")
    for material_reason in _material_slots_payload(obj).get("reasons") or []:
        reasons.append(f"{prefix}:{material_reason}")
    if object_type == "CURVE":
        curve_data = getattr(obj, "data", None)
        for attr in ("bevel_object", "taper_object"):
            dependency = getattr(curve_data, attr, None) if curve_data is not None else None
            if dependency is not None:
                reasons.append(f"{prefix}:curve_{attr}_unverified:{getattr(dependency, 'name', '')}")

    signature = pnw_animation_identity.sign_object_transform(obj) if allow_simple_transform_animation else None
    simple_animated = bool(signature is not None and signature.get("animated"))
    if signature is not None:
        for reason in signature.get("reasons") or []:
            reasons.append(f"{prefix}:{reason}")
    if simple_animated:
        if object_type != "MESH":
            reasons.append(f"{prefix}:animated_collider_type_unsupported:{object_type}")
        for label, id_block in (
            ("data", getattr(obj, "data", None)),
            ("shape_keys", getattr(getattr(obj, "data", None), "shape_keys", None)),
        ):
            if id_block is not None and _is_animated(id_block):
                reasons.append(f"{prefix}:{label}_animated_or_driven")
        try:
            constraints = list(getattr(obj, "constraints", []) or [])
        except Exception:
            constraints = []
        if constraints:
            reasons.append(
                f"{prefix}:constraints_unsupported:"
                + ",".join(sorted(str(getattr(constraint, "name", "")) for constraint in constraints))
            )
        if getattr(obj, "rigid_body", None) is not None:
            reasons.append(f"{prefix}:rigid_body_unsupported")
        for reason in pnw_animation_identity.static_parent_reasons(obj):
            reasons.append(f"{prefix}:{reason}")
    else:
        for label, id_block in (
            ("object", obj),
            ("data", getattr(obj, "data", None)),
            ("shape_keys", getattr(getattr(obj, "data", None), "shape_keys", None)),
        ):
            if id_block is not None and _is_animated(id_block):
                reasons.append(f"{prefix}:{label}_animated_or_driven")
        active = _active_constraints(obj)
        if active:
            reasons.append(f"{prefix}:constraints:{','.join(active)}")

        current = getattr(obj, "parent", None)
        while current is not None:
            if _is_animated(current) or _is_animated(getattr(current, "data", None)):
                reasons.append(f"{prefix}:parent_animated:{getattr(current, 'name', '')}")
            parent_constraints = _active_constraints(current)
            if parent_constraints:
                reasons.append(f"{prefix}:parent_constraints:{getattr(current, 'name', '')}:{','.join(parent_constraints)}")
            current = getattr(current, "parent", None)

    try:
        modifiers = list(obj.modifiers)
    except Exception:
        modifiers = []
    for modifier in modifiers:
        if skip_modifier is not None and modifier == skip_modifier:
            continue
        if not bool(getattr(modifier, "show_viewport", True)):
            continue
        covered, reason = _prior_modifier_covered(modifier)
        if not covered:
            reasons.append(
                f"{prefix}:modifier_unverified:{getattr(modifier, 'name', '')}:{getattr(modifier, 'type', '')}:{reason}"
            )
        node_group = getattr(modifier, "node_group", None)
        if node_group is not None:
            reasons.extend(_tree_reliability(node_group, prefix=f"{prefix}:modifier:{getattr(modifier, 'name', '')}", seen=set()))

    instance_collection = getattr(obj, "instance_collection", None)
    if instance_collection is not None:
        reasons.extend(_collection_reliability_reasons(instance_collection, prefix=f"{prefix}:instance_collection", seen=seen))
    return reasons


def _collection_reliability_reasons(
    collection: bpy.types.Collection | None,
    *,
    prefix: str,
    seen: set[int],
    allow_simple_transform_animation: bool = False,
) -> list[str]:
    reasons: list[str] = []
    if collection is None:
        return reasons
    try:
        pointer = int(collection.as_pointer())
    except Exception:
        pointer = id(collection)
    if pointer in seen:
        return reasons
    seen.add(pointer)
    if _is_animated(collection):
        reasons.append(f"{prefix}:collection_animated")
    try:
        objects = list(collection.objects)
    except Exception:
        objects = []
    for obj in objects:
        reasons.extend(
            _object_reliability_reasons(
                obj,
                prefix=f"{prefix}:{getattr(obj, 'name', '')}",
                seen=seen,
                allow_simple_transform_animation=allow_simple_transform_animation,
            )
        )
    try:
        children = list(collection.children)
    except Exception:
        children = []
    for child in children:
        reasons.extend(
            _collection_reliability_reasons(
                child,
                prefix=f"{prefix}:{getattr(child, 'name', '')}",
                seen=seen,
                allow_simple_transform_animation=allow_simple_transform_animation,
            )
        )
    return reasons


def _object_payload(
    obj: bpy.types.Object,
    *,
    role: str,
    ancestors: frozenset[int] = frozenset(),
    allow_simple_transform_animation: bool = False,
) -> dict[str, Any]:
    try:
        pointer = int(obj.as_pointer())
    except Exception:
        pointer = id(obj)
    if pointer in ancestors:
        return {"role": role, "name": getattr(obj, "name", ""), "cycle": True}
    next_ancestors = ancestors | {pointer}
    data = getattr(obj, "data", None)
    signature = (
        pnw_animation_identity.sign_object_transform(obj)
        if allow_simple_transform_animation
        else None
    )
    animated_transform = bool(signature is not None and signature.get("animated"))
    parent_chain = (
        pnw_animation_identity.static_parent_chain_payload(obj)
        if animated_transform
        else _parent_chain(obj)
    )
    payload = {
        "role": role,
        "name": getattr(obj, "name", ""),
        "type": getattr(obj, "type", ""),
        "matrix_world": None if animated_transform else _matrix_payload(getattr(obj, "matrix_world", [])),
        "matrix_local": None if animated_transform else _matrix_payload(getattr(obj, "matrix_local", [])),
        "matrix_parent_inverse": _matrix_payload(getattr(obj, "matrix_parent_inverse", [])),
        "parent_chain": parent_chain,
        "constraints": _constraint_payload(obj),
        "animation": _animation_summary(obj),
        "data_animation": _animation_summary(data),
        "shape_keys_animation": _animation_summary(getattr(data, "shape_keys", None)),
        "base_geometry_sha256": base_geometry_sha256(obj),
        "geometry_summary": geometry_summary(obj),
        "material_slots": _material_slots_payload(obj),
        "modifiers": [_modifier_static_payload(modifier) for modifier in list(getattr(obj, "modifiers", []) or [])],
        "hide_viewport": bool(getattr(obj, "hide_viewport", False)),
        "hide_render": bool(getattr(obj, "hide_render", False)),
        "instance_type": getattr(obj, "instance_type", None),
        "instance_collection": _collection_payload(
            getattr(obj, "instance_collection", None),
            role=f"{role}:instance_collection",
            ancestors=next_ancestors,
        ),
    }
    if animated_transform:
        # The evaluated world/local matrices change with the current frame and
        # must never participate in a cache identity.  The static transform
        # channels, the bound-slot animation signature and the static parent
        # chain describe the transform for every frame instead.
        payload["evaluated_matrices_excluded"] = "simple_transform_animation"
        payload["transform_animation"] = signature.get("payload")
    return payload


def _collection_payload(
    collection: bpy.types.Collection | None,
    *,
    role: str,
    ancestors: frozenset[int] = frozenset(),
    allow_simple_transform_animation: bool = False,
) -> dict[str, Any] | None:
    if collection is None:
        return None
    try:
        pointer = int(collection.as_pointer())
    except Exception:
        pointer = id(collection)
    if pointer in ancestors:
        return {"role": role, "name": getattr(collection, "name", ""), "cycle": True}
    next_ancestors = ancestors | {pointer}
    objects = []
    try:
        iterable = sorted(list(collection.objects), key=lambda obj: str(getattr(obj, "name", "")))
    except Exception:
        iterable = []
    for obj in iterable:
        objects.append(
            _object_payload(
                obj,
                role=f"{role}:object:{getattr(obj, 'name', '')}",
                ancestors=next_ancestors,
                allow_simple_transform_animation=allow_simple_transform_animation,
            )
        )
    children = []
    try:
        child_iterable = sorted(list(collection.children), key=lambda child: str(getattr(child, "name", "")))
    except Exception:
        child_iterable = []
    for child in child_iterable:
        children.append(
            _collection_payload(
                child,
                role=f"{role}:child:{getattr(child, 'name', '')}",
                ancestors=next_ancestors,
                allow_simple_transform_animation=allow_simple_transform_animation,
            )
        )
    payload = {
        "role": role,
        "name": getattr(collection, "name", ""),
        "hide_viewport": bool(getattr(collection, "hide_viewport", False)),
        "hide_render": bool(getattr(collection, "hide_render", False)),
        "animation": _animation_summary(collection),
        "objects": objects,
        "children": children,
    }
    payload["content_sha256"] = _sha256_text(canonical_json(payload))
    return payload


def _custom_properties(id_block: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        items = list(id_block.items())
    except Exception:
        return result
    for key, value in sorted(items, key=lambda item: str(item[0])):
        if str(key).startswith("_RNA_UI"):
            continue
        try:
            result[str(key)] = _serialize_value(value)
        except Exception:
            result[str(key)] = str(value)
    return result


def _modifier_inputs_payload(modifier: bpy.types.Modifier) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    tree = getattr(modifier, "node_group", None)
    if tree is None:
        return payload
    try:
        items = list(tree.interface.items_tree)
    except Exception:
        return payload
    for item in items:
        if getattr(item, "item_type", None) != "SOCKET" or getattr(item, "in_out", None) != "INPUT":
            continue
        if getattr(item, "socket_type", None) == "NodeSocketGeometry":
            continue
        identifier = getattr(item, "identifier", None)
        if not identifier:
            continue
        socket_payload: dict[str, Any] = {
            "name": getattr(item, "name", ""),
            "identifier": identifier,
            "socket_type": getattr(item, "socket_type", ""),
            "value": None,
        }
        try:
            socket_prop = getattr(modifier.properties.inputs, identifier)
        except Exception:
            payload[str(getattr(item, "name", identifier))] = socket_payload
            continue
        if hasattr(socket_prop, "value"):
            try:
                socket_payload["value"] = _serialize_value(socket_prop.value)
            except Exception as exc:
                socket_payload["value_error"] = type(exc).__name__
        if hasattr(socket_prop, "attribute_name"):
            try:
                socket_payload["attribute_name"] = str(socket_prop.attribute_name)
            except Exception:
                pass
        payload[str(getattr(item, "name", identifier))] = socket_payload
    return payload


def _modifier_payload(modifier: bpy.types.Modifier) -> dict[str, Any]:
    return {
        "name": getattr(modifier, "name", ""),
        "type": getattr(modifier, "type", ""),
        "show_viewport": bool(getattr(modifier, "show_viewport", True)),
        "show_render": bool(getattr(modifier, "show_render", True)),
        "node_group": {
            "semantic_fingerprint": semantic_tree_fingerprint(getattr(modifier, "node_group", None)),
            "interface": _interface_payload(getattr(modifier, "node_group", None)) if getattr(modifier, "node_group", None) else [],
        },
        "inputs": _modifier_inputs_payload(modifier),
        "properties": _simple_rna_properties(
            modifier,
            extra_deny=_UI_MODIFIER_PROPERTIES,
        ),
    }


def _scene_payload(scene: bpy.types.Scene | None, frame_start: int, frame_end: int) -> dict[str, Any]:
    if scene is None:
        return {"available": False, "frame_start": frame_start, "frame_end": frame_end}
    render = getattr(scene, "render", None)
    unit_settings = getattr(scene, "unit_settings", None)
    payload: dict[str, Any] = {
        "available": True,
        "fps": int(getattr(render, "fps", 24)),
        "fps_base": _round_float(float(getattr(render, "fps_base", 1.0))),
        "frame_start": int(getattr(scene, "frame_start", frame_start)),
        "frame_end": int(getattr(scene, "frame_end", frame_end)),
        "frame_step": int(getattr(scene, "frame_step", 1)),
        "use_preview_range": bool(getattr(scene, "use_preview_range", False)),
        "frame_preview_start": int(getattr(scene, "frame_preview_start", frame_start)),
        "frame_preview_end": int(getattr(scene, "frame_preview_end", frame_end)),
        "use_gravity": bool(getattr(scene, "use_gravity", False)),
        "gravity": _vector_payload(getattr(scene, "gravity", ())),
        "unit_system": getattr(unit_settings, "system", None),
        "unit_scale_length": _round_float(float(getattr(unit_settings, "scale_length", 1.0))),
        "animation": _animation_summary(scene),
        "requested_frame_start": int(frame_start),
        "requested_frame_end": int(frame_end),
    }
    rigidbody_world = getattr(scene, "rigidbody_world", None)
    if rigidbody_world is not None:
        payload["rigidbody_world"] = {
            "enabled": bool(getattr(rigidbody_world, "enabled", False)),
            "collection": _id_ref(getattr(rigidbody_world, "collection", None)),
            "time_scale": _round_float(float(getattr(rigidbody_world, "time_scale", 1.0))),
            "substeps_per_frame": int(getattr(rigidbody_world, "substeps_per_frame", 1)),
            "solver_iterations": int(getattr(rigidbody_world, "solver_iterations", 1)),
            "use_split_impulse": bool(getattr(rigidbody_world, "use_split_impulse", False)),
        }
    return payload


def _implementation_state_payload(obj: bpy.types.Object, mod: bpy.types.Modifier | None) -> dict[str, Any]:
    """Read only the workflow implementation marker from core's object state.

    Cache ownership/bookkeeping properties such as ``pnw_cache_last_root__*``
    and ``pnw_cache_owner_binding__*`` are deliberately not part of this
    payload.  Only the implementation version affects solver semantics.
    """
    if mod is None:
        return {"implementation_version": None}
    keys = (f"pnw_state_json__{getattr(mod, 'name', '')}", "pnw_state_json")
    saw_state_without_version = False
    for key in keys:
        try:
            raw = obj.get(key)
        except Exception:
            continue
        if not isinstance(raw, str) or not raw:
            continue
        try:
            state = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(state, dict):
            if "implementation_version" in state:
                return {"implementation_version": _serialize_value(state.get("implementation_version"))}
            saw_state_without_version = True
    try:
        direct = obj.get("implementation_version")
    except Exception:
        direct = None
    if direct is not None or not saw_state_without_version:
        return {"implementation_version": _serialize_value(direct)}
    return {"implementation_version": None}


def _actual_id_value(modifier: bpy.types.Modifier, socket_name: str, expected_type: str) -> Any:
    tree = getattr(modifier, "node_group", None)
    if tree is None:
        return None
    try:
        items = list(tree.interface.items_tree)
    except Exception:
        return None
    for item in items:
        if getattr(item, "item_type", None) != "SOCKET" or getattr(item, "in_out", None) != "INPUT":
            continue
        if getattr(item, "name", None) != socket_name or getattr(item, "socket_type", None) != expected_type:
            continue
        identifier = getattr(item, "identifier", None)
        if not identifier:
            return None
        try:
            socket_prop = getattr(modifier.properties.inputs, identifier)
            value = getattr(socket_prop, "value", None)
        except Exception:
            return None
        return value if isinstance(value, bpy.types.ID) else None
    return None


def _actual_id_value_by_identifier(modifier: bpy.types.Modifier, identifier: str) -> Any:
    try:
        socket_prop = getattr(modifier.properties.inputs, identifier)
        value = getattr(socket_prop, "value", None)
    except Exception:
        return None
    return value if isinstance(value, bpy.types.ID) else None


def build_cache_identity(
    obj: bpy.types.Object,
    mod: bpy.types.Modifier | None,
    *,
    params_hash: str = "",
    graph_fingerprint: str = "",
    frame_start: int | None = None,
    frame_end: int | None = None,
) -> dict[str, Any]:
    """Build a deterministic cache identity and report whether it is reliable.

    ``params_hash`` and ``graph_fingerprint`` are recorded as *additional*
    caller-provided hashes.  They never replace the actual modifier values,
    graph semantics, geometry or dependency content captured below.
    """
    scene = bpy.context.scene
    if frame_start is None:
        frame_start = int(getattr(scene, "frame_start", 1)) if scene is not None else 1
    if frame_end is None:
        frame_end = int(getattr(scene, "frame_end", frame_start)) if scene is not None else frame_start
    frame_start = int(frame_start)
    frame_end = int(frame_end)

    reasons: list[str] = []
    if mod is None:
        reasons.append("modifier_missing")
    elif getattr(mod, "type", None) != "NODES":
        reasons.append(f"modifier_type:{getattr(mod, 'type', None)}")
    elif getattr(mod, "node_group", None) is None:
        reasons.append("modifier_has_no_node_group")

    if getattr(obj, "name", None) is None:
        reasons.append("target_missing")
    if scene is None:
        reasons.append("scene_missing")
    if frame_end < frame_start:
        reasons.append("invalid_frame_range")
    if not bool(getattr(obj, "users_scene", [])):
        reasons.append("target_not_in_scene")
    if mod is not None and not bool(getattr(mod, "show_viewport", True)):
        reasons.append("modifier_disabled")

    target_payload = _object_payload(obj, role="target") if getattr(obj, "name", None) is not None else None
    modifier_payload = _modifier_payload(mod) if mod is not None else None
    scene_payload = _scene_payload(scene, frame_start, frame_end)
    prior_modifiers = _prior_modifiers(obj, mod) if mod is not None else []

    if (scene_payload.get("animation") or {}).get("animated"):
        reasons.append("scene_animated_or_driven")
    if scene is not None and _is_animated(getattr(scene, "rigidbody_world", None)):
        reasons.append("rigidbody_world_animated_or_driven")

    reasons.extend(
        _object_reliability_reasons(
            obj,
            prefix="target",
            seen=set(),
            skip_modifier=mod,
            allow_empty=False,
        )
    )
    if mod is not None:
        reasons.extend(_tree_reliability(getattr(mod, "node_group", None), prefix="modifier", seen=set()))

    dependencies: dict[str, Any] = {}
    if mod is not None and isinstance(modifier_payload, dict):
        for socket_name, input_payload in (modifier_payload.get("inputs") or {}).items():
            socket_type = str(input_payload.get("socket_type", ""))
            identifier = input_payload.get("identifier")
            if socket_type not in {"NodeSocketObject", "NodeSocketCollection"} or not isinstance(identifier, str):
                continue
            id_block = _actual_id_value_by_identifier(mod, identifier)
            if id_block is None:
                continue
            role = f"socket:{socket_name}"
            if socket_type == "NodeSocketObject":
                allow_simple = socket_name == "Collider"
                dependencies[role] = _object_payload(
                    id_block,
                    role=role,
                    allow_simple_transform_animation=allow_simple,
                )
                reasons.extend(
                    _object_reliability_reasons(
                        id_block,
                        prefix=f"dependency:{socket_name}",
                        seen=set(),
                        allow_simple_transform_animation=allow_simple,
                    )
                )
            else:
                allow_simple = socket_name == "Collider Collection"
                dependencies[role] = _collection_payload(
                    id_block,
                    role=role,
                    allow_simple_transform_animation=allow_simple,
                )
                reasons.extend(
                    _collection_reliability_reasons(
                        id_block,
                        prefix=f"dependency:{socket_name}",
                        seen=set(),
                        allow_simple_transform_animation=allow_simple,
                    )
                )

    # Non-finite actual solver values are not a stable cache identity.
    if isinstance(modifier_payload, dict):
        for input_name, input_payload in modifier_payload.get("inputs", {}).items():
            raw = canonical_json(input_payload.get("value"))
            if "#nonfinite" in raw:
                reasons.append(f"nonfinite_modifier_value:{input_name}")

    reasons = sorted(set(reasons))
    payload = {
        "schema": IDENTITY_SCHEMA,
        "blender": {
            "version": getattr(bpy.app, "version_string", ""),
            "build_hash": getattr(bpy.app, "build_hash", b"").decode("ascii", "replace")
            if isinstance(getattr(bpy.app, "build_hash", b""), (bytes, bytearray))
            else str(getattr(bpy.app, "build_hash", "")),
        },
        "frames": {"start": frame_start, "end": frame_end},
        "target": target_payload,
        "modifier": modifier_payload,
        "implementation": _implementation_state_payload(obj, mod),
        "prior_modifiers": prior_modifiers,
        "dependencies": dependencies,
        "scene": scene_payload,
        "caller_hashes": {
            "parameters_hash": str(params_hash or ""),
            "graph_fingerprint": str(graph_fingerprint or ""),
        },
        "reliability": {"reliable": not reasons, "reasons": reasons},
    }
    # The caller-provided hashes are advisory/additional.  They must not decide
    # the identity hash or they could mask (or spuriously change) the actual
    # solver inputs captured above.
    hash_payload = dict(payload)
    hash_payload.pop("caller_hashes", None)
    payload["identity_hash"] = _sha256_text(canonical_json(hash_payload))
    return payload


def identity_reason(old_identity: dict[str, Any], new_identity: dict[str, Any]) -> str:
    """Return a human-readable reason why two identities differ."""
    if not isinstance(old_identity, dict) or not isinstance(new_identity, dict):
        return "cache_identity_changed"
    checks = (
        ("target_transform_changed", ("target", "matrix_world")),
        ("target_transform_changed", ("target", "matrix_local")),
        ("target_transform_changed", ("target", "parent_chain")),
        ("material_slots_changed", ("target", "material_slots")),
        ("input_mesh_changed", ("target", "base_geometry_sha256")),
        ("input_mesh_changed", ("target", "geometry_summary")),
        ("graph_changed", ("modifier", "node_group", "semantic_fingerprint")),
        ("modifier_values_changed", ("modifier", "inputs")),
        ("modifier_values_changed", ("modifier", "properties")),
        ("dependency_changed", ("dependencies",)),
        ("implementation_changed", ("implementation", "implementation_version")),
        ("scene_time_changed", ("scene",)),
        ("frame_range_changed", ("frames",)),
        ("prior_modifiers_changed", ("prior_modifiers",)),
        ("object_changed", ("target", "name")),
        ("object_changed", ("target", "type")),
        ("blender_version_changed", ("blender",)),
    )
    for label, path in checks:
        if _get_path(old_identity, path) != _get_path(new_identity, path):
            return label
    return "cache_identity_changed"


def _get_path(payload: Any, path: tuple[str, ...]) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current
