import math
import unittest

from auto_level import AutoLevel
from tracker import MultiWindowRates, Reading, Tracker, WINDOW_ALGORITHM, WINDOW_SECONDS


class MultiWindowRateTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.windows = MultiWindowRates(lambda: self.now)

    def test_inactive_and_first_observation_have_no_rate(self):
        self.assertEqual(WINDOW_SECONDS, (60, 120, 300, 600, 1800))
        self.assertEqual(WINDOW_ALGORITHM, 'observed_window_v1')
        snapshot = self.windows.snapshot()
        self.assertEqual(tuple(snapshot), tuple(map(str, WINDOW_SECONDS)))
        for row in snapshot.values():
            self.assertEqual(row['state'], 'unavailable')
            self.assertIsNone(row['value'])
        self.windows.add(999, observed_at=10)
        for seconds, row in self.windows.snapshot().items():
            self.assertEqual(row['window_seconds'], int(seconds))
            self.assertEqual(row['observed_seconds'], 0)
            self.assertEqual(row['state'], 'warming')
            self.assertIsNone(row['value'])

    def test_thirty_seconds_have_same_value_and_warmup_in_every_window(self):
        self.windows.reset(active=True, observed_at=100)
        for seconds in range(1, 31):
            self.windows.add(20, observed_at=100 + seconds)
        for row in self.windows.snapshot().values():
            self.assertEqual(row['value'], 1200)
            self.assertEqual(row['observed_seconds'], 30)
            self.assertEqual(row['state'], 'warming')

    def test_constant_rate_at_thirty_minutes_makes_all_windows_ready(self):
        self.windows.reset(active=True, observed_at=0)
        for seconds in range(1, 1801):
            self.windows.add(10, observed_at=seconds)
        for row in self.windows.snapshot().values():
            self.assertEqual(row['value'], 600)
            self.assertEqual(row['observed_seconds'], row['window_seconds'])
            self.assertEqual(row['state'], 'ready')

    def test_left_boundary_is_excluded_for_each_window(self):
        for seconds in WINDOW_SECONDS:
            with self.subTest(window=seconds):
                self.windows.reset(active=True, observed_at=0)
                self.windows.add(7, observed_at=1)
                self.windows.add(11, observed_at=seconds)
                self.assertEqual(self.windows.snapshot()[str(seconds)]['value'], 18 * 60 / seconds)
                self.windows.add(13, observed_at=seconds + 1)
                self.assertEqual(self.windows.snapshot()[str(seconds)]['value'], 24 * 60 / seconds)

    def test_confirmed_zeros_expire_the_same_gain_at_each_window_boundary(self):
        self.windows.reset(active=True, observed_at=0)
        self.windows.add(10, observed_at=1)
        for completed in WINDOW_SECONDS:
            endpoint = completed + 1
            self.windows.add(0, observed_at=endpoint)
            snapshot = self.windows.snapshot()
            for seconds in WINDOW_SECONDS:
                row = snapshot[str(seconds)]
                if seconds <= completed:
                    self.assertEqual(row['value'], 0)
                    self.assertEqual(row['state'], 'ready')
                else:
                    self.assertEqual(row['value'], 600 / endpoint)
                    self.assertEqual(row['state'], 'warming')

    def test_clock_and_query_order_cannot_advance_or_mutate_windows(self):
        self.windows.reset(active=True, observed_at=0)
        self.windows.add(10, observed_at=1)
        expected = self.windows.snapshot()
        self.now = 10000
        for seconds in reversed(WINDOW_SECONDS):
            self.assertEqual(self.windows.snapshot()[str(seconds)], expected[str(seconds)])
        self.assertEqual(self.windows.snapshot(), expected)
        # The view returned to callers owns no mutable filter state.
        result = self.windows.snapshot()
        result['60']['value'] = -999
        self.assertEqual(self.windows.snapshot(), expected)

    def test_short_window_expiration_preserves_long_window_history(self):
        self.windows.reset(active=True, observed_at=0)
        self.windows.add(60, observed_at=1)
        self.windows.add(0, observed_at=61)
        self.assertEqual(self.windows.snapshot()['60']['value'], 0)
        self.assertEqual(self.windows.snapshot()['1800']['value'], 3600 / 61)
        self.windows.add(60, observed_at=62)
        self.assertEqual(self.windows.snapshot()['60']['value'], 60)
        self.assertEqual(self.windows.snapshot()['1800']['value'], 7200 / 62)

    def test_invalid_delta_or_timestamp_does_not_change_any_window(self):
        self.windows.reset(active=True, observed_at=0)
        self.windows.add(10, observed_at=1)
        before = self.windows.snapshot()
        for delta, timestamp in ((999, 1), (999, .5), (999, math.nan),
                                 (999, math.inf), (math.nan, 2), (math.inf, 2), (-1, 2)):
            self.windows.add(delta, observed_at=timestamp, missing_gain=True)
            self.assertEqual(self.windows.snapshot(), before)
            self.assertEqual(self.windows.last_observed_at, 1)
        self.windows.reset(active=True, observed_at=math.nan)
        self.assertEqual(self.windows.snapshot(), before)

    def test_missing_gain_marks_even_zero_and_expires_at_each_left_boundary(self):
        self.windows.reset(active=True, observed_at=0)
        self.windows.add(0, observed_at=1, missing_gain=True)
        for row in self.windows.snapshot().values():
            self.assertEqual(row['value'],0)
            self.assertTrue(row['has_gaps'])
            self.assertEqual(row['quality'],'lower_bound')
        for seconds in WINDOW_SECONDS:
            self.windows.add(0, observed_at=seconds+1)
            for width, row in self.windows.snapshot().items():
                expected=int(width)>seconds
                self.assertEqual(row['has_gaps'],expected)
                self.assertEqual(row['quality'],'lower_bound' if expected else 'complete')
        self.assertFalse(self.windows.gaps)

    def test_quality_uses_latest_gap_and_reset_clears_it(self):
        self.windows.reset(active=True, observed_at=0)
        self.windows.add(10, observed_at=1, missing_gain=True)
        self.windows.add(10, observed_at=30, missing_gain=True)
        self.windows.add(0, observed_at=61)
        self.assertTrue(self.windows.snapshot()['60']['has_gaps'])
        self.windows.add(0, observed_at=90)
        self.assertFalse(self.windows.snapshot()['60']['has_gaps'])
        self.assertTrue(self.windows.snapshot()['120']['has_gaps'])
        self.windows.reset(active=True, observed_at=91)
        self.assertFalse(self.windows.gaps)
        self.assertTrue(all(not row['has_gaps'] for row in self.windows.snapshot().values()))

    def test_running_sums_match_direct_reference_on_irregular_observations(self):
        self.windows.reset(active=True, observed_at=20)
        events = []
        endpoint = 20
        for index in range(700):
            endpoint += (.25, 1, 4.5, 3, .75)[index % 5]
            delta = (index * 31) % 101
            events.append((endpoint, delta))
            self.windows.add(delta, observed_at=endpoint)
            if index % 49 == 0:
                for seconds, row in self.windows.snapshot().items():
                    width = int(seconds)
                    expected = 60 * sum(value for timestamp, value in events if endpoint - width < timestamp <= endpoint)
                    expected /= min(width, endpoint - 20)
                    self.assertEqual(row['value'], expected)

    def test_event_caches_remain_bounded_by_their_own_window(self):
        self.windows.reset(active=True, observed_at=0)
        for seconds in range(1, 5401):
            self.windows.add(10, observed_at=seconds)
        for seconds in WINDOW_SECONDS:
            self.assertEqual(len(self.windows.events[seconds]), seconds)
            self.assertEqual(self.windows.running_sum[seconds], seconds * 10)
            self.assertGreater(self.windows.events[seconds][0][0], 5400 - seconds)
        self.windows.add(0, observed_at=7200)
        for seconds in WINDOW_SECONDS:
            self.assertEqual(len(self.windows.events[seconds]), 0)
            self.assertEqual(self.windows.running_sum[seconds], 0)


class TrackerWindowRateTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.tracker = Tracker(lambda: self.now)

    def feed(self, current, timestamp, cap=1000, **kwargs):
        self.now = timestamp
        return self.tracker.feed(Reading(current, cap), observed_at=timestamp, **kwargs)

    def assert_empty_windows(self, state):
        for row in self.tracker.windows.snapshot().values():
            self.assertIsNone(row['value'])
            self.assertEqual(row['observed_seconds'], 0)
            self.assertEqual(row['state'], state)

    def test_public_reset_uses_one_time_for_every_filter(self):
        times = iter((10, 20, 30))
        tracker = Tracker(lambda: next(times))
        tracker.reset_rates(active=True)
        self.assertEqual(tracker.live.origin, 10)
        self.assertEqual(tracker.rolling.origin, 10)
        self.assertEqual(tracker.windows.origin, 10)
        self.assertEqual(next(times), 20)

    def test_gap_only_adds_to_ledger_and_preserves_every_window(self):
        self.feed(100, 0)
        self.feed(110, 1)
        self.assertEqual(self.feed(500, 10), ('gap', 390))
        self.assertEqual(self.tracker.total, 400)
        self.assertEqual(self.tracker.windows.snapshot()['1800']['observed_seconds'], 1)
        self.assertEqual(self.tracker.live.origin, 9)
        self.assertEqual(self.tracker.rolling.origin, 9)
        self.assertEqual(self.tracker.windows.origin, 9)
        self.feed(510, 11)
        for row in self.tracker.windows.snapshot().values():
            self.assertEqual(row['value'], 600)
            self.assertEqual(row['observed_seconds'], 2)

    def test_short_invalid_reading_keeps_every_window_until_trusted_recovery(self):
        self.feed(100, 0)
        self.feed(110, 1)
        before = self.tracker.windows.snapshot()
        self.now = 2
        self.tracker.live.invalidate()
        self.assertEqual(self.tracker.windows.snapshot(), before)
        self.feed(130, 3)
        for row in self.tracker.windows.snapshot().values():
            self.assertEqual(row['value'], 600)
            self.assertEqual(row['observed_seconds'], 3)

    def test_paused_manual_confirmation_and_resume_cannot_pollute_windows(self):
        self.feed(950, 0)
        self.feed(50, 1)
        self.tracker.pause()
        self.assert_empty_windows('unavailable')
        self.now = 1.001
        self.assertTrue(self.tracker.confirm_level(Reading(50, 1200), observed_at=self.now))
        self.assertEqual(self.tracker.total, 100)
        self.assert_empty_windows('unavailable')
        self.tracker.start()
        self.assertEqual(self.feed(60, 2, cap=1200), ('baseline', 0))
        self.assert_empty_windows('warming')
        self.feed(70, 3, cap=1200)
        self.assertEqual(self.tracker.windows.snapshot()['60']['value'], 600)

    def test_paused_rebase_preserves_first_resume_frame_as_baseline(self):
        self.feed(100, 0)
        self.tracker.pause()
        self.feed(200, 1, rebase=True)
        self.assert_empty_windows('unavailable')
        self.tracker.start()
        self.assertEqual(self.feed(300, 2), ('baseline', 0))
        self.assert_empty_windows('warming')
        self.assertEqual(self.tracker.total, 0)

    def test_delivery_clock_and_stale_frames_do_not_change_window_time(self):
        self.now = 100
        self.tracker.feed(Reading(100), observed_at=98)
        self.now = 101
        self.tracker.feed(Reading(130), observed_at=99)
        before = self.tracker.windows.snapshot()
        for row in before.values():
            self.assertEqual(row['value'], 1800)
            self.assertEqual(row['observed_seconds'], 1)
        for timestamp in (99, 98, math.nan, math.inf):
            self.assertEqual(self.tracker.feed(Reading(900), observed_at=timestamp), ('stale', 0))
            self.assertEqual(self.tracker.windows.snapshot(), before)

    def test_confirmed_upgrade_is_counted_once_in_all_windows(self):
        guard = AutoLevel(self.tracker, lambda: self.now)
        for timestamp in (.3, .6, .9):
            self.now = timestamp
            guard.feed(Reading(950, 1000, 95), 61, observed_at=timestamp)
        for timestamp, current in ((1.2, 50), (1.5, 60), (1.8, 70)):
            self.now = timestamp
            result = guard.feed(Reading(current, 1200, 6), 62, observed_at=timestamp)
        self.assertEqual(result, ('auto_level', 120))
        for row in self.tracker.windows.snapshot().values():
            self.assertAlmostEqual(row['value'], 4800)
            self.assertAlmostEqual(row['observed_seconds'], 1.5)
        self.assertEqual(self.tracker.total, 120)

    def test_unknown_tail_preserves_all_windows_with_explicit_missing_gain(self):
        guard = AutoLevel(self.tracker, lambda: self.now)
        for timestamp in (.3, .6, .9):
            self.now = timestamp
            guard.feed(Reading(950, None, 95), 61, observed_at=timestamp)
        for timestamp, current in ((1.2, 50), (1.5, 60), (1.8, 70)):
            self.now = timestamp
            result = guard.feed(Reading(current, 1200, 6), 62, observed_at=timestamp)
        self.assertEqual(result, ('auto_level_gap', 70))
        self.assertEqual(self.tracker.total, 70)
        self.assertEqual(self.tracker.level_gaps, 1)
        for row in self.tracker.windows.snapshot().values():
            self.assertAlmostEqual(row['value'],2800)
            self.assertAlmostEqual(row['observed_seconds'],1.5)
            self.assertEqual(row['state'],'warming')
            self.assertTrue(row['has_gaps'])
            self.assertEqual(row['quality'],'lower_bound')
        self.assertEqual(self.tracker.windows.origin, self.tracker.live.origin)
        self.assertEqual(self.tracker.windows.origin, self.tracker.rolling.origin)

    def test_unknown_cap_upgrade_preserves_completed_thirty_minute_window(self):
        guard=AutoLevel(self.tracker,lambda:self.now)
        for second in range(1801):
            self.now=float(second)
            guard.feed(Reading(100+second,None,95),61)
        origin=self.tracker.windows.origin
        segment=self.tracker.live.segment
        before=list(self.tracker.windows.events[1800])
        for at,current in ((1800.3,20),(1800.6,30),(1800.9,40)):
            self.now=at
            result=guard.feed(Reading(current,None,4),62)
        self.assertEqual(result,('auto_level_gap',40))
        self.assertEqual(self.tracker.total,1840)
        self.assertEqual(self.tracker.windows.origin,origin)
        self.assertEqual(self.tracker.live.segment,segment)
        row=self.tracker.windows.snapshot()['1800']
        self.assertEqual(row['observed_seconds'],1800)
        self.assertEqual(row['state'],'ready')
        self.assertTrue(row['has_gaps'])
        self.assertEqual(row['quality'],'lower_bound')
        self.assertEqual(list(self.tracker.windows.events[1800]),before+[(1800.9,40)])
        self.assertAlmostEqual(row['value'],1840*60/1800)
        self.now=1801.9
        guard.feed(Reading(50,None,5),62)
        self.assertEqual(self.tracker.total,1850)
        self.assertEqual(self.tracker.confirmed_levels,1)

    def test_unqueried_legacy_counter_remains_bounded(self):
        self.feed(0, 0, cap=None)
        for second in range(1, 7201):
            self.feed(second, second, cap=None)
        self.assertEqual(len(self.tracker.rolling.events), 60)
        self.assertEqual(len(self.tracker.windows.events[1800]), 1800)
        self.assertEqual(self.tracker.rolling.rate(), 60)


if __name__ == '__main__':
    unittest.main()
