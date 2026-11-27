"""Pure-Python contract and normalization layer for Physics Nodes Workflow v1.0.

Zero bpy dependency so both the Blender addon and external CLI overlay validate
requests identically before invoking Blender.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any
from . import capabilities

CONTRACT_ID = "BLENDER_PHYSICS_WORKFLOW_CONTRACT_V1"
SCHEMA_VERSION = "1.0"

OPERATIONS = (
    "build",
    "update",
    "parameters",
    "bake",
    "invalidate_cache",
    "inspect",
    "adopt",
    "export",
    "prepare_instances",
)

DOMAINS = ("rigid_scatter", "cloth_rope", "fluid_emission")

NATIVE_FLUID_PRESETS = {"native_liquid": "LIQUID", "native_gas": "GAS"}
NATIVE_FLUID_ACTIONS = (
    "build", "adopt", "update", "parameters", "bake", "inspect",
    "invalidate_cache", "export", "guide_density", "export_density",
)
NATIVE_INTEGER_SETTINGS = {"resolution_max": (16, 256), "timesteps_min": (1, 8), "timesteps_max": (1, 8)}
NATIVE_FLOAT_SETTINGS = {"time_scale": (0.1, 3), "particle_radius": (0.25, 3), "flip_ratio": (0, 1)}

BRIDGE_MODES = (
    "GN_NATIVE",
    "BULLET_BRIDGE",
    "XPBD_ASSET_BRIDGE",
    "MANTAFLOW_BRIDGE",
)

EXPORT_FORMATS = ("NONE", "STATIC_MESH", "ALEMBIC", "OPENVDB")

PRESET_DEFINITIONS: dict[str, dict[str, Any]] = {
    "rigid_drop_pack": {
        "domain": "rigid_scatter",
        "title": "Rigid Scatter & Drop Pack",
        "description": "Procedural rigid body scatter/drop with ramp & floor collision, bounce, friction, sleep threshold, and optional tether.",
        "defaults": {
            "substeps": 4,
            "seed": 42,
            "frame_start": 1,
            "frame_end": 120,
            "gravity": [0.0, 0.0, -9.81],
            "drag": 0.04,
            "restitution": 0.35,
            "friction": 0.45,
            "sleep_velocity": 0.08,
            "contact_thickness": 0.08,
            "particle_radius": 0.12,
            "ground_z": 0.0,
            "scatter_count": 48,
            "angular_spin": 1.2,
            "stiffness": 0.0,
            "anchor_pos": [0.0, 0.0, 2.5],
            "rest_length": 0.0,
            "wind_vector": [0.0, 0.0, 0.0],
            "wind_turbulence": 0.0,
            "emit_rate": 0,
            "emit_speed": 0.0,
            "emit_end_frame": 1,
            "lifetime": 250,
            "sdf_voxel_size": 0.08,
            "buoyancy": 0.0,
        },
    },
    "rigid_chain_pendulum": {
        "domain": "rigid_scatter",
        "title": "Rigid Chain & Tether Pendulum",
        "description": "Tethered rigid links/beads with anchor distance constraint, gravity, drag, and ground/obstacle collision.",
        "defaults": {
            "substeps": 6,
            "seed": 17,
            "frame_start": 1,
            "frame_end": 120,
            "gravity": [0.0, 0.0, -9.81],
            "drag": 0.02,
            "restitution": 0.25,
            "friction": 0.3,
            "sleep_velocity": 0.03,
            "contact_thickness": 0.06,
            "particle_radius": 0.14,
            "ground_z": 0.0,
            "scatter_count": 24,
            "angular_spin": 0.5,
            "stiffness": 0.65,
            "anchor_pos": [0.0, 0.0, 3.2],
            "rest_length": 1.8,
            "wind_vector": [1.5, 0.0, 0.0],
            "wind_turbulence": 0.3,
            "emit_rate": 0,
            "emit_speed": 0.0,
            "emit_end_frame": 1,
            "lifetime": 250,
            "sdf_voxel_size": 0.08,
            "buoyancy": 0.0,
        },
    },
    "rigid_box_tipping": {
        "domain": "rigid_scatter",
        "title": "Rigid Box Floor Tipping (Planar)",
        "description": "Isolated planar box-on-fixed-floor test: one real support contact, persistent angular velocity, normal/tangent impulses, true-size cube instancing. No box-box or arbitrary mesh collision.",
        "defaults": {
            "substeps": 8,
            "seed": 211,
            "frame_start": 1,
            "frame_end": 120,
            "gravity": [0.0, 0.0, -9.81],
            "drag": 0.0,
            "restitution": 0.0,
            "friction": 0.45,
            "sleep_velocity": 0.0,
            "contact_thickness": 0.001,
            "particle_radius": 0.12,
            "ground_z": 0.0,
            "scatter_count": 0,
            "angular_spin": 0.0,
            "stiffness": 0.0,
            "anchor_pos": [0.0, 0.0, 0.0],
            "rest_length": 0.0,
            "wind_vector": [0.0, 0.0, 0.0],
            "wind_turbulence": 0.0,
            "emit_rate": 0,
            "emit_speed": 0.0,
            "emit_end_frame": 1,
            "lifetime": 250,
            "sdf_voxel_size": 0.08,
            "buoyancy": 0.0,
            "box_half_extents": [0.2, 0.3, 1.0],
            "box_initial_tilt": 0.3,
        },
    },
    "cloth_banner_wind": {
        "domain": "cloth_rope",
        "title": "Cloth Banner & Wind Field",
        "description": "Pinned cloth/ribbon mesh with edge-relaxation elasticity, directional wind turbulence, and obstacle collision.",
        "defaults": {
            "substeps": 5,
            "seed": 101,
            "frame_start": 1,
            "frame_end": 120,
            "gravity": [0.0, 0.0, -9.81],
            "drag": 0.12,
            "restitution": 0.1,
            "friction": 0.4,
            "sleep_velocity": 0.01,
            "contact_thickness": 0.04,
            "particle_radius": 0.03,
            "ground_z": 0.0,
            "scatter_count": 0,
            "angular_spin": 0.0,
            "stiffness": 0.72,
            "anchor_pos": [0.0, 0.0, 0.0],
            "rest_length": 0.0,
            "wind_vector": [4.5, 1.2, 0.4],
            "wind_turbulence": 2.2,
            "emit_rate": 0,
            "emit_speed": 0.0,
            "emit_end_frame": 1,
            "lifetime": 250,
            "sdf_voxel_size": 0.08,
            "buoyancy": 0.0,
        },
    },
    "rope_catenary_swing": {
        "domain": "cloth_rope",
        "title": "Rope Catenary & Swing",
        "description": "Flexible rope/cable with pinned anchors, gravity sag, wind sway, and collider deflection.",
        "defaults": {
            "substeps": 6,
            "seed": 73,
            "frame_start": 1,
            "frame_end": 120,
            "gravity": [0.0, 0.0, -9.81],
            "drag": 0.08,
            "restitution": 0.15,
            "friction": 0.35,
            "sleep_velocity": 0.01,
            "contact_thickness": 0.03,
            "particle_radius": 0.03,
            "ground_z": 0.0,
            "scatter_count": 0,
            "angular_spin": 0.0,
            "stiffness": 0.85,
            "anchor_pos": [0.0, 0.0, 0.0],
            "rest_length": 0.0,
            "wind_vector": [2.0, 0.8, 0.0],
            "wind_turbulence": 1.4,
            "emit_rate": 0,
            "emit_speed": 0.0,
            "emit_end_frame": 1,
            "lifetime": 250,
            "sdf_voxel_size": 0.08,
            "buoyancy": 0.0,
        },
    },
    "softbody_elastic_shell": {
        "domain": "cloth_rope",
        "title": "Softbody Elastic Shell (Closed Surface)",
        "description": "Approximate closed-surface elastic softbody for a local, closed triangulated manifold: native XPBD edge-length plus cross-edge bending, gravity, wind and contacts. Surface-only elastic shell, not a tetrahedral solid, incompressible body, or pressure/volume-preserving solver.",
        "defaults": {
            "substeps": 8,
            "seed": 909,
            "frame_start": 1,
            "frame_end": 120,
            "gravity": [0.0, 0.0, -9.81],
            "drag": 0.10,
            "restitution": 0.55,
            "friction": 0.20,
            "sleep_velocity": 0.01,
            "contact_thickness": 0.12,
            "particle_radius": 0.03,
            "ground_z": 0.0,
            "scatter_count": 0,
            "angular_spin": 0.0,
            "stiffness": 0.55,
            "anchor_pos": [0.0, 0.0, 0.0],
            "rest_length": 0.0,
            "wind_vector": [4.0, 0.0, 0.0],
            "wind_turbulence": 0.30,
            "emit_rate": 0,
            "emit_speed": 0.0,
            "emit_end_frame": 1,
            "lifetime": 250,
            "sdf_voxel_size": 0.08,
            "buoyancy": 0.0,
            "instance_normalize": True,
            "bending": True,
            "bend_softness": 0.12,
            "tearing": False,
            "tear_threshold": 1.2,
            "curve_resolution": 64,
            "contact_iterations": 3,
            "constraint_iterations": 24,
            "activation_frame": 0,
        },
    },
    "fluid_fountain_sdf": {
        "domain": "fluid_emission",
        "title": "Fluid Fountain (Particle -> SDF Mesh)",
        "description": "Node particle emitter with seeded spray, obstacle deflection, and SDF GridToMesh liquid surface reconstruction.",
        "defaults": {
            "substeps": 3,
            "seed": 303,
            "frame_start": 1,
            "frame_end": 120,
            "gravity": [0.0, 0.0, -9.81],
            "drag": 0.03,
            "restitution": 0.25,
            "friction": 0.15,
            "sleep_velocity": 0.02,
            "contact_thickness": 0.05,
            "particle_radius": 0.16,
            "ground_z": 0.0,
            "scatter_count": 24,
            "angular_spin": 0.0,
            "stiffness": 0.0,
            "anchor_pos": [0.0, 0.0, 0.0],
            "rest_length": 0.0,
            "wind_vector": [0.3, 0.0, 0.0],
            "wind_turbulence": 0.6,
            "emit_rate": 24,
            "emit_speed": 6.5,
            "emit_end_frame": 90,
            "lifetime": 110,
            "sdf_voxel_size": 0.075,
            "buoyancy": 0.0,
        },
    },
    "smoke_plume_volume": {
        "domain": "fluid_emission",
        "title": "Buoyant Smoke Plume (Points -> Volume/Mesh)",
        "description": "Buoyant rising gas emitter with curl/noise turbulence and PointsToVolume/VolumeToMesh plume output.",
        "defaults": {
            "substeps": 2,
            "seed": 505,
            "frame_start": 1,
            "frame_end": 120,
            "gravity": [0.0, 0.0, 2.8],
            "drag": 0.18,
            "restitution": 0.1,
            "friction": 0.1,
            "sleep_velocity": 0.0,
            "contact_thickness": 0.06,
            "particle_radius": 0.24,
            "ground_z": 0.0,
            "scatter_count": 18,
            "angular_spin": 0.0,
            "stiffness": 0.0,
            "anchor_pos": [0.0, 0.0, 0.0],
            "rest_length": 0.0,
            "wind_vector": [0.8, 0.2, 0.4],
            "wind_turbulence": 2.4,
            "emit_rate": 18,
            "emit_speed": 2.2,
            "emit_end_frame": 100,
            "lifetime": 95,
            "sdf_voxel_size": 0.1,
            "buoyancy": 2.8,
        },
    },
}


for _preset in PRESET_DEFINITIONS.values():
    _preset["defaults"].setdefault("activation_frame", 0)
    _preset["defaults"].setdefault("instance_normalize", True)
    _preset["defaults"].setdefault("bending", False)
    _preset["defaults"].setdefault("bend_softness", 0.1)
    _preset["defaults"].setdefault("tearing", False)
    _preset["defaults"].setdefault("tear_threshold", 1.2)
    _preset["defaults"].setdefault("curve_resolution", 64)
    _preset["defaults"].setdefault("contact_iterations", 3)
    _preset["defaults"].setdefault("constraint_iterations", 12)


class PhysicsContractError(ValueError):
    """Raised when a workflow request violates the contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _require_vec3(value: Any, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise PhysicsContractError("VALIDATION_ERROR", f"{name} must be a 3-element float list")
    try:
        parsed = [float(v) for v in value]
        if not all(math.isfinite(v) for v in parsed):
            raise ValueError("Non-finite vector")
        return parsed
    except (TypeError, ValueError, OverflowError) as exc:
        raise PhysicsContractError("VALIDATION_ERROR", f"{name} contains non-numeric value") from exc


def _require_range(value: Any, name: str, lo: float, hi: float, is_int: bool = False) -> int | float:
    try:
        number = float(value)
        if not math.isfinite(number) or (is_int and not number.is_integer()):
            raise ValueError("Expected finite number (integer where required)")
        parsed = int(number) if is_int else number
    except (TypeError, ValueError, OverflowError) as exc:
        raise PhysicsContractError("VALIDATION_ERROR", f"{name} must be numeric") from exc
    if parsed < lo or parsed > hi:
        raise PhysicsContractError("VALIDATION_ERROR", f"{name}={parsed} out of allowed bounds [{lo}, {hi}]")
    return parsed


def _require_strict_int(value: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PhysicsContractError(
            "VALIDATION_ERROR",
            f"{name} must be an integer in [{lo}, {hi}]; booleans, fractions and non-finite values are rejected",
        )
    if value < lo or value > hi:
        raise PhysicsContractError("VALIDATION_ERROR", f"{name}={value} out of allowed bounds [{lo}, {hi}]")
    return value


def compute_parameters_hash(params: dict[str, Any], bridge_mode: str, preset_id: str, collider_object: str | None,
                            collider_collection: str | None = None, instance_collection: str | None = None) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "preset_id": preset_id,
        "bridge_mode": bridge_mode,
        "collider_object": collider_object or "",
        "collider_collection": collider_collection or "",
        "instance_collection": instance_collection or "",
        "parameters": params,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _native_text(raw, key, *, required=False):
    value = raw.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PhysicsContractError("VALIDATION_ERROR", f"{key} must be a nonempty string")
    return value.strip()


def _normalize_native_fluid(raw, preset_id, operation):
    mode = raw.get("bridge_mode", "MANTAFLOW_BRIDGE")
    if mode != "MANTAFLOW_BRIDGE" or raw.get("domain", "fluid_emission") != "fluid_emission":
        raise PhysicsContractError("VALIDATION_ERROR", "Native fluid presets require the Mantaflow bridge")
    if operation not in NATIVE_FLUID_ACTIONS:
        raise PhysicsContractError("VALIDATION_ERROR", f"Unsupported native fluid action: {operation}")
    if raw.get("bake") or raw.get("force_rebuild"):
        raise PhysicsContractError("VALIDATION_ERROR", "Use the explicit bake action for an existing native fluid domain")
    if raw.get("verify_cold_reopen"):
        raise PhysicsContractError("VALIDATION_ERROR", "Save the work copy and verify it in a separate Blender process")
    if any(raw.get(field) is not None for field in ("collider_object", "collider_collection", "instance_collection")):
        raise PhysicsContractError("VALIDATION_ERROR", "Set native fluid flow and effector collections on the domain")

    supplied = raw.get("parameters", {})
    if not isinstance(supplied, dict) or supplied.keys() - {"frame_start", "frame_end"}:
        raise PhysicsContractError("VALIDATION_ERROR", "Set native solver properties on the domain; pass only the frame range")
    start = _require_strict_int(raw.get("frame_start", supplied.get("frame_start")), "frame_start", 1, 100000)
    end = _require_strict_int(raw.get("frame_end", supplied.get("frame_end")), "frame_end", start, 100000)
    params = {"frame_start": start, "frame_end": end}
    tuning = raw.get("native_settings", {})
    if not isinstance(tuning, dict) or (tuning and operation != "parameters"):
        raise PhysicsContractError("VALIDATION_ERROR", "native_settings applies only to the parameters action")
    edits = {}
    for key, value in tuning.items():
        if key in NATIVE_INTEGER_SETTINGS:
            edits[key] = _require_strict_int(value, key, *NATIVE_INTEGER_SETTINGS[key])
        elif key in NATIVE_FLOAT_SETTINGS and type(value) in (int, float):
            if preset_id == "native_gas" and key in {"particle_radius", "flip_ratio"}:
                raise PhysicsContractError("VALIDATION_ERROR", f"{key} requires a liquid domain")
            edits[key] = _require_range(value, key, *NATIVE_FLOAT_SETTINGS[key])
        elif key == "simulation_method" and preset_id == "native_liquid" and value in ("APIC", "FLIP"):
            edits[key] = value
        else:
            raise PhysicsContractError("VALIDATION_ERROR", f"Invalid native solver setting: {key}")
    fmt = raw.get("export_format", "NONE")
    expected_format = "STATIC_MESH" if operation == "export" and preset_id == "native_liquid" else "NONE"
    if fmt != expected_format or (operation == "export" and fmt != "STATIC_MESH"):
        raise PhysicsContractError("VALIDATION_ERROR", "Native liquid frame export requires STATIC_MESH")
    export_path = _native_text(raw, "export_path")
    if export_path and (operation != "export" or not export_path.lower().endswith(".blend")):
        raise PhysicsContractError("VALIDATION_ERROR", "Native liquid frame export uses a new .blend path")
    frame = _require_strict_int(raw.get("export_frame", end), "export_frame", start, end)
    if operation in {"guide_density", "export_density"}:
        if preset_id != "native_gas":
            raise PhysicsContractError("VALIDATION_ERROR", "Density actions require a native gas domain")
        provided = raw.get("native_density", {})
        if not isinstance(provided, dict):
            raise PhysicsContractError("VALIDATION_ERROR", "native_density must be an object")
        allowed = {"guide_name", "output_directory"}
        if operation == "guide_density":
            allowed.update(("bypass_object", "exit_object", "guide_strength"))
        if provided.keys() - allowed:
            raise PhysicsContractError("VALIDATION_ERROR", "Unknown or inapplicable native_density settings")
        density = {key: _native_text(provided, key) for key in provided if key != "guide_strength"}
        if "guide_strength" in provided:
            strength = provided["guide_strength"]
            if isinstance(strength, bool) or not isinstance(strength, (int, float)):
                raise PhysicsContractError("VALIDATION_ERROR", "guide_strength must be numeric")
            density["guide_strength"] = _require_range(strength, "guide_strength", 0, 1)
        if operation == "export_density" and not density.get("output_directory"):
            raise PhysicsContractError("VALIDATION_ERROR", "export_density requires output_directory")
    else:
        if raw.get("native_density") is not None:
            raise PhysicsContractError("VALIDATION_ERROR", "native_density applies to gas guide and density export actions")
        density = {}

    return {
        "schema_version": SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "operation": operation,
        "domain": "fluid_emission",
        "preset_id": preset_id,
        "bridge_mode": mode,
        "target_object": _native_text(raw, "target_object", required=True),
        "modifier_name": _native_text(raw, "modifier_name", required=True),
        "bake": False,
        "bake_directory": _native_text(raw, "bake_directory"),
        "force_rebuild": False,
        "verify_cold_reopen": raw.get("verify_cold_reopen", False),
        "export_format": fmt,
        "export_path": export_path,
        "export_frame": frame,
        "parameters": params,
        "parameters_hash": compute_parameters_hash(
            {**params, "native_settings": edits} if edits else params, mode, preset_id, None,
        ),
        "native_density": density,
        "native_settings": edits,
    }


def normalize_request(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PhysicsContractError("VALIDATION_ERROR", "Request payload must be a JSON object")
    for flag in ("bake", "force_rebuild", "verify_cold_reopen"):
        if flag in raw and not isinstance(raw[flag], bool):
            raise PhysicsContractError("VALIDATION_ERROR", f"{flag} must be a JSON boolean")

    schema_version = str(raw.get("schema_version", SCHEMA_VERSION))
    if schema_version != SCHEMA_VERSION:
        raise PhysicsContractError("VALIDATION_ERROR", f"Unsupported schema_version: {schema_version}")

    operation = str(raw.get("operation", "build")).strip()
    preset_id = str(raw.get("preset_id", "rigid_drop_pack")).strip()
    if preset_id in NATIVE_FLUID_PRESETS:
        return _normalize_native_fluid(raw, preset_id, operation)
    if operation not in OPERATIONS:
        raise PhysicsContractError("VALIDATION_ERROR", f"Unsupported operation: {operation}")

    if preset_id not in PRESET_DEFINITIONS:
        raise PhysicsContractError("VALIDATION_ERROR", f"Unknown preset_id: {preset_id}")

    preset = PRESET_DEFINITIONS[preset_id]
    domain = str(raw.get("domain", preset["domain"])).strip()
    if domain not in DOMAINS:
        raise PhysicsContractError("VALIDATION_ERROR", f"Unsupported domain: {domain}")
    if domain != preset["domain"] and "domain" in raw:
        raise PhysicsContractError(
            "VALIDATION_ERROR",
            f"preset_id '{preset_id}' belongs to domain '{preset['domain']}', got '{domain}'",
        )

    bridge_mode = str(raw.get("bridge_mode", "GN_NATIVE")).strip()
    if bridge_mode not in BRIDGE_MODES:
        raise PhysicsContractError("VALIDATION_ERROR", f"Unsupported bridge_mode: {bridge_mode}")

    export_format = str(raw.get("export_format", "NONE")).strip()
    if export_format not in EXPORT_FORMATS:
        raise PhysicsContractError("VALIDATION_ERROR", f"Unsupported export_format: {export_format}")

    params = copy.deepcopy(preset["defaults"])
    params.setdefault("curve_resolution", 64)
    user_params = raw.get("parameters", {})
    if user_params is None:
        user_params = {}
    if not isinstance(user_params, dict):
        raise PhysicsContractError("VALIDATION_ERROR", "parameters must be a JSON object")
    for k, v in user_params.items():
        if k not in params:
            raise PhysicsContractError("VALIDATION_ERROR", f"Unknown physics parameter: {k}")
        params[k] = v

    # Allow top-level overrides for convenience
    for top_k in ("substeps", "seed", "frame_start", "frame_end"):
        if top_k in raw and raw[top_k] is not None:
            params[top_k] = raw[top_k]

    params["substeps"] = int(_require_range(params["substeps"], "substeps", 1, 8, is_int=True))
    for flag in ("instance_normalize", "bending", "tearing"):
        if not isinstance(params[flag], bool):
            raise PhysicsContractError("VALIDATION_ERROR", f"{flag} must be a JSON boolean")
    params["bend_softness"] = float(_require_range(params["bend_softness"], "bend_softness", 0.0, 1.0))
    params["tear_threshold"] = float(_require_range(params["tear_threshold"], "tear_threshold", 1.0, 100.0))
    if domain != "cloth_rope" and (params["bending"] or params["tearing"]):
        raise PhysicsContractError("VALIDATION_ERROR", "Bending and tearing require the cloth_rope domain")
    params["curve_resolution"] = int(_require_range(params["curve_resolution"], "curve_resolution", 2, 50000, is_int=True))
    params["contact_iterations"] = int(_require_range(params["contact_iterations"], "contact_iterations", 1, 32, is_int=True))
    params["constraint_iterations"] = int(_require_range(params["constraint_iterations"], "constraint_iterations", 1, 64, is_int=True))
    params["seed"] = int(_require_range(params["seed"], "seed", 0, 2**31 - 1, is_int=True))
    params["frame_start"] = int(_require_range(params["frame_start"], "frame_start", 1, 100000, is_int=True))
    params["frame_end"] = int(_require_range(params["frame_end"], "frame_end", params["frame_start"], 100000, is_int=True))
    params["activation_frame"] = _require_strict_int(params["activation_frame"], "activation_frame", 0, 100000)
    if domain != "rigid_scatter" and params["activation_frame"] != 0:
        raise PhysicsContractError(
            "VALIDATION_ERROR",
            "activation_frame requires the rigid_scatter domain; other domains must use 0",
        )
    params["gravity"] = _require_vec3(params["gravity"], "gravity")
    if preset_id == "rigid_box_tipping" and params["gravity"][1] != 0.0:
        raise PhysicsContractError(
            "VALIDATION_ERROR",
            "rigid_box_tipping supports XZ-plane motion with Y-axis rotation; gravity Y must be 0",
        )
    params["wind_vector"] = _require_vec3(params["wind_vector"], "wind_vector")
    params["anchor_pos"] = _require_vec3(params["anchor_pos"], "anchor_pos")
    params["drag"] = float(_require_range(params["drag"], "drag", 0.0, 20.0))
    params["restitution"] = float(_require_range(params["restitution"], "restitution", 0.0, 1.0))
    params["friction"] = float(_require_range(params["friction"], "friction", 0.0, 1.0))
    params["sleep_velocity"] = float(_require_range(params["sleep_velocity"], "sleep_velocity", 0.0, 10.0))
    params["contact_thickness"] = float(_require_range(params["contact_thickness"], "contact_thickness", 0.001, 2.0))
    params["particle_radius"] = float(_require_range(params["particle_radius"], "particle_radius", 0.001, 5.0))
    params["ground_z"] = float(_require_range(params["ground_z"], "ground_z", -10000.0, 10000.0))
    params["scatter_count"] = int(_require_range(params["scatter_count"], "scatter_count", 0, 10000, is_int=True))
    params["angular_spin"] = float(_require_range(params["angular_spin"], "angular_spin", 0.0, 50.0))
    params["stiffness"] = float(_require_range(params["stiffness"], "stiffness", 0.0, 1.0))
    params["rest_length"] = float(_require_range(params["rest_length"], "rest_length", 0.0, 1000.0))
    params["wind_turbulence"] = float(_require_range(params["wind_turbulence"], "wind_turbulence", 0.0, 50.0))
    params["emit_rate"] = int(_require_range(params["emit_rate"], "emit_rate", 0, 5000, is_int=True))
    params["emit_speed"] = float(_require_range(params["emit_speed"], "emit_speed", 0.0, 100.0))
    params["emit_end_frame"] = int(_require_range(params["emit_end_frame"], "emit_end_frame", 1, 100000, is_int=True))
    params["lifetime"] = int(_require_range(params["lifetime"], "lifetime", 1, 100000, is_int=True))
    params["sdf_voxel_size"] = float(_require_range(params["sdf_voxel_size"], "sdf_voxel_size", 0.01, 2.0))
    params["buoyancy"] = float(_require_range(params["buoyancy"], "buoyancy", -50.0, 50.0))
    if preset_id == "rigid_box_tipping":
        half_extents = _require_vec3(params["box_half_extents"], "box_half_extents")
        if any(value <= 0.0 for value in half_extents):
            raise PhysicsContractError(
                "VALIDATION_ERROR",
                "box_half_extents components must be strictly positive",
            )
        params["box_half_extents"] = half_extents
        params["box_initial_tilt"] = float(_require_range(
            params["box_initial_tilt"],
            "box_initial_tilt",
            -math.pi / 2.0 + 1e-9,
            math.pi / 2.0 - 1e-9,
        ))

    target_object = str(raw.get("target_object", "")).strip()
    modifier_name = str(raw.get("modifier_name", "PhysicsNodes")).strip() or "PhysicsNodes"
    collider_object = str(raw["collider_object"]).strip() if raw.get("collider_object") else None
    collider_collection = str(raw["collider_collection"]).strip() if raw.get("collider_collection") else None
    instance_collection = str(raw["instance_collection"]).strip() if raw.get("instance_collection") else None

    if operation == "prepare_instances":
        if not instance_collection or domain != "rigid_scatter":
            raise PhysicsContractError("VALIDATION_ERROR", "prepare_instances requires a rigid preset and instance_collection")
        if raw.get("bake") or export_format != "NONE" or raw.get("verify_cold_reopen"):
            raise PhysicsContractError("VALIDATION_ERROR", "prepare_instances only prepares inputs; build/bake/export the returned collection in a subsequent request")

    return {
        "schema_version": SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "operation": operation,
        "domain": domain,
        "preset_id": preset_id,
        "bridge_mode": bridge_mode,
        "target_object": target_object,
        "modifier_name": modifier_name,
        "collider_object": collider_object,
        "collider_collection": collider_collection,
        "instance_collection": instance_collection,
        "bake": bool(raw.get("bake", False)),
        "bake_directory": str(raw["bake_directory"]).strip() if raw.get("bake_directory") else None,
        "force_rebuild": bool(raw.get("force_rebuild", False)),
        "verify_cold_reopen": bool(raw.get("verify_cold_reopen", False)),
        "export_format": export_format,
        "export_path": str(raw["export_path"]).strip() if raw.get("export_path") else None,
        "export_frame": int(_require_range(raw.get("export_frame", params["frame_end"]), "export_frame", params["frame_start"], params["frame_end"], is_int=True)),
        "parameters": params,
        "parameters_hash": compute_parameters_hash(params, bridge_mode, preset_id, collider_object, collider_collection, instance_collection),
    }


def describe_contract() -> dict[str, Any]:
    return {
        "contract_id": CONTRACT_ID,
        "schema_version": SCHEMA_VERSION,
        "operations": list(OPERATIONS),
        "domains": list(DOMAINS),
        "bridge_modes": list(BRIDGE_MODES),
        "export_formats": list(EXPORT_FORMATS),
        "capabilities": capabilities.describe_capabilities(),
        "native_fluid": {
            "bridge_mode": "MANTAFLOW_BRIDGE",
            "presets": NATIVE_FLUID_PRESETS.copy(),
            "operations": list(NATIVE_FLUID_ACTIONS),
            "tunable": {
                "native_liquid": {**NATIVE_INTEGER_SETTINGS, **NATIVE_FLOAT_SETTINGS,
                                  "simulation_method": ["APIC", "FLIP"]},
                "native_gas": {**NATIVE_INTEGER_SETTINGS, "time_scale": NATIVE_FLOAT_SETTINGS["time_scale"]},
            },
        },
        "presets": {
            k: {
                "domain": v["domain"],
                "title": v["title"],
                "description": v["description"],
                "defaults": copy.deepcopy(v["defaults"]),
            }
            for k, v in PRESET_DEFINITIONS.items()
        },
        "bounds": {
            "substeps": [1, 8],
            "windows_bake_file_max": 259,
            "windows_bake_parent": "validated including owned generation suffix before write",
            "curve_resolution": [2, 50000],
            "contact_iterations": [1, 32],
            "constraint_iterations": [1, 64],
        },
    }
