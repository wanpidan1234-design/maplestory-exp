"""Optional review metadata must not damage the actual accounting/status path."""
import unittest
from types import SimpleNamespace
from uuid import uuid4

import test_rate_app as harness


class StartupReviewStatusTests(unittest.TestCase):
    setUp = harness.RateAppTests.setUp
    receive = harness.RateAppTests.receive
    last_row = harness.RateAppTests.last_row
    status_json = harness.RateAppTests.status_json

    def test_public_status_identifies_runtime_and_preserves_exp(self):
        b = self.backend
        identity = str(uuid4())
        b.review_launch = dict(instance_id=identity, pid=12345,
                               created_at='2026-10-05T17:00:00+00:00')
        b.review_ui_snapshot = lambda: dict(windows={'panel': {'visible': True}})
        self.receive(100, 1000)
        self.receive(101, 1050)
        state = self.status_json()
        self.assertEqual(state['app_instance_id'], identity)
        self.assertEqual(state['app_pid'], 12345)
        self.assertEqual(state['app_started_at'], b.review_launch['created_at'])
        self.assertTrue(state['ui_windows']['windows']['panel']['visible'])
        self.assertEqual(state['total'], 50)
        self.assertEqual(self.last_row()['current_exp'], 1050)

    def test_optional_ui_metadata_failure_still_publishes_accounting(self):
        def unavailable():
            raise RuntimeError('window closed during metadata collection')
        self.backend.review_ui_snapshot = unavailable
        self.receive(100, 1000)
        self.receive(101, 1050)
        state = self.status_json()
        self.assertEqual(state['ui_snapshot_error'], 'RuntimeError')
        self.assertEqual(state['total'], 50)
        self.assertEqual(self.last_row()['current_exp'], 1050)
        self.assertIsNone(self.backend.storage_error)

    def test_background_review_failure_is_visible_without_stopping_records(self):
        self.backend.startup_review_thread = SimpleNamespace(review_error='PermissionError', review_persisted=False)
        self.receive(100, 1000)
        self.receive(101, 1050)
        state = self.status_json()
        self.assertEqual(state['startup_review_error'], 'PermissionError')
        self.assertFalse(state['startup_review_queued'])
        self.assertEqual(state['total'], 50)
        self.assertIsNone(self.backend.storage_error)


if __name__ == '__main__':
    unittest.main()
