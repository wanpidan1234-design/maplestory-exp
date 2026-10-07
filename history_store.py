"""Transactional, append-only history and local-calendar daily summaries."""
import csv
import json
import hashlib
import math
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from app_time import BEIJING, day_bounds, format_time, local_day, split_seconds
from history_calendar import install_calendar


class HistoryStore:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, started REAL NOT NULL, ended REAL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS samples(
          id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER NOT NULL,
          timestamp REAL NOT NULL, day TEXT NOT NULL, current_exp INTEGER, gain INTEGER NOT NULL,
          total INTEGER NOT NULL, elapsed REAL NOT NULL, realtime REAL, average REAL, hourly REAL,
          status TEXT, raw TEXT, percent REAL, monitor TEXT, UNIQUE(session_id,seq));
        CREATE INDEX IF NOT EXISTS samples_day ON samples(day,timestamp);
        CREATE INDEX IF NOT EXISTS samples_session_time ON samples(session_id,timestamp,id);
        CREATE TABLE IF NOT EXISTS daily_time(session_id TEXT NOT NULL,day TEXT NOT NULL,seconds REAL NOT NULL,PRIMARY KEY(session_id,day));
        CREATE TABLE IF NOT EXISTS imports(digest TEXT PRIMARY KEY,path TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS resume_state(
          id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,
          session_id TEXT,seq INTEGER NOT NULL,total INTEGER NOT NULL,
          elapsed REAL NOT NULL,payload TEXT,saved_at REAL NOT NULL);
        ''')
        # Existing samples retain their original rate; only new metadata is added.
        columns={row['name'] for row in self.db.execute('PRAGMA table_info(samples)')}
        with self.db:
            for name,definition in (
                ('rate_algorithm',"TEXT NOT NULL DEFAULT 'rolling60_v1'"),
                ('rate_state','TEXT'),
                ('rate_half_life','REAL'),
                ('rate_observed_seconds','REAL'),
                ('rate_segment','INTEGER'),
                ('rolling60','REAL'),
                ('rolling120','REAL'),
                ('rolling300','REAL'),
                ('rolling600','REAL'),
                ('rolling1800','REAL'),
                ('window_algorithm','TEXT'),
                ('rate_has_gaps','INTEGER NOT NULL DEFAULT 0'),
                ('window_gap_mask','INTEGER NOT NULL DEFAULT 0'),
            ):
                if name not in columns:
                    self.db.execute(f'ALTER TABLE samples ADD COLUMN {name} {definition}')
        install_calendar(self.db)
        self.active=None
        self.seq=0
        self.last_elapsed=0
        self.last_wall=None

    def begin(self,now=None):
        now=time.time() if now is None else now
        session=uuid.uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO sessions VALUES(?,?,NULL,?)',(session,now,'live'))
        self.active=session
        self.seq=0
        self.last_elapsed=0
        self.last_wall=now
        return self.active

    def _duration(self,session,start,end,seconds):
        for day,value in split_seconds(start,end,seconds):
            self.db.execute('INSERT INTO daily_time VALUES(?,?,?) ON CONFLICT(session_id,day) DO UPDATE SET seconds=seconds+excluded.seconds',(session,day,value))
            self.db.execute('''INSERT INTO beijing_daily_time VALUES(?,?,?,0)
                ON CONFLICT(session_id,day) DO UPDATE SET seconds=seconds+excluded.seconds''',(session,day,value))

    def checkpoint(self,elapsed,now=None):
        if self.active is None:
            return
        now=time.time() if now is None else now
        seconds=max(0,elapsed-self.last_elapsed)
        with self.db:
            self._duration(self.active,self.last_wall,now,seconds)
        self.last_elapsed=elapsed
        self.last_wall=now

    def append(self,row,realtime=0,percent=None,monitor='',now=None,*,rate_metadata=None,resume_state=None):
        now=time.time() if now is None else now
        new_session=self.active is None
        session=uuid.uuid4().hex if new_session else self.active
        next_seq=1 if new_session else self.seq+1
        elapsed=self.last_elapsed
        encoded=None
        if resume_state is not None:
            elapsed=resume_state.get('elapsed') if isinstance(resume_state,dict) else None
            encoded=self._encode_resume(resume_state,elapsed,int(row[4]))
            if not new_session and elapsed<self.last_elapsed:
                raise ValueError('Resume elapsed cannot move backwards')
        metadata=rate_metadata or {}
        window_algorithm=metadata.get('window_algorithm')
        windows=metadata.get('windows') or {}
        window_values=([windows.get(str(seconds),{}).get('value') for seconds in (60,120,300,600,1800)]
                       if window_algorithm is not None else [metadata.get('rolling60'),None,None,None,None])
        gap_mask=sum(1<<index for index,seconds in enumerate((60,120,300,600,1800))
                     if windows.get(str(seconds),{}).get('has_gaps'))
        with self.db:
            if new_session:
                self.db.execute('INSERT INTO sessions VALUES(?,?,NULL,?)',(session,now,'live'))
            self.db.execute('INSERT INTO samples(session_id,seq,timestamp,day,current_exp,gain,total,elapsed,realtime,average,hourly,status,raw,percent,monitor,rate_algorithm,rate_state,rate_half_life,rate_observed_seconds,rate_segment,rolling60,rolling120,rolling300,rolling600,rolling1800,window_algorithm,rate_has_gaps,window_gap_mask) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (session,next_seq,now,local_day(now),row[2],int(row[3]),int(row[4]),float(row[1]),realtime,float(row[5]),float(row[6]),row[7],row[8],percent,monitor,
                 metadata.get('algorithm') or 'rolling60_v1',metadata.get('state'),metadata.get('half_life_seconds'),metadata.get('observed_seconds'),metadata.get('segment'),*window_values,window_algorithm,int(bool(metadata.get('has_gaps'))),gap_mask))
            if encoded is not None:
                self._duration(session,now if new_session else self.last_wall,now,
                               elapsed-(0 if new_session else self.last_elapsed))
                self._write_resume(session,next_seq,int(row[4]),elapsed,encoded,now)
        self.active=session
        self.seq=next_seq
        if new_session:
            self.last_elapsed=0
            self.last_wall=now
        if encoded is not None:
            self.last_elapsed=elapsed
            self.last_wall=now

    def finish(self,elapsed,now=None):
        now=time.time() if now is None else now
        with self.db:
            if self.active is not None:
                self._duration(self.active,self.last_wall,now,max(0,elapsed-self.last_elapsed))
                self.db.execute('UPDATE sessions SET ended=? WHERE id=?',(now,self.active))
            self._write_resume(None,0,0,0,None,now)
        self._detach()

    @staticmethod
    def _encode_resume(payload,elapsed,total):
        if (not isinstance(payload,dict) or isinstance(elapsed,bool)
                or not isinstance(elapsed,(int,float)) or not math.isfinite(elapsed) or elapsed<0
                or type(total) is not int or total<0
                or type(payload.get('total')) is not int or payload['total']!=total
                or isinstance(payload.get('elapsed'),bool)
                or payload.get('elapsed')!=elapsed):
            raise ValueError('Invalid or inconsistent resume state')
        return json.dumps(payload,ensure_ascii=False,allow_nan=False)

    def _write_resume(self,session,seq,total,elapsed,payload,now):
        self.db.execute('''INSERT INTO resume_state VALUES(1,1,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET version=excluded.version,session_id=excluded.session_id,
            seq=excluded.seq,total=excluded.total,elapsed=excluded.elapsed,
            payload=excluded.payload,saved_at=excluded.saved_at''',
            (session,seq,total,elapsed,payload,now))

    def _detach(self):
        self.active=None
        self.seq=0
        self.last_elapsed=0
        self.last_wall=None

    def clear_resume_state(self):
        """Persist an explicit reset, including when no new sample follows it."""
        with self.db:
            self._write_resume(None,0,0,0,None,time.time())
        self._detach()

    def save_resume_state(self,payload,elapsed,now=None):
        """Commit the frozen runtime snapshot and its effective time together."""
        now=time.time() if now is None else now
        total=payload.get('total') if isinstance(payload,dict) else None
        encoded=self._encode_resume(payload,elapsed,total)
        if self.active is None:
            if total or elapsed:
                raise ValueError('A nonempty resume state requires a committed session')
        else:
            latest=self.db.execute('SELECT seq,total FROM samples WHERE session_id=? ORDER BY seq DESC LIMIT 1',
                                   (self.active,)).fetchone()
            if ((latest is None and (self.seq or total))
                    or (latest is not None and (latest['seq']!=self.seq or latest['total']!=total))
                    or elapsed<self.last_elapsed):
                raise ValueError('Resume state does not match the committed ledger')
        with self.db:
            if self.active is not None:
                self._duration(self.active,self.last_wall,now,elapsed-self.last_elapsed)
            self._write_resume(self.active,self.seq,total,elapsed,encoded,now)
        self.last_elapsed=elapsed
        self.last_wall=now

    def _migrate_resume(self,now):
        """One-time aggregate recovery for pre-resume databases, never a curve."""
        session=self.db.execute('''SELECT s.id FROM sessions s WHERE s.source='live'
            AND EXISTS(SELECT 1 FROM samples p WHERE p.session_id=s.id)
            ORDER BY s.started DESC,s.rowid DESC LIMIT 1''').fetchone()
        if session is None:
            with self.db:
                self._write_resume(None,0,0,0,None,now)
            return
        session=session['id']
        latest=self.db.execute('SELECT seq,total,elapsed,timestamp FROM samples WHERE session_id=? ORDER BY seq DESC LIMIT 1',
                               (session,)).fetchone()
        counted=self.db.execute('SELECT COALESCE(SUM(seconds),0) FROM daily_time WHERE session_id=?',(session,)).fetchone()[0]
        elapsed=max(counted,latest['elapsed'])
        payload=dict(legacy=True,total=latest['total'],elapsed=elapsed)
        encoded=self._encode_resume(payload,elapsed,latest['total'])
        with self.db:
            # A crash may leave the final committed sample ahead of its old
            # once-per-second time checkpoint. Assign only that known tail to
            # the old sample's wall time, never across the offline interval.
            tail=elapsed-counted
            self._duration(session,latest['timestamp']-tail,latest['timestamp'],tail)
            self._write_resume(session,latest['seq'],latest['total'],elapsed,encoded,now)

    def load_resume_state(self,now=None):
        """Attach only a snapshot consistent with the committed ledger.

        Rebase the wall-clock checkpoint to this process so shutdown time is
        neither counted nor used to distribute future time across dates.
        """
        now=time.time() if now is None else now
        row=self.db.execute('SELECT * FROM resume_state WHERE id=1').fetchone()
        if row is None:
            try:
                self._migrate_resume(now)
            except (TypeError,ValueError,OverflowError):
                return None
            row=self.db.execute('SELECT * FROM resume_state WHERE id=1').fetchone()
        if row['version']!=1 or row['payload'] is None:
            return None
        try:
            payload=json.loads(row['payload'])
            self._encode_resume(payload,row['elapsed'],row['total'])
            if row['session_id'] is None:
                if row['seq'] or row['total'] or row['elapsed']:
                    return None
            else:
                session=self.db.execute('SELECT source,ended FROM sessions WHERE id=?',(row['session_id'],)).fetchone()
                latest=self.db.execute('SELECT seq,total FROM samples WHERE session_id=? ORDER BY seq DESC LIMIT 1',
                                       (row['session_id'],)).fetchone()
                counted=self.db.execute('SELECT COALESCE(SUM(seconds),0) FROM daily_time WHERE session_id=?',
                                        (row['session_id'],)).fetchone()[0]
                if (session is None or session['source']!='live'
                        or (latest is None and (row['seq'] or row['total']))
                        or (latest is not None and (row['seq']!=latest['seq'] or row['total']!=latest['total']))
                        or not math.isclose(counted,row['elapsed'],rel_tol=1e-10,abs_tol=1e-6)):
                    return None
                if session['ended'] is not None:
                    with self.db:
                        self.db.execute('UPDATE sessions SET ended=NULL WHERE id=?',(row['session_id'],))
        except (TypeError,ValueError,OverflowError):
            return None
        self.active=row['session_id']
        self.seq=row['seq']
        self.last_elapsed=row['elapsed']
        self.last_wall=now
        return payload

    def session_records(self,session=None):
        """Restore the current export rows and their original sequence mapping."""
        session=self.active if session is None else session
        rows=[]
        seqs=[]
        for sample in self.db.execute('SELECT * FROM samples WHERE session_id=? ORDER BY seq',(session,)):
            rows.append([format_time(sample['timestamp'],'%Y-%m-%d %H:%M:%S'),
                         sample['elapsed'],sample['current_exp'],sample['gain'],sample['total'],
                         sample['average'],sample['hourly'],sample['status'],sample['raw']])
            seqs.append(sample['seq'])
        return rows,seqs

    def days(self):
        return [dict(r) for r in self.db.execute('''
        WITH gains AS (SELECT day,SUM(gain) AS gain,COUNT(*) AS sessions,MAX(last_id) last_id FROM beijing_session_days GROUP BY day),
        duration AS (SELECT day,SUM(seconds) AS seconds,MAX(estimated) seconds_estimated FROM beijing_daily_time GROUP BY day),
        dates AS (SELECT day FROM gains UNION SELECT day FROM duration)
        SELECT dates.day,COALESCE(gain,0) gain,COALESCE(seconds,0) seconds,COALESCE(sessions,0) sessions,
            COALESCE(last_id,0) last_id,COALESCE(seconds_estimated,0) seconds_estimated
        FROM dates LEFT JOIN gains USING(day) LEFT JOIN duration USING(day) ORDER BY dates.day DESC''')]

    def sessions(self,day):
        return [dict(r) for r in self.db.execute('''SELECT s.id,s.started,s.ended,s.source,p.first,p.last,p.gain,
        COALESCE(d.seconds,0) seconds,COALESCE(d.estimated,0) seconds_estimated
        FROM sessions s JOIN beijing_session_days p ON s.id=p.session_id
        LEFT JOIN beijing_daily_time d ON d.session_id=s.id AND d.day=p.day
        WHERE p.day=? ORDER BY p.first DESC''',(day,))]

    def samples(self,day,session=None):
        sql='SELECT * FROM samples WHERE timestamp>=? AND timestamp<?'
        args=list(day_bounds(day))
        if session:
            sql+=' AND session_id=?'
            args.append(session)
        return [dict(r) for r in self.db.execute(sql+' ORDER BY timestamp,id',args)]

    def day_minutes(self,day,session=None):
        """At most 1,440 recorded minutes; omit missing minutes and old summaries."""
        sql='''SELECT CAST(timestamp/60 AS INTEGER)*60 timestamp,SUM(gain) gain
            FROM samples WHERE timestamp>=? AND timestamp<? AND COALESCE(status,'')!='legacy_summary' '''
        args=list(day_bounds(day))
        if session:
            sql+=' AND session_id=?'
            args.append(session)
        return [dict(r) for r in self.db.execute(sql+' GROUP BY CAST(timestamp/60 AS INTEGER) ORDER BY timestamp',args)]

    def recent_rate_samples(self,session,after):
        """Read one session across midnight, including missing-rate breakpoints."""
        return [dict(r) for r in self.db.execute('''SELECT session_id,timestamp,realtime,
            rate_algorithm,rate_state,rate_half_life,rate_observed_seconds,rate_segment,
            rolling60,rolling120,rolling300,rolling600,rolling1800,window_algorithm,rate_has_gaps,window_gap_mask
            FROM samples WHERE session_id=? AND timestamp>=? ORDER BY timestamp,id''',(session,after))]

    def session_rate_metadata(self,session):
        """Stream persisted rate snapshots in ledger order for session export."""
        for row in self.db.execute('''SELECT seq,realtime,rate_algorithm,rate_state,rate_half_life,
            rate_observed_seconds,rate_segment,rolling60,rolling120,rolling300,rolling600,
            rolling1800,window_algorithm,rate_has_gaps,window_gap_mask FROM samples WHERE session_id=? ORDER BY seq''',(session,)):
            yield dict(row)

    def import_legacy(self,directory):
        imported=0
        for path in sorted(Path(directory).glob('*.csv')):
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            if self.db.execute('SELECT 1 FROM imports WHERE digest=?',(digest,)).fetchone():
                continue
            with path.open(encoding='utf-8-sig',newline='') as file:
                contents=list(csv.reader(file))
                rows=contents[1:]
            if not rows:
                continue
            parsed=[]
            for row in rows:
                stamp=datetime.strptime(row[0],'%Y-%m-%d %H:%M:%S')
                # Older CSVs carry no offset and retain their original local
                # interpretation; new exports explicitly identify Beijing time.
                if contents and '北京时间' in contents[0][0]:
                    stamp=stamp.replace(tzinfo=BEIJING)
                ts=stamp.timestamp()
                parsed.append((ts,float(row[1]),int(row[2]) if row[2] else None,int(row[3]),int(row[4]),float(row[5]),float(row[6]),row[7],row[8]))
            session='legacy-'+digest
            with self.db:
                self.db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(session,parsed[0][0],parsed[-1][0],'legacy'))
                last_time,last_elapsed=parsed[0][0],0
                for seq,(ts,elapsed,current,gain,total,average,hourly,status,raw) in enumerate(parsed,1):
                    self.db.execute('INSERT INTO samples(session_id,seq,timestamp,day,current_exp,gain,total,elapsed,realtime,average,hourly,status,raw) VALUES(?,?,?,?,?,?,?,?,NULL,?,?,?,?)',(session,seq,ts,local_day(ts),current,gain,total,elapsed,average,hourly,status,raw))
                    self._duration(session,last_time,ts,max(0,elapsed-last_elapsed))
                    last_time,last_elapsed=ts,elapsed
                self.db.execute('INSERT INTO imports VALUES(?,?)',(digest,str(path)))
            imported+=1
        return imported

    def import_snapshot(self,path):
        """Recover only the last old-version aggregate; never invent a curve."""
        path=Path(path)
        if not path.exists():
            return False
        data=json.loads(path.read_text(encoding='utf-8'))
        if 'history_path' in data or not data.get('total'):
            return False
        digest='snapshot-'+hashlib.sha256(path.read_bytes()).hexdigest()
        if self.db.execute('SELECT 1 FROM imports WHERE digest=?',(digest,)).fetchone():
            return False
        timestamp=float(data['updated'])
        # A legacy CSV of this same final sample already contains the aggregate.
        duplicate=self.db.execute('SELECT 1 FROM samples WHERE ABS(timestamp-?)<3 AND total=? AND current_exp=?',(timestamp,data['total'],data['current'])).fetchone()
        if duplicate:
            return False
        elapsed=float(data['elapsed'])
        start=timestamp-elapsed
        # Without dated events a cross-midnight legacy aggregate cannot be split honestly.
        if local_day(start)!=local_day(timestamp):
            return False
        with self.db:
            self.db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(digest,start,timestamp,'summary'))
            self.db.execute('INSERT INTO samples(session_id,seq,timestamp,day,current_exp,gain,total,elapsed,realtime,average,hourly,status,raw) VALUES(?,1,?,?,?,?,?,?,NULL,?,?,?,?)',
                (digest,timestamp,local_day(timestamp),data['current'],data['total'],data['total'],elapsed,data['per_minute'],data['per_hour'],'legacy_summary','旧版最终汇总；逐帧明细不可恢复'))
            self._duration(digest,start,timestamp,elapsed)
            self.db.execute('INSERT INTO imports VALUES(?,?)',(digest,str(path)))
        return True
