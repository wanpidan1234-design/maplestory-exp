"""Reminder lifecycle, stale-frame rejection and icon movement regressions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from flower_buff import FlowerReading, FlowerVision, FlowerWatch
from flower_service import FlowerService, FlowerSound, flower_pixels_visible
from image_assets import read_image


class FlowerWatchTests(unittest.TestCase):
    def test_one_alert_per_cycle_and_confirmed_refresh_rearms(self):
        watch = FlowerWatch()
        alerts = []
        for t, seconds in enumerate((22, 21, 16, 15, 14, 13, 12, 45, 44, 16, 15, 14), 1):
            # Include realistic elapsed time across gaps in the countdown.
            now = t if t < 3 else t+3
            alerts.append(watch.observe(FlowerReading(True, seconds), now))
        # Large decreases are intentionally rejected until confirmed.
        self.assertEqual(sum(alerts), 2)

    def test_first_seen_near_expiry_requires_two_reads(self):
        watch = FlowerWatch()
        self.assertFalse(watch.observe(FlowerReading(True, 12), 1))
        self.assertTrue(watch.observe(FlowerReading(True, 11), 2))

    def test_one_bad_low_read_does_not_alert(self):
        watch = FlowerWatch()
        for t, n in enumerate((45, 44, 14, 42, 41), 1):
            self.assertFalse(watch.observe(FlowerReading(True, n), t))

    def test_visibility_loss_does_not_reset_alert_latch(self):
        watch = FlowerWatch()
        watch.observe(FlowerReading(True, 15), 1)
        self.assertTrue(watch.observe(FlowerReading(True, 14), 2))
        watch.unavailable()
        self.assertFalse(watch.observe(FlowerReading(True, 12), 4))
        self.assertFalse(watch.observe(FlowerReading(True, 11), 5))

    def test_absence_does_not_alert_or_rearm(self):
        watch = FlowerWatch()
        watch.alerted = True
        self.assertFalse(watch.observe(FlowerReading(False), 1))
        self.assertTrue(watch.alerted)
        self.assertIsNone(watch.snapshot(2)['seconds'])

    def test_expired_and_missing_timer_do_not_alert(self):
        watch = FlowerWatch()
        for t, r in enumerate((FlowerReading(True), FlowerReading(True),
                               FlowerReading(True, 0), FlowerReading(True, 0)), 1):
            self.assertFalse(watch.observe(r, t))

    def test_minute_read_cannot_rearm_false_timer(self):
        watch = FlowerWatch()
        watch.alerted = True
        watch.observe(FlowerReading(True, minutes=1), 1)
        watch.observe(FlowerReading(True, minutes=1), 2)
        self.assertTrue(watch.alerted)
        self.assertEqual(watch.snapshot(2)['minutes'], 1)

    def test_old_evidence_is_labelled_estimate_and_cannot_alert_without_visible_game(self):
        watch = FlowerWatch()
        watch.observe(FlowerReading(True, 20), 1)
        watch.observe(FlowerReading(True, 19), 2)
        self.assertEqual(watch.snapshot(2)['seconds'], 19)
        self.assertEqual(watch.snapshot(10)['seconds'], 11)
        self.assertTrue(watch.snapshot(10)['estimated'])
        self.assertFalse(watch.check_due(10))

    def test_megaphone_occlusion_keeps_known_deadline_and_warns_once(self):
        watch = FlowerWatch()
        watch.observe(FlowerReading(True, 30), 1)
        watch.observe(FlowerReading(True, 29), 2)
        alerts = [watch.observe(FlowerReading(False), t) for t in range(3, 30)]
        self.assertEqual(sum(alerts), 1)
        self.assertTrue(watch.snapshot(29)['estimated'])
        self.assertIn('预计', watch.snapshot(29)['text'])

    def test_hidden_from_start_never_invents_120_seconds(self):
        watch = FlowerWatch()
        for t in range(1, 150):
            self.assertFalse(watch.observe(FlowerReading(False), t))
        self.assertIsNone(watch.deadline)

    def test_confirmed_minute_refresh_cancels_old_estimate(self):
        watch = FlowerWatch()
        watch.observe(FlowerReading(True, 17), 1)
        watch.observe(FlowerReading(True, 16), 2)
        self.assertFalse(watch.observe(FlowerReading(True, minutes=1), 3))
        self.assertFalse(watch.check_due(3))
        self.assertFalse(watch.observe(FlowerReading(True, minutes=1), 4))
        self.assertGreater(watch.deadline, 60)
        self.assertTrue(watch.snapshot(4)['estimated'])
        self.assertFalse(watch.alerted)

    def test_minute_observation_survives_megaphone_and_continues_countdown(self):
        watch = FlowerWatch()
        watch.observe(FlowerReading(True, minutes=1, fill_fraction=.75), 1)
        watch.observe(FlowerReading(True, minutes=1, fill_fraction=.75), 2)
        watch.observe(FlowerReading(False), 12)
        snap = watch.snapshot(12)
        self.assertTrue(snap['estimated'])
        self.assertEqual(snap['remaining_seconds'], 80)
        self.assertEqual(snap['state'], 'active')
        self.assertNotIn('未见', snap['text'])

    def test_real_seconds_replace_estimate_from_minute_icon(self):
        watch = FlowerWatch()
        watch.observe(FlowerReading(True, minutes=1, fill_fraction=.75), 1)
        watch.observe(FlowerReading(True, minutes=1, fill_fraction=.75), 2)
        watch.observe(FlowerReading(True, 55), 40)
        watch.observe(FlowerReading(True, 54), 41)
        self.assertEqual(watch.snapshot(41)['seconds'], 54)
        self.assertFalse(watch.snapshot(41)['estimated'])

    def test_minute_estimate_calibration_does_not_warn_twice_in_same_cycle(self):
        watch = FlowerWatch()
        watch.observe(FlowerReading(True, minutes=1, fill_fraction=.5), 1)
        watch.observe(FlowerReading(True, minutes=1, fill_fraction=.5), 2)
        for t in range(3, 47):
            watch.observe(FlowerReading(False), t)
        self.assertTrue(watch.observe(FlowerReading(False), 47))
        # Actual seconds correct an early minute-phase estimate. This is not
        # evidence of a recast, so its already-issued reminder stays latched.
        watch.observe(FlowerReading(True, 40), 48)
        self.assertFalse(watch.observe(FlowerReading(True, 39), 49))
        self.assertTrue(watch.alerted)
        for t, seconds in enumerate(range(38, 0, -1), 50):
            self.assertFalse(watch.observe(FlowerReading(True, seconds), t))

    def test_new_seconds_after_estimated_expiration_rearms(self):
        watch = FlowerWatch()
        watch.observe(FlowerReading(True, 15), 1)
        self.assertTrue(watch.observe(FlowerReading(True, 14), 2))
        watch.observe(FlowerReading(False), 25)
        self.assertFalse(watch.observe(FlowerReading(True, 14), 26))
        self.assertTrue(watch.observe(FlowerReading(True, 13), 27))

    def test_refresh_discovered_late_rearms_even_below_hysteresis(self):
        watch = FlowerWatch()
        watch.observe(FlowerReading(True, 15), 1)
        self.assertTrue(watch.observe(FlowerReading(True, 14), 2))
        watch.observe(FlowerReading(False), 10)
        self.assertFalse(watch.observe(FlowerReading(True, 19), 11))
        self.assertFalse(watch.observe(FlowerReading(True, 18), 12))
        self.assertFalse(watch.alerted)
        for t, n in ((13, 17), (14, 16)):
            self.assertFalse(watch.observe(FlowerReading(True, n), t))
        self.assertTrue(watch.observe(FlowerReading(True, 15), 15))

    def test_frozen_pixels_do_not_extend_deadline(self):
        watch = FlowerWatch()
        watch.observe(FlowerReading(True, 30), 1)
        watch.observe(FlowerReading(True, 30), 2)
        before = watch.deadline
        for t in range(3, 10):
            watch.observe(FlowerReading(True, 30), t)
        self.assertEqual(watch.deadline, before)
        self.assertTrue(watch.snapshot(9)['estimated'])

    def test_frozen_low_timer_does_not_rearm_periodically(self):
        watch = FlowerWatch()
        alerts = [watch.observe(FlowerReading(True, 12), t) for t in range(1, 90)]
        self.assertEqual(sum(alerts), 1)

    def test_frozen_low_timer_with_brief_occlusion_does_not_rearm(self):
        watch = FlowerWatch()
        watch.observe(FlowerReading(True, 12), 1)
        self.assertTrue(watch.observe(FlowerReading(True, 12), 2))
        watch.observe(FlowerReading(False), 20)
        self.assertFalse(watch.observe(FlowerReading(True, 12), 21))
        self.assertFalse(watch.observe(FlowerReading(True, 12), 22))

    def test_gap_needs_new_consensus(self):
        watch = FlowerWatch()
        watch.observe(FlowerReading(True, 15), 1)
        self.assertFalse(watch.observe(FlowerReading(True, 14), 10))
        self.assertTrue(watch.observe(FlowerReading(True, 13), 11))


class FlowerVisionTests(unittest.TestCase):
    def test_icon_moves_and_timer_overlay_does_not_break_identity(self):
        vision = FlowerVision(recognizer=Mock())
        icon = read_image(Path(__file__).parent/'assets/holy-symbol-anchor.png')
        for size, x, y in ((32, 50, 5), (56, 300, 0), (60, 520, 50)):
            frame = np.full((160, 800, 3), 100, np.uint8)
            frame[y:y+size, x:x+size] = cv2.resize(icon, (size, size), interpolation=cv2.INTER_AREA)
            frame[y+round(size*.70):y+size, x:x+size//2] = (0, 255, 255)
            reading = vision.locate(frame)
            self.assertTrue(reading.present)
            self.assertEqual(reading.box[:2], (x, y))

    def test_no_icon_and_partial_icon_rejected(self):
        vision = FlowerVision()
        frame = np.random.default_rng(7).integers(0, 255, (160, 800, 3), dtype=np.uint8)
        self.assertFalse(vision.locate(frame).present)
        self.assertFalse(vision.locate(np.zeros_like(frame)).present)

    def test_countdown_shading_keeps_icon_identity(self):
        vision = FlowerVision()
        icon = read_image(Path(__file__).parent/'assets/holy-symbol-anchor.png')
        icon = cv2.resize(icon, (52, 52), interpolation=cv2.INTER_AREA)
        for fraction in (.1, .3, .5):
            shaded = icon.copy()
            shaded[round(52*fraction):] = np.uint8(shaded[round(52*fraction):]*.4)
            frame = np.full((160, 800, 3), 100, np.uint8)
            frame[3:55,400:452] = shaded
            reading = vision.locate(frame)
            self.assertTrue(reading.present, (fraction, reading.score))
            self.assertEqual(reading.box[:2], (400, 3))

    def test_low_confidence_ocr_rejected(self):
        engine = Mock()
        engine.recognize_exp.return_value = ('15', .4)
        vision = FlowerVision(engine)
        mask = np.zeros((22, 50), dtype=np.uint8)
        cv2.putText(mask, '15', (0, 19), cv2.FONT_HERSHEY_SIMPLEX, .6, 255, 2)
        self.assertIsNone(vision._number(mask, 59))


class FlowerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = FlowerService(Path(self.temp.name)/'flower.json')
        self.service.sound = Mock()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_settings_persist_and_bounds_rejected(self):
        self.service.configure(False, False, 10)
        again = FlowerService(self.service.path)
        self.assertEqual((again.enabled, again.music, again.ahead), (False, False, 10))
        with self.assertRaises(ValueError):
            self.service.configure(True, True, 99)
        again.close()

    def test_corrupt_config_uses_defaults(self):
        self.service.path.write_text('[]', encoding='utf-8')
        again = FlowerService(self.service.path)
        self.assertEqual(again.ahead, 15)
        again.close()

    def test_timer_only_occlusion_rejected(self):
        roi = dict(left=100, top=200, width=56, height=56)
        # The old midpoint check at y=228 passes; timer at y>=236 is covered.
        self.assertFalse(flower_pixels_visible(7, roi, lambda hwnd, r: r['top'] < 236))
        self.assertTrue(flower_pixels_visible(7, roi, lambda hwnd, r: True))

    def test_occluded_left_digit_edge_rejected(self):
        roi = dict(left=100, top=200, width=56, height=56)
        self.assertFalse(flower_pixels_visible(7, roi,
                         lambda hwnd, r: not (r['top'] >= 236 and r['left'] == 100 and r['width'] == 1)))

    @patch('flower_service.threading.Thread')
    def test_old_generation_cannot_alert_after_resume(self, thread):
        self.service.poll(True)
        old_epoch = self.service.epoch
        self.service.poll(False)
        self.service.results.put((old_epoch, 10, FlowerReading(True, 10), None))
        with patch('flower_service.time.monotonic', return_value=11):
            self.service.poll(True)
        self.service.sound.play.assert_not_called()
        self.assertIsNone(self.service.watch.last)

    @patch('flower_service.threading.Thread')
    def test_stale_ocr_result_cannot_alert(self, thread):
        self.service.poll(True)
        self.service.results.put((self.service.epoch, 0, FlowerReading(True, 10), None))
        with patch('flower_service.time.monotonic', return_value=20):
            self.service.poll(True)
        self.service.sound.play.assert_not_called()

    @patch('flower_service.threading.Thread')
    def test_music_off_keeps_visual_warning(self, thread):
        self.service.music = False
        self.service.poll(True)
        for now, n in ((1, 15), (2, 14)):
            self.service.results.put((self.service.epoch, now, FlowerReading(True, n), None))
            with patch('flower_service.time.monotonic', return_value=now):
                self.service.poll(True)
        self.assertEqual(self.service.watch.state, 'soon')
        self.service.sound.play.assert_not_called()

    @patch('winsound.PlaySound')
    def test_music_is_async_and_tests_do_not_queue(self, play):
        import winsound
        sound = FlowerSound()
        self.assertTrue(sound.play())
        self.assertFalse(sound.play(test=True))
        self.assertEqual(play.call_count, 1)
        self.assertTrue(play.call_args.args[1] & winsound.SND_ASYNC)
        sound.close()
        self.assertEqual(play.call_args.args, (None, 0))

    @patch('winsound.PlaySound')
    def test_real_reminder_preempts_test_audio(self, play):
        sound = FlowerSound()
        self.assertTrue(sound.play(test=True))
        self.assertTrue(sound.play())
        self.assertEqual(play.call_count, 2)
        sound.close()

    @patch('winsound.PlaySound', side_effect=RuntimeError('no device'))
    def test_audio_failure_is_reported_without_throwing(self, play):
        sound = FlowerSound()
        self.assertFalse(sound.play())
        self.assertIn('音乐播放失败', sound.error)


class FlowerPanelTests(unittest.TestCase):
    def test_minute_estimate_keeps_estimated_marker(self):
        from ui_midnight import MidnightPanel
        view = MidnightPanel.__new__(MidnightPanel)
        view.canvas = Mock()
        view.flower_images = {True: 'active-image', False: 'inactive-image'}
        view.flower_icon, view.flower_timer, view.flower_caption = 'icon', 'timer', 'caption'
        view.flower_outline = ['outline']
        view.paint_flower(dict(enabled=True, music=True, state='active', seconds=None,
                               minutes=1, estimated=True, audio_error=None, text=''), True)
        calls = [call for call in view.canvas.itemconfigure.call_args_list if call.args == ('caption',)]
        self.assertEqual(calls[-1].kwargs['text'], '预计')

    def test_shadow_moves_up_and_refresh_clears_it(self):
        from PIL import Image
        from ui_midnight import flower_shadow
        base = Image.new('RGB', (36, 36), 'white')
        half = flower_shadow(base, .5)
        near = flower_shadow(base, .125)
        self.assertEqual(half.getpixel((18, 5)), (255, 255, 255))
        self.assertLess(sum(half.getpixel((18, 30))), 400)
        self.assertLess(sum(near.getpixel((18, 10))), sum(half.getpixel((18, 10))))
        self.assertEqual(flower_shadow(base, 1).tobytes(), base.tobytes())

    def test_audio_error_does_not_hide_estimated_marker(self):
        from ui_midnight import MidnightPanel
        view = MidnightPanel.__new__(MidnightPanel)
        view.canvas = Mock()
        view.flower_images = {True: 'active-image', False: 'inactive-image'}
        view.flower_icon, view.flower_timer, view.flower_caption = 'icon', 'timer', 'caption'
        view.flower_outline = ['outline']
        view.paint_flower(dict(enabled=True, music=True, state='soon', seconds=14,
                               minutes=None, estimated=True, audio_error='failed', text=''), True)
        calls = [call for call in view.canvas.itemconfigure.call_args_list if call.args == ('caption',)]
        self.assertEqual(calls[-1].kwargs['text'], '预计')


if __name__ == '__main__':
    unittest.main()
