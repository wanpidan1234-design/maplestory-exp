"""Native realization of approved C: three tilted orbits and satellite trails."""
import math,time
import tkinter as tk
from PIL import Image,ImageDraw
from ui_feedback import reduce_motion,OrbMotion

ORBITS=((125,84,-.57,'#61dafa',.64,0),(117,89,.65,'#7aa9ff',.48,2),(103,94,1.55,'#93f2e8',-.40,4))
RATIO=138/282

def point(orbit,phase):
    rx,ry,angle,*_=orbit;x=rx*math.cos(phase)*RATIO;y=ry*math.sin(phase)*RATIO
    return 69+x*math.cos(angle)-y*math.sin(angle),69+x*math.sin(angle)+y*math.cos(angle)

def orbital_surface(size,key):
    factor=3;s=size*factor/138
    image=Image.new('RGB',(size*factor,size*factor),'#0d2138');mask=Image.new('L',image.size,0)
    draw=ImageDraw.Draw(image);md=ImageDraw.Draw(mask)
    for orbit in ORBITS:
        points=[tuple(round(v*s) for v in point(orbit,i*math.tau/180)) for i in range(181)]
        width=max(1,round(1.1*s));draw.line(points,fill=orbit[3],width=width);md.line(points,fill=255,width=width)
    r=79*RATIO;box=tuple(round(v*s) for v in (69-r,69-r,69+r,69+r))
    draw.ellipse(box,fill='#0d2138',outline='#3f647a',width=max(1,round(.6*s)));md.ellipse(box,fill=255)
    image=image.resize((size,size),Image.Resampling.LANCZOS);mask=mask.resize((size,size),Image.Resampling.LANCZOS)
    image.paste(key,mask=mask.point(lambda value:255 if value<128 else 0))
    return image

class OrbitEngine:
    DURATION=.9
    def __init__(self,canvas,scale):
        self.canvas=canvas;self.scale=scale;self.reduced=reduce_motion();self.after_id=None;self.closed=False
        self.running=False;self.hover=False;self.level=0.;self.hover_from=0.;self.hover_started=None
        self.pulse_started=None;self.phase=0.;self.last=None;self.base='#8bcdea'
        self.items=[]
        for orbit in ORBITS:
            self.items.append([canvas.create_oval(0,0,0,0,fill=orbit[3],outline='',tags=('orbit-engine',)) for _ in range(9)])
        self.render(time.monotonic())

    def set_running(self,value):
        value=bool(value)
        if self.running==value:
            if value and not self.reduced and self.after_id is None and self.canvas.winfo_viewable():self._schedule()
            return
        self.running=value;self.last=None
        if value:self._schedule()
        elif not self.hover_started and self.pulse_started is None:self._cancel()

    def set_color(self,color):self.base=color

    def set_hover(self,active):
        if self.closed or self.hover==active:return
        self.hover=active;self.hover_from=self.level;self.hover_started=time.monotonic();self._schedule()

    def pulse(self):
        if self.closed or self.pulse_started is not None:return
        self.pulse_started=time.monotonic();self._schedule()

    def _cancel(self):
        if self.after_id is not None:
            try:self.canvas.after_cancel(self.after_id)
            except tk.TclError:pass
        self.after_id=None

    def _schedule(self):
        if self.after_id is None:self._tick()

    def _tick(self):
        self.after_id=None
        if self.closed:return
        try:
            if not self.canvas.winfo_exists():self.close();return
            if not self.canvas.winfo_viewable():self.last=None;return
            self.render(time.monotonic())
            if (self.running and not self.reduced) or self.hover_started is not None or self.pulse_started is not None:
                self.after_id=self.canvas.after(40,self._tick)
        except tk.TclError:self.closed=True;self.after_id=None

    def render(self,now):
        dt=0 if self.last is None else min(.1,max(0,now-self.last));self.last=now
        boost=0
        if self.pulse_started is not None:
            p=max(0,(now-self.pulse_started)/self.DURATION)
            if p>=1:self.pulse_started=None
            else:boost=math.sin(math.pi*p)
        if self.running and not self.reduced:self.phase+=dt*(1+boost*1.8)
        if self.hover_started is not None:
            p=min(1,max(0,(now-self.hover_started)/.14))
            self.level=self.hover_from+(float(self.hover)-self.hover_from)*(1-(1-p)**3)
            if p>=1:self.hover_started=None
        for orbit,items in zip(ORBITS,self.items):
            phase=orbit[5]+self.phase*orbit[4]
            for i,item in enumerate(items):
                x,y=point(orbit,phase-i*.037)
                r=(1.3 if i==0 else .90-i*.065)*(1+.25*boost+.2*self.level)
                color=OrbMotion.blend(orbit[3],'#f0ffff',min(1,.6+boost*.4)) if i==0 else OrbMotion.blend('#17405a',orbit[3],(1-i/9)*.8)
                self.canvas.coords(item,(x-r)*self.scale,(y-r)*self.scale,(x+r)*self.scale,(y+r)*self.scale)
                self.canvas.itemconfigure(item,fill=color,state='hidden' if self.reduced and i else 'normal')

    def clear(self):
        self._cancel();self.running=False;self.pulse_started=None;self.hover_started=None;self.hover=False;self.level=0.;self.last=None
        if not self.closed:
            try:self.render(time.monotonic())
            except tk.TclError:pass

    def close(self):self.clear();self.closed=True
