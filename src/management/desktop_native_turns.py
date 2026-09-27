"""Reconcile native desktop-owned jobs from their exact persisted turn receipt.

The MCP transport records thread/turn identity before this observer runs. No
desktop idle state, latest-turn guess, shim, or model dispatch is used here.
"""
from __future__ import annotations

import json
import time
import uuid

from .desktop_seed import PlainAppServer
from .native_desktop import terminal_receipt
from .storage import encode, now

READ_BUDGET_SECONDS = 15
MAX_PAGES = 8


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _binding(core, c, expected):
    """Used before reading and again inside the committing transaction."""
    row = c.execute('SELECT * FROM jobs WHERE id=?', (expected['id'],)).fetchone()
    if (not row or row['status'] != 'running' or row['epoch'] != expected['epoch']
            or row['generation'] != expected['generation']
            or row['epoch'] != core.store.meta(c, 'epoch')):
        return None
    current = dict(row)
    value = json.loads(current['input'])
    if value.get('desktop_transport') != 'native_ipc_v1':
        return None
    op = c.execute('SELECT * FROM conversation_operations WHERE job_id=? AND epoch=?',
                   (current['id'], current['epoch'])).fetchone()
    if (not op or not _uuid(op['provider_thread_id']) or not _uuid(op['provider_turn_id'])
            or op['provider_generation'] != current['generation']):
        return None
    if value.get('execution_owner') != 'desktop':
        if (not op['pending_terminal'] or not op['candidate']
                or current['id'] in core.cancel_events):
            return None  # Never compete with a live software worker.
    if value.get('conversation_id') is None:
        if value.get('execution_owner') == 'desktop' or op['conversation_id'] != 'operation:'+str(value.get('context_operation_id')):
            return None
        context = c.execute('SELECT job_id,epoch FROM context_operations WHERE id=?',
                            (value.get('context_operation_id'),)).fetchone()
        if not context or context['job_id'] != current['id'] or context['epoch'] != current['epoch']:
            return None
        return current, value, dict(op)
    if op['conversation_id'] != value['conversation_id']:
        return None
    conversation = c.execute('SELECT active_job_id,provider_thread_id FROM conversations WHERE id=?',
                             (op['conversation_id'],)).fetchone()
    if (not conversation or conversation['active_job_id'] != current['id']
            or conversation['provider_thread_id'] != op['provider_thread_id']):
        return None
    return current, value, dict(op)


def _read_exact(reader, thread_id, turn_id, deadline):
    cursor, seen_cursors, seen_turns = None, set(), set()
    for _ in range(MAX_PAGES):
        if time.monotonic() >= deadline:
            return None
        params = {'threadId': thread_id, 'limit': 50, 'sortDirection': 'desc', 'itemsView': 'notLoaded'}
        if cursor is not None:
            params['cursor'] = cursor
        page = reader.request('thread/turns/list', params)
        if type(page) is not dict or type(page.get('data')) is not list or len(page['data']) > 500:
            return None
        match = None
        for turn in page['data']:
            if type(turn) is not dict or not _uuid(turn.get('id')) or turn['id'] in seen_turns:
                return None
            seen_turns.add(turn['id'])
            if turn['id'] == turn_id:
                match = turn
        if match is not None:
            return match
        cursor = page.get('nextCursor')
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not cursor or len(cursor) > 8192 or cursor in seen_cursors:
            return None
        seen_cursors.add(cursor)
    return None


def check_native_turns(core, jobs):
    """Observe a bounded batch supplied by the coordinator; uncertainty is inert."""
    from . import conversations, conversation_progress
    from .session_coordinator import get_candidate, complete_candidate, settle
    from .context_driver import requeue

    reader = None
    deadline = time.monotonic() + READ_BUDGET_SECONDS
    try:
        for expected in jobs:
            if time.monotonic() >= deadline:
                break
            try:
                with core.store.connect() as c:
                    bound = _binding(core, c, expected)
                if bound is None:
                    continue
                _, _, op = bound
                if reader is None:
                    reader = PlainAppServer(core.root / 'Codex事务助手', timeout=3)
                    reader.__enter__()
                receipt = _read_exact(reader, op['provider_thread_id'], op['provider_turn_id'], deadline)
                if receipt is None or not terminal_receipt(receipt):
                    continue
                with core.store.lock, core.store.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    try:
                        current_binding = _binding(core, c, expected)
                        if current_binding is None:
                            c.rollback()
                            continue
                        current, value, current_op = current_binding
                        if (current_op['provider_thread_id'] != op['provider_thread_id']
                                or current_op['provider_turn_id'] != op['provider_turn_id']):
                            c.rollback()
                            continue
                        candidate = get_candidate(c, current['id'])
                        if candidate:
                            complete_candidate(core, c, current, candidate)
                        else:
                            context = c.execute('SELECT phase FROM context_operations WHERE id=? AND epoch=? AND job_id=?',
                                (value.get('context_operation_id'), current['epoch'], current['id'])).fetchone()
                            if context and context['phase'] in {'checkpointed', 'budget_stop'}:
                                # requeue preserves job/generation/context identity
                                # and hands the same job back to the software worker.
                                if not requeue(core, c, current):
                                    c.rollback()
                                    continue
                            else:
                                error = {'code': 'discussion_no_candidate',
                                    'message': 'Codex 这一轮已结束，但未返回可保存候选。原会话保留，可继续补充。'}
                                c.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?",
                                    (encode(error), now(), current['id']))
                                conversations.update_job(core, c, current, 'failed', error)
                                settle(c, current['id'], 'reconciled', error)
                                conversation_progress.finish(c, current['id'])
                        c.commit()
                    except Exception:
                        c.rollback()
                        raise
            except Exception:
                # A missing provider, partial rollout, or raced binding is not
                # evidence of business failure. No model is retried here.
                if reader is not None:
                    try:
                        reader.close()
                    except Exception:
                        pass
                    reader = None
    finally:
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass
