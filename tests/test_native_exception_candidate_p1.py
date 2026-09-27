"""A failed software wait hands its candidate to a read-only exact-turn owner."""
import copy
import json
import threading
from types import SimpleNamespace

import pytest

from management.core import Core
from management import conversation_progress, desktop_dispatch, desktop_native_turns
from management import native_desktop as native, scheduler, session_coordinator as coordinator
from management.ai import AIError
from test_conversation_progress_v12 import command, sent


THREAD = '00000000-0000-4000-8000-000000000061'
TURN = '00000000-0000-4000-8000-000000000062'
OTHER = '00000000-0000-4000-8000-000000000063'


class OneIdleStop(threading.Event):
    def wait(self, timeout=None):
        self.set()
        return True


def snapshot(space):
    with space.core.store.connect() as c:
        job = space.core._job(c, space.job_id)
        value = json.loads(job['input'])
        conversation = c.execute('SELECT active_job_id,current_proposal_job_id FROM conversations WHERE id=?',
                                 (value.get('conversation_id'),)).fetchone()
        return {'job': job, 'conversation': dict(conversation) if conversation else None,
            'operation': dict(c.execute('SELECT * FROM conversation_operations WHERE job_id=?', (job['id'],)).fetchone()),
            'dispatches': [dict(r) for r in c.execute('SELECT * FROM desktop_dispatches')],
            'entities': c.execute('SELECT count(*) FROM entities').fetchone()[0],
            'messages': c.execute("SELECT count(*) FROM conversation_messages WHERE role='assistant'").fetchone()[0]}


@pytest.fixture
def build_space(tmp_path, monkeypatch):
    def make(*, actions=False, standalone=False, failure='AI_TIMEOUT'):
        core = Core(tmp_path / ('standalone' if standalone else 'conversation'))
        command(core, 'settings', {'settings': {'ai': {'enabled': True, 'execution_mode': 'desktop_shared'}}})
        made = (command(core, 'create_job', {'kind': 'ai', 'input': {'prompt': 'Synthetic standalone'}})
                if standalone else sent(core))
        space = SimpleNamespace(core=core, job_id=made['job']['id'], send_calls=0, history_calls=[], actions=actions,
            receipt={'id': TURN, 'status': 'inProgress', 'completedAt': None}, on_read=None)

        def provider(core, job, value, settings, cancel, stop, progress):
            native._mark_native(core, job)
            # Durable journal for exactly one synthetic send, no model call.
            with core.store.connect() as c:
                intent = desktop_dispatch.prepare(c, epoch=job['epoch'], job_id=job['id'], step='model-stage-0',
                    method=native.START, payload={'conversationId': THREAD, 'synthetic': True})
                claim = desktop_dispatch.claim(c, epoch=job['epoch'], operation_id=intent['operation_id'])
                assert claim is not None
                desktop_dispatch.acknowledge(c, epoch=job['epoch'], operation_id=intent['operation_id'],
                    claim_id=claim['claim_id'], provider_thread_id=THREAD, provider_turn_id=TURN)
            space.send_calls += 1
            progress({'phase': 'waiting_model', 'provider_thread_id': THREAD,
                'provider_turn_id': TURN, 'provider_contract': native.CONTRACT})
            begun = coordinator.handle_native(core, 'begin', {'provider_thread_id': THREAD, 'provider_turn_id': TURN,
                'text': value['prompt'], 'request_id': 'synthetic-once'})
            proposal = {'summary': 'Synthetic saved candidate', 'sources': [], 'unknowns': [], 'actions': [
                {'command': 'create', 'payload_json': json.dumps({'type': 'task', 'title': 'Synthetic proposed task'}),
                 'reason': 'Synthetic explicit request'}] if actions else []}
            coordinator.handle_native(core, 'submit', {'provider_thread_id': THREAD, 'provider_turn_id': TURN,
                'job_id': begun['job_id'], 'generation': begun['generation'], 'proposal': proposal})
            if failure == 'exception':
                raise RuntimeError('Synthetic interrupted observation')
            raise AIError(failure, 'Synthetic loss while actual turn still inProgress')

        with monkeypatch.context() as local:
            local.setattr(native, 'generate', provider)
            local.setattr(coordinator, 'check_native_turns', lambda *_: None)
            local.setattr('management.materials.idle_batch', lambda *_: None)
            scheduler.Background(core, OneIdleStop()).worker()

        class Reader:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def request(self, method, params):
                assert method == 'thread/turns/list' and params['threadId'] == THREAD
                space.history_calls.append(method)
                if space.on_read: space.on_read()
                if isinstance(space.receipt, Exception): raise space.receipt
                return {'data': [copy.deepcopy(space.receipt)], 'nextCursor': None}
            def close(self): pass
        monkeypatch.setattr(desktop_native_turns, 'PlainAppServer', Reader)
        space.original = snapshot(space)
        return space
    return make


