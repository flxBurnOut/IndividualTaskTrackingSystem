"""Synthetic provider history with real durable SQLite dispatch records."""
from __future__ import annotations

import copy
import json
import threading
from types import SimpleNamespace
import uuid

import pytest

from management import context_schema, desktop_compaction as compact, desktop_dispatch as journal
from management.ai import AIError
from management.native_desktop import ServiceDetached, _record
from management.storage import Store, now


THREAD, BASE, COMPACT, OTHER = [str(uuid.uuid4()) for _ in range(4)]


def turn(ident, status='completed'):
    return {'id': ident, 'status': status, 'items': [], 'itemsView': 'notLoaded',
            'completedAt': None if status == 'inProgress' else 1800000000}


class Progress:
    def __init__(self, core, identifier):
        self.core, self.identifier = core, identifier
        self.resets = 0
        self.events = []
        self.before_reset = None

    def context_status(self):
        with self.core.store.connect() as c:
            row = c.execute('SELECT * FROM context_operations WHERE id=?', (self.identifier,)).fetchone()
            return {**dict(row), 'operation_id': self.identifier}

    def __call__(self, event):
        self.events.append(copy.deepcopy(event))

    def compact_context(self):
        if self.before_reset:
            self.before_reset()
        with self.core.store.lock, self.core.store.connect() as c:
            c.execute("UPDATE context_operations SET stage=stage+1,phase='reading',delivered_bytes=0,tool_calls=0 WHERE id=?",
                      (self.identifier,))
        self.resets += 1


class Gateway:
    def __init__(self, space):
        self.space, self.calls = space, []
        self.compactions = 0
        self.disconnect_after_dispatch = False
        self.compaction_reply = {'resultType': 'success',
            'result': {'method': compact.METHOD, 'result': {'ok': True}}}

    def request_owner(self, thread, method, params, *, version):
        self.calls.append((thread, method, copy.deepcopy(params), version))
        if method == compact.METHOD:
            record = _record(self.space.core, self.space.job, 'compact-stage-0')
            assert record['state'] == 'sending' and record['attempt'] == 1
            assert record['claim_id']
            assert params == {'conversationId': THREAD} and version == 1
            self.compactions += 1
            if self.disconnect_after_dispatch:
                raise ConnectionError('Synthetic disconnect after bytes were sent')
            return copy.deepcopy(self.compaction_reply)
        return {'resultType': 'success', 'result': {'ok': True}}


class Readers:
    def __init__(self, space, mode='complete'):
        self.space, self.mode = space, mode
        self.calls, self.instances = [], []
        self.fail_reads = 0
        self.after_reads = 0
        self.on_items = None
        self.completed_at = 'default'

    def __call__(self, workspace, *, timeout):
        assert timeout <= 8
        reader = Reader(self)
        self.instances.append(reader)
        return reader

    def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        # The real installed App Server rejects items/list with -32601.
        assert method == 'thread/turns/list'
        assert params['threadId'] == THREAD
        if self.fail_reads:
            self.fail_reads -= 1
            raise ConnectionError('Synthetic read outage')
        dispatched = self.space.gateway.compactions > 0 or self.space.recovering
        if params['itemsView'] == 'full':
            assert params['limit'] == 10 and params['sortDirection'] == 'desc'
            if self.on_items:
                self.on_items()
            if self.mode == 'item_pages' and params.get('cursor') is None:
                return {'data': [], 'nextCursor': 'full-turn-page-2'}
            if self.mode == 'cursor_cycle':
                return {'data': [], 'nextCursor': 'repeat'}
            status = {'failed': 'failed', 'interrupted': 'interrupted', 'running': 'inProgress'}.get(self.mode, 'completed')
            if self.mode == 'lag' and self.after_reads <= 1:
                status = 'inProgress'
            identities = [OTHER] if self.mode == 'cross_turn_item' else [OTHER, COMPACT] if self.mode == 'multiple' else [COMPACT]
            data = []
            for identifier in identities:
                item = turn(identifier, status)
                item['itemsView'] = 'summary' if self.mode == 'full_not_loaded' else 'full'
                item['items'] = [{'id': 'synthetic-item-' + identifier,
                    'type': 'agentMessage' if self.mode == 'normal_turn' else 'contextCompaction'}]
                if self.completed_at != 'default':
                    item['completedAt'] = self.completed_at
                if self.mode == 'full_partial':
                    item['completedAt'] = None
                elif self.mode == 'full_failed':
                    item['status'] = 'failed'
                data.append(item)
            return {'data': data, 'nextCursor': None}
        if method == 'thread/turns/list':
            assert params['itemsView'] == 'notLoaded' and params['sortDirection'] == 'desc'
            if self.mode == 'baseline_missing':
                return {'data': [], 'nextCursor': None}
            if not dispatched:
                return {'data': [turn(BASE, 'inProgress' if self.mode == 'baseline_active' else 'completed')],
                        'nextCursor': None}
            self.after_reads += 1
            status = {'failed': 'failed', 'interrupted': 'interrupted', 'running': 'inProgress'}.get(self.mode, 'completed')
            if self.mode == 'lag':
                status = 'inProgress' if self.after_reads <= 1 else 'completed'
            if self.mode == 'ack_only':
                data = [turn(BASE)]
            elif self.mode == 'old_compaction':
                data = [turn(BASE), turn(COMPACT)]
            elif self.mode == 'multiple':
                data = [turn(OTHER), turn(COMPACT), turn(BASE)]
            else:
                data = [turn(COMPACT, status), turn(BASE)]
            if self.completed_at != 'default':
                for item in data:
                    if item['id'] == COMPACT:
                        item['completedAt'] = self.completed_at
            if self.mode == 'turn_pages':
                return {'data': [turn(BASE)] if params.get('cursor') else [turn(COMPACT)],
                        'nextCursor': None if params.get('cursor') else 'turn-page-2'}
            return {'data': data, 'nextCursor': None}


