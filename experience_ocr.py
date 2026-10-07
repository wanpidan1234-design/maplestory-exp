"""Recognize absolute experience separately from the noisy percent glyph."""
from functools import lru_cache
import math
import re
import time
import cv2
import numpy as np
import onnxruntime as ort
from rapidocr_onnxruntime import RapidOCR
from tracker import Reading,parse_exp


_NUMBER = r'(?:[0-9]{1,14}|[0-9]{1,3}(?:,[0-9]{3}){1,4})'
_PERCENT = r'[0-9]{1,3}(?:\.[0-9]{1,2})?\s*%'
_PERCENT_FIELD = rf'(?:{_PERCENT}|\[\s*{_PERCENT}\s*\])'
_FULL_READING = re.compile(rf'\s*(?:{_NUMBER}(?:\s*/\s*{_NUMBER})?(?:\s*{_PERCENT_FIELD})?|{_PERCENT_FIELD})\s*')


def strict_exp_reading(raw):
    """Parse an entire field, never salvage digits from adjacent labels."""
    raw = raw.replace('，', ',').replace('％', '%').replace('／', '/')
    if not _FULL_READING.fullmatch(raw):
        return None
    reading = parse_exp(raw)
    if reading is None:
        return None
    if any(value is not None and len(str(value)) > 14 for value in (reading.current, reading.cap)):
        return None
    return reading


def short_character_confidence(raw, legacy_score):
    """Undo the pinned decoder's extra zero only for an original digit token.

    Spaces/punctuation are decoded characters too. They must not be removed to
    qualify a token for this local one-to-three-digit correction.
    """
    if not re.fullmatch(r'[0-9]{1,3}', raw):
        return None
    if not math.isfinite(legacy_score) or legacy_score < 0:
        return 0.0
    return min(1.0, legacy_score * (len(raw) + 1) / len(raw))


def normalize_absolute_token(raw):
    """Normalize only an already isolated, geometry-checked integer field.

    Comma grouping is checked before removing anything inside the token.
    Never use this on percentages, compound fields or to adjust confidence.
    """
    token = raw.strip(' ')
    reason = None
    if ',' in token:
        if not re.fullmatch(r'[0-9]{1,3}(?:,[0-9]{3})+', token):
            reason = 'invalid_comma_grouping'
        digits = token.replace(',', '')
    else:
        if not re.fullmatch(r'[0-9]+(?: +[0-9]+)*', token):
            reason = 'invalid_absolute_characters'
        digits = token.replace(' ', '')
    if reason is None and not 1 <= len(digits) <= 14:
        reason = 'invalid_absolute_length'
    removed = {name: raw.count(char) for name, char in
               (('ascii_space', ' '), ('grouping_comma', ',')) if char in raw}
    return dict(normalized=None if reason else digits,
                removed_characters={} if reason else removed,
                normalization_reason=reason or ('cleaned' if removed else 'unchanged'))


