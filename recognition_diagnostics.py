"""Bounded, best-effort local evidence for recognition failures.

Only small uint8 regions may be written. A JSON file is the commit marker for
its slot; image and JSON replacements are atomic individually. This module
never reads or changes the experience history database.
"""
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from collections import deque
from collections.abc import Mapping

import cv2
import numpy as np


_MAX_SLOTS = 24
_MAX_METADATA_BYTES = 64 * 1024
_MAX_RECORD_BYTES = _MAX_METADATA_BYTES + 8192
_LOCKS = {}
_LOCKS_LOCK = threading.Lock()


def _text(value, limit):
    """Keep strings safe for UTF-8 without invoking arbitrary object reprs."""
    if not isinstance(value, str):
        value = f'<{type(value).__name__}>'
    return value.encode('utf-8', errors='replace').decode('utf-8')[:limit]


def _metadata(value):
    truncated = False
    remaining = 256

    def clean(item, depth=0):
        nonlocal truncated, remaining
        remaining -= 1
        if remaining < 0 or depth > 6:
            truncated = True
            return '<limit>'
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else '<non-finite>'
        if isinstance(item, str):
            result = _text(item, 2048)
            truncated |= result != item
            return result
        if isinstance(item, np.generic):
            return clean(item.item(), depth + 1)
        if isinstance(item, np.ndarray):
            return dict(type='ndarray', shape=list(item.shape),
                        dtype=str(item.dtype), omitted='array_values')
        if isinstance(item, Path):
            return clean(str(item), depth + 1)
        if isinstance(item, (bytes, bytearray, memoryview)):
            return dict(type=type(item).__name__, length=len(item), omitted='binary_values')
        if isinstance(item, Mapping):
            result = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= 64 or remaining <= 0:
                    truncated = True
                    result['_diagnostic_truncated'] = True
                    break
                if not isinstance(key, str):
                    key = str(key) if isinstance(key, (int, float, bool)) else _text(key, 128)
                result[_text(key, 128)] = clean(child, depth + 1)
            return result
        if isinstance(item, (list, tuple, set, frozenset)):
            result = []
            for index, child in enumerate(item):
                if index >= 64 or remaining <= 0:
                    truncated = True
                    result.append('<truncated>')
                    break
                result.append(clean(child, depth + 1))
            return result
        return _text(item, 128)

    try:
        result = clean(value)
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False,
                             separators=(',', ':')).encode('utf-8')
        if len(encoded) > _MAX_METADATA_BYTES:
            truncated = True
            preview = encoded.decode('utf-8')[:8192]
            result = dict(omitted='metadata_size', original_bytes=len(encoded), preview=preview)
        return result, truncated
    except Exception as error:
        # A hostile/custom mapping, huge integer, or other unserializable object
        # is diagnostic metadata failure, not recognition or accounting failure.
        return dict(omitted='metadata_serialization', error=type(error).__name__), True


