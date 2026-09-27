"""Durable native-desktop compaction, verified only from stored provider history.

An IPC acknowledgement confirms receipt, not completed compaction. A stage can
advance only after the exact newly observed turn is completed and contains a
contextCompaction item. Reattachment never repeats an uncertain dispatch.
"""
from __future__ import annotations

import sqlite3

from .ai import AIError
from . import desktop_dispatch as journal

METHOD = 'thread-follower-compact-thread'
COMPACTION_TIMEOUT = 180
READ_INTERVAL = .5
MAX_TURN_PAGES = 40
MAX_NEW_TURNS = 128
_STATUSES = frozenset({'completed', 'interrupted', 'failed', 'inProgress'})


class _HistoryIncomplete(RuntimeError):
    pass


def _finished(turn):
    # A partial legacy rollout can temporarily read as failed/completed before
    # its final record lands. Status alone is not authoritative completion.
    ended = turn.get('completedAt')
    return (turn.get('status') in {'completed', 'failed', 'interrupted'}
            and type(ended) is int and 0 < ended < (1 << 63))


def _turn(value):
    from .native_desktop import _uuid
    if (type(value) is not dict or value.get('status') not in _STATUSES
            or type(value.get('items')) is not list):
        raise _HistoryIncomplete()
    try:
        _uuid(value.get('id'))
    except AIError:
        raise _HistoryIncomplete() from None
    return value


def _page(value):
    if type(value) is not dict or type(value.get('data')) is not list or len(value['data']) > 500:
        raise _HistoryIncomplete()
    cursor = value.get('nextCursor')
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 8192):
        raise _HistoryIncomplete()
    return value['data'], cursor


def _after_baseline(reader, thread, baseline, check):
    """Newest-first official ordering; never infer an absent baseline's position."""
    cursor, cursors, identities, newer = None, set(), set(), []
    for _ in range(MAX_TURN_PAGES):
        check()
        params = {'threadId': thread, 'limit': 50, 'sortDirection': 'desc', 'itemsView': 'notLoaded'}
        if cursor is not None:
            params['cursor'] = cursor
        data, following = _page(reader.request('thread/turns/list', params))
        for value in data:
            value = _turn(value)
            ident = value['id']
            if ident in identities:
                raise _HistoryIncomplete()
            identities.add(ident)
            if ident == baseline:
                if not _finished(value):
                    raise _HistoryIncomplete()
                return newer
            newer.append(value)
            if len(newer) > MAX_NEW_TURNS:
                raise _HistoryIncomplete()
        if following is None or following in cursors:
            raise _HistoryIncomplete()
        cursors.add(following)
        cursor = following
    raise _HistoryIncomplete()


def _has_compaction(reader, thread, turn, check):
    """Read the exact turn's full history; items/list is not implemented yet.

    The installed App Server advertises thread/items/list in its schema but
    returns -32601 at runtime. A summary marker alone is insufficient: use the
    supported turns/list full view and fence changed completion metadata.
    """
    def marker(item):
        return (type(item) is dict and item.get('type') == 'contextCompaction'
                and isinstance(item.get('id'), str) and bool(item['id']) and len(item['id']) <= 200)

    if turn.get('itemsView') == 'full':
        return any(marker(item) for item in turn['items'])
    cursor, seen, identities = None, set(), set()
    for _ in range(MAX_TURN_PAGES):
        check()
        params = {'threadId': thread, 'limit': 10,
                  'sortDirection': 'desc', 'itemsView': 'full'}
        if cursor is not None:
            params['cursor'] = cursor
        data, following = _page(reader.request('thread/turns/list', params))
        for entry in data:
            entry = _turn(entry)
            if entry['id'] in identities:
                raise _HistoryIncomplete()
            identities.add(entry['id'])
            if entry['id'] != turn['id']:
                continue
            if (entry.get('itemsView') != 'full'
                    or entry['status'] != turn['status']
                    or entry.get('completedAt') != turn.get('completedAt')):
                # A stale overview and a newer/partial full read cannot jointly
                # prove completion. Re-read both on the next observation pass.
                raise _HistoryIncomplete()
            return any(marker(item) for item in entry['items'])
        if following is None:
            raise _HistoryIncomplete()
        if following in seen:
            raise _HistoryIncomplete()
        seen.add(following)
        cursor = following
    raise _HistoryIncomplete()


