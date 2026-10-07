"""Missing-tail quality survives the tracker, ledger, replay and export path."""
import csv
import tempfile
import unittest
from datetime import datetime
from app_time import BEIJING
from pathlib import Path
from unittest.mock import Mock, patch

from auto_level import AutoLevel
from history_store import HistoryStore
from history_view import HistoryWindow
from tracker import Reading, Tracker, WINDOW_ALGORITHM, WINDOW_SECONDS
from ui_charts import plot, rate_plot_rows, rate_prefix, window_sample
from ui_theme import ACCENT, WARNING


class RateQualityHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'quality.sqlite3'
        self.store=HistoryStore(self.path)
        self.wall=datetime(2026,9,27,12,tzinfo=BEIJING).timestamp()
        self.day='2026-09-27'

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def upgraded_history(self):
        now=[0.]
        tracker=Tracker(lambda:now[0])
        guard=AutoLevel(tracker,lambda:now[0])
        tracker.start()
        for second in range(1801):
            now[0]=float(second)
            guard.feed(Reading(100+second,None,95),61)

        def save(status='ok',gain=0):
            metadata=tracker.live.snapshot()
            metadata.update(windows=tracker.windows.snapshot(),window_algorithm=WINDOW_ALGORITHM)
            average,hourly=tracker.rates()
            row=['',tracker.elapsed,tracker.previous,gain,tracker.total,average,hourly,status,'']
            self.store.append(row,metadata['value'],now=self.wall+now[0],rate_metadata=metadata)

        save()
        for at,current in ((1800.3,20),(1800.6,30),(1800.9,40)):
            now[0]=at
            status,gain=guard.feed(Reading(current,None,4),62)
        self.assertEqual(status,'auto_level_gap')
        save(status,gain)
        for elapsed in range(1,1801):
            now[0]=1800.9+elapsed
            guard.feed(Reading(40+elapsed,None,5),62)
            if elapsed in WINDOW_SECONDS:
                save()
        return self.store.active

    def history_view(self,rows,view='1 min'):
        window=HistoryWindow.__new__(HistoryWindow)
        window.day=self.day
        window.store=self.store
        window.window=None
        window.rows=rows
        window.rate_view=Mock()
        window.rate_view.get.return_value=view
        window.timeline=Mock()
        window.timeline.get_children.return_value=[]
        window.rate_hint=Mock()
        window.draw_chart=Mock()
        return window

    def test_tracker_quality_roundtrip_and_each_window_expiry(self):
        session=self.upgraded_history()
        self.store.db.close()
        self.store=HistoryStore(self.path)
        rows=self.store.samples(self.day)
        expected_masks=[0,31,30,28,24,16,0]
        self.assertEqual([row['window_gap_mask'] for row in rows],expected_masks)
        self.assertEqual([row['rate_has_gaps'] for row in rows],[0,1,1,1,1,1,1])
        for index,row in enumerate(rows):
            for width in WINDOW_SECONDS:
                sample=window_sample(row,width)
                # These windows were all full before the upgrade and stay full.
                self.assertEqual(sample['state'],'ready')
                self.assertEqual(sample['observed_seconds'],width)
                age=None if index==0 else (0,*WINDOW_SECONDS)[index-1]
                expected=age is not None and age<width
                self.assertEqual(sample['has_gaps'],expected)
                self.assertEqual(rate_prefix(sample),'≥ ' if expected else '')
        self.assertEqual([row['window_gap_mask'] for row in self.store.recent_rate_samples(session,self.wall)],expected_masks)
        exported=list(self.store.session_rate_metadata(session))
        self.assertEqual([row['window_gap_mask'] for row in exported],expected_masks)
        self.assertEqual([row['rate_has_gaps'] for row in exported],[0,1,1,1,1,1,1])

    def test_history_selected_window_removes_lower_bound_independently_of_trend(self):
        self.upgraded_history()
        row=self.store.samples(self.day)[2] # Exactly one minute after the upgrade.
        window=self.history_view([row])
        window.select_rate_view()
        values=window.timeline.insert.call_args.kwargs['values']
        self.assertFalse(values[2].startswith('≥'))
        self.assertNotIn('尾段未计入',values[4])
        for view in ('2 min','30 min','速度趋势'):
            window.rate_view.get.return_value=view
            window.select_rate_view()
            values=window.timeline.insert.call_args.kwargs['values']
            self.assertTrue(values[2].startswith('≥ '))
            self.assertIn('尾段未计入',values[4])
            self.assertIn('≥ 收益下限',window.rate_hint.set.call_args.args[0])

    def test_export_appends_quality_without_changing_original_numeric_columns(self):
        self.upgraded_history()
        rows=self.store.samples(self.day)
        window=self.history_view(rows)
        path=Path(self.temp.name)/'quality.csv'
        with patch('history_view.filedialog.asksaveasfilename',return_value=str(path)):
            window.export()
        with path.open(encoding='utf-8-sig',newline='') as source:
            header,*exported=csv.reader(source)
        self.assertEqual(header[20],'窗口算法版本')
        self.assertEqual(len(header),23)
        self.assertIn('1/2/5/10/30min=1/2/4/8/16',header[22])
        self.assertEqual([row[21:] for row in exported],[['0','0'],['1','31'],['1','30'],['1','28'],['1','24'],['1','16'],['1','0']])
        self.assertEqual([float(row[14]) for row in exported],[row['rolling60'] for row in rows])
        self.assertEqual([float(row[4]) for row in exported],[row['realtime'] for row in rows])
        self.assertEqual([float(row[19]) for row in exported],[row['rolling1800'] for row in rows])


