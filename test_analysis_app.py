"""Exercise diagnostics on the real result/SQLite path with controlled failures."""
import json
import queue
import sqlite3
import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

import app
from analysis_log import AnalysisJournal
from recognition_diagnostics import AsyncRecognitionJournal
from tracker import Reading
import test_rate_app as harness

REAL_THREAD = threading.Thread


class AnalysisAppTests(unittest.TestCase):
    def setUp(self):
        harness.RateAppTests.setUp(self)
        self.encoded=[]
        self.journal=self.make_journal()

    def make_journal(self, **options):
        with patch.object(threading,'Thread',REAL_THREAD):
            journal=AnalysisJournal(self.config.parent/'logs',sink=options.pop('sink',self.encoded.append),**options)
        self.backend.analysis_journal=journal
        self.addCleanup(lambda:journal.close(timeout=1))
        return journal

    def receive(self, at, value, *, captured=None, error=None, extra=None):
        b=self.backend;self.now[0]=at
        sample=self.journal.next_sample_id()
        queued=time.monotonic()
        info=dict(source='manual',observed_at=at if captured is None else captured,
                  analysis=dict(sample_id=sample,generation=b.generation,
                                worker_started_at=queued-.01,enqueued_at=queued))
        info.update(extra or {})
        b.events.put((b.generation,np.zeros((8,20,3),dtype=np.uint8),'private OCR text',
                      Reading(value,None,25.5) if value is not None else None,error,info))
        b.tick()
        return sample

    def rows(self):
        self.assertTrue(self.journal.close(timeout=2))
        return [json.loads(row) for row in self.encoded]

    def test_failure_hold_recovery_separates_display_from_ledger_and_gap_from_reset(self):
        b=self.backend
        self.receive(100,1000);self.receive(101,1100)
        self.receive(102,None,extra={'recognition':{'path':'bracket','reason':'absolute_conflict'}})
        self.now[0]=107;b.tick()
        resumed=self.receive(108,1500)
        rows=self.rows()
        self.assertEqual(b.tracker.total,500)
        self.assertEqual(b.tracker.windows.snapshot()['1800']['observed_seconds'],1)
        rejected=next(r['fields'] for r in rows if r['event']=='sample_decision' and r['fields']['outcome']=='ocr_rejected')
        self.assertEqual(rejected['ocr_reason'],'absolute_conflict')
        self.assertIsNotNone(rejected['queue_age'])
        gap=next(r['fields'] for r in rows if r['event']=='sample_decision' and r['fields']['outcome']=='gap')
        self.assertEqual(gap['sample_id'],resumed)
        self.assertEqual(gap['gain_disposition'],'ledger_only')
        self.assertEqual(gap['before']['observed_seconds'],gap['after']['observed_seconds'])
        self.assertEqual(gap['before']['windows']['60']['sum'],gap['after']['windows']['60']['sum'])
        held=next(r['fields'] for r in rows if r['event']=='display_transition' and r['fields']['display_state']=='held')
        self.assertEqual(held['raw_state'],'stale')
        self.assertTrue(held['presence']['primary'])
        committed=next(r['fields'] for r in rows if r['event']=='history_committed' and r['fields']['sample_id']==resumed)
        self.assertTrue(committed['persisted'])
        self.assertEqual(committed['history_seq'],3)
        self.assertNotIn('private OCR text',b''.join(self.encoded).decode())

    def test_failed_sqlite_save_is_never_reported_committed(self):
        self.receive(100,1000);self.receive(101,1100)
        with patch.object(self.backend.history,'append',side_effect=sqlite3.OperationalError('secret/path disk full')):
            failed=self.receive(102,1200)
        rows=self.rows()
        self.assertFalse(any(r['event']=='history_committed' and r['fields'].get('sample_id')==failed for r in rows))
        row=next(r['fields'] for r in rows if r['event']=='history_commit_failed')
        self.assertEqual(row['error_type'],'OperationalError')
        self.assertFalse(row['persisted'])
        self.assertTrue(row['attempted'])
        self.assertFalse(self.backend.running)
        self.assertEqual(self.backend.history.db.execute('SELECT SUM(gain) FROM samples').fetchone()[0],100)
        self.assertNotIn('secret/path',b''.join(self.encoded).decode())

    def test_blocked_text_sink_and_queue_full_do_not_stop_acceptance_or_close(self):
        gate=threading.Event();entered=threading.Event()
        self.addCleanup(gate.set)
        def sink(content):
            entered.set();gate.wait(5)
        self.journal.close(timeout=1)
        self.journal=self.make_journal(sink=sink,queue_size=2)
        self.journal.emit('sink_test')
        self.assertTrue(entered.wait(1))
        self.receive(100,1000);self.receive(101,1100);self.receive(102,1200)
        self.assertFalse(gate.is_set())
        self.assertEqual(self.backend.tracker.total,200)
        self.assertEqual(self.backend.history.seq,3)
        self.assertGreater(self.journal.status()['queue_full'],0)
        started=time.monotonic()
        self.assertFalse(self.journal.close(timeout=.01))
        self.assertLess(time.monotonic()-started,.5)
        gate.set()

    def test_blocked_roi_writer_does_not_block_failed_result_or_next_good_sample(self):
        b=self.backend
        self.receive(100,1000)
        gate=threading.Event();entered=threading.Event()
        self.addCleanup(gate.set)
        class Sink:
            last_error=None
            def record(self,*args):
                entered.set();gate.wait(5);return None
        with patch.object(threading,'Thread',REAL_THREAD):
            evidence=AsyncRecognitionJournal(self.config.parent/'roi',journal=Sink(),min_interval=0)
        self.addCleanup(lambda:evidence.close(timeout=1))
        b.recognition_journal=evidence
        b.capture=Mock(level_frame=None)
        b.engine=Mock(last_diagnostics={'path':'rejected','reason':'invalid_frame'})
        b.engine.read.side_effect=[(None,[]),(Reading(1100),[])]
        with patch.object(app,'read_level',return_value=(None,[])):
            for at in (101,102):
                self.now[0]=at
                b.capture.capture.return_value=(np.zeros((8,20,3),dtype=np.uint8),{'source':'screen','observed_at':at})
                b.worker(b.generation,{'game_window':True})
                self.assertEqual(b.events.qsize(),1)
                b.tick()
                if at==101:self.assertTrue(entered.wait(1))
        self.assertFalse(gate.is_set())
        self.assertEqual(b.tracker.total,100)
        self.assertEqual(b.history.seq,2)
        self.assertFalse(evidence.close(timeout=.01))
        gate.set()

    def test_expired_and_old_generation_are_explicit_discard_events(self):
        b=self.backend
        self.receive(100,1000);self.receive(101,1100)
        old=self.receive(108,9999,captured=102)
        info={'observed_at':109,'analysis':{'sample_id':999,'generation':-1}}
        b.events.put((-1,None,'',None,None,info));b.tick()
        rows=self.rows()
        reasons={(r['fields'].get('sample_id'),r['fields'].get('outcome')) for r in rows if r['event']=='sample_decision'}
        self.assertIn((old,'expired_frame'),reasons)
        self.assertIn((999,'generation_discard'),reasons)
        self.assertEqual(b.tracker.total,100)

    def test_manual_control_cache_clear_reasons_and_renderer_transitions(self):
        b=self.backend
        self.receive(100,1000);self.receive(101,1000)
        b.diagnostic_surface('panel',b.display_rate_snapshot())
        b.diagnostic_surface('panel',b.display_rate_snapshot())
        b.pause()
        b.diagnostic_surface('panel',b.display_rate_snapshot())
        rows=self.rows()
        controls=[r['fields'] for r in rows if r['event']=='surface_configured']
        self.assertEqual(len(controls),2)
        self.assertTrue(controls[0]['presence']['primary'])  # Genuine zero is present.
        self.assertFalse(controls[1]['presence']['primary'])
        self.assertTrue(any(r['event']=='display_cache_cleared' and r['fields']['reason']=='manual_pause' for r in rows))

    def test_visibility_and_restart_baselines_keep_distinct_reasons(self):
        b=self.backend
        self.receive(100,1000);self.receive(101,1100)
        b.region={'game_window':True}
        self.receive(102,None,error='hidden',extra=dict(source='screen',hwnd=10,
            failure_kind='visibility_interrupted',visibility_reason='occluded',failure_stage='occluded'))
        hidden_return=self.receive(150,9000)
        saved=b.tracker.export_resume_state()
        self.assertTrue(b.tracker.restore_resume_state(saved))
        restart_return=self.receive(200,11000)
        rows=self.rows()
        reasons={r['fields']['sample_id']:r['fields'] for r in rows
                 if r['event']=='sample_decision' and r['fields']['outcome']=='baseline'}
        self.assertEqual(reasons[hidden_return]['baseline_reason'],'visibility_return')
        self.assertEqual(reasons[restart_return]['baseline_reason'],'restart_resume')
        self.assertEqual(reasons[hidden_return]['gain_disposition'],'baseline_only')
        self.assertEqual(reasons[restart_return]['before']['observed_seconds'],1)
        self.assertEqual(reasons[restart_return]['after']['observed_seconds'],1)
        self.assertEqual(b.tracker.total,100)

    def test_tick_exception_is_logged_and_still_propagates(self):
        with patch.object(self.backend,'_tick',side_effect=RuntimeError('private detail')):
            with self.assertRaises(RuntimeError):self.backend.tick()
        rows=self.rows()
        self.assertEqual(next(r['fields']['error_type'] for r in rows if r['event']=='tick_exception'),'RuntimeError')
        self.assertNotIn('private detail',b''.join(self.encoded).decode())

    def test_new_rejection_reason_is_not_hidden_by_same_outcome_throttle(self):
        self.receive(100,1000);self.receive(101,1100)
        for at,reason in ((101.2,'absolute_conflict'),(101.3,'absolute_conflict'),
                          (101.4,'foreground_touches_crop_edge')):
            self.receive(at,None,extra={'recognition':{'path':'rejected','reason':reason}})
        self.receive(101.5,1110)
        rows=self.rows()
        reasons=[r['fields']['ocr_reason'] for r in rows if r['event']=='sample_decision'
                 and r['fields']['outcome']=='ocr_rejected']
        self.assertIn('foreground_touches_crop_edge',reasons)

    def test_worker_new_exception_type_is_logged_without_waiting_for_throttle(self):
        b=self.backend
        for index,error in enumerate(('RuntimeError','RuntimeError','PermissionError')):
            trace=dict(sample_id=index+1,generation=0,worker_started_at=time.monotonic())
            info=dict(failure_stage='capture',error_type=error)
            b.queue_worker_result(0,None,'',None,'private',info,trace)
        rows=self.rows()
        self.assertIn('PermissionError',[r['fields']['error_type'] for r in rows if r['event']=='worker_completed'])

    def test_vote_span_uses_captured_times_and_roi_has_cross_run_identity(self):
        b=self.backend
        self.receive(100,1000);self.receive(101,1100)
        b.auto_level.candidate=62;b.auto_level.count=2
        b.auto_level.since=101;b.auto_level.last_time=101.2;b.auto_level.wait_since=101
        self.now[0]=104.2
        b.diagnostic_result('level_pending',{'observed_at':101.2},b.diagnostic_state(),delta=0)
        b.recognition_journal=Mock(last_error=None)
        b.recognition_journal.record.return_value=None
        b.diagnose_failure('experience_rejected',{'sample_id':7},b.generation)
        metadata=b.recognition_journal.record.call_args.args[1]
        self.assertEqual(metadata['run_id'],self.journal.run_id)
        self.assertEqual(metadata['sample_id'],7)
        rows=self.rows()
        level=next(r['fields']['level'] for r in rows if r['event']=='sample_decision' and r['fields']['outcome']=='level_pending')
        self.assertAlmostEqual(level['vote_span'],.2)
        self.assertAlmostEqual(level['wait_age'],3.2)

    def test_normal_anchor_is_shared_by_worker_decision_and_commit(self):
        b=self.backend
        anchors={1,4,7}
        for sample in range(1,9):
            self.now[0]=100+sample
            trace=dict(sample_id=sample,generation=0,worker_started_at=time.monotonic(),anchor=sample in anchors)
            b.queue_worker_result(0,np.zeros((8,20,3),dtype=np.uint8),'',Reading(1000+sample*10),None,
                                  {'observed_at':self.now[0]},trace)
            b.tick()
        rows=self.rows()
        for event in ('worker_completed','sample_decision','history_committed'):
            actual={r['fields']['sample_id'] for r in rows if r['event']==event and r['fields'].get('anchor')}
            self.assertEqual(actual,anchors,event)

    def test_private_callback_function_name_survives_without_traceback_or_message(self):
        b=self.backend;b._previous_callback_exception=Mock()
        def _failing_callback():raise ValueError('do not log this private message')
        try:
            _failing_callback()
        except ValueError as error:
            b.report_callback_exception(type(error),error,error.__traceback__)
        rows=self.rows()
        fields=next(r['fields'] for r in rows if r['event']=='ui_callback_exception')
        self.assertEqual(fields['action'],'_failing_callback')
        self.assertNotIn('private message',b''.join(self.encoded).decode())


if __name__=='__main__':
    unittest.main()
