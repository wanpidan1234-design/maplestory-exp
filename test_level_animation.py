"""Real Tk lifecycle regressions for asynchronous level animation playback."""
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image
import ui_levelup
from ui_levelup import LevelCelebration
from ui_levelup_frames import PreparedFrames


class LevelAnimationTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.geometry('380x500+40+40')
        self.canvas=tk.Canvas(self.root,bg='#edf4ff',highlightthickness=0)
        self.canvas.pack(fill='both',expand=True)
        self.label=self.canvas.create_text(100,30,text='Observer stays live')
        self.root.update();self.effects=[]

    def tearDown(self):
        for effect in self.effects:effect.close()
        self.root.destroy()

    def effect(self,mode='orb'):
        effect=LevelCelebration(self.canvas,self.label,mode=mode)
        self.effects.append(effect)
        return effect

    def pump_until(self,predicate,timeout=2):
        end=time.monotonic()+timeout
        while not predicate() and time.monotonic()<end:
            self.root.update();time.sleep(.002)
        self.root.update()
        self.assertTrue(predicate(),'Timed out waiting for the real Tk event loop')

    def test_real_level_65_decoding_is_off_tk_thread_and_finishes_cleanly(self):
        effect=self.effect();effect.reduced=False
        calls=[];original=Image.open;main_thread=threading.get_ident()
        def observed_open(*args,**kwargs):
            calls.append(threading.get_ident())
            return original(*args,**kwargs)
        with patch('ui_levelup_frames.Image.open',side_effect=observed_open):
            self.assertTrue(effect.play(from_level=64,to_level=65))
            self.pump_until(lambda:effect.frames_rendered>=5)
        self.assertEqual(effect.to_level,65)
        self.assertIsNone(effect.last_error)
        self.assertTrue(calls)
        self.assertNotIn(main_thread,calls)
        self.assertEqual(self.canvas.itemcget(self.label,'state'),'hidden')
        # Move past the boundary: subtraction of monotonic floats can round
        # an exact start + duration back below duration at some system uptimes.
        effect.render(effect.started+effect.DURATION+.001)
        self.assertFalse(effect.active)
        self.assertIsNone(effect.photo)
        self.assertEqual(self.canvas.itemcget(self.label,'state'),'')

    def test_replay_cancels_old_producer_and_keeps_clean_panel_background(self):
        effect=self.effect('panel');effect.play(from_level=64,to_level=65)
        self.pump_until(lambda:effect.frames_rendered>=2)
        background=effect.snapshot;old=effect._frames
        effect.play(from_level=65,to_level=66)
        self.assertIs(effect.snapshot,background)
        self.assertTrue(old.stop.is_set())
        self.pump_until(lambda:effect.frames_rendered>=2 and not old.thread.is_alive())
        self.assertEqual(effect.to_level,66)
        self.assertTrue(effect.effect.winfo_ismapped())
        effect.effect.event_generate('<ButtonRelease-1>');self.root.update()
        self.assertFalse(effect.active)
        self.assertFalse(effect.effect.winfo_ismapped())

    def test_missing_number_uses_unknown_art_without_breaking_playback(self):
        with tempfile.TemporaryDirectory() as folder:
            assets=Path(folder)
            Image.new('RGBA',(32,32),'#1a3456').save(assets/'orb-reduced.png')
            Image.new('RGBA',(32,16),'white').save(assets/'orb-text-unknown.png')
            effect=self.effect();effect.reduced=True
            with patch.object(ui_levelup,'ASSETS',assets):
                effect.play(from_level=64,to_level=65)
                self.pump_until(lambda:effect.last_frame=='reduced')
            self.assertIn('orb-text-65.png',effect.last_error)
            self.assertTrue(effect.active)

    def test_failed_assets_restore_observer_and_next_event_still_plays(self):
        effect=self.effect();effect.reduced=True
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(ui_levelup,'ASSETS',Path(folder)):
                with self.assertLogs('ui_levelup_frames',level='ERROR'):
                    effect.play(from_level=64,to_level=65)
                    self.pump_until(lambda:not effect.active)
        self.assertFalse(effect.closed)
        self.assertEqual(self.canvas.itemcget(self.label,'state'),'')
        self.assertIsNotNone(effect.last_error)
        effect.play(from_level=65,to_level=66)
        self.pump_until(lambda:effect.last_frame=='reduced')
        self.assertIsNone(effect.last_error)

    def test_queue_is_bounded_and_close_releases_producer(self):
        prepared=PreparedFrames(ui_levelup.ASSETS,Image.new('RGBA',(138,138)),
                                'orb','65',False,25,4.2)
        try:
            self.pump_until(lambda:len(prepared.ready)==prepared.MAX_READY)
            time.sleep(.05)
            self.assertEqual(len(prepared.ready),prepared.MAX_READY)
        finally:
            prepared.cancel();prepared.thread.join(timeout=2)
        self.assertFalse(prepared.thread.is_alive())
        self.assertEqual(len(prepared.ready),0)


if __name__=='__main__':unittest.main()
