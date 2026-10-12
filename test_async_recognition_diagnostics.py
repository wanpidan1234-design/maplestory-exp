import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from recognition_diagnostics import AsyncRecognitionJournal, RecognitionJournal


class BlockingJournal:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.last_error = None

    def record(self, stage, metadata, frame, level_frame):
        self.entered.set()
        self.release.wait(3)
        self.calls.append((stage, metadata, frame, level_frame))
        return Path('/private/directory/slot-00.json')


class AsyncRecognitionJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'evidence'

    def blocked(self, **kwargs):
        writer = BlockingJournal()
        journal = AsyncRecognitionJournal(self.directory, journal=writer, **kwargs)
        self.addCleanup(lambda: journal.close(1))
        self.addCleanup(writer.release.set)
        return journal, writer

    def test_blocked_writer_bounds_pending_and_close(self):
        journal, writer = self.blocked(min_interval=0)
        journal.record('first', {'sample_id': 1})
        self.assertTrue(writer.entered.wait(1))
        start = time.monotonic()
        for sample in range(2, 100):
            self.assertIsNone(journal.record('next', {'sample_id': sample}))
        self.assertLess(time.monotonic() - start, .2)
        status = journal.status()
        self.assertEqual(status['queue'], 1)
        self.assertTrue(status['inflight'])
        self.assertEqual(status['dropped'], 97)
        start = time.monotonic()
        self.assertFalse(journal.close(.02))
        self.assertLess(time.monotonic() - start, .2)
        writer.release.set()
        self.assertTrue(journal.close(1))
        self.assertEqual(len(writer.calls), 2)
        journal.record('after-close', {})
        self.assertEqual(journal.status()['dropped'], 98)

    def test_metadata_and_roi_are_detached_without_producer_encoding(self):
        callbacks = []
        journal, writer = self.blocked(min_interval=0, on_result=callbacks.append)
        journal.record('first', {})
        self.assertTrue(writer.entered.wait(1))
        metadata = {'sample_id': 'sample-2', 'capture': {'ocr': [['123', .9]]}}
        frame = np.full((12, 80, 3), 7, dtype=np.uint8)
        with patch('recognition_diagnostics.json.dumps', side_effect=AssertionError('JSON on producer')), \
                patch('recognition_diagnostics.cv2.imencode', side_effect=AssertionError('PNG on producer')):
            journal.record('ocr', metadata, frame, frame)
        metadata['capture']['ocr'][0][0] = 'changed'
        frame[:] = 0
        writer.release.set()
        self.assertTrue(journal.close(1))
        _, detached, exp, level = writer.calls[1]
        self.assertEqual(detached['capture']['ocr'][0][0], '123')
        self.assertTrue(np.all(exp == 7))
        self.assertTrue(np.all(level == 7))
        self.assertEqual(callbacks[1], dict(sample_id='sample-2', stage='ocr',
                         slot_file='slot-00.json', error_type=None, result='written'))
        self.assertFalse(self.directory.exists())

    def test_throttle_precedes_copy_and_is_shared_across_stages(self):
        now = [0.]
        journal, writer = self.blocked(clock=lambda: now[0])
        journal.record('first', {})
        self.assertTrue(writer.entered.wait(1))
        with patch.object(journal, '_frame', side_effect=AssertionError('must not copy')):
            now[0] = 4.99
            journal.record('other', {}, np.zeros((10, 10), dtype=np.uint8))
        self.assertEqual(journal.status()['throttled'], 1)
        self.assertEqual(journal.status()['dropped'], 0)
        now[0] = 5
        journal.record('boundary', {})
        self.assertEqual(journal.status()['queue'], 1)

    def test_large_or_invalid_roi_is_not_copied_or_resized(self):
        journal, writer = self.blocked(min_interval=0)
        journal.record('first', {})
        self.assertTrue(writer.entered.wait(1))
        large = np.zeros((1080, 1920, 3), dtype=np.uint8)
        invalid = np.zeros((12, 80), dtype=np.float32)
        journal.record('rejected', {}, large, invalid)
        writer.release.set()
        self.assertTrue(journal.close(1))
        self.assertIsNone(writer.calls[1][2])
        self.assertIsNone(writer.calls[1][3])

    def test_rotation_and_committed_png_are_delegated(self):
        sync = RecognitionJournal(self.directory, max_entries=1, min_interval=0)
        roi = np.full((12, 80, 3), 190, dtype=np.uint8)
        for index in range(3):
            journal = AsyncRecognitionJournal(self.directory, journal=sync, min_interval=0)
            journal.record('ocr', {'sample_id': index}, roi if index == 0 else None)
            self.assertTrue(journal.close(2))
            self.assertEqual(journal.status()['write_errors'], 0)
        paths = list(self.directory.glob('slot-*.json'))
        self.assertEqual(len(paths), 1)
        row = json.loads(paths[0].read_text(encoding='utf-8'))
        self.assertEqual(row['sequence'], 2)
        self.assertEqual(row['metadata']['sample_id'], 2)
        self.assertEqual(list(self.directory.glob('*.png')), [])

    def test_writer_and_callback_errors_are_isolated(self):
        class FailedJournal:
            last_error = None

            def record(self, *args):
                raise PermissionError('private path must not enter callback')

        callbacks = []

        def callback(result):
            callbacks.append(result)
            raise RuntimeError('callback failed')

        journal = AsyncRecognitionJournal(self.directory, journal=FailedJournal(), on_result=callback)
        journal.record('ocr', {'sample_id': 7})
        self.assertTrue(journal.close(1))
        self.assertEqual(journal.last_error, 'PermissionError')
        self.assertEqual(journal.status()['write_errors'], 1)
        self.assertEqual(journal.status()['callback_errors'], 1)
        self.assertEqual(callbacks, [dict(sample_id=7, stage='ocr', slot_file=None,
                                        error_type='PermissionError', result='error')])

    def test_metadata_is_bounded_and_custom_mapping_is_never_called(self):
        class Hostile(dict):
            def items(self):
                raise AssertionError('custom mapping must not run')

        journal, writer = self.blocked(min_interval=0)
        journal.record('first', {})
        self.assertTrue(writer.entered.wait(1))
        recursive = {}
        recursive['self'] = recursive
        journal.record('bounded', {'hostile': Hostile(), 'recursive': recursive,
                                   'huge_int': 1 << 50000,
                                   'large': ['\n"中' * 5000 for _ in range(100)]})
        writer.release.set()
        self.assertTrue(journal.close(1))
        metadata = writer.calls[1][1]
        self.assertEqual(metadata['hostile'], '<unsupported>')
        self.assertEqual(metadata['huge_int'], '<large-int>')
        self.assertLess(len(json.dumps(metadata, ensure_ascii=False).encode('utf-8')), 65536)


if __name__ == '__main__':
    unittest.main()
