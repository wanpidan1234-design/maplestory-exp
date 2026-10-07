"""Receipt truth and bounded local feedback, without starting a model or GUI."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from ui_review import ReviewStatusView, read_review_status


class ReviewReceiptTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.event = str(uuid.uuid4())
        self.session = str(uuid.uuid4())

    def write(self, name, **data):
        (self.folder / name).write_text(json.dumps(dict(event_id=self.event, **data)), encoding='utf-8')

    def read(self):
        return read_review_status(self.folder, self.event)

    def started(self):
        (self.folder / 'events.jsonl').write_text(json.dumps(dict(type='thread.started', thread_id=self.session))+'\n', encoding='utf-8')

    def test_dispatch_and_process_creation_do_not_claim_a_started_model(self):
        self.write('dispatch.json', status='runner_started', runner_pid=123)
        self.write('runner.json', status='cli_starting', model_started=None)
        state = self.read()
        self.assertEqual(state['state'], 'starting')
        self.assertIsNone(state['session_id'])
        self.assertFalse(state['model_started'])
        self.started()
        state = self.read()
        self.assertEqual(state['state'], 'running')
        self.assertEqual(state['session_id'], self.session)

    def test_failed_run_preserves_model_reason_and_actual_session(self):
        self.started()
        self.write('runner.json', status='failed', error='FileNotFoundError')
        self.write('result.json', outcome='blocked', summary='权限不足，本次未修改软件。')
        state = self.read()
        self.assertEqual(state['state'], 'failed')
        self.assertEqual(state['summary'], '权限不足，本次未修改软件。')
        self.assertEqual(state['session_id'], self.session)

    def test_explicit_blocked_and_error_message_are_visible(self):
        self.write('runner.json', status='blocked', error_message='暂时无法完成评审。', thread_id=self.session)
        state = self.read()
        self.assertEqual(state['state'], 'failed')
        self.assertEqual(state['summary'], '暂时无法完成评审。')
        self.assertEqual(state['session_id'], self.session)

    def test_completed_outcome_is_separate_from_exit_zero(self):
        self.started()
        self.write('runner.json', status='cli_starting', exit_code=0)
        self.write('result.json', outcome='no_change', summary='无需修改。')
        self.assertEqual(self.read()['state'], 'running')
        self.write('runner.json', status='completed', outcome='no_change', summary='无需修改。')
        self.assertEqual(self.read()['state'], 'completed')
        self.write('runner.json', status='completed', outcome='blocked', summary='没有实施改动。')
        self.assertEqual(self.read()['state'], 'failed')

    def test_busy_receipt_does_not_look_like_active_review(self):
        self.write('dispatch.json', status='runner_started')
        self.write('skipped-first.json', status='busy')
        state = self.read()
        self.assertEqual(state['state'], 'skipped')
        self.assertFalse(state['model_started'])

    def test_malformed_oversized_and_wrong_event_files_are_not_authority(self):
        self.write('dispatch.json', status='runner_started')
        (self.folder / 'runner.json').write_text('{broken', encoding='utf-8')
        (self.folder / 'result.json').write_text('x'*100000, encoding='utf-8')
        self.assertEqual(self.read()['state'], 'starting')
        (self.folder / 'runner.json').write_text(json.dumps(dict(event_id=str(uuid.uuid4()), status='completed')), encoding='utf-8')
        self.assertEqual(self.read()['state'], 'starting')
        with self.assertRaises(ValueError):
            read_review_status(self.folder, '../another-event')

    def test_only_bounded_start_of_cli_log_is_needed(self):
        self.started()
        with (self.folder / 'events.jsonl').open('a', encoding='utf-8') as output:
            output.write('x' * 2_000_000)
        self.assertEqual(self.read()['session_id'], self.session)

    def test_stale_incomplete_receipt_reports_uncertainty_without_retry(self):
        self.write('runner.json', status='cli_starting', at='2026-10-05T00:00:00+00:00')
        state = read_review_status(self.folder, self.event, now=1791158400+1600)
        self.assertEqual(state['state'], 'uncertain')
        self.assertIn('不会自动重复', state['summary'])


class ReviewViewLifecycleTests(unittest.TestCase):
    def make_view(self):
        orb = SimpleNamespace(backend=SimpleNamespace(), root=Mock(), ball_level=SimpleNamespace(active=False),
                              panel_level=None, midnight_panel=None)
        view = ReviewStatusView(orb)
        view._paint_notice = Mock()
        view._paint_footer = Mock()
        view._paint_details = Mock()
        view._read_async = Mock()
        return view

    def test_trigger_latches_once_and_local_terminal_read_stops(self):
        view = self.make_view()
        event = str(uuid.uuid4())
        view.notify_trigger(event)
        view.tick()
        view._read_async.assert_called_once()
        view.inbox.put(dict(event_id=event, state='completed', summary='无需修改。', session_id=None))
        view.tick()
        view.notify_trigger(event)
        self.assertEqual(view.data['state'], 'completed')
        self.assertTrue(view.read_finished)
        view._read_async.assert_called_once()

    def test_wrong_event_background_result_is_ignored(self):
        view = self.make_view()
        view.notify_trigger(str(uuid.uuid4()))
        view.inbox.put(dict(event_id=str(uuid.uuid4()), state='failed', summary='wrong', session_id=None))
        view.tick()
        self.assertEqual(view.data['state'], 'triggered')

    def test_maintenance_follows_parent_without_new_trigger_notice(self):
        view = self.make_view()
        parent = str(uuid.uuid4())
        view.orb.backend.review_launch = dict(parent_review_id=parent)
        view.tick()
        self.assertEqual(view.event_id, parent)
        self.assertFalse(view.announce)
        self.assertIsNone(view.pending_toast)

    def test_failure_before_dispatch_is_visible_and_never_retried(self):
        view = self.make_view()
        view.orb.backend.review_launch = dict(event_id=str(uuid.uuid4()))
        view.orb.backend.review_sample_fired = True
        view.orb.backend.startup_review_error = 'PermissionError'
        view.tick()
        self.assertEqual(view.data['state'], 'failed')
        self.assertIn('权限不足', view.data['summary'])
        view._read_async.assert_not_called()

    def test_optional_ui_exception_does_not_escape_observer_update(self):
        view = self.make_view()
        view._paint_notice.side_effect = RuntimeError('fixture unavailable')
        view.tick()
        self.assertEqual(view.last_error, 'RuntimeError')

    def test_close_destroys_owned_windows_and_stops_future_reads(self):
        view = self.make_view()
        view.notify_trigger(str(uuid.uuid4()))
        notice, details = Mock(), Mock()
        view.notice, view.window = notice, details
        view.close()
        view.tick()
        notice.destroy.assert_called_once()
        details.destroy.assert_called_once()
        view._read_async.assert_not_called()

    def test_copy_uses_actual_session_id_and_never_copies_invented_fallback(self):
        view = self.make_view()
        view.window = Mock()
        view.session_label = Mock()
        view._copy_session()
        view.window.clipboard_append.assert_not_called()
        session = str(uuid.uuid4())
        view.data['session_id'] = session
        view._copy_session()
        view.window.clipboard_append.assert_called_once_with(session)

    def test_level_effect_defers_notice_without_touching_orb_canvas(self):
        view = self.make_view()
        view._paint_notice = ReviewStatusView._paint_notice.__get__(view)
        view.notify_trigger(str(uuid.uuid4()))
        view.notice = Mock()
        view.orb.ball_level.active = True
        view._paint_notice(100)
        view.notice.withdraw.assert_called_once()
        self.assertEqual(view.pending_toast, 'triggered')
        self.assertFalse(hasattr(view.orb, 'canvas'))


if __name__ == '__main__':
    unittest.main()
