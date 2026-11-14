"""Persistent job status and cooperative cancellation for workflow plans.

The control record is a sidecar next to ``resume_plan.json``.  It is written
only by the process that currently owns the manifest's OS exclusive lock, so
the existing lock still serializes every manifest/control transition.

``status`` is read-only: it reads one atomically published sidecar and never
acquires the manifest lock.  ``cancel`` writes a tokenized request and never
acquires the manifest lock, never signals a PID and never kills a process.
Only the original executor - the owner of the Job Object / process group -
consults the request while it runs, closes its own process tree, and after
releasing the manifest lock publishes the terminal ``cancelled`` state.

This module deliberately does not import ``resume`` at import time: resume is
one of the callers and it loads this module.  Plan fallback reads are lazy.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import uuid
from typing import Any

from . import path_guard
from . import protocol


CONTROL_SCHEMA = "pnw.workflow.job_control"
CONTROL_VERSION = 1
STATUS_SCHEMA = "pnw.workflow.job_status"
STATUS_VERSION = 1
REQUEST_SCHEMA = "pnw.workflow.cancel_request"
REQUEST_VERSION = 1

CONTROL_FILENAME = "job_control.json"
REQUEST_FILENAME = "cancel_request.json"

# The executor refreshes the heartbeat while it supervises child processes and
# at every phase boundary.  A status reader never treats an old owner.json PID
# as a live executor.
ACTIVE_HEARTBEAT_SECONDS = 30.0
CANCEL_POLL_SECONDS = 0.25
HEARTBEAT_WRITE_SECONDS = 1.0

TERMINAL_SUCCESS = "success"
TERMINAL_FAILED = "failed"
TERMINAL_CANCELLED = "cancelled"
TERMINAL_VALUES = (TERMINAL_SUCCESS, TERMINAL_FAILED, TERMINAL_CANCELLED)

PENDING = "pending"
SUCCESS = "success"
FAILED = "failed"

_WINDOWS_REPLACE_RETRY_SECONDS = 2.0
_WINDOWS_REPLACE_RETRY_MIN_DELAY = 0.01
_WINDOWS_REPLACE_RETRY_MAX_DELAY = 0.2
_WINDOWS_REPLACE_RETRY_WINERRORS = {5, 32, 33}


class JobControlError(RuntimeError):
    """A structured control/status failure."""

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


def control_path(manifest_path: str | os.PathLike[str]) -> Path:
    manifest = path_guard.normalize_path(manifest_path)
    return manifest.parent / CONTROL_FILENAME


def cancel_request_path(manifest_path: str | os.PathLike[str]) -> Path:
    manifest = path_guard.normalize_path(manifest_path)
    return manifest.parent / REQUEST_FILENAME


def _replace_temp_atomically(temporary: Path, path: Path) -> None:
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
            if getattr(exc, "winerror", None) not in _WINDOWS_REPLACE_RETRY_WINERRORS or path.is_dir():
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2.0, _WINDOWS_REPLACE_RETRY_MAX_DELAY)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
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
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
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


def _read_document(path: Path, attempts: int = 8) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Return ``(document, error, exists)`` for an atomically replaced JSON file."""

    path = Path(path)
    last_error: str | None = None
    for attempt in range(max(1, attempts)):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, None, False
        except OSError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if getattr(exc, "winerror", None) in _WINDOWS_REPLACE_RETRY_WINERRORS and attempt + 1 < attempts:
                time.sleep(min(0.02 * (attempt + 1), 0.2))
                continue
            return None, last_error, True
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            last_error = f"invalid JSON: {exc}"
            # Atomic replacement should never expose a partial document.  A
            # single short retry also covers an external editor's in-place save.
            if attempt < 1:
                time.sleep(0.05)
                continue
            return None, last_error, True
        if not isinstance(value, dict):
            return None, "JSON document must be an object", True
        return value, None, True
    return None, last_error or "unreadable", True


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _heartbeat_age(control: dict[str, Any]) -> float | None:
    stamp = _parse_utc(control.get("heartbeat_utc") or control.get("updated_utc"))
    if stamp is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())


