"""Standalone, screen-only third-job quiz assistant; no game input is sent."""
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

import cv2
import numpy as np

from quiz_bank import QuizMatch, load_bank, match_question, normalize
from quiz_capture import QuizRegion, capture_region, validate_region
from game_capture import game_geometry, physical_dpi
from ui_theme import (ACCENT, FONT, INK, MIDNIGHT, MUTED, SUCCESS, SURFACE,
                      WARNING, configure_history, configure_window)


def desktop_quiz_pdf():
    """Honor Windows desktop redirection (including OneDrive desktops)."""
    desktop = Path.home() / 'Desktop'
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
            value, _ = winreg.QueryValueEx(key, 'Desktop')
            desktop = Path(os.path.expandvars(value))
    except (ImportError, OSError):
        pass
    return desktop / '冒险岛国服怀旧服-三转答题速查-20261015.pdf'


@dataclass
class QuizState:
    result: QuizMatch = field(default_factory=lambda: QuizMatch('no_match'))
    generation: int = 0

    def clear(self, reason=''):
        self.generation += 1
        self.result = QuizMatch('no_match', reason=reason)
        return self.generation

    def accept(self, generation, result):
        if generation != self.generation:
            return False
        self.result = result
        return True


class QuizOCR:
    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        import onnxruntime as ort
        self.engine = RapidOCR(use_text_det=True, use_angle_cls=False)
        for wrapper in (self.engine.text_detector.infer, self.engine.text_recognizer.session):
            original = wrapper.session
            options = original.get_session_options()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.add_session_config_entry('session.intra_op.allow_spinning', '0')
            options.add_session_config_entry('session.inter_op.allow_spinning', '0')
            wrapper.session = ort.InferenceSession(original._model_path, sess_options=options,
                                                   providers=['CPUExecutionProvider'])

    def recognize(self, image):
        if image is None or not isinstance(image, np.ndarray) or image.size == 0:
            return '', 0.0
        # Small in-game text benefits from enlargement before line detection.
        height, width = image.shape[:2]
        scale = min(2.0, 1600 / max(height, width))
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        lines, _ = self.engine(image)
        if not lines:
            return '', 0.0
        # Pinned RapidOCR 1.2.3 averages conf_list + [1e-50], adding a
        # non-character zero to every line. Undo that denominator here using
        # ORIGINAL decoded text, including spaces and punctuation. This does
        # not boost the underlying character probabilities or lower the gate.
        scores = [min(1.0, float(line[2]) * (len(line[1])+1) / len(line[1]))
                  if line[1] else 0.0 for line in lines]
        return '\n'.join(line[1] for line in lines), min(scores)


