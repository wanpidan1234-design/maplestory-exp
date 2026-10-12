import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from analysis_log import AnalysisJournal, summarize


class AnalysisJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.journals = []
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        for journal in self.journals:
            journal.close(timeout=2)
        self.temp.cleanup()

    def journal(self, **kwargs):
        journal = AnalysisJournal(self.directory, **kwargs)
        self.journals.append(journal)
        return journal

    def wait_for(self, condition):
        deadline = time.monotonic() + 2
        while not condition():
            if time.monotonic() >= deadline:
                self.fail('writer did not reach expected state')
            time.sleep(.002)

    def rows(self):
        return [json.loads(line) for path in self.directory.glob('analysis.jsonl*')
                for line in path.read_bytes().splitlines()]

    def blocked(self, **kwargs):
        entered, release, content = threading.Event(), threading.Event(), []
        self.releases.append(release)
        def sink(data):
            content.append(data)
            entered.set()
            release.wait(5)
        journal = self.journal(sink=sink, **kwargs)
        self.assertTrue(journal.emit('first'))
        self.assertTrue(entered.wait(2))
        return journal, release, content

    def test_blocked_writer_does_not_block_emit_or_close_and_queue_is_bounded(self):
        journal, release, content = self.blocked(queue_size=2)
        started = time.perf_counter()
        self.assertTrue(journal.emit('second', {'current': 0}))
        self.assertTrue(journal.emit('third'))
        for _ in range(100):
            self.assertFalse(journal.emit('overflow'))
        self.assertLess(time.perf_counter() - started, .25)
        self.assertEqual(journal.status()['queue_depth'], 2)
        self.assertEqual(journal.status()['queue_full'], 100)
        started = time.perf_counter()
        self.assertFalse(journal.close(.02))
        self.assertLess(time.perf_counter() - started, .15)
        self.assertEqual(journal.status()['pending'], 3)
        release.set()
        self.assertTrue(journal.close(2))
        self.assertEqual(journal.status()['written'], 3)
        self.assertEqual(journal.status()['pending'], 0)
        self.assertEqual(len(content), 3)

    def test_snapshot_filters_private_values_and_preserves_none_zero(self):
        journal, release, content = self.blocked()
        fields = {'sample_id': 5, 'current': 0, 'percent': None,
                  'reason': 'low_confidence', 'error_type': 'ValueError',
                  'raw': 'PRIVATE_OCR', 'message': 'SECRET_EXCEPTION',
                  'path': 'C:/Users/private', 'stage': 'C:/Users/private',
                  'session_id': 'f' * 32, 'slot': 'slot-03.json',
                  'before': {'windows': {'60': {'count': 0, 'sum': 0}},
                             'value': None}, 'presence': {'current': True}}
        self.assertTrue(journal.emit('decision', fields))
        fields['before']['windows']['60']['sum'] = 999
        release.set()
        self.assertTrue(journal.close(2))
        row = json.loads(content[1])
        self.assertEqual(row['fields']['current'], 0)
        self.assertIsNone(row['fields']['percent'])
        self.assertEqual(row['fields']['before']['windows']['60']['sum'], 0)
        self.assertEqual(row['fields']['slot'], 'slot-03.json')
        self.assertTrue(row['truncated'])
        data = b''.join(content)
        for secret in (b'PRIVATE_OCR', b'SECRET_EXCEPTION', b'Users/private', b'999'):
            self.assertNotIn(secret, data)

    def test_hostile_objects_arrays_big_numbers_and_strings_do_not_call_repr(self):
        class Hostile:
            def __repr__(self):
                raise AssertionError('must never represent object')
            def __str__(self):
                raise AssertionError('must never stringify object')
        journal = self.journal()
        self.assertTrue(journal.emit('bad_input', {'before': Hostile(), 'current': 10**10000,
            'reason': 'x' * 1000000, 'value': float('nan'), 'after': [Hostile()]}))
        self.assertTrue(journal.close(2))
        self.assertTrue(self.rows()[0]['truncated'])
        self.assertEqual(self.rows()[0]['fields'], {})

    def test_context_contains_only_last_eight_bounded_summaries(self):
        journal = self.journal()
        for sample in range(20):
            journal.observe({'sample_id': sample, 'generation': 2,
                             'outcome': 'ok', 'current': 0, 'segment': 1,
                             'observed_at': float(sample), 'raw': 'PRIVATE'})
        self.assertTrue(journal.emit('anomaly', {'reason': 'rejected'}, context=True))
        self.assertTrue(journal.close(2))
        row = self.rows()[0]
        self.assertEqual([s['sample_id'] for s in row['context']], list(range(12, 20)))
        for summary in row['context']:
            self.assertLessEqual(len(json.dumps(summary, separators=(',', ':')).encode()), 256)
        self.assertLessEqual(len(json.dumps(row['context'], separators=(',', ':')).encode()), 2048)
        self.assertNotIn('PRIVATE', json.dumps(row))

    def test_oversized_fields_keep_valid_json_and_anomaly_body(self):
        journal = self.journal(max_bytes=512)
        fields = {'reason': 'low_confidence', 'sample_id': 22,
                  'before': {'windows': {str(k): {'count': 300, 'sum': 987654321,
                        'observed_seconds': 100.0} for k in (60, 120, 300, 600, 1800)}},
                  'after': {'current': 250, 'total': 500}}
        for sample in range(8):
            journal.observe({'sample_id': sample, 'reason': 'r' * 64})
        self.assertTrue(journal.emit('failure', fields, context=True))
        self.assertTrue(journal.close(2))
        content = (self.directory / 'analysis.jsonl').read_bytes()
        self.assertLessEqual(len(content), 512)
        row = json.loads(content)
        self.assertTrue(row['truncated'])
        self.assertEqual(row['event'], 'failure')
        self.assertEqual(row['fields']['reason'], 'low_confidence')
        self.assertEqual(row['fields']['sample_id'], 22)

    def test_producer_lock_contention_is_dropped_without_waiting(self):
        journal = self.journal()
        with journal._lock:
            started = time.perf_counter()
            self.assertFalse(journal.emit('sample'))
            self.assertLess(time.perf_counter() - started, .05)
        self.assertEqual(journal.status()['dropped'], 1)
        self.assertTrue(journal.close(2))

    def test_json_encoding_occurs_only_in_writer(self):
        real_dumps = json.dumps
        names = []
        def checked(*args, **kwargs):
            names.append(threading.current_thread().name)
            return real_dumps(*args, **kwargs)
        with patch('analysis_log.json.dumps', side_effect=checked):
            journal = self.journal()
            journal.observe({'sample_id': 1})
            self.assertTrue(journal.emit('sample', {'sample_id': 1}, context=True))
            self.assertTrue(journal.close(2))
        self.assertTrue(names)
        self.assertEqual(set(names), {'analysis-log'})

    def test_throttle_is_reason_scoped_and_reports_suppression(self):
        now = [10.0]
        content = []
        journal = self.journal(clock=lambda: now[0], sink=content.append)
        self.assertTrue(journal.emit('failure', {'reason': 'anchor'}, interval=5))
        now[0] = 11
        self.assertFalse(journal.emit('failure', {'reason': 'anchor'}, interval=5))
        self.assertTrue(journal.emit('failure', {'reason': 'occluded'}, interval=5))
        now[0] = 12
        self.assertFalse(journal.emit('failure', {'reason': 'anchor'}, interval=5))
        now[0] = 15
        self.assertTrue(journal.emit('failure', {'reason': 'anchor'}, interval=5))
        self.assertTrue(journal.close(2))
        self.assertEqual(journal.status()['suppressed'], 2)
        self.assertEqual(json.loads(content[-1])['suppression'], {'count': 2, 'first': 11., 'last': 12.})

    def test_throttle_keys_are_bounded_and_closed_journal_rejects(self):
        journal = self.journal(queue_size=4096, sink=lambda _: None)
        for index in range(500):
            journal.emit('failure', key=f'key_{index}', interval=5)
        self.assertLessEqual(len(journal._throttles), 128)
        self.assertTrue(journal.close(2))
        self.assertFalse(journal.emit('after_close'))
        self.assertTrue(journal.close(.01))
        self.assertEqual(journal.status()['pending'], 0)

    def test_disk_failure_backoff_has_no_automatic_retry_and_recovers_on_emit(self):
        now, fail, content = [1.0], [True], []
        def sink(data):
            if fail[0]:
                raise PermissionError('PRIVATE_PATH')
            content.append(data)
        journal = self.journal(clock=lambda: now[0], sink=sink)
        self.assertTrue(journal.emit('first'))
        self.wait_for(lambda: journal.status()['write_errors'] == 1)
        self.assertEqual(journal.status()['last_error'], 'PermissionError')
        self.assertFalse(journal.emit('during_backoff'))
        now[0], fail[0] = 20.0, False
        time.sleep(.02)
        self.assertEqual(content, [])
        self.assertTrue(journal.emit('recovered'))
        self.assertTrue(journal.close(2))
        self.assertEqual(journal.status()['write_errors'], 1)
        self.assertEqual(journal.status()['backoff_dropped'], 1)
        self.assertEqual(journal.status()['dropped'], 2)
        self.assertIsNone(journal.status()['last_error'])
        self.assertNotIn(b'PRIVATE_PATH', b''.join(content))

    def test_unwritable_directory_is_optional_and_nonfatal(self):
        target = self.directory / 'file_not_directory'
        target.write_text('existing', encoding='utf-8')
        journal = AnalysisJournal(target)
        self.journals.append(journal)
        self.assertTrue(journal.emit('failure'))
        self.assertTrue(journal.close(2))
        self.assertEqual(journal.status()['write_errors'], 1)
        self.assertEqual(target.read_text(), 'existing')

    def test_rotation_has_only_three_bounded_files(self):
        journal = self.journal(max_bytes=1200, queue_size=256)
        for sample in range(100):
            journal.emit('sample', {'sample_id': sample, 'current': sample * 10})
        self.assertTrue(journal.close(2))
        files = sorted(self.directory.iterdir())
        self.assertEqual([p.name for p in files], ['analysis.jsonl', 'analysis.jsonl.1', 'analysis.jsonl.2'])
        self.assertTrue(all(p.stat().st_size <= 1200 for p in files))
        for row in self.rows():
            self.assertEqual(row['schema'], 'analysis_log_v1')

    def test_rotation_error_preserves_existing_file_and_is_nonfatal(self):
        existing = self.directory / 'analysis.jsonl'
        existing.write_bytes(b'x' * 1100)
        with patch('analysis_log.Path.replace', side_effect=PermissionError('PRIVATE')):
            journal = self.journal(max_bytes=1200)
            self.assertTrue(journal.emit('sample'))
            self.assertTrue(journal.close(2))
        self.assertEqual(existing.read_bytes(), b'x' * 1100)
        self.assertEqual(journal.status()['write_errors'], 1)

    def test_summary_is_readonly_and_reports_facts_not_causes(self):
        journal = self.journal()
        journal.emit('failure', {'reason': 'occluded'}, interval=5)
        journal.emit('failure', {'reason': 'occluded'}, interval=5)
        journal.emit('recovered')
        self.assertTrue(journal.close(2))
        path = self.directory / 'analysis.jsonl'
        with path.open('ab') as output:
            output.write(b'{incomplete\n' + b'x' * 9000 + b'\n')
        before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.directory.iterdir()}
        result = summarize(self.directory)
        self.assertEqual(result['facts']['events'], {'failure': 1, 'recovered': 1})
        self.assertEqual(result['facts']['reasons'], {'occluded': 1})
        self.assertEqual(result['facts']['malformed_lines'], 1)
        self.assertEqual(result['facts']['oversized_lines'], 1)
        self.assertEqual(result['facts']['recorded_loss_counters']['suppressed'], 1)
        self.assertIn('root_cause_not_inferred', result['unknown'])
        process = subprocess.run([sys.executable, 'analysis_log.py', '--summary', str(self.directory)],
                                 capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(process.stdout), result)
        self.assertEqual(before, {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.directory.iterdir()})

    def test_summary_missing_directory_does_not_create_it(self):
        target = self.directory / 'missing'
        self.assertEqual(summarize(target)['facts']['files_read'], 0)
        self.assertFalse(target.exists())

    def test_sample_ids_are_unique_and_bad_clock_does_not_escape(self):
        journal = self.journal(clock=lambda: float('nan'))
        self.assertEqual([journal.next_sample_id() for _ in range(3)], [1, 2, 3])
        self.assertFalse(journal.emit('event'))
        self.assertTrue(journal.close(2))
        self.assertEqual(journal.status()['rejected'], 1)


if __name__ == '__main__':
    unittest.main()
