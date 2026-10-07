"""Restart the real queue/accounting path using isolated SQLite stores."""
import csv
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import app
from auto_level import AutoLevel
from history_store import HistoryStore
import test_rate_app as harness
from tracker import Tracker


class AppResumeTests(unittest.TestCase):
    setUp = harness.RateAppTests.setUp
    receive = harness.RateAppTests.receive
    last_row = harness.RateAppTests.last_row
    status_json = harness.RateAppTests.status_json

    def restart(self, at=5, clean=True):
        b = self.backend
        path = b.history.path
        if clean:
            b.close()
        else:
            b.history.db.close()  # No close checkpoint: emulate a crashed process.
        self.now[0] = at
        b.history = HistoryStore(path)
        self.addCleanup(b.history.db.close)
        b.tracker = Tracker(lambda: self.now[0])
        b.auto_level = AutoLevel(b.tracker, lambda: self.now[0])
        b.running = False
        b.reading_valid = False
        b.records = []
        b.record_seqs = []
        b.latest = None
        b.last_valid_time = b.last_valid_monotonic = None
        b.storage_error = None
        b.clear_rate_display()
        return b.restore_statistics()

    def populate(self):
        self.receive(100, 1000)
        for second in range(1, 66):
            self.receive(100+second, 1000+second*10)

    def test_clean_restart_keeps_windows_time_total_and_export(self):
        self.populate()
        b = self.backend
        before = b.rate_snapshot()
        session, seq = b.history.active, b.history.seq
        self.assertTrue(self.restart())
        self.assertEqual((b.tracker.total, b.tracker.elapsed), (650, 65))
        self.assertEqual((b.history.active, b.history.seq), (session, seq))
        self.assertEqual(len(b.records), seq)
        restored = b.display_rate_snapshot()
        self.assertEqual(restored['display_state'], 'restored')
        self.assertEqual(restored['windows'], before['windows'])
        self.assertFalse(b.fresh_reading())
        self.assertIsNone(self.status_json()['primary_per_minute'])
        self.now[0] = 50000
        self.assertEqual(b.tracker.elapsed, 65)
        self.assertEqual(b.display_rate_snapshot()['windows'], before['windows'])
        b.auto_game()  # Production startup route must not erase restored windows.
        b.auto_game()  # Repeated locating/--auto-start callbacks are idempotent.
        self.assertTrue(b.running)
        self.assertTrue(b.tracker.resume_pending)
        self.receive(50001, 5)  # Offline level/EXP changes only establish baseline.
        self.assertEqual(b.tracker.total, 650)
        self.assertEqual(b.tracker.elapsed, 65)
        self.assertEqual(b.rate_snapshot()['windows'], before['windows'])
        self.assertEqual(b.history.seq, seq+1)
        self.assertEqual(self.last_row()['gain'], 0)
        self.receive(50002, 15)
        self.assertEqual((b.tracker.total, b.tracker.elapsed), (660, 66))
        self.assertEqual(b.rate_snapshot()['windows']['1800']['observed_seconds'], 66)
        target = Path(self.temp.name)/'resumed.csv'
        with patch.object(app.filedialog, 'asksaveasfilename', return_value=str(target)):
            b.export()
        with target.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(len(rows), seq+3)
        self.assertEqual(rows[2][4], '10')
        self.assertEqual(rows[-1][4], '660')
        self.assertEqual(rows[-1][10], 'time_ewma_v1')

    def test_crash_restores_last_committed_sample_and_repeated_restart_is_idempotent(self):
        self.receive(100, 1000)
        self.receive(101, 1100)
        b = self.backend
        session = b.history.active
        self.assertTrue(self.restart(clean=False))
        snapshot = b.display_rate_snapshot()
        self.now[0] = 90000
        b.persist_time()
        self.assertTrue(self.restart(at=3))
        self.assertEqual((b.tracker.total, b.tracker.elapsed), (100, 1))
        self.assertEqual(b.display_rate_snapshot()['windows'], snapshot['windows'])
        self.assertEqual(b.history.active, session)
        seconds = b.history.db.execute('SELECT SUM(seconds) FROM daily_time').fetchone()[0]
        self.assertEqual(seconds, 1)
        self.assertEqual(len(b.records), 2)

    def test_pause_and_explicit_reset_do_not_resurrect_old_windows_or_record(self):
        self.populate()
        b = self.backend
        b.pause()
        self.assertTrue(self.restart())
        self.assertEqual(b.tracker.total, 650)
        self.assertIsNone(b.display_rate_snapshot()['primary']['value'])
        with patch.object(app.messagebox, 'askyesno', return_value=True):
            b.reset()
        self.assertFalse(self.restart(clean=False))
        self.assertEqual(b.tracker.total, 0)
        self.assertEqual(b.records, [])
        self.assertIsNone(b.history.active)
        self.assertEqual(b.history.db.execute('SELECT SUM(gain) FROM samples').fetchone()[0], 650)

    def test_failed_sample_cannot_overwrite_last_committed_resume_state(self):
        self.receive(100, 1000)
        self.receive(101, 1100)
        b = self.backend
        with patch.object(b.history, 'append', side_effect=sqlite3.OperationalError('disk full')):
            self.receive(102, 1200)
        self.assertIsNotNone(b.storage_error)
        seq = b.history.seq
        b.toggle()
        self.assertFalse(b.running)
        b.auto_game()
        self.assertFalse(b.running)
        b.rebase()
        b.level()
        self.assertFalse(b.record('baseline'))
        self.assertEqual(b.history.seq, seq)
        b.persist_time()
        self.assertTrue(self.restart())
        self.assertEqual((b.tracker.total, b.tracker.elapsed), (100, 1))
        self.assertEqual(len(b.records), 2)

    def test_first_upgrade_restores_old_total_time_without_fabricating_windows(self):
        self.populate()
        b = self.backend
        b.history.db.execute('DELETE FROM resume_state')
        b.history.db.commit()
        self.assertTrue(self.restart(clean=False))
        self.assertEqual((b.tracker.total, b.tracker.elapsed), (650, 65))
        self.assertIsNone(b.display_rate_snapshot()['primary']['value'])
        self.assertEqual(len(b.records), 66)

    def test_close_during_short_ocr_retry_preserves_only_last_trusted_progress(self):
        self.populate()
        b = self.backend
        saved = b.rate_snapshot()['windows']
        self.receive(165.8, None)
        self.assertEqual(b.display_rate_snapshot()['display_state'], 'retrying')
        self.assertTrue(self.restart())
        self.assertEqual(b.tracker.total, 650)
        self.assertAlmostEqual(b.tracker.elapsed, 65.8)
        self.assertEqual(b.display_rate_snapshot()['windows'], saved)
        self.assertEqual(len(b.records), 66)


if __name__ == '__main__':
    unittest.main()
