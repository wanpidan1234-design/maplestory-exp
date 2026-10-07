"""Data-derived compact charts with truthful time axes and no interpolated gains."""
import math
import time
from datetime import date, datetime, timedelta
from app_time import BEIJING, format_time
from ui_theme import ACCENT, MUTED, LINE, FONT, WARNING

RATE_VIEWS={'1 min':60,'2 min':120,'5 min':300,'10 min':600,'30 min':1800,'速度趋势':None}
DEFAULT_RATE_VIEW='1 min'
WINDOW_ALGORITHM='observed_window_v1'


def window_label(window_seconds):
    return next(label for label,seconds in RATE_VIEWS.items() if seconds==window_seconds)


def rate_prefix(sample, approximate=False):
    """Display only a missing-gain lower bound; coverage/state describe warmup."""
    if sample.get('has_gaps'):
        return '≥ '
    return ''


def window_sample(row,window_seconds):
    """Read a saved window without fabricating missing values or old coverage."""
    if window_seconds not in RATE_VIEWS.values() or window_seconds is None:
        raise ValueError('Unsupported rate window')
    value=row.get(f'rolling{window_seconds}')
    algorithm=row.get('window_algorithm')
    index=tuple(seconds for seconds in RATE_VIEWS.values() if seconds is not None).index(window_seconds)
    has_gaps=bool((row.get('window_gap_mask') or 0)&(1<<index)) if algorithm==WINDOW_ALGORITHM else False
    quality=dict(has_gaps=has_gaps,quality='lower_bound' if has_gaps else 'complete')
    if algorithm!=WINDOW_ALGORITHM:
        # Unversioned 60-second values came from the previous rolling estimator.
        if window_seconds!=60:
            value=None
        return dict(value=value,observed_seconds=None,state='legacy' if value is not None else 'unavailable',**quality)
    parent_state=row.get('rate_state')
    observed=row.get('rate_observed_seconds')
    coverage=min(window_seconds,max(0,observed)) if observed is not None else None
    if value is None or parent_state not in ('warming','ready') or not coverage:
        state=parent_state if parent_state in ('stale','paused') else 'unavailable'
        return dict(value=None,observed_seconds=coverage,state=state,**quality)
    return dict(value=value,observed_seconds=coverage,state='ready' if coverage>=window_seconds else 'warming',**quality)


def rate_label(row, compact=False):
    """Describe the saved measurement instead of reinterpreting older rates."""
    algorithm=row.get('rate_algorithm') or 'rolling60_v1'
    if algorithm=='time_ewma_v1':
        half_life=row.get('rate_half_life')
        if compact:
            return f'半衰期 {half_life:g}秒' if half_life is not None else '时间加权'
        return f'时间加权 · 半衰期 {half_life:g} 秒' if half_life is not None else '时间加权'
    if algorithm=='rolling60_v1':
        return '旧60秒口径' if row.get('realtime') is not None else '旧记录无值'
    return algorithm


def rate_plot_rows(rows,window_seconds=None):
    """Keep gaps and measurement boundaries when adapting history to a plot."""
    result=[]
    previous=None
    continuity=0
    for row in rows:
        if window_seconds is None:
            sample=dict(value=row.get('realtime'),state=row.get('rate_state'),has_gaps=bool(row.get('rate_has_gaps')))
            algorithm=row.get('rate_algorithm') or 'rolling60_v1'
        else:
            sample=window_sample(row,window_seconds)
            algorithm=row.get('window_algorithm') or 'legacy_rolling60'
        value=sample['value']
        key=(row.get('session_id'),algorithm,row.get('rate_segment'),sample['has_gaps'])
        if window_seconds is None:
            key+= (row.get('rate_half_life'),)
        if value is None or sample['state'] in ('unavailable','stale','paused'):
            previous=None
            continue
        if key!=previous:
            continuity+=1
        result.append(dict(timestamp=row['timestamp'],value=value,segment=(*key,continuity),has_gaps=sample['has_gaps']))
        previous=key
    return result


