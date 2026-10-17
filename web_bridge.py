"""Authenticated loopback web companion. The desktop remains the only writer.

HTTP workers read independent read-only database connections. Mutations travel
through a bounded queue to Tk's owner thread and expire before execution.
No browser request can launch a shell, choose a file path, or wake a model.
"""
from concurrent.futures import Future, TimeoutError
from contextlib import closing
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import base64
import csv
import io
import json
import math
import mimetypes
import queue
import secrets
import sqlite3
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
PAGES = 'https://wanpidan1234-design.github.io'
PORT = 17321
VERSION = '1.0.0'


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')


def csv_bytes(rows):
    stream = io.StringIO(newline='')
    if rows:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            # OCR and names are untrusted spreadsheet text.
            writer.writerow({k: "'"+v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v
                             for k, v in row.items()})
    return stream.getvalue().encode('utf-8-sig')


class LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16


class Bridge:
    def __init__(self, backend, orb, port=PORT):
        self.backend, self.orb = backend, orb
        self.root = backend.root
        self.token = secrets.token_urlsafe(32)
        self.commands = queue.Queue(maxsize=16)
        self.closed = False
        self.snapshot = {}
        self.lock = threading.Lock()
        self.quiz_lock = threading.Lock()
        self.quiz_engine = None
        self.quiz_region = None
        self.quiz_generation = 0
        self.quiz_running = False
        self.quiz_result = {'status': 'idle', 'reason': '框选题目与选项后开始识别'}
        self.quiz_stop = threading.Event()
        self.capture_ticket = None
        self.last_snapshot = 0
        self.history_path = Path(backend.history.path).resolve()
        self.server = LoopbackServer(('127.0.0.1', port), Handler)
        self.server.bridge = self
        self.port = self.server.server_port
        self.local_origin = f'http://127.0.0.1:{self.port}'
        self.origins = {PAGES, self.local_origin, f'http://localhost:{self.port}'}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.quiz_thread = threading.Thread(target=self.quiz_worker, daemon=True)
        self.quiz_thread.start()
        self.pump()

    def close(self):
        self.closed = True
        self.quiz_stop.set()
        while True:
            try:
                _, _, future, _ = self.commands.get_nowait()
                future.cancel()
            except queue.Empty:
                break
        threading.Thread(target=self.server.shutdown, daemon=True).start()
        self.server.server_close()

    def url(self, hosted=False):
        base = PAGES+'/maplestory-exp/' if hosted else self.local_origin+'/'
        return f'{base}#token={self.token}'

    def pump(self):
        if self.closed:
            return
        for _ in range(4):
            try:
                name, data, future, deadline = self.commands.get_nowait()
            except queue.Empty:
                break
            if time.monotonic() > deadline or not future.set_running_or_notify_cancel():
                if not future.done():
                    future.set_exception(ValueError('操作已过期，请重新检查状态'))
                continue
            try:
                future.set_result(self.command(name, data))
                self.last_snapshot = 0
            except Exception as error:
                future.set_exception(error)
        try:
            if time.monotonic()-self.last_snapshot < .75:
                return
            state = self.backend.status_payload()
            state.update(bridge_version=VERSION, settings=dict(cap=self.backend.manual.get(),
                region=self.backend.region, opacity=self.orb.alpha, flower_ahead=self.backend.flower.ahead),
                review=dict(getattr(self.orb.review_view, 'data', {}) or {}))
            try:
                state['cap'] = self.backend.cap() or (self.backend.latest.cap if self.backend.latest else None)
            except ValueError:
                state['cap'] = None
            from ui_midnight import percent_rates, level_up_minutes
            minute, hour = percent_rates(state['display_primary_per_minute'],
                state['display_current_exp'], state['display_current_percent'], state['cap'])
            state['percent_per_minute'], state['percent_per_hour'] = minute, hour
            state['level_eta_minutes'] = level_up_minutes(state['display_primary_per_minute'],
                state['display_current_exp'], state['display_current_percent'], state['cap'])
            state['blocked'] = bool(self.backend.tracker.blocked)
            state['raw'] = self.backend.raw.get()
            if '--no-startup-review' in sys.argv:
                state['review'] = dict(state='disabled', summary='此启动方式已关闭自动开发评审。')
            with self.quiz_lock:
                state['quiz'] = dict(self.quiz_result, running=self.quiz_running,
                                     region=asdict(self.quiz_region) if self.quiz_region else None)
            with self.lock:
                self.snapshot = json.loads(json_bytes(state))
            self.last_snapshot = time.monotonic()
        except Exception as error:
            self.backend.log_event('web_snapshot_failed', error_type=type(error).__name__, interval=10)
        finally:
            self.root.after(100, self.pump)

    def submit(self, name, data):
        future = Future()
        try:
            self.commands.put_nowait((name, data, future, time.monotonic()+4))
        except queue.Full:
            raise ValueError('操作队列繁忙，请稍后重试')
        try:
            return future.result(timeout=5)
        except TimeoutError:
            future.cancel()
            raise ValueError('操作超时；请刷新状态后确认结果，不要重复提交')

    def command(self, name, data):
        app, orb = self.backend, self.orb
        if name == 'recording':
            enabled = data.get('enabled') is True
            if enabled:
                app.cap()  # Validate without a native modal dialog on a web action.
            if enabled != app.running:
                if enabled and not app.region:
                    app.auto_game()
                else:
                    app.toggle()
            if app.running != enabled:
                raise ValueError(app.status.get())
        elif name == 'locate':
            app.auto_game()
        elif name in ('rebase', 'level', 'reset'):
            if data.get('confirmed') is not True:
                raise ValueError('此操作需要明确确认')
            if name == 'reset':
                app.reset(confirmed=True)
            else:
                if app.storage_error:
                    raise ValueError(app.storage_error)
                if app.latest is None:
                    raise ValueError('尚无可供确认的读数')
                if name == 'level' and not app.tracker.blocked:
                    raise ValueError('仅在检测到经验下降后使用升级确认')
                getattr(app, name)()
        elif name == 'settings':
            cap = str(data.get('cap', '')).replace(',', '').strip()
            if cap and (not cap.isdecimal() or not 0 < int(cap) <= 10**15):
                raise ValueError('升级所需经验应为正整数，留空则自动识别')
            opacity = float(data.get('opacity', orb.alpha))
            if not math.isfinite(opacity) or not .45 <= opacity <= 1:
                raise ValueError('悬浮球透明度应为 45%～100%')
            ahead = int(data.get('ahead', app.flower.ahead))
            if not 5 <= ahead <= 30:
                raise ValueError('提醒时间应为 5～30 秒')
            app.flower.configure(data.get('flower_enabled') is True, data.get('music') is True, ahead)
            app.flower_enabled.set(app.flower.enabled)
            app.flower_music.set(app.flower.music)
            app.flower_ahead.set(str(ahead))
            app.manual.set(cap)
            app.save()
            orb.set_opacity(opacity*100)
        elif name == 'sound':
            if not app.flower.sound.play(test=True) and app.flower.sound.error:
                raise ValueError(app.flower.sound.error)
        elif name == 'window':
            target = data.get('target')
            actions = {'panel': orb.toggle_panel, 'settings': orb.show_settings,
                       'history': orb.show_history, 'combat': orb.show_combat,
                       'multiscale': orb.show_multiscale, 'animation': orb.celebrate_level}
            if target == 'orb':
                orb.ball.deiconify() if data.get('visible', True) else orb.ball.withdraw()
            elif target == 'review':
                orb.review_view.show_details()
            elif target in actions:
                actions[target]()
            else:
                raise ValueError('未知窗口')
        elif name == 'region':
            self.select_region(data)
        elif name == 'quiz':
            with self.quiz_lock:
                if data.get('enabled') and self.quiz_region is None:
                    raise ValueError('请先框选题目与选项')
                self.quiz_generation += 1
                self.quiz_running = data.get('enabled') is True
                self.quiz_result = {'status': 'waiting' if self.quiz_running else 'paused',
                                    'reason': '等待读题' if self.quiz_running else '已暂停识别'}
        else:
            raise ValueError('未知操作')
        return {'ok': True, 'status': app.status.get()}

    def screenshot(self):
        import cv2
        from game_capture import game_geometry
        from quiz_capture import QuizRegion, capture_region
        geo = game_geometry(include_monitor=False)
        client = geo['client']
        # Capture rejects occlusion and checks geometry before/after sampling.
        region = QuizRegion(geo['hwnd'], (client['width'], client['height']), geo['dpi'],
                            0, 0, client['width'], client['height'])
        frame = capture_region(region)
        success, encoded = cv2.imencode('.png', frame)
        if not success:
            raise ValueError('截图编码失败')
        ticket = secrets.token_urlsafe(16)
        with self.lock:
            self.capture_ticket = (ticket, geo, time.monotonic())
        return {'image': 'data:image/png;base64,'+base64.b64encode(encoded).decode('ascii'),
                'width': client['width'], 'height': client['height'], 'ticket': ticket}

    def select_region(self, data):
        from game_capture import game_geometry
        from quiz_capture import QuizRegion, validate_region
        with self.lock:
            capture = self.capture_ticket
        if not capture or data.get('ticket') != capture[0] or time.monotonic()-capture[2] > 180:
            raise ValueError('截图已过期，请重新截取游戏画面')
        geo = game_geometry(include_monitor=False)
        if geo != capture[1]:
            raise ValueError('游戏位置或尺寸已变化，请重新截图框选')
        c = geo['client']
        region = QuizRegion(geo['hwnd'], (c['width'], c['height']), geo['dpi'],
                            *(int(data[k]) for k in ('left', 'top', 'width', 'height')))
        roi = validate_region(region, geo)
        if data.get('purpose') == 'quiz':
            with self.quiz_lock:
                self.quiz_generation += 1
                self.quiz_region = region
                self.quiz_running = False
                self.quiz_result = {'status': 'ready', 'reason': '已框选，点击开始读题'}
        elif data.get('purpose') == 'exp':
            self.backend.pause(reason='region_selection')
            self.backend.region = roi
            self.backend.latest = None
            self.backend.save()
            self.backend.status.set('已框选经验栏，点击继续记录。')
        else:
            raise ValueError('未知选区类型')

    def quiz_worker(self):
        while not self.quiz_stop.wait(.8):
            with self.quiz_lock:
                region, generation, running = self.quiz_region, self.quiz_generation, self.quiz_running
            if not running or region is None:
                continue
            try:
                from quiz_capture import capture_region
                from quiz_helper import QuizOCR
                from quiz_bank import match_question
                frame = capture_region(region)
                if self.quiz_engine is None:
                    self.quiz_engine = QuizOCR()
                raw, confidence = self.quiz_engine.recognize(frame)
                result = dict(asdict(match_question(raw, confidence)), raw=raw, updated=time.time())
            except Exception as error:
                result = {'status': 'error', 'reason': str(error), 'updated': time.time()}
            with self.quiz_lock:
                if generation == self.quiz_generation and self.quiz_running:
                    self.quiz_result = result

    def history(self, args):
        from history_store import HistoryStore
        from app_time import local_day, day_bounds, format_time
        from ui_charts import rate_plot_rows, reduce_plot_rows
        day = args.get('day', local_day())
        day_bounds(day)  # validate before opening the database
        session = args.get('session') or None
        window = int(args.get('window', '60'))
        if window not in (0, 60, 120, 300, 600, 1800):
            raise ValueError('不支持的分均窗口')
        with closing(sqlite3.connect(self.history_path.as_uri()+'?mode=ro', uri=True, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            store = HistoryStore.__new__(HistoryStore)
            store.db = db
            action = args.get('action', 'overview')
            if action == 'days':
                return {'days': store.days()}
            if action == 'live':
                cutoff = time.time()-1800
                rows = store.recent_rate_samples(session, cutoff) if session else []
                minutes = [dict(r) for r in db.execute('''SELECT CAST(timestamp/60 AS INTEGER)*60 timestamp,
                    SUM(gain) gain FROM samples WHERE session_id=? AND timestamp>=?
                    AND COALESCE(status,'')!='legacy_summary'
                    GROUP BY CAST(timestamp/60 AS INTEGER) ORDER BY timestamp''', (session, cutoff))]
                today_minutes = store.day_minutes(day)
                gain = db.execute('SELECT COALESCE(SUM(gain),0) FROM samples WHERE timestamp>=? AND timestamp<?', day_bounds(day)).fetchone()[0]
                return dict(plot=reduce_plot_rows(rate_plot_rows(rows, window or None)),
                            minutes=minutes, today_minutes=today_minutes, today_gain=gain)
            if action == 'combat':
                from combat_service import CombatStore
                combat = CombatStore.__new__(CombatStore)
                combat.db, combat.session = db, session
                return {'days': combat.days(), 'events': combat.events(day, 2000),
                        'kills': combat.kills(day), 'kill_days': combat.kill_days()}
            if action == 'combat_csv':
                rows = [dict(r) for r in db.execute('SELECT * FROM combat_events WHERE timestamp>=? AND timestamp<? ORDER BY timestamp,id', day_bounds(day))]
                for row in rows:
                    row['time_beijing'] = format_time(row['timestamp'], '%Y-%m-%d %H:%M:%S')
                return csv_bytes(rows)
            if action == 'kills_csv':
                rows = [dict(r) for r in db.execute('SELECT * FROM monster_kills WHERE timestamp>=? AND timestamp<? ORDER BY timestamp,id', day_bounds(day))]
                for row in rows:
                    row['time_beijing'] = format_time(row['timestamp'], '%Y-%m-%d %H:%M:%S')
                return csv_bytes(rows)
            if action == 'current_csv':
                if not session:
                    return csv_bytes([])
                rows = [dict(r) for r in db.execute('SELECT * FROM samples WHERE session_id=? ORDER BY timestamp,id', (session,))]
            else:
                rows = store.samples(day, session)
            if action in ('csv', 'current_csv'):
                for row in rows:
                    row['time_beijing'] = format_time(row['timestamp'], '%Y-%m-%d %H:%M:%S')
                return csv_bytes(rows)
            if action != 'overview':
                raise ValueError('未知历史查询')
            offset = max(0, int(args.get('offset', 0)))
            return {'day': day, 'sessions': store.sessions(day), 'total_rows': len(rows),
                    'rows': rows[offset:offset+200], 'offset': offset,
                    'minutes': store.day_minutes(day, session),
                    'plot': reduce_plot_rows(rate_plot_rows(rows, window or None)),
                    'window': window, 'gain': sum(row['gain'] for row in rows)}

    def knowledge(self, args):
        directory = ROOT/'knowledge'/'maplestory-classic'
        # Explicitly local. Never bundled into public Pages assets.
        files = sorted(p for p in directory.rglob('*.md') if p.is_file()
                       and 'review-backups' not in p.parts) if directory.is_dir() else []
        requested = args.get('document')
        query = args.get('q', '').casefold()[:120]
        if requested:
            match = next((p for p in files if p.relative_to(directory).as_posix() == requested), None)
            if match is None:
                raise ValueError('文档不存在')
            return {'document': requested, 'content': match.read_text(encoding='utf-8-sig')[:500000]}
        result = []
        for path in files:
            content = path.read_text(encoding='utf-8-sig')
            if not query or query in content.casefold() or query in path.name.casefold():
                title = next((line.lstrip('# ').strip() for line in content.splitlines() if line.startswith('# ')), path.stem)
                result.append({'document': path.relative_to(directory).as_posix(), 'title': title})
        claims = []
        index = directory/'index'/'knowledge.sqlite3'
        if index.is_file() and query:
            with closing(sqlite3.connect(index.resolve().as_uri()+'?mode=ro', uri=True, timeout=3)) as db:
                where = ["scope='cn-classic'", "status!='historical'"]
                values = []
                for word in query.split()[:8]:
                    where.append('instr(lower(search_text), ?) > 0')
                    values.append(word)
                rows = db.execute('SELECT record FROM claims WHERE '+' AND '.join(where)+
                                  ' ORDER BY confidence DESC,id LIMIT 50', values)
                for row in rows:
                    record = json.loads(row[0])
                    record['sources'] = []
                    for sid in record.get('source_ids', []):
                        source = db.execute('SELECT record FROM sources WHERE id=?', (sid,)).fetchone()
                        if source:
                            record['sources'].append(json.loads(source[0]))
                    claims.append(record)
        return {'documents': result[:300], 'total': len(result), 'claims': claims,
                'index_updated': index.stat().st_mtime if index.is_file() else None}


class Handler(BaseHTTPRequestHandler):
    server_version = 'MapleCompanion/1'

    def log_message(self, *_):
        pass  # No tokens, OCR, or request contents in access logs.

    @property
    def bridge(self):
        return self.server.bridge

    def send(self, status, body, content_type='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json_bytes(body)
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        origin = self.headers.get('Origin')
        if origin in self.bridge.origins:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def valid_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.bridge.port}', f'localhost:{self.bridge.port}')

    def authorized(self):
        origin = self.headers.get('Origin')
        return (self.valid_host() and (origin is None or origin in self.bridge.origins)
                and secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+self.bridge.token))

    def do_OPTIONS(self):
        if not self.valid_host() or self.headers.get('Origin') not in self.bridge.origins:
            self.send(403, {'error': '来源不被允许'})
        else:
            self.send(204, b'')

    def do_GET(self):
        parsed = urlsplit(self.path)
        if not self.valid_host():
            return self.send(403, {'error': 'Invalid Host'})
        if not parsed.path.startswith('/api/'):
            paths = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js',
                     '/style.css': 'style.css', '/icon.png': 'icon.png'}
            file = paths.get(parsed.path)
            if not file or not (ROOT/'web'/file).is_file():
                return self.send(404, {'error': '页面不存在'})
            return self.send(200, (ROOT/'web'/file).read_bytes(), mimetypes.guess_type(file)[0] or 'application/octet-stream')
        if not self.authorized():
            return self.send(403, {'error': '连接码无效，请从本机服务重新打开网页'})
        args = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        try:
            if parsed.path == '/api/state':
                with self.bridge.lock:
                    result = self.bridge.snapshot
            elif parsed.path == '/api/history':
                result = self.bridge.history(args)
            elif parsed.path == '/api/knowledge':
                result = self.bridge.knowledge(args)
            elif parsed.path == '/api/quiz-bank':
                from quiz_bank import load_bank, normalize
                q = normalize(args.get('q', ''))
                result = {'entries': [asdict(e) for e in load_bank() if not q or q in normalize(e.question+e.answer)]}
            elif parsed.path == '/api/quiz-pdf':
                from quiz_helper import desktop_quiz_pdf
                path = desktop_quiz_pdf()
                if not path.is_file():
                    return self.send(404, {'error': '桌面未找到三转答题速查 PDF，可继续使用网页题库查询'})
                return self.send(200, path.read_bytes(), 'application/pdf')
            else:
                return self.send(404, {'error': '接口不存在'})
            if isinstance(result, bytes):
                self.send(200, result, 'text/csv; charset=utf-8')
            else:
                self.send(200, result)
        except (ValueError, KeyError, OSError, sqlite3.Error) as error:
            self.send(400, {'error': str(error)})

    def do_POST(self):
        if not self.authorized():
            return self.send(403, {'error': '连接码或来源无效'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.send(400, {'error': '请求格式无效'})
            self.connection.settimeout(5)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('请求必须是对象')
            path = urlsplit(self.path).path
            if path == '/api/screenshot':
                # The browser can cover the game on a single-monitor desktop.
                # This delay runs on the HTTP worker, never blocks accounting.
                delay = float(data.get('delay', 0))
                if not math.isfinite(delay) or not 0 <= delay <= 5:
                    raise ValueError('截图等待时间无效')
                if delay:
                    time.sleep(delay)
                result = self.bridge.screenshot()
            elif path.startswith('/api/command/'):
                result = self.bridge.submit(path.removeprefix('/api/command/'), data)
            else:
                return self.send(404, {'error': '接口不存在'})
            self.send(200, result)
        except Exception as error:
            self.send(400, {'error': str(error)})
