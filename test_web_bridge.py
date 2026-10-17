"""HTTP boundary, single-writer commands, and read-only history contracts."""
from concurrent.futures import Future
from contextlib import closing
from datetime import datetime
import http.client
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app_time import BEIJING
from history_store import HistoryStore
from web_bridge import Bridge, Handler, LoopbackServer, PAGES, csv_bytes


class WebHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'history.sqlite3'
        store = HistoryStore(self.path)
        first = datetime(2026, 10, 16, 23, 59, 58, tzinfo=BEIJING).timestamp()
        store.begin(first)
        self.session = store.active
        for index, stamp in enumerate((first, first+3)):
            store.append(['', index+1, 100+index*10, 10, 10+index*10, 600, 36000, 'ok', '=unsafe'], now=stamp)
        store.db.close()
        self.bridge = Bridge.__new__(Bridge)
        self.bridge.token = 'test-token-32-characters-long-not-a-secret'
        self.bridge.history_path = self.path
        self.bridge.snapshot = {'total': 20, 'private': 'local-only'}
        self.bridge.lock = threading.Lock()
        self.bridge.commands = queue.Queue(maxsize=2)
        self.server = LoopbackServer(('127.0.0.1', 0), Handler)
        self.server.bridge = self.bridge
        self.bridge.port = self.server.server_port
        self.bridge.origins = {PAGES, f'http://127.0.0.1:{self.bridge.port}'}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path='/api/state', method='GET', auth=True, origin=PAGES, body=None, extra=None):
        with closing(http.client.HTTPConnection('127.0.0.1', self.bridge.port, timeout=4)) as client:
            headers = {'Origin': origin}
            if auth:
                headers['Authorization'] = 'Bearer '+self.bridge.token
            if body is not None:
                body = json.dumps(body).encode('utf-8')
                headers['Content-Type'] = 'application/json'
            headers.update(extra or {})
            client.request(method, path, body=body, headers=headers)
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()

    def test_no_secret_or_history_without_pairing(self):
        status, headers, body = self.request(auth=False)
        self.assertEqual(status, 403)
        self.assertNotIn(b'local-only', body)
        self.assertNotIn(self.bridge.token.encode(), body)
        status, _, body = self.request('/')
        self.assertEqual(status, 200)
        self.assertNotIn(self.bridge.token.encode(), body)

    def test_rejects_other_origins_and_dns_rebinding(self):
        self.assertEqual(self.request(origin='https://evil.example')[0], 403)
        self.assertEqual(self.request(extra={'Host':'evil.example'})[0], 403)
        self.assertEqual(self.request(origin='null')[0], 403)

    def test_pages_cors_and_private_network_preflight(self):
        status, headers, _ = self.request(method='OPTIONS', auth=False)
        self.assertEqual(status, 204)
        self.assertEqual(headers['Access-Control-Allow-Origin'], PAGES)
        self.assertEqual(headers['Access-Control-Allow-Private-Network'], 'true')
        self.assertNotEqual(headers['Access-Control-Allow-Origin'], '*')
        self.assertEqual(self.request(method='OPTIONS', auth=False, origin='https://evil.example')[0], 403)

    def test_no_generic_filesystem_route(self):
        for path in ('/../app.py', '/web_bridge.py', '/api/../data/history.sqlite3', '/.git/config'):
            self.assertEqual(self.request(path)[0], 404)

    def test_history_dates_and_session_export_cross_midnight(self):
        before = self.path.read_bytes()
        status, _, body = self.request('/api/history?action=days')
        self.assertEqual(status, 200)
        self.assertEqual([d['day'] for d in json.loads(body)['days']], ['2026-10-17','2026-10-16'])
        status, _, body = self.request('/api/history?day=2026-10-17&window=60')
        data = json.loads(body)
        self.assertEqual((status, data['gain'], data['total_rows']), (200, 10, 1))
        status, headers, body = self.request('/api/history?action=current_csv&session='+self.session)
        self.assertEqual(status, 200)
        self.assertTrue(headers['Content-Type'].startswith('text/csv'))
        text = body.decode('utf-8-sig')
        self.assertIn('2026-10-16 23:59:58', text)
        self.assertIn('2026-10-17 00:00:01', text)
        self.assertIn("'=unsafe", text)
        self.assertEqual(before, self.path.read_bytes())

    def test_invalid_queries_return_errors_without_sql_execution(self):
        self.assertEqual(self.request('/api/history?day=oops')[0], 400)
        self.assertEqual(self.request('/api/history?window=42')[0], 400)
        self.assertEqual(self.request('/api/history?day=2026-10-17&session=%27%20OR%201=1--')[0], 200)

    def test_command_is_queued_not_executed_on_http_worker(self):
        result = []
        worker = threading.Thread(target=lambda: result.append(self.request('/api/command/recording', method='POST', body={'enabled':True})))
        worker.start()
        name, data, future, deadline = self.bridge.commands.get(timeout=2)
        self.assertEqual((name, data), ('recording', {'enabled':True}))
        self.assertGreater(deadline, time.monotonic())
        self.assertFalse(result)
        future.set_result({'ok':True})
        worker.join(timeout=3)
        self.assertEqual(result[0][0], 200)

    def test_request_shape_is_bounded(self):
        self.assertEqual(self.request('/api/command/settings',method='POST',body=[])[0],400)
        self.assertEqual(self.request('/api/command/settings',method='POST',body={'cap':'1'*17000})[0],400)


