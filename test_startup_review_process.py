import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import startup_review_process as process_runner


_TREE_PARENT = r'''
import json, os, pathlib, subprocess, sys, time
info_path, mode = sys.argv[1:]
child_code = r"""
import json, os, pathlib, subprocess, sys, time
grandchild = subprocess.Popen(
    [sys.executable, '-c', 'import time; time.sleep(60)'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    'child': os.getpid(), 'grandchild': grandchild.pid,
}), encoding='utf-8')
time.sleep(60)
"""
child = subprocess.Popen(
    [sys.executable, '-c', child_code, info_path + '.child'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
deadline = time.monotonic() + 10
child_path = pathlib.Path(info_path + '.child')
while not child_path.exists():
    if child.poll() is not None or time.monotonic() > deadline:
        raise RuntimeError('child did not become ready')
    time.sleep(0.01)
info = json.loads(child_path.read_text(encoding='utf-8'))
info['parent'] = os.getpid()
info['child_was_alive'] = child.poll() is None
pathlib.Path(info_path).write_text(json.dumps(info), encoding='utf-8')
print('ready', flush=True)
if mode == 'timeout':
    time.sleep(60)
elif mode == 'error':
    print('expected error', file=sys.stderr, flush=True)
    sys.exit(7)
'''


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object integration tests')
class BoundedProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup_directory, temporary)
        self.root = Path(temporary.name)
        self.stdout = (self.root / 'stdout.bin').open('w+b')
        self.stderr = (self.root / 'stderr.bin').open('w+b')
        self.addCleanup(self.stdout.close)
        self.addCleanup(self.stderr.close)

    @staticmethod
    def cleanup_directory(temporary):
        # Windows can signal a terminated process before inherited file handles
        # finish closing. Retry only that short-lived sharing violation.
        for attempt in range(40):
            try:
                temporary.cleanup()
                return
            except PermissionError as exc:
                if exc.winerror != 32 or attempt == 39:
                    raise
                time.sleep(0.025)

    def run_command(self, command, *, input_bytes=b'', timeout=10, env=None, guard=None):
        return process_runner.run_bounded(
            command, input_bytes=input_bytes, stdout=self.stdout, stderr=self.stderr,
            cwd=self.root, env=env, timeout=timeout, guard=guard,
        )

    def assert_process_exited(self, pid):
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            self.assertEqual(ctypes.get_last_error(), 87)  # PID no longer exists
            return
        try:
            self.assertEqual(kernel32.WaitForSingleObject(handle, 5000), 0, f'PID {pid} survived')
        finally:
            kernel32.CloseHandle(handle)

    def assert_tree_exited(self, info_path):
        info = json.loads(info_path.read_text(encoding='utf-8'))
        self.assertTrue(info['child_was_alive'])
        for key in ('parent', 'child', 'grandchild'):
            self.assert_process_exited(info[key])

    def test_returns_output_and_passes_binary_input_cwd_env_without_console(self):
        code = (
            "import ctypes,json,os,sys; "
            "print(json.dumps({'cwd':os.getcwd(),'env':os.environ['BOUNDED_TEST'],"
            "'console':bool(ctypes.windll.kernel32.GetConsoleWindow())})); "
            "sys.stdout.flush(); sys.stdout.buffer.write(sys.stdin.buffer.read()); "
            "sys.stderr.buffer.write(b'diagnostic')"
        )
        env = dict(os.environ, BOUNDED_TEST='passed')
        payload = b'\x00binary\xff\n' * 20000
        self.assertEqual(self.run_command([sys.executable, '-c', code], input_bytes=payload, env=env), 0)
        self.assertFalse(self.stdout.closed)
        self.assertFalse(self.stderr.closed)
        self.stdout.seek(0)
        metadata = json.loads(self.stdout.readline())
        self.assertEqual(Path(metadata['cwd']), self.root)
        self.assertEqual(metadata['env'], 'passed')
        self.assertFalse(metadata['console'])
        self.assertEqual(self.stdout.read(), payload)
        self.stderr.seek(0)
        self.assertEqual(self.stderr.read(), b'diagnostic')

    def test_success_kills_lingering_child_and_grandchild(self):
        info = self.root / 'tree.json'
        self.assertEqual(self.run_command([sys.executable, '-c', _TREE_PARENT, str(info), 'success']), 0)
        self.assert_tree_exited(info)

    def test_nonzero_exit_kills_lingering_child_and_grandchild(self):
        info = self.root / 'tree.json'
        self.assertEqual(self.run_command([sys.executable, '-c', _TREE_PARENT, str(info), 'error']), 7)
        self.assert_tree_exited(info)
        self.stderr.seek(0)
        self.assertIn(b'expected error', self.stderr.read())

    def test_timeout_kills_parent_child_and_grandchild(self):
        info = self.root / 'tree.json'
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.run_command([sys.executable, '-c', _TREE_PARENT, str(info), 'timeout'], timeout=2)
        self.assertEqual(caught.exception.timeout, 2)
        self.assertLess(time.monotonic() - started, 7)
        self.assert_tree_exited(info)

    def test_large_unread_input_does_not_bypass_timeout(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_command([sys.executable, '-c', 'import time; time.sleep(60)'],
                             input_bytes=b'x' * (4 * 1024 * 1024), timeout=0.3)
        self.assertLess(time.monotonic() - started, 5)

    def test_guard_rejects_before_command_executes(self):
        marker = self.root / 'guard.started'
        code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')"
        checks = []

        def guard():
            checks.append(time.monotonic())
            return False

        with self.assertRaisesRegex(RuntimeError, 'guard rejected'):
            self.run_command([sys.executable, '-c', code, str(marker)], guard=guard)
        self.assertEqual(len(checks), 1)
        self.assertFalse(marker.exists())

    def test_second_guard_rejection_or_error_kills_running_tree(self):
        for failure in ('false', 'exception'):
            with self.subTest(failure=failure):
                info = self.root / (failure + '.json')
                checks = []

                def guard():
                    checks.append(time.monotonic())
                    if len(checks) == 1:
                        return True
                    if failure == 'exception':
                        raise OSError('lease renewal failed')
                    return False

                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, 'process guard') as caught:
                    self.run_command(
                        [sys.executable, '-c', _TREE_PARENT, str(info), 'timeout'],
                        guard=guard, timeout=10,
                    )
                self.assertEqual(len(checks), 2)
                self.assertGreaterEqual(checks[1] - checks[0], 2.0)
                self.assertLess(time.monotonic() - started, 7)
                if failure == 'exception':
                    self.assertIsInstance(caught.exception.__cause__, OSError)
                self.assert_tree_exited(info)

    def test_successful_guard_checks_do_not_extend_deadline(self):
        info = self.root / 'guard-timeout.json'
        checks = []

        def guard():
            checks.append(time.monotonic())
            return True

        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.run_command(
                [sys.executable, '-c', _TREE_PARENT, str(info), 'timeout'],
                guard=guard, timeout=2.4,
            )
        self.assertEqual(caught.exception.timeout, 2.4)
        self.assertEqual(len(checks), 2)
        self.assertLess(time.monotonic() - started, 4.0)
        self.assert_tree_exited(info)

    def test_assignment_and_resume_failure_reap_suspended_process(self):
        for method in ('assign', 'resume'):
            with self.subTest(method=method):
                marker = self.root / (method + '.started')
                children = []

                def reject(job, process):
                    children.append(process)
                    raise OSError('injected initialization failure')

                code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')"
                with patch.object(process_runner._WindowsJob, method, reject):
                    with self.assertRaisesRegex(OSError, 'injected initialization failure'):
                        self.run_command([sys.executable, '-c', code, str(marker)])
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].returncode)
                self.assert_process_exited(children[0].pid)
                self.assertFalse(marker.exists(), 'suspended process executed before setup succeeded')

    def test_job_setup_failure_does_not_start_process(self):
        with patch.object(process_runner._WindowsJob, '_set_limits', side_effect=OSError('job setup')), \
                patch.object(process_runner.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(OSError, 'job setup'):
                self.run_command([sys.executable, '-c', 'pass'])
            spawn.assert_not_called()

    def test_missing_executable_propagates_error(self):
        with self.assertRaises(FileNotFoundError):
            self.run_command([str(self.root / 'missing.exe')])

    def test_invalid_timeout_never_starts_process(self):
        for timeout in (0, -1, float('inf'), float('nan')):
            with self.subTest(timeout=timeout), patch.object(process_runner.subprocess, 'Popen') as spawn:
                with self.assertRaises(ValueError):
                    self.run_command([sys.executable, '-c', 'pass'], timeout=timeout)
                spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
