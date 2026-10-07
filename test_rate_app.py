"""Exercise the actual result queue, history and public status without a GUI."""
import csv
import json
from pathlib import Path
import queue
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

import app
import game_capture
from auto_level import AutoLevel
from history_store import HistoryStore
from tracker import Reading, Tracker


class Value:
    def __init__(self, value=''):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class RateAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name)/'settings.json'
        self.addCleanup(patch.stopall)
        patch.object(app, 'CONFIG', self.config).start()
        patch.object(app.ImageTk, 'PhotoImage', return_value=object()).start()
        patch.object(app.threading, 'Thread').start()
        self.now = [100.0]
        b = self.backend = app.App.__new__(app.App)
        b.root = Mock()
        b.tracker = Tracker(lambda: self.now[0])
        b.auto_level = AutoLevel(b.tracker, lambda: self.now[0])
        b.history = HistoryStore(Path(self.temp.name)/'history.sqlite3')
        self.addCleanup(b.history.db.close)
        b.combat = Mock()
        b.combat.summary.return_value = {}
        b.combat.kill_summary.return_value = {}
        b.running = True
        b.busy = False
        b.generation = 0
        b.events = queue.Queue()
        b.region = {'source': 'manual'}
        b.capture_info = {}
        b.last_valid_time = b.last_valid_monotonic = None
        b.reading_valid = False
        b.latest = None
        b.storage_error = None
        b.last_maintenance = self.now[0]
        b.records = []
        b.record_seqs = []
        b.manual = Value()
        b.raw = Value()
        b.status = Value()
        b.elapsed = Value()
        b.start_button = Mock()
        b.metrics = [Mock(), Mock(), Mock()]
        b.preview = Mock()

    def receive(self, at, value, *, captured=None, cap=None):
        b = self.backend
        self.now[0] = at
        reading = Reading(value, cap) if value is not None else None
        b.events.put((b.generation, np.zeros((8, 20, 3), dtype=np.uint8),
                      str(value), reading, None,
                      {'observed_at': at if captured is None else captured}))
        b.tick()

    def last_row(self):
        return dict(self.backend.history.db.execute('SELECT * FROM samples ORDER BY id DESC LIMIT 1').fetchone())

    def status_json(self):
        self.backend.write_status()
        return json.loads(self.config.with_name('live-status.json').read_text(encoding='utf-8'))

    def test_display_history_and_json_share_algorithm_and_missing_state(self):
        b = self.backend
        self.receive(100, 1000)
        b.metrics[1].configure.assert_called_with(text='—')
        self.assertIsNone(self.last_row()['realtime'])
        self.assertEqual(self.last_row()['rate_algorithm'], 'time_ewma_v1')
        self.receive(101, 1100)
        self.assertEqual(b.rate_snapshot()['value'], 6000)
        self.assertEqual(self.last_row()['realtime'], 6000)
        data = self.status_json()
        self.assertEqual(data['rate_state'], 'warming')
        self.assertEqual(data['realtime_per_hour'], 360000)
        self.assertEqual(data['rolling60_per_minute'], 6000)
        self.assertIsNone(data['rate_window_seconds'])
        segment = b.rate_snapshot()['segment']
        self.receive(101.5, None)
        b.metrics[1].configure.assert_called_with(text='6,000')
        self.assertIsNone(self.status_json()['realtime_per_minute'])
        self.receive(102, 1200)
        self.assertEqual(b.rate_snapshot()['segment'], segment)
        self.assertAlmostEqual(b.rate_snapshot()['value'], 6000)

    def test_primary_window_drives_main_display_and_trend_keeps_its_metadata(self):
        b = self.backend
        self.receive(100, 1000)
        for second in range(1, 36):
            self.receive(100+second, 1100)
        snapshot = b.rate_snapshot()
        primary = b.primary_rate_snapshot()
        self.assertEqual(primary['value'], 6000/35)
        self.assertEqual(primary['window_seconds'], 60)
        self.assertEqual(primary['algorithm'], 'observed_window_v1')
        self.assertEqual(primary['state'], 'warming')
        self.assertEqual(snapshot['state'], 'ready')
        self.assertNotAlmostEqual(primary['value'], snapshot['value'])
        b.metrics[1].configure.assert_called_with(text='171')
        data = self.status_json()
        self.assertEqual(data['primary_per_minute'], primary['value'])
        self.assertEqual(data['primary_per_hour'], primary['value']*60)
        self.assertEqual(data['primary_observed_seconds'], 35)
        self.assertEqual(data['primary_state'], 'warming')
        self.assertEqual(data['trend_state'], 'ready')
        self.assertEqual(data['trend_per_minute'], snapshot['value'])
        self.assertEqual(data['realtime_per_minute'], data['trend_per_minute'])
        self.assertEqual(data['trend_algorithm'], 'time_ewma_v1')
        row = self.last_row()
        self.assertEqual(row['rolling60'], primary['value'])
        self.assertEqual(row['realtime'], snapshot['value'])
        self.assertEqual(row['rate_half_life'], snapshot['half_life_seconds'])

    def test_primary_uses_observed_window_boundary_without_ui_time_decay(self):
        b = self.backend
        self.receive(100, 1000)
        for second in range(1, 61):
            self.receive(100+second, 1100)
        self.assertEqual(b.primary_rate_snapshot()['value'], 100)
        self.assertEqual(b.primary_rate_snapshot()['state'], 'ready')
        b.metrics[1].configure.assert_called_with(text='100')
        before = b.primary_rate_snapshot()
        self.now[0] = 160.9
        b.tick()
        self.assertEqual(b.primary_rate_snapshot(), before)
        self.receive(161, 1100)  # The gain observed at 101 leaves (101,161].
        self.assertEqual(b.primary_rate_snapshot()['value'], 0)
        self.assertGreater(b.rate_snapshot()['value'], 0)
        self.assertEqual(b.tracker.total, 100)

    def test_primary_missing_and_long_gap_never_present_catchup_as_live_rate(self):
        b = self.backend
        self.receive(100, 1000)
        self.assertIsNone(b.primary_rate_snapshot()['value'])
        self.receive(101, 1100)
        self.receive(102, None)
        self.assertIsNone(b.primary_rate_snapshot()['value'])
        self.assertIsNone(self.status_json()['primary_per_minute'])
        self.receive(108, 1800)
        self.assertEqual(b.tracker.total, 800)
        self.assertEqual(b.primary_rate_snapshot()['value'], 6000)
        self.assertEqual(b.primary_rate_snapshot()['observed_seconds'], 1)
        self.receive(109, 1800)
        self.assertEqual(b.primary_rate_snapshot()['value'], 3000)
        b.pause()
        self.assertEqual(b.primary_rate_snapshot()['state'], 'paused')
        self.assertIsNone(b.primary_rate_snapshot()['value'])

    def test_long_gap_books_net_gain_without_live_spike(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(101, 1100)
        segment = b.rate_snapshot()['segment']
        self.receive(108, 1800)
        row = self.last_row()
        self.assertEqual((row['gain'], row['total'], row['status']), (700, 800, 'gap'))
        self.assertEqual(row['realtime'],6000)
        self.assertGreater(row['rate_segment'], segment)
        self.receive(109, 1900)
        self.assertAlmostEqual(b.rate_snapshot()['value'], 6000)

    def test_paused_manual_upgrade_only_changes_ledger(self):
        b = self.backend
        self.receive(100, 950, cap=1000)
        self.receive(101, 50, cap=1200)
        self.assertFalse(b.running)
        self.now[0] = 110
        b.level()
        self.assertEqual(b.tracker.total, 100)
        self.assertEqual(b.tracker.elapsed, 1)
        self.assertIsNone(self.last_row()['realtime'])
        self.assertEqual(self.last_row()['rate_state'], 'paused')
        self.assertIsNone(self.status_json()['realtime_per_hour'])
        b.toggle()
        self.receive(111, 400, cap=1200)
        self.assertEqual(b.tracker.total, 100)
        self.assertIsNone(b.rate_snapshot()['value'])
        self.receive(112, 500, cap=1200)
        self.assertEqual(b.tracker.total, 200)
        self.assertAlmostEqual(b.rate_snapshot()['value'], 6000)

    def test_delayed_frame_uses_capture_interval_and_expired_frame_is_ignored(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(104, 1100, captured=101)
        self.assertAlmostEqual(b.rate_snapshot()['value'], 6000)
        self.assertEqual(b.rate_snapshot()['rolling60'], 6000)
        self.assertEqual(b.rate_snapshot()['windows']['60']['value'], 6000)
        self.assertEqual(b.rate_snapshot()['observed_seconds'], 1)
        count = len(b.records)
        self.receive(108, 9000, captured=102)
        self.assertEqual(len(b.records), count)
        self.assertEqual(b.tracker.previous, 1100)
        self.assertFalse(b.fresh_reading())

    def test_duplicate_sample_preserves_last_confirmed_reading(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(101, 1100)
        self.receive(102, 9999, captured=101)
        self.assertEqual(b.latest.current, 1100)
        self.assertEqual(b.tracker.total, 100)
        self.assertEqual(len(b.records), 2)
        self.assertTrue(b.fresh_reading())

    def test_invalid_numeric_reading_cannot_restore_old_live_value(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(101, 1100)
        self.receive(102, -1)
        self.assertFalse(b.reading_valid)
        self.assertEqual(b.tracker.total, 100)
        self.assertEqual(len(b.records), 2)
        self.assertIsNone(b.rate_snapshot()['value'])
        b.metrics[1].configure.assert_called_with(text='—')

    def test_game_capture_timestamp_precedes_anchor_processing(self):
        capture = game_capture.GameCapture.__new__(game_capture.GameCapture)
        geometry = dict(hwnd=7, client=dict(left=0, top=0, width=100, height=100))

        def crop(band):
            self.now[0] = 104
            return band, dict(left=0, top=0, width=20, height=8, scale=1)

        capture.crop = crop
        with patch.object(game_capture, 'game_geometry', return_value=geometry), \
                patch.object(game_capture.time, 'monotonic', side_effect=lambda: self.now[0]), \
                patch.object(game_capture.mss, 'mss') as screen, \
                patch.object(game_capture.USER, 'WindowFromPoint', return_value=7), \
                patch.object(game_capture.USER, 'GetAncestor', return_value=7):
            screen.return_value.__enter__.return_value.grab.return_value = np.zeros((12, 100, 4), dtype=np.uint8)
            _, info = capture.capture()
        self.assertEqual(info['observed_at'], 100)
        self.assertEqual(self.now[0], 104)

    def test_worker_preserves_capture_timestamp_through_ocr(self):
        b = self.backend
        b.capture = Mock()
        b.engine = Mock()
        b.capture.capture.return_value = (np.zeros((8, 20, 3), dtype=np.uint8), {'observed_at': 99.0})
        b.engine.read.return_value = (Reading(123), [])
        with patch.object(app, 'read_level', return_value=(None, [])):
            b.worker(0, {'game_window': True})
        event = b.events.get_nowait()
        self.assertIsNone(event[4])
        self.assertEqual(event[5]['observed_at'], 99)

    def test_optional_level_error_does_not_delay_or_discard_exp(self):
        b = self.backend
        self.receive(100, 1000)
        self.now[0] = 104
        b.region = {'game_window': True}
        b.capture = Mock()
        b.capture.level_frame = np.zeros((39, 84, 3), dtype=np.uint8)
        b.capture.capture.return_value = (np.zeros((25, 290, 3), dtype=np.uint8), {'observed_at': 101.0})
        b.engine = Mock()
        b.engine.read.return_value = (Reading(1100), [('1100', .85)])
        b.recognition_journal = Mock()
        b.recognition_journal.record.side_effect = OSError('disk unavailable')
        with patch.object(app, 'read_level', side_effect=RuntimeError('badge unavailable')):
            b.worker(0, b.region)
        b.tick()
        self.assertEqual(b.tracker.total, 100)
        self.assertEqual(b.last_valid_monotonic, 101)
        self.assertEqual(b.rate_snapshot()['value'], 6000)
        self.assertIsNone(b.capture_info['level_candidate'])
        self.assertIn('badge unavailable', b.capture_info['level_error'])
        b.recognition_journal.record.assert_not_called()

    def test_diagnostic_io_error_does_not_change_accounting_or_mask_rejection(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(101, 1100)
        count = len(b.records)
        self.now[0] = 102
        b.capture = Mock()
        b.capture.level_frame = None
        b.capture.capture.return_value = (np.zeros((25, 290, 3), dtype=np.uint8), {'observed_at': 102.0})
        b.engine = Mock()
        b.engine.read.return_value = (None, [('1108', .45)])
        b.recognition_journal = Mock()
        b.recognition_journal.record.side_effect = OSError('disk unavailable')
        with patch.object(app, 'read_level', return_value=(None, [])):
            b.worker(0, {'game_window': True})
        b.tick()
        self.assertEqual(b.tracker.total, 100)
        self.assertEqual(b.tracker.previous, 1100)
        self.assertEqual(len(b.records), count)
        self.assertIsNone(b.rate_snapshot()['value'])
        self.assertIn('disk unavailable', b.capture_info['diagnostic_error'])
        self.assertIn('识别重试中', b.status.get())
        self.assertIn('本帧未计入', b.status.get())

    def test_exp_exception_preserves_frame_capture_time_and_failure_stage(self):
        b = self.backend
        b.capture = Mock()
        frame = np.zeros((25, 290, 3), dtype=np.uint8)
        b.capture.capture.return_value = (frame, {'observed_at': 99.0})
        b.capture.level_frame = None
        b.engine = Mock()
        b.engine.read.side_effect = RuntimeError('inference failed')
        b.recognition_journal = Mock(last_error=None)
        b.recognition_journal.record.return_value = None
        b.worker(0, {'game_window': True})
        event = b.events.get_nowait()
        self.assertIs(event[1], frame)
        self.assertIsNone(event[3])
        self.assertEqual(event[5]['observed_at'], 99)
        self.assertEqual(event[5]['failure_stage'], 'experience_ocr')
        self.assertEqual(b.recognition_journal.record.call_args.args[0], 'experience_ocr')

    def test_capture_rejection_keeps_metadata_without_old_pixels(self):
        b = self.backend
        b.engine, b.capture = Mock(), Mock()
        b.capture.capture.side_effect = game_capture.CaptureFailure(
            'anchor unavailable', 'anchor', {'observed_at': 99., 'anchor_score': .42})
        b.recognition_journal = Mock(last_error=None)
        b.recognition_journal.record.return_value = None
        b.worker(0, {'game_window': True})
        event = b.events.get_nowait()
        self.assertIsNone(event[1])
        self.assertEqual(event[5]['failure_stage'], 'anchor')
        self.assertEqual(event[5]['anchor_score'], .42)
        self.assertEqual(event[5]['observed_at'], 99)
        b.engine.read.assert_not_called()
        self.assertIsNone(b.recognition_journal.record.call_args.kwargs['frame'])

    def test_canceled_generation_does_not_save_diagnostic_images(self):
        b = self.backend
        b.recognition_journal = Mock()
        b.diagnose_failure('capture', {}, b.generation-1)
        b.recognition_journal.record.assert_not_called()

    def test_corrupt_pixels_then_recovered_short_exp_never_book_wrong_gain(self):
        if not (Path(__file__).parent/'verification/probe_exp_short.py').is_file():
            self.skipTest('Optional local OCR screenshot corpus is not installed')
        from experience_ocr import ExperienceOCR
        from verification.probe_exp_short import make_short_case, make_truncated_case

        b = self.backend
        self.receive(100, 0)
        b.region = {'game_window': True}
        b.capture = Mock()
        b.capture.level_frame = None
        b.engine = ExperienceOCR()
        b.recognition_journal = Mock(last_error=None)
        b.recognition_journal.record.return_value = None
        good, _ = make_short_case(52)  # Labelled synthetic whole-glyph EXP 14.
        bad = make_truncated_case(22)  # The old OCR incorrectly accepted 68914.
        for at, frame, expected_total in ((101, bad, 0), (102, good, 14), (103, good, 14)):
            self.now[0] = at
            b.capture.capture.return_value = (frame, {'observed_at': float(at)})
            b.worker(0, b.region)
            b.tick()
            self.assertEqual(b.tracker.total, expected_total)
            self.assertFalse(b.tracker.blocked)
        rows = b.history.db.execute('SELECT current_exp,gain,total FROM samples ORDER BY id').fetchall()
        self.assertEqual([tuple(row) for row in rows], [(0, 0, 0), (14, 14, 14), (14, 0, 14)])
        self.assertEqual(b.last_valid_monotonic, 103)

    def test_optional_level_crop_exception_does_not_reject_capture(self):
        capture = game_capture.GameCapture.__new__(game_capture.GameCapture)
        geometry = dict(hwnd=7, client=dict(left=0, top=0, width=100, height=100))
        capture.crop = lambda band: (band, dict(left=0, top=0, width=20, height=8, scale=1))
        with patch.object(game_capture, 'game_geometry', return_value=geometry), \
                patch.object(game_capture.time, 'monotonic', return_value=99.), \
                patch.object(game_capture.mss, 'mss') as screen, \
                patch.object(game_capture.USER, 'WindowFromPoint', return_value=7), \
                patch.object(game_capture.USER, 'GetAncestor', return_value=7), \
                patch('level_ocr.crop_level', side_effect=RuntimeError('badge template missing')):
            screen.return_value.__enter__.return_value.grab.return_value = np.zeros((12, 100, 4), dtype=np.uint8)
            frame, info = capture.capture(include_level=True)
        self.assertIsNotNone(frame)
        self.assertIsNone(capture.level_frame)
        self.assertEqual(info['observed_at'], 99.)
        self.assertIn('badge template missing', info['level_error'])

    def test_true_zero_and_stale_are_distinct_and_wall_clock_is_irrelevant(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(101, 1000)
        with patch.object(app.time, 'time', return_value=1):
            self.assertTrue(b.fresh_reading())
            self.assertEqual(b.rate_snapshot()['value'], 0)
        self.now[0] = 107
        b.tick()
        data = self.status_json()
        self.assertFalse(data['valid'])
        self.assertEqual(data['rate_state'], 'stale')
        self.assertIsNone(data['realtime_per_minute'])
        b.metrics[1].configure.assert_called_with(text='—')

    def test_five_windows_share_observed_time_quality_and_saved_values(self):
        b = self.backend
        self.receive(100, 1000)
        for second in range(1, 131):
            self.receive(100+second, 1000+100*second)
        snapshot = b.rate_snapshot()
        self.assertEqual(snapshot['window_algorithm'], 'observed_window_v1')
        for seconds in (60, 120, 300, 600, 1800):
            window = snapshot['windows'][str(seconds)]
            self.assertEqual(window['value'], 6000)
            self.assertEqual(window['observed_seconds'], min(seconds, 130))
            self.assertEqual(window['state'], 'ready' if seconds <= 130 else 'warming')
            self.assertEqual(self.last_row()[f'rolling{seconds}'], 6000)
        self.now[0] += 2
        self.assertEqual(b.rate_snapshot()['windows'], snapshot['windows'])
        self.assertEqual(self.status_json()['window_rates'], snapshot['windows'])
        self.receive(233, None)
        self.assertTrue(all(w['value'] is None for w in b.rate_snapshot()['windows'].values()))
        self.receive(240, 16000)
        self.assertEqual(b.rate_snapshot()['windows'],snapshot['windows'])
        self.assertEqual(self.last_row()['window_algorithm'], 'observed_window_v1')
        self.assertTrue(all(self.last_row()[f'rolling{seconds}'] == 6000
                            for seconds in (60, 120, 300, 600, 1800)))

    def test_retry_display_preserves_old_observation_without_creating_a_sample(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(101, 1100)
        before = b.display_rate_snapshot()
        count = len(b.records)
        self.receive(101.5, None)
        shown = b.display_rate_snapshot()
        self.assertEqual(shown['display_state'], 'retrying')
        self.assertEqual(shown['display_age_seconds'], .5)
        self.assertEqual(shown['primary'], before['primary'])
        self.assertEqual(shown['windows'], before['windows'])
        self.assertEqual(shown['value'], before['value'])
        self.assertFalse(b.fresh_reading())
        self.assertIsNone(b.rate_snapshot()['primary']['value'])
        self.assertEqual(b.last_valid_monotonic, 101)
        self.assertEqual((b.tracker.total, len(b.records)), (100, count))
        data = self.status_json()
        self.assertFalse(data['valid'])
        self.assertIsNone(data['primary_per_minute'])
        self.assertEqual(data['display_primary_per_minute'], 6000)
        self.assertEqual(data['display_state'], 'retrying')
        shown['windows']['60']['value'] = 99999
        self.assertEqual(b.display_rate_snapshot()['windows']['60']['value'], 6000)

    def test_repeated_failures_do_not_extend_retry_deadline_or_coverage(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(101, 1100)
        for at in (101.5, 102, 102.9, 103):
            self.receive(at, None)
            shown = b.display_rate_snapshot()
            self.assertEqual(shown['display_state'], 'retrying')
            self.assertEqual(shown['primary']['observed_seconds'], 1)
            self.assertAlmostEqual(shown['display_age_seconds'], at-101)
        self.now[0] = 103.001
        b.tick()  # Expiry requires no additional failed frame.
        self.assertIsNone(b.display_rate_snapshot()['primary']['value'])
        b.metrics[1].configure.assert_called_with(text='—')
        self.assertNotIn('显示', b.status.get())
        self.assertEqual(len(b.records), 2)
        self.assertEqual(b.tracker.total, 100)

    def test_retry_age_uses_capture_time_not_slow_delivery_time(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(102.8, 1100, captured=101)
        self.receive(103.1, None)
        self.assertIsNone(b.display_rate_snapshot()['primary']['value'])
        self.assertEqual(b.last_valid_monotonic, 101)

    def test_retry_recovery_books_real_gain_once_then_long_gap_continues(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(101, 1100)
        segment = b.rate_snapshot()['segment']
        self.receive(101.5, None)
        self.receive(102, 1200)
        self.assertEqual(b.display_rate_snapshot()['display_state'], 'live')
        self.assertEqual(b.rate_snapshot()['segment'], segment)
        self.assertEqual(b.tracker.total, 200)
        self.assertEqual([r[3] for r in b.records], [0, 100, 100])
        self.receive(102.5, None)
        self.receive(108, 1800)
        self.assertEqual(b.tracker.total, 800)
        self.assertEqual(b.display_rate_snapshot()['primary']['value'],6000)
        self.assertEqual(b.display_rate_snapshot()['primary']['observed_seconds'], 2)

    def test_first_sample_is_never_invented_but_confirmed_zero_can_be_held(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(100.5, None)
        self.assertIsNone(b.display_rate_snapshot()['primary']['value'])
        self.receive(101, 1000)
        self.receive(101.5, None)
        self.assertEqual(b.display_rate_snapshot()['primary']['value'], 0)
        self.assertEqual(b.display_rate_snapshot()['display_state'], 'retrying')

    def test_numeric_contradiction_clears_display_and_later_failure_cannot_revive_it(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(101, 1100)
        self.receive(101.2, -1)
        self.receive(101.4, None)
        self.assertIsNone(b.display_rate_snapshot()['primary']['value'])
        self.assertEqual(b.tracker.total, 100)

    def test_only_ocr_exception_can_hold_and_capture_failure_revokes_it(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(101, 1100)
        for at, stage, held in ((101.2, 'experience_ocr', True),
                                (101.4, 'occluded', False),
                                (101.6, 'experience_ocr', False)):
            self.now[0] = at
            b.events.put((b.generation, None, '', None, 'failure',
                          {'failure_stage': stage, 'observed_at': at}))
            b.tick()
            self.assertEqual(b.display_rate_snapshot()['primary']['value'] is not None, held)
        self.assertEqual(len(b.records), 2)

    def test_pause_new_generation_and_level_pending_never_hold_old_rates(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(101, 1100)
        self.receive(101.2, None)
        b.auto_level.wait_since = 101.2
        self.assertIsNone(b.display_rate_snapshot()['primary']['value'])
        b.auto_level.wait_since = None
        b.generation += 1
        self.assertIsNone(b.display_rate_snapshot()['primary']['value'])
        b.pause()
        self.assertIsNone(b._confirmed_rate_display)
        self.assertEqual(b.display_rate_snapshot()['primary']['state'], 'paused')

    def test_rebase_revokes_retry_cache_even_without_generation_change(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(101, 1100)
        self.receive(101.2, None)
        generation = b.generation
        b.rebase()
        self.receive(101.4, None)
        self.assertEqual(b.generation, generation)
        self.assertIsNone(b.display_rate_snapshot()['primary']['value'])
        self.assertEqual(b.tracker.total, 100)

    def test_session_csv_keeps_original_columns_and_all_window_metadata(self):
        b = self.backend
        self.receive(100, 1000)
        self.receive(101, 1100)
        path = Path(self.temp.name)/'session.csv'
        with patch.object(app.filedialog, 'asksaveasfilename', return_value=str(path)):
            b.export()
        with path.open(encoding='utf-8-sig', newline='') as stream:
            header, baseline, sample = list(csv.reader(stream))
        self.assertEqual(header[:9], ['时间（北京时间 UTC+8）', '有效秒数', '当前经验', '本次增加', '累计经验',
                                     '分均经验', '时均经验', '状态', '识别文字'])
        self.assertEqual(header[-5:], ['1min分均', '2min分均', '5min分均', '10min分均', '30min分均'])
        self.assertEqual(baseline[-5:], ['']*5)
        self.assertEqual([float(v) for v in sample[-5:]], [6000]*5)
        self.assertEqual(sample[header.index('窗口算法')], 'observed_window_v1')
        self.assertEqual(float(sample[header.index('观测秒数')]), 1)

    def test_failed_save_blocks_continuation_and_export_marks_unsaved_metadata_missing(self):
        b = self.backend
        self.receive(100, 1000)
        with patch.object(b.history, 'append', side_effect=sqlite3.OperationalError('test disk failure')):
            self.receive(101, 1100)
        self.assertFalse(b.running)
        self.assertEqual(b.record_seqs, [1, None])
        b.toggle()
        self.assertFalse(b.running)
        self.assertFalse(b.record('baseline'))
        self.assertEqual(b.record_seqs, [1, None])
        path = Path(self.temp.name)/'recovered.csv'
        with patch.object(app.filedialog, 'asksaveasfilename', return_value=str(path)):
            b.export()
        with path.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows[2][9:], ['']*(len(rows[0])-9))
        self.assertEqual(len(rows), 3)


if __name__ == '__main__':
    unittest.main()
