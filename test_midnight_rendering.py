"""Opt-in B colors and aspect-preserving rendering keep legacy defaults intact."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw

import ui_charts
from ui_feedback import CanvasGains
from ui_levelup_frames import PreparedFrames
from ui_theme import MIDNIGHT


ASSETS=Path(__file__).resolve().parent/'assets'/'levelup-d-dynamic'


class RecordingCanvas:
    def __init__(self):self.calls=[]

    def __getattr__(self,name):
        if name in ('delete','create_text','create_line','create_rectangle','create_oval',
                    'coords','itemconfigure','tag_raise'):
            def record(*args,**kwargs):
                self.calls.append((name,args,kwargs))
                return len(self.calls)
            return record
        raise AttributeError(name)


def prepared_for(size,fit='cover',assets=ASSETS):
    """Exercise the Pillow compositor without scheduling a producer thread."""
    prepared=PreparedFrames.__new__(PreparedFrames)
    prepared.assets=assets;prepared.snapshot=Image.new('RGBA',size,MIDNIGHT['surface'])
    prepared.size=size;prepared.mode='panel';prepared.label='65';prepared.reduced=False
    prepared.fps=25;prepared.duration=4.2;prepared.panel_fit=fit;prepared.native_size=None
    return prepared


class MidnightChartTests(unittest.TestCase):
    def plots(self,**kwargs):
        canvas=RecordingCanvas()
        rows=[dict(timestamp=100,value=10,segment=1),dict(timestamp=105,value=20,segment=1),
              dict(timestamp=110,value=15,segment=2,has_gaps=True),
              dict(timestamp=115,value=16,segment=2,has_gaps=True)]
        ui_charts.plot(canvas,(0,0,530,78),rows,**kwargs)
        with patch('ui_charts.time.time',return_value=120):
            ui_charts.plot(canvas,(0,0,530,78),[
                dict(timestamp=0,value=10),dict(timestamp=60,value=0,observed=False),
                dict(timestamp=120,value=20)],bars=True,**kwargs)
        return canvas.calls

    def test_palette_default_keeps_all_legacy_colors(self):
        calls=self.plots()
        self.assertEqual(calls,self.plots(palette=None))
        fills={kw.get('fill') for _,_,kw in calls}
        self.assertEqual(fills-{None},{ui_charts.MUTED,ui_charts.LINE,ui_charts.ACCENT,
                                     ui_charts.WARNING,'#81b1fc'})
        self.assertEqual({kw['outline'] for kind,_,kw in calls if kind=='create_oval'},{'white'})
        self.assertTrue(all(kw['font'][1]==-9 for kind,_,kw in calls if kind=='create_text'))

    def test_dark_palette_changes_colors_without_changing_gaps_or_coordinates(self):
        legacy=self.plots();dark=self.plots(palette=MIDNIGHT)
        self.assertEqual(len(legacy),len(dark))
        for (old_kind,old_args,old_kw),(kind,args,kw) in zip(legacy,dark):
            self.assertEqual((old_kind,old_args),(kind,args))
            self.assertEqual({k:v for k,v in old_kw.items() if k not in ('fill','outline')},
                             {k:v for k,v in kw.items() if k not in ('fill','outline')})
        self.assertEqual({kw.get('fill') for _,_,kw in dark}-{None},
                         {MIDNIGHT[k] for k in ('muted','line','accent','warning','bar')})
        self.assertEqual({kw['outline'] for kind,_,kw in dark if kind=='create_oval'},
                         {MIDNIGHT['surface']})
        larger=self.plots(palette=MIDNIGHT,font_size=11,scale=1.5)
        self.assertTrue(all(kw['font'][1]==-16 for kind,_,kw in larger if kind=='create_text'))


class MidnightGainTests(unittest.TestCase):
    def gain_fill(self,**kwargs):
        canvas=RecordingCanvas()
        gains=CanvasGains(canvas,{'session':(100,100,90,MIDNIGHT['accent'],10,'e')},**kwargs)
        gains.active['session']=dict(item=1,amount=20,started=0)
        gains.render(gains.DURATION*.999)
        fill=next(kw['fill'] for kind,_,kw in canvas.calls if kind=='itemconfigure')
        gains.render(gains.DURATION)
        self.assertFalse(gains.active)
        self.assertIn(('delete',(1,),{}),canvas.calls)
        return tuple(int(fill[i:i+2],16) for i in (1,3,5))

    def test_text_fades_to_explicit_visible_dark_surface(self):
        actual=self.gain_fill(background=MIDNIGHT['surface'])
        expected=tuple(int(MIDNIGHT['surface'][i:i+2],16) for i in (1,3,5))
        self.assertTrue(all(abs(a-b)<=1 for a,b in zip(actual,expected)))

    def test_legacy_gain_still_fades_to_white(self):
        self.assertTrue(all(channel>=254 for channel in self.gain_fill()))


class MidnightFrameTests(unittest.TestCase):
    def test_real_level_and_transition_art_stays_visible_and_undistorted_at_both_dpi_sizes(self):
        with Image.open(ASSETS/'panel-text-65.png') as source:text=source.convert('RGBA')
        source_bounds=text.getbbox()
        for size in ((570,640),(855,960)):
            for fit in ('contain','cover'):
                with self.subTest(size=size,fit=fit):
                    prepared=prepared_for(size,fit)
                    sx,sy,ox,oy=prepared.transform()
                    self.assertEqual(sx,sy)
                    result=Image.new('RGBA',size)
                    prepared.lettering(result,text,None,1.5)
                    bounds=result.getbbox()
                    self.assertIsNotNone(bounds)
                    self.assertGreater(bounds[0],0);self.assertGreater(bounds[1],0)
                    self.assertLess(bounds[2],size[0]);self.assertLess(bounds[3],size[1])
                    expected_w=(source_bounds[2]-source_bounds[0])*sx/2
                    expected_h=(source_bounds[3]-source_bounds[1])*sy/2
                    self.assertAlmostEqual(bounds[2]-bounds[0],expected_w,delta=5)
                    self.assertAlmostEqual(bounds[3]-bounds[1],expected_h,delta=5)
                    # The caption lives below the large number and must survive cropping too.
                    caption_y=round(oy+(420+342/2)*sy)
                    self.assertIsNotNone(result.crop((0,caption_y,size[0],min(size[1],caption_y+round(24*sy)))).getbbox())

    def test_original_background_and_dynamic_art_use_one_transform(self):
        with tempfile.TemporaryDirectory() as folder:
            assets=Path(folder)
            frame=Image.new('RGBA',(760,2320))
            ImageDraw.Draw(frame).rectangle((300,1020,460,1180),fill='white')
            frame.save(assets/'panel-040.png')
            # A legacy pre-scaled variant must not override the unwarped original.
            Image.new('RGBA',(570,640),'red').save(assets/'panel-040@570x640.png')
            text=Image.new('RGBA',(760,420))
            ImageDraw.Draw(text).rectangle((300,180,460,340),fill='white')
            for size in ((570,640),(855,960)):
                prepared=prepared_for(size,assets=assets)
                prepared.snapshot=Image.new('RGBA',size)
                composed=prepared.compose(40,None,None)
                letters=Image.new('RGBA',size)
                prepared.lettering(letters,text,None,1.6)
                a=composed.getbbox();b=letters.getbbox()
                self.assertTrue(all(abs(x-y)<=2 for x,y in zip(a,b)),(a,b))
                self.assertAlmostEqual(a[2]-a[0],a[3]-a[1],delta=2)

    def test_legacy_transform_and_frame_composition_remain_unchanged(self):
        for size in ((380,1160),(570,1741)):
            prepared=prepared_for(size,'legacy')
            self.assertEqual(prepared.transform(),(size[0]/380,size[1]/1160,0,0))
            actual=prepared.compose(40,None,None)
            with Image.open(ASSETS/f'panel-040@{size[0]}x{size[1]}.png') as source:
                expected=Image.alpha_composite(prepared.snapshot,source.convert('RGBA')).convert('RGB')
            self.assertIsNone(ImageChops.difference(actual,expected).getbbox())


def check_native_panel_lifecycle():
    """A dedicated Tcl interpreter process keeps this fixture out of other Tk suites."""
    import threading
    import time
    import tkinter as tk
    from ui_levelup import LevelCelebration
    root=tk.Tk();canvas=tk.Canvas(root,highlightthickness=0);canvas.pack(fill='both',expand=True)
    title=canvas.create_text(100,30,text='Observer stays live')
    effects=[];prepared_frames=[];main_thread=threading.get_ident()
    def pump_until(predicate):
        end=time.monotonic()+2
        while not predicate() and time.monotonic()<end:root.update();time.sleep(.002)
        root.update()
        assert predicate(),'Native panel lifecycle timed out'
    try:
        for width,height in ((570,640),(855,960)):
            root.geometry(f'{width}x{height}+40+40');root.update()
            effect=LevelCelebration(canvas,title,mode='panel',background='#152b40',panel_fit='cover')
            effects.append(effect);effect.reduced=False
            calls=[];original_open=Image.open
            def observed_open(*args,**kwargs):
                calls.append(threading.get_ident())
                return original_open(*args,**kwargs)
            with patch('ui_levelup.ImageGrab.grab',side_effect=OSError('No snapshot')):
                with patch('ui_levelup_frames.Image.open',side_effect=observed_open):
                    assert effect.play(from_level=64,to_level=65)
                    prepared=effect._frames;prepared_frames.append(prepared)
                    pump_until(lambda:effect.frames_rendered>=3)
            assert calls and main_thread not in calls
            assert effect.snapshot.getpixel((0,0))==(21,43,64,255)
            assert prepared.panel_fit=='cover'
            assert prepared.transform()[0]==prepared.transform()[1]
            assert effect.last_error is None
            assert (effect.photo.width(),effect.photo.height())==(width,height)
            assert len(prepared.ready)<=prepared.MAX_READY
            effect.close();root.update();pump_until(lambda:not prepared.thread.is_alive())
            assert not effect.active and effect.after_id is None
            assert not prepared.ready and not effect.effect.winfo_exists()
    finally:
        for effect in effects:effect.close()
        for prepared in prepared_frames:prepared.cancel();prepared.thread.join(timeout=2)
        root.destroy()


class MidnightNativeTests(unittest.TestCase):
    def test_panel_cover_background_queue_and_async_close_at_both_dpi_sizes(self):
        result=subprocess.run([sys.executable,'-c',
            'from test_midnight_rendering import check_native_panel_lifecycle; check_native_panel_lifecycle()'],
            cwd=Path(__file__).resolve().parent,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