class Reader:
    def __init__(self, factory):
        self.factory = factory
        self.closed = False
    def __enter__(self):
        return self
    def request(self, method, params):
        return self.factory.request(method, params)
    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.time = 0
        self.on_wait = None
    def __call__(self):
        return self.time
    def wait(self, seconds):
        self.time += seconds
        if self.on_wait:
            self.on_wait()


@pytest.fixture
def space(tmp_path, monkeypatch):
    monkeypatch.setattr(compact, 'COMPACTION_TIMEOUT', 1.5)
    store = Store(tmp_path / 'data')
    core = SimpleNamespace(store=store)
    with store.connect() as c:
        context_schema.initialize(c)
        journal.initialize(c)
        epoch = store.meta(c, 'epoch')
        job_id, operation_id = str(uuid.uuid4()), str(uuid.uuid4())
        c.execute("""INSERT INTO jobs(id,kind,status,input,epoch,snapshot_revision,created_at,updated_at)
            VALUES (?,'ai','running','{}',?,0,?,?)""", (job_id, epoch, now(), now()))
        c.execute("""INSERT INTO context_operations(id,epoch,scope,goal,job_id,phase,stage,delivered_bytes,
            tool_calls,checkpoint,created_at,updated_at) VALUES (?,?,'{}','Synthetic',?,'checkpointed',0,
            12345,128,'{"summary":"preserve this checkpoint"}',?,?)""", (operation_id, epoch, job_id, now(), now()))
        job = dict(c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
    result = SimpleNamespace(core=core, job=job, value={'context_operation_id': operation_id},
        operation_id=operation_id, workspace=tmp_path, cancel=threading.Event(), stop=threading.Event(),
        clock=Clock(), recovering=False)
    result.progress = Progress(core, operation_id)
    result.gateway = Gateway(result)
    result.readers = Readers(result)
    with store.connect() as c:
        record = journal.prepare(c, epoch=epoch, job_id=job_id, step='model-stage-0',
            method='thread-follower-start-turn', payload={'conversationId': THREAD})
        claimed = journal.claim(c, epoch=epoch, operation_id=record['operation_id'])
        journal.acknowledge(c, epoch=epoch, operation_id=record['operation_id'], claim_id=claimed['claim_id'],
            provider_thread_id=THREAD, provider_turn_id=BASE)
    return result


def run(space, stage=0):
    return compact.compact_stage(space.core, space.job, space.value, THREAD, stage,
        space.gateway, space.workspace, space.progress, space.readers,
        space.cancel, space.stop, space.clock, space.clock.wait)


def context(space):
    with space.core.store.connect() as c:
        return dict(c.execute('SELECT * FROM context_operations WHERE id=?', (space.operation_id,)).fetchone())


def recover_intent(space, state, *, turn_id=None):
    with space.core.store.connect() as c:
        intent = journal.prepare(c, epoch=space.job['epoch'], job_id=space.job['id'],
            step='compact-stage-0', method=compact.METHOD, payload={'conversationId': THREAD})
        claimed = journal.claim(c, epoch=space.job['epoch'], operation_id=intent['operation_id'])
        key = dict(epoch=space.job['epoch'], operation_id=intent['operation_id'], claim_id=claimed['claim_id'])
        if state == 'uncertain':
            journal.mark_uncertain(c, **key, reason='connection_lost', provider_thread_id=THREAD,
                provider_turn_id=turn_id)
        elif state == 'acknowledged':
            journal.acknowledge(c, **key, provider_thread_id=THREAD, provider_turn_id=turn_id)
    space.recovering = True


def test_completed_new_compaction_advances_once_after_durable_intent_and_exact_item(space):
    before = context(space)
    run(space)
    after = context(space)
    assert space.progress.resets == 1 and after['stage'] == 1 and after['phase'] == 'reading'
    assert after['delivered_bytes'] == after['tool_calls'] == 0
    assert before['checkpoint'] == after['checkpoint']
    record = _record(space.core, space.job, 'compact-stage-0')
    assert record['state'] == 'acknowledged' and record['provider_turn_id'] == COMPACT
    assert record['attempt'] == 1 and space.gateway.compactions == 1
    assert all(reader.closed for reader in space.readers.instances)
    assert ('thread/turns/list', {'threadId': THREAD,
        'limit': 10, 'sortDirection': 'desc', 'itemsView': 'full'}) in space.readers.calls


@pytest.mark.parametrize('nested', [True, False])
def test_compaction_acknowledges_native_envelope_and_flat_compatibility_without_claiming_completion(space, nested):
    result = {'method': compact.METHOD, 'result': {'ok': True}} if nested else {'ok': True}
    space.gateway.compaction_reply = {'resultType': 'success', 'result': result}
    space.readers.mode = 'ack_only'
    before = context(space)
    with pytest.raises(AIError) as error:
        run(space)
    assert error.value.code == 'AI_COMPACTION_PENDING'
    record = _record(space.core, space.job, 'compact-stage-0')
    assert record['state'] == 'acknowledged' and record['provider_turn_id'] is None
    assert record['attempt'] == 1 and space.gateway.compactions == 1
    assert context(space) == before and space.progress.resets == 0


@pytest.mark.parametrize('result', [
    {'ok': False}, {'ok': 1}, {'result': {'ok': False}}, {'result': {'ok': 1}},
    {'ok': True, 'result': None}, {'result': {}},
])
def test_malformed_or_nontrue_compaction_ack_stays_uncertain_without_replaying(space, result):
    space.gateway.compaction_reply = {'resultType': 'success', 'result': result}
    space.readers.mode = 'ack_only'
    before = context(space)
    with pytest.raises(AIError):
        run(space)
    record = _record(space.core, space.job, 'compact-stage-0')
    assert record['state'] == 'uncertain' and record['provider_turn_id'] is None
    assert record['attempt'] == 1 and space.gateway.compactions == 1
    assert context(space) == before and space.progress.resets == 0


def test_full_view_marker_needs_no_unsupported_items_api(space):
    complete = turn(COMPACT)
    complete.update(itemsView='full', items=[{'id': 'full-marker', 'type': 'contextCompaction'}])
    assert compact._has_compaction(space.readers, THREAD, complete, lambda: None)
    assert space.readers.calls == []


def test_summary_marker_cannot_replace_missing_full_evidence(space):
    space.readers.mode = 'normal_turn'
    summary = turn(COMPACT)
    summary.update(itemsView='summary', items=[{'id': 'summary-marker', 'type': 'contextCompaction'}])
    assert not compact._has_compaction(space.readers, THREAD, summary, lambda: None)
    assert all(method == 'thread/turns/list' for method, _ in space.readers.calls)


def test_full_turn_paging_rejects_duplicate_identity_across_pages():
    class DuplicateReader:
        def __init__(self):
            self.calls = 0
        def request(self, method, params):
            assert method == 'thread/turns/list' and params['itemsView'] == 'full'
            self.calls += 1
            return {'data': [dict(turn(OTHER), itemsView='full')], 'nextCursor': 'page-' + str(self.calls)}
    reader = DuplicateReader()
    with pytest.raises(compact._HistoryIncomplete):
        compact._has_compaction(reader, THREAD, turn(COMPACT), lambda: None)
    assert reader.calls == 2


def test_full_turn_paging_is_bounded_when_target_never_arrives():
    class EndlessReader:
        def __init__(self):
            self.calls = 0
        def request(self, method, params):
            assert method == 'thread/turns/list' and params['itemsView'] == 'full'
            self.calls += 1
            return {'data': [dict(turn(str(uuid.UUID(int=self.calls))), itemsView='full')],
                    'nextCursor': 'page-' + str(self.calls)}
    reader = EndlessReader()
    with pytest.raises(compact._HistoryIncomplete):
        compact._has_compaction(reader, THREAD, turn(COMPACT), lambda: None)
    assert reader.calls == compact.MAX_TURN_PAGES


@pytest.mark.parametrize('mode', ['ack_only', 'normal_turn', 'old_compaction', 'cross_turn_item', 'cursor_cycle',
                                 'full_not_loaded', 'full_partial', 'full_failed'])
def test_ack_or_unrelated_history_cannot_reset_quota_and_timeout_keeps_checkpoint(space, mode):
    space.readers.mode = mode
    before = context(space)
    with pytest.raises(AIError) as error:
        run(space)
    assert error.value.code == 'AI_COMPACTION_PENDING'
    assert space.progress.resets == 0 and context(space) == before
    assert space.gateway.compactions == 1
    assert all(reader.closed for reader in space.readers.instances)


@pytest.mark.parametrize('state', ['sending', 'uncertain', 'acknowledged'])
def test_restart_reconciles_inflight_dispatch_without_sending_again(space, state):
    recover_intent(space, state)
    run(space)
    assert space.gateway.calls == [] and space.progress.resets == 1
    assert _record(space.core, space.job, 'compact-stage-0')['attempt'] == 1


def test_known_compaction_identity_is_read_exactly_on_resume(space):
    recover_intent(space, 'acknowledged', turn_id=COMPACT)
    run(space)
    assert space.gateway.calls == [] and space.progress.resets == 1
    assert all(params.get('turnId', COMPACT) == COMPACT for method, params in space.readers.calls)


def test_disconnect_after_actual_dispatch_is_reconciled_without_replay(space):
    space.gateway.disconnect_after_dispatch = True
    run(space)
    assert space.gateway.compactions == 1 and space.progress.resets == 1
    record = _record(space.core, space.job, 'compact-stage-0')
    assert record['provider_turn_id'] == COMPACT and record['state'] == 'acknowledged'


@pytest.mark.parametrize('mode', ['turn_pages', 'item_pages', 'lag'])
def test_paginated_or_delayed_completion_is_observed_before_advancing(space, mode):
    space.readers.mode = mode
    run(space)
    assert space.progress.resets == 1 and space.gateway.compactions == 1
    if mode == 'turn_pages':
        assert any(params.get('cursor') == 'turn-page-2' for _, params in space.readers.calls)
    if mode == 'item_pages':
        assert any(params.get('cursor') == 'full-turn-page-2' for _, params in space.readers.calls)


def test_two_new_compaction_turns_are_ambiguous_not_arbitrarily_selected(space):
    space.readers.mode = 'multiple'
    with pytest.raises(AIError) as error:
        run(space)
    assert error.value.code == 'AI_COMPACTION_PENDING' and space.progress.resets == 0
    assert _record(space.core, space.job, 'compact-stage-0')['provider_turn_id'] is None


@pytest.mark.parametrize('ended', [None, 0, -1, True, '1800000000', 1800000000.0, 1 << 63])
@pytest.mark.parametrize('mode', ['complete', 'failed'])
def test_partial_terminal_status_without_valid_completion_time_is_not_final(space, ended, mode):
    space.readers.mode = mode
    space.readers.completed_at = ended
    before = context(space)
    with pytest.raises(AIError) as error:
        run(space)
    assert error.value.code == 'AI_COMPACTION_PENDING'
    assert space.progress.resets == 0 and context(space) == before
    assert space.gateway.compactions == 1


@pytest.mark.parametrize('mode', ['failed', 'interrupted'])
def test_failed_compaction_keeps_checkpoint_and_never_dispatches_a_successor(space, mode):
    space.readers.mode = mode
    before = context(space)
    with pytest.raises(AIError) as error:
        run(space)
    assert error.value.code == 'AI_COMPACTION_FAILED'
    assert context(space) == before and space.progress.resets == 0
    with pytest.raises(AIError):
        run(space)
    assert space.gateway.compactions == 1


def test_cancel_interrupts_only_the_exact_observed_running_compaction(space):
    space.readers.mode = 'running'
    space.clock.on_wait = space.cancel.set
    with pytest.raises(AIError) as error:
        run(space)
    assert error.value.code == 'AI_CANCELLED'
    interruptions = [call for call in space.gateway.calls if call[1] == 'thread-follower-interrupt-turn']
    assert len(interruptions) == 1
    assert interruptions[0][2] == {'conversationId': THREAD, 'mode': 'user-stop', 'expectedTurnId': COMPACT}
    assert space.progress.resets == 0


def test_cancel_with_no_exact_compaction_identity_never_interrupts_latest_turn(space):
    space.readers.mode = 'ack_only'
    space.clock.on_wait = space.cancel.set
    with pytest.raises(AIError):
        run(space)
    assert [c[1] for c in space.gateway.calls] == [compact.METHOD]


def test_service_stop_detaches_without_interrupting_desktop_work(space):
    space.readers.mode = 'running'
    space.clock.on_wait = space.stop.set
    before = context(space)
    with pytest.raises(ServiceDetached):
        run(space)
    assert [c[1] for c in space.gateway.calls] == [compact.METHOD]
    assert context(space) == before and space.progress.resets == 0
    assert all(reader.closed for reader in space.readers.instances)


def test_already_advanced_stage_is_idempotent_and_never_sends_or_resets_twice(space):
    run(space)
    calls, reads = list(space.gateway.calls), list(space.readers.calls)
    run(space)
    assert space.progress.resets == 1
    assert space.gateway.calls == calls and space.readers.calls == reads


def test_stage_advanced_by_another_observer_before_reset_is_not_incremented_again(space):
    def advance_elsewhere():
        with space.core.store.connect() as c:
            c.execute("UPDATE context_operations SET stage=1,phase='reading',delivered_bytes=0,tool_calls=0 WHERE id=?",
                      (space.operation_id,))
    space.readers.on_items = advance_elsewhere
    run(space)
    assert context(space)['stage'] == 1 and space.progress.resets == 0


def test_changed_job_generation_cannot_advance_old_stage(space):
    def supersede():
        with space.core.store.connect() as c:
            c.execute('UPDATE jobs SET generation=generation+1 WHERE id=?', (space.job['id'],))
    space.readers.on_items = supersede
    with pytest.raises(AIError) as error:
        run(space)
    assert error.value.code == 'AI_CANCELLED'
    assert context(space)['stage'] == 0 and space.progress.resets == 0


def test_history_reconnect_performs_only_reads_and_dispatch_remains_once(space):
    space.readers.fail_reads = 1
    run(space)
    assert len(space.readers.instances) == 2 and space.gateway.compactions == 1
    assert all(method == 'thread/turns/list' for method, _ in space.readers.calls)


@pytest.mark.parametrize('mode', ['baseline_missing', 'baseline_active'])
def test_missing_or_active_baseline_does_not_dispatch_compaction(space, mode):
    space.readers.mode = mode
    with pytest.raises(AIError) as error:
        run(space)
    assert error.value.code == 'AI_COMPACTION_PENDING'
    assert space.gateway.calls == [] and space.progress.resets == 0
    assert _record(space.core, space.job, 'compact-stage-0')['state'] == 'prepared'


def test_no_durable_model_baseline_prevents_any_new_intent(space):
    with space.core.store.connect() as c:
        c.execute("DELETE FROM desktop_dispatches WHERE step='model-stage-0'")
    with pytest.raises(AIError):
        run(space)
    assert _record(space.core, space.job, 'compact-stage-0') is None
    assert space.gateway.calls == [] and space.readers.calls == []


def test_already_cancelled_operation_never_creates_or_sends_compaction(space):
    space.cancel.set()
    with pytest.raises(AIError) as error:
        run(space)
    assert error.value.code == 'AI_CANCELLED'
    assert _record(space.core, space.job, 'compact-stage-0') is None


def manual_baseline(space, *, epoch=None, thread=THREAD, native=True, remove_journal=True):
    with space.core.store.connect() as c:
        if remove_journal:
            c.execute("DELETE FROM desktop_dispatches WHERE step='model-stage-0'")
        c.execute('UPDATE jobs SET input=? WHERE id=?', (json.dumps({
            'desktop_transport': 'native_ipc_v1' if native else 'legacy',
            'context_operation_id': space.operation_id, 'execution_owner': 'software'}), space.job['id']))
        c.execute("""INSERT INTO conversation_operations(job_id,conversation_id,epoch,phase,
            provider_thread_id,provider_turn_id,project_path,contract,updated_at)
            VALUES (?,?,?,'waiting_model',?,?,?,'desktop_mcp_v3',?)""",
            (space.job['id'], 'operation:' + space.operation_id, epoch or space.job['epoch'],
             thread, BASE, str(space.workspace), now()))


def test_manual_native_checkpoint_uses_its_trusted_operation_turn_as_baseline(space):
    manual_baseline(space)
    run(space)
    assert space.progress.resets == 1 and space.gateway.compactions == 1
    assert context(space)['stage'] == 1


@pytest.mark.parametrize('wrong', ['epoch', 'thread', 'generation', 'transport'])
def test_manual_baseline_fallback_rejects_stale_or_foreign_identity(space, wrong):
    manual_baseline(space, epoch=str(uuid.uuid4()) if wrong == 'epoch' else None,
                    thread=OTHER if wrong == 'thread' else THREAD, native=wrong != 'transport')
    if wrong == 'generation':
        with space.core.store.connect() as c:
            c.execute('UPDATE jobs SET generation=generation+1 WHERE id=?', (space.job['id'],))
    with pytest.raises(AIError) as error:
        run(space)
    assert error.value.code == 'AI_COMPACTION_PENDING'
    assert space.gateway.calls == [] and space.progress.resets == 0


def test_manual_metadata_cannot_override_an_unconfirmed_software_journal(space):
    manual_baseline(space, remove_journal=False)
    with space.core.store.connect() as c:
        c.execute("UPDATE desktop_dispatches SET provider_turn_id=NULL,state='uncertain' WHERE step='model-stage-0'")
    with pytest.raises(AIError):
        run(space)
    assert space.gateway.calls == [] and space.progress.resets == 0


def test_manual_baseline_survives_compaction_progress_and_service_reattachment(space, monkeypatch):
    manual_baseline(space)
    publish = Progress.__call__
    def persist_like_publisher(instance, event):
        publish(instance, event)
        if event.get('provider_turn_id'):
            with space.core.store.connect() as c:
                c.execute('UPDATE conversation_operations SET provider_turn_id=? WHERE job_id=?',
                          (event['provider_turn_id'], space.job['id']))
    monkeypatch.setattr(Progress, '__call__', persist_like_publisher)
    space.readers.mode = 'running'
    space.clock.on_wait = space.stop.set
    with pytest.raises(ServiceDetached):
        run(space)
    assert _record(space.core, space.job, 'compact-stage-0')['provider_turn_id'] == COMPACT
    with space.core.store.connect() as c:
        assert c.execute('SELECT provider_turn_id FROM conversation_operations WHERE job_id=?',
                         (space.job['id'],)).fetchone()[0] == BASE
    space.stop.clear()
    space.clock.on_wait = None
    space.readers.mode = 'complete'
    run(space)
    assert space.gateway.compactions == 1 and space.progress.resets == 1