def _manifest_matches(document: dict[str, Any], manifest: Path) -> bool:
    recorded = document.get("manifest")
    if not isinstance(recorded, str) or not recorded:
        return False
    try:
        return os.path.normcase(os.path.normpath(str(path_guard.normalize_path(recorded)))) == os.path.normcase(
            os.path.normpath(str(manifest))
        )
    except path_guard.PathGuardError:
        return False


def counts_for_plan(plan: dict[str, Any] | None) -> dict[str, int]:
    counts = {"total": 0, SUCCESS: 0, FAILED: 0, PENDING: 0, "running": 0}
    candidates = plan.get("candidates") if isinstance(plan, dict) else None
    if not isinstance(candidates, list):
        return counts
    counts["total"] = len(candidates)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        status = candidate.get("status")
        if status == SUCCESS:
            counts[SUCCESS] += 1
        elif status == FAILED:
            counts[FAILED] += 1
        elif status == PENDING:
            counts[PENDING] += 1
        elif status == "running":
            counts["running"] += 1
    return counts


def candidate_brief(candidate: dict[str, Any] | None, *, status: str | None = None, attempt: Any = None) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None
    brief = {
        "candidate_id": candidate.get("candidate_id"),
        "index": candidate.get("index"),
        "label": candidate.get("label"),
        "status": status if status is not None else candidate.get("status"),
    }
    if attempt is not None:
        if isinstance(attempt, dict):
            attempt = attempt.get("attempt")
        if isinstance(attempt, int) and not isinstance(attempt, bool):
            brief["attempt"] = attempt
    return brief


def _candidate_is_replayable(candidate: dict[str, Any]) -> bool:
    if not isinstance(candidate.get("request"), dict):
        return False
    normalized = candidate.get("normalized") if isinstance(candidate.get("normalized"), dict) else {}
    # invalidate_cache mutates a pre-existing cache reference and is not
    # replayed from its request by resume.
    return normalized.get("operation") != "invalidate_cache"


def recoverable_for_plan(plan: dict[str, Any] | None) -> bool:
    candidates = plan.get("candidates") if isinstance(plan, dict) else None
    if not isinstance(candidates, list):
        return False
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("status") in (PENDING, FAILED) and _candidate_is_replayable(candidate):
            return True
    return False


def terminal_for_plan(plan: dict[str, Any] | None) -> str | None:
    candidates = plan.get("candidates") if isinstance(plan, dict) else None
    if not isinstance(candidates, list) or not candidates:
        return None
    statuses = [candidate.get("status") if isinstance(candidate, dict) else None for candidate in candidates]
    if any(status == PENDING or status == "running" for status in statuses):
        return None
    if all(status == SUCCESS for status in statuses):
        return TERMINAL_SUCCESS
    if any(status == FAILED for status in statuses):
        return TERMINAL_FAILED
    return None


def _plan_fallback(manifest: Path, plan: dict[str, Any], warning: str | None = None) -> dict[str, Any]:
    terminal = terminal_for_plan(plan)
    counts = counts_for_plan(plan)
    recoverable = recoverable_for_plan(plan)
    warnings = [warning] if warning else []
    warnings.append("No original executor control record is present; no process is claimed live.")
    return {
        "schema": STATUS_SCHEMA,
        "version": STATUS_VERSION,
        "manifest": str(manifest),
        "plan_id": plan.get("plan_id"),
        "plan_command": plan.get("command"),
        "state": terminal if terminal else "interrupted",
        "phase": "terminal" if terminal else "interrupted",
        "candidate": None,
        "counts": counts,
        "terminal": terminal,
        "is_terminal": bool(terminal),
        "terminal_confirmed": bool(terminal),
        "terminal_source": "plan" if terminal else None,
        "terminal_reason": None,
        "recoverable": recoverable,
        "cancel_requested": False,
        "executor": {
            "active": False,
            "health": "absent",
            "owner": None,
            "generation": None,
            "heartbeat_age_seconds": None,
        },
        "cancel": {
            "requested": False,
            "request_id": None,
            "generation_matches": None,
            "created_utc": None,
            "ignored_stale_request": False,
            "terminal_observed": bool(terminal),
        },
        "control": {
            "path": str(control_path(manifest)),
            "present": False,
            "generation": None,
            "owner": None,
        },
        "lock_path": str(manifest.with_name(manifest.name + ".lock")),
        "warnings": warnings,
        "updated_utc": utc_now(),
    }


