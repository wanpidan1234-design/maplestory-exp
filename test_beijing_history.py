from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app_time import BEIJING,day_bounds,format_time,local_day,split_seconds
from history_store import HistoryStore
from history_reader import HistoryReader
from minute_chart import recorded_minute_curve
from ui_charts import reduce_plot_rows


class BeijingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'history.sqlite3'
        self.store=HistoryStore(self.path)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def append(self,stamp,gain,elapsed,total,status='ok'):
        self.store.append(['',elapsed,1000+total,gain,total,0,0,status,''],now=stamp,realtime=100,
            rate_metadata=dict(algorithm='time_ewma_v1',state='ready',half_life_seconds=30,
                observed_seconds=120,segment=1,window_algorithm='observed_window_v1',
                windows={str(s):dict(value=gain) for s in (60,120,300,600,1800)}))

    def test_timezone_does_not_follow_windows_and_midnight_split_is_exact(self):
        self.assertEqual(format_time(0),'08:00:00')
        start,end=day_bounds('2026-10-07')
        self.assertEqual(end-start,86400)
        self.assertEqual(local_day(start-1),'2026-10-06')
        self.assertEqual(local_day(start),'2026-10-07')
        self.assertEqual(split_seconds(start-5,start+5,10),[('2026-10-06',5),('2026-10-07',5)])

    def test_calendar_projection_keeps_original_rows_and_aggregate_time(self):
        boundary=day_bounds('2026-10-07')[0]
        session=self.store.begin(boundary-5)
        self.append(boundary-2,30,3,30)
        self.append(boundary+2,40,7,70)
        self.store.checkpoint(7,boundary+2)
        # Model a pre-timezone database without changing the original ledger on reopen.
        with self.store.db:
            self.store.db.execute('DROP TRIGGER beijing_sample_insert')
            self.store.db.execute('DELETE FROM history_calendar_meta')
            self.store.db.execute('DELETE FROM beijing_session_days')
            self.store.db.execute('DELETE FROM beijing_daily_time')
            self.store.db.execute("UPDATE samples SET day='2026-10-06'")
            self.store.db.execute('DELETE FROM daily_time')
            self.store.db.execute('INSERT INTO daily_time VALUES(?,?,?)',(session,'2026-10-06',7))
        original=[tuple(r) for r in self.store.db.execute('SELECT * FROM samples')]
        times=[tuple(r) for r in self.store.db.execute('SELECT * FROM daily_time')]
        self.store.db.close();self.store=HistoryStore(self.path)
        self.assertEqual(original,[tuple(r) for r in self.store.db.execute('SELECT * FROM samples')])
        self.assertEqual(times,[tuple(r) for r in self.store.db.execute('SELECT * FROM daily_time')])
        days={r['day']:r for r in self.store.days()}
        self.assertEqual([days[d]['gain'] for d in ('2026-10-06','2026-10-07')],[30,40])
        self.assertAlmostEqual(sum(r['seconds'] for r in days.values()),7)
        self.assertTrue(all(r['seconds_estimated'] for r in days.values()))
        self.assertEqual(len(self.store.samples('2026-10-07')),1)
        self.store.db.close();self.store=HistoryStore(self.path)
        self.assertEqual(sum(r['gain'] for r in self.store.days()),70)

    def test_day_curve_includes_all_sessions_and_zeroes_but_omits_missing_and_legacy(self):
        start=day_bounds('2026-10-07')[0]
        self.append(start+1,10,0,10)
        self.store.finish(0,start+2)
        self.append(start+20,5,0,5)
        self.append(start+60,0,0,5)
        self.append(start+180,9,0,14)
        self.append(start+240,100,0,114,'legacy_summary')
        minutes=self.store.day_minutes('2026-10-07')
        self.assertEqual([(r['timestamp']-start,r['gain']) for r in minutes],[(0,15),(60,0),(180,9)])
        curve=recorded_minute_curve(minutes)
        self.assertEqual([r['value'] for r in curve],[15,0,9])
        self.assertEqual(curve[0]['segment'],curve[1]['segment'])
        self.assertNotEqual(curve[1]['segment'],curve[2]['segment'])

    def test_reader_reuses_unchanged_rows_and_appends_new_samples_without_writes(self):
        start=day_bounds('2026-10-07')[0]
        self.append(start+1,10,0,10)
        reader=HistoryReader(self.path)
        first=reader.read('2026-10-07',None,60)
        repeated=reader.read('2026-10-07',None,60)
        self.assertIs(first['rows'],repeated['rows'])
        self.append(start+61,20,0,30)
        updated=reader.read('2026-10-07',None,60)
        self.assertEqual(len(first['rows']),1)
        self.assertEqual(len(updated['rows']),2)
        self.assertEqual(sum(row['gain'] for row in updated['rows']),30)
        self.assertEqual(reader.read('2026-10-06',None,60)['rows'],())

    def test_legacy_duration_projection_does_not_spread_time_over_offline_days(self):
        start=day_bounds('2026-10-01')[0]+100
        end=day_bounds('2026-10-04')[0]+100
        session=self.store.begin(start)
        self.append(start+5,10,5,10)
        self.append(end,0,6,10)
        self.store.checkpoint(6,end)
        with self.store.db:
            self.store.db.execute('DELETE FROM history_calendar_meta')
            self.store.db.execute('DELETE FROM beijing_session_days')
            self.store.db.execute('DELETE FROM beijing_daily_time')
        self.store.db.close();self.store=HistoryStore(self.path)
        times={r['day']:r['seconds'] for r in self.store.days() if r['seconds']}
        self.assertEqual(times,{'2026-10-04':1,'2026-10-01':5})

    def test_plot_reduction_preserves_spike_trough_and_long_gap(self):
        rows=[dict(timestamp=i,value=100,segment=1) for i in range(10000)]
        rows[501]['value']=100000
        rows[502]['value']=0
        rows.extend([dict(timestamp=20000,value=10,segment=1),dict(timestamp=20001,value=20,segment=1)])
        reduced=reduce_plot_rows(rows,buckets=100)
        self.assertLess(len(reduced),500)
        self.assertIn(100000,[r['value'] for r in reduced])
        self.assertIn(0,[r['value'] for r in reduced])
        self.assertNotEqual(reduced[-3]['segment'],reduced[-2]['segment'])


if __name__=='__main__':unittest.main()
