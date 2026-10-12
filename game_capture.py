"""Per-frame game client geometry and visual EXP anchor; physical pixels only."""
import ctypes
import time
from ctypes import wintypes as wt
from pathlib import Path
import cv2
import mss
import numpy as np

USER = ctypes.windll.user32
USER.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
USER.FindWindowW.restype = wt.HWND
USER.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
USER.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
USER.IsIconic.argtypes = [wt.HWND]
USER.GetDpiForWindow.argtypes = [wt.HWND]
USER.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
USER.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
USER.WindowFromPoint.argtypes = [wt.POINT]
USER.WindowFromPoint.restype = wt.HWND
USER.GetAncestor.argtypes = [wt.HWND, wt.UINT]
USER.GetAncestor.restype = wt.HWND


class CaptureFailure(RuntimeError):
    """A rejected capture with small metadata, never untrusted screen pixels."""
    def __init__(self, message, stage, context=None):
        super().__init__(message)
        self.stage = stage
        self.context = context or {}

def physical_dpi():
    return USER.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))

def overlap(a,b):
    return max(0,min(a['left']+a['width'],b['left']+b['width'])-max(a['left'],b['left']))*max(0,min(a['top']+a['height'],b['top']+b['height'])-max(a['top'],b['top']))

def choose_monitor(rect,monitors):
    return max(monitors,key=lambda m:overlap(rect,m))

def get_monitors():
    physical_dpi()
    with mss.mss() as capture:
        return [dict(m,index=i+1) for i,m in enumerate(capture.monitors[1:])]

def game_geometry(include_monitor=True):
    physical_dpi()
    hwnd = USER.FindWindowW(None,'冒险岛怀旧服')
    if not hwnd:
        raise CaptureFailure('未找到冒险岛怀旧服窗口','window_not_found',dict(source='screen'))
    if USER.IsIconic(hwnd):
        raise CaptureFailure('游戏已最小化，请恢复窗口','minimized',dict(
            hwnd=hwnd,source='screen',observed_at=time.monotonic(),
            failure_kind='visibility_interrupted',visibility_reason='minimized'))
    rect,point = wt.RECT(),wt.POINT()
    if not USER.GetClientRect(hwnd,ctypes.byref(rect)) or not USER.ClientToScreen(hwnd,ctypes.byref(point)):
        raise CaptureFailure('无法读取游戏窗口位置','window_geometry',dict(source='screen'))
    client = dict(left=point.x,top=point.y,width=rect.right,height=rect.bottom)
    if client['width']<100 or client['height']<100:
        raise CaptureFailure('游戏窗口尺寸无效','window_size',dict(source='screen'))
    result=dict(hwnd=hwnd,client=client,dpi=USER.GetDpiForWindow(hwnd))
    if include_monitor:result['monitor']=choose_monitor(client,get_monitors())
    return result

