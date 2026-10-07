"""Visibility suspension versus genuine gaps, using the real queue and SQLite."""
from copy import deepcopy
import sqlite3
import unittest
from unittest.mock import Mock, patch

import numpy as np

import game_capture
from multiscale_view import window_row
from orb import rate_window_display
import test_rate_app as harness
from tracker import Reading, Tracker, WINDOW_SECONDS


class ScreenSwitchAppTests(unittest.TestCase):
    def setUp(self):
        harness.RateAppTests.setUp(self)
        self.backend.region={'game_window':True}

    def receive(self, at, value=None, *, stage=None, visibility=False, monitor=1,
                observed_at=None, generation=None):
        b=self.backend
        self.now[0]=at
        info=dict(source='screen',hwnd=7,observed_at=at if observed_at is None else observed_at,
                  monitor=dict(index=monitor,is_primary=monitor==1))
        if stage:
            info['failure_stage']=stage
        if visibility:
            info.update(failure_kind='visibility_interrupted',visibility_reason=stage)
        b.events.put((b.generation if generation is None else generation,
            np.zeros((8,20,3),dtype=np.uint8),str(value),
            Reading(value) if value is not None else None,'fixture' if stage else None,info))
        b.tick()

    def sampled(self, seconds=640):
        self.receive(100,1000)
        for elapsed in range(4,seconds+1,4):
            self.receive(100+elapsed,1000+elapsed*10)
        return deepcopy(self.backend.tracker.windows.snapshot())

    def test_short_monitor_change_and_movement_keep_all_window_progress(self):
        before=self.sampled()
        b=self.backend
        segment,generation=b.tracker.live.segment,b.generation
        self.receive(741,stage='window_moved',monitor=2)
        self.assertFalse(b.tracker.visibility_interrupted)
        self.assertEqual(b.tracker.windows.snapshot(),before)
        missing=b.display_rate_snapshot()
        for seconds in WINDOW_SECONDS:
            rendered=rate_window_display(missing,seconds)
            self.assertEqual(rendered['number'],'—')
            self.assertAlmostEqual(rendered['ratio'],before[str(seconds)]['observed_seconds']/seconds)
        self.receive(744.5,7445,monitor=2)
        self.assertEqual(b.generation,generation)
        self.assertEqual(b.tracker.live.segment,segment)
        for seconds,row in b.tracker.windows.snapshot().items():
            self.assertEqual(row['observed_seconds'],min(int(seconds),644.5))

    def test_proven_long_occlusion_freezes_and_resumes_without_hidden_gain_or_time(self):
        before=self.sampled()
        b=self.backend
        total,segment,count=b.tracker.total,b.tracker.live.segment,b.tracker.confirmed_levels
        self.receive(741,stage='occluded',visibility=True)
        elapsed=b.tracker.elapsed
        for at in (745,800,1100):
            self.receive(at,stage='occluded',visibility=True)
            frozen=b.display_rate_snapshot()
            self.assertEqual(frozen['display_state'],'visibility_paused')
            self.assertTrue(frozen['visibility_preserved'])
            self.assertEqual(frozen['windows'],before)
            self.assertEqual(b.tracker.elapsed,elapsed)
            self.assertEqual(b.tracker.total,total)
            self.assertFalse(b.fresh_reading())
            for seconds in WINDOW_SECONDS:
                rendered=rate_window_display(frozen,seconds)
                self.assertFalse(rendered['number'].startswith('≈'))
                self.assertEqual(rendered['caption'],'切屏暂停')
                self.assertEqual(window_row(frozen,seconds)[3],'切屏暂停')
        self.receive(1101,9)  # Hidden upgrades/EXP changes only set the new baseline.
        self.assertFalse(b.tracker.visibility_interrupted)
        self.assertFalse(b.tracker.resume_pending)
        self.assertEqual(b.tracker.windows.snapshot(),before)
        self.assertEqual(b.tracker.total,total)
        self.assertEqual(b.tracker.confirmed_levels,count)
        self.assertEqual(b.tracker.elapsed,elapsed)
        self.assertEqual(b.tracker.live.segment,segment+1)  # A truthful chart break.
        row=dict(b.history.db.execute('SELECT * FROM samples ORDER BY id DESC LIMIT 1').fetchone())
        self.assertEqual((row['status'],row['gain']),('baseline',0))
        self.receive(1102,19)
        self.assertEqual(b.tracker.total,total+10)
        self.assertEqual(b.tracker.elapsed,elapsed+1)
        self.assertEqual(b.tracker.windows.snapshot()['1800']['observed_seconds'],641)

    def test_visibility_resume_snapshot_is_durable_and_preserves_lower_bound(self):
        self.sampled(120)
        b=self.backend
        b.tracker.confirmed_levels=1;b.tracker.level_gaps=1
        b.tracker.windows.gaps.append(220)
        b.tracker.live.has_gaps=True
        expected=b.tracker.windows.snapshot()
        self.receive(221,stage='minimized',visibility=True)
        frozen=b.display_rate_snapshot()
        self.assertTrue(rate_window_display(frozen,120)['number'].startswith('≥ '))
        self.assertTrue(window_row(frozen,120)[1].startswith('≥ '))
        saved=b.history.load_resume_state()
        now=[1.0];restored=Tracker(lambda:now[0])
        self.assertTrue(restored.restore_resume_state(saved))
        self.assertEqual(restored.resume_rate_snapshot()['windows'],expected)
        now[0]=100000
        self.assertEqual(restored.feed(Reading(999999)),('baseline',0))
        self.assertEqual(restored.windows.snapshot(),expected)
        self.assertEqual(restored.total,b.tracker.total)

    def test_visible_ocr_after_return_preserves_the_held_continuation(self):
        self.sampled(120)
        b=self.backend
        total=b.tracker.total
        self.receive(221,stage='minimized',visibility=True)
        self.receive(300,None)  # Pixels returned, but OCR is not yet trustworthy.
        self.assertTrue(b.display_rate_snapshot()['visibility_preserved'])
        self.now[0]=305;b.tick()
        self.assertTrue(b.display_rate_snapshot()['visibility_preserved'])
        self.now[0]=305.01;b.tick()
        self.assertTrue(b.display_rate_snapshot()['visibility_preserved'])
        self.assertEqual(b.display_rate_snapshot()['primary']['value'],600)
        self.receive(306,20000)
        self.assertEqual(b.tracker.total,total)
        self.assertEqual(b.tracker.windows.snapshot()['1800']['observed_seconds'],120)
        self.receive(307,20010)
        self.assertEqual(b.tracker.total,total+10)

    def test_ordinary_visible_ocr_long_gap_preserves_windows_and_ledger_rules(self):
        self.sampled(120)
        b=self.backend
        segment,total=b.tracker.live.segment,b.tracker.total
        self.receive(221,None)
        self.assertFalse(b.tracker.visibility_interrupted)
        self.now[0]=225.01;b.tick()
        self.assertEqual(b.display_rate_snapshot()['state'],'stale')
        self.assertEqual(rate_window_display(b.display_rate_snapshot(),120)['ratio'],0)
        self.receive(226,2300)
        self.assertEqual(b.tracker.live.segment,segment+1)
        self.assertEqual(b.tracker.total,total+100)
        self.assertEqual(b.tracker.windows.snapshot()['1800']['observed_seconds'],120)
        row=b.history.db.execute('SELECT status FROM samples ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(row[0],'gap')

    def test_unknown_capture_failure_after_hidden_interval_preserves_samples(self):
        self.sampled(120)
        b=self.backend
        self.receive(221,stage='occluded',visibility=True)
        self.receive(300,stage='capture')
        self.now[0]=306;b.tick()
        self.assertTrue(b.display_rate_snapshot()['visibility_preserved'])
        self.receive(307,3000)
        self.assertEqual(b.tracker.windows.snapshot()['1800']['observed_seconds'],120)

    def test_manual_pause_discards_visibility_continuation(self):
        self.sampled(120)
        b=self.backend
        self.receive(221,stage='minimized',visibility=True)
        b.pause()
        self.assertFalse(b.tracker.visibility_interrupted)
        self.assertFalse(b.tracker.resume_pending)
        self.assertTrue(all(row['observed_seconds']==0 for row in b.tracker.windows.snapshot().values()))
        b.toggle()
        self.receive(300,9000)
        self.assertEqual(b.tracker.total,1200)
        self.assertEqual(b.tracker.windows.snapshot()['1800']['observed_seconds'],0)

    def test_relocating_an_already_automatic_game_does_not_restart_statistics(self):
        before=self.sampled(120)
        b=self.backend
        generation,segment,total=b.generation,b.tracker.live.segment,b.tracker.total
        b.auto_game();b.auto_game()
        self.assertEqual(b.generation,generation)
        self.assertEqual(b.tracker.live.segment,segment)
        self.assertEqual(b.tracker.total,total)
        self.assertEqual(b.tracker.windows.snapshot(),before)

    def test_old_generation_and_stale_visibility_evidence_cannot_suspend_new_data(self):
        before=self.sampled(120)
        b=self.backend
        self.receive(221,stage='occluded',visibility=True,generation=b.generation-1)
        self.assertFalse(b.tracker.visibility_interrupted)
        self.receive(221,stage='occluded',visibility=True,observed_at=219)
        self.assertFalse(b.tracker.visibility_interrupted)
        self.assertEqual(b.tracker.windows.snapshot(),before)
        self.receive(222,2220)
        self.assertEqual(b.tracker.windows.snapshot()['1800']['observed_seconds'],122)

    def test_first_minimized_start_has_no_invented_saved_rates(self):
        self.receive(100,stage='minimized',visibility=True)
        state=self.backend.display_rate_snapshot()
        self.assertEqual(state['display_state'],'visibility_paused')
        self.assertFalse(state['visibility_preserved'])
        self.assertIsNone(state['primary']['value'])
        self.receive(200,50000)
        self.assertEqual(self.backend.tracker.total,0)
        self.assertEqual(self.backend.tracker.elapsed,0)

    def test_delayed_error_during_hidden_interval_cannot_expire_saved_progress(self):
        expected=self.sampled(120)
        b=self.backend
        self.receive(221,stage='minimized',visibility=True)
        self.receive(300,stage='capture',observed_at=222)
        self.receive(300,stage='occluded',visibility=True,observed_at=223)
        self.assertTrue(b.display_rate_snapshot()['visibility_preserved'])
        self.assertEqual(b.display_rate_snapshot()['windows'],expected)
        self.assertIsNone(b.tracker._visibility_returned_at)
        self.receive(301,3000)
        self.assertEqual(b.tracker.windows.snapshot(),expected)

    def test_checkpoint_failure_keeps_storage_error_above_visibility_status(self):
        self.sampled(8)
        b=self.backend
        with patch.object(b.history,'save_resume_state',side_effect=sqlite3.OperationalError('fixture full')):
            self.receive(109,stage='occluded',visibility=True)
        self.assertFalse(b.running)
        self.assertIn('历史保存失败',b.status.get())
        self.assertFalse(b.tracker.visibility_interrupted)


class ScreenVisibilityEvidenceTests(unittest.TestCase):
    def capture_fixture(self):
        capture=game_capture.GameCapture.__new__(game_capture.GameCapture)
        capture.trusted_roi=None
        capture.crop=Mock(return_value=(np.zeros((20,200,3),dtype=np.uint8),
            dict(left=50,top=12,width=200,height=20,scale=1)))
        geometry=dict(hwnd=7,client=dict(left=100,top=200,width=1000,height=600))
        return capture,geometry

    def screen(self):
        screen=Mock()
        screen.return_value.__enter__=Mock(return_value=screen)
        screen.return_value.__exit__=Mock(return_value=False)
        screen.grab.return_value=np.zeros((72,1000,4),dtype=np.uint8)
        return screen

    def test_minimized_is_explicit_visibility_evidence(self):
        with patch.object(game_capture,'physical_dpi'), \
             patch.object(game_capture.USER,'FindWindowW',return_value=7), \
             patch.object(game_capture.USER,'IsIconic',return_value=True):
            with self.assertRaises(game_capture.CaptureFailure) as caught:
                game_capture.game_geometry()
        self.assertEqual(caught.exception.context['failure_kind'],'visibility_interrupted')
        self.assertEqual(caught.exception.context['visibility_reason'],'minimized')
        self.assertEqual(caught.exception.context['hwnd'],7)

    def test_trusted_roi_checks_occlusion_before_anchor_and_follows_negative_coordinates(self):
        capture,geo=self.capture_fixture();screen=self.screen()
        with patch.object(game_capture,'game_geometry',return_value=geo), \
             patch.object(game_capture.mss,'mss',screen), \
             patch.object(capture,'roi_visible',return_value=True):
            capture.capture()
        self.assertEqual(capture.trusted_roi['roi']['left'],50)
        moved=dict(geo,client=dict(geo['client'],left=-1920,top=0))
        capture.crop.reset_mock()
        with patch.object(game_capture,'game_geometry',return_value=moved), \
             patch.object(game_capture.mss,'mss') as grab, \
             patch.object(capture,'roi_visible',return_value=False) as visible:
            with self.assertRaises(game_capture.CaptureFailure) as caught:
                capture.capture()
        self.assertEqual(visible.call_args.args[1]['left'],-1870)
        self.assertEqual(caught.exception.stage,'occluded')
        self.assertEqual(caught.exception.context['failure_kind'],'visibility_interrupted')
        capture.crop.assert_not_called();grab.assert_not_called()

    def test_visible_background_window_is_still_captured(self):
        capture,geo=self.capture_fixture()
        with patch.object(game_capture,'game_geometry',return_value=geo), \
             patch.object(game_capture.mss,'mss',self.screen()), \
             patch.object(game_capture.USER,'WindowFromPoint',return_value=7), \
             patch.object(game_capture.USER,'GetAncestor',return_value=7), \
             patch.object(game_capture.USER,'GetForegroundWindow',return_value=999) as foreground:
            frame,info=capture.capture()
        self.assertIsNotNone(frame)
        self.assertNotIn('failure_kind',info)
        foreground.assert_not_called()

    def test_moving_window_is_not_classified_as_occluded(self):
        capture,geo=self.capture_fixture()
        moved=dict(geo,client=dict(geo['client'],left=101))
        with patch.object(game_capture,'game_geometry',side_effect=[geo,moved]), \
             patch.object(game_capture.mss,'mss',self.screen()), \
             patch.object(capture,'roi_visible',return_value=True):
            with self.assertRaises(game_capture.CaptureFailure) as caught:
                capture.capture()
        self.assertEqual(caught.exception.stage,'window_moved')
        self.assertNotIn('failure_kind',caught.exception.context)
        self.assertIsNone(capture.trusted_roi)

    def test_unlocated_anchor_and_changed_roi_shape_cannot_claim_occlusion(self):
        capture,geo=self.capture_fixture()
        capture.trusted_roi=dict(hwnd=7,size=(900,600),roi=dict(left=50,top=550,width=200,height=20))
        capture.crop.side_effect=game_capture.CaptureFailure('missing anchor','anchor',{'anchor_score':.3})
        with patch.object(game_capture,'game_geometry',return_value=geo), \
             patch.object(game_capture.mss,'mss',self.screen()), \
             patch.object(capture,'roi_visible',return_value=False) as visible:
            with self.assertRaises(game_capture.CaptureFailure) as caught:
                capture.capture()
        self.assertEqual(caught.exception.stage,'anchor')
        self.assertNotIn('failure_kind',caught.exception.context)
        visible.assert_not_called()

    def test_cached_roi_from_another_dpi_cannot_claim_occlusion(self):
        capture,geo=self.capture_fixture()
        geo['dpi']=144
        capture.trusted_roi=dict(hwnd=7,size=(1000,600),dpi=96,
            roi=dict(left=50,top=550,width=200,height=20))
        with patch.object(capture,'roi_visible',return_value=False) as visible:
            capture.check_cached_visibility(geo)
        visible.assert_not_called()


if __name__=='__main__':
    unittest.main()
