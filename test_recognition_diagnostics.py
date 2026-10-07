import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from recognition_diagnostics import RecognitionJournal


class RecognitionJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / '诊断记录'
        self.now = [0.0]
        self.journal = RecognitionJournal(self.directory, clock=lambda: self.now[0],
                                          wall_clock=lambda: 1234.5)

    def tearDown(self):
        self.temporary.cleanup()

    def read(self, path):
        return json.loads(path.read_text(encoding='utf-8'))

    def test_all_stages_share_throttle_and_exact_boundary(self):
        first = self.journal.record('anchor', {'message': '未定位'})
        self.assertIsNotNone(first)
        self.now[0] = 4.99
        self.assertIsNone(self.journal.record('ocr', {}))
        self.now[0] = 5
        self.assertIsNotNone(self.journal.record('ocr', {}))
        self.assertEqual(len(list(self.directory.glob('slot-*.json'))), 2)

    def test_retention_and_reopened_journals_use_fixed_slots(self):
        for index in range(55):
            journal = RecognitionJournal(self.directory, min_interval=0)
            self.assertIsNotNone(journal.record('failure', {'index': index}))
        paths = sorted(self.directory.glob('slot-*.json'))
        self.assertEqual(len(paths), 24)
        self.assertEqual({self.read(path)['sequence'] for path in paths}, set(range(31, 55)))
        self.assertEqual({path.name for path in paths}, {f'slot-{i:02d}.json' for i in range(24)})

    def test_replacing_slot_without_frames_removes_old_images_only(self):
        journal = RecognitionJournal(self.directory, max_entries=1, min_interval=0)
        roi = np.full((12, 80, 3), 180, dtype=np.uint8)
        path = journal.record('ocr', {}, roi, roi)
        row = self.read(path)
        image_path = self.directory / row['frames']['exp']['path']
        self.assertTrue(image_path.exists())
        decoded = cv2.imdecode(np.frombuffer(image_path.read_bytes(), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        np.testing.assert_array_equal(decoded, roi)
        unrelated = self.directory / 'keep.png'
        unrelated.write_bytes(b'untouched')
        path = journal.record('anchor', {})
        self.assertEqual(self.read(path)['omitted'], {'exp': 'no_frame', 'level': 'no_frame'})
        self.assertFalse((self.directory / 'slot-00-exp.png').exists())
        self.assertFalse((self.directory / 'slot-00-level.png').exists())
        self.assertEqual(unrelated.read_bytes(), b'untouched')

    def test_big_or_invalid_frames_are_omitted_without_resizing(self):
        large = np.zeros((1080, 1920, 3), dtype=np.uint8)
        path = self.journal.record('large', {}, large, np.zeros((20, 20), dtype=np.float32))
        row = self.read(path)
        self.assertEqual(row['shape']['exp'], [1080, 1920, 3])
        self.assertEqual(row['omitted']['exp'], 'roi_too_large')
        self.assertEqual(row['omitted']['level'], 'not_uint8')
        self.assertEqual(list(self.directory.glob('*.png')), [])

    def test_metadata_is_safe_and_bounded(self):
        class BadRepr:
            def __repr__(self):
                raise RuntimeError('must not call repr')

        recursive = {}
        recursive['self'] = recursive
        metadata = {'object': BadRepr(), 'number': np.int64(7), 'missing': float('nan'),
                    'pixels': np.zeros((100, 100), dtype=np.uint8), 'recursive': recursive,
                    'large': {str(i): '\n"中' * 5000 for i in range(100)}}
        row = self.read(self.journal.record('元数据', metadata))
        self.assertLessEqual(len(json.dumps(row['metadata'], ensure_ascii=False).encode('utf-8')), 64 * 1024)
        self.assertTrue(row['metadata_truncated'])
        self.assertIsNone(self.journal.last_error)

    def test_io_failure_is_nonfatal_and_throttled(self):
        with patch.object(RecognitionJournal, '_atomic_write', side_effect=PermissionError('denied')):
            self.assertIsNone(self.journal.record('failure', {}))
        self.assertIn('PermissionError', self.journal.last_error)
        self.assertIsNone(self.journal.record('failure', {}))
        self.now[0] = 5
        self.assertIsNotNone(self.journal.record('recovered', {}))
        self.assertIsNone(self.journal.last_error)

    def test_encode_and_clock_failure_do_not_raise(self):
        roi = np.zeros((10, 10), dtype=np.uint8)
        with patch('recognition_diagnostics.cv2.imencode', side_effect=RuntimeError('encoder failed')):
            self.assertIsNone(self.journal.record('encode', {}, roi))
        self.assertIn('encoder failed', self.journal.last_error)
        self.now[0] = float('nan')
        self.assertIsNone(self.journal.record('clock', {}))
        self.assertIn('non-finite', self.journal.last_error)

    def test_threads_and_distinct_instances_do_not_corrupt_slots(self):
        journals = [RecognitionJournal(self.directory, max_entries=4, min_interval=0) for _ in range(3)]
        errors = []

        def save(journal):
            for _ in range(8):
                if journal.record('thread', {}) is None:
                    errors.append(journal.last_error)

        threads = [threading.Thread(target=save, args=(journal,)) for journal in journals]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        rows = [self.read(path) for path in self.directory.glob('slot-*.json')]
        self.assertEqual({row['sequence'] for row in rows}, {20, 21, 22, 23})
        self.assertEqual(list(self.directory.glob('*.tmp')), [])

    def test_failed_atomic_replace_cleans_temporary_file(self):
        with patch('recognition_diagnostics.os.replace', side_effect=OSError('replace failed')):
            self.assertIsNone(self.journal.record('failure', {}))
        self.assertEqual(list(self.directory.glob('*.tmp')), [])
        self.assertLessEqual(len(self.journal.last_error), 240)


if __name__ == '__main__':
    unittest.main()
