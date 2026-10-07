import unittest,tkinter as tk,json
from PIL import Image,ImageTk
from monster_portraits import Portraits,sprite_path,LIBRARY

class SpriteLibrary(unittest.TestCase):
    def test_downloaded_library_has_real_transparency(self):
        if not (LIBRARY/'index.json').is_file():
            self.skipTest('Optional local monster sprite library is not installed')
        index=json.loads((LIBRARY/'index.json').read_text(encoding='utf-8'))
        self.assertGreater(len(index),1600)
        for mid in index:
            with Image.open(LIBRARY/f'{mid}.png') as image:
                self.assertEqual(image.mode,'RGBA')
                minimum,maximum=image.getchannel('A').getextrema()
                self.assertEqual(minimum,0);self.assertGreater(maximum,0)

    def test_other_species_resolve_without_scene_capture(self):
        if not (LIBRARY/'index.json').is_file():
            self.skipTest('Optional local monster sprite library is not installed')
        for name in ('青龙','冰龙','蜗牛','绿水灵','花蘑菇'):
            self.assertTrue(sprite_path(name).exists(),name)
        self.assertEqual(sprite_path('不存在的怪物'),LIBRARY/'missing.png')

    def test_display_canvas_retains_alpha(self):
        root=tk.Tk();root.withdraw()
        try:
            portraits=Portraits(root)
            for name in ('青龙','冰龙','蜗牛','不存在的怪物'):
                image=ImageTk.getimage(portraits.get(name,64))
                self.assertEqual(image.getpixel((0,0))[3],0)
                self.assertGreater(image.getchannel('A').getextrema()[1],0)
        finally:root.destroy()
