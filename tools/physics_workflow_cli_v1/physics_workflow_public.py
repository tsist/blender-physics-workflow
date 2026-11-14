"""Public Python helper wrapper for programmatic invocation of physics_workflow_cli_v1."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import job_control, protocol, resume, runner


def describe_workflow() -> dict[str, Any]:
    return runner.describe().to_dict()


def run_workflow(
    request: dict[str, Any],
    jobs_dir: str | Path,
    timeout_seconds: int = 300,
    memory_limit_mb: int | None = 4096,
) -> dict[str, Any]:
    return runner.run_single(
        request,
        Path(jobs_dir),
        timeout_seconds=timeout_seconds,
        memory_limit_mb=memory_limit_mb,
    ).to_dict()


def sweep_workflow(
    request: dict[str, Any],
    jobs_dir: str | Path,
    timeout_seconds: int = 300,
    memory_limit_mb: int | None = 4096,
) -> dict[str, Any]:
    return runner.run_sweep(
        request,
        Path(jobs_dir),
        timeout_seconds=timeout_seconds,
        memory_limit_mb=memory_limit_mb,
    ).to_dict()


def batch_workflow(
    request: dict[str, Any],
    jobs_dir: str | Path,
    timeout_seconds: int = 300,
    memory_limit_mb: int | None = 4096,
) -> dict[str, Any]:
    return runner.run_batch(
        request,
        Path(jobs_dir),
        timeout_seconds=timeout_seconds,
        memory_limit_mb=memory_limit_mb,
    ).to_dict()


def resume_workflow(
    manifest_path: str | Path,
    timeout_seconds: int = 300,
    memory_limit_mb: int | None = 4096,
) -> dict[str, Any]:
    """Resume failed/pending candidates in a persisted run/sweep/batch plan.

    Candidates are replayed from their original input.  This helper does not
    continue a simulation from an arbitrary intermediate frame.
    """

    return resume.resume_manifest(
        manifest_path,
        timeout_seconds=timeout_seconds,
        memory_limit_mb=memory_limit_mb,
    ).to_dict()


def status_workflow(manifest_path: str | Path) -> dict[str, Any]:
    """Return compact read-only status for a run/sweep/batch/resume plan."""

    return job_control.status_manifest(manifest_path).to_dict()


def cancel_workflow(manifest_path: str | Path) -> dict[str, Any]:
    """Request cooperative cancellation of a run/sweep/batch/resume plan.

    A successful ``requested`` response is not a terminal ``cancelled`` state.
    Only the original executor can confirm cancellation after closing its own
    process tree and releasing the manifest lock.
    """

    return job_control.cancel_manifest(manifest_path).to_dict()
