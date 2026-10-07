"""Classic orange damage skin: separate pixel glyphs before reading a number."""
import json
from functools import lru_cache
from pathlib import Path
import cv2
import numpy as np


def split_glyphs(mask):
    """Projection joins disconnected highlights within a digit, not entire rows."""
    columns=np.flatnonzero((mask>0).sum(axis=0)>=max(2,mask.shape[0]*.035))
    if not len(columns):return []
    groups=np.split(columns,np.where(np.diff(columns)>1)[0]+1)
    output=[]
    intervals=[(int(g[0]),int(g[-1])+1) for g in groups]
    while intervals:
        left,right=intervals.pop(0)
        glyph=mask[:,left:right]
        rows=np.flatnonzero((glyph>0).sum(axis=1)>0)
        if not len(rows):continue
        bands=np.split(rows,np.where(np.diff(rows)>2)[0]+1)
        # A pet, weapon or character below the number can share its x columns.
        # Keep the densest disconnected row band rather than concatenating it.
        band=max(bands,key=lambda r:int((glyph[r[0]:r[-1]+1]>0).sum()))
        row_offset=int(band[0]);glyph=glyph[row_offset:int(band[-1])+1]
        points=cv2.findNonZero(glyph)
        if points is None:continue
        x,y,w,h=cv2.boundingRect(points)
        if h<mask.shape[0]*.48 or w<3:continue
        # Adjacent slanted digits (especially 11) can touch at one thin column.
        if w/h>.85:
            profile=(glyph[y:y+h,x:x+w]>0).sum(axis=0)
            low=max(3,round(h*.25));high=w-low
            if high>low:
                cut=low+int(np.argmin(profile[low:high]))
                # Wide pairs such as 61 share more orange pixels than 11.
                # Only widen the valley tolerance for a definite multi-digit
                # aspect; each resulting glyph must still pass recognition.
                valley_limit=.35 if w/h>1.1 else .23
                if profile[cut]<h*valley_limit:
                    intervals[0:0]=[(left+x,left+x+cut),(left+x+cut,left+x+w)]
                    continue
        output.append(dict(mask=glyph[y:y+h,x:x+w],box=(left+x,y+row_offset,w,h)))
    return output


class DamageDigits:
    def __init__(self):
        path=Path(__file__).parent/'assets'/'damage-digits.json'
        raw=json.loads(path.read_text(encoding='utf-8'))
        self.templates={c:[np.array(t,dtype=np.uint8)*255 for t in variants] for c,variants in raw['templates'].items()}
        self.features={c:[(t.shape[1]/t.shape[0],self.normalized(t),self.holes(self.normalized(t))) for t in ts] for c,ts in self.templates.items()}
        self.packed_features={c:[(ratio,self.pack(mask),int(mask.sum()),holes) for ratio,mask,holes in variants]
                              for c,variants in self.features.items()}
        self._rank_pixels=lru_cache(maxsize=256)(self._rank_pixels)

    @staticmethod
    def pack(mask):
        return int.from_bytes(np.packbits(mask).tobytes(),'little')

    @staticmethod
    def normalized(mask):
        return cv2.resize(mask,(48,64),interpolation=cv2.INTER_AREA)>100

    @staticmethod
    def holes(mask):
        contours,hierarchy=cv2.findContours(mask.astype('uint8')*255,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
        if hierarchy is None:return 0
        return sum(cv2.contourArea(c)>16 and hierarchy[0][i][3]>=0 for i,c in enumerate(contours))

    def _rank_pixels(self,shape,pixels):
        g=np.frombuffer(pixels,dtype=np.uint8).reshape(shape);h,w=g.shape
        edge=max(1,round(h*.035))
        crops=[g,g[:,edge:],g[:,:-edge]] if w>edge*5 else [g]
        features=[]
        for crop in crops:
            points=cv2.findNonZero(crop)
            if points is None:continue
            gx,gy,gw,gh=cv2.boundingRect(points);a=self.normalized(crop[gy:gy+gh,gx:gx+gw])
            features.append((gw/gh,self.pack(a),int(a.sum()),self.holes(a)))
        ranked=[]
        for digit,variants in self.packed_features.items():
            best=0
            for ratio,b,b_count,template_holes in variants:
                for glyph_ratio,a,a_count,holes in features:
                    aspect=min(glyph_ratio,ratio)/max(glyph_ratio,ratio)
                    intersection=(a & b).bit_count()
                    score=intersection/max(1,a_count+b_count-intersection)*aspect
                    if holes!=template_holes:score*=.8
                    best=max(best,score)
            ranked.append((best,digit))
        return tuple(sorted(ranked,reverse=True))

    def read(self,mask,relaxed=False,glyphs=None):
        if glyphs is None:glyphs=split_glyphs(mask)
        if not 1<=len(glyphs)<=6:return None
        left=glyphs[0]['box'][0];last=glyphs[-1]['box']
        if (last[0]+last[2]-left)/mask.shape[1]<.80:return None
        text='';confidence=1
        heights=[g['mask'].shape[0] for g in glyphs]
        if min(heights)/max(heights)<.65:return None
        for item in glyphs:
            g=item['mask'];ranked=self._rank_pixels(g.shape,g.tobytes())
            margin=ranked[0][0]-ranked[1][0]
            accepted=(ranked[0][0]>=.76 and margin>=.075) or (ranked[0][0]>=.70 and margin>=.16)
            if relaxed:accepted=ranked[0][0]>=.66 and margin>=.035
            if not accepted:return None
            text+=ranked[0][1];confidence=min(confidence,ranked[0][0])
        if text.startswith('0') or int(text)==0:return None
        return int(text),float(confidence),len(glyphs)
