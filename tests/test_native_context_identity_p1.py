"""Trusted callback identity across cancellation, transactions and migrations."""
import json
import threading
from types import SimpleNamespace

import pytest

from management.core import Core
from management import context_service as context, conversation_progress as progress
from management import native_desktop, session_coordinator as coordinator
from management.schemas import BusinessError
from test_conversation_progress_v12 import claimed, command


THREAD = '00000000-0000-4000-8000-000000000051'
OLD = '00000000-0000-4000-8000-000000000052'
NEW = '00000000-0000-4000-8000-000000000053'
PROPOSAL = {'summary': 'Synthetic candidate', 'actions': [], 'sources': [], 'unknowns': []}


def identity(turn):
    return {'provider_thread_id': THREAD, 'provider_turn_id': turn}


def claim(core, job_id):
    with core.store.connect() as c:
        c.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        job = core._job(c, job_id)
        progress.start(c, job)
    return job, progress.Publisher(core, job, threading.Event(), threading.Event())


def observe(publish, turn):
    publish({'phase': 'waiting_model', 'provider_thread_id': THREAD,
        'provider_turn_id': turn, 'provider_contract': native_desktop.CONTRACT})


@pytest.fixture(params=[False, True], ids=['conversation', 'standalone'])
def space(tmp_path, request):
    core = Core(tmp_path / 'data')
    command(core, 'settings', {'settings': {'ai': {'enabled': True, 'execution_mode': 'desktop_shared'}}})
    if request.param:
        made = command(core, 'create_job', {'kind': 'ai', 'input': {'prompt': 'Synthetic independent request'}})['job']
        job, publish = claim(core, made['id'])
    else:
        job, publish = claimed(core)
    native_desktop._mark_native(core, job)
    observe(publish, OLD)
    value = json.loads(job['input'])
    return SimpleNamespace(core=core, job=job, publish=publish, value=value,
        operation=value['context_operation_id'], standalone=request.param)


def call(space, name, turn=OLD, **params):
    return coordinator.handle_native(space.core, 'context', {**identity(turn), 'name': name,
        'params': {'operation_id': space.operation, **params}})


def state(space):
    with space.core.store.connect() as c:
        return {table: [tuple(row) for row in c.execute('SELECT * FROM '+table+' ORDER BY rowid')]
            for table in ('context_operations', 'context_actions', 'context_queries', 'conversation_operations', 'entities', 'changes')}


def resume(space, *, bind=True):
    command(space.core, 'cancel_job', {'id': space.job['id']})
    command(space.core, 'resume_context_operation', {'id': space.job['id']})
    if bind:
        current, publish = claim(space.core, space.job['id'])
        observe(publish, NEW)
        return current
    with space.core.store.connect() as c:
        return space.core._job(c, space.job['id'])


@pytest.mark.parametrize('name,params', [
    ('checkpoint_context', {'summary': 'Late old result', 'yield': True}),
    ('refresh_context', {'include_new_sources': False}),
    ('validate_candidate', {'proposal': PROPOSAL}),
])
def test_old_turn_cannot_mutate_new_generation_context(space, name, params):
    resumed = resume(space)
    before = state(space)
    with pytest.raises(BusinessError) as error:
        call(space, name, OLD, **params)
    assert error.value.code == 'discussion_turn_stale'
    assert state(space) == before
    assert resumed['generation'] > space.job['generation']
    assert call(space, 'validate_candidate', NEW, proposal=PROPOSAL)['valid']


def test_new_generation_without_a_new_turn_does_not_reclaim_old_turn(space):
    resume(space, bind=False)
    before = state(space)
    with pytest.raises(BusinessError, match='代次'):
        call(space, 'checkpoint_context', summary='Do not relabel old turn')
    with pytest.raises(BusinessError):
        coordinator.handle_native(space.core, 'begin', {**identity(OLD),
            'text': space.value['prompt'], 'request_id': 'late-old-begin'})
    assert state(space) == before


@pytest.mark.parametrize('name,params', [
    ('checkpoint_context', {'summary': 'Raced write', 'yield': True}),
    ('refresh_context', {'include_new_sources': False}),
    ('validate_candidate', {'proposal': PROPOSAL}),
])
def test_generation_rechecked_inside_write_transaction_not_only_at_route(space, monkeypatch, name, params):
    original = context._handle
    captured = {}
    def raced(core, method, payload):
        resume(space)
        captured['after_resume'] = state(space)
        return original(core, method, payload)
    monkeypatch.setattr(context, '_handle', raced)
    with pytest.raises(BusinessError) as error:
        call(space, name, **params)
    assert error.value.code == 'discussion_turn_stale'
    assert state(space) == captured['after_resume']


