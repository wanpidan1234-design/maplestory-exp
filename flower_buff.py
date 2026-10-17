"""Holy Symbol pixels and reminder state; no game input or EXP accounting."""
from dataclasses import dataclass
import math
import re
from pathlib import Path

import cv2
import numpy as np

from image_assets import read_image


@dataclass(frozen=True)
class FlowerReading:
    present: bool
    seconds: int | None = None
    minutes: int | None = None
    score: float = 0.0
    box: tuple | None = None
    fill_fraction: float | None = None


class FlowerWatch:
    """Confirm visual reads, then maintain a labelled deadline across HUD cover."""
    def __init__(self, ahead=15):
        self.ahead = ahead
        self.alerted = False
        self.last = None
        self.last_at = None
        self.confirmed = None
        self.confirmed_at = None
        self.message = '等待花 Buff'
        self.state = 'waiting'
        self.deadline = None
        self.last_screen_at = None
        self.obscured = False
        self.anchor_value = None
        self.anchor_at = None
        self.deadline_estimated = False
        self.deadline_upper = None

    def unavailable(self, message='等待游戏画面'):
        self.last = self.confirmed = None
        self.last_at = self.confirmed_at = None
        self.message, self.state = message, 'waiting'
        self.deadline = self.last_screen_at = None
        self.anchor_value = self.anchor_at = None
        self.obscured = False
        self.deadline_estimated = False
        self.deadline_upper = None
        # Losing pixels is not proof of a new application. Keep the alert latch.

    def observe(self, reading, now):
        self.last_screen_at = now
        if not reading.present:
            self.last = self.last_at = None
            self.obscured = True
            self.message, self.state = '未见花 Buff', 'waiting'
            return self.check_due(now)
        previous, stamp = self.last, self.last_at
        self.last, self.last_at = reading, now
        self.message, self.state = '花已识别 · 等待读秒', 'present'
        self.obscured = True
        valid = previous is not None and stamp is not None and 0 < now-stamp <= 3
        if reading.minutes is not None:
            if valid and previous.minutes == reading.minutes:
                # Minute counts are rounded: never invent a precise deadline.
                # A confirmed minute count that contradicts the old seconds
                # proves a refresh, cancelling the old predicted expiration.
                contradiction = self.deadline is not None and self.deadline-now < reading.minutes*60-3
                refresh = contradiction and (not self.deadline_estimated or self.deadline_upper is None or
                                             now+reading.minutes*60 > self.deadline_upper+3)
                if self.deadline is None or contradiction:
                    # The minute label is coarse, but the icon's dark boundary
                    # carries a visual progress estimate. Keep this explicitly
                    # estimated until actual yellow seconds become available.
                    fraction = reading.fill_fraction
                    estimate = fraction*120 if fraction is not None else (reading.minutes+.5)*60
                    estimate = min(120, max(reading.minutes*60, min((reading.minutes+1)*60-1, estimate)))
                    self.deadline = now+estimate
                    self.deadline_estimated = True
                    upper = now+min(120, (reading.minutes+1)*60)
                    self.deadline_upper = upper if self.deadline_upper is None or refresh else min(self.deadline_upper, upper)
                    self.anchor_value = self.anchor_at = None
                    if refresh:
                        self.alerted = False
                self.confirmed, self.confirmed_at = reading, now
                self.message, self.state = f'花 · {reading.minutes} 分钟', 'active'
                self.obscured = False
            return False
        seconds = reading.seconds
        if seconds is None or not 0 <= seconds <= 59:
            return self.check_due(now)
        # Reject single-frame misreads and upward jumps until a second read
        # confirms the new countdown. Equal values are valid within one second.
        consistent = (valid and previous.seconds is not None and
                      -1 <= previous.seconds-seconds <= math.ceil(now-stamp)+1)
        if not consistent:
            # Do not fire the old estimate while a possible refresh is being
            # confirmed. The next frame either confirms it or resumes prediction.
            return False
        self.confirmed, self.confirmed_at = reading, now
        self.obscured = False
        was_estimated = self.deadline_estimated
        old_deadline = self.deadline
        self.deadline_estimated = False
        candidate_at = stamp if previous.seconds == seconds else now
        refreshed = (self.deadline is not None and seconds > 0 and
                     seconds != self.anchor_value and candidate_at+seconds > self.deadline+5 and
                     (not was_estimated or self.deadline_upper is None or candidate_at+seconds > self.deadline_upper+3))
        if refreshed:
            self.alerted = False
        if seconds != self.anchor_value or was_estimated:
            self.anchor_value = seconds
            self.anchor_at = candidate_at
        self.deadline = self.anchor_at + seconds
        self.deadline_upper = None
        self.message = f'花 · {seconds} 秒'
        self.state = 'soon' if seconds <= self.ahead else 'active'
        if old_deadline is None and seconds > self.ahead + 5:
            self.alerted = False
        return self.check_due(now)

    def check_due(self, now):
        fresh_screen = self.last_screen_at is not None and now-self.last_screen_at <= 3
        # A pending higher read may be a recast. Wait for confirmation, never
        # speak an old deadline over a possible new application.
        pending_refresh = (self.last is not None and self.deadline is not None and
                           ((self.last.seconds is not None and self.last.seconds > self.deadline-now+5) or
                            (self.last.minutes is not None and self.deadline-now < self.last.minutes*60-3)))
        if (fresh_screen and not pending_refresh and self.deadline is not None and
                0 < self.deadline-now <= self.ahead and not self.alerted):
            self.alerted = True
            return True
        return False

    def snapshot(self, now):
        fresh = self.confirmed_at is not None and now-self.confirmed_at <= 3
        estimated = bool(self.deadline is not None and (self.obscured or not fresh or self.deadline_estimated))
        if self.deadline is not None:
            seconds = max(0, math.ceil(self.deadline-now))
            if self.confirmed is not None and self.confirmed.seconds is not None:
                estimated = estimated or abs(self.confirmed.seconds-seconds) > 2
            state = 'soon' if seconds <= self.ahead else 'active'
            if seconds == 0:
                return dict(state='waiting', text='花 · 待确认', seconds=None, minutes=None,
                            alerted=self.alerted, ahead=self.ahead, estimated=True, remaining_seconds=0)
            minutes = seconds//60 if seconds >= 60 else None
            label = f'{minutes} 分钟' if minutes else f'{seconds} 秒'
            return dict(state=state, text=f'花 · {"预计 " if estimated else ""}{label}',
                        seconds=seconds if minutes is None else None, minutes=minutes, alerted=self.alerted,
                        ahead=self.ahead, estimated=estimated, remaining_seconds=max(0, self.deadline-now))
        return dict(state=self.state if fresh or self.state in ('waiting', 'present') else 'waiting',
                    text=self.message if fresh or self.state in ('waiting', 'present') else '等待花读秒',
                    seconds=self.confirmed.seconds if fresh else None,
                    minutes=self.confirmed.minutes if fresh else None,
                    alerted=self.alerted, ahead=self.ahead, estimated=False, remaining_seconds=None)


