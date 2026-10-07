import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

import startup_review as review


class StartupReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'project'
        self.config = Path(self.temporary.name) / 'config'
        self.history = self.root / 'data' / 'history.sqlite3'
        self.now = [1000.0]
        self.queue = review.Queue(self.root, self.config, self.history, clock=lambda: self.now[0])
        self.owner = str(uuid.uuid4())
        self.other_owner = str(uuid.uuid4())
        self.environment = patch.dict('os.environ', {'MAPLE_EXP_REVIEW_PARENT': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def context(self):
        return review.make_launch_context(self.root, self.config, self.history)

    def enqueue(self):
        context = self.context()
        self.queue.enqueue(context, {'windows': ['orb'], 'running': False})
        return context

    def test_only_explicit_user_requested_start_can_review_after_maintenance(self):
        parent = str(uuid.uuid4())
        with patch.dict('os.environ', {'MAPLE_EXP_REVIEW_PARENT': parent}):
            self.assertFalse(self.context()['review_required'])
            explicit = review.make_launch_context(self.root, self.config, self.history, user_requested=True)
            self.assertTrue(explicit['review_required'])
            self.assertIsNone(explicit['parent_review_id'])
            self.assertEqual(explicit['reason'], 'user')
            self.assertEqual(review.os.environ['MAPLE_EXP_REVIEW_PARENT'], parent)

    def test_context_and_emission_do_not_do_main_thread_disk_io(self):
        main_thread = threading.get_ident()
        entered, finish = threading.Event(), threading.Event()

        def slow_enqueue(queue, context, snapshot):
            self.assertNotEqual(threading.get_ident(), main_thread)
            entered.set()
            self.assertTrue(finish.wait(3))

        with patch.object(review.Queue, 'enqueue', slow_enqueue), \
                patch.object(review.Path, 'resolve', side_effect=AssertionError('disk access')), \
                patch.object(review.sqlite3, 'connect', side_effect=AssertionError('disk access')), \
                patch.object(review.Path, 'mkdir', side_effect=AssertionError('disk access')):
            context = self.context()
            worker = review.emit_startup_async(context, {})
            self.assertTrue(worker.daemon)
            self.assertTrue(entered.wait(2))
            self.assertTrue(worker.is_alive())
            self.assertFalse(worker.review_persisted)
            self.assertIsNone(worker.review_error)
            finish.set()
            worker.join(3)
        self.assertEqual(context['instance_id'], context['event_id'])
        self.assertEqual(context['reason'], 'user')
        self.assertTrue(context['review_required'])
        self.assertFalse(self.config.exists())

    def test_async_snapshot_persists_without_opening_history(self):
        context = self.context()
        worker = review.emit_startup_async(context, {'title': '游戏', 'running': True})
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(worker.review_persisted)
        self.assertIsNone(worker.review_error)
        with self.assertRaises(AttributeError):
            worker.review_persisted = False
        event = self.queue.peek()['event']
        self.assertEqual(event['ui_snapshot']['title'], '游戏')
        self.assertFalse(self.history.exists())

    def test_idempotence_and_maintenance_is_never_claimed(self):
        with patch.dict('os.environ', {'MAPLE_EXP_REVIEW_PARENT': self.owner}):
            maintenance = self.context()
        self.assertFalse(maintenance['review_required'])
        self.assertEqual(self.queue.enqueue(maintenance, {})['status'], 'maintenance')
        self.assertEqual(self.queue.claim(self.owner)['status'], 'no_pending')
        context = self.enqueue()
        self.assertEqual(self.queue.enqueue(context, {'different': 'ignored'})['status'], 'duplicate')
        claim = self.queue.claim(self.owner)
        self.assertEqual(claim['event_id'], context['event_id'])
        self.assertEqual(claim['event']['ui_snapshot'], {'windows': ['orb'], 'running': False})
        self.queue.complete(context['event_id'], self.owner, 'no_change', 'Reviewed', lease_token=claim['lease_token'])
        self.assertEqual(self.queue.enqueue(context, {})['status'], 'duplicate')
        self.assertEqual(self.queue.peek()['status'], 'no_pending')

    def test_two_consumers_two_events_have_one_global_lease(self):
        first, second = self.enqueue(), self.enqueue()
        barrier = threading.Barrier(2)
        results, errors = [], []

        def claim(owner):
            try:
                queue = review.Queue(self.root, self.config, self.history, clock=lambda: self.now[0])
                barrier.wait(3)
                results.append(queue.claim(owner))
            except Exception as error:
                errors.append(error)

        workers = [threading.Thread(target=claim, args=(owner,)) for owner in (self.owner, self.other_owner)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(6)
            self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertCountEqual([result['status'] for result in results], ['claimed', 'busy'])
        winner = next(result for result in results if result['status'] == 'claimed')
        self.assertEqual(winner['event_id'], first['event_id'])
        self.queue.complete(first['event_id'], winner['event']['owner'], 'no_change', 'Reviewed', lease_token=winner['lease_token'])
        self.assertEqual(self.queue.claim(self.owner)['event_id'], second['event_id'])

    def test_expiry_recovers_identity_and_attempts_and_rejects_old_owner(self):
        context = self.enqueue()
        first = self.queue.claim(self.owner, lease_seconds=5)
        self.assertEqual(first['event']['attempts'], 1)
        self.now[0] += 5
        self.assertEqual(self.queue.renew(context['event_id'], self.owner, first['lease_token'])['status'], 'lease_lost')
        recovered = self.queue.claim(self.other_owner)
        self.assertEqual(recovered['event_id'], context['event_id'])
        self.assertTrue(recovered['event']['recovered'])
        self.assertEqual(recovered['event']['attempts'], 2)
        self.assertEqual(self.queue.complete(context['event_id'], self.owner, 'implemented', 'Stale', lease_token=first['lease_token'])['status'], 'lease_lost')
        self.assertEqual(self.queue.release(context['event_id'], self.owner, first['lease_token'])['status'], 'lease_lost')
        self.assertEqual(self.queue.complete(context['event_id'], self.other_owner, 'blocked', 'Needs inspection', lease_token=recovered['lease_token'])['status'], 'completed')

    def test_same_owner_reclaim_invalidates_previous_token(self):
        context = self.enqueue()
        first = self.queue.claim(self.owner, lease_seconds=1)
        self.now[0] += 1
        second = self.queue.claim(self.owner)
        self.assertNotEqual(first['lease_token'], second['lease_token'])
        self.assertTrue(second['event']['recovered'])
        self.assertEqual(self.queue.renew(context['event_id'], self.owner, first['lease_token'])['status'], 'lease_lost')
        self.assertEqual(self.queue.complete(context['event_id'], self.owner, 'implemented', 'Stale', lease_token=first['lease_token'])['status'], 'lease_lost')
        self.assertEqual(self.queue.release(context['event_id'], self.owner, first['lease_token'])['status'], 'lease_lost')
        self.assertEqual(self.queue.renew(context['event_id'], self.owner, second['lease_token'])['status'], 'renewed')
        self.assertEqual(self.queue.complete(context['event_id'], self.owner, 'no_change', 'Reviewed', lease_token=second['lease_token'])['status'], 'completed')

    def test_two_real_process_consumers_share_one_lease(self):
        self.enqueue()
        self.enqueue()
        prefix = [sys.executable, review.__file__, '--project-root', str(self.root),
                  '--config-dir', str(self.config), '--history-path', str(self.history), 'claim', '--owner']
        processes = [subprocess.Popen(prefix + [owner], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                     for owner in (self.owner, self.other_owner)]
        try:
            results = []
            for process in processes:
                output, errors = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, errors)
                results.append(json.loads(output))
            self.assertCountEqual([item['status'] for item in results], ['claimed', 'busy'])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_renew_extends_lease_and_wrong_event_cannot_finish(self):
        first, second = self.enqueue(), self.enqueue()
        claimed = self.queue.claim(self.owner, lease_seconds=5)
        self.now[0] += 4
        self.assertEqual(self.queue.renew(first['event_id'], self.owner, claimed['lease_token'], lease_seconds=10)['lease_until'], 1014)
        self.now[0] += 3
        self.assertEqual(self.queue.claim(self.other_owner)['status'], 'busy')
        self.assertEqual(self.queue.complete(second['event_id'], self.owner, 'no_change', 'wrong', lease_token=claimed['lease_token'])['status'], 'lease_lost')

    def test_release_and_repeated_crashes_have_bounded_attempts(self):
        for expiry in (False, True):
            with self.subTest(expiry=expiry):
                context = self.enqueue()
                for attempt in range(1, review.MAX_ATTEMPTS + 1):
                    result = self.queue.claim(self.owner, lease_seconds=1)
                    self.assertEqual(result['event']['attempts'], attempt)
                    if expiry:
                        self.now[0] += 1
                    else:
                        released = self.queue.release(context['event_id'], self.owner, result['lease_token'], 'Transient failure')
                        self.assertEqual(released['status'], 'exhausted' if attempt == review.MAX_ATTEMPTS else 'released')
                result = self.queue.claim(self.owner)
                self.assertEqual(result['status'], 'needs_attention')
                self.assertIsNotNone(result['summary'])
                self.assertGreater(result['exhausted'], 0)
        with contextlib.closing(sqlite3.connect(self.queue.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE state='exhausted'").fetchone()[0], 2)

    def test_later_exhausted_events_remain_visible_after_first_failure(self):
        identities = []
        for summary in ('A' * 300, 'B failed later'):
            context = self.enqueue()
            identities.append(context['event_id'])
            for _ in range(review.MAX_ATTEMPTS):
                claimed = self.queue.claim(self.owner)
                self.queue.release(context['event_id'], self.owner, claimed['lease_token'], summary)
            for result in (self.queue.peek(), self.queue.claim(self.owner)):
                self.assertEqual(result['status'], 'needs_attention')
                self.assertEqual(result['exhausted'], len(identities))
                self.assertEqual([event['event_id'] for event in result['events']], identities)
                self.assertEqual(result['event_id'], identities[0])
                self.assertEqual(result['summary'], 'A' * 256)
                self.assertEqual(result['events'][-1]['summary'], summary[:256])

    def test_strict_context_validation_before_disk_access(self):
        base = self.context()
        cases = [dict(reason='surprise'), dict(reason='maintenance'), dict(pid=True),
                 dict(review_required=1), dict(project_root=str(self.root / 'other')),
                 dict(config_dir=str(self.root)), dict(instance_id=str(uuid.uuid4())),
                 dict(created_at='2026-10-05T01:00:00'), dict(parent_review_id='$(execute)'),
                 dict(evidence={'live_status': 'elsewhere'}), dict(extra='unknown')]
        for updates in cases:
            with self.subTest(updates=updates):
                context = deepcopy(base)
                context.update(updates)
                with self.assertRaises((ValueError, TypeError)):
                    self.queue.enqueue(context, {})
        self.assertFalse(self.config.exists())
        with patch.dict('os.environ', {'MAPLE_EXP_REVIEW_PARENT': 'secret/not-a-uuid'}):
            self.assertIsNone(self.context()['parent_review_id'])
        self.queue.enqueue(base, {})
        changed = dict(base, pid=base['pid'] + 1)
        with self.assertRaises(ValueError):
            self.queue.enqueue(changed, {})

    def test_payload_is_bounded_data_and_never_executed(self):
        for snapshot in ({'x': float('nan')}, {1: 'key'}, {'x': object()}, {'x': 'x' * 65536}):
            with self.subTest(type=type(next(iter(snapshot.values())))):
                with self.assertRaises(ValueError):
                    self.queue.enqueue(self.context(), snapshot)
        text = '__import__("os").system("should never run")'
        self.queue.enqueue(self.context(), {'title': text})
        self.assertEqual(self.queue.peek()['event']['ui_snapshot']['title'], text)
        with self.assertRaises(ValueError):
            self.queue.claim('invalid owner')
        with self.assertRaises(ValueError):
            self.queue.claim(self.owner, lease_seconds=float('nan'))

    def test_unwritable_queue_and_serialization_failure_are_isolated(self):
        review.RECENT_ERRORS.clear()
        context = self.context()
        with patch.object(review.sqlite3, 'connect', side_effect=PermissionError('secret/token')):
            for _ in range(35):
                worker = review.emit_startup_async(context, {})
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertFalse(worker.review_persisted)
                self.assertEqual(worker.review_error, 'PermissionError')
        self.assertEqual(len(review.RECENT_ERRORS), 32)
        errors = json.dumps(list(review.RECENT_ERRORS))
        self.assertNotIn('secret', errors)
        disk_error = (self.queue.path.parent / 'last-error.json').read_text(encoding='utf-8')
        self.assertNotIn('secret', disk_error)
        self.assertLess(len(disk_error), 1024)
        worker = review.emit_startup_async(context, {'bad': object()})
        worker.join(3)
        self.assertEqual(review.RECENT_ERRORS[-1]['error'], 'ValueError')

    def test_no_writable_directory_and_thread_start_failure_do_not_escape(self):
        self.config.write_text('not a directory', encoding='utf-8')
        worker = review.emit_startup_async(self.context(), {})
        worker.join(3)
        self.assertFalse(worker.is_alive())
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('secret')):
            worker = review.emit_startup_async(self.context(), {})
        self.assertTrue(worker.daemon)
        self.assertEqual(worker.review_error, 'RuntimeError')
        self.assertFalse(worker.review_persisted)
        self.assertEqual(review.RECENT_ERRORS[-1]['error'], 'RuntimeError')

    def test_retains_256_completed_and_all_pending(self):
        first_completed = None
        for index in range(260):
            context = self.enqueue()
            first_completed = first_completed or context
            claimed = self.queue.claim(self.owner)
            self.queue.complete(context['event_id'], self.owner, 'no_change', 'Reviewed', lease_token=claimed['lease_token'])
            self.now[0] += 1
        pending = [self.enqueue()['event_id'] for _ in range(3)]
        with patch.dict('os.environ', {'MAPLE_EXP_REVIEW_PARENT': self.owner}):
            for _ in range(260):
                self.queue.enqueue(self.context(), {})
                self.now[0] += 1
        for _ in range(3):
            self.enqueue()
        with contextlib.closing(sqlite3.connect(self.queue.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE state='completed'").fetchone()[0], 256)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE state='maintenance'").fetchone()[0], 256)
            rows = db.execute("SELECT event_id FROM events WHERE state='pending'").fetchall()
            self.assertEqual(len(rows), 6)
            self.assertTrue(set(pending).issubset({row[0] for row in rows}))
        self.assertEqual(self.queue.enqueue(first_completed, {})['status'], 'duplicate')
        with self.assertRaises(ValueError):
            self.queue.enqueue(dict(first_completed, pid=first_completed['pid'] + 1), {})

    def test_cli_json_contract(self):
        prefix = ['--project-root', str(self.root), '--config-dir', str(self.config), '--history-path', str(self.history)]

        def invoke(*args):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = review.main(prefix + list(args))
            return code, json.loads(output.getvalue())

        self.assertEqual(invoke('peek')[1]['status'], 'no_pending')
        context = self.enqueue()
        claimed = invoke('claim', '--owner', self.owner)[1]
        self.assertEqual(claimed['status'], 'claimed')
        self.assertEqual(invoke('claim', '--owner', self.other_owner)[1]['status'], 'busy')
        self.assertEqual(invoke('renew', '--event', context['event_id'], '--owner', self.owner,
                                '--lease-token', claimed['lease_token'])[1]['status'], 'renewed')
        self.assertEqual(invoke('complete', '--event', context['event_id'], '--owner', self.owner,
                                '--lease-token', claimed['lease_token'], '--outcome', 'no_change', '--summary', 'Reviewed')[1]['status'], 'completed')
        self.assertEqual(invoke('claim', '--owner', 'secret/token'), (1, {'status': 'error', 'error': 'ValueError'}))


if __name__ == '__main__':
    unittest.main()
