"""Persistent workflow plans and candidate-level resume support.

The plan is a small, versioned JSON sidecar next to an existing run, sweep or
batch output.  It intentionally stores references and hashes rather than
copying receipts or cache inventories: success verification re-reads the
original receipt and the cache module's existing SHA inventory.

Resume replays a complete candidate from its original input; it never claims
to continue a simulation from an arbitrary intermediate frame.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import traceback
from typing import Any
import uuid

from . import deployment
from . import job_control
from . import path_guard
from . import protocol


PLAN_FILENAME = "resume_plan.json"
PLAN_SCHEMA = "pnw.workflow.resume_plan"
PLAN_VERSION = 1
PLAN_COMMANDS = ("run", "sweep", "batch")

CACHE_MANIFEST_FILENAME = "physics-cache-manifest.json"
CACHE_CURRENT_FILENAME = "physics-cache-current.json"
CACHE_SCHEMA_VERSION = "2.0"
CACHE_KIND = "pnw_cache_generation"
CURRENT_KIND = "pnw_cache_current"

SUCCESS = "success"
FAILED = "failed"
PENDING = "pending"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPORT_EXTENSIONS = {"STATIC_MESH": ".blend", "ALEMBIC": ".abc", "OPENVDB": ".vdb"}

# Parent integration target.  ``capabilities.describe_capabilities()`` provides
# ``physics_evidence_schema`` after the add-on merge; an older capability module
# intentionally falls back to v2 so a v1 cold receipt cannot be skipped.
FALLBACK_EVIDENCE_SCHEMA = "physics-geometry-evidence-v2"
_EVIDENCE_SCHEMA_KEYS = ("evidence_schema", "schema", "geometry_evidence_schema")

# A reader on Windows can briefly deny MoveFileEx/os.replace with WinError 5.
# Keep the replacement atomic and bounded; never truncate the destination or
# steal another process's lock.
_WINDOWS_REPLACE_RETRY_SECONDS = 2.0
_WINDOWS_REPLACE_RETRY_MIN_DELAY = 0.01
_WINDOWS_REPLACE_RETRY_MAX_DELAY = 0.2
_WINDOWS_REPLACE_RETRY_WINERRORS = {5, 32, 33}


class ResumeError(RuntimeError):
    """A structured plan/lock/resume failure."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_error(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return error


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    # allow_nan mirrors Python's JSON loader so an invalid-but-loadable request
    # can still be recorded and rejected by the contract on replay.
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=True,
    )


def sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def request_sha256(request: Any) -> str:
    return sha256_text(canonical_json(request))


def manifest_lock_path(manifest_path: str | os.PathLike[str]) -> Path:
    path = path_guard.normalize_path(manifest_path)
    return path.with_name(path.name + ".lock")


def _atomic_write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_temp_atomically(Path(temporary), path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _replace_temp_atomically(temporary: Path, path: Path) -> None:
    """Replace ``path`` without ever writing it in place.

    On Windows, an independent reader can hold a share mode that blocks
    ``os.replace`` with WinError 5/32/33 even though the destination is a valid
    file.  Retry only that atomic replacement for a short bounded interval.
    The caller's private temp file is preserved for its own cleanup on failure.
    """

    if os.name != "nt":
        os.replace(temporary, path)
        return
    deadline = time.monotonic() + _WINDOWS_REPLACE_RETRY_SECONDS
    delay = _WINDOWS_REPLACE_RETRY_MIN_DELAY
    while True:
        try:
            os.replace(temporary, path)
            return
        except OSError as exc:
            if (
                getattr(exc, "winerror", None) not in _WINDOWS_REPLACE_RETRY_WINERRORS
                or path.is_dir()
            ):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if hasattr(exc, "add_note"):
                    exc.add_note(
                        "atomic replace remained blocked for "
                        f"{_WINDOWS_REPLACE_RETRY_SECONDS:.1f}s; temp file will be cleaned by caller"
                    )
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2.0, _WINDOWS_REPLACE_RETRY_MAX_DELAY)


def _plan_checksum(plan: dict[str, Any]) -> str:
    data = copy.deepcopy(plan)
    data.pop("manifest_sha256", None)
    return sha256_text(canonical_json(data))


def sign_plan(plan: dict[str, Any]) -> dict[str, Any]:
    signed = copy.deepcopy(plan)
    signed["updated_utc"] = utc_now()
    signed.pop("manifest_sha256", None)
    signed["manifest_sha256"] = _plan_checksum(signed)
    return signed


