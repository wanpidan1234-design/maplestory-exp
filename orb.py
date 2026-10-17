"""Transparent, draggable desktop companion for the screen-only EXP engine."""
import ctypes
import json
import time
import sys
import tkinter as tk
from pathlib import Path
from PIL import ImageTk
from app import App, CONFIG
from game_capture import get_monitors,choose_monitor,game_geometry,USER,wt
from history_view import HistoryWindow
from combat_view import CombatWindow
from multiscale_view import MultiscaleWindow
from history_store import local_day
from minute_chart import minute_buckets, recorded_minute_curve
from ui_theme import INK, MUTED, ACCENT, WARNING, SUCCESS, FONT, MIDNIGHT
from ui_charts import rate_plot_rows, rate_prefix
from ui_feedback import CanvasGains,DeltaWatch
from ui_levelup import LevelCelebration
from ui_orbit import OrbitEngine,orbital_surface
from ui_midnight import MidnightPanel, WIDTH as PANEL_WIDTH, HEIGHT as PANEL_HEIGHT
import tkinter.font as tkfont

KEY = '#010203'
PREFS = CONFIG.with_name('orb.json')


def install_startup_review(root, backend, companion, *, user_requested=False):
    """Arm one event after accepted game evidence; imported instances stay quiet."""
    if getattr(backend,'review_launch',None) is not None:
        return
    try:
        from startup_review import make_launch_context, emit_startup_async
        context = make_launch_context(Path(__file__).absolute().parent, CONFIG.parent,
                                      backend.history.path, user_requested=user_requested)
        backend.review_launch = context
        backend.review_ui_snapshot = companion.review_snapshot
        backend.review_on_game_sample = None
        backend.review_sample_fired = False

        def announce_maintenance():
            try:
                # Tk reads stay on this thread; the worker receives plain data.
                snapshot = companion.review_snapshot()
                backend.startup_review_thread = emit_startup_async(context, snapshot)
            except Exception as error:
                backend.startup_review_error = type(error).__name__

        def on_game_sample():
            if backend.review_sample_fired:
                return
            capture = backend.capture_info
            hwnd = capture.get('hwnd')
            if (not backend.running or not (backend.region and backend.region.get('game_window'))
                    or capture.get('source')!='screen' or type(hwnd) is not int or hwnd<=0
                    or not backend.fresh_reading() or backend.tracker.resume_pending
                    or capture.get('observed_at')!=backend.last_valid_monotonic):
                return
            # Latch before optional snapshot/dispatch work, including failures.
            # A failed dispatcher must not be retried every sampling tick.
            backend.review_sample_fired = True
            try:
                view = getattr(companion,'review_view',None)
                if view is not None:
                    view.notify_trigger(context['event_id'])
                snapshot = dict(companion.review_snapshot())
                snapshot['qualified_sample'] = dict(source='screen',hwnd=hwnd,
                    observed_at=backend.last_valid_monotonic,sampled_at=backend.last_valid_time)
                # Reuse the existing local status checkpoint so the independent
                # runner sees this accepted capture and launch identity first.
                if not backend.write_status():
                    raise OSError('Unable to publish the accepted game sample')
                from startup_review_runner import dispatch_async
                backend.startup_review_thread = dispatch_async(context,snapshot)
            except Exception as error:
                backend.startup_review_error = type(error).__name__

        if context['review_required']:
            backend.review_on_game_sample = on_game_sample
        else:
            root.after_idle(announce_maintenance)
    except Exception as error:
        # Optional review infrastructure cannot stop recording or startup.
        backend.startup_review_error = type(error).__name__


def compact(value):
    if value >= 100000000:
        return f'{value/100000000:.2f}亿'
    if value >= 10000:
        return f'{value/10000:.2f}万'
    return f'{value:,.0f}'


