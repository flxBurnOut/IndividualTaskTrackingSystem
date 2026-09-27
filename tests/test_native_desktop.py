"""Real Store/Publisher with a synthetic desktop owner; no model or user data."""
import json
import threading
import uuid

import pytest

from management import native_desktop as native
from management import session_coordinator
from management.core import Core
from management.ai import AIError
from test_conversation_progress_v12 import claimed, command

THREAD = '00000000-0000-4000-8000-000000000010'
TURN = '00000000-0000-4000-8000-000000000020'


@pytest.mark.parametrize('stage', [0, 1])
def test_native_text_input_is_safe_for_desktop_message_projection(tmp_path, stage):
    # Unlike app-server deserialization, the desktop's optimistic message and
    # bookmark projections read input.text_elements.length without a default.
    image = tmp_path / 'fixture.png'
    image.write_bytes(b'fixture')
    value = {'prompt': 'Original request', 'context_operation_id': 'original-context',
             'local_images': [str(image)]}
    job = {'epoch': 'fixture-epoch', 'id': 'fixture-job'}
    payload = native._payload(THREAD, value, tmp_path, None, job, stage)
    request = payload['turnStart']['request']
    text, attachment = request['input']
    assert text['type'] == 'text' and text['text_elements'] == []
    assert attachment == {'type': 'localImage', 'path': str(image)}
    assert (text['text'] == value['prompt']) is (stage == 0)
    if stage:
        assert value['context_operation_id'] in text['text']
    assert request['clientUserMessageId'] == native._payload(
        THREAD, value, tmp_path, None, job, stage)['turnStart']['request']['clientUserMessageId']


class Reader:
    status = 'completed'
    def __init__(self, *args, **kwargs): self.closed = False
    def __enter__(self): return self
    def request(self, method, params):
        assert method in {'thread/turns/list', 'thread/read'}
        return {'data': [{'id': TURN, 'status': self.status, 'completedAt': 1}], 'nextCursor': None}
    def close(self): self.closed = True