def write_plan(plan_path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Atomically publish one complete plan revision."""

    signed = sign_plan(plan)
    _atomic_write_json(Path(plan_path), signed)
    # Keep the caller's candidate objects live.  Replacing the list with deep
    # copies would detach later begin/complete_attempt mutations from the plan.
    plan["updated_utc"] = signed["updated_utc"]
    plan["manifest_sha256"] = signed["manifest_sha256"]
    return signed


def read_plan(plan_path: Path) -> dict[str, Any]:
    path = Path(plan_path)
    if not path.is_file():
        raise ResumeError("MANIFEST_NOT_FOUND", f"Resume manifest not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ResumeError("MANIFEST_UNREADABLE", f"Cannot read resume manifest: {exc}", manifest=str(path)) from exc
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResumeError("MANIFEST_INTEGRITY_FAILED", f"Resume manifest is not valid JSON: {exc}") from exc
    if not isinstance(plan, dict):
        raise ResumeError("MANIFEST_INTEGRITY_FAILED", "Resume manifest must be a JSON object")
    if plan.get("plan_schema") != PLAN_SCHEMA:
        raise ResumeError("MANIFEST_INTEGRITY_FAILED", "Unsupported resume manifest schema")
    if plan.get("plan_version") != PLAN_VERSION:
        raise ResumeError(
            "UNSUPPORTED_PLAN_VERSION",
            f"Unsupported resume manifest version: {plan.get('plan_version')!r}",
            supported=PLAN_VERSION,
        )
    if plan.get("command") not in PLAN_COMMANDS:
        raise ResumeError("MANIFEST_INTEGRITY_FAILED", "Resume manifest has an unsupported command")
    if not isinstance(plan.get("candidates"), list) or not plan["candidates"]:
        raise ResumeError("MANIFEST_INTEGRITY_FAILED", "Resume manifest has no candidates")
    checksum = plan.get("manifest_sha256")
    if not isinstance(checksum, str) or not _SHA256_RE.fullmatch(checksum):
        raise ResumeError("MANIFEST_INTEGRITY_FAILED", "Resume manifest checksum is missing or invalid")
    if checksum != _plan_checksum(plan):
        raise ResumeError(
            "MANIFEST_INTEGRITY_FAILED",
            "Resume manifest checksum does not match its contents",
            manifest=str(path),
        )
    return plan


def _normalized_candidate_fields(normalized: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(normalized, dict):
        return {
            "operation": None,
            "parameters_hash": None,
            "frame_start": None,
            "frame_end": None,
            "cache_expected": False,
            "verify_cold_reopen": False,
            "export_format": "NONE",
        }
    operation = normalized.get("operation")
    parameters = normalized.get("parameters") if isinstance(normalized.get("parameters"), dict) else {}
    cache_expected = bool(
        operation in ("build", "update", "parameters", "adopt", "bake")
        and (normalized.get("bake") or operation == "bake")
    )
    return {
        "operation": operation,
        "parameters_hash": normalized.get("parameters_hash"),
        "frame_start": parameters.get("frame_start"),
        "frame_end": parameters.get("frame_end"),
        "cache_expected": cache_expected,
        "verify_cold_reopen": bool(normalized.get("verify_cold_reopen")),
        "export_format": normalized.get("export_format", "NONE"),
    }


def _input_identity(request: dict[str, Any] | None) -> tuple[str | None, str | None, dict[str, Any] | None]:
    if not isinstance(request, dict) or not request.get("input_blend"):
        return None, None, {"code": "VALIDATION_ERROR", "message": "input_blend path is required"}
    try:
        input_blend = path_guard.normalize_path(request["input_blend"])
    except path_guard.PathGuardError as exc:
        return None, None, exc.to_error()
    if not input_blend.is_file():
        return str(input_blend), None, {
            "code": "SCENE_STATE_ERROR",
            "message": f"input_blend not found: {input_blend}",
        }
    try:
        digest = protocol.sha256_file(input_blend)
    except OSError as exc:
        return str(input_blend), None, {
            "code": "INPUT_UNREADABLE",
            "message": f"Cannot hash input_blend: {exc}",
        }
    return str(input_blend), digest, None


def make_candidate(
    candidate_id: str,
    index: int,
    label: str,
    request: dict[str, Any] | None,
    *,
    normalized: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    input_identity: tuple[str | None, str | None, dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Build one candidate record, including its immutable input identity."""

    stored_request: dict[str, Any] | None = None
    if isinstance(request, dict):
        try:
            stored_request = json.loads(json.dumps(request, ensure_ascii=False, allow_nan=True))
        except (TypeError, ValueError) as exc:
            if error is None:
                error = {
                    "code": "VALIDATION_ERROR",
                    "message": f"Request is not JSON-serializable: {exc}",
                }
    elif request is not None and error is None:
        error = {"code": "VALIDATION_ERROR", "message": "Candidate request must be a JSON object"}

    input_blend = None
    input_sha256 = None
    input_error = None
    if stored_request is not None:
        if input_identity is None:
            input_blend, input_sha256, input_error = _input_identity(stored_request)
        else:
            input_blend, input_sha256, input_error = input_identity

    status = FAILED if error is not None else PENDING
    candidate = {
        "candidate_id": candidate_id,
        "index": index,
        "label": label,
        "request": stored_request,
        "request_sha256": request_sha256(stored_request) if stored_request is not None else None,
        "input_blend": input_blend,
        "input_sha256": input_sha256,
        "normalized": _normalized_candidate_fields(normalized),
        "status": status,
        "attempts": [],
        "success_attempt": None,
        "last_error": error or input_error,
        "created_utc": utc_now(),
        "updated_utc": utc_now(),
    }
    return candidate


def create_plan(
    command: str,
    raw_request: dict[str, Any],
    plan_id: str,
    jobs_dir: Path,
    plan_dir: Path,
    candidates: list[dict[str, Any]],
    *,
    contract_id: str | None = None,
) -> dict[str, Any]:
    if command not in PLAN_COMMANDS:
        raise ResumeError("VALIDATION_ERROR", f"Unsupported plan command: {command}")
    if not isinstance(raw_request, dict):
        raise ResumeError("VALIDATION_ERROR", "Plan request must be a JSON object")
    if not candidates:
        raise ResumeError("VALIDATION_ERROR", "A resume plan needs at least one candidate")

    inputs: dict[str, str] = {}
    for candidate in candidates:
        path = candidate.get("input_blend")
        digest = candidate.get("input_sha256")
        if isinstance(path, str) and isinstance(digest, str):
            if path in inputs and inputs[path] != digest:
                raise ResumeError(
                    "PLAN_INPUT_CHANGED",
                    "One input changed while the plan candidates were being prepared",
                    input_blend=path,
                    first_sha256=inputs[path],
                    later_sha256=digest,
                )
            inputs[path] = digest

    stored_request = json.loads(json.dumps(raw_request, ensure_ascii=False, allow_nan=True))
    now = utc_now()
    plan = {
        "plan_schema": PLAN_SCHEMA,
        "plan_version": PLAN_VERSION,
        "plan_id": plan_id,
        "command": command,
        "created_utc": now,
        "updated_utc": now,
        "jobs_dir": str(path_guard.normalize_path(jobs_dir)),
        "plan_dir": str(path_guard.normalize_path(plan_dir)),
        "contract_id": contract_id,
        "request": stored_request,
        "request_sha256": request_sha256(stored_request),
        "execution_mode": "candidate_replay",
        "intermediate_frame_resume": False,
        "inputs": [
            {"path": path, "sha256": digest}
            for path, digest in sorted(inputs.items(), key=lambda item: item[0])
        ],
        "candidates": candidates,
        "resume_runs": [],
    }
    plan["manifest_sha256"] = _plan_checksum(plan)
    return plan


def _assert_same_file_sha(path_value: str, expected_sha: str) -> str | None:
    try:
        path = path_guard.normalize_path(path_value)
    except path_guard.PathGuardError as exc:
        return exc.code
    if not path.is_file():
        return "missing"
    try:
        actual = protocol.sha256_file(path)
    except OSError:
        return "unreadable"
    return None if actual == expected_sha else "sha256_mismatch"


def validate_plan_identity(plan: dict[str, Any]) -> None:
    """Reject input/request changes before any candidate is replayed."""

    contract = deployment.import_addon_module("contract")
    jobs_dir = path_guard.normalize_path(plan.get("jobs_dir"))
    plan_dir = path_guard.normalize_path(plan.get("plan_dir"))
    if not plan_dir.is_dir():
        raise ResumeError(
            "PLAN_LOCATION_INVALID",
            f"Recorded plan directory is missing: {plan_dir}",
            plan_dir=str(plan_dir),
        )
    if not jobs_dir.is_dir():
        raise ResumeError(
            "PLAN_LOCATION_INVALID",
            f"Recorded jobs directory is missing: {jobs_dir}",
            jobs_dir=str(jobs_dir),
        )

    try:
        recorded_request_sha = request_sha256(plan.get("request"))
    except (TypeError, ValueError) as exc:
        raise ResumeError("MANIFEST_INTEGRITY_FAILED", f"Recorded request is not JSON-safe: {exc}") from exc
    if recorded_request_sha != plan.get("request_sha256"):
        raise ResumeError(
            "PLAN_REQUEST_CHANGED",
            "Recorded plan request no longer matches its checksum",
        )

    for entry in plan.get("inputs") or []:
        if not isinstance(entry, dict):
            raise ResumeError("MANIFEST_INTEGRITY_FAILED", "Plan input inventory is malformed")
        path_value = entry.get("path")
        expected = entry.get("sha256")
        if not isinstance(path_value, str) or not isinstance(expected, str):
            raise ResumeError("MANIFEST_INTEGRITY_FAILED", "Plan input inventory is malformed")
        try:
            path = path_guard.normalize_path(path_value)
        except path_guard.PathGuardError as exc:
            raise ResumeError("PLAN_INPUT_CHANGED", exc.message, input_blend=path_value) from exc
        if not path.is_file():
            raise ResumeError(
                "PLAN_INPUT_CHANGED",
                f"Recorded input_blend is missing: {path}",
                input_blend=str(path),
            )
        actual = protocol.sha256_file(path)
        if actual != expected:
            raise ResumeError(
                "PLAN_INPUT_CHANGED",
                "input_blend changed since the plan was created; refusing silent takeover",
                input_blend=str(path),
                expected_sha256=expected,
                actual_sha256=actual,
            )

    for candidate in plan["candidates"]:
        if not isinstance(candidate, dict):
            raise ResumeError("MANIFEST_INTEGRITY_FAILED", "Plan candidate is not an object")
        request = candidate.get("request")
        if request is None:
            continue
        if not isinstance(request, dict):
            raise ResumeError("MANIFEST_INTEGRITY_FAILED", "Candidate request is malformed")
        if not isinstance(candidate.get("normalized"), dict):
            raise ResumeError(
                "MANIFEST_INTEGRITY_FAILED",
                "Candidate normalized summary is missing",
                candidate_id=candidate.get("candidate_id"),
            )
        try:
            actual_request_sha = request_sha256(request)
        except (TypeError, ValueError) as exc:
            raise ResumeError("MANIFEST_INTEGRITY_FAILED", f"Candidate request is not JSON-safe: {exc}") from exc
        if actual_request_sha != candidate.get("request_sha256"):
            raise ResumeError(
                "PLAN_REQUEST_CHANGED",
                "Candidate request changed since the plan was created",
                candidate_id=candidate.get("candidate_id"),
            )
        normalized_summary = candidate.get("normalized") or {}
        recorded_parameters_hash = normalized_summary.get("parameters_hash")
        if isinstance(recorded_parameters_hash, str):
            try:
                normalized_now = contract.normalize_request(request)
            except Exception as exc:
                raise ResumeError(
                    "PLAN_REQUEST_CHANGED",
                    f"Candidate request no longer normalizes: {getattr(exc, 'message', str(exc))}",
                    candidate_id=candidate.get("candidate_id"),
                    contract_code=getattr(exc, "code", None),
                ) from exc
            if normalized_now.get("parameters_hash") != recorded_parameters_hash:
                raise ResumeError(
                    "PLAN_PARAMETERS_CHANGED",
                    "Normalized parameters changed since the plan was created; refusing silent takeover",
                    candidate_id=candidate.get("candidate_id"),
                    expected_parameters_hash=recorded_parameters_hash,
                    actual_parameters_hash=normalized_now.get("parameters_hash"),
                )
        input_path = candidate.get("input_blend")
        input_sha = candidate.get("input_sha256")
        if input_path is not None:
            if not isinstance(input_path, str) or not isinstance(input_sha, str):
                raise ResumeError(
                    "PLAN_INPUT_CHANGED",
                    "Candidate input identity is incomplete",
                    candidate_id=candidate.get("candidate_id"),
                )
            try:
                current_path = path_guard.normalize_path(request.get("input_blend"))
            except path_guard.PathGuardError as exc:
                raise ResumeError(
                    "PLAN_REQUEST_CHANGED",
                    "Candidate input path is no longer valid",
                    candidate_id=candidate.get("candidate_id"),
                ) from exc
            if str(current_path) != input_path:
                raise ResumeError(
                    "PLAN_REQUEST_CHANGED",
                    "Candidate input path changed since the plan was created",
                    candidate_id=candidate.get("candidate_id"),
                    expected=input_path,
                    actual=str(current_path),
                )
            if not current_path.is_file():
                raise ResumeError(
                    "PLAN_INPUT_CHANGED",
                    f"Recorded candidate input is missing: {current_path}",
                    candidate_id=candidate.get("candidate_id"),
                )
            actual = protocol.sha256_file(current_path)
            if actual != input_sha:
                raise ResumeError(
                    "PLAN_INPUT_CHANGED",
                    "Candidate input_blend changed since the plan was created",
                    candidate_id=candidate.get("candidate_id"),
                    input_blend=str(current_path),
                    expected_sha256=input_sha,
                    actual_sha256=actual,
                )


def begin_attempt(
    candidate: dict[str, Any],
    job_id: str,
    job_dir: Path,
    used_paths: dict[str, Any],
    request_sha: str | None,
) -> dict[str, Any]:
    attempt_no = len(candidate.get("attempts") or []) + 1
    attempt = {
        "attempt": attempt_no,
        "job_id": job_id,
        "job_dir": str(path_guard.normalize_path(job_dir)),
        "status": PENDING,
        "started_utc": utc_now(),
        "finished_utc": None,
        "request_sha256": request_sha,
        "used_paths": {
            key: str(value)
            for key, value in (used_paths or {}).items()
            if value not in (None, "")
        },
        "receipt": None,
        "output": None,
        "exports": [],
        "cache": None,
        "cold_reopen": None,
        "error": None,
    }
    candidate.setdefault("attempts", []).append(attempt)
    candidate["status"] = PENDING
    candidate["last_error"] = None
    candidate["updated_utc"] = utc_now()
    return attempt


def _safe_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _record_success_evidence(
    candidate: dict[str, Any],
    attempt: dict[str, Any],
    response: protocol.CommandResponse,
) -> None:
    data = response.data if isinstance(response.data, dict) else {}
    job_dir = Path(attempt["job_dir"])
    receipt_path = job_dir / "receipt.json"
    if not receipt_path.is_file():
        raise ResumeError(
            "RECEIPT_MISSING",
            f"Successful run has no receipt: {receipt_path}",
        )

    output_value = data.get("output_blend") or (attempt.get("used_paths") or {}).get("output_blend")
    if not output_value:
        raise ResumeError("ARTIFACT_MISSING", "Successful run did not report output_blend")
    output_path = path_guard.normalize_path(output_value)
    if not output_path.is_file() or output_path.is_symlink():
        raise ResumeError("ARTIFACT_MISSING", f"Successful output_blend is missing: {output_path}")
    output_sha = protocol.sha256_file(output_path)
    reported_output_sha = data.get("output_sha256")
    if isinstance(reported_output_sha, str) and reported_output_sha != output_sha:
        raise ResumeError(
            "ARTIFACT_MISMATCH",
            "output_blend no longer matches the reported SHA-256",
            output_blend=str(output_path),
            reported_sha256=reported_output_sha,
            actual_sha256=output_sha,
        )

    worker = data.get("worker") if isinstance(data.get("worker"), dict) else {}
    normalized = candidate.get("normalized") or {}
    exports: list[dict[str, Any]] = []
    export_info = worker.get("export") if isinstance(worker.get("export"), dict) else None
    if export_info:
        seen: set[str] = set()
        for kind, key in (
            ("static_blend", "saved_path"),
            (str(normalized.get("export_format", "export")).lower(), "export_path"),
        ):
            value = export_info.get(key)
            if not value:
                continue
            export_path = path_guard.normalize_path(value)
            key_text = str(export_path)
            if key_text in seen:
                continue
            seen.add(key_text)
            if export_path.is_file() and not export_path.is_symlink():
                exports.append(
                    {
                        "kind": kind,
                        "path": key_text,
                        "sha256": protocol.sha256_file(export_path),
                        "size": export_path.stat().st_size,
                    }
                )
        if normalized.get("export_format") not in (None, "NONE"):
            requested_path = (candidate.get("request") or {}).get("export_path")
            if requested_path and not exports:
                raise ResumeError(
                    "ARTIFACT_MISSING",
                    "Requested export was not produced",
                )

    export_format = normalized.get("export_format")
    primary = (attempt.get("used_paths") or {}).get("export_path") or (candidate.get("request") or {}).get("export_path")
    try:
        delivery = deployment.import_addon_module("export_delivery").verify_export_result(export_info, export_format, primary)
    except (OSError, ValueError, TypeError) as exc:
        raise ResumeError("EXPORT_DELIVERY_INVALID", str(exc)) from exc
    attempt["delivery_manifest"] = copy.deepcopy(export_info.get("delivery_manifest")) if delivery else None

    cache_record = None
    bake = worker.get("bake") if isinstance(worker.get("bake"), dict) else None
    if bake and bake.get("baked"):
        generation_value = bake.get("bake_directory")
        if not generation_value:
            raise ResumeError("CACHE_INVALID", "Successful bake did not report its generation directory")
        generation = path_guard.normalize_path(generation_value)
        manifest_path = generation / CACHE_MANIFEST_FILENAME
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise ResumeError("CACHE_INVALID", f"Cache generation manifest is missing: {manifest_path}")
        manifest = _safe_json(manifest_path)
        if not isinstance(manifest, dict):
            raise ResumeError("CACHE_INVALID", f"Cache generation manifest is not valid JSON: {manifest_path}")
        cache_record = {
            "generation_dir": str(generation),
            "owner_root": str(path_guard.normalize_path(bake["owner_root"])) if bake.get("owner_root") else None,
            "manifest": str(manifest_path),
            "manifest_file_sha256": protocol.sha256_file(manifest_path),
            "manifest_sha256": manifest.get("manifest_sha256"),
            "identity_hash": manifest.get("identity_hash"),
            "generation": manifest.get("generation"),
            "files_verified": bake.get("files_verified"),
            "frame_start": normalized.get("frame_start"),
            "frame_end": normalized.get("frame_end"),
        }
        if bake.get("manifest_sha256") not in (None, cache_record["manifest_sha256"]):
            raise ResumeError("CACHE_INVALID", "Worker receipt and cache manifest disagree on manifest_sha256")
    elif normalized.get("cache_expected"):
        raise ResumeError("CACHE_INVALID", "Request expected a disk bake but the worker reported no cache")

    cold_reopen = data.get("cold_reopen_verify") if isinstance(data.get("cold_reopen_verify"), dict) else None
    if normalized.get("verify_cold_reopen") and not (cold_reopen and cold_reopen.get("passed") is True):
        raise ResumeError("VERIFICATION_FAILED", "Cold reopen verification did not pass")
    cold_schema = _evidence_schema_from_mapping(cold_reopen)
    if normalized.get("verify_cold_reopen") and cold_schema != current_evidence_schema():
        raise ResumeError(
            "VERIFICATION_FAILED",
            "Cold verifier itself must report the current output evidence schema",
            cold_reopen_schema=cold_schema,
            required_evidence_schema=current_evidence_schema(),
        )
    worker_schema = _evidence_schema_from_mapping(worker.get("verification_evidence"))
    schemas = [schema for schema in (cold_schema, worker_schema) if schema is not None]
    if len(set(schemas)) > 1:
        raise ResumeError(
            "VERIFICATION_FAILED",
            "Cold verifier and worker evidence disagree on the geometry evidence schema",
            cold_reopen_schema=cold_schema,
            worker_evidence_schema=worker_schema,
        )
    evidence_schema = schemas[0] if schemas else None

    receipt_sha = protocol.sha256_file(receipt_path)
    attempt["receipt"] = {"path": str(receipt_path), "sha256": receipt_sha}
    attempt["evidence_schema"] = evidence_schema
    attempt["output"] = {
        "path": str(output_path),
        "sha256": output_sha,
        "size": output_path.stat().st_size,
    }
    attempt["exports"] = exports
    attempt["cache"] = cache_record
    used_paths = attempt.setdefault("used_paths", {})
    used_paths["output_blend"] = str(output_path)
    if cache_record is not None:
        used_paths["bake_directory"] = cache_record["generation_dir"]
    if exports:
        used_paths["export_path"] = exports[0]["path"]
    attempt["cold_reopen"] = {
        "required": bool(normalized.get("verify_cold_reopen")),
        "passed": bool(cold_reopen and cold_reopen.get("passed") is True),
        "reason_code": cold_reopen.get("reason_code") if isinstance(cold_reopen, dict) else None,
        "evidence_schema": evidence_schema,
    }


def complete_attempt(
    candidate: dict[str, Any],
    attempt: dict[str, Any],
    response: protocol.CommandResponse,
) -> None:
    """Atomically mark one attempt and its owning candidate terminal in memory."""

    attempt["finished_utc"] = utc_now()
    if response.ok:
        try:
            _record_success_evidence(candidate, attempt, response)
        except ResumeError as exc:
            attempt["status"] = FAILED
            attempt["error"] = exc.to_error()
            candidate["status"] = FAILED
            candidate["last_error"] = exc.to_error()
            candidate["success_attempt"] = None
        except (OSError, ValueError) as exc:
            error = {
                "code": "ARTIFACT_HASH_FAILED",
                "message": f"Cannot sign successful artifacts: {exc}",
            }
            attempt["status"] = FAILED
            attempt["error"] = error
            candidate["status"] = FAILED
            candidate["last_error"] = error
            candidate["success_attempt"] = None
        else:
            attempt["status"] = SUCCESS
            candidate["status"] = SUCCESS
            candidate["last_error"] = None
            candidate["success_attempt"] = attempt["attempt"]
    else:
        attempt["status"] = FAILED
        attempt["error"] = response.error or {"code": "EXECUTION_ERROR", "message": "Candidate failed"}
        candidate["status"] = FAILED
        candidate["last_error"] = attempt["error"]
        candidate["success_attempt"] = None
    candidate["updated_utc"] = utc_now()
    if response.ok and candidate["status"] == FAILED:
        # Signing is part of execution success, including a dependency changed
        # after worker acceptance. Propagate it to run/sweep/batch and control.
        response.ok = False
        response.error = copy.deepcopy(candidate["last_error"])


def _safe_rel_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("\\", "/")
    candidate = Path(text)
    if candidate.is_absolute() or candidate.drive:
        return None
    parts = candidate.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    return candidate


def _verify_cache_inventory(cache: dict[str, Any]) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    try:
        generation = path_guard.normalize_path(cache.get("generation_dir"))
        manifest_path = path_guard.normalize_path(cache.get("manifest"))
    except path_guard.PathGuardError as exc:
        return [{"code": "cache_path_invalid", "message": exc.message}]
    if not generation.is_dir():
        return [{"code": "cache_generation_missing", "path": str(generation)}]
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return [{"code": "cache_manifest_missing", "path": str(manifest_path)}]
    if generation / CACHE_MANIFEST_FILENAME != manifest_path:
        reasons.append({"code": "cache_manifest_path_mismatch", "path": str(manifest_path)})

    recorded_file_sha = cache.get("manifest_file_sha256")
    if not isinstance(recorded_file_sha, str) or protocol.sha256_file(manifest_path) != recorded_file_sha:
        reasons.append({"code": "cache_manifest_tampered", "path": str(manifest_path)})
    manifest = _safe_json(manifest_path)
    if not isinstance(manifest, dict):
        reasons.append({"code": "cache_manifest_corrupt", "path": str(manifest_path)})
        return reasons
    if manifest.get("schema_version") != CACHE_SCHEMA_VERSION or manifest.get("kind") != CACHE_KIND:
        reasons.append({"code": "cache_manifest_schema_invalid"})
    manifest_sha = manifest.get("manifest_sha256")
    if not isinstance(manifest_sha, str) or manifest_sha != cache.get("manifest_sha256"):
        reasons.append({"code": "cache_manifest_sha256_mismatch"})
    checksum_payload = dict(manifest)
    checksum_payload.pop("manifest_sha256", None)
    if not isinstance(manifest_sha, str) or manifest_sha != sha256_text(canonical_json(checksum_payload)):
        reasons.append({"code": "cache_manifest_checksum_invalid"})
    if manifest.get("generation") != generation.name:
        reasons.append({"code": "cache_generation_mismatch"})
    if not isinstance(manifest.get("identity"), dict):
        reasons.append({"code": "cache_identity_missing"})

    frame_start = cache.get("frame_start")
    frame_end = cache.get("frame_end")
    if isinstance(frame_start, int) and isinstance(frame_end, int):
        if manifest.get("frame_start") != frame_start or manifest.get("frame_end") != frame_end:
            reasons.append({"code": "cache_frame_range_mismatch"})
        frames = {
            int(entry.get("frame"))
            for entry in (manifest.get("files") or [])
            if isinstance(entry, dict)
            and entry.get("kind") == "meta"
            and isinstance(entry.get("frame"), int)
            and not isinstance(entry.get("frame"), bool)
        }
        expected_frames = set(range(frame_start, frame_end + 1))
        if frames != expected_frames:
            reasons.append(
                {
                    "code": "cache_frame_coverage_mismatch",
                    "missing": sorted(expected_frames - frames)[:8],
                    "extra": sorted(frames - expected_frames)[:8],
                }
            )

    current_path = Path(cache["owner_root"]) / CACHE_CURRENT_FILENAME if cache.get("owner_root") else None
    if current_path is not None:
        if not current_path.is_file():
            reasons.append({"code": "cache_current_missing", "path": str(current_path)})
        else:
            current = _safe_json(current_path)
            if not isinstance(current, dict):
                reasons.append({"code": "cache_current_corrupt", "path": str(current_path)})
            else:
                pointer_payload = dict(current)
                pointer_payload.pop("pointer_sha256", None)
                if current.get("kind") != CURRENT_KIND or current.get("pointer_sha256") != sha256_text(
                    canonical_json(pointer_payload)
                ):
                    reasons.append({"code": "cache_current_corrupt", "path": str(current_path)})
                if (
                    current.get("status") != "committed"
                    or current.get("generation") != generation.name
                    or current.get("manifest_sha256") != manifest_sha
                ):
                    reasons.append({"code": "cache_current_mismatch", "path": str(current_path)})

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        reasons.append({"code": "cache_manifest_files_missing"})
        return reasons
    listed_meta: set[str] = set()
    listed_blob: set[str] = set()
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            reasons.append({"code": "cache_manifest_file_entry_invalid"})
            continue
        relative = _safe_rel_path(entry.get("path"))
        if relative is None:
            reasons.append({"code": "cache_manifest_path_invalid", "value": entry.get("path")})
            continue
        rel_text = relative.as_posix()
        if rel_text in seen:
            reasons.append({"code": "cache_manifest_duplicate_file", "path": rel_text})
            continue
        seen.add(rel_text)
        kind = entry.get("kind")
        if kind == "meta":
            listed_meta.add(rel_text)
        elif kind == "blob":
            listed_blob.add(rel_text)
        else:
            reasons.append({"code": "cache_manifest_file_kind_invalid", "path": rel_text})
            continue
        size = entry.get("size")
        digest = entry.get("sha256")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
        ):
            reasons.append({"code": "cache_manifest_file_entry_invalid", "path": rel_text})
            continue
        actual_path = generation / relative
        if not actual_path.is_file() or actual_path.is_symlink():
            reasons.append({"code": "cache_file_missing", "path": rel_text})
            continue
        if actual_path.stat().st_size != size or protocol.sha256_file(actual_path) != digest:
            reasons.append({"code": "cache_file_corrupt", "path": rel_text})

    try:
        actual_meta = {
            path.relative_to(generation).as_posix()
            for path in generation.rglob("*.json")
            if path.is_file() and path.parent.name == "meta"
        }
        actual_blob = {
            path.relative_to(generation).as_posix()
            for path in generation.rglob("*.blob")
            if path.is_file() and path.parent.name == "blobs"
        }
    except OSError as exc:
        reasons.append({"code": "cache_generation_unreadable", "message": str(exc)})
        actual_meta = set()
        actual_blob = set()
    if actual_meta != listed_meta:
        reasons.append(
            {
                "code": "cache_meta_inventory_mismatch",
                "missing": sorted(listed_meta - actual_meta)[:8],
                "extra": sorted(actual_meta - listed_meta)[:8],
            }
        )
    if actual_blob != listed_blob:
        reasons.append(
            {
                "code": "cache_blob_inventory_mismatch",
                "missing": sorted(listed_blob - actual_blob)[:8],
                "extra": sorted(actual_blob - listed_blob)[:8],
            }
        )
    return reasons


