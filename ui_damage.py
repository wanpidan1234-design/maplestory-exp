"""Classic MapleStory damage lettering from the recorded game's digit silhouettes."""
from functools import lru_cache
import json
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw,ImageFilter,ImageFont

PALETTES={
    'damage_dealt':('#c93912','#ff9317','#ffe831'),
    'damage_taken':('#6827ec','#b318ee','#f414d9'),
}


@lru_cache(maxsize=1)
def glyphs():
    data=json.loads((Path(__file__).parent/'assets/damage-digits.json').read_text(encoding='utf-8'))
    result={}
    for digit,variants in data['templates'].items():
        # The first manually transcribed example is a real game glyph. Recognition
        # templates remain unchanged; this UI copy is scaled to a common cap height.
        mask=Image.fromarray(np.array(variants[0],dtype='uint8')*255)
        result[digit]=mask.resize((round(mask.width*64/mask.height),64),Image.Resampling.LANCZOS)
    for char in ('万','亿'):
        mask=Image.new('L',(100,100));draw=ImageDraw.Draw(mask)
        draw.text((8,0),char,font=ImageFont.truetype('C:/Windows/Fonts/msyhbd.ttc',64),fill=255)
        box=mask.getbbox();mask=mask.crop(box)
        result[char]=mask.resize((round(mask.width*58/mask.height),58),Image.Resampling.LANCZOS)
    minus=Image.new('L',(34,64));ImageDraw.Draw(minus).polygon(((4,27),(33,27),(30,37),(1,37)),fill=255);result['−']=minus
    for char in ('.',','):
        mask=Image.new('L',(13,64));draw=ImageDraw.Draw(mask);draw.ellipse((3,52,11,60),fill=255)
        if char==',':draw.polygon(((5,58),(10,58),(5,64),(1,64)),fill=255)
        result[char]=mask
    return result


@lru_cache(maxsize=192)
def damage_image(text,kind,height,max_width):
    """Bounded, transparent, fully outlined image; numeric text retains 万/亿 units."""
    font=glyphs();parts=[font[c] for c in text]
    gap=2;padding=12
    width=sum(p.width for p in parts)+gap*(len(parts)-1)+padding*2
    mask=Image.new('L',(width,90));x=padding
    for char,part in zip(text,parts):
        mask.paste(part,(x,padding+(64-part.height)));x+=part.width+gap
    outer=mask.filter(ImageFilter.MaxFilter(19));inner=mask.filter(ImageFilter.MaxFilter(11))
    image=Image.new('RGBA',mask.size,(0,0,0,0))
    image.paste('#261a29',(0,0),outer)
    image.paste('#fffdee' if kind=='damage_dealt' else '#fffaff',(0,0),inner)
    stops=[np.array(tuple(bytes.fromhex(color[1:])),float) for color in PALETTES[kind]]
    gradient=np.zeros((90,width,3),dtype=np.uint8)
    for y in range(90):
        p=max(0,min(1,(y-padding)/63));a,b=(stops[0],stops[1]) if p<.55 else (stops[1],stops[2])
        q=p/.55 if p<.55 else (p-.55)/.45
        gradient[y,:]=np.round(a*(1-q)+b*q).astype('uint8')
    image.paste(Image.fromarray(gradient),(0,0),mask)
    factor=min(max(1,height)/image.height,max(1,max_width)/image.width)
    return image.resize((max(1,round(image.width*factor)),max(1,round(image.height*factor))),Image.Resampling.LANCZOS)
