import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import uuid

import startup_review as review
import startup_review_runner as runner


class EventRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.config = Path(self.temp.name) / 'config'
        self.config.mkdir()
        self.history = self.root / 'data/history.sqlite3'
        for target, value in ((runner, self.root), (review, self.root)):
            p = patch.object(target, 'ROOT', value)
            p.start()
            self.addCleanup(p.stop)
        env = patch.dict(os.environ, {'MAPLE_EXP_REVIEW_PARENT': ''})
        env.start()
        self.addCleanup(env.stop)
        self.context = review.make_launch_context(self.root, self.config, self.history)
        self.queue = review.Queue(self.root, self.config, self.history)
        self.sample = dict(source='screen', hwnd=42, observed_at=time.monotonic(), sampled_at=time.time())
        self.snapshot = dict(qualified_sample=self.sample, running=True)
        self.status = dict(app_instance_id=self.context['instance_id'], app_pid=self.context['pid'],
                           running=True, valid=True, updated=time.time(), capture=dict(self.sample),
                           last_valid=self.sample['sampled_at'])
        self.write_status()
        self.directory = runner._run_dir(self.context['event_id'])
        self.sidebar_thread = str(uuid.uuid4())
        sidebar = patch.object(runner, 'prepare_sidebar_thread', return_value={
            'status': 'prepared', 'thread_id': self.sidebar_thread, 'source': 'vscode'})
        self.prepare_sidebar = sidebar.start()
        self.addCleanup(sidebar.stop)

    def write_status(self):
        (self.config / 'live-status.json').write_text(json.dumps(self.status), encoding='utf-8')

    def call(self):
        return runner.run_event(self.context['event_id'], str(self.config), str(self.history))

    def valid_final(self, outcome='no_change'):
        return dict(event_id=self.context['event_id'], outcome=outcome, summary='Evidence reviewed.',
                    council_decision='not_needed', council_agents=[], changed_files=[], validation=[])

    def fake_cli(self, command, **kwargs):
        self.assertLess(kwargs['timeout'], review.LEASE_SECONDS)
        self.assertNotIn('--ephemeral', command)
        self.assertEqual(command[command.index('resume') + 1], self.sidebar_thread)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)
        self.assertNotIn('CODEX_THREAD_ID', kwargs['env'])
        kwargs['stdout'].write((json.dumps(dict(type='thread.started', thread_id=self.sidebar_thread)) + '\n' +
                                json.dumps(dict(type='turn.completed')) + '\n').encode())
        (self.directory / 'result.json').write_text(json.dumps(self.valid_final()), encoding='utf-8')
        (self.directory / 'progress.json').write_text(json.dumps(dict(event_id=self.context['event_id'],
                                                                    phase='completed', outcome='no_change')))
        (self.directory / 'decision.md').write_text('No new actionable evidence.')
        return 0

    def test_current_event_claim_does_not_consume_old_pending(self):
        old = review.make_launch_context(self.root, self.config, self.history)
        self.queue.enqueue(old, {})
        self.queue.enqueue(self.context, self.snapshot)
        claimed = self.queue.claim(str(uuid.uuid4()), event_id=self.context['event_id'])
        self.assertEqual(claimed['event_id'], self.context['event_id'])
        self.assertEqual(self.queue.claim(str(uuid.uuid4()), event_id=old['event_id'])['status'], 'busy')
        self.queue.complete(claimed['event_id'], claimed['event']['owner'], 'no_change', 'Done',
                            lease_token=claimed['lease_token'])
        self.assertEqual(self.queue.peek()['event_id'], old['event_id'])
        self.assertEqual(self.queue.claim(str(uuid.uuid4()), event_id=self.context['event_id'])['status'], 'not_pending')

    def test_targeted_missing_event_never_claims_other_event(self):
        self.queue.enqueue(self.context, self.snapshot)
        self.assertEqual(self.queue.claim(str(uuid.uuid4()), event_id=str(uuid.uuid4()))['status'], 'not_pending')
        self.assertEqual(self.queue.peek()['status'], 'pending')

    def test_one_completed_model_call_and_duplicate_runner_stays_quiet(self):
        self.queue.enqueue(self.context, self.snapshot)
        with patch.object(runner, '_app_alive', return_value=True), patch.object(runner, '_cli', return_value=['codex.exe']), \
                patch('startup_review_process.run_bounded', side_effect=self.fake_cli) as cli:
            result = self.call()
            self.assertEqual(result['status'], 'completed')
            self.assertTrue(result['thread_id'])
            self.assertEqual(self.call()['status'], 'not_pending')
            self.assertEqual(cli.call_count, 1)
        self.assertEqual(json.loads((self.directory / 'runner.json').read_text())['status'], 'completed')

    def test_busy_does_not_launch_or_replay_another_event(self):
        old = review.make_launch_context(self.root, self.config, self.history)
        self.queue.enqueue(old, {})
        self.queue.claim(str(uuid.uuid4()))
        self.queue.enqueue(self.context, self.snapshot)
        with patch('startup_review_process.run_bounded') as cli:
            self.assertEqual(self.call()['status'], 'busy')
        cli.assert_not_called()

    def test_invalid_freshness_and_identity_do_not_start_model(self):
        event = dict(self.context, ui_snapshot=self.snapshot)
        changes = [dict(valid=False), dict(running=False), dict(updated=time.time()-11),
                   dict(updated=time.time()+60), dict(app_instance_id=str(uuid.uuid4())),
                   dict(app_pid=999), dict(capture={'source': 'manual', 'hwnd': 42}),
                   dict(capture={'source': 'screen', 'hwnd': 43})]
        base = dict(self.status)
        with patch.object(runner, '_app_alive', return_value=True):
            self.assertTrue(runner._qualified(event))
            for change in changes:
                with self.subTest(change=change):
                    self.status = dict(base, **change)
                    self.write_status()
                    self.assertFalse(runner._qualified(event))

    def test_parent_exited_is_completed_without_model_or_restart(self):
        self.queue.enqueue(self.context, self.snapshot)
        with patch.object(runner, '_app_alive', return_value=False), patch('startup_review_process.run_bounded') as cli:
            result = self.call()
        self.assertEqual(result['status'], 'skipped_inactive')
        self.assertFalse(result['model_started'])
        cli.assert_not_called()
        self.prepare_sidebar.assert_not_called()

    def test_sidebar_failure_keeps_one_original_cli_review(self):
        self.prepare_sidebar.return_value = {'status': 'failed', 'error_type': 'TimeoutError'}
        self.queue.enqueue(self.context, self.snapshot)
        def original_cli(command, **kwargs):
            self.assertNotIn('resume', command)
            # Reuse result fixtures after independently checking the fallback.
            return self.fake_cli(command + ['resume', self.sidebar_thread], **kwargs)
        with patch.object(runner, '_qualified', return_value=True), patch.object(runner, '_cli', return_value=['codex.exe']), \
                patch('startup_review_process.run_bounded', side_effect=original_cli) as cli:
            result = self.call()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['sidebar']['status'], 'failed')
        self.assertEqual(cli.call_count, 1)

    def test_resume_identity_mismatch_is_not_reported_as_success(self):
        self.queue.enqueue(self.context, self.snapshot)
        def mismatched(command, **kwargs):
            code = self.fake_cli(command, **kwargs)
            kwargs['stdout'].seek(0)
            kwargs['stdout'].truncate()
            kwargs['stdout'].write((json.dumps({'type': 'thread.started', 'thread_id': str(uuid.uuid4())})
                                     + '\n' + json.dumps({'type': 'turn.completed'}) + '\n').encode())
            return code
        with patch.object(runner, '_qualified', return_value=True), patch.object(runner, '_cli', return_value=['codex.exe']), \
                patch('startup_review_process.run_bounded', side_effect=mismatched):
            self.assertEqual(self.call()['status'], 'failed')

    def test_launch_failure_and_timeout_are_blocked_without_retry(self):
        for error in (FileNotFoundError('secret'), subprocess.TimeoutExpired('codex', 1)):
            self.context = review.make_launch_context(self.root, self.config, self.history)
            self.directory = runner._run_dir(self.context['event_id'])
            self.queue.enqueue(self.context, self.snapshot)
            with patch.object(runner, '_qualified', return_value=True), patch.object(runner, '_cli', return_value=['codex.exe']), \
                    patch('startup_review_process.run_bounded', side_effect=error) as cli:
                result = self.call()
                self.assertIn(result['status'], ('failed', 'timed_out'))
                self.assertEqual(self.call()['status'], 'not_pending')
                self.assertEqual(cli.call_count, 1)
                self.assertNotIn('secret', json.dumps(result))

    def test_receipt_requires_success_event_schema_and_exact_identity(self):
        self.directory.mkdir(parents=True)
        final_path = self.directory / 'result.json'
        event_path = self.directory / 'events.jsonl'
        valid = self.valid_final()
        final_path.write_text(json.dumps(valid))
        for records in ([dict(type='thread.started', thread_id='id')],
                        [dict(type='thread.started', thread_id='id'), dict(type='turn.failed')],
                        [dict(type='turn.completed')]):
            event_path.write_text('\n'.join(map(json.dumps, records)))
            with self.assertRaises(ValueError):
                runner.validate_result(self.directory, self.context['event_id'], 0, {})
        event_path.write_text('\n'.join(map(json.dumps, [dict(type='thread.started', thread_id='id'), dict(type='turn.completed')])))
        for invalid in (dict(valid, event_id=str(uuid.uuid4())), dict(valid, unexpected=True),
                        dict(valid, outcome='implemented'), dict(valid, validation='claimed checks')):
            final_path.write_text(json.dumps(invalid))
            with self.assertRaises(ValueError):
                runner.validate_result(self.directory, self.context['event_id'], 0, {})
        final_path.write_text(json.dumps(valid))
        with self.assertRaises(ValueError):
            runner.validate_result(self.directory, self.context['event_id'], 1, {})

    def test_maintenance_dispatch_and_duplicate_dispatch_do_not_spawn(self):
        with patch.dict(os.environ, {'MAPLE_EXP_REVIEW_PARENT': str(uuid.uuid4())}):
            maintenance = review.make_launch_context(self.root, self.config, self.history)
        with patch.object(runner.subprocess, 'Popen') as launch:
            worker = runner.dispatch_async(maintenance, {})
            worker.join(3)
            self.queue.enqueue(self.context, self.snapshot)
            worker = runner.dispatch_async(self.context, self.snapshot)
            worker.join(3)
        launch.assert_not_called()

    def test_spawn_error_is_isolated_and_not_retried(self):
        with patch.object(runner, '_python', return_value='pythonw.exe'), \
                patch.object(runner.subprocess, 'Popen', side_effect=OSError('secret')) as launch:
            first = runner.dispatch_async(self.context, self.snapshot)
            first.join(3)
            second = runner.dispatch_async(self.context, self.snapshot)
            second.join(3)
        self.assertEqual(first.review_error, 'OSError')
        self.assertTrue(first.review_persisted)
        self.assertEqual(launch.call_count, 1)

    def test_named_permissions_replace_legacy_split_roots(self):
        arguments = runner.cli_permissions(r'C:\Users\Player\AppData\Local\MapleExpTracker')
        self.assertIn('default_permissions="maple_review"', arguments)
        self.assertIn('permissions.maple_review.extends=":workspace"', arguments)
        self.assertTrue(any('workspace_roots=' in arg and 'MapleExpTracker' in arg for arg in arguments))
        self.assertNotIn('-s', arguments)
        self.assertNotIn('--add-dir', arguments)

    def test_permissions_use_canonical_spelling_in_runner_namespace(self):
        with patch.object(runner.Path,'resolve',return_value=Path('C:/Users/Player/AppData/Local/MapleExpTracker')):
            arguments=runner.cli_permissions('c:/users/player/appdata/local/mapleexptracker')
        self.assertIn('permissions.maple_review.workspace_roots={"C:/Users/Player/AppData/Local/MapleExpTracker"=true}',arguments)

    def test_transient_progress_write_error_does_not_revoke_valid_lease(self):
        self.queue.enqueue(self.context,self.snapshot)
        def cli(command,**kwargs):
            code=self.fake_cli(command,**kwargs)
            kwargs['stdout'].flush()
            with patch.object(runner,'_write',side_effect=PermissionError('reader temporarily owns target')):
                self.assertTrue(kwargs['guard']())
            return code
        with patch.object(runner,'_qualified',return_value=True),patch.object(runner,'_cli',return_value=['codex.exe']), \
                patch('startup_review_process.run_bounded',side_effect=cli):
            self.assertEqual(self.call()['status'],'completed')

    def test_blocked_permission_result_preserves_reason_without_missing_receipts(self):
        self.queue.enqueue(self.context, self.snapshot)
        def blocked_cli(command, **kwargs):
            self.fake_cli(command, **kwargs)
            (self.directory / 'progress.json').unlink()
            (self.directory / 'decision.md').unlink()
            (self.directory / 'result.json').write_text(json.dumps(dict(self.valid_final('blocked'),
                summary='Shell permission policy rejected the required operation.', council_decision='HOLD')))
            return 0
        with patch.object(runner, '_qualified', return_value=True), patch.object(runner, '_cli', return_value=['codex.exe']), \
                patch('startup_review_process.run_bounded', side_effect=blocked_cli):
            result = self.call()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['outcome'], 'blocked')
        self.assertIn('permission policy', result['summary'])
        self.assertNotIn('error', result)

    def test_queue_failure_still_records_final_failure(self):
        self.queue.enqueue(self.context, self.snapshot)
        with patch.object(runner, '_qualified', return_value=True), patch.object(runner, '_cli', return_value=['codex.exe']), \
                patch('startup_review_process.run_bounded', side_effect=RuntimeError('guard lost')), \
                patch.object(review.Queue, 'complete', side_effect=OSError('database unavailable')):
            result = self.call()
        receipt = json.loads((self.directory / 'runner.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(receipt['completion_error'], 'OSError')
        self.assertIn('finished_at', receipt)

    def test_source_change_and_baseline_rewrite_cannot_pass_as_no_change(self):
        self.queue.enqueue(self.context, self.snapshot)
        def mutate(command, **kwargs):
            code = self.fake_cli(command, **kwargs)
            (self.root / 'changed.py').write_text('changed = True')
            (self.directory / 'baseline.json').write_text(json.dumps(runner._baseline(self.directory)))
            return code
        with patch.object(runner, '_qualified', return_value=True), patch.object(runner, '_cli', return_value=['codex.exe']), \
                patch('startup_review_process.run_bounded', side_effect=mutate):
            self.assertEqual(self.call()['status'], 'failed')
        self.assertTrue((self.root / 'changed.py').exists())  # Never blindly roll back.

    def test_claimed_council_ids_must_be_real_distinct_completed_children(self):
        self.directory.mkdir(parents=True)
        child1, child2, thread = (str(uuid.uuid4()) for _ in range(3))
        final = dict(self.valid_final('implemented'), council_decision='GO',
                     council_agents=['/root/expert', '/root/challenger'],
                     changed_files=['changed.py'], validation=['Regression reproducer passed.'])
        (self.directory / 'result.json').write_text(json.dumps(final))
        (self.directory / 'progress.json').write_text(json.dumps(dict(event_id=self.context['event_id'],
                                                                    phase='completed', outcome='implemented')))
        (self.directory / 'decision.md').write_text('GO; evidence and challenge resolved.')
        (self.directory / 'events.jsonl').write_text('\n'.join(map(json.dumps, [dict(type='thread.started', thread_id=thread), dict(type='turn.completed')])))
        (self.root / 'changed.py').write_text('changed = True')
        activity = [dict(kind=kind, agent_thread_id=identity, agent_path=path)
                    for identity, path in ((child1, '/root/expert'), (child2, '/root/challenger'))
                    for kind in ('started', 'completed')]
        with patch.object(runner, 'council_activity', return_value=activity):
            self.assertEqual(runner.validate_result(self.directory, self.context['event_id'], 0, {})[0]['outcome'], 'implemented')
            final['council_agents'] = ['/root/expert', child1]
            (self.directory / 'result.json').write_text(json.dumps(final))
            with self.assertRaises(ValueError):
                runner.validate_result(self.directory, self.context['event_id'], 0, {})

        final['council_agents'] = ['/root/expert', '/root/challenger']
        (self.directory / 'result.json').write_text(json.dumps(final))
        with patch.object(runner, 'council_activity', return_value=activity[:-1]):
            with self.assertRaises(ValueError):
                runner.validate_result(self.directory, self.context['event_id'], 0, {})

    def enable_reports(self):
        directory = self.root / 'work/startup-reviews'
        directory.mkdir(parents=True, exist_ok=True)
        target = str(uuid.uuid4())
        (directory / 'controller.json').write_text(json.dumps(dict(chat_reporting=dict(
            enabled=True, target_thread_id=target))), encoding='utf-8')
        transport = Mock()
        transport.error_type = None
        publisher = patch('startup_review_reporting.EventReporter', return_value=transport)
        self.addCleanup(publisher.stop)
        constructor = publisher.start()
        return transport, constructor, target

    def test_reports_use_fixed_target_and_structured_monotonic_stages(self):
        transport, constructor, target = self.enable_reports()
        self.directory.mkdir(parents=True)
        owner, thread = str(uuid.uuid4()), str(uuid.uuid4())
        reports = runner.ChatReports(self.directory, self.context['event_id'], owner, ['codex.exe'])
        constructor.assert_called_once_with(self.directory, self.context['event_id'], target, ['codex.exe'])
        receipt = dict(model_started=False, thread_id=thread, last_message='completed and GO!')
        reports.observe(receipt)
        transport.publish.assert_not_called()
        receipt['model_started'] = True
        reports.observe(receipt)
        def progress(**changes):
            value = dict(event_id=self.context['event_id'], owner=owner, phase='council', summary='Evidence ready')
            value.update(changes)
            (self.directory / 'progress.json').write_text(json.dumps(value), encoding='utf-8')
            reports.observe(receipt)
        progress(event_id=str(uuid.uuid4()))
        progress(owner=str(uuid.uuid4()))
        progress(phase='completed')
        self.assertEqual(transport.publish.call_count, 1)
        progress()
        progress()
        progress(phase='evidence_collection')
        progress(phase='implementing', council_decision='HOLD', council_agents=['a','b'])
        with patch.object(runner, 'council_activity', return_value=[]):
            progress(phase='implementing', council_decision='GO', council_agents=['a','b'])
        self.assertEqual(transport.publish.call_count, 2)
        progress(phase='validating')
        progress(phase='council')
        self.assertEqual([call.args[0] for call in transport.publish.call_args_list],
                         ['started', 'council', 'verification'])

    def test_implementation_report_requires_observed_independent_council(self):
        transport, _, _ = self.enable_reports()
        self.directory.mkdir(parents=True)
        owner, thread = str(uuid.uuid4()), str(uuid.uuid4())
        reports = runner.ChatReports(self.directory, self.context['event_id'], owner, ['codex.exe'])
        (self.directory / 'progress.json').write_text(json.dumps(dict(
            event_id=self.context['event_id'], owner=owner, phase='implementing',
            council_decision='GO', council_agents=['/root/expert', '/root/challenger'])))
        activity = [dict(kind=kind, agent_thread_id=identity, agent_path=path)
                    for identity, path in (('a','/root/expert'),('b','/root/challenger'))
                    for kind in ('started','completed')]
        with patch.object(runner, 'council_activity', return_value=activity):
            reports.observe(dict(model_started=True, thread_id=thread))
        self.assertEqual([call.args[0] for call in transport.publish.call_args_list], ['started','implementation'])

    def test_final_report_follows_validated_result_queue_and_receipt(self):
        transport, _, _ = self.enable_reports()
        self.queue.enqueue(self.context, self.snapshot)
        def verify_report(phase, payload):
            self.assertEqual(phase, 'completed')
            self.assertEqual(payload['outcome'], 'no_change')
            saved = json.loads((self.directory / 'runner.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['status'], 'completed')
            self.assertEqual(self.queue.claim(str(uuid.uuid4()), event_id=self.context['event_id'])['status'], 'not_pending')
        transport.publish.side_effect = verify_report
        with patch.object(runner, '_qualified', return_value=True), patch.object(runner, '_cli', return_value=['codex.exe']), \
                patch('startup_review_process.run_bounded', side_effect=self.fake_cli):
            self.assertEqual(self.call()['status'], 'completed')
        transport.publish.assert_called_once()
        transport.close.assert_called_once_with(timeout=30)

    def test_invalid_final_cannot_publish_completed_or_unvalidated_summary(self):
        transport, _, _ = self.enable_reports()
        self.queue.enqueue(self.context, self.snapshot)
        def invalid(command, **kwargs):
            code = self.fake_cli(command, **kwargs)
            (self.directory / 'changed.py').write_text('evidence only')
            (self.directory / 'result.json').write_text(json.dumps(dict(self.valid_final(), event_id=str(uuid.uuid4()),
                                                                      summary='Unvalidated success')))
            return code
        with patch.object(runner, '_qualified', return_value=True), patch.object(runner, '_cli', return_value=['codex.exe']), \
                patch('startup_review_process.run_bounded', side_effect=invalid):
            self.assertEqual(self.call()['status'], 'failed')
        self.assertEqual(transport.publish.call_args.args[0], 'failed')
        self.assertNotIn('Unvalidated success', json.dumps(transport.publish.call_args.args))
        transport.close.assert_called_once()

    def test_reporting_exceptions_do_not_kill_guard_or_change_outcome(self):
        transport, _, _ = self.enable_reports()
        transport.publish.side_effect = OSError('transport unavailable')
        transport.close.side_effect = RuntimeError('close failed')
        self.queue.enqueue(self.context, self.snapshot)
        def active(command, **kwargs):
            code = self.fake_cli(command, **kwargs)
            kwargs['stdout'].flush()
            self.assertTrue(kwargs['guard']())
            return code
        with patch.object(runner, '_qualified', return_value=True), patch.object(runner, '_cli', return_value=['codex.exe']), \
                patch('startup_review_process.run_bounded', side_effect=active):
            result = self.call()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['reporting_error'], 'RuntimeError')

    def test_inactive_and_duplicate_events_do_not_create_reporter(self):
        transport, constructor, _ = self.enable_reports()
        self.queue.enqueue(self.context, self.snapshot)
        with patch.object(runner, '_qualified', return_value=False):
            self.assertEqual(self.call()['status'], 'skipped_inactive')
            self.assertEqual(self.call()['status'], 'not_pending')
        constructor.assert_not_called()
        transport.publish.assert_not_called()

    def test_missing_final_receipt_reports_storage_failure_not_completion(self):
        transport, _, _ = self.enable_reports()
        self.queue.enqueue(self.context, self.snapshot)
        original = runner._write
        def write(path, value):
            if path.name == 'runner.json' and value.get('finished_at'):
                raise PermissionError('temporarily locked')
            return original(path, value)
        with patch.object(runner, '_qualified', return_value=True), patch.object(runner, '_cli', return_value=['codex.exe']), \
                patch('startup_review_process.run_bounded', side_effect=self.fake_cli), patch.object(runner, '_write', side_effect=write):
            result = self.call()
        self.assertEqual(result['status'], 'completed')  # Delivery/storage does not erase validated work.
        self.assertEqual(result['receipt_error'], 'PermissionError')
        self.assertEqual(transport.publish.call_args.args[0], 'failed')
        transport.close.assert_called_once()

    def test_disabled_publisher_failure_is_visible_without_failing_review(self):
        transport, _, _ = self.enable_reports()
        transport.error_type = 'PermissionError'
        transport.publish.return_value = False
        self.queue.enqueue(self.context, self.snapshot)
        with patch.object(runner, '_qualified', return_value=True), patch.object(runner, '_cli', return_value=['codex.exe']), \
                patch('startup_review_process.run_bounded', side_effect=self.fake_cli):
            result = self.call()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['reporting_error'], 'PermissionError')
        receipt = json.loads((self.directory / 'runner.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['reporting_error'], 'PermissionError')


if __name__ == '__main__':
    unittest.main()
