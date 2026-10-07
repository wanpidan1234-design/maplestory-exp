"""Screen-only MapleStory experience tracker. No game process access."""
import math
import re
import time
from collections import deque
from copy import deepcopy
from dataclasses import dataclass


WINDOW_SECONDS = (60, 120, 300, 600, 1800)
WINDOW_ALGORITHM = 'observed_window_v1'
TREND_HALF_LIFE_SECONDS = 30.0
RESUME_SCHEMA = 'maple_exp_tracker_resume'
RESUME_VERSION = 1


@dataclass(frozen=True)
class Reading:
    current: int | None
    cap: int | None = None
    percent: float | None = None


class RollingRate:
    """Trailing 60 wall seconds; normalize startup to the observed duration."""
    def __init__(self,clock=time.monotonic):
        self.clock = clock
        self.events = deque()
        self.origin = None

    def reset(self,active=False,observed_at=None):
        self.events.clear()
        self.origin = (self.clock() if observed_at is None else observed_at) if active else None

    def add(self,delta,observed_at=None):
        now = self.clock() if observed_at is None else observed_at
        if self.origin is None:
            self.origin = now
        while self.events and self.events[0][0] <= now-60:
            self.events.popleft()
        if delta>0:
            self.events.append((now,delta))

    def rate(self):
        now = self.clock()
        while self.events and self.events[0][0]<=now-60:
            self.events.popleft()
        seconds = min(60,max(0,now-self.origin)) if self.origin is not None else 0
        return sum(value for _,value in self.events)*60/seconds if seconds>0 else 0


def _finite(value):
    try:
        return math.isfinite(value)
    except (TypeError, ValueError, OverflowError):
        return False


class MultiWindowRates:
    """Five trailing rates ending at the same last confirmed observation.

    Each window has its own event queue and running sum. Queries are read-only:
    only a new trusted reading (including a zero gain) advances their common
    endpoint. Tracker owns continuity resets; the caller masks unavailable or
    stale readings without treating missing observations as zero EXP.
    """
    algorithm = WINDOW_ALGORITHM

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self, active=False, observed_at=None):
        now = self.clock() if active and observed_at is None else observed_at
        if active and not _finite(now):
            return
        self.origin = self.last_observed_at = now if active else None
        self.events = {seconds: deque() for seconds in WINDOW_SECONDS}
        self.running_sum = {seconds: 0 for seconds in WINDOW_SECONDS}
        self.gaps = deque()

    def add(self, delta, observed_at=None, *, missing_gain=False):
        now = self.clock() if observed_at is None else observed_at
        if not _finite(now) or not _finite(delta) or delta < 0:
            return
        if self.last_observed_at is None:
            self.reset(active=True, observed_at=now)
            return
        dt = now - self.last_observed_at
        if not _finite(dt) or dt <= 0:
            return
        self.last_observed_at = now
        while self.gaps and self.gaps[0] <= now - max(WINDOW_SECONDS):
            self.gaps.popleft()
        if missing_gain:
            self.gaps.append(now)
        for seconds in WINDOW_SECONDS:
            events = self.events[seconds]
            total = self.running_sum[seconds]
            while events and events[0][0] <= now - seconds:
                total -= events.popleft()[1]
            if not events:
                total = 0
            if delta > 0:
                events.append((now, delta))
                total += delta
            self.running_sum[seconds] = total

    def snapshot(self):
        elapsed = 0.0 if self.origin is None else self.last_observed_at - self.origin
        result = {}
        for seconds in WINDOW_SECONDS:
            observed = min(seconds, elapsed)
            state = 'unavailable' if self.origin is None else 'ready' if observed >= seconds else 'warming'
            value = 60 * self.running_sum[seconds] / observed if observed > 0 else None
            has_gaps = bool(self.gaps and self.gaps[-1] > self.last_observed_at - seconds)
            result[str(seconds)] = dict(value=value, window_seconds=seconds,
                                        observed_seconds=observed, state=state,
                                        has_gaps=has_gaps,
                                        quality='lower_bound' if has_gaps else 'complete')
        return result


