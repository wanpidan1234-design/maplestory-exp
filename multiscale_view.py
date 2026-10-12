"""Non-modal view of observed EXP rates at several time scales."""
import math
import time
import tkinter as tk
from tkinter import ttk

from ui_charts import RATE_VIEWS, plot, rate_plot_rows, rate_prefix
from ui_theme import WARNING, configure_history, configure_window, SURFACE, MIDNIGHT


WINDOW_SECONDS = (60, 120, 300, 600, 1800)
STATE_LABELS = {
    'warming': '采样中', 'ready': '满窗口', 'paused': '已暂停',
    'stale': '读数中断', 'unavailable': '等待采样',
}


def trend_hint(rows, snapshot):
    """Describe saved smoothing metadata without relabeling older points."""
    saved = [row for row in rows if row.get('realtime') is not None]
    half_lives = {row.get('rate_half_life') for row in saved
                  if row.get('rate_algorithm') == 'time_ewma_v1'}
    legacy = any(row.get('rate_algorithm') != 'time_ewma_v1' for row in saved)
    if legacy:
        return '含旧算法记录 · 按各点保存值显示'
    if len(half_lives) > 1:
        return '半衰期不同的记录分段显示'
    half_life = next(iter(half_lives), snapshot.get('half_life_seconds'))
    return f'时间加权 · 半衰期 {half_life:g} 秒' if isinstance(half_life, (int, float)) else '时间加权 · 半衰期待记录'


def window_row(snapshot, window_seconds):
    """Format one window without replacing a missing value with another rate."""
    sample = snapshot.get('windows', {}).get(str(window_seconds), {})
    state = sample.get('state', 'unavailable')
    if snapshot.get('state') not in ('warming', 'ready'):
        state = snapshot.get('state', 'unavailable')
    observed = min(window_seconds, max(0, sample.get('observed_seconds') or 0))
    value = sample.get('value')
    valid = value is not None and observed > 0 and state in ('warming', 'ready')
    retrying = snapshot.get('display_state') in ('retrying','held')
    restored = snapshot.get('display_state') == 'restored'
    interrupted = snapshot.get('display_state') == 'visibility_paused'
    number = rate_prefix(sample, observed < window_seconds or retrying or restored or interrupted) + f'{value:,.0f}' if valid else '—'
    # Tenths avoid displaying a full window before the first complete one exists.
    coverage = f'{math.floor(observed * 10) / 10:.1f} / {window_seconds:g} 秒'
    label = ('切屏暂停' if interrupted else '已恢复' if restored and valid else '待更新' if snapshot.get('display_state')=='held' and valid else '识别重试' if retrying and valid
             else STATE_LABELS.get(state, STATE_LABELS['unavailable']))
    if sample.get('has_gaps') and valid and not (retrying or restored or interrupted):
        label='尾段未计'
    return (f'{window_seconds // 60} min', number, coverage, label)


