"""One bounded operational snapshot per AI job, separate from business history.

Progress does not advance the business revision or add receipts/change records.
All writes retain the worker's epoch, generation and current-conversation checks.
"""
from __future__ import annotations

import datetime as dt
import json
import time

from .storage import Store, now


PHASES = frozenset({'connecting', 'resuming', 'creating_thread', 'thread_ready',
                   'sending', 'waiting_model', 'reasoning', 'receiving',
                   'validating', 'provider_retry', 'reconciled', 'compacting', 'reading', 'checkpointed'})
MAX_PREVIEW = 32_000
PREVIEW_INTERVAL = .25
FIELDS = ('phase', 'provider_thread_id', 'provider_turn_id', 'provider_contract', 'recovery', 'preview_text')


class ProgressRejected(RuntimeError):
    """Tracking cannot safely continue; the adapter must stop its model turn."""


def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS conversation_progress(
        job_id TEXT PRIMARY KEY REFERENCES jobs(id),
        conversation_id TEXT REFERENCES conversations(id),
        epoch TEXT NOT NULL,generation INTEGER NOT NULL,phase TEXT NOT NULL,
        provider_thread_id TEXT,provider_turn_id TEXT,provider_project_path TEXT,provider_contract TEXT,recovery TEXT,
        preview_text TEXT NOT NULL DEFAULT '' CHECK(length(preview_text)<=32000),
        started_at TEXT NOT NULL,updated_at TEXT NOT NULL,finished_at TEXT)''')
    if 'provider_contract' not in {r[1] for r in c.execute('PRAGMA table_info(conversation_progress)')}:
        c.execute('ALTER TABLE conversation_progress ADD COLUMN provider_contract TEXT')
    c.execute('CREATE INDEX IF NOT EXISTS conversation_progress_latest ON conversation_progress(conversation_id,started_at DESC)')


def _input(job):
    value = job['input']
    return json.loads(value) if isinstance(value, str) else value


def _current(c, job):
    row = c.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
    if (row is None or row['kind'] != 'ai' or row['status'] != 'running' or
            row['generation'] != job['generation'] or row['epoch'] != job['epoch'] or
            row['epoch'] != Store.meta(c, 'epoch')):
        raise ProgressRejected('处理作业或数据空间已变化，已停止更新旧进度。')
    value = _input(row)
    conversation_id = value.get('conversation_id')
    if conversation_id:
        conversation = c.execute('SELECT active_job_id FROM conversations WHERE id=?', (conversation_id,)).fetchone()
        if conversation is None or conversation['active_job_id'] != row['id']:
            raise ProgressRejected('这段讨论已经开始了新的处理，旧进度已停止。')
    return row, conversation_id


def start(c, job):
    """Called inside the worker's transaction, after the job becomes running."""
    if job['kind'] != 'ai':
        return False
    row, conversation_id = _current(c, job)
    stamp = now()
    c.execute('''INSERT INTO conversation_progress(
        job_id,conversation_id,epoch,generation,phase,started_at,updated_at)
        VALUES (?,?,?,?,?,?,?) ON CONFLICT(job_id) DO NOTHING''',
        (row['id'], conversation_id, row['epoch'], row['generation'], 'connecting', stamp, stamp))
    return True


def _clean(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get('phase') not in PHASES:
        raise ProgressRejected('Codex 处理阶段格式无效，已停止本次请求。')
    clean = {'phase': snapshot['phase']}
    for name in ('provider_thread_id', 'provider_turn_id'):
        if name in snapshot:
            value = snapshot[name]
            if not isinstance(value, str) or not value or len(value) > 200:
                raise ProgressRejected('Codex 处理标识格式无效，已停止本次请求。')
            clean[name] = value
    if 'provider_contract' in snapshot:
        if snapshot['provider_contract'] not in {'background_json_v1', 'desktop_candidate_tool_v1', 'desktop_mcp_v2','desktop_mcp_v3'}:
            raise ProgressRejected('Codex 会话接口版本无效。')
        clean['provider_contract'] = snapshot['provider_contract']
    if 'recovery' in snapshot:
        if snapshot['recovery'] not in {'new', 'resumed', 'history_rebuilt'}:
            raise ProgressRejected('Codex 会话恢复状态无效，已停止本次请求。')
        clean['recovery'] = snapshot['recovery']
    if 'preview_text' in snapshot:
        text = snapshot['preview_text']
        if not isinstance(text, str) or len(text) > MAX_PREVIEW:
            raise ProgressRejected('Codex 回复预览超过允许大小，已停止本次请求。')
        try:
            text.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise ProgressRejected('Codex 回复预览编码无效，已停止本次请求。') from exc
        clean['preview_text'] = text
    return clean


class Publisher:
    def __init__(self, core, job, cancel, stop):
        self.core = core
        self.job = {key: job[key] for key in ('id', 'kind', 'input', 'epoch', 'generation')}
        self.cancel, self.stop = cancel, stop
        self.last_written = None
        self.snapshot = {}
        self.written_snapshot = {}
        self.project_path = str((core.root / 'Codex事务助手').resolve())

    def context_status(self):
        value=_input(self.job)
        identifier=value.get('context_operation_id')
        if not identifier:return None
        from .context_service import _operation,coverage
        with self.core.store.connect() as c:return coverage(self.core,c,_operation(self.core,c,identifier))

    def compact_context(self):
        from .context_service import compacted
        identifier=_input(self.job).get('context_operation_id')
        if identifier:compacted(self.core,identifier)

    def candidate_result(self):
        with self.core.store.connect() as c:
            from .session_coordinator import get_candidate
            return get_candidate(c,self.job['id'])

    def _check_stop(self):
        if self.cancel.is_set() or self.stop.is_set():
            raise ProgressRejected('本次处理已停止，不再更新旧进度。')

    def __call__(self, snapshot):
        self._check_stop()
        clean = _clean(snapshot)
        updated = {**self.snapshot, **clean}
        self.snapshot = updated
        clock = time.monotonic()
        immediate = any(updated.get(key) != self.written_snapshot.get(key)
                        for key in FIELDS if key != 'preview_text')
        if (not immediate and self.last_written is not None and
                (updated == self.written_snapshot or clock - self.last_written < PREVIEW_INTERVAL)):
            return False
        with self.core.store.lock, self.core.store.connect() as c:
            self._check_stop()
            c.execute('BEGIN IMMEDIATE')
            try:
                current, conversation_id = _current(c, self.job)
                row = c.execute('SELECT * FROM conversation_progress WHERE job_id=?', (self.job['id'],)).fetchone()
                if (row is None or row['finished_at'] is not None or
                        row['epoch'] != self.job['epoch'] or row['generation'] != self.job['generation'] or
                        row['conversation_id'] != conversation_id):
                    raise ProgressRejected('本次处理的进度记录已经失效，已停止请求。')
                stamp = now()
                values = {key: updated.get(key, row[key]) for key in FIELDS}
                self._check_stop()
                c.execute('''UPDATE conversation_progress SET phase=?,provider_thread_id=?,provider_turn_id=?,
                    provider_project_path=?,provider_contract=?,recovery=?,preview_text=?,updated_at=? WHERE job_id=?''',
                    (values['phase'], values['provider_thread_id'], values['provider_turn_id'], self.project_path, values['provider_contract'],
                     values['recovery'], values['preview_text'], stamp, self.job['id']))
                from .session_coordinator import observe
                observe(c,current,conversation_id or ('operation:'+_input(current)['context_operation_id'] if _input(current).get('context_operation_id') else None),values,self.project_path)
                if values['phase']=='reconciled':
                    from .session_coordinator import settle
                    prior=_input(current).get('previous_operation') or {}
                    if prior.get('job_id'):settle(c,prior['job_id'],'reconciled')
                if conversation_id and values['provider_thread_id']:
                    # A provider turn may later fail; retain its real discussion
                    # identity as soon as it exists, before another model turn.
                    c.execute('''UPDATE conversations SET provider_thread_id=?,recovery=COALESCE(?,recovery),
                        version=version+1,updated_at=? WHERE id=? AND active_job_id=? AND
                        (provider_thread_id IS NOT ? OR (? IS NOT NULL AND recovery IS NOT ?))''',
                        (values['provider_thread_id'], values['recovery'], stamp, conversation_id, current['id'],
                         values['provider_thread_id'], values['recovery'], values['recovery']))
                self._check_stop()
                c.commit()
            except Exception:
                c.rollback()
                raise
        self.last_written, self.written_snapshot = clock, dict(updated)
        return True


def finish(c, job_id):
    """Freeze elapsed time when the owner transaction records a terminal state."""
    row = c.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is not None and row['status'] not in {'queued', 'running'}:
        c.execute('UPDATE conversation_progress SET finished_at=COALESCE(finished_at,?) WHERE job_id=?', (now(), job_id))
        if row['status'] in {'completed', 'awaiting_review', 'applied', 'superseded'}:
            c.execute("UPDATE conversation_progress SET preview_text='' WHERE job_id=?", (job_id,))


def _elapsed(started_at, ended_at):
    try:
        start = dt.datetime.fromisoformat(started_at)
        end = dt.datetime.fromisoformat(ended_at)
        return round(max(0.0, (end - start).total_seconds()), 1)
    except (TypeError, ValueError):
        return 0.0


def _view(row, job):
    result = dict(row) if row else {
        'job_id': job['id'], 'conversation_id': _input(job).get('conversation_id'),
        'epoch': job['epoch'], 'generation': job['generation'],
        'phase': 'queued' if job['status'] == 'queued' else 'connecting',
        'preview_text': '', 'provider_thread_id': None, 'provider_turn_id': None,
        'provider_project_path': None, 'provider_contract': None, 'recovery': None,
        'started_at': job['created_at'] if job['status'] == 'queued' else job['updated_at'],
        'updated_at': job['updated_at'], 'finished_at': None,
    }
    result['status'] = job['status']
    terminal = job['status'] not in {'queued', 'running'}
    if terminal and not result['finished_at']:
        result['finished_at'] = job['updated_at']
    result['elapsed_seconds'] = _elapsed(result['started_at'], result['finished_at'] or now())
    return result


def query(c, conversation):
    if conversation is None:
        return {'active_progress': None, 'latest_progress': None}
    epoch = Store.meta(c, 'epoch')
    active = None
    if conversation.get('active_job_id'):
        job = c.execute('SELECT * FROM jobs WHERE id=?', (conversation['active_job_id'],)).fetchone()
        if job and job['kind'] == 'ai' and job['epoch'] == epoch and job['status'] in {'queued', 'running'}:
            row = c.execute('SELECT * FROM conversation_progress WHERE job_id=? AND epoch=? AND generation=?',
                            (job['id'], epoch, job['generation'])).fetchone()
            active = _view(row, job)
            identifier=_input(job).get('context_operation_id')
            if identifier:
                operation=c.execute('SELECT stage,phase,delivered_bytes FROM context_operations WHERE id=?',(identifier,)).fetchone()
                if operation:
                    steps={r['status']:r['total'] for r in c.execute('SELECT status,count(*) total FROM context_steps WHERE operation_id=? GROUP BY status',(identifier,))}
                    pending=c.execute("SELECT count(*) FROM context_sources s JOIN material_catalog m ON m.key=s.material_key WHERE s.operation_id=? AND m.status='pending'",(identifier,)).fetchone()[0]
                    active['context_progress']={'stage':operation['stage']+1,'phase':operation['phase'],'processed':steps.get('completed',0),
                        'extracted':sum(steps.values()),'more_extracting':bool(pending)}
    latest = active
    if latest is None:
        row = c.execute('''SELECT p.* FROM conversation_progress p JOIN jobs j ON j.id=p.job_id
            WHERE p.conversation_id=? AND p.epoch=? AND p.epoch=j.epoch
            ORDER BY p.started_at DESC,p.rowid DESC LIMIT 1''', (conversation['id'], epoch)).fetchone()
        if row:
            job = c.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
            latest = _view(row, job)
            latest['resumable_context']=bool(_input(job).get('context_operation_id'))
    return {'active_progress': active, 'latest_progress': latest}


def reset(c):
    """Restoring a data space must not retain a live provider binding/preview."""
    c.execute('DELETE FROM conversation_progress')
