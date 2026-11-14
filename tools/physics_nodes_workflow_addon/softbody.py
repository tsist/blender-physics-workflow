"""Closed-surface softbody input preflight.

The softbody preset is a surface-only elastic shell.  It reuses the verified
cloth XPBD edge-length and cross-edge bending constraints; it is deliberately
not a tetrahedral solid, an incompressible body, or a pressure solver.

The solver consumes the raw mesh before the managed PhysicsNodes modifier.
Enabled modifiers above PhysicsNodes would change that topology, so they are
rejected instead of being evaluated and truncated here.  Modifiers after the
managed workflow remain allowed as post-processing.

This module only reads the target.  It must run before any modifier, node
group, bake identity or cache identity is created so a rejected input cannot
damage the source or a previously authored workflow.
"""

from __future__ import annotations

import math
from typing import Any

import bpy


SOFTBODY_PRESET_IDS = ("softbody_elastic_shell",)
MIN_VOLUME_M3 = 1.0e-9
MIN_EDGE_LENGTH_M = 1.0e-9
MIN_FACE_AREA_M2 = 1.0e-12


class SoftbodyValidationError(ValueError):
    """Raised when a closed softbody shell preflight fails."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def is_softbody_request(request: dict[str, Any]) -> bool:
    return str(request.get("preset_id", "")) in SOFTBODY_PRESET_IDS


def _upstream_enabled_modifiers(obj: bpy.types.Object, request: dict[str, Any]) -> tuple[str, int, list[dict], list[str]]:
    requested_name = str(request.get("modifier_name") or "PhysicsNodes")
    modifiers = list(obj.modifiers)
    requested_index = len(modifiers)
    for index, modifier in enumerate(modifiers):
        if modifier.name == requested_name:
            requested_index = index
            break
    upstream = []
    for index, modifier in enumerate(modifiers):
        if index >= requested_index:
            break
        if modifier.show_viewport or modifier.show_render:
            upstream.append({
                "index": index,
                "name": modifier.name,
                "type": modifier.type,
                "show_viewport": bool(modifier.show_viewport),
                "show_render": bool(modifier.show_render),
            })
    return requested_name, requested_index, upstream, [modifier.name for modifier in modifiers]


def _edge_key(first: int, second: int) -> tuple[int, int]:
    return (first, second) if first < second else (second, first)


def _world_volume(mesh: bpy.types.Mesh, matrix) -> float:
    """Absolute world-space volume of a consistently wound triangle mesh."""
    corners = [matrix @ vertex.co for vertex in mesh.vertices]
    total = 0.0
    for polygon in mesh.polygons:
        indices = polygon.vertices
        if len(indices) != 3:
            continue
        a, b, c = (corners[int(index)] for index in indices)
        total += a.dot(b.cross(c)) / 6.0
    return abs(total)


def _topology_issues(mesh: bpy.types.Mesh) -> tuple[list[str], dict[str, Any]]:
    issues: list[str] = []
    vertex_count = len(mesh.vertices)
    face_count = len(mesh.polygons)
    edge_faces: dict[tuple[int, int], list[tuple[int, tuple[int, int]]]] = {}
    vertex_faces: list[list[int]] = [[] for _ in range(vertex_count)]
    triangle_faces = 0
    degenerate_faces = 0
    min_edge_length = math.inf

    for face_index, polygon in enumerate(mesh.polygons):
        vertices = [int(index) for index in polygon.vertices]
        if len(vertices) == 3:
            triangle_faces += 1
        else:
            issues.append(f"face {face_index} has {len(vertices)} sides, expected 3")
        if len(set(vertices)) != len(vertices):
            degenerate_faces += 1
            issues.append(f"face {face_index} repeats a vertex")
        if polygon.area <= MIN_FACE_AREA_M2:
            degenerate_faces += 1
            issues.append(f"face {face_index} has non-positive area")
        for vertex_index in vertices:
            if 0 <= vertex_index < vertex_count:
                vertex_faces[vertex_index].append(face_index)
        for offset, first in enumerate(vertices):
            second = vertices[(offset + 1) % len(vertices)]
            key = _edge_key(first, second)
            edge_faces.setdefault(key, []).append((face_index, (first, second)))
            if 0 <= first < vertex_count and 0 <= second < vertex_count:
                length = (mesh.vertices[first].co - mesh.vertices[second].co).length
                if length < min_edge_length:
                    min_edge_length = length

    loose_vertices = [index for index, faces in enumerate(vertex_faces) if not faces]
    if loose_vertices:
        issues.append(f"{len(loose_vertices)} loose vertices")

    mesh_edge_keys = [_edge_key(int(edge.vertices[0]), int(edge.vertices[1])) for edge in mesh.edges]
    mesh_edge_set = set(mesh_edge_keys)
    face_edge_set = set(edge_faces)
    loose_edges = sorted(mesh_edge_set - face_edge_set)
    undeclared_edges = sorted(face_edge_set - mesh_edge_set)
    if loose_edges:
        issues.append(f"{len(loose_edges)} loose edges")
    if undeclared_edges:
        issues.append(f"{len(undeclared_edges)} face edges missing from the mesh edge table")
    if len(mesh_edge_keys) != len(mesh_edge_set):
        issues.append("duplicate mesh edges")

    boundary_edges = 0
    non_manifold_edges = 0
    inconsistent_winding = 0
    for key, incident in edge_faces.items():
        if len(incident) == 1:
            boundary_edges += 1
        elif len(incident) > 2:
            non_manifold_edges += 1
        elif len(incident) == 2:
            first_direction = incident[0][1]
            second_direction = incident[1][1]
            if first_direction == second_direction:
                inconsistent_winding += 1
    if boundary_edges:
        issues.append(f"{boundary_edges} open boundary edges")
    if non_manifold_edges:
        issues.append(f"{non_manifold_edges} non-manifold edges shared by more than two faces")
    if inconsistent_winding:
        issues.append(f"{inconsistent_winding} edges with inconsistent face winding")

    edges_at_vertex: list[list[tuple[int, int]]] = [[] for _ in range(vertex_count)]
    for key in edge_faces:
        for endpoint in key:
            if 0 <= endpoint < vertex_count:
                edges_at_vertex[endpoint].append(key)

    # A closed 2-manifold needs a single disk-like fan around every vertex.
    # Edge counts alone miss bow-tie vertices shared by two otherwise closed
    # surface fans.
    non_manifold_vertices = 0
    for vertex_index, faces in enumerate(vertex_faces):
        if not faces:
            continue
        face_set = set(faces)
        adjacency: dict[int, set[int]] = {face: set() for face in faces}
        for face in faces:
            for key in edges_at_vertex[vertex_index]:
                incident = edge_faces[key]
                if len(incident) != 2:
                    continue
                face_ids = {item[0] for item in incident}
                if face in face_ids:
                    other = next(iter(face_ids - {face}), None)
                    if other is not None and other in face_set:
                        adjacency[face].add(other)
        if any(len(neighbours) != 2 for neighbours in adjacency.values()):
            non_manifold_vertices += 1
            continue
        seen: set[int] = set()
        stack = [faces[0]]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(adjacency[current] - seen)
        if len(seen) != len(faces):
            non_manifold_vertices += 1
    if non_manifold_vertices:
        issues.append(f"{non_manifold_vertices} non-manifold vertices")

    if min_edge_length <= MIN_EDGE_LENGTH_M:
        issues.append("zero-length edge")

    return issues, {
        "vertex_count": vertex_count,
        "edge_count": len(mesh.edges),
        "face_count": face_count,
        "triangle_face_count": triangle_faces,
        "boundary_edge_count": boundary_edges,
        "non_manifold_edge_count": non_manifold_edges,
        "non_manifold_vertex_count": non_manifold_vertices,
        "loose_vertex_count": len(loose_vertices),
        "loose_edge_count": len(loose_edges),
        "inconsistent_winding_edge_count": inconsistent_winding,
        "degenerate_face_count": degenerate_faces,
        "min_edge_length_m": min_edge_length if math.isfinite(min_edge_length) else 0.0,
    }


def validate_softbody_input(obj: bpy.types.Object, request: dict[str, Any]) -> dict[str, Any]:
    """Validate a closed triangulated manifold before workflow IDs are created.

    Non-softbody presets are untouched.  For the softbody preset this function
    raises ``SoftbodyValidationError`` with a stable code and leaves the object
    and scene data exactly as it found them.
    """
    if not is_softbody_request(request):
        return {"checked": False, "softbody": False}

    if obj is None:
        raise SoftbodyValidationError("softbody_not_mesh", "Softbody requires an existing Mesh object")
    if obj.library is not None or (obj.data is not None and obj.data.library is not None):
        raise SoftbodyValidationError(
            "softbody_linked_input",
            "Softbody requires a local Mesh datablock; create a local working copy first",
            {"object": obj.name, "object_library": bool(obj.library), "data_library": bool(obj.data and obj.data.library)},
        )
    if obj.type != "MESH" or not isinstance(obj.data, bpy.types.Mesh):
        raise SoftbodyValidationError(
            "softbody_not_mesh",
            f"Softbody requires a Mesh target, got {obj.type}",
            {"object": obj.name, "type": obj.type},
        )
    if obj.mode != "OBJECT":
        raise SoftbodyValidationError(
            "softbody_not_object_mode",
            "Softbody preflight requires Object Mode",
            {"object": obj.name, "mode": obj.mode},
        )

    mesh = obj.data
    if len(mesh.vertices) == 0:
        raise SoftbodyValidationError(
            "softbody_empty_mesh",
            "Softbody requires a non-empty Mesh",
            {"object": obj.name, "vertices": 0},
        )
    if len(mesh.polygons) == 0:
        raise SoftbodyValidationError(
            "softbody_empty_mesh",
            "Softbody requires a closed surface with faces",
            {"object": obj.name, "vertices": len(mesh.vertices), "faces": 0},
        )

    requested_name, requested_index, upstream, modifier_order = _upstream_enabled_modifiers(obj, request)
    if upstream:
        names = ", ".join(repr(item["name"]) for item in upstream)
        raise SoftbodyValidationError(
            "softbody_upstream_modifier_unsupported",
            "softbody_upstream_modifier_unsupported: Softbody requires the raw closed Mesh input; "
            f"enabled modifier(s) {names} precede {requested_name!r}. "
            "Only post-processing modifiers after the managed PhysicsNodes are supported.",
            {
                "object": obj.name,
                "requested_modifier": requested_name,
                "requested_modifier_index": requested_index,
                "modifier_order": modifier_order,
                "upstream_modifiers": upstream,
            },
        )

    for vertex_index, vertex in enumerate(mesh.vertices):
        if not all(math.isfinite(float(component)) for component in vertex.co):
            raise SoftbodyValidationError(
                "softbody_non_finite_geometry",
                f"Softbody mesh vertex {vertex_index} has non-finite coordinates",
                {"object": obj.name, "vertex": vertex_index},
            )

    issues, metrics = _topology_issues(mesh)
    if issues:
        code = "softbody_not_closed_manifold"
        if any("loose" in issue for issue in issues):
            code = "softbody_loose_geometry"
        elif any("sides, expected 3" in issue for issue in issues):
            code = "softbody_not_triangulated"
        raise SoftbodyValidationError(
            code,
            "Softbody requires a closed triangulated manifold with no loose vertices or edges: "
            + "; ".join(issues[:8]),
            {"object": obj.name, **metrics, "issues": issues},
        )

    volume = _world_volume(mesh, obj.matrix_world)
    if not math.isfinite(volume) or volume <= MIN_VOLUME_M3:
        raise SoftbodyValidationError(
            "softbody_zero_volume",
            f"Softbody closed shell must enclose a nonzero volume (got {volume:.12g} m^3)",
            {"object": obj.name, **metrics, "volume_m3": volume},
        )

    return {
        "checked": True,
        "softbody": True,
        "preset_id": request.get("preset_id"),
        "object": obj.name,
        "mesh": mesh.name,
        "local": True,
        "raw_input": True,
        "upstream_modifier_count": 0,
        "closed": True,
        "triangulated": True,
        "manifold": True,
        "volume_m3": float(volume),
        **metrics,
    }
