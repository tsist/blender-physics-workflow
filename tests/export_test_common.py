"""Shared helpers for the real Blender export tests."""
from __future__ import annotations

import json
import math
from pathlib import Path
import sys

import bpy


ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from physics_nodes_workflow_addon import execute_request, export as pnw_export  # noqa: E402


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # Test harness only: the addon's module-level template cache can retain
    # bpy references across a factory reset within one process.  Production
    # code is intentionally not changed here.
    from physics_nodes_workflow_addon import subgroups

    subgroups._ACTIVE_TEMPLATES.clear()
    subgroups._BUILTIN_DYNAMICS_CACHE.clear()
    scene = bpy.context.scene
    scene.render.fps = 24
    scene.render.fps_base = 1.0
    scene.frame_step = 1
    return scene


def grid_mesh(name, sx, sy, nx, ny, z=0.0):
    mesh = bpy.data.meshes.new(name)
    verts = []
    faces = []
    for j in range(ny + 1):
        for i in range(nx + 1):
            verts.append((-sx / 2 + sx * i / nx, -sy / 2 + sy * j / ny, z))
    for j in range(ny):
        for i in range(nx):
            a = j * (nx + 1) + i
            faces.append((a, a + 1, a + nx + 2, a + nx + 1))
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    return mesh


def realized_world_vertices(obj):
    mesh = pnw_export._realized_mesh(obj)
    if mesh is None:
        return [], 0, 0
    try:
        matrix = obj.matrix_world.copy()
        world = [[round(float(v), 9) for v in (matrix @ vert.co)] for vert in mesh.vertices]
        return world, len(mesh.vertices), len(mesh.polygons)
    finally:
        bpy.data.meshes.remove(mesh)


def bbox(points):
    if not points:
        return None
    return [
        [min(float(p[i]) for p in points) for i in range(3)],
        [max(float(p[i]) for p in points) for i in range(3)],
    ]


def centroid(points):
    if not points:
        return None
    return [sum(float(p[i]) for p in points) / len(points) for i in range(3)]


def instance_positions(scene, obj):
    dg = bpy.context.evaluated_depsgraph_get()
    positions = []
    for inst in dg.object_instances:
        if inst.is_instance and inst.parent is not None and inst.parent.original == obj:
            positions.append([round(float(v), 9) for v in inst.matrix_world.translation])
    return positions


def sample_managed_frames(scene, obj, frames):
    rows = []
    for frame in frames:
        scene.frame_set(frame)
        world, vertex_count, face_count = realized_world_vertices(obj)
        rows.append({
            "frame": int(frame),
            "vertex_count": vertex_count,
            "face_count": face_count,
            "world_vertices": world,
            "bbox": bbox(world),
            "centroid": centroid(world),
            "instance_positions": instance_positions(scene, obj),
        })
    return rows


def connected_component_centroids(mesh, matrix):
    count = len(mesh.vertices)
    parent = list(range(count))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for edge in mesh.edges:
        union(int(edge.vertices[0]), int(edge.vertices[1]))
    groups = {}
    for index, vert in enumerate(mesh.vertices):
        groups.setdefault(find(index), []).append(matrix @ vert.co)
    return [centroid([[float(v) for v in p] for p in points]) for points in groups.values() if points]


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def parse_args(argv=None):
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--case", default="rigid")
    parser.add_argument("--abc", default=None)
    parser.add_argument("--source", default=None)
    parser.add_argument("--blend", default=None)
    parser.add_argument("--out", default=None)
    args = argv if argv is not None else sys.argv[sys.argv.index("--") + 1:]
    return parser.parse_args(args)


def rounded(value, digits=6):
    return round(float(value), digits)


def finite(value):
    return math.isfinite(float(value))
