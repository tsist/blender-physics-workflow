"""Headless Blender worker invoked by physics_workflow_cli_v1.

The worker repeats the runner's path-safety checks before opening the input
file.  This makes direct worker invocation an unsupported bypass route: a
request that would overwrite its own input, a hardlink, or an existing output
without explicit authorization is rejected with a structured result.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
from typing import Any

import bpy


_CLI_DIR = Path(__file__).resolve().parent
if str(_CLI_DIR) not in sys.path:
    sys.path.insert(0, str(_CLI_DIR))

import path_guard  # noqa: E402  (Blender script path setup must precede import)


def _read_args() -> dict[str, str]:
    argv = sys.argv
    if "--" not in argv:
        raise RuntimeError("Worker requires '-- <request_json> <output_json>'")
    rest = argv[argv.index("--") + 1 :]
    if len(rest) < 2:
        raise RuntimeError("Worker requires <request_json> and <output_json>")
    return {"request_path": rest[0], "result_path": rest[1]}


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return error


def _write_result(result_path: Path, payload: dict[str, Any]) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = result_path.with_name(result_path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, result_path)


def _normalized_report(normalized: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "operation",
        "domain",
        "preset_id",
        "target_object",
        "modifier_name",
        "parameters",
        "parameters_hash",
        "bake_directory",
        "export_format",
        "export_path",
        "export_frame",
    )
    return {key: normalized.get(key) for key in keys}


def _collect_evidence(normalized: dict[str, Any], target_object: str) -> dict[str, Any]:
    from geometry_evidence import GeometryEvidenceError, sample_frames

    obj = bpy.data.objects.get(target_object)
    if obj is None:
        return {
            "ok": False,
            "frames": {},
            "error": _error("SCENE_STATE_ERROR", f"Target object not found for evidence: {target_object}"),
        }
    parameters = normalized["parameters"]
    frame_start = int(parameters["frame_start"])
    frame_end = int(parameters["frame_end"])
    mid = (frame_start + frame_end) // 2
    try:
        return sample_frames(obj, [frame_start, mid, frame_end])
    except GeometryEvidenceError as exc:
        return {"ok": False, "frames": {}, "error": _error(exc.code, exc.message)}
    except Exception as exc:
        return {
            "ok": False,
            "frames": {},
            "error": _error("EVIDENCE_ERROR", str(exc), traceback=traceback.format_exc()[-4000:]),
        }


def _needs_cache_working_copy(request, bake_module):
    """An owned cache may be invalidated or superseded by a parameter edit or rebake."""
    if request["operation"] not in {"build", "update", "parameters", "adopt", "bake"}:
        return False
    obj = bpy.data.objects.get(request["target_object"]) if request["target_object"] else bpy.context.active_object
    modifier = obj.modifiers.get(request["modifier_name"]) if obj else None
    if modifier is None or modifier.type != "NODES" or not bake_module._current_workflow_binding(obj, modifier):
        return False
    state = json.loads(obj.get("pnw_state_json__" + modifier.name, "{}"))
    return (request["operation"] == "bake" or request.get("bake") or request.get("force_rebuild")
            or state.get("parameters_hash") != request["parameters_hash"])


def main() -> int:
    try:
        args = _read_args()
    except Exception as exc:
        print(f"worker argument error: {exc}", file=sys.stderr)
        return 2

    req_path = Path(args["request_path"]).resolve()
    res_path = Path(args["result_path"]).resolve()
    result: dict[str, Any] = {
        "ok": False,
        "worker_version": "2.0",
        "error": _error("EXECUTION_ERROR", "Worker did not execute"),
    }
    normalized: dict[str, Any] | None = None
    cache_copy = None

    try:
        payload = json.loads(req_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Worker request payload must be a JSON object")

        input_blend = path_guard.normalize_path(payload.get("input_blend"))
        output_blend = path_guard.normalize_path(payload.get("output_blend"))
        allow_overwrite = path_guard.overwrite_authorized(payload)

        # Independent guard: runs before wm.open_mainfile and before any output
        # parent directory is created.
        path_guard.guard_output_path(
            input_blend,
            output_blend,
            allow_overwrite=allow_overwrite,
            reserved_paths=[req_path, res_path],
            role="output_blend",
        )
        if not input_blend.is_file():
            raise path_guard.PathGuardError("SCENE_STATE_ERROR", f"input_blend not found: {input_blend}")

        tools_dir = str(Path(payload["tools_dir"]).resolve())
        if tools_dir not in sys.path:
            # Keep the CLI overlay's own modules at index 0 while still making
            # the intended addon package win over unrelated cwd packages.
            sys.path.insert(1, tools_dir)
        import physics_nodes_workflow_addon.contract as contract
        import physics_nodes_workflow_addon as pnw

        raw_request = payload.get("request")
        normalized = contract.normalize_request(raw_request)
        export_path = normalized.get("export_path")
        if normalized.get("export_format", "NONE") != "NONE" and export_path:
            path_guard.guard_output_path(
                input_blend,
                export_path,
                allow_overwrite=allow_overwrite,
                reserved_paths=[output_blend, req_path, res_path],
                role="export_path",
            )

        opened = bpy.ops.wm.open_mainfile(filepath=str(input_blend), use_scripts=False)
        if "FINISHED" not in opened:
            raise RuntimeError(f"Blender could not open input blend: {opened}")

        if _needs_cache_working_copy(normalized, pnw.pnw_bake):
            # The native owner includes the saved project path. Run a write
            # from a genuine separate project so the input's cache stays usable.
            cache_copy = tempfile.TemporaryDirectory(prefix="physics-copy-", dir=req_path.parent)
            copy_file = Path(cache_copy.name) / "working.blend"
            if bpy.ops.wm.save_as_mainfile(filepath=str(copy_file), relative_remap=True) != {"FINISHED"}:
                raise RuntimeError("Could not isolate the input project's owned cache")
        execution = pnw.execute_request(normalized)
        if not isinstance(execution, dict):
            raise RuntimeError("Addon execute_request returned a non-object result")
        if normalized["operation"] == "invalidate_cache" and execution.get("ok"):
            invalidation = execution.get("invalidate") or {}
            if not invalidation.get("invalidated"):
                execution["ok"] = False
                execution["error"] = _error(
                    "CACHE_INVALIDATION_REFUSED",
                    str(invalidation.get("reason") or "Cache was not invalidated"),
                    invalidate=invalidation,
                )
        result = execution
        result.setdefault("worker_version", "2.0")
        result["normalized_request"] = _normalized_report(normalized)

        if result.get("ok"):
            if not allow_overwrite and output_blend.exists():
                raise path_guard.PathGuardError(
                    "OUTPUT_EXISTS",
                    f"output_blend appeared during execution; refusing to replace it: {output_blend}",
                )
            output_blend.parent.mkdir(parents=True, exist_ok=True)
            saved = bpy.ops.wm.save_as_mainfile(filepath=str(output_blend))
            if "FINISHED" not in saved:
                raise RuntimeError(f"Blender could not save output blend: {saved}")
            result["output_blend"] = str(output_blend)

            if payload.get("collect_verification_evidence"):
                target_object = result.get("target_object") or normalized.get("target_object")
                result["verification_evidence"] = _collect_evidence(normalized, target_object)

    except path_guard.PathGuardError as exc:
        result = {
            "ok": False,
            "worker_version": "2.0",
            "error": exc.to_error(),
        }
    except Exception as exc:
        code = getattr(exc, "code", "EXECUTION_ERROR")
        result = {
            "ok": False,
            "worker_version": "2.0",
            "error": _error(
                str(code),
                str(exc),
                traceback=traceback.format_exc()[-6000:],
            ),
        }
    finally:
        if cache_copy is not None:
            cache_copy.cleanup()

    if normalized is not None:
        result.setdefault("normalized_request", _normalized_report(normalized))
    result.setdefault("worker_version", "2.0")

    try:
        _write_result(res_path, result)
    except Exception as exc:
        print(f"worker could not write result: {exc}", file=sys.stderr)
        return 2

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
