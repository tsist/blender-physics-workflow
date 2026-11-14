"""Real Windows reader-handle tests for atomic JSON replacement."""

from __future__ import annotations

from datetime import datetime, timezone
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

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from physics_workflow_cli_v1 import resume  # noqa: E402


_READER_CODE = r"""
import sys
import time

path, ready, hold = sys.argv[1], sys.argv[2], float(sys.argv[3])
handle = open(path, "rb")
try:
    open(ready, "w", encoding="utf-8").write("ready")
    time.sleep(hold)
finally:
    handle.close()
"""


class TestAtomicReplaceWithWindowsReader(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.name != "nt":
            raise unittest.SkipTest("Windows-specific replacement conflict test")
        if not PYTHON.is_file():
            raise unittest.SkipTest(f"Blender Python missing: {PYTHON}")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        cls.root = Path(os.environ.get("PNW_TEST_OUTPUT", WORKSPACE / "test-output/physics-resume-tests")) / f"{stamp}-{uuid.uuid4().hex[:6]}" / "atomic"
        cls.root.mkdir(parents=True, exist_ok=False)

    def _start_reader(self, target: Path, ready: Path, hold_seconds: float) -> subprocess.Popen:
        proc = subprocess.Popen(
            [str(PYTHON), "-c", _READER_CODE, str(target), str(ready), str(hold_seconds)],
            cwd=str(WORKSPACE),
            env=os.environ.copy(),
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if ready.is_file():
                return proc
            if proc.poll() is not None:
                raise AssertionError(f"reader exited before ready (exit {proc.returncode})")
            time.sleep(0.01)
        proc.kill()
        proc.wait(timeout=10)
        raise AssertionError("reader did not open the target in time")

    def _temp_files(self, target: Path) -> list[Path]:
        return sorted(target.parent.glob(f".{target.name}.*.tmp"))

    def test_transient_reader_release_allows_complete_atomic_json(self) -> None:
        case = self.root / "short"
        case.mkdir()
        target = case / "resume_plan.json"
        target.write_text('{"original": true}\n', encoding="utf-8")
        ready = case / "ready"
        reader = self._start_reader(target, ready, 0.5)
        payload = {"plan": "short-hold", "candidates": [{"status": "success"}]}
        started = time.monotonic()
        try:
            resume._atomic_write_json(target, payload)
        finally:
            reader.wait(timeout=20)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), payload)
        self.assertEqual(self._temp_files(target), [])

    def test_long_reader_hold_fails_within_bound_and_cleans_temp(self) -> None:
        case = self.root / "long"
        case.mkdir()
        target = case / "resume_plan.json"
        original = '{"original": true}\n'
        target.write_text(original, encoding="utf-8")
        ready = case / "ready"
        reader = self._start_reader(target, ready, 5.0)
        payload = {"plan": "long-hold", "candidates": [{"status": "success"}]}
        started = time.monotonic()
        try:
            with self.assertRaises(OSError) as ctx:
                resume._atomic_write_json(target, payload)
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 1.0)
            self.assertLess(elapsed, 4.5)
            self.assertEqual(getattr(ctx.exception, "winerror", None), 5)
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertEqual(self._temp_files(target), [])
        finally:
            reader.wait(timeout=20)

        # After the independent reader releases, the same private-temp atomic
        # path must recover without touching the destination in place.
        resume._atomic_write_json(target, payload)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), payload)
        self.assertEqual(self._temp_files(target), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
