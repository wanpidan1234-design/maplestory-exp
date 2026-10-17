"""Screen geometry is client-relative; no fallback to arbitrary desktop pixels."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

import quiz_capture
from game_capture import CaptureFailure
from quiz_capture import QuizRegion, capture_region, validate_region


class QuizRegionTests(unittest.TestCase):
    def setUp(self):
        self.geometry = dict(hwnd=17, client=dict(left=-1920, top=50, width=1280, height=720), dpi=120)
        self.region = QuizRegion(hwnd=17, client_size=(1280, 720), dpi=120,
                                 left=100, top=80, width=600, height=300)

    def test_region_maps_to_negative_monitor_coordinates(self):
        self.assertEqual(validate_region(self.region, self.geometry),
                         dict(left=-1820, top=130, width=600, height=300))

    def test_entire_region_must_be_inside_game_client(self):
        for left, top, width, height in ((-1, 10, 600, 300), (100, -1, 600, 300),
                                         (1000, 80, 600, 300), (100, 600, 600, 300),
                                         (100, 80, 0, 300), (100, 80, 600, -1)):
            with self.subTest(rect=(left, top, width, height)):
                region = QuizRegion(17, (1280, 720), 120, left, top, width, height)
                with self.assertRaises(ValueError):
                    validate_region(region, self.geometry)

    def test_handle_resize_and_dpi_changes_require_reselection(self):
        changed = (
            dict(self.geometry, hwnd=18),
            dict(self.geometry, client=dict(self.geometry['client'], width=1920)),
            dict(self.geometry, dpi=144),
        )
        for geometry in changed:
            with self.subTest(geometry=geometry), self.assertRaises(ValueError):
                validate_region(self.region, geometry)

    def test_window_translation_keeps_client_relative_region(self):
        moved = dict(self.geometry, client=dict(self.geometry['client'], left=30, top=70))
        self.assertEqual(validate_region(self.region, moved),
                         dict(left=130, top=150, width=600, height=300))

    def test_missing_game_fails_before_any_screen_capture(self):
        with patch.object(quiz_capture, 'game_geometry', side_effect=CaptureFailure('missing', 'window_not_found')):
            with patch.object(quiz_capture.mss, 'mss') as factory:
                with self.assertRaises((CaptureFailure, ValueError)):
                    capture_region(self.region)
                factory.assert_not_called()

    def test_obscured_question_fails_before_any_screen_capture(self):
        with patch.object(quiz_capture, 'game_geometry', return_value=self.geometry):
            with patch.object(quiz_capture, 'region_visible', return_value=False):
                with patch.object(quiz_capture.mss, 'mss') as factory:
                    with self.assertRaises((CaptureFailure, ValueError)):
                        capture_region(self.region)
                    factory.assert_not_called()

    def test_capture_is_limited_to_selected_game_region(self):
        fake_capture = Mock()
        fake_capture.grab.return_value = np.zeros((300, 600, 4), dtype=np.uint8)
        manager = Mock()
        manager.__enter__ = Mock(return_value=fake_capture)
        manager.__exit__ = Mock(return_value=False)
        with patch.object(quiz_capture, 'game_geometry', return_value=self.geometry):
            with patch.object(quiz_capture, 'region_visible', return_value=True):
                with patch.object(quiz_capture.mss, 'mss', return_value=manager):
                    capture_region(self.region)
        fake_capture.grab.assert_called_once_with(dict(left=-1820, top=130, width=600, height=300))

    def test_moving_during_capture_rejects_pixels(self):
        moved = dict(self.geometry, client=dict(self.geometry['client'], left=-1800))
        manager = Mock()
        manager.__enter__ = Mock(return_value=Mock(grab=Mock(return_value=np.zeros((300, 600, 4), dtype=np.uint8))))
        manager.__exit__ = Mock(return_value=False)
        with patch.object(quiz_capture, 'game_geometry', side_effect=[self.geometry, moved]):
            with patch.object(quiz_capture, 'region_visible', return_value=True):
                with patch.object(quiz_capture.mss, 'mss', return_value=manager):
                    with self.assertRaises((CaptureFailure, ValueError)):
                        capture_region(self.region)


class QuizVisibilityTests(unittest.TestCase):
    roi = dict(left=100, top=100, width=600, height=300)

    def visible(self, windows, bounds, hidden=(), minimized=()):
        def window_rect(hwnd, pointer):
            left, top, right, bottom = bounds.get(hwnd, (0, 0, 0, 0))
            rect = pointer._obj
            rect.left, rect.top, rect.right, rect.bottom = left, top, right, bottom
            return True

        def enumerate_windows(callback, param):
            for hwnd in windows:
                if not callback(hwnd, param):
                    break
            return True

        user = SimpleNamespace(
            IsWindowVisible=Mock(side_effect=lambda hwnd: hwnd not in hidden),
            IsIconic=Mock(side_effect=lambda hwnd: hwnd in minimized),
            GetWindowRect=Mock(side_effect=window_rect),
            EnumWindows=Mock(side_effect=enumerate_windows),
        )
        with patch.object(quiz_capture, 'USER', user):
            return quiz_capture.region_visible(17, self.roi)

    def test_small_overlay_between_sample_points_is_rejected(self):
        self.assertFalse(self.visible([31, 17], {31: (250, 110, 270, 130)}))

    def test_nonoverlapping_and_behind_game_windows_are_allowed(self):
        self.assertTrue(self.visible([31, 17, 32],
                                     {31: (0, 0, 99, 99), 32: (100, 100, 700, 400)}))

    def test_hidden_and_minimized_overlays_are_allowed(self):
        self.assertTrue(self.visible([31, 32, 17],
                                     {31: (100, 100, 700, 400), 32: (100, 100, 700, 400)},
                                     hidden=(31,), minimized=(32,)))

    def test_missing_game_in_window_stack_is_not_trusted(self):
        self.assertFalse(self.visible([], {}))


if __name__ == '__main__':
    unittest.main()
