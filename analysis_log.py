"""Best-effort local analysis events; never part of the accounting transaction.

Only bounded built-in values under FIELD_KEYS are accepted. Strings are short
ASCII identifiers (or explicitly constrained IDs/version/slot names), never OCR
text, exception messages, titles, paths, pixels or object representations.
The daemon is the only caller of JSON encoding and filesystem writes. Loss is
possible under overload, disk failure, rotation and process termination.
"""
from collections import Counter, OrderedDict, deque
import argparse
import json
import math
from pathlib import Path
import queue
import re
import threading
import time
import uuid


FIELD_KEYS = frozenset('''sample_id generation session_id history_seq app_instance_id
stage failure_stage failure_kind visibility_reason error_type ocr_reason ocr_path anchor
percent_reason level_reason timing_ms capture experience_ocr level_ocr engine_init
worker queue_wait tick checkpoint commit consume observed_at worker_started_at
enqueued_at consumed_at frame_age sample_gap queue_age duration_ms outcome status
baseline_reason gain_disposition before after level candidate count vote_span
wait_age confirmed_level confirmed_levels level_gaps busy queue_depth persisted
attempted delta source reason pid python_version action running reading_valid
blocked resume_pending visibility_interrupted paused needs_baseline current total
elapsed segment observed_seconds value windows sum surface display_state raw_state
cache_reason display_observation_id display_age_seconds presence primary percent
previous threshold worker_age result slot sequence dropped suppressed write_errors
pending written enqueued queue_full backoff_dropped closed truncated health
60 120 300 600 1800'''.split())
_STRING_KEYS = frozenset('''stage failure_stage failure_kind visibility_reason
error_type ocr_reason ocr_path percent_reason level_reason outcome status
baseline_reason gain_disposition source reason action surface display_state
raw_state cache_reason result'''.split())
_IDENTIFIER = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,63}\Z')
_ID = re.compile(r'(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})\Z')
_STOP = object()
_OMIT = object()
_MAX_RECORD = 8192
_MAX_THROTTLES = 128
_CONTEXT_KEYS = frozenset('sample_id generation outcome reason current segment observed_at'.split())


def _token(value):
    return type(value) is str and len(value) <= 64 and _IDENTIFIER.fullmatch(value) is not None


def _clean(fields, *, context=False):
    """Conservative JSON byte budgeting without serialization on the caller."""
    budget = [252 if context else 5500]
    nodes = [16 if context else 192]
    trimmed = [False]
    allowed = _CONTEXT_KEYS if context else FIELD_KEYS

    def scalar(value, key):
        if value is None or type(value) is bool:
            return value, 5
        if type(value) is int and value.bit_length() <= 63:
            return value, 21
        if type(value) is float and math.isfinite(value):
            return value, 25
        if type(value) is str and len(value) <= 64:
            valid = (key in _STRING_KEYS and _token(value))
            if key in ('session_id', 'app_instance_id'):
                valid = _ID.fullmatch(value) is not None
            elif key == 'action':
                valid = re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',value) is not None
            elif key == 'python_version':
                valid = re.fullmatch(r'[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,3}', value) is not None
            elif key == 'slot':
                valid = re.fullmatch(r'slot-[0-9]{2}\.json', value) is not None
            if valid:
                return value, len(value) + 2
        return _OMIT, 0

    def mapping(value, depth):
        if type(value) is not dict or depth > 5:
            trimmed[0] = True
            return {}
        result = {}
        budget[0] -= 2
        for index, (key, item) in enumerate(value.items()):
            if index >= 64 or nodes[0] <= 0 or budget[0] < 8:
                trimmed[0] = True
                break
            nodes[0] -= 1
            if type(key) is not str or len(key) > 32 or key not in allowed:
                trimmed[0] = True
                continue
            overhead = len(key) + 4
            if type(item) is dict and not context:
                if budget[0] < overhead + 2:
                    trimmed[0] = True
                    continue
                budget[0] -= overhead
                result[key] = mapping(item, depth + 1)
                continue
            cleaned, size = scalar(item, key)
            if cleaned is _OMIT or overhead + size > budget[0]:
                trimmed[0] = True
                continue
            budget[0] -= overhead + size
            result[key] = cleaned
        return result

    return mapping(fields, 0), trimmed[0]


