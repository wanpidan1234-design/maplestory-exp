"""Conservative visual accounting: confirmed HUD deltas and tracked floating text."""
from collections import Counter
import math

KINDS=('damage_dealt','damage_taken','hp_spent','mp_spent','hp_restored','mp_restored')


class CombatTracker:
    def __init__(self):
        self.totals={k:0 for k in KINDS}
        self.vitals={'hp':None,'mp':None}
        self.pending={}
        self.last_vitals_time={}
        self.tracks=[]
        self.next_id=0
        self.last_frame=None
        self.frame_count=0

    def break_continuity(self):
        self.pending.clear()
        self.vitals={'hp':None,'mp':None}
        self.last_vitals_time.clear()
        self.tracks=[]
        self.last_frame=None

    def add(self,kind,value,stamp,detail):
        self.totals[kind]+=int(value)
        return dict(kind=kind,amount=int(value),timestamp=stamp,detail=detail)

    def feed_vital(self,name,reading,stamp):
        if reading is None:
            self.pending.pop(name,None)
            if stamp-self.last_vitals_time.get(name,stamp)>2:self.vitals[name]=None
            return []
        reading=tuple(reading)
        previous_time=self.last_vitals_time.get(name)
        if previous_time is not None and stamp-previous_time>2:
            self.vitals[name]=None;self.pending.pop(name,None)
        self.last_vitals_time[name]=stamp
        candidate,count=self.pending.get(name,(None,0))
        count=count+1 if candidate==reading else 1
        self.pending[name]=(reading,count)
        if count<2:return []
        previous=self.vitals[name]
        self.vitals[name]=reading
        if previous is None or previous[1]!=reading[1]:return []
        delta=reading[0]-previous[0]
        if delta==0:return []
        kind=name+('_restored' if delta>0 else '_spent')
        return [self.add(kind,abs(delta),stamp,f'{previous[0]}/{previous[1]} → {reading[0]}/{reading[1]}')]

    @staticmethod
    def distance(a,b):
        return math.hypot((a[0]+a[2]/2)-(b[0]+b[2]/2),(a[1]+a[3]/2)-(b[1]+b[3]/2))

    def feed(self,stamp,readings,observations):
        events=[]
        if self.last_frame is not None and stamp-self.last_frame>1.0:self.break_continuity()
        self.last_frame=stamp;self.frame_count+=1
        if readings is not None:
            for name in ('hp','mp'):events+=self.feed_vital(name,readings.get(name),stamp)
        matched=set()
        for obs in sorted(observations,key=lambda r:-r['score']):
            eligible=[t for t in self.tracks if t['id'] not in matched and t['kind']==obs['kind']
                      and (not t['counted'] or obs['value']==t['counted_value'])
                      and stamp-t['last']<.5 and self.distance(t['box'],obs['box'])<max(40,obs['box'][3]*1.2)]
            if eligible:
                track=min(eligible,key=lambda t:self.distance(t['box'],obs['box']))
            else:
                self.next_id+=1
                track=dict(id=self.next_id,kind=obs['kind'],first=stamp,last=stamp,box=obs['box'],votes=Counter(),counted=False)
                self.tracks.append(track)
            matched.add(track['id'])
            track.update(last=stamp,box=obs['box'])
            track['votes'][obs['value']]+=1
            # Ordinary OCR needs temporal confirmation. A decisive full glyph
            # match can survive a brief float with only one sampled frame.
            value,votes=track['votes'].most_common(1)[0]
            decisive=obs.get('source')=='classic-glyphs' and obs['score']>=.92 and obs['value']>=10
            if not track['counted'] and ((votes>=2 and stamp-track['first']>=.06) or decisive):
                track['counted']=True
                if decisive:value=obs['value']
                track['counted_value']=value
                evidence='逐字形确认' if decisive else '跨帧确认'
                events.append(self.add(track['kind'],value,stamp,f'飘字轨迹 #{track["id"]} · {evidence} · 轨迹去重'))
        self.tracks=[t for t in self.tracks if stamp-t['last']<.65]
        return events
