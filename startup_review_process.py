"""Run one hidden Windows process tree inside an owned, bounded Job Object.

Ordinary descendants remain in the job and are killed when this call ends,
including after a successful root-process exit. Explicit CREATE_BREAKAWAY_FROM_JOB
launches may escape: callers must reserve that flag for deliberate GUI recovery.
This is process cleanup, not a security boundary against untrusted programs.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import os
import subprocess
import tempfile
import time


_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_REAP_TIMEOUT = 5.0
_GUARD_INTERVAL = 2.0


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_int64),
        ('PerJobUserTimeLimit', ctypes.c_int64),
        ('LimitFlags', wintypes.DWORD),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', wintypes.DWORD),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', wintypes.DWORD),
        ('SchedulingClass', wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64) for name in (
            'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
            'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount',
        )
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', _BasicLimitInformation),
        ('IoInfo', _IoCounters),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


class _WindowsJob:
    def __init__(self):
        self.handle = None
        self._kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self._ntdll = ctypes.WinDLL('ntdll', use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self._ntdll.NtResumeProcess.restype = wintypes.LONG
        self._ntdll.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
        self._ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG

        # No name or inheritable security attributes: only this owner holds the job.
        self.handle = self._kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self._set_limits()
        except BaseException:
            self.close()
            raise

    def _set_limits(self):
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )
        if not self._kernel32.SetInformationJobObject(
                self.handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        # CPython keeps the CreateProcess handle; using it avoids reopening by PID.
        if not self._kernel32.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def resume(self, process):
        # Popen closes the primary thread handle, so resume by its owned process handle.
        status = self._ntdll.NtResumeProcess(int(process._handle))
        if status < 0:
            raise ctypes.WinError(self._ntdll.RtlNtStatusToDosError(status))

    def close(self):
        if self.handle is not None:
            if not self._kernel32.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None


def _check_guard(guard):
    if guard is not None:
        try:
            allowed = guard()
        except Exception as exc:
            raise RuntimeError('process guard failed') from exc
        if not allowed:
            raise RuntimeError('process guard rejected continued execution')


def run_bounded(command, *, input_bytes, stdout, stderr, cwd, env, timeout, guard=None) -> int:
    """Run a command once and return its exit code, including nonzero exit codes.

    Windows/CPython only. ``stdout`` and ``stderr`` are caller-owned open binary
    files. ``input_bytes`` is supplied as file-backed stdin, avoiding Windows'
    synchronous PIPE writes bypassing ``communicate``'s timeout. No shell is used.

    ``timeout`` must be finite and positive. It covers setup and execution after
    input is spooled; OS process creation itself cannot be interrupted. Timeout
    raises subprocess.TimeoutExpired after closing the job and reaping the root
    (up to five additional seconds for OS termination).
    Other setup/runtime errors propagate after the same cleanup. No process is
    resumed until job assignment succeeds. Caller-owned output files stay open.

    Optional ``guard`` must be a fast, synchronous local check returning truthy
    to continue. It runs before resume, then at most once every two seconds while
    waiting. A false result or exception raises RuntimeError and kills this job.
    Guard checks consume the original deadline; they never extend it. Like OS
    process creation, a blocking guard cannot itself be interrupted here.
    """
    if os.name != 'nt':
        raise OSError('run_bounded requires Windows')
    if not isinstance(input_bytes, bytes):
        raise TypeError('input_bytes must be bytes')
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be finite and positive')

    with tempfile.TemporaryFile(mode='w+b') as source:
        source.write(input_bytes)
        source.seek(0)
        deadline = time.monotonic() + timeout
        job = _WindowsJob()
        process = None
        try:
            process = subprocess.Popen(
                command, stdin=source, stdout=stdout, stderr=stderr,
                cwd=cwd, env=env, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW | _CREATE_SUSPENDED,
            )
            job.assign(process)
            _check_guard(guard)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            job.resume(process)
            next_guard = time.monotonic() + _GUARD_INTERVAL
            while True:
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                wait_timeout = remaining
                if guard is not None:
                    wait_timeout = min(wait_timeout, max(0.0, next_guard - now))
                try:
                    process.communicate(timeout=wait_timeout)
                except subprocess.TimeoutExpired as exc:
                    now = time.monotonic()
                    if guard is None or now >= deadline:
                        # Present the public budget, not a supervision wait slice.
                        exc.timeout = timeout
                        raise
                    if now >= next_guard:
                        _check_guard(guard)
                        next_guard = time.monotonic() + _GUARD_INTERVAL
                else:
                    return process.returncode
        finally:
            try:
                # Closing also kills ordinary MCP/other descendants after root success.
                job.close()
            finally:
                if process is not None:
                    try:
                        # Assignment may have failed while the root was still suspended.
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=_REAP_TIMEOUT)
                    finally:
                        process._handle.Close()