class WebCommandTests(unittest.TestCase):
    def make_bridge(self):
        bridge = Bridge.__new__(Bridge)
        bridge.backend = Mock()
        bridge.backend.status.get.return_value = 'ok'
        bridge.orb = Mock()
        bridge.orb.alpha = .58
        bridge.backend.flower.ahead = 15
        bridge.backend.storage_error = None
        bridge.backend.running = False
        bridge.backend.region = {'game_window':True}
        return bridge

    def test_recording_is_idempotent_not_a_toggle(self):
        bridge = self.make_bridge()
        bridge.command('recording', {'enabled':False})
        bridge.backend.toggle.assert_not_called()
        bridge.backend.running = True
        bridge.command('recording', {'enabled':True})
        bridge.backend.toggle.assert_not_called()

    def test_destructive_stats_commands_require_confirmation(self):
        bridge = self.make_bridge()
        for name in ('reset','level','rebase'):
            with self.assertRaises(ValueError):
                bridge.command(name, {})
        bridge.backend.reset.assert_not_called()
        bridge.command('reset', {'confirmed':True})
        bridge.backend.reset.assert_called_once_with(confirmed=True)

    def test_validates_settings_before_side_effects(self):
        bridge = self.make_bridge()
        for data in ({'cap':'-2'}, {'opacity':float('nan')}, {'ahead':31}):
            with self.assertRaises(ValueError):
                bridge.command('settings', data)
        bridge.backend.flower.configure.assert_not_called()
        bridge.backend.save.assert_not_called()

    def test_opacity_converts_web_fraction_to_native_percentage(self):
        bridge = self.make_bridge()
        bridge.command('settings', {'cap':'1200000', 'opacity':.58,
                                   'flower_enabled':True, 'music':True, 'ahead':15})
        bridge.orb.set_opacity.assert_called_once()
        self.assertAlmostEqual(bridge.orb.set_opacity.call_args.args[0], 58)

    def test_region_rejects_expired_capture_before_mutation(self):
        bridge = self.make_bridge()
        bridge.lock = threading.Lock()
        bridge.capture_ticket = ('ticket', {}, time.monotonic()-181)
        with self.assertRaises(ValueError):
            bridge.select_region({'ticket':'ticket','purpose':'exp','left':0,'top':0,'width':50,'height':20})
        bridge.backend.pause.assert_not_called()

    def test_region_maps_to_physical_monitor_coordinates(self):
        bridge = self.make_bridge()
        bridge.lock = threading.Lock()
        geo = {'hwnd':1,'client':{'left':-1920,'top':40,'width':1280,'height':720},'dpi':96}
        bridge.capture_ticket = ('ticket', geo, time.monotonic())
        with patch('game_capture.game_geometry', return_value=geo):
            bridge.select_region({'ticket':'ticket','purpose':'exp','left':20,'top':30,'width':300,'height':40})
        self.assertEqual(bridge.backend.region,dict(left=-1900,top=70,width=300,height=40))
        bridge.backend.pause.assert_called_once_with(reason='region_selection')

    def test_region_rejects_moved_window(self):
        bridge = self.make_bridge()
        bridge.lock = threading.Lock()
        bridge.capture_ticket = ('ticket', {'hwnd':1}, time.monotonic())
        with patch('game_capture.game_geometry', return_value={'hwnd':2}):
            with self.assertRaises(ValueError):
                bridge.select_region({'ticket':'ticket','purpose':'quiz','left':0,'top':0,'width':100,'height':100})
        bridge.backend.pause.assert_not_called()

    def test_expired_and_cancelled_queue_entries_never_execute(self):
        bridge = self.make_bridge()
        bridge.closed = False
        bridge.root = Mock()
        bridge.commands = queue.Queue()
        bridge.last_snapshot = time.monotonic()
        bridge.command = Mock()
        expired, cancelled = Future(), Future()
        cancelled.cancel()
        bridge.commands.put(('reset', {}, expired, time.monotonic()-1))
        bridge.commands.put(('reset', {}, cancelled, time.monotonic()+2))
        bridge.pump()
        bridge.command.assert_not_called()
        self.assertIsInstance(expired.exception(), ValueError)
        bridge.root.after.assert_called_once()

    def test_csv_neutralizes_formula_text_without_changing_numbers(self):
        value = csv_bytes([{'a':'=1+1','b':-5,'c':'@bad','d':'普通文字'}]).decode('utf-8-sig')
        self.assertIn("'=1+1,-5,'@bad,普通文字", value)


if __name__ == '__main__':
    unittest.main()
