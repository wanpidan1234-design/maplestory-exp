import csv
from copy import deepcopy
import ctypes
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
import sys
import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import cv2
import mss
import numpy as np
from PIL import Image, ImageTk
from rapidocr_onnxruntime import RapidOCR
from tracker import Tracker, parse_exp, WINDOW_SECONDS, WINDOW_ALGORITHM
from game_capture import GameCapture
from experience_ocr import ExperienceOCR
from level_ocr import read_level
from auto_level import AutoLevel
from history_store import HistoryStore
from app_time import format_time
from combat_service import CombatService
from recognition_diagnostics import RecognitionJournal
from ui_charts import rate_prefix

ROOT = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get('LOCALAPPDATA', str(ROOT))) / 'MapleExpTracker' / 'settings.json'
DISPLAY_RETRY_SECONDS = 2.0


class App:
    def __init__(self, root):
        self.root = root
        root.title('冒险岛 · 经验观察器')
        root.geometry('1050x920')
        root.minsize(740, 640)
        root.configure(bg='#ffffff')
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background='#ffffff', foreground='#16324f')
        style.configure('TButton', background='#edf4ff', padding=(12, 8))
        style.map('TButton', background=[('active', '#dceaff')])
        style.configure('TEntry', fieldbackground='#ffffff', foreground='#16324f')
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 23, 'bold'))
        style.configure('Sub.TLabel', foreground='#64748b')
        self.tracker = Tracker()
        self.auto_level = AutoLevel(self.tracker)
        self.history = HistoryStore(Path(os.environ.get('MAPLE_EXP_DATA_DIR',str(ROOT/'data')))/'history.sqlite3')
        self.history.import_legacy(CONFIG.parent/'sessions')
        self.history.import_snapshot(CONFIG.with_name('live-status.json'))
        self.combat = CombatService(self.history.db)
        self.storage_error = None
        self.region = None
        self.running = False
        self.busy = False
        self.last_maintenance = 0
        self.generation = 0
        self.events = queue.Queue()
        self.engine = None
        self.capture = None
        self.capture_info = {}
        self.last_valid_time = None
        self.last_valid_monotonic = None
        self.reading_valid = False
        self._confirmed_rate_display = None
        self._rate_retry = False
        self.latest = None
        self.records = []
        self.record_seqs = []
        self.manual = tk.StringVar()
        self.topmost = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='先框选经验数字，再开始记录。游戏须保持可见。')
        self.raw = tk.StringVar(value='等待画面识别')
        self.elapsed = tk.StringVar(value='有效计时  00:00:00')
        outer = ttk.Frame(root, padding=24)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='经验观察器', style='Title.TLabel').pack(anchor='w')
        ttk.Label(outer, text='MAPLESTORY   /   屏幕识别 · 本地计算', style='Sub.TLabel').pack(anchor='w', pady=(4, 22))
        cards = ttk.Frame(outer)
        cards.pack(fill='x')
        self.metrics = []
        for title in ('本次累计经验', '最近 1 分钟分均 / EXP·min', '全程分均 / EXP·min'):
            frame = tk.Frame(cards, bg='#f4f8fd', padx=16, pady=18)
            frame.pack(side='left', expand=True, fill='both', padx=(0, 8))
            tk.Label(frame, text=title, bg='#f4f8fd', fg='#64748b', font=('Microsoft YaHei UI', 10)).pack(anchor='w')
            label = tk.Label(frame, text='0', bg='#f4f8fd', fg='#2563eb', font=('Segoe UI', 20, 'bold'))
            label.pack(anchor='w', pady=(8, 0))
            self.metrics.append(label)
        ttk.Label(outer, textvariable=self.elapsed, style='Sub.TLabel').pack(anchor='w', pady=12)
        buttons = ttk.Frame(outer)
        buttons.pack(fill='x')
        ttk.Button(buttons, text='① 框选经验栏', command=self.select).pack(side='left', padx=(0, 8))
        self.start_button = ttk.Button(buttons, text='② 开始记录', command=self.toggle)
        self.start_button.pack(side='left', padx=(0, 8))
        ttk.Button(buttons, text='重置统计', command=self.reset).pack(side='left', padx=(0, 8))
        ttk.Button(buttons, text='导出 CSV', command=self.export).pack(side='left')
        ttk.Button(buttons, text='自动定位游戏', command=self.auto_game).pack(side='left', padx=8)
        settings = ttk.Frame(outer)
        settings.pack(fill='x', pady=14)
        ttk.Label(settings, text='当前等级升级所需经验（可选）').pack(side='left')
        ttk.Entry(settings, textvariable=self.manual, width=17).pack(side='left', padx=8)
        ttk.Checkbutton(settings, text='窗口置顶', variable=self.topmost, command=lambda: root.attributes('-topmost', self.topmost.get())).pack(side='right')
        self.preview = tk.Label(outer, text='框选后的画面预览', bg='#f4f8fd', fg='#64748b', height=5)
        self.preview.pack(fill='x', pady=(0, 12))
        ttk.Label(outer, textvariable=self.raw, wraplength=700, style='Sub.TLabel').pack(anchor='w')
        ttk.Label(outer, textvariable=self.status, wraplength=700).pack(anchor='w', pady=12)
        recovery = ttk.Frame(outer)
        recovery.pack(fill='x')
        ttk.Button(recovery, text='确认刚刚升了一级', command=self.level).pack(side='left', padx=(0, 8))
        ttk.Button(recovery, text='以当前读数接续（跳过差值）', command=self.rebase).pack(side='left')
        ttk.Label(outer, text='主数字为最近 1 分钟观测分均；未满 60 秒按实际时长折算。\n识别重试时暂显 2 秒内的上次可信值；超时显示空缺，超过 5 秒断采后重建窗口。\n速度趋势为辅助指数估算，可在多尺度窗口查看；全程均速 = 本次累计 ÷ 有效计时。\n只框选经验数字。自动模式会跟随游戏窗口；手动选区需自行更新。', style='Sub.TLabel', wraplength=700).pack(anchor='w', pady=18)
        try:
            config = json.loads(CONFIG.read_text(encoding='utf-8'))
            self.region = config.get('region')
            self.manual.set(config.get('cap', ''))
        except (OSError, ValueError):
            pass
        self.restore_statistics()
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(150, self.tick)
        if '--auto-start' in sys.argv:
            self.root.after(600, self.auto_game)

    def auto_game(self):
        if self.running and self.region and self.region.get('game_window'):
            # GameCapture already rechecks client geometry every frame. A second
            # locate request must not act as a manual pause/new rate segment.
            return
        if not self.tracker.resume_pending:
            self.pause()
        elif self.running:
            self.generation += 1
            self.combat.set_state(False, self.generation)
        self.region = {'game_window': True}
        self.save()
        if not self.running:
            self.toggle()

    def cap(self):
        text = self.manual.get().replace(',', '').strip()
        if not text:
            return None
        value = int(text)
        if value <= 0:
            raise ValueError('升级经验必须大于 0')
        return value

    def save(self):
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps({'region': self.region, 'cap': self.manual.get()}), encoding='utf-8')

    def close(self):
        try:
            self.combat.close()
            # Export before pause clears the observation windows. The stored
            # timer is frozen; process downtime is excluded on restoration.
            self.persist_time()
            self.save()
        finally:
            self.history.db.close()
            self.root.destroy()

    def restore_statistics(self):
        """Resume only committed statistics; capture evidence starts afresh."""
        try:
            state = self.history.load_resume_state()
            if state is None:
                return False
            legacy = state.get('legacy', False)
            if legacy:
                restored = self.tracker.export_resume_state()
                restored.update(total=state['total'], elapsed=state['elapsed'])
                state = restored
            if not self.tracker.restore_resume_state(state):
                self.history.clear_resume_state()
                self.status.set('恢复数据无法验证，已保留历史明细；本次重新开始采样。')
                return False
            self.records, self.record_seqs = self.history.session_records()
            self.auto_level.reset()
            self.latest = None
            self.reading_valid = False
            self.last_valid_time = self.last_valid_monotonic = None
            self.clear_rate_display()
            self.start_button.configure(text='② 继续记录')
            self.status.set('已恢复累计与计时；旧版窗口需重新采样。' if legacy else
                            '已恢复累计、计时与采样进度；等待新读数，离线期间不计入。')
            return True
        except (OSError, sqlite3.Error) as error:
            self.storage_failed(error)
            return False

    def pause(self):
        self.clear_rate_display()
        self.running = False
        self.reading_valid = False
        self.generation += 1
        self.combat.set_state(False,self.generation)
        self.tracker.pause()
        self.persist_time()
        self.start_button.configure(text='② 继续记录')

    def toggle(self):
        if self.storage_error:
            self.status.set('历史保存失败，无法继续当前记录；请重启以恢复最后成功保存的状态，或新建记录。')
            return
        if self.running:
            self.pause()
            self.status.set('已暂停；继续时以第一帧作为新起点，暂停期间经验不计入。')
            return
        if not self.region:
            self.select()
            return
        try:
            self.cap()
        except ValueError:
            messagebox.showerror('输入有误', '升级所需经验请填写正整数。')
            return
        if self.tracker.blocked:
            self.status.set('先确认升级或跳过差值，再继续。')
            return
        self.tracker.previous = None
        self.clear_rate_display()
        restoring = self.tracker.resume_pending
        if not restoring:
            self.tracker.reset_rates()
        self.reading_valid = False
        self.last_valid_monotonic = None
        self.auto_level.reset()
        self.tracker.level = None
        self.running = True
        self.generation += 1
        self.start_button.configure(text='暂停记录')
        self.status.set('已恢复采样进度，等待新读数；离线期间不计入。' if restoring else
                        '正在初始化本地识别器并等待第一帧……')

    def reset(self):
        if self.records and not messagebox.askyesno('新建记录', '结束本次统计并开始新记录？已有记录会保留在历史中。'):
            return
        self.pause()
        self.combat.reset()
        self.history.finish(self.tracker.elapsed)
        self.history.clear_resume_state()
        self.tracker = Tracker()
        self.auto_level = AutoLevel(self.tracker)
        self.records = []
        self.record_seqs = []
        self.latest = None
        self.storage_error = None
        self.status.set('已保存上一段历史。可继续开始新记录。')

    def select(self):
        self.pause()
        self.root.withdraw()
        self.root.after(250, self.selection_overlay)

    def selection_overlay(self):
        try:
            with mss.mss() as capture:
                monitor = capture.monitors[0]
                shot = np.asarray(capture.grab(monitor))[:, :, :3][:, :, ::-1]
            overlay = tk.Toplevel(self.root)
            overlay.title('MapleStory EXP - Select Region')
            overlay.attributes('-topmost', True)
            overlay.geometry(f"{monitor['width']}x{monitor['height']}+0+0")
            overlay.update_idletasks()
            ctypes.windll.user32.SetWindowPos(ctypes.windll.user32.GetParent(overlay.winfo_id()), -1, monitor['left'], monitor['top'], monitor['width'], monitor['height'], 0x0040)
            canvas = tk.Canvas(overlay, highlightthickness=0, cursor='crosshair')
            canvas.pack(fill='both', expand=True)
            photo = ImageTk.PhotoImage(Image.fromarray(shot))
            canvas.create_image(0, 0, image=photo, anchor='nw')
            canvas.image = photo
            canvas.create_text(30, 30, text='拖动框选经验数字区域 · Esc 取消', anchor='nw', fill='#2563eb', font=('Microsoft YaHei UI', 20, 'bold'))
            state = {}
            def finish():
                overlay.destroy()
                self.root.deiconify()
            def down(event):
                state['origin'] = (event.x, event.y)
                if 'box' in state:
                    canvas.delete(state['box'])
                state['box'] = canvas.create_rectangle(event.x, event.y, event.x, event.y, outline='#2563eb', width=2)
            def move(event):
                if 'origin' in state:
                    canvas.coords(state['box'], *state['origin'], event.x, event.y)
            def up(event):
                if 'origin' not in state:
                    return
                x, y = state['origin']
                left, top = min(x, event.x), min(y, event.y)
                width, height = abs(event.x-x), abs(event.y-y)
                if width < 20 or height < 8:
                    return
                self.region = dict(left=left+monitor['left'], top=top+monitor['top'], width=width, height=height)
                self.save()
                self.latest = None
                self.status.set(f'已选取 {width} × {height} 像素区域，点击开始记录。')
                finish()
            canvas.bind('<ButtonPress-1>', down)
            canvas.bind('<B1-Motion>', move)
            canvas.bind('<ButtonRelease-1>', up)
            overlay.bind('<Escape>', lambda _: finish())
            overlay.focus_force()
        except Exception as error:
            self.root.deiconify()
            messagebox.showerror('无法截屏', str(error))

    def worker(self, generation, region):
        frame = level_frame = None
        info = {'source': 'screen' if region.get('game_window') else 'manual'}
        stage = 'engine_init'
        try:
            if self.engine is None:
                self.engine = ExperienceOCR()
            stage = 'capture'
            started = time.perf_counter()
            if region.get('game_window'):
                if self.capture is None:
                    self.capture = GameCapture()
                frame,info = self.capture.capture(include_level=True)
                info.setdefault('observed_at',time.monotonic())
                level_frame = self.capture.level_frame
            else:
                with mss.mss() as capture:
                    shot = capture.grab(region)
                    observed_at = time.monotonic()
                    frame = np.asarray(shot)[:, :, :3].copy()
                info = {'source':'manual','roi':region,'observed_at':observed_at}
            timings = info['timing_ms'] = {'capture': round((time.perf_counter()-started)*1000, 3)}
            stage = 'experience_ocr'
            started = time.perf_counter()
            reading,details = self.engine.read(frame)
            timings['experience_ocr'] = round((time.perf_counter()-started)*1000, 3)
            diagnostics = getattr(self.engine, 'last_diagnostics', None)
            if isinstance(diagnostics, dict):
                info['recognition'] = diagnostics.copy()
            # Level is optional evidence. A local badge failure must not discard
            # a valid EXP frame or replace its capture timestamp with OCR time.
            if region.get('game_window'):
                started = time.perf_counter()
                level, level_details = None, []
                try:
                    level, level_details = read_level(level_frame, self.engine)
                except Exception as error:
                    info['level_error'] = f'{type(error).__name__}: {error}'[:1000]
                info.update(level_candidate=level, level_ocr=level_details,
                            level_confidence_kind='character_mean')
                timings['level_ocr'] = round((time.perf_counter()-started)*1000, 3)
            raw = str(reading.current) + (f' [{reading.percent:.2f}%]' if reading.percent is not None else '') if reading and reading.current is not None else ' / '.join(r[0] for r in details)
            info['ocr'] = details
            if reading is None:
                self.diagnose_failure('experience_rejected', info, generation, frame, level_frame)
            # A valid EXP result must not wait for optional diagnostic disk I/O.
            # Badge errors remain visible in this result's status metadata.
            self.events.put((generation, frame, raw, reading, None,info))
        except Exception as error:
            stage = getattr(error, 'stage', stage)
            context = getattr(error, 'context', None)
            if isinstance(context, dict):
                info.update(context)
            info['failure_stage'] = stage
            info['error'] = f'{type(error).__name__}: {error}'[:1000]
            self.diagnose_failure(stage, info, generation, frame, level_frame)
            self.events.put((generation, frame, '', None, str(error),info))

    def diagnose_failure(self, stage, info, generation, frame=None, level_frame=None):
        """Best-effort, bounded local evidence; never a prerequisite for accounting."""
        if generation != self.generation:
            return
        try:
            journal = getattr(self, 'recognition_journal', None)
            if journal is None:
                journal = self.recognition_journal = RecognitionJournal(CONFIG.parent/'recognition-failures')
            path = journal.record(stage, {'generation': generation, 'capture': info},
                                  frame=frame, level_frame=level_frame)
            if path is not None:
                info['diagnostic_file'] = str(path)
            if journal.last_error:
                info['diagnostic_error'] = journal.last_error
        except Exception as error:
            info['diagnostic_error'] = f'{type(error).__name__}: {error}'[:300]

    def fresh_reading(self):
        """Freshness uses the same monotonic timeline as the rate estimator."""
        observed = self.last_valid_monotonic
        return bool(self.running and self.reading_valid and not self.tracker.blocked
                    and observed is not None
                    and 0 <= self.tracker.clock()-observed <= self.tracker.live.max_gap)

    def rate_snapshot(self):
        """Keep trend storage compatible and expose the primary observation window."""
        snapshot = self.tracker.live.snapshot()
        windows = self.tracker.windows.snapshot()
        if not self.running:
            snapshot.update(value=None, state='paused')
        elif not self.fresh_reading():
            snapshot.update(value=None, state='stale' if snapshot['state']=='stale' else 'unavailable')
        if snapshot['value'] is None:
            for window in windows.values():
                window.update(value=None, state=snapshot['state'])
        snapshot['windows'] = windows
        snapshot['window_algorithm'] = WINDOW_ALGORITHM
        snapshot['rolling60'] = windows['60']['value']
        snapshot['primary'] = dict(windows['60'], algorithm=WINDOW_ALGORITHM,
                                   segment=snapshot['segment'])
        return snapshot

    def primary_rate_snapshot(self):
        return self.rate_snapshot()['primary']

    def clear_rate_display(self):
        self._confirmed_rate_display = None
        self._rate_retry = False

    def display_rate_snapshot(self):
        """A bounded last-observation display, never an accepted accounting sample.

        Its capture time and coverage never advance on failures or UI refresh.
        """
        current = self.rate_snapshot()
        current.update(display_state='live' if current['primary']['value'] is not None else 'unavailable',
                       display_age_seconds=None)
        if current['primary']['value'] is not None:
            return current
        if self.tracker.resume_pending and not self.storage_error:
            restored = self.tracker.resume_rate_snapshot()
            result = restored if restored is not None else current
            interrupted=self.tracker.visibility_interrupted
            result.update(display_state='visibility_paused' if interrupted else 'restored',
                          display_age_seconds=None)
            if interrupted:
                result['visibility_preserved']=restored is not None
            return result
        saved = getattr(self, '_confirmed_rate_display', None)
        if (not getattr(self, '_rate_retry', False) or not saved or not self.running
                or self.tracker.blocked or self.auto_level.wait_since is not None
                or saved['generation'] != self.generation
                or saved['segment'] != self.tracker.live.segment):
            return current
        age = self.tracker.clock() - saved['observed_at']
        if (not math.isfinite(age) or not 0 <= age <= min(DISPLAY_RETRY_SECONDS, self.tracker.live.max_gap)
                or saved['snapshot']['primary']['value'] is None):
            return current
        result = deepcopy(saved['snapshot'])
        result.update(display_state='retrying', display_age_seconds=age)
        return result

    def record(self, status, delta=0):
        if self.storage_error:
            return False
        minute, hour = self.tracker.rates()
        row=[format_time(pattern='%Y-%m-%d %H:%M:%S'), round(self.tracker.elapsed, 2), self.tracker.previous, delta, self.tracker.total, round(minute, 2), round(hour, 2), status, self.raw.get()]
        if status.startswith('auto_level'):
            row[-1]+=' | '+json.dumps(self.auto_level.last_event,ensure_ascii=False)
        self.records.append(row)
        self.record_seqs.append(None)
        try:
            monitor=self.capture_info.get('monitor',{})
            rate = self.rate_snapshot()
            self.history.append(row,rate['value'],self.latest.percent if self.latest else None,
                                str(monitor.get('index','')),rate_metadata=rate,
                                resume_state=self.tracker.export_resume_state())
            self.record_seqs[-1] = self.history.seq
            if self.fresh_reading():
                self._confirmed_rate_display = dict(generation=self.generation,
                    segment=self.tracker.live.segment, observed_at=self.last_valid_monotonic,
                    snapshot=deepcopy(rate))
            return True
        except (OSError,sqlite3.Error,ValueError) as error:
            self.storage_failed(error)
            return False

    def storage_failed(self,error):
        self.clear_rate_display()
        self.storage_error=str(error)
        self.running=False
        self.generation+=1
        self.tracker.pause()
        self.start_button.configure(text='历史保存失败')
        self.status.set('历史保存失败，已暂停：'+str(error))

    def persist_time(self):
        if self.storage_error:
            return
        try:
            # Persist committed observations even during an extended OCR outage.
            # Display freshness never revokes ownership of these samples.
            retrying = self.display_rate_snapshot()['display_state'] == 'retrying'
            state = self.tracker.export_resume_state(allow_invalid=retrying)
            self.history.save_resume_state(state, state['elapsed'])
        except (OSError,sqlite3.Error,ValueError) as error:
            self.storage_failed(error)

    def rebase(self):
        if self.storage_error or self.latest is None:
            return
        self.clear_rate_display()
        try:
            status, _ = self.tracker.feed(self.latest, self.cap(), rebase=True)
        except ValueError:
            self.status.set('升级所需经验请填写正整数。')
            return
        if status == 'need_cap':
            self.status.set('仅有百分比，需填写当前等级升级所需经验。')
            return
        self.record('manual_rebase')
        self.auto_level.reset()
        self.status.set('已跳过差值。点击继续记录。')

    def level(self):
        if self.storage_error:
            return
        if not self.tracker.blocked or self.latest is None:
            self.status.set('仅在检测到经验下降后使用升级确认。')
            return
        self.clear_rate_display()
        old_total = self.tracker.total
        if self.tracker.confirm_level(self.latest):
            self.manual.set('')
            self.record('confirmed_level', self.tracker.total-old_total)
            self.status.set('已计入跨一级经验。旧等级升级经验已清空；下一次升级前请更新。点击继续记录。')
        else:
            self.status.set('缺少上一级升级经验或当前绝对经验，无法精确补算；请跳过差值。')

    def tick(self):
        try:
            while True:
                generation, frame, raw, reading, error,info = self.events.get_nowait()
                self.busy = False
                if generation != self.generation:
                    continue
                was_valid = self.reading_valid
                was_retry = getattr(self, '_rate_retry', False)
                self._rate_retry = False
                self.reading_valid = False
                self.capture_info = info
                if error:
                    failure_at=info.get('observed_at')
                    current_failure=(type(failure_at) in (int,float) and math.isfinite(failure_at)
                        and 0<=self.tracker.clock()-failure_at<=self.tracker.live.max_gap
                        and (self.last_valid_monotonic is None or failure_at>self.last_valid_monotonic))
                    visibility = (self.running and self.region and self.region.get('game_window')
                        and info.get('source')=='screen' and type(info.get('hwnd')) is int
                        and info['hwnd']>0 and current_failure
                        and info.get('failure_kind')=='visibility_interrupted')
                    if visibility:
                        if self.tracker.suspend_visibility():
                            self.auto_level.reset()
                            self.latest = None
                            self.clear_rate_display()
                            self.persist_time()
                            if self.storage_error:
                                continue
                        self.status.set('切屏暂停 · 等待游戏经验栏恢复可见，采样进度已保留。')
                        continue
                    self.tracker.note_visibility_recovery(info.get('observed_at'))
                    self.tracker.live.invalidate()
                    if info.get('failure_stage') == 'experience_ocr':
                        self._rate_retry = True
                    else:
                        self.clear_rate_display()
                    self.status.set('识别失败：' + error)
                    continue
                observed_at = info.get('observed_at',self.tracker.clock())
                if (not isinstance(observed_at,(int,float)) or not math.isfinite(observed_at)
                        or not 0 <= self.tracker.clock()-observed_at <= self.tracker.live.max_gap):
                    self.tracker.live.invalidate()
                    self.clear_rate_display()
                    self.status.set('采样画面已过期，等待新的有效读数。')
                    continue
                self.tracker.note_visibility_recovery(observed_at)
                image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                image.thumbnail((690, 90))
                self.photo = ImageTk.PhotoImage(image)
                self.preview.configure(image=self.photo, text='', height=90)
                self.raw.set('识别文字：' + (raw or '未找到文字'))
                if reading is None:
                    self.tracker.live.invalidate()
                    self._rate_retry = True
                    self.status.set('读数未通过校验，本帧不计入；请检查框选范围和遮挡。')
                    continue
                try:
                    if self.region and self.region.get('game_window'):
                        status,delta=self.auto_level.feed(reading,info.get('level_candidate'),self.cap(),observed_at=observed_at)
                    else:
                        status, delta = self.tracker.feed(reading, self.cap(), observed_at=observed_at)
                except ValueError:
                    self.tracker.live.invalidate()
                    self.clear_rate_display()
                    self.status.set('升级所需经验请填写正整数。')
                    continue
                if status == 'stale':
                    self.reading_valid = was_valid
                    self._rate_retry = was_retry
                    continue
                self.latest = reading
                if status == 'need_cap':
                    self.tracker.live.invalidate()
                    self.clear_rate_display()
                    self.status.set('仅识别到百分比，请填写当前等级升级所需经验（结果为估算）。')
                elif status == 'invalid':
                    self.tracker.live.invalidate()
                    self.clear_rate_display()
                    self.status.set('读数未通过校验，本帧不计入；等待新的有效读数。')
                elif status == 'level_pending':
                    self.tracker.live.invalidate()
                    self.clear_rate_display()
                    self.status.set('正在多帧核验等级与经验变化，暂不计入差值……')
                elif status in ('decrease','level_mismatch','blocked'):
                    self.pause()
                    self.status.set('等级或经验变化未能自动确认，已暂停。可确认升级或以当前读数接续。')
                    self.record(status)
                else:
                    self.tracker.start()
                    self.reading_valid = True
                    self.last_valid_time = time.time()
                    self.last_valid_monotonic = observed_at
                    if not self.record(status, delta):
                        continue
                    suffix = ' · 百分比换算估算值' if self.tracker.estimated else ''
                    if status.startswith('auto_level'):
                        self.manual.set('')
                        suffix+=' · 已自动确认升级'
                    if self.tracker.level_gaps:
                        suffix+=f' · {self.tracker.level_gaps} 次跨级尾段未计入'
                    if status == 'gap':
                        suffix+=' · 已补记净差，分均窗口已接续（跳过中断时段）'
                    monitor = self.capture_info.get('monitor',{})
                    location = ('主屏' if monitor.get('is_primary') else '副屏')+str(monitor.get('index','')) if monitor else '手动选区'
                    level_text=f' · Lv.{self.tracker.level}' if self.tracker.level is not None else ''
                    self.status.set(f'{location}{level_text} · 当前经验 {self.tracker.previous:,}{suffix}')
                    on_game_sample = getattr(self,'review_on_game_sample',None)
                    if callable(on_game_sample):
                        try:
                            on_game_sample()
                        except Exception as error:
                            # Optional review dispatch cannot reject a saved sample.
                            self.startup_review_error = type(error).__name__
        except queue.Empty:
            pass
        self.tracker.expire_visibility_continuation()
        rate = self.display_rate_snapshot()
        primary = rate['primary']
        values = (self.tracker.total,primary['value'],self.tracker.rates()[0])
        for index, (label, value) in enumerate(zip(self.metrics, values)):
            text = '—' if value is None else f'{value:,.0f}'
            if index == 1 and value is not None:
                text = rate_prefix(primary,rate['display_state'] in ('retrying', 'restored','visibility_paused')) + text
            label.configure(text=text)
        if rate['display_state'] == 'retrying':
            self.status.set(f"识别重试中，显示 {rate['display_age_seconds']:.1f} 秒前的可信值；本帧未计入。")
        elif rate['display_state'] == 'restored':
            self.status.set('已恢复累计、计时与采样进度；等待新读数，离线期间不计入。')
        elif rate['display_state'] == 'visibility_paused':
            self.status.set('切屏暂停 · 已保留分均与采样进度；等待读数，离屏期间不计入。'
                if rate.get('visibility_preserved') else '切屏暂停 · 等待新的有效读数，离屏期间不计入。')
        elif getattr(self, '_rate_retry', False) and not self.reading_valid:
            self.status.set('识别暂未恢复，等待新的有效读数；失败帧不计入。')
        seconds = int(self.tracker.elapsed)
        self.elapsed.set(f'有效计时  {seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}')
        # History timing remains checkpointed once per second; reading results
        # are drained more frequently so recognition is not delayed by the UI.
        maintenance=time.monotonic()-self.last_maintenance>=1
        if maintenance:self.persist_time()
        try:
            self.combat.poll(self.running and bool(self.region and self.region.get('game_window')),self.generation)
        except (OSError,sqlite3.Error) as error:
            self.combat.set_state(False,self.generation)
            self.storage_failed(error)
        if maintenance:
            self.write_status()
            self.last_maintenance=time.monotonic()
        if self.running and not self.busy:
            self.busy = True
            threading.Thread(target=self.worker, args=(self.generation, dict(self.region)), daemon=True).start()
        self.root.after(250, self.tick)

    def write_status(self):
        try:
            CONFIG.parent.mkdir(parents=True,exist_ok=True)
            data = dict(updated=time.time(),valid=self.fresh_reading(),running=self.running,last_valid=self.last_valid_time,current=self.tracker.previous,percent=self.latest.percent if self.latest else None,total=self.tracker.total,elapsed=self.tracker.elapsed,per_minute=self.tracker.rates()[0],per_hour=self.tracker.rates()[1],samples=len(self.records),capture=self.capture_info,status=self.status.get())
            rate = self.rate_snapshot()
            primary = rate['primary']
            display = self.display_rate_snapshot()
            # Legacy realtime_* fields retain their saved EWMA meaning. New
            # consumers can use explicit primary_* and trend_* contracts.
            data.update(realtime_per_minute=rate['value'],rate_window_seconds=None,
                        rate_algorithm=rate['algorithm'],rate_state=rate['state'],
                        rate_half_life_seconds=rate['half_life_seconds'],
                        rate_observed_seconds=rate['observed_seconds'],rate_segment=rate['segment'],
                        rolling60_per_minute=rate['rolling60'],
                        window_algorithm=rate['window_algorithm'],window_rates=rate['windows'],
                        history_path=str(self.history.path),history_error=self.storage_error)
            data.update(primary_per_minute=primary['value'],
                        primary_per_hour=primary['value']*60 if primary['value'] is not None else None,
                        primary_algorithm=primary['algorithm'],primary_window_seconds=primary['window_seconds'],
                        primary_state=primary['state'],primary_observed_seconds=primary['observed_seconds'],
                        primary_segment=primary['segment'],
                        primary_has_gaps=primary.get('has_gaps',False),
                        trend_per_minute=rate['value'],trend_algorithm=rate['algorithm'],
                        trend_half_life_seconds=rate['half_life_seconds'],trend_state=rate['state'],
                        trend_has_gaps=rate.get('has_gaps',False))
            data.update(display_state=display['display_state'],
                        display_age_seconds=display['display_age_seconds'],
                        display_primary_per_minute=display['primary']['value'],
                        display_window_rates=display['windows'],display_trend_per_minute=display['value'])
            data.update(level=self.tracker.level,confirmed_levels=self.tracker.confirmed_levels,level_gaps=self.tracker.level_gaps,last_level_event=self.auto_level.last_event)
            data['realtime_per_hour'] = rate['value'] * 60 if rate['value'] is not None else None
            data['combat'] = self.combat.summary()
            data['kills'] = self.combat.kill_summary()
            data['ui_animation'] = getattr(self,'ui_animation_state',{})
            launch = getattr(self, 'review_launch', {})
            review_thread = getattr(self, 'startup_review_thread', None)
            data.update(app_instance_id=launch.get('instance_id'),
                        app_started_at=launch.get('created_at'), app_pid=launch.get('pid'),
                        startup_review_queued=bool(getattr(review_thread, 'review_persisted', False)),
                        startup_review_error=(getattr(self, 'startup_review_error', None)
                                              or getattr(review_thread, 'review_error', None)))
            ui_snapshot = getattr(self, 'review_ui_snapshot', None)
            if callable(ui_snapshot):
                try:
                    data['ui_windows'] = ui_snapshot()
                except Exception as error:
                    data['ui_snapshot_error'] = type(error).__name__
            temporary = CONFIG.with_name('live-status.tmp')
            temporary.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
            temporary.replace(CONFIG.with_name('live-status.json'))
            return True
        except OSError:
            return False

    def export(self):
        path = filedialog.asksaveasfilename(defaultextension='.csv', initialfile='冒险岛经验记录.csv', filetypes=[('CSV 文件', '*.csv')])
        if path:
            try:
                with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
                    writer = csv.writer(stream)
                    writer.writerow(['时间（北京时间 UTC+8）', '有效秒数', '当前经验', '本次增加', '累计经验', '分均经验', '时均经验', '状态', '识别文字',
                                     '速度趋势（按算法列）', '分均算法', '半衰期秒数', '分均状态', '观测秒数', '分均采样段', '窗口算法',
                                     '趋势收益有缺口', '窗口收益有缺口（秒数）',
                                     *[f'{seconds//60}min分均' for seconds in WINDOW_SECONDS]])
                    # Read saved metadata only when exporting, without retaining
                    # five additional snapshot dictionaries for every frame.
                    metadata = iter(self.history.session_rate_metadata(self.history.active))
                    saved = next(metadata, None)
                    for index, row in enumerate(self.records):
                        seq = self.record_seqs[index]
                        while seq is not None and saved is not None and saved['seq'] < seq:
                            saved = next(metadata, None)
                        rate = saved if seq is not None and saved is not None and saved['seq'] == seq else {}
                        writer.writerow([*row, rate.get('realtime'), rate.get('rate_algorithm'),
                                         rate.get('rate_half_life'), rate.get('rate_state'),
                                         rate.get('rate_observed_seconds'), rate.get('rate_segment'),
                                         rate.get('window_algorithm'),
                                         rate.get('rate_has_gaps'),
                                         ','.join(str(seconds) for bit,seconds in enumerate(WINDOW_SECONDS)
                                                  if (rate.get('window_gap_mask') or 0)&(1<<bit)),
                                         *[rate.get(f'rolling{seconds}') for seconds in WINDOW_SECONDS]])
                self.status.set('记录已导出。')
            except (OSError,sqlite3.Error) as error:
                messagebox.showerror('导出失败', str(error))


if __name__ == '__main__':
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    App(tk.Tk()).root.mainloop()
