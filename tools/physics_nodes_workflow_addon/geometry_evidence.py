"""Plugin-owned full-geometry evidence sampler.

The summary intentionally hashes every evaluated mesh vertex/loop and every
instance matrix instead of relying on a centroid or bounding box.  Values are
quantized to six decimal places so a fresh Blender process can reproduce the
digest while still detecting real geometry, topology or instance changes.

This module is intentionally self-contained (bpy + standard library only) so
the addon remains independently installable.  The CLI adapter imports this
same module, keeping production inspection and cold-reopen verification on one
algorithm.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import bpy
from . import attribute_evidence


EVIDENCE_SCHEMA = "physics-geometry-evidence-v2"
QUANTIZATION_DECIMALS = 6
COUNT_FIELDS = ("vertex_count", "edge_count", "face_count", "loop_count", "instance_count")
DIGEST_FIELDS = (
    "vertices_sha256",
    "edges_sha256",
    "faces_sha256",
    "instances_sha256",
    "object_matrix_sha256",
    "geometry_sha256",
    "physics_attributes_sha256",
    "state_sha256",
)
REQUIRED_SUMMARY_FIELDS = COUNT_FIELDS + DIGEST_FIELDS + ("has_geometry",)
STATE_COVERAGE = {
    "mesh_geometry": True,
    "instance_geometry": True,
    "raw_pointcloud_geometry": False,
    "raw_curves_geometry": False,
    "note": (
        "geometry_sha256 preserves the v1 mesh/instance geometry algorithm. "
        "physics_attributes_sha256 covers pnw_ attributes on available top-level "
        "evaluated mesh, pointcloud, curves and INSTANCE components. It does not "
        "claim internal solver state or nested reference attribute coverage."
    ),
}


class GeometryEvidenceError(RuntimeError):
    """Raised when geometry cannot be sampled faithfully."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _canonical_float(value: Any, label: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise GeometryEvidenceError("NON_FINITE_GEOMETRY", f"{label} is not numeric: {value!r}") from exc
    if not math.isfinite(number):
        raise GeometryEvidenceError("NON_FINITE_GEOMETRY", f"{label} is not finite: {number!r}")
    if number == 0.0:
        number = 0.0
    return f"{number:.{QUANTIZATION_DECIMALS}f}"