def _request_matches(request: dict[str, Any], *, manifest: Path, generation: str | None, plan_id: Any = None) -> bool:
    if request.get("schema") != REQUEST_SCHEMA or request.get("version") != REQUEST_VERSION:
        return False
    if not isinstance(request.get("request_id"), str) or not request["request_id"]:
        return False
    if not _manifest_matches(request, manifest):
        return False
    if generation is not None and request.get("generation") != generation:
        return False
    if plan_id is not None and request.get("plan_id") not in (None, plan_id):
        return False
    return True


def _status_from_control(manifest: Path, control: dict[str, Any]) -> dict[str, Any]:
    terminal = control.get("terminal")
    if terminal not in TERMINAL_VALUES:
        terminal = None
    if terminal is not None and control.get("state") != "terminal":
        # A terminal marker is authoritative only in the terminal state written
        # by the original executor.
        terminal = None
    age = _heartbeat_age(control)
    active = terminal is None and age is not None and age <= ACTIVE_HEARTBEAT_SECONDS
    if terminal is not None:
        health = "terminal"
    elif active:
        health = "active"
    elif age is None:
        health = "unknown"
    else:
        health = "stale"

    generation = control.get("generation") if isinstance(control.get("generation"), str) else None
    request, request_error, request_exists = _read_document(cancel_request_path(manifest), attempts=2)
    matching_request = bool(request and _request_matches(request, manifest=manifest, generation=generation, plan_id=control.get("plan_id")))
    ignored_stale = bool(request_exists and request and not matching_request)
    cancel_requested = bool(matching_request and terminal is None)
    cancel_info = {
        "requested": bool(matching_request),
        "request_id": request.get("request_id") if matching_request and request else None,
        "generation_matches": matching_request if request_exists else None,
        "created_utc": request.get("created_utc") if matching_request and request else None,
        "ignored_stale_request": ignored_stale,
        "terminal_observed": terminal is not None,
    }
    if request_error:
        cancel_info["read_error"] = request_error

    if terminal is not None:
        state = terminal
        phase = "terminated"
    elif active and cancel_requested:
        state = "cancel_requested"
        phase = "cancelling"
    elif active:
        state = "running"
        phase = control.get("phase") or "running"
    elif cancel_requested:
        state = "cancel_requested_unconfirmed"
        phase = "cancelling_unconfirmed"
    else:
        state = "interrupted"
        phase = "interrupted"

    counts = control.get("counts")
    if not isinstance(counts, dict):
        counts = {"total": 0, SUCCESS: 0, FAILED: 0, PENDING: 0, "running": 0}
    candidate = control.get("candidate") if isinstance(control.get("candidate"), dict) else None
    warnings: list[str] = []
    if not active and terminal is None:
        warnings.append(
            f"No live executor heartbeat within {ACTIVE_HEARTBEAT_SECONDS:.0f}s; the plan is interrupted, not confirmed cancelled."
        )
    if ignored_stale:
        warnings.append("A cancel request belongs to an older control generation and is ignored.")
    return {
        "schema": STATUS_SCHEMA,
        "version": STATUS_VERSION,
        "manifest": str(manifest),
        "plan_id": control.get("plan_id"),
        "plan_command": control.get("plan_command"),
        "state": state,
        "phase": phase,
        "candidate": candidate,
        "counts": counts,
        "terminal": terminal,
        "is_terminal": terminal is not None,
        "terminal_confirmed": terminal is not None,
        "terminal_source": "control" if terminal is not None else None,
        "terminal_reason": control.get("terminal_reason"),
        "recoverable": bool(control.get("recoverable")),
        "cancel_requested": cancel_requested,
        "executor": {
            "active": active,
            "health": health,
            "owner": control.get("owner"),
            "generation": generation,
            "heartbeat_age_seconds": round(age, 3) if age is not None else None,
        },
        "cancel": cancel_info,
        "control": {
            "path": str(control_path(manifest)),
            "present": True,
            "generation": generation,
            "owner": control.get("owner"),
        },
        "lock_path": str(manifest.with_name(manifest.name + ".lock")),
        "warnings": warnings,
        "updated_utc": control.get("updated_utc") or utc_now(),
    }


