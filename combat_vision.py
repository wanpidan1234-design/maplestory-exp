"""Visible HUD readings and classic orange/purple floating-number observations."""
from collections import Counter
import re
import cv2
import numpy as np
from experience_ocr import ExperienceOCR
from game_capture import GameCapture
from hud_digits import HudDigits
from damage_digits import DamageDigits,split_glyphs


class CombatVision:
    def __init__(self):
        self.ocr=ExperienceOCR()
        self.locator=GameCapture()
        self.digits=HudDigits()
        self.damage_digits=DamageDigits()

    def integer(self,image):
        exact=self.digits.read(image)
        if exact is not None:return exact
        hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
        mask=cv2.inRange(hsv,np.array((0,0,170)),np.array((180,95,255)))
        points=cv2.findNonZero(mask)
        if points is None:return None
        x,y,w,h=cv2.boundingRect(points)
        if h<5 or w<2:return None
        pad=max(3,round(h*.35))
        images=[image[y:y+h,x:x+w],255-mask[y:y+h,x:x+w]]
        votes=[]
        for im,bg in zip(images,[(55,65,65),255]):
            im=cv2.copyMakeBorder(im,pad,pad,pad,pad,cv2.BORDER_CONSTANT,value=bg)
            raw,score=self.ocr.recognize(cv2.resize(im,None,fx=3,fy=3,interpolation=cv2.INTER_NEAREST))
            raw=raw.replace(' ','')
            if score>=.78 and re.fullmatch(r'\d{1,7}',raw):votes.append(int(raw))
        return votes[0] if len(votes)==2 and len(set(votes))==1 else None

    def ratio(self,image):
        """Locate the diagonal slash geometrically, then OCR both integers."""
        hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
        mask=cv2.inRange(hsv,np.array((0,0,180)),np.array((180,90,255)))
        n,labels,stats,_=cv2.connectedComponentsWithStats(mask)
        slash=[]
        for i,(x,y,w,h,area) in enumerate(stats[1:],1):
            if h<6 or not .65<=w/h<=1.5 or area/max(1,w*h)>.32:continue
            yy,xx=np.where(labels[y:y+h,x:x+w]==i)
            if len(xx)>5 and np.std(xx)>0 and np.std(yy)>0 and np.corrcoef(xx,yy)[0,1]<-.94:
                slash.append((x,w))
        if len(slash)!=1:return None
        x,w=slash[0]
        current=self.integer(image[:,:x])
        maximum=self.integer(image[:,x+w:])
        if current is None or maximum is None or not 0<=current<=maximum or maximum<=0:return None
        return (current,maximum)

    def vitals(self,frame):
        top=round(frame.shape[0]*.88)
        band=frame[top:]
        _,roi=self.locator.crop(band)
        x,y,h,s=roi['left'],roi['top'],roi['height'],roi['scale']
        result={'scale':s,'rects':{}}
        for name,offset in [('hp',660),('mp',340)]:
            left=x-round(offset*s);width=round(270*s)
            if left<0:result[name]=None;continue
            # MSS BGRA is consumed directly for full-frame processing; only
            # these tiny color OCR patches need contiguous three-channel BGR.
            crop=np.ascontiguousarray(band[y:y+h,left:left+width,:3])
            result[name]=self.ratio(crop)
            result['rects'][name]=(left,top+y,width,h)
        return result

    @staticmethod
    def damage_regions(joined,scale):
        # Trace boundaries first instead of allocating a full-screen int32 label
        # map. CCOMP retains foreground islands inside holes as separate roots.
        contours,hierarchy=cv2.findContours(joined,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
        if hierarchy is None:return []
        regions=[]
        for i,contour in enumerate(contours):
            if hierarchy[0][i][3]>=0:continue
            x,y,w,h=cv2.boundingRect(contour)
            if not(40*scale<=h<=95*scale and 20*scale<=w<=420*scale):continue
            # Local labeling preserves the original pixel-area threshold even
            # when another disconnected shape lies inside this bounding box.
            _,labels,stats,_=cv2.connectedComponentsWithStats(joined[y:y+h,x:x+w])
            px,py=contour[0,0];label=labels[py-y,px-x]
            area=stats[label,cv2.CC_STAT_AREA]
            if area>180*scale*scale:regions.append((x,y,w,h,int(area)))
        return sorted(regions,key=lambda r:(r[1],r[0]))

    def damage_candidates(self,frame,scale=1):
        top=round(80*scale);bottom=round(frame.shape[0]*.89)
        if bottom<=top:return []
        kernel=np.ones((max(1,round(5*scale)),max(3,round(13*scale))),np.uint8)
        # Excluded HUD rows need not pass through HSV/labeling. Retain enough
        # zero padding to reproduce morphology and OCR margins at both edges.
        padding=max(3,round(7*scale),kernel.shape[0]*2)
        y_offset=top-padding
        hsv=cv2.cvtColor(frame[top:bottom],cv2.COLOR_BGR2HSV)
        output=[]
        for kind,lo,hi in [('damage_dealt',(0,125,125),(38,255,255)),('damage_taken',(125,125,125),(174,255,255))]:
            mask=cv2.inRange(hsv,np.array(lo),np.array(hi))
            mask=cv2.copyMakeBorder(mask,padding,padding,0,0,cv2.BORDER_CONSTANT,value=0)
            # Minimap and fixed left-side HUD are not floating combat numbers.
            mask[:max(0,round(370*scale)-y_offset),:round(345*scale)]=0
            joined=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,kernel)
            for x,y,w,h,area in self.damage_regions(joined,scale):
                if not(40*scale<=h<=95*scale and 20*scale<=w<=420*scale and area>180*scale*scale):continue
                glyph_count=None
                weak=None
                if kind=='damage_dealt':
                    number_mask=mask[y:y+h,x:x+w]
                    glyphs=split_glyphs(number_mask)
                    exact=self.damage_digits.read(number_mask,glyphs=glyphs)
                    if exact is not None:
                        value,score,glyph_count=exact
                        output.append(dict(kind=kind,value=value,score=score,box=(int(x),int(y+y_offset),int(w),int(h)),source='classic-glyphs'))
                        continue
                    weak=self.damage_digits.read(number_mask,relaxed=True,glyphs=glyphs)
                    glyph_count=len(glyphs)
                    # Do not send merged rows or object silhouettes to the OCR.
                    if not 1<=glyph_count<=6:continue
                    heights=[g['mask'].shape[0] for g in glyphs]
                    if min(heights)/max(heights)<.65:continue
                    left=glyphs[0]['box'][0];last=glyphs[-1]['box']
                    if (last[0]+last[2]-left)/w<.80:continue
                    # Animation offsets each digit vertically. Align the clean
                    # glyphs into a text line so OCR sees a normal baseline.
                    aligned=[]
                    for item in glyphs:
                        g=item['mask'];gh,gw=g.shape
                        g=cv2.resize(255-g,(max(1,round(gw/gh*48)),48),interpolation=cv2.INTER_NEAREST)
                        aligned.append(cv2.copyMakeBorder(g,0,0,3,3,cv2.BORDER_CONSTANT,value=255))
                    row=np.hstack(aligned);aligned_votes=[];aligned_scores=[]
                    for pad in (10,15):
                        im=cv2.copyMakeBorder(row,pad,pad,pad,pad,cv2.BORDER_CONSTANT,value=255)
                        raw,score=self.ocr.recognize(im)
                        raw=raw.replace(' ','').replace(',','')
                        if re.fullmatch(r'\d{1,6}',raw) and len(raw)==glyph_count and not raw.startswith('0'):
                            threshold=.65 if weak is not None and int(raw)==weak[0] else .75
                            if score>=threshold:aligned_votes.append(int(raw));aligned_scores.append(score)
                        # Two valid, agreeing variants are required here. Once
                        # the first fails, the second cannot change the verdict.
                        if not aligned_votes:break
                    if len(aligned_votes)==2 and len(set(aligned_votes))==1:
                        output.append(dict(kind=kind,value=aligned_votes[0],score=min(aligned_scores),box=(int(x),int(y+y_offset),int(w),int(h)),source='aligned-ocr'))
                        continue
                margin=max(3,round(7*scale))
                x0,y0=max(0,x-margin),max(0,y-margin)
                patch=255-mask[y0:y+h+margin,x0:x+w+margin]
                votes=[];scores=[]
                variants=[(patch,max(4,round(h*fraction)),255) for fraction in (.10,.15)]
                if kind=='damage_taken':
                    variants=[(patch,round(h*.35),255),(255-patch,round(h*.35),0)]
                for patch,pad,bg in variants:
                    im=cv2.copyMakeBorder(patch,pad,pad,pad,pad,cv2.BORDER_CONSTANT,value=bg)
                    raw,score=self.ocr.recognize(im)
                    raw=raw.replace(' ','').replace(',','')
                    threshold=.65 if kind=='damage_taken' else .75
                    shaped=weak is not None and raw.isdigit() and int(raw)==weak[0] and score>=.65
                    if (score>=threshold or shaped) and re.fullmatch(r'\d{1,7}',raw) and int(raw)>0 and (glyph_count is None or len(raw)==glyph_count):
                        votes.append(int(raw));scores.append(score)
                    if not votes and weak is None:break
                if votes and len(set(votes))==1 and (len(votes)==2 or (weak is not None and votes[0]==weak[0] and max(scores)>=.72)):
                    output.append(dict(kind=kind,value=votes[0],score=min(scores),box=(int(x),int(y+y_offset),int(w),int(h)),source='glyphs+ocr' if weak else 'ocr'))
        return output
