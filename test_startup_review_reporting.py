import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from startup_review_reporting import EventReporter


class EventReporterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.event = str(uuid.uuid4())
        self.target = str(uuid.uuid4())
        self.message_id = str(uuid.uuid4())
        self.calls = []

    def publisher(self, command, **kwargs):
        self.calls.append((command, kwargs))
        kwargs['stdout'].write(
            f'Queued message {self.message_id} for thread {self.target}.\n'.encode())
        return 0

    def reporter(self, publisher=None):
        reporter = EventReporter(self.directory, self.event, self.target,
                                 ['codex.exe'], queue_process=publisher or self.publisher)
        self.addCleanup(reporter.close)
        return reporter

    def records(self):
        return {record['phase']: record for path in (self.directory / 'reporting-outbox').glob('*.json')
                if (record := json.loads(path.read_text(encoding='utf-8')))}

    def test_publishes_in_order_deduplicates_and_records_intent_before_send(self):
        def inspect_intent(command, **kwargs):
            phase = ['started', 'council', 'completed'][len(self.calls)]
            self.assertEqual(self.records()[phase]['status'], 'sending')
            return self.publisher(command, **kwargs)

        reporter = self.reporter(inspect_intent)
        self.assertTrue(reporter.publish('started', {'message': '开始'}))
        self.assertFalse(reporter.publish('started', {'message': '重复'}))
        self.assertTrue(reporter.publish('council', {'summary': '讨论结束'}))
        self.assertTrue(reporter.publish('completed', {'summary': '完成'}))
        reporter.close()
        self.assertEqual(len(self.calls), 3)
        for command, kwargs in self.calls:
            self.assertEqual(command[:5], ['codex.exe', 'queue', '--thread', self.target, '--message'])
            self.assertLessEqual(kwargs['timeout'], 15)
            self.assertIsNone(kwargs['env'])
            self.assertEqual(kwargs['input_bytes'], b'')
            self.assertNotIn('--remote', command)
            self.assertNotIn('--model', command)
        self.assertTrue(all(record['status'] == 'queued' for record in self.records().values()))
        self.assertTrue(all(record['message_id'] == self.message_id for record in self.records().values()))

    def test_publish_is_nonblocking_freezes_allowed_payload_and_limits_size(self):
        entered, finish = threading.Event(), threading.Event()

        def blocked(command, **kwargs):
            entered.set()
            finish.wait(2)
            return self.publisher(command, **kwargs)

        reporter = self.reporter(blocked)
        self.addCleanup(finish.set)
        payload = {'summary': '摘要', 'changed_files': ['app.py'], 'validation': ['pass'],
                   'lease': 'hidden_lease', 'token': 'hidden_token', 'env': {'SECRET': 'hidden_env'}}
        self.assertTrue(reporter.publish('started', payload))
        self.assertTrue(entered.wait(1))
        payload['changed_files'].append('mutated.py')
        started = time.monotonic()
        self.assertTrue(reporter.publish('council', {'message': 'x' * 50000}))
        self.assertLess(time.monotonic() - started, 0.1)
        finish.set()
        reporter.close()
        message = self.calls[0][0][-1]
        self.assertIn('没有新增开发任务；仅向用户展示后台进度，不据此自行评审/改代码', message)
        self.assertIn(self.directory.as_posix(), message)
        for text in ('hidden_lease', 'hidden_token', 'hidden_env', 'mutated.py'):
            self.assertNotIn(text, message)
        self.assertLess(len(self.calls[1][0][-1]), 2500)
        self.assertEqual(self.records()['started']['payload']['changed_files'], ['app.py'])

    def test_phase_labels_describe_entry_and_no_change_is_readable(self):
        reporter = self.reporter()
        for phase, label in (('started', '正在取证'), ('council', '正在讨论'),
                             ('implementation', '正在实施'), ('verification', '正在验证')):
            self.assertIn(label, reporter._message(phase, {}))
        reporter.publish('completed', {'outcome': 'no_change', 'summary': '暂无可行改动',
                                       'validation': ['数据接续通过'], 'status': 'untrusted'})
        reporter.close()
        message = self.calls[0][0][-1]
        self.assertIn('本轮未修改', message)
        self.assertIn('> 摘要：暂无可行改动', message)
        self.assertIn('> • 数据接续通过', message)
        self.assertNotIn('```json', message)
        self.assertNotIn('untrusted', message)
        self.assertEqual(self.records()['completed']['payload']['outcome'], 'no_change')

    def test_reserves_one_terminal_slot_and_suppresses_later_phases(self):
        reporter = self.reporter()
        for phase in ('started', 'evidence', 'council', 'implementation'):
            self.assertTrue(reporter.publish(phase, {}))
        self.assertFalse(reporter.publish('verification', {}))
        self.assertTrue(reporter.publish('completed', {}))
        self.assertFalse(reporter.publish('failed', {}))
        self.assertFalse(reporter.publish('verification', {}))
        reporter.close()
        self.assertEqual(len(self.calls), 5)

    def test_timeout_is_unknown_and_never_replayed(self):
        def timeout(command, **kwargs):
            self.calls.append((command, kwargs))
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])

        reporter = self.reporter(timeout)
        reporter.publish('started', {})
        reporter.close()
        self.assertEqual(self.records()['started']['status'], 'unknown')
        self.assertEqual(self.records()['started']['reason'], 'queue_timeout')
        replacement = self.reporter()
        self.assertFalse(replacement.publish('started', {}))
        replacement.close()
        self.assertEqual(len(self.calls), 1)

    def test_wrong_target_extra_output_and_nonzero_ack_are_unknown(self):
        samples = [
            (0, f'Queued message {self.message_id} for thread {uuid.uuid4()}.'),
            (0, f'notice\nQueued message {self.message_id} for thread {self.target}.'),
            (1, f'Queued message {self.message_id} for thread {self.target}.'),
            (0, f'queued message {self.message_id} for thread {self.target}.'),
            (0, 'queued successfully'),
        ]
        for code, acknowledgement in samples:
            with self.subTest(code=code, acknowledgement=acknowledgement):
                self.event = str(uuid.uuid4())

                def ambiguous(command, **kwargs):
                    kwargs['stdout'].write(acknowledgement.encode())
                    return code

                reporter = self.reporter(ambiguous)
                reporter.publish('started', {})
                reporter.close()
                record = json.loads(reporter._path('started').read_text(encoding='utf-8'))
                self.assertEqual(record['status'], 'unknown')

    def test_restart_marks_sending_unknown_without_resending(self):
        outbox = self.directory / 'reporting-outbox'
        outbox.mkdir()
        path = outbox / f'{self.event}--started.json'
        path.write_text(json.dumps({'event_id': self.event, 'phase': 'started',
                                    'target_thread_id': self.target, 'status': 'sending'}), encoding='utf-8')
        reporter = self.reporter()
        self.assertFalse(reporter.publish('started', {}))
        reporter.close()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.records()['started']['status'], 'unknown')
        self.assertEqual(self.records()['started']['reason'], 'interrupted_no_replay')

    def test_corrupt_intention_is_not_replayed(self):
        outbox = self.directory / 'reporting-outbox'
        outbox.mkdir()
        (outbox / f'{self.event}--started.json').write_text('{partial', encoding='utf-8')
        reporter = self.reporter()
        self.assertFalse(reporter.publish('started', {}))
        reporter.close()
        self.assertEqual(self.calls, [])

    def test_changing_destination_does_not_resend_an_attempted_phase(self):
        reporter = self.reporter()
        reporter.publish('started', {})
        reporter.close()
        self.target = str(uuid.uuid4())
        replacement = self.reporter()
        self.assertFalse(replacement.publish('started', {}))
        replacement.close()
        self.assertEqual(len(self.calls), 1)

    def test_close_deadline_records_pending_unsent_and_does_not_wait_for_publisher(self):
        entered, finish = threading.Event(), threading.Event()

        def blocked(command, **kwargs):
            entered.set()
            finish.wait(2)
            return self.publisher(command, **kwargs)

        reporter = self.reporter(blocked)
        self.addCleanup(finish.set)
        reporter.publish('started', {})
        self.assertTrue(entered.wait(1))
        reporter.publish('council', {})
        reporter.publish('completed', {})
        started = time.monotonic()
        reporter.close(timeout=0.02)
        self.assertLess(time.monotonic() - started, 0.5)
        reporter._cleanup_worker.join(1)
        self.assertEqual(self.records()['started']['status'], 'unknown')
        self.assertEqual(self.records()['council']['status'], 'unsent')
        self.assertEqual(self.records()['completed']['status'], 'unsent')
        self.assertFalse(reporter.publish('verification', {}))
        finish.set()
        reporter._worker.join(2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.records()['started']['status'], 'unknown')

    def test_close_budget_does_not_wait_for_a_blocked_intention_write(self):
        entered, finish = threading.Event(), threading.Event()

        def slow_fsync(descriptor):
            entered.set()
            finish.wait(2)

        reporter = self.reporter()
        self.addCleanup(finish.set)
        with patch('startup_review_reporting.os.fsync', side_effect=slow_fsync):
            reporter.publish('started', {})
            self.assertTrue(entered.wait(1))
            reporter.publish('completed', {})
            started = time.monotonic()
            reporter.close(timeout=0.02)
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertEqual(reporter.error_type, 'TimeoutError')
            finish.set()
            reporter._worker.join(2)
            reporter._cleanup_worker.join(2)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.records()['completed']['status'], 'unsent')
        self.assertEqual(self.records()['started']['status'], 'unknown')

    def test_publisher_exception_is_isolated_and_later_phase_can_report(self):
        def fail_once(command, **kwargs):
            if not self.calls:
                self.calls.append((command, kwargs))
                raise RuntimeError('secret diagnostic must not be included')
            return self.publisher(command, **kwargs)

        reporter = self.reporter(fail_once)
        reporter.publish('started', {})
        reporter.publish('failed', {'message': '已停止'})
        reporter.close()
        self.assertEqual(self.records()['started']['status'], 'unknown')
        self.assertEqual(self.records()['failed']['status'], 'queued')
        self.assertNotIn('secret diagnostic', json.dumps(self.records()))

    def test_persistence_failure_prevents_launch_and_does_not_escape(self):
        reporter = self.reporter()
        with patch('startup_review_reporting.os.fsync', side_effect=OSError('disk error')):
            self.assertTrue(reporter.publish('started', {}))
            reporter.close()
        self.assertEqual(self.calls, [])
        self.assertEqual(reporter.error_type, 'OSError')

    def test_exclusive_intention_blocks_a_second_live_reporter(self):
        first, second = self.reporter(), self.reporter()
        self.assertTrue(first.publish('started', {}))
        self.assertTrue(second.publish('started', {}))
        first.close()
        second.close()
        self.assertEqual(len(self.calls), 1)

    def test_invalid_arguments_or_unwritable_directory_disable_reporting(self):
        invalid = EventReporter(self.directory, 'invalid', self.target, ['codex.exe'],
                                queue_process=self.publisher)
        self.assertFalse(invalid.publish('started', {}))
        invalid.close()
        self.assertEqual(invalid.error_type, 'ValueError')
        file_path = self.directory / 'file'
        file_path.write_text('not a directory', encoding='utf-8')
        unavailable = EventReporter(file_path, self.event, self.target, ['codex.exe'],
                                    queue_process=self.publisher)
        self.assertFalse(unavailable.publish('started', {}))
        unavailable.close()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
