"""Independent cold-reopen verifier executed in a fresh Blender process.

Verification is fail-closed: full vertex/topology/instance digests are required
for every sampled frame.  A missing expected summary, a non-finite value, a
count mismatch (for example expected 999 vertices while the file has 2), or a
digest mismatch fails the verification.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sys
import traceback
from typing import Any

import bpy


_CLI_DIR = Path(__file__).resolve().parent
if str(_CLI_DIR) not in sys.path:
    sys.path.insert(0, str(_CLI_DIR))

from geometry_evidence import (  # noqa: E402  (Blender script path setup must precede import)
    COUNT_FIELDS,
    DIGEST_FIELDS,
    EVIDENCE_SCHEMA,
    GeometryEvidenceError,
    sample_frame,
)

_CONFIGURED_ADDON = os.environ.get("PHYSICS_WORKFLOW_ADDON_DIR")
_ADDON_PARENT = (
    Path(_CONFIGURED_ADDON).resolve().parent
    if _CONFIGURED_ADDON
    else Path(__file__).resolve().parent.parent
)
if str(_ADDON_PARENT) not in sys.path:
    sys.path.insert(0, str(_ADDON_PARENT))

from physics_nodes_workflow_addon import bake as pnw_bake  # noqa: E402
from physics_nodes_workflow_addon import subgroups as pnw_subgroups  # noqa: E402


_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
CENTROID_TOLERANCE = 1e-4


def _read_object_state(obj: bpy.types.Object, modifier_name: str) -> dict[str, Any]:
    for key in (f"pnw_state_json__{modifier_name}", "pnw_state_json"):
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


def _actual_core_tree(modifier: bpy.types.Modifier) -> Any:
    wrapper = getattr(modifier, "node_group", None)
    if wrapper is None:
        return None
    core_node = wrapper.nodes.get("PNW_CoreNode") or next(
        (
            node
            for node in wrapper.nodes
            if node.bl_idname == "GeometryNodeGroup" and getattr(node, "node_tree", None) is not None
        ),
        None,
    )
    return core_node.node_tree if core_node is not None else None


def _is_managed_pnw(obj: bpy.types.Object, modifier: bpy.types.Modifier | None) -> bool:
    if modifier is None or modifier.type != "NODES" or modifier.node_group is None:
        return False
    wrapper = modifier.node_group
    if wrapper.name.startswith("PNW_Wrapper_"):
        return True
    if wrapper.nodes.get("PNW_CoreNode") is not None:
        return True
    if _actual_core_tree(modifier) is not None:
        return True
    state = _read_object_state(obj, modifier.name)
    return bool(state.get("parameters_hash") or state.get("graph_fingerprint"))


def _find_managed_modifier(obj: bpy.types.Object) -> bpy.types.Modifier | None:
    for modifier in obj.modifiers:
        if _is_managed_pnw(obj, modifier):
            return modifier
    return None


def _cache_validation(obj: bpy.types.Object, spec: dict[str, Any]) -> dict[str, Any]:
    """Validate the actual cache generation consumed by the reopened modifier."""

    required = bool(spec.get("require_cache") is True or spec.get("cache_required") is True)
    requested_name = str(spec.get("modifier_name") or "").strip()
    modifier = obj.modifiers.get(requested_name) if requested_name else None
    managed = _is_managed_pnw(obj, modifier)
    if modifier is None and not requested_name:
        modifier = _find_managed_modifier(obj)
        managed = modifier is not None
    if modifier is not None and not managed:
        managed = _is_managed_pnw(obj, modifier)

    base_info: dict[str, Any] = {
        "required": required or managed,
        "managed": bool(managed),
        "modifier_name": modifier.name if modifier is not None else requested_name or None,
        "valid": None,
        "reason": "not_managed_pnw",
    }
    cache_spec = spec.get("cache") if isinstance(spec.get("cache"), dict) else {}
    expected_directory = cache_spec.get("expected_directory") or spec.get("bake_directory")
    if expected_directory:
        base_info["expected_directory"] = str(expected_directory)

    if modifier is None and requested_name:
        other_managed = _find_managed_modifier(obj)
        if other_managed is not None:
            base_info.update(
                required=True,
                managed=True,
                valid=False,
                reason="modifier_missing",
            )
            return base_info
    if not managed:
        if required:
            base_info.update(valid=False, reason="managed_cache_required_but_modifier_not_managed")
        return base_info
    if modifier is None:
        base_info.update(valid=False, reason="modifier_missing")
        return base_info
    if modifier.type != "NODES" or modifier.node_group is None:
        base_info.update(valid=False, reason="modifier_not_managed")
        return base_info

    state = _read_object_state(obj, modifier.name)
    state_parameters_hash = str(state.get("parameters_hash") or "")
    state_graph_fingerprint = str(state.get("graph_fingerprint") or "")
    parameters_hash = str(spec.get("parameters_hash") or "")
    hash_source = "spec" if parameters_hash else "unavailable"
    if not parameters_hash and state_parameters_hash:
        parameters_hash = state_parameters_hash
        hash_source = "object_state"
    base_info.update(
        parameters_hash=parameters_hash,
        hash_source=hash_source,
        state_parameters_hash=state_parameters_hash,
        state_graph_fingerprint=state_graph_fingerprint,
    )
    if not parameters_hash:
        base_info.update(valid=False, reason="parameters_hash_missing")
        return base_info
    if spec.get("parameters_hash") and state_parameters_hash and str(spec["parameters_hash"]) != state_parameters_hash:
        base_info.update(valid=False, reason="state_parameters_hash_mismatch")
        return base_info

    core_tree = _actual_core_tree(modifier)
    if core_tree is None:
        base_info.update(valid=False, reason="core_tree_missing")
        return base_info
    try:
        graph_fingerprint = pnw_subgroups.tree_fingerprint(core_tree)
    except Exception as exc:
        base_info.update(valid=False, reason=f"graph_fingerprint_error:{type(exc).__name__}")
        return base_info
    base_info["graph_fingerprint"] = graph_fingerprint
    if state_graph_fingerprint and state_graph_fingerprint != graph_fingerprint:
        base_info.update(valid=False, reason="state_graph_fingerprint_mismatch")
        return base_info

    try:
        valid, reason, bake_directory = pnw_bake.is_cache_valid(
            obj, modifier, parameters_hash, graph_fingerprint
        )
    except Exception as exc:
        base_info.update(valid=False, reason=f"cache_validation_error:{type(exc).__name__}")
        return base_info

    base_info.update(
        valid=bool(valid),
        reason=str(reason),
        bake_directory=str(bake_directory),
    )
    if expected_directory:
        try:
            base_info["expected_directory_matches_actual"] = (
                Path(expected_directory).resolve() == Path(bake_directory).resolve()
            )
        except (OSError, ValueError):
            base_info["expected_directory_matches_actual"] = False
    return base_info


def _read_args() -> dict[str, str]:
    argv = sys.argv
    if "--" not in argv:
        raise RuntimeError("Verifier requires '-- <verify_json> <output_json>'")
    rest = argv[argv.index("--") + 1 :]
    if len(rest) < 2:
        raise RuntimeError("Verifier requires <verify_json> and <output_json>")
    return {"spec_path": rest[0], "result_path": rest[1]}


def _write_result(result_path: Path, payload: dict[str, Any]) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = result_path.with_name(result_path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(result_path)


def _fail(result_path: Path, reason_code: str, message: str, **extra: Any) -> int:
    payload: dict[str, Any] = {
        "passed": False,
        "reason_code": reason_code,
        "error": message,
        "verifier_version": "2.0",
        "evidence_schema": EVIDENCE_SCHEMA,
    }
    payload.update(extra)
    _write_result(result_path, payload)
    return 1


def _int_field(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} must be an integer")
    return parsed


def _find_nonfinite(value: Any, path: str = "$") -> list[str]:
    problems: list[str] = []
    if isinstance(value, float):
        if not math.isfinite(value):
            problems.append(f"{path}={value!r}")
    elif isinstance(value, dict):
        for key, item in value.items():
            problems.extend(_find_nonfinite(item, f"{path}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            problems.extend(_find_nonfinite(item, f"{path}[{index}]"))
    return problems


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX_DIGEST.fullmatch(value))


def _number_list(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        numbers = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(number) for number in numbers):
        return None
    return numbers


def _compare_summary(actual: dict[str, Any], expected: dict[str, Any], frame: int) -> tuple[list[dict[str, Any]], list[str]]:
    checks: list[dict[str, Any]] = []
    failures: list[str] = []

    def record(field: str, passed: bool, expected_value: Any, actual_value: Any, code: str) -> None:
        check = {
            "frame": frame,
            "field": field,
            "passed": bool(passed),
            "expected": expected_value,
            "actual": actual_value,
        }
        checks.append(check)
        if not passed:
            failures.append(code)

    record("evidence_schema", expected.get("evidence_schema") == EVIDENCE_SCHEMA
           and actual.get("evidence_schema") == EVIDENCE_SCHEMA,
           expected.get("evidence_schema"), actual.get("evidence_schema"), "EVIDENCE_SCHEMA_MISMATCH")
    # Compare the entire declaration, including types: bool True is not integer 1.
    # Missing or invented coverage must never widen the observed contract.
    wanted = expected.get("state_coverage")
    observed = actual.get("state_coverage")
    record("state_coverage", isinstance(wanted, dict) and
           json.dumps(wanted, sort_keys=True) == json.dumps(observed, sort_keys=True),
           wanted, observed, "STATE_COVERAGE_MISMATCH")
    # Do not accept a correct top-level digest alongside an unverified payload
    # claiming different attributes or internal-state coverage. Diagnostic speed
    # statistics are excluded: their resting threshold is caller configurable.
    expected_attributes = expected.get("physics_attributes")
    actual_attributes = actual.get("physics_attributes") or {}
    for field in ("schema", "components", "attributes", "scope", "sha256", "coverage"):
        wanted = expected_attributes.get(field) if isinstance(expected_attributes, dict) else None
        observed = actual_attributes.get(field)
        record("physics_attributes."+field, wanted is not None and
               json.dumps(wanted, sort_keys=True) == json.dumps(observed, sort_keys=True),
               wanted, observed, "ATTRIBUTE_PAYLOAD_MISMATCH")

    for field in COUNT_FIELDS:
        expected_value = expected.get(field)
        if isinstance(expected_value, bool) or not isinstance(expected_value, int) or expected_value < 0:
            record(field, False, expected_value, actual.get(field), "INVALID_EXPECTED_COUNT")
            continue
        record(field, expected_value == actual.get(field), expected_value, actual.get(field), "COUNT_MISMATCH")

    for field in DIGEST_FIELDS:
        expected_value = expected.get(field)
        if not _valid_digest(expected_value):
            record(field, False, expected_value, actual.get(field), "INVALID_EXPECTED_DIGEST")
            continue
        record(field, expected_value == actual.get(field), expected_value, actual.get(field), "DIGEST_MISMATCH")

    expected_has_geometry = expected.get("has_geometry")
    if expected_has_geometry is not True:
        record("has_geometry", False, expected_has_geometry, actual.get("has_geometry"), "INVALID_EXPECTED_COVERAGE")
    elif actual.get("has_geometry") is not True:
        record("has_geometry", False, expected_has_geometry, actual.get("has_geometry"), "EMPTY_ACTUAL_GEOMETRY")
    else:
        record("has_geometry", True, expected_has_geometry, actual.get("has_geometry"), "OK")

    for field in ("centroid", "bbox_min", "bbox_max"):
        if field not in expected:
            continue
        expected_numbers = _number_list(expected.get(field))
        actual_numbers = _number_list(actual.get(field))
        if expected_numbers is None:
            record(field, False, expected.get(field), actual.get(field), "INVALID_EXPECTED_VECTOR")
            continue
        if actual_numbers is None:
            record(field, False, expected_numbers, actual.get(field), "INVALID_ACTUAL_VECTOR")
            continue
        difference = max(abs(expected_numbers[index] - actual_numbers[index]) for index in range(3))
        record(field, difference <= CENTROID_TOLERANCE, expected_numbers, actual_numbers, "CENTROID_MISMATCH")

    return checks, failures


def _repeat_equal(first: dict[str, Any], last: dict[str, Any]) -> bool:
    for field in COUNT_FIELDS + DIGEST_FIELDS + ("has_geometry", "evidence_schema", "state_coverage"):
        if first.get(field) != last.get(field):
            return False
    return True


def _max_vector_diff(first: Any, last: Any) -> float:
    first_numbers = _number_list(first)
    last_numbers = _number_list(last)
    if first_numbers is None or last_numbers is None:
        return math.inf
    return max(abs(first_numbers[index] - last_numbers[index]) for index in range(3))


def main() -> int:
    try:
        args = _read_args()
    except Exception as exc:
        print(f"verifier argument error: {exc}", file=sys.stderr)
        return 2

    spec_path = Path(args["spec_path"]).resolve()
    result_path = Path(args["result_path"]).resolve()

    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        if not isinstance(spec, dict):
            return _fail(result_path, "INVALID_SPEC", "Verify spec must be a JSON object")

        blend_path = Path(spec["output_blend"]).resolve()
        if not blend_path.is_file():
            return _fail(result_path, "OUTPUT_MISSING", f"output_blend not found: {blend_path}")

        frame_start = _int_field(spec["frame_start"], "frame_start")
        frame_end = _int_field(spec["frame_end"], "frame_end")
        if frame_start > frame_end:
            return _fail(result_path, "INVALID_FRAME_RANGE", "frame_start must be <= frame_end")
        target_name = str(spec.get("target_object", "")).strip()
        if not target_name:
            return _fail(result_path, "INVALID_SPEC", "target_object is required")

        raw_expected = spec.get("expected_frames")
        if not isinstance(raw_expected, dict) or not raw_expected:
            legacy = spec.get("expected_end_summary")
            if isinstance(legacy, dict):
                raw_expected = {str(frame_end): legacy}
            else:
                return _fail(
                    result_path,
                    "MISSING_EXPECTED_EVIDENCE",
                    "expected_frames is required; a centroid-only or absent summary is not evidence",
                )

        expected_frames: dict[str, dict[str, Any]] = {}
        for raw_frame, summary in raw_expected.items():
            frame = _int_field(raw_frame, f"expected_frames key {raw_frame!r}")
            if frame < frame_start or frame > frame_end:
                return _fail(
                    result_path,
                    "INVALID_EXPECTED_EVIDENCE",
                    f"Expected frame {frame} is outside [{frame_start}, {frame_end}]",
                )
            if not isinstance(summary, dict):
                return _fail(
                    result_path,
                    "INVALID_EXPECTED_EVIDENCE",
                    f"Expected frame {frame} summary must be an object",
                )
            expected_frames[str(frame)] = summary

        nonfinite = _find_nonfinite(expected_frames)
        if nonfinite:
            return _fail(
                result_path,
                "NON_FINITE_EXPECTED_EVIDENCE",
                "Expected evidence contains non-finite values",
                non_finite_fields=nonfinite[:20],
            )

        mid = (frame_start + frame_end) // 2
        required_frames = sorted(set([frame_start, mid, frame_end] + [int(key) for key in expected_frames]))
        missing_expected = [frame for frame in required_frames if str(frame) not in expected_frames]
        if missing_expected:
            return _fail(
                result_path,
                "MISSING_EXPECTED_FRAME",
                "Expected evidence is missing sampled frames: " + ", ".join(str(frame) for frame in missing_expected),
                missing_frames=missing_expected,
            )

        opened = bpy.ops.wm.open_mainfile(filepath=str(blend_path), use_scripts=False)
        if "FINISHED" not in opened:
            return _fail(result_path, "OPEN_FAILED", f"Blender could not open output blend: {opened}")
        obj = bpy.data.objects.get(target_name)
        if obj is None:
            return _fail(
                result_path,
                "TARGET_MISSING",
                f"Target object missing after cold reopen: {target_name}",
            )

        cache_validation = _cache_validation(obj, spec)
        if cache_validation.get("valid") is False:
            return _fail(
                result_path,
                "CACHE_INVALID",
                "Managed PNW cache is invalid: " + str(cache_validation.get("reason")),
                cache_validation=cache_validation,
            )

        scrambled = [frame_end] + [frame for frame in required_frames if frame != frame_end] + [frame_end]
        samples: dict[str, dict[str, Any]] = {}
        end_first: dict[str, Any] | None = None
        end_last: dict[str, Any] | None = None
        try:
            for frame in scrambled:
                sample = sample_frame(obj, frame)
                if frame == frame_end:
                    if end_first is None:
                        end_first = sample
                    end_last = sample
                samples.setdefault(str(frame), sample)
        except GeometryEvidenceError as exc:
            return _fail(result_path, exc.code, exc.message, samples=list(samples))

        checks: list[dict[str, Any]] = []
        failures: list[str] = []
        max_expected_centroid_diff = 0.0
        for frame in required_frames:
            actual = samples[str(frame)]
            expected = expected_frames[str(frame)]
            frame_checks, frame_failures = _compare_summary(actual, expected, frame)
            checks.extend(frame_checks)
            failures.extend(frame_failures)
            if "centroid" in expected and isinstance(actual.get("centroid"), list):
                expected_centroid = _number_list(expected.get("centroid"))
                actual_centroid = _number_list(actual.get("centroid"))
                if expected_centroid is not None and actual_centroid is not None:
                    max_expected_centroid_diff = max(
                        max_expected_centroid_diff,
                        max(abs(expected_centroid[i] - actual_centroid[i]) for i in range(3)),
                    )

        repeat_equal = end_first is not None and end_last is not None and _repeat_equal(end_first, end_last)
        max_repeat_centroid_diff = (
            _max_vector_diff(end_first.get("centroid"), end_last.get("centroid"))
            if end_first is not None and end_last is not None
            else math.inf
        )
        if not repeat_equal:
            failures.append("REPEAT_EVIDENCE_MISMATCH")

        checks_total = len(checks) + 1
        checks_failed = sum(1 for check in checks if not check["passed"])
        if not repeat_equal:
            checks_failed += 1
        passed = not failures
        reason_code = "VERIFICATION_PASSED" if passed else (failures[0] if failures else "GEOMETRY_MISMATCH")

        output = {
            "passed": passed,
            "reason_code": reason_code,
            "verifier_version": "2.0",
            "evidence_schema": EVIDENCE_SCHEMA,
            "output_blend": str(blend_path),
            "target_object": target_name,
            "frame_range": [frame_start, frame_end],
            "scrambled_frames": scrambled,
            "expected_frames_used": required_frames,
            "samples": [samples[str(frame)] for frame in sorted(samples, key=int)],
            "checks_total": checks_total,
            "checks_failed": checks_failed,
            "checks": checks,
            "failures": failures,
            "repeat_end_equal": repeat_equal,
            "max_repeat_centroid_diff": None
            if not math.isfinite(max_repeat_centroid_diff)
            else max_repeat_centroid_diff,
            "max_expected_centroid_diff": max_expected_centroid_diff,
            "cache_validation": cache_validation,
        }
        _write_result(result_path, output)
        return 0 if passed else 1

    except Exception as exc:
        return _fail(
            result_path,
            "VERIFIER_ERROR",
            str(exc),
            traceback=traceback.format_exc()[-6000:],
        )


if __name__ == "__main__":
    sys.exit(main())
