import csv
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from app_time import BEIJING, format_time, local_day
from pathlib import Path
from unittest.mock import Mock,patch
from tracker import RollingRate
from history_store import HistoryStore,split_seconds
from history_view import HistoryWindow
from ui_charts import DEFAULT_RATE_VIEW,RATE_VIEWS,WINDOW_ALGORITHM,plot,rate_label,rate_plot_rows,window_sample
from ui_theme import ACCENT

class RollingTests(unittest.TestCase):
    def test_startup_and_trailing_window_differ_from_all_time(self):
        now=[0]
        r=RollingRate(lambda:now[0]);r.reset(True)
        now[0]=10;r.add(100)
        self.assertEqual(r.rate(),600)
        now[0]=60;r.add(300)
        self.assertEqual(r.rate(),400)
        now[0]=71;r.add(50)
        self.assertEqual(r.rate(),350)
        self.assertNotEqual(r.rate(),450*60/71)
        now[0]=132
        self.assertEqual(r.rate(),0)

    def test_pause_resume_does_not_reuse_old_minute(self):
        now=[0];r=RollingRate(lambda:now[0]);r.reset(True)
        now[0]=10;r.add(100);r.reset()
        self.assertEqual(r.rate(),0)
        now[0]=200;r.reset(True)
        now[0]=210;r.add(50)
        self.assertEqual(r.rate(),300)

    def test_exact_sixty_second_boundary(self):
        now=[0];r=RollingRate(lambda:now[0]);r.reset(True)
        now[0]=1;r.add(20)
        now[0]=60;self.assertEqual(r.rate(),20)
        now[0]=61;self.assertEqual(r.rate(),0)

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'history.sqlite3'
        self.store=HistoryStore(self.path)

    def tearDown(self):
        self.store.db.close();self.temp.cleanup()

    def row(self,gain,total,elapsed,current=1000):
        return ['',elapsed,current,gain,total,0,0,'ok','']

    def test_midnight_duration_and_gain_assignment(self):
        start=datetime(2026,9,27,23,59,50).replace(tzinfo=BEIJING).timestamp()
        end=datetime(2026,9,28,0,0,10).replace(tzinfo=BEIJING).timestamp()
        self.store.begin(start)
        self.store.append(self.row(100,100,5),now=start+5)
        self.store.append(self.row(200,300,20),now=end)
        self.store.checkpoint(20,end)
        rows={r['day']:r for r in self.store.days()}
        self.assertEqual(rows['2026-09-27']['gain'],100)
        self.assertEqual(rows['2026-09-28']['gain'],200)
        self.assertAlmostEqual(rows['2026-09-27']['seconds'],10)
        self.assertAlmostEqual(rows['2026-09-28']['seconds'],10)

    def test_committed_samples_survive_reopen_without_finish(self):
        now=datetime(2026,9,27,12).replace(tzinfo=BEIJING).timestamp()
        self.store.append(self.row(123,123,1),now=now)
        self.store.db.close()
        self.store=HistoryStore(self.path)
        self.assertEqual(self.store.days()[0]['gain'],123)

    def test_original_schema_migrates_without_rewriting_rates(self):
        path=Path(self.temp.name)/'old-schema.sqlite3'
        with closing(sqlite3.connect(path)) as db,db:
            db.execute('''CREATE TABLE samples(
                id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,seq INTEGER NOT NULL,
                timestamp REAL NOT NULL,day TEXT NOT NULL,current_exp INTEGER,gain INTEGER NOT NULL,
                total INTEGER NOT NULL,elapsed REAL NOT NULL,realtime REAL,average REAL,hourly REAL,
                status TEXT,raw TEXT,percent REAL,monitor TEXT,UNIQUE(session_id,seq))''')
            db.execute("INSERT INTO samples VALUES(1,'old',1,100,'2026-09-27',1400,400,400,1,24000,400,24000,'ok','original',NULL,'monitor')")
        for _ in range(2):
            store=HistoryStore(path)
            try:
                rows=store.samples(local_day(100))
                self.assertEqual(len(rows),1)
                row=rows[0]
                self.assertEqual((row['realtime'],row['gain'],row['total'],row['raw']),(24000,400,400,'original'))
                self.assertEqual(row['rate_algorithm'],'rolling60_v1')
                self.assertIsNone(row['rate_state'])
                self.assertIsNone(row['rate_half_life'])
                self.assertIsNone(row['rolling60'])
                self.assertIsNone(row['rolling120'])
                self.assertIsNone(row['window_algorithm'])
                self.assertEqual(row['rate_has_gaps'],0)
                self.assertEqual(row['window_gap_mask'],0)
                columns=[r['name'] for r in store.db.execute('PRAGMA table_info(samples)')]
                self.assertEqual(columns.count('rate_algorithm'),1)
            finally:
                store.db.close()

    def test_ewma_schema_migrates_without_backfilling_windows(self):
        path=Path(self.temp.name)/'ewma-schema.sqlite3'
        with closing(sqlite3.connect(path)) as db,db:
            db.execute('''CREATE TABLE samples(
                id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,seq INTEGER NOT NULL,
                timestamp REAL NOT NULL,day TEXT NOT NULL,current_exp INTEGER,gain INTEGER NOT NULL,
                total INTEGER NOT NULL,elapsed REAL NOT NULL,realtime REAL,average REAL,hourly REAL,
                status TEXT,raw TEXT,percent REAL,monitor TEXT,
                rate_algorithm TEXT NOT NULL DEFAULT 'rolling60_v1',rate_state TEXT,rate_half_life REAL,
                rate_observed_seconds REAL,rate_segment INTEGER,rolling60 REAL,UNIQUE(session_id,seq))''')
            db.execute("INSERT INTO samples VALUES(1,'old',1,100,'2026-09-27',1400,400,400,1,24000,400,24000,'ok','original',NULL,'monitor','time_ewma_v1','ready',10,500,2,321)")
        for _ in range(2):
            store=HistoryStore(path)
            try:
                row=store.samples(local_day(100))[0]
                self.assertEqual((row['realtime'],row['rolling60'],row['rate_observed_seconds']),(24000,321,500))
                self.assertIsNone(row['window_algorithm'])
                self.assertTrue(all(row[f'rolling{seconds}'] is None for seconds in (120,300,600,1800)))
                self.assertEqual(window_sample(row,60),dict(value=321,observed_seconds=None,state='legacy',has_gaps=False,quality='complete'))
                self.assertEqual(row['rate_has_gaps'],0)
                self.assertEqual(row['window_gap_mask'],0)
            finally:
                store.db.close()

    def test_nullable_rate_and_metadata_round_trip(self):
        now=datetime(2026,9,27,12).replace(tzinfo=BEIJING).timestamp()
        metadata=dict(value=None,state='unavailable',algorithm='time_ewma_v1',
                      half_life_seconds=10,observed_seconds=0,segment=2,rolling60=None)
        self.store.append(self.row(500,500,20),None,12.5,'display',now,rate_metadata=metadata)
        metadata.update(value=0,state='warming',observed_seconds=2.5,rolling60=0)
        self.store.append(self.row(0,500,22.5),0,now=now+2.5,rate_metadata=metadata)
        missing,zero=self.store.samples('2026-09-27')
        self.assertIsNone(missing['realtime'])
        self.assertEqual(missing['gain'],500)
        self.assertIsNone(missing['rolling60'])
        self.assertEqual(missing['rate_algorithm'],'time_ewma_v1')
        self.assertEqual(missing['rate_half_life'],10)
        self.assertEqual(missing['rate_segment'],2)
        self.assertEqual(missing['rate_state'],'unavailable')
        self.assertEqual(zero['realtime'],0)
        self.assertEqual(zero['rolling60'],0)
        self.assertEqual(zero['rate_observed_seconds'],2.5)
        self.assertEqual(zero['rate_state'],'warming')

    def test_old_append_call_keeps_original_algorithm(self):
        now=datetime(2026,9,27,12).replace(tzinfo=BEIJING).timestamp()
        self.store.append(self.row(100,100,10),600,1.0,'display',now)
        row=self.store.samples('2026-09-27')[0]
        self.assertEqual(row['realtime'],600)
        self.assertEqual(row['rate_algorithm'],'rolling60_v1')
        self.assertIsNone(row['rate_half_life'])

    def test_saved_half_lives_keep_values_labels_and_export_metadata(self):
        now=datetime(2026,9,27,12).replace(tzinfo=BEIJING).timestamp()
        for index,half_life in enumerate((10,30)):
            self.store.append(self.row(10,10*(index+1),60+index),600+index,now=now+index,
                rate_metadata=dict(algorithm='time_ewma_v1',state='ready',
                    half_life_seconds=half_life,observed_seconds=60+index,segment=1,
                    window_algorithm=WINDOW_ALGORITHM,windows={'60':dict(value=100+index)}))
        rows=self.store.samples('2026-09-27')
        self.assertEqual([row['realtime'] for row in rows],[600,601])
        self.assertEqual([row['rolling60'] for row in rows],[100,101])
        self.assertEqual([row['rate_half_life'] for row in rows],[10,30])
        for row,half_life in zip(rows,(10,30)):
            self.assertIn(f'半衰期 {half_life} 秒',rate_label(row))
            self.assertIn(f'{half_life}秒',rate_label(row,compact=True))
        window=HistoryWindow.__new__(HistoryWindow)
        window.day='2026-09-27';window.store=self.store;window.window=None
        path=Path(self.temp.name)/'half-lives.csv'
        with patch('history_view.filedialog.asksaveasfilename',return_value=str(path)):
            window.export()
        with path.open(encoding='utf-8-sig',newline='') as file:
            header,*exported=csv.reader(file)
        self.assertEqual([row[11] for row in exported],['10.0','30.0'])
        self.assertEqual([row[4] for row in exported],['600.0','601.0'])
        self.assertEqual([row[14] for row in exported],['100.0','101.0'])
        self.assertTrue(all(row[10]=='time_ewma_v1' and row[20]==WINDOW_ALGORITHM for row in exported))

    def test_default_history_window_does_not_substitute_legacy_trend(self):
        now=datetime(2026,9,27,12).replace(tzinfo=BEIJING).timestamp()
        self.store.append(self.row(100,100,10),600,now=now)
        window=HistoryWindow.__new__(HistoryWindow)
        window.rows=self.store.samples('2026-09-27')
        window.rate_view=Mock();window.timeline=Mock();window.timeline.get_children.return_value=[]
        window.rate_hint=Mock()
        window.draw_chart=Mock()
        window.rate_view.get.return_value=DEFAULT_RATE_VIEW
        window.select_rate_view()
        values=window.timeline.insert.call_args.kwargs['values']
        self.assertEqual(values[2],'—')
        self.assertEqual(values[4],'旧记录无此窗口')
        window.rate_hint.set.assert_called_with('观测未满窗口时按实际采样时长计算')
        window.rate_view.get.return_value='速度趋势'
        window.select_rate_view()
        window.timeline.heading.assert_called_with('realtime',text='速度趋势')
        values=window.timeline.insert.call_args.kwargs['values']
        self.assertEqual(values[2],'600')
        self.assertEqual(values[4],'旧60秒口径')
        window.rate_hint.set.assert_called_with('速度趋势预热中；非整分钟收益')

    def test_five_windows_round_trip_use_shared_segment_coverage(self):
        now=datetime(2026,9,27,12).replace(tzinfo=BEIJING).timestamp()
        values={60:100,120:0,300:None,600:55,1800:77}
        metadata=dict(algorithm='time_ewma_v1',state='ready',half_life_seconds=10,
                      observed_seconds=450,segment=3,rolling60=999,
                      window_algorithm=WINDOW_ALGORITHM,
                      windows={str(seconds):dict(value=value) for seconds,value in values.items()})
        self.store.append(self.row(100,100,450),120,now=now,rate_metadata=metadata)
        row=self.store.samples('2026-09-27')[0]
        self.assertEqual(row['window_algorithm'],WINDOW_ALGORITHM)
        self.assertEqual(row['rate_segment'],3)
        for seconds,value in values.items():
            self.assertEqual(row[f'rolling{seconds}'],value)
            sample=window_sample(row,seconds)
            self.assertEqual(sample['observed_seconds'],min(seconds,450))
            self.assertEqual(sample['value'],value)
            self.assertEqual(sample['state'],'unavailable' if value is None else 'ready' if seconds<=450 else 'warming')

    def test_recent_rates_cross_midnight_and_session_export_keeps_sequence(self):
        start=datetime(2026,9,27,23,59,50).replace(tzinfo=BEIJING).timestamp()
        session=self.store.begin(start)
        for now,rate in ((start+1,100),(start+20,200),(start+20,300)):
            self.store.append(self.row(0,0,now-start),rate,now=now)
        self.store.finish(20,start+20)
        self.store.begin(start+30)
        self.store.append(self.row(0,0,1),999,now=start+31)
        statements=[]
        self.store.db.set_trace_callback(statements.append)
        try:
            rows=self.store.recent_rate_samples(session,start+1)
        finally:
            self.store.db.set_trace_callback(None)
        self.assertEqual([row['realtime'] for row in rows],[100,200,300])
        self.assertTrue(all(row['session_id']==session for row in rows))
        self.assertEqual(len(statements),1)
        self.assertEqual([row['realtime'] for row in self.store.recent_rate_samples(session,start+20)],[200,300])
        exported=self.store.session_rate_metadata(session)
        self.assertIs(iter(exported),exported)
        self.assertEqual([(row['seq'],row['realtime']) for row in exported],[(1,100),(2,200),(3,300)])

    def test_export_preserves_existing_columns_and_adds_rate_metadata(self):
        now=datetime(2026,9,27,12).replace(tzinfo=BEIJING).timestamp()
        self.store.append(self.row(0,0,1),None,now=now,rate_metadata=dict(
            algorithm='time_ewma_v1',state='unavailable',half_life_seconds=10,
            observed_seconds=0,segment=3,rolling60=None))
        window=HistoryWindow.__new__(HistoryWindow)
        window.day='2026-09-27';window.store=self.store;window.window=None
        path=Path(self.temp.name)/'export.csv'
        with patch('history_view.filedialog.asksaveasfilename',return_value=str(path)):
            window.export()
        with path.open(encoding='utf-8-sig',newline='') as file:
            header,row=list(csv.reader(file))
        self.assertEqual(header[4],'速度趋势（按算法列）')
        self.assertEqual(header[10:16],['分均算法','半衰期秒数','分均状态','观测秒数','1min分均（按窗口算法）','分均采样段'])
        self.assertEqual(header[16:21],['2min分均（按窗口算法）','5min分均（按窗口算法）','10min分均（按窗口算法）','30min分均（按窗口算法）','窗口算法版本'])
        self.assertEqual(header[21:],['趋势收益有缺口（1是/0否）','窗口缺口位掩码（1/2/5/10/30min=1/2/4/8/16）'])
        self.assertEqual(row[4],'')
        self.assertEqual(row[10:16],['time_ewma_v1','10.0','unavailable','0.0','','3'])
        self.assertEqual(row[16:21],['']*5)
        self.assertEqual(row[21:],['0','0'])

    def test_export_multiscale_values_and_switch_history_view(self):
        now=datetime(2026,9,27,12).replace(tzinfo=BEIJING).timestamp()
        self.store.append(self.row(10,10,100),600,now=now,rate_metadata=dict(
            algorithm='time_ewma_v1',state='ready',half_life_seconds=10,observed_seconds=100,
            segment=1,window_algorithm=WINDOW_ALGORITHM,
            windows={str(seconds):dict(value=value) for seconds,value in ((60,100),(120,80),(300,60),(600,40),(1800,0))}))
        window=HistoryWindow.__new__(HistoryWindow)
        window.day='2026-09-27';window.store=self.store;window.window=None
        window.rows=self.store.samples(window.day)
        window.rate_view=Mock();window.timeline=Mock();window.timeline.get_children.return_value=[]
        window.rate_hint=Mock()
        window.draw_chart=Mock()
        window.rate_view.get.return_value='2 min'
        window.select_rate_view()
        window.timeline.heading.assert_called_with('realtime',text='2 min 分均')
        values=window.timeline.insert.call_args.kwargs['values']
        self.assertEqual(values[2],'80')
        self.assertIn('预热',values[4])
        window.rate_view.get.return_value=DEFAULT_RATE_VIEW
        window.select_rate_view()
        window.timeline.heading.assert_called_with('realtime',text='1 min 分均')
        self.assertEqual(window.timeline.insert.call_args.kwargs['values'][2],'100')
        path=Path(self.temp.name)/'multiscale-export.csv'
        with patch('history_view.filedialog.asksaveasfilename',return_value=str(path)):
            window.export()
        with path.open(encoding='utf-8-sig',newline='') as file:
            header,row=list(csv.reader(file))
        self.assertEqual(row[14],'100.0')
        self.assertEqual(row[16:21],['80.0','60.0','40.0','0.0',WINDOW_ALGORITHM])

    def test_new_sessions_do_not_reset_daily_total_or_double_duration(self):
        now=datetime(2026,9,27,12).replace(tzinfo=BEIJING).timestamp()
        for gain in [100,250]:
            self.store.begin(now)
            self.store.append(self.row(gain,gain,60),now=now+60)
            self.store.checkpoint(60,now+60)
            self.store.checkpoint(60,now+90)
            self.store.finish(60,now+90)
            now+=100
        day=self.store.days()[0]
        self.assertEqual(day['gain'],350)
        self.assertEqual(day['sessions'],2)
        self.assertEqual(day['seconds'],120)

    def test_legacy_migration_is_idempotent(self):
        folder=Path(self.temp.name)/'sessions';folder.mkdir()
        with (folder/'old.csv').open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.writer(f);w.writerow(['header']*9)
            w.writerow(['2026-09-27 12:00:00',0,1000,0,0,0,0,'baseline','1000'])
            w.writerow(['2026-09-27 12:01:00',60,1123,123,123,123,7380,'ok','1123'])
        self.assertEqual(self.store.import_legacy(folder),1)
        self.assertEqual(self.store.import_legacy(folder),0)
        self.assertEqual(self.store.days()[0]['gain'],123)
        original_wall_time=datetime(2026,9,27,12).timestamp()  # untagged legacy CSV used the host timezone
        self.assertIsNone(self.store.samples(local_day(original_wall_time))[0]['realtime'])

    def test_summary_fallback_is_explicit_and_not_duplicated(self):
        path=Path(self.temp.name)/'status.json'
        path.write_text(json.dumps(dict(updated=datetime(2026,9,27,12).replace(tzinfo=BEIJING).timestamp(),total=400,current=1400,elapsed=60,per_minute=400,per_hour=24000)),encoding='utf-8')
        self.assertTrue(self.store.import_snapshot(path))
        self.assertFalse(self.store.import_snapshot(path))
        sample=self.store.samples('2026-09-27')[0]
        self.assertEqual(sample['status'],'legacy_summary')
        self.assertIsNone(sample['realtime'])
        self.assertEqual(self.store.days()[0]['gain'],400)


