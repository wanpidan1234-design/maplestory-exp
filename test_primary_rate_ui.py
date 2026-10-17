"""The main UI consumes observed 60-second rates, independently of its trend."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from multiscale_view import trend_hint
from orb import Orb, rate_window_display
from ui_midnight import MidnightPanel, percent_rates, level_up_minutes, format_level_eta
from ui_theme import ACCENT, WARNING, MIDNIGHT
from app_time import local_day


class PrimaryRateUiTests(unittest.TestCase):
    def make_orb(self, value=100, state='warming', observed=40):
        primary = dict(value=value, state=state, observed_seconds=observed,
                       window_seconds=60, algorithm='observed_window_v1')
        snapshot = dict(value=900, state='ready', half_life_seconds=30,
                        primary=primary, windows={str(seconds): dict(
                            value=value,state=state,observed_seconds=observed)
                            for seconds in (120,300,1800)})
        backend = SimpleNamespace(
            primary_rate_snapshot=lambda: primary, rate_snapshot=lambda: snapshot,
            display_rate_snapshot=lambda: snapshot,
            tracker=SimpleNamespace(blocked=False, elapsed=observed,
                                    rates=lambda: (10, 600), previous=1234,
                                    total=500, level=None, level_gaps=0),
            running=True, fresh_reading=lambda: True, capture_info={},
            history=SimpleNamespace(days=lambda: []),
            latest=SimpleNamespace(percent=25), status=Mock(),
        )
        view = Orb.__new__(Orb)
        view.backend = backend
        view.scale = view.base_scale = 1
        view.panel = Mock()
        view.panel_canvas = Mock()
        view.panel_canvas.bbox.return_value = (20,20,90,35)
        view.canvas = Mock()
        view.fit_value = Mock()
        view.paint_combat_summary = Mock()
        view.paint_minutes = Mock()
        view.toggle_tag = Mock()
        view.ball_level = SimpleNamespace(active=False)
        view.ball_motion = SimpleNamespace(pulse_started=None)
        view.inline_rates = {seconds:f'window_{seconds}' for seconds in (120,300,1800)}
        view.rate_progress = {seconds:dict(track=f'track_{seconds}',fill=f'fill_{seconds}',
                                          caption=f'caption_{seconds}',x=x,y=233,width=72)
                              for seconds,x in ((120,26),(300,138),(1800,250))}
        for name in ('session_state', 'status_dot', 'timer', 'total', 'minute',
                     'hour', 'today', 'live_value', 'live_hour', 'current_value',
                     'current_percent', 'progress', 'detail', 'ball_value', 'ball_unit'):
            setattr(view, name, name)
        renderer = view.midnight_panel = MidnightPanel.__new__(MidnightPanel)
        renderer.orb, renderer.canvas, renderer.scale = view, view.panel_canvas, 1
        renderer.expanded, renderer.note_extra = False, 0
        renderer.exp_box = (301,138,249)
        renderer.chart_boxes = dict(curve=(20,398,530,74),bars=(20,493,530,92),
                                    daily=(20,605,530,92),daycurve=(20,727,530,110))
        renderer.daily_rows=[]
        renderer.day_caption='day_caption'
        renderer.paint_combat = Mock()
        renderer._reflow = Mock()
        renderer.icon = Mock()
        for name in ('session_state','status_dot','timer','total','minute','today',
                     'live_value','live_hour','current_value','current_percent',
                     'progress','detail','toggle_tag','inline_rates','rate_progress'):
            setattr(renderer,name,getattr(view,name))
        renderer.live_unit,renderer.level='live_unit','level'
        renderer.percent_minute,renderer.percent_hour='percent_minute','percent_hour'
        renderer.eta_label,renderer.eta_value='eta_label','eta_value'
        def fit(item,value,size,width,color=None,**options):
            view.fit_value(view.panel_canvas,item,value,size,width,color=color)
            view.panel_canvas.itemconfigure(item,text=value,**({'fill':color} if color else {}))
        renderer.fit = fit
        return view, snapshot

    def fitted_values(self, view):
        return {call.args[1]: call.args[2] for call in view.fit_value.call_args_list}

    def configured_text(self, canvas, name):
        return next(call.kwargs['text'] for call in canvas.itemconfigure.call_args_list
                    if call.args[0] == name and 'text' in call.kwargs)

    def test_primary_stays_warming_after_trend_is_ready(self):
        view, snapshot = self.make_orb()
        self.assertEqual(view.state()[0], '采样中')
        view.paint_panel()
        fitted = self.fitted_values(view)
        self.assertEqual(fitted['live_value'], '100')
        self.assertEqual(fitted['live_hour'], '折算 6,000 / 时')
        self.assertIn('40.0 / 60 秒', self.configured_text(view.panel_canvas, 'session_state'))
        self.assertEqual(self.configured_text(view.panel_canvas, 'detail'),'')

    def test_full_window_has_no_approximation_and_hour_is_its_projection(self):
        view, snapshot = self.make_orb(value=125, state='ready', observed=60)
        view.paint_panel()
        self.assertEqual(view.state()[0], '识别正常')
        self.assertEqual(self.fitted_values(view)['live_value'], '125')
        self.assertEqual(self.fitted_values(view)['live_hour'], '折算 7,500 / 时')

    def test_percentage_rates_use_the_same_window_and_accepted_hud(self):
        view, snapshot = self.make_orb(value=1000)
        snapshot.update(current_exp=50000,current_percent=25)
        view.paint_panel()
        values = self.fitted_values(view)
        self.assertEqual(values['percent_minute'],'0.50 % / 分')
        self.assertEqual(values['percent_hour'],'30.00 % / 时')
        # A retry must use the saved HUD pair, not the latest unconfirmed reading.
        snapshot.update(display_state='held',display_age_seconds=5)
        view.backend.latest.percent=90
        view.backend.tracker.previous=100
        view.paint_panel()
        self.assertEqual(self.fitted_values(view)['percent_hour'],'30.00 % / 时')

    def test_percentage_conversion_handles_unknown_zero_and_exact_cap(self):
        self.assertEqual(percent_rates(1000,50000,25),(0.5,30))
        self.assertEqual(percent_rates(0,50000,25),(0,0))
        self.assertEqual(percent_rates(1000,0,0,100000),(1,60))
        self.assertEqual(percent_rates(3000,50000,25,100000),(3,180))
        for rate,exp,pct in ((None,50000,25),(1000,0,0),(1000,50000,None),
                             (float('nan'),50000,25),(1000,50000,101)):
            with self.subTest(rate=rate,exp=exp,pct=pct):
                self.assertEqual(percent_rates(rate,exp,pct),(None,None))

    def test_percentage_rates_keep_missing_and_lower_bound_states(self):
        view, snapshot = self.make_orb(value=None,state='paused')
        view.paint_panel()
        self.assertEqual(self.fitted_values(view)['percent_minute'],'— % / 分')
        snapshot['primary'].update(value=1000,state='ready',has_gaps=True)
        snapshot.update(current_exp=50000,current_percent=25)
        view.paint_panel()
        self.assertEqual(self.fitted_values(view)['percent_minute'],'≥ 0.50 % / 分')

    def test_level_eta_uses_current_level_and_same_primary_rate(self):
        view, snapshot = self.make_orb(value=1000)
        snapshot.update(current_exp=50000,current_percent=25)
        view.paint_panel()
        self.assertEqual(self.fitted_values(view)['eta_value'],'2 小时 30 分钟')
        snapshot.update(current_cap=100000)
        view.paint_panel()
        self.assertEqual(self.fitted_values(view)['eta_value'],'50 分钟')
        snapshot['primary']['has_gaps']=True
        view.paint_panel()
        self.assertEqual(self.fitted_values(view)['eta_value'],'≤ 50 分钟')
        snapshot.update(display_state='held',display_age_seconds=8)
        view.backend.tracker.previous=99999
        view.backend.latest.percent=99.99
        view.paint_panel()
        self.assertEqual(self.fitted_values(view)['eta_label'],'上次估算')
        self.assertEqual(self.fitted_values(view)['eta_value'],'≤ 50 分钟')

    def test_level_eta_unknown_and_duration_boundaries(self):
        self.assertEqual(level_up_minutes(1000,50000,25),150)
        self.assertEqual(level_up_minutes(1000,0,None,100000),100)
        for args in ((0,50000,25),(None,50000,25),(1000,0,0),
                     (1000,50000,None),(1000,100001,99,100000),
                     (1000,None,99,100000),(float('inf'),50000,25)):
            with self.subTest(args=args):
                self.assertIsNone(level_up_minutes(*args))
        for minutes, label in ((None,'暂无法估算'),(0.5,'不足 1 分钟'),
                               (1,'1 分钟'),(59.5,'1 小时'),
                               (61,'1 小时 1 分钟'),(1500,'1 天 1 小时')):
            self.assertEqual(format_level_eta(minutes),label)

    def test_level_eta_paused_or_zero_gain_is_unavailable(self):
        for rate in (None,0):
            view, snapshot = self.make_orb(value=rate)
            snapshot.update(current_exp=50000,current_percent=25)
            view.paint_panel()
            self.assertEqual(self.fitted_values(view)['eta_value'],'暂无法估算')

    def test_upgrade_gap_is_a_lower_bound_without_hiding_full_coverage(self):
        view, snapshot = self.make_orb(value=125,state='ready',observed=1800)
        snapshot['has_gaps']=True
        snapshot['primary']['has_gaps']=True
        for sample in snapshot['windows'].values():sample['has_gaps']=True
        view.backend.tracker.level_gaps=1
        view.paint_panel()
        self.assertEqual(self.fitted_values(view)['live_value'],'≥ 125')
        self.assertEqual(self.fitted_values(view)['live_hour'],'折算 ≥ 7,500 / 时')
        for seconds in (120,300,1800):
            shown=rate_window_display(snapshot,seconds)
            self.assertEqual(shown['number'],'≥ 125')
            self.assertEqual(shown['ratio'],1)
            self.assertEqual(shown['caption'],'')
        self.assertIn('升级尾段未知',self.configured_text(view.panel_canvas,'detail'))

    def test_feedback_exception_keeps_upgrade_watcher_scheduled(self):
        view=Orb.__new__(Orb)
        view.closing=False
        view.backend=SimpleNamespace(ui_animation_state={})
        view.ball=Mock()
        view._feedback_tick=Mock(side_effect=OSError('fixture asset failure'))
        view.feedback_tick()
        view.ball.after.assert_called_once_with(100,view.feedback_tick)
        self.assertIn('fixture asset failure',view.backend.ui_animation_state['last_error'])

    def test_failed_orb_animation_does_not_suppress_panel_trigger(self):
        view=Orb.__new__(Orb)
        view.backend=SimpleNamespace(ui_animation_state={})
        view.ball_motion=Mock()
        view.ball_level=Mock()
        view.panel_level=Mock()
        view.ball_level.play.side_effect=OSError('fixture frame failure')
        event=dict(from_level=64,to_level=65,confirmation_id=1)
        view.celebrate_level(event,preview=False)
        view.panel_level.play.assert_called_once_with(view.level_started,from_level=64,to_level=65)
        self.assertEqual(view.backend.ui_animation_state['confirmation_id'],1)
        self.assertIn('fixture frame failure',view.backend.ui_animation_state['last_error'])

    def test_first_observation_is_missing_but_confirmed_zero_is_numeric(self):
        view, snapshot = self.make_orb(value=None, observed=0)
        snapshot.update(value=None, state='warming')
        view.paint_panel()
        self.assertEqual(self.fitted_values(view)['live_value'], '—')
        self.assertEqual(self.fitted_values(view)['live_hour'], '折算 — / 时')
        view, _ = self.make_orb(value=0, state='ready', observed=60)
        view.paint_panel()
        self.assertEqual(self.fitted_values(view)['live_value'], '0')

    def test_orb_uses_primary_number_and_approximation(self):
        view, _ = self.make_orb(value=100)
        font = Mock()
        font.measure.return_value = 20
        font.metrics.return_value = 10
        with patch('orb.tkfont.Font', return_value=font):
            view.paint_ball()
        self.assertEqual(self.configured_text(view.canvas, 'ball_value'), '100')

    def test_stale_and_paused_states_hide_the_trend(self):
        for state in ('stale', 'paused'):
            with self.subTest(state=state):
                view, snapshot = self.make_orb(value=None, state=state)
                snapshot.update(value=None, state=state)
                view.backend.fresh_reading = lambda: False
                view.backend.running = state != 'paused'
                view.backend.status.get.return_value = ''
                view.paint_panel()
                self.assertEqual(self.fitted_values(view)['live_value'], '—')
                self.assertEqual(self.fitted_values(view)['current_value'], '—')
                self.assertNotIn('速度趋势', self.configured_text(view.panel_canvas, 'detail'))

    def test_retry_retains_all_values_and_marks_their_original_age(self):
        view, snapshot = self.make_orb(value=125, state='ready', observed=60)
        snapshot.update(display_state='retrying', display_age_seconds=1.3,current_exp=1234,current_percent=25)
        view.backend.fresh_reading = lambda: False
        view.paint_panel()
        fitted = self.fitted_values(view)
        self.assertEqual(view.state(), ('识别重试', WARNING))
        self.assertEqual(fitted['live_value'], '125')
        self.assertEqual(fitted['live_hour'], '折算 7,500 / 时')
        for seconds in (120, 300, 1800):
            self.assertEqual(fitted[f'window_{seconds}'], '125')
            self.assertEqual(self.configured_text(view.panel_canvas, f'caption_{seconds}'),
                             '重试 · 已采样 01:00')
            view.panel_canvas.itemconfigure.assert_any_call(
                f'fill_{seconds}',state='normal',fill=MIDNIGHT['warning'])
        self.assertEqual(self.configured_text(view.panel_canvas, 'session_state'),'识别重试')
        self.assertIn('1.3 秒前',self.configured_text(view.panel_canvas,'detail'))
        self.assertIn('等待读数', self.configured_text(view.panel_canvas, 'detail'))
        self.assertNotIn('速度趋势', self.configured_text(view.panel_canvas, 'detail'))
        self.assertEqual(self.configured_text(view.panel_canvas, 'current_percent'), '25.00%')
        self.assertEqual(fitted['current_value'], '1,234')
        self.assertEqual(snapshot['primary']['observed_seconds'], 60)
        self.assertEqual(snapshot['primary']['state'], 'ready')
        self.assertEqual(self.fitted_values(view)['live_value'], '125')
        self.assertTrue(any(c.args==('live_value',) and c.kwargs.get('fill')==MIDNIGHT['warning']
                            for c in view.panel_canvas.itemconfigure.call_args_list))

    def test_long_interruption_keeps_numbers_progress_and_exp_with_explicit_age(self):
        view,snapshot=self.make_orb(value=125,state='ready',observed=60)
        snapshot.update(display_state='held',display_age_seconds=33,current_exp=1234,current_percent=25)
        view.backend.fresh_reading=lambda:False
        view.paint_panel()
        self.assertEqual(view.state(),('等待读数',WARNING))
        self.assertEqual(self.fitted_values(view)['live_value'],'125')
        self.assertEqual(self.fitted_values(view)['current_value'],'1,234')
        self.assertEqual(self.configured_text(view.panel_canvas,'current_percent'),'25.00%')
        self.assertIn('33.0 秒前',self.configured_text(view.panel_canvas,'detail'))
        self.assertEqual(rate_window_display(snapshot,120)['ratio'],.5)
        self.assertIn('待更新',rate_window_display(snapshot,120)['caption'])

    def test_retry_orb_marks_once_and_recovers_normal_color(self):
        for state in ('ready', 'warming'):
            with self.subTest(state=state):
                view, snapshot = self.make_orb(value=125, state=state, observed=40)
                snapshot.update(display_state='retrying', display_age_seconds=.8)
                view.backend.fresh_reading = lambda: False
                font = Mock()
                font.measure.return_value = 20
                font.metrics.return_value = 10
                with patch('orb.tkfont.Font', return_value=font):
                    view.paint_ball()
                self.assertEqual(self.configured_text(view.canvas, 'ball_value'), '125')
                view.canvas.itemconfigure.assert_any_call(
                    'ball_value', text='125', font=('Bahnschrift', -21),
                    anchor='sw', fill='#f2d5a1')
                snapshot['display_state'] = 'live'
                view.backend.fresh_reading = lambda: True
                view.paint_panel()
                self.assertTrue(any(c.args==('live_value',) and c.kwargs.get('fill')==MIDNIGHT['ink']
                                    for c in view.panel_canvas.itemconfigure.call_args_list))

    def test_pause_block_and_level_confirmation_take_precedence_over_retry_label(self):
        view, snapshot = self.make_orb()
        snapshot.update(display_state='retrying', display_age_seconds=.5)
        view.backend.running = False
        self.assertEqual(view.state()[0], '已暂停')
        view.backend.running = True
        view.backend.tracker.blocked = True
        self.assertEqual(view.state()[0], '需要确认')
        view.backend.tracker.blocked = False
        view.backend.auto_level = SimpleNamespace(wait_since=10)
        self.assertEqual(view.state()[0], '核验变化')

    def test_restored_values_and_totals_display_before_start_or_new_reading(self):
        for running in (False, True):
            with self.subTest(running=running):
                view, snapshot = self.make_orb(value=125, state='ready', observed=60)
                snapshot.update(display_state='restored', display_age_seconds=None)
                view.backend.running = running
                view.backend.fresh_reading = lambda: False
                view.backend.tracker.previous = None
                view.backend.primary_rate_snapshot = lambda: dict(value=None,state='unavailable')
                for seconds in (120,300,1800):
                    snapshot['windows'][str(seconds)]['observed_seconds'] = seconds
                self.assertEqual(view.state(), ('已恢复', WARNING))
                view.paint_panel()
                fitted = self.fitted_values(view)
                self.assertEqual(fitted['live_value'], '125')
                self.assertEqual(fitted['live_hour'], '折算 7,500 / 时')
                self.assertEqual((fitted['total'],fitted['minute']),('500','10'))
                self.assertEqual(fitted['current_value'], '—')
                self.assertEqual(self.configured_text(view.panel_canvas, 'session_state'),'已恢复')
                self.assertIn('等待新读数',self.configured_text(view.panel_canvas,'detail'))
                self.assertIn('离线时间不计入', self.configured_text(view.panel_canvas, 'detail'))
                for seconds in (120,300,1800):
                    self.assertEqual(fitted[f'window_{seconds}'], '125')
                    self.assertEqual(self.configured_text(view.panel_canvas,f'caption_{seconds}'),
                                     '已恢复')
                    view.panel_canvas.itemconfigure.assert_any_call(
                        f'fill_{seconds}',state='normal',fill=MIDNIGHT['warning'])
                view.backend.tracker.blocked = True
                self.assertEqual(view.state()[0], '需要确认')

    def test_saved_zero_totals_remain_visible_without_current_exp(self):
        view, snapshot = self.make_orb()
        snapshot.update(display_state='restored',display_age_seconds=None)
        view.backend.tracker.previous = None
        view.backend.tracker.total = 0
        view.backend.tracker.elapsed = 0
        view.backend.tracker.rates = lambda: (0,0)
        view.backend.records = [dict(total=0)]
        view.paint_panel()
        fitted = self.fitted_values(view)
        self.assertEqual((fitted['total'],fitted['minute']), ('0','0'))
        self.assertEqual(fitted['current_value'], '—')

    def test_restored_orb_and_panel_return_to_live_color_and_state(self):
        view, snapshot = self.make_orb(value=125,state='ready',observed=60)
        snapshot.update(display_state='restored',display_age_seconds=None)
        view.backend.fresh_reading = lambda: False
        font = Mock()
        font.measure.return_value = 20
        font.metrics.return_value = 10
        with patch('orb.tkfont.Font',return_value=font):
            view.paint_ball()
            self.assertEqual(self.configured_text(view.canvas,'ball_value'),'125')
            view.canvas.itemconfigure.assert_any_call(
                'ball_value',text='125',font=('Bahnschrift',-21),
                anchor='sw',fill='#f2d5a1')
            view.canvas.reset_mock()
            snapshot['display_state'] = 'live'
            view.backend.fresh_reading = lambda: True
            view.paint_ball()
            self.assertEqual(self.configured_text(view.canvas,'ball_value'),'125')
            view.canvas.itemconfigure.assert_any_call(
                'ball_value',text='125',font=('Bahnschrift',-21),
                anchor='sw',fill='#f0f8ff')
        view.paint_panel()
        self.assertEqual(view.state()[0],'识别正常')
        self.assertTrue(any(c.args==('live_value',) and c.kwargs.get('fill')==MIDNIGHT['ink']
                            for c in view.panel_canvas.itemconfigure.call_args_list))
        self.assertNotIn('已恢复',self.configured_text(view.panel_canvas,'detail'))

    def test_main_curve_uses_saved_60_second_values_with_fixed_time_axis(self):
        view, _ = self.make_orb()
        view.paint_minutes = Orb.paint_minutes.__get__(view)
        view.backend.history.active = 'session'
        view.minute_cache_time = 200
        view.minute_cache_session = 'session'
        view.minute_cache = []
        view.chart_cache_day=local_day(1000)
        view.day_curve=[]
        view.bar_caption = 'bar_caption'
        view.sample_cache = [dict(timestamp=999, realtime=900, rolling60=100,
                                 rate_state='ready', rate_observed_seconds=60,
                                 window_algorithm='observed_window_v1')]
        with patch('orb.time.time', return_value=1000), \
                patch('orb.time.monotonic', return_value=200), patch('ui_midnight.plot') as draw:
            view.paint_minutes()
        curve = next(call for call in draw.call_args_list if call.kwargs.get('tag') == 'curve')
        self.assertEqual(curve.args[2][0]['value'], 100)
        self.assertEqual(curve.kwargs['time_range'], (940, 1000))


class RateProgressTests(unittest.TestCase):
    def snapshot(self, observed, value=1234, state='warming', **extra):
        return dict(state='ready', windows={'120':dict(value=value,state=state,
                                                      observed_seconds=observed)}, **extra)

    def test_coverage_comes_from_observed_seconds_not_value_or_refresh_age(self):
        snapshot = self.snapshot(89.9, value=987654321)
        result = rate_window_display(snapshot, 120)
        self.assertAlmostEqual(result['ratio'], 89.9/120)
        self.assertEqual(result['caption'], '采样 01:29/02:00')
        self.assertEqual(result['number'], '9.88亿')
        snapshot['windows']['120']['value'] = 0
        self.assertEqual(rate_window_display(snapshot, 120)['ratio'], result['ratio'])
        snapshot.update(display_state='retrying',display_age_seconds=.2)
        frozen = rate_window_display(snapshot, 120)
        snapshot['display_age_seconds'] = 1.9
        self.assertEqual(rate_window_display(snapshot, 120), frozen)
        self.assertEqual(frozen['ratio'], result['ratio'])
        self.assertEqual(frozen['caption'], '重试 · 已采样 01:29')

    def test_near_full_is_not_rounded_to_full_and_overshoot_clamps(self):
        result = rate_window_display(self.snapshot(119.999), 120)
        self.assertLess(result['ratio'], 1)
        self.assertEqual(result['caption'], '采样 01:59/02:00')
        for observed in (120, 120.1, 300):
            with self.subTest(observed=observed):
                result = rate_window_display(self.snapshot(observed,state='ready'), 120)
                self.assertEqual(result['ratio'], 1)
                self.assertEqual(result['caption'], '')
                self.assertEqual(result['number'], '1,234')

    def test_paused_or_stale_parent_and_sample_clear_existing_coverage(self):
        for state in ('stale', 'paused'):
            with self.subTest(state=state):
                snapshot = self.snapshot(120,state='ready')
                snapshot['state'] = state
                result = rate_window_display(snapshot, 120)
                self.assertEqual(result['ratio'], 0)
                self.assertEqual(result['number'], '—')
                self.assertFalse(result['available'])
                snapshot['state'] = 'ready'
                snapshot['windows']['120']['state'] = state
                self.assertEqual(rate_window_display(snapshot, 120)['ratio'], 0)
                # A parent transient must not revive a child explicitly marked
                # stale or paused, even if a historical duration is present.
                snapshot['state'] = 'unavailable'
                self.assertEqual(rate_window_display(snapshot, 120)['ratio'], 0)
        self.assertEqual(rate_window_display(self.snapshot(120,value=None), 120)['ratio'], 0)
        self.assertEqual(rate_window_display({}, 120)['number'], '—')

    def test_brief_unavailable_parent_or_sample_keeps_coverage_but_hides_number(self):
        for source in ('parent','sample'):
            with self.subTest(source=source):
                snapshot=self.snapshot(120,state='ready')
                if source=='parent':
                    snapshot['state']='unavailable'
                else:
                    snapshot['windows']['120']['state']='unavailable'
                result=rate_window_display(snapshot,120)
                self.assertEqual(result['ratio'],1)
                self.assertEqual(result['number'],'—')
                self.assertEqual(result['caption'],'采样中断')
                self.assertFalse(result['available'])
        empty=self.snapshot(0,value=None,state='unavailable')
        self.assertEqual(rate_window_display(empty,120)['ratio'],0)

    def test_baseline_empty_track_differs_from_confirmed_zero_exp(self):
        baseline = rate_window_display(self.snapshot(0,value=None), 120)
        self.assertEqual((baseline['number'],baseline['ratio']), ('—',0))
        self.assertEqual(baseline['caption'], '采样 00:00/02:00')
        zero = rate_window_display(self.snapshot(120,value=0,state='ready'), 120)
        self.assertEqual((zero['number'],zero['ratio']), ('0',1))
        self.assertEqual(rate_window_display(self.snapshot(-2), 120)['ratio'], 0)

    def test_retry_of_full_window_keeps_coverage_and_marks_value(self):
        result = rate_window_display(self.snapshot(120,state='ready',
                                                   display_state='retrying'),120)
        self.assertEqual(result['ratio'],1)
        self.assertEqual(result['number'],'1,234')
        self.assertEqual(result['caption'],'重试')

    def test_restored_coverage_is_frozen_and_live_state_clears_restore_marker(self):
        snapshot = self.snapshot(89.9,display_state='restored',display_age_seconds=None)
        result = rate_window_display(snapshot,120)
        self.assertTrue(result['restored'])
        self.assertFalse(result['retrying'])
        self.assertAlmostEqual(result['ratio'],89.9/120)
        self.assertEqual(result['caption'],'已恢复 · 采样01:29')
        self.assertEqual(rate_window_display(snapshot,120),result)
        self.assertEqual(snapshot['windows']['120']['observed_seconds'],89.9)
        snapshot['windows']['120'].update(observed_seconds=120,state='ready')
        self.assertEqual(rate_window_display(snapshot,120)['number'],'1,234')
        snapshot['display_state'] = 'live'
        live = rate_window_display(snapshot,120)
        self.assertFalse(live['restored'])
        self.assertEqual(live['number'],'1,234')
        self.assertEqual(live['caption'],'')


class TrendHintTests(unittest.TestCase):
    def test_saved_half_life_takes_precedence_over_new_default(self):
        row = dict(realtime=100, rate_algorithm='time_ewma_v1', rate_half_life=10)
        self.assertEqual(trend_hint([row], dict(half_life_seconds=30)),
                         '时间加权 · 半衰期 10 秒')

    def test_mixed_parameters_and_legacy_are_not_relabeled(self):
        rows = [dict(realtime=100, rate_algorithm='time_ewma_v1', rate_half_life=half)
                for half in (10, 30)]
        self.assertEqual(trend_hint(rows, dict(half_life_seconds=30)),
                         '半衰期不同的记录分段显示')
        self.assertIn('旧算法', trend_hint([dict(realtime=100)], {}))

    def test_empty_chart_describes_current_dynamic_parameter(self):
        self.assertEqual(trend_hint([], dict(half_life_seconds=30)),
                         '时间加权 · 半衰期 30 秒')


if __name__ == '__main__':
    unittest.main()
