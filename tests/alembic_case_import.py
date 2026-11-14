"""Independently reimport an Alembic archive and compare dynamic geometry."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import bpy
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import export_test_common as common  # noqa: E402


TOLERANCE = 2e-4


def sample_import(scene, frames):
    rows = []
    for frame in frames:
        scene.frame_set(frame)
        dg = bpy.context.evaluated_depsgraph_get()
        points = []
        objects = []
        components = []
        for obj in scene.objects:
            if obj.type != "MESH":
                continue
            evaluated = obj.evaluated_get(dg)
            mesh = evaluated.to_mesh()
            try:
                matrix = obj.matrix_world.copy()
                local_points = [[float(v) for v in (matrix @ vert.co)] for vert in mesh.vertices]
                points.extend(local_points)
                components.extend(common.connected_component_centroids(mesh, matrix))
                objects.append({
                    "name": obj.name,
                    "type": obj.type,
                    "parent": obj.parent.name if obj.parent else None,
                    "matrix_world": [[round(float(v), 9) for v in row] for row in obj.matrix_world],
                    "vertex_count": len(mesh.vertices),
                    "face_count": len(mesh.polygons),
                    "data_filepath": getattr(getattr(obj, "data", None), "filepath", None),
                })
            finally:
                evaluated.to_mesh_clear()
        rows.append({
            "frame": int(frame),
            "objects": objects,
            "vertex_count": len(points),
            "face_count": sum(item["face_count"] for item in objects),
            "world_vertices": [[round(v, 9) for v in p] for p in points],
            "bbox": common.bbox(points),
            "centroid": common.centroid(points),
            "component_centroids": components,
        })
    return rows


def array(points):
    return np.asarray(points, dtype=np.float64)


def nearest_distance(point, candidates):
    if not candidates:
        return float("inf")
    return min(
        float(np.linalg.norm(np.asarray(point, dtype=np.float64) - np.asarray(candidate, dtype=np.float64)))
        for candidate in candidates
    )


def main() -> None:
    args = common.parse_args()
    source = json.loads(Path(args.source).read_text(encoding="utf-8"))
    frames = list(range(int(source["export_range"][0]), int(source["export_range"][1]) + 1))
    source_by_frame = {row["frame"]: row for row in source["source_frames"]}

    common.reset_scene()
    import_result = bpy.ops.wm.alembic_import(filepath=str(args.abc), as_background_job=False)
    scene = bpy.context.scene
    imported_rows = sample_import(scene, frames)

    checks = []
    component_checks = []
    for imported in imported_rows:
        frame = imported["frame"]
        expected = source_by_frame[frame]
        expected_points = array(expected["world_vertices"])
        imported_points = array(imported["world_vertices"])
        count_equal = len(expected_points) == len(imported_points)
        sorted_error = None
        if count_equal and len(expected_points):
            expected_sorted = expected_points[np.lexsort(expected_points.T[::-1])]
            imported_sorted = imported_points[np.lexsort(imported_points.T[::-1])]
            sorted_error = float(np.max(np.abs(expected_sorted - imported_sorted)))
        bbox_error = None
        if expected["bbox"] and imported["bbox"]:
            bbox_error = float(np.max(np.abs(array(expected["bbox"]) - array(imported["bbox"]))))
        centroid_error = None
        if expected["centroid"] and imported["centroid"]:
            centroid_error = float(np.max(np.abs(array(expected["centroid"]) - array(imported["centroid"]))))
        row = {
            "frame": frame,
            "expected_vertex_count": len(expected_points),
            "imported_vertex_count": len(imported_points),
            "count_equal": count_equal,
            "sorted_vertex_max_error": sorted_error,
            "bbox_max_error": bbox_error,
            "centroid_max_error": centroid_error,
            "imported_objects": imported["objects"],
            "imported_component_count": len(imported["component_centroids"]),
        }
        row["pass"] = bool(
            count_equal
            and sorted_error is not None
            and sorted_error <= TOLERANCE
            and bbox_error is not None
            and bbox_error <= TOLERANCE
            and centroid_error is not None
            and centroid_error <= TOLERANCE
            and len(imported["objects"]) >= 1
        )
        checks.append(row)

        if source["case"] == "rigid":
            positions = expected.get("instance_positions") or []
            centroids = imported["component_centroids"]
            nearest = [nearest_distance(position, centroids) for position in positions]
            component_checks.append({
                "frame": frame,
                "source_instance_count": len(positions),
                "imported_component_count": len(centroids),
                "nearest_instance_to_component_max": max(nearest) if nearest else None,
                "nearest_distances": nearest,
                "pass": bool(centroids) and all(value <= 0.3 for value in nearest if value != float("inf")),
            })

    dynamic = None
    if len(imported_rows) >= 2:
        first, last = imported_rows[0], imported_rows[-1]
        dynamic = {
            "centroid_delta": float(np.linalg.norm(array(last["centroid"]) - array(first["centroid"]))),
            "bbox_min_delta": float(np.linalg.norm(array(last["bbox"][0]) - array(first["bbox"][0]))),
            "bbox_max_delta": float(np.linalg.norm(array(last["bbox"][1]) - array(first["bbox"][1]))),
        }
        dynamic["pass"] = max(dynamic["centroid_delta"], dynamic["bbox_min_delta"], dynamic["bbox_max_delta"]) > 0.005

    primary_matrix = checks[0]["imported_objects"][0]["matrix_world"] if checks and checks[0]["imported_objects"] else None
    expected_matrix = source.get("target_matrix_world")
    matrix_error = None
    if primary_matrix and expected_matrix:
        matrix_error = float(np.max(np.abs(array(primary_matrix) - array(expected_matrix))))

    payload = {
        "ok": bool(
            "FINISHED" in list(import_result)
            and checks
            and all(row["pass"] for row in checks)
            and (dynamic is None or dynamic["pass"])
            and all(row["pass"] for row in component_checks)
            and (matrix_error is None or matrix_error <= TOLERANCE)
        ),
        "case": source["case"],
        "abc": str(args.abc),
        "import_result": list(import_result),
        "scene_frame_range": [int(scene.frame_start), int(scene.frame_end)],
        "frame_checks": checks,
        "component_checks": component_checks,
        "dynamic": dynamic,
        "target_matrix_max_error": matrix_error,
        "tolerance": TOLERANCE,
    }
    if args.out:
        common.write_json(Path(args.out), payload)
    print("ALEMBIC_IMPORT_CHECK_BEGIN")
    print(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True))
    print("ALEMBIC_IMPORT_CHECK_END")


main()