def test_same_turn_wrong_generation_and_missing_turn_are_refused(space):
    before = state(space)
    for turn in (NEW, None):
        with pytest.raises(BusinessError):
            call(space, 'validate_candidate', turn, proposal=PROPOSAL)
        assert state(space) == before


def test_current_standalone_and_conversation_submit_are_idempotent(space):
    begun = coordinator.handle_native(space.core, 'begin', {**identity(OLD),
        'text': space.value['prompt'], 'request_id': 'valid-original'})
    request = {**identity(OLD), 'job_id': begun['job_id'], 'generation': begun['generation'], 'proposal': PROPOSAL}
    before = state(space)
    with pytest.raises(BusinessError):
        coordinator.handle_native(space.core, 'submit', {**request, **identity(NEW)})
    assert state(space) == before
    assert coordinator.handle_native(space.core, 'submit', request)['received']
    received = state(space)
    assert coordinator.handle_native(space.core, 'submit', request)['received']
    assert state(space) == received


def test_same_generation_restart_preserves_current_turn_identity(space):
    with space.core.store.connect() as c:
        native_desktop.recover(space.core, c)
    space.core = Core(space.core.root)
    current, publish = claim(space.core, space.job['id'])
    assert current['generation'] == space.job['generation']
    # Existing turn stays valid through a same-generation reconnect.
    assert call(space, 'validate_candidate', proposal=PROPOSAL)['valid']
    observe(publish, NEW)  # e.g. the trusted next model turn after compaction.
    with pytest.raises(BusinessError):
        call(space, 'checkpoint_context', OLD, summary='Old stage')
    assert call(space, 'checkpoint_context', NEW, summary='Current stage')['saved']


@pytest.mark.parametrize('resumed', [False, True])
def test_existing_database_migration_never_assigns_new_generation_to_old_turn(space, resumed):
    if resumed:
        resume(space, bind=False)
    with space.core.store.connect() as c:
        c.execute('ALTER TABLE conversation_operations DROP COLUMN provider_generation')
    space.core = Core(space.core.root)
    with space.core.store.connect() as c:
        bound = c.execute('SELECT provider_generation FROM conversation_operations WHERE job_id=?', (space.job['id'],)).fetchone()[0]
    if resumed:
        assert bound is None
        with pytest.raises(BusinessError):
            call(space, 'checkpoint_context', summary='Old generation after migration')
        current, publish = claim(space.core, space.job['id'])
        observe(publish, NEW)
        assert call(space, 'validate_candidate', NEW, proposal=PROPOSAL)['valid']
    else:
        assert bound == space.job['generation']
        assert call(space, 'validate_candidate', proposal=PROPOSAL)['valid']


def test_existing_running_manual_job_migrates_from_matching_begin_progress(tmp_path):
    core = Core(tmp_path / 'manual-data')
    command(core, 'settings', {'settings': {'ai': {'enabled': True, 'execution_mode': 'desktop_shared'}}})
    seed, publish = claimed(core)
    observe(publish, OLD)
    with core.store.connect() as c:
        coordinator.complete_candidate(core, c, seed, {**PROPOSAL, '_candidate_validated': True,
            'provider': {'thread_id': THREAD}})
    begun = coordinator.handle_native(core, 'begin', {**identity(NEW),
        'text': 'Synthetic manual next turn', 'request_id': 'manual-next'})
    with core.store.connect() as c:
        before = core._job(c, begun['job_id'])
        tracking = c.execute('SELECT * FROM conversation_progress WHERE job_id=?', (before['id'],)).fetchone()
        assert tracking['provider_turn_id'] is None and tracking['generation'] == before['generation']
        c.execute('ALTER TABLE conversation_operations DROP COLUMN provider_generation')
    reopened = Core(core.root)
    with reopened.store.connect() as c:
        bound = c.execute('SELECT provider_generation FROM conversation_operations WHERE job_id=?', (before['id'],)).fetchone()[0]
        assert bound == before['generation']
    operation = json.loads(before['input'])['context_operation_id']
    result = coordinator.handle_native(reopened, 'context', {**identity(NEW), 'name': 'validate_candidate',
        'params': {'operation_id': operation, 'proposal': PROPOSAL}})
    assert result['valid']