class RecognitionJournal:
    def __init__(self, directory, clock=time.monotonic, wall_clock=time.time,
                 max_entries=24, min_interval=5):
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or not 1 <= max_entries <= _MAX_SLOTS:
            raise ValueError('max_entries must be an integer between 1 and 24')
        if not isinstance(min_interval, (int, float)) or not math.isfinite(min_interval) or min_interval < 0:
            raise ValueError('min_interval must be finite and non-negative')
        self.directory = Path(directory)
        self.clock = clock
        self.wall_clock = wall_clock
        self.max_entries = max_entries
        self.min_interval = float(min_interval)
        self.last_error = None
        self._last_attempt = None
        key = os.path.normcase(os.path.abspath(os.fspath(self.directory)))
        with _LOCKS_LOCK:
            self._lock = _LOCKS.setdefault(key, threading.RLock())

    def _paths(self, slot):
        stem = f'slot-{slot:02d}'
        return (self.directory / f'{stem}.json',
                self.directory / f'{stem}-exp.png',
                self.directory / f'{stem}-level.png')

    def _next_sequence(self):
        latest = -1
        for slot in range(_MAX_SLOTS):
            paths = self._paths(slot)
            if slot >= self.max_entries:
                # These are only this journal's exact, fixed slot filenames.
                for path in paths:
                    path.unlink(missing_ok=True)
                continue
            try:
                with paths[0].open('rb') as source:
                    data = source.read(_MAX_RECORD_BYTES + 1)
                if len(data) > _MAX_RECORD_BYTES:
                    continue
                row = json.loads(data)
                sequence = row.get('sequence')
                if row.get('schema') == 'recognition_diagnostics_v1' and type(sequence) is int and sequence >= 0:
                    latest = max(latest, sequence)
            except (FileNotFoundError, ValueError, TypeError, AttributeError, UnicodeError):
                continue
        return latest + 1

    @staticmethod
    def _atomic_write(path, content):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f'.{path.name}.',
                                             suffix='.tmp', delete=False) as output:
                temporary = Path(output.name)
            # Path.write_bytes handles Unicode paths on Windows; OpenCV only
            # encodes to memory and never receives the filesystem path.
            temporary.write_bytes(content)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _image(frame):
        if frame is None:
            return None, None, 'no_frame'
        if not isinstance(frame, np.ndarray):
            return None, None, 'not_ndarray'
        shape = list(frame.shape)
        if frame.dtype != np.uint8:
            return shape, None, 'not_uint8'
        if frame.ndim not in (2, 3) or (frame.ndim == 3 and frame.shape[2] not in (1, 3, 4)):
            return shape, None, 'unsupported_shape'
        height, width = frame.shape[:2]
        if not height or not width:
            return shape, None, 'empty_frame'
        if width > 2048 or height > 256 or frame.nbytes > 1_500_000:
            return shape, None, 'roi_too_large'
        success, encoded = cv2.imencode('.png', frame)
        if not success:
            raise OSError('PNG encoding failed')
        return shape, encoded.tobytes(), None

    def record(self, stage, metadata, frame=None, level_frame=None):
        """Return the committed JSON Path, or None for throttling/failure.

        All stages share the same throttle. Attempts also consume the throttle
        after an I/O failure, so a full/unwritable disk is not retried per frame.
        Existing slots survive process restarts; no old image is reused when a
        replacement record has no frame. Throttling leaves last_error unchanged.
        """
        with self._lock:
            try:
                now = float(self.clock())
                if not math.isfinite(now):
                    raise ValueError('non-finite monotonic time')
                if self._last_attempt is not None and now - self._last_attempt < self.min_interval:
                    return None
                self._last_attempt = now
                created = float(self.wall_clock())
                if not math.isfinite(created):
                    raise ValueError('non-finite wall time')
                self.directory.mkdir(parents=True, exist_ok=True)
                sequence = self._next_sequence()
                paths = self._paths(sequence % self.max_entries)
                clean_metadata, truncated = _metadata(metadata)
                payload = dict(schema='recognition_diagnostics_v1', sequence=sequence,
                               created=created, stage=_text(stage, 128), metadata=clean_metadata,
                               metadata_truncated=truncated, shape={}, omitted={}, frames={})
                images = []
                for name, candidate, path in zip(('exp', 'level'), (frame, level_frame), paths[1:]):
                    shape, encoded, omitted = self._image(candidate)
                    payload['shape'][name] = shape
                    payload['omitted'][name] = omitted
                    payload['frames'][name] = dict(shape=shape, path=path.name if encoded is not None else None,
                                                  omitted=omitted)
                    images.append((path, encoded))
                content = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                     separators=(',', ':')).encode('utf-8')
                # Invalidate the old commit marker before replacing images. If
                # interrupted, readers cannot mistake new images for old evidence.
                paths[0].unlink(missing_ok=True)
                for path, encoded in images:
                    if encoded is None:
                        path.unlink(missing_ok=True)
                    else:
                        self._atomic_write(path, encoded)
                self._atomic_write(paths[0], content)
                self.last_error = None
                return paths[0]
            except Exception as error:
                try:
                    self.last_error = _text(f'{type(error).__name__}: {error}', 240)
                except Exception:
                    self.last_error = type(error).__name__
                return None


