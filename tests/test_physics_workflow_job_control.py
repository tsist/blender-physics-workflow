"""Unit tests for persistent job control, cancellation and resource guards."""

from __future__ import annotations

import ctypes
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock
import uuid

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from physics_workflow_cli_v1 import cli, job_control, protocol, resume, resource_guard, runner  # noqa: E402

TEST_TMP = Path(os.environ.get("PNW_TEST_TMP", str(Path(__file__).resolve().parent.parent / "test-output" / "job-control")))


def _plan(root: Path, status: str = "pending", *, command: str = "batch") -> dict:
    candidate = {
        "candidate_id": "item_00",
        "index": 0,
        "label": "item_00",
        "status": status,
        "request": {"input_blend": str(root / "source.blend")},
        "request_sha256": "0" * 64,
        "normalized": {"operation": "build"},
        "attempts": [],
        "success_attempt": None,
        "last_error": None,
    }
    plan = {
        "plan_schema": resume.PLAN_SCHEMA,
        "plan_version": resume.PLAN_VERSION,
        "plan_id": "plan-test",
        "command": command,
        "jobs_dir": str(root),
        "plan_dir": str(root),
        "contract_id": "test",
        "request": {},
        "request_sha256": resume.request_sha256({}),
        "execution_mode": "candidate_replay",
        "intermediate_frame_resume": False,
        "inputs": [],
        "candidates": [candidate],
        "resume_runs": [],
    }
    return plan


def _write_plan(root: Path, plan: dict) -> Path:
    manifest = root / resume.PLAN_FILENAME
    resume.write_plan(manifest, plan)
    return manifest


def _status(manifest: Path) -> dict:
    response = job_control.status_manifest(manifest).to_dict()
    if not response["ok"]:
        raise AssertionError(response)
    return response["data"]


def _cancel(manifest: Path) -> dict:
    return job_control.cancel_manifest(manifest).to_dict()


