"""Bounded increment feedback for the native blue/white instrument."""
import ctypes
import math
import time
import tkinter as tk
import tkinter.font as tkfont
from ui_theme import FONT,format_damage
from PIL import ImageTk
from ui_damage import damage_image


class OrbMotion:
    """Event-driven rim sweep plus short hover feedback; idle uses no timer."""
    DURATION=.62

    def __init__(self,canvas,ring,scale):
        self.canvas=canvas;self.ring=ring;self.scale=scale
        self.base='#8bcdea';self.hover=False;self.level=0.;self.hover_from=0.
        self.hover_started=None;self.pulse_started=None;self.after_id=None;self.closed=False
        self.reduced=reduce_motion()
        self.sweep=canvas.create_arc(*(round(v*scale) for v in (4,4,134,134)),start=112,extent=0,
            style='arc',outline='#c8f4ff',width=max(1,round(2.4*scale)),state='hidden',tags=('orb-motion',))

    @staticmethod
    def blend(a,b,amount):
        return '#'+''.join(f'{round(int(a[i:i+2],16)*(1-amount)+int(b[i:i+2],16)*amount):02x}' for i in (1,3,5))

    def set_color(self,color):
        self.base=color
        if not self.closed:self.canvas.itemconfigure(self.ring,outline=self.blend(self.base,'#e5faff',self.level*.75))

    def set_hover(self,active):
        if self.closed or self.hover==active:return
        self.hover=active;self.hover_from=self.level;self.hover_started=time.monotonic();self._schedule()

    def pulse(self):
        if self.closed or self.pulse_started is not None:return
        self.pulse_started=time.monotonic();self._schedule()

    def _schedule(self):
        if self.after_id is None:self._tick()

    def _tick(self):
        self.after_id=None
        if self.closed:return
        try:
            if not self.canvas.winfo_exists():self.closed=True;return
            self.render(time.monotonic())
            if self.pulse_started is not None or self.hover_started is not None:
                self.after_id=self.canvas.after(33,self._tick)
        except tk.TclError:self.closed=True

    def render(self,now):
        if self.hover_started is not None:
            progress=min(1,max(0,(now-self.hover_started)/.14));ease=1-(1-progress)**3
            self.level=self.hover_from+(float(self.hover)-self.hover_from)*ease
            if progress>=1:self.hover_started=None
        self.canvas.itemconfigure(self.ring,outline=self.blend(self.base,'#e5faff',self.level*.75),
                                  width=max(1,round((1.4+self.level*.6)*self.scale)))
        if self.pulse_started is None:return
        progress=min(1,max(0,(now-self.pulse_started)/self.DURATION))
        if progress>=1:
            self.pulse_started=None;self.canvas.itemconfigure(self.sweep,state='hidden');return
        ease=1-(1-progress)**3
        strength=math.sin(math.pi*progress)
        self.canvas.itemconfigure(self.sweep,state='normal',
            start=112 if self.reduced else 112-210*ease,
            extent=50 if self.reduced else 12+40*strength,
            outline=self.blend('#366881','#d5f9ff',strength),
            width=max(1,round((1.4+strength)*self.scale)))

    def clear(self):
        if self.after_id is not None:
            try:self.canvas.after_cancel(self.after_id)
            except tk.TclError:pass
        self.after_id=None;self.pulse_started=None;self.hover_started=None
        self.hover=False;self.level=0.
        try:
            self.canvas.itemconfigure(self.sweep,state='hidden')
            self.canvas.itemconfigure(self.ring,outline=self.base,width=max(1,round(1.4*self.scale)))
        except tk.TclError:pass

    def close(self):
        self.clear();self.closed=True


class DeltaWatch:
    def __init__(self):self.token=None;self.values={};self.enabled=False

    def observe(self,values,token,enabled):
        previous=self.values;fresh=self.enabled and enabled and token==self.token
        self.values=dict(values);self.token=token;self.enabled=enabled
        if not fresh:return {}
        return {key:value-previous[key] for key,value in values.items()
                if value is not None and previous.get(key) is not None and value>previous[key]}


def reduce_motion():
    enabled=ctypes.c_int(1)
    try:
        if ctypes.windll.user32.SystemParametersInfoW(0x1042,0,ctypes.byref(enabled),0):return not bool(enabled.value)
    except (AttributeError,OSError):pass
    return False