def rate_window_display(snapshot, seconds):
    """Show captured coverage, including explicitly held display snapshots."""
    sample = snapshot.get('windows', {}).get(str(seconds), {})
    state = sample.get('state', 'unavailable')
    if snapshot.get('state') not in ('warming', 'ready'):
        state = snapshot.get('state', 'unavailable')
    observed = min(seconds, max(0, sample.get('observed_seconds') or 0))
    value = sample.get('value')
    available = state in ('warming', 'ready') and value is not None and observed > 0
    retrying = available and snapshot.get('display_state') in ('retrying','held')
    restored = available and snapshot.get('display_state') == 'restored'
    interrupted = snapshot.get('display_state') == 'visibility_paused'
    number = rate_prefix(sample, observed < seconds or retrying or restored or interrupted) + compact(value) if available else '—'
    captured = int(observed)
    duration = f'{captured//60:02}:{captured%60:02}'
    if available:
        caption = '' if observed >= seconds else f'采样 {duration}/{seconds//60:02}:00'
        if interrupted:
            caption = '切屏暂停'
        elif retrying:
            label = '待更新' if snapshot.get('display_state')=='held' else '重试'
            caption = label if observed >= seconds else f'{label} · 已采样 {duration}'
        elif restored:
            caption = '已恢复' if observed >= seconds else f'已恢复 · 采样{duration}'
    elif interrupted:
        caption = '等待读数'
    elif state == 'warming' and observed == 0:
        caption = f'采样 00:00/{seconds//60:02}:00'
    else:
        caption = {'paused': '已暂停', 'stale': '采样中断'}.get(state,
                    '采样中断' if observed>0 else '等待采样')
    keep_progress=available or (state=='unavailable' and observed>0
                              and sample.get('state') not in ('paused','stale'))
    return dict(number=number, ratio=observed/seconds if keep_progress else 0,
                caption=caption, retrying=retrying, restored=restored,
                interrupted=interrupted, available=available)