def axis_peak(values):
    peak = max([2, *values])
    unit = 10 ** math.floor(math.log10(peak))
    return math.ceil(peak / unit) * unit


def reduce_plot_rows(rows, buckets=600):
    """Keep real endpoints/extrema and every existing break for screen rendering."""
    if not rows:
        return []
    start,end=rows[0]['timestamp'],rows[-1]['timestamp']
    output=[]
    group=[]
    previous=None
    previous_key=None
    segment=0
    def flush():
        if not group:return
        chosen={0,len(group)-1,min(range(len(group)),key=lambda i:group[i]['value']),
                max(range(len(group)),key=lambda i:group[i]['value'])}
        output.extend(dict(group[i],segment=(group[i].get('segment'),segment)) for i in sorted(chosen))
    for row in rows:
        gap=(previous is None or row.get('segment')!=previous.get('segment')
             or row.get('has_gaps')!=previous.get('has_gaps') or row['timestamp']-previous['timestamp']>10)
        slot=int((row['timestamp']-start)/max(1,end-start)*buckets)
        key=(slot,row.get('segment'),bool(row.get('has_gaps')))
        if gap or key!=previous_key:
            flush();group=[]
        if gap:segment+=1
        group.append(row)
        previous,previous_key=row,key
    flush()
    return output


def short_number(value):
    if value >= 100000000:
        return f'{value / 100000000:.1f}亿'
    if value >= 10000:
        return f'{value / 10000:.1f}万'
    return f'{value:,.0f}'


def daily_chart_rows(days, today):
    """Seven local calendar dates; absent history stays missing, not zero."""
    end = date.fromisoformat(today)
    recorded = {row['day']: row for row in days}
    result = []
    for offset in range(6, -1, -1):
        day = end - timedelta(days=offset)
        row = recorded.get(day.isoformat())
        result.append(dict(timestamp=datetime.combine(day, datetime.min.time(),BEIJING).timestamp(),
                           value=row['gain'] if row else 0, observed=row is not None and row.get('sessions',1)>0,
                           label='今天' if offset == 0 else f'{day.month}/{day.day}',
                           current=offset == 0))
    return result