class ExponentialRate:
    """Time-aware EXP/min from confirmed intervals, with normalized startup.

    Each interval contributes its average rate with a weight determined by real
    elapsed time. Missing observations do not mean zero EXP, and merely reading
    the estimate never updates the filter. The half-life is a smoothing choice,
    not a confidence interval or a guarantee of short-sample accuracy.
    """
    algorithm = 'time_ewma_v1'

    def __init__(self, clock=time.monotonic, half_life=TREND_HALF_LIFE_SECONDS, max_gap=5.0):
        if not _finite(half_life) or half_life <= 0:
            raise ValueError('half_life must be finite and positive')
        if not _finite(max_gap) or max_gap <= 0:
            raise ValueError('max_gap must be finite and positive')
        self.clock = clock
        self.half_life = float(half_life)
        self.max_gap = float(max_gap)
        self.segment = 0
        self.reset()

    def reset(self, active=False, observed_at=None):
        now = self.clock() if active and observed_at is None else observed_at
        if active and not _finite(now):
            return
        self.origin = self.last_observed_at = now if active else None
        self.numerator = self.weight = 0.0
        self.invalid = False
        self.has_gaps = False
        self.segment += 1

    def add(self, delta, observed_at=None, *, missing_gain=False):
        now = self.clock() if observed_at is None else observed_at
        if not _finite(now) or not _finite(delta) or delta < 0:
            return
        if self.last_observed_at is None:
            self.reset(active=True, observed_at=now)
            return
        dt = now - self.last_observed_at
        if not _finite(dt) or dt <= 0:
            return
        if dt > self.max_gap:
            # This frame closes an unknown interval; it only starts a new one.
            self.reset(active=True, observed_at=now)
            return
        rate = 60.0 * delta / dt
        if not _finite(rate):
            return
        weight = -math.expm1(-math.log(2.0) * dt / self.half_life)
        decay = 1.0 - weight
        self.numerator = decay * self.numerator + weight * rate
        self.weight = decay * self.weight + weight
        self.last_observed_at = now
        self.invalid = False
        self.has_gaps = self.has_gaps or missing_gain

    def invalidate(self):
        # Keep the segment for a brief failure; the next trusted timestamp
        # decides whether continuity was actually lost.
        self.invalid = True

    def snapshot(self):
        now = self.clock()
        seconds = 0.0 if self.origin is None else self.last_observed_at - self.origin
        state = 'unavailable'
        value = None
        if self.last_observed_at is not None and _finite(now):
            if now - self.last_observed_at > self.max_gap:
                state = 'stale'
            elif not self.invalid:
                state = 'warming' if seconds < self.half_life else 'ready'
                if self.weight > 0:
                    value = self.numerator / self.weight
        return dict(value=value, state=state, observed_seconds=seconds,
                    half_life_seconds=self.half_life, segment=self.segment,
                    algorithm=self.algorithm, has_gaps=self.has_gaps,
                    quality='lower_bound' if self.has_gaps else 'complete')

    def rate(self):
        return self.snapshot()['value']


def parse_exp(text):
    text = text.replace('，', ',').replace('％', '%').replace('／', '/')
    text = re.sub(r'(?<=\d),(?=\d)', '', text)
    pct = re.search(r'(\d+(?:\.\d+)?)\s*%', text)
    percent = float(pct[1]) if pct else None
    if percent is not None and not 0 <= percent <= 100:
        return None
    ratio = re.search(r'(\d+)\s*/\s*(\d+)', text)
    if ratio:
        current, cap = map(int, ratio.groups())
        return Reading(current, cap, percent) if cap > 0 and current < cap else None
    remaining = re.sub(r'\d+(?:\.\d+)?\s*%', '', text)
    # A tightly selected experience field should contain only one absolute number.
    numbers = re.findall(r'(?<![\d.])\d+(?![\d.])', remaining)
    if len(numbers) == 1:
        return Reading(int(numbers[0]), None, percent)
    if not numbers and percent is not None:
        return Reading(None, None, percent)
    return None


