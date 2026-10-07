"""Best-effort startup evidence and a local, globally leased review queue.

The history database is an evidence path only: this module never opens it.
Recovered events require inspection of partial work, never blind action replay.
"""
import argparse
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

ROOT = Path(os.path.abspath(__file__)).parent
RECENT_ERRORS = deque(maxlen=32)
LEASE_SECONDS = 1800
MAX_ATTEMPTS = 3
_FIELDS = {'event_id', 'instance_id', 'pid', 'created_at', 'reason',
           'parent_review_id', 'review_required', 'project_root', 'config_dir',
           'history_path', 'evidence'}


def _path(value):
    # abspath is lexical; launch identity construction must not touch disk.
    return os.path.normcase(os.path.abspath(os.fspath(value)))


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('expected canonical UUID')
    return value


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _paths(project_root, config_dir=None, history_path=None):
    root = _path(project_root)
    config = _path(config_dir or Path(os.environ.get('LOCALAPPDATA', root)) / 'MapleExpTracker')
    history = _path(history_path or Path(os.environ.get('MAPLE_EXP_DATA_DIR', str(Path(root) / 'data'))) / 'history.sqlite3')
    return root, config, history


def _evidence(config, history):
    return {'live_status': str(Path(config) / 'live-status.json'),
            'recognition_failures': str(Path(config) / 'recognition-failures'),
            'history': history}


def make_launch_context(project_root, config_dir, history_path, *, user_requested=False):
    root, config, history = _paths(project_root, config_dir, history_path)
    try:
        parent = _uuid(os.environ.get('MAPLE_EXP_REVIEW_PARENT'))
    except (ValueError, TypeError, AttributeError):
        parent = None
    # Only an explicit user-requested verification start overrides maintenance
    # suppression. Automatic maintenance launches always use the default.
    if user_requested:
        parent = None
    identity = str(uuid.uuid4())
    return dict(event_id=identity, instance_id=identity, pid=os.getpid(), created_at=_utc(),
                reason='maintenance' if parent else 'user', parent_review_id=parent,
                review_required=parent is None, project_root=root, config_dir=config,
                history_path=history, evidence=_evidence(config, history))


def _json(value):
    def check(item, depth=0):
        if depth > 12:
            raise ValueError('snapshot nesting limit')
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                raise ValueError('JSON keys must be strings')
            for child in item.values():
                check(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check(child, depth + 1)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError('unsupported JSON value')
    check(value)
    result = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
    if len(result.encode('utf-8')) > 65536:
        raise ValueError('snapshot size limit')
    return result


def _record_error(context, error, persist=False):
    identity = context.get('event_id') if isinstance(context, dict) else None
    try:
        identity = _uuid(identity)
    except (ValueError, TypeError, AttributeError):
        identity = None
    record = dict(created_at=_utc(), event_id=identity, error=type(error).__name__)
    RECENT_ERRORS.append(record)  # Never retain exception messages, payloads or env.
    if persist:
        try:
            directory = Path(context['config_dir']) / 'startup-review'
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'last-error.json').write_text(json.dumps(record), encoding='utf-8')
        except Exception:
            pass


class _EmissionThread(threading.Thread):
    review_error = property(lambda self: self._review_error)
    review_persisted = property(lambda self: self._review_persisted)


def emit_startup_async(context, ui_snapshot):
    """Return a daemon worker; all serialization and filesystem work is deferred."""
    def emit():
        try:
            Queue(context['project_root'], context['config_dir'], context['history_path']).enqueue(context, ui_snapshot)
            worker._review_persisted = True
        except Exception as error:
            worker._review_error = type(error).__name__
            _record_error(context, error, persist=True)
    worker = _EmissionThread(target=emit, name='startup-review', daemon=True)
    worker._review_error, worker._review_persisted = None, False
    try:
        worker.start()
    except Exception as error:
        worker._review_error = type(error).__name__
        _record_error(context, error)
    return worker


