import unittest
from tracker import Reading, Tracker, parse_exp


class TrackerTests(unittest.TestCase):
    def test_supported_readings(self):
        self.assertEqual(parse_exp('123,456 [12.34%]'), Reading(123456, None, 12.34))
        self.assertEqual(parse_exp('100 / 1000'), Reading(100, 1000))
        self.assertIsNone(parse_exp('Lv 200 123456 [12.34%]'))
        self.assertIsNone(parse_exp('101%'))

    def test_pause_excludes_time(self):
        now = [0]
        tracker = Tracker(lambda: now[0])
        tracker.feed(Reading(100))
        tracker.start()
        now[0] = 60
        tracker.feed(Reading(220))
        self.assertEqual(tracker.rates(), (120, 7200))
        tracker.pause()
        now[0] = 600
        self.assertEqual(tracker.elapsed, 60)

    def test_drop_requires_explicit_decision(self):
        tracker = Tracker()
        tracker.feed(Reading(950, 1000))
        self.assertEqual(tracker.feed(Reading(50, 1200))[0], 'decrease')
        self.assertEqual(tracker.total, 0)
        self.assertEqual(tracker.feed(Reading(70, 1200))[0], 'blocked')
        self.assertTrue(tracker.confirm_level(Reading(50, 1200)))
        self.assertEqual(tracker.total, 100)

    def test_unknown_level_cap_never_invented(self):
        tracker = Tracker()
        tracker.feed(Reading(950))
        tracker.feed(Reading(50))
        self.assertFalse(tracker.confirm_level(Reading(50)))
        tracker.feed(Reading(50), rebase=True)
        tracker.feed(Reading(70))
        self.assertEqual(tracker.total, 20)

    def test_level_event_only_after_successful_confirmation(self):
        tracker=Tracker()
        tracker.feed(Reading(950,1000))
        self.assertFalse(tracker.confirm_level(Reading(50,1200)))
        tracker.feed(Reading(50,1200))
        self.assertEqual(tracker.confirmed_levels,0)
        self.assertTrue(tracker.confirm_level(Reading(50,1200)))
        self.assertEqual(tracker.confirmed_levels,1)
        total=tracker.total
        self.assertFalse(tracker.confirm_level(Reading(50,1200)))
        self.assertEqual(tracker.total,total)
        self.assertEqual(tracker.confirmed_levels,1)
        tracker.feed(Reading(20),rebase=True)
        self.assertEqual(tracker.confirmed_levels,1)

    def test_percent_requires_cap_and_marks_estimate(self):
        tracker = Tracker()
        self.assertEqual(tracker.feed(Reading(None, None, 10))[0], 'need_cap')
        tracker.feed(Reading(None, None, 10), 10000)
        tracker.feed(Reading(None, None, 11), 10000)
        self.assertEqual(tracker.total, 100)
        self.assertTrue(tracker.estimated)


if __name__ == '__main__':
    unittest.main()
