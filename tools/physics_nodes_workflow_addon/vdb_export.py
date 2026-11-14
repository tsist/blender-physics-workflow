"""Independent OpenVDB surface level-set export.

This module deliberately owns only the VDB file boundary: it converts an
already realized Blender mesh into a narrow-band surface level set, writes it
to a private temporary file, validates that file with an independent
``openvdb.readAll`` pass, and only then promotes it to the requested path.

The Blender build ships the OpenVDB Python bindings used by the verified
``blenderctl`` hair-volume implementation.  In this build the module-level
``meshToLevelSet`` helper is absent; the equivalent supported API is
``FloatGrid.createLevelSetFromPolygons`` with world-space ``float32`` points,
``uint32`` triangle indices and an explicit linear transform.

Semantics are intentionally explicit: the grid is the surface level set of the
given frame's realized **world mesh**.  It is not the native density grid of a
smoke/fluid domain, and no density values are fabricated from the mesh.
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import tempfile
from typing import Any

import bpy
import numpy as np


GRID_NAME = "surface"
GRID_TYPE = "FloatGrid"
GRID_CLASS = "LEVEL_SET"
LEVEL_SET_HALF_WIDTH = 3.0

# Bounds are derived from the actual evaluated mesh extent, never from a
# caller-supplied dense resolution.  They cap the bounding box that the level
# set construction may touch and the narrow-band grid that may be written.
MAX_BOUNDING_VOXELS = 16_777_216
MAX_ACTIVE_VOXELS = 8_388_608
MAX_GRID_BYTES = 512 * 1024 * 1024
MAX_VERTICES = 4_000_000
MAX_TRIANGLES = 2_000_000
MAX_INDEX_MAGNITUDE = 100_000_000
MIN_EXTENT_FACTOR = 1e-9


class VdbExportError(ValueError):
    """Explicit, user-facing VDB export failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _openvdb():
    if not bool(getattr(bpy.app.build_options, "openvdb", False)):
        raise VdbExportError(
            "UNSUPPORTED_EXPORT",
            "this Blender build was compiled without OpenVDB support",
        )
    try:
        import openvdb  # type: ignore[import-not-found]
    except ImportError as exc:
        raise VdbExportError(
            "UNSUPPORTED_EXPORT",
            "the bundled Blender Python environment has no OpenVDB module",
        ) from exc
    if not hasattr(openvdb.FloatGrid, "createLevelSetFromPolygons"):
        raise VdbExportError(
            "UNSUPPORTED_EXPORT",
            "the bundled OpenVDB module lacks FloatGrid.createLevelSetFromPolygons",
        )
    return openvdb


