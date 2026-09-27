"""Committed dispatch intents for a desktop gateway; no transport or model calls.

Call initialize() on a Store connection, then prepare() with a stable logical
step. Only a non-None claim() result permits one network dispatch. Every public
operation owns and commits its transaction before returning; callers must not
wrap it in a business transaction or hold that transaction during network I/O.

An unfinished claim never expires into permission to resend. Reopen/reconnect
and reconcile it, or confirm_not_sent() only after the transport/provider has
proved that the request was not delivered. A claim_id fences late results from
an older attempt; it is a local concurrency identifier, not a provider credential.
Only identifiers, a request digest and fixed reason codes are persisted.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re

from .schemas import BusinessError
from .storage import encode, new_id, now


_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z')
_METHOD = re.compile(r'[A-Za-z][A-Za-z0-9_./:-]{0,127}\Z')
_UNCERTAIN = frozenset({'connection_lost', 'ack_timeout', 'process_interrupted',
                        'response_unreadable', 'unknown'})
_NOT_SENT = frozenset({'transport_not_started', 'request_not_written',
                       'provider_confirmed_not_received'})


def _error(code, message):
    return BusinessError('dispatch_' + code, message)


def _identifier(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise _error('invalid_identifier', '派发记录需要有效的操作标识。')
    return value


def _json_value(value, depth=0):
    if depth > 100:
        raise ValueError()
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError()
            _json_value(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _json_value(item, depth + 1)
    elif value is not None and type(value) not in {str, int, float, bool}:
        raise ValueError()


def _fingerprint(method, payload):
    if not isinstance(method, str) or not _METHOD.fullmatch(method):
        raise _error('invalid_method', '派发方法标识无效。')
    try:
        if type(payload) is not dict:
            raise ValueError()
        _json_value(payload)
        canonical = encode({'method': method, 'payload': payload}).encode('utf-8')
        if len(canonical) > 4 * 1024 * 1024:
            raise ValueError()
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise _error('invalid_payload', '派发内容必须是容量范围内的标准 JSON 对象。') from None
    return hashlib.sha256(canonical).hexdigest()


def initialize(c):
    """Create the operational table; compatible with Store initialization."""
    c.execute('''CREATE TABLE IF NOT EXISTS desktop_dispatches(
        operation_id TEXT PRIMARY KEY, epoch TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES jobs(id), step TEXT NOT NULL,
        method TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('prepared','sending','acknowledged','uncertain')),
        attempt INTEGER NOT NULL DEFAULT 0, claim_id TEXT,
        provider_thread_id TEXT, provider_turn_id TEXT, reason_code TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(epoch,job_id,step))''')


@contextmanager
def _transaction(c):
    if c.in_transaction:
        raise _error('transaction_active', '派发登记需要独立事务；请先结束当前业务事务。')
    c.execute('BEGIN IMMEDIATE')
    try:
        yield
        c.commit()
    except BaseException:
        c.rollback()
        raise


def _current_epoch(c, epoch):
    _identifier(epoch)
    row = c.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
    if not row or json.loads(row[0]) != epoch:
        raise _error('epoch_mismatch', '数据空间身份已变化，旧派发不能继续。')


def _row(cursor):
    value = cursor.fetchone()
    return dict(zip((item[0] for item in cursor.description), value)) if value is not None else None


def _load(c, epoch, operation_id):
    _current_epoch(c, epoch)
    _identifier(operation_id)
    record = _row(c.execute('SELECT * FROM desktop_dispatches WHERE operation_id=?', (operation_id,)))
    if not record:
        raise _error('not_found', '派发记录不存在。')
    if record['epoch'] != epoch:
        raise _error('epoch_mismatch', '派发记录属于其他数据空间身份。')
    job = c.execute('SELECT epoch FROM jobs WHERE id=?', (record['job_id'],)).fetchone()
    if not job or job[0] != epoch:
        raise _error('epoch_mismatch', '派发所属作业的身份已变化。')
    return record


def prepare(c, *, epoch, job_id, step, method, payload):
    """Commit a stable intent; replaying the same key never resets its state."""
    _identifier(job_id)
    _identifier(step)
    digest = _fingerprint(method, payload)
    with _transaction(c):
        _current_epoch(c, epoch)
        job = c.execute('SELECT epoch FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job or job[0] != epoch:
            raise _error('epoch_mismatch', '派发作业不存在或属于其他数据空间身份。')
        record = _row(c.execute('SELECT * FROM desktop_dispatches WHERE epoch=? AND job_id=? AND step=?',
                               (epoch, job_id, step)))
        if record:
            if record['method'] != method or record['payload_sha256'] != digest:
                raise _error('content_conflict', '同一派发步骤的内容已变化，不能覆盖或重新发送。')
        else:
            stamp = now()
            operation_id = new_id()
            c.execute('''INSERT INTO desktop_dispatches(operation_id,epoch,job_id,step,
                method,payload_sha256,state,created_at,updated_at) VALUES (?,?,?,?,?,?,'prepared',?,?)''',
                (operation_id, epoch, job_id, step, method, digest, stamp, stamp))
            record = _load(c, epoch, operation_id)
    return record


def get(c, *, epoch, operation_id):
    """Return an epoch-checked operational snapshot, never the request body."""
    with _transaction(c):
        record = _load(c, epoch, operation_id)
    return record


def claim(c, *, epoch, operation_id):
    """Commit sending before return; only the winning non-None result may send.

    There is deliberately no timeout, lease takeover or reset-on-restart path.
    A sending/uncertain/acknowledged intent is not permission to dispatch again.
    """
    with _transaction(c):
        record = _load(c, epoch, operation_id)
        if record['state'] != 'prepared':
            return None
        c.execute("""UPDATE desktop_dispatches SET state='sending',attempt=attempt+1,
            claim_id=?,reason_code=NULL,updated_at=? WHERE operation_id=? AND state='prepared'""",
            (new_id(), now(), operation_id))
        record = _load(c, epoch, operation_id)
    return record


def _claimed(c, epoch, operation_id, claim_id):
    _identifier(claim_id)
    record = _load(c, epoch, operation_id)
    if record['claim_id'] != claim_id:
        raise _error('claim_mismatch', '这份回执不属于当前派发尝试，未修改记录。')
    return record


def _identities(record, provider_thread_id, provider_turn_id):
    result = []
    for name, value in (('provider_thread_id', provider_thread_id), ('provider_turn_id', provider_turn_id)):
        if value is not None:
            _identifier(value)
            if record[name] is not None and value != record[name]:
                raise _error('result_conflict', '派发回执的远端标识发生冲突，未覆盖原记录。')
        result.append(value if value is not None else record[name])
    return result


def acknowledge(c, *, epoch, operation_id, claim_id, provider_thread_id=None, provider_turn_id=None):
    """Record a verified provider acknowledgement, including a late one."""
    with _transaction(c):
        record = _claimed(c, epoch, operation_id, claim_id)
        if record['state'] not in {'sending', 'uncertain', 'acknowledged'}:
            raise _error('state_conflict', '当前状态不能接收派发回执。')
        thread, turn = _identities(record, provider_thread_id, provider_turn_id)
        c.execute("""UPDATE desktop_dispatches SET state='acknowledged',
            provider_thread_id=?,provider_turn_id=?,reason_code=NULL,updated_at=? WHERE operation_id=?""",
            (thread, turn, now(), operation_id))
        record = _load(c, epoch, operation_id)
    return record


def mark_uncertain(c, *, epoch, operation_id, claim_id, reason='unknown',
                   provider_thread_id=None, provider_turn_id=None):
    """Persist uncertainty and any verified partial identifiers; never resend."""
    if not isinstance(reason, str) or reason not in _UNCERTAIN:
        raise _error('invalid_reason', '发送结果未知需要固定的原因标识。')
    with _transaction(c):
        record = _claimed(c, epoch, operation_id, claim_id)
        if record['state'] not in {'sending', 'uncertain'}:
            raise _error('state_conflict', '当前派发状态不能改为结果未知。')
        thread, turn = _identities(record, provider_thread_id, provider_turn_id)
        c.execute("""UPDATE desktop_dispatches SET state='uncertain',reason_code=?,
            provider_thread_id=?,provider_turn_id=?,updated_at=? WHERE operation_id=?""",
            (reason, thread, turn, now(), operation_id))
        record = _load(c, epoch, operation_id)
    return record


def confirm_not_sent(c, *, epoch, operation_id, claim_id, evidence):
    """Permit a new claim only with proof of non-delivery, never a timeout.

    Evidence must come from the transport (no request bytes written) or a
    provider's authoritative negative acknowledgement. A disconnect, missing
    local receipt, elapsed time or unsuccessful lookup is not such proof.
    """
    if not isinstance(evidence, str) or evidence not in _NOT_SENT:
        raise _error('not_sent_unproven', '未证明请求没有送达，不能恢复派发。')
    with _transaction(c):
        record = _claimed(c, epoch, operation_id, claim_id)
        if record['state'] not in {'sending', 'uncertain'}:
            raise _error('state_conflict', '当前状态不能恢复派发。')
        if record['provider_thread_id'] is not None or record['provider_turn_id'] is not None:
            raise _error('not_sent_unproven', '已有远端回执标识，不能视为尚未发送。')
        c.execute("""UPDATE desktop_dispatches SET state='prepared',claim_id=NULL,
            reason_code=?,updated_at=? WHERE operation_id=?""", (evidence, now(), operation_id))
        record = _load(c, epoch, operation_id)
    return record
