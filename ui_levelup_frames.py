"""Bounded Pillow-only preparation for the approved native D animation."""
from collections import deque
import logging
import threading
from PIL import Image


class PreparedFrames:
    # At 570 x 1831 the RGB queue is at most 18 MiB, not a 105-frame movie.
    MAX_READY = 6

    def __init__(self, assets, snapshot, mode, label, reduced, fps, duration, start_index=0,
                 panel_fit='legacy'):
        if panel_fit not in ('legacy','contain','cover'):raise ValueError('Unsupported panel fit')
        self.assets=assets;self.snapshot=snapshot;self.size=snapshot.size
        self.mode=mode;self.label=label;self.reduced=reduced;self.fps=fps;self.duration=duration
        self.panel_fit=panel_fit
        self.ready=deque();self.lock=threading.Lock();self.stop=threading.Event()
        self.requested=start_index;self.error=None;self.done=False
        sizes=[]
        for path in assets.glob(f'{mode}-000@*.png'):
            try:sizes.append(tuple(map(int,path.stem.rsplit('@',1)[1].split('x'))))
            except ValueError:continue
        self.native_size=min(sizes,key=lambda size:abs(size[0]-self.size[0])*3+abs(size[1]-self.size[1])) if sizes else None
        self.thread=threading.Thread(target=self._run,name=f'level-frames-{mode}',daemon=True)
        self.thread.start()

    def _load(self,name,fallback=None):
        try:
            with Image.open(self.assets/name) as source:return source.convert('RGBA')
        except (OSError,ValueError) as error:
            self.error=f'{name}: {type(error).__name__}: {error}'
            if fallback:
                try:
                    with Image.open(self.assets/fallback) as source:return source.convert('RGBA')
                except (OSError,ValueError):pass
            return None

    def compose(self,index,text,heading):
        suffix='reduced' if index=='reduced' else f'{index:03d}'
        original=self.assets/f'{self.mode}-{suffix}.png'
        path=self.assets/f'{self.mode}-{suffix}@{self.size[0]}x{self.size[1]}.png'
        if not path.is_file() and self.native_size:
            path=self.assets/f'{self.mode}-{suffix}@{self.native_size[0]}x{self.native_size[1]}.png'
        if not path.is_file():path=original
        fitted=self.mode=='panel' and self.panel_fit!='legacy'
        # Existing DPI assets encode the old panel's stretched proportions.
        # Use the approved original for a uniform transform on the new viewport.
        if fitted and original.is_file():path=original
        with Image.open(path) as source:
            frame=source.convert('RGBA')
            if fitted:
                sx,sy,ox,oy=self.transform()
                size=(max(1,round(380*sx)),max(1,round(1160*sy)))
            else:size=self.size
            if fitted and self.panel_fit=='cover':
                # Resize only the visible source area, avoiding a tall off-screen
                # intermediate at 150% DPI. The crop uses lettering's transform.
                source_box=(max(0,-ox*frame.width/(380*sx)),
                            max(0,-oy*frame.height/(1160*sy)),
                            min(frame.width,(self.size[0]-ox)*frame.width/(380*sx)),
                            min(frame.height,(self.size[1]-oy)*frame.height/(1160*sy)))
                frame=frame.resize(self.size,Image.Resampling.LANCZOS,box=source_box)
                ox=oy=0
            elif frame.size!=size:frame=frame.resize(size,Image.Resampling.LANCZOS)
        if fitted:
            result=self.snapshot.copy()
            result.alpha_composite(frame,(round(ox),round(oy)))
        else:result=Image.alpha_composite(self.snapshot,frame)
        self.lettering(result,text,heading,1.5 if self.reduced else index/self.fps)
        return result.convert('RGB')

    def transform(self):
        """Artwork and lettering share the same logical viewport transform."""
        sx=self.size[0]/(380 if self.mode=='panel' else 138)
        sy=self.size[1]/(1160 if self.mode=='panel' else 138)
        if self.mode=='panel' and self.panel_fit!='legacy':
            sx=sy=(min if self.panel_fit=='contain' else max)(sx,sy)
            return sx,sy,(self.size[0]-380*sx)/2,(self.size[1]-1160*sy)/2
        return sx,sy,0,0

    def lettering(self,result,text,heading,t):
        a=min(1,t/.3)*min(1,(self.duration-t)/.7)
        if a<=0:return
        sx,sy,ox,oy=self.transform()
        def paste(image,x,y,k=1,opacity=1):
            if image is None:return
            size=(max(1,round(image.width/2*sx*k)),max(1,round(image.height/2*sy*k)))
            layer=image.resize(size,Image.Resampling.LANCZOS)
            if opacity<1:layer.putalpha(layer.getchannel('A').point(lambda v:round(v*opacity)))
            result.alpha_composite(layer,(round(ox+x*sx),round(oy+y*sy)))
        if self.mode=='orb':paste(text,0,45);return
        appear=1-(1-max(0,min(1,(t-.58)/.42)))**3
        if not appear:return
        k=.84+.16*appear;cy=560+(1-appear)*34
        paste(heading,0,cy-110,opacity=a*appear)
        paste(text,190*(1-k),cy-140*k,k,opacity=a*appear)

    def _run(self):
        try:
            text=self._load(f'{self.mode}-text-{self.label}.png',fallback=f'{self.mode}-text-unknown.png')
            heading=self._load('heading.png') if self.mode=='panel' else None
            index=self.requested
            while not self.stop.is_set() and index<round(self.duration*self.fps):
                with self.lock:
                    full=len(self.ready)>=self.MAX_READY
                    index=max(index,self.requested)
                if full:self.stop.wait(.005);continue
                key='reduced' if self.reduced else index
                frame=self.compose(key,text,heading)
                with self.lock:
                    if self.stop.is_set():break
                    self.ready.append((key,frame))
                if self.reduced:break
                index+=1
        except Exception as error:
            self.error=f'{type(error).__name__}: {error}'
            logging.getLogger(__name__).exception('Unable to prepare %s level animation',self.mode)
        finally:self.done=True

    def take(self,index):
        with self.lock:
            if isinstance(index,int):self.requested=index
            result=None
            while self.ready and (index=='reduced' or self.ready[0][0]<=index):result=self.ready.popleft()
            return result

    def cancel(self):
        self.stop.set()
        with self.lock:self.ready.clear()
