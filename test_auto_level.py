import math
import unittest
from tracker import Tracker,Reading
from auto_level import AutoLevel


class AutomaticLevelTests(unittest.TestCase):
    def setUp(self):
        self.now=0.;self.tracker=Tracker(lambda:self.now);self.guard=AutoLevel(self.tracker,lambda:self.now)

    def feed(self,exp,level=61,percent=95,cap=1000,dt=.3):
        self.now+=dt
        return self.guard.feed(Reading(exp,cap,percent),level)

    def baseline(self,cap=1000):
        for _ in range(3):self.feed(950,cap=cap)
        self.assertEqual(self.guard.level,61)

    def test_level_requires_three_fresh_frames_and_half_second(self):
        self.feed(950);self.feed(950);self.assertIsNone(self.tracker.level)
        self.feed(950);self.assertEqual(self.tracker.level,61)

    def test_confirm_upgrade_and_resume_once(self):
        self.baseline()
        self.assertEqual(self.feed(50,62,4,1200)[0],'level_pending')
        self.assertEqual(self.feed(60,62,5,1200)[0],'level_pending')
        self.assertEqual(self.feed(70,62,6,1200),('auto_level',120))
        self.assertEqual(self.tracker.confirmed_levels,1)
        self.assertEqual(self.tracker.level,62)
        self.assertEqual(self.feed(80,62,7,1200),('ok',10))
        self.assertEqual(self.tracker.total,130)
        self.assertEqual(self.tracker.confirmed_levels,1)
        self.assertFalse(self.tracker.blocked)

    def test_unknown_cap_counts_only_observed_new_level_exp(self):
        self.baseline(cap=None)
        for value in (50,60,70):result=self.feed(value,62,4,cap=None)
        self.assertEqual(result,('auto_level_gap',70));self.assertEqual(self.tracker.level_gaps,1)
        self.assertTrue(self.guard.last_event['missing_tail'])

    def test_death_never_celebrates(self):
        self.baseline()
        for _ in range(13):result=self.feed(700,61,70)
        self.assertTrue(self.tracker.blocked);self.assertEqual(self.tracker.total,0)
        self.assertEqual(self.tracker.confirmed_levels,0)

    def test_transient_exp_drop_recovers_without_false_gain(self):
        self.baseline();self.feed(90,61,9)
        self.assertEqual(self.feed(960,61,96),('ok',10))
        self.assertEqual(self.tracker.confirmed_levels,0)

    def test_one_frame_higher_level_does_not_confirm(self):
        self.baseline();self.feed(50,62,4)
        for _ in range(3):self.feed(950,61,95)
        self.assertEqual(self.tracker.confirmed_levels,0);self.assertEqual(self.tracker.total,0)

    def test_level_change_without_exp_reset_is_rejected(self):
        self.baseline()
        for _ in range(13):self.feed(950,62,95)
        self.assertTrue(self.tracker.blocked);self.assertEqual(self.tracker.confirmed_levels,0)

    def test_missing_level_votes_reset_streak(self):
        self.baseline();self.feed(50,62,4);self.feed(55,None,4)
        self.feed(60,62,5);self.feed(70,62,6)
        self.assertEqual(self.tracker.confirmed_levels,0)
        self.feed(80,62,7);self.assertEqual(self.tracker.confirmed_levels,1)

    def test_multi_level_jump_or_character_change_is_not_single_upgrade(self):
        for new in (63,20):
            self.setUp();self.baseline()
            for _ in range(13):self.feed(50,new,4)
            self.assertTrue(self.tracker.blocked);self.assertEqual(self.tracker.confirmed_levels,0)

    def test_resume_baseline_never_replays_an_upgrade(self):
        self.baseline();self.guard.reset();self.tracker.previous=None
        for _ in range(3):self.feed(50,62,4)
        self.assertEqual(self.tracker.level,62);self.assertEqual(self.tracker.confirmed_levels,0)

    def test_level_becomes_readable_after_game_flash(self):
        self.baseline()
        self.feed(50,None,4)
        self.feed(60,62,5,dt=4.)
        self.feed(70,62,6)
        self.assertEqual(self.feed(80,62,7)[0],'auto_level')
        self.assertEqual(self.tracker.confirmed_levels,1)

    def test_known_cap_upgrade_updates_live_once_over_confirmed_interval(self):
        self.baseline()
        segment=self.tracker.live.segment
        self.feed(50,62,4,1200)
        self.assertIsNone(self.tracker.live.rate())
        self.feed(60,62,5,1200)
        self.assertEqual(self.feed(70,62,6,1200),('auto_level',120))
        # Two zero intervals followed by one confirmed 0.9 second interval.
        half_life=self.tracker.live.half_life
        expected=(120*60/.9)*(1-2**(-.9/half_life))/(1-2**(-1.5/half_life))
        self.assertAlmostEqual(self.tracker.live.rate(),expected)
        self.assertEqual(self.tracker.live.segment,segment)
        self.assertEqual(self.tracker.total,120)

    def test_unknown_tail_preserves_live_as_a_lower_bound(self):
        self.baseline(cap=None)
        segment=self.tracker.live.segment
        origin=self.tracker.live.origin
        for value in (50,60,70):result=self.feed(value,62,4,cap=None)
        self.assertEqual(result,('auto_level_gap',70))
        self.assertEqual(self.tracker.live.segment,segment)
        self.assertEqual(self.tracker.live.origin,origin)
        snapshot=self.tracker.live.snapshot()
        self.assertGreater(snapshot['value'],0)
        self.assertAlmostEqual(snapshot['observed_seconds'],1.5)
        self.assertTrue(snapshot['has_gaps'])
        self.assertEqual(snapshot['quality'],'lower_bound')
        self.feed(80,62,5,cap=None)
        self.assertEqual(self.tracker.live.segment,segment)
        self.assertEqual(self.tracker.live.snapshot()['quality'],'lower_bound')
        self.assertEqual(self.tracker.total,80)

    def test_unknown_tail_after_real_long_outage_preserves_rates(self):
        self.baseline(cap=None)
        before=self.tracker.windows.snapshot()
        segment=self.tracker.live.segment
        self.feed(50,None,4,cap=None)
        self.feed(60,62,5,cap=None,dt=6)
        self.feed(70,62,6,cap=None)
        self.assertEqual(self.feed(80,62,7,cap=None),('auto_level_gap',80))
        self.assertEqual(self.tracker.total,80)
        self.assertEqual(self.tracker.level_gaps,1)
        self.assertNotEqual(self.tracker.live.segment,segment)
        self.assertEqual(self.tracker.live.rate(),0)
        self.assertAlmostEqual(self.tracker.live.snapshot()['observed_seconds'],.6)
        for width,row in self.tracker.windows.snapshot().items():
            self.assertAlmostEqual(row['observed_seconds'],before[width]['observed_seconds'])
        self.assertFalse(self.tracker.live.snapshot()['has_gaps'])
        self.feed(90,62,8,cap=None)
        self.assertAlmostEqual(self.tracker.windows.snapshot()['60']['value'],600/.9)
        self.assertFalse(self.tracker.windows.snapshot()['60']['has_gaps'])

    def test_upgrade_after_long_flash_does_not_inject_catchup_into_live(self):
        self.baseline()
        self.feed(50,None,4)
        self.feed(60,62,5,1200,dt=6)
        self.feed(70,62,6,1200)
        self.assertEqual(self.feed(80,62,7,1200),('auto_level',130))
        self.assertEqual(self.tracker.total,130)
        self.assertEqual(self.tracker.live.rate(),0)
        self.assertAlmostEqual(self.tracker.live.snapshot()['observed_seconds'],.6)

    def test_duplicate_and_reversed_frames_never_count_as_upgrade_votes(self):
        self.baseline()
        self.feed(50,62,4)
        before=(self.guard.count,self.guard.last_time,self.guard.wait_since)
        for timestamp in (self.now,self.now-.1,math.nan,math.inf):
            self.assertEqual(self.guard.feed(Reading(70,1200,6),62,observed_at=timestamp),('stale',0))
            self.assertEqual((self.guard.count,self.guard.last_time,self.guard.wait_since),before)
            self.assertEqual(self.tracker.total,0)
        self.assertEqual(self.feed(60,62,5,1200)[0],'level_pending')
        self.assertEqual(self.feed(70,62,6,1200),('auto_level',120))

    def test_capture_time_drives_votes_even_when_delivery_clock_is_unchanged(self):
        self.now=100
        for timestamp in (.3,.6,.9):
            self.guard.feed(Reading(950,1000,95),61,observed_at=timestamp)
        self.assertEqual(self.guard.level,61)
        self.assertAlmostEqual(self.guard.since,.3)
        self.assertAlmostEqual(self.tracker.live.last_observed_at,.9)
        for timestamp,value in ((1.2,50),(1.5,60),(1.8,70)):
            result=self.guard.feed(Reading(value,1200,6),62,observed_at=timestamp)
        self.assertEqual(result,('auto_level',120))
        self.assertAlmostEqual(self.tracker.live.last_observed_at,1.8)


if __name__=='__main__':unittest.main()
