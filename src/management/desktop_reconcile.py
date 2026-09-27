"""Read-only provider reconciliation of another job's uncertain native dispatch.

Only the local operational dispatch journal changes. No provider database,
business job, conversation binding, candidate or model request is modified.
Finding a turn preserves its identity in uncertain state; ACK follows proof of
that exact turn's completion, so an active turn cannot disappear from this check.
"""
from __future__ import annotations

import re
import time
import uuid

from .ai import AIError
from . import desktop_dispatch as journal


_STAGE = re.compile(r'model-stage-(0|[1-9][0-9]{0,8})\Z')
_clock = time.monotonic
_pause = time.sleep
_MAX_PENDING = 32
_BUDGET = 25.0
_HISTORY_WAIT = 10.0


def _unknown():
    return AIError('AI_DELIVERY_UNKNOWN', '原事项还有未核实的发送记录；已保留原任务，未重复发送。')


def _busy():
    return AIError('AI_CONVERSATION_BUSY', '原事项上一轮仍在处理，尚未确认结束；未再发送一轮。')


def _client_message_id(record):
    matched = _STAGE.fullmatch(record['step'])
    if not matched:
        raise _unknown()
    stage = matched.group(1)
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
        'personal-management/' + record['epoch'] + '/' + record['job_id'] + '/stage/' + stage))


def _snapshot(gateway, thread, timeout=1):
    try:
        value = gateway.snapshot(thread, timeout=timeout)
    except Exception:
        return None
    if isinstance(value, dict) and value.get('id', thread) != thread:
        raise _unknown()
    return value


def _load_history(gateway, thread, deadline):
    """A load acknowledgement is useful only after its stream revision arrives."""
    try:
        stream = gateway.subscribe(thread)
        owner = getattr(stream, 'owner_client_id', None)
        reply = gateway.request_owner(thread, 'thread-follower-load-complete-history',
            {'conversationId': thread}, version=1, timeout=10)
        if not isinstance(reply, dict) or reply.get('resultType') != 'success':
            raise _unknown()
        result = reply.get('result')
        if not isinstance(result, dict):
            raise _unknown()
        revision = result.get('revision')
        if revision is None and isinstance(result.get('result'), dict):
            revision = result['result'].get('revision')
        if type(revision) is not int or revision < 0:
            raise _unknown()
        until = min(deadline, _clock() + _HISTORY_WAIT)
        # Fixed iterations also bound faulty clocks or unusual test transports.
        for _ in range(201):
            if _clock() >= until:
                break
            before = getattr(stream, 'revision', None)
            state = _snapshot(gateway, thread, timeout=min(.2, max(0, until - _clock())))
            after = getattr(stream, 'revision', None)
            if owner != getattr(stream, 'owner_client_id', None):
                raise _unknown()
            if (type(before) is int and type(after) is int and before >= revision
                    and after >= revision and state is not None):
                return state
            _pause(min(.05, max(0, until - _clock())))
    except AIError:
        raise
    except Exception:
        raise _unknown() from None
    raise _unknown()


class _DeadlineReader:
    def __init__(self, reader, deadline):
        self.reader, self.deadline = reader, deadline

    def request(self, method, params):
        if method not in {'thread/read', 'thread/turns/list'} or _clock() >= self.deadline:
            raise _unknown()
        result = self.reader.request(method, params)
        if _clock() > self.deadline:
            raise _unknown()
        return result


def reconcile_pending(core, job, thread, gateway, workspace, reader_factory):
    """Reconcile only same-epoch/thread native intents belonging to other jobs.

    Returns None when every selected request has an exact terminal proof.
    Missing/ambiguous history remains unknown; a known active turn remains busy.
    This is called before a new explicitly requested dispatch, never as a resend.
    """
    # Avoid a cycle when native_desktop calls this adapter.
    from .native_desktop import START, TERMINAL, _correlated_turn, _status, _stored_turn, _uuid, state_turns, terminal_receipt

    try:
        thread = _uuid(thread)
    except AIError:
        raise _unknown() from None
    with core.store.connect() as c:
        if core.store.meta(c, 'epoch') != job['epoch']:
            raise _unknown()
        records = [dict(row) for row in c.execute('''SELECT d.* FROM desktop_dispatches d
            JOIN conversation_operations o ON o.job_id=d.job_id
            WHERE d.epoch=? AND d.job_id!=? AND o.provider_thread_id=?
            AND d.method=? AND d.state IN ('sending','uncertain')
            ORDER BY d.created_at,d.operation_id LIMIT ?''',
            (job['epoch'], job['id'], thread, START, _MAX_PENDING + 1))]
    if not records:
        return None
    if len(records) > _MAX_PENDING:
        raise _unknown()

    deadline = _clock() + _BUDGET
    state = _snapshot(gateway, thread)
    history_loaded = False
    reader = None
    try:
        for record in records:
            if _clock() >= deadline:
                raise _unknown()
            client_id = _client_message_id(record)
            turn_id = record.get('provider_turn_id')
            if not turn_id:
                matched = _correlated_turn(state, client_id)
                if matched is None and not history_loaded:
                    state = _load_history(gateway, thread, deadline)
                    history_loaded = True
                    matched = _correlated_turn(state, client_id)
                if matched is None:
                    raise _unknown()
                turn_id = matched['turnId']
            try:
                turn_id = _uuid(turn_id)
            except AIError:
                raise _unknown() from None
            try:
                with core.store.connect() as c:
                    record = journal.mark_uncertain(c, epoch=job['epoch'], operation_id=record['operation_id'],
                        claim_id=record['claim_id'], reason='unknown', provider_thread_id=thread, provider_turn_id=turn_id)
            except Exception:
                raise _unknown() from None

            observed = next((turn for turn in state_turns(state) if turn['turnId'] == turn_id), None)
            stored = None
            try:
                if reader is None:
                    reader = reader_factory(workspace, timeout=min(8, max(.1, deadline - _clock())))
                    reader.__enter__()
                stored = _stored_turn(_DeadlineReader(reader, deadline), thread, turn_id)
            except Exception:
                # A verified desktop terminal event remains sufficient even if
                # the independent persisted-history reader is temporarily down.
                stored = None
            terminal = terminal_receipt(stored) or (observed is not None and _status(observed) in TERMINAL)
            if not terminal:
                if _status(observed) in {'inProgress', 'active', 'running'} or _status(stored) in {'inProgress', 'active', 'running'}:
                    raise _busy()
                raise _unknown()
            try:
                with core.store.connect() as c:
                    journal.acknowledge(c, epoch=job['epoch'], operation_id=record['operation_id'],
                        claim_id=record['claim_id'], provider_thread_id=thread, provider_turn_id=turn_id)
            except Exception:
                raise _unknown() from None
    finally:
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass
    return None