def _hash_lines(lines) -> str:
    digest = hashlib.sha256()
    for line in lines:
        digest.update(str(line).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _matrix_rows(matrix) -> list[list[float]]:
    rows: list[list[float]] = []
    for row_index in range(4):
        row = matrix[row_index]
        rows.append([float(row[column]) for column in range(4)])
    return rows


def _matrix_digest(matrix) -> tuple[str, list[list[float]]]:
    rows = _matrix_rows(matrix)
    lines = []
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            lines.append(_canonical_float(value, f"matrix[{row_index}][{column_index}]"))
    return _hash_lines(lines), rows


def _mesh_data_summary(data: Any, cache: dict[int, dict[str, Any]]) -> dict[str, Any]:
    if data is None:
        return {
            "type": "NONE",
            "vertex_count": 0,
            "edge_count": 0,
            "face_count": 0,
            "vertices_sha256": _hash_lines([]),
            "edges_sha256": _hash_lines([]),
            "faces_sha256": _hash_lines([]),
        }

    key = int(data.as_pointer())
    cached = cache.get(key)
    if cached is not None:
        return cached

    data_type = getattr(data, "type", None)
    if not isinstance(data_type, str):
        data_type = type(data).__name__
    if (
        getattr(data, "bl_rna", None) is not None
        and hasattr(data, "vertices")
        and hasattr(data, "edges")
        and hasattr(data, "polygons")
    ):
        vertex_lines = []
        for vertex_index, vertex in enumerate(data.vertices):
            co = vertex.co
            vertex_lines.append(
                ",".join(
                    (
                        _canonical_float(co.x, f"instance_vertex[{vertex_index}].x"),
                        _canonical_float(co.y, f"instance_vertex[{vertex_index}].y"),
                        _canonical_float(co.z, f"instance_vertex[{vertex_index}].z"),
                    )
                )
            )
        edge_lines = []
        for edge in data.edges:
            first, second = (int(edge.vertices[0]), int(edge.vertices[1]))
            edge_lines.append(f"{min(first, second)},{max(first, second)}")
        face_lines = []
        for polygon in data.polygons:
            indices = ",".join(str(int(index)) for index in polygon.vertices)
            face_lines.append(f"{len(polygon.vertices)}:{int(polygon.material_index)}:{indices}")
        summary = {
            "type": str(data_type),
            "vertex_count": len(data.vertices),
            "edge_count": len(data.edges),
            "face_count": len(data.polygons),
            "vertices_sha256": _hash_lines(vertex_lines),
            "edges_sha256": _hash_lines(sorted(edge_lines)),
            "faces_sha256": _hash_lines(face_lines),
        }
    else:
        summary = {
            "type": str(data_type),
            "vertex_count": 0,
            "edge_count": 0,
            "face_count": 0,
            "vertices_sha256": _hash_lines([]),
            "edges_sha256": _hash_lines([]),
            "faces_sha256": _hash_lines([]),
        }
    cache[key] = summary
    return summary


def _reference_geometry_digest(reference, depsgraph, data_cache, active=()) -> str:
    """Bind an instance row to its evaluated prototype shape, not its slot number.

    Keep each borrowed GeometrySet alive until its components are consumed.
    Nested pnw attributes are deliberately excluded; geometry still identifies
    the prototype to which the top-level physics attribute belongs.
    """
    if reference is None:
        return _hash_lines(["NONE"])
    # GeometrySet is a Python wrapper, not an RNA ID with as_pointer().
    key = (type(reference).__name__, id(reference) if isinstance(reference, bpy.types.GeometrySet)
           else int(reference.as_pointer()))
    if key in active or len(active) >= 64:
        raise GeometryEvidenceError("UNSUPPORTED_INSTANCE_REFERENCE", "Cyclic or excessively nested reference")
    active = (*active, key)
    if isinstance(reference, bpy.types.Collection):
        children = []
        for child in reference.objects:
            children.append([_matrix_digest(child.matrix_world)[0],
                             _reference_geometry_digest(child, depsgraph, data_cache, active)])
        children.extend(["collection", _reference_geometry_digest(child, depsgraph, data_cache, active)]
                        for child in reference.children)
        return _hash_lines([json.dumps(attribute_evidence._plain(reference.instance_offset, "instance_offset")),
                            *sorted(json.dumps(row, sort_keys=True) for row in children)])
    if isinstance(reference, bpy.types.Object):
        evaluated = reference if reference.is_evaluated else reference.evaluated_get(depsgraph)
        geometry = evaluated.evaluated_geometry()
    elif isinstance(reference, bpy.types.GeometrySet):
        geometry = reference
    else:
        raise GeometryEvidenceError("UNSUPPORTED_INSTANCE_REFERENCE", type(reference).__name__)

    for name in ("volume", "grease_pencil"):
        if getattr(geometry, name, None) is not None:
            raise GeometryEvidenceError("UNSUPPORTED_INSTANCE_REFERENCE", f"Reference contains {name}")
    payload = {"mesh": _mesh_data_summary(geometry.mesh, {})}
    for name in ("pointcloud", "curves"):
        component = getattr(geometry, name)
        if component is None:
            payload[name] = None
            continue
        # Geometry attributes include position/radius and curve type/handles/
        # NURBS parameters. Curves additionally need their topology offsets.
        attributes = []
        for attr in sorted(component.attributes, key=lambda item: item.name):
            if attr.name.startswith("pnw_"):
                continue
            field = attribute_evidence.TYPE_FIELDS.get(attr.data_type)
            if field is None:
                raise GeometryEvidenceError("UNSUPPORTED_INSTANCE_REFERENCE", f"{name}.{attr.name}: {attr.data_type}")
            values = [attribute_evidence._plain(getattr(item, field), attr.name) for item in attr.data]
            attributes.append([attr.name, attr.domain, attr.data_type, values])
        payload[name] = {"attributes": attributes}
        if name == "curves":
            payload[name]["offsets"] = [int(item.value) for item in component.curve_offset_data]
    instances = geometry.instances_pointcloud()
    payload["instances"] = None
    if instances is not None:
        references = [_reference_geometry_digest(ref, depsgraph, data_cache, active)
                      for ref in geometry.instance_references()]
        transforms = instances.attributes.get("instance_transform")
        indices = instances.attributes.get(".reference_index")
        if transforms is None or indices is None or len(transforms.data) != len(indices.data):
            raise GeometryEvidenceError("INSTANCE_ATTRIBUTE_BINDING_MISSING", "Nested instance rows unavailable")
        rows = []
        for index, transform in enumerate(transforms.data):
            ref_index = int(indices.data[index].value)
            if not 0 <= ref_index < len(references):
                raise GeometryEvidenceError("INSTANCE_ATTRIBUTE_BINDING_MISSING", "Invalid nested reference index")
            rows.append([attribute_evidence._plain(transform.value, "instance_transform"), references[ref_index]])
        payload["instances"] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
    return _hash_lines([json.dumps(payload, sort_keys=True, separators=(",", ":"))])


def _is_instance_of(instance, obj: bpy.types.Object, eval_obj: bpy.types.Object) -> bool:
    parent = getattr(instance, "parent", None)
    if parent is None:
        return False
    if parent == eval_obj or parent == obj:
        return True
    original = getattr(parent, "original", None)
    return original is not None and original == obj


def _instance_entries(
    depsgraph,
    obj: bpy.types.Object,
    eval_obj: bpy.types.Object,
    data_cache: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for instance in depsgraph.object_instances:
        if not getattr(instance, "is_instance", False):
            continue
        if not _is_instance_of(instance, obj, eval_obj):
            continue

        matrix_digest, matrix_rows = _matrix_digest(instance.matrix_world)
        origin = [
            _canonical_float(instance.matrix_world[row][3], f"instance_origin[{row}]")
            for row in range(3)
        ]
        instance_object = getattr(instance, "object", None)
        data = getattr(instance_object, "data", None) if instance_object is not None else None
        data_summary = _mesh_data_summary(data, data_cache)
        entries.append(
            {
                "matrix": matrix_rows,
                "matrix_sha256": matrix_digest,
                "origin": origin,
                "data_type": data_summary["type"],
                "edge_count": data_summary["edge_count"],
                "edges_sha256": data_summary["edges_sha256"],
                "data_vertex_count": data_summary["vertex_count"],
                "data_edge_count": data_summary["edge_count"],
                "data_face_count": data_summary["face_count"],
                "data_vertices_sha256": data_summary["vertices_sha256"],
                "data_edges_sha256": data_summary["edges_sha256"],
                "data_faces_sha256": data_summary["faces_sha256"],
            }
        )

    entries.sort(key=lambda entry: json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return entries


def _evaluated_mesh_summary(eval_obj: bpy.types.Object) -> dict[str, Any]:
    mesh = None
    try:
        mesh = eval_obj.to_mesh()
        if mesh is None:
            return {
                "vertex_count": 0,
                "edge_count": 0,
                "face_count": 0,
                "loop_count": 0,
                "vertices_sha256": _hash_lines([]),
                "edges_sha256": _hash_lines([]),
                "faces_sha256": _hash_lines([]),
                "bbox_min": [0.0, 0.0, 0.0],
                "bbox_max": [0.0, 0.0, 0.0],
                "centroid": [0.0, 0.0, 0.0],
                "physics_attribute_names": [],
            }

        matrix = eval_obj.matrix_world.copy()
        vertex_lines = []
        sums = [0.0, 0.0, 0.0]
        minimum = [math.inf, math.inf, math.inf]
        maximum = [-math.inf, -math.inf, -math.inf]
        for vertex_index, vertex in enumerate(mesh.vertices):
            world = matrix @ vertex.co
            values = (
                _canonical_float(world.x, f"vertex[{vertex_index}].x"),
                _canonical_float(world.y, f"vertex[{vertex_index}].y"),
                _canonical_float(world.z, f"vertex[{vertex_index}].z"),
            )
            vertex_lines.append(",".join(values))
            for axis in range(3):
                component = float(world[axis])
                sums[axis] += component
                if component < minimum[axis]:
                    minimum[axis] = component
                if component > maximum[axis]:
                    maximum[axis] = component

        face_lines = []
        edge_lines = []
        for edge in mesh.edges:
            first, second = (int(edge.vertices[0]), int(edge.vertices[1]))
            edge_lines.append(f"{min(first, second)},{max(first, second)}")
        loop_count = 0
        for polygon in mesh.polygons:
            loop_count += len(polygon.vertices)
            indices = ",".join(str(int(index)) for index in polygon.vertices)
            face_lines.append(f"{len(polygon.vertices)}:{int(polygon.material_index)}:{indices}")

        vertex_count = len(mesh.vertices)
        if vertex_count:
            centroid = [round(sums[axis] / vertex_count, QUANTIZATION_DECIMALS) for axis in range(3)]
            bbox_min = [round(minimum[axis], QUANTIZATION_DECIMALS) for axis in range(3)]
            bbox_max = [round(maximum[axis], QUANTIZATION_DECIMALS) for axis in range(3)]
        else:
            centroid = [0.0, 0.0, 0.0]
            bbox_min = [0.0, 0.0, 0.0]
            bbox_max = [0.0, 0.0, 0.0]

        return {
            "vertex_count": vertex_count,
            "edge_count": len(mesh.edges),
            "face_count": len(mesh.polygons),
            "loop_count": loop_count,
            "vertices_sha256": _hash_lines(vertex_lines),
            "edges_sha256": _hash_lines(sorted(edge_lines)),
            "faces_sha256": _hash_lines(face_lines),
            "bbox_min": bbox_min,
            "bbox_max": bbox_max,
            "centroid": centroid,
            "physics_attribute_names": sorted(
                attribute.name for attribute in mesh.attributes if attribute.name.startswith("pnw_")
            ),
        }
    finally:
        if mesh is not None:
            eval_obj.to_mesh_clear()


def sample_frame(obj: bpy.types.Object, frame: int, *, velocity_threshold=0.05) -> dict[str, Any]:
    """Sample one frame and return a complete deterministic geometry summary."""

    if not isinstance(frame, int) or isinstance(frame, bool):
        raise GeometryEvidenceError("INVALID_FRAME", f"Frame must be an integer, got {frame!r}")

    scene = bpy.context.scene
    scene.frame_set(frame)
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(depsgraph)

    mesh_summary = _evaluated_mesh_summary(eval_obj)
    data_cache: dict[int, dict[str, Any]] = {}
    instances = _instance_entries(depsgraph, obj, eval_obj, data_cache)
    instances_sha256 = _hash_lines(
        json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True) for entry in instances
    )
    object_matrix_sha256, object_matrix = _matrix_digest(eval_obj.matrix_world)

    instance_count = len(instances)
    vertex_count = int(mesh_summary["vertex_count"])
    edge_count = int(mesh_summary["edge_count"])
    face_count = int(mesh_summary["face_count"])
    loop_count = int(mesh_summary["loop_count"])
    has_geometry = bool(vertex_count or instance_count)

    geometry_lines = [
        f"vertex_count={vertex_count}",
        f"edge_count={edge_count}",
        f"face_count={face_count}",
        f"loop_count={loop_count}",
        f"instance_count={instance_count}",
        f"vertices={mesh_summary['vertices_sha256']}",
        f"edges={mesh_summary['edges_sha256']}",
        f"faces={mesh_summary['faces_sha256']}",
        f"instances={instances_sha256}",
        f"object_matrix={object_matrix_sha256}",
    ]
    geometry_sha256 = _hash_lines(geometry_lines)
    try:
        attributes = attribute_evidence.sample_output_attributes(
            eval_obj, velocity_threshold=velocity_threshold,
            reference_digest=lambda ref: _reference_geometry_digest(ref, depsgraph, data_cache))
    except attribute_evidence.AttributeEvidenceError as exc:
        raise GeometryEvidenceError(exc.code, exc.message) from exc
    state_sha256 = _hash_lines([EVIDENCE_SCHEMA, geometry_sha256, attributes["sha256"]])

    if vertex_count:
        centroid = list(mesh_summary["centroid"])
        bbox_min = list(mesh_summary["bbox_min"])
        bbox_max = list(mesh_summary["bbox_max"])
    elif instance_count:
        origins = [[float(value) for value in entry["origin"]] for entry in instances]
        centroid = [round(sum(origin[axis] for origin in origins) / instance_count, QUANTIZATION_DECIMALS) for axis in range(3)]
        bbox_min = [round(min(origin[axis] for origin in origins), QUANTIZATION_DECIMALS) for axis in range(3)]
        bbox_max = [round(max(origin[axis] for origin in origins), QUANTIZATION_DECIMALS) for axis in range(3)]
    else:
        centroid = [0.0, 0.0, 0.0]
        bbox_min = [0.0, 0.0, 0.0]
        bbox_max = [0.0, 0.0, 0.0]

    return {
        "frame": frame,
        "vertex_count": vertex_count,
        "edge_count": edge_count,
        "face_count": face_count,
        "loop_count": loop_count,
        "instance_count": instance_count,
        "has_geometry": has_geometry,
        "vertices_sha256": mesh_summary["vertices_sha256"],
        "edges_sha256": mesh_summary["edges_sha256"],
        "faces_sha256": mesh_summary["faces_sha256"],
        "instances_sha256": instances_sha256,
        "object_matrix_sha256": object_matrix_sha256,
        "geometry_sha256": geometry_sha256,
        "object_matrix": object_matrix,
        "centroid": centroid,
        "bbox_min": bbox_min,
        "bbox_max": bbox_max,
        "evidence_schema": EVIDENCE_SCHEMA,
        "physics_attribute_names": attributes["attribute_names"],
        "physics_attributes": attributes,
        "physics_attributes_sha256": attributes["sha256"],
        "state_sha256": state_sha256,
        "state_coverage": {**STATE_COVERAGE, **attributes["coverage"]},
    }


def sample_frames(obj: bpy.types.Object, frames: list[int] | tuple[int, ...]) -> dict[str, Any]:
    """Sample all requested frames and return the production evidence envelope."""

    frame_list = sorted(set(int(frame) for frame in frames))
    if not frame_list:
        raise GeometryEvidenceError("MISSING_FRAMES", "At least one frame is required for evidence sampling")
    summaries: dict[str, dict[str, Any]] = {}
    for frame in frame_list:
        summaries[str(frame)] = sample_frame(obj, frame)
    return {
        "ok": True,
        "schema": EVIDENCE_SCHEMA,
        "quantization_decimals": QUANTIZATION_DECIMALS,
        "object_name": obj.name,
        "frames": summaries,
    }