class Tracker:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.total = 0
        self.previous = None
        self.cap = None
        self.accumulated = 0.0
        self.started = None
        self.blocked = False
        self.estimated = False
        self.confirmed_levels = 0
        self.level = None
        self.level_gaps = 0
        self.live = ExponentialRate(clock)
        self.rolling = RollingRate(clock)
        self.windows = MultiWindowRates(clock)
        self._last_input_at = None
        self._paused = False
        self._needs_baseline = False
        self._resume_pending = False
        self._resume_rates = None
        self._visibility_interrupted = False
        self._visibility_returned_at = None

    @property
    def resume_pending(self):
        return self._resume_pending

    @property
    def visibility_interrupted(self):
        return self._visibility_interrupted

    def suspend_visibility(self):
        """Freeze a proven hidden game, using the same ages as restart resume."""
        if self._paused or self.blocked:
            return False
        if self.visibility_interrupted:
            self._visibility_returned_at = None
            return False
        saved = self.export_resume_state(allow_invalid=True)
        self.accumulated = saved['elapsed']
        self.started = None
        self._resume_pending = True
        self._resume_rates = saved['rates']
        self._visibility_interrupted = True
        self._visibility_returned_at = None
        # Hidden gains and level changes cannot be reconstructed from pixels.
        # Keep committed windows, but require a new current-EXP baseline.
        self.previous = self.cap = self.level = None
        self.live.invalidate()
        return True

    def note_visibility_recovery(self, observed_at=None):
        """A visible/unknown failure can no longer extend the hidden interval."""
        if not self.visibility_interrupted:
            return
        current = self.clock()
        now = current if observed_at is None else observed_at
        if (not _finite(now) or not 0<=current-now<=self.live.max_gap
                or (self._last_input_at is not None and now<=self._last_input_at)):
            return
        if self._visibility_returned_at is None:
            self._visibility_returned_at = now
        self.expire_visibility_continuation()

    def expire_visibility_continuation(self):
        """Kept for callers: elapsed wall time cannot erase committed samples.

        Recovery still requires a trusted baseline; missing frames never add
        window time or gains, however long recognition takes to recover.
        """

    def export_resume_state(self, *, allow_invalid=False):
        """Freeze accounting and trusted rates without persisting a clock epoch.

        Window events are ages at the last trusted observation. Program downtime
        is neither an observation interval nor a source of experience gains.
        Freshness controls display availability, not ownership of committed
        samples. An invalid/new missing frame must not destroy a checkpoint.
        allow_invalid remains accepted for compatibility with existing callers.
        """
        rates = deepcopy(self._resume_rates) if self.resume_pending else None
        if not self.resume_pending:
            live = self.live
            now = self.clock()
            endpoint = live.last_observed_at
            if (not self._paused and not self.blocked
                    and endpoint is not None and _finite(now)
                    and 0 <= now - endpoint
                    and self.windows.last_observed_at == endpoint
                    and self.windows.origin == live.origin):
                rates = dict(window_algorithm=WINDOW_ALGORITHM,
                    trend_algorithm=live.algorithm, half_life_seconds=live.half_life,
                    max_gap_seconds=live.max_gap, observed_seconds=endpoint-live.origin,
                    segment=live.segment, numerator=live.numerator, weight=live.weight,
                    trend_has_gaps=live.has_gaps,
                    gap_ages=[endpoint-at for at in self.windows.gaps],
                    events=[[endpoint-at, delta] for at, delta in self.windows.events[max(WINDOW_SECONDS)]])
        return dict(schema=RESUME_SCHEMA, version=RESUME_VERSION,
                    total=self.total, elapsed=self.elapsed, estimated=self.estimated,
                    confirmed_levels=self.confirmed_levels, level_gaps=self.level_gaps,
                    rates=rates)

    @staticmethod
    def _validated_resume_state(state):
        """Validate into detached primitive data before touching a live tracker."""
        def number(value, *, positive=False, integer=False):
            if (type(value) not in (int, float) or not _finite(value)
                    or value < 0 or (positive and value <= 0)
                    or (integer and type(value) is not int)):
                raise ValueError('invalid resume number')
            return value

        fields = {'schema', 'version', 'total', 'elapsed', 'estimated',
                  'confirmed_levels', 'level_gaps', 'rates'}
        if (type(state) is not dict or set(state) != fields
                or state['schema'] != RESUME_SCHEMA
                or type(state['version']) is not int or state['version'] != RESUME_VERSION
                or type(state['estimated']) is not bool):
            raise ValueError('unsupported resume schema')
        result = deepcopy(state)
        number(result['total'])
        number(result['elapsed'])
        number(result['confirmed_levels'], integer=True)
        number(result['level_gaps'], integer=True)
        if result['level_gaps'] > result['confirmed_levels']:
            raise ValueError('invalid level counters')
        rates = result['rates']
        if rates is None:
            return result
        rate_fields = {'window_algorithm', 'trend_algorithm', 'half_life_seconds',
                       'max_gap_seconds', 'observed_seconds', 'segment', 'numerator',
                       'weight', 'events'}
        optional_fields = {'gap_ages', 'trend_has_gaps'}
        if (type(rates) is not dict or not rate_fields <= set(rates)
                or set(rates) - rate_fields - optional_fields
                or rates['window_algorithm'] != WINDOW_ALGORITHM
                or rates['trend_algorithm'] != ExponentialRate.algorithm
                or type(rates['events']) is not list):
            raise ValueError('unsupported rate snapshot')
        # v1 snapshots from before missing-tail tracking remain fully resumable.
        rates.setdefault('gap_ages', [])
        rates.setdefault('trend_has_gaps', False)
        if type(rates['gap_ages']) is not list or type(rates['trend_has_gaps']) is not bool:
            raise ValueError('invalid resume quality')
        for field in ('half_life_seconds', 'max_gap_seconds', 'observed_seconds',
                      'numerator', 'weight'):
            number(rates[field])
        number(rates['segment'], integer=True)
        if (rates['half_life_seconds'] != TREND_HALF_LIFE_SECONDS
                or rates['max_gap_seconds'] != 5.0 or rates['weight'] > 1):
            raise ValueError('unsupported rate parameters')
        observed = rates['observed_seconds']
        previous_gap_age = math.inf
        for age in rates['gap_ages']:
            number(age)
            if age >= max(WINDOW_SECONDS) or age > observed or age >= previous_gap_age or observed == 0:
                raise ValueError('invalid resume gap time')
            previous_gap_age = age
        if rates['gap_ages'] and not rates['trend_has_gaps']:
            raise ValueError('inconsistent resume quality')
        expected_weight = -math.expm1(-math.log(2.0) * observed / TREND_HALF_LIFE_SECONDS)
        if not math.isclose(rates['weight'], expected_weight, rel_tol=1e-8, abs_tol=1e-12):
            raise ValueError('inconsistent trend duration')
        if rates['weight'] == 0:
            if rates['numerator'] != 0 or observed != 0:
                raise ValueError('invalid empty trend')
        elif not _finite(rates['numerator'] / rates['weight']):
            raise ValueError('invalid trend rate')
        previous_age = math.inf
        total = 0
        for event in rates['events']:
            if type(event) is not list or len(event) != 2:
                raise ValueError('invalid resume event')
            age, delta = event
            number(age)
            number(delta, positive=True)
            if age >= max(WINDOW_SECONDS) or age > observed or age >= previous_age or observed == 0:
                raise ValueError('invalid resume event time')
            previous_age = age
            total += delta
        if not _finite(total) or total > result['total']:
            raise ValueError('invalid resume window total')
        for seconds in WINDOW_SECONDS:
            duration = min(seconds, observed)
            gain = sum(delta for age, delta in rates['events'] if age < seconds)
            if duration > 0 and not _finite(gain * 60 / duration):
                raise ValueError('invalid resume window rate')
        return result

    def restore_resume_state(self, state):
        """Restore a frozen continuation; invalid data leaves this object intact."""
        try:
            saved = self._validated_resume_state(state)
        except (TypeError, ValueError, OverflowError):
            return False
        restored = Tracker(self.clock)
        restored.total = saved['total']
        restored.accumulated = saved['elapsed']
        restored.estimated = saved['estimated']
        restored.confirmed_levels = saved['confirmed_levels']
        restored.level_gaps = saved['level_gaps']
        restored._resume_pending = True
        restored._resume_rates = saved['rates']
        self.__dict__.update(restored.__dict__)
        return True

    def resume_rate_snapshot(self):
        """Return a stable display-only snapshot while waiting for a new baseline."""
        rates = self._resume_rates if self.resume_pending else None
        if rates is None:
            return None
        observed = rates['observed_seconds']
        windows = {}
        for seconds in WINDOW_SECONDS:
            duration = min(seconds, observed)
            total = sum(delta for age, delta in rates['events'] if age < seconds)
            has_gaps = any(age < seconds for age in rates['gap_ages'])
            windows[str(seconds)] = dict(value=60*total/duration if duration > 0 else None,
                window_seconds=seconds, observed_seconds=duration,
                state='ready' if duration >= seconds else 'warming',
                has_gaps=has_gaps, quality='lower_bound' if has_gaps else 'complete')
        # A restart is a history breakpoint even though trusted rates continue.
        segment = rates['segment'] + 1
        snapshot = dict(value=rates['numerator']/rates['weight'] if rates['weight'] > 0 else None,
            state='ready' if observed >= rates['half_life_seconds'] else 'warming',
            observed_seconds=observed, half_life_seconds=rates['half_life_seconds'],
            segment=segment, algorithm=rates['trend_algorithm'], windows=windows,
            window_algorithm=rates['window_algorithm'], rolling60=windows['60']['value'],
            has_gaps=rates['trend_has_gaps'],
            quality='lower_bound' if rates['trend_has_gaps'] else 'complete')
        snapshot['primary'] = dict(windows['60'], algorithm=WINDOW_ALGORITHM, segment=segment)
        return snapshot

    def _resume_at(self, now):
        rates = self._resume_rates
        self.reset_rates(active=True, observed_at=now)
        if rates is None:
            return
        origin = now - rates['observed_seconds']
        self.live.origin = self.windows.origin = self.rolling.origin = origin
        self.live.numerator = rates['numerator']
        self.live.weight = rates['weight']
        self.live.segment = rates['segment'] + 1
        self.live.has_gaps = rates['trend_has_gaps']
        self.windows.gaps = deque(now-age for age in rates['gap_ages'])
        for seconds in WINDOW_SECONDS:
            events = [(now-age, delta) for age, delta in rates['events'] if age < seconds]
            self.windows.events[seconds] = deque(events)
            self.windows.running_sum[seconds] = sum(delta for _, delta in events)
        self.rolling.events = deque(self.windows.events[60])

    def start(self):
        if self.resume_pending:
            return
        if self.started is None:
            self.started = self.clock()
        self._paused = False

    def pause(self):
        if self.started is not None:
            self.accumulated += self.clock() - self.started
            self.started = None
        self.reset_rates()
        self._paused = True
        self._needs_baseline = True

    @property
    def elapsed(self):
        return self.accumulated + (self.clock() - self.started if self.started is not None else 0)

    def rates(self):
        elapsed = self.elapsed
        return (self.total * 60 / elapsed, self.total * 3600 / elapsed) if elapsed else (0, 0)

    def _fresh_time(self, now):
        return _finite(now) and (self._last_input_at is None or now > self._last_input_at)

    def reset_rates(self, active=False, observed_at=None):
        """Reset every rate against one shared observation timestamp."""
        now = self.clock() if active and observed_at is None else observed_at
        if active and not _finite(now):
            return
        self._resume_pending = False
        self._resume_rates = None
        self._visibility_interrupted = False
        self._visibility_returned_at = None
        self.live.reset(active=active, observed_at=now)
        self.rolling.reset(active=active, observed_at=now)
        self.windows.reset(active=active, observed_at=now)

    def _record_gain(self, delta, now, *, missing_gain=False):
        self.total += delta
        if not self._paused:
            previous_observation = self.live.last_observed_at
            if previous_observation is None:
                self.reset_rates(active=True, observed_at=now)
            elif now - previous_observation > self.live.max_gap:
                # Stitch trusted observation time across an unobserved interval.
                # Preserve every event age and warmup duration. Its net delta
                # belongs only to the cumulative ledger, never to a rate spike.
                shift = now - previous_observation
                self.live.origin += shift
                self.live.last_observed_at = now
                self.live.invalid = False
                self.live.segment += 1  # Break history curves across the outage.
                self.windows.origin += shift
                self.windows.last_observed_at = now
                self.windows.gaps = deque(at + shift for at in self.windows.gaps)
                for seconds in WINDOW_SECONDS:
                    self.windows.events[seconds] = deque(
                        (at + shift, gain) for at, gain in self.windows.events[seconds])
                self.rolling.origin += shift
                self.rolling.events = deque(self.windows.events[60])
            else:
                self.live.add(delta, observed_at=now, missing_gain=missing_gain)
                self.rolling.add(delta, observed_at=now)
                self.windows.add(delta, observed_at=now, missing_gain=missing_gain)

    def feed(self, reading, manual_cap=None, rebase=False, observed_at=None):
        now = self.clock() if observed_at is None else observed_at
        if not self._fresh_time(now):
            return 'stale', 0
        if self.blocked and not rebase:
            return 'blocked', 0
        cap = reading.cap or manual_cap
        value = reading.current
        if value is None:
            if not cap:
                return 'need_cap', 0
            if not _finite(reading.percent) or not _finite(cap) or cap <= 0:
                return 'invalid', 0
            value = round(reading.percent * cap / 100)
            self.estimated = True
        if not _finite(value) or value < 0:
            return 'invalid', 0
        self._last_input_at = now
        if self.resume_pending and not rebase:
            self.previous, self.cap, self.blocked = value, cap, False
            self._paused = self._needs_baseline = False
            self._resume_at(now)
            return 'baseline', 0
        if rebase or self.previous is None or self._needs_baseline:
            self.previous, self.cap, self.blocked = value, cap, False
            if not rebase:
                self._paused = False
            self._needs_baseline = self._paused
            self.reset_rates(active=not self._paused, observed_at=now)
            return 'baseline', 0
        delta = value - self.previous
        status = 'ok'
        if not _finite(delta):
            return 'invalid', 0
        if delta < 0:
            # A single screenshot cannot distinguish a level-up from death or OCR failure.
            self.blocked = True
            self.live.invalidate()
            return 'decrease', 0
        previous_segment = self.live.segment
        self._record_gain(delta, now)
        if self.live.segment != previous_segment:
            status = 'gap'
        self.previous, self.cap = value, cap
        return status, delta

    def confirm_level(self, reading, observed_at=None):
        now = self.clock() if observed_at is None else observed_at
        if not self._fresh_time(now):
            return False
        if not self.blocked or self.cap is None or reading.current is None or self.previous is None:
            return False
        delta = self.cap - self.previous + reading.current
        if not _finite(delta) or delta < 0 or not _finite(reading.current) or reading.current < 0:
            return False
        self._last_input_at = now
        self._record_gain(delta, now)
        self.previous, self.cap = reading.current, reading.cap
        self.blocked = False
        self.confirmed_levels += 1
        return True