class RateQualityPlotTests(unittest.TestCase):
    def test_lower_bound_prefix_takes_precedence_over_warmup_and_approximation(self):
        self.assertEqual(rate_prefix(dict(has_gaps=True,state='warming')),'≥ ')
        self.assertEqual(rate_prefix(dict(has_gaps=True,state='ready'),approximate=True),'≥ ')
        self.assertEqual(rate_prefix(dict(state='warming')),'')
        self.assertEqual(rate_prefix(dict(state='ready'),approximate=True),'')
        self.assertEqual(rate_prefix(dict(state='ready')),'')

    def test_quality_transitions_split_curves_for_only_the_affected_window(self):
        rows=[dict(timestamp=index,session_id='a',rate_segment=1,
                   realtime=100+index,rolling60=100+index,rolling120=100+index,
                   rate_algorithm='time_ewma_v1',rate_half_life=30,rate_state='ready',
                   rate_observed_seconds=1800+index,window_algorithm=WINDOW_ALGORITHM,
                   window_gap_mask=mask,rate_has_gaps=index>=2)
              for index,mask in enumerate((0,0,31,31,30,30))]
        one=rate_plot_rows(rows,60)
        two=rate_plot_rows(rows,120)
        trend=rate_plot_rows(rows)
        self.assertEqual(len({row['segment'] for row in one}),3)
        self.assertEqual(len({row['segment'] for row in two}),2)
        self.assertEqual(len({row['segment'] for row in trend}),2)
        self.assertEqual([row['has_gaps'] for row in one],[False,False,True,True,False,False])
        self.assertEqual([row['has_gaps'] for row in trend],[False,False,True,True,True,True])

    def test_plot_uses_distinct_lower_bound_segments_and_legend(self):
        # Use one shared segment to verify the generic renderer itself also
        # protects quality boundaries for callers that supply direct snapshots.
        rows=[dict(timestamp=index,value=100+index,segment=1,has_gaps=2<=index<4)
              for index in range(6)]
        canvas=Mock()
        plot(canvas,(0,0,400,140),rows)
        data_lines=[call for call in canvas.create_line.call_args_list
                    if call.kwargs.get('fill') in (ACCENT,WARNING)]
        self.assertEqual(len(data_lines),3)
        self.assertEqual([call.kwargs['fill'] for call in data_lines],[ACCENT,WARNING,ACCENT])
        self.assertTrue(data_lines[1].kwargs['dash'])
        self.assertNotIn('dash',data_lines[0].kwargs)
        self.assertTrue(all(len(call.args)==4 for call in data_lines))
        labels=[call.kwargs['text'] for call in canvas.create_text.call_args_list]
        self.assertTrue(any('下限' in label and '尾段未计入' in label for label in labels))

    def test_old_rows_have_no_invented_gaps_or_lower_bound_legend(self):
        row=dict(timestamp=0,session_id='old',rolling60=100,realtime=120)
        self.assertFalse(window_sample(row,60)['has_gaps'])
        self.assertEqual(rate_prefix(window_sample(row,60)),'')
        rows=rate_plot_rows([row])
        self.assertFalse(rows[0]['has_gaps'])
        canvas=Mock()
        plot(canvas,(0,0,400,140),rows)
        self.assertFalse(any('下限' in call.kwargs['text'] for call in canvas.create_text.call_args_list))


if __name__=='__main__':
    unittest.main()