def _load_control(manifest: Path) -> tuple[dict[str, Any] | None, str | None]:
    document, error, exists = _read_document(control_path(manifest))
    if not exists:
        return None, None
    if error:
        return None, f"control sidecar unreadable: {error}"
    if document is None:
        return None, "control sidecar is empty"
    if document.get("schema") != CONTROL_SCHEMA or document.get("version") != CONTROL_VERSION:
        return None, "control sidecar has an unsupported schema or version"
    if not _manifest_matches(document, manifest):
        return None, "control sidecar belongs to a different manifest"
    if not isinstance(document.get("generation"), str) or not document["generation"]:
        return None, "control sidecar has no generation token"
    return document, None


def _load_plan(manifest: Path) -> tuple[dict[str, Any] | None, str | None]:
    # Lazy import avoids a module cycle: resume imports this module.
    try:
        from . import resume
    except Exception as exc:  # pragma: no cover - import failure is not a plan state
        return None, f"resume module unavailable: {exc}"
    last_error: str | None = None
    for attempt in range(6):
        try:
            return resume.read_plan(manifest), None
        except resume.ResumeError as exc:
            last_error = f"{exc.code}: {exc.message}"
            if exc.code in ("MANIFEST_UNREADABLE", "MANIFEST_INTEGRITY_FAILED") and attempt < 5:
                time.sleep(0.05)
                continue
            return None, last_error
        except OSError as exc:
            last_error = f"cannot read manifest: {exc}"
            if attempt < 5:
                time.sleep(0.05)
                continue
            return None, last_error
    return None, last_error or "manifest unreadable"


def _status_snapshot(manifest: Path) -> dict[str, Any]:
    control, control_error = _load_control(manifest)
    if control is not None:
        return _status_from_control(manifest, control)
    plan, plan_error = _load_plan(manifest)
    if plan is not None:
        warning = f"Control sidecar unavailable: {control_error}" if control_error else None
        return _plan_fallback(manifest, plan, warning=warning)
    if control_error:
        raise JobControlError(
            "CONTROL_INTEGRITY_FAILED",
            control_error,
            manifest=str(manifest),
            control=str(control_path(manifest)),
        )
    raise JobControlError(
        "MANIFEST_INTEGRITY_FAILED",
        plan_error or "Resume manifest is unreadable",
        manifest=str(manifest),
    )


def _validate_manifest_argument(manifest_path: str | os.PathLike[str]) -> Path:
    raw = os.fspath(manifest_path)
    if not Path(raw).is_absolute():
        raise JobControlError("MANIFEST_PATH_NOT_ABSOLUTE", "--manifest must be an absolute path", manifest=raw)
    try:
        manifest = path_guard.normalize_path(raw)
    except path_guard.PathGuardError as exc:
        raise JobControlError(exc.code, exc.message) from exc
    if not manifest.is_file():
        raise JobControlError("MANIFEST_NOT_FOUND", f"Resume manifest not found: {manifest}", manifest=str(manifest))
    return manifest


