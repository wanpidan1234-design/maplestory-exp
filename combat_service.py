"""Bounded screenshot worker; database and accounting stay on the UI thread."""
import queue
import threading
import time
import uuid
from app_time import local_day
from combat_tracker import CombatTracker,KINDS
from combat_vision import CombatVision
from game_capture import game_geometry,USER,wt
from fast_capture import FastCapture
from monster_counter import MonsterWorker,MonsterTracker

LABELS={'damage_dealt':'造成伤害','damage_taken':'受到伤害','hp_spent':'血量减少','mp_spent':'蓝量减少',
        'hp_restored':'血量恢复','mp_restored':'蓝量恢复'}


class CombatStore:
    def __init__(self,db):
        self.db=db
        db.executescript('''
        CREATE TABLE IF NOT EXISTS combat_sessions(id TEXT PRIMARY KEY,started REAL NOT NULL,ended REAL);
        CREATE TABLE IF NOT EXISTS combat_events(id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,timestamp REAL NOT NULL,
          day TEXT NOT NULL,kind TEXT NOT NULL,amount INTEGER NOT NULL,detail TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS combat_event_day ON combat_events(day,timestamp);
        CREATE TABLE IF NOT EXISTS combat_samples(id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,timestamp REAL NOT NULL,
          hp INTEGER,hp_max INTEGER,mp INTEGER,mp_max INTEGER,status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS monster_kills(id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,track_id INTEGER NOT NULL,
          timestamp REAL NOT NULL,day TEXT NOT NULL,name TEXT NOT NULL,detail TEXT NOT NULL,UNIQUE(session_id,track_id));
        CREATE INDEX IF NOT EXISTS monster_kills_day ON monster_kills(day,timestamp);
        CREATE INDEX IF NOT EXISTS combat_event_beijing_day
            ON combat_events(strftime('%Y-%m-%d',timestamp,'unixepoch','+8 hours'),timestamp);
        CREATE INDEX IF NOT EXISTS monster_kills_beijing_day
            ON monster_kills(strftime('%Y-%m-%d',timestamp,'unixepoch','+8 hours'),timestamp);
        ''')
        self.session=None;self.sample_time=0

    def begin(self,stamp):
        if self.session is None:
            self.session=uuid.uuid4().hex
            with self.db:self.db.execute('INSERT INTO combat_sessions VALUES(?,?,NULL)',(self.session,stamp))

    def append(self,events,tracker,stamp,status):
        self.begin(stamp)
        with self.db:
            for e in events:
                day=local_day(e['timestamp'])
                self.db.execute('INSERT INTO combat_events(session_id,timestamp,day,kind,amount,detail) VALUES(?,?,?,?,?,?)',
                                (self.session,e['timestamp'],day,e['kind'],e['amount'],e['detail']))
            if stamp-self.sample_time>=1:
                hp=tracker.vitals['hp'] or (None,None);mp=tracker.vitals['mp'] or (None,None)
                self.db.execute('INSERT INTO combat_samples(session_id,timestamp,hp,hp_max,mp,mp_max,status) VALUES(?,?,?,?,?,?,?)',
                                (self.session,stamp,*hp,*mp,status))
                self.sample_time=stamp

    def finish(self):
        if self.session:
            with self.db:self.db.execute('UPDATE combat_sessions SET ended=? WHERE id=?',(time.time(),self.session))
        self.session=None

    def days(self):
        rows=self.db.execute("SELECT strftime('%Y-%m-%d',timestamp,'unixepoch','+8 hours') bj_day,kind,SUM(amount) amount FROM combat_events GROUP BY bj_day,kind ORDER BY bj_day DESC")
        result={}
        for r in rows:
            result.setdefault(r['bj_day'],{k:0 for k in KINDS})[r['kind']]=r['amount']
        return result

    def events(self,day=None,limit=200):
        if day:return [dict(r) for r in self.db.execute("SELECT * FROM combat_events WHERE strftime('%Y-%m-%d',timestamp,'unixepoch','+8 hours')=? ORDER BY id DESC LIMIT ?",(day,limit))]
        return [dict(r) for r in self.db.execute('SELECT * FROM combat_events WHERE session_id=? ORDER BY id DESC LIMIT ?',(self.session,limit))]

    def append_kills(self,events):
        for e in events:
            self.begin(e['timestamp'])
            with self.db:
                if e['action']=='rename':
                    self.db.execute('UPDATE monster_kills SET name=?,detail=? WHERE session_id=? AND track_id=?',
                                    (e['name'],e['detail'],self.session,e['track_id']))
                else:
                    self.db.execute('INSERT OR IGNORE INTO monster_kills(session_id,track_id,timestamp,day,name,detail) VALUES(?,?,?,?,?,?)',
                        (self.session,e['track_id'],e['timestamp'],local_day(e['timestamp']),e['name'],e['detail']))

    def kill_days(self):
        return [r[0] for r in self.db.execute("SELECT DISTINCT strftime('%Y-%m-%d',timestamp,'unixepoch','+8 hours') bj_day FROM monster_kills ORDER BY bj_day DESC")]

    def kills(self,day=None,grouped=True):
        where="strftime('%Y-%m-%d',timestamp,'unixepoch','+8 hours')=?" if day else 'session_id=?';arg=day if day else self.session
        select='name,COUNT(*) count,MAX(timestamp) last' if grouped else '*'
        tail=' GROUP BY name ORDER BY count DESC,name' if grouped else ' ORDER BY timestamp,id'
        return [dict(r) for r in self.db.execute('SELECT '+select+' FROM monster_kills WHERE '+where+tail,(arg,))]


