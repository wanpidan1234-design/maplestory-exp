"""Temporal, screen-only upgrade confirmation; uncertain transitions never add EXP."""
import time
from tracker import _finite


class AutoLevel:
    def __init__(self,tracker,clock=time.monotonic):
        self.tracker=tracker;self.clock=clock;self.reset()

    def reset(self):
        self.level=None;self.candidate=None;self.count=0;self.since=0
        self.last_time=None;self.percent=None;self.wait_since=None;self.last_event=None

    def _vote(self,level,now):
        if level is None:
            self.candidate=None;self.count=0;return False
        if self.last_time is None or now-self.last_time>2 or level!=self.candidate:
            self.candidate=level;self.count=1;self.since=now
        else:self.count+=1
        return self.count>=3 and now-self.since>=.5

    def feed(self,reading,level,manual_cap=None,observed_at=None):
        now=self.clock() if observed_at is None else observed_at
        t=self.tracker
        if not t._fresh_time(now) or (self.last_time is not None and now<=self.last_time):
            return 'stale',0
        if reading.current is not None and (not _finite(reading.current) or reading.current<0):
            return 'invalid',0
        stable=self._vote(level,now);self.last_time=now
        if t.blocked:return 'blocked',0
        if self.level is None and stable:self.level=level;t.level=level
        drop=t.previous is not None and reading.current is not None and reading.current<t.previous
        changed=self.level is not None and level is not None and level!=self.level
        if drop or changed or self.wait_since is not None:
            if self.wait_since is None:self.wait_since=now
            reset_percent=self.percent is None or reading.percent is None or reading.percent<self.percent
            if stable and self.level is not None and level==self.level+1 and drop and reset_percent:
                old_level=self.level;old_exp=t.previous;old_cap=t.cap
                t.blocked=True
                before=t.total
                if old_cap is not None and old_cap>=old_exp and t.confirm_level(reading,observed_at=now):
                    status='auto_level';delta=t.total-before
                else:
                    # New-level EXP is observed exactly. Unknown old-level tail is
                    # explicitly recorded as a lower bound, never estimated from
                    # rounded %. A confirmed upgrade preserves continuous windows;
                    # _record_gain preserves samples across an observation outage.
                    delta=reading.current;t._record_gain(delta,now,missing_gain=True)
                    t._last_input_at=now
                    t.previous=reading.current;t.cap=reading.cap;t.blocked=False
                    t.confirmed_levels+=1;t.level_gaps+=1;status='auto_level_gap'
                self.last_event=dict(from_level=old_level,to_level=level,previous_exp=old_exp,current_exp=reading.current,previous_cap=old_cap,missing_tail=status=='auto_level_gap',confirmation_id=t.confirmed_levels)
                self.level=level;t.level=level;self.percent=reading.percent;self.wait_since=None
                return status,delta
            if not drop and not changed and level==self.level and stable:
                self.wait_since=None  # A transient low EXP frame recovered.
            elif self.level is not None and level==self.level+1:
                # A plausible new level has just become readable (e.g. after the
                # in-game flash). Finish collecting fresh votes even if the EXP
                # drop was observed more than three seconds ago.
                if not stable:
                    t.live.invalidate()
                    return 'level_pending',0
                t.blocked=True
                t.live.invalidate()
                return 'level_mismatch',0
            elif now-self.wait_since<(8 if level is None else 3):
                t.live.invalidate()
                return 'level_pending',0
            else:
                t.blocked=True
                t.live.invalidate()
                return ('level_mismatch' if changed else 'decrease'),0
        status,delta=t.feed(reading,manual_cap,observed_at=now)
        if status in ('ok','baseline','gap'):self.percent=reading.percent
        return status,delta