def _detached_metadata(value):
    """Bounded builtin-only snapshot; no JSON, pixels, or user object callbacks.

    Limits are at least as strict as the disk journal. A shared character
    budget also bounds escaped JSON produced later by its writer thread.
    """
    remaining, characters = 256, 8192

    def string(item, limit=2048):
        nonlocal characters
        result = item[:min(limit, characters)]
        characters -= len(result)
        return result

    def clean(item, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 6:
            return '<limit>'
        kind = type(item)
        if item is None or kind is bool:
            return item
        if kind is int:
            return item if item.bit_length() <= 256 else '<large-int>'
        if kind is float:
            return item if math.isfinite(item) else '<non-finite>'
        if kind is str:
            return string(item)
        if kind is np.ndarray:
            return dict(type='ndarray', shape=list(item.shape), omitted='array_values')
        if kind in (np.bool_, np.int8, np.int16, np.int32, np.int64,
                    np.uint8, np.uint16, np.uint32, np.uint64,
                    np.float16, np.float32, np.float64):
            return clean(item.item(), depth + 1)
        if kind in (bytes, bytearray, memoryview):
            return dict(type=kind.__name__, length=len(item), omitted='binary_values')
        if kind is dict:
            result = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= 64 or remaining <= 0 or characters <= 0:
                    result['_diagnostic_truncated'] = True
                    break
                name = key if type(key) is str else '<non-string-key>'
                result[string(name, 128)] = clean(child, depth + 1)
            return result
        if kind in (list, tuple, set, frozenset):
            result = []
            for index, child in enumerate(item):
                if index >= 64 or remaining <= 0 or characters <= 0:
                    result.append('<truncated>')
                    break
                result.append(clean(child, depth + 1))
            return result
        return '<unsupported>'

    return clean(value)


class AsyncRecognitionJournal:
    """Best-effort ROI writer with bounded pending work and detached callbacks.

    record() never touches disk or encodes images/JSON. The callback runs on
    the daemon writer and must itself be nonblocking; it receives only small
    immutable scalar evidence, never an app object, metadata, or pixels.
    """
    def __init__(self, directory, *, on_result=None, max_pending=1,
                 min_interval=5, journal=None, clock=time.monotonic):
        if type(max_pending) is not int or not 1 <= max_pending <= 24:
            raise ValueError('max_pending must be an integer between 1 and 24')
        if type(min_interval) not in (int, float) or not math.isfinite(min_interval) or min_interval < 0:
            raise ValueError('min_interval must be finite and non-negative')
        self._journal = journal if journal is not None else RecognitionJournal(directory, min_interval=0)
        self._callback = on_result
        self._clock = clock
        self._max_pending = max_pending
        self._min_interval = min_interval
        self._condition = threading.Condition(threading.Lock())
        self._pending = deque()
        self._closed = False
        self._inflight = False
        self._last_attempt = None
        self._last_error = None
        self._dropped = self._throttled = self._write_errors = self._callback_errors = 0
        self._thread = threading.Thread(target=self._run, name='recognition-evidence', daemon=True)
        self._thread.start()

    @staticmethod
    def _frame(frame):
        if (type(frame) is not np.ndarray or frame.dtype != np.uint8
                or frame.ndim not in (2, 3)
                or (frame.ndim == 3 and frame.shape[2] not in (1, 3, 4))
                or not frame.shape[0] or not frame.shape[1]
                or frame.shape[0] > 256 or frame.shape[1] > 2048
                or frame.nbytes > 1_500_000):
            return None
        return frame.copy()

    @property
    def last_error(self):
        with self._condition:
            return self._last_error

    def status(self):
        with self._condition:
            return dict(queue=len(self._pending), inflight=self._inflight,
                        dropped=self._dropped, throttled=self._throttled,
                        write_errors=self._write_errors, callback_errors=self._callback_errors,
                        closed=self._closed)

    def record(self, stage, metadata, frame=None, level_frame=None):
        # Contending producers must not wait even for another producer's copy.
        if not self._condition.acquire(blocking=False):
            self._dropped += 1
            return None
        try:
            if self._closed or len(self._pending) >= self._max_pending:
                self._dropped += 1
                return None
            now = float(self._clock())
            if not math.isfinite(now):
                raise ValueError('non-finite monotonic time')
            if self._last_attempt is not None and now - self._last_attempt < self._min_interval:
                self._throttled += 1
                return None
            self._last_attempt = now
            detached = _detached_metadata(metadata)
            sample_id = metadata.get('sample_id') if type(metadata) is dict else None
            if type(sample_id) is str:
                sample_id = sample_id[:128]
            elif type(sample_id) is not int or sample_id.bit_length() > 256:
                sample_id = None
            stage = _text(stage[:128] if type(stage) is str else stage, 128)
            self._pending.append((stage, detached, self._frame(frame),
                                  self._frame(level_frame), sample_id))
            self._condition.notify()
        except Exception as error:
            self._dropped += 1
            self._last_error = type(error).__name__
        finally:
            self._condition.release()
        return None

    def _run(self):
        while True:
            with self._condition:
                while not self._pending and not self._closed:
                    self._condition.wait()
                if not self._pending:
                    return
                stage, metadata, frame, level_frame, sample_id = self._pending.popleft()
                self._inflight = True
            path = None
            error_type = None
            try:
                path = self._journal.record(stage, metadata, frame, level_frame)
                error = self._journal.last_error
                if error:
                    error_type = str(error).split(':', 1)[0][:128]
                slot_file = Path(path).name if path is not None else None
            except Exception as error:
                error_type = type(error).__name__
                slot_file = None
            with self._condition:
                self._last_error = error_type
                if error_type:
                    self._write_errors += 1
            if self._callback is not None:
                try:
                    self._callback(dict(sample_id=sample_id, stage=stage, slot_file=slot_file,
                                        error_type=error_type,
                                        result='written' if slot_file else ('error' if error_type else 'skipped')))
                except Exception:
                    with self._condition:
                        self._callback_errors += 1
            with self._condition:
                self._inflight = False
            del metadata, frame, level_frame

    def close(self, timeout=.2):
        """Stop accepting work; drain if possible, never wait beyond timeout."""
        timeout = max(0., min(float(timeout), 60.))
        deadline = time.monotonic() + timeout
        self._closed = True
        if not self._condition.acquire(timeout=timeout):
            return False
        try:
            self._condition.notify_all()
        finally:
            self._condition.release()
        if threading.current_thread() is self._thread:
            return False
        self._thread.join(max(0., deadline - time.monotonic()))
        return not self._thread.is_alive()