class AnalysisJournal:
    """Nonblocking bounded producer and an independent, lossy disk writer.

    ``sink`` is an optional test callable accepting one encoded JSONL ``bytes``
    record. It replaces the filesystem sink and runs only on the daemon thread.
    Clock injection must be a fast, nonblocking callable. ``close`` does not
    cancel a blocked OS write; it returns False when the daemon remains alive.
    """
    def __init__(self, directory, *, queue_size=256, max_bytes=2*1024*1024,
                 backup_count=2, sink=None, clock=time.monotonic,
                 wall_clock=time.time):
        if type(queue_size) is not int or not 1 <= queue_size <= 4096:
            raise ValueError('invalid queue_size')
        if type(max_bytes) is not int or not 512 <= max_bytes <= 64*1024*1024:
            raise ValueError('invalid max_bytes')
        if type(backup_count) is not int or not 0 <= backup_count <= 2:
            raise ValueError('invalid backup_count')
        self.directory = Path(directory)
        self.run_id = uuid.uuid4().hex
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._sink = sink
        self._clock = clock
        self._wall_clock = wall_clock
        self._queue = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._context = deque(maxlen=8)
        self._throttles = OrderedDict()
        self._counts = Counter(enqueued=0, written=0, dropped=0, suppressed=0,
                               write_errors=0, queue_full=0, backoff_dropped=0,
                               rejected=0)
        self._contention_dropped = 0
        self._sample = self._sequence = 0
        self._closed = False
        self._inflight = False
        self._last_error = None
        self._retry_after = 0.0
        self._consecutive_errors = 0
        self._thread = threading.Thread(target=self._run, name='analysis-log', daemon=True)
        self._thread.start()

    def next_sample_id(self):
        # No I/O lock is held by any participant. IDs only label attempts.
        with self._lock:
            self._sample += 1
            return self._sample

    def observe(self, summary):
        try:
            clean, _ = _clean(summary, context=True)
            if not self._lock.acquire(blocking=False):
                return
            try:
                if not self._closed:
                    self._context.append(clean)
            finally:
                self._lock.release()
        except Exception:
            pass

    def emit(self, event, fields=None, *, key=None, interval=0, context=False):
        """Attempt enqueue; False means invalid, closed, suppressed or dropped.

        Throttle entries are bounded to 128 and scoped by event plus safe key.
        A successfully queued repeat includes its suppressed count/first/last
        timestamps. Lifetime counters remain visible even after key eviction.
        """
        try:
            if not _token(event):
                self._counts['rejected'] += 1
                return False
            clean, trimmed = _clean({} if fields is None else fields)
            now = float(self._clock())
            wall = float(self._wall_clock())
            if not math.isfinite(now) or not math.isfinite(wall):
                self._counts['rejected'] += 1
                return False
            duration = float(interval) if type(interval) in (int, float) else 0
            duration = min(3600., max(0., duration)) if math.isfinite(duration) else 0
            # Arbitrary objects/strings never become retained throttle keys.
            if key is None:
                throttle_key = (event, *(clean.get(name) if _token(clean.get(name)) else None
                                        for name in ('reason', 'stage')))
            elif _token(key):
                throttle_key = (event, key)
            elif type(key) is tuple and len(key) <= 4 and all(_token(k) or type(k) is int and k.bit_length() <= 63 for k in key):
                throttle_key = (event, *key)
            else:
                throttle_key = (event,)
            if not self._lock.acquire(blocking=False):
                self._contention_dropped += 1
                return False
            try:
                if self._closed:
                    self._counts['dropped'] += 1
                    return False
                if now < self._retry_after:
                    self._counts['dropped'] += 1
                    self._counts['backoff_dropped'] += 1
                    return False
                throttle = self._throttles.get(throttle_key) if duration else None
                if throttle and 0 <= now - throttle['at'] < duration:
                    self._counts['suppressed'] += 1
                    if not throttle['count']:
                        throttle['first'] = now
                    throttle['count'] += 1
                    throttle['last'] = now
                    return False
                self._sequence += 1
                row = dict(schema='analysis_log_v1', run_id=self.run_id,
                           sequence=self._sequence, event=event,
                           monotonic=now, wall_time=wall, fields=clean,
                           truncated=trimmed)
                if context:
                    row['context'] = list(self._context)
                if throttle and throttle['count']:
                    row['suppression'] = dict(count=throttle['count'],
                        first=throttle['first'], last=throttle['last'])
                try:
                    self._queue.put_nowait(row)
                except queue.Full:
                    self._counts['dropped'] += 1
                    self._counts['queue_full'] += 1
                    return False
                self._counts['enqueued'] += 1
                if duration:
                    self._throttles[throttle_key] = dict(at=now, first=now, last=now, count=0)
                    self._throttles.move_to_end(throttle_key)
                    while len(self._throttles) > _MAX_THROTTLES:
                        self._throttles.popitem(last=False)
                return True
            finally:
                self._lock.release()
        except Exception:
            # No error message/repr is retained and diagnostic errors never escape.
            self._counts['rejected'] += 1
            return False

    def status(self):
        with self._lock:
            return self._status_locked()

    def _status_locked(self):
        result = dict(self._counts)
        result['dropped'] += self._contention_dropped
        result.update(run_id=self.run_id, queue_depth=self._queue.qsize(),
                      pending=max(0, self._counts['enqueued'] - self._counts['written']
                                  - self._counts['write_errors'] - self._counts.get('writer_dropped', 0)),
                      closed=self._closed, worker_alive=self._thread.is_alive(),
                      last_error=self._last_error)
        return result

    def _encode(self, row):
        # Only this daemon performs JSON encoding, including byte-size checks.
        limit = min(_MAX_RECORD, self.max_bytes)
        def encode():
            return (json.dumps(row, ensure_ascii=True, allow_nan=False,
                               separators=(',', ':')) + '\n').encode('ascii')
        content = encode()
        if len(content) <= limit:
            return content
        row['truncated'] = True
        # Preserve the anomaly body ahead of preceding normal observations.
        while row.get('context') and len(content) > limit:
            row['context'].pop(0)
            content = encode()
        if len(content) > limit:
            row['health'] = {key: row['health'][key] for key in
                             ('dropped', 'suppressed', 'write_errors') if key in row['health']}
            content = encode()
        while row['fields'] and len(content) > limit:
            # Keep cause/identity fields until other details have been removed.
            protected = {'stage', 'reason', 'sample_id', 'error_type', 'outcome',
                         'generation', 'history_seq'}
            removable = next((key for key in reversed(row['fields']) if key not in protected),
                             next(reversed(row['fields'])))
            del row['fields'][removable]
            content = encode()
        if len(content) > limit:
            row.pop('suppression', None)
            row.pop('context', None)
            content = encode()
        return content

    def _write(self, content):
        if self._sink is not None:
            self._sink(content)
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / 'analysis.jsonl'
        if path.exists() and path.stat().st_size + len(content) > self.max_bytes:
            if self.backup_count:
                for index in range(self.backup_count, 0, -1):
                    target = path.with_name(f'analysis.jsonl.{index}')
                    source = path if index == 1 else path.with_name(f'analysis.jsonl.{index-1}')
                    if source.exists():
                        source.replace(target)
            else:
                path.unlink()
        with path.open('ab') as output:
            output.write(content)

    def _run(self):
        while True:
            row = self._queue.get()
            if row is _STOP:
                self._queue.task_done()
                return
            with self._lock:
                self._inflight = True
                row['health'] = dict(self._counts)
                row['health']['dropped'] += self._contention_dropped
                retry_after = self._retry_after
            try:
                now = self._clock()
                if now < retry_after:
                    with self._lock:
                        self._counts['dropped'] += 1
                        self._counts['backoff_dropped'] += 1
                        self._counts['writer_dropped'] += 1
                else:
                    self._write(self._encode(row))
                    with self._lock:
                        self._counts['written'] += 1
                        self._last_error = None
                        self._consecutive_errors = 0
            except Exception as error:
                with self._lock:
                    self._counts['write_errors'] += 1
                    self._counts['dropped'] += 1
                    self._last_error = type(error).__name__ if _token(type(error).__name__) else 'Exception'
                    self._consecutive_errors = min(5, self._consecutive_errors + 1)
                    try:
                        self._retry_after = self._clock() + min(30, 2 ** self._consecutive_errors)
                    except Exception:
                        self._retry_after = float('inf')
            finally:
                with self._lock:
                    self._inflight = False
                    finished = self._closed and self._queue.empty()
                self._queue.task_done()
            if finished:
                return

    def close(self, timeout=.2):
        """Stop accepting events; bounded join, False means writes remain pending."""
        try:
            with self._lock:
                if not self._closed:
                    self._closed = True
                    if self._queue.empty():
                        self._queue.put_nowait(_STOP)
            seconds = float(timeout) if type(timeout) in (int, float) else .2
            seconds = max(0., min(2., seconds)) if math.isfinite(seconds) else .2
            self._thread.join(seconds)
            return not self._thread.is_alive()
        except Exception:
            return False


