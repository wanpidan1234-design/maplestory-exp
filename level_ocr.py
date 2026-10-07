"""Read the orange level badge beside the visual LV. anchor, from pixels only."""
from pathlib import Path
import re
import cv2
import numpy as np


def character_confidence(text,score):
    """Undo RapidOCR 1.2.3's mean(conf_list + [1e-50]) for strict digit tokens.

    This is local to level OCR: existing EXP/combat thresholds keep their meaning.
    Two perfect digits otherwise cannot score above 2/3; one above 1/2.
    """
    return min(1.,float(score)*(len(text)+1)/len(text)) if text else 0.


def crop_level(band,scale):
    original=cv2.imread(str(Path(__file__).parent/'assets'/'level-anchor.png'),0)
    template=cv2.resize(original,None,fx=scale,fy=scale,interpolation=cv2.INTER_NEAREST)
    gray=cv2.cvtColor(band[:,:min(band.shape[1],round(320*scale))],cv2.COLOR_BGR2GRAY)
    gray=cv2.threshold(gray,180,255,cv2.THRESH_BINARY)[1]
    if gray.shape[0]<template.shape[0] or gray.shape[1]<template.shape[1]:return None,{}
    _,score,_,(x,y)=cv2.minMaxLoc(cv2.matchTemplate(gray,template,cv2.TM_CCOEFF_NORMED))
    if score<.98:
        for dy in range(3):
            for dx in range(3):
                shifted=np.roll(np.roll(original,dy,axis=0),dx,axis=1)
                variant=cv2.resize(shifted,None,fx=scale,fy=scale,interpolation=cv2.INTER_NEAREST)
                _,candidate,_,pos=cv2.minMaxLoc(cv2.matchTemplate(gray,variant,cv2.TM_CCOEFF_NORMED))
                if candidate>score:score=candidate;x=round(pos[0]+dx*scale);y=round(pos[1]+dy*scale)
    if score<.83:return None,{'anchor_score':round(score,3)}
    left=x+template.shape[1]+round(8*scale)
    right=min(band.shape[1],left+round(84*scale))
    return band[y:y+template.shape[0],left:right].copy(),dict(left=left,top=y,width=right-left,height=template.shape[0],anchor_score=round(score,3))


def read_level(image,ocr):
    if image is None or not image.size:return None,[]
    level,details=_read_badge(image,ocr)
    if level is not None or image.shape[:2]==(39,84):return level,details
    level,extra=_read_badge(cv2.resize(image,(84,39),interpolation=cv2.INTER_LINEAR),ocr)
    return level,details+extra


def _read_badge(image,ocr):
    hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
    orange=cv2.inRange(hsv,np.array([4,120,140]),np.array([30,255,255]))
    points=cv2.findNonZero(orange)
    if points is None:return None,[]
    x,y,w,h=cv2.boundingRect(points)
    # Restrict to the orange badge; adjacent name/class text cannot vote as a level.
    badge=hsv[y:y+h,x:x+w]
    white=cv2.inRange(badge,np.array([0,0,195]),np.array([179,85,255]))
    points=cv2.findNonZero(white)
    if points is None:return None,[]
    x,y,w,h=cv2.boundingRect(points);glyphs=white[y:y+h,x:x+w]
    votes=[];details=[]
    for factor,invert in ((3,True),(4,False),(5,True)):
        padded=cv2.copyMakeBorder(glyphs,4,4,4,4,cv2.BORDER_CONSTANT,value=0)
        variant=cv2.resize(255-padded if invert else padded,None,fx=factor,fy=factor,interpolation=cv2.INTER_NEAREST)
        raw,legacy_score=ocr.recognize(variant)
        score=character_confidence(raw,legacy_score)
        details.append((raw,round(score,3)))
        # Require three agreeing transforms plus the temporal/EXP guards. The
        # threshold is a character mean, not the length-biased legacy score.
        if score>=.82 and re.fullmatch(r'\d{1,3}',raw.strip()) and 1<=int(raw)<=300:votes.append(int(raw))
    return (votes[0] if len(votes)==3 and len(set(votes))==1 else None),details
