import unittest
from unittest.mock import Mock, patch

from multiscale_view import MultiscaleWindow, window_row
from ui_charts import RATE_VIEWS


class MultiscaleFormatTests(unittest.TestCase):
    def snapshot(self, value, observed, state='warming'):
        return {'state': 'ready', 'windows': {'60': {
            'value': value, 'observed_seconds': observed, 'state': state,
        }}}

    def test_partial_window_is_approximate_and_does_not_round_up_to_full(self):
        row = window_row(self.snapshot(1234.5, 59.99), 60)
        self.assertEqual(row, ('1 min', '1,234', '59.9 / 60 秒', '采样中'))

    def test_zero_and_missing_values_remain_distinct(self):
        self.assertEqual(window_row(self.snapshot(0, 60, 'ready'), 60)[1:],
                         ('0', '60.0 / 60 秒', '满窗口'))
        self.assertEqual(window_row(self.snapshot(None, 0), 60)[1], '—')
        self.assertEqual(window_row(self.snapshot(1200, 60, 'ready'), 120)[1], '—')

    def test_parent_availability_masks_all_values(self):
        for state, label in [('paused', '已暂停'), ('stale', '读数中断'),
                             ('unavailable', '等待采样')]:
            with self.subTest(state=state):
                snapshot = self.snapshot(1200, 60, 'ready')
                snapshot['state'] = state
                row = window_row(snapshot, 60)
                self.assertEqual(row[1], '—')
                self.assertEqual(row[3], label)

    def test_retry_keeps_coverage_and_original_window_state(self):
        snapshot = self.snapshot(1234, 60, 'ready')
        snapshot.update(display_state='retrying', display_age_seconds=1.2)
        self.assertEqual(window_row(snapshot, 60),
                         ('1 min', '1,234', '60.0 / 60 秒', '识别重试'))
        self.assertEqual(snapshot['windows']['60']['state'], 'ready')
        self.assertEqual(snapshot['windows']['60']['observed_seconds'], 60)
        snapshot['windows']['60']['observed_seconds'] = 40
        self.assertEqual(window_row(snapshot, 60)[1], '1,234')

    def test_retry_never_turns_a_missing_window_into_a_value(self):
        snapshot = self.snapshot(None, 0)
        snapshot['display_state'] = 'retrying'
        self.assertEqual(window_row(snapshot, 60)[1], '—')

    def test_restored_full_window_is_marked_and_keeps_saved_coverage(self):
        snapshot = self.snapshot(1234,60,'ready')
        snapshot.update(display_state='restored',display_age_seconds=None)
        self.assertEqual(window_row(snapshot,60),
                         ('1 min','1,234','60.0 / 60 秒','已恢复'))
        self.assertEqual(snapshot['windows']['60']['observed_seconds'],60)
        snapshot['display_state'] = 'live'
        self.assertEqual(window_row(snapshot,60),
                         ('1 min','1,234','60.0 / 60 秒','满窗口'))
        snapshot.update(display_state='restored')
        snapshot['windows']['60']['value'] = None
        self.assertEqual(window_row(snapshot,60)[1],'—')


class MultiscaleSelectorTests(unittest.TestCase):
    def test_refresh_displays_retained_values_without_rewriting_chart_history(self):
        view = MultiscaleWindow.__new__(MultiscaleWindow)
        view._closed = False
        view.table = Mock()
        view.table_hint = Mock()
        view.backend = Mock()
        view.backend.history.active = 'session'
        view.backend.display_rate_snapshot.return_value = dict(
            state='ready', display_state='retrying', display_age_seconds=1.2,
            windows={'60': dict(value=1234, observed_seconds=60, state='ready')})
        view._cache_session = 'session'
        view._cache_time = 1999
        view._chart_rows = [dict(timestamp=1998, rolling60=100)]
        with patch('multiscale_view.time.monotonic', return_value=2000):
            view.refresh()
        view.table.item.assert_any_call('60', values=(
            '1 min', '1,234', '60.0 / 60 秒', '识别重试'), tags=('held',))
        view.table_hint.set.assert_called_once_with('重试中 · 暂显 1.2 秒前的有效分均')
        self.assertEqual(view._chart_rows, [dict(timestamp=1998, rolling60=100)])
        view.backend.history.recent_rate_samples.assert_not_called()

    def test_restore_hint_and_row_color_clear_on_new_live_reading(self):
        view = MultiscaleWindow.__new__(MultiscaleWindow)
        view._closed = False
        view.table = Mock()
        view.table_hint = Mock()
        view.backend = Mock()
        view.backend.history.active = 'session'
        snapshot = dict(state='ready',display_state='restored',display_age_seconds=None,
                        windows={'60':dict(value=1234,observed_seconds=60,state='ready')})
        view.backend.display_rate_snapshot.return_value = snapshot
        view._cache_session = 'session'
        view._cache_time = 1999
        with patch('multiscale_view.time.monotonic',return_value=2000):
            view.refresh()
            view.table.item.assert_any_call('60',values=(
                '1 min','1,234','60.0 / 60 秒','已恢复'),tags=('held',))
            self.assertIn('离线时间不计入',view.table_hint.set.call_args.args[0])
            snapshot['display_state'] = 'live'
            view.refresh()
        view.table.item.assert_any_call('60',values=(
            '1 min','1,234','60.0 / 60 秒','满窗口'),tags=())
        self.assertNotIn('已恢复',view.table_hint.set.call_args.args[0])
        view.backend.history.recent_rate_samples.assert_not_called()

    def test_selection_uses_saved_matching_window_and_reuses_cache(self):
        view = MultiscaleWindow.__new__(MultiscaleWindow)
        view._closed = False
        view.scale = 1
        view.selection = Mock()
        view.chart_title = Mock()
        view.chart_hint = Mock()
        view.chart = Mock()
        view.chart.winfo_width.return_value = 650
        view.chart.winfo_height.return_value = 180
        view.backend = Mock()
        view._chart_rows = [dict(
            timestamp=1995, realtime=250, rolling60=100, rolling120=150,
            rate_state='ready', rate_observed_seconds=1800,
            window_algorithm='observed_window_v1', rate_algorithm='time_ewma_v1',
            session_id='test', rate_segment=1,
        )]
        with patch('multiscale_view.time.time', return_value=2000), patch('multiscale_view.plot') as draw:
            for label, seconds in RATE_VIEWS.items():
                with self.subTest(label=label):
                    view.selection.get.return_value = label
                    view.draw_chart()
                    args, kwargs = draw.call_args
                    expected = {None: 250, 60: 100, 120: 150}.get(seconds)
                    self.assertEqual([row['value'] for row in args[2]],
                                     [] if expected is None else [expected])
                    self.assertEqual(kwargs['time_range'], (2000 - max(60, seconds or 0), 2000))
            view.backend.history.recent_rate_samples.assert_not_called()


if __name__ == '__main__':
    unittest.main()
