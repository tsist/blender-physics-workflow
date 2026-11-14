"""Actual process-tree resource guards for Blender worker/verifier launches.

Windows uses a Job Object with a hard committed-memory limit and
``KILL_ON_JOB_CLOSE``.  A small gated bootstrap is assigned to the Job Object
before it launches Blender, so the Blender process and any children inherit the
limit.  A monitor terminates the whole Job Object when committed memory or the
wall-clock timeout is exceeded.

POSIX uses ``RLIMIT_AS`` when available and a process-group kill on timeout.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Callable
import uuid


class ResourceGuardError(RuntimeError):
    code = "RESOURCE_GUARD_ERROR"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_error(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return error


class ProcessTimeout(ResourceGuardError):
    code = "TIMEOUT"


class MemoryLimitExceeded(ResourceGuardError):
    code = "MEMORY_LIMIT"


class ProcessCancelled(ResourceGuardError):
    """The owned process tree was closed because its executor saw a cancel request."""

    code = "CANCELLED"


@dataclass
class GuardedResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    timeout_seconds: int
    memory_limit_bytes: int | None
    enforcement: str
    peak_job_memory_bytes: int | None = None
    peak_process_memory_bytes: int | None = None
    timed_out: bool = False
    memory_limit_hit: bool = False


def _read_temp(file) -> str:
    file.seek(0)
    data = file.read()
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _command_with_bootstrap(command: list[str], gate: Path) -> list[str]:
    bootstrap = Path(__file__).resolve().with_name("resource_bootstrap.py")
    return [sys.executable, "-B", str(bootstrap), str(gate), *command]


def run_guarded(
    command: list[str],
    *,
    timeout_seconds: int,
    memory_limit_mb: int | None = 4096,
    scratch_dir: str | os.PathLike[str] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> GuardedResult:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        raise ResourceGuardError("timeout_seconds must be a positive integer")
    memory_limit_bytes = None
    if memory_limit_mb not in (None, 0):
        if isinstance(memory_limit_mb, bool) or not isinstance(memory_limit_mb, int) or memory_limit_mb < 0:
            raise ResourceGuardError("memory_limit_mb must be a non-negative integer")
        memory_limit_bytes = int(memory_limit_mb) * 1024 * 1024
    if os.name == "nt":
        return _run_windows(command, timeout_seconds, memory_limit_bytes, scratch_dir, cancel_check)
    return _run_posix(command, timeout_seconds, memory_limit_bytes, scratch_dir, cancel_check)


if os.name == "nt":
    from ctypes import wintypes as _w

    class _JobBasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", _w.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", _w.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", _w.DWORD),
            ("SchedulingClass", _w.DWORD),
        ]

    class _JobIoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JobExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobBasicLimit),
            ("IoInfo", _JobIoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _JobProcessIdList(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", _w.DWORD),
            ("NumberOfProcessIdsInList", _w.DWORD),
            ("ProcessIdList", ctypes.c_size_t * 1024),
        ]

    class _ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", _w.DWORD),
            ("PageFaultCount", _w.DWORD),
        ] + [
            (name, ctypes.c_size_t)
            for name in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
                "PrivateUsage",
            )
        ]

    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    _JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_VM_READ = 0x0010
    # Windows raises these when a process cannot commit memory.  A hard job
    # limit can deny the allocation without ever growing PeakJobMemoryUsed to
    # the configured limit, so the exit status is the better evidence.
    _WINDOWS_MEMORY_EXIT_CODES = {
        0xC0000017,  # STATUS_NO_MEMORY
        0xC000009A,  # STATUS_INSUFFICIENT_RESOURCES
        0xC000012D,  # STATUS_COMMITMENT_LIMIT
    }

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.CreateJobObjectW.argtypes = [ctypes.c_void_p, _w.LPCWSTR]
    _KERNEL32.CreateJobObjectW.restype = _w.HANDLE
    _KERNEL32.SetInformationJobObject.argtypes = [_w.HANDLE, ctypes.c_int, ctypes.c_void_p, _w.DWORD]
    _KERNEL32.SetInformationJobObject.restype = _w.BOOL
    _KERNEL32.QueryInformationJobObject.argtypes = [
        _w.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        _w.DWORD,
        ctypes.c_void_p,
    ]
    _KERNEL32.QueryInformationJobObject.restype = _w.BOOL
    _KERNEL32.AssignProcessToJobObject.argtypes = [_w.HANDLE, _w.HANDLE]
    _KERNEL32.AssignProcessToJobObject.restype = _w.BOOL
    _KERNEL32.TerminateJobObject.argtypes = [_w.HANDLE, _w.UINT]
    _KERNEL32.TerminateJobObject.restype = _w.BOOL
    _KERNEL32.CloseHandle.argtypes = [_w.HANDLE]
    _KERNEL32.CloseHandle.restype = _w.BOOL
    _KERNEL32.OpenProcess.argtypes = [_w.DWORD, _w.BOOL, _w.DWORD]
    _KERNEL32.OpenProcess.restype = _w.HANDLE

    _PSAPI = ctypes.WinDLL("psapi", use_last_error=True)
    _PSAPI.GetProcessMemoryInfo.argtypes = [_w.HANDLE, ctypes.c_void_p, _w.DWORD]
    _PSAPI.GetProcessMemoryInfo.restype = _w.BOOL

    def _create_job(memory_limit_bytes: int | None):
        handle = _KERNEL32.CreateJobObjectW(None, None)
        if not handle:
            raise ResourceGuardError("CreateJobObjectW failed", winerror=ctypes.get_last_error())
        limits = _JobExtendedLimit()
        flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_limit_bytes is not None:
            flags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY | _JOB_OBJECT_LIMIT_JOB_MEMORY
            limits.ProcessMemoryLimit = memory_limit_bytes
            limits.JobMemoryLimit = memory_limit_bytes
        limits.BasicLimitInformation.LimitFlags = flags
        if not _KERNEL32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            _KERNEL32.CloseHandle(handle)
            raise ResourceGuardError("SetInformationJobObject failed", winerror=error)
        return handle

    def _job_peak(handle) -> int | None:
        info = _JobExtendedLimit()
        if not _KERNEL32.QueryInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            return None
        return int(info.PeakJobMemoryUsed)

    def _job_memory(handle) -> tuple[int, int, int]:
        """Return (private_committed_sum, rss_sum, sampled_process_count)."""

        ids = _JobProcessIdList()
        if not _KERNEL32.QueryInformationJobObject(
            handle,
            _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
            ctypes.byref(ids),
            ctypes.sizeof(ids),
            None,
        ):
            return 0, 0, 0
        private_sum = 0
        rss_sum = 0
        sampled = 0
        for index in range(min(int(ids.NumberOfProcessIdsInList), 1024)):
            pid = int(ids.ProcessIdList[index])
            process = _KERNEL32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ,
                False,
                pid,
            )
            if not process:
                continue
            try:
                counters = _ProcessMemoryCountersEx()
                counters.cb = ctypes.sizeof(counters)
                if _PSAPI.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
                    private_sum += int(counters.PrivateUsage)
                    rss_sum += int(counters.WorkingSetSize)
                    sampled += 1
            finally:
                _KERNEL32.CloseHandle(process)
        return private_sum, rss_sum, sampled

    def _run_windows(
        command: list[str],
        timeout_seconds: int,
        memory_limit_bytes: int | None,
        scratch_dir: str | os.PathLike[str] | None,
        cancel_check: Callable[[], bool] | None,
    ) -> GuardedResult:
        job = _create_job(memory_limit_bytes)
        process = None
        work_dir = None
        try:
            base = Path(scratch_dir) if scratch_dir is not None else Path.cwd() / ".pnw-resource"
            base.mkdir(parents=True, exist_ok=True)
            work_dir = base / f"run-{uuid.uuid4().hex}"
            work_dir.mkdir(parents=False, exist_ok=False)
            gate = work_dir / "launch.gate"
            stdout_path = work_dir / "stdout.log"
            stderr_path = work_dir / "stderr.log"
            wrapper_command = _command_with_bootstrap(command, gate)
            with stdout_path.open("w+b") as stdout_file, stderr_path.open("w+b") as stderr_file:
                process = subprocess.Popen(
                    wrapper_command,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if not _KERNEL32.AssignProcessToJobObject(job, _w.HANDLE(int(process._handle))):
                    error = ctypes.get_last_error()
                    process.kill()
                    process.wait(timeout=10)
                    raise ResourceGuardError(
                        "AssignProcessToJobObject failed",
                        winerror=error,
                        pid=process.pid,
                    )
                # A request that arrived while the gated bootstrap was being
                # assigned must not be allowed to launch Blender at all.
                if cancel_check is not None and cancel_check():
                    _KERNEL32.TerminateJobObject(job, 7)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                    raise ProcessCancelled(
                        "Process tree cancelled before Blender launch",
                        pid=process.pid,
                        termination="job_object",
                    )
                gate.write_text("owned job assigned; Blender launch allowed\n", encoding="utf-8")

                started = time.monotonic()
                monitor_peak = 0
                timed_out = False
                memory_limit_hit = False
                cancelled = False
                while process.poll() is None:
                    if time.monotonic() - started > timeout_seconds:
                        timed_out = True
                        _KERNEL32.TerminateJobObject(job, 7)
                        break
                    if memory_limit_bytes is not None:
                        private_sum, _, _ = _job_memory(job)
                        if private_sum > monitor_peak:
                            monitor_peak = private_sum
                        if private_sum > memory_limit_bytes:
                            memory_limit_hit = True
                            _KERNEL32.TerminateJobObject(job, 7)
                            break
                    if cancel_check is not None and cancel_check():
                        cancelled = True
                        _KERNEL32.TerminateJobObject(job, 7)
                        break
                    time.sleep(0.05)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                peak_job = _job_peak(job)
                stdout = _read_temp(stdout_file)
                stderr = _read_temp(stderr_file)

            if (
                memory_limit_bytes is not None
                and process.returncode is not None
                and (int(process.returncode) & 0xFFFFFFFF) in _WINDOWS_MEMORY_EXIT_CODES
            ):
                memory_limit_hit = True
            if memory_limit_hit or (
                memory_limit_bytes is not None
                and process.returncode != 0
                and (
                    (peak_job is not None and peak_job >= memory_limit_bytes)
                    or monitor_peak >= memory_limit_bytes
                    or any(
                        marker in (stdout + "\n" + stderr).lower()
                        for marker in (
                            "memoryerror",
                            "bad_alloc",
                            "cannot allocate memory",
                            "out of memory",
                            "not enough memory",
                        )
                    )
                )
            ):
                raise MemoryLimitExceeded(
                    f"Process tree exceeded the hard memory limit ({memory_limit_bytes} bytes)",
                    memory_limit_bytes=memory_limit_bytes,
                    peak_job_memory_bytes=peak_job,
                    monitor_peak_bytes=monitor_peak,
                    returncode=process.returncode,
                    stderr_tail=stderr[-1500:],
                )
            if timed_out:
                raise ProcessTimeout(
                    f"Process tree timed out after {timeout_seconds}s",
                    timeout_seconds=timeout_seconds,
                    returncode=process.returncode,
                    stderr_tail=stderr[-1500:],
                )
            if cancelled:
                raise ProcessCancelled(
                    "Process tree closed after a bound cancel request",
                    returncode=process.returncode,
                    peak_job_memory_bytes=peak_job,
                    monitor_peak_bytes=monitor_peak,
                    stderr_tail=stderr[-1500:],
                    termination="job_object",
                )
            return GuardedResult(
                args=list(command),
                returncode=int(process.returncode),
                stdout=stdout,
                stderr=stderr,
                timeout_seconds=timeout_seconds,
                memory_limit_bytes=memory_limit_bytes,
                enforcement="windows_job_object_process+job_memory_with_kill_on_close",
                peak_job_memory_bytes=peak_job,
                peak_process_memory_bytes=monitor_peak or None,
                timed_out=False,
                memory_limit_hit=False,
            )
        finally:
            if process is not None and process.poll() is None:
                try:
                    _KERNEL32.TerminateJobObject(job, 7)
                except Exception:
                    pass
            _KERNEL32.CloseHandle(job)
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)


def _run_posix(
    command: list[str],
    timeout_seconds: int,
    memory_limit_bytes: int | None,
    scratch_dir: str | os.PathLike[str] | None,
    cancel_check: Callable[[], bool] | None,
) -> GuardedResult:
    preexec_fn = None
    if memory_limit_bytes is not None:
        try:
            import resource as posix_resource
        except ImportError as exc:
            raise ResourceGuardError("Hard memory limits are unavailable on this platform") from exc

        def limit_memory() -> None:
            posix_resource.setrlimit(posix_resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))

        preexec_fn = limit_memory

    base = Path(scratch_dir) if scratch_dir is not None else Path.cwd() / ".pnw-resource"
    base.mkdir(parents=True, exist_ok=True)
    work_dir = base / f"run-{uuid.uuid4().hex}"
    work_dir.mkdir(parents=False, exist_ok=False)
    stdout_path = work_dir / "stdout.log"
    stderr_path = work_dir / "stderr.log"
    with stdout_path.open("w+b") as stdout_file, stderr_path.open("w+b") as stderr_file:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
            preexec_fn=preexec_fn,
        )
        timed_out = False
        cancelled = False
        started = time.monotonic()
        while process.poll() is None:
            if time.monotonic() - started > timeout_seconds:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            if cancel_check is not None and cancel_check():
                cancelled = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            time.sleep(0.05)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        stdout = _read_temp(stdout_file)
        stderr = _read_temp(stderr_file)
    shutil.rmtree(work_dir, ignore_errors=True)
    if timed_out:
        raise ProcessTimeout(
            f"Process tree timed out after {timeout_seconds}s",
            timeout_seconds=timeout_seconds,
            returncode=process.returncode,
            stderr_tail=stderr[-1500:],
        )
    if cancelled:
        raise ProcessCancelled(
            "Process group closed after a bound cancel request",
            returncode=process.returncode,
            stderr_tail=stderr[-1500:],
            termination="process_group",
        )
    return GuardedResult(
        args=list(command),
        returncode=int(process.returncode),
        stdout=stdout,
        stderr=stderr,
        timeout_seconds=timeout_seconds,
        memory_limit_bytes=memory_limit_bytes,
        enforcement="posix_rlimit_as_and_process_group"
        if memory_limit_bytes is not None
        else "posix_process_group",
        timed_out=False,
        memory_limit_hit=False,
    )