def summarize(directory):
    """Read only the three fixed local files; report observations, not causes."""
    events, reasons, stages, outcomes, error_types, run_health = Counter(), Counter(), Counter(), Counter(), Counter(), {}
    files, malformed, oversized, read_errors = 0, 0, 0, Counter()
    for name in ('analysis.jsonl.2', 'analysis.jsonl.1', 'analysis.jsonl'):
        try:
            with (Path(directory) / name).open('rb') as source:
                files += 1
                while True:
                    line = source.readline(_MAX_RECORD + 1)
                    if not line:
                        break
                    if len(line) > _MAX_RECORD:
                        oversized += 1
                        while line and not line.endswith(b'\n'):
                            line = source.readline(_MAX_RECORD + 1)
                        continue
                    try:
                        row = json.loads(line)
                        if type(row) is not dict or row.get('schema') != 'analysis_log_v1' or not _token(row.get('event')):
                            raise ValueError('unsupported record')
                        events[row['event']] += 1
                        fields = row.get('fields', {})
                        if type(fields) is dict:
                            for key in ('reason', 'ocr_reason', 'failure_kind'):
                                if _token(fields.get(key)):
                                    reasons[fields[key]] += 1
                            for key,counter in (('failure_stage',stages),('outcome',outcomes),('error_type',error_types)):
                                if _token(fields.get(key)):
                                    counter[fields[key]] += 1
                        identity = row.get('run_id')
                        if type(identity) is str and _ID.fullmatch(identity):
                            health = row.get('health', {})
                            if type(health) is dict:
                                latest = run_health.setdefault(identity, Counter())
                                for key in ('dropped', 'suppressed', 'write_errors', 'queue_full', 'backoff_dropped'):
                                    value = health.get(key)
                                    if type(value) is int and 0 <= value <= 2**63-1:
                                        latest[key] = max(latest[key], value)
                    except (ValueError, TypeError, UnicodeError):
                        malformed += 1
        except FileNotFoundError:
            pass
        except OSError as error:
            read_errors[type(error).__name__] += 1
    losses = Counter()
    for health in run_health.values():
        losses.update(health)
    return dict(facts=dict(files_read=files, events=dict(events), reasons=dict(reasons),
                           failure_stages=dict(stages),outcomes=dict(outcomes),error_types=dict(error_types),
                           recorded_loss_counters=dict(losses), malformed_lines=malformed,
                           oversized_lines=oversized, read_errors=dict(read_errors)),
                unknown=['root_cause_not_inferred', 'rotated_or_unflushed_events',
                         'loss_after_last_persisted_health_snapshot'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Read-only analysis log facts; no root-cause inference.')
    parser.add_argument('--summary', required=True, metavar='DIRECTORY')
    args = parser.parse_args()
    print(json.dumps(summarize(args.summary), ensure_ascii=True, indent=2))