def _world_triangles(obj: bpy.types.Object, mesh: bpy.types.Mesh) -> tuple[np.ndarray, np.ndarray]:
    """Return world-space unique points and outward-wound triangle indices."""
    if mesh is None:
        raise VdbExportError("EMPTY_EXPORT", "evaluated geometry contains no mesh")
    vertex_count = len(mesh.vertices)
    if vertex_count == 0:
        raise VdbExportError("EMPTY_EXPORT", "evaluated mesh contains no vertices")
    if vertex_count > MAX_VERTICES:
        raise VdbExportError(
            "RESOURCE_LIMIT",
            f"evaluated mesh has {vertex_count} vertices; limit is {MAX_VERTICES}",
        )
    if len(mesh.polygons) == 0:
        raise VdbExportError("EMPTY_EXPORT", "evaluated mesh contains no faces")
    if len(mesh.polygons) > MAX_TRIANGLES:
        raise VdbExportError(
            "RESOURCE_LIMIT",
            f"evaluated mesh has {len(mesh.polygons)} faces; limit is {MAX_TRIANGLES}",
        )

    local = np.empty(vertex_count * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", local)
    local = local.reshape(vertex_count, 3)
    if not np.isfinite(local).all():
        raise VdbExportError("NONFINITE_EXPORT", "evaluated mesh has non-finite vertex coordinates")

    matrix = np.asarray(obj.matrix_world, dtype=np.float64)
    if not np.isfinite(matrix).all():
        raise VdbExportError("NONFINITE_EXPORT", "target object has a non-finite world matrix")
    if abs(float(np.linalg.det(matrix[:3, :3]))) < 1e-15:
        raise VdbExportError("DEGENERATE_EXPORT_MESH", "target world matrix is singular")
    world = local @ matrix[:3, :3].T + matrix[:3, 3]
    if not np.isfinite(world).all():
        raise VdbExportError("NONFINITE_EXPORT", "world-space mesh coordinates are non-finite")

    mesh.calc_loop_triangles()
    triangle_count = len(mesh.loop_triangles)
    if triangle_count == 0:
        raise VdbExportError("EMPTY_EXPORT", "evaluated mesh could not be triangulated")
    if triangle_count > MAX_TRIANGLES:
        raise VdbExportError(
            "RESOURCE_LIMIT",
            f"triangulated mesh has {triangle_count} triangles; limit is {MAX_TRIANGLES}",
        )
    triangles = np.empty(triangle_count * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", triangles)
    triangles = triangles.reshape(triangle_count, 3)
    if triangles.min() < 0 or triangles.max() >= vertex_count:
        raise VdbExportError("INVALID_EXPORT_MESH", "triangulation references out-of-range vertices")

    # Merge exactly coincident vertices.  Realized marching-cubes meshes can
    # carry duplicated seam vertices even though their surface is closed.
    unique, inverse = np.unique(world, axis=0, return_inverse=True)
    triangles = inverse[triangles.astype(np.int64)]

    # Loose vertices are not part of the surface and must not enlarge its
    # world bounding box or resource estimate.
    used = np.unique(triangles)
    if len(used) < len(unique):
        remap = np.full(len(unique), -1, dtype=np.int64)
        remap[used] = np.arange(len(used), dtype=np.int64)
        unique = unique[used]
        triangles = remap[triangles]
    if len(unique) < 4:
        raise VdbExportError("DEGENERATE_EXPORT_MESH", "mesh has fewer than four surface vertices")

    a = unique[triangles[:, 0]]
    b = unique[triangles[:, 1]]
    c = unique[triangles[:, 2]]
    cross = np.cross(b - a, c - a)
    area = np.linalg.norm(cross, axis=1)
    extent = unique.max(axis=0) - unique.min(axis=0)
    scale = float(max(extent.max(), 1e-30))
    keep = area > scale * scale * 1e-18
    if not keep.any():
        raise VdbExportError("DEGENERATE_EXPORT_MESH", "mesh has no non-degenerate triangles")
    if not keep.all():
        triangles = triangles[keep]
        used = np.unique(triangles)
        if len(used) < len(unique):
            remap = np.full(len(unique), -1, dtype=np.int64)
            remap[used] = np.arange(len(used), dtype=np.int64)
            unique = unique[used]
            triangles = remap[triangles]
        if len(unique) < 4:
            raise VdbExportError("DEGENERATE_EXPORT_MESH", "mesh has fewer than four surface vertices")
        a = unique[triangles[:, 0]]
        b = unique[triangles[:, 1]]
        c = unique[triangles[:, 2]]

    edges = np.concatenate(
        (triangles[:, (0, 1)], triangles[:, (1, 2)], triangles[:, (2, 0)]),
        axis=0,
    )
    edges.sort(axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    boundary = int((counts == 1).sum())
    nonmanifold = int((counts > 2).sum())
    if boundary or nonmanifold:
        raise VdbExportError(
            "UNSUPPORTED_EXPORT_TOPOLOGY",
            "surface level-set export requires a closed watertight mesh; "
            f"found {boundary} boundary edges and {nonmanifold} non-manifold edges",
        )

    # OpenVDB infers the interior from face winding.  Normalize a globally
    # inverted result; mixed per-shell orientation remains the caller's
    # responsibility and is reported by the verification step.
    signed_volume = float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)
    minimum_volume = scale ** 3 * 1e-12
    if abs(signed_volume) <= minimum_volume:
        raise VdbExportError(
            "DEGENERATE_EXPORT_MESH",
            "mesh signed volume is zero; a flat or open surface cannot form a level set",
        )
    if signed_volume < 0.0:
        triangles = triangles[:, (0, 2, 1)]
        a, b, c = a, c, b

    if (extent <= scale * MIN_EXTENT_FACTOR).any():
        raise VdbExportError(
            "DEGENERATE_EXPORT_MESH",
            "mesh is flat along at least one world axis; a volume level set needs three-dimensional extent",
        )

    return (
        np.ascontiguousarray(unique, dtype=np.float32),
        np.ascontiguousarray(triangles, dtype=np.uint32),
    )


def _bounded_index_box(world: np.ndarray, voxel_size: float) -> tuple[np.ndarray, np.ndarray, int]:
    band = LEVEL_SET_HALF_WIDTH * voxel_size
    low_world = world.min(axis=0) - band
    high_world = world.max(axis=0) + band
    low = np.floor(low_world / voxel_size).astype(np.int64)
    high = np.ceil(high_world / voxel_size).astype(np.int64)
    if max(abs(int(low.min())), abs(int(high.max()))) > MAX_INDEX_MAGNITUDE:
        raise VdbExportError(
            "RESOURCE_LIMIT",
            "world-space mesh is too far from the origin for a bounded OpenVDB index space",
        )
    dims = high - low + 1
    bounding = int(np.prod(dims, dtype=np.float64))
    if bounding > MAX_BOUNDING_VOXELS:
        raise VdbExportError(
            "RESOURCE_LIMIT",
            f"world bounds require {bounding} voxels at sdf_voxel_size={voxel_size}; "
            f"limit is {MAX_BOUNDING_VOXELS} (increase sdf_voxel_size or split the mesh)",
        )
    return low, high, bounding


def _verify_written_grid(
    openvdb,
    temp_path: Path,
    *,
    voxel_size: float,
    expected_world_min: np.ndarray,
    expected_world_max: np.ndarray,
) -> dict[str, Any]:
    grids, metadata = openvdb.readAll(str(temp_path))
    if len(grids) != 1:
        raise VdbExportError("VDB_VERIFY_FAILED", f"expected exactly one grid, read {len(grids)}")
    grid = grids[0]
    if not isinstance(grid, openvdb.FloatGrid):
        raise VdbExportError("VDB_VERIFY_FAILED", f"expected FloatGrid, read {type(grid).__name__}")
    if grid.name != GRID_NAME:
        raise VdbExportError("VDB_VERIFY_FAILED", f"expected grid name '{GRID_NAME}', read '{grid.name}'")
    if grid.gridClass != openvdb.GridClass.LEVEL_SET:
        raise VdbExportError("VDB_VERIFY_FAILED", f"expected LEVEL_SET, read {grid.gridClass}")
    active = int(grid.activeVoxelCount())
    if active <= 0:
        raise VdbExportError("EMPTY_VDB_LEVEL_SET", "OpenVDB produced no active voxels")
    if active > MAX_ACTIVE_VOXELS:
        raise VdbExportError(
            "RESOURCE_LIMIT",
            f"OpenVDB narrow band has {active} active voxels; limit is {MAX_ACTIVE_VOXELS}",
        )
    if int(grid.memUsage()) > MAX_GRID_BYTES:
        raise VdbExportError(
            "RESOURCE_LIMIT",
            f"OpenVDB grid uses {grid.memUsage()} bytes; limit is {MAX_GRID_BYTES}",
        )

    expected_background = LEVEL_SET_HALF_WIDTH * voxel_size
    background = float(grid.background)
    if not math.isfinite(background) or abs(background - expected_background) > max(1e-6, expected_background * 1e-5):
        raise VdbExportError(
            "VDB_VERIFY_FAILED",
            f"level-set background {background} does not match half-width {LEVEL_SET_HALF_WIDTH} "
            f"at voxel size {voxel_size}",
        )

    for index, expected in (
        ((0, 0, 0), (0.0, 0.0, 0.0)),
        ((1, 0, 0), (voxel_size, 0.0, 0.0)),
        ((0, 1, 0), (0.0, voxel_size, 0.0)),
        ((0, 0, 1), (0.0, 0.0, voxel_size)),
    ):
        world = grid.transform.indexToWorld(index)
        if max(abs(float(world[i]) - expected[i]) for i in range(3)) > max(1e-9, voxel_size * 1e-6):
            raise VdbExportError(
                "VDB_VERIFY_FAILED",
                f"VDB world transform changed at index {index}: {tuple(world)}",
            )

    low, high = grid.evalActiveVoxelBoundingBox()
    low = np.asarray(low, dtype=np.int64)
    high = np.asarray(high, dtype=np.int64)
    world_low = np.asarray(grid.transform.indexToWorld(tuple(int(v) for v in low)), dtype=np.float64)
    world_high = np.asarray(grid.transform.indexToWorld(tuple(int(v) for v in high)), dtype=np.float64)
    band = LEVEL_SET_HALF_WIDTH * voxel_size
    tolerance = 2.0 * voxel_size + 1e-6
    for axis in range(3):
        if world_low[axis] > expected_world_min[axis] + tolerance:
            raise VdbExportError(
                "VDB_WORLD_MISMATCH",
                f"VDB active band starts inside the mesh bounds on axis {axis}",
            )
        if world_high[axis] < expected_world_max[axis] - tolerance:
            raise VdbExportError(
                "VDB_WORLD_MISMATCH",
                f"VDB active band ends inside the mesh bounds on axis {axis}",
            )
        if world_low[axis] < expected_world_min[axis] - band - tolerance:
            raise VdbExportError(
                "VDB_WORLD_MISMATCH",
                f"VDB active band extends too far before the mesh bounds on axis {axis}",
            )
        if world_high[axis] > expected_world_max[axis] + band + tolerance:
            raise VdbExportError(
                "VDB_WORLD_MISMATCH",
                f"VDB active band extends too far after the mesh bounds on axis {axis}",
            )

    shape = tuple(int(v) for v in (high - low + 1))
    dense_voxels = int(np.prod(shape, dtype=np.float64))
    if dense_voxels > MAX_BOUNDING_VOXELS:
        raise VdbExportError(
            "RESOURCE_LIMIT",
            f"VDB active bounding box needs {dense_voxels} dense voxels for verification; "
            f"limit is {MAX_BOUNDING_VOXELS}",
        )
    data = np.zeros(shape, dtype=np.float32)
    grid.copyToArray(data, ijk=tuple(int(v) for v in low))
    if not np.isfinite(data).all():
        raise VdbExportError("VDB_VERIFY_FAILED", "VDB contains non-finite voxel values")
    minimum = float(data.min())
    maximum = float(data.max())
    if minimum >= 0.0:
        raise VdbExportError(
            "VDB_VERIFY_FAILED",
            "level set has no negative interior voxels; surface is open or inverted",
        )
    if maximum <= 0.0:
        raise VdbExportError("VDB_VERIFY_FAILED", "level set has no positive exterior voxels")

    return {
        "grid_name": grid.name,
        "grid_type": type(grid).__name__,
        "grid_class": "LEVEL_SET",
        "active_voxels": active,
        "mem_bytes": int(grid.memUsage()),
        "background": background,
        "index_bbox_min": [int(v) for v in low],
        "index_bbox_max": [int(v) for v in high],
        "world_bbox_min": [round(float(v), 9) for v in world_low],
        "world_bbox_max": [round(float(v), 9) for v in world_high],
        "value_min": round(minimum, 9),
        "value_max": round(maximum, 9),
        "metadata_keys": sorted(metadata.keys()) if isinstance(metadata, dict) else [],
        "readback": "openvdb.readAll",
    }


def export_surface_vdb(
    obj: bpy.types.Object,
    mesh: bpy.types.Mesh,
    path: Path,
    voxel_size: float,
    *,
    frame: int,
    sampling_mode: str,
    source_semantics: str,
) -> dict[str, Any]:
    """Write and independently verify a surface level set for one realized frame."""
    openvdb = _openvdb()
    voxel_size = float(voxel_size)
    if not math.isfinite(voxel_size) or voxel_size <= 0.0:
        raise VdbExportError("INVALID_EXPORT_PARAMETER", f"sdf_voxel_size must be positive, got {voxel_size}")
    path = Path(path)
    if path.exists():
        raise VdbExportError("EXPORT_PATH_EXISTS", f"refusing to overwrite an existing path: {path}")

    points, triangles = _world_triangles(obj, mesh)
    world_min = points.min(axis=0).astype(np.float64)
    world_max = points.max(axis=0).astype(np.float64)
    low, high, bounding_voxels = _bounded_index_box(points, voxel_size)

    transform = openvdb.createLinearTransform(voxelSize=voxel_size)
    try:
        grid = openvdb.FloatGrid().createLevelSetFromPolygons(
            points,
            triangles=triangles,
            transform=transform,
            halfWidth=float(LEVEL_SET_HALF_WIDTH),
        )
    except Exception as exc:  # The native constructor reports its own preconditions.
        raise VdbExportError(
            "VDB_LEVEL_SET_FAILED",
            f"OpenVDB createLevelSetFromPolygons failed: {type(exc).__name__}: {exc}",
        ) from exc
    if grid is None or int(grid.activeVoxelCount()) <= 0:
        raise VdbExportError("EMPTY_VDB_LEVEL_SET", "OpenVDB produced no active level-set voxels")
    if int(grid.activeVoxelCount()) > MAX_ACTIVE_VOXELS:
        raise VdbExportError(
            "RESOURCE_LIMIT",
            f"OpenVDB narrow band has {grid.activeVoxelCount()} active voxels; limit is {MAX_ACTIVE_VOXELS}",
        )
    if int(grid.memUsage()) > MAX_GRID_BYTES:
        raise VdbExportError(
            "RESOURCE_LIMIT",
            f"OpenVDB grid uses {grid.memUsage()} bytes; limit is {MAX_GRID_BYTES}",
        )
    grid.name = GRID_NAME
    grid.gridClass = openvdb.GridClass.LEVEL_SET

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    os.close(handle)
    temp_path = Path(temp_name)
    promoted = False
    try:
        # openvdb.write owns the file contents; start from a non-existent path
        # so a partially written temp file can never be mistaken for a grid.
        temp_path.unlink()
        openvdb.write(str(temp_path), grids=[grid])
        if not temp_path.is_file() or temp_path.stat().st_size == 0:
            raise VdbExportError("VDB_WRITE_FAILED", "OpenVDB writer produced an empty file")
        verification = _verify_written_grid(
            openvdb,
            temp_path,
            voxel_size=voxel_size,
            expected_world_min=world_min,
            expected_world_max=world_max,
        )
        checksum = _sha256_file(temp_path)
        size_bytes = temp_path.stat().st_size
        if path.exists():
            raise VdbExportError("EXPORT_PATH_EXISTS", f"refusing to overwrite an existing path: {path}")
        try:
            os.rename(temp_path, path)
        except FileExistsError as exc:
            raise VdbExportError("EXPORT_PATH_EXISTS", f"refusing to overwrite an existing path: {path}") from exc
        promoted = True
    finally:
        if not promoted:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass

    return {
        "exported": True,
        "export_format": "OPENVDB",
        "export_path": str(path),
        "export_frame": int(frame),
        "sampling_mode": sampling_mode,
        "file_sha256": checksum,
        "size_bytes": size_bytes,
        "voxel_size": voxel_size,
        "level_set_half_width": LEVEL_SET_HALF_WIDTH,
        "bounding_voxels": bounding_voxels,
        "index_bbox_min": [int(v) for v in low],
        "index_bbox_max": [int(v) for v in high],
        "vertex_count": len(points),
        "triangle_count": len(triangles),
        "world_bbox_min": [round(float(v), 9) for v in world_min],
        "world_bbox_max": [round(float(v), 9) for v in world_max],
        "semantics": "surface_level_set_of_realized_world_mesh",
        "source_semantics": source_semantics,
        "native_volume_density_exported": False,
        "grid": verification,
    }


def describe() -> dict[str, Any]:
    """Machine-readable capability statement for RESULT/evidence consumers."""
    return {
        "format": "OPENVDB",
        "writer": "FloatGrid.createLevelSetFromPolygons + openvdb.write",
        "reader": "openvdb.readAll",
        "grid": {"name": GRID_NAME, "type": GRID_TYPE, "class": GRID_CLASS},
        "semantics": "surface level set of the realized world mesh at one frame",
        "native_volume_density_exported": False,
        "bounds": {
            "max_bounding_voxels": MAX_BOUNDING_VOXELS,
            "max_active_voxels": MAX_ACTIVE_VOXELS,
            "max_grid_bytes": MAX_GRID_BYTES,
            "max_vertices": MAX_VERTICES,
            "max_triangles": MAX_TRIANGLES,
        },
        "topology": "closed watertight triangle/quad mesh; exactly coincident vertices are merged",
    }
