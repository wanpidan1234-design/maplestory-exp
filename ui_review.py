"""Local review feedback beside the C orb and inside the B observer.

The existing Orb update drives this view. A short-lived worker only reads the
current event's bounded files; it never starts, retries or polls a model.
"""
import ctypes
from datetime import datetime
import itertools
import json
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
import uuid

from ui_theme import ACCENT, FONT, INK, LINE, MIDNIGHT, MUTED, PALE, SURFACE, configure_history, configure_window


ROOT = Path(__file__).absolute().parent
TERMINAL = {'completed', 'failed', 'skipped', 'uncertain'}
LABELS = {
    'idle': ('后台评审', '后台评审', ''),
    'triggered': ('后台评审已触发', '评审已触发', '评审已触发'),
    'starting': ('正在启动后台评审', '评审启动中', '评审启动中'),
    'running': ('后台评审进行中', '评审中', '评审中'),
    'completed': ('后台评审已完成', '查看评审', '评审完成'),
    'failed': ('后台评审未完成', '评审未完成', '评审未完成'),
    'skipped': ('本次未开展评审', '查看评审', '评审未启动'),
    'uncertain': ('评审状态待确认', '查看评审', '评审待确认'),
}


def event_uuid(value):
    try:
        return value if isinstance(value, str) and str(uuid.UUID(value)) == value else None
    except (ValueError, AttributeError):
        return None


