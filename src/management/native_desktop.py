"""Send once through the current desktop owner; reconnect by observing that turn.

The desktop remains the only model runner. The official API helper creates an
empty, durable thread or reads stored history, never resumes an owned writer.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
import uuid

from . import desktop_dispatch as journal
from .ai import AIError, INSTRUCTIONS, _image_inputs, _discussion_title, _settings
from .ai_commands import CANDIDATE_COMMANDS
from .storage import encode, now

CONTRACT = 'desktop_mcp_v3'
START = 'thread-follower-start-turn'
TERMINAL = {'completed', 'failed', 'interrupted', 'cancelled'}


class ServiceDetached(RuntimeError):
    """Service shutdown is not a user request to interrupt a desktop turn."""


def instructions():
    from .discussion_mcp import instructions as original
    return original(INSTRUCTIONS).replace('personal_management_discussion', 'personal_management')


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise AIError('AI_BINDING_CONFLICT', '已保存的 Codex 任务标识无效，未创建替代会话。') from None
    return value


def _record(core, job, step):
    with core.store.connect() as c:
        row = c.execute('SELECT * FROM desktop_dispatches WHERE epoch=? AND job_id=? AND step=?',
                        (job['epoch'], job['id'], step)).fetchone()
        return dict(row) if row else None


def _journal(core, operation, **kwargs):
    with core.store.connect() as c:
        return operation(c, **kwargs)


def _mark_native(core, job):
    with core.store.lock, core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = core._job(c, job['id'])
        if row['epoch'] != job['epoch'] or row['generation'] != job['generation'] or row['status'] != 'running':
            raise AIError('AI_CANCELLED', '这轮已经结束或被取消。')
        value = json.loads(row['input'])
        value['desktop_transport'] = 'native_ipc_v1'
        c.execute('UPDATE jobs SET input=? WHERE id=?', (encode(value), job['id']))
        c.commit()


def _step(core, value):
    with core.store.connect() as c:
        row = c.execute('SELECT stage,phase FROM context_operations WHERE id=?',
                        (value.get('context_operation_id'),)).fetchone()
    return (int(row['stage']), row['phase']) if row else (0, None)


def _payload(thread, value, workspace, model, job, stage):
    message = value['prompt'] if stage == 0 else (
        '[软件自动接续原请求，无新增用户授权] 原操作 ' + value['context_operation_id'] +
        '。请用 operation_status 从检查点继续，勿重新 begin_discussion。')
    request = {'threadId': thread, 'clientUserMessageId': str(uuid.uuid5(uuid.NAMESPACE_URL,
               'personal-management/' + job['epoch'] + '/' + job['id'] + '/stage/' + str(stage))),
               # Desktop projection sees this before app-server defaults apply.
               'input': [{'type': 'text', 'text': message, 'text_elements': []}, *_image_inputs(value)],
               'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'readOnly', 'networkAccess': False},
               'cwd': str(workspace), 'summary': 'none'}
    if model:
        request['model'] = model
    return {'conversationId': thread, 'turnStart': {'request': request,
        'context': {'inheritThreadSettings': True, 'useAppServerPermissionDefault': False,
                    'usePermissionSelection': False}}}


def state_turns(state):
    """Bounded extraction from the presentation stream; never treat idle as done."""
    if not isinstance(state, dict):
        return []
    pending = [state.get(key) for key in ('turns', 'turnHistory', 'paginatedHistory')]
    found, seen, count = [], set(), 0
    while pending and count < 10000:
        current = pending.pop()
        count += 1
        if isinstance(current, dict):
            ident = current.get('turnId')
            if isinstance(ident, str) and ident not in seen:
                seen.add(ident)
                found.append(current)
            else:
                pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(reversed(current))
    return found


def _correlated_turn(state, client_message_id):
    matched = [t for t in state_turns(state)
               if (t.get('params') or {}).get('clientUserMessageId') == client_message_id]
    return matched[0] if len(matched) == 1 else None


def _status(turn):
    status = (turn or {}).get('status')
    return status.get('type') if isinstance(status, dict) else status


def terminal_receipt(turn):
    # A separate history reader can reconstruct an unfinished rollout as
    # "failed". Only its persisted completion timestamp proves a terminal turn.
    stamp = (turn or {}).get('completedAt')
    return _status(turn) in TERMINAL and type(stamp) in {int, float} and stamp > 0


def _busy(state):
    if not isinstance(state, dict):
        return None
    runtime = state.get('threadRuntimeStatus') or {}
    if runtime.get('type') == 'active' or any(_status(t) == 'inProgress' for t in state_turns(state)):
        return True
    if runtime.get('type') == 'idle':
        return False
    return None


def _provider(thread, workspace, turn=None):
    return {'kind': 'codex_app_server', 'thread_id': thread, 'turn_id': turn,
            'contract': CONTRACT, 'project_path': str(workspace), 'recovery': 'resumed',
            'transport': 'native_ipc_v1'}


def recover(core, c):
    """Reattach pending native jobs on service restart without incrementing generation."""
    epoch = core.store.meta(c, 'epoch')
    c.execute("""UPDATE jobs SET status='queued',updated_at=? WHERE status='running' AND epoch=?
        AND json_extract(input,'$.desktop_transport')='native_ipc_v1'
        AND coalesce(json_extract(input,'$.execution_owner'),'software')!='desktop'""", (now(), epoch))


def _stored_turn(reader, thread, turn_id):
    cursor, seen = None, set()
    for _ in range(40):
        params = {'threadId': thread, 'limit': 50, 'sortDirection': 'desc', 'itemsView': 'notLoaded'}
        if cursor:
            params['cursor'] = cursor
        page = reader.request('thread/turns/list', params)
        for turn in page.get('data', []):
            if turn.get('id') == turn_id:
                return turn
        cursor = page.get('nextCursor')
        if not cursor:
            return None
        if not isinstance(cursor, str) or cursor in seen:
            break
        seen.add(cursor)
    raise AIError('AI_HISTORY_LIMIT', '尚未完整核对这轮的已保存状态，原任务已保留。')


def _interrupt(gateway, thread, turn_id, *, mode='user-stop'):
    """Expected identity is checked by the owner; never interrupt an arbitrary latest turn."""
    if turn_id:
        return gateway.request_owner(thread, 'thread-follower-interrupt-turn',
            {'conversationId': thread, 'mode': mode, 'expectedTurnId': turn_id}, version=4)


def generate(core, job, value, settings, cancel, stop, progress, *,
             seed=None, reader_factory=None, clock=time.monotonic, wait=None):
    """One user action, one durable dispatch; disconnection only changes observation."""
    from .desktop_seed import create_empty_thread, PlainAppServer
    from .codex_project import project_binding, ensure_project, require_ready_project
    seed, reader_factory = seed or create_empty_thread, reader_factory or PlainAppServer
    wait = wait or (lambda seconds: stop.wait(seconds))
    config = _settings(settings)
    if not isinstance(value.get('prompt'), str) or not value['prompt'].strip():
        raise AIError('AI_INPUT_INVALID', '请输入要处理的内容。')
    if not set(value.get('allowed_commands', [])).issubset(CANDIDATE_COMMANDS):
        raise AIError('AI_SCOPE_ERROR', '这轮包含不支持的业务操作。')
    workspace = (core.root / 'Codex事务助手').resolve()
    previous = value.get('provider_thread_id')
    if previous:
        _uuid(previous)
        if value.get('provider_project_path') and os.path.normcase(str(Path(value['provider_project_path']).resolve())) != os.path.normcase(str(workspace)):
            raise AIError('AI_BINDING_CONFLICT', '原任务属于其他数据空间，未改换关联。')
    if stop.is_set():
        raise ServiceDetached()
    if cancel.is_set():
        raise AIError('AI_CANCELLED', '本轮已取消。')
    _mark_native(core, job)
    project = project_binding(workspace)
    if not project or project.get('instructions_verified') is not True:
        project = require_ready_project(ensure_project(core.root, config), workspace)
    gateway = core.desktop_gateway
    progress({'phase': 'connecting'})
    connection = gateway.status(force=True)
    if not connection.get('ready'):
        connection = gateway.connect()
    if not connection.get('ready'):
        raise AIError('AI_SHARED_NOT_READY', connection.get('message', 'Codex 连接尚未就绪。'))
    # Read the current durable binding as a service may have stopped after seed ACK.
    with core.store.connect() as c:
        prior = c.execute('SELECT * FROM conversation_operations WHERE job_id=? AND epoch=?',
                          (job['id'], job['epoch'])).fetchone()
    thread = previous or (prior['provider_thread_id'] if prior else None)
    if not thread:
        seed_payload = {'workspace': str(workspace), 'project_id': project['project_id'],
                        'model': config.get('model') or None}
        intent = _journal(core, journal.prepare, epoch=job['epoch'], job_id=job['id'], step='seed',
                          method='thread/start', payload=seed_payload)
        claimed = _journal(core, journal.claim, epoch=job['epoch'], operation_id=intent['operation_id'])
        if not claimed:
            if intent.get('provider_thread_id'):
                thread = _uuid(intent['provider_thread_id'])
            else:
                raise AIError('AI_DELIVERY_UNKNOWN', '上次创建任务的结果尚未核实，未另建同名任务。')
        else:
            try:
                progress({'phase': 'creating_thread'})
            except Exception:
                _journal(core, journal.confirm_not_sent, epoch=job['epoch'], operation_id=intent['operation_id'],
                         claim_id=claimed['claim_id'], evidence='transport_not_started')
                raise
            def created(ident):
                nonlocal thread
                thread = _uuid(ident)
                _journal(core, journal.acknowledge, epoch=job['epoch'], operation_id=intent['operation_id'],
                         claim_id=claimed['claim_id'], provider_thread_id=thread)
                progress({'phase': 'thread_ready', 'provider_thread_id': thread,
                          'provider_contract': CONTRACT, 'recovery': 'new'})
            try:
                seed(workspace, project_id=project['project_id'], model=config.get('model') or None,
                     base_instructions=instructions(), on_created=created, title=_discussion_title(value))
            except Exception:
                if not thread:
                    _journal(core, journal.mark_uncertain, epoch=job['epoch'], operation_id=intent['operation_id'],
                             claim_id=claimed['claim_id'], reason='unknown')
                raise
    thread = _uuid(thread)
    progress({'phase': 'thread_ready', 'provider_thread_id': thread,
              'provider_contract': CONTRACT, 'recovery': 'resumed' if previous else 'new'})
    gateway.owner(thread, open_if_missing=True)
    gateway.subscribe(thread)
    from .desktop_reconcile import reconcile_pending
    reconcile_pending(core, job, thread, gateway, workspace, reader_factory)
    previous_operation = value.get('previous_operation') or {}
    if previous_operation.get('job_id') not in {None, job['id']} and previous_operation.get('phase') in {
            'sending', 'waiting_model', 'reasoning', 'receiving', 'validating', 'provider_retry', 'compacting'}:
        known_turn = previous_operation.get('provider_turn_id')
        if not known_turn:
            with core.store.connect() as c:
                receipt = c.execute("""SELECT provider_turn_id FROM desktop_dispatches
                    WHERE job_id=? AND epoch=? AND provider_thread_id=? AND method=?
                    AND state='acknowledged' AND provider_turn_id IS NOT NULL
                    ORDER BY updated_at DESC LIMIT 1""", (previous_operation['job_id'],
                    job['epoch'], thread, START)).fetchone()
            known_turn = receipt['provider_turn_id'] if receipt else None
        if not known_turn:
            raise AIError('AI_DELIVERY_UNKNOWN', '前一轮的发送结果仍待核对，未向原任务再发一轮。')
        with reader_factory(workspace, timeout=8) as history:
            known = _stored_turn(history, thread, known_turn)
        if not terminal_receipt(known):
            raise AIError('AI_CONVERSATION_BUSY', '前一轮尚未确认结束，请稍后继续原事项。')
        progress({'phase': 'reconciled'})
    stage, phase = _step(core, value)
    # A checkpoint is handled by compact_stage before the next stage can dispatch.
    if phase == 'checkpointed':
        compact_stage(core, job, value, thread, stage, gateway, workspace, progress,
                      reader_factory, cancel, stop, clock, wait)
        stage, phase = _step(core, value)
    step = 'model-stage-' + str(stage)
    payload = _payload(thread, value, workspace, config.get('model'), job, stage)
    existing = _record(core, job, step)
    if existing and existing['state'] != 'prepared':
        # Settings or continuation bookkeeping may change while disconnected;
        # they are never permission to modify or resend an existing intent.
        intent = existing
    else:
        intent = _journal(core, journal.prepare, epoch=job['epoch'], job_id=job['id'], step=step,
                          method=START, payload=payload)
    turn_id = intent.get('provider_turn_id')
    if intent['state'] == 'prepared':
        idle_deadline = clock() + 15
        while True:
            if stop.is_set():
                raise ServiceDetached()
            if cancel.is_set():
                raise AIError('AI_CANCELLED', '本轮已取消，尚未发送。')
            busy = _busy(gateway.snapshot(thread, timeout=1))
            if busy is False:
                break
            if clock() >= idle_deadline:
                raise AIError('AI_CONVERSATION_BUSY', 'Codex 正在处理原事项或尚未确认空闲；这条消息尚未发送。')
            wait(.25)
    claimed = _journal(core, journal.claim, epoch=job['epoch'], operation_id=intent['operation_id'])
    if claimed:
        try:
            if stop.is_set():
                raise ServiceDetached()
            if cancel.is_set():
                raise AIError('AI_CANCELLED', '本轮已取消，尚未发送。')
            progress({'phase': 'sending'})
        except Exception:
            _journal(core, journal.confirm_not_sent, epoch=job['epoch'], operation_id=intent['operation_id'],
                     claim_id=claimed['claim_id'], evidence='transport_not_started')
            raise
        try:
            reply = gateway.request_owner(thread, START, payload, version=2)
            if reply.get('resultType') != 'success':
                raise AIError('AI_DELIVERY_UNKNOWN', 'Codex 未确认这轮发送结果，正在核对原任务。')
            turn_id = _uuid(reply['result']['result']['turn']['id'])
            intent = _journal(core, journal.acknowledge, epoch=job['epoch'], operation_id=intent['operation_id'],
                              claim_id=claimed['claim_id'], provider_thread_id=thread, provider_turn_id=turn_id)
        except Exception as error:
            from .codex_ipc import IpcError
            if isinstance(error, IpcError) and not error.outcome_unknown:
                _journal(core, journal.confirm_not_sent, epoch=job['epoch'], operation_id=intent['operation_id'],
                         claim_id=claimed['claim_id'], evidence='request_not_written')
                raise AIError('AI_DISCONNECTED', '连接在发送前断开；这条消息尚未送达，正在恢复连接。') from error
            intent = _journal(core, journal.mark_uncertain, epoch=job['epoch'], operation_id=intent['operation_id'],
                              claim_id=claimed['claim_id'], reason='unknown', provider_thread_id=thread)
            # Continue with read-only reconciliation; the request is never repeated.
    deadline = clock() + min(900, max(10, float(config.get('timeout_seconds', 180))))
    last_read = -float('inf')
    forced_checkpoint = False
    reader = None
    try:
        while True:
            if stop.is_set():
                raise ServiceDetached()
            if cancel.is_set():
                if not turn_id:
                    raise AIError('AI_CANCEL_DELIVERY_UNKNOWN', '已停止本地等待；Codex 是否收到这轮仍待核对，未声称远端已取消。')
                _interrupt(gateway, thread, turn_id)
                raise AIError('AI_CANCELLED', '本轮已取消。')
            candidate = progress.candidate_result()
            if clock() >= deadline:
                raise AIError('AI_TIMEOUT' if turn_id else 'AI_DELIVERY_UNKNOWN',
                              '等待这轮回传已超时；原任务和发送记录已保留，未重复发送。')
            state = None
            try:
                state = gateway.snapshot(thread, timeout=1)
            except Exception:
                pass
            if not turn_id:
                match = _correlated_turn(state, payload['turnStart']['request']['clientUserMessageId'])
                if match:
                    turn_id = _uuid(match['turnId'])
                    intent = _journal(core, journal.acknowledge, epoch=job['epoch'], operation_id=intent['operation_id'],
                                      claim_id=intent['claim_id'], provider_thread_id=thread, provider_turn_id=turn_id)
            if turn_id:
                progress({'phase': 'waiting_model', 'provider_turn_id': turn_id})
                context = progress.context_status()
                if context and not forced_checkpoint and (context.get('phase') == 'budget_stop' or context.get('tool_calls', 0) >= 128):
                    _interrupt(gateway, thread, turn_id, mode='system')
                    forced_checkpoint = True
                observed = next((t for t in state_turns(state) if t['turnId'] == turn_id), None)
                # Only an exact turn's terminal status proves completion. A bare idle
                # desktop, missing streamed history or a closed app does not.
                if _status(observed) in TERMINAL or clock() - last_read >= 5:
                    last_read = clock()
                    try:
                        if reader is None:
                            reader = reader_factory(workspace, timeout=8)
                            reader.__enter__()
                        stored = _stored_turn(reader, thread, turn_id)
                    except Exception:
                        stored = None
                        if reader is not None:
                            reader.close()
                            reader = None
                    if terminal_receipt(stored) or (observed and _status(observed) in TERMINAL):
                        settled = stored if terminal_receipt(stored) else observed
                        candidate = progress.candidate_result()
                        if candidate:
                            return {**candidate, 'provider': _provider(thread, workspace, turn_id)}
                        if context and (context.get('phase') in {'checkpointed', 'budget_stop'} or forced_checkpoint):
                            progress({'phase': 'checkpointed'})
                            return {'context_continuation': True, 'actions': [],
                                    'summary': '本批进度已保存，继续原任务。',
                                    'provider': _provider(thread, workspace, turn_id)}
                        raise AIError('AI_EMPTY_OUTPUT' if _status(settled) == 'completed' else 'AI_GENERATION_FAILED',
                                      'Codex 这轮已结束，但未通过业务接口返回可核对的候选。原讨论已保留。')
            wait(.25)
    finally:
        if reader is not None:
            reader.close()


def compact_stage(core, job, value, thread, stage, gateway, workspace, progress,
                  reader_factory, cancel, stop, clock, wait):
    """A checkpoint never becomes a new stage on a compaction ACK alone."""
    from .desktop_compaction import compact_stage as compact
    return compact(core, job, value, thread, stage, gateway, workspace, progress,
                   reader_factory, cancel, stop, clock, wait)
