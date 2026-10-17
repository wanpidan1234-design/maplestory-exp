"""Launch the web companion with the standalone user's data directory."""
import ctypes
import json
import os
from pathlib import Path
import runpy
import sys
import webbrowser


def main():
    local = Path(os.environ.get('LOCALAPPDATA', Path.home()/'AppData'/'Local'))/'MapleExpTracker'
    # The same named mutex as orb.py prevents a second recorder.
    kernel = ctypes.windll.kernel32
    kernel.OpenMutexW.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.OpenMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    existing = kernel.OpenMutexW(0x00100000, False, 'Local\\MapleExpTrackerHistoryV1')
    if existing:
        kernel.CloseHandle(existing)
        try:
            import urllib.request
            from urllib.parse import urlsplit, parse_qs
            receipt = json.loads((local/'web-connection.json').read_text(encoding='utf-8'))
            parts = urlsplit(receipt['url'])
            if parts.hostname != '127.0.0.1' or parts.port != 17321:
                raise ValueError('Invalid companion address')
            token = parse_qs(parts.fragment)['token'][0]
            request = urllib.request.Request('http://127.0.0.1:17321/api/state', headers={'Authorization':'Bearer '+token})
            with urllib.request.urlopen(request, timeout=2) as response:
                state = json.load(response)
            if not state.get('bridge_version'):
                raise ValueError('Old companion')
            webbrowser.open(receipt['hosted_url'] if '--hosted' in sys.argv else receipt['url'])
            return
        except Exception:
            ctypes.windll.user32.MessageBoxW(None,
                '当前桌面版尚未提供网页连接。请先从悬浮球菜单正常退出，再运行 start-web.cmd。历史数据会保留。',
                '冒险岛 · 网页服务', 0x40)
            return
    os.environ.setdefault('MAPLE_EXP_DATA_DIR', str(local/'data'))
    if '--web' not in sys.argv:
        sys.argv.append('--web')
    # Source installs retain the existing event-driven review policy. No timer
    # or automatic model requests are added by this entry point.
    runpy.run_module('orb', run_name='__main__')


if __name__ == '__main__':
    main()
