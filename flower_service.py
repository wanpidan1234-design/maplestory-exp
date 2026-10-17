"""Bounded local screen worker and a short musical Holy Symbol reminder."""
import json
from pathlib import Path
import queue
import threading
import time

from flower_buff import FlowerVision, FlowerWatch


def flower_pixels_visible(hwnd, icon_roi, visible):
    """Check icon and timer edges, not only the icon's unoccluded centerline."""
    x, y, width, height = (icon_roi[k] for k in ('left', 'top', 'width', 'height'))
    timer_top = y+round(height*.65)
    for line in (y+2, y+height//2, timer_top, y+height-1, y+height+2):
        # roi_visible checks three horizontal points. Narrow probes at both
        # edges also protect a partially covered tens digit.
        if not visible(hwnd, dict(left=x, top=line, width=width, height=1)):
            return False
        for edge in (x, x+width-1):
            if not visible(hwnd, dict(left=edge, top=line, width=1, height=1)):
                return False
    return True


class FlowerSound:
    def __init__(self):
        self.playing_until = 0
        self.error = None

    def play(self, test=False):
        if test and time.monotonic() < self.playing_until:
            return False
        try:
            import winsound
            path = Path(__file__).parent/'assets'/'flower-reminder.wav'
            if not path.is_file():
                raise OSError('missing reminder audio')
            # ASYNC replaces the one current sound. A real reminder has
            # priority over a test; no subprocess, speech engine or queue.
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            self.playing_until, self.error = time.monotonic()+1.6, None
            return True
        except (OSError, RuntimeError, ImportError):
            self.error = '音乐播放失败，请检查声音输出'
            return False

    def close(self):
        if time.monotonic() < self.playing_until:
            try:
                import winsound
                winsound.PlaySound(None, 0)
            except (OSError, RuntimeError, ImportError):
                pass
        self.playing_until = 0


class FlowerService:
    def __init__(self, config_path):
        self.path = Path(config_path)
        self.enabled, self.music, self.ahead = True, True, 15
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            self.enabled = saved.get('enabled', True) is True
            self.music = saved.get('music', saved.get('voice', True)) is True
            self.ahead = max(5, min(30, int(saved.get('ahead', 15))))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.watch = FlowerWatch(self.ahead)
        self.sound = FlowerSound()
        self.active = None
        self.epoch = 0
        self.busy = False
        self.closed = False
        self.next_capture = 0
        self.results = queue.Queue(maxsize=1)
        self.vision = None
        self.last_icon = None
        self.last_alert = None
        self.last_score = None

    def configure(self, enabled, music, ahead):
        ahead = int(ahead)
        if not 5 <= ahead <= 30:
            raise ValueError('提醒时间应为 5～30 秒')
        self.enabled, self.music, self.ahead = bool(enabled), bool(music), ahead
        self.watch.ahead = ahead
        self.epoch += 1
        self.watch.unavailable('等待花 Buff' if enabled else '花提醒已关闭')
        if not enabled or not music:
            self.sound.close()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(enabled=self.enabled, music=self.music, ahead=ahead)), encoding='utf-8')
        temporary.replace(self.path)

    def _capture(self, epoch):
        try:
            import mss
            import numpy as np
            from game_capture import game_geometry, GameCapture
            geo = game_geometry(include_monitor=False)
            client = geo['client']
            width = min(client['width'], 1400)
            roi = dict(left=client['left']+client['width']-width, top=client['top'],
                       width=width, height=min(160, client['height']))
            with mss.mss() as capture:
                frame = np.asarray(capture.grab(roi))[:, :, :3].copy()
            captured = time.monotonic()
            if self.vision is None:
                self.vision = FlowerVision()
            reading = self.vision.read(frame)
            identity = (geo['hwnd'], client['width'], client['height'], geo.get('dpi'))
            if reading.present:
                x, y, w, h = reading.box
                icon_roi = dict(left=roi['left']+x, top=roi['top']+y, width=w, height=h)
                if not flower_pixels_visible(geo['hwnd'], icon_roi, GameCapture.roi_visible):
                    raise RuntimeError('花 Buff 被遮挡')
                self.last_icon = (identity, reading.box)
            elif self.last_icon:
                old_identity, box = self.last_icon
                if old_identity != identity:
                    self.last_icon = None
                    raise RuntimeError('游戏窗口已变化，重新识别花')
                x, y, w, h = box
                icon_roi = dict(left=roi['left']+x, top=roi['top']+y, width=w, height=h)
                if not flower_pixels_visible(geo['hwnd'], icon_roi, GameCapture.roi_visible):
                    raise RuntimeError('花 Buff 被其他窗口遮挡')
            result = (epoch, captured, reading, None)
        except Exception as error:
            result = (epoch, time.monotonic(), None, str(error)[:100])
        if not self.closed:
            try:
                self.results.put_nowait(result)
            except queue.Full:
                pass

    def poll(self, recording):
        if self.closed:
            return
        now = time.monotonic()
        active = bool(recording and self.enabled)
        if active != self.active:
            self.active = active
            self.epoch += 1
            self.watch.unavailable('等待花 Buff' if active else '花提醒已暂停' if self.enabled else '花提醒已关闭')
            if not active:
                self.sound.close()
        try:
            epoch, captured, reading, error = self.results.get_nowait()
            self.busy = False
            if epoch == self.epoch and active and now-captured <= 3:
                if error:
                    self.watch.unavailable(error)
                else:
                    self.last_score = round(reading.score, 3)
                    if self.watch.observe(reading, captured):
                        self.last_alert = time.time()
                        if self.music:
                            self.sound.play()
        except queue.Empty:
            pass
        if active and self.watch.check_due(now):
            self.last_alert = time.time()
            if self.music:
                self.sound.play()
        if active and not self.busy and now >= self.next_capture:
            self.busy = True
            self.next_capture = now+.3
            threading.Thread(target=self._capture, args=(self.epoch,), daemon=True,
                             name='flower-buff').start()

    def snapshot(self):
        result = self.watch.snapshot(time.monotonic())
        result.update(enabled=self.enabled, music=self.music, last_alert=self.last_alert,
                      icon_score=self.last_score, audio_error=self.sound.error)
        return result

    def close(self):
        self.closed = True
        self.epoch += 1
        self.sound.close()