class ExperienceOCR:
    def __init__(self):
        self.engine = RapidOCR(use_text_det=False,use_angle_cls=False)
        # RapidOCR 1.2.3 creates an automatic all-core ORT pool. Two independent
        # readers would otherwise spin on most CPU cores between tiny crops.
        # Reuse the pinned package's model and options with a bounded CPU pool.
        wrapper=self.engine.text_recognizer.session
        original=wrapper.session
        options=original.get_session_options()
        options.intra_op_num_threads=1
        options.inter_op_num_threads=1
        options.enable_cpu_mem_arena=True
        options.add_session_config_entry('session.intra_op.allow_spinning','0')
        options.add_session_config_entry('session.inter_op.allow_spinning','0')
        wrapper.session=ort.InferenceSession(original._model_path,sess_options=options,providers=['CPUExecutionProvider'])
        # Per-engine, bounded cache. Only identical pixels reuse a prediction;
        # confidence checks and per-frame accounting still run normally.
        self._recognize_pixels=lru_cache(maxsize=96)(self._recognize_pixels)
        self._recognize_exp_pixels=lru_cache(maxsize=96)(self._recognize_exp_pixels)
        self.last_diagnostics = {}

    def recognize(self,image):
        if image.dtype==np.uint8 and image.nbytes<=131072:
            return self._recognize_pixels(image.shape,image.tobytes())
        return self._recognize_uncached(image)

    def _recognize_pixels(self,shape,pixels):
        return self._recognize_uncached(np.frombuffer(pixels,dtype=np.uint8).reshape(shape))

    def _recognize_uncached(self,image):
        result,_ = self.engine(image)
        return (' '.join(r[1] for r in result),min(float(r[2]) for r in result)) if result else ('',0)

    def recognize_exp(self, image):
        """Raw EXP candidates without changing RapidOCR's shared .5 filter.

        Generic recognize() remains the legacy channel used by level/combat.
        Calling RapidOCR with text_score=... would mutate that engine setting;
        the lower recognizer instead reuses its existing bounded ORT session.
        """
        if image.dtype == np.uint8 and image.nbytes <= 131072:
            return self._recognize_exp_pixels(image.shape, image.tobytes())
        return self._recognize_exp_uncached(image)

    def _recognize_exp_pixels(self, shape, pixels):
        return self._recognize_exp_uncached(np.frombuffer(pixels, dtype=np.uint8).reshape(shape))

    def _recognize_exp_uncached(self, image):
        result, _ = self.engine.text_recognizer([self.engine.load_img(image)])
        return (str(result[0][0]), float(result[0][1])) if result else ('', 0)

    def _candidate(self, field, stage, raw, score, qualified, reason, *, normalization=None):
        score = float(score) if math.isfinite(score) else 0.0
        candidates = self.last_diagnostics.setdefault('candidates', [])
        if len(candidates) < 12:
            character = short_character_confidence(raw, score) if field in ('current', 'full') else None
            candidates.append(dict(field=field, stage=stage, raw=raw[:80],
                                   legacy_score=round(score, 6),
                                   character_score=None if character is None else round(character, 6),
                                   qualified=qualified, reason=reason))
            if normalization is not None:
                candidates[-1].update(normalization)

    @staticmethod
    def _foreground_geometry(image, check_spacing=True, right_delimiter=False):
        """Require complete neutral foreground, with no separated number runs."""
        if image is None or not image.size:
            return 'empty_crop'
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array([0, 0, 170]), np.array([179, 110, 255]))
        points = cv2.findNonZero(mask)
        if points is None:
            return 'no_digit_foreground'
        x, y, width, height = cv2.boundingRect(points)
        rows, columns = image.shape[:2]
        if x == 0 or x + width == columns and not right_delimiter or y == 0 or y + height == rows:
            return 'foreground_touches_crop_edge'
        if height < max(3, rows * .25):
            return 'insufficient_digit_foreground'
        if check_spacing:
            occupied = np.flatnonzero((mask > 0).any(axis=0))
            if occupied.size > 1 and np.max(np.diff(occupied) - 1) > max(3, height * .75):
                return 'separated_numeric_groups'
        return None

    @staticmethod
    def _brackets(frame):
        """Find aligned [ and ] strokes, including a left bracket near x=0."""
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array([25, 85, 120]), np.array([90, 255, 255]))
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        left, right = [], []
        frame_height = frame.shape[0]
        for label in range(1, count):
            x, y, width, height, _ = map(int, stats[label])
            if not (2 <= width <= max(3, height * .45) and height >= max(5, frame_height * .4)):
                continue
            component = labels[y:y + height, x:x + width] == label
            profile = component.sum(axis=0)
            edge = max(1, round(height * .25))
            dense = np.flatnonzero(profile >= height * .70)
            if not dense.size:
                continue
            if profile[0] >= height * .70 and profile[-1] <= height * .50 and component[:edge, -1].any() and component[-edge:, -1].any():
                left.append((x, y, width, height, x + int(dense[-1]) + 1))
            if profile[-1] >= height * .70 and profile[0] <= height * .50 and component[:edge, 0].any() and component[-edge:, 0].any():
                right.append((x, y, width, height, x + int(dense[0])))
        pairs = [(a, b) for a in left for b in right
                 if frame_height * .8 < b[0] - a[0] < frame_height * 8
                 and abs(a[1] - b[1]) <= max(2, frame_height * .2)
                 and abs(a[3] - b[3]) <= max(2, frame_height * .25)]
        if len(pairs) == 1 and len(left) == 1 and len(right) == 1:
            return pairs[0], None
        if len(left) == 1 and not right:
            # A cropped or absent percentage must not discard a complete long
            # EXP token to the left of a structurally valid opening bracket.
            return (left[0], None), None
        if left or right:
            return None, 'ambiguous_or_incomplete_brackets'
        return None, None

    def variants(self,image):
        gray = cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)
        binary = cv2.threshold(gray,170,255,cv2.THRESH_BINARY)[1]
        return [image,cv2.resize(255-binary,None,fx=3,fy=3,interpolation=cv2.INTER_NEAREST),cv2.resize(binary,None,fx=3,fy=3,interpolation=cv2.INTER_NEAREST)]

    @staticmethod
    def _separate_percent_point(image):
        """Separate an existing attached decimal tail, without drawing a dot.

        At native HUD scale, the point after 0 touches its lower right stroke.
        Only the observed low protrusion is moved: no decimal location is
        inferred from an OCR token or from the number of decoded digits.
        """
        hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
        mask=cv2.inRange(hsv,np.array([0,0,170]),np.array([179,110,255]))
        points=cv2.findNonZero(mask)
        if points is None:return None
        x,y,w,h=cv2.boundingRect(points)
        if x==0 or x+w==image.shape[1] or y==0 or y+h==image.shape[0] or h<5:return None
        glyphs=mask[y:y+h,x:x+w]
        occupied=np.flatnonzero((glyphs>0).any(axis=0))
        groups=np.split(occupied,np.where(np.diff(occupied)>1)[0]+1)
        # One integer glyph with the attached tail, then two full digit glyphs.
        # An absent fractional digit or a detached mark is not repaired here.
        if len(groups)!=3:return None
        for group in groups:
            rows=np.flatnonzero((glyphs[:,group]>0).any(axis=1))
            if (rows[-1]-rows[0]+1<h*.85 or rows[0]>h*.15
                    or len(group)<max(2,h*.15) or len(group)>h*1.2):return None
            # A narrow 1 retains a full-height stem. A clipped 2/3/etc. can
            # still fool OCR at high confidence, but lacks that complete stem.
            if len(group)<h*.55 and not (len(group)<=h*.4
                    and (glyphs[:,group]>0).sum(axis=0).max()>=h*.85):return None
        first=groups[0]
        upper=np.flatnonzero((glyphs[:round(h*.7),first]>0).any(axis=0))
        if not upper.size:return None
        split=int(first[0]+upper[-1]+1)
        width=int(first[-1]+1-split)
        if not 1<=width<=max(2,round(h*.25)):return None
        tail=glyphs[:,split:first[-1]+1]
        rows=np.flatnonzero((tail>0).any(axis=1))
        if (not rows.size or rows[0]<h*.70 or rows[-1]<h-2
                or not 2<=len(rows)<=max(3,round(h*.3))):return None
        if (groups[1][0]-first[-1]-1<h*.3
                or groups[2][0]-groups[1][-1]-1>h*.7):return None
        gap=np.zeros((h,max(2,round(h*.3))),dtype=np.uint8)
        return np.concatenate((glyphs[:,:split],gap,glyphs[:,split:]),axis=1)

    def read_percent(self,image,details,point_image=None):
        """Pad the tiny numeric glyphs; do not infer missing decimal digits."""
        all_votes=[]
        conflict=False
        def consensus(variants,stage,explicit_point=False):
            nonlocal conflict
            votes=[]
            for index,variant in enumerate(variants):
                raw,score=self.recognize(variant)
                details.append(('percent: '+raw,round(score,3)))
                pattern=r'\s*(\d)\.\s*(\d{2})\s*' if explicit_point else r'\s*(\d{1,3})[. ,]+(\d{2})\s*'
                match=re.fullmatch(pattern,raw)
                value=None
                if score>=.75 and match:
                    value=float(match[1]+'.'+match[2])
                    if 0<=value<=100:
                        votes.append(value)
                        all_votes.append(value)
                qualified=value is not None and 0<=value<=100 and score>=.75
                self._candidate('percent',f'{stage}:{index}',raw,score,qualified,
                                'qualified' if qualified else 'invalid_format_or_score')
            conflict=len(set(all_votes))>1
            if conflict:
                self.last_diagnostics.setdefault('fields',{})['percent']=dict(value=None,reason='percent_conflict')
                return None
            if len(votes)>=2:
                self.last_diagnostics.setdefault('fields',{})['percent']=dict(value=votes[0],reason=None)
                return votes[0]
            return None
        value=consensus(self.variants(image),'percent_original')
        if value is not None or conflict:
            return value
        separated=self._separate_percent_point(image if point_image is None else point_image)
        if separated is not None:
            padded=[]
            for ratio,invert in ((.23,True),(.31,False),(.46,True)):
                pad=max(2,round(separated.shape[0]*ratio))
                bordered=cv2.copyMakeBorder(255-separated if invert else separated,pad,pad,pad,pad,
                                           cv2.BORDER_CONSTANT,value=255 if invert else 0)
                padded.append(cv2.resize(bordered,None,fx=4,fy=4,interpolation=cv2.INTER_NEAREST))
            value=consensus(padded,'percent_point_separated',explicit_point=True)
            if value is not None or conflict:
                return value
        original_mask=cv2.threshold(cv2.cvtColor(image,cv2.COLOR_BGR2GRAY),170,255,cv2.THRESH_BINARY)[1]
        original_points=cv2.findNonZero(original_mask)
        if original_points is not None:
            x,y,w,h=cv2.boundingRect(original_points)
            pad=max(2,round(image.shape[0]*.16))
            candidates=[]
            for crop,bg in ((image[y:y+h,x:x+w],(55,65,65)),(255-original_mask[y:y+h,x:x+w],255)):
                bordered=cv2.copyMakeBorder(crop,pad,pad,pad,pad,cv2.BORDER_CONSTANT,value=bg)
                candidates.append(cv2.resize(bordered,None,fx=4,fy=4,interpolation=cv2.INTER_NEAREST))
            value=consensus(candidates,'percent_tight')
            if value is not None or conflict:
                return value
        # Extra background dominates this short pixel-font label. Tight bounds
        # plus a proportional quiet border improve OCR without relaxing confidence.
        image=image.copy()
        # Bracket arms can extend past the detected vertical bracket stroke.
        # Their lime pixels otherwise become punctuation beside the first digit.
        saturation=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)[:,:,1]
        image[saturation>80]=(55,65,65)
        gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)
        mask=cv2.threshold(gray,170,255,cv2.THRESH_BINARY)[1]
        points=cv2.findNonZero(mask)
        if points is None:
            return None
        x,y,w,h=cv2.boundingRect(points)
        crop=image[y:y+h,x:x+w]
        binary=255-mask[y:y+h,x:x+w]
        padded=[]
        for ratio in (.16,.24):
            pad=max(2,round(image.shape[0]*ratio))
            color=cv2.copyMakeBorder(crop,pad,pad,pad,pad,cv2.BORDER_CONSTANT,value=(55,65,65))
            mono=cv2.copyMakeBorder(binary,pad,pad,pad,pad,cv2.BORDER_CONSTANT,value=255)
            padded.extend(cv2.resize(v,None,fx=4,fy=4,interpolation=cv2.INTER_NEAREST) for v in (color,mono))
        value=consensus(padded,'percent_clean')
        if value is None and not conflict:
            self.last_diagnostics.setdefault('fields',{})['percent']=dict(value=None,reason='insufficient_percent_votes')
        return value

    def _absolute(self,image,details,allow_short=True):
        votes=[]
        original_tokens=[]
        normalized_tokens=[]
        for index,variant in enumerate(self.variants(image)):
            raw,score=self.recognize_exp(variant)
            original_tokens.append(raw)
            details.append((raw,round(score,3)))
            normalization=normalize_absolute_token(raw)
            digits=normalization['normalized']
            normalized_tokens.append(digits)
            valid=digits is not None
            character=short_character_confidence(raw,score)
            short=valid and len(digits)<=3
            qualified=valid and (character is not None and character>=.90 if short else score>=.75)
            reason=('qualified' if qualified else normalization['normalization_reason'] if not valid
                    else 'short_requires_raw_digits' if short and character is None else 'low_confidence')
            self._candidate('current',f'absolute:{index}',raw,score,qualified,
                            reason,normalization=normalization)
            if qualified:votes.append((int(digits),short))
        if len({value for value,_ in votes})>1:
            return None,'absolute_conflict'
        long_votes=sum(not short for _,short in votes)
        if votes and (long_votes>=2 or allow_short and len(votes)==3 and all(short for _,short in votes)):
            return votes[0][0],None
        if not allow_short and len(votes)==3 and all(short for _,short in votes):
            return None,'short_requires_complete_brackets'
        # The HUD's fixed-height background overwhelms short labels, including
        # four-digit values after a level-up. Retry only unanimous strict tokens,
        # with the complete foreground geometry already checked by read(). Do
        # not lower the legacy thresholds or collect votes across frames.
        if (len(set(original_tokens)) == 1
                and re.fullmatch(r'[0-9]{1,14}',original_tokens[0])):
            return self._tight_absolute(image,details,original_tokens[0],allow_short)
        # Cleaning permits a fresh pixel retry, never promotes weak votes.
        # Every original candidate must normalize successfully and agree,
        # including low-score candidates. Short-token rules remain separate.
        if (long_votes and None not in normalized_tokens
                and len(set(normalized_tokens)) == 1
                and len(normalized_tokens[0]) >= 4):
            return self._tight_absolute(image,details,normalized_tokens[0],allow_short)
        return None,'insufficient_absolute_votes'

    def _tight_absolute(self,image,details,token,allow_short):
        """One bounded, padded retry; preserve all strong conflicting evidence."""
        short=len(token)<=3
        if short and not allow_short:
            return None,'short_requires_complete_brackets'
        hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
        mask=cv2.inRange(hsv,np.array([0,0,170]),np.array([179,110,255]))
        points=cv2.findNonZero(mask)
        if points is None:
            return None,'no_digit_foreground'
        x,y,w,h=cv2.boundingRect(points)
        pad=max(2,round(h*.4))
        glyphs=cv2.copyMakeBorder(mask[y:y+h,x:x+w],pad,pad,pad,pad,
                                 cv2.BORDER_CONSTANT,value=0)
        votes=[]
        consistent=True
        conflict=False
        for index,(factor,invert) in enumerate(((3,True),(4,False),(5,True))):
            variant=cv2.resize(255-glyphs if invert else glyphs,None,fx=factor,fy=factor,
                               interpolation=cv2.INTER_NEAREST)
            raw,score=self.recognize_exp(variant)
            details.append((raw,round(score,3)))
            strict=re.fullmatch(r'[0-9]{1,14}',raw) is not None
            candidate_short=strict and len(raw)<=3
            character=short_character_confidence(raw,score)
            qualified=strict and (character is not None and character>=.90 if candidate_short else score>=.75)
            normalization=normalize_absolute_token(raw)
            self._candidate('current',f'absolute_tight:{index}',raw,score,qualified,
                            'qualified' if qualified else 'low_confidence' if strict
                            else 'retry_requires_raw_digits',normalization=normalization)
            consistent=consistent and raw==token
            if qualified:
                conflict=conflict or int(raw)!=int(token)
                votes.append(int(raw))
        if conflict:
            return None,'absolute_conflict'
        if consistent and len(votes)>=(3 if short else 2):
            return int(token),None
        return None,'insufficient_absolute_votes'

    def _full_field(self,frame,details):
        parsed=[]
        for index,variant in enumerate(self.variants(frame)):
            raw,score=self.recognize_exp(variant)
            details.append((raw,round(score,3)))
            reading=strict_exp_reading(raw)
            character=short_character_confidence(raw,score)
            token=raw.strip().replace(',','')
            short=(reading is not None and reading.current is not None
                   and reading.cap is None and reading.percent is None
                   and token.isdigit() and len(token)<=3)
            qualified=reading is not None and (character is not None and character>=.90 if short else score>=.80)
            if qualified and re.fullmatch(_NUMBER,raw.strip()):
                qualified=self._foreground_geometry(frame) is None
            self._candidate('full',f'full:{index}',raw,score,qualified,
                            'qualified' if qualified else 'invalid_format_score_or_geometry')
            if qualified:parsed.append((reading,short))
        absolute=[(reading,short) for reading,short in parsed if reading.current is not None]
        if len({reading.current for reading,_ in absolute})>1:
            return None,'absolute_conflict'
        if absolute:
            long_votes=sum(not short for _,short in absolute)
            if not (long_votes>=2 or len(absolute)==3 and all(short for _,short in absolute)):
                return None,'insufficient_absolute_votes'
            values=[reading for reading,_ in absolute]
            def unanimous(field):
                # A confident percent-only candidate can contradict the
                # percentage beside two agreeing absolute EXP candidates.
                # Field evidence spans every qualified parse, not only the
                # candidates that also happened to recognize absolute EXP.
                options={getattr(reading,field) for reading,_ in parsed}
                value=next(iter(options)) if len(options)==1 else None
                self.last_diagnostics['fields'][field]=dict(value=value,reason=None if value is not None else 'missing_or_conflicting')
                return value
            return Reading(values[0].current,unanimous('cap'),unanimous('percent')),None
        percentages=[reading.percent for reading,_ in parsed]
        if len(percentages)>=2 and len(set(percentages))==1:
            return Reading(None,None,percentages[0]),None
        return None,'insufficient_full_field_votes'

    def read(self,frame):
        started=time.perf_counter()
        details=[]
        self.last_diagnostics=dict(path=None,reason=None,fields={},candidates=[],evidence='same_frame_variants')
        try:
            if frame is None or not frame.size or frame.ndim!=3 or frame.shape[2]!=3:
                self.last_diagnostics.update(path='rejected',reason='invalid_frame')
                return None,details
            brackets,reason=self._brackets(frame)
            if reason:
                self.last_diagnostics.update(path='rejected',reason=reason)
                return None,details
            if brackets:
                self.last_diagnostics['path']='bracket'
                left,right=brackets
                absolute=frame[:,:left[0]]
                # At 0.5x scale the final glyph can meet the validated green
                # delimiter without a blank pixel. This is an internal split,
                # not the outer image edge of a potentially truncated glyph.
                reason=self._foreground_geometry(absolute,right_delimiter=True)
                if reason:
                    self.last_diagnostics['reason']=reason
                    return None,details
                current,reason=self._absolute(absolute,details,allow_short=right is not None)
                self.last_diagnostics['fields']['current']=dict(value=current,reason=reason)
                if current is None:
                    self.last_diagnostics['reason']=reason
                    return None,details
                percent=None
                if right is not None:
                    a,b=left[4],right[4]
                    end=b-round(frame.shape[0]*.72)
                    if end>a:
                        # The fixed legacy split can end exactly after a final
                        # glyph. Supply one actual neighboring pixel column to
                        # the new geometry check; never manufacture its margin.
                        percent=self.read_percent(frame[:,a:end],details,
                                                  point_image=frame[:,a:min(end+1,b)])
                else:
                    self.last_diagnostics['fields']['percent']=dict(value=None,reason='missing_right_bracket')
                reading=Reading(current,None,percent)
            else:
                self.last_diagnostics['path']='strict_full'
                reason=self._foreground_geometry(frame,check_spacing=False)
                if reason:
                    self.last_diagnostics['reason']=reason
                    return None,details
                reading,reason=self._full_field(frame,details)
                self.last_diagnostics['reason']=reason
            if reading is not None:
                for field in ('current','cap','percent'):
                    self.last_diagnostics['fields'].setdefault(field,dict(value=getattr(reading,field),reason=None))
            return reading,details
        finally:
            self.last_diagnostics['elapsed_ms']=round((time.perf_counter()-started)*1000,3)
            self.last_diagnostics['candidate_count']=len(self.last_diagnostics['candidates'])
