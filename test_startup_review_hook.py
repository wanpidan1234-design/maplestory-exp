"""First accepted game evidence arms one review; no real CLI or GUI is started."""
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4
import unittest

import numpy as np

import orb
import startup_review
import test_rate_app as harness
from tracker import Reading


class StartupReviewHookTests(unittest.TestCase):
    def setUp(self):
        harness.RateAppTests.setUp(self)
        self.backend.region = {'game_window': True}
        patch.object(orb,'CONFIG',self.config).start()
        patch.dict('os.environ',{'MAPLE_EXP_REVIEW_PARENT':''}).start()
        self.published = []
        self.runner = ModuleType('startup_review_runner')
        self.runner.dispatch_async = Mock(side_effect=self.dispatched)
        patch.dict(sys.modules,{'startup_review_runner':self.runner}).start()
        self.companion = SimpleNamespace(review_snapshot=Mock(return_value={
            'windows':{'orb':{'visible':True}},'running':True}))

    def dispatched(self, context, snapshot):
        # If this read fails, dispatch happened before the fresh checkpoint.
        status = json.loads(self.config.with_name('live-status.json').read_text(encoding='utf-8'))
        self.published.append((context,snapshot,status))
        return SimpleNamespace(review_error=None,review_persisted=True)

    def install(self):
        orb.install_startup_review(self.backend.root,self.backend,self.companion)

    def receive(self, at=100, value=0, *, observed_at=None, generation=None,
                source='screen', hwnd=12345, error=None):
        b=self.backend
        self.now[0]=at
        info=dict(source=source,hwnd=hwnd,observed_at=at if observed_at is None else observed_at)
        if error:
            info['failure_stage']='experience_ocr'
        b.events.put((b.generation if generation is None else generation,
            np.zeros((8,20,3),dtype=np.uint8),str(value),
            Reading(value) if value is not None else None,error,info))
        b.tick()

    def test_install_only_arms_callback_and_is_idempotent(self):
        self.install()
        b=self.backend
        context,callback=b.review_launch,b.review_on_game_sample
        self.install()
        self.assertIs(b.review_launch,context)
        self.assertIs(b.review_on_game_sample,callback)
        b.root.after_idle.assert_not_called()
        self.runner.dispatch_async.assert_not_called()
        self.assertFalse(b.review_sample_fired)

    def test_imported_backend_without_install_never_dispatches(self):
        self.receive()
        self.assertTrue(self.backend.reading_valid)
        self.runner.dispatch_async.assert_not_called()
        self.assertFalse(hasattr(self.backend,'review_launch'))

    def test_real_zero_baseline_dispatches_once_after_fresh_status_is_written(self):
        self.install()
        self.receive(100,0)
        b=self.backend
        self.assertTrue(b.review_sample_fired)
        self.assertEqual(b.records[0][7],'baseline')
        self.assertIsNone(b.primary_rate_snapshot()['value'])
        context,snapshot,status=self.published[0]
        qualified=snapshot['qualified_sample']
        self.assertEqual(qualified,dict(source='screen',hwnd=12345,observed_at=100,
                                      sampled_at=b.last_valid_time))
        self.assertEqual(status['app_instance_id'],context['instance_id'])
        self.assertEqual(status['capture']['observed_at'],qualified['observed_at'])
        self.assertEqual(status['last_valid'],qualified['sampled_at'])
        self.assertEqual(status['current'],0)
        self.assertTrue(status['valid'])
        self.assertTrue(status['running'])
        self.receive(101,0)
        self.receive(101,0)  # A repeated observation is never another event.
        self.install()     # Re-installing cannot rearm a completed launch.
        self.receive(102,1)
        self.runner.dispatch_async.assert_called_once()

    def test_failed_expired_and_old_generation_events_do_not_arm_dispatch(self):
        self.install()
        for values in (dict(value=None),dict(error='OCR failed'),dict(observed_at=90),
                       dict(generation=-1)):
            with self.subTest(values=values):
                self.receive(**values)
                self.assertFalse(self.backend.review_sample_fired)
                self.runner.dispatch_async.assert_not_called()
        self.receive(101,10)
        self.runner.dispatch_async.assert_called_once()

    def test_paused_manual_capture_and_invalid_window_are_not_game_evidence(self):
        self.install()
        b=self.backend
        b.running=False
        self.receive(100,10)
        self.runner.dispatch_async.assert_not_called()
        b.running=True
        b.region={'left':0,'top':0,'width':100,'height':20}
        self.receive(101,11)
        self.runner.dispatch_async.assert_not_called()
        b.region={'game_window':True}
        self.receive(102,12,source='manual')
        self.receive(103,13,hwnd=0)
        self.receive(104,14,hwnd=-1)
        self.receive(105,15,hwnd=True)
        self.runner.dispatch_async.assert_not_called()
        self.assertFalse(b.review_sample_fired)
        self.receive(106,16)
        self.runner.dispatch_async.assert_called_once()

    def test_repeated_timestamp_does_not_qualify_a_previous_manual_sample(self):
        self.install()
        self.receive(100,10,source='manual')
        self.assertTrue(self.backend.fresh_reading())
        self.receive(100,10,source='screen')
        self.assertTrue(self.backend.fresh_reading())
        self.runner.dispatch_async.assert_not_called()
        self.receive(101,10,source='screen')
        self.runner.dispatch_async.assert_called_once()

    def test_retry_and_restored_display_are_not_accepted_game_events(self):
        self.install()
        b=self.backend
        self.receive(100,10,source='manual')
        self.receive(101,20,source='manual')
        self.receive(101.5,None)
        self.assertEqual(b.display_rate_snapshot()['display_state'],'retrying')
        self.runner.dispatch_async.assert_not_called()
        state=b.tracker.export_resume_state()
        self.assertTrue(b.tracker.restore_resume_state(state))
        b.reading_valid=False;b.latest=None
        b.last_valid_time=b.last_valid_monotonic=None
        b.clear_rate_display()
        b.tick()  # Empty queue may display saved rates, but cannot dispatch.
        self.assertEqual(b.display_rate_snapshot()['display_state'],'restored')
        self.runner.dispatch_async.assert_not_called()
        self.receive(102,25)  # A new game baseline after restoration qualifies.
        self.assertFalse(b.tracker.resume_pending)
        self.runner.dispatch_async.assert_called_once()

    def test_unsaved_accounting_and_failed_status_checkpoint_never_dispatch(self):
        self.install()
        with patch.object(self.backend,'record',return_value=False):
            self.receive(100,10)
        self.assertFalse(self.backend.review_sample_fired)
        self.runner.dispatch_async.assert_not_called()
        with patch.object(self.backend,'write_status',return_value=False):
            self.receive(101,20)
            self.receive(102,30)
        self.assertTrue(self.backend.review_sample_fired)
        self.assertEqual(self.backend.startup_review_error,'OSError')
        self.runner.dispatch_async.assert_not_called()

    def test_optional_dispatch_failure_preserves_accounting_without_retry_storm(self):
        self.install()
        self.runner.dispatch_async.side_effect=RuntimeError('fixture dispatch failure')
        self.receive(100,10)
        self.receive(101,20)
        self.assertEqual(self.backend.tracker.total,10)
        self.assertEqual(self.backend.startup_review_error,'RuntimeError')
        self.assertIsNone(self.backend.storage_error)
        self.runner.dispatch_async.assert_called_once()

    def test_maintenance_records_one_local_event_without_dispatch(self):
        with patch.dict('os.environ',{'MAPLE_EXP_REVIEW_PARENT':str(uuid4())}), \
             patch.object(startup_review,'emit_startup_async') as emit:
            emit.return_value=SimpleNamespace(review_error=None,review_persisted=True)
            self.install()
            self.install()
            b=self.backend
            self.assertFalse(b.review_launch['review_required'])
            self.assertIsNone(b.review_on_game_sample)
            b.root.after_idle.assert_called_once()
            b.root.after_idle.call_args.args[0]()
            self.receive(100,0)
            emit.assert_called_once()
            self.runner.dispatch_async.assert_not_called()


if __name__=='__main__':
    unittest.main()