class GameCapture:
    def __init__(self):
        self.anchor = cv2.imread(str(Path(__file__).parent/'assets'/'exp-anchor.png'),0)
        if self.anchor is None:
            raise CaptureFailure('缺少经验栏定位模板','anchor_template_missing',dict(source='screen'))
        self.anchor = cv2.threshold(self.anchor,175,255,cv2.THRESH_BINARY)[1]
        self.cached = None
        self.cached_anchor = None
        self.cached_shape = None
        self.trusted_roi = None

    @staticmethod
    def roi_visible(hwnd, roi):
        """Visibility is about EXP pixels, never simply foreground ownership."""
        y=roi['top']+roi['height']//2
        for fraction in (.1,.5,.9):
            point=wt.POINT(roi['left']+round(roi['width']*fraction),y)
            if USER.GetAncestor(USER.WindowFromPoint(point),2)!=hwnd:
                return False
        return True

    def check_cached_visibility(self, geo):
        saved=getattr(self,'trusted_roi',None)
        c=geo['client']
        if (not saved or saved['hwnd']!=geo['hwnd'] or saved['size']!=(c['width'],c['height'])
                or saved.get('dpi')!=geo.get('dpi')):
            return
        roi=dict(saved['roi'],left=c['left']+saved['roi']['left'],top=c['top']+saved['roi']['top'])
        if not self.roi_visible(geo['hwnd'],roi):
            raise CaptureFailure('经验文字被其他窗口遮挡，等待恢复可见','occluded',dict(
                geo,roi=roi,source='screen',observed_at=time.monotonic(),
                failure_kind='visibility_interrupted',visibility_reason='occluded'))

    def locate(self,band):
        if self.cached_anchor and self.cached_shape==band.shape:
            x,y,w,h,scale=self.cached_anchor
            # Validate pixels at the last client-relative location every frame.
            # On any mismatch, fall back to the complete multi-scale search.
            patch=band[y:y+h,x:x+w]
            gray=cv2.threshold(cv2.cvtColor(patch,cv2.COLOR_BGR2GRAY),175,255,cv2.THRESH_BINARY)[1]
            template=cv2.resize(self.anchor,(w,h),interpolation=cv2.INTER_NEAREST)
            score=float(cv2.matchTemplate(gray,template,cv2.TM_CCOEFF_NORMED)[0,0])
            if score>.98:return score,self.cached_anchor
        gray = cv2.threshold(cv2.cvtColor(band,cv2.COLOR_BGR2GRAY),175,255,cv2.THRESH_BINARY)[1]
        best = (0,None)
        scales = [1,.75,1.25,1.5,.5,.625,2/3,.8,4/3,1.6,2]
        candidates = []
        if self.cached:
            scales = [self.cached]+[s for s in scales if s!=self.cached]
        for scale in scales:
            template = cv2.resize(self.anchor,None,fx=scale,fy=scale,interpolation=cv2.INTER_NEAREST)
            h,w = template.shape
            if h>gray.shape[0] or w>gray.shape[1]:
                continue
            _,score,_,pos = cv2.minMaxLoc(cv2.matchTemplate(gray,template,cv2.TM_CCOEFF_NORMED))
            candidates.append((score,scale))
            if score>best[0]:
                best = score,(pos[0],pos[1],w,h,scale)
            if score>.98:
                break
        # Pixel-font downscaling depends on subpixel sampling phase.
        if best[0]<.80:
            for _,scale in sorted(candidates,reverse=True)[:3]:
                for dy in range(3):
                    for dx in range(3):
                        shifted = np.roll(np.roll(self.anchor,dy,0),dx,1)
                        candidate = cv2.resize(shifted,None,fx=scale,fy=scale,interpolation=cv2.INTER_NEAREST)
                        h,w = candidate.shape
                        _,score2,_,pos2 = cv2.minMaxLoc(cv2.matchTemplate(gray,candidate,cv2.TM_CCOEFF_NORMED))
                        if score2>best[0]:
                            best = score2,(round(pos2[0]+dx*scale),round(pos2[1]+dy*scale),w,h,scale)
        if best[0]<.80:
            raise CaptureFailure('经验栏未找到或被遮挡，请让游戏底部保持可见',
                                 'anchor', {'anchor_score': round(best[0], 4)})
        self.cached = best[1][-1]
        self.cached_anchor = best[1]
        self.cached_shape = band.shape
        return best

    def crop(self,band):
        score,(x,y,w,h,scale) = self.locate(band)
        left = x+w+round(6*scale)
        right = min(band.shape[1],left+round(290*scale))
        return band[y:y+h,left:right].copy(),dict(left=left,top=y,width=right-left,height=h,scale=scale,anchor_score=round(score,4))

    def capture(self,include_level=False):
        self.level_frame=None
        geo = game_geometry()
        c = geo['client']
        # A covered EXP bar often fails anchor matching before the old late
        # visibility check. Only reuse a ROI from a fully accepted capture.
        self.check_cached_visibility(geo)
        top = round(c['height']*.88)
        region = dict(left=c['left'],top=c['top']+top,width=c['width'],height=c['height']-top)
        with mss.mss() as capture:
            shot = capture.grab(region)
            observed_at = time.monotonic()
            band = np.asarray(shot)[:,:,:3].copy()
        try:
            frame,roi = self.crop(band)
        except CaptureFailure as error:
            error.context.update(geo, source='screen', observed_at=observed_at)
            raise
        if include_level:
            try:
                from level_ocr import crop_level
                level_frame,level_roi=crop_level(band,roi['scale'])
                if level_frame is not None:
                    level_roi['left']+=region['left'];level_roi['top']+=region['top']
                    visible=all(USER.GetAncestor(USER.WindowFromPoint(wt.POINT(level_roi['left']+round(level_roi['width']*part),level_roi['top']+level_roi['height']//2)),2)==geo['hwnd'] for part in (.1,.5,.9))
                    if visible:self.level_frame=level_frame
                    level_roi['visible']=visible
                geo['level_roi']=level_roi
            except Exception as error:
                self.level_frame = None
                geo['level_error'] = f'{type(error).__name__}: {error}'[:1000]
        roi['left'] += region['left']
        roi['top'] += region['top']
        geo.update(roi=roi,source='screen',observed_at=observed_at)
        if not self.roi_visible(geo['hwnd'],roi):
            raise CaptureFailure('经验文字被其他窗口遮挡，等待恢复可见','occluded',dict(
                geo,failure_kind='visibility_interrupted',visibility_reason='occluded'))
        if game_geometry(include_monitor=False)['client']!=c:
            raise CaptureFailure('游戏窗口正在移动，等待位置稳定', 'window_moved', geo)
        self.trusted_roi=dict(hwnd=geo['hwnd'],size=(c['width'],c['height']),dpi=geo.get('dpi'),
            roi=dict(roi,left=roi['left']-c['left'],top=roi['top']-c['top']))
        return frame,geo

def capture_game():
    return GameCapture().capture()[0]