def plot(canvas, box, rows, *, bars=False, scale=1, tag='plot',time_range=None,
         palette=None,font_size=9,bar_labels=False,bar_slots=12,indexed=False,max_gap=10):
    """Logical coordinates; optional time_range fixes a rate chart's visible axis."""
    colors=dict(muted=MUTED,line=LINE,accent=ACCENT,warning=WARNING,
                bar='#81b1fc',surface='white')
    if palette is not None:colors.update(palette)
    canvas.delete(tag)
    x, y, width, height = box
    left, right, top, bottom = x+38, x+width-6, y+6, y+height-22
    if bars and bar_labels:
        top += font_size+7  # Dedicated headroom even when the tallest bar reaches the axis maximum.
    def text(px, py, value, anchor='center'):
        canvas.create_text(round(px*scale), round(py*scale), text=value, anchor=anchor,
                           fill=colors['muted'], font=(FONT, -round(font_size*scale)), tags=(tag,))
    def line(points, **kw):
        canvas.create_line(*[round(p*scale) for p in points], tags=(tag,), **kw)
    def time_axis(start,end):
        text(left,bottom+15,format_time(start),'w')
        if end>start:
            text(right,bottom+15,format_time(end),'e')
    if not bars and time_range is not None:
        rows=[row for row in rows if time_range[0]<=row['timestamp']<=time_range[1]]
    if not bars and any(row.get('has_gaps') for row in rows):
        canvas.create_text(round(right*scale),round(top*scale),
                           text='虚线 ≥ 下限 · 升级尾段未计入',anchor='ne',
                           fill=colors['warning'],font=(FONT,-round(font_size*scale)),tags=(tag,))
        top+=15
    peak = axis_peak([r['value'] for r in rows])
    for ratio in (0, .5, 1):
        yy = bottom-(bottom-top)*ratio
        line((left, yy, right, yy), fill=colors['line'])
        text(left-7, yy, short_number(peak*ratio), 'e')
    if not rows:
        text((left+right)/2, (top+bottom)/2, '开始记录后显示每分钟收益' if bars else '等待有效经验读数')
        if not bars and time_range is not None:
            time_axis(*time_range)
        return
    if bars:
        step = (right-left)/max(bar_slots, len(rows))
        current = int(time.time()//60)*60
        for i, row in enumerate(rows):
            xx = left+i*step
            if row.get('observed', True):
                h = (bottom-top)*row['value']/peak
                canvas.create_rectangle(round((xx+2)*scale), round((bottom-max(1,h))*scale),
                                         round((xx+step-2)*scale), round(bottom*scale),
                                         fill=colors['accent'] if row.get('current',row['timestamp']==current) else colors['bar'],
                                         outline='', tags=(tag,))
                if bar_labels:
                    label=f"{row['value']:,.0f}"
                    if len(label)*font_size*.65 > step-6:
                        label=short_number(row['value'])
                    text(xx+step/2,bottom-max(1,h)-3,label,'s')
            else:
                line((xx+2,bottom-2,xx+step-2,bottom-2),fill=colors['muted'],dash=(2,2))
                if bar_labels:
                    text(xx+step/2,bottom-5,'—','s')
        indices = sorted(set([0, len(rows)//2, len(rows)-1]))
        # Avoid adjacent labels when the first few minutes have just arrived.
        indices = [i for j,i in enumerate(indices) if j==0 or i-indices[j-1]>=3]
        if all('label' in row for row in rows):
            indices=range(len(rows))
        for i in indices:
            text(left+(i+.5)*step, bottom+15, rows[i].get('label',format_time(rows[i]['timestamp'],'%H:%M')))
    else:
        start, end = time_range if time_range is not None else (rows[0]['timestamp'],rows[-1]['timestamp'])
        points, previous = [], None
        def draw_segment(points,endpoint=False,has_gaps=False):
            color=colors['warning'] if has_gaps else colors['accent']
            if len(points)>=4:
                style=dict(fill=color,width=max(1,round(1.8*scale)))
                if has_gaps:
                    style['dash']=(max(1,round(5*scale)),max(1,round(3*scale)))
                line(points,**style)
            if points and (len(points)==2 or endpoint):
                xx,yy=points[-2:]
                canvas.create_oval((xx-2.5)*scale,(yy-2.5)*scale,(xx+2.5)*scale,(yy+2.5)*scale,fill=color,outline=colors['surface'],tags=(tag,))
        for index,row in enumerate(rows):
            if previous and (row.get('segment')!=previous.get('segment')
                             or bool(row.get('has_gaps'))!=bool(previous.get('has_gaps'))
                             or (max_gap is not None and row['timestamp']-previous['timestamp']>max_gap)):
                draw_segment(points,has_gaps=bool(previous.get('has_gaps')))
                points=[]
            xx=(left+(right-left)*index/max(1,len(rows)-1) if indexed else
                left+(right-left)*(row['timestamp']-start)/max(1,end-start))
            if indexed and len(rows)==1:xx=(left+right)/2
            yy=bottom-(bottom-top)*row['value']/peak
            points.extend((xx,yy))
            previous=row
        draw_segment(points,endpoint=True,has_gaps=bool(previous.get('has_gaps')))
        if indexed:
            for i in sorted({round(j*(len(rows)-1)/4) for j in range(5)}):
                xx=(left+right)/2 if len(rows)==1 else left+(right-left)*i/(len(rows)-1)
                text(xx,bottom+15,format_time(rows[i]['timestamp'],'%H:%M'),
                     'center' if len(rows)==1 or 0<i<len(rows)-1 else 'w' if i==0 else 'e')
        else:
            time_axis(start,end)
