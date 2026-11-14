"""Cross-frame evaluation, kinematics diagnostics, penetration check, and cache/edit reporting.

The existing diagnostics are preserved.  New geometry summaries are computed
by the plugin-owned ``geometry_evidence`` module over the complete evaluated
mesh/instance data; ``sample_points`` remains a bounded compatibility view and
must not be treated as the complete comparison.
"""

from __future__ import annotations

import json
import math
from typing import Any

import bpy

from . import bake as pnw_bake
from . import capabilities as pnw_capabilities
from . import geometry_evidence as pnw_geometry
from . import diagnostics as pnw_diagnostics
from . import subgroups as pnw_subgroups


DEFAULT_FPS = 24.0


def _particle_population(mesh):
    """Read the solver's count carried on a surface; absence is unknown, not zero."""
    attr = mesh.attributes.get("pnw_particle_count") if mesh is not None else None
    if attr is None or not len(attr.data):
        return {"count": None, "status": "not_observable", "source": "pnw_particle_count"}
    if attr.data_type != "INT" or attr.domain != "POINT":
        return {"count": None, "status": "invalid_attribute", "source": "pnw_particle_count"}
    count = int(attr.data[0].value)
    if count < 0 or any(item.value != count for item in attr.data):
        return {"count": None, "status": "inconsistent_attribute", "source": "pnw_particle_count"}
    return {"count": count, "status": "observed", "source": "pnw_particle_count"}


def _read_object_state(obj: bpy.types.Object, mod_name: str) -> dict[str, Any]:
    """Read the persisted execution state without trusting cache files alone."""

    keys = (f"pnw_state_json__{mod_name}", "pnw_state_json")
    for key in keys:
        try:
            raw = obj.get(key)
        except Exception:
            raw = None
        if not isinstance(raw, str):
            continue
        try:
            state = json.loads(raw)
        except Exception:
            continue
        if isinstance(state, dict):
            return state
    return {}


def scene_time_basis(scene: bpy.types.Scene | None = None) -> dict[str, Any]:
    """Return the scene frame-rate basis used for seconds-based diagnostics."""

    scene = scene or bpy.context.scene
    render = scene.render
    try:
        fps = int(render.fps)
    except (TypeError, ValueError):
        fps = int(DEFAULT_FPS)
    try:
        fps_base = float(render.fps_base)
    except (TypeError, ValueError):
        fps_base = 1.0
    valid = bool(fps > 0 and math.isfinite(fps_base) and fps_base > 0)
    effective_fps = (float(fps) / fps_base) if valid else DEFAULT_FPS
    return {
        "fps": fps,
        "fps_base": fps_base,
        "effective_fps": effective_fps,
        "valid": valid,
        "seconds_per_frame": 1.0 / effective_fps,
    }