class QuizWindow:
    def __init__(self, root):
        self.root = root
        self.entries = load_bank()
        self.state = QuizState()
        self.region = None
        self.running = False
        self.busy = False
        self.closed = False
        self.last_result_at = 0.0
        self.events = queue.Queue(maxsize=8)
        self.ocr = None
        self.previous_digest = None
        self.previous_result = None
        self.selector = None
        self.pdf_path = desktop_quiz_pdf()
        self.jobs = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.worker = threading.Thread(target=self._worker, name='quiz-ocr', daemon=True)
        self.worker.start()

        root.title('冒险岛 · 三转答题助手')
        root.geometry('480x740')
        root.minsize(420, 700)
        root.attributes('-topmost', True)
        configure_window(root)
        style = ttk.Style(root)
        style.theme_use('clam')
        configure_history(style)
        icon = Path(__file__).parent / 'designs/app-icons/maplestory-exp-v1.ico'
        if icon.exists():
            root.iconbitmap(str(icon))
        body = ttk.Frame(root, padding=20)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='三转答题助手', style='Title.TLabel').pack(anchor='w')
        ttk.Label(body, text=f'国服怀旧服 · {len(self.entries)} 道参考题 · 2026-10-15').pack(anchor='w', pady=(5, 16))
        self.status = tk.StringVar(value='先在游戏中打开题目，再框选题目与全部选项')
        self.status_label = ttk.Label(body, textvariable=self.status, wraplength=430)
        self.status_label.pack(fill='x', pady=(0, 12))
        actions = ttk.Frame(body)
        actions.pack(fill='x')
        self.select_button = ttk.Button(actions, text='框选题目区域', style='Primary.TButton', command=self.select_region)
        self.select_button.pack(side='left')
        self.toggle_button = ttk.Button(actions, text='开始识别', command=self.toggle, state='disabled')
        self.toggle_button.pack(side='left', padx=10)
        self.topmost = tk.BooleanVar(value=True)
        ttk.Checkbutton(actions, text='置顶', variable=self.topmost,
                        command=lambda: root.attributes('-topmost', self.topmost.get())).pack(side='right')
        ttk.Separator(body).pack(fill='x', pady=18)
        self.answer = tk.StringVar(value='等待题目')
        self.answer_label = ttk.Label(body, textvariable=self.answer, foreground=ACCENT,
                                      font=(FONT, -28, 'bold'), wraplength=420)
        self.answer_label.pack(anchor='w', fill='x')
        self.question = tk.StringVar(value='答案会显示在这里；请把助手放到答题框外。')
        self.question_label = ttk.Label(body, textvariable=self.question, wraplength=420)
        self.question_label.pack(fill='x', pady=(10, 8))
        ttk.Label(body, text='按答案文字选择，选项序号可能变化。', foreground=WARNING).pack(anchor='w')
        self.raw = tk.Text(body, height=4, wrap='word', bg=MIDNIGHT['field'], fg=MUTED,
                           font=(FONT, -14), relief='flat', padx=10, pady=8, state='disabled')
        self.raw.pack(fill='both', expand=True, pady=(12, 16))
        ttk.Label(body, text='手动查题', foreground=INK).pack(anchor='w')
        self.search = tk.StringVar()
        entry = ttk.Entry(body, textvariable=self.search)
        entry.pack(fill='x', pady=(6, 6))
        list_frame = ttk.Frame(body)
        list_frame.pack(fill='x')
        self.matches = tk.Listbox(list_frame, height=4, bg=MIDNIGHT['field'], fg=INK,
                                 selectbackground=MIDNIGHT['selection'], selectforeground=INK,
                                 font=(FONT, -12), relief='flat', activestyle='dotbox', exportselection=False)
        self.matches.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(list_frame, orient='vertical', command=self.matches.yview)
        scroll.pack(side='right', fill='y')
        self.matches.configure(yscrollcommand=scroll.set)
        self.matches.bind('<<ListboxSelect>>', self.show_manual)
        self.search.trace_add('write', self.search_bank)
        self.filtered = []
        self.search_bank()
        ttk.Label(body, text='输入关键词，选择一条查看参考答案。', style='Sub.TLabel').pack(anchor='w', pady=(6, 10))
        references = ttk.Frame(body)
        references.pack(fill='x')
        self.pdf_button = ttk.Button(references, text='打开题库 PDF', command=self.open_pdf)
        self.pdf_button.pack(side='left', padx=(0, 10))
        ttk.Button(references, text='打开题库来源与最新勘误',
                   command=lambda: webbrowser.open('https://mxdc.dvg.cn/tools/third-job-quiz/')).pack(anchor='w')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<Configure>', self.resize, add='+')
        self.poll_id = root.after(100, self.poll)
        self.scan_id = root.after(250, self.scan)

    def open_pdf(self):
        path = self.pdf_path
        if not path.is_file():
            selected = filedialog.askopenfilename(
                parent=self.root, title='选择三转题库 PDF',
                initialdir=str(path.parent if path.parent.is_dir() else Path.home()),
                filetypes=[('PDF 文档', '*.pdf')])
            if not selected:
                return
            path = Path(selected)
        try:
            os.startfile(str(path))
            self.pdf_path = path
        except OSError as error:
            messagebox.showerror('无法打开题库 PDF',
                                 f'请确认文件仍存在，并已安装或设置默认 PDF 阅读器。\n\n{error}',
                                 parent=self.root)

    def resize(self, event):
        if event.widget is self.root:
            width = max(320, event.width-44)
            for label in (self.status_label, self.answer_label, self.question_label):
                label.configure(wraplength=width)

    def render(self, result, raw=''):
        self.status.set(result.reason)
        confirmed = result.status == 'matched' and result.entry is not None
        suggested = result.status == 'uncertain' and result.entry is not None
        self.answer.set(result.entry.answer if confirmed else
                        f'参考：{result.entry.answer}' if suggested else
                        '等待核对' if result.status == 'uncertain' else '等待题目')
        self.answer_label.configure(foreground=SUCCESS if confirmed else WARNING if result.status == 'uncertain' else ACCENT)
        details = result.entry.question if confirmed or suggested else '请框入完整题干与全部选项；识别原文显示在下方。'
        for row in result.alternatives:
            details += f'\n候选：{row.answer} · {row.question}'
        self.question.set(details)
        self.raw.configure(state='normal')
        self.raw.delete('1.0', 'end')
        self.raw.insert('1.0', raw or '识别到的原文会显示在这里')
        self.raw.configure(state='disabled')

    def pause(self, reason='已暂停识别'):
        self.running = False
        self.state.clear(reason)
        self.toggle_button.configure(text='开始识别')
        self.render(self.state.result)

    def toggle(self):
        if self.running:
            self.pause()
        elif self.region:
            self.state.clear('正在识别题目…')
            self.render(self.state.result)
            self.running = True
            self.last_result_at = time.monotonic()
            self.toggle_button.configure(text='暂停识别')

    def select_region(self):
        if self.selector is not None:
            return
        self.pause('正在框选，Esc 取消')
        self.root.withdraw()
        self.root.after(200, self._select_game)

    def _select_game(self):
        import mss
        from PIL import Image, ImageTk
        try:
            geo = game_geometry(include_monitor=False)
            client = geo['client']
            from quiz_capture import region_visible
            if not region_visible(geo['hwnd'], client):
                raise ValueError('游戏窗口被遮挡，请让完整游戏窗口可见后重试')
            with mss.mss() as capture:
                shot = capture.grab(client)
                image = Image.frombytes('RGB', shot.size, shot.rgb)
            overlay = self.selector = tk.Toplevel(self.root)
            overlay.overrideredirect(True)
            overlay.attributes('-topmost', True)
            # wm_geometry interprets negative offsets relative to screen edges;
            # SetWindowPos preserves physical positions on a left-hand monitor.
            overlay.geometry(f"{client['width']}x{client['height']}+0+0")
            canvas = tk.Canvas(overlay, highlightthickness=0, cursor='crosshair')
            canvas.pack(fill='both', expand=True)
            photo = ImageTk.PhotoImage(image)
            canvas.photo = photo
            canvas.create_image(0, 0, image=photo, anchor='nw')
            canvas.create_rectangle(0, 0, client['width'], 38, fill=SURFACE, outline='')
            canvas.create_text(12, 18, anchor='w', text='拖动框选题干与全部选项  ·  Esc 取消', fill=INK, font=(FONT, -16))
            overlay.update_idletasks()
            import ctypes
            from ctypes import wintypes as wt
            user = ctypes.windll.user32
            user.GetParent.argtypes = [wt.HWND]
            user.GetParent.restype = wt.HWND
            user.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
            handle = user.GetParent(overlay.winfo_id()) or overlay.winfo_id()
            user.SetWindowPos(handle, -1, client['left'], client['top'], client['width'], client['height'], 0x0010)
            start = []
            rectangle = [None]

            def cancel(event=None):
                overlay.destroy()
                self.selector = None
                self.root.deiconify()
                self.status.set('已取消框选；可以重新选择题目区域')

            def press(event):
                start[:] = [event.x, event.y]
                if rectangle[0]:
                    canvas.delete(rectangle[0])
                rectangle[0] = canvas.create_rectangle(event.x, event.y, event.x, event.y, outline='#ffb95e', width=3)

            def drag(event):
                if start:
                    canvas.coords(rectangle[0], *start, event.x, event.y)

            def release(event):
                if not start:
                    return
                left, right = sorted((start[0], event.x))
                top, bottom = sorted((start[1], event.y))
                region = QuizRegion(geo['hwnd'], (client['width'], client['height']), geo['dpi'],
                                    left, top, right-left, bottom-top)
                try:
                    current = game_geometry(include_monitor=False)
                    if current != geo:
                        raise ValueError('游戏已移动，请重新框选')
                    validate_region(region, current)
                except (ValueError, RuntimeError) as error:
                    canvas.itemconfigure(hint, text=str(error))
                    return
                self.region = region
                cancel()
                self.toggle_button.configure(state='normal')
                self.toggle()

            hint = canvas.create_text(12, 55, anchor='w', text='', fill='#ffb95e', font=(FONT, -16))
            canvas.bind('<ButtonPress-1>', press)
            canvas.bind('<B1-Motion>', drag)
            canvas.bind('<ButtonRelease-1>', release)
            overlay.bind('<Escape>', cancel)
            overlay.focus_force()
        except Exception as error:
            if self.selector is not None:
                self.selector.destroy()
                self.selector = None
            self.root.deiconify()
            self.render(QuizMatch('no_match', reason=f'无法框选：{error}'))

    def _emit(self, event):
        if not self.stop_event.is_set():
            try:
                self.events.put_nowait(event)
            except queue.Full:
                pass  # UI expiry still revokes answers if the consumer stalls.

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                job = self.jobs.get(timeout=.2)
            except queue.Empty:
                continue
            generation, region = job
            try:
                pixels = capture_region(region)
                digest = hashlib.blake2b(pixels.tobytes(), digest_size=16).digest()
                if digest != self.previous_digest or self.previous_result is None:
                    self._emit((generation, 'pending', None, '', time.monotonic()))
                    if self.ocr is None:
                        self.ocr = QuizOCR()
                    raw, confidence = self.ocr.recognize(pixels)
                    result = match_question(raw, confidence, self.entries)
                    # Re-read before publishing an expensive OCR result: reject
                    # any frame that changed while the worker was recognizing it.
                    latest = capture_region(region)
                    if hashlib.blake2b(latest.tobytes(), digest_size=16).digest() != digest:
                        raise ValueError('画面正在变化，等待题目稳定')
                    self.previous_digest = digest
                    self.previous_result = (result, raw)
                result, raw = self.previous_result
                self._emit((generation, 'done', result, raw, time.monotonic()))
            except Exception as error:
                self.previous_digest = None
                self.previous_result = None
                self._emit((generation, 'done', QuizMatch('no_match', reason=str(error)[:180]), '', time.monotonic()))
            finally:
                self._emit((generation, 'idle', None, '', time.monotonic()))

    def scan(self):
        if self.closed:
            return
        if self.running and not self.busy and self.region:
            self.busy = True
            self.jobs.put_nowait((self.state.generation, self.region))
        self.scan_id = self.root.after(800, self.scan)

    def poll(self):
        if self.closed:
            return
        while True:
            try:
                generation, kind, result, raw, timestamp = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'idle':
                self.busy = False
            elif generation == self.state.generation and self.running:
                if kind == 'pending':
                    self.state.accept(generation, QuizMatch('no_match', reason='画面变化，正在识别新题目…'))
                    self.render(self.state.result)
                elif time.monotonic()-timestamp < 2.5 and self.state.accept(generation, result):
                    self.last_result_at = timestamp
                    self.render(result, raw)
        if (self.running and self.state.result.entry is not None
                and time.monotonic()-self.last_result_at > 2.5):
            self.state.clear('识别超时，等待新画面')
            self.render(self.state.result)
        self.poll_id = self.root.after(100, self.poll)

    def search_bank(self, *_):
        query = normalize(self.search.get())
        self.filtered = [row for row in self.entries if not query or
                         query in normalize(row.question+' '+row.answer)][:39]
        self.matches.delete(0, 'end')
        for row in self.filtered:
            self.matches.insert('end', row.question)

    def show_manual(self, event=None):
        selection = self.matches.curselection()
        if selection:
            self.pause()
            row = self.filtered[selection[0]]
            self.render(QuizMatch('matched', row, reason='手动查题 · 已暂停屏幕识别，请核对游戏题干与选项'))

    def close(self):
        self.closed = True
        self.running = False
        self.state.clear()
        self.stop_event.set()
        self.root.after_cancel(self.poll_id)
        self.root.after_cancel(self.scan_id)
        if self.selector is not None:
            self.selector.destroy()
        self.root.destroy()


def main():
    physical_dpi()
    root = tk.Tk()
    QuizWindow(root)
    root.mainloop()


if __name__ == '__main__':
    main()