class Gateway:
    def __init__(self):
        self.calls = []
        self.state = {'threadRuntimeStatus': {'type': 'idle'}}
        self.effect = None
    def status(self, **kwargs): return {'ready': True}
    def connect(self): raise AssertionError('Already open; should not launch')
    def owner(self, thread, **kwargs):
        assert thread == THREAD
        return 'owner'
    def subscribe(self, thread): assert thread == THREAD
    def snapshot(self, thread, timeout=1): return self.state
    def request_owner(self, thread, method, params, **kwargs):
        self.calls.append((method, params))
        if self.effect: return self.effect(method, params)
        return {'resultType': 'success', 'result': {'result': {'turn': {'id': TURN}}}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    core = Core(tmp_path / 'data')
    command(core, 'settings', {'settings': {'ai': {'enabled': True, 'execution_mode': 'desktop_shared'}}})
    job, progress = claimed(core)
    value = json.loads(job['input'])
    gateway = core._desktop_gateway = Gateway()
    workspace = core.root / 'Codex事务助手'
    workspace.mkdir()
    monkeypatch.setattr('management.codex_project.project_binding', lambda path: {'project_id': 'project', 'instructions_verified': True})
    monkeypatch.setattr('management.codex_project.ensure_project', lambda *args: pytest.fail('Project already ready'))
    clock = [0.0]
    seeds = []
    def seed(workspace, **kwargs):
        seeds.append(kwargs)
        kwargs['on_created'](THREAD)
        return {'thread_id': THREAD, 'handoff_ready': True}
    def wait(seconds): clock[0] += seconds
    def run():
        return native.generate(core, job, value, core.query('settings')['settings'],
            progress.cancel, progress.stop, progress, seed=seed, reader_factory=Reader,
            clock=lambda: clock[0], wait=wait)
    return core, job, progress, gateway, seeds, run, value


def submit(core, job):
    value = json.loads(job['input'])
    handle = session_coordinator.handle_native(core, 'begin', {'provider_thread_id': THREAD, 'provider_turn_id': TURN,
        'text': value['prompt'], 'request_id': 'native-fixture-first'})
    return session_coordinator.handle_native(core, 'submit', {'provider_thread_id': THREAD, 'provider_turn_id': TURN,
        'job_id': handle['job_id'], 'generation': handle['generation'],
        'proposal': {'summary': '已返回同一事项', 'unknowns': [], 'sources': [], 'actions': []}})


def test_native_send_routes_candidate_to_same_business_job(setup):
    core, job, progress, gateway, seeds, run, value = setup
    def effect(method, params):
        submit(core, job)
        return {'resultType': 'success', 'result': {'result': {'turn': {'id': TURN}}}}
    gateway.effect = effect
    result = run()
    assert result['summary'] == '已返回同一事项'
    assert result['provider']['thread_id'] == THREAD
    assert result['_candidate_validated'] is True
    assert len(seeds) == 1 and len(gateway.calls) == 1
    request = gateway.calls[0][1]['turnStart']['request']
    assert request['input'][0]['text'] == value['prompt']
    assert request['approvalPolicy'] == 'never' and request['sandboxPolicy']['type'] == 'readOnly'
    with core.store.connect() as c:
        intents = c.execute('SELECT * FROM desktop_dispatches').fetchall()
        assert len(intents) == 2 and all(r['state'] == 'acknowledged' for r in intents)
        assert c.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
    assert session_coordinator.native_binding(core, THREAD)['conversation_id'] == value['conversation_id']


def test_stop_after_dispatch_detaches_then_reconciles_without_resend(setup):
    core, job, progress, gateway, seeds, run, value = setup
    def lost(method, params):
        gateway.state = {'turns': [{'turnId': TURN, 'status': 'inProgress',
            'params': {'clientUserMessageId': params['turnStart']['request']['clientUserMessageId']}}]}
        progress.stop.set()
        raise ConnectionError('ack lost')
    gateway.effect = lost
    with pytest.raises(native.ServiceDetached): run()
    assert len(gateway.calls) == 1
    progress.stop.clear()
    old_snapshot = gateway.snapshot
    counter = [0]
    def observed(*args, **kwargs):
        counter[0] += 1
        if counter[0] == 1: submit(core, job)
        return old_snapshot(*args, **kwargs)
    gateway.snapshot = observed
    result = run()
    assert result['provider']['turn_id'] == TURN
    assert len(seeds) == 1 and len(gateway.calls) == 1
    assert native._record(core, job, 'model-stage-0')['state'] == 'acknowledged'


def test_timeout_unknown_is_never_permission_to_repeat(setup):
    core, job, progress, gateway, seeds, run, value = setup
    gateway.effect = lambda *args: {'resultType': 'error', 'error': 'timeout'}
    with pytest.raises(AIError, match='未重复发送') as first: run()
    assert first.value.code == 'AI_DELIVERY_UNKNOWN'
    with pytest.raises(AIError): run()
    assert len(gateway.calls) == 1 and len(seeds) == 1


def test_cancel_interrupts_only_the_owned_exact_turn(setup):
    core, job, progress, gateway, seeds, run, value = setup
    def cancel_after_ack(method, params):
        if method == native.START:
            progress.cancel.set()
            return {'resultType': 'success', 'result': {'result': {'turn': {'id': TURN}}}}
        assert params['expectedTurnId'] == TURN and params['conversationId'] == THREAD
        return {'resultType': 'success'}
    gateway.effect = cancel_after_ack
    with pytest.raises(AIError) as exc: run()
    assert exc.value.code == 'AI_CANCELLED'
    assert [call[0] for call in gateway.calls] == [native.START, 'thread-follower-interrupt-turn']


def test_native_job_survives_service_restart_with_same_generation(setup):
    core, job, progress, gateway, seeds, run, value = setup
    native._mark_native(core, job)
    with core.store.connect() as c:
        native.recover(core, c)
        current = core._job(c, job['id'])
    assert current['status'] == 'queued' and current['generation'] == job['generation']


def test_historical_thread_cannot_fall_back_to_unrestricted_mcp(setup):
    core, job, progress, gateway, seeds, run, value = setup
    progress({'phase': 'thread_ready', 'provider_thread_id': THREAD, 'provider_contract': native.CONTRACT})
    with core.store.connect() as c:
        c.execute("UPDATE conversation_bindings SET state='historical'")
    with pytest.raises(Exception) as exc: session_coordinator.native_binding(core, THREAD)
    assert exc.value.code == 'conversation_binding_stale'


def test_real_metadata_routing_does_not_accept_forged_business_scope(setup):
    core, job, progress, gateway, seeds, run, value = setup
    progress({'phase': 'thread_ready', 'provider_thread_id': THREAD, 'provider_contract': native.CONTRACT})
    result = session_coordinator.handle_native(core, 'begin', {'provider_thread_id': THREAD, 'provider_turn_id': TURN,
        'conversation_id': 'wrong', 'epoch': 'wrong', 'text': value['prompt'], 'request_id': 'native-fixture-2'})
    assert result['job_id'] == job['id'] and result['epoch'] == job['epoch']
    capability = session_coordinator.handle_native(core, 'query', {'provider_thread_id': THREAD, 'name': 'capabilities'})
    assert capability['direct_business_writes'] is False


def test_unrelated_turn_cannot_reconcile_unknown_dispatch():
    state = {'turns': [{'turnId': TURN, 'params': {'clientUserMessageId': 'other'}}]}
    assert native._correlated_turn(state, str(uuid.uuid4())) is None


def test_idle_or_empty_desktop_state_never_proves_completion():
    assert native.state_turns({'threadRuntimeStatus': {'type': 'idle'}}) == []
    assert native._status(None) is None


def test_partial_rollout_failed_without_completion_receipt_is_not_terminal():
    assert not native.terminal_receipt({'id': TURN, 'status': 'failed', 'completedAt': None})
    assert not native.terminal_receipt({'id': TURN, 'status': 'completed'})
    assert native.terminal_receipt({'id': TURN, 'status': 'completed', 'completedAt': 100})


def test_existing_desktop_turn_blocks_a_second_dispatch(setup):
    core, job, progress, gateway, seeds, run, value = setup
    gateway.state = {'turns': [], 'threadRuntimeStatus': {'type': 'active'}}
    with pytest.raises(AIError) as exc: run()
    assert exc.value.code == 'AI_CONVERSATION_BUSY'
    assert gateway.calls == []
    assert native._record(core, job, 'model-stage-0')['state'] == 'prepared'


def test_cancel_between_idle_check_and_send_is_proven_not_sent(setup):
    core, job, progress, gateway, seeds, run, value = setup
    def snapshot(*args, **kwargs):
        progress.cancel.set()
        return {'threadRuntimeStatus': {'type': 'idle'}}
    gateway.snapshot = snapshot
    with pytest.raises(AIError): run()
    assert gateway.calls == []
    assert native._record(core, job, 'model-stage-0')['state'] == 'prepared'


def test_partial_failed_history_waits_for_actual_mcp_and_terminal(setup, monkeypatch):
    core, job, progress, gateway, seeds, run, value = setup
    reads = []
    def read(reader, method, params):
        reads.append(method)
        if len(reads) == 1:
            return {'data': [{'id': TURN, 'status': 'failed', 'completedAt': None}]}
        submit(core, job)
        return {'data': [{'id': TURN, 'status': 'completed', 'completedAt': 100}]}
    monkeypatch.setattr(Reader, 'request', read)
    result = run()
    assert result['summary'] == '已返回同一事项'
    assert len(reads) == 2 and len(gateway.calls) == 1
    with core.store.connect() as c:
        assert c.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
