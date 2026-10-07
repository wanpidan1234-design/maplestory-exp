"""Fixed pixel-font matching for short HUD integers that generic OCR drops."""
import json
from pathlib import Path
import cv2
import numpy as np


class HudDigits:
    def __init__(self):
        raw=json.loads((Path(__file__).parent/'assets'/'hud-digits.json').read_text(encoding='utf-8'))
        self.templates={c:[np.array(t,dtype=np.uint8) for t in variants] for c,variants in raw['templates'].items()}
        self.features={c:[(t.shape[1]/t.shape[0],self.pack(cv2.resize(t,(64,64),interpolation=cv2.INTER_NEAREST)>0)) for t in variants]
                       for c,variants in self.templates.items()}

    @staticmethod
    def pack(mask):
        return int.from_bytes(np.packbits(mask).tobytes(),'little')

    def read(self,image):
        hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
        mask=cv2.inRange(hsv,np.array((0,0,180)),np.array((180,90,255)))
        n,labels,stats,_=cv2.connectedComponentsWithStats(mask)
        if n<2:return None
        tallest=max(stats[1:,3]);glyphs=[]
        for i,(x,y,w,h,area) in enumerate(stats[1:],1):
            if h<tallest*.65 or area<max(5,tallest*.6):continue
            g=(labels[y:y+h,x:x+w]==i).astype(np.uint8)
            a=self.pack(cv2.resize(g,(64,64),interpolation=cv2.INTER_NEAREST)>0)
            scores=[]
            for char,variants in self.features.items():
                best=0
                for ratio,b in variants:
                    # Penalize aspect mismatch so a narrow 1 cannot become a 7.
                    aspect=min(w/h,ratio)/max(w/h,ratio)
                    score=((a & b).bit_count()/max(1,(a | b).bit_count()))*aspect
                    best=max(best,score)
                scores.append((best,char))
            scores.sort(reverse=True)
            if scores[0][0]<.78 or scores[0][0]-scores[1][0]<.055:return None
            glyphs.append((x,scores[0][1]))
        if not 1<=len(glyphs)<=7:return None
        return int(''.join(c for x,c in sorted(glyphs)))
