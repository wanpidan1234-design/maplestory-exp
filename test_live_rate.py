import math
import unittest

from tracker import ExponentialRate, Reading, Tracker


class ExponentialRateTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        # Formula checks retain the original explicit 10-second fixture.
        self.rate = ExponentialRate(lambda: self.now, half_life=10)

    def add(self, delta, seconds):
        self.now += seconds
        self.rate.add(delta, observed_at=self.now)

    def test_baseline_and_warmup_are_explicit(self):
        self.assertEqual(self.rate.snapshot()['state'], 'unavailable')
        self.assertIsNone(self.rate.rate())
        self.rate.reset(active=True, observed_at=0)
        self.assertEqual(self.rate.snapshot()['state'], 'warming')
        self.assertIsNone(self.rate.rate())
        self.add(5, 1)
        self.assertEqual(self.rate.snapshot()['state'], 'warming')
        self.assertAlmostEqual(self.rate.rate(), 300)
        for seconds in (2, 3, 4):
            self.add(5 * seconds, seconds)
        snapshot = self.rate.snapshot()
        self.assertEqual(snapshot['state'], 'ready')
        self.assertEqual(snapshot['observed_seconds'], 10)
        self.assertEqual(snapshot['half_life_seconds'], 10)
        self.assertEqual(snapshot['algorithm'], 'time_ewma_v1')

    def test_constant_rate_is_independent_of_sampling_intervals(self):
        self.rate.reset(active=True, observed_at=0)
        for seconds in (.01, .19, .8, 1.5, 4.5, .25, 2.75):
            self.add(seconds * 20, seconds)
            self.assertAlmostEqual(self.rate.rate(), 1200, places=9)

    def test_zero_observations_decay_by_elapsed_time(self):
        self.rate.reset(active=True, observed_at=0)
        for _ in range(20):
            self.add(100, 5)
        self.assertAlmostEqual(self.rate.rate(), 1200)
        for seconds in (1, 2, 3, 4):
            self.add(0, seconds)
        expected = 1200 * (1 - 2 ** -10) * 0.5 / (1 - 2 ** -11)
        self.assertAlmostEqual(self.rate.rate(), expected)

    def test_frequent_and_irregular_observations_share_time_response(self):
        def estimate(partitions):
            rate = ExponentialRate(lambda: 20, half_life=10)
            rate.reset(active=True, observed_at=0)
            for now in range(1, 11):
                rate.add(10, observed_at=now)
            now = 10
            for seconds in partitions:
                now += seconds
                rate.add(30 * seconds, observed_at=now)
            return rate.rate()
        self.assertAlmostEqual(estimate([1] * 10), estimate([.2, .8, 4, 2, 3]))
        # Ten seconds at 600/min then ten at 1800/min, with startup normalization.
        self.assertAlmostEqual(estimate([1] * 10), 1400)

    def test_display_refresh_neither_decays_nor_advances_observed_duration(self):
        self.rate.reset(active=True, observed_at=0)
        self.add(10, 1)
        before = self.rate.snapshot()
        self.now = 4
        self.assertEqual(self.rate.snapshot(), before)
        self.now = 7
        self.assertEqual(self.rate.snapshot()['state'], 'stale')
        self.assertIsNone(self.rate.rate())
        self.assertEqual(self.rate.snapshot()['observed_seconds'], 1)

    def test_missing_reading_does_not_become_zero_or_restart_short_segment(self):
        self.rate.reset(active=True, observed_at=0)
        self.add(10, 1)
        segment = self.rate.segment
        self.now = 2
        self.rate.invalidate()
        self.assertIsNone(self.rate.rate())
        self.assertEqual(self.rate.snapshot()['state'], 'unavailable')
        self.add(20, 1)
        self.assertEqual(self.rate.segment, segment)
        self.assertEqual(self.rate.snapshot()['observed_seconds'], 3)
        self.assertAlmostEqual(self.rate.rate(), 600)

    def test_long_gap_restarts_without_counting_recovery_gain(self):
        self.rate.reset(active=True, observed_at=0)
        self.add(10, 1)
        segment = self.rate.segment
        self.add(5000, 6)
        self.assertNotEqual(self.rate.segment, segment)
        self.assertEqual(self.rate.snapshot()['state'], 'warming')
        self.assertEqual(self.rate.snapshot()['observed_seconds'], 0)
        self.assertIsNone(self.rate.rate())
        self.add(10, 1)
        self.assertAlmostEqual(self.rate.rate(), 600)

    def test_gap_limit_includes_exactly_five_seconds(self):
        self.rate.reset(active=True, observed_at=0)
        segment = self.rate.segment
        self.add(50, 5)
        self.assertEqual(self.rate.segment, segment)
        self.assertAlmostEqual(self.rate.rate(), 600)

    def test_duplicate_reverse_nonfinite_and_negative_inputs_are_noops(self):
        self.rate.reset(active=True, observed_at=0)
        self.add(10, 1)
        before = self.rate.snapshot()
        for delta, timestamp in ((999, 1), (999, 0), (999, math.nan),
                                 (999, math.inf), (-1, 2), (math.inf, 2)):
            self.rate.add(delta, observed_at=timestamp)
            self.assertEqual(self.rate.snapshot(), before)

    def test_tiny_intervals_keep_nonzero_weight(self):
        self.rate.reset(active=True, observed_at=0)
        self.add(1e-15, 1e-15)
        self.assertAlmostEqual(self.rate.rate(), 60)

    def test_parameters_require_positive_finite_values(self):
        for invalid in (0, -1, math.nan, math.inf):
            with self.assertRaises(ValueError):
                ExponentialRate(half_life=invalid)
            with self.assertRaises(ValueError):
                ExponentialRate(max_gap=invalid)

    def test_default_trend_uses_thirty_seconds_without_changing_explicit_old_parameters(self):
        now = [0.0]
        rate = Tracker(lambda: now[0]).live
        self.assertEqual(rate.half_life, 30)
        rate.reset(active=True, observed_at=0)
        for second in range(1, 31):
            now[0] = second
            rate.add(10, observed_at=second)
            self.assertEqual(rate.snapshot()['state'], 'ready' if second == 30 else 'warming')
        self.assertEqual(rate.snapshot()['half_life_seconds'], 30)
        self.assertAlmostEqual(rate.rate(), 600)
        self.assertEqual(self.rate.half_life, 10)


class TrackerLiveRateTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.tracker = Tracker(lambda: self.now)

    def feed(self, current, timestamp, **kwargs):
        self.now = timestamp
        return self.tracker.feed(Reading(current, 1000), observed_at=timestamp, **kwargs)

    def test_long_gap_counts_total_but_never_injects_catchup_into_live(self):
        self.feed(100, 0)
        self.feed(110, 1)
        self.assertEqual(self.feed(500, 10), ('gap', 390))
        self.assertEqual(self.tracker.total, 400)
        self.assertAlmostEqual(self.tracker.live.rate(), 600)
        self.assertEqual(self.tracker.rolling.rate(), 600)
        self.feed(510, 11)
        self.assertAlmostEqual(self.tracker.live.rate(), 600)
        self.assertEqual(self.tracker.total, 410)

    def test_stale_timestamps_do_not_change_baseline_or_total(self):
        self.feed(100, 0)
        self.feed(110, 1)
        for timestamp in (1, .5, math.nan, math.inf):
            self.assertEqual(self.tracker.feed(Reading(900), observed_at=timestamp), ('stale', 0))
            self.assertEqual(self.tracker.previous, 110)
            self.assertEqual(self.tracker.total, 10)
        self.assertEqual(self.feed(120, 2), ('ok', 10))
        self.assertAlmostEqual(self.tracker.live.rate(), 600)

    def test_pause_and_manual_upgrade_never_create_live_spike(self):
        self.feed(950, 0)
        self.tracker.start()
        self.assertEqual(self.feed(50, 1), ('decrease', 0))
        self.tracker.pause()
        self.now = 1.001
        self.assertTrue(self.tracker.confirm_level(Reading(50, 1200), observed_at=self.now))
        self.assertEqual(self.tracker.total, 100)
        self.assertIsNone(self.tracker.live.rate())
        self.assertEqual(self.tracker.rolling.rate(), 0)
        self.now = 2
        self.tracker.start()
        self.assertEqual(self.feed(60, 2), ('baseline', 0))
        self.assertIsNone(self.tracker.live.rate())
        self.feed(70, 3)
        self.assertAlmostEqual(self.tracker.live.rate(), 600)
        self.assertEqual(self.tracker.total, 110)

    def test_paused_rebase_does_not_start_a_segment(self):
        self.feed(100, 0)
        self.tracker.pause()
        self.feed(200, 1, rebase=True)
        self.assertIsNone(self.tracker.live.rate())
        self.assertEqual(self.tracker.live.snapshot()['state'], 'unavailable')
        self.now = 2
        self.tracker.start()
        self.assertEqual(self.feed(250, 2), ('baseline', 0))
        self.assertEqual(self.tracker.total, 0)

    def test_explicit_capture_timestamp_controls_rate_not_delivery_time(self):
        self.now = 100
        self.tracker.feed(Reading(100), observed_at=98)
        self.now = 101
        self.tracker.feed(Reading(130), observed_at=99)
        self.assertAlmostEqual(self.tracker.live.rate(), 1800)
        self.assertEqual(self.tracker.live.snapshot()['observed_seconds'], 1)


if __name__ == '__main__':
    unittest.main()
