"""CLI execution orchestrator for describe, run, sweep, and batch workflows."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import traceback
from typing import Any

from . import deployment
from . import job_control
from . import path_guard
from . import protocol
from . import resource_guard
from . import resume


_TOP_LEVEL_PARAM_KEYS = ("substeps", "seed", "frame_start", "frame_end")
_EXPORT_EXTENSIONS = {"STATIC_MESH": ".blend", "ALEMBIC": ".abc", "OPENVDB": ".vdb"}


def _failure(
    command: str,
    job_id: str,
    code: str,
    message: str,
    **details: Any,
) -> protocol.CommandResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return protocol.CommandResponse(ok=False, command=command, job_id=job_id, error=error)


def _short_bake_dir(jobs_root: Path, job_id: str) -> Path:
    short = hashlib.sha1(job_id.encode("utf-8")).hexdigest()[:10]
    return jobs_root / "bakes" / short


def _check_timeout(timeout_seconds: Any) -> str | None:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        return "timeout_seconds must be a positive integer"
    return None


def _check_memory_limit(memory_limit_mb: Any) -> str | None:
    if memory_limit_mb is None:
        return None
    if (
        isinstance(memory_limit_mb, bool)
        or not isinstance(memory_limit_mb, int)
        or memory_limit_mb < 0
        or memory_limit_mb > 65536
    ):
        return "memory_limit_mb must be an integer in [0, 65536]"
    return None


def _resource_summary(
    timeout_seconds: int,
    memory_limit_mb: int | None,
    result: Any = None,
) -> dict[str, Any]:
    enforcement = getattr(result, "enforcement", None)
    if not enforcement:
        enforcement = (
            "windows_job_object_process+job_memory_with_kill_on_close"
            if os.name == "nt"
            else "posix_rlimit_as_and_process_group"
        )
    summary = {
        "timeout_seconds": timeout_seconds,
        "memory_limit_mb": memory_limit_mb,
        "memory_limit_bytes": None if not memory_limit_mb else int(memory_limit_mb) * 1024 * 1024,
        "enforcement": enforcement,
    }
    if result is not None:
        summary["peak_job_memory_bytes"] = getattr(result, "peak_job_memory_bytes", None)
        summary["peak_process_memory_bytes"] = getattr(result, "peak_process_memory_bytes", None)
    return summary


def _run_blender_script(
    script_path: Path,
    arg_paths: list[Path],
    timeout_seconds: int = 300,
    memory_limit_mb: int | None = 4096,
    cancel_check: Any = None,
) -> resource_guard.GuardedResult:
    blender_bin = str(deployment.default_blender_binary())
    cmd = [
        blender_bin,
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--python-exit-code",
        "1",
        "--python",
        str(script_path),
        "--",
        *[str(p) for p in arg_paths],
    ]
    return resource_guard.run_guarded(
        cmd,
        timeout_seconds=timeout_seconds,
        memory_limit_mb=memory_limit_mb,
        scratch_dir=arg_paths[0].parent / ".resource",
        cancel_check=cancel_check,
    )


def _control_phase(
    control: job_control.JobControl | None,
    phase: str,
    *,
    plan: dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
    attempt: Any = None,
    candidate_status: str | None = None,
) -> None:
    if control is None:
        return
    try:
        control.set_phase(
            phase,
            plan=plan,
            candidate=candidate,
            attempt=attempt,
            candidate_status=candidate_status,
        )
    except Exception:
        # Status publication is best effort after the initial control record
        # has been published; it must not turn a running job into a crash.
        pass


def _cancelled_response(
    command: str,
    job_id: str,
    control: job_control.JobControl | None,
    message: str,
    **details: Any,
) -> protocol.CommandResponse:
    payload_details = dict(details)
    if control is not None:
        payload_details.update(control.cancellation_details())
    error: dict[str, Any] = {"code": "CANCELLED", "message": message}
    if payload_details:
        error["details"] = payload_details
    return protocol.CommandResponse(ok=False, command=command, job_id=job_id, error=error)


def _acquire_plan_lock(
    lock_state: dict[str, Any],
    manifest_path: Path,
    owner: str,
) -> resume.ManifestLock:
    if not isinstance(lock_state, dict):
        raise RuntimeError("Runner-owned lock state is missing")
    if lock_state.get("lock") is not None:
        raise RuntimeError("Runner-owned lock state already holds a manifest lock")
    lock = resume.ManifestLock(manifest_path, owner=owner)
    lock.acquire()
    lock_state["lock"] = lock
    return lock


def _release_plan_lock(lock_state: dict[str, Any] | None) -> None:
    if not isinstance(lock_state, dict):
        return
    lock = lock_state.pop("lock", None)
    if lock is not None:
        lock.release()


def _normalized_for_report(normalized: dict[str, Any], worker_res: dict[str, Any]) -> dict[str, Any]:
    worker_normalized = worker_res.get("normalized_request")
    if isinstance(worker_normalized, dict):
        return worker_normalized
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
    return {key: copy.deepcopy(normalized.get(key)) for key in keys}


def _append_existing_artifact(artifacts: list[str], value: Any) -> None:
    if not value:
        return
    try:
        path = Path(str(value))
    except (TypeError, ValueError):
        return
    if path.exists():
        artifacts.append(str(path))


def describe() -> protocol.CommandResponse:
    contract = deployment.import_addon_module("contract")
    cli_contract = contract.describe_contract()
    cli_contract.pop("native_fluid", None)
    blender_bin = deployment.default_blender_binary(require_exists=False)
    return protocol.CommandResponse(
        ok=True,
        command="describe",
        job_id=protocol.new_job_id("desc"),
        data={
            **cli_contract,
            "native_fluid_entrypoint": "blender_addon_panel",
            "blender_binary": str(blender_bin),
            "addon_directory": str(deployment.addon_directory()),
            "default_memory_limit_mb": 4096,
            "default_timeout_seconds": 300,
            "resume": {
                "manifest_schema": resume.PLAN_SCHEMA,
                "manifest_version": resume.PLAN_VERSION,
                "manifest_filename": resume.PLAN_FILENAME,
                "entrypoints": [
                    "physics-workflow resume --manifest <absolute resume_plan.json>",
                    "physics_workflow_public.resume_workflow",
                ],
                "execution_mode": "candidate_replay",
                "intermediate_frame_resume": False,
                "locking": "writers_and_resume_share_os_exclusive_byte_range_lock_with_owner_metadata",
                "success_verification": [
                    "input_sha256",
                    "receipt_sha256",
                    "output_sha256",
                    "export_sha256",
                    "cache_manifest_sha256_and_file_inventory",
                ],
            },
            "control": {
                "status_entrypoints": [
                    "physics-workflow status --manifest <absolute resume_plan.json>",
                    "physics_workflow_public.status_workflow",
                ],
                "cancel_entrypoints": [
                    "physics-workflow cancel --manifest <absolute resume_plan.json>",
                    "physics_workflow_public.cancel_workflow",
                ],
                "status_schema": job_control.STATUS_SCHEMA,
                "cancel_request_schema": job_control.REQUEST_SCHEMA,
                "control_filename": job_control.CONTROL_FILENAME,
                "cancel_request_filename": job_control.REQUEST_FILENAME,
                "cancel_binding": "request_id + manifest + plan_id + control generation",
                "cancel_owner": "only the original Job Object/process-group owner terminates its tree",
                "pid_kill": False,
                "terminal_cancelled": "written by the original executor after manifest lock release",
                "heartbeat_active_seconds": job_control.ACTIVE_HEARTBEAT_SECONDS,
            },
        },
    )


def run_single(
    raw_request: dict[str, Any],
    jobs_dir: Path,
    timeout_seconds: int = 300,
    memory_limit_mb: int | None = 4096,
    job_id: str | None = None,
    *,
    _control: job_control.JobControl | None = None,
) -> protocol.CommandResponse:
    job_id = job_id or protocol.new_job_id("run")
    lock_state: dict[str, Any] = {}
    response: protocol.CommandResponse | None = None
    try:
        try:
            response = _run_single_impl(
                raw_request,
                Path(jobs_dir),
                timeout_seconds,
                memory_limit_mb,
                job_id,
                _lock_state=lock_state,
                _control=_control,
            )
        except Exception as exc:
            response = _failure(
                "run",
                job_id,
                "EXECUTION_ERROR",
                f"Unhandled runner error: {exc}",
                traceback=traceback.format_exc()[-4000:],
            )
        try:
            finalized = resume.finalize_single_plan(
                Path(jobs_dir) / job_id,
                response,
                lock=lock_state.get("lock"),
            )
            if finalized is not None:
                # The finalizer reads and mutates its own plan object.  Keep
                # the executor's plan reference aligned so terminal control
                # counts and recoverability reflect the persisted attempt.
                lock_state["plan"] = finalized
                data = dict(response.data or {})
                data["resume_plan"] = str(Path(jobs_dir) / job_id / resume.PLAN_FILENAME)
                response.data = data
        except Exception as exc:
            data = dict(response.data or {})
            data["resume_plan_error"] = {
                "code": "PLAN_FINALIZE_FAILED",
                "message": str(exc),
            }
            response.data = data
            if response.ok:
                response.ok = False
                response.error = dict(data["resume_plan_error"])
        return response
    finally:
        control = lock_state.get("control")
        owns_control = bool(lock_state.get("control_owned"))
        plan = lock_state.get("plan")
        _release_plan_lock(lock_state)
        if owns_control and control is not None:
            error_code = ((response.error or {}) if response is not None else {}).get("code")
            if error_code == "CANCELLED" or control.cancel_seen:
                terminal = job_control.TERMINAL_CANCELLED
                reason = "cancel_request_observed_by_original_executor"
            elif response is not None and response.ok:
                terminal = job_control.TERMINAL_SUCCESS
                reason = "run_completed_successfully"
            else:
                terminal = job_control.TERMINAL_FAILED
                reason = error_code or "run_failed"
            control.finish_after_unlock(terminal, plan=plan, reason=reason)


def _run_single_impl(
    raw_request: dict[str, Any],
    jobs_dir: Path,
    timeout_seconds: int,
    memory_limit_mb: int | None,
    job_id: str,
    *,
    _lock_state: dict[str, Any] | None = None,
    _control: job_control.JobControl | None = None,
) -> protocol.CommandResponse:
    contract = deployment.import_addon_module("contract")
    timeout_error = _check_timeout(timeout_seconds)
    if timeout_error:
        return _failure("run", job_id, "VALIDATION_ERROR", timeout_error)
    memory_error = _check_memory_limit(memory_limit_mb)
    if memory_error:
        return _failure("run", job_id, "VALIDATION_ERROR", memory_error)
    if not isinstance(raw_request, dict):
        return _failure("run", job_id, "VALIDATION_ERROR", "Run request payload must be a JSON object")

    if str(raw_request.get("preset_id", "")).strip() in getattr(contract, "NATIVE_FLUID_PRESETS", ()):
        return _failure(
            "run", job_id, "VALIDATION_ERROR",
            "Native liquid and gas are available in the Blender add-on Native Fluid panel.",
        )

    try:
        normalized = contract.normalize_request(raw_request)
    except contract.PhysicsContractError as exc:
        return _failure("run", job_id, exc.code, exc.message)

    input_value = raw_request.get("input_blend")
    if not input_value:
        return _failure("run", job_id, "VALIDATION_ERROR", "input_blend path is required")

    try:
        jobs_root = path_guard.normalize_path(jobs_dir)
        input_blend = path_guard.normalize_path(input_value)
    except path_guard.PathGuardError as exc:
        return protocol.CommandResponse(
            ok=False,
            command="run",
            job_id=job_id,
            error=exc.to_error(),
        )
    if not input_blend.is_file():
        return _failure("run", job_id, "SCENE_STATE_ERROR", f"input_blend not found: {input_blend}")

    job_dir = jobs_root / job_id
    req_file = job_dir / "worker_request.json"
    res_file = job_dir / "worker_result.json"
    worker_stdout = job_dir / "worker_stdout.log"
    worker_stderr = job_dir / "worker_stderr.log"
    verify_spec_file = job_dir / "verify_spec.json"
    verify_result_file = job_dir / "verify_result.json"
    verify_stdout = job_dir / "verify_stdout.log"
    verify_stderr = job_dir / "verify_stderr.log"
    receipt_file = job_dir / "receipt.json"
    resume_plan_file = job_dir / resume.PLAN_FILENAME
    job_files = [
        jobs_root,
        job_dir,
        req_file,
        res_file,
        worker_stdout,
        worker_stderr,
        verify_spec_file,
        verify_result_file,
        verify_stdout,
        verify_stderr,
        receipt_file,
        resume_plan_file,
    ]

    try:
        output_blend = path_guard.guard_output_path(
            input_blend,
            raw_request.get("output_blend") or (job_dir / "output.blend"),
            allow_overwrite=path_guard.overwrite_authorized(raw_request),
            reserved_paths=job_files,
            role="output_blend",
        )
        export_path = (
            str(normalized.get("export_path"))
            if normalized.get("export_format", "NONE") != "NONE" and normalized.get("export_path")
            else None
        )
        if export_path:
            path_guard.guard_output_path(
                input_blend,
                export_path,
                allow_overwrite=path_guard.overwrite_authorized(raw_request),
                reserved_paths=[output_blend, *job_files],
                role="export_path",
            )
    except path_guard.PathGuardError as exc:
        return protocol.CommandResponse(ok=False, command="run", job_id=job_id, error=exc.to_error())

    allow_overwrite = path_guard.overwrite_authorized(raw_request)
    try:
        job_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return _failure("run", job_id, "OUTPUT_EXISTS", f"Job directory already exists: {job_dir}")
    except OSError as exc:
        return _failure("run", job_id, "EXECUTION_ERROR", f"Cannot create job directory: {exc}", job_dir=str(job_dir))

    try:
        _acquire_plan_lock(_lock_state, resume_plan_file, f"run:{job_id}")
    except (resume.ResumeError, OSError, RuntimeError) as exc:
        return _failure(
            "run",
            job_id,
            "PLAN_LOCK_FAILED",
            f"Cannot own the run plan lock: {exc}",
            job_dir=str(job_dir),
            lock_path=str(resume.manifest_lock_path(resume_plan_file)),
        )

    input_sha256 = protocol.sha256_file(input_blend)
    worker_req = copy.deepcopy(raw_request)
    worker_req["allow_overwrite"] = allow_overwrite
    if bool(worker_req.get("bake")) or str(worker_req.get("operation", "build")) == "bake":
        if not worker_req.get("bake_directory"):
            worker_req["bake_directory"] = str(_short_bake_dir(jobs_root, job_id))

    worker_payload = {
        "tools_dir": str(deployment.addon_directory().parent),
        "input_blend": str(input_blend),
        "output_blend": str(output_blend),
        "allow_overwrite": allow_overwrite,
        "collect_verification_evidence": bool(normalized.get("verify_cold_reopen")),
        "request": worker_req,
    }
    try:
        req_file.write_text(json.dumps(worker_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        return _failure("run", job_id, "EXECUTION_ERROR", f"Cannot write worker request: {exc}", job_dir=str(job_dir))

    candidate: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    attempt: dict[str, Any] | None = None
    try:
        candidate = resume.make_candidate(
            "run_00",
            0,
            "run",
            raw_request,
            normalized=normalized,
            input_identity=(str(input_blend), input_sha256, None),
        )
        plan = resume.create_plan(
            "run",
            raw_request,
            job_id,
            jobs_root,
            job_dir,
            [candidate],
            contract_id=contract.CONTRACT_ID,
        )
        attempt = resume.begin_attempt(
            candidate,
            job_id,
            job_dir,
            {
                "output_blend": output_blend,
                "bake_directory": worker_req.get("bake_directory"),
                "export_path": export_path,
            },
            resume.request_sha256(raw_request),
        )
        resume.write_plan(resume_plan_file, plan)
    except (OSError, TypeError, ValueError, resume.ResumeError) as exc:
        return _failure(
            "run",
            job_id,
            "PLAN_WRITE_FAILED",
            f"Cannot create the persistent resume plan: {exc}",
            job_dir=str(job_dir),
            resume_plan=str(resume_plan_file),
        )

    owns_control = _control is None
    control = _control
    if control is None:
        control = job_control.JobControl(
            resume_plan_file,
            owner=f"run:{job_id}",
            plan=plan,
        )
        try:
            control.start()
        except job_control.JobControlError as exc:
            return protocol.CommandResponse(
                ok=False,
                command="run",
                job_id=job_id,
                data={"job_dir": str(job_dir), "resume_plan": str(resume_plan_file)},
                error=exc.to_error(),
            )
        if isinstance(_lock_state, dict):
            _lock_state["control"] = control
            _lock_state["control_owned"] = True
            _lock_state["plan"] = plan
        _control_phase(
            control,
            "worker_starting",
            plan=plan,
            candidate=candidate,
            attempt=attempt,
        )
    else:
        _control_phase(control, "worker_starting")

    if control.cancel_requested_now():
        return _cancelled_response(
            "run",
            job_id,
            control,
            "Cancelled before the worker was launched",
            job_dir=str(job_dir),
        )
    _control_phase(
        control,
        "worker_running",
        plan=plan if owns_control else None,
        candidate=candidate if owns_control else None,
        attempt=attempt,
        candidate_status="running",
    )

    worker_proc = None
    verifier_proc = None
    worker_script = Path(__file__).resolve().parent / "worker.py"
    try:
        proc = _run_blender_script(
            worker_script,
            [req_file, res_file],
            timeout_seconds=timeout_seconds,
            memory_limit_mb=memory_limit_mb,
            cancel_check=control.poll_cancel,
        )
        worker_proc = proc
    except resource_guard.ProcessCancelled as exc:
        return _cancelled_response(
            "run",
            job_id,
            control,
            exc.message,
            job_dir=str(job_dir),
            **exc.details,
        )
    except resource_guard.MemoryLimitExceeded as exc:
        return _failure(
            "run",
            job_id,
            "MEMORY_LIMIT",
            exc.message,
            job_dir=str(job_dir),
            **exc.details,
        )
    except resource_guard.ProcessTimeout as exc:
        return _failure(
            "run",
            job_id,
            "TIMEOUT",
            exc.message,
            job_dir=str(job_dir),
            **exc.details,
        )
    except resource_guard.ResourceGuardError as exc:
        return _failure(
            "run",
            job_id,
            "EXECUTION_ERROR",
            f"Resource guard failed: {exc.message}",
            job_dir=str(job_dir),
            **exc.details,
        )
    except subprocess.TimeoutExpired:
        return _failure(
            "run",
            job_id,
            "TIMEOUT",
            f"Blender worker timed out after {timeout_seconds}s",
            job_dir=str(job_dir),
            timeout_seconds=timeout_seconds,
        )
    except OSError as exc:
        return _failure("run", job_id, "EXECUTION_ERROR", f"Cannot launch Blender worker: {exc}", job_dir=str(job_dir))

    try:
        worker_stdout.write_text(proc.stdout or "", encoding="utf-8")
        worker_stderr.write_text(proc.stderr or "", encoding="utf-8")
    except OSError:
        pass
    _control_phase(control, "worker_completed", plan=plan if owns_control else None, candidate=candidate if owns_control else None, attempt=attempt)
    if control.cancel_requested_now():
        return _cancelled_response(
            "run",
            job_id,
            control,
            "Cancelled before worker result acceptance",
            job_dir=str(job_dir),
        )

    if not res_file.is_file():
        return _failure(
            "run",
            job_id,
            "EXECUTION_ERROR",
            f"Worker exited ({proc.returncode}) without result file",
            job_dir=str(job_dir),
            stderr_tail=(proc.stderr or "")[-1500:],
        )

    try:
        worker_res = json.loads(res_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _failure(
            "run",
            job_id,
            "EXECUTION_ERROR",
            f"Worker result is not valid JSON: {exc}",
            job_dir=str(job_dir),
            stderr_tail=(proc.stderr or "")[-1500:],
        )
    if not isinstance(worker_res, dict):
        return _failure("run", job_id, "EXECUTION_ERROR", "Worker result must be a JSON object", job_dir=str(job_dir))
    if not worker_res.get("ok"):
        return protocol.CommandResponse(
            ok=False,
            command="run",
            job_id=job_id,
            error=worker_res.get("error", {"code": "EXECUTION_ERROR", "message": "Worker failed"}),
            data={"worker": worker_res, "job_dir": str(job_dir)},
        )

    try:
        deployment.import_addon_module("export_delivery").verify_export_result(
            worker_res.get("export"), normalized.get("export_format"), worker_req.get("export_path"))
    except (OSError, ValueError, TypeError) as exc:
        return _failure("run", job_id, "EXPORT_DELIVERY_INVALID", str(exc), job_dir=str(job_dir))

    if not output_blend.is_file():
        return _failure(
            "run",
            job_id,
            "EXECUTION_ERROR",
            f"Worker reported success but output_blend was not created: {output_blend}",
            job_dir=str(job_dir),
        )

    input_sha256_after = protocol.sha256_file(input_blend)
    if input_sha256_after != input_sha256:
        return _failure(
            "run",
            job_id,
            "INPUT_MUTATED",
            "input_blend changed during execution; refusing to sign the result",
            job_dir=str(job_dir),
            input_blend=str(input_blend),
            input_sha256_before=input_sha256,
            input_sha256_after=input_sha256_after,
        )

    parameters = normalized["parameters"]
    cold_verify_data = None
    if normalized.get("verify_cold_reopen"):
        if control.cancel_requested_now():
            return _cancelled_response(
                "run",
                job_id,
                control,
                "Cancelled before cold verification",
                job_dir=str(job_dir),
            )
        _control_phase(control, "verifier_starting", plan=plan if owns_control else None)
        evidence = worker_res.get("verification_evidence")
        if not isinstance(evidence, dict) or evidence.get("ok") is not True:
            return _failure(
                "run",
                job_id,
                "VERIFICATION_FAILED",
                "Missing production geometry evidence; refusing to run a cold verification",
                job_dir=str(job_dir),
                reason_code="MISSING_EXPECTED_EVIDENCE",
                evidence=evidence,
            )
        expected_frames = evidence.get("frames")
        if not isinstance(expected_frames, dict) or not expected_frames:
            return _failure(
                "run",
                job_id,
                "VERIFICATION_FAILED",
                "Production geometry evidence contains no frame summaries",
                job_dir=str(job_dir),
                reason_code="MISSING_EXPECTED_EVIDENCE",
            )
        expected_end = expected_frames.get(str(parameters["frame_end"]))
        if not isinstance(expected_end, dict):
            return _failure(
                "run",
                job_id,
                "VERIFICATION_FAILED",
                f"Production geometry evidence is missing end frame {parameters['frame_end']}",
                job_dir=str(job_dir),
                reason_code="MISSING_EXPECTED_EVIDENCE",
            )

        verify_spec = {
            "output_blend": str(output_blend),
            "source_blend": str(input_blend),
            "target_object": worker_res.get("target_object", normalized["target_object"]),
            "modifier_name": worker_res.get("modifier_name") or normalized["modifier_name"],
            "parameters_hash": normalized["parameters_hash"],
            "require_cache": bool(
                normalized.get("operation") in ("build", "update", "parameters", "adopt", "bake")
                or isinstance(worker_res.get("bake"), dict)
            ),
            "cache": {
                "required": bool(
                    normalized.get("operation") in ("build", "update", "parameters", "adopt", "bake")
                    or isinstance(worker_res.get("bake"), dict)
                ),
                "expected_directory": (
                    worker_res.get("bake", {}).get("bake_directory")
                    if isinstance(worker_res.get("bake"), dict)
                    else normalized.get("bake_directory")
                ),
                "baked": bool(
                    isinstance(worker_res.get("bake"), dict)
                    and worker_res["bake"].get("baked")
                ),
                "baked_ids": (
                    worker_res["bake"].get("baked_ids")
                    if isinstance(worker_res.get("bake"), dict)
                    else None
                ),
                "manifest": (
                    worker_res["bake"].get("manifest")
                    if isinstance(worker_res.get("bake"), dict)
                    else None
                ),
            },
            "frame_start": parameters["frame_start"],
            "frame_end": parameters["frame_end"],
            "expected_frames": expected_frames,
            "expected_end_summary": expected_end,
        }
        try:
            verify_spec_file.write_text(json.dumps(verify_spec, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            return _failure("run", job_id, "EXECUTION_ERROR", f"Cannot write verify spec: {exc}", job_dir=str(job_dir))

        verifier_script = Path(__file__).resolve().parent / "physics_verifier.py"
        _control_phase(
            control,
            "verifier_running",
            plan=plan if owns_control else None,
            candidate=candidate if owns_control else None,
            attempt=attempt,
            candidate_status="running",
        )
        try:
            v_proc = _run_blender_script(
                verifier_script,
                [verify_spec_file, verify_result_file],
                timeout_seconds=timeout_seconds,
                memory_limit_mb=memory_limit_mb,
                cancel_check=control.poll_cancel,
            )
            verifier_proc = v_proc
        except resource_guard.ProcessCancelled as exc:
            return _cancelled_response(
                "run",
                job_id,
                control,
                exc.message,
                job_dir=str(job_dir),
                phase="verifier",
                **exc.details,
            )
        except resource_guard.MemoryLimitExceeded as exc:
            return _failure(
                "run",
                job_id,
                "MEMORY_LIMIT",
                f"Cold verifier exceeded memory limit: {exc.message}",
                job_dir=str(job_dir),
                **exc.details,
            )
        except resource_guard.ProcessTimeout as exc:
            return _failure(
                "run",
                job_id,
                "VERIFICATION_TIMEOUT",
                exc.message,
                job_dir=str(job_dir),
                **exc.details,
            )
        except resource_guard.ResourceGuardError as exc:
            return _failure(
                "run",
                job_id,
                "EXECUTION_ERROR",
                f"Cold verifier resource guard failed: {exc.message}",
                job_dir=str(job_dir),
                **exc.details,
            )
        except subprocess.TimeoutExpired:
            return _failure(
                "run",
                job_id,
                "VERIFICATION_TIMEOUT",
                f"Cold verifier timed out after {timeout_seconds}s",
                job_dir=str(job_dir),
                timeout_seconds=timeout_seconds,
            )
        except OSError as exc:
            return _failure("run", job_id, "EXECUTION_ERROR", f"Cannot launch cold verifier: {exc}", job_dir=str(job_dir))

        try:
            verify_stdout.write_text(v_proc.stdout or "", encoding="utf-8")
            verify_stderr.write_text(v_proc.stderr or "", encoding="utf-8")
        except OSError:
            pass

        if not verify_result_file.is_file():
            return _failure(
                "run",
                job_id,
                "VERIFICATION_FAILED",
                f"Cold verifier crashed ({v_proc.returncode}) without result file",
                job_dir=str(job_dir),
                stderr_tail=(v_proc.stderr or "")[-1000:],
            )
        try:
            cold_verify_data = json.loads(verify_result_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _failure(
                "run",
                job_id,
                "VERIFICATION_FAILED",
                f"Cold verifier result is not valid JSON: {exc}",
                job_dir=str(job_dir),
                stderr_tail=(v_proc.stderr or "")[-1000:],
            )
        if not isinstance(cold_verify_data, dict) or cold_verify_data.get("passed") is not True:
            reason_code = (
                cold_verify_data.get("reason_code")
                if isinstance(cold_verify_data, dict)
                else "INVALID_VERIFIER_RESULT"
            )
            error_code = "CACHE_INVALID" if reason_code == "CACHE_INVALID" else "VERIFICATION_FAILED"
            return _failure(
                "run",
                job_id,
                error_code,
                "Cold reopen verification failed",
                job_dir=str(job_dir),
                reason_code=reason_code,
                worker=worker_res,
                cold_reopen_verify=cold_verify_data,
            )

    if verifier_proc is not None:
        _control_phase(
            control,
            "verifier_completed",
            plan=plan if owns_control else None,
            candidate=candidate if owns_control else None,
            attempt=attempt,
        )
    if control.cancel_requested_now():
        return _cancelled_response(
            "run",
            job_id,
            control,
            "Cancelled before signing the result",
            job_dir=str(job_dir),
        )
    _control_phase(control, "signing", plan=plan if owns_control else None)
    output_sha256 = protocol.sha256_file(output_blend)
    actual_normalized = _normalized_for_report(normalized, worker_res)
    summary = {
        "input_blend": str(input_blend),
        "input_sha256_before": input_sha256,
        "input_sha256_after": input_sha256_after,
        "input_unchanged": True,
        "output_blend": str(output_blend),
        "output_sha256": output_sha256,
        "allow_overwrite": allow_overwrite,
        "normalized_request": actual_normalized,
        "parameters_used": actual_normalized.get("parameters"),
        "resource_limits": _resource_summary(timeout_seconds, memory_limit_mb, worker_proc),
        "verification_resource_limits": (
            _resource_summary(timeout_seconds, memory_limit_mb, verifier_proc)
            if verifier_proc is not None
            else None
        ),
        "worker": worker_res,
        "cold_reopen_verify": cold_verify_data,
        "job_dir": str(job_dir),
        "resume_plan": str(resume_plan_file),
        "job_control": str(job_control.control_path(resume_plan_file)),
    }
    if control.cancel_requested_now():
        return _cancelled_response(
            "run",
            job_id,
            control,
            "Cancelled before the receipt was signed",
            job_dir=str(job_dir),
        )
    try:
        receipt_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        return _failure("run", job_id, "EXECUTION_ERROR", f"Cannot write receipt: {exc}", job_dir=str(job_dir))

    artifacts: list[str] = []
    _append_existing_artifact(artifacts, output_blend)
    if isinstance(worker_res.get("bake"), dict):
        _append_existing_artifact(artifacts, worker_res["bake"].get("bake_directory"))
    if isinstance(worker_res.get("export"), dict):
        _append_existing_artifact(artifacts, worker_res["export"].get("export_path"))
        _append_existing_artifact(artifacts, worker_res["export"].get("saved_path"))
        _append_existing_artifact(artifacts, (worker_res["export"].get("delivery_manifest") or {}).get("path"))
    _append_existing_artifact(artifacts, receipt_file)
    _append_existing_artifact(artifacts, resume_plan_file)
    _append_existing_artifact(artifacts, job_control.control_path(resume_plan_file))

    return protocol.CommandResponse(
        ok=True,
        command="run",
        job_id=job_id,
        data=summary,
        artifacts=artifacts,
    )


def _explicit_parameters(request: dict[str, Any], label: str) -> dict[str, Any]:
    nested = request.get("parameters")
    if nested is None:
        nested = {}
    if not isinstance(nested, dict):
        raise ValueError(f"{label}.parameters must be a JSON object")
    merged = copy.deepcopy(nested)
    for key in _TOP_LEVEL_PARAM_KEYS:
        if key in request and request[key] is not None and key not in merged:
            merged[key] = request[key]
    return merged


def _sweep_variant_request(
    base_request: dict[str, Any],
    variant: dict[str, Any],
    index: int,
    sweep_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not isinstance(variant, dict):
        raise ValueError(f"variants[{index}] must be a JSON object")
    if not isinstance(base_request, dict):
        raise ValueError("base_request must be a JSON object")

    request = copy.deepcopy(base_request)
    for key, value in variant.items():
        if key in ("parameters", "output_blend", "bake_directory", "export_path"):
            continue
        request[key] = copy.deepcopy(value)

    # Deterministic layer order:
    #   preset defaults < base top-level < base nested
    #                    < variant top-level < variant nested
    # Within any single request object, nested parameters win over the
    # convenience top-level aliases.  Removing the aliases afterwards prevents
    # contract.normalize_request from re-applying a stale base top-level value.
    base_parameters = _explicit_parameters(base_request, "base_request")
    variant_parameters = _explicit_parameters(variant, f"variants[{index}]")
    merged_parameters = copy.deepcopy(base_parameters)
    merged_parameters.update(variant_parameters)
    for key in _TOP_LEVEL_PARAM_KEYS:
        request.pop(key, None)
    request["parameters"] = merged_parameters

    provided_paths: dict[str, Any] = {}
    for key in ("output_blend", "bake_directory", "export_path"):
        value = variant.get(key, base_request.get(key))
        if value not in (None, ""):
            provided_paths[key] = str(value)
        request.pop(key, None)

    variant_dir = sweep_dir / f"variant_{index:02d}"
    used_paths: dict[str, Any] = {}
    request["output_blend"] = str(variant_dir / "output.blend")
    used_paths["output_blend"] = request["output_blend"]

    if bool(request.get("bake")) or str(request.get("operation", "build")) == "bake":
        request["bake_directory"] = str(variant_dir / "bake")
        used_paths["bake_directory"] = request["bake_directory"]

    export_format = str(request.get("export_format", "NONE"))
    if export_format != "NONE":
        extension = _EXPORT_EXTENSIONS.get(export_format, "")
        default_name = f"variant_{index:02d}{extension}"
        export_name = default_name
        provided_export = provided_paths.get("export_path")
        if provided_export:
            candidate_name = Path(str(provided_export)).name
            if candidate_name:
                if extension and not candidate_name.lower().endswith(extension):
                    export_name = f"{Path(candidate_name).stem or f'variant_{index:02d}'}{extension}"
                else:
                    export_name = candidate_name
        request["export_path"] = str(variant_dir / "export" / export_name)
        used_paths["export_path"] = request["export_path"]

    return request, used_paths, provided_paths


def run_sweep(
    raw_sweep: dict[str, Any],
    jobs_dir: Path,
    timeout_seconds: int = 300,
    memory_limit_mb: int | None = 4096,
) -> protocol.CommandResponse:
    sweep_id = protocol.new_job_id("sweep")
    lock_state: dict[str, Any] = {}
    response: protocol.CommandResponse | None = None
    try:
        try:
            response = _run_sweep_impl(
                raw_sweep,
                Path(jobs_dir),
                timeout_seconds,
                memory_limit_mb,
                sweep_id,
                _lock_state=lock_state,
            )
        except Exception as exc:
            response = _failure(
                "sweep",
                sweep_id,
                "EXECUTION_ERROR",
                f"Unhandled sweep error: {exc}",
                traceback=traceback.format_exc()[-4000:],
            )
        return response
    finally:
        control = lock_state.get("control")
        plan = lock_state.get("plan")
        _release_plan_lock(lock_state)
        if control is not None:
            error_code = ((response.error or {}) if response is not None else {}).get("code")
            if error_code == "CANCELLED" or control.cancel_seen:
                terminal = job_control.TERMINAL_CANCELLED
                reason = "cancel_request_observed_by_original_executor"
            elif response is not None and response.ok:
                terminal = job_control.TERMINAL_SUCCESS
                reason = "sweep_completed_successfully"
            else:
                terminal = job_control.TERMINAL_FAILED
                reason = error_code or "sweep_failed"
            control.finish_after_unlock(terminal, plan=plan, reason=reason)


def _run_sweep_impl(
    raw_sweep: dict[str, Any],
    jobs_dir: Path,
    timeout_seconds: int,
    memory_limit_mb: int | None,
    sweep_id: str,
    *,
    _lock_state: dict[str, Any] | None = None,
) -> protocol.CommandResponse:
    contract = deployment.import_addon_module("contract")
    timeout_error = _check_timeout(timeout_seconds)
    if timeout_error:
        return _failure("sweep", sweep_id, "VALIDATION_ERROR", timeout_error)
    memory_error = _check_memory_limit(memory_limit_mb)
    if memory_error:
        return _failure("sweep", sweep_id, "VALIDATION_ERROR", memory_error)
    if not isinstance(raw_sweep, dict):
        return _failure("sweep", sweep_id, "VALIDATION_ERROR", "Sweep request payload must be a JSON object")

    base_request = raw_sweep.get("base_request") or {}
    if not isinstance(base_request, dict):
        return _failure("sweep", sweep_id, "VALIDATION_ERROR", "base_request must be a JSON object")
    variants = raw_sweep.get("variants") or []
    if not isinstance(variants, list) or not variants:
        return _failure("sweep", sweep_id, "VALIDATION_ERROR", "sweep requires non-empty 'variants' list")

    try:
        jobs_root = path_guard.normalize_path(jobs_dir)
    except path_guard.PathGuardError as exc:
        return protocol.CommandResponse(ok=False, command="sweep", job_id=sweep_id, error=exc.to_error())
    sweep_dir = jobs_root / sweep_id
    try:
        sweep_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return _failure("sweep", sweep_id, "OUTPUT_EXISTS", f"Sweep directory already exists: {sweep_dir}")
    except OSError as exc:
        return _failure("sweep", sweep_id, "EXECUTION_ERROR", f"Cannot create sweep directory: {exc}")

    resume_plan_file = sweep_dir / resume.PLAN_FILENAME
    try:
        _acquire_plan_lock(_lock_state, resume_plan_file, f"sweep:{sweep_id}")
    except (resume.ResumeError, OSError, RuntimeError) as exc:
        return _failure(
            "sweep",
            sweep_id,
            "PLAN_LOCK_FAILED",
            f"Cannot own the sweep plan lock: {exc}",
            sweep_dir=str(sweep_dir),
            lock_path=str(resume.manifest_lock_path(resume_plan_file)),
        )

    prepared: list[dict[str, Any]] = []
    for index, variant in enumerate(variants):
        label = str(variant.get("label", f"variant_{index}")) if isinstance(variant, dict) else f"variant_{index}"
        try:
            request, used_paths, provided_paths = _sweep_variant_request(
                base_request, variant, index, sweep_dir
            )
        except (TypeError, ValueError) as exc:
            prepared.append(
                {
                    "index": index,
                    "label": label,
                    "request": None,
                    "used_paths": {},
                    "provided_paths": {},
                    "preview": None,
                    "preview_parameters": None,
                    "parameters_override": None,
                    "error": {"code": "VALIDATION_ERROR", "message": str(exc)},
                }
            )
            continue

        preview = None
        preview_parameters = None
        try:
            preview = contract.normalize_request(request)
            preview_parameters = preview.get("parameters")
        except Exception:
            pass
        prepared.append(
            {
                "index": index,
                "label": label,
                "request": request,
                "used_paths": used_paths,
                "provided_paths": provided_paths,
                "preview": preview,
                "preview_parameters": preview_parameters,
                "parameters_override": _explicit_parameters(variant, f"variants[{index}]"),
                "error": None,
            }
        )

    candidates: list[dict[str, Any]] = []
    for item in prepared:
        candidate = resume.make_candidate(
            f"variant_{item['index']:02d}",
            item["index"],
            item["label"],
            item["request"],
            normalized=item["preview"],
            error=item["error"],
        )
        if candidate.get("request") is None and item["error"] is None:
            item["error"] = candidate.get("last_error") or {
                "code": "VALIDATION_ERROR",
                "message": "Sweep request cannot be represented in the resume plan",
            }
        item["candidate"] = candidate
        candidates.append(candidate)

    try:
        plan = resume.create_plan(
            "sweep",
            raw_sweep,
            sweep_id,
            sweep_dir,
            sweep_dir,
            candidates,
            contract_id=contract.CONTRACT_ID,
        )
        resume.write_plan(resume_plan_file, plan)
    except (OSError, TypeError, ValueError, resume.ResumeError) as exc:
        return _failure(
            "sweep",
            sweep_id,
            "PLAN_WRITE_FAILED",
            f"Cannot create the persistent resume plan: {exc}",
            sweep_dir=str(sweep_dir),
            resume_plan=str(resume_plan_file),
        )

    control = job_control.JobControl(
        resume_plan_file,
        owner=f"sweep:{sweep_id}",
        plan=plan,
    )
    try:
        control.start()
    except job_control.JobControlError as exc:
        return protocol.CommandResponse(
            ok=False,
            command="sweep",
            job_id=sweep_id,
            data={"sweep_dir": str(sweep_dir), "resume_plan": str(resume_plan_file)},
            error=exc.to_error(),
        )
    if isinstance(_lock_state, dict):
        _lock_state["control"] = control
        _lock_state["plan"] = plan
    control.set_phase("prepared", plan=plan)

    runs: list[dict[str, Any]] = []
    all_ok = True
    cancelled = False
    for item in prepared:
        if control.cancel_requested_now():
            cancelled = True
            all_ok = False
            break
        index = item["index"]
        label = item["label"]
        request = item["request"]
        used_paths = item["used_paths"]
        provided_paths = item["provided_paths"]
        preview_parameters = item["preview_parameters"]
        parameters_override = item["parameters_override"]
        candidate = item["candidate"]
        if item["error"] is not None:
            all_ok = False
            runs.append(
                {
                    "index": index,
                    "label": label,
                    "ok": False,
                    "job_id": None,
                    "parameters_override": parameters_override,
                    "parameters_used": None,
                    "paths": {},
                    "path_overrides": {},
                    "error": item["error"],
                }
            )
            continue

        job_id = protocol.new_job_id("run")
        attempt = resume.begin_attempt(
            candidate,
            job_id,
            sweep_dir / job_id,
            used_paths,
            resume.request_sha256(request),
        )
        resume.write_plan(resume_plan_file, plan)
        control.set_phase("candidate_starting", plan=plan, candidate=candidate, attempt=attempt)
        try:
            result = run_single(
                request,
                sweep_dir,
                timeout_seconds=timeout_seconds,
                memory_limit_mb=memory_limit_mb,
                job_id=job_id,
                _control=control,
            )
        except Exception as exc:
            result = _failure(
                "run",
                job_id,
                "EXECUTION_ERROR",
                f"Unhandled variant error: {exc}",
                traceback=traceback.format_exc()[-4000:],
            )
            resume.complete_attempt(candidate, attempt, result)
            resume.write_plan(resume_plan_file, plan)
            control.refresh_plan(plan, phase="candidate_finished")
            all_ok = False
            runs.append(
                {
                    "index": index,
                    "label": label,
                    "ok": False,
                    "job_id": None,
                    "parameters_override": parameters_override,
                    "parameters_used": preview_parameters,
                    "paths": used_paths,
                    "path_overrides": {
                        key: {"provided": value, "used": used_paths.get(key)}
                        for key, value in provided_paths.items()
                    },
                    "error": {"code": "EXECUTION_ERROR", "message": str(exc)},
                }
            )
            continue

        resume.complete_attempt(candidate, attempt, result)
        resume.write_plan(resume_plan_file, plan)
        control.refresh_plan(plan, phase="candidate_finished")
        if not result.ok:
            all_ok = False
        worker_normalized = (
            result.data.get("worker", {}).get("normalized_request")
            if result.ok and isinstance(result.data.get("worker"), dict)
            else None
        )
        parameters_used = (
            worker_normalized.get("parameters")
            if isinstance(worker_normalized, dict)
            else preview_parameters
        )
        inspection = (
            result.data.get("worker", {}).get("inspection", {})
            if result.ok and isinstance(result.data.get("worker"), dict)
            else {}
        )
        runs.append(
            {
                "index": index,
                "label": label,
                "ok": result.ok,
                "job_id": result.job_id,
                "parameters_override": parameters_override,
                "parameters_used": parameters_used,
                "paths": used_paths,
                "path_overrides": {
                    key: {"provided": value, "used": used_paths.get(key)}
                    for key, value in provided_paths.items()
                },
                "centroid_displacement": inspection.get("centroid_displacement"),
                "late_frame_jitter": inspection.get("late_frame_jitter"),
                "resting_ratio": inspection.get("resting_ratio"),
                "output_blend": result.data.get("output_blend") if result.ok else None,
                "error": result.error,
            }
        )
        if ((result.error or {}).get("code") == "CANCELLED") or control.cancel_seen:
            cancelled = True
            break

    control.refresh_plan(plan, phase="finalizing")
    counts = job_control.counts_for_plan(plan)

    report = {
        "sweep_id": sweep_id,
        "total": len(prepared),
        "processed": len(runs),
        "pending": max(0, len(prepared) - len(runs)),
        "passed": sum(1 for run in runs if run["ok"]),
        "cancelled": cancelled,
        "counts": counts,
        "resource_limits": _resource_summary(timeout_seconds, memory_limit_mb),
        "resume_plan": str(resume_plan_file),
        "job_control": str(job_control.control_path(resume_plan_file)),
        "runs": runs,
    }
    report_path = sweep_dir / "sweep_report.json"
    try:
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        return _failure("sweep", sweep_id, "EXECUTION_ERROR", f"Cannot write sweep report: {exc}")
    try:
        plan["report"] = {
            "path": str(report_path),
            "sha256": protocol.sha256_file(report_path),
        }
        resume.write_plan(resume_plan_file, plan)
    except (OSError, ValueError, resume.ResumeError) as exc:
        return _failure(
            "sweep",
            sweep_id,
            "PLAN_WRITE_FAILED",
            f"Cannot finalize the persistent resume plan: {exc}",
            sweep_dir=str(sweep_dir),
            resume_plan=str(resume_plan_file),
        )
    return protocol.CommandResponse(
        ok=all_ok and not cancelled,
        command="sweep",
        job_id=sweep_id,
        data=report,
        artifacts=[str(report_path), str(resume_plan_file), str(job_control.control_path(resume_plan_file))],
        error=(
            {"code": "CANCELLED", "message": "Sweep cancelled by request; completed variants were preserved"}
            if cancelled
            else None
            if all_ok
            else {"code": "SWEEP_PARTIAL_FAILURE", "message": "One or more sweep variants failed"}
        ),
    )


def run_batch(
    raw_batch: dict[str, Any],
    jobs_dir: Path,
    timeout_seconds: int = 300,
    memory_limit_mb: int | None = 4096,
) -> protocol.CommandResponse:
    batch_id = protocol.new_job_id("batch")
    lock_state: dict[str, Any] = {}
    response: protocol.CommandResponse | None = None
    try:
        try:
            response = _run_batch_impl(
                raw_batch,
                Path(jobs_dir),
                timeout_seconds,
                memory_limit_mb,
                batch_id,
                _lock_state=lock_state,
            )
        except Exception as exc:
            response = _failure(
                "batch",
                batch_id,
                "EXECUTION_ERROR",
                f"Unhandled batch error: {exc}",
                traceback=traceback.format_exc()[-4000:],
            )
        return response
    finally:
        control = lock_state.get("control")
        plan = lock_state.get("plan")
        _release_plan_lock(lock_state)
        if control is not None:
            error_code = ((response.error or {}) if response is not None else {}).get("code")
            if error_code == "CANCELLED" or control.cancel_seen:
                terminal = job_control.TERMINAL_CANCELLED
                reason = "cancel_request_observed_by_original_executor"
            elif response is not None and response.ok:
                terminal = job_control.TERMINAL_SUCCESS
                reason = "batch_completed_successfully"
            else:
                terminal = job_control.TERMINAL_FAILED
                reason = error_code or "batch_failed"
            control.finish_after_unlock(terminal, plan=plan, reason=reason)


def _run_batch_impl(
    raw_batch: dict[str, Any],
    jobs_dir: Path,
    timeout_seconds: int,
    memory_limit_mb: int | None,
    batch_id: str,
    *,
    _lock_state: dict[str, Any] | None = None,
) -> protocol.CommandResponse:
    contract = deployment.import_addon_module("contract")
    timeout_error = _check_timeout(timeout_seconds)
    if timeout_error:
        return _failure("batch", batch_id, "VALIDATION_ERROR", timeout_error)
    memory_error = _check_memory_limit(memory_limit_mb)
    if memory_error:
        return _failure("batch", batch_id, "VALIDATION_ERROR", memory_error)
    if not isinstance(raw_batch, dict):
        return _failure("batch", batch_id, "VALIDATION_ERROR", "Batch request payload must be a JSON object")
    items = raw_batch.get("items") or []
    if not isinstance(items, list) or not items:
        return _failure("batch", batch_id, "VALIDATION_ERROR", "batch requires non-empty 'items' list")

    try:
        batch_dir = path_guard.normalize_path(jobs_dir) / batch_id
        batch_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return _failure("batch", batch_id, "OUTPUT_EXISTS", f"Batch directory already exists: {batch_dir}")
    except (path_guard.PathGuardError, OSError) as exc:
        return _failure("batch", batch_id, "EXECUTION_ERROR", f"Cannot create batch directory: {exc}")

    resume_plan_file = batch_dir / resume.PLAN_FILENAME
    try:
        _acquire_plan_lock(_lock_state, resume_plan_file, f"batch:{batch_id}")
    except (resume.ResumeError, OSError, RuntimeError) as exc:
        return _failure(
            "batch",
            batch_id,
            "PLAN_LOCK_FAILED",
            f"Cannot own the batch plan lock: {exc}",
            batch_dir=str(batch_dir),
            lock_path=str(resume.manifest_lock_path(resume_plan_file)),
        )

    prepared: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            prepared.append(
                {
                    "index": index,
                    "item": None,
                    "request": None,
                    "preview": None,
                    "target_object": None,
                    "preset_id": None,
                    "error": {"code": "VALIDATION_ERROR", "message": f"items[{index}] must be a JSON object"},
                }
            )
            continue
        preview = None
        try:
            preview = contract.normalize_request(item)
        except Exception:
            pass
        prepared.append(
            {
                "index": index,
                "item": item,
                "request": copy.deepcopy(item),
                "preview": preview,
                "target_object": item.get("target_object"),
                "preset_id": item.get("preset_id"),
                "error": None,
            }
        )

    candidates: list[dict[str, Any]] = []
    for item in prepared:
        candidate = resume.make_candidate(
            f"item_{item['index']:02d}",
            item["index"],
            f"item_{item['index']:02d}",
            item["request"],
            normalized=item["preview"],
            error=item["error"],
        )
        if candidate.get("request") is None and item["error"] is None:
            item["error"] = candidate.get("last_error") or {
                "code": "VALIDATION_ERROR",
                "message": "Batch item cannot be represented in the resume plan",
            }
        item["candidate"] = candidate
        candidates.append(candidate)

    try:
        plan = resume.create_plan(
            "batch",
            raw_batch,
            batch_id,
            batch_dir,
            batch_dir,
            candidates,
            contract_id=contract.CONTRACT_ID,
        )
        resume.write_plan(resume_plan_file, plan)
    except (OSError, TypeError, ValueError, resume.ResumeError) as exc:
        return _failure(
            "batch",
            batch_id,
            "PLAN_WRITE_FAILED",
            f"Cannot create the persistent resume plan: {exc}",
            batch_dir=str(batch_dir),
            resume_plan=str(resume_plan_file),
        )

    control = job_control.JobControl(
        resume_plan_file,
        owner=f"batch:{batch_id}",
        plan=plan,
    )
    try:
        control.start()
    except job_control.JobControlError as exc:
        return protocol.CommandResponse(
            ok=False,
            command="batch",
            job_id=batch_id,
            data={"batch_dir": str(batch_dir), "resume_plan": str(resume_plan_file)},
            error=exc.to_error(),
        )
    if isinstance(_lock_state, dict):
        _lock_state["control"] = control
        _lock_state["plan"] = plan
    control.set_phase("prepared", plan=plan)

    results: list[dict[str, Any]] = []
    all_ok = True
    cancelled = False
    for item in prepared:
        if control.cancel_requested_now():
            cancelled = True
            all_ok = False
            break
        index = item["index"]
        request = item["request"]
        candidate = item["candidate"]
        if item["error"] is not None:
            all_ok = False
            results.append(
                {
                    "index": index,
                    "ok": False,
                    "job_id": None,
                    "target_object": item["target_object"],
                    "preset_id": item["preset_id"],
                    "error": item["error"],
                    "output_blend": None,
                }
            )
            continue

        job_id = protocol.new_job_id("run")
        attempt = resume.begin_attempt(
            candidate,
            job_id,
            batch_dir / job_id,
            {
                "output_blend": request.get("output_blend"),
                "bake_directory": request.get("bake_directory"),
                "export_path": request.get("export_path"),
            },
            resume.request_sha256(request),
        )
        resume.write_plan(resume_plan_file, plan)
        control.set_phase("candidate_starting", plan=plan, candidate=candidate, attempt=attempt)
        try:
            result = run_single(
                request,
                batch_dir,
                timeout_seconds=timeout_seconds,
                memory_limit_mb=memory_limit_mb,
                job_id=job_id,
                _control=control,
            )
        except Exception as exc:
            result = _failure(
                "run",
                job_id,
                "EXECUTION_ERROR",
                f"Unhandled batch item error: {exc}",
                traceback=traceback.format_exc()[-4000:],
            )
            resume.complete_attempt(candidate, attempt, result)
            resume.write_plan(resume_plan_file, plan)
            control.refresh_plan(plan, phase="candidate_finished")
            all_ok = False
            results.append(
                {
                    "index": index,
                    "ok": False,
                    "job_id": None,
                    "target_object": item["target_object"],
                    "preset_id": item["preset_id"],
                    "error": {"code": "EXECUTION_ERROR", "message": str(exc)},
                    "output_blend": None,
                }
            )
            continue
        resume.complete_attempt(candidate, attempt, result)
        resume.write_plan(resume_plan_file, plan)
        control.refresh_plan(plan, phase="candidate_finished")
        if not result.ok:
            all_ok = False
        results.append(
            {
                "index": index,
                "ok": result.ok,
                "job_id": result.job_id,
                "target_object": item["target_object"],
                "preset_id": item["preset_id"],
                "error": result.error,
                "output_blend": result.data.get("output_blend") if result.ok else None,
                "parameters_used": result.data.get("parameters_used") if result.ok else None,
            }
        )
        if ((result.error or {}).get("code") == "CANCELLED") or control.cancel_seen:
            cancelled = True
            break

    control.refresh_plan(plan, phase="finalizing")
    counts = job_control.counts_for_plan(plan)

    report = {
        "batch_id": batch_id,
        "total": len(prepared),
        "processed": len(results),
        "pending": max(0, len(prepared) - len(results)),
        "passed": sum(1 for result in results if result["ok"]),
        "cancelled": cancelled,
        "counts": counts,
        "resource_limits": _resource_summary(timeout_seconds, memory_limit_mb),
        "resume_plan": str(resume_plan_file),
        "job_control": str(job_control.control_path(resume_plan_file)),
        "items": results,
    }
    report_path = batch_dir / "batch_report.json"
    try:
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        return _failure("batch", batch_id, "EXECUTION_ERROR", f"Cannot write batch report: {exc}")
    try:
        plan["report"] = {
            "path": str(report_path),
            "sha256": protocol.sha256_file(report_path),
        }
        resume.write_plan(resume_plan_file, plan)
    except (OSError, ValueError, resume.ResumeError) as exc:
        return _failure(
            "batch",
            batch_id,
            "PLAN_WRITE_FAILED",
            f"Cannot finalize the persistent resume plan: {exc}",
            batch_dir=str(batch_dir),
            resume_plan=str(resume_plan_file),
        )
    return protocol.CommandResponse(
        ok=all_ok and not cancelled,
        command="batch",
        job_id=batch_id,
        data=report,
        artifacts=[str(report_path), str(resume_plan_file), str(job_control.control_path(resume_plan_file))],
        error=(
            {"code": "CANCELLED", "message": "Batch cancelled by request; completed items were preserved"}
            if cancelled
            else None
            if all_ok
            else {"code": "BATCH_PARTIAL_FAILURE", "message": "One or more batch items failed"}
        ),
    )