def _json(path, limit=65536):
    try:
        with path.open('rb') as source:
            raw = source.read(limit + 1)
        if len(raw) > limit:
            return {}
        value = json.loads(raw.decode('utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}


def _text(value, limit=4096):
    return value[:limit] if isinstance(value, str) else ''


def _error_copy(error):
    if error in ('PermissionError', 'SandboxError'):
        return '后台权限不足，本次评审未完成。经验记录仍会继续。'
    if error == 'FileNotFoundError':
        return '未能取得完整评审结果，请查看本次记录。经验记录仍会继续。'
    if error == 'TimeoutExpired':
        return '本次评审超时，已停止。经验记录仍会继续。'
    return '本次后台评审未完成，请查看记录中的原因。经验记录仍会继续。'


def read_review_status(directory, event_id, now=None):
    """Read only one UUID's receipt and the beginning of its CLI event stream."""
    if not event_uuid(event_id):
        raise ValueError('invalid review event')
    directory = Path(directory)
    dispatch = _json(directory / 'dispatch.json')
    receipt = _json(directory / 'runner.json')
    result = _json(directory / 'result.json')
    for value in (dispatch, receipt, result):
        if value and value.get('event_id') != event_id:
            value.clear()
    if not receipt:
        candidates = list(itertools.islice(directory.glob('skipped-*.json'), 8))
        for path in candidates:
            skipped = _json(path)
            if skipped.get('event_id') == event_id:
                receipt = skipped
                break
    session = event_uuid(receipt.get('thread_id'))
    started = session is not None
    if not session:
        try:
            # thread.started is emitted before tool output, so large later logs
            # never need to enter the UI process's memory.
            with (directory / 'events.jsonl').open('rb') as source:
                raw = source.read(16384)
            for line in raw.decode('utf-8', errors='replace').splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get('type') == 'thread.started':
                    session = event_uuid(item.get('thread_id'))
                    started = session is not None
                    break
        except OSError:
            pass
    status = receipt.get('status')
    outcome = receipt.get('outcome') or result.get('outcome')
    summary = (_text(receipt.get('user_message')) or _text(receipt.get('summary'))
               or _text(receipt.get('error_message')) or _text(result.get('summary')))
    if status == 'completed':
        state = 'failed' if outcome == 'blocked' else 'completed'
        summary = summary or ('评审已完成，本次无需修改。' if outcome == 'no_change' else '本次评审已完成。')
    elif status in ('failed', 'timed_out', 'lease_lost', 'blocked'):
        state = 'failed'
        summary = summary or _error_copy('TimeoutExpired' if status == 'timed_out' else receipt.get('error'))
    elif status in ('skipped_inactive', 'busy', 'not_pending'):
        state = 'skipped'
        summary = ('已有评审在处理，本次不重复启动。' if status == 'busy' else
                   '本次没有启动新的评审。经验记录不受影响。')
    elif started or status in ('running', 'reviewing'):
        state = 'running'
        summary = '正在检查本次游戏体验和运行证据。你可以继续游戏。'
    elif receipt or dispatch:
        state = 'starting'
        summary = '已接收本次有效游戏采样，正在启动后台评审。'
    else:
        state = 'triggered'
        summary = '已识别到有效游戏画面，正在交给后台评审。'
    if state not in TERMINAL:
        stamp = receipt.get('at') or dispatch.get('at')
        try:
            age = (time.time() if now is None else now) - datetime.fromisoformat(stamp).timestamp()
            if age > 1560:
                state = 'uncertain'
                summary = '暂未收到完成回执，请查看本次记录；不会自动重复启动。'
        except (ValueError, TypeError):
            pass
    return dict(event_id=event_id, state=state, summary=summary, session_id=session,
                model_started=started, outcome=outcome)


class ReviewStatusView:
    """Tk owns the notices; file readers return plain dictionaries only."""
    def __init__(self, orb):
        self.orb = orb
        self.closed = False
        self.event_id = None
        self.data = dict(state='idle', summary='首个可信游戏采样入账后，后台评审会自动开始。', session_id=None)
        self.worker = None
        self.inbox = queue.SimpleQueue()
        self.next_read = 0
        self.read_finished = False
        self.announce = False
        self.pending_toast = None
        self.toast_until = 0
        self.notice = None
        self.notice_canvas = None
        self.notice_mode = None
        self.notice_position = None
        self.window = None
        self.details_text = None
        self.details_signature = None
        self.last_error = None

    @property
    def footer_text(self):
        return LABELS[self.data['state']][1]

    @property
    def footer_color(self):
        if self.data['state'] in ('failed', 'uncertain'):
            return MIDNIGHT['warning']
        if self.data['state'] == 'completed':
            return MIDNIGHT['success']
        return MIDNIGHT['accent'] if self.data['state'] != 'idle' else MIDNIGHT['muted']

    def notify_trigger(self, event_id):
        if self.closed or not event_uuid(event_id) or (event_id == self.event_id and self.announce):
            return
        self.event_id = event_id
        self.announce = True
        self.read_finished = False
        self.next_read = 0
        self.data = dict(event_id=event_id, state='triggered',
                         summary='已识别到有效游戏画面，正在交给后台评审。', session_id=None)
        self.pending_toast = 'triggered'

    def _accept(self, data):
        before = self.data['state']
        self.data = data
        self.read_finished = data['state'] in TERMINAL
        if self.announce and data['state'] != before and data['state'] in ('running', 'completed', 'failed', 'skipped', 'uncertain'):
            self.pending_toast = data['state']

    def _read_async(self):
        event_id = self.event_id
        directory = ROOT / 'work' / 'startup-reviews' / event_id
        def read():
            try:
                self.inbox.put(read_review_status(directory, event_id))
            except Exception:
                self.inbox.put(dict(event_id=event_id, state='uncertain',
                                    summary='暂时无法读取评审记录，请稍后查看。', session_id=None))
        self.worker = threading.Thread(target=read, name='review-ui-files', daemon=True)
        self.worker.start()

    def tick(self):
        # Optional review feedback must never stop the observer's own update.
        try:
            self._tick()
        except Exception as error:
            self.last_error = type(error).__name__

    def _tick(self):
        if self.closed:
            return
        backend = self.orb.backend
        launch = getattr(backend, 'review_launch', {}) or {}
        if not self.event_id:
            if getattr(backend, 'review_sample_fired', False) and event_uuid(launch.get('event_id')):
                self.notify_trigger(launch['event_id'])
            elif event_uuid(launch.get('parent_review_id')):
                # A maintenance restart may show its parent's result, but must
                # not announce a new trigger or start another consumer.
                self.event_id = launch['parent_review_id']
                self.read_finished = False
        while not self.inbox.empty():
            data = self.inbox.get_nowait()
            if data.get('event_id') == self.event_id:
                self._accept(data)
        error = (getattr(backend, 'startup_review_error', None)
                 or getattr(getattr(backend, 'startup_review_thread', None), 'review_error', None))
        if error and self.event_id and self.data['state'] not in TERMINAL:
            self._accept(dict(event_id=self.event_id, state='failed', summary=_error_copy(error), session_id=None))
        now = time.monotonic()
        if (self.event_id and not self.read_finished and now >= self.next_read
                and (self.worker is None or not self.worker.is_alive())):
            self.next_read = now + 1
            self._read_async()
        self._paint_notice(now)
        self._paint_footer()
        self._paint_details()

    def _paint_footer(self):
        panel = getattr(self.orb, 'midnight_panel', None)
        button = getattr(panel, 'controls', {}).get('review') if panel else None
        if button is not None:
            try:
                button.configure(text=self.footer_text, fg=self.footer_color)
            except tk.TclError:
                pass

    def _level_active(self):
        return any(getattr(getattr(self.orb, name, None), 'active', False)
                   for name in ('ball_level', 'panel_level'))

    def _ensure_notice(self):
        if self.notice is not None:
            return
        window = self.notice = tk.Toplevel(self.orb.root)
        window.withdraw()
        window.overrideredirect(True)
        window.attributes('-topmost', True)
        window.attributes('-alpha', 1.0)
        window.configure(bg=MIDNIGHT['surface'])
        self.notice_canvas = tk.Canvas(window, bg=MIDNIGHT['surface'], highlightthickness=1,
                                       highlightbackground=MIDNIGHT['line'], cursor='hand2')
        self.notice_canvas.pack(fill='both', expand=True)
        self.notice_canvas.bind('<ButtonRelease-1>', lambda event: self.show_details())
        window.update_idletasks()
        try:
            user = ctypes.windll.user32
            pointer = ctypes.c_void_p
            user.GetAncestor.argtypes = [pointer, ctypes.c_uint]
            user.GetAncestor.restype = pointer
            user.GetWindowLongPtrW.argtypes = [pointer, ctypes.c_int]
            user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            user.SetWindowLongPtrW.argtypes = [pointer, ctypes.c_int, ctypes.c_ssize_t]
            hwnd = user.GetAncestor(window.winfo_id(), 2)
            style = user.GetWindowLongPtrW(hwnd, -20)
            user.SetWindowLongPtrW(hwnd, -20, style | 0x08000000 | 0x00000080)
        except (AttributeError, OSError, tk.TclError):
            # Non-Windows tests can still inspect the inert Tk surface.
            pass

    def _paint_notice(self, now):
        state = self.data['state']
        if state == 'idle' or self._level_active():
            if self.notice is not None:
                self.notice.withdraw()
            if self.toast_until > now:
                self.pending_toast = state
                self.toast_until = 0
            return
        self._ensure_notice()
        if self.pending_toast:
            self.toast_until = now + 5
            self.pending_toast = None
        toast = now < self.toast_until
        scale = self.orb.base_scale
        width, height = (248, 52) if toast else (86, 27)
        mode = (state, toast, scale)
        if mode != self.notice_mode:
            self.notice_mode = mode
            canvas = self.notice_canvas
            canvas.delete('all')
            p = lambda value: round(value * scale)
            canvas.create_oval(p(11), p(14 if toast else 10), p(17), p(20 if toast else 16),
                               fill=self.footer_color, outline='')
            canvas.create_text(p(27), p(17 if toast else 14), anchor='w',
                               text=LABELS[state][0 if toast else 2], fill=MIDNIGHT['ink'],
                               font=(FONT, -p(12 if toast else 11)))
            if toast:
                canvas.create_text(p(27), p(37), anchor='w', text='点击查看本次评审',
                                   fill=MIDNIGHT['muted'], font=(FONT, -p(10)))
        ball = self.orb.ball
        position = (ball.winfo_x(), ball.winfo_y(), ball.winfo_width(), ball.winfo_height(), width, height, scale)
        if position != self.notice_position:
            self.notice_position = position
            from game_capture import choose_monitor, get_monitors
            x, y, bw, bh = position[:4]
            monitor = choose_monitor(dict(left=x, top=y, width=bw, height=bh), get_monitors())
            w, h = round(width * scale), round(height * scale)
            x = max(monitor['left'], min(x+bw-w, monitor['left']+monitor['width']-w))
            y = y+bh+round(4*scale)
            if y+h > monitor['top']+monitor['height']:
                y = ball.winfo_y()-h-round(4*scale)
            y = max(monitor['top'], min(y, monitor['top']+monitor['height']-h))
            self.notice.geometry(f'{w}x{h}')
            self.orb.position(self.notice, x, y)
        if not self.notice.winfo_viewable():
            self.notice.deiconify()

    def show_details(self):
        if self.closed:
            return
        self.pending_toast = None
        self.toast_until = 0
        self.next_read = 0
        if self.window is not None and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            self._paint_details()
            return
        scale = self.orb.base_scale
        p = lambda value: round(value * scale)
        window = self.window = tk.Toplevel(self.orb.root)
        window.title('后台体验评审')
        configure_window(window)
        configure_history(ttk.Style(window),scale)
        window.geometry(f'{p(520)}x{p(310)}')
        window.minsize(p(390), p(250))
        self.details_heading = tk.Label(window, text='', bg=SURFACE, fg=INK,
                                        font=(FONT, -p(19)), anchor='w')
        self.details_heading.pack(fill='x', padx=p(22), pady=(p(20), p(10)))
        body = tk.Frame(window, bg=SURFACE)
        body.pack(fill='both', expand=True, padx=p(22))
        self.details_text = tk.Text(body, wrap='word', bg=SURFACE, fg=INK,
                                    relief='flat', borderwidth=0, height=7,insertbackground=ACCENT,
                                    selectbackground=MIDNIGHT['selection'],selectforeground=INK,
                                    font=(FONT, -p(12)), padx=0, pady=0)
        scrollbar = ttk.Scrollbar(body, orient='vertical', command=self.details_text.yview,style='History.Vertical.TScrollbar')
        scrollbar.pack(side='right', fill='y')
        self.details_text.configure(yscrollcommand=scrollbar.set)
        self.details_text.pack(side='left', fill='both', expand=True)
        self.session_label = tk.Label(window, text='', anchor='w', bg=SURFACE, fg=MUTED,
                                      font=(FONT, -p(10)))
        self.session_label.pack(fill='x', padx=p(22), pady=(p(8), p(10)))
        actions = tk.Frame(window, bg=SURFACE)
        actions.pack(fill='x', padx=p(22), pady=(0, p(18)))
        def button(label, command):
            widget = tk.Button(actions, text=label, command=command, bg=PALE, fg=INK,
                               activebackground=MIDNIGHT['hover'],activeforeground=INK,disabledforeground=MIDNIGHT['disabled'], relief='flat', borderwidth=0,
                               padx=p(12), pady=p(7), font=(FONT, -p(11)),
                               highlightthickness=1, highlightcolor=ACCENT,
                               highlightbackground=LINE, takefocus=True)
            widget.pack(side='left', padx=(0, p(8)))
            widget.bind('<Return>', lambda event: (widget.invoke(), 'break')[1])
            return widget
        self.open_button = button('打开本次记录', self._open_directory)
        self.copy_button = button('复制会话编号', self._copy_session)
        button('关闭', self._close_details)
        window.protocol('WM_DELETE_WINDOW', self._close_details)
        window.bind('<Escape>', lambda event: self._close_details())
        self.details_signature = None
        self._paint_details()
        window.update_idletasks()
        from game_capture import choose_monitor, get_monitors
        monitor = choose_monitor(dict(left=self.orb.ball.winfo_x(), top=self.orb.ball.winfo_y(),
                                      width=self.orb.ball.winfo_width(), height=self.orb.ball.winfo_height()), get_monitors())
        self.orb.position(window, monitor['left']+max(0,(monitor['width']-window.winfo_width())//2),
                          monitor['top']+max(0,(monitor['height']-window.winfo_height())//2))
        window.lift()

    def _paint_details(self):
        if self.window is None or not self.window.winfo_exists():
            return
        signature = (self.data['state'], self.data.get('summary'), self.data.get('session_id'), self.event_id)
        if signature == self.details_signature:
            return
        self.details_signature = signature
        self.details_heading.configure(text=LABELS[self.data['state']][0])
        self.details_text.configure(state='normal')
        self.details_text.delete('1.0', 'end')
        self.details_text.insert('1.0', self.data.get('summary', ''))
        self.details_text.configure(state='disabled')
        self.session_label.configure(text='会话已建立，可复制编号继续查看。' if self.data.get('session_id') else '尚未取得会话编号。')
        self.copy_button.configure(state='normal' if self.data.get('session_id') else 'disabled')
        self.open_button.configure(state='normal' if self.event_id else 'disabled')

    def _copy_session(self):
        session = self.data.get('session_id')
        if event_uuid(session):
            self.window.clipboard_clear()
            self.window.clipboard_append(session)
            self.session_label.configure(text='会话编号已复制。')

    def _open_directory(self):
        if self.event_id:
            try:
                os.startfile(str(ROOT / 'work' / 'startup-reviews' / self.event_id))
            except OSError:
                self.session_label.configure(text='本次记录尚未保存，请稍后查看。')

    def _close_details(self):
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None
            self.details_signature = None

    def close(self):
        self.closed = True
        self._close_details()
        if self.notice is not None:
            try:
                self.notice.destroy()
            except tk.TclError:
                pass
            self.notice = None