def status_manifest(manifest_path: str | os.PathLike[str]) -> protocol.CommandResponse:
    """Return a compact, read-only status snapshot for one plan manifest."""

    job_id = protocol.new_job_id("status")
    try:
        manifest = _validate_manifest_argument(manifest_path)
        snapshot = _status_snapshot(manifest)
    except JobControlError as exc:
        return protocol.CommandResponse(ok=False, command="status", job_id=job_id, error=exc.to_error())
    except OSError as exc:
        return protocol.CommandResponse(
            ok=False,
            command="status",
            job_id=job_id,
            error={"code": "MANIFEST_UNREADABLE", "message": f"Cannot read status sources: {exc}"},
        )
    return protocol.CommandResponse(
        ok=True,
        command="status",
        job_id=job_id,
        data=snapshot,
        artifacts=[str(control_path(manifest))] if control_path(manifest).is_file() else [],
    )


def _write_cancel_request(manifest: Path, control: dict[str, Any]) -> dict[str, Any]:
    request = {
        "schema": REQUEST_SCHEMA,
        "version": REQUEST_VERSION,
        "request_id": "cancel-" + uuid.uuid4().hex,
        "manifest": str(manifest),
        "plan_id": control.get("plan_id"),
        "generation": control.get("generation"),
        "created_utc": utc_now(),
        "purpose": "cooperative_cancel",
    }
    _atomic_write_json(cancel_request_path(manifest), request)
    return request


