"""Indexed Beijing projections; original samples and duration rows stay intact."""
from collections import defaultdict

from app_time import local_day, split_seconds


def install_calendar(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS history_calendar_meta(version INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS beijing_session_days(
            session_id TEXT NOT NULL, day TEXT NOT NULL, gain INTEGER NOT NULL,
            first REAL NOT NULL, last REAL NOT NULL, samples INTEGER NOT NULL, last_id INTEGER NOT NULL,
            PRIMARY KEY(session_id,day));
        CREATE INDEX IF NOT EXISTS beijing_session_days_day ON beijing_session_days(day);
        CREATE INDEX IF NOT EXISTS samples_timestamp ON samples(timestamp,id);
        CREATE TABLE IF NOT EXISTS beijing_daily_time(
            session_id TEXT NOT NULL,day TEXT NOT NULL,seconds REAL NOT NULL,
            estimated INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(session_id,day));
    ''')
    if db.execute('SELECT 1 FROM history_calendar_meta WHERE version=1').fetchone():
        return
    with db:
        db.execute('''INSERT INTO beijing_session_days
            SELECT session_id,strftime('%Y-%m-%d',timestamp,'unixepoch','+8 hours'),SUM(gain),
                   MIN(timestamp),MAX(timestamp),COUNT(*),MAX(id)
            FROM samples GROUP BY session_id,strftime('%Y-%m-%d',timestamp,'unixepoch','+8 hours')''')
        totals = list(db.execute('SELECT session_id,SUM(seconds) seconds FROM daily_time GROUP BY session_id'))
        resume = db.execute('SELECT session_id,saved_at FROM resume_state WHERE id=1').fetchone()
        for total in totals:
            session, remaining = total['session_id'], total['seconds']
            source = db.execute('SELECT started,ended FROM sessions WHERE id=?',(session,)).fetchone()
            if source is None:
                raise ValueError('Historical duration has no session metadata')
            if remaining <= 0:
                continue
            previous, elapsed = source['started'], 0.0
            allocation = defaultdict(float)
            uncertain=False
            def allocate(start,end,seconds):
                nonlocal uncertain
                if seconds<=0:return
                if end-start>max(5,seconds+1) and local_day(start)!=local_day(end):
                    # A tiny active-time delta across days is not evidence of
                    # activity on the intervening offline dates.
                    allocation[local_day(end)]+=seconds
                    uncertain=True
                else:
                    for day,value in split_seconds(start,end,seconds):allocation[day]+=value
            for row in db.execute('SELECT timestamp,elapsed FROM samples WHERE session_id=? ORDER BY seq',(session,)):
                delta = min(remaining,max(0,row['elapsed']-elapsed))
                allocate(previous,row['timestamp'],delta)
                remaining -= delta
                previous,elapsed = row['timestamp'],max(elapsed,row['elapsed'])
            if remaining > 0:
                end = source['ended'] or (resume['saved_at'] if resume and resume['session_id']==session else previous)
                allocate(previous,max(previous,end),remaining)
            # Historical day totals lacked checkpoint timestamps. The total is
            # exact; a reconstructed split across Beijing dates is approximate.
            estimated = int(uncertain or len(allocation)>1)
            db.executemany('INSERT INTO beijing_daily_time VALUES(?,?,?,?)',
                           [(session,day,seconds,estimated) for day,seconds in allocation.items()])
        db.execute('''CREATE TRIGGER IF NOT EXISTS beijing_sample_insert AFTER INSERT ON samples BEGIN
            INSERT INTO beijing_session_days VALUES(NEW.session_id,
                strftime('%Y-%m-%d',NEW.timestamp,'unixepoch','+8 hours'),NEW.gain,NEW.timestamp,NEW.timestamp,1,NEW.id)
            ON CONFLICT(session_id,day) DO UPDATE SET gain=gain+NEW.gain,
                first=MIN(first,NEW.timestamp),last=MAX(last,NEW.timestamp),samples=samples+1,last_id=MAX(last_id,NEW.id);
        END''')
        db.execute('INSERT INTO history_calendar_meta VALUES(1)')
