"""Command-line interface for Physics Nodes Workflow (isolated CLI overlay v1)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Support direct execution (`python tools/physics_workflow_cli_v1/cli.py ...`) and package execution
_CLI_DIR = Path(__file__).resolve().parent
if str(_CLI_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_CLI_DIR.parent))

from physics_workflow_cli_v1 import job_control, protocol, resume, runner


def _load_json_arg(spec: str) -> dict:
    p = Path(spec)
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(spec)


def _exit_code(resp: protocol.CommandResponse) -> int:
    if resp.ok:
        return protocol.EXIT_OK
    code = (resp.error or {}).get("code", "")
    if code in {
        "VALIDATION_ERROR",
        "PATH_CONFLICT",
        "OUTPUT_EXISTS",
        "INVALID_OUTPUT",
        "INVALID_PATH",
        "MANIFEST_NOT_FOUND",
        "MANIFEST_UNREADABLE",
        "MANIFEST_INTEGRITY_FAILED",
        "MANIFEST_PATH_NOT_ABSOLUTE",
        "UNSUPPORTED_PLAN_VERSION",
        "PLAN_LOCATION_INVALID",
        "PLAN_REQUEST_CHANGED",
        "PLAN_PARAMETERS_CHANGED",
        "PLAN_INPUT_CHANGED",
        "PLAN_CANDIDATE_INVALID",
        "CONTROL_INTEGRITY_FAILED",
        "MANIFEST_UNREADABLE",
    }:
        return protocol.EXIT_VALIDATION
    if code in {"VERIFICATION_FAILED", "VERIFICATION_TIMEOUT"}:
        return protocol.EXIT_VERIFICATION
    return protocol.EXIT_EXECUTION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="physics-workflow", description="Physics Nodes Workflow CLI")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("describe", help="Return contract capabilities, presets, domains, and bounds")

    for name in ("run", "sweep", "batch"):
        p = sub.add_parser(name, help=f"Execute physics workflow {name}")
        p.add_argument("--request", required=True, help="Path to JSON request file or inline JSON string")
        p.add_argument("--jobs-dir", required=True, help="Directory for job logs, bakes, and receipts")
        p.add_argument("--timeout-seconds", type=int, default=300, help="Per-Blender-invocation timeout seconds")
        p.add_argument(
            "--memory-limit-mb",
            type=int,
            default=4096,
            help="Hard committed-memory limit per Blender process tree (0 disables)",
        )

    resume_parser = sub.add_parser("resume", help="Resume a persisted run/sweep/batch plan")
    resume_parser.add_argument(
        "--manifest",
        required=True,
        help="Absolute path to resume_plan.json",
    )
    resume_parser.add_argument("--timeout-seconds", type=int, default=300, help="Per-Blender-invocation timeout seconds")
    resume_parser.add_argument(
        "--memory-limit-mb",
        type=int,
        default=4096,
        help="Hard committed-memory limit per Blender process tree (0 disables)",
    )

    status_parser = sub.add_parser("status", help="Read compact persistent status for a run/sweep/batch/resume plan")
    status_parser.add_argument(
        "--manifest",
        required=True,
        help="Absolute path to resume_plan.json",
    )

    cancel_parser = sub.add_parser("cancel", help="Request cooperative cancellation of a run/sweep/batch/resume plan")
    cancel_parser.add_argument(
        "--manifest",
        required=True,
        help="Absolute path to resume_plan.json",
    )

    args = parser.parse_args(argv)

    if args.subcommand == "describe":
        resp = runner.describe()
        print(json.dumps(resp.to_dict(), indent=2, ensure_ascii=False))
        return protocol.EXIT_OK

    if args.subcommand == "resume":
        resp = resume.resume_manifest(
            args.manifest,
            timeout_seconds=args.timeout_seconds,
            memory_limit_mb=args.memory_limit_mb,
        )
        print(json.dumps(resp.to_dict(), indent=2, ensure_ascii=False))
        return _exit_code(resp)

    if args.subcommand == "status":
        resp = job_control.status_manifest(args.manifest)
        print(json.dumps(resp.to_dict(), indent=2, ensure_ascii=False))
        return _exit_code(resp)

    if args.subcommand == "cancel":
        resp = job_control.cancel_manifest(args.manifest)
        print(json.dumps(resp.to_dict(), indent=2, ensure_ascii=False))
        return _exit_code(resp)

    try:
        payload = _load_json_arg(args.request)
    except Exception as exc:
        err = protocol.CommandResponse(
            ok=False,
            command=args.subcommand,
            job_id=protocol.new_job_id("err"),
            error={"code": "VALIDATION_ERROR", "message": f"Invalid request JSON: {exc}"},
        )
        print(json.dumps(err.to_dict(), indent=2, ensure_ascii=False))
        return protocol.EXIT_VALIDATION

    jobs_dir = Path(args.jobs_dir)
    if args.subcommand == "run":
        resp = runner.run_single(
            payload,
            jobs_dir,
            timeout_seconds=args.timeout_seconds,
            memory_limit_mb=args.memory_limit_mb,
        )
    elif args.subcommand == "sweep":
        resp = runner.run_sweep(
            payload,
            jobs_dir,
            timeout_seconds=args.timeout_seconds,
            memory_limit_mb=args.memory_limit_mb,
        )
    else:
        resp = runner.run_batch(
            payload,
            jobs_dir,
            timeout_seconds=args.timeout_seconds,
            memory_limit_mb=args.memory_limit_mb,
        )

    print(json.dumps(resp.to_dict(), indent=2, ensure_ascii=False))
    return _exit_code(resp)


if __name__ == "__main__":
    sys.exit(main())