def cancel_manifest(manifest_path: str | os.PathLike[str]) -> protocol.CommandResponse:
    """Record a cancel request without acquiring the manifest lock.

    The response distinguishes requested-but-unconfirmed from terminal
    cancelled.  A request is bound to the current control generation; a later
    resume starts a new generation and ignores stale requests.  No PID is
    trusted or signalled here.
    """

    job_id = protocol.new_job_id("cancel")
    try:
        manifest = _validate_manifest_argument(manifest_path)
    except JobControlError as exc:
        return protocol.CommandResponse(ok=False, command="cancel", job_id=job_id, error=exc.to_error())

    control, control_error = _load_control(manifest)
    if control is None:
        if control_error:
            return protocol.CommandResponse(
                ok=False,
                command="cancel",
                job_id=job_id,
                data={"manifest": str(manifest)},
                error={
                    "code": "CONTROL_INTEGRITY_FAILED",
                    "message": control_error,
                    "details": {"control": str(control_path(manifest))},
                },
            )
        plan, plan_error = _load_plan(manifest)
        if plan is None:
            return protocol.CommandResponse(
                ok=False,
                command="cancel",
                job_id=job_id,
                error={"code": "MANIFEST_INTEGRITY_FAILED", "message": plan_error or "Manifest unreadable"},
            )
        snapshot = _plan_fallback(manifest, plan)
        if snapshot["terminal"] is not None:
            return protocol.CommandResponse(
                ok=True,
                command="cancel",
                job_id=job_id,
                data={
                    "action": "already_terminal",
                    "requested": False,
                    "terminal": snapshot["terminal"],
                    "status": snapshot,
                },
                artifacts=[str(manifest)],
            )
        return protocol.CommandResponse(
            ok=False,
            command="cancel",
            job_id=job_id,
            data={"action": "no_active_executor", "requested": False, "terminal": None, "status": snapshot},
            error={
                "code": "NO_ACTIVE_EXECUTOR",
                "message": "No original executor controls this plan; pending/failed candidates can only be resumed",
                "details": {"manifest": str(manifest)},
            },
        )

    snapshot = _status_from_control(manifest, control)
    if snapshot["terminal"] is not None:
        return protocol.CommandResponse(
            ok=True,
            command="cancel",
            job_id=job_id,
            data={
                "action": "already_terminal",
                "requested": False,
                "terminal": snapshot["terminal"],
                "status": snapshot,
            },
            artifacts=[str(manifest)],
        )

    if not snapshot["executor"]["active"]:
        if snapshot["cancel"]["requested"]:
            return protocol.CommandResponse(
                ok=False,
                command="cancel",
                job_id=job_id,
                data={
                    "action": "cancel_unconfirmed",
                    "requested": True,
                    "request_id": snapshot["cancel"].get("request_id"),
                    "terminal": None,
                    "status": snapshot,
                },
                error={
                    "code": "CANCEL_UNCONFIRMED",
                    "message": "A cancel request exists but no live original executor can confirm it; terminal remains cancelled=false",
                },
            )
        return protocol.CommandResponse(
            ok=False,
            command="cancel",
            job_id=job_id,
            data={"action": "no_active_executor", "requested": False, "terminal": None, "status": snapshot},
            error={
                "code": "NO_ACTIVE_EXECUTOR",
                "message": "No live original executor heartbeat; refusing to claim cancellation. Resume the plan to continue.",
                "details": {"manifest": str(manifest), "generation": snapshot["executor"].get("generation")},
            },
        )

    if snapshot["cancel"]["requested"]:
        return protocol.CommandResponse(
            ok=True,
            command="cancel",
            job_id=job_id,
            data={
                "action": "requested",
                "requested": True,
                "idempotent": True,
                "request_id": snapshot["cancel"].get("request_id"),
                "terminal": None,
                "status": snapshot,
            },
            artifacts=[str(cancel_request_path(manifest))],
        )

    try:
        request = _write_cancel_request(manifest, control)
    except OSError as exc:
        return protocol.CommandResponse(
            ok=False,
            command="cancel",
            job_id=job_id,
            error={
                "code": "CANCEL_REQUEST_FAILED",
                "message": f"Cannot write cancel request: {exc}",
                "details": {"request": str(cancel_request_path(manifest))},
            },
        )
    updated = _status_from_control(manifest, control)
    return protocol.CommandResponse(
        ok=True,
        command="cancel",
        job_id=job_id,
        data={
            "action": "requested",
            "requested": True,
            "request_id": request["request_id"],
            "generation": request["generation"],
            "terminal": None,
            "terminal_pending": True,
            "message": "Cancellation requested; only the original executor can confirm terminal cancelled",
            "status": updated,
        },
        artifacts=[str(cancel_request_path(manifest))],
    )