def _windows_process_exited(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    synchronize = 0x00100000
    handle = kernel32.OpenProcess(synchronize, 0, pid)
    if not handle:
        return True
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0
    finally:
        kernel32.CloseHandle(handle)


class JobControlTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.root = TEST_TMP / f"job-control-{uuid.uuid4().hex[:12]}"
        self.root.mkdir(parents=True, exist_ok=False, mode=0o777)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_status_is_read_only_and_cancel_is_requested_not_terminal(self) -> None:
        plan = _plan(self.root)
        manifest = _write_plan(self.root, plan)
        control = job_control.JobControl(manifest, owner="batch:test", plan=plan)
        control.start()
        control_path = job_control.control_path(manifest)
        before = control_path.read_bytes()

        status = _status(manifest)
        self.assertEqual(status["state"], "running")
        self.assertIsNone(status["terminal"])
        self.assertTrue(status["executor"]["active"])
        self.assertEqual(status["counts"]["pending"], 1)
        self.assertTrue(status["recoverable"])
        self.assertEqual(control_path.read_bytes(), before, "status must not rewrite control state")

        requested = _cancel(manifest)
        self.assertTrue(requested["ok"])
        self.assertEqual(requested["data"]["action"], "requested")
        self.assertIsNone(requested["data"]["terminal"])
        request_id = requested["data"]["request_id"]
        self.assertTrue(request_id.startswith("cancel-"))

        status = _status(manifest)
        self.assertEqual(status["state"], "cancel_requested")
        self.assertTrue(status["cancel_requested"])
        self.assertTrue(status["cancel"]["requested"])
        self.assertEqual(status["cancel"]["request_id"], request_id)
        self.assertIsNone(status["terminal"])
        self.assertTrue(status["terminal_confirmed"] is False)

        repeated = _cancel(manifest)
        self.assertTrue(repeated["ok"])
        self.assertEqual(repeated["data"]["action"], "requested")
        self.assertTrue(repeated["data"]["idempotent"])
        self.assertEqual(repeated["data"]["request_id"], request_id)

        self.assertTrue(control.poll_cancel())
        self.assertTrue(control.finish_after_unlock(job_control.TERMINAL_CANCELLED, plan=plan, reason="test"))
        status = _status(manifest)
        self.assertEqual(status["state"], "cancelled")
        self.assertEqual(status["terminal"], "cancelled")
        self.assertTrue(status["terminal_confirmed"])
        self.assertEqual(status["terminal_source"], "control")
        self.assertEqual(status["cancel"]["request_id"], request_id)

        repeated = _cancel(manifest)
        self.assertTrue(repeated["ok"])
        self.assertEqual(repeated["data"]["action"], "already_terminal")
        self.assertEqual(repeated["data"]["terminal"], "cancelled")
        repeated_again = _cancel(manifest)
        self.assertTrue(repeated_again["ok"])
        self.assertEqual(repeated_again["data"]["action"], "already_terminal")

    def test_new_generation_ignores_old_cancel_and_old_writer_cannot_finish(self) -> None:
        plan = _plan(self.root)
        manifest = _write_plan(self.root, plan)
        old = job_control.JobControl(manifest, owner="batch:old", plan=plan)
        old.start()
        self.assertTrue(_cancel(manifest)["ok"])

        new = job_control.JobControl(manifest, owner="resume:new", plan=plan)
        new.start()
        self.assertFalse(new.poll_cancel(force=True))
        status = _status(manifest)
        self.assertEqual(status["state"], "running")
        self.assertFalse(status["cancel"]["requested"])
        self.assertTrue(status["cancel"]["ignored_stale_request"])
        self.assertEqual(status["executor"]["generation"], new.generation)

        self.assertFalse(old.finish_after_unlock(job_control.TERMINAL_CANCELLED, plan=plan))
        self.assertEqual(_status(manifest)["state"], "running")
        self.assertTrue(_cancel(manifest)["ok"])
        self.assertTrue(new.poll_cancel(force=True))
        self.assertTrue(new.finish_after_unlock(job_control.TERMINAL_CANCELLED, plan=plan))
        self.assertEqual(_status(manifest)["terminal"], "cancelled")

    def test_legacy_pending_plan_has_no_active_executor(self) -> None:
        plan = _plan(self.root)
        manifest = _write_plan(self.root, plan)
        status = _status(manifest)
        self.assertEqual(status["state"], "interrupted")
        self.assertIsNone(status["terminal"])
        self.assertFalse(status["executor"]["active"])
        self.assertTrue(status["recoverable"])

        cancelled = _cancel(manifest)
        self.assertFalse(cancelled["ok"])
        self.assertEqual(cancelled["error"]["code"], "NO_ACTIVE_EXECUTOR")
        self.assertFalse(cancelled["data"]["requested"])
        self.assertIsNone(cancelled["data"]["terminal"])
        self.assertFalse(job_control.cancel_request_path(manifest).exists())

    def test_unknown_manifest_is_reported_without_creating_files(self) -> None:
        missing = self.root / "missing" / resume.PLAN_FILENAME
        status = job_control.status_manifest(missing).to_dict()
        self.assertFalse(status["ok"])
        self.assertEqual(status["error"]["code"], "MANIFEST_NOT_FOUND")
        cancelled = job_control.cancel_manifest(missing).to_dict()
        self.assertFalse(cancelled["ok"])
        self.assertEqual(cancelled["error"]["code"], "MANIFEST_NOT_FOUND")
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())

    def test_terminal_publication_skips_when_new_owner_holds_manifest_lock(self) -> None:
        plan = _plan(self.root)
        manifest = _write_plan(self.root, plan)
        old = job_control.JobControl(manifest, owner="old-writer", plan=plan)
        old.start()
        new_lock = resume.ManifestLock(manifest, owner="new-resume")
        new_lock.acquire()
        try:
            new = job_control.JobControl(manifest, owner="new-resume", plan=plan)
            new.start()
            self.assertFalse(old.finish_after_unlock(job_control.TERMINAL_SUCCESS, plan=plan))
            document = json.loads(job_control.control_path(manifest).read_text(encoding="utf-8"))
            self.assertEqual(document["generation"], new.generation)
            self.assertNotEqual(document["generation"], old.generation)
            self.assertIsNone(document["terminal"])
        finally:
            new_lock.release()
        # The old writer still must not replace the new generation after the
        # new owner releases the lock.
        self.assertFalse(old.finish_after_unlock(job_control.TERMINAL_SUCCESS, plan=plan))
        document = json.loads(job_control.control_path(manifest).read_text(encoding="utf-8"))
        self.assertEqual(document["generation"], new.generation)
        self.assertIsNone(document["terminal"])

    def test_terminal_publication_holds_manifest_lock_during_generation_compare(self) -> None:
        plan = _plan(self.root)
        manifest = _write_plan(self.root, plan)
        old = job_control.JobControl(manifest, owner="old-writer", plan=plan)
        old.start()
        attempted: dict[str, object] = {}
        read_document = job_control._read_document

        def read_then_try_new_owner(path, *args, **kwargs):
            result = read_document(path, *args, **kwargs)
            if Path(path) == old.control_path and "done" not in attempted:
                attempted["done"] = True
                contender = resume.ManifestLock(manifest, owner="new-resume")
                try:
                    contender.acquire()
                except resume.ResumeError as exc:
                    attempted["code"] = exc.code
                else:
                    attempted["acquired"] = True
                    contender.release()
            return result

        job_control._read_document = read_then_try_new_owner
        try:
            self.assertTrue(old.finish_after_unlock(job_control.TERMINAL_SUCCESS, plan=plan))
        finally:
            job_control._read_document = read_document
        self.assertEqual(attempted.get("code"), "RESUME_LOCKED")
        self.assertNotIn("acquired", attempted)
        document = json.loads(job_control.control_path(manifest).read_text(encoding="utf-8"))
        self.assertEqual(document["generation"], old.generation)
        self.assertEqual(document["terminal"], "success")

    def _cold_candidate(self, schema: str | None) -> dict:
        directory = self.root / f"cold-{schema or 'missing'}"
        directory.mkdir(parents=True, exist_ok=False, mode=0o777)
        output = directory / "output.blend"
        output.write_bytes(b"cold-evidence-output")
        output_sha = protocol.sha256_file(output)
        input_sha = "a" * 64
        cold: dict = {"passed": True, "reason_code": "VERIFICATION_PASSED"}
        if schema is not None:
            cold["evidence_schema"] = schema
        receipt = {
            "output_blend": str(output),
            "output_sha256": output_sha,
            "input_sha256_before": input_sha,
            "input_sha256_after": input_sha,
            "cold_reopen_verify": dict(cold),
        }
        receipt_path = directory / "receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        return {
            "status": "success",
            "success_attempt": 1,
            "input_sha256": input_sha,
            "normalized": {"verify_cold_reopen": True, "cache_expected": False},
            "attempts": [
                {
                    "attempt": 1,
                    "status": "success",
                    "job_id": "legacy-job",
                    "receipt": {"path": str(receipt_path), "sha256": protocol.sha256_file(receipt_path)},
                    "output": {"path": str(output), "sha256": output_sha},
                    "exports": [],
                    "cache": None,
                    "cold_reopen": dict(cold),
                }
            ],
        }

    def test_v1_cold_evidence_is_not_skipped_for_v2(self) -> None:
        v1 = resume.verify_success_attempt(self._cold_candidate("physics-geometry-evidence-v1"))
        self.assertFalse(v1["ok"])
        self.assertIn("cold_reopen_evidence_schema_mismatch", [reason["code"] for reason in v1["reasons"]])
        self.assertEqual(v1["evidence_schema"], "physics-geometry-evidence-v1")
        self.assertEqual(v1["required_evidence_schema"], "physics-geometry-evidence-v2")

        missing = resume.verify_success_attempt(self._cold_candidate(None))
        self.assertFalse(missing["ok"])
        self.assertIn("cold_reopen_evidence_schema_missing", [reason["code"] for reason in missing["reasons"]])

        current = resume.verify_success_attempt(self._cold_candidate("physics-geometry-evidence-v2"))
        self.assertTrue(current["ok"], current)
        self.assertEqual(current["required_evidence_schema"], "physics-geometry-evidence-v2")

    def test_worker_schema_cannot_replace_cold_verifier_declaration(self) -> None:
        candidate = self._cold_candidate("physics-geometry-evidence-v2")
        attempt = candidate["attempts"][0]
        path = Path(attempt["receipt"]["path"])
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt["cold_reopen_verify"].pop("evidence_schema")
        receipt["worker"] = {"verification_evidence": {"schema": "physics-geometry-evidence-v2"}}
        path.write_text(json.dumps(receipt), encoding="utf-8")
        attempt["receipt"]["sha256"] = protocol.sha256_file(path)
        result = resume.verify_success_attempt(candidate)
        self.assertFalse(result["ok"])
        self.assertIn("cold_verifier_evidence_schema_mismatch", [r["code"] for r in result["reasons"]])

    def test_current_evidence_schema_prefers_capabilities_field(self) -> None:
        class FakeCapabilities:
            @staticmethod
            def describe_capabilities():
                return {"physics_evidence_schema": "physics-geometry-evidence-v3"}

        with mock.patch.object(resume.deployment, "import_addon_module", return_value=FakeCapabilities):
            self.assertEqual(resume.current_evidence_schema(), "physics-geometry-evidence-v3")

    def test_cancel_before_first_candidate_returns_failure_and_exit_3(self) -> None:
        input_file = self.root / "input.blend"
        input_file.write_bytes(b"cancel-before-first-candidate")
        base = {
            "input_blend": str(input_file),
            "target_object": "Cube",
            "preset_id": "rigid_drop_pack",
            "parameters": {"frame_start": 1, "frame_end": 2},
        }
        original_start = job_control.JobControl.start

        def start_then_cancel(control_self):
            original_start(control_self)
            job_control.cancel_manifest(control_self.manifest)

        job_control.JobControl.start = start_then_cancel
        try:
            sweep = runner.run_sweep(
                {"base_request": base, "variants": [{"label": "a", "parameters": {}}]},
                self.root / "jobs-sweep",
            )
            batch = runner.run_batch({"items": [dict(base)]}, self.root / "jobs-batch")
        finally:
            job_control.JobControl.start = original_start
        for name, response in (("sweep", sweep), ("batch", batch)):
            self.assertFalse(response.ok, name)
            self.assertEqual((response.error or {}).get("code"), "CANCELLED", name)
            self.assertTrue(response.data.get("cancelled"), name)
            self.assertEqual(response.data.get("processed"), 0, name)
            self.assertEqual(cli._exit_code(response), protocol.EXIT_EXECUTION, name)

    def test_stale_control_is_interrupted_not_cancelled(self) -> None:
        plan = _plan(self.root)
        manifest = _write_plan(self.root, plan)
        control = job_control.JobControl(manifest, owner="batch:test", plan=plan)
        control.start()
        path = job_control.control_path(manifest)
        document = json.loads(path.read_text(encoding="utf-8"))
        old = (datetime.now(timezone.utc) - timedelta(seconds=job_control.ACTIVE_HEARTBEAT_SECONDS + 5)).isoformat()
        document["heartbeat_utc"] = old
        document["updated_utc"] = old
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        status = _status(manifest)
        self.assertEqual(status["state"], "interrupted")
        self.assertEqual(status["executor"]["health"], "stale")
        cancelled = _cancel(manifest)
        self.assertFalse(cancelled["ok"])
        self.assertEqual(cancelled["error"]["code"], "NO_ACTIVE_EXECUTOR")
        self.assertFalse(job_control.cancel_request_path(manifest).exists())

    def test_status_and_cancel_do_not_acquire_manifest_lock(self) -> None:
        plan = _plan(self.root)
        manifest = _write_plan(self.root, plan)
        lock = resume.ManifestLock(manifest, owner="test-writer")
        lock.acquire()
        control = None
        try:
            control = job_control.JobControl(manifest, owner="batch:test", plan=plan)
            control.start()
            status = _status(manifest)
            self.assertEqual(status["state"], "running")
            requested = _cancel(manifest)
            self.assertTrue(requested["ok"])
            self.assertTrue(control.poll_cancel(force=True))
        finally:
            lock.release()
        if control is not None:
            control.finish_after_unlock(job_control.TERMINAL_CANCELLED, plan=plan)
        # The cancellation terminal state is published after the lock release.
        self.assertEqual(_status(manifest)["terminal"], "cancelled")

    def test_pid_fields_in_control_or_request_are_never_signalled(self) -> None:
        plan = _plan(self.root)
        manifest = _write_plan(self.root, plan)
        control = job_control.JobControl(manifest, owner="batch:test", plan=plan)
        control.start()
        sentinel = subprocess.Popen(
            [sys.executable, "-B", "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            path = job_control.control_path(manifest)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["writer"]["pid"] = sentinel.pid
            document["pid"] = sentinel.pid
            document["target_pid"] = sentinel.pid
            path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            # A request with hostile/foreign PID fields is still bound by
            # request_id + manifest + plan_id + generation, and no PID is used.
            request = {
                "schema": job_control.REQUEST_SCHEMA,
                "version": job_control.REQUEST_VERSION,
                "request_id": "cancel-hostile-pid-test",
                "manifest": str(manifest),
                "plan_id": plan["plan_id"],
                "generation": control.generation,
                "created_utc": job_control.utc_now(),
                "pid": sentinel.pid,
                "target_pid": sentinel.pid,
                "process_tree": [sentinel.pid],
            }
            job_control._atomic_write_json(job_control.cancel_request_path(manifest), request)
            status = _status(manifest)
            self.assertEqual(status["state"], "cancel_requested")
            requested = _cancel(manifest)
            self.assertTrue(requested["ok"])
            self.assertEqual(requested["data"]["action"], "requested")
            self.assertEqual(requested["data"]["request_id"], "cancel-hostile-pid-test")
            self.assertTrue(control.poll_cancel(force=True))
            self.assertIsNone(sentinel.poll(), "cancel must never signal a PID from a request/control field")
        finally:
            sentinel.terminate()
            try:
                sentinel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sentinel.kill()
                sentinel.wait(timeout=5)


class ResourceGuardCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.root = TEST_TMP / f"guard-{uuid.uuid4().hex[:12]}"
        self.root.mkdir(parents=True, exist_ok=False, mode=0o777)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _child_with_grandchild(self, pid_file: Path) -> list[str]:
        code = (
            "import json,os,subprocess,sys,time\n"
            "child=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(60)'])\n"
            "open(sys.argv[1],'w',encoding='utf-8').write(json.dumps({'parent':os.getpid(),'child':child.pid}))\n"
            "time.sleep(60)\n"
        )
        return [sys.executable, "-B", "-c", code, str(pid_file)]

    @unittest.skipUnless(os.name == "nt", "process-tree assertion uses Windows handles")
    def test_cancel_closes_owned_process_tree(self) -> None:
        pid_file = self.root / "pids.json"
        command = self._child_with_grandchild(pid_file)
        deadline = time.monotonic() + 0.8
        started = time.monotonic()
        with self.assertRaises(resource_guard.ProcessCancelled) as ctx:
            resource_guard.run_guarded(
                command,
                timeout_seconds=60,
                memory_limit_mb=512,
                scratch_dir=self.root / "scratch",
                cancel_check=lambda: time.monotonic() >= deadline,
            )
        self.assertEqual(ctx.exception.code, "CANCELLED")
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(pid_file.is_file())
        pids = json.loads(pid_file.read_text(encoding="utf-8"))
        for pid in (pids["parent"], pids["child"]):
            self.assertTrue(_windows_process_exited(int(pid)), f"owned process {pid} survived cancellation")

    def test_timeout_semantics_unchanged(self) -> None:
        with self.assertRaises(resource_guard.ProcessTimeout) as ctx:
            resource_guard.run_guarded(
                [sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                timeout_seconds=1,
                memory_limit_mb=512,
                scratch_dir=self.root / "timeout-scratch",
            )
        self.assertEqual(ctx.exception.code, "TIMEOUT")

    @unittest.skipUnless(os.name == "nt", "hard job memory checks are Windows-only")
    def test_memory_limit_semantics_unchanged(self) -> None:
        code = "import time; a=bytearray(400*1024*1024); time.sleep(30)"
        with self.assertRaises(resource_guard.MemoryLimitExceeded) as ctx:
            resource_guard.run_guarded(
                [sys.executable, "-B", "-c", code],
                timeout_seconds=30,
                memory_limit_mb=128,
                scratch_dir=self.root / "memory-scratch",
            )
        self.assertEqual(ctx.exception.code, "MEMORY_LIMIT")


if __name__ == "__main__":
    unittest.main()