class CombatService:
    def __init__(self,db):
        self.store=CombatStore(db)
        self.tracker=CombatTracker()
        self.monster_tracker=MonsterTracker();self.monster_worker=MonsterWorker();self.monster_status='等待怪物血条'
        self.results=queue.Queue(maxsize=48)
        self.frames=queue.Queue(maxsize=2)
        self.stop=threading.Event();self.lock=threading.Lock()
        self.running=False;self.generation=0
        self.status='等待游戏画面'
        self.last_reading=0;self.fps=0;self.last_box=None
        self.capture_backend='initializing'
        self.thread=threading.Thread(target=self.worker,daemon=True)
        self.capture_thread=threading.Thread(target=self.capture_worker,daemon=True)
        self.thread.start()
        self.capture_thread.start()

    def set_state(self,running,generation):
        with self.lock:
            if self.generation!=generation or self.running!=running:
                self.tracker.break_continuity()
                self.monster_tracker.break_continuity()
                self.last_reading=0
            self.running=running;self.generation=generation
        if not running:self.status='已暂停'

    def poll(self,running,generation):
        self.set_state(running,generation)
        while True:
            try:token,stamp,vitals,observations,status,box=self.results.get_nowait()
            except queue.Empty:break
            if token!=self.generation or not self.running:continue
            self.status=status
            if box is None:
                self.tracker.break_continuity();self.last_reading=0;continue
            if self.last_box!=box:self.tracker.break_continuity();self.last_box=box
            if self.last_reading and stamp>self.last_reading:self.fps=1/(stamp-self.last_reading)
            self.last_reading=stamp
            events=self.tracker.feed(stamp,vitals,observations)
            self.store.append(events,self.tracker,stamp,status)
        while True:
            try:token,stamp,observations,moving,scale,error=self.monster_worker.results.get_nowait()
            except queue.Empty:break
            if token!=self.generation or not self.running:continue
            if error:
                self.monster_tracker.break_continuity();self.monster_status=error;continue
            self.monster_status='按怪物名称识别 · 血条归零确认'
            self.store.append_kills(self.monster_tracker.feed(stamp,observations,moving,scale))

    def reset(self):
        self.store.finish();self.tracker=CombatTracker();self.monster_tracker=MonsterTracker();self.last_reading=0

    def close(self):
        self.stop.set();self.thread.join(timeout=2);self.capture_thread.join(timeout=2);self.monster_worker.close();self.store.finish()

    def summary(self):
        return dict(status=self.status,totals=self.tracker.totals,vitals=self.tracker.vitals,
                    updated=self.last_reading,fps=round(self.fps,1),frames=self.tracker.frame_count,
                    capture_backend=self.capture_backend,mode='single-player-visible-estimate')

    def kill_summary(self):
        today=local_day()
        return dict(totals=dict(self.monster_tracker.totals),today={r['name']:r['count'] for r in self.store.kills(today)},
                    status=self.monster_status,frames=self.monster_tracker.frames)

    @staticmethod
    def visible(geo,rect):
        x,y,w,h=rect;c=geo['client']
        for xx,yy in ((x+w/2,y+h/2),(x+2,y+2),(x+w-2,y+h-2)):
            window=USER.WindowFromPoint(wt.POINT(round(c['left']+xx),round(c['top']+yy)))
            if USER.GetAncestor(window,2)!=geo['hwnd']:return False
        return True

    def offer_frame(self,packet):
        # Keep recent screenshots, never build an ever-growing replay backlog.
        try:self.frames.put_nowait(packet)
        except queue.Full:
            try:self.frames.get_nowait()
            except queue.Empty:pass
            try:self.frames.put_nowait(packet)
            except queue.Full:pass

    def capture_worker(self):
        capture=None
        try:
            while not self.stop.is_set():
                with self.lock:enabled,token=self.running,self.generation
                if not enabled:self.stop.wait(.05);continue
                started=time.monotonic()
                try:
                    geo=game_geometry(include_monitor=False);c=geo['client']
                    if not self.visible(geo,(c['width']*.35,c['height']*.25,c['width']*.3,c['height']*.45)):
                        raise RuntimeError('游戏战斗区域被遮挡')
                    if capture is None:capture=FastCapture()
                    frame=capture.grab(c)
                    self.capture_backend=capture.backend
                    stamp=time.time()
                    if game_geometry(include_monitor=False)['client']!=c:
                        raise RuntimeError('窗口正在移动，等待稳定')
                    packet=(token,stamp,geo,frame,None)
                except Exception as error:
                    if capture is not None:capture.close();capture=None
                    packet=(token,time.time(),None,None,str(error))
                self.offer_frame(packet)
                self.monster_worker.offer(packet)
                interval=.3 if packet[-1] else 1/30
                delay=max(.001,interval-(time.monotonic()-started))
                # Event.wait uses coarse Windows waits (~46ms for a 33ms
                # request here). Python 3.11+ sleep uses a high-resolution timer.
                # Keep long error waits interruptible; active waits are <=33ms.
                if packet[-1]:self.stop.wait(delay)
                else:time.sleep(delay)
        finally:
            if capture is not None:capture.close()

    def worker(self):
        vision=None;last_vitals=0;last_geo=None
        while not self.stop.is_set():
            try:token,stamp,geo,frame,error=self.frames.get(timeout=.1)
            except queue.Empty:continue
            with self.lock:enabled,current_generation=self.running,self.generation
            if not enabled or token!=current_generation:continue
            try:
                if error:raise RuntimeError(error)
                if time.time()-stamp>.5:raise RuntimeError('画面采样延迟，等待新画面')
                if vision is None:vision=CombatVision()
                c=geo['client']
                if not self.visible(geo,(c['width']*.35,c['height']*.25,c['width']*.3,c['height']*.45)):
                    raise RuntimeError('游戏战斗区域被遮挡')
                vitals=None
                if stamp-last_vitals>.3 or c!=last_geo:
                    vitals=vision.vitals(frame)
                    for name in ('hp','mp'):
                        if name not in vitals['rects'] or not self.visible(geo,vitals['rects'][name]):vitals[name]=None
                    last_vitals=stamp
                scale=vision.locator.cached or c['width']/2560
                observations=[r for r in vision.damage_candidates(frame,scale) if self.visible(geo,r['box'])]
                if game_geometry(include_monitor=False)['client']!=c:raise RuntimeError('窗口正在移动，等待稳定')
                status='单人刷怪 · 画面估算'
                if vitals is not None and (vitals.get('hp') is None or vitals.get('mp') is None):status='血蓝读数待确认 · 飘字继续采样'
                result=(token,stamp,vitals,observations,status,tuple(c.values()))
                last_geo=c
            except Exception as error:
                result=(token,stamp,None,[],str(error),None)
            try:self.results.put_nowait(result)
            except queue.Full:
                # A missing frame is a gap, not a license to interpolate damage.
                try:self.results.get_nowait()
                except queue.Empty:pass
                try:self.results.put_nowait((token,time.time(),None,[],'采样积压，重新定位',None))
                except queue.Full:pass