@pytest.mark.parametrize('actions', [False, True])
@pytest.mark.parametrize('failure', ['AI_TIMEOUT', 'AI_DISCONNECTED', 'exception'])
def test_native_exception_retains_candidate_and_active_job_without_terminal(build_space, actions, failure):
    space = build_space(actions=actions, failure=failure)
    before = snapshot(space)
    assert before['job']['status'] == 'running'
    assert before['conversation']['active_job_id'] == space.job_id
    assert before['operation']['pending_terminal'] == 1 and before['operation']['candidate']
    assert before['messages'] == 0 and before['entities'] == 0
    coordinator.check_native_turns(space.core)
    assert snapshot(space) == before
    assert space.send_calls == 1


@pytest.mark.parametrize('status', ['completed', 'failed', 'interrupted', 'cancelled'])
@pytest.mark.parametrize('standalone', [False, True])
def test_exact_terminal_finishes_retained_candidate_once_without_dispatch(build_space, status, standalone):
    space = build_space(actions=True, standalone=standalone)
    before = snapshot(space)
    space.receipt.update(status=status, completedAt=1800000000)
    coordinator.check_native_turns(space.core)
    after = snapshot(space)
    assert after['job']['status'] == 'awaiting_review'
    if not standalone:
        assert after['conversation']['active_job_id'] is None
        assert after['conversation']['current_proposal_job_id'] == space.job_id
    assert after['entities'] == 0 and after['dispatches'] == before['dispatches']
    coordinator.check_native_turns(space.core)
    assert snapshot(space) == after and space.send_calls == 1


@pytest.mark.parametrize('receipt', [
    {'id': OTHER, 'status': 'completed', 'completedAt': 1800000000},
    {'id': TURN, 'status': 'failed', 'completedAt': None},
    ConnectionError('Synthetic history unavailable'),
])
def test_wrong_partial_or_unknown_receipt_cannot_finish_pending_candidate(build_space, receipt):
    space = build_space()
    before = snapshot(space)
    space.receipt = receipt
    coordinator.check_native_turns(space.core)
    assert snapshot(space) == before


def test_observer_does_not_compete_with_active_software_worker(build_space):
    space = build_space()
    before = snapshot(space)
    space.core.cancel_events[space.job_id] = threading.Event()
    space.receipt.update(status='completed', completedAt=1800000000)
    coordinator.check_native_turns(space.core)
    assert not space.history_calls and snapshot(space) == before
    space.core.cancel_events.pop(space.job_id)
    coordinator.check_native_turns(space.core)
    assert snapshot(space)['job']['status'] == 'completed'


@pytest.mark.parametrize('race', ['cancel', 'resume', 'different_turn'])
def test_pending_observer_rechecks_after_history_read(build_space, race):
    space = build_space()
    captured = {}
    def change():
        if race == 'different_turn':
            with space.core.store.connect() as c:
                c.execute('UPDATE conversation_operations SET provider_turn_id=? WHERE job_id=?', (OTHER, space.job_id))
        else:
            command(space.core, 'cancel_job', {'id': space.job_id})
            if race == 'resume':
                command(space.core, 'resume_context_operation', {'id': space.job_id})
        captured['state'] = snapshot(space)
    space.on_read = change
    space.receipt.update(status='completed', completedAt=1800000000)
    coordinator.check_native_turns(space.core)
    assert snapshot(space) == captured['state']


def test_service_restart_keeps_pending_candidate_in_read_only_observation(build_space, monkeypatch):
    space = build_space(actions=True)
    before = snapshot(space)
    space.core = Core(space.core.root)
    class InertThread:
        def __init__(self, **kwargs): pass
        def start(self): pass
    monkeypatch.setattr(scheduler.threading, 'Thread', InertThread)
    scheduler.Background(space.core, threading.Event()).start()
    recovered = snapshot(space)
    assert recovered['job']['status'] == 'running'
    assert recovered['job']['generation'] == before['job']['generation']
    assert recovered['dispatches'] == before['dispatches']
    coordinator.check_native_turns(space.core)
    assert snapshot(space) == recovered
    space.receipt.update(status='completed', completedAt=1800000000)
    coordinator.check_native_turns(space.core)
    assert snapshot(space)['job']['status'] == 'awaiting_review' and space.send_calls == 1


def test_restart_does_not_promote_old_pending_candidate_into_resumed_generation(build_space):
    space = build_space()
    command(space.core, 'cancel_job', {'id': space.job_id})
    command(space.core, 'resume_context_operation', {'id': space.job_id})
    before = snapshot(space)
    with space.core.store.connect() as c:
        coordinator.recover_pending_native_candidates(space.core, c)
    assert snapshot(space) == before and before['job']['status'] == 'queued'
