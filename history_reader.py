"""Read-only history work off the Tk thread, with incremental sample loading."""
from pathlib import Path
import sqlite3
from contextlib import closing

from app_time import day_bounds
from minute_chart import minute_buckets
from ui_charts import rate_label, rate_plot_rows, reduce_plot_rows, window_sample

_COLUMNS='''id,session_id,seq,timestamp,current_exp,gain,total,elapsed,realtime,average,hourly,status,
    percent,rate_algorithm,rate_state,rate_half_life,rate_observed_seconds,rate_segment,rolling60,
    rolling120,rolling300,rolling600,rolling1800,window_algorithm,rate_has_gaps,window_gap_mask'''


class HistoryReader:
    """Owned by one worker. Returned tuples are never mutated by a later load."""
    def __init__(self,path):
        self.path=Path(path)
        self.key=None
        self.rows=()
        self.revision=None
        self.projections={}
        self.minutes=[]

    def read(self,day,session,window):
        key=(day,session)
        with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
            db.row_factory=sqlite3.Row
            try:
                sql='SELECT MAX(last_id) FROM beijing_session_days WHERE day=?'
                args=[day]
                if session:sql+=' AND session_id=?';args.append(session)
                revision=db.execute(sql,args).fetchone()[0]
                if key!=self.key or revision!=self.revision:
                    rows=self.rows if key==self.key else ()
                    query=f'SELECT {_COLUMNS} FROM samples WHERE timestamp>=? AND timestamp<?'
                    params=list(day_bounds(day))
                    if session:query+=' AND session_id=?';params.append(session)
                    if rows:query+=' AND id>?';params.append(max(row['id'] for row in rows))
                    added=tuple(dict(row) for row in db.execute(query+' ORDER BY timestamp,id',params))
                    combined=rows+added
                    if rows and added and added[0]['timestamp']<rows[-1]['timestamp']:
                        combined=tuple(sorted(combined,key=lambda row:(row['timestamp'],row['id'])))
                    self.rows,self.key,self.revision=combined,key,revision
                    self.minutes=minute_buckets(combined)
                    self.projections={}
            finally:
                db.close()
        if window not in self.projections:
            rows=self.rows
            plotted=reduce_plot_rows(rate_plot_rows(rows,window))
            if window is None:
                labels=list(dict.fromkeys(rate_label(row) for row in rows if row['realtime'] is not None))
                caption=labels[0] if len(labels)==1 else '混合口径（见明细）' if labels else ''
                has_gaps=any(bool(row.get('rate_has_gaps')) for row in rows)
            else:
                samples=[window_sample(row,window) for row in rows]
                versions={row.get('window_algorithm') or 'legacy' for row,sample in zip(rows,samples) if sample['value'] is not None}
                caption='混合版本（见明细）' if len(versions)>1 else ('旧60秒口径 · 覆盖未知' if versions=={'legacy'} else '有效观测窗口')
                has_gaps=any(sample['has_gaps'] for sample in samples)
            self.projections[window]={'plot_rows':plotted,'caption':caption,'has_gaps':has_gaps}
        return dict(self.projections[window],rows=self.rows,minute_data=self.minutes,window=window,
                    selection=key,revision=self.revision)
