import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import startup_review_sidebar as sidebar


class SidebarTests(unittest.TestCase):
    def setUp(self):
        self.event, self.thread = str(uuid.uuid4()), str(uuid.uuid4())

    def test_prepares_named_persistent_thread_without_inference(self):
        calls = []
        def rpc(method, params):
            calls.append((method, params))
            if method in {'thread/start', 'thread/read'}:
                return {'thread': {'id': self.thread, 'source': 'vscode'}}
            return {}
        result = sidebar.prepare_with_rpc(rpc, self.event, 'E:/project')
        self.assertEqual(result['thread_id'], self.thread)
        self.assertEqual([method for method, _ in calls], [
            'initialize', 'thread/start', 'thread/name/set', 'thread/inject_items', 'thread/read'])
        self.assertIn(self.event[:8], calls[2][1]['name'])
        self.assertIn(self.event, calls[3][1]['items'][0]['content'][0]['text'])
        self.assertEqual(calls[1][1]['permissions'], 'maple_review')

    def test_unlistable_source_fails_before_seed(self):
        calls = []
        def rpc(method, params):
            calls.append(method)
            return {'thread': {'id': self.thread, 'source': 'exec'}}
        with self.assertRaises(ValueError):
            sidebar.prepare_with_rpc(rpc, self.event, 'E:/project')
        self.assertEqual(calls, ['initialize', 'thread/start'])

    def test_wrong_saved_identity_cannot_pass(self):
        def rpc(method, params):
            return {'thread': {'id': str(uuid.uuid4()) if method == 'thread/read' else self.thread,
                               'source': 'vscode'}}
        with self.assertRaises(ValueError):
            sidebar.prepare_with_rpc(rpc, self.event, 'E:/project')

    def test_helper_failure_is_recorded_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('startup_review_process.run_bounded', side_effect=TimeoutError('private details')) as run:
                result = sidebar.prepare_sidebar_thread(directory, self.event, ['codex.exe'], [],
                                                        directory, {}, lambda: True)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(result, {'status': 'failed', 'error_type': 'TimeoutError'})
            saved = json.loads((Path(directory)/'sidebar.json').read_text(encoding='utf-8'))
            self.assertEqual(saved, result)


if __name__ == '__main__':
    unittest.main()
