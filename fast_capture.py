"""Fresh visible desktop pixels via DXGI, with a multi-monitor MSS fallback."""
import re
import time
import numpy as np
import mss


def relative_region(client,bounds):
    left,top,right,bottom=bounds
    x,y,w,h=(client[k] for k in ('left','top','width','height'))
    if left<=x and top<=y and x+w<=right and y+h<=bottom:
        return x-left,y-top,x+w-left,y+h-top
    return None


class FastCapture:
    def __init__(self):
        self.cameras=[];self.fallback=None;self.backend='MSS';self.retry_after=0
        try:
            import dxcam
            for device,output in re.findall(r'Device\[(\d+)\] Output\[(\d+)\]',dxcam.output_info()):
                camera=dxcam.create(device_idx=int(device),output_idx=int(output),output_color='BGRA',max_buffer_len=2)
                self.cameras.append(camera)
        except Exception:
            # Capture must remain available on unsupported/remote adapters.
            self.close()

    def grab(self,client):
        if time.monotonic()>=self.retry_after:
            for camera in self.cameras:
                try:
                    # DXcam 0.3.0's output descriptor supplies physical desktop
                    # coordinates; primary-monitor assumptions break at x < 0.
                    camera._output.update_desc()
                    rect=camera._output.desc.DesktopCoordinates
                    region=relative_region(client,(rect.left,rect.top,rect.right,rect.bottom))
                    if region is None:continue
                    frame=camera.grab(region=region,new_frame_only=True,copy=True)
                    if frame is not None and frame.shape[:2]==(client['height'],client['width']):
                        self.backend='DXGI';return frame
                    # Never label DXGI's cached pixels as a fresh capture.
                    break
                except Exception:
                    self.retry_after=time.monotonic()+2
                    break
        if self.fallback is None:self.fallback=mss.MSS()
        self.backend='MSS'
        return np.asarray(self.fallback.grab(client))

    def close(self):
        for camera in self.cameras:
            try:camera.release()
            except Exception:pass
        self.cameras=[]
        if self.fallback is not None:self.fallback.close();self.fallback=None