class FlowerVision:
    """Locate the icon in the top HUD, then read only its colored timer pixels."""
    def __init__(self, recognizer=None):
        icon = read_image(Path(__file__).parent/'assets'/'holy-symbol-anchor.png')
        if icon is None:
            raise RuntimeError('缺少花 Buff 识别模板')
        self.templates = []
        self.full_icons = {}
        # Source crop is the 56-pixel icon from the official skill card. The
        # lower 35% is omitted because the countdown covers that part in-game.
        for size in (28, 30, 32, 36, 40, 44, 48, 52, 56, 60, 64, 72):
            scaled = cv2.resize(icon, (size, size), interpolation=cv2.INTER_AREA)
            self.full_icons[size] = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
            patch = scaled[3:round(size*.65), 3:size-3]
            self.templates.append((size, cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)))
        self.recognizer = recognizer

    def locate(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        best = None
        candidates = []
        for size, template in self.templates:
            if template.shape[0] > gray.shape[0] or template.shape[1] > gray.shape[1]:
                continue
            result = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, point = cv2.minMaxLoc(result)
            candidates.append((score, size, template, point))
            if best is None or score > best[0]:
                best = score, point[0]-3, point[1]-3, size
        # The game darkens the icon from bottom to top as time runs out. Fit
        # that visual mask at the best coarse positions instead of mistaking a
        # half-darkened Holy Symbol for a missing icon. Small local searches
        # keep this bounded rather than scanning every mask over the whole HUD.
        for _, size, template, point in sorted(candidates, key=lambda row: row[0], reverse=True)[:3]:
            px, py = point
            x0, y0 = max(0, px-3), max(0, py-3)
            region = gray[y0:py+template.shape[0]+4, x0:px+template.shape[1]+4]
            for fraction in (.1, .2, .3, .4, .5, .6):
                variant = template.copy()
                start = max(0, round(size*fraction)-3)
                variant[start:] = np.uint8(variant[start:]*.4)
                result = cv2.matchTemplate(region, variant, cv2.TM_CCOEFF_NORMED)
                _, score, _, offset = cv2.minMaxLoc(result)
                if best is None or score > best[0]:
                    best = score, x0+offset[0]-3, y0+offset[1]-3, size
        if best is None or best[0] < .82:
            return FlowerReading(False, score=best[0] if best else 0)
        score, x, y, size = best
        if x < 0 or y < 0 or x+size > frame.shape[1] or y+size > frame.shape[0]:
            return FlowerReading(False, score=score)
        return FlowerReading(True, score=score, box=(x, y, size, size))

    def read(self, frame):
        found = self.locate(frame)
        if not found.present:
            return found
        x, y, size, _ = found.box
        # Timer sits at the icon's lower-left corner, not under its neighbor.
        timer = frame[y+round(size*.65):y+size+3, x:x+size]
        hsv = cv2.cvtColor(timer, cv2.COLOR_BGR2HSV)
        yellow = cv2.inRange(hsv, (18, 130, 170), (40, 255, 255))
        # Seconds are yellow. Minute counts are white. White icon details can
        # look like numbers: they may be displayed but never trigger a warning.
        white = cv2.inRange(hsv, (0, 0, 210), (180, 65, 255))
        seconds = self._number(yellow, 59)
        minutes = None if seconds is not None else self._number(white, 2)
        if minutes == 0:
            minutes = None
        fraction = self._fill_fraction(frame, found)
        return FlowerReading(True, seconds, minutes, found.score, found.box, fraction)

    def _fill_fraction(self, frame, found):
        x, y, size, _ = found.box
        actual = cv2.cvtColor(frame[y:y+size, x:x+size], cv2.COLOR_BGR2GRAY)
        reference = self.full_icons[size]
        dark_rows = []
        for row in range(3, size-3):
            bright = reference[row, size//2:-3] > 200
            if np.count_nonzero(bright) < 3:
                continue
            ratio = np.median(actual[row, size//2:-3][bright]/reference[row, size//2:-3][bright])
            dark_rows.append((row, ratio < .70))
        for i in range(len(dark_rows)-2):
            group = dark_rows[i:i+3]
            if all(dark for _, dark in group) and group[-1][0]-group[0][0] <= 4:
                return max(0, min(1, (group[0][0]+3)/(size+6)))
        return 1.0

    def _number(self, mask, maximum):
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        components = [(i, x, y, w, h) for i, (x, y, w, h, area) in enumerate(stats[1:], 1)
                      if h >= max(5, mask.shape[0]*.40) and area >= 5]
        if not 1 <= len(components) <= 2:
            return None
        selected = np.zeros_like(mask)
        for i, *_ in components:
            selected[labels == i] = 255
        ys, xs = np.where(selected)
        if len(xs) < 5:
            return None
        selected = selected[ys.min():ys.max()+1, xs.min():xs.max()+1]
        if self.recognizer is None:
            from experience_ocr import ExperienceOCR
            self.recognizer = ExperienceOCR()
        prepared = cv2.copyMakeBorder(selected, 4, 4, 6, 6, cv2.BORDER_CONSTANT)
        prepared = cv2.resize(prepared, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
        raw, confidence = self.recognizer.recognize_exp(cv2.cvtColor(prepared, cv2.COLOR_GRAY2BGR))
        from experience_ocr import short_character_confidence
        confidence = short_character_confidence(raw, confidence) or 0
        if not re.fullmatch(r'[0-9]{1,2}', raw) or confidence < .94:
            return None
        value = int(raw)
        return value if 0 <= value <= maximum else None
