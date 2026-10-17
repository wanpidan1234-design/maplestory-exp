"""A delayed OCR worker may never resurrect an answer after clear/stop."""
import unittest
import hashlib
import queue
import threading
from unittest.mock import Mock, patch

import numpy as np

from quiz_bank import QuizEntry, QuizMatch
from quiz_helper import QuizState, QuizWindow


class QuizStateTests(unittest.TestCase):
    def answer(self, identifier='a', answer='废弃都市'):
        return QuizMatch('matched', QuizEntry(identifier, '测试题目', answer), .99)

    def test_initial_state_has_no_answer(self):
        state = QuizState()
        self.assertNotEqual(state.result.status, 'matched')
        self.assertIsNone(state.result.entry)

    def test_clear_removes_old_answer_and_advances_generation(self):
        state = QuizState()
        generation = state.generation
        self.assertTrue(state.accept(generation, self.answer()))
        new_generation = state.clear('题目变化')
        self.assertGreater(new_generation, generation)
        self.assertEqual(state.generation, new_generation)
        self.assertIsNone(state.result.entry)
        self.assertNotEqual(state.result.status, 'matched')

    def test_stale_worker_result_is_ignored(self):
        state = QuizState()
        old_generation = state.generation
        state.clear('已停止')
        self.assertFalse(state.accept(old_generation, self.answer()))
        self.assertIsNone(state.result.entry)

    def test_new_question_replaces_old_answer(self):
        state = QuizState()
        state.accept(state.generation, self.answer())
        current = state.clear('正在识别新题目')
        self.assertTrue(state.accept(current, self.answer('b', '魔法密林')))
        self.assertEqual(state.result.entry.answer, '魔法密林')

    def test_blank_unknown_and_failure_results_clear_answer(self):
        for result in (QuizMatch('no_match'), QuizMatch('uncertain', reason='识别失败')):
            with self.subTest(status=result.status):
                state = QuizState()
                state.accept(state.generation, self.answer())
                self.assertTrue(state.accept(state.generation, result))
                self.assertIsNone(state.result.entry)
                self.assertNotEqual(state.result.status, 'matched')


class QuizWindowLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.window = QuizWindow.__new__(QuizWindow)
        self.window.state = QuizState()
        self.window.closed = False
        self.window.running = True
        self.window.busy = True
        self.window.last_result_at = 100.0
        self.window.events = queue.Queue()
        self.window.root = Mock()
        self.window.render = Mock()
        self.answer = QuizMatch('matched', QuizEntry('a', '题目甲', '答案甲'), .99)
        self.window.state.accept(0, self.answer)

    def poll(self):
        with patch('quiz_helper.time.monotonic', return_value=101.0):
            self.window.poll()

    def test_new_pixels_pending_immediately_clear_display(self):
        self.window.events.put((0, 'pending', None, '', 101.0))
        self.poll()
        self.assertIsNone(self.window.state.result.entry)
        self.window.render.assert_called_once()
        self.assertIsNone(self.window.render.call_args.args[0].entry)

    def test_pause_rejects_delayed_worker_answer(self):
        self.window.state.clear('已暂停')
        self.window.running = False
        self.window.events.put((0, 'done', self.answer, 'old text', 101.0))
        self.poll()
        self.assertIsNone(self.window.state.result.entry)
        self.window.render.assert_not_called()

    def test_reselect_rejects_answer_from_previous_region(self):
        self.window.state.clear('新区域')
        self.window.events.put((0, 'done', self.answer, 'old region text', 101.0))
        self.poll()
        self.assertIsNone(self.window.state.result.entry)
        self.window.render.assert_not_called()

    def test_result_age_expiration_removes_old_answer(self):
        self.window.last_result_at = 90.0
        self.poll()
        self.assertIsNone(self.window.state.result.entry)
        self.window.render.assert_called_once()

    def test_suggested_answer_also_expires(self):
        self.window.state.result = QuizMatch('uncertain', self.answer.entry, .7)
        self.window.last_result_at = 90.0
        self.poll()
        self.assertIsNone(self.window.state.result.entry)

    def test_render_shows_suggestion_and_alternative(self):
        window = self.window
        for attr in ('status', 'answer', 'answer_label', 'question', 'raw'):
            setattr(window, attr, Mock())
        other = QuizEntry('b', '另一种问法', '答案乙')
        result = QuizMatch('uncertain', self.answer.entry, .7, '请核对', (other,))
        QuizWindow.render(window, result, '识别原文')
        window.answer.set.assert_called_once_with('参考：答案甲')
        self.assertIn('候选：答案乙', window.question.set.call_args.args[0])
        window.raw.insert.assert_called_once_with('1.0', '识别原文')

    def test_result_that_waited_too_long_in_queue_is_rejected(self):
        self.window.state.clear('等待题目')
        self.window.events.put((1, 'done', self.answer, 'old text', 90.0))
        self.poll()
        self.assertIsNone(self.window.state.result.entry)
        self.window.render.assert_not_called()

    def run_worker(self, captures, cached=None):
        window = self.window
        window.stop_event = threading.Event()
        window.jobs = queue.Queue()
        window.jobs.put((0, object()))
        window.previous_digest = hashlib.blake2b(cached.tobytes(), digest_size=16).digest() if cached is not None else None
        window.previous_result = (self.answer, 'cached') if cached is not None else None
        window.ocr = Mock(recognize=Mock(return_value=('测试题干', .99)))
        window.entries = ()
        emitted = []

        def emit(event):
            emitted.append(event)
            if event[1] == 'idle':
                window.stop_event.set()

        window._emit = emit
        with patch('quiz_helper.capture_region', side_effect=captures):
            with patch('quiz_helper.match_question', return_value=self.answer):
                window._worker()
        return emitted

    def test_ocr_result_for_changing_pixels_is_never_published(self):
        emitted = self.run_worker([np.zeros((20, 80, 3), dtype=np.uint8),
                                   np.ones((20, 80, 3), dtype=np.uint8)])
        done = [event[2] for event in emitted if event[1] == 'done']
        self.assertEqual(len(done), 1)
        self.assertNotEqual(done[0].status, 'matched')
        self.assertIsNone(done[0].entry)
        self.assertIsNone(self.window.previous_result)

    def test_capture_failure_clears_confirmed_answer(self):
        emitted = self.run_worker([RuntimeError('game hidden')])
        for event in emitted:
            self.window.events.put(event)
        with patch('quiz_helper.time.monotonic', return_value=emitted[-1][4]):
            self.window.poll()
        self.assertIsNone(self.window.state.result.entry)
        self.assertIsNone(self.window.previous_result)
        self.window.ocr.recognize.assert_not_called()
        self.window.render.assert_called_once()

    def test_identical_pixels_reuse_result_without_rerunning_ocr(self):
        pixels = np.zeros((20, 80, 3), dtype=np.uint8)
        emitted = self.run_worker([pixels], cached=pixels)
        self.window.ocr.recognize.assert_not_called()
        done = [event[2] for event in emitted if event[1] == 'done']
        self.assertEqual(done, [self.answer])
        self.assertFalse(any(event[1] == 'pending' for event in emitted))


if __name__ == '__main__':
    unittest.main()
