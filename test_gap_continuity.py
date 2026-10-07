"""Regression of repeated observed production gaps, without private recordings."""
import unittest
from tracker import Tracker, Reading


class GapContinuityTests(unittest.TestCase):
    def sampled(self, duration=1900):
        self.now=0.0
        t=Tracker(lambda:self.now)
        t.feed(Reading(100))
        for second in range(1,duration+1):
            self.now=float(second)
            t.feed(Reading(100+second*10))
        return t

    def test_repeated_production_gaps_preserve_all_five_windows(self):
        t=self.sampled()
        # Gaps from the incident, plus an extended outage. Every window is full.
        for gap in (12.546,6.30,8.009,19.177,32.783,11.051,19.825,15.241,3600):
            before=t.windows.snapshot();trend=t.live.rate();total=t.total
            self.now+=gap
            t.live.invalidate()
            saved=t.export_resume_state()
            self.assertIsNotNone(saved['rates'])
            self.assertEqual(t.live.snapshot()['state'],'stale')
            self.assertEqual(t.feed(Reading(t.previous+900)),('gap',900))
            self.assertEqual(t.total,total+900)
            for width,row in t.windows.snapshot().items():
                self.assertAlmostEqual(row['value'],before[width]['value'])
                self.assertEqual(row['observed_seconds'],before[width]['observed_seconds'])
                self.assertEqual(row['state'],'ready')
            self.assertAlmostEqual(t.live.rate(),trend)
            self.assertTrue(Tracker(lambda:0).restore_resume_state(t.export_resume_state()))
            self.now+=1
            t.feed(Reading(t.previous+10))
            for row in t.windows.snapshot().values():
                self.assertAlmostEqual(row['value'],600)

    def test_delayed_hidden_detection_and_failed_return_keep_checkpoint(self):
        t=self.sampled(220);expected=t.windows.snapshot();total=t.total
        self.now+=33;t.live.invalidate()
        self.assertTrue(t.suspend_visibility())
        self.now+=3600;t.note_visibility_recovery()
        self.now+=100;t.expire_visibility_continuation()
        saved=t.export_resume_state()
        self.assertEqual(saved['rates']['observed_seconds'],220)
        resumed=Tracker(lambda:self.now)
        self.assertTrue(resumed.restore_resume_state(saved))
        self.assertEqual(resumed.feed(Reading(999999)),('baseline',0))
        self.assertEqual(resumed.total,total)
        self.assertEqual(resumed.windows.snapshot(),expected)

    def test_stale_and_invalid_checkpoints_remain_restorable_without_offline_gain(self):
        for delay in (1,33,3600):
            t=self.sampled(220);before=t.windows.snapshot()
            self.now+=delay;t.live.invalidate()
            other=Tracker(lambda:0)
            self.assertTrue(other.restore_resume_state(t.export_resume_state()))
            self.assertEqual(other.feed(Reading(100000)),('baseline',0))
            self.assertEqual(other.windows.snapshot(),before)
            self.assertEqual(other.total,t.total)

    def test_gap_does_not_erase_missing_tail_quality_and_explicit_reset_still_works(self):
        t=self.sampled(220)
        self.now+=1;t._record_gain(5,self.now,missing_gain=True)
        self.now+=33;t._record_gain(900,self.now)
        self.assertTrue(all(row['has_gaps'] for row in t.windows.snapshot().values()))
        self.assertTrue(Tracker(lambda:0).restore_resume_state(t.export_resume_state()))
        t.pause()
        self.assertTrue(all(row['observed_seconds']==0 for row in t.windows.snapshot().values()))


if __name__=='__main__':
    unittest.main()
