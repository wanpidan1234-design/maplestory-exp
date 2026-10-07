"""Approved D Canvas frames on native Tk; no browser/runtime rendering dependency.

Assets are baked from the approved renderer with only truthful level-copy changes.
The native observer remains live underneath a temporary, full-window image layer.
"""
import logging
from pathlib import Path
import time
import tkinter as tk
from PIL import Image, ImageGrab, ImageTk
from ui_feedback import reduce_motion
from game_capture import USER
from ui_levelup_frames import PreparedFrames

ASSETS=Path(__file__).resolve().parent/'assets'/'levelup-d-dynamic'
KEY='#010203'


class LevelCelebration:
    DURATION=4.2
    FPS=25

    def __init__(self,canvas,title,scale=1,mode='orb',on_finish=None,hide_items=(),
                 background=None,panel_fit='legacy'):
        if panel_fit not in ('legacy','contain','cover'):raise ValueError('Unsupported panel fit')
        self.canvas=canvas;self.title=title;self.scale=scale;self.mode=mode;self.on_finish=on_finish
        self.background=background;self.panel_fit=panel_fit
        self.started=None;self.after_id=None;self.closed=False;self.reduced=reduce_motion()
        self.hide_items=hide_items;self.hidden_states={};self.photo=None;self.snapshot=None
        self.last_frame=None;self.last_render_ms=0;self.last_error=None;self.frames_rendered=0;self._frames=None
        self.from_level=None;self.to_level=None
        self.effect=canvas if mode=='orb' else tk.Canvas(canvas,bg=background or '#081221',highlightthickness=0,cursor='hand2')
        self.frame_item=self.effect.create_image(0,0,anchor='nw',state='hidden',tags=('level-celebration',))
        self.effect_items=[self.frame_item]
        if mode=='panel':self.effect.bind('<ButtonRelease-1>',lambda event:self.finish())

    @property
    def active(self):return self.started is not None

    @property
    def preparing(self):return self.active and self.last_frame is None

    def diagnostics(self):
        return dict(active=self.active,preparing=self.preparing,last_error=self.last_error,
                    frames_rendered=self.frames_rendered,last_frame=self.last_frame,
                    last_render_ms=round(self.last_render_ms,3),from_level=self.from_level,to_level=self.to_level)

    def _snapshot(self):
        self.canvas.update_idletasks()
        self.size=(max(1,self.canvas.winfo_width()),max(1,self.canvas.winfo_height()))
        if self.mode=='orb':
            self.snapshot=Image.new('RGBA',self.size,KEY)
            return
        try:
            hwnd=USER.GetAncestor(self.canvas.winfo_toplevel().winfo_id(),2)
            frame=ImageGrab.grab(window=hwnd).convert('RGBA')
            self.snapshot=frame if frame.size==self.size else frame.resize(self.size)
        except (OSError,ValueError,tk.TclError):self.snapshot=Image.new('RGBA',self.size,self.background or '#edf4ff')

    def play(self,started=None,from_level=None,to_level=None):
        if self.closed:return False
        # A replay during an active celebration must not snapshot its own overlay.
        background=self.snapshot if self.active and self.mode=='panel' else None
        self.clear()
        self.last_error=None;self.frames_rendered=0
        if background is not None:self.snapshot=background;self.size=background.size
        else:self._snapshot()
        valid=isinstance(from_level,int) and isinstance(to_level,int) and 1<=from_level<to_level<=300 and to_level==from_level+1
        self.from_level=from_level if valid else None;self.to_level=to_level if valid else None
        label=str(self.to_level) if valid else 'unknown'
        self.started=time.monotonic() if started is None else started
        elapsed=max(0,time.monotonic()-self.started)
        if elapsed>=self.DURATION:self.finish();return False
        self._frames=PreparedFrames(ASSETS,self.snapshot,self.mode,label,self.reduced,self.FPS,
                                    self.DURATION,start_index=int(elapsed*self.FPS),panel_fit=self.panel_fit)
        self._tick()
        return True

    def _show(self,index):
        if self.mode=='panel':
            self.effect.place(x=0,y=0,relwidth=1,relheight=1)
            self.effect.tk.call('raise',self.effect._w)
        elif not self.hidden_states:
            # Hide the incumbent C artwork as well, avoiding duplicate orbit rings.
            self.hidden_states={item:self.canvas.itemcget(item,'state') for item in self.canvas.find_all() if item!=self.frame_item}
        if self.mode=='orb':
            for item,state in self.hidden_states.items():self.canvas.itemconfigure(item,state=state if index==0 else 'hidden')
        self.effect.itemconfigure(self.frame_item,state='hidden' if self.mode=='orb' and index==0 else 'normal')
        self.effect.tag_raise(self.frame_item)

    def _tick(self):
        self.after_id=None
        if self.closed or not self.active:return
        began=time.monotonic()
        try:
            self.render(began)
            if self.active:
                # Poll prepared frames cheaply without adding preparation time
                # to the animation timeline or blocking other Tk callbacks.
                elapsed=time.monotonic()-self.started
                delay=min(8,max(1,round(((int(elapsed*self.FPS)+1)/self.FPS-elapsed)*1000)))
                self.after_id=self.canvas.after(delay,self._tick)
        except tk.TclError as error:
            self.last_error=f'TclError: {error}';self.clear();self.closed=True
        except Exception as error:
            self.last_error=f'{type(error).__name__}: {error}'
            logging.getLogger(__name__).exception('Unable to present %s level animation',self.mode)
            self.finish()

    def render(self,now):
        if not self.active:return
        began=time.perf_counter();elapsed=max(0,now-self.started)
        if elapsed>=self.DURATION:self.finish();return
        index='reduced' if self.reduced else min(104,int(elapsed*self.FPS+1e-7))
        if self._frames.error:self.last_error=self._frames.error
        if index==self.last_frame:return
        prepared=self._frames.take(index)
        if prepared is None:
            if self._frames.done and self._frames.error and self.last_frame is None:self.finish()
            return
        shown_index,result=prepared
        if self.photo is None:
            self.photo=ImageTk.PhotoImage(result,master=self.effect)
            self.effect.itemconfigure(self.frame_item,image=self.photo)
        else:self.photo.paste(result)
        self._show(shown_index)
        self.last_frame=shown_index;self.frames_rendered+=1
        self.last_render_ms=(time.perf_counter()-began)*1000

    def finish(self):
        if not self.active:return
        self.clear()
        if self.on_finish:self.on_finish()

    def clear(self):
        if self.after_id is not None:
            try:self.canvas.after_cancel(self.after_id)
            except tk.TclError:pass
        self.after_id=None;self.started=None
        if self._frames is not None:self._frames.cancel();self._frames=None
        try:
            self.effect.itemconfigure(self.frame_item,state='hidden',image='')
            if self.mode=='panel':self.effect.place_forget()
            else:
                for item,state in self.hidden_states.items():self.canvas.itemconfigure(item,state=state)
        except tk.TclError:pass
        self.photo=None;self.snapshot=None;self.last_frame=None;self.hidden_states={}

    def close(self):
        self.clear();self.closed=True
        if self.mode=='panel':
            try:self.effect.destroy()
            except tk.TclError:pass