class Orb:
    def __init__(self, root, backend):
        self.root, self.backend = root, backend
        backend.review_ui_snapshot = self.review_snapshot
        self.scale = max(1, root.winfo_fpixels('1i')/96)
        self.base_scale = self.scale
        self.sample_cache = []
        self.alpha = .58
        self.history = []
        self.dragged = False
        self.panel = None
        self.midnight_panel = None
        self.last_sample = 0
        self.last_record_count = 0
        self.last_good = 0
        self.closing = False
        self.game_monitor = None
        self.history_window = None
        self.combat_window = None
        self.multiscale_window = None
        self.gain_watch=DeltaWatch();self.panel_gains=None
        self.level_tracker=backend.tracker;self.seen_levels=getattr(backend.tracker,'confirmed_levels',0)
        self.level_started=None;self.panel_level=None
        self.level_transition={}
        backend.ui_animation_state = dict(triggers=0, last_error=None)
        self.minute_cache=[]
        self.minute_cache_time=0
        self.minute_cache_session=None
        self.config = {}
        try:
            self.config = json.loads(PREFS.read_text(encoding='utf-8'))
            self.alpha = max(.45, min(1, float(self.config.get('alpha', .58))))
            if self.config.get('orb_style') not in ('single-line-v1','orbit-c-v1'):self.alpha=min(self.alpha,.58)
        except (OSError, ValueError, TypeError):
            pass
        self.ball = tk.Toplevel(root)
        self.ball.title('MapleStory · 经验悬浮球')
        self.ball.overrideredirect(True)
        self.ball.attributes('-topmost', True)
        self.ball.attributes('-transparentcolor', KEY)
        self.ball.attributes('-alpha', self.alpha)
        size = self.p(138)
        self.ball.geometry(f'{size}x{size}')
        self.canvas = tk.Canvas(self.ball, width=size, height=size, bg=KEY, highlightthickness=0, cursor='hand2')
        self.canvas.pack()
        self.ball_image = ImageTk.PhotoImage(orbital_surface(size,KEY))
        self.canvas.create_image(0,0,image=self.ball_image,anchor='nw')
        self.ball_value = self.text(self.canvas,69,69,'等待识别',14,'#f4faff',anchor='center',bold=True)
        self.ball_unit = self.text(self.canvas,69,69,'/ 分',8,'#9ebbd1',anchor='sw',state='hidden')
        # No secondary text or floating numbers in the collapsed one-line orb.
        # Full metrics and gain animations remain in the expanded observer.
        self.ball_gains=CanvasGains(self.canvas,{},self.base_scale)
        self.ball_motion=OrbitEngine(self.canvas,self.base_scale)
        self.ball_level=LevelCelebration(self.canvas,self.ball_value,self.base_scale,on_finish=self.paint_ball,hide_items=(self.ball_unit,))
        self.canvas.bind('<Enter>',lambda event:self.ball_motion.set_hover(True))
        self.canvas.bind('<Leave>',lambda event:self.ball_motion.set_hover(False))

        self.canvas.bind('<ButtonPress-1>',self.drag_start)
        self.canvas.bind('<B1-Motion>',self.drag_move)
        self.canvas.bind('<ButtonRelease-1>',self.drag_end)
        self.canvas.bind('<Button-3>',self.context)
        self.ball.bind('<Escape>',lambda e:self.hide_panel())
        x = int(self.config.get('x',root.winfo_screenwidth()-size-self.p(35)))
        y = int(self.config.get('y',self.p(160)))
        self.place_ball(x,y)
        root.protocol('WM_DELETE_WINDOW',self.hide_settings)
        root.withdraw()
        from ui_review import ReviewStatusView
        self.review_view = ReviewStatusView(self)
        self.ball.after(500,self.update)
        self.ball.after(100,self.feedback_tick)

    def p(self,n):
        return round(n*self.scale)

    def text(self,canvas,x,y,text,size=12,color=INK,anchor='w',bold=False,**kw):
        return canvas.create_text(self.p(x),self.p(y),text=text,fill=color,anchor=anchor,font=(FONT,-self.p(size),'bold' if bold else 'normal'),**kw)

    def place_ball(self,x,y):
        size = self.p(138)
        monitor = choose_monitor(dict(left=x,top=y,width=size,height=size),get_monitors())
        x = max(monitor['left'],min(x,monitor['left']+monitor['width']-size))
        y = max(monitor['top'],min(y,monitor['top']+monitor['height']-size))
        self.position(self.ball,x,y)

    def position(self,window,x,y):
        window.update_idletasks()
        USER.SetWindowPos.argtypes = [wt.HWND,wt.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wt.UINT]
        hwnd = USER.GetAncestor(window.winfo_id(),2)
        USER.SetWindowPos(hwnd,None,int(x),int(y),0,0,0x0015)

    def drag_start(self,event):
        self.dragged = False
        self.origin = (event.x_root,event.y_root,self.ball.winfo_x(),self.ball.winfo_y())

    def drag_move(self,event):
        x,y,bx,by = self.origin
        dx,dy = event.x_root-x,event.y_root-y
        if abs(dx)+abs(dy)>self.p(5):
            self.dragged = True
            self.hide_panel()
            self.place_ball(bx+dx,by+dy)

    def drag_end(self,event):
        if not self.dragged:
            self.toggle_panel()
        self.save()

    def save(self):
        PREFS.parent.mkdir(parents=True,exist_ok=True)
        PREFS.write_text(json.dumps({'x':self.ball.winfo_x(),'y':self.ball.winfo_y(),'alpha':self.alpha,'orb_style':'orbit-c-v1'}),encoding='utf-8')

    def review_snapshot(self):
        """Lightweight UI metadata, not a screenshot or restoration guarantee."""
        windows = dict(orb=self.ball, panel=self.panel, settings=self.root)
        for name in ('history', 'combat', 'multiscale'):
            windows[name] = getattr(getattr(self, name+'_window', None), 'window', None)
        windows['review'] = getattr(getattr(self,'review_view',None),'window',None)
        snapshot = dict(captured_at=time.time(), running=bool(self.backend.running),
                        orb_alpha=self.alpha, orb_style='orbit-c-v1',
                        levelup_style='D', panel_style='midnight-b-v1',
                        combat_expanded=bool(self.midnight_panel and self.midnight_panel.expanded),
                        settings_open=bool(self.midnight_panel and self.midnight_panel.options),
                        scale=self.scale, windows={})
        for name, window in windows.items():
            info = dict(exists=False, visible=False)
            try:
                if window is not None and window.winfo_exists():
                    info.update(exists=True, visible=bool(window.winfo_viewable()),
                                state=window.state(), geometry=window.geometry(),
                                x=window.winfo_x(), y=window.winfo_y(),
                                width=window.winfo_width(), height=window.winfo_height())
            except tk.TclError:
                info['error'] = 'window_unavailable'
            snapshot['windows'][name] = info
        return snapshot

    def toggle_panel(self):
        if self.panel and self.panel.winfo_exists():
            self.hide_panel()
            return
        self.panel = tk.Toplevel(self.root)
        self.panel.title('\u7ecf\u9a8c\u89c2\u5bdf\u5668 \u00b7 \u5b9e\u65f6\u7edf\u8ba1')
        self.panel.overrideredirect(True)
        self.panel.attributes('-topmost',True)
        self.panel.attributes('-alpha',1.0)
        monitor = choose_monitor(dict(left=self.ball.winfo_x(),top=self.ball.winfo_y(),
            width=round(138*self.base_scale),height=round(138*self.base_scale)),get_monitors())
        self.scale = min(self.base_scale, (monitor['height']-50)/PANEL_HEIGHT,
                         (monitor['width']-24)/PANEL_WIDTH)
        self.midnight_panel = MidnightPanel(self,self.panel,self.scale,monitor)
        width,height = self.p(PANEL_WIDTH),self.p(PANEL_HEIGHT)
        x = self.ball.winfo_x()-width-self.p(12)
        if x<monitor['left']:
            x = self.ball.winfo_x()+round(150*self.base_scale)
        x = max(monitor['left'],min(x,monitor['left']+monitor['width']-width))
        y = max(monitor['top'],min(self.ball.winfo_y(),monitor['top']+monitor['height']-height-20))
        self.position(self.panel,x,y)
        self.paint_panel()
        self.ball_gains.clear()
        self.refresh_panel_gains()
        self.panel_level=LevelCelebration(self.panel_canvas,self.panel_title,self.scale,
            mode='panel',hide_items=(self.panel_subtitle,),
            background=MIDNIGHT['surface'],panel_fit='cover')
        if self.level_started is not None and time.monotonic()-self.level_started<LevelCelebration.DURATION:
            self.panel_level.play(self.level_started,**self.level_transition)

    def refresh_panel_gains(self):
        if self.panel_gains:
            self.panel_gains.close()
            self.panel_gains=None
        if self.midnight_panel:
            self.panel_gains=CanvasGains(self.panel_canvas,self.midnight_panel.gain_lanes(),
                self.scale,background=MIDNIGHT['surface'])

    def refit_panel(self,monitor,x,y):
        """Refit a dragged panel without restarting recording or celebrations."""
        previous=self.midnight_panel
        if not self.panel or previous is None:
            return
        state=dict(expanded=previous.expanded,settings_open=bool(previous.options),
            kill_scope=previous.kill_scope,kill_page=previous.kill_page,
            scroll=previous.canvas.yview()[0])
        active=self.panel_level if self.panel_level and self.panel_level.active else None
        celebration=(active.started,active.from_level,active.to_level) if active else None
        if self.panel_level:self.panel_level.close();self.panel_level=None
        if self.panel_gains:self.panel_gains.close();self.panel_gains=None
        previous.close()
        previous.canvas.destroy()
        self.scale=min(self.base_scale,(monitor['height']-50)/PANEL_HEIGHT,
                       (monitor['width']-24)/PANEL_WIDTH)
        view=self.midnight_panel=MidnightPanel(self,self.panel,self.scale,monitor)
        view.kill_scope=state['kill_scope'];view.kill_page=state['kill_page']
        view.controls['kill_scope'].configure(text=view.kill_scope)
        view.set_expanded(state['expanded'],refresh=False)
        self.paint_panel()
        self.refresh_panel_gains()
        if state['settings_open']:view.toggle_settings()
        view.canvas.yview_moveto(state['scroll'])
        self.panel.update_idletasks()
        x=max(monitor['left'],min(x,monitor['left']+monitor['width']-self.panel.winfo_width()))
        y=max(monitor['top'],min(y,monitor['top']+monitor['height']-self.panel.winfo_height()))
        self.position(self.panel,x,y)
        self.panel.attributes('-alpha',1.0)
        self.panel_level=LevelCelebration(self.panel_canvas,self.panel_title,self.scale,
            mode='panel',hide_items=(self.panel_subtitle,),
            background=MIDNIGHT['surface'],panel_fit='cover')
        if celebration:
            started,from_level,to_level=celebration
            self.panel_level.play(started,from_level=from_level,to_level=to_level)

    def set_opacity(self,value):
        self.alpha=max(.45,min(1,float(value)/100))
        self.ball.attributes('-alpha',self.alpha)
        if self.panel:
            self.panel.attributes('-alpha',1.0)
        self.save()

    def toggle_kill_scope(self):
        if self.midnight_panel:
            self.midnight_panel.toggle_kill_scope()

    def next_kill_page(self):
        if self.midnight_panel:
            self.midnight_panel.next_kill_page()

    def paint_combat_summary(self):
        if self.midnight_panel:
            self.midnight_panel.paint_combat()

    def hide_panel(self):
        if self.midnight_panel:self.midnight_panel.close();self.midnight_panel=None
        if self.panel_level:self.panel_level.close();self.panel_level=None
        if self.panel_gains:self.panel_gains.close();self.panel_gains=None
        if self.panel and self.panel.winfo_exists():
            self.panel.destroy()
        self.panel = None
        self.scale = self.base_scale

    def feedback_tick(self):
        if self.closing:return
        try:
            self._feedback_tick()
        except Exception as error:
            # Decorative feedback must not permanently stop upgrade watching.
            self.backend.ui_animation_state['last_error'] = f'{type(error).__name__}: {error}'[:500]
        finally:
            if not self.closing:self.ball.after(100,self.feedback_tick)

    def _feedback_tick(self):
        if self.closing:return
        b=self.backend;service=getattr(b,'combat',None)
        level_count=getattr(b.tracker,'confirmed_levels',0)
        if b.tracker is not self.level_tracker:
            self.level_tracker=b.tracker;self.seen_levels=level_count;self.level_started=None
            self.ball_level.clear()
            if self.panel_level:self.panel_level.clear()
        elif level_count>self.seen_levels:
            self.seen_levels=level_count
            event=getattr(getattr(b,'auto_level',None),'last_event',None)
            if event and event.get('confirmation_id')!=level_count:event=None
            self.celebrate_level(event,preview=False)
        totals=service.tracker.totals if service else {}
        token=(id(b.tracker),id(service.tracker) if service else None,getattr(b,'generation',None))
        enabled=b.running and not b.tracker.blocked
        scope_changed=token!=self.gain_watch.token
        values={'exp':b.tracker.total,'damage_dealt':totals.get('damage_dealt'),'damage_taken':totals.get('damage_taken')}
        changes=self.gain_watch.observe(values,token,enabled)
        if not enabled or scope_changed:
            self.ball_gains.clear()
            self.ball_motion.clear()
            if self.panel_gains:self.panel_gains.clear()
        for key,amount in changes.items():
            if self.panel_gains:self.panel_gains.push(key,amount)
            if key=='exp' and not self.ball_level.active and b.fresh_reading():
                self.ball_motion.pulse()
        self.ball_motion.set_running(enabled and b.fresh_reading() and not self.ball_level.active)
        state=getattr(b,'ui_animation_state',None)
        if isinstance(state,dict):
            for name,animator in (('orb',self.ball_level),('panel',self.panel_level)):
                if animator is not None:
                    diagnostics=getattr(animator,'diagnostics',None)
                    state[name]=diagnostics() if callable(diagnostics) else dict(active=animator.active,
                        last_frame=animator.last_frame,last_render_ms=animator.last_render_ms)

    def celebrate_level(self,event=None,preview=True):
        if event is None and preview:
            event=getattr(getattr(self.backend,'auto_level',None),'last_event',None) or {'from_level':61,'to_level':62}
        self.level_transition={key:event.get(key) for key in ('from_level','to_level')} if event else {}
        self.level_started=time.monotonic();self.ball_motion.clear()
        prior=getattr(self.backend,'ui_animation_state',{})
        state=dict(triggers=prior.get('triggers',0)+1, confirmation_id=(event or {}).get('confirmation_id'),
            transition=dict(self.level_transition),preview=preview,triggered_at=time.time(),last_error=None)
        self.backend.ui_animation_state=state
        for name,animator in (('orb',self.ball_level),('panel',self.panel_level)):
            if animator is not None:
                try:animator.play(self.level_started,**self.level_transition)
                except Exception as error:
                    animator.clear()
                    state['last_error']=f'{name}: {type(error).__name__}: {error}'[:500]

    def toggle(self):
        if self.backend.tracker.blocked:
            self.show_settings()
        elif not self.backend.region:
            self.backend.auto_game()
        else:
            self.backend.toggle()
        self.paint_panel()

    def show_settings(self):
        self.hide_panel()
        self.root.deiconify()
        self.root.lift()

    def show_history(self):
        self.hide_panel()
        if self.history_window and self.history_window.window.winfo_exists():
            self.history_window.window.deiconify()
            self.history_window.window.lift()
            self.history_window.refresh()
        else:
            self.history_window=HistoryWindow(self.root,self.backend.history)
            window=self.history_window.window
            window.update_idletasks()
            monitor=choose_monitor(dict(left=self.ball.winfo_x(),top=self.ball.winfo_y(),width=self.p(138),height=self.p(138)),get_monitors())
            self.position(window,monitor['left']+(monitor['width']-window.winfo_width())//2,monitor['top']+(monitor['height']-window.winfo_height())//2)

    def hide_settings(self):
        self.backend.save()
        self.root.withdraw()

    def show_combat(self):
        self.hide_panel()
        if self.combat_window and self.combat_window.window.winfo_exists():
            self.combat_window.window.deiconify();self.combat_window.window.lift();self.combat_window.refresh()
        else:
            self.combat_window=CombatWindow(self.root,self.backend.combat)
            window=self.combat_window.window;window.update_idletasks()
            monitor=choose_monitor(dict(left=self.ball.winfo_x(),top=self.ball.winfo_y(),width=self.p(138),height=self.p(138)),get_monitors())
            x=monitor['left']+(monitor['width']-window.winfo_width())//2
            y=monitor['top']+max(0,(monitor['height']-window.winfo_height())//2)
            try:
                game=game_geometry()['client']
                if game['left']+game['width']+window.winfo_width()+24<=monitor['left']+monitor['width']:
                    x=game['left']+game['width']+16
                elif game['left']-window.winfo_width()-16>=monitor['left']:
                    x=game['left']-window.winfo_width()-16
            except RuntimeError:pass
            self.position(window,x,y)

    def show_multiscale(self):
        self.hide_panel()
        if self.multiscale_window and self.multiscale_window.window.winfo_exists():
            self.multiscale_window.window.deiconify()
            self.multiscale_window.window.lift()
            self.multiscale_window.refresh()
        else:
            self.multiscale_window=MultiscaleWindow(self.root,self.backend)
            window=self.multiscale_window.window
            window.update_idletasks()
            monitor=choose_monitor(dict(left=self.ball.winfo_x(),top=self.ball.winfo_y(),width=self.p(138),height=self.p(138)),get_monitors())
            x=monitor['left']+max(0,(monitor['width']-window.winfo_width())//2)
            y=monitor['top']+max(0,(monitor['height']-window.winfo_height())//2)
            self.position(window,x,y)

    def context(self,event):
        menu = tk.Menu(self.ball,tearoff=False,bg=MIDNIGHT['raised'],fg=INK,activebackground=MIDNIGHT['selection'],activeforeground=INK,font=('Microsoft YaHei UI',10))
        menu.add_command(label='展开 / 收起统计',command=self.toggle_panel)
        menu.add_command(label='暂停 / 继续',command=self.toggle)
        menu.add_command(label='历史回看 / 每日总计',command=self.show_history)
        menu.add_command(label='多尺度分均',command=self.show_multiscale)
        menu.add_command(label='战斗伤害 / 血蓝消耗',command=self.show_combat)
        menu.add_command(label='预览升级动效（不计入统计）',command=self.celebrate_level)
        menu.add_command(label='新建记录',command=self.backend.reset)
        menu.add_command(label='导出本次记录',command=self.backend.export)
        menu.add_command(label='重新定位游戏',command=self.backend.auto_game)
        menu.add_command(label='框选经验栏…',command=self.backend.select)
        menu.add_command(label='识别设置…',command=self.show_settings)
        menu.add_command(label='花 Buff / 音乐提醒…',command=self.show_settings)
        menu.add_command(label='查看本次评审…',command=self.review_view.show_details)
        menu.add_separator()
        menu.add_command(label='退出',command=self.close)
        try:
            menu.tk_popup(event.x_root,event.y_root)
        finally:
            menu.grab_release()

    def state(self, snapshot=None):
        b = self.backend
        if b.tracker.blocked:
            return '需要确认',WARNING
        snapshot=b.display_rate_snapshot() if snapshot is None else snapshot
        if snapshot.get('display_state')=='visibility_paused':
            return '切屏暂停',WARNING
        if snapshot.get('display_state')=='restored':
            return '已恢复',WARNING
        if not b.running:
            return '已暂停',MUTED
        if getattr(getattr(b,'auto_level',None),'wait_since',None) is not None:
            return '核验变化',WARNING
        if snapshot.get('display_state')=='retrying':
            return '识别重试',WARNING
        if snapshot.get('display_state')=='held':
            return '等待读数',WARNING
        if not b.fresh_reading():
            return '等待识别',WARNING
        if snapshot['primary']['state']=='warming':
            return '采样中',WARNING
        return '识别正常',SUCCESS

    def paint_ball(self):
        b=self.backend;snapshot=b.display_rate_snapshot();state,color=self.state(snapshot)
        rate=snapshot['primary']
        retrying=snapshot.get('display_state')=='retrying'
        held=retrying or snapshot.get('display_state') in ('held','restored','visibility_paused')
        minute=rate['value']
        fresh=minute is not None
        # The expanded panel may have its own fit-to-screen scale. The orb
        # always keeps its original DPI and bounds, including large values.
        if not self.ball_level.active:
            s=self.base_scale;text=rate_prefix(rate,held).strip()+compact(minute) if fresh else state;size=21 if fresh else 14
            family='Bahnschrift' if fresh else FONT
            f=tkfont.Font(family=family,size=-round(size*s));unit=tkfont.Font(family=FONT,size=-round(7*s))
            extra=unit.measure('/ 分')+3*s if fresh else 0
            while f.measure(text)+extra>72*s and size>8:
                size-=1;f.configure(size=-round(size*s))
            if fresh:
                width=f.measure(text)+extra;left=69*s-width/2;baseline=69*s+(f.metrics('ascent')-f.metrics('descent'))/2
                self.canvas.coords(self.ball_value,left,baseline+f.metrics('descent'))
                self.canvas.itemconfigure(self.ball_value,text=text,font=(family,-round(size*s)),anchor='sw',fill='#f2d5a1' if held else '#f0f8ff')
                self.canvas.coords(self.ball_unit,left+f.measure(text)+3*s,baseline+unit.metrics('descent'))
                self.canvas.itemconfigure(self.ball_unit,state='normal',font=(FONT,-round(7*s)))
            else:
                self.canvas.coords(self.ball_value,69*s,69*s)
                self.canvas.itemconfigure(self.ball_value,text=text,font=(family,-round(size*s)),anchor='center',fill='#c3dcec')
                self.canvas.itemconfigure(self.ball_unit,state='hidden')
        if not fresh and self.ball_motion.pulse_started is not None:self.ball_motion.clear()
        reporter=getattr(b,'diagnostic_surface',None)
        if callable(reporter):
            reporter('orb',snapshot,reason='animation_active' if self.ball_level.active else 'configured')

    def paint_panel(self):
        if not self.panel or not self.midnight_panel:
            return
        snapshot=self.backend.display_rate_snapshot()
        self.midnight_panel.paint(snapshot,rate_window_display)
        reporter=getattr(self.backend,'diagnostic_surface',None)
        if callable(reporter):reporter('panel',snapshot)
        self.paint_minutes()

    def paint_minutes(self):
        if not self.midnight_panel:
            return
        b=self.backend
        now=time.monotonic()
        day=local_day(time.time())
        if (now-self.minute_cache_time>=5 or self.minute_cache_session!=b.history.active
                or getattr(self,'chart_cache_day',None)!=day):
            self.sample_cache=b.history.recent_rate_samples(b.history.active,time.time()-60) if b.history.active else []
            minutes=b.history.day_minutes(day,b.history.active) if b.history.active else []
            self.minute_cache=minute_buckets(minutes)[-12:]
            self.day_curve=recorded_minute_curve(b.history.day_minutes(day))
            self.chart_cache_day=day
            self.minute_cache_time=now
            self.minute_cache_session=b.history.active
        end=time.time();cutoff=end-60
        curve=rate_plot_rows([r for r in self.sample_cache if cutoff<=r['timestamp']<=end],window_seconds=60)
        bars=[dict(timestamp=r['timestamp'],value=r['gain'],observed=r['observed']) for r in self.minute_cache]
        self.midnight_panel.paint_minutes(curve,bars,(cutoff,end),self.day_curve)

    def update(self):
        if self.closing:
            return
        request = CONFIG.with_name('shutdown.request')
        if request.exists():
            request.unlink()
            self.close()
            return
        b = self.backend
        monitor = b.capture_info.get('monitor')
        if monitor and monitor.get('index')!=self.game_monitor:
            self.game_monitor = monitor.get('index')
            expanded = self.panel is not None
            self.hide_panel()
            self.place_ball(monitor['left']+monitor['width']-self.p(173),monitor['top']+self.p(160))
            if expanded:
                self.toggle_panel()
        count = len(b.records)
        if count<self.last_record_count:
            self.history = []
            self.last_good = 0
        if count>self.last_record_count and b.fresh_reading():
            self.last_good = time.monotonic()
            value=b.primary_rate_snapshot()['value']
            if value is not None:self.history = (self.history+[value])[-60:]
        self.last_record_count = count
        self.paint_ball()
        self.paint_panel()
        self.review_view.tick()
        self.ball.after(700,self.update)

    def close(self):
        self.closing = True
        self.review_view.close()
        if self.multiscale_window:self.multiscale_window.close()
        self.ball_gains.close()
        self.ball_motion.close()
        self.ball_level.close()
        if self.panel_level:self.panel_level.close()
        if self.panel_gains:self.panel_gains.close()
        if self.midnight_panel:self.midnight_panel.close()
        self.save()
        self.backend.close()


if __name__=='__main__':
    # Multiple companions would record the same gains twice in daily history.
    kernel=ctypes.windll.kernel32
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wt.BOOL,wt.LPCWSTR]
    kernel.CreateMutexW.restype=wt.HANDLE
    instance_mutex=kernel.CreateMutexW(None,False,'Local\\MapleExpTrackerHistoryV1')
    if kernel.GetLastError()==183:
        sys.exit(0)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError,OSError):
        pass
    root = tk.Tk()
    root.withdraw()
    backend = App(root)
    orb = Orb(root,backend)
    if '--paused' in sys.argv:
        backend.status.set('已暂停；点击继续统计开始记录。')
    else:
        root.after(800,backend.auto_game)
    if '--expanded' in sys.argv:
        root.after(1000,orb.toggle_panel)
    if '--history' in sys.argv:
        root.after(1400,orb.show_history)
    if '--combat' in sys.argv:
        root.after(1500,orb.show_combat)
    # A public clone has no personal policy or authorization for auto-development.
    if '--no-startup-review' not in sys.argv and Path(__file__).with_name('STARTUP_REVIEW.md').is_file():
        install_startup_review(root, backend, orb, user_requested='--review-once' in sys.argv)
    root.mainloop()