class CanvasGains:
    """One floating number per lane; bursts queue/coalesce without losing sums."""
    DURATION=.90
    def __init__(self,canvas,lanes,scale=1,font_size=11,on_activity=None,background='#ffffff'):
        self.canvas=canvas;self.lanes=lanes;self.scale=scale;self.font_size=font_size
        self.active={};self.pending={};self.after_id=None;self.closed=False
        self.reduced=reduce_motion();self.on_activity=on_activity
        # A transparent-key canvas may not report the surface visible behind its text.
        self.background=tuple(int(background[i:i+2],16) for i in (1,3,5))

    def push(self,key,amount):
        if self.closed or key not in self.lanes or amount<=0:return
        now=time.monotonic();current=self.active.get(key)
        if current and now-current['started']<.18:
            current['amount']+=amount;self._text(key,current)
        elif current:self.pending[key]=self.pending.get(key,0)+amount
        else:self._start(key,amount,now)
        if self.after_id is None:self._tick()

    def _start(self,key,amount,now):
        x,y,width,color,rise,anchor=self.lanes[key]
        if key in ('damage_dealt','damage_taken'):
            item=self.canvas.create_image(round(x*self.scale),round(y*self.scale),anchor=anchor,tags=('increment-feedback',))
        else:
            item=self.canvas.create_text(round(x*self.scale),round(y*self.scale),anchor=anchor,
                                         fill=color,tags=('increment-feedback',))
        current=dict(item=item,amount=amount,started=now);self.active[key]=current
        self._text(key,current)
        if self.on_activity:self.on_activity(True)

    def _text(self,key,current):
        prefix='' if key in ('damage_dealt','damage_taken') else '+'
        text=prefix+format_damage(current['amount']);width=self.lanes[key][2]*self.scale
        current['text']=text
        if key in ('damage_dealt','damage_taken'):
            current['base']=damage_image(text,key,round(max(16,self.font_size)*self.scale),round(width))
            current['photo']=ImageTk.PhotoImage(current['base'],master=self.canvas)
            self.canvas.itemconfigure(current['item'],image=current['photo'])
            return
        size=self.font_size;font=tkfont.Font(family=FONT,size=-round(size*self.scale),weight='bold')
        while font.measure(text)>width and size>8:
            size-=1;font.configure(size=-round(size*self.scale))
        self.canvas.itemconfigure(current['item'],text=text,font=(FONT,-round(size*self.scale),'bold'))

    def _tick(self):
        self.after_id=None
        if self.closed:return
        try:
            if not self.canvas.winfo_exists():self.closed=True;return
            self.render(time.monotonic())
            if self.active:self.after_id=self.canvas.after(33,self._tick)
        except tk.TclError:self.closed=True;self.active.clear();self.pending.clear()

    def render(self,now):
        for key,current in list(self.active.items()):
            progress=max(0,(now-current['started'])/self.DURATION)
            if progress>=1:
                self.canvas.delete(current['item']);del self.active[key]
                amount=self.pending.pop(key,0)
                if amount:self._start(key,amount,now)
                continue
            x,y,width,color,rise,anchor=self.lanes[key]
            eased=1-(1-progress)**3
            y-=0 if self.reduced else rise*eased
            fade=max(0,(progress-.50)/.50)
            rgb=[int(color[i:i+2],16) for i in (1,3,5)]
            fill='#'+''.join(f'{round(c+(target-c)*fade):02x}' for c,target in zip(rgb,self.background))
            self.canvas.coords(current['item'],round(x*self.scale),round(y*self.scale))
            if key in ('damage_dealt','damage_taken'):
                frame=current['base'].copy()
                if fade:frame.putalpha(frame.getchannel('A').point(lambda value:round(value*(1-fade))))
                current['photo']=ImageTk.PhotoImage(frame,master=self.canvas)
                self.canvas.itemconfigure(current['item'],image=current['photo'])
            else:self.canvas.itemconfigure(current['item'],fill=fill)
            self.canvas.tag_raise(current['item'])
        if not self.active and self.on_activity:self.on_activity(False)

    def clear(self):
        if self.after_id is not None:
            try:self.canvas.after_cancel(self.after_id)
            except tk.TclError:pass
        self.after_id=None;self.pending.clear()
        try:
            for current in self.active.values():self.canvas.delete(current['item'])
            if self.on_activity:self.on_activity(False)
        except tk.TclError:pass
        self.active.clear()

    def close(self):
        self.clear();self.closed=True
