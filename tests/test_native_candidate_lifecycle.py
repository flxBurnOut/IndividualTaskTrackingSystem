"""Regression for a native candidate ACK arriving before its actual turn ends.

Uses real Core/SQLite and SDK metadata, with only the provider history stubbed.
No model, desktop connection, or real business space is used.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp import Client as MCPClient

from management import desktop_native_turns, session_coordinator as coordinator
from management.core import Core
from management.mcp_server import create_server
from management.schemas import BusinessError
from management.storage import encode
from test_conversation_progress_v12 import claimed, command


THREAD = '00000000-0000-4000-8000-000000000041'
TURN = '00000000-0000-4000-8000-000000000042'
NEXT = '00000000-0000-4000-8000-000000000043'
TEXT = 'Synthetic native user turn'


def begin(core, *, rid='native-first', turn=TURN, text=TEXT):
    return coordinator.handle_native(core, 'begin', {'provider_thread_id': THREAD,
        'provider_turn_id': turn, 'text': text, 'request_id': rid})


def proposal(actions=False):
    return {'summary': 'Synthetic candidate', 'unknowns': [], 'sources': [],
        'actions': [{'command': 'create', 'reason': 'Synthetic request',
                     'payload_json': json.dumps({'type': 'task', 'title': 'Synthetic candidate task'})}] if actions else []}


def submit(space, *, turn=TURN, actions=False):
    return coordinator.handle_native(space.core, 'submit', {'provider_thread_id': THREAD,
        'provider_turn_id': turn, 'job_id': space.job['id'], 'generation': space.job['generation'],
        'proposal': proposal(actions)})


def snapshot(space):
    with space.core.store.connect() as c:
        return {'job': dict(c.execute('SELECT * FROM jobs WHERE id=?', (space.job['id'],)).fetchone()),
            'conversation': dict(c.execute('SELECT * FROM conversations WHERE id=?', (space.conversation_id,)).fetchone()),
            'candidate': coordinator.get_candidate(c, space.job['id']),
            'count': c.execute('SELECT count(*) FROM jobs').fetchone()[0],
            'same_turn_count': c.execute('SELECT count(*) FROM conversation_operations WHERE provider_thread_id=? AND provider_turn_id=?', (THREAD, TURN)).fetchone()[0],
            'entities': c.execute('SELECT count(*) FROM entities').fetchone()[0],
            'messages': c.execute('SELECT count(*) FROM conversation_messages').fetchone()[0]}


@pytest.fixture
def space(tmp_path, monkeypatch):
    core = Core(tmp_path / 'data')
    command(core, 'settings', {'settings': {'ai': {'enabled': True, 'execution_mode': 'desktop_shared'}}})
    seed, publish = claimed(core)
    publish({'phase': 'thread_ready', 'provider_thread_id': THREAD, 'provider_contract': 'desktop_mcp_v3'})
    with core.store.connect() as c:
        coordinator.complete_candidate(core, c, seed,
            {'summary': 'Synthetic prior turn', 'actions': [], 'unknowns': [], 'sources': [],
             '_candidate_validated': True, 'provider': {'thread_id': THREAD, 'contract': 'desktop_mcp_v3',
                 'project_path': str(core.root / 'Codex事务助手')}})
    native = begin(core)
    with core.store.connect() as c:
        job = core._job(c, native['job_id'])
    receipt = {'id': TURN, 'status': 'inProgress', 'completedAt': None}
    calls = []
    class Reader:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def request(self, method, params):
            assert method == 'thread/turns/list' and params['threadId'] == THREAD
            calls.append(method)
            return {'data': [dict(receipt)], 'nextCursor': None}
        def close(self): pass
    monkeypatch.setattr(desktop_native_turns, 'PlainAppServer', Reader)
    return SimpleNamespace(core=core, job=job, receipt=receipt, calls=calls,
        conversation_id=json.loads(job['input'])['conversation_id'])


@pytest.mark.parametrize('actions', [False, True])
def test_native_submit_keeps_active_until_exact_terminal_receipt(space, actions):
    before = snapshot(space)
    result = submit(space, actions=actions)
    after = snapshot(space)
    assert result['received'] and not result['applied']
    assert after['candidate'] and after['job']['status'] == 'running'
    assert after['conversation']['active_job_id'] == space.job['id']
    assert after['conversation']['current_proposal_job_id'] is None
    assert after['count'] == before['count'] and after['messages'] == before['messages']
    assert after['entities'] == before['entities']
    with pytest.raises(BusinessError) as error:
        command(space.core, 'send_message', {'scope': {'kind': 'general'}, 'text': 'Next software request'})
    assert error.value.code == 'conversation_busy'
    assert snapshot(space) == after
    desktop_native_turns.check_native_turns(space.core, [space.job])
    assert snapshot(space) == after
    space.receipt.update(status='completed', completedAt=1800000000)
    desktop_native_turns.check_native_turns(space.core, [space.job])
    settled = snapshot(space)
    assert settled['job']['status'] == ('awaiting_review' if actions else 'completed')
    assert settled['conversation']['active_job_id'] is None
    assert settled['entities'] == before['entities']
    desktop_native_turns.check_native_turns(space.core, [space.job])
    assert snapshot(space) == settled


def test_same_actual_turn_cannot_create_another_job_after_submit_or_completion(space):
    submit(space)
    before = snapshot(space)
    for rid in ('different-request', 'third-request'):
        with pytest.raises(BusinessError) as error:
            begin(space.core, rid=rid)
        assert error.value.code == 'conversation_busy'
        assert snapshot(space) == before
    assert begin(space.core)['job_id'] == space.job['id']
    space.receipt.update(status='completed', completedAt=1800000000)
    desktop_native_turns.check_native_turns(space.core, [space.job])
    before = snapshot(space)
    with pytest.raises(BusinessError) as error:
        begin(space.core, rid='late-other-request')
    assert error.value.code == 'discussion_turn_finished'
    assert snapshot(space) == before and before['same_turn_count'] == 1
    next_job = begin(space.core, rid='new-user-turn', turn=NEXT, text='A distinct real user turn')
    assert next_job['job_id'] != space.job['id']
    assert snapshot(space)['count'] == before['count'] + 1


@pytest.mark.parametrize('owner', ['desktop', 'software'])
def test_restart_recovery_cannot_complete_native_candidate_before_terminal(space, owner):
    submit(space)
    with space.core.store.connect() as c:
        value = json.loads(space.job['input']); value['execution_owner'] = owner
        c.execute('UPDATE jobs SET input=? WHERE id=?', (encode(value), space.job['id']))
    space.core = Core(space.core.root)
    before = snapshot(space)
    with space.core.store.connect() as c:
        coordinator.recover_candidates(space.core, c)
    assert snapshot(space) == before
    assert before['job']['status'] == 'running' and before['conversation']['active_job_id'] == space.job['id']


@pytest.mark.parametrize('action', ['begin', 'submit'])
def test_native_writes_without_trusted_turn_identity_fail_without_changing_state(space, action):
    before = snapshot(space)
    with pytest.raises(BusinessError) as error:
        if action == 'begin': begin(space.core, turn=None)
        else: submit(space, turn=None)
    assert error.value.code == 'discussion_turn_identity_required'
    assert snapshot(space) == before


def test_different_turn_cannot_rebind_active_job_or_submit_its_candidate(space):
    before = snapshot(space)
    with pytest.raises(BusinessError):
        begin(space.core, turn=NEXT)  # Same request ID and text must not rebind its turn.
    assert snapshot(space) == before
    with pytest.raises(BusinessError): submit(space, turn=NEXT)
    assert snapshot(space) == before


@pytest.mark.parametrize('completed', [False, True])
def test_original_manual_turn_identity_survives_operation_progress(space, completed):
    if completed:
        submit(space)
        space.receipt.update(status='completed', completedAt=1800000000)
        desktop_native_turns.check_native_turns(space.core, [space.job])
    with space.core.store.connect() as c:
        c.execute('UPDATE conversation_operations SET provider_turn_id=? WHERE job_id=?', (NEXT, space.job['id']))
        mapping = dict(c.execute('SELECT * FROM conversation_native_turns WHERE provider_turn_id=?', (TURN,)).fetchone())
    space.core = Core(space.core.root)  # The original turn mapping must survive reopen.
    before = snapshot(space)
    for rid in ('native-first', 'different-after-stage'):
        with pytest.raises(BusinessError) as error:
            begin(space.core, rid=rid)
        assert error.value.code == ('discussion_turn_finished' if completed else 'conversation_busy')
        assert snapshot(space) == before
    with space.core.store.connect() as c:
        assert dict(c.execute('SELECT * FROM conversation_native_turns WHERE provider_turn_id=?', (TURN,)).fetchone()) == mapping
        assert c.execute('SELECT provider_turn_id FROM conversation_operations WHERE job_id=?', (space.job['id'],)).fetchone()[0] == NEXT


def test_missing_identity_cannot_downgrade_finished_native_matter_via_legacy_entry(space, monkeypatch):
    submit(space)
    space.receipt.update(status='completed', completedAt=1800000000)
    desktop_native_turns.check_native_turns(space.core, [space.job])
    before = snapshot(space)
    monkeypatch.setattr(space.core, '_prepare', lambda *args: pytest.fail('Missing native identity must fail before material preparation'))
    with pytest.raises(BusinessError) as error:
        coordinator.handle(space.core, 'begin', {'conversation_id': space.conversation_id, 'epoch': space.job['epoch'],
            'text': TEXT, 'request_id': 'no-metadata-late-call'})
    assert error.value.code == 'discussion_turn_identity_required'
    assert snapshot(space) == before


def test_explicit_cancel_after_candidate_cannot_be_undone_by_terminal_observer(space):
    submit(space)
    command(space.core, 'cancel_job', {'id': space.job['id']})
    before = snapshot(space)
    assert before['job']['status'] == 'cancelled'
    space.receipt.update(status='completed', completedAt=1800000000)
    desktop_native_turns.check_native_turns(space.core, [space.job])
    assert snapshot(space) == before


def test_real_sdk_nested_metadata_preserves_one_job_while_candidate_ack_is_early(space):
    class LocalBusiness:
        def _request(self, category, action, data, **kwargs):
            assert category == 'native-discussion'
            return coordinator.handle_native(space.core, action, data)
    async def scenario():
        meta = {'threadId': THREAD, 'x-codex-turn-metadata': {'thread_id': THREAD, 'turn_id': TURN}}
        async with MCPClient(create_server(space.core.root, client=LocalBusiness())) as client:
            first = await client.session.call_tool('begin_discussion', {'text': TEXT, 'request_id': 'native-first'}, meta=meta)
            assert not first.is_error and first.structured_content['job_id'] == space.job['id']
            candidate = await client.session.call_tool('submit_candidate', {'job_id': space.job['id'],
                'generation': space.job['generation'], 'proposal': proposal()}, meta=meta)
            assert not candidate.is_error
            assert snapshot(space)['job']['status'] == 'running'
            duplicate = await client.session.call_tool('begin_discussion', {'text': TEXT, 'request_id': 'second-tool-id'}, meta=meta)
            assert duplicate.is_error
            assert snapshot(space)['same_turn_count'] == 1
    asyncio.run(scenario())