class JobControl:
    """Writer-side control record owned by the current manifest lock holder."""

    def __init__(
        self,
        manifest_path: str | os.PathLike[str],
        *,
        owner: str,
        plan: dict[str, Any],
    ) -> None:
        self.manifest = path_guard.normalize_path(manifest_path)
        self.control_path = control_path(self.manifest)
        self.request_path = cancel_request_path(self.manifest)
        self.owner = owner
        self.plan_id = plan.get("plan_id")
        self.plan_command = plan.get("command")
        self.generation = uuid.uuid4().hex
        self._state: dict[str, Any] = {}
        self._last_poll = 0.0
        self._last_heartbeat = 0.0
        self._cancel_seen = False
        self._cancel_request: dict[str, Any] | None = None
        self.last_control_error: str | None = None
        self._seed_state(plan)

    def _seed_state(self, plan: dict[str, Any]) -> None:
        now = utc_now()
        first_pending = next(
            (
                candidate
                for candidate in plan.get("candidates", [])
                if isinstance(candidate, dict) and candidate.get("status") == PENDING
            ),
            None,
        )
        self._state = {
            "schema": CONTROL_SCHEMA,
            "version": CONTROL_VERSION,
            "manifest": str(self.manifest),
            "plan_id": self.plan_id,
            "plan_command": self.plan_command,
            "owner": self.owner,
            "generation": self.generation,
            "state": "running",
            "phase": "starting",
            "terminal": None,
            "terminal_reason": None,
            "candidate": candidate_brief(first_pending),
            "counts": counts_for_plan(plan),
            "recoverable": recoverable_for_plan(plan),
            "writer": {
                "pid": os.getpid(),
                "hostname": _hostname(),
                "started_utc": now,
            },
            "cancel": {
                "requested": False,
                "request_id": None,
                "acknowledged_utc": None,
            },
            "heartbeat_utc": now,
            "updated_utc": now,
        }

    @property
    def cancel_seen(self) -> bool:
        return self._cancel_seen

    @property
    def cancel_request_id(self) -> str | None:
        request = self._cancel_request or {}
        value = request.get("request_id")
        return value if isinstance(value, str) else None

    def cancellation_details(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "request_id": self.cancel_request_id,
            "control": str(self.control_path),
        }

    def _write(self) -> None:
        self._state["heartbeat_utc"] = utc_now()
        self._state["updated_utc"] = self._state["heartbeat_utc"]
        _atomic_write_json(self.control_path, self._state)
        self._last_heartbeat = time.monotonic()

    def _write_best_effort(self) -> None:
        try:
            self._write()
            self.last_control_error = None
        except OSError as exc:
            self.last_control_error = f"{type(exc).__name__}: {exc}"

    def start(self) -> None:
        try:
            self._write()
        except OSError as exc:
            raise JobControlError(
                "CONTROL_WRITE_FAILED",
                f"Cannot publish job control state: {exc}",
                control=str(self.control_path),
            ) from exc

    def _set_candidate(self, candidate: dict[str, Any], *, phase: str | None, attempt: Any = None) -> None:
        status = candidate.get("status")
        if status == PENDING and phase not in (None, "candidate_finished", "candidate_starting"):
            status = "running"
        self._state["candidate"] = candidate_brief(candidate, status=status, attempt=attempt)

    def set_phase(
        self,
        phase: str,
        *,
        plan: dict[str, Any] | None = None,
        candidate: dict[str, Any] | None = None,
        attempt: Any = None,
        candidate_status: str | None = None,
        state: str | None = None,
    ) -> None:
        if self._state.get("terminal") is not None:
            return
        self._state["phase"] = phase
        if state is not None:
            self._state["state"] = state
        if plan is not None:
            self._state["counts"] = counts_for_plan(plan)
            self._state["recoverable"] = recoverable_for_plan(plan)
        if candidate is not None:
            self._set_candidate(candidate, phase=phase, attempt=attempt)
        elif candidate_status is not None and isinstance(self._state.get("candidate"), dict):
            self._state["candidate"]["status"] = candidate_status
        self._write_best_effort()

    def refresh_plan(self, plan: dict[str, Any], *, phase: str | None = None) -> None:
        if self._state.get("terminal") is not None:
            return
        if phase is not None:
            self._state["phase"] = phase
        self._state["counts"] = counts_for_plan(plan)
        self._state["recoverable"] = recoverable_for_plan(plan)
        self._write_best_effort()

    def heartbeat(self, *, force: bool = False) -> None:
        if self._state.get("terminal") is not None:
            return
        now = time.monotonic()
        if not force and now - self._last_heartbeat < HEARTBEAT_WRITE_SECONDS:
            return
        self._write_best_effort()

    def poll_cancel(self, *, force: bool = False) -> bool:
        """Return True when a valid request targets this control generation."""

        if self._cancel_seen:
            return True
        now = time.monotonic()
        if not force and now - self._last_poll < CANCEL_POLL_SECONDS:
            self.heartbeat()
            return False
        self._last_poll = now
        request, _error, exists = _read_document(self.request_path, attempts=2)
        if (
            exists
            and request is not None
            and _request_matches(request, manifest=self.manifest, generation=self.generation, plan_id=self.plan_id)
        ):
            self._cancel_seen = True
            self._cancel_request = request
            self._state["state"] = "cancelling"
            self._state["phase"] = "cancelling"
            self._state["cancel"] = {
                "requested": True,
                "request_id": request.get("request_id"),
                "acknowledged_utc": utc_now(),
            }
            self._write_best_effort()
            return True
        self.heartbeat()
        return False

    def cancel_requested_now(self) -> bool:
        return self.poll_cancel(force=True)

    def finish_after_unlock(
        self,
        terminal: str,
        *,
        plan: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> bool:
        """Publish terminal state after the manifest lock has been released.

        The executor first releases its long-lived work lock, then briefly
        re-acquires the same OS manifest lock for this publication.  While
        holding it, the control generation is re-read and compared before the
        atomic terminal write.  A new resume that already owns the manifest
        lock makes the old writer skip entirely; a new resume that arrives
        later waits for this short publication lock and then publishes its own
        generation.  This closes the read-then-replace race the earlier
        unlocked implementation had.
        """

        if terminal not in TERMINAL_VALUES:
            raise ValueError(f"Unsupported terminal state: {terminal!r}")
        # Lazy import: resume imports this module at module load time.
        try:
            from .resume import ManifestLock
        except Exception as exc:  # pragma: no cover - import failure is not a normal state
            self.last_control_error = f"terminal publication lock unavailable: {exc}"
            return False
        publication_lock = ManifestLock(self.manifest, owner=f"terminal:{self.owner}")
        try:
            publication_lock.acquire()
        except Exception as exc:
            # The new resume owns the manifest lock.  It is authoritative; do
            # not read or replace its control record.
            self.last_control_error = f"terminal publication skipped: {exc}"
            return False
        try:
            current, _error, exists = _read_document(self.control_path)
            if not exists or current is None:
                return False
            if current.get("schema") != CONTROL_SCHEMA or current.get("version") != CONTROL_VERSION:
                return False
            if current.get("generation") != self.generation or not _manifest_matches(current, self.manifest):
                return False
            now = utc_now()
            current["state"] = "terminal"
            current["phase"] = "terminated"
            current["terminal"] = terminal
            current["terminal_reason"] = reason
            current["terminal_confirmed"] = True
            current["terminal_source"] = "original_executor"
            current["finished_utc"] = now
            current["heartbeat_utc"] = now
            current["updated_utc"] = now
            if plan is not None:
                current["counts"] = counts_for_plan(plan)
                current["recoverable"] = recoverable_for_plan(plan)
                shown = current.get("candidate") if isinstance(current.get("candidate"), dict) else None
                shown_id = shown.get("candidate_id") if shown else None
                if shown_id is not None:
                    for candidate in plan.get("candidates", []):
                        if isinstance(candidate, dict) and candidate.get("candidate_id") == shown_id:
                            current["candidate"] = candidate_brief(candidate)
                            break
            cancel = self._cancel_request or {}
            current["cancel"] = {
                "requested": bool(cancel),
                "request_id": cancel.get("request_id"),
                "acknowledged_utc": current.get("cancel", {}).get("acknowledged_utc"),
                "terminal_observed_utc": now,
            }
            # Defense in depth: re-check the on-disk generation immediately
            # before the replace.  A real writer cannot change it while we hold
            # the manifest lock, but this also catches a deterministic/lock-
            # bypassing interleaving between acquisition and publication.
            latest, _latest_error, latest_exists = _read_document(self.control_path)
            if (
                not latest_exists
                or latest is None
                or latest.get("generation") != self.generation
                or not _manifest_matches(latest, self.manifest)
            ):
                return False
            try:
                _atomic_write_json(self.control_path, current)
            except OSError as exc:
                self.last_control_error = f"{type(exc).__name__}: {exc}"
                return False
            return True
        finally:
            publication_lock.release()