def _sample_object_state(
    obj: bpy.types.Object,
    frame: int,
    ground_z: float,
    *,
    capture_full_digest: bool = True,
    velocity_threshold: float = 0.05,
) -> dict[str, Any]:
    scene = bpy.context.scene
    scene.frame_set(frame)
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(dg)
    try:
        geometry = eval_obj.evaluated_geometry()
    except TypeError:
        # Blender rejects Empty/camera/light at this API boundary. Report an
        # unavailable measurement instead of failing the entire inspection.
        geometry = None
    contact_convergence = pnw_diagnostics.contact_convergence(geometry)
    solver_sleep = pnw_diagnostics.solver_sleep(geometry)
    velocity_observation = pnw_diagnostics.velocity_observation(geometry, velocity_threshold)
    ground_vertices = pnw_diagnostics.GroundVertices(ground_z)
    for name in ('pointcloud', 'curves', 'volume', 'grease_pencil'):
        if getattr(geometry, name, None) is not None:
            ground_vertices.unsupported.add(name)

    # Check instanced bodies first
    inst_positions: list[tuple[float, float, float]] = []
    for inst in dg.object_instances:
        if not inst.is_instance:
            continue
        parent = inst.parent
        if parent == eval_obj or (parent is not None and parent.original == obj):
            loc = inst.matrix_world.translation
            inst_positions.append((float(loc.x), float(loc.y), float(loc.z)))
            ground_vertices.add_instance(inst)

    mesh = eval_obj.to_mesh() if geometry is not None else None
    particle_population = _particle_population(mesh)
    vert_coords: list[tuple[float, float, float]] = []
    velocities: list[float] = []
    face_count = 0
    if mesh is not None:
        face_count = len(mesh.polygons)
        mat = eval_obj.matrix_world
        for v in mesh.vertices:
            w = mat @ v.co
            vert_coords.append((float(w.x), float(w.y), float(w.z)))
            ground_vertices.add(float(w.z))
        vel_attr = mesh.attributes.get("pnw_velocity")
        if vel_attr is not None and vel_attr.data_type == "FLOAT_VECTOR":
            for d in vel_attr.data:
                vx, vy, vz = d.vector
                velocities.append(math.sqrt(vx * vx + vy * vy + vz * vz))
        eval_obj.to_mesh_clear()

    # Prefer realized mesh vertices when geometry is a deformed mesh/curve/volume (or single wrapper instance),
    # and use instance transforms when outputting multiple rigid scatter instances (or pure empty-mesh instances).
    coords = inst_positions if (len(inst_positions) > 1 or (inst_positions and not vert_coords)) else vert_coords
    if coords:
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        zs = [c[2] for c in coords]
        bbox_min = [round(min(xs), 5), round(min(ys), 5), round(min(zs), 5)]
        bbox_max = [round(max(xs), 5), round(max(ys), 5), round(max(zs), 5)]
        centroid = [
            round(sum(xs) / len(xs), 5),
            round(sum(ys) / len(ys), 5),
            round(sum(zs) / len(zs), 5),
        ]
        ground_min = round(min(zs) - ground_z, 5)
        penetration_count = sum(1 for z in zs if z < ground_z - 1e-4)
    else:
        bbox_min = bbox_max = centroid = None
        ground_min = None
        penetration_count = None

    geometry_summary = None
    geometry_summary_error = None
    if capture_full_digest:
        try:
            geometry_summary = pnw_geometry.sample_frame(obj, int(frame), velocity_threshold=velocity_threshold)
        except Exception as exc:
            geometry_summary_error = {
                "code": getattr(exc, "code", "GEOMETRY_SUMMARY_ERROR"),
                "message": str(exc),
            }

    result: dict[str, Any] = {
        "frame": int(frame),
        "instance_count": len(inst_positions),
        "vertex_count": len(vert_coords),
        "face_count": face_count,
        "particle_population": particle_population,
        "contact_convergence": contact_convergence,
        "solver_sleep": solver_sleep,
        "velocity_observation": velocity_observation,
        "sample_points": coords[:500],
        "sample_point_count": len(coords),
        "sample_points_truncated": len(coords) > 500,
        "bbox_min": bbox_min,
        "bbox_max": bbox_max,
        "centroid": centroid,
        "ground_clearance_min": ground_min,
        "ground_penetration_count": penetration_count,
        "ground_measurement_scope": "instance_origins" if coords is inst_positions else "mesh_vertices",
        "ground_contact": {
            "reference": "world_horizontal_plane", "ground_z": float(ground_z),
            "evaluation_mode": str(dg.mode),
            "penetration_tolerance": ground_vertices.tolerance,
            "visible_geometry": ground_vertices.result(),
            "instance_origins": {
                "status": "observed" if inst_positions else "not_observable",
                "count": len(inst_positions),
                "clearance_min": min(p[2] for p in inst_positions) - ground_z if inst_positions else None,
            },
            "scope_note": "Mesh geometry only; not collision proxy clearance or distance to arbitrary colliders.",
        },
        "velocities": velocities,
        # Full coordinate sequence for internal late-frame statistics.  It is
        # deliberately removed from the public frames summary.
        "_all_points": coords,
        "geometry_summary": geometry_summary,
        "geometry_summary_schema": pnw_geometry.EVIDENCE_SCHEMA,
        "geometry_summary_error": geometry_summary_error,
        "digest_valid": geometry_summary is not None,
    }
    if geometry_summary is not None:
        for field in (
            "edge_count",
            "loop_count",
            "has_geometry",
            "vertices_sha256",
            "edges_sha256",
            "faces_sha256",
            "instances_sha256",
            "object_matrix_sha256",
            "geometry_sha256",
            "physics_attributes_sha256",
            "state_sha256",
            "evidence_schema",
        ):
            result[field] = geometry_summary.get(field)
        result["physics_attribute_names"] = geometry_summary.get("physics_attribute_names", [])
        result["state_coverage"] = geometry_summary.get("state_coverage")
        result["velocity_statistics"] = geometry_summary["physics_attributes"]["velocity_statistics"]
    else:
        for field in (
            "edge_count",
            "loop_count",
            "has_geometry",
            "vertices_sha256",
            "edges_sha256",
            "faces_sha256",
            "instances_sha256",
            "object_matrix_sha256",
            "geometry_sha256",
            "physics_attributes_sha256",
            "state_sha256",
            "evidence_schema",
        ):
            result[field] = None
        result["physics_attribute_names"] = []
        result["state_coverage"] = None
        result["velocity_statistics"] = None
    return result


