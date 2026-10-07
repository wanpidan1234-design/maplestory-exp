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
