"""Process-level tests that original run/sweep/batch writers own their plan lock.

Each test starts the normal CLI in a separate process, waits until its Blender
worker is actually running, starts a second process that attempts resume, and
then verifies that the original writer completed its plan and that resume works
after the original process exits.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
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

from physics_workflow_cli_v1 import protocol, resume  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestResumeWriterLock(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not BLENDER.is_file():
            raise unittest.SkipTest(f"Blender binary missing: {BLENDER}")
        if not PYTHON.is_file():
            raise unittest.SkipTest(f"Blender Python missing: {PYTHON}")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        test_root = Path(os.environ.get("PNW_TEST_OUTPUT", WORKSPACE / "test-output/physics-resume-tests"))
        cls.root = test_root / f"{stamp}-{uuid.uuid4().hex[:6]}"
        cls.root.mkdir(parents=True, exist_ok=False)
        cls.config = cls.root / "cfg"
        cls.config.mkdir()
        os.environ["BLENDER_USER_CONFIG"] = str(cls.config)
        os.environ["PHYSICS_WORKFLOW_ADDON_DIR"] = str(TOOLS_DIR / "physics_nodes_workflow_addon")
        os.environ["BLENDER_PATH"] = str(BLENDER)

        previous = sorted(
            test_root.glob("*/input.blend"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if previous:
            cls.input_blend = previous[0]
        else:
            cls.input_blend = cls.root / "input.blend"
            cls._create_scene()
        cls.input_sha = sha256(cls.input_blend)

    @classmethod
    def _create_scene(cls) -> None:
        cmd = [
            str(BLENDER),
            "--background",
            "--factory-startup",
            "--disable-autoexec",
            "--python-exit-code",
            "1",
            "--python",
            str(WORKSPACE / "tests" / "support" / "make_resume_scene.py"),
            "--",
            str(cls.input_blend),
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
            raise RuntimeError(proc.stdout[-2000:] + "\n" + proc.stderr[-2000:])

    def _request(self) -> dict:
        return {
            "input_blend": str(self.input_blend),
            "operation": "build",
            "preset_id": "rigid_drop_pack",
            "target_object": "ResumeTarget",
            "frame_start": 1,
            "frame_end": 240,
            "parameters": {
                "scatter_count": 32,
                "substeps": 2,
                "contact_iterations": 2,
                "constraint_iterations": 4,
            },
        }

    def _spawn_original(
        self,
        case: Path,
        subcommand: str,
        payload: dict,
    ) -> tuple[subprocess.Popen, Path, Path]:
        case.mkdir(parents=True)
        request_path = case / "request.json"
        request_path.write_text(json.dumps(payload), encoding="utf-8")
        stdout_path = case / "original_stdout.json"
        stderr_path = case / "original_stderr.log"
        cmd = [
            str(PYTHON),
            str(CLI),
            subcommand,
            "--request",
            str(request_path),
            "--jobs-dir",
            str(case / "jobs"),
            "--timeout-seconds",
            "300",
            "--memory-limit-mb",
            "2048",
        ]
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            proc = subprocess.Popen(
                cmd,
                cwd=str(WORKSPACE),
                env=os.environ.copy(),
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
        return proc, stdout_path, stderr_path

    def _find_plan(self, proc: subprocess.Popen, jobs_dir: Path, pattern: str) -> Path:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(
                    f"original process exited before a plan was observed (exit {proc.returncode})"
                )
            matches = sorted(jobs_dir.glob(pattern))
            for candidate in matches:
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate
            time.sleep(0.02)
        raise AssertionError(f"plan was not created: {jobs_dir / pattern}")

    def _wait_for_running_worker(self, proc: subprocess.Popen, plan_path: Path) -> Path:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(
                    f"original process exited before its worker was observed (exit {proc.returncode})"
                )
            try:
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                time.sleep(0.02)
                continue
            for candidate in plan.get("candidates") or []:
                if not isinstance(candidate, dict):
                    continue
                for attempt in reversed(candidate.get("attempts") or []):
                    if not isinstance(attempt, dict) or attempt.get("status") != "pending":
                        continue
                    job_dir = Path(str(attempt.get("job_dir")))
                    if (
                        (job_dir / "worker_request.json").is_file()
                        and (job_dir / ".resource").is_dir()
                        and not (job_dir / "worker_result.json").exists()
                        and not (job_dir / "receipt.json").exists()
                    ):
                        return job_dir
            time.sleep(0.02)
        raise AssertionError("worker did not enter the observed running state")

    def _resume_cli(self, plan_path: Path, output_path: Path, timeout: int = 120) -> tuple[int, dict, str]:
        proc = subprocess.run(
            [
                str(PYTHON),
                str(CLI),
                "resume",
                "--manifest",
                str(plan_path),
                "--timeout-seconds",
                "300",
                "--memory-limit-mb",
                "2048",
            ],
            cwd=str(WORKSPACE),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output_path.write_text(proc.stdout, encoding="utf-8")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"resume did not emit JSON (exit {proc.returncode})\n"
                f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}"
            ) from exc
        return proc.returncode, payload, proc.stderr

    def _exercise(self, subcommand: str, pattern: str) -> None:
        case = self.root / subcommand
        request = self._request()
        if subcommand == "sweep":
            payload = {"base_request": request, "variants": [{"label": "slow"}]}
        elif subcommand == "batch":
            payload = {"items": [request]}
        else:
            payload = request

        proc, stdout_path, stderr_path = self._spawn_original(case, subcommand, payload)
        try:
            jobs_dir = case / "jobs"
            plan_path = self._find_plan(proc, jobs_dir, pattern)
            running_job_dir = self._wait_for_running_worker(proc, plan_path)
            self.assertTrue((running_job_dir / "worker_request.json").is_file())
            self.assertFalse((running_job_dir / "worker_result.json").exists())
            before_retry_dirs = {path.name for path in jobs_dir.rglob("retry-*")}
            self.assertEqual(before_retry_dirs, set())

            locked_code, locked, locked_stderr = self._resume_cli(
                plan_path, case / "resume_locked.json"
            )
            self.assertEqual(locked_code, protocol.EXIT_EXECUTION, locked_stderr[-2000:])
            self.assertFalse(locked["ok"])
            self.assertEqual(locked["error"]["code"], "RESUME_LOCKED")
            self.assertEqual(
                Path(locked["error"]["details"]["lock_path"]),
                resume.manifest_lock_path(plan_path),
            )
            self.assertEqual({path.name for path in jobs_dir.rglob("retry-*")}, set())

            returncode = proc.wait(timeout=300)
            stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
            self.assertEqual(returncode, 0, stderr_text[-4000:])
            original = json.loads(stdout_path.read_text(encoding="utf-8"))
            self.assertTrue(original["ok"], original.get("error"))

            final_plan = resume.read_plan(plan_path)
            candidate = final_plan["candidates"][0]
            self.assertEqual(candidate["status"], "success")
            self.assertEqual(candidate["attempts"][-1]["status"], "success")
            output = Path(candidate["attempts"][-1]["output"]["path"])
            receipt = Path(candidate["attempts"][-1]["receipt"]["path"])
            self.assertTrue(output.is_file())
            self.assertTrue(receipt.is_file())
            self.assertEqual(sha256(output), candidate["attempts"][-1]["output"]["sha256"])
            self.assertEqual(sha256(receipt), candidate["attempts"][-1]["receipt"]["sha256"])
            if subcommand in {"sweep", "batch"}:
                report = Path(final_plan["report"]["path"])
                self.assertTrue(report.is_file())
            self.assertEqual(sha256(self.input_blend), self.input_sha)

            after_code, after, after_stderr = self._resume_cli(
                plan_path, case / "resume_after_exit.json"
            )
            self.assertEqual(after_code, protocol.EXIT_OK, after_stderr[-2000:])
            self.assertTrue(after["ok"], after.get("error"))
            self.assertEqual(after["data"]["skipped"], 1)
            self.assertEqual(after["data"]["rerun"], 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)

    def test_run_holds_manifest_lock_during_worker(self) -> None:
        self._exercise("run", "run-*/resume_plan.json")

    def test_sweep_holds_manifest_lock_during_worker(self) -> None:
        self._exercise("sweep", "sweep-*/resume_plan.json")

    def test_batch_holds_manifest_lock_during_worker(self) -> None:
        self._exercise("batch", "batch-*/resume_plan.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