class Queue:
    def __init__(self, project_root=ROOT, config_dir=None, history_path=None, *, clock=time.time):
        self.project_root, self.config_dir, self.history_path = _paths(project_root, config_dir, history_path)
        self.path = Path(self.config_dir) / 'startup-review' / 'queue.sqlite3'
        self.clock = clock

    def _validate(self, context):
        if not isinstance(context, dict) or set(context) != _FIELDS:
            raise ValueError('invalid context fields')
        _uuid(context['event_id'])
        if context['instance_id'] != context['event_id'] or type(context['pid']) is not int or context['pid'] <= 0:
            raise ValueError('invalid process identity')
        stamp = datetime.fromisoformat(context['created_at'])
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError('created_at must be UTC')
        parent = context['parent_review_id']
        if parent is not None:
            _uuid(parent)
        reason = 'maintenance' if parent is not None else 'user'
        if context['reason'] != reason or type(context['review_required']) is not bool or context['review_required'] != (parent is None):
            raise ValueError('invalid review reason')
        if (context['project_root'], context['config_dir'], context['history_path']) != (self.project_root, self.config_dir, self.history_path):
            raise ValueError('context paths do not match configured paths')
        if context['evidence'] != _evidence(self.config_dir, self.history_path):
            raise ValueError('invalid evidence paths')
        return _json(context)

    @contextmanager
    def _transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('''CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, context TEXT NOT NULL, ui_snapshot TEXT NOT NULL,
                state TEXT NOT NULL, owner TEXT, lease_token TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
                recovered INTEGER NOT NULL DEFAULT 0, outcome TEXT, summary TEXT,
                completed_at REAL)''')
            connection.execute('CREATE TABLE IF NOT EXISTS receipts (event_id TEXT PRIMARY KEY, digest TEXT NOT NULL)')
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _prune(db):
        # Retain completed reviews separately from suppressed maintenance starts.
        # Tiny tombstones preserve idempotence after full payloads are pruned.
        for state in ('completed', 'maintenance'):
            rows = db.execute('''SELECT event_id, context FROM events WHERE state=?
                ORDER BY completed_at DESC, rowid DESC LIMIT -1 OFFSET 256''', (state,)).fetchall()
            db.executemany('INSERT OR IGNORE INTO receipts VALUES (?,?)',
                           ((row['event_id'], hashlib.sha256(row['context'].encode('utf-8')).hexdigest()) for row in rows))
            db.executemany('DELETE FROM events WHERE event_id=?', ((row['event_id'],) for row in rows))

    def enqueue(self, context, ui_snapshot):
        payload = self._validate(context)
        if not isinstance(ui_snapshot, dict):
            raise ValueError('ui_snapshot must be an object')
        snapshot = _json(ui_snapshot)
        identity = context['event_id']
        maintenance = not context['review_required']
        with self._transaction() as db:
            archived = db.execute('SELECT digest FROM receipts WHERE event_id=?', (identity,)).fetchone()
            if archived:
                if archived['digest'] != hashlib.sha256(payload.encode('utf-8')).hexdigest():
                    raise ValueError('event identity conflict')
                return dict(status='duplicate', event_id=identity)
            existing = db.execute('SELECT context FROM events WHERE event_id=?', (identity,)).fetchone()
            if existing:
                if json.loads(existing['context']) != context:
                    raise ValueError('event identity conflict')
                return dict(status='duplicate', event_id=identity)
            db.execute('INSERT INTO events (event_id, context, ui_snapshot, state, completed_at) VALUES (?,?,?,?,?)',
                       (identity, payload, snapshot, 'maintenance' if maintenance else 'pending', self.clock() if maintenance else None))
            self._prune(db)
        return dict(status='maintenance' if maintenance else 'enqueued', event_id=identity)

    def _event(self, row):
        event = json.loads(row['context'])
        self._validate(event)
        event.update(ui_snapshot=json.loads(row['ui_snapshot']), state=row['state'], owner=row['owner'], lease_token=row['lease_token'],
                     lease_until=row['lease_until'], attempts=row['attempts'], recovered=bool(row['recovered']),
                     outcome=row['outcome'], summary=row['summary'])
        return event

    def _recover(self, db, now):
        db.execute("""UPDATE events SET state=CASE WHEN attempts>=? THEN 'exhausted' ELSE 'pending' END,
            recovered=1, owner=NULL, lease_token=NULL, lease_until=NULL, summary='lease_expired; inspect partial work before continuing'
            WHERE state='claimed' AND lease_until<=?""", (MAX_ATTEMPTS, now))

    def _next(self, db, now):
        self._recover(db, now)
        active = db.execute("SELECT * FROM events WHERE state='claimed' LIMIT 1").fetchone()
        if active:
            return dict(status='busy', event_id=active['event_id'], owner=active['owner'], lease_until=active['lease_until'])
        row = db.execute("SELECT * FROM events WHERE state='pending' ORDER BY rowid LIMIT 1").fetchone()
        if row:
            return dict(status='pending', event_id=row['event_id'], event=self._event(row))
        rows = db.execute("SELECT event_id, substr(summary,1,256) AS summary FROM events WHERE state='exhausted' ORDER BY rowid").fetchall()
        if rows:
            events = [dict(row) for row in rows]
            return dict(status='needs_attention', exhausted=len(events), events=events, **events[0])
        return dict(status='no_pending', exhausted=0)

    def peek(self):
        with self._transaction() as db:
            return self._next(db, self.clock())

    @staticmethod
    def _lease(seconds):
        if type(seconds) not in (int, float) or not 1 <= seconds <= LEASE_SECONDS:
            raise ValueError('lease must be between 1 and 1800 seconds')
        return seconds

    def claim(self, owner, lease_seconds=LEASE_SECONDS, event_id=None):
        _uuid(owner)
        if event_id is not None:
            _uuid(event_id)
        self._lease(lease_seconds)
        with self._transaction() as db:
            now = self.clock()
            result = self._next(db, now)
            if event_id is not None and result['status'] != 'busy':
                row = db.execute('SELECT * FROM events WHERE event_id=?', (event_id,)).fetchone()
                if not row or row['state'] != 'pending':
                    return dict(status='not_pending', event_id=event_id)
                result = dict(status='pending', event_id=event_id)
            if result['status'] != 'pending':
                return result
            identity = result['event_id']
            token = str(uuid.uuid4())
            db.execute("UPDATE events SET state='claimed', owner=?, lease_token=?, lease_until=?, attempts=attempts+1 WHERE event_id=?",
                       (owner, token, now + lease_seconds, identity))
            row = db.execute('SELECT * FROM events WHERE event_id=?', (identity,)).fetchone()
            return dict(status='claimed', event_id=identity, lease_token=token, event=self._event(row))

    def _owned(self, db, event_id, owner, lease_token):
        _uuid(event_id)
        _uuid(owner)
        _uuid(lease_token)
        return db.execute("SELECT * FROM events WHERE event_id=? AND owner=? AND lease_token=? AND state='claimed' AND lease_until>?",
                          (event_id, owner, lease_token, self.clock())).fetchone()

    def renew(self, event_id, owner, lease_token, lease_seconds=LEASE_SECONDS):
        self._lease(lease_seconds)
        with self._transaction() as db:
            if not self._owned(db, event_id, owner, lease_token):
                return dict(status='lease_lost', event_id=event_id)
            until = self.clock() + lease_seconds
            db.execute('UPDATE events SET lease_until=? WHERE event_id=?', (until, event_id))
            return dict(status='renewed', event_id=event_id, lease_until=until)

    def complete(self, event_id, owner, outcome, summary, *, lease_token):
        if outcome not in ('no_change', 'implemented', 'blocked'):
            raise ValueError('invalid completion outcome')
        if not isinstance(summary, str) or not 1 <= len(summary) <= 4096:
            raise ValueError('summary must contain 1 to 4096 characters')
        with self._transaction() as db:
            if not self._owned(db, event_id, owner, lease_token):
                return dict(status='lease_lost', event_id=event_id)
            db.execute("UPDATE events SET state='completed', outcome=?, summary=?, completed_at=?, lease_token=NULL, lease_until=NULL WHERE event_id=?",
                       (outcome, summary, self.clock(), event_id))
            self._prune(db)
        return dict(status='completed', event_id=event_id, outcome=outcome)

    def release(self, event_id, owner, lease_token, summary='retry_requested'):
        if not isinstance(summary, str) or not 1 <= len(summary) <= 4096:
            raise ValueError('summary must contain 1 to 4096 characters')
        with self._transaction() as db:
            row = self._owned(db, event_id, owner, lease_token)
            if not row:
                return dict(status='lease_lost', event_id=event_id)
            state = 'exhausted' if row['attempts'] >= MAX_ATTEMPTS else 'pending'
            db.execute('UPDATE events SET state=?, owner=NULL, lease_token=NULL, lease_until=NULL, summary=? WHERE event_id=?',
                       (state, summary, event_id))
            return dict(status='released' if state == 'pending' else state, event_id=event_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', default=str(ROOT))
    parser.add_argument('--config-dir')
    parser.add_argument('--history-path')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('peek')
    enqueue = sub.add_parser('enqueue')
    enqueue.add_argument('--file', required=True, help='JSON object with context and ui_snapshot')
    for name in ('claim', 'renew', 'complete', 'release'):
        command = sub.add_parser(name)
        command.add_argument('--owner', required=True)
        if name == 'claim':
            command.add_argument('--event')
        else:
            command.add_argument('--event', required=True)
            command.add_argument('--lease-token', required=True)
        if name == 'complete':
            command.add_argument('--outcome', choices=('no_change', 'implemented', 'blocked'), required=True)
            command.add_argument('--summary', required=True)
        if name == 'release':
            command.add_argument('--summary', default='retry_requested')
    args = parser.parse_args(argv)
    try:
        queue = Queue(args.project_root, args.config_dir, args.history_path)
        if args.command == 'enqueue':
            payload = json.loads(Path(args.file).read_text(encoding='utf-8'))
            result = queue.enqueue(payload['context'], payload['ui_snapshot'])
        else:
            kwargs = {key: getattr(args, key) for key in ('owner', 'lease_token', 'outcome', 'summary') if hasattr(args, key)}
            if getattr(args, 'event', None):
                kwargs['event_id'] = args.event
            result = getattr(queue, args.command)(**kwargs)
    except Exception as error:
        result = dict(status='error', error=type(error).__name__)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 1 if result['status'] == 'error' else 0


if __name__ == '__main__':
    raise SystemExit(main())
