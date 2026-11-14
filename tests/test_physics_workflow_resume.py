"""Real Blender integration tests for persistent plans and resume.

Every execution test launches the shipped Blender 5.2.1 binary through the
normal CLI/runner entrypoint.  Physical quality is out of scope here; these
tests only assert candidate planning, replay selection, artifact integrity and
lock/input-identity behaviour.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid


WORKSPACE = Path(__file__).resolve().parent.parent
TOOLS_DIR = WORKSPACE / "tools"
BLENDER = Path(
    os.environ.get(
        "BLENDER_PATH",
        str(WORKSPACE / "runtime/blender/releases/5.2.1/blender-5.2.1-windows-x64/blender.exe"),
    )
)
PYTHON = BLENDER.parent / "5.2" / "python" / "bin" / "python.exe"
CLI = TOOLS_DIR / "physics_workflow_cli_v1" / "cli.py"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from physics_workflow_cli_v1 import physics_workflow_public, protocol, resume, runner  # noqa: E402
from physics_nodes_workflow_addon import contract  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestPhysicsWorkflowResume(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not BLENDER.is_file():
            raise unittest.SkipTest(f"Blender binary missing: {BLENDER}")
        if not PYTHON.is_file():
            raise unittest.SkipTest(f"Blender Python missing: {PYTHON}")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        cls.root = Path(os.environ.get("PNW_TEST_OUTPUT", WORKSPACE / "test-output/physics-resume-tests")) / f"{stamp}-{uuid.uuid4().hex[:6]}"
        cls.root.mkdir(parents=True, exist_ok=False)
        cls.config = cls.root / "blender-config"
        cls.config.mkdir()
        os.environ["BLENDER_USER_CONFIG"] = str(cls.config)
        os.environ["PHYSICS_WORKFLOW_ADDON_DIR"] = str(TOOLS_DIR / "physics_nodes_workflow_addon")
        os.environ["BLENDER_PATH"] = str(BLENDER)

        cls.input_blend = cls.root / "input.blend"
        cls._run_scene_script(
            WORKSPACE / "tests" / "support" / "make_resume_scene.py",
            [cls.input_blend],
        )
        cls.input_sha = sha256(cls.input_blend)

        cls.linked_root = cls.root / "linked"
        cls.linked_root.mkdir()
        cls.linked_external = cls.linked_root / "external_collider.blend"
        cls.linked_input = cls.linked_root / "input_linked.blend"
        cls._run_scene_script(
            WORKSPACE / "tests" / "support" / "make_linked_resume_scene.py",
            [cls.linked_external, cls.linked_input],
        )
        cls.linked_input_sha = sha256(cls.linked_input)

    @classmethod
    def _run_scene_script(cls, script: Path, args: list[Path]) -> None:
        cmd = [
            str(BLENDER),
            "--background",
            "--factory-startup",
            "--disable-autoexec",
            "--python-exit-code",
            "1",
            "--python",
            str(script),
            "--",
            *[str(arg) for arg in args],
        ]
        proc = subprocess.run(
            cmd,
            cwd=str(WORKSPACE),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "scene creation failed\n"
                f"returncode={proc.returncode}\nstdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}"
            )

    @classmethod
    def _cli(cls, *args: str, timeout: int = 300) -> tuple[int, dict]:
        proc = subprocess.run(
            [str(PYTHON), str(CLI), *args],
            cwd=str(WORKSPACE),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"CLI did not emit JSON (exit {proc.returncode})\n"
                f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}"
            ) from exc
        return proc.returncode, payload

    def _base_request(self, *, bake: bool = False) -> dict:
        request = {
            "input_blend": str(self.input_blend),
            "operation": "build",
            "preset_id": "rigid_drop_pack",
            "target_object": "ResumeTarget",
            "frame_start": 1,
            "frame_end": 2,
            "parameters": {
                "scatter_count": 4,
                "substeps": 1,
                "contact_iterations": 1,
                "constraint_iterations": 1,
            },
        }
        if bake:
            request["bake"] = True
        return request

    def test_batch_skips_verified_success_and_retries_only_failure(self) -> None:
        case = self.root / "b"
        case.mkdir()
        conflict = case / "occupied.blend"
        conflict.write_bytes(b"external conflict")
        failing = self._base_request()
        failing["output_blend"] = str(conflict)
        batch = {"items": [self._base_request(), failing]}
        request_file = case / "batch_request.json"
        request_file.write_text(json.dumps(batch), encoding="utf-8")
        jobs_dir = case / "jobs"

        code, initial = self._cli(
            "batch",
            "--request",
            str(request_file),
            "--jobs-dir",
            str(jobs_dir),
            "--timeout-seconds",
            "120",
            "--memory-limit-mb",
            "2048",
        )
        self.assertEqual(code, protocol.EXIT_EXECUTION)
        self.assertFalse(initial["ok"])
        self.assertEqual([item["ok"] for item in initial["data"]["items"]], [True, False])
        self.assertEqual(initial["data"]["items"][1]["error"]["code"], "OUTPUT_EXISTS")
        plan_path = Path(initial["data"]["resume_plan"])
        self.assertTrue(plan_path.is_file())

        plan = resume.read_plan(plan_path)
        self.assertEqual([candidate["status"] for candidate in plan["candidates"]], ["success", "failed"])
        success_output = Path(plan["candidates"][0]["attempts"][-1]["output"]["path"])
        success_sha = sha256(success_output)

        conflict.unlink()
        public_result = physics_workflow_public.resume_workflow(
            plan_path,
            timeout_seconds=120,
            memory_limit_mb=2048,
        )
        self.assertTrue(public_result["ok"], public_result.get("error"))
        self.assertEqual(public_result["data"]["skipped"], 1)
        self.assertEqual(public_result["data"]["rerun"], 1)
        self.assertEqual(public_result["data"]["passed"], 2)
        self.assertEqual(public_result["data"]["failed"], 0)
        self.assertEqual([c["action"] for c in public_result["data"]["candidates"]], ["skipped", "rerun"])
        self.assertIn("resume_report", public_result["data"])
        self.assertTrue(Path(public_result["data"]["resume_report"]).is_file())

        plan = resume.read_plan(plan_path)
        self.assertEqual([candidate["status"] for candidate in plan["candidates"]], ["success", "success"])
        self.assertEqual(len(plan["candidates"][1]["attempts"]), 2)
        self.assertEqual(plan["candidates"][1]["attempts"][-1]["status"], "success")
        self.assertNotEqual(
            plan["candidates"][1]["attempts"][-1]["job_id"],
            plan["candidates"][1]["attempts"][0]["job_id"],
        )
        retry_output = Path(plan["candidates"][1]["attempts"][-1]["output"]["path"])
        self.assertTrue(retry_output.is_file())
        self.assertNotEqual(retry_output, conflict)
        self.assertEqual(sha256(success_output), success_sha, "verified success output was modified")
        self.assertEqual(sha256(self.input_blend), self.input_sha)

        code, second = self._cli(
            "resume",
            "--manifest",
            str(plan_path),
            "--timeout-seconds",
            "120",
            "--memory-limit-mb",
            "2048",
        )
        self.assertEqual(code, protocol.EXIT_OK)
        self.assertTrue(second["ok"])
        self.assertEqual(second["data"]["skipped"], 2)
        self.assertEqual(second["data"]["rerun"], 0)
        self.assertEqual(sha256(self.input_blend), self.input_sha)

    def test_sweep_repairs_missing_external_library_and_skips_success(self) -> None:
        case = self.root / "s"
        case.mkdir()
        base = {
            "input_blend": str(self.linked_input),
            "operation": "build",
            "preset_id": "rigid_drop_pack",
            "target_object": "ResumeTarget",
            "collider_object": "ExternalCollider",
            "frame_start": 1,
            "frame_end": 2,
            "parameters": {
                "scatter_count": 4,
                "substeps": 1,
                "contact_iterations": 1,
                "constraint_iterations": 1,
            },
        }
        sweep = {
            "base_request": base,
            "variants": [
                {"label": "no_collider", "collider_object": ""},
                {"label": "external_collider", "parameters": {"scatter_count": 3}},
            ],
        }

        away = self.linked_external.with_name(self.linked_external.name + ".away")
        self.linked_external.rename(away)
        try:
            initial = runner.run_sweep(
                sweep,
                case / "jobs",
                timeout_seconds=120,
                memory_limit_mb=2048,
            )
        finally:
            away.rename(self.linked_external)
        self.assertFalse(initial.ok)
        self.assertEqual([run["ok"] for run in initial.data["runs"]], [True, False])
        failure = initial.data["runs"][1]["error"]
        self.assertIn("Collider", failure["message"])
        plan_path = Path(initial.data["resume_plan"])
        plan = resume.read_plan(plan_path)
        self.assertEqual([candidate["status"] for candidate in plan["candidates"]], ["success", "failed"])
        self.assertEqual(sha256(self.linked_input), self.linked_input_sha)

        repaired = resume.resume_manifest(plan_path, timeout_seconds=120, memory_limit_mb=2048)
        self.assertTrue(repaired.ok, repaired.error)
        self.assertEqual(repaired.data["skipped"], 1)
        self.assertEqual(repaired.data["rerun"], 1)
        self.assertEqual(repaired.data["passed"], 2)
        self.assertEqual([c["action"] for c in repaired.data["candidates"]], ["skipped", "rerun"])
        plan = resume.read_plan(plan_path)
        self.assertEqual([candidate["status"] for candidate in plan["candidates"]], ["success", "success"])
        self.assertEqual(sha256(self.linked_input), self.linked_input_sha)

        again = resume.resume_manifest(plan_path, timeout_seconds=120, memory_limit_mb=2048)
        self.assertTrue(again.ok)
        self.assertEqual(again.data["skipped"], 2)
        self.assertEqual(again.data["rerun"], 0)

    def test_simulated_interruption_pending_candidate_is_replayed(self) -> None:
        case = self.root / "p"
        case.mkdir()
        batch = {"items": [self._base_request(), self._base_request()]}
        initial = runner.run_batch(
            batch,
            case / "jobs",
            timeout_seconds=120,
            memory_limit_mb=2048,
        )
        self.assertTrue(initial.ok, initial.error)
        plan_path = Path(initial.data["resume_plan"])
        plan = resume.read_plan(plan_path)
        second = plan["candidates"][1]
        resume.begin_attempt(
            second,
            "interrupted-sim",
            Path(plan["jobs_dir"]) / "interrupted-sim",
            {"output_blend": str(Path(plan["jobs_dir"]) / "interrupted-sim" / "output.blend")},
            None,
        )
        resume.write_plan(plan_path, plan)

        result = resume.resume_manifest(plan_path, timeout_seconds=120, memory_limit_mb=2048)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["skipped"], 1)
        self.assertEqual(result.data["rerun"], 1)
        self.assertEqual([c["action"] for c in result.data["candidates"]], ["skipped", "rerun"])
        plan = resume.read_plan(plan_path)
        self.assertEqual(plan["candidates"][1]["status"], "success")
        self.assertEqual(len(plan["candidates"][1]["attempts"]), 3)
        self.assertEqual(
            [attempt["status"] for attempt in plan["candidates"][1]["attempts"]],
            ["success", "pending", "success"],
        )
        self.assertEqual(sha256(self.input_blend), self.input_sha)

    def test_tampered_or_missing_outputs_are_not_skipped(self) -> None:
        case = self.root / "o"
        case.mkdir()
        batch = {"items": [self._base_request(), self._base_request()]}
        initial = runner.run_batch(
            batch,
            case / "jobs",
            timeout_seconds=120,
            memory_limit_mb=2048,
        )
        self.assertTrue(initial.ok, initial.error)
        plan_path = Path(initial.data["resume_plan"])
        plan = resume.read_plan(plan_path)
        self.assertEqual([candidate["status"] for candidate in plan["candidates"]], ["success"] * 2)

        tampered_output = Path(plan["candidates"][0]["attempts"][-1]["output"]["path"])
        tampered_bytes = tampered_output.read_bytes()
        tampered_output.write_bytes(tampered_bytes + b"tamper")

        missing_output = Path(plan["candidates"][1]["attempts"][-1]["output"]["path"])
        missing_output.unlink()

        result = resume.resume_manifest(plan_path, timeout_seconds=120, memory_limit_mb=2048)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["skipped"], 0)
        self.assertEqual(result.data["rerun"], 2)
        self.assertEqual(result.data["passed"], 2)
        reasons = [
            {reason["code"] for reason in (candidate.get("success_verification") or {}).get("reasons", [])}
            for candidate in result.data["candidates"]
        ]
        self.assertIn("output_sha256_mismatch", reasons[0])
        self.assertIn("output_missing", reasons[1])

        self.assertEqual(tampered_output.read_bytes(), tampered_bytes + b"tamper")
        self.assertFalse(missing_output.exists())

        plan = resume.read_plan(plan_path)
        self.assertEqual([candidate["status"] for candidate in plan["candidates"]], ["success"] * 2)
        for candidate in plan["candidates"]:
            new_output = Path(candidate["attempts"][-1]["output"]["path"])
            self.assertTrue(new_output.is_file())
        self.assertEqual(sha256(self.input_blend), self.input_sha)

        again = resume.resume_manifest(plan_path, timeout_seconds=120, memory_limit_mb=2048)
        self.assertTrue(again.ok)
        self.assertEqual(again.data["skipped"], 2)
        self.assertEqual(again.data["rerun"], 0)

    def test_tampered_export_is_not_skipped(self) -> None:
        case = self.root / "x"
        case.mkdir()
        export_path = case / "export" / "static.blend"
        request = self._base_request()
        request["export_format"] = "STATIC_MESH"
        request["export_path"] = str(export_path)
        request["export_frame"] = 2
        initial = runner.run_single(
            request,
            case / "jobs",
            timeout_seconds=120,
            memory_limit_mb=2048,
        )
        self.assertTrue(initial.ok, initial.error)
        plan_path = Path(initial.data["resume_plan"])
        plan = resume.read_plan(plan_path)
        exports = plan["candidates"][0]["attempts"][-1]["exports"]
        self.assertTrue(any(Path(item["path"]) == export_path for item in exports))
        original = export_path.read_bytes()
        export_path.write_bytes(original + b"export-tamper")

        result = resume.resume_manifest(plan_path, timeout_seconds=120, memory_limit_mb=2048)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["skipped"], 0)
        self.assertEqual(result.data["rerun"], 1)
        self.assertEqual(result.data["passed"], 1)
        reasons = {
            reason["code"]
            for reason in (result.data["candidates"][0].get("success_verification") or {}).get(
                "reasons", []
            )
        }
        self.assertIn("export_sha256_mismatch", reasons)
        self.assertEqual(export_path.read_bytes(), original + b"export-tamper")

        plan = resume.read_plan(plan_path)
        new_exports = plan["candidates"][0]["attempts"][-1]["exports"]
        self.assertTrue(any(Path(item["path"]) != export_path for item in new_exports))
        self.assertTrue(all(Path(item["path"]).is_file() for item in new_exports))
        self.assertEqual(sha256(self.input_blend), self.input_sha)

    def test_tampered_cache_inventory_is_not_skipped(self) -> None:
        case = self.root / "c"
        jobs_dir = case
        request = self._base_request(bake=True)
        request["verify_cold_reopen"] = True
        initial = runner.run_single(
            request,
            jobs_dir,
            timeout_seconds=120,
            memory_limit_mb=2048,
        )
        self.assertTrue(initial.ok, initial.error)
        plan_path = Path(initial.data["resume_plan"])
        plan = resume.read_plan(plan_path)
        candidate = plan["candidates"][0]
        self.assertEqual(candidate["status"], "success")
        cache = candidate["attempts"][-1]["cache"]
        self.assertIsNotNone(cache)
        cache_dir = Path(cache["generation_dir"])
        cache_blob = sorted(cache_dir.rglob("*.blob"))[0]
        cache_bytes = cache_blob.read_bytes()
        cache_blob.write_bytes(cache_bytes + b"cache-tamper")

        result = resume.resume_manifest(plan_path, timeout_seconds=120, memory_limit_mb=2048)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["skipped"], 0)
        self.assertEqual(result.data["rerun"], 1)
        self.assertEqual(result.data["passed"], 1)
        reasons = {
            reason["code"]
            for reason in (result.data["candidates"][0].get("success_verification") or {}).get(
                "reasons", []
            )
        }
        self.assertIn("cache_file_corrupt", reasons)
        self.assertEqual(cache_blob.read_bytes(), cache_bytes + b"cache-tamper")

        plan = resume.read_plan(plan_path)
        self.assertEqual(plan["candidates"][0]["status"], "success")
        new_output = Path(plan["candidates"][0]["attempts"][-1]["output"]["path"])
        self.assertTrue(new_output.is_file())
        self.assertEqual(sha256(self.input_blend), self.input_sha)

        again = resume.resume_manifest(plan_path, timeout_seconds=120, memory_limit_mb=2048)
        self.assertTrue(again.ok)
        self.assertEqual(again.data["skipped"], 1)
        self.assertEqual(again.data["rerun"], 0)

    def test_concurrent_resume_lock_and_plan_integrity(self) -> None:
        described = runner.describe()
        self.assertTrue(described.ok)
        self.assertEqual(described.data["resume"]["manifest_schema"], resume.PLAN_SCHEMA)
        self.assertFalse(described.data["resume"]["intermediate_frame_resume"])

        case = self.root / "l"
        plan_dir = case / "plan"
        jobs_dir = case / "jobs"
        plan_dir.mkdir(parents=True)
        jobs_dir.mkdir()
        request = self._base_request()
        candidate = resume.make_candidate("item_00", 0, "item_00", request)
        plan = resume.create_plan(
            "batch",
            {"items": [request]},
            "batch-lock-test",
            jobs_dir,
            plan_dir,
            [candidate],
            contract_id="BLENDER_PHYSICS_WORKFLOW_CONTRACT_V1",
        )
        plan_path = plan_dir / resume.PLAN_FILENAME
        resume.write_plan(plan_path, plan)
        before = plan_path.read_bytes()

        lock = resume.ManifestLock(plan_path, owner="test-holder")
        lock.acquire()
        try:
            code, locked = self._cli(
                "resume",
                "--manifest",
                str(plan_path),
                "--timeout-seconds",
                "30",
                "--memory-limit-mb",
                "1024",
            )
        finally:
            lock.release()
        self.assertEqual(code, protocol.EXIT_EXECUTION)
        self.assertEqual(locked["error"]["code"], "RESUME_LOCKED")
        self.assertEqual(Path(locked["error"]["details"]["lock_path"]), lock.lock_path)
        self.assertEqual(locked["error"]["details"]["owner"]["owner"], "test-holder")
        self.assertEqual(plan_path.read_bytes(), before)

        crash_code = (
            "import os, sys\n"
            f"sys.path.insert(0, r'{TOOLS_DIR}')\n"
            "from pathlib import Path\n"
            "from physics_workflow_cli_v1 import resume\n"
            f"lock = resume.ManifestLock(Path(r'{plan_path}'), owner='crash-holder')\n"
            "lock.acquire()\n"
            "print('ACQUIRED', flush=True)\n"
            "os._exit(0)\n"
        )
        crash = subprocess.run(
            [str(PYTHON), "-c", crash_code],
            cwd=str(WORKSPACE),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(crash.returncode, 0, crash.stderr[-2000:])
        self.assertIn("ACQUIRED", crash.stdout)
        recovered = resume.ManifestLock(plan_path, owner="after-crash")
        recovered.acquire()
        recovered.release()

        tampered = json.loads(before.decode("utf-8"))
        tampered["candidates"][0]["status"] = "success"
        plan_path.write_text(json.dumps(tampered, indent=2), encoding="utf-8")
        code, rejected = self._cli(
            "resume",
            "--manifest",
            str(plan_path),
            "--timeout-seconds",
            "30",
            "--memory-limit-mb",
            "1024",
        )
        self.assertEqual(code, protocol.EXIT_VALIDATION)
        self.assertEqual(rejected["error"]["code"], "MANIFEST_INTEGRITY_FAILED")

        relative = os.path.relpath(plan_path, WORKSPACE)
        code, relative_result = self._cli(
            "resume",
            "--manifest",
            relative,
            "--timeout-seconds",
            "30",
            "--memory-limit-mb",
            "1024",
        )
        self.assertEqual(code, protocol.EXIT_VALIDATION)
        self.assertEqual(relative_result["error"]["code"], "MANIFEST_PATH_NOT_ABSOLUTE")

    def test_input_change_is_rejected_without_taking_over_plan(self) -> None:
        case = self.root / "i"
        plan_dir = case / "plan"
        jobs_dir = case / "jobs"
        plan_dir.mkdir(parents=True)
        jobs_dir.mkdir()
        copied_input = case / "input_copy.blend"
        shutil.copyfile(self.input_blend, copied_input)
        request = self._base_request()
        request["input_blend"] = str(copied_input)
        candidate = resume.make_candidate("item_00", 0, "item_00", request)
        plan = resume.create_plan(
            "batch",
            {"items": [request]},
            "batch-input-change",
            jobs_dir,
            plan_dir,
            [candidate],
            contract_id="BLENDER_PHYSICS_WORKFLOW_CONTRACT_V1",
        )
        plan_path = plan_dir / resume.PLAN_FILENAME
        resume.write_plan(plan_path, plan)
        before = plan_path.read_bytes()
        original = copied_input.read_bytes()
        original_sha = hashlib.sha256(original).hexdigest()
        copied_input.write_bytes(original + b"changed-input")

        result = resume.resume_manifest(plan_path, timeout_seconds=30, memory_limit_mb=1024)
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "PLAN_INPUT_CHANGED")
        self.assertEqual(plan_path.read_bytes(), before)
        self.assertEqual(result.data, {})
        copied_input.write_bytes(original)
        self.assertEqual(sha256(copied_input), original_sha)

    def test_normalized_parameter_change_is_rejected(self) -> None:
        case = self.root / "q"
        plan_dir = case / "plan"
        jobs_dir = case / "jobs"
        plan_dir.mkdir(parents=True)
        jobs_dir.mkdir()
        request = self._base_request()
        normalized = contract.normalize_request(request)
        candidate = resume.make_candidate(
            "item_00",
            0,
            "item_00",
            request,
            normalized=normalized,
        )
        plan = resume.create_plan(
            "batch",
            {"items": [request]},
            "batch-parameter-change",
            jobs_dir,
            plan_dir,
            [candidate],
            contract_id=contract.CONTRACT_ID,
        )
        plan_path = plan_dir / resume.PLAN_FILENAME
        resume.write_plan(plan_path, plan)
        plan["candidates"][0]["normalized"]["parameters_hash"] = "0" * 64
        resume.write_plan(plan_path, plan)
        before = plan_path.read_bytes()

        result = resume.resume_manifest(plan_path, timeout_seconds=30, memory_limit_mb=1024)
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "PLAN_PARAMETERS_CHANGED")
        self.assertEqual(plan_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
