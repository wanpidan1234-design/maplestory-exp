"""Application calendar and display time, independent of the Windows timezone."""
from datetime import date, datetime, timedelta, timezone
import time

BEIJING = timezone(timedelta(hours=8), 'Asia/Shanghai')


def beijing_datetime(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, BEIJING)


def local_day(timestamp=None):
    return beijing_datetime(timestamp).date().isoformat()


def format_time(timestamp=None, pattern='%H:%M:%S'):
    return beijing_datetime(timestamp).strftime(pattern)


def day_bounds(day):
    start = datetime.combine(date.fromisoformat(day), datetime.min.time(), BEIJING)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def split_seconds(start, end, seconds):
    if seconds <= 0:
        return []
    if end <= start:
        return [(local_day(end), seconds)]
    result = []
    cursor = start
    while cursor < end:
        day = local_day(cursor)
        stop = min(end, day_bounds(day)[1])
        result.append((day, seconds * (stop-cursor)/(end-start)))
        cursor = stop
    return result
