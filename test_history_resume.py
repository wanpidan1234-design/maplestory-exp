"""Restart accounting uses only committed local state, never offline time."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from app_time import BEIJING, format_time, local_day
from pathlib import Path

from history_store import HistoryStore, local_day


class ResumeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'history.sqlite3'
        self.store=HistoryStore(self.path)
        self.start=datetime(2026,9,27,12).replace(tzinfo=BEIJING).timestamp()

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    @staticmethod
    def state(total,elapsed):
        return dict(version=1,total=total,elapsed=elapsed,windows={'saved':'opaque'})

    @staticmethod
    def row(gain,total,elapsed):
        return ['',round(elapsed,2),1000+total,gain,total,0,0,'ok','sample']

    def append(self,gain,total,elapsed,now):
        state=self.state(total,elapsed)
        self.store.append(self.row(gain,total,elapsed),now=now,resume_state=state)
        return state

    def reopen(self):
        self.store.db.close()
        self.store=HistoryStore(self.path)

    def duration(self):
        return self.store.db.execute('SELECT COALESCE(SUM(seconds),0) FROM daily_time').fetchone()[0]

    def reject_resume_writes(self):
        self.store.db.execute("""CREATE TRIGGER reject_resume BEFORE INSERT ON resume_state
            BEGIN SELECT RAISE(ABORT,'resume write failed'); END""")

    def test_normal_close_save_restores_session_and_precise_effective_time(self):
        self.append(0,0,0,self.start)
        self.append(100,100,1.123456,self.start+1.123456)
        session=self.store.active
        state=self.state(100,1.765432)
        self.store.save_resume_state(state,1.765432,self.start+1.765432)
        self.reopen()
        self.assertEqual(self.store.load_resume_state(self.start+86400),state)
        self.assertEqual(self.store.active,session)
        self.assertEqual(self.store.seq,2)
        self.assertEqual(self.store.last_elapsed,1.765432)
        self.assertEqual(self.store.last_wall,self.start+86400)
        self.assertAlmostEqual(self.duration(),1.765432)

    def test_crash_after_append_has_atomic_sample_duration_and_state(self):
        self.append(0,0,0,self.start)
        state=self.append(200,200,2.123456,self.start+2.123456)
        self.reopen()
        self.assertEqual(self.store.load_resume_state(self.start+10000),state)
        self.assertAlmostEqual(self.duration(),2.123456)
        self.assertEqual(self.store.seq,2)

    def test_offline_midnight_does_not_receive_effective_time(self):
        before=datetime(2026,9,27,23,59,50).replace(tzinfo=BEIJING).timestamp()
        resumed=datetime(2026,9,30,8).replace(tzinfo=BEIJING).timestamp()
        self.append(0,0,0,before)
        self.append(100,100,5,before+5)
        self.reopen()
        self.store.load_resume_state(resumed)
        self.store.save_resume_state(self.state(100,7),7,resumed+2)
        times={r['day']:r['seconds'] for r in self.store.days()}
        self.assertEqual(times,{'2026-09-27':5,'2026-09-30':2})
        self.assertEqual(sum(times.values()),7)

    def test_appending_after_restore_preserves_sequence_and_gain(self):
        self.append(0,0,0,self.start)
        self.append(100,100,1,self.start+1)
        session=self.store.active
        self.reopen()
        self.store.load_resume_state(self.start+100)
        self.append(0,100,1,self.start+100)  # Restart's baseline has no gain.
        self.append(50,150,2,self.start+101)
        self.assertEqual(self.store.active,session)
        self.assertEqual(self.store.seq,4)
        rows=self.store.samples(local_day(self.start),session)
        self.assertEqual([r['seq'] for r in rows],[1,2,3,4])
        self.assertEqual(sum(r['gain'] for r in rows),150)
        self.assertEqual(self.duration(),2)

    def test_session_records_restore_all_dates_and_original_export_mapping(self):
        before=datetime(2026,9,27,23,59,59).replace(tzinfo=BEIJING).timestamp()
        self.append(0,0,0,before)
        self.append(10,10,2,before+2)
        self.reopen()
        self.store.load_resume_state(before+200)
        rows,seqs=self.store.session_records()
        self.assertEqual(seqs,[1,2])
        self.assertEqual([r[0][:10] for r in rows],['2026-09-27','2026-09-28'])
        self.assertTrue(all(len(row)==9 for row in rows))
        self.assertEqual([r[3] for r in rows],[0,10])
        self.assertEqual([r['seq'] for r in self.store.session_rate_metadata(self.store.active)],seqs)

    def test_clear_is_an_explicit_persistent_reset_and_detaches(self):
        self.append(20,20,2,self.start)
        self.store.clear_resume_state()
        self.assertEqual((self.store.active,self.store.seq,self.store.last_elapsed,self.store.last_wall),
                         (None,0,0,None))
        self.reopen()
        self.assertIsNone(self.store.load_resume_state(self.start+100))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM samples').fetchone()[0],1)
        self.assertEqual(self.duration(),2)

    def test_finish_clears_resume_in_same_transaction_as_final_time(self):
        self.append(20,20,2,self.start)
        session=self.store.active
        self.store.finish(3,self.start+1)
        self.reopen()
        self.assertIsNone(self.store.load_resume_state(self.start+100))
        self.assertEqual(self.duration(),3)
        self.assertEqual(self.store.db.execute('SELECT ended FROM sessions WHERE id=?',(session,)).fetchone()[0],self.start+1)

    def test_repeated_restarts_never_duplicate_gain_or_duration(self):
        state=self.append(100,100,12.345,self.start)
        for offset in (100,1000,10000):
            self.reopen()
            self.assertEqual(self.store.load_resume_state(self.start+offset),state)
            self.store.save_resume_state(state,state['elapsed'],self.start+offset+1)
        self.assertAlmostEqual(self.duration(),12.345)
        self.assertEqual(self.store.days()[0]['gain'],100)

    def test_first_append_rollback_does_not_leave_session_or_memory(self):
        self.reject_resume_writes()
        with self.assertRaises(sqlite3.IntegrityError):
            self.append(100,100,1,self.start)
        self.assertEqual((self.store.active,self.store.seq,self.store.last_elapsed,self.store.last_wall),
                         (None,0,0,None))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM samples').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)
        self.assertEqual(self.duration(),0)

    def test_later_append_rollback_keeps_previous_state_and_memory(self):
        initial=self.append(0,0,0,self.start)
        before=(self.store.active,self.store.seq,self.store.last_elapsed,self.store.last_wall)
        self.reject_resume_writes()
        with self.assertRaises(sqlite3.IntegrityError):
            self.append(100,100,1,self.start+1)
        self.assertEqual((self.store.active,self.store.seq,self.store.last_elapsed,self.store.last_wall),before)
        self.assertEqual(self.duration(),0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM samples').fetchone()[0],1)
        saved=self.store.db.execute('SELECT payload FROM resume_state').fetchone()[0]
        self.assertEqual(json.loads(saved),initial)

    def test_save_rollback_does_not_advance_duration_or_checkpoint(self):
        self.append(100,100,1,self.start)
        before=(self.store.last_elapsed,self.store.last_wall)
        self.reject_resume_writes()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_resume_state(self.state(100,2),2,self.start+1)
        self.assertEqual((self.store.last_elapsed,self.store.last_wall),before)
        self.assertEqual(self.duration(),1)

    def test_finish_rollback_does_not_end_or_detach_session(self):
        self.append(100,100,1,self.start)
        before=(self.store.active,self.store.seq,self.store.last_elapsed,self.store.last_wall)
        self.reject_resume_writes()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.finish(2,self.start+1)
        self.assertEqual((self.store.active,self.store.seq,self.store.last_elapsed,self.store.last_wall),before)
        self.assertIsNone(self.store.db.execute('SELECT ended FROM sessions').fetchone()[0])
        self.assertEqual(self.duration(),1)

    def test_clear_rollback_keeps_session_attached(self):
        self.append(100,100,1,self.start)
        before=(self.store.active,self.store.seq,self.store.last_elapsed,self.store.last_wall)
        self.reject_resume_writes()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.clear_resume_state()
        self.assertEqual((self.store.active,self.store.seq,self.store.last_elapsed,self.store.last_wall),before)

    def test_saved_total_must_match_committed_ledger(self):
        self.append(100,100,1,self.start)
        with self.assertRaises(ValueError):
            self.store.save_resume_state(self.state(999,2),2,self.start+1)
        with self.assertRaises(ValueError):
            self.store.append(self.row(10,110,2),now=self.start+1,resume_state=self.state(999,2))
        self.assertEqual(self.duration(),1)
        self.assertEqual(self.store.seq,1)

    def test_corrupt_payload_is_not_restored_or_replaced_by_legacy(self):
        self.append(100,100,1,self.start)
        with self.store.db:
            self.store.db.execute("UPDATE resume_state SET payload='{' WHERE id=1")
        self.reopen()
        self.assertIsNone(self.store.load_resume_state(self.start+100))
        self.assertIsNone(self.store.active)
        self.assertEqual(self.duration(),1)

    def test_newer_sample_makes_an_old_state_unrestorable(self):
        self.append(100,100,1,self.start)
        self.store.append(self.row(100,200,2),now=self.start+1)
        self.reopen()
        self.assertIsNone(self.store.load_resume_state(self.start+100))
        self.assertIsNone(self.store.active)

    def test_duration_mismatch_and_unknown_version_are_rejected(self):
        self.append(100,100,1,self.start)
        with self.store.db:
            self.store.db.execute('UPDATE resume_state SET version=2')
        self.reopen()
        self.assertIsNone(self.store.load_resume_state(self.start+100))
        with self.store.db:
            self.store.db.execute('UPDATE resume_state SET version=1')
            self.store.db.execute('UPDATE daily_time SET seconds=2')
        self.assertIsNone(self.store.load_resume_state(self.start+100))
        self.assertIsNone(self.store.active)

    def test_invalid_payload_numbers_and_nonobjects_are_rejected(self):
        self.append(100,100,1,self.start)
        for payload in ('[]','null','{"total":100,"elapsed":true}',
                        '{"total":999,"elapsed":1}','{"total":100,"elapsed":NaN}'):
            with self.store.db:
                self.store.db.execute('UPDATE resume_state SET payload=?',(payload,))
            self.reopen()
            self.assertIsNone(self.store.load_resume_state(self.start+100))
            self.assertIsNone(self.store.active)
        self.assertEqual(self.duration(),1)

    def test_missing_session_is_rejected_without_legacy_fallback(self):
        self.append(100,100,1,self.start)
        with self.store.db:
            self.store.db.execute("UPDATE resume_state SET session_id='missing'")
        self.reopen()
        self.assertIsNone(self.store.load_resume_state(self.start+100))
        self.assertIsNone(self.store.active)
        self.assertEqual(self.duration(),1)

    def test_corrupt_legacy_aggregate_does_not_attach_or_invent_time(self):
        self.store.append(self.row(0,-100,1),now=self.start)
        self.reopen()
        self.assertIsNone(self.store.load_resume_state(self.start+100))
        self.assertIsNone(self.store.active)
        self.assertEqual(self.duration(),0)

    def test_legacy_closed_session_recovers_known_tail_once_without_offline_time(self):
        before=datetime(2026,9,27,23,59,50).replace(tzinfo=BEIJING).timestamp()
        self.store.append(self.row(100,100,1),now=before)
        session=self.store.active
        self.store.checkpoint(1,before)
        self.store.append(self.row(100,200,2),now=before+1)
        with self.store.db:
            self.store.db.execute('UPDATE sessions SET ended=? WHERE id=?',(before+1,session))
        resumed=datetime(2026,9,30,8).replace(tzinfo=BEIJING).timestamp()
        for offset in (0,100):
            self.reopen()
            self.assertEqual(self.store.load_resume_state(resumed+offset),dict(legacy=True,total=200,elapsed=2))
            self.assertEqual(self.store.active,session)
            self.assertEqual(self.duration(),2)
            self.assertEqual({r['day'] for r in self.store.days()},{'2026-09-27'})
        self.assertIsNone(self.store.db.execute('SELECT ended FROM sessions').fetchone()[0])

    def test_legacy_final_checkpoint_later_than_sample_is_preserved(self):
        self.store.append(self.row(100,100,1),now=self.start)
        self.store.checkpoint(1.654321,self.start+0.654321)
        self.reopen()
        state=self.store.load_resume_state(self.start+100)
        self.assertEqual(state,dict(legacy=True,total=100,elapsed=1.654321))
        self.assertAlmostEqual(self.duration(),1.654321)

    def test_legacy_migration_rollback_does_not_duplicate_tail_on_retry(self):
        self.store.append(self.row(100,100,1),now=self.start)
        self.reopen()
        self.reject_resume_writes()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.load_resume_state(self.start+100)
        self.assertEqual(self.duration(),0)
        self.assertIsNone(self.store.active)
        self.store.db.execute('DROP TRIGGER reject_resume')
        self.assertEqual(self.store.load_resume_state(self.start+100)['elapsed'],1)
        self.assertEqual(self.duration(),1)

    def test_empty_start_writes_marker_instead_of_searching_history_later(self):
        self.assertIsNone(self.store.load_resume_state(self.start))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM resume_state').fetchone()[0],1)
        self.store.save_resume_state(self.state(0,0),0,self.start)
        self.reopen()
        self.assertEqual(self.store.load_resume_state(self.start+100),self.state(0,0))
        self.assertIsNone(self.store.active)


if __name__=='__main__':
    unittest.main()
