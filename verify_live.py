"""Collect real visual evidence and independently check experience arithmetic."""
import argparse
import json
import time
from pathlib import Path
import cv2
from game_capture import GameCapture
from experience_ocr import ExperienceOCR
from tracker import Tracker

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    parser.add_argument('--count',type=int,default=20)
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True,exist_ok=True)
    capture,ocr = GameCapture(),ExperienceOCR()
    tracker = Tracker()
    samples = []
    for index in range(args.count):
        started = time.monotonic()
        try:
            frame,meta = capture.capture()
            reading,details = ocr.read(frame)
            filename = f'{index:03}.png'
            cv2.imwrite(str(out/filename),frame)
            if reading is None:
                samples.append(dict(index=index,error='OCR rejected',file=filename,ocr=details,capture=meta))
            else:
                status,delta = tracker.feed(reading)
                tracker.start()
                samples.append(dict(index=index,current=reading.current,percent=reading.percent,status=status,delta=delta,total=tracker.total,elapsed=tracker.elapsed,minute=tracker.rates()[0],hour=tracker.rates()[1],file=filename,capture=meta,ocr=details))
        except Exception as error:
            samples.append(dict(index=index,error=str(error)))
        print(json.dumps(samples[-1],ensure_ascii=True),flush=True)
        time.sleep(max(0,1.2-(time.monotonic()-started)))
    valid = [s for s in samples if 'current' in s]
    result = dict(samples=samples,accepted=len(valid),attempts=len(samples))
    if valid:
        expected = valid[-1]['current']-valid[0]['current']
        result.update(first=valid[0]['current'],last=valid[-1]['current'],expected_gain=expected,tracked_gain=tracker.total,arithmetic_correct=expected==tracker.total and sum(s['delta'] for s in valid)==tracker.total)
    (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('SUMMARY '+json.dumps({k:v for k,v in result.items() if k!='samples'}),flush=True)

if __name__=='__main__':
    main()
