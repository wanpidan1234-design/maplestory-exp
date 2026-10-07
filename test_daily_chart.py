import unittest

from ui_charts import daily_chart_rows


class DailyChartTests(unittest.TestCase):
    def test_recent_calendar_week_preserves_recorded_zero_and_missing_days(self):
        rows=daily_chart_rows([{'day':'2026-10-06','gain':234567},
                              {'day':'2026-10-04','gain':0},
                              {'day':'2026-09-30','gain':12345},
                              {'day':'2026-09-29','gain':999999}], '2026-10-06')
        self.assertEqual(len(rows),7)
        self.assertEqual([r['label'] for r in rows],['9/30','10/1','10/2','10/3','10/4','10/5','今天'])
        self.assertEqual([r['value'] for r in rows],[12345,0,0,0,0,0,234567])
        self.assertEqual([r['observed'] for r in rows],[True,False,False,False,True,False,True])
        self.assertEqual([r['current'] for r in rows],[False]*6+[True])

    def test_empty_history_does_not_claim_seven_zero_gain_days(self):
        rows=daily_chart_rows([], '2026-01-03')
        self.assertEqual(rows[0]['label'],'12/28')
        self.assertFalse(any(r['observed'] for r in rows))


if __name__=='__main__':
    unittest.main()
