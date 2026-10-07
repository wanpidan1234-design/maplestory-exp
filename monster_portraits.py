"""Transparent original monster sprites, resolved by the catalog's numeric IDs."""
from pathlib import Path
from hashlib import sha256
from PIL import Image, ImageDraw, ImageOps, ImageTk
import json
from functools import lru_cache

ROOT=Path(__file__).parent/'data'/'monster-portraits'
LIBRARY=Path(__file__).parent/'assets'/'monster-sprites'


def portrait_path(name):
    return ROOT/(sha256(name.encode('utf-8')).hexdigest()+'.png')


@lru_cache(maxsize=1)
def name_ids():
    rows=json.loads((Path(__file__).parent/'assets/monster-catalog.json').read_text(encoding='utf-8'))['monsters']
    result={}
    for r in sorted(rows,key=lambda r:int(r['id'])):result.setdefault(r['name'],[]).append(r['id'])
    return result


def sprite_path(name):
    for mid in name_ids().get(name,[]):
        candidate=LIBRARY/f'{mid}.png'
        if candidate.exists():return candidate
    # Missing species use a neutral placeholder, never a scene screenshot.
    return LIBRARY/'missing.png'


class Portraits:
    def __init__(self,master):
        self.master=master;self.cache={}

    def get(self,name,size):
        path=sprite_path(name);stamp=path.stat().st_mtime_ns if path.exists() else 0
        key=(str(path),size,stamp)
        if key not in self.cache:
            im=Image.new('RGBA',(size,size),(0,0,0,0))
            try:
                with Image.open(path) as source:
                    thumb=ImageOps.contain(source.convert('RGBA'),(size,size),Image.Resampling.LANCZOS)
                im.alpha_composite(thumb,((size-thumb.width)//2,(size-thumb.height)//2))
            except (OSError,ValueError):
                d=ImageDraw.Draw(im)
                d.polygon([(size*.20,size*.22),(size*.40,size*.36),(size*.28,size*.55)],fill='#b9cfe4')
                d.polygon([(size*.80,size*.22),(size*.60,size*.36),(size*.72,size*.55)],fill='#b9cfe4')
                d.ellipse((size*.20,size*.30,size*.80,size*.83),fill='#b9cfe4')
                for x in (.36,.60):d.ellipse((size*x,size*.48,size*(x+.07),size*.58),fill=(0,0,0,0))
            if len(self.cache)>128:self.cache.clear()
            self.cache[key]=ImageTk.PhotoImage(im,master=self.master)
        return self.cache[key]