class MultiscaleWindow:
    REFRESH_MS = 800
    CHART_REFRESH_SECONDS = 2

    def __init__(self, root, backend):
        self.backend = backend
        self.scale = max(1, root.winfo_fpixels('1i') / 96)
        self._closed = False
        self._after_id = None
        self._cache_session = object()
        self._cache_time = None
        self._chart_rows = []
        self.window = tk.Toplevel(root)
        self.window.title('经验观察器 · 多尺度分均')
        configure_window(self.window)
        self.window.attributes('-topmost', True)
        self.window.geometry(f'{self.p(720)}x{self.p(680)}')
        self.window.minsize(self.p(640), self.p(600))
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.window.bind('<Escape>', lambda event: self.close())
        self.window.bind('<Destroy>', self._on_destroy, add='+')

        style = ttk.Style()
        configure_history(style, self.scale)
        style.configure('Multiscale.History.Treeview', rowheight=self.p(34))
        outer = ttk.Frame(self.window, padding=self.p(22), style='History.TFrame')
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='多尺度分均', style='HistoryTitle.TLabel').pack(anchor='w')
        ttk.Label(
            outer, text='按有效采样时间计算；切屏、识别中断与重启接续进度，手动暂停或新建记录重新采样。',
            style='History.TLabel', wraplength=self.p(580),
        ).pack(anchor='w', pady=(self.p(8), self.p(16)))

        self.table = ttk.Treeview(
            outer, columns=('window', 'rate', 'coverage', 'state'),
            show='headings', height=5, selectmode='none',
            style='Multiscale.History.Treeview',
        )
        for key, title, width, anchor in (
            ('window', '观测窗口', 90, 'center'),
            ('rate', '分均经验 · EXP/min', 190, 'e'),
            ('coverage', '已采样 / 窗口', 205, 'center'),
            ('state', '状态', 105, 'center'),
        ):
            self.table.heading(key, text=title)
            self.table.column(key, width=self.p(width), minwidth=self.p(width - 20), anchor=anchor)
        self.table.pack(fill='x')
        self.table.tag_configure('held',foreground=WARNING)
        for seconds in WINDOW_SECONDS:
            self.table.insert('', 'end', iid=str(seconds), values=window_row({}, seconds))
        self.table_hint = tk.StringVar()
        ttk.Label(
            outer, textvariable=self.table_hint,
            style='History.TLabel', wraplength=self.p(580),
        ).pack(anchor='w', pady=(self.p(8), self.p(18)))

        controls = ttk.Frame(outer, style='History.TFrame')
        controls.pack(fill='x')
        ttk.Label(controls, text='曲线尺度', style='History.TLabel').pack(side='left')
        self.selection = tk.StringVar(value='1 min')
        self.selector = ttk.Combobox(
            controls, textvariable=self.selection, values=tuple(RATE_VIEWS),
            state='readonly', width=13, style='History.TCombobox',
        )
        self.selector.pack(side='left', padx=self.p(12))
        self.selector.bind('<<ComboboxSelected>>', lambda event: self.draw_chart())
        self.chart_title = tk.StringVar()
        ttk.Label(outer, textvariable=self.chart_title, style='HistorySummary.TLabel').pack(
            anchor='w', pady=(self.p(16), self.p(4)))
        self.chart_hint = tk.StringVar()
        ttk.Label(outer, textvariable=self.chart_hint, style='History.TLabel').pack(anchor='w')
        self.chart = tk.Canvas(outer, bg=SURFACE, height=self.p(180), highlightthickness=0)
        self.chart.pack(fill='both', expand=True, pady=(self.p(8), 0))
        self.chart.bind('<Configure>', lambda event: self.draw_chart())

        self.refresh()
        self._after_id = self.window.after(self.REFRESH_MS, self._poll)

    def p(self, value):
        return round(value * self.scale)

    def _poll(self):
        self._after_id = None
        if self._closed:
            return
        self.refresh()
        self._after_id = self.window.after(self.REFRESH_MS, self._poll)

    def refresh(self):
        if self._closed:
            return
        snapshot = self.backend.display_rate_snapshot()
        for seconds in WINDOW_SECONDS:
            row = window_row(snapshot, seconds)
            held = row[3] in ('已恢复','识别重试','切屏暂停','待更新')
            self.table.item(str(seconds), values=row, tags=('held',) if held else ())
        hint = ('切屏暂停 · 已保留分均与采样进度；等待新读数，离屏时间与经验不计入。'
                if snapshot.get('display_state') == 'visibility_paused' and snapshot.get('visibility_preserved') else
                '切屏暂停 · 等待新的有效读数，离屏时间与经验不计入。'
                if snapshot.get('display_state') == 'visibility_paused' else
                '已恢复上次分均与采样进度 · 等待新读数，离线时间不计入。'
                if snapshot.get('display_state') == 'restored' else
                '重试中 · 暂显 ' + f"{snapshot['display_age_seconds']:.1f} 秒前的有效分均"
                if snapshot.get('display_state') == 'retrying'
                else '等待读数 · 保留 ' + f"{snapshot['display_age_seconds']:.0f} 秒前的分均与采样进度"
                if snapshot.get('display_state') == 'held'
                else '≥ 为缺少升级尾段的收益下限。')
        self.table_hint.set(hint)
        reporter=getattr(self.backend,'diagnostic_surface',None)
        if callable(reporter):reporter('multiscale',snapshot)
        session = self.backend.history.active
        now = time.monotonic()
        if (session != self._cache_session or self._cache_time is None
                or now - self._cache_time >= self.CHART_REFRESH_SECONDS):
            self._chart_rows = self.backend.history.recent_rate_samples(
                session, time.time() - max(WINDOW_SECONDS)) if session else []
            self._cache_session = session
            self._cache_time = now
            self.draw_chart()

    def draw_chart(self):
        if self._closed:
            return
        selected = self.selection.get()
        seconds = RATE_VIEWS[selected]
        span = max(60, seconds or 0)
        end = time.time()
        start = end - span
        saved = [row for row in self._chart_rows if start <= row['timestamp'] <= end]
        self.chart_title.set(
            '速度趋势 · 经验/分' if seconds is None
            else f'{selected} 观测窗口分均 · 经验/分')
        description = trend_hint(saved, self.backend.rate_snapshot()) if seconds is None else '每点为当时保存的分均值'
        if any(row.get('rate_has_gaps') if seconds is None else row.get('window_gap_mask') for row in saved):
            description+='；暖色虚线为已确认收益下限'
        self.chart_hint.set(f'横轴：最近 {span // 60} 分钟 · {description}')
        rows = rate_plot_rows(saved, seconds)
        width, height = max(self.p(300), self.chart.winfo_width()), max(self.p(110), self.chart.winfo_height())
        plot(self.chart, (0, 0, width / self.scale, height / self.scale), rows,
             scale=self.scale, time_range=(start, end),palette=MIDNIGHT)

    def _cancel_timer(self):
        if self._after_id is not None:
            self.window.after_cancel(self._after_id)
            self._after_id = None

    def _on_destroy(self, event):
        if event.widget is self.window:
            self._closed = True
            self._cancel_timer()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._cancel_timer()
        self.window.destroy()