def _inspect_cache(
    obj: bpy.types.Object,
    mod: bpy.types.Modifier | None,
    params_hash: str,
) -> tuple[dict[str, Any], dict[str, Any], bool, bool]:
    """Return cache_info, manual_info, cache_trusted, cache_hash_checked."""

    cache_info: dict[str, Any] = {"is_baked": False, "hash_valid": False}
    manual_info: dict[str, Any] = {"wrapper_edited": False, "edited_subgroups": []}
    cache_trusted = False
    state = {}
    state_parameters_hash = ""
    state_graph_fingerprint = ""
    effective_parameters_hash = str(params_hash or "")
    hash_source = "request" if params_hash else "unavailable"
    cache_hash_checked = bool(effective_parameters_hash)
    if mod is not None and mod.type == "NODES" and mod.node_group is not None:
        state = _read_object_state(obj, mod.name)
        state_parameters_hash = str(state.get("parameters_hash") or "")
        state_graph_fingerprint = str(state.get("graph_fingerprint") or "")
        if not effective_parameters_hash and state_parameters_hash:
            effective_parameters_hash = state_parameters_hash
            hash_source = "object_state"
            cache_hash_checked = True
        wrapper = mod.node_group
        core_node = wrapper.nodes.get("PNW_CoreNode") or next(
            (n for n in wrapper.nodes if n.bl_idname == "GeometryNodeGroup"), None
        )
        core_tree = core_node.node_tree if core_node else None
        graph_fp = pnw_subgroups.tree_fingerprint(core_tree) if core_tree else ""
        try:
            if effective_parameters_hash:
                valid, reason, bdir = pnw_bake.is_cache_valid(
                    obj, mod, effective_parameters_hash, graph_fp
                )
            else:
                bdir = pnw_bake.resolve_bake_directory(obj, mod, mod.bake_directory or None)
                stats = pnw_bake.cache_stats(bdir)
                valid = False
                reason = "hash_unavailable"
            if params_hash and state_parameters_hash and params_hash != state_parameters_hash:
                valid = False
                reason = "state_parameters_hash_mismatch"
            if state_graph_fingerprint and graph_fp and state_graph_fingerprint != graph_fp:
                valid = False
                reason = "state_graph_fingerprint_mismatch"
            stats = pnw_bake.cache_stats(bdir)
            cache_info = {
                "is_baked": stats["meta_files"] > 0 and stats["blob_files"] > 0,
                "hash_valid": valid,
                "reason": reason,
                "bake_directory": str(bdir),
                "meta_files": stats["meta_files"],
                "blob_files": stats["blob_files"],
                "total_bytes": stats["total_bytes"],
                "parameters_hash": effective_parameters_hash,
                "hash_source": hash_source,
                "state_parameters_hash": state_parameters_hash,
                "graph_fingerprint": graph_fp,
                "state_graph_fingerprint": state_graph_fingerprint,
                "hash_checked": bool(effective_parameters_hash),
            }
            cache_trusted = bool(cache_info["is_baked"] and cache_info["hash_valid"])
        except Exception as exc:
            cache_info = {
                "is_baked": False,
                "hash_valid": False,
                "reason": "cache_check_error",
                "error": str(exc),
            }
            cache_trusted = False
        manual_info = {
            "wrapper_edited": pnw_subgroups.is_tree_manually_edited(wrapper),
            "edited_subgroups": [
                ng.name
                for ng in bpy.data.node_groups
                if ng.get(pnw_subgroups.SUBGROUP_ROLE_KEY) and pnw_subgroups.is_tree_manually_edited(ng)
            ],
        }
    return cache_info, manual_info, cache_trusted, cache_hash_checked