def _current_stage(progress, operation_id, stage):
    status = progress.context_status()
    if (type(status) is not dict or status.get('operation_id') != operation_id
            or type(status.get('stage')) is not int or status['stage'] < stage):
        raise AIError('AI_COMPACTION_PENDING', '上下文检查点的阶段无法确认，原任务已保留。')
    if status['stage'] > stage:
        return status['stage']
    if status.get('phase') != 'checkpointed':
        raise AIError('AI_COMPACTION_PENDING', '当前操作尚未停在可接续的检查点，未重置读取额度。')
    return stage


def _advance(core, job, value, stage, progress, check):
    """The normal service has one Store owner; its lock is reentrant."""
    with core.store.lock:
        check()
        if _current_stage(progress, value['context_operation_id'], stage) > stage:
            return
        with core.store.connect() as c:
            current = c.execute('SELECT epoch,generation,status FROM jobs WHERE id=?', (job['id'],)).fetchone()
            if (not current or current['epoch'] != job['epoch']
                    or current['generation'] != job['generation']
                    or current['status'] != 'running'):
                raise AIError('AI_CANCELLED', '这轮作业已失效，未推进旧检查点。')
        check()
        progress.compact_context()


def compact_stage(core, job, value, thread, stage, gateway, workspace, progress,
                  reader_factory, cancel, stop, clock, wait):
    """Prepare/claim once, then observe until completion, cancel, stop or timeout."""
    from .native_desktop import _record, _journal, _stored_turn, _interrupt, _uuid, ServiceDetached

    observed = None
    deadline = clock() + COMPACTION_TIMEOUT

    def check():
        if stop.is_set():
            raise ServiceDetached()
        if cancel.is_set():
            if observed is not None and not _finished(observed):
                try:
                    _interrupt(gateway, thread, observed['id'])
                except Exception:
                    pass
            raise AIError('AI_CANCELLED', '本轮已取消，检查点仍保留。')
        if clock() >= deadline:
            raise AIError('AI_COMPACTION_PENDING', '压缩完成状态尚未核实；检查点、读取额度和原任务已保留，未重复发送。')

    check()
    if type(stage) is not int or stage < 0:
        raise AIError('AI_COMPACTION_PENDING', '上下文阶段标识无效。')
    if _current_stage(progress, value.get('context_operation_id'), stage) > stage:
        return
    _uuid(thread)
    baseline = _record(core, job, 'model-stage-' + str(stage))
    manual_baseline = baseline is None
    if baseline is None:
        # A manual desktop turn has no software model-dispatch journal. Its
        # identity was instead persisted from trusted MCP caller metadata. Never
        # substitute the provider's latest turn or override an uncertain journal.
        with core.store.connect() as c:
            try:
                row = c.execute("""SELECT o.provider_thread_id,o.provider_turn_id
                    FROM conversation_operations o JOIN jobs j ON j.id=o.job_id
                    WHERE o.job_id=? AND o.epoch=? AND j.epoch=? AND j.generation=?
                      AND j.status='running' AND o.provider_thread_id=?
                      AND json_extract(j.input,'$.desktop_transport')='native_ipc_v1'""",
                    (job['id'], job['epoch'], job['epoch'], job['generation'], thread)).fetchone()
                if row is not None and core.store.meta(c, 'epoch') == job['epoch']:
                    baseline = dict(row)
            except sqlite3.DatabaseError:
                # Incomplete migrations cannot justify a guessed model baseline.
                baseline = None
    if (not baseline or baseline.get('provider_thread_id') != thread
            or not baseline.get('provider_turn_id')):
        raise AIError('AI_COMPACTION_PENDING', '上一轮模型任务的标识尚未核实，未发起压缩或重置检查点。')
    baseline_id = _uuid(baseline['provider_turn_id'])
    intent = _journal(core, journal.prepare, epoch=job['epoch'], job_id=job['id'],
        step='compact-stage-' + str(stage), method=METHOD, payload={'conversationId': thread})
    if intent.get('provider_thread_id') not in (None, thread):
        raise AIError('AI_BINDING_CONFLICT', '压缩记录属于另一条讨论，未接管其他任务。')
    known_turn = intent.get('provider_turn_id')
    if known_turn:
        _uuid(known_turn)
        if known_turn == baseline_id:
            raise AIError('AI_COMPACTION_PENDING', '压缩记录不能复用上一轮模型回合的标识。')

    reader = None
    last_read = -float('inf')
    try:
        while True:
            check()
            if _current_stage(progress, value['context_operation_id'], stage) > stage:
                return
            if clock() - last_read < READ_INTERVAL:
                wait(min(.25, max(0, deadline - clock())))
                continue
            last_read = clock()
            try:
                if reader is None:
                    reader = reader_factory(workspace, timeout=min(8, max(.1, deadline - clock())))
                    reader.__enter__()

                if intent['state'] == 'prepared':
                    # The baseline must actually be terminal before requesting
                    # compaction; an active model is not a safe stage boundary.
                    prior = _stored_turn(reader, thread, baseline_id)
                    if prior is None or not _finished(_turn(prior)):
                        raise _HistoryIncomplete()
                    check()
                    progress({'phase': 'compacting'})
                    claimed = _journal(core, journal.claim, epoch=job['epoch'], operation_id=intent['operation_id'])
                    if claimed is not None:
                        intent = claimed
                        if stop.is_set() or cancel.is_set():
                            _journal(core, journal.confirm_not_sent, epoch=job['epoch'],
                                operation_id=intent['operation_id'], claim_id=intent['claim_id'],
                                evidence='transport_not_started')
                            check()
                        try:
                            reply = gateway.request_owner(thread, METHOD, {'conversationId': thread}, version=1)
                            result = reply.get('result') if type(reply) is dict else None
                            # Native IPC wraps the follower response as
                            # result={method, result:{ok:true}}. Accept the
                            # historical flattened adapter shape as well.
                            acknowledgement = result.get('result', result) if type(result) is dict else None
                            if (type(reply) is not dict or reply.get('resultType') != 'success'
                                    or type(acknowledgement) is not dict or acknowledgement.get('ok') is not True):
                                raise _HistoryIncomplete()
                            intent = _journal(core, journal.acknowledge, epoch=job['epoch'],
                                operation_id=intent['operation_id'], claim_id=intent['claim_id'],
                                provider_thread_id=thread)
                        except Exception:
                            intent = _journal(core, journal.mark_uncertain, epoch=job['epoch'],
                                operation_id=intent['operation_id'], claim_id=intent['claim_id'],
                                reason='unknown', provider_thread_id=thread)
                    else:
                        intent = _record(core, job, 'compact-stage-' + str(stage))
                        if intent is None:
                            raise _HistoryIncomplete()
                        known_turn = intent.get('provider_turn_id')

                check()
                if known_turn is not None:
                    candidate = _stored_turn(reader, thread, known_turn)
                    if candidate is not None:
                        candidate = _turn(candidate)
                        if not _has_compaction(reader, thread, candidate, check):
                            candidate = None
                else:
                    candidates = []
                    for candidate in _after_baseline(reader, thread, baseline_id, check):
                        if _has_compaction(reader, thread, candidate, check):
                            candidates.append(candidate)
                    if len(candidates) > 1:
                        raise AIError('AI_COMPACTION_PENDING', '上一轮之后出现多条压缩回合，尚不能唯一对应这次接续；检查点已保留。')
                    candidate = candidates[0] if candidates else None
                if candidate is not None:
                    observed = candidate
                    known_turn = candidate['id']
                    intent = _journal(core, journal.acknowledge, epoch=job['epoch'],
                        operation_id=intent['operation_id'], claim_id=intent['claim_id'],
                        provider_thread_id=thread, provider_turn_id=known_turn)
                    check()
                    # Keep the trusted manual model baseline intact. Its
                    # compaction turn is already durable in compact-stage.
                    progress({'phase': 'compacting',
                        **({} if manual_baseline else {'provider_turn_id': known_turn})})
                    if candidate['status'] == 'completed' and _finished(candidate):
                        _advance(core, job, value, stage, progress, check)
                        return
                    if candidate['status'] in {'failed', 'interrupted'} and _finished(candidate):
                        raise AIError('AI_COMPACTION_FAILED', '原任务的上下文压缩未完成，已保留检查点与读取额度。')
            except (ServiceDetached, AIError):
                raise
            except Exception:
                # Unknown dispatches and history outages are reconciled by
                # reading only. Neither reconnect nor timeout replays compaction.
                if reader is not None:
                    try:
                        reader.close()
                    except Exception:
                        pass
                    reader = None
            wait(min(.25, max(0, deadline - clock())))
    finally:
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass
