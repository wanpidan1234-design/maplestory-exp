"""Generic screen-name categories and conservative zero-health kill evidence."""
from collections import Counter
from pathlib import Path
import json,re,math,queue,threading,time
import cv2
import numpy as np
from experience_ocr import ExperienceOCR
from game_capture import GameCapture,game_geometry,USER,wt

UNKNOWN='未识别怪物'


class MonsterVision:
    def __init__(self):
        assets=Path(__file__).parent/'assets'
        self.prefix=cv2.imread(str(assets/'monster-level-prefix.png'),0)
        catalog=json.loads((assets/'monster-catalog.json').read_text(encoding='utf-8'))
        self.names=sorted({r['name'] for r in catalog['monsters'] if r['name'] and len(r['name'])>=2},key=len,reverse=True)
        self.name_set=set(self.names)
        self.ocr=ExperienceOCR();self.locator=GameCapture();self.last_names=0;self.previous=None

    def parse_name(self,raw,score):
        if score<.75:return None
        text=re.sub(r'\s+','',raw)
        match=re.search(r'等级[0-9Oo?？]{1,3}[·:：.]*(.+)',text)
        if not match:return None
        tail=match[1]
        for name in self.names:
            if tail.startswith(name):return name
        # New/custom names are admitted without a fixed map whitelist, but
        # require cleaner OCR and repeated temporal agreement in the tracker.
        if score>=.90 and re.fullmatch(r'[\u4e00-\u9fff]{2,16}',tail):return tail
        return None

    def read_name(self,frame,x,y,height,scale):
        answers=[]
        # A wide line used to concatenate pet/player names onto monster text.
        # Try shorter fields too; require agreement or a clean, confident name.
        for width in (160,220,330):
            patch=np.ascontiguousarray(frame[max(0,y-2):y+height+2,x:min(frame.shape[1],x+round(width*scale)),:3])
            if not patch.size:continue
            im=cv2.copyMakeBorder(cv2.resize(patch,None,fx=2,fy=2,interpolation=cv2.INTER_NEAREST),5,5,5,5,cv2.BORDER_CONSTANT,value=(30,30,30))
            raw,score=self.ocr.recognize(im);name=self.parse_name(raw,score)
            if name:answers.append((name,score))
        if not answers:
            # The fixed level badge is followed by the name field. Reading that
            # short field alone avoids the level/pet/player collision in CTC.
            start=x+round(77*scale)
            for width in (52,86):
                patch=np.ascontiguousarray(frame[max(0,y-2):y+height+2,start:min(frame.shape[1],start+round(width*scale)),:3])
                if not patch.size:continue
                im=cv2.copyMakeBorder(cv2.resize(patch,None,fx=3,fy=3,interpolation=cv2.INTER_NEAREST),8,8,8,8,cv2.BORDER_CONSTANT,value=(30,30,30))
                raw,score=self.ocr.recognize(im);name=re.sub(r'[\s·:：.]','',raw)
                if score>=.80 and name in self.name_set:answers.append((name,score))
        if not answers:return None
        votes=Counter(n for n,s in answers)
        ranked=sorted(votes,key=lambda n:(votes[n],len(n)),reverse=True)
        if len(ranked)>1 and votes[ranked[0]]==votes[ranked[1]]:return None
        name=ranked[0]
        if votes[name]>=2 or max(s for n,s in answers if n==name)>=.84:
            return name,max(s for n,s in answers if n==name)
        return None

    def read(self,frame,stamp,scale=1):
        h,w=frame.shape[:2];bottom=round(h*.88)
        hsv=cv2.cvtColor(frame[:bottom],cv2.COLOR_BGR2HSV)
        gray=cv2.cvtColor(frame[:bottom],cv2.COLOR_BGR2GRAY)
        small=cv2.resize(gray,(256,144),interpolation=cv2.INTER_AREA).astype(np.float32)
        moving=False
        if self.previous is not None:
            shift,response=cv2.phaseCorrelate(self.previous,small)
            moving=response<.08 or (response>.12 and math.hypot(shift[0]*w/256,shift[1]*bottom/144)>9*scale)
        self.previous=small
        border=cv2.inRange(hsv,np.array((0,0,75)),np.array((180,45,240)))
        border[:round(100*scale)]=0;border[:round(400*scale),:round(360*scale)]=0
        contours,_=cv2.findContours(border,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        observations=[]
        for contour in contours:
            x,y,bw,bh=cv2.boundingRect(contour)
            if not(90*scale<=bw<=98*scale and 12*scale<=bh<=18*scale):continue
            inset=max(2,round(3*scale));inside=hsv[y+inset:y+bh-inset,x+inset:x+bw-inset]
            if not inside.size:continue
            green=cv2.inRange(inside,np.array((35,110,110)),np.array((100,255,255)))
            green_columns=(green>0).sum(axis=0)>=max(1,inside.shape[0]*.4)
            fraction=float(green_columns.mean())
            empty=fraction==0 and float((inside[:,:,2]<65).mean())>.94
            if not empty and fraction==0:continue
            observations.append(dict(box=(x,y,bw,bh),hp=fraction,empty=empty,name=None))
        # Names change much less often than HP. Restrict expensive matching to
        # the union of narrow bands beneath observed health bars.
        if observations and stamp-self.last_names>=.08:
            self.last_names=stamp
            template=cv2.resize(self.prefix,None,fx=scale,fy=scale,interpolation=cv2.INTER_NEAREST)
            white=cv2.inRange(hsv,np.array((0,0,145)),np.array((180,95,255)))
            seen=set();labels=[]
            for obs in observations:
                x,y,bw,bh=obs['box']
                left=max(0,x-round(150*scale));right=min(w,x+bw+round(150*scale))
                top=y+round(45*scale);end=min(bottom,y+round(340*scale))
                search=white[top:end,left:right]
                if search.shape[0]<template.shape[0] or search.shape[1]<template.shape[1]:continue
                scores=cv2.matchTemplate(search,template,cv2.TM_CCOEFF_NORMED)
                for _ in range(8):
                    _,score,_,(xx,yy)=cv2.minMaxLoc(scores)
                    if score<.60:break
                    scores[max(0,yy-template.shape[0]):yy+template.shape[0],max(0,xx-template.shape[1]):xx+template.shape[1]]=0
                    xx+=left;yy+=top;key=(round(xx/5),round(yy/5))
                    if key in seen:continue
                    seen.add(key)
                    reading=self.read_name(frame,xx,yy,template.shape[0],scale)
                    if reading:labels.append((xx,yy,*reading))
            for obs in observations:
                x,y,bw,bh=obs['box']
                candidates=[(abs(lx+75*scale-(x+bw/2)),ly,name,score) for lx,ly,name,score in labels
                            if y+40*scale<ly<y+340*scale and abs(lx+75*scale-(x+bw/2))<85*scale]
                candidates.sort()
                if candidates and (len(candidates)==1 or candidates[1][0]-candidates[0][0]>15*scale):
                    obs['name']=candidates[0][2]
        return observations,moving


class MonsterTracker:
    def __init__(self):
        self.tracks=[];self.next_id=0;self.totals=Counter();self.last_stamp=None;self.frames=0

    def break_continuity(self):
        self.tracks=[];self.last_stamp=None

    def feed(self,stamp,observations,moving=False,scale=1):
        self.frames+=1
        if moving or (self.last_stamp is not None and stamp-self.last_stamp>.6):self.break_continuity()
        self.last_stamp=stamp;events=[];matched=set()
        for obs in observations:
            x,y,w,h=obs['box'];eligible=[]
            for t in self.tracks:
                if t['id'] in matched or stamp-t['last']>.75:continue
                tx,ty,tw,th=t['box'];distance=math.hypot(x-tx,y-ty)
                if distance<max(65,200*(stamp-t['last']))*scale:eligible.append((distance,t))
            eligible.sort(key=lambda pair:pair[0])
            if eligible and (len(eligible)==1 or eligible[1][0]-eligible[0][0]>12*scale):t=eligible[0][1]
            else:
                self.next_id+=1
                t=dict(id=self.next_id,last=stamp,box=obs['box'],alive=0,zero=0,zero_since=None,names=Counter(),counted=False)
                self.tracks.append(t)
            matched.add(t['id']);t.update(last=stamp,box=obs['box'])
            if obs.get('name'):t['names'][obs['name']]+=1
            if not obs['empty']:
                t['alive']+=1;t['zero']=0;t['zero_since']=None
            else:
                t['zero']+=1
                if t['zero_since'] is None:t['zero_since']=stamp
            if not moving and not t['counted'] and t['alive']>=2 and t['zero']>=2 and stamp-t['zero_since']>=.08:
                name,votes=t['names'].most_common(1)[0] if t['names'] else (UNKNOWN,0)
                if votes<2:name=UNKNOWN
                t['counted']=True;t['counted_name']=name;self.totals[name]+=1
                events.append(dict(action='kill',track_id=t['id'],timestamp=stamp,name=name,detail='有血量 → 血条归零跨帧确认；名称跨帧识别' if name!=UNKNOWN else '血条归零跨帧确认；名称遮挡或未通过校验'))
            elif t['counted'] and t['counted_name']==UNKNOWN and t['names']:
                name,votes=t['names'].most_common(1)[0]
                if votes>=2:
                    self.totals[UNKNOWN]-=1;self.totals[name]+=1;t['counted_name']=name
                    events.append(dict(action='rename',track_id=t['id'],timestamp=stamp,name=name,detail='死亡前后名称跨帧补充确认'))
        self.tracks=[t for t in self.tracks if stamp-t['last']<.9]
        return events


class MonsterWorker:
    def __init__(self):
        self.frames=queue.Queue(maxsize=1);self.results=queue.Queue(maxsize=32);self.stop=threading.Event()
        self.last_offer=0;self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def offer(self,packet):
        if packet[-1] is None and packet[1]-self.last_offer<.08:return
        self.last_offer=packet[1]
        try:self.frames.put_nowait(packet)
        except queue.Full:
            try:self.frames.get_nowait()
            except queue.Empty:pass
            try:self.frames.put_nowait(packet)
            except queue.Full:pass

    def run(self):
        vision=None;last_key=None
        while not self.stop.is_set():
            try:token,stamp,geo,frame,error=self.frames.get(timeout=.1)
            except queue.Empty:continue
            try:
                if error:raise RuntimeError(error)
                if time.time()-stamp>.5:raise RuntimeError('怪物画面过期')
                key=(token,tuple(geo['client'].values()))
                if vision is None:vision=MonsterVision()
                changed=last_key is not None and key!=last_key
                if key!=last_key:vision.previous=None
                last_key=key
                _,hud_roi=vision.locator.crop(frame[round(frame.shape[0]*.88):])
                scale=hud_roi['scale']
                observations,moving=vision.read(frame,stamp,scale)
                if game_geometry(include_monitor=False)['client']!=geo['client']:raise RuntimeError('窗口移动，等待稳定')
                visible=[];client=geo['client']
                for obs in observations:
                    x,y,w,h=obs['box']
                    if all(USER.GetAncestor(USER.WindowFromPoint(wt.POINT(round(client['left']+px),round(client['top']+py))),2)==geo['hwnd']
                           for px,py in ((x+2,y+2),(x+w-2,y+h-2))):visible.append(obs)
                result=(token,stamp,visible,moving or changed,scale,None)
            except Exception as error:result=(token,stamp,[],True,1,str(error))
            try:self.results.put_nowait(result)
            except queue.Full:
                try:self.results.get_nowait()
                except queue.Empty:pass
                try:self.results.put_nowait((token,stamp,[],True,1,'怪物采样中断'))
                except queue.Full:pass

    def close(self):
        self.stop.set();self.thread.join(timeout=2)