class RatePlotTests(unittest.TestCase):
    def test_window_views_preserve_the_agreed_order(self):
        self.assertEqual(list(RATE_VIEWS.items()),[('1 min',60),('2 min',120),('5 min',300),('10 min',600),('30 min',1800),('速度趋势',None)])
        self.assertEqual(RATE_VIEWS[DEFAULT_RATE_VIEW],60)

    def test_window_plot_separates_legacy_values_and_never_falls_back(self):
        rows=[
            dict(timestamp=0,realtime=900,rolling60=100,session_id='a',rate_segment=1),
            dict(timestamp=1,realtime=800,rolling60=200,session_id='a',rate_segment=1),
            dict(timestamp=2,realtime=700,rolling60=300,session_id='a',rate_segment=1,
                 window_algorithm=WINDOW_ALGORITHM,rate_state='ready',rate_observed_seconds=120),
            dict(timestamp=3,realtime=600,rolling60=None,session_id='a',rate_segment=1,
                 window_algorithm=WINDOW_ALGORITHM,rate_state='ready',rate_observed_seconds=121),
            dict(timestamp=4,realtime=500,rolling60=0,session_id='a',rate_segment=1,
                 window_algorithm=WINDOW_ALGORITHM,rate_state='ready',rate_observed_seconds=122),
        ]
        plotted=rate_plot_rows(rows,60)
        self.assertEqual([row['value'] for row in plotted],[100,200,300,0])
        self.assertEqual(plotted[0]['segment'],plotted[1]['segment'])
        self.assertNotEqual(plotted[1]['segment'],plotted[2]['segment'])
        self.assertNotEqual(plotted[2]['segment'],plotted[3]['segment'])
        self.assertEqual(rate_plot_rows(rows,120),[])

    def test_window_availability_uses_parent_state_and_coverage(self):
        row=dict(window_algorithm=WINDOW_ALGORITHM,rate_state='warming',rate_observed_seconds=10,rolling60=0)
        self.assertEqual(window_sample(row,60),dict(value=0,observed_seconds=10,state='warming',has_gaps=False,quality='complete'))
        for state in ('unavailable','stale','paused'):
            self.assertIsNone(window_sample(dict(row,rate_state=state),60)['value'])
        self.assertIsNone(window_sample(dict(row,rate_observed_seconds=0),60)['value'])

    def test_half_life_change_breaks_only_trend_curve(self):
        rows=[dict(timestamp=index,realtime=600,rolling60=100,session_id='a',
                   rate_algorithm='time_ewma_v1',rate_half_life=half_life,rate_segment=1,
                   rate_state='ready',rate_observed_seconds=60+index,window_algorithm=WINDOW_ALGORITHM)
              for index,half_life in enumerate((10,10,30,30))]
        trend=rate_plot_rows(rows)
        self.assertEqual(trend[0]['segment'],trend[1]['segment'])
        self.assertNotEqual(trend[1]['segment'],trend[2]['segment'])
        self.assertEqual(trend[2]['segment'],trend[3]['segment'])
        self.assertEqual(len({row['segment'] for row in rate_plot_rows(rows,60)}),1)

    def test_history_trend_caption_describes_saved_half_lives_and_legacy_mix(self):
        old=dict(timestamp=0,realtime=100,session_id='a',rate_algorithm='rolling60_v1')
        h10=dict(timestamp=1,realtime=200,session_id='a',rate_algorithm='time_ewma_v1',rate_half_life=10)
        h30=dict(timestamp=2,realtime=300,session_id='a',rate_algorithm='time_ewma_v1',rate_half_life=30)
        view=HistoryWindow.__new__(HistoryWindow)
        view.chart=Mock();view.chart.winfo_width.return_value=600
        view.minute_chart=Mock();view.draw_minutes=Mock();view.scale=1
        view.rate_view=Mock();view.rate_view.get.return_value='速度趋势'
        for rows,caption in (([old],'旧60秒口径'),([h10],'半衰期 10 秒'),
                             ([h30],'半衰期 30 秒'),([old,h30],'混合口径'),
                             ([h10,h30],'混合口径')):
            with self.subTest(caption=caption),patch('history_view.plot'):
                view.rows=rows;view.chart.reset_mock()
                view.draw_chart()
                title=view.chart.create_text.call_args_list[0].kwargs['text']
                self.assertTrue(title.startswith('速度趋势曲线'))
                self.assertIn(caption,title)

    def test_gaps_algorithms_sessions_and_segments_break_lines(self):
        rows=[
            dict(timestamp=0,realtime=100,session_id='a'),
            dict(timestamp=1,realtime=200,session_id='a'),
            dict(timestamp=2,realtime=None,session_id='a'),
            dict(timestamp=3,realtime=300,session_id='a'),
            dict(timestamp=4,realtime=400,session_id='a',rate_algorithm='time_ewma_v1',rate_segment=1),
            dict(timestamp=5,realtime=500,session_id='a',rate_algorithm='time_ewma_v1',rate_segment=1),
            dict(timestamp=6,realtime=600,session_id='a',rate_algorithm='time_ewma_v1',rate_segment=2),
            dict(timestamp=7,realtime=700,session_id='b',rate_algorithm='time_ewma_v1',rate_segment=2),
            dict(timestamp=8,realtime=800,session_id='b',rate_algorithm='rolling60_v1'),
            dict(timestamp=9,realtime=900,session_id='b',rate_algorithm='time_ewma_v1',rate_segment=2),
        ]
        plotted=rate_plot_rows(rows)
        self.assertEqual([r['timestamp'] for r in plotted],[0,1,3,4,5,6,7,8,9])
        segments=[r['segment'] for r in plotted]
        self.assertEqual(segments[0],segments[1])
        self.assertEqual(segments[3],segments[4])
        self.assertEqual(len(set(segments)),7)

    def test_unavailable_state_breaks_lines_but_valid_zero_is_kept(self):
        rows=[dict(timestamp=i,realtime=0,session_id='a',rate_algorithm='time_ewma_v1',rate_segment=1,rate_state=state)
              for i,state in enumerate(('warming','stale','ready','unavailable','ready'))]
        plotted=rate_plot_rows(rows)
        self.assertEqual([r['timestamp'] for r in plotted],[0,2,4])
        self.assertEqual([r['value'] for r in plotted],[0,0,0])
        self.assertEqual(len({r['segment'] for r in plotted}),3)

    def test_labels_identify_saved_algorithm(self):
        self.assertEqual(rate_label(dict(realtime=0)),'旧60秒口径')
        self.assertEqual(rate_label(dict(realtime=None)),'旧记录无值')
        self.assertIn('半衰期 10 秒',rate_label(dict(rate_algorithm='time_ewma_v1',rate_half_life=10)))
        self.assertEqual(rate_label(dict(rate_algorithm='future_v2')),'future_v2')

    def test_isolated_rates_remain_visible_without_connecting_over_gaps(self):
        rows=[dict(timestamp=i,realtime=value,session_id='a')
              for i,value in enumerate((100,None,200,None,300))]
        canvas=Mock()
        plot(canvas,(0,0,400,140),rate_plot_rows(rows))
        self.assertEqual(canvas.create_oval.call_count,3)
        rate_lines=[call for call in canvas.create_line.call_args_list if call.kwargs.get('fill')==ACCENT]
        self.assertEqual(rate_lines,[])

    def test_fixed_plot_range_keeps_axis_on_empty_and_partial_data(self):
        for rows in ([],[dict(timestamp=50,value=100)]):
            canvas=Mock()
            plot(canvas,(0,0,400,140),rows,time_range=(0,100))
            labels=[call.kwargs['text'] for call in canvas.create_text.call_args_list]
            import time
            self.assertIn(format_time(0),labels)
            self.assertIn(format_time(100),labels)
            if rows:
                point=canvas.create_oval.call_args.args
                self.assertAlmostEqual((point[0]+point[2])/2,216)

if __name__=='__main__':
    unittest.main()