def _sample_direct(
    obj: bpy.types.Object,
    frames: list[int],
    ground_z: float,
    velocity_threshold: float = 0.05,
) -> tuple[dict[int, dict[str, Any]], int]:
    return {frame: _sample_object_state(obj, frame, ground_z, velocity_threshold=velocity_threshold) for frame in frames}, 0


def _sample_sequential(
    obj: bpy.types.Object,
    frames: list[int],
    frame_start: int,
    ground_z: float,
    velocity_threshold: float = 0.05,
) -> tuple[dict[int, dict[str, Any]], int]:
    """Advance every frame from frame_start so unbaked simulation is meaningful."""

    scene = bpy.context.scene
    targets = set(frames)
    last = max(frames)
    start = min(frame_start, min(frames))
    per_frame: dict[int, dict[str, Any]] = {}
    advanced = 0
    for frame in range(start, last + 1):
        if frame in targets:
            per_frame[frame] = _sample_object_state(obj, frame, ground_z, velocity_threshold=velocity_threshold)
        else:
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            # Force depsgraph evaluation for simulation zones without extracting
            # a whole mesh on every intermediate frame.
            bpy.context.evaluated_depsgraph_get()
        advanced += 1
    return per_frame, advanced


def inspect_workflow(
    obj: bpy.types.Object,
    mod: bpy.types.Modifier | None,
    frame_start: int = 1,
    frame_end: int = 120,
    ground_z: float = 0.0,
    sleep_velocity: float = 0.05,
    params_hash: str = "",
) -> dict[str, Any]:
    mid = (frame_start + frame_end) // 2
    pre_end = max(frame_start, frame_end - 5)
    sample_frames = sorted(set([frame_start, mid, pre_end, frame_end]))

    timing = scene_time_basis(bpy.context.scene)
    dt_span = (frame_end - pre_end) / float(timing["effective_fps"])
    timing["dt_span_seconds"] = round(dt_span, 6)

    cache_info, manual_info, cache_trusted, cache_hash_checked = _inspect_cache(
        obj, mod, params_hash
    )
    stale_cache_present = bool(cache_info.get("is_baked") and not cache_trusted)

    sampling = {
        "mode": "DIRECT_NO_MODIFIER",
        "cache_trusted": cache_trusted,
        "cache_hash_checked": cache_hash_checked,
        "advanced_frames": 0,
        "diagnostics_valid": True,
        "warning": None,
    }
    per_frame: dict[int, dict[str, Any]] = {}
    if mod is not None and mod.type == "NODES" and mod.node_group is not None and not cache_trusted:
        try:
            per_frame, advanced = _sample_sequential(obj, sample_frames, frame_start, ground_z, max(sleep_velocity, 1e-4))
            sampling.update(
                mode="SEQUENTIAL_UNBAKED",
                advanced_frames=advanced,
                diagnostics_valid=not stale_cache_present,
                warning=(
                    "Disk cache exists but its hash is invalid; sequential diagnostics may still read stale cache."
                    if stale_cache_present
                    else "No trusted disk cache; frames were advanced sequentially from frame_start."
                ),
            )
        except Exception as exc:
            per_frame, _ = _sample_direct(obj, sample_frames, ground_z, max(sleep_velocity, 1e-4))
            sampling.update(
                mode="DIRECT_FALLBACK",
                diagnostics_valid=False,
                warning=f"Sequential uncached sampling failed; diagnostics invalid: {exc}",
            )
    else:
        per_frame, _ = _sample_direct(obj, sample_frames, ground_z, max(sleep_velocity, 1e-4))
        if cache_trusted:
            sampling.update(
                mode="CACHED_DIRECT",
                warning=(
                    None
                    if cache_hash_checked
                    else "Cache hashes were not checked; direct sampling assumes the baked cache is valid."
                ),
            )
        elif mod is None or mod.type != "NODES" or mod.node_group is None:
            sampling.update(mode="DIRECT_NO_MODIFIER")

    # Late frame positional delta between pre_end and frame_end.  Use the full
    # internal coordinate sequence, not the bounded sample_points view.
    pts_a = per_frame[pre_end]["_all_points"]
    pts_b = per_frame[frame_end]["_all_points"]
    late_motion, deltas = pnw_diagnostics.late_motion(pts_a, pts_b, frame_end - pre_end, dt_span)
    a, b = per_frame[pre_end], per_frame[frame_end]
    incompatible = None
    if a['ground_measurement_scope'] != b['ground_measurement_scope']:
        incompatible = 'point_source_changed'
    elif a['ground_measurement_scope'] == 'mesh_vertices' and any(
            a.get(key) != b.get(key) for key in ('edges_sha256', 'faces_sha256')):
        incompatible = 'mesh_topology_changed'
    elif not timing['valid']:
        incompatible = 'invalid_time_basis'
    if incompatible:
        late_motion.update(status='not_observable', reason=incompatible, mean_delta=None, max_delta=None)
        deltas = []
    late_jitter = round(late_motion['mean_delta'], 6) if deltas else None
    max_late_delta = round(late_motion['max_delta'], 6) if deltas else None

    # End-frame speed stats if velocity attribute available, else approximate from late delta
    output_stats = per_frame[frame_end]['velocity_observation']
    speed_source = "evaluated_output_pnw_velocity"
    if output_stats['status'] == 'observed':
        mean_speed = round(output_stats["mean"], 5)
        max_speed = round(output_stats["max"], 5)
        resting_ratio = round(output_stats["resting_ratio"], 4)
    elif deltas and output_stats['status'] == 'not_observable':
        speed_source = "estimated_from_sampled_position_delta"
        speeds = [delta / dt_span for delta in deltas]
        mean_speed = round(sum(speeds) / len(speeds), 5)
        max_speed = round(max(speeds), 5)
        resting_ratio = round(sum(v <= max(sleep_velocity, 1e-4) for v in speeds) / len(speeds), 4)
    else:
        speed_source = "not_observable"
        mean_speed = max_speed = resting_ratio = None

    # Check start->end displacement to verify real motion occurred
    c_start = per_frame[frame_start]["centroid"]
    c_end = per_frame[frame_end]["centroid"]
    centroid_displacement = round(
        math.sqrt((c_end[0] - c_start[0]) ** 2 + (c_end[1] - c_start[1]) ** 2 + (c_end[2] - c_start[2]) ** 2),
        5,
    ) if c_start is not None and c_end is not None else None

    frames_summary = {}
    for frame, data in per_frame.items():
        frames_summary[str(frame)] = {
            key: value
            for key, value in data.items()
            if key not in ("sample_points", "velocities", "_all_points")
        }

    if not all(item.get("digest_valid") for item in per_frame.values()):
        sampling["diagnostics_valid"] = False
        sampling["warning"] = "Output state evidence failed; inspect frame geometry_summary_error."
    return {
        "object_name": obj.name,
        "capability_assessment": pnw_capabilities.runtime_capabilities(),
        "modifier_name": mod.name if mod else None,
        "frame_range": [frame_start, frame_end],
        "frames": frames_summary,
        "centroid_displacement": centroid_displacement,
        "late_frame_jitter": late_jitter,
        "max_late_delta": max_late_delta,
        "late_motion": late_motion,
        "mean_speed": mean_speed,
        "max_speed": max_speed,
        "speed_source": speed_source,
        "velocity_attribute_status": output_stats['status'],
        "resting_ratio": resting_ratio,
        "resting_ratio_scope": "sampled_speed_threshold_fraction_not_solver_sleep",
        "cache_status": cache_info,
        "manual_edit_status": manual_info,
        "timing": timing,
        "sampling": sampling,
        "diagnostics_valid": bool(sampling["diagnostics_valid"]),
        "geometry_summary_schema": pnw_geometry.EVIDENCE_SCHEMA,
        "state_coverage": per_frame[frame_end].get("state_coverage"),
    }
