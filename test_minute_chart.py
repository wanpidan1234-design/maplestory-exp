import unittest
from minute_chart import minute_buckets

class MinuteChartTests(unittest.TestCase):
    def test_natural_minute_boundary_and_total(self):
        rows=[dict(timestamp=60,gain=10),dict(timestamp=119.99,gain=20),dict(timestamp=120,gain=7)]
        result=minute_buckets(rows)
        self.assertEqual([r['gain'] for r in result],[30,7])
        self.assertEqual(sum(r['gain'] for r in result),37)

    def test_missing_minutes_are_not_recorded_zeroes(self):
        result=minute_buckets([dict(timestamp=60,gain=0),dict(timestamp=180,gain=5)])
        self.assertEqual([r['observed'] for r in result],[True,False,True])

    def test_aggregate_only_legacy_not_assigned_to_one_minute(self):
        result=minute_buckets([dict(timestamp=60,gain=900,status='legacy_summary'),dict(timestamp=65,gain=3,status='ok')])
        self.assertEqual(result[0]['gain'],3)
        self.assertEqual(minute_buckets([]),[])

if __name__=='__main__':
    unittest.main()
