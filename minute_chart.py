"""Calendar-minute gains, distinct from the rolling 60-second rate."""
def recorded_minute_curve(minutes):
    """Compressed minute axis; gaps are omitted and never bridged by a line."""
    result=[]
    segment=0
    previous=None
    for row in minutes:
        if not row.get('observed',True):
            continue
        stamp=row['timestamp']
        if previous is None or stamp-previous!=60:
            segment+=1
        result.append(dict(timestamp=stamp,value=row['gain'],segment=segment))
        previous=stamp
    return result


def minute_buckets(rows):
    buckets={}
    for row in rows:
        if row.get('status')=='legacy_summary':
            continue
        minute=int(row['timestamp']//60)*60
        bucket=buckets.setdefault(minute,dict(timestamp=minute,gain=0,observed=True))
        bucket['gain']+=row['gain']
    if not buckets:
        return []
    return [buckets.get(t,dict(timestamp=t,gain=0,observed=False)) for t in range(min(buckets),max(buckets)+60,60)]