def _evidence_schema_from_mapping(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in _EVIDENCE_SCHEMA_KEYS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _recorded_evidence_schemas(
    attempt: dict[str, Any],
    receipt_payload: dict[str, Any] | None = None,
) -> list[str]:
    """Return every distinct schema actually signed by the recorded evidence."""

    sources: list[Any] = [attempt.get("cold_reopen"), attempt]
    if isinstance(receipt_payload, dict):
        worker = receipt_payload.get("worker")
        sources.extend(
            [
                receipt_payload.get("cold_reopen_verify"),
                receipt_payload.get("geometry_evidence"),
                receipt_payload,
                worker.get("verification_evidence") if isinstance(worker, dict) else None,
            ]
        )
    schemas: list[str] = []
    for source in sources:
        schema = _evidence_schema_from_mapping(source)
        if schema is not None and schema not in schemas:
            schemas.append(schema)
    return schemas


def _recorded_evidence_schema(
    attempt: dict[str, Any],
    receipt_payload: dict[str, Any] | None = None,
) -> str | None:
    schemas = _recorded_evidence_schemas(attempt, receipt_payload)
    return schemas[0] if schemas else None


def current_evidence_schema() -> str:
    """Current evidence schema, without importing bpy into the host process."""

    try:
        capabilities = deployment.import_addon_module("capabilities")
        describe = getattr(capabilities, "describe_capabilities", None)
        if callable(describe):
            info = describe()
            if isinstance(info, dict):
                value = info.get("physics_evidence_schema")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    except Exception:
        # Older capability modules predate the field.  The parent integration
        # target is v2; never silently skip a v1 cold receipt.
        pass
    return FALLBACK_EVIDENCE_SCHEMA


def verify_success_attempt(candidate: dict[str, Any]) -> dict[str, Any]:
    """Re-verify a successful candidate without rerunning Blender."""

    reasons: list[dict[str, Any]] = []
    attempts = candidate.get("attempts") or []
    success_attempt = candidate.get("success_attempt")
    attempt = next(
        (
            item
            for item in attempts
            if isinstance(item, dict)
            and item.get("attempt") == success_attempt
            and item.get("status") == SUCCESS
        ),
        None,
    )
    if attempt is None:
        return {
            "ok": False,
            "reasons": [{"code": "success_attempt_missing"}],
            "attempt": success_attempt,
        }

    receipt = attempt.get("receipt") or {}
    receipt_path = receipt.get("path")
    receipt_sha = receipt.get("sha256")
    if not isinstance(receipt_path, str) or not isinstance(receipt_sha, str):
        reasons.append({"code": "receipt_reference_missing"})
        receipt_payload = None
    else:
        resolved_receipt = Path(receipt_path)
        if not resolved_receipt.is_file() or resolved_receipt.is_symlink():
            reasons.append({"code": "receipt_missing", "path": receipt_path})
            receipt_payload = None
        else:
            try:
                actual_receipt_sha = protocol.sha256_file(resolved_receipt)
            except OSError as exc:
                reasons.append({"code": "receipt_unreadable", "path": receipt_path, "message": str(exc)})
            else:
                if actual_receipt_sha != receipt_sha:
                    reasons.append(
                        {
                            "code": "receipt_tampered",
                            "path": receipt_path,
                            "expected_sha256": receipt_sha,
                            "actual_sha256": actual_receipt_sha,
                        }
                    )
            receipt_payload = _safe_json(resolved_receipt)
            if not isinstance(receipt_payload, dict):
                reasons.append({"code": "receipt_corrupt", "path": receipt_path})

    output = attempt.get("output") or {}
    output_path = output.get("path")
    output_sha = output.get("sha256")
    if not isinstance(output_path, str) or not isinstance(output_sha, str):
        reasons.append({"code": "output_reference_missing"})
    else:
        output_reason = _assert_same_file_sha(output_path, output_sha)
        if output_reason:
            reasons.append(
                {"code": "output_" + output_reason, "path": output_path, "expected_sha256": output_sha}
            )
        elif isinstance(receipt_payload, dict):
            input_sha = candidate.get("input_sha256")
            if isinstance(input_sha, str) and (
                receipt_payload.get("input_sha256_before") != input_sha
                or receipt_payload.get("input_sha256_after") != input_sha
            ):
                reasons.append({"code": "receipt_input_sha256_mismatch", "path": receipt_path})
            if receipt_payload.get("output_sha256") != output_sha:
                reasons.append({"code": "receipt_output_sha256_mismatch", "path": output_path})
            try:
                receipt_output = Path(str(receipt_payload.get("output_blend"))).resolve()
                recorded_output = Path(output_path).resolve()
            except (OSError, RuntimeError, ValueError):
                reasons.append({"code": "receipt_output_path_unreadable", "path": output_path})
            else:
                if receipt_output != recorded_output:
                    reasons.append({"code": "receipt_output_path_mismatch", "path": output_path})

    for export in attempt.get("exports") or []:
        if not isinstance(export, dict):
            reasons.append({"code": "export_reference_invalid"})
            continue
        path_value = export.get("path")
        digest = export.get("sha256")
        if not isinstance(path_value, str) or not isinstance(digest, str):
            reasons.append({"code": "export_reference_invalid"})
            continue
        export_reason = _assert_same_file_sha(path_value, digest)
        if export_reason:
            reasons.append(
                {
                    "code": "export_" + export_reason,
                    "path": path_value,
                    "expected_sha256": digest,
                }
            )

    export_format = (candidate.get("normalized") or {}).get("export_format")
    primary = (attempt.get("used_paths") or {}).get("export_path") or (candidate.get("request") or {}).get("export_path")
    if export_format not in (None, "NONE"):
        receipt_worker = receipt_payload.get("worker") if isinstance(receipt_payload, dict) else None
        receipt_export = receipt_worker.get("export") if isinstance(receipt_worker, dict) else None
        if primary and attempt.get("delivery_manifest") is None:
            reasons.append({"code": "delivery_manifest_missing"})
        elif primary and (not isinstance(receipt_export, dict) or receipt_export.get("delivery_manifest") != attempt.get("delivery_manifest")):
            reasons.append({"code": "delivery_manifest_receipt_mismatch"})
        try:
            deployment.import_addon_module("export_delivery").verify_export_result(receipt_export, export_format, primary)
        except (OSError, ValueError, TypeError) as exc:
            reasons.append({"code": "delivery_manifest_invalid", "message": str(exc)})

    cache = attempt.get("cache")
    normalized = candidate.get("normalized") or {}
    if isinstance(cache, dict):
        try:
            reasons.extend(_verify_cache_inventory(cache))
        except (OSError, ValueError, TypeError) as exc:
            reasons.append({"code": "cache_unreadable", "message": str(exc)})
    elif normalized.get("cache_expected"):
        reasons.append({"code": "cache_reference_missing"})

    cold = attempt.get("cold_reopen") or {}
    if normalized.get("verify_cold_reopen") and not (
        isinstance(cold, dict) and cold.get("passed") is True
    ):
        reasons.append({"code": "cold_reopen_evidence_missing"})
    if isinstance(receipt_payload, dict) and normalized.get("verify_cold_reopen") is True:
        receipt_cold = receipt_payload.get("cold_reopen_verify")
        if not isinstance(receipt_cold, dict) or receipt_cold.get("passed") is not True:
            reasons.append({"code": "receipt_cold_reopen_missing"})

    required_schema = current_evidence_schema() if normalized.get("verify_cold_reopen") is True else None
    if required_schema is not None:
        # A worker/attempt tag cannot supply a missing verifier declaration.
        verifier_schema = _evidence_schema_from_mapping(receipt_payload.get("cold_reopen_verify")) if isinstance(receipt_payload, dict) else None
        if verifier_schema != required_schema:
            reasons.append({"code": "cold_verifier_evidence_schema_mismatch",
                            "recorded": verifier_schema, "current": required_schema})
    recorded_schemas = _recorded_evidence_schemas(attempt, receipt_payload)
    recorded_schema = recorded_schemas[0] if recorded_schemas else None
    if normalized.get("verify_cold_reopen") is True:
        if not recorded_schemas:
            reasons.append(
                {
                    "code": "cold_reopen_evidence_schema_missing",
                    "current": required_schema,
                }
            )
        elif any(schema != required_schema for schema in recorded_schemas):
            reasons.append(
                {
                    "code": "cold_reopen_evidence_schema_mismatch",
                    "recorded": recorded_schema,
                    "recorded_schemas": recorded_schemas,
                    "current": required_schema,
                }
            )

    return {
        "ok": not reasons,
        "reasons": reasons,
        "attempt": attempt.get("attempt"),
        "job_id": attempt.get("job_id"),
        "evidence_schema": recorded_schema,
        "required_evidence_schema": required_schema,
        "checked_at_utc": utc_now(),
    }


class ManifestLock:
    """An OS-backed exclusive lock with explicit owner metadata.

    The lock is released automatically when the owning process exits, so a
    crashed resume does not require a TTL-based takeover.  The lock file is
    intentionally never deleted: deleting it can split lockers across inodes.
    """

    def __init__(self, manifest_path: Path, owner: str = "resume") -> None:
        self.manifest_path = path_guard.normalize_path(manifest_path)
        self.lock_path = manifest_lock_path(self.manifest_path)
        self.owner_path = self.lock_path.with_name(self.lock_path.name + ".owner.json")
        self.owner = owner
        self.token = uuid.uuid4().hex
        self.owner_payload: dict[str, Any] = {}
        self._handle = None
        self._locked = False

    @property
    def held(self) -> bool:
        return self._locked

    def _metadata(self) -> dict[str, Any]:
        hostname = os.environ.get("COMPUTERNAME")
        if not hostname and hasattr(os, "uname"):
            hostname = os.uname().nodename
        return {
            "schema": "pnw.workflow.resume_lock/1",
            "token": self.token,
            "owner": self.owner,
            "pid": os.getpid(),
            "hostname": hostname or "",
            "created_utc": utc_now(),
            "manifest": str(self.manifest_path),
        }

    def _try_lock(self, handle) -> bool:
        if os.name == "nt":
            import msvcrt

            try:
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.seek(0)
                    handle.write(b"\n")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            return True
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(self, handle) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass

    def _read_owner(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.owner_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        try:
            return json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def acquire(self) -> None:
        if self._locked:
            raise ResumeError(
                "RESUME_LOCK_ALREADY_HELD",
                "This ManifestLock instance already owns the manifest lock",
                lock_path=str(self.lock_path),
            )
        if not self.manifest_path.parent.is_dir():
            raise ResumeError(
                "MANIFEST_NOT_FOUND",
                f"Manifest directory does not exist: {self.manifest_path.parent}",
            )
        try:
            handle = self.lock_path.open("a+b")
        except PermissionError as exc:
            # Windows commonly denies opening a file whose byte range is
            # already locked by another process.  That is contention, not an
            # unusable lock implementation.
            raise ResumeError(
                "RESUME_LOCKED",
                "Another resume already owns this manifest",
                lock_path=str(self.lock_path),
                owner=self._read_owner(),
            ) from exc
        except OSError as exc:
            raise ResumeError(
                "RESUME_LOCK_ERROR",
                f"Cannot open resume lock: {exc}",
                lock_path=str(self.lock_path),
            ) from exc
        if not self._try_lock(handle):
            owner = self._read_owner()
            handle.close()
            raise ResumeError(
                "RESUME_LOCKED",
                "Another resume already owns this manifest",
                lock_path=str(self.lock_path),
                owner=owner,
            )
        try:
            payload = self._metadata()
            encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
            handle.seek(0)
            handle.truncate()
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            _atomic_write_json(self.owner_path, payload)
        except OSError:
            self._unlock(handle)
            handle.close()
            raise
        self._handle = handle
        self._locked = True
        self.owner_payload = payload

    def release(self) -> None:
        if self._handle is None:
            return
        if self._locked:
            self._unlock(self._handle)
        try:
            self._handle.close()
        finally:
            self._handle = None
            self._locked = False

    def __enter__(self) -> "ManifestLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _retry_request(candidate: dict[str, Any], job_id: str, job_dir: Path) -> dict[str, Any]:
    request = copy.deepcopy(candidate.get("request"))
    if not isinstance(request, dict):
        raise ResumeError(
            "PLAN_CANDIDATE_INVALID",
            "Candidate has no reusable request",
            candidate_id=candidate.get("candidate_id"),
        )
    normalized = candidate.get("normalized") or {}
    if normalized.get("operation") == "invalidate_cache":
        raise ResumeError(
            "PLAN_CANDIDATE_NOT_REPLAYABLE",
            "invalidate_cache candidates are not replayed because they mutate an existing cache reference",
            candidate_id=candidate.get("candidate_id"),
        )
    request["output_blend"] = str(job_dir / "output.blend")
    if normalized.get("cache_expected"):
        # Keep the retry bake root short.  ``<job>/bake`` adds the job id plus
        # the add-on's owned-generation suffix and can exceed the add-on's
        # Windows path budget when the jobs root is already deep.  The original
        # run already uses a short ``bakes/<hash>`` root; mirror it for retry
        # jobs so a replay remains possible under the same path budget.
        short = hashlib.sha1(job_id.encode("utf-8")).hexdigest()[:10]
        request["bake_directory"] = str(job_dir.parent / "bakes" / short)
    export_format = normalized.get("export_format", request.get("export_format", "NONE"))
    original = request.get("export_path")
    if export_format not in (None, "NONE") and (original or str(export_format) == "ALEMBIC"):
        extension = _EXPORT_EXTENSIONS.get(str(export_format), "")
        if original:
            name = Path(str(original)).name
        else:
            name = f"{candidate.get('candidate_id', 'candidate')}{extension}"
        if extension and not name.lower().endswith(extension):
            name = f"{Path(name).stem or 'candidate'}{extension}"
        request["export_path"] = str(job_dir / "export" / name)
    # Resume must never inherit overwrite permission from the plan.
    request["allow_overwrite"] = False
    request.pop("overwrite", None)
    return request


def _new_retry_job_id() -> str:
    # Retry paths live under the original jobs root; keep the owned directory
    # name compact so the addon's Windows bake path budget still holds.
    return "retry-" + uuid.uuid4().hex[:12]


def _response_for_candidate(
    result: protocol.CommandResponse,
    candidate: dict[str, Any],
    verification: dict[str, Any] | None,
) -> dict[str, Any]:
    data = result.data if isinstance(result.data, dict) else {}
    return {
        "candidate_id": candidate.get("candidate_id"),
        "index": candidate.get("index"),
        "label": candidate.get("label"),
        "action": "rerun",
        "status": candidate.get("status"),
        "job_id": result.job_id,
        "job_dir": data.get("job_dir"),
        "receipt": (
            (candidate.get("attempts") or [{}])[-1].get("receipt")
            if candidate.get("attempts")
            else None
        ),
        "error": result.error,
        "success_verification": verification,
    }


def resume_manifest(
    manifest_path: str | Path,
    timeout_seconds: int = 300,
    memory_limit_mb: int | None = 4096,
) -> protocol.CommandResponse:
    """Resume one plan by replaying only failed, pending or tampered candidates."""

    resume_id = protocol.new_job_id("resume")
    raw_manifest = str(manifest_path)
    if not Path(raw_manifest).is_absolute():
        return protocol.CommandResponse(
            ok=False,
            command="resume",
            job_id=resume_id,
            error={
                "code": "MANIFEST_PATH_NOT_ABSOLUTE",
                "message": "--manifest must be an absolute path",
                "manifest": raw_manifest,
            },
        )
    try:
        manifest = path_guard.normalize_path(raw_manifest)
    except path_guard.PathGuardError as exc:
        return protocol.CommandResponse(
            ok=False,
            command="resume",
            job_id=resume_id,
            error=exc.to_error(),
        )
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
    ):
        return protocol.CommandResponse(
            ok=False,
            command="resume",
            job_id=resume_id,
            error={"code": "VALIDATION_ERROR", "message": "timeout_seconds must be a positive integer"},
        )
    if memory_limit_mb is not None and (
        isinstance(memory_limit_mb, bool)
        or not isinstance(memory_limit_mb, int)
        or memory_limit_mb < 0
        or memory_limit_mb > 65536
    ):
        return protocol.CommandResponse(
            ok=False,
            command="resume",
            job_id=resume_id,
            error={
                "code": "VALIDATION_ERROR",
                "message": "memory_limit_mb must be an integer in [0, 65536]",
            },
        )

    lock = ManifestLock(manifest)
    try:
        lock.acquire()
    except ResumeError as exc:
        return protocol.CommandResponse(
            ok=False,
            command="resume",
            job_id=resume_id,
            error=exc.to_error(),
        )
    except OSError as exc:
        return protocol.CommandResponse(
            ok=False,
            command="resume",
            job_id=resume_id,
            error={
                "code": "RESUME_LOCK_ERROR",
                "message": f"Cannot own resume lock: {exc}",
                "lock_path": str(lock.lock_path),
            },
        )

    control: job_control.JobControl | None = None
    plan: dict[str, Any] | None = None
    terminal_intent = job_control.TERMINAL_FAILED
    try:
        try:
            plan = read_plan(manifest)
            validate_plan_identity(plan)
        except path_guard.PathGuardError as exc:
            return protocol.CommandResponse(
                ok=False,
                command="resume",
                job_id=resume_id,
                error=exc.to_error(),
            )
        except ResumeError as exc:
            return protocol.CommandResponse(
                ok=False,
                command="resume",
                job_id=resume_id,
                error=exc.to_error(),
            )

        control = job_control.JobControl(
            manifest,
            owner=f"resume:{resume_id}",
            plan=plan,
        )
        try:
            control.start()
        except job_control.JobControlError as exc:
            control = None
            return protocol.CommandResponse(
                ok=False,
                command="resume",
                job_id=resume_id,
                error=exc.to_error(),
            )
        control.set_phase("starting", plan=plan)

        # Import lazily: runner owns the plan writers and imports this module.
        from . import runner

        jobs_dir = path_guard.normalize_path(plan["jobs_dir"])
        plan_dir = path_guard.normalize_path(plan["plan_dir"])
        started_utc = utc_now()
        candidate_results: list[dict[str, Any]] = []
        rerun_count = 0
        skipped_count = 0
        passed_count = 0
        failed_count = 0
        blocked_count = 0
        plan_changed = False
        cancelled = False

        for candidate in plan["candidates"]:
            if control.cancel_requested_now():
                cancelled = True
                break
            if not isinstance(candidate, dict):
                continue
            verification = None
            if candidate.get("status") == SUCCESS:
                control.set_phase("verifying_success", plan=plan, candidate=candidate)
                verification = verify_success_attempt(candidate)
                if verification.get("ok"):
                    skipped_count += 1
                    passed_count += 1
                    candidate_results.append(
                        {
                            "candidate_id": candidate.get("candidate_id"),
                            "index": candidate.get("index"),
                            "label": candidate.get("label"),
                            "action": "skipped",
                            "status": SUCCESS,
                            "job_id": verification.get("job_id"),
                            "verification": verification,
                        }
                    )
                    continue
                control.refresh_plan(plan, phase="candidate_verification_failed")

            request = candidate.get("request")
            if not isinstance(request, dict):
                blocked_count += 1
                if candidate.get("status") != FAILED or not candidate.get("last_error"):
                    candidate["status"] = FAILED
                    candidate["last_error"] = {
                        "code": "PLAN_CANDIDATE_INVALID",
                        "message": "Candidate has no reusable request",
                    }
                    candidate["updated_utc"] = utc_now()
                    write_plan(manifest, plan)
                    plan_changed = True
                failed_count += 1
                candidate_results.append(
                    {
                        "candidate_id": candidate.get("candidate_id"),
                        "index": candidate.get("index"),
                        "label": candidate.get("label"),
                        "action": "blocked",
                        "status": FAILED,
                        "error": candidate.get("last_error"),
                        "verification": verification,
                    }
                )
                control.refresh_plan(plan, phase="candidate_blocked")
                continue

            job_id = _new_retry_job_id()
            job_dir = jobs_dir / job_id
            try:
                retry_request = _retry_request(candidate, job_id, job_dir)
            except ResumeError as exc:
                blocked_count += 1
                failed_count += 1
                candidate["status"] = FAILED
                candidate["last_error"] = exc.to_error()
                candidate["updated_utc"] = utc_now()
                write_plan(manifest, plan)
                plan_changed = True
                candidate_results.append(
                    {
                        "candidate_id": candidate.get("candidate_id"),
                        "index": candidate.get("index"),
                        "label": candidate.get("label"),
                        "action": "blocked",
                        "status": FAILED,
                        "error": exc.to_error(),
                        "verification": verification,
                    }
                )
                control.refresh_plan(plan, phase="candidate_blocked")
                continue

            attempt = begin_attempt(
                candidate,
                job_id,
                job_dir,
                {
                    "output_blend": retry_request.get("output_blend"),
                    "bake_directory": retry_request.get("bake_directory"),
                    "export_path": retry_request.get("export_path"),
                },
                request_sha256(retry_request),
            )
            write_plan(manifest, plan)
            plan_changed = True
            rerun_count += 1
            control.set_phase("candidate_starting", plan=plan, candidate=candidate, attempt=attempt)
            result = runner.run_single(
                retry_request,
                jobs_dir,
                timeout_seconds=timeout_seconds,
                memory_limit_mb=memory_limit_mb,
                job_id=job_id,
                _control=control,
            )
            complete_attempt(candidate, attempt, result)
            write_plan(manifest, plan)
            control.refresh_plan(plan, phase="candidate_finished")
            if result.ok and candidate.get("status") == SUCCESS:
                passed_count += 1
            else:
                failed_count += 1
            candidate_results.append(_response_for_candidate(result, candidate, verification))
            if ((result.error or {}).get("code") == "CANCELLED") or control.cancel_seen:
                cancelled = True
                break

        control.refresh_plan(plan, phase="finalizing")
        finished_utc = utc_now()
        report_path = None
        report_error = None
        if rerun_count or cancelled:
            plan.setdefault("resume_runs", []).append(
                {
                    "resume_id": resume_id,
                    "started_utc": started_utc,
                    "finished_utc": finished_utc,
                    "rerun": rerun_count,
                    "skipped": skipped_count,
                    "passed": passed_count,
                    "failed": failed_count,
                    "cancelled": cancelled,
                }
            )
            write_plan(manifest, plan)
            report_dir = plan_dir / "resume_reports" / resume_id
            report = {
                "resume_id": resume_id,
                "manifest": str(manifest),
                "plan_id": plan.get("plan_id"),
                "plan_command": plan.get("command"),
                "started_utc": started_utc,
                "finished_utc": finished_utc,
                "total": len(plan["candidates"]),
                "skipped": skipped_count,
                "rerun": rerun_count,
                "passed": passed_count,
                "failed": failed_count,
                "blocked": blocked_count,
                "cancelled": cancelled,
                "counts": job_control.counts_for_plan(plan),
                "candidates": candidate_results,
            }
            try:
                report_dir.mkdir(parents=True, exist_ok=False)
                report_path = report_dir / "resume_report.json"
                _atomic_write_json(report_path, report)
            except OSError as exc:
                report_error = str(exc)

        all_ok = failed_count == 0 and not cancelled
        counts = job_control.counts_for_plan(plan)
        data = {
            "manifest": str(manifest),
            "plan_id": plan.get("plan_id"),
            "plan_command": plan.get("command"),
            "execution_mode": "candidate_replay",
            "intermediate_frame_resume": False,
            "total": len(plan["candidates"]),
            "skipped": skipped_count,
            "rerun": rerun_count,
            "passed": passed_count,
            "failed": failed_count,
            "blocked": blocked_count,
            "cancelled": cancelled,
            "counts": counts,
            "plan_changed": plan_changed,
            "started_utc": started_utc,
            "finished_utc": finished_utc,
            "candidates": candidate_results,
            "resume_report": str(report_path) if report_path else None,
            "report_error": report_error,
            "job_control": str(job_control.control_path(manifest)),
            "lock": {
                "path": str(lock.lock_path),
                "token": lock.token,
                "owner": lock.owner_payload,
            },
        }
        if cancelled:
            terminal_intent = job_control.TERMINAL_CANCELLED
        elif all_ok:
            terminal_intent = job_control.TERMINAL_SUCCESS
        else:
            terminal_intent = job_control.TERMINAL_FAILED
        artifacts = [
            str(path)
            for path in [report_path, job_control.control_path(manifest)]
            if path is not None and Path(path).exists()
        ]
        for item in candidate_results:
            receipt = item.get("receipt")
            if isinstance(receipt, dict) and isinstance(receipt.get("path"), str):
                artifacts.append(receipt["path"])
        return protocol.CommandResponse(
            ok=all_ok,
            command="resume",
            job_id=resume_id,
            data=data,
            artifacts=artifacts,
            error=(
                {
                    "code": "CANCELLED",
                    "message": "Resume cancelled by request; successful candidates were preserved",
                }
                if cancelled
                else None
                if all_ok
                else {
                    "code": "RESUME_PARTIAL_FAILURE",
                    "message": "One or more candidates remain failed after resume",
                }
            ),
        )
    except ResumeError as exc:
        return protocol.CommandResponse(
            ok=False,
            command="resume",
            job_id=resume_id,
            error=exc.to_error(),
        )
    except OSError as exc:
        return protocol.CommandResponse(
            ok=False,
            command="resume",
            job_id=resume_id,
            error={
                "code": "RESUME_IO_ERROR",
                "message": f"Resume I/O failed: {exc}",
                "manifest": str(manifest),
            },
        )
    except Exception as exc:
        return protocol.CommandResponse(
            ok=False,
            command="resume",
            job_id=resume_id,
            error={
                "code": "RESUME_EXECUTION_ERROR",
                "message": f"Unhandled resume error: {exc}",
                "traceback": traceback.format_exc()[-4000:],
            },
        )
    finally:
        lock.release()
        if control is not None and plan is not None:
            control.finish_after_unlock(
                terminal_intent,
                plan=plan,
                reason=(
                    "cancel_request_observed_by_original_executor"
                    if terminal_intent == job_control.TERMINAL_CANCELLED
                    else "resume_completed"
                ),
            )


def finalize_single_plan(
    job_dir: Path,
    response: protocol.CommandResponse,
    lock: ManifestLock | None = None,
) -> dict[str, Any] | None:
    """Record the terminal state of a direct run's one-candidate plan."""

    plan_path = Path(job_dir) / PLAN_FILENAME
    if not plan_path.is_file():
        return None
    owns_lock = False
    if lock is None:
        lock = ManifestLock(plan_path, owner="run-finalize")
        try:
            lock.acquire()
        except ResumeError:
            # A concurrent resume owns this plan; do not rewrite it underneath
            # the lock holder.  The caller reports a warning instead of
            # corrupting state.
            return None
        owns_lock = True
    else:
        expected_lock_path = manifest_lock_path(plan_path)
        if not lock.held or lock.lock_path != expected_lock_path:
            raise ResumeError(
                "PLAN_LOCK_NOT_HELD",
                "The caller must pass the same held ManifestLock used to create this plan",
                expected_lock_path=str(expected_lock_path),
                passed_lock_path=str(lock.lock_path),
                held=lock.held,
            )
    try:
        plan = read_plan(plan_path)
        candidate = plan["candidates"][0]
        attempts = candidate.get("attempts") or []
        attempt = next(
            (
                item
                for item in reversed(attempts)
                if isinstance(item, dict) and item.get("status") == PENDING
            ),
            None,
        )
        if attempt is None:
            return plan
        complete_attempt(candidate, attempt, response)
        write_plan(plan_path, plan)
        return plan
    finally:
        if owns_lock:
            lock.release()
