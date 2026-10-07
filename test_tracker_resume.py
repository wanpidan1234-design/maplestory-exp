import copy
import json
import math
import unittest

from auto_level import AutoLevel
from tracker import Reading, Tracker, WINDOW_SECONDS


class ResumeTrackerTests(unittest.TestCase):
    def sampled(self, seconds=220, origin=10_000):
        now = [float(origin)]
        tracker = Tracker(lambda: now[0])
        tracker.feed(Reading(100, 10_000))
        tracker.start()
        for second in range(1, seconds + 1):
            now[0] = origin + second
            tracker.feed(Reading(100 + second * 3, 10_000))
        return tracker, now

    def test_json_roundtrip_preserves_all_windows_and_exponential_state(self):
        original, _ = self.sampled(1900)
        before = original.windows.snapshot()
        trend = original.live.snapshot()
        saved = json.loads(json.dumps(original.export_resume_state(), allow_nan=False))
        clock = [7.0]
        restored = Tracker(lambda: clock[0])
        self.assertTrue(restored.restore_resume_state(saved))
        self.assertTrue(restored.resume_pending)
        self.assertEqual(restored.total, original.total)
        self.assertEqual(restored.elapsed, original.elapsed)
        self.assertIsNone(restored.started)
        self.assertIsNone(restored.previous)
        self.assertIsNone(restored._last_input_at)
        self.assertEqual(restored.live.snapshot()['state'], 'unavailable')
        self.assertTrue(all(value['state'] == 'unavailable'
                            for value in restored.windows.snapshot().values()))
        display = restored.resume_rate_snapshot()
        self.assertEqual(display['windows'], before)
        self.assertAlmostEqual(display['value'], trend['value'])
        self.assertEqual(display['observed_seconds'], trend['observed_seconds'])
        self.assertEqual(display['segment'], trend['segment'] + 1)
        clock[0] += 60 * 60 * 24 * 30
        restored.start()
        self.assertEqual(restored.elapsed, original.elapsed)
        self.assertEqual(restored.resume_rate_snapshot(), display)
        self.assertEqual(restored.feed(Reading(5, 2000)), ('baseline', 0))
        self.assertFalse(restored.resume_pending)
        self.assertIsNone(restored.resume_rate_snapshot())
        self.assertEqual(restored.total, original.total)
        self.assertEqual(restored.elapsed, original.elapsed)
        self.assertEqual(restored.windows.snapshot(), before)
        self.assertAlmostEqual(restored.live.rate(), trend['value'])
        self.assertEqual(restored.previous, 5)
        restored.start()
        clock[0] += 1
        self.assertEqual(restored.feed(Reading(8, 2000)), ('ok', 3))
        self.assertEqual(restored.total, original.total + 3)
        self.assertEqual(restored.elapsed, original.elapsed + 1)
        self.assertAlmostEqual(restored.windows.snapshot()['1800']['value'], 180)

    def test_partial_progress_is_frozen_across_two_restarts_before_baseline(self):
        original, _ = self.sampled(220)
        saved = original.export_resume_state()
        clock = [1.0]
        first = Tracker(lambda: clock[0])
        self.assertTrue(first.restore_resume_state(saved))
        clock[0] = 900_000
        saved_again = first.export_resume_state()
        self.assertEqual(saved_again, saved)
        second = Tracker(lambda: -50.0)
        self.assertTrue(second.restore_resume_state(saved_again))
        self.assertEqual(second.resume_rate_snapshot(), first.resume_rate_snapshot())
        self.assertEqual(second.feed(Reading(999_999)), ('baseline', 0))
        self.assertEqual(second.total, 660)
        self.assertEqual(second.windows.snapshot()['300']['observed_seconds'], 220)
        self.assertEqual(second.windows.snapshot()['1800']['observed_seconds'], 220)
        self.assertEqual(second.windows.snapshot()['120']['state'], 'ready')
        self.assertEqual(second.windows.snapshot()['300']['state'], 'warming')

    def test_new_clock_smaller_than_saved_events_reanchors_every_time(self):
        original, _ = self.sampled(220)
        now = [0.0]
        resumed = Tracker(lambda: now[0])
        self.assertTrue(resumed.restore_resume_state(original.export_resume_state()))
        resumed.feed(Reading(10))
        self.assertEqual(resumed.live.last_observed_at, 0)
        self.assertEqual(resumed.windows.last_observed_at, 0)
        self.assertEqual(resumed.live.origin, -220)
        self.assertEqual(resumed.windows.origin, -220)
        self.assertEqual(resumed._last_input_at, 0)
        self.assertTrue(all(at <= 0 for at, _ in resumed.windows.events[1800]))
        self.assertEqual(resumed.feed(Reading(20), observed_at=-1), ('stale', 0))
        now[0] = 1
        resumed.feed(Reading(13))
        self.assertAlmostEqual(resumed.windows.snapshot()['60']['value'], 180)
        self.assertEqual(resumed.total, 663)

    def test_restart_baseline_does_not_count_offline_gain_or_new_level(self):
        for reading in (Reading(1, 20_000), Reading(2_000_000, 50_000_000)):
            with self.subTest(reading=reading):
                original, _ = self.sampled()
                original.confirmed_levels = 5
                original.level_gaps = 2
                original.level = 220
                original.estimated = True
                restored = Tracker(lambda: 0)
                self.assertTrue(restored.restore_resume_state(original.export_resume_state()))
                self.assertIsNone(restored.level)
                self.assertEqual(restored.confirmed_levels, 5)
                self.assertEqual(restored.level_gaps, 2)
                self.assertTrue(restored.estimated)
                self.assertEqual(restored.feed(reading), ('baseline', 0))
                self.assertFalse(restored.blocked)
                self.assertEqual(restored.total, original.total)
                self.assertEqual(restored.cap, reading.cap)

    def test_blocked_paused_reset_and_future_do_not_export_windows(self):
        for condition in ('blocked', 'paused', 'reset', 'future'):
            with self.subTest(condition=condition):
                source, clock = self.sampled()
                if condition == 'blocked':
                    source.blocked = True
                elif condition == 'paused':
                    source.pause()
                elif condition == 'reset':
                    source.reset_rates()
                else:
                    clock[0] -= .1
                saved = source.export_resume_state()
                self.assertIsNone(saved['rates'])
                restored = Tracker(lambda: 0)
                self.assertTrue(restored.restore_resume_state(saved))
                self.assertEqual(restored.total, source.total)
                self.assertIsNone(restored.resume_rate_snapshot())
                restored.feed(Reading(1))
                self.assertEqual(restored.windows.snapshot()['1800']['observed_seconds'], 0)

    def test_pause_reset_and_rebase_cancel_pending_continuation(self):
        source, _ = self.sampled()
        for action in ('pause', 'reset', 'rebase'):
            with self.subTest(action=action):
                restored = Tracker(lambda: 0)
                self.assertTrue(restored.restore_resume_state(source.export_resume_state()))
                if action == 'pause':
                    restored.pause()
                elif action == 'reset':
                    restored.reset_rates()
                else:
                    self.assertEqual(restored.feed(Reading(5), rebase=True), ('baseline', 0))
                self.assertFalse(restored.resume_pending)
                self.assertIsNone(restored.resume_rate_snapshot())
                self.assertTrue(all(value['observed_seconds'] == 0
                                    for value in restored.windows.snapshot().values()))
                self.assertEqual(restored.total, source.total)

    def test_resume_preserves_windows_across_another_long_gap(self):
        source, _ = self.sampled()
        now = [0.0]
        restored = Tracker(lambda: now[0])
        self.assertTrue(restored.restore_resume_state(source.export_resume_state()))
        restored.feed(Reading(10))
        restored.start()
        now[0] += 5.1
        self.assertEqual(restored.feed(Reading(20)), ('gap', 10))
        self.assertEqual(restored.total, source.total + 10)
        self.assertEqual(restored.windows.snapshot(), source.windows.snapshot())

    def test_window_boundary_and_zero_gains_survive_resume(self):
        now = [0.0]
        source = Tracker(lambda: now[0])
        source.feed(Reading(100))
        source.start()
        for second in range(1, 122):
            now[0] = second
            source.feed(Reading(160 if second >= 1 else 100))
        self.assertEqual(source.windows.snapshot()['120']['value'], 0)
        self.assertGreater(source.windows.snapshot()['300']['value'], 0)
        restored = Tracker(lambda: 0)
        self.assertTrue(restored.restore_resume_state(source.export_resume_state()))
        self.assertEqual(restored.resume_rate_snapshot()['windows'], source.windows.snapshot())
        restored.feed(Reading(8))
        self.assertEqual(restored.windows.snapshot(), source.windows.snapshot())

    def test_invalid_reading_does_not_consume_saved_window(self):
        source, _ = self.sampled()
        restored = Tracker(lambda: 0)
        self.assertTrue(restored.restore_resume_state(source.export_resume_state()))
        frozen = restored.export_resume_state()
        self.assertEqual(restored.feed(Reading(-1)), ('invalid', 0))
        self.assertTrue(restored.resume_pending)
        self.assertEqual(restored.feed(Reading(None)), ('need_cap', 0))
        self.assertEqual(restored.export_resume_state(), frozen)

    def test_empty_baseline_and_new_tracker_roundtrip(self):
        for with_baseline in (False, True):
            with self.subTest(with_baseline=with_baseline):
                source = Tracker(lambda: 0)
                if with_baseline:
                    source.feed(Reading(100))
                restored = Tracker(lambda: 1000)
                self.assertTrue(restored.restore_resume_state(source.export_resume_state()))
                self.assertEqual(restored.feed(Reading(1000)), ('baseline', 0))
                self.assertEqual(restored.total, 0)
                self.assertEqual(restored.elapsed, 0)

    def test_caller_mutation_cannot_change_saved_state(self):
        source, _ = self.sampled()
        saved = source.export_resume_state()
        restored = Tracker(lambda: 0)
        self.assertTrue(restored.restore_resume_state(saved))
        expected = restored.resume_rate_snapshot()
        saved['rates']['events'].clear()
        exported = restored.export_resume_state()
        exported['rates']['events'].clear()
        shown = restored.resume_rate_snapshot()
        shown['windows']['1800']['value'] = 999
        self.assertEqual(restored.resume_rate_snapshot(), expected)

    def test_legacy_v1_without_quality_fields_keeps_complete_thirty_minute_window(self):
        source,_=self.sampled(1900)
        saved=source.export_resume_state()
        del saved['rates']['gap_ages']
        del saved['rates']['trend_has_gaps']
        # Historical missing tails from the old implementation reset all rates,
        # so they do not make the currently saved segment incomplete.
        saved['confirmed_levels']=saved['level_gaps']=2
        target=Tracker(lambda:0)
        self.assertTrue(target.restore_resume_state(saved))
        self.assertEqual(target.total,source.total)
        self.assertEqual(target.elapsed,source.elapsed)
        self.assertEqual(target.resume_rate_snapshot()['windows'],source.windows.snapshot())
        self.assertEqual(target.feed(Reading(10)),('baseline',0))
        self.assertEqual(target.windows.snapshot(),source.windows.snapshot())
        self.assertFalse(target.live.snapshot()['has_gaps'])

    def test_upgrade_gap_at_zero_age_survives_restart_and_expires_by_observed_time(self):
        source,clock=self.sampled(1900)
        guard=AutoLevel(source,lambda:clock[0])
        guard.level=61
        source.level=61
        source.cap=None
        for current in (10,20,30):
            clock[0]+=.3
            result=guard.feed(Reading(current,None,1),62)
        self.assertEqual(result,('auto_level_gap',30))
        before=source.windows.snapshot()
        saved=json.loads(json.dumps(source.export_resume_state(),allow_nan=False))
        self.assertEqual(saved['rates']['gap_ages'],[0.0])
        self.assertTrue(saved['rates']['trend_has_gaps'])
        now=[0.0]
        target=Tracker(lambda:now[0])
        self.assertTrue(target.restore_resume_state(saved))
        self.assertEqual(target.resume_rate_snapshot()['windows'],before)
        self.assertEqual(target.resume_rate_snapshot()['quality'],'lower_bound')
        # Downtime must not expire the gap or count another upgrade.
        now[0]=1_000_000
        self.assertEqual(target.feed(Reading(100)),('baseline',0))
        self.assertEqual(target.windows.snapshot(),before)
        self.assertEqual(target.total,source.total)
        self.assertEqual(target.confirmed_levels,1)
        for elapsed in range(1,1801):
            now[0]+=1
            target.feed(Reading(100+elapsed))
            if elapsed in WINDOW_SECONDS:
                for width,row in target.windows.snapshot().items():
                    self.assertEqual(row['has_gaps'],int(width)>elapsed)
        self.assertFalse(target.windows.gaps)
        self.assertTrue(target.live.snapshot()['has_gaps'])
        # EWMA keeps a decaying contribution from the incomplete interval even
        # after the last finite window has expired its missing-tail marker.
        again=Tracker(lambda:0)
        self.assertTrue(again.restore_resume_state(target.export_resume_state()))
        self.assertTrue(again.resume_rate_snapshot()['has_gaps'])
        self.assertTrue(all(not row['has_gaps'] for row in again.resume_rate_snapshot()['windows'].values()))

    def test_corrupt_state_rejected_atomically(self):
        source, _ = self.sampled()
        valid = source.export_resume_state()
        corruptions = [None, {}, [], {**valid, 'version': 2}, {**valid, 'version': True},
                       {**valid, 'schema': 'other'}, {**valid, 'elapsed': math.nan},
                       {**valid, 'total': math.inf}, {**valid, 'total': -1},
                       {**valid, 'total': True}, {**valid, 'estimated': 1},
                       {**valid, 'confirmed_levels': .5}, {**valid, 'level_gaps': 1},
                       {**valid, 'extra': 'unexpected'}]
        for key, value in [('window_algorithm', 'unknown'), ('trend_algorithm', 'unknown'),
                           ('half_life_seconds', 5), ('max_gap_seconds', 6),
                           ('observed_seconds', -1), ('observed_seconds', math.inf),
                           ('segment', True), ('numerator', -1), ('weight', 2),
                           ('weight', 0), ('events', [[1800, 1]]),
                           ('events', [[221, 1]]), ('events', [[1, 1], [2, 1]]),
                           ('events', [[1, 1], [1, 1]]), ('events', [[0, 1000]]),
                           ('events', [[0, -1]]), ('events', [[0, math.nan]]),
                           ('events', [[0, True]]), ('events', [[0]]),
                           ('events', 'not-events'), ('gap_ages',None),
                           ('gap_ages',[math.nan]), ('gap_ages',[True]),
                           ('gap_ages',[1800]), ('gap_ages',[221]),
                           ('gap_ages',[1,2]), ('gap_ages',[1,1]),
                           ('gap_ages',[0]), ('trend_has_gaps',1)]:
            bad = copy.deepcopy(valid)
            bad['rates'][key] = value
            corruptions.append(bad)
        target, _ = self.sampled(5, origin=0)
        before = target.export_resume_state()
        previous = target.previous
        for corrupted in corruptions:
            with self.subTest(corrupted=corrupted):
                self.assertFalse(target.restore_resume_state(corrupted))
                self.assertEqual(target.export_resume_state(), before)
                self.assertEqual(target.previous, previous)
                self.assertFalse(target.resume_pending)


if __name__ == '__main__':
    unittest.main()
