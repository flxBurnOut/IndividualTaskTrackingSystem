"""Service/desktop lifecycle without launching a real application or model."""
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from management import desktop_gateway as gateway
from management.codex_ipc import IpcDisconnected, IpcError, IpcTimeout, IpcProtocolError


THREAD = '00000000-0000-4000-8000-000000000001'


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


class Core:
    def __init__(self, root):
        self.root, self.enabled = root, True

    def query(self, name):
        assert name == 'settings'
        return {'settings': {'ai': {'enabled': self.enabled, 'execution_mode': 'desktop_shared'}}}


class Desktop:
    def __init__(self, pid=42, owner='owner-1'):
        self.installation = SimpleNamespace(version='26.924.2738.0')
        self.server_pid = pid
        self.owner = owner
        self.available = True
        self.closed = False
        self.events = queue.Queue()
        self.calls = []
        self.revision = 1
        self.on_mutation = None
        self.supported = True

    def request(self, method, params, version, target_client_id=None, timeout=10):
        if self.closed:
            raise IpcDisconnected('ipc_eof', 'synthetic closed')
        self.calls.append(('request', method, params, version, target_client_id))
        if method == 'thread-owner-discovery':
            if params['conversationId'] == gateway._CONTROL_THREAD or not self.available:
                return {'resultType': 'error', 'error': 'no-client-found'}
            return {'resultType': 'success', 'handledByClientId': self.owner,
                    'result': {'supportsUntrustedAppInput': self.supported}}
        if self.on_mutation:
            return self.on_mutation()
        return {'resultType': 'success', 'handledByClientId': target_client_id,
                'result': {'result': {'turn': {'id': 'synthetic-turn'}}}}

    def broadcast(self, method, params, version, target_client_ids, timeout=10):
        self.calls.append(('broadcast', method, params, version, target_client_ids))
        self.emit_snapshot(params['conversationId'])

    def emit_snapshot(self, thread_id=THREAD):
        self.events.put({'type': 'broadcast', 'method': 'thread-stream-state-changed', 'version': 11,
                         'sourceClientId': self.owner, 'params': {'conversationId': thread_id, 'hostId': 'local',
                         'change': {'type': 'snapshot', 'revision': self.revision,
                                    'conversationState': {'id': thread_id, 'hostId': 'local', 'turns': [], 'owner': self.owner}}}})

    def next_event(self, timeout):
        try:
            event = self.events.get(timeout=timeout)
        except queue.Empty:
            raise IpcTimeout('ipc_event_timeout', 'synthetic idle')
        if isinstance(event, BaseException):
            raise event
        return event

    def close(self):
        self.closed = True
        self.events.put(IpcDisconnected('ipc_closed', 'synthetic close'))


@pytest.fixture
def harness(monkeypatch, tmp_path):
    core = Core(tmp_path)
    state = {'desktop': None, 'connects': 0, 'activations': 0, 'opens': []}
    def connect(**kwargs):
        state['connects'] += 1
        if state['desktop'] is None or state['desktop'].closed:
            raise IpcDisconnected('ipc_disconnected', 'synthetic desktop absent')
        return state['desktop']
    def activate():
        state['activations'] += 1
        state['desktop'] = Desktop()
        return {'status': 'activation_requested'}
    def open_thread(thread_id):
        state['opens'].append(thread_id)
        if state['desktop']:
            state['desktop'].available = True
    monkeypatch.setattr(gateway.NativeDesktopIpc, 'connect', connect)
    monkeypatch.setattr(gateway, 'activate_codex', activate)
    monkeypatch.setattr(gateway, '_open_thread', open_thread)
    instance = gateway.DesktopGateway(core, project_ready=lambda: True)
    yield instance, state, core
    instance.close()


def test_status_never_activates_desktop_and_connect_is_explicit(harness):
    instance, state, _ = harness
    assert instance.status()['state'] == 'desktop_closed'
    assert state['activations'] == 0
    result = instance.connect()
    assert result['ready'] and state['activations'] == 1
    assert result['mcp_ready'] is None and result['mcp_config_ready'] is True
    instance.connect()
    assert state['activations'] == 1


def test_existing_desktop_is_attached_independent_of_launch_order(harness):
    instance, state, _ = harness
    state['desktop'] = Desktop()
    result = instance.status(force=True)
    assert result['state'] == 'ready' and result['desktop_pid'] == 42
    assert state['activations'] == 0
    assert all(call[1] == 'thread-owner-discovery' for call in state['desktop'].calls)


def test_desktop_opened_later_is_attached_without_special_launch(harness):
    instance, state, _ = harness
    assert not instance.status()['ready']
    state['desktop'] = Desktop()
    assert instance.status()['ready']
    assert state['activations'] == 0


def test_disabled_connection_does_not_attach_or_activate(harness):
    instance, state, core = harness
    core.enabled = False
    assert instance.status()['state'] == 'disabled'
    assert instance.connect()['state'] == 'disabled'
    assert state['connects'] == 0 and state['activations'] == 0


def test_project_readiness_is_separate_from_live_mcp_readiness(harness):
    instance, state, _ = harness
    state['desktop'] = Desktop()
    instance._project_check = lambda: {'project_ready': False, 'mcp_config_ready': True}
    result = instance.status()
    assert not result['ready'] and result['desktop_ready'] and result['mcp_ready'] is None
    assert result['code'] == 'codex_project_incomplete'


def test_owner_discovers_existing_thread_and_opens_missing_thread_once(harness):
    instance, state, _ = harness
    state['desktop'] = Desktop()
    assert instance.owner(THREAD) == 'owner-1' and not state['opens']
    state['desktop'].available = False
    assert instance.owner(THREAD) == 'owner-1' and state['opens'] == [THREAD]
    assert state['activations'] == 0


def test_owner_open_false_never_opens_and_requires_compatible_protocol(harness):
    instance, state, _ = harness
    state['desktop'] = Desktop()
    state['desktop'].available = False
    with pytest.raises(IpcError) as caught:
        instance.owner(THREAD, open_if_missing=False)
    assert caught.value.code == 'desktop_owner_missing' and not state['opens']
    state['desktop'].available = True
    state['desktop'].supported = False
    with pytest.raises(IpcProtocolError) as caught:
        instance.owner(THREAD)
    assert caught.value.code == 'desktop_protocol_unsupported'


@pytest.mark.parametrize('value', ['not-an-id', '../other', THREAD + '?prompt=injected', None])
def test_invalid_thread_never_opens_or_sends(harness, value):
    instance, state, _ = harness
    with pytest.raises(IpcProtocolError):
        instance.owner(value)
    assert state['connects'] == 0 and not state['opens']


def test_mutation_is_sent_once_with_fresh_owner_and_complete_envelope(harness):
    instance, state, _ = harness
    state['desktop'] = Desktop(owner='current-owner')
    result = instance.request_owner(THREAD, 'thread-follower-start-turn', {'conversationId': THREAD, 'hostId': 'local'}, 2)
    assert result['result']['result']['turn']['id'] == 'synthetic-turn'
    sent = [call for call in state['desktop'].calls if call[1] == 'thread-follower-start-turn']
    assert len(sent) == 1 and sent[0][4] == 'current-owner'
    def fail():
        raise IpcTimeout('ipc_response_timeout', 'unknown', outcome_unknown=True)
    state['desktop'].on_mutation = fail
    with pytest.raises(IpcTimeout) as caught:
        instance.request_owner(THREAD, 'thread-follower-start-turn', {'conversationId': THREAD}, 2)
    assert caught.value.outcome_unknown
    assert len([call for call in state['desktop'].calls if call[1] == 'thread-follower-start-turn']) == 2


def test_mutation_rejects_conflicting_thread_before_any_io(harness):
    instance, state, _ = harness
    with pytest.raises(IpcError) as caught:
        instance.request_owner(THREAD, 'synthetic', {'conversationId': 'other'}, 1)
    assert caught.value.code == 'desktop_request_thread_mismatch' and state['connects'] == 0


def test_single_event_reader_projects_snapshot_and_recovers_gap(harness):
    instance, state, _ = harness
    desktop = state['desktop'] = Desktop()
    stream = instance.subscribe(THREAD)
    assert instance.snapshot(THREAD, timeout=1)['owner'] == 'owner-1'
    desktop.revision = 5
    desktop.events.put({'type': 'broadcast', 'method': 'thread-stream-state-changed', 'version': 11,
                       'sourceClientId': desktop.owner, 'params': {'conversationId': THREAD, 'hostId': 'local',
                       'change': {'type': 'patches', 'baseRevision': 3, 'revision': 4, 'patches': []}}})
    eventually(lambda: stream.revision == 5)
    follows = [call for call in desktop.calls if call[0] == 'broadcast']
    assert len(follows) >= 2 and all(call[2]['following'] is True for call in follows)
    assert not any(call[1] == 'thread-follower-start-turn' for call in desktop.calls)


def test_owner_change_requires_rediscovery_then_new_snapshot(harness):
    instance, state, _ = harness
    desktop = state['desktop'] = Desktop()
    old = instance.subscribe(THREAD)
    assert instance.snapshot(THREAD, timeout=1)['owner'] == 'owner-1'
    desktop.owner = 'owner-2'
    desktop.emit_snapshot()
    eventually(lambda: (instance.snapshot(THREAD, timeout=0) or {}).get('owner') == 'owner-2')
    assert old.get_state() is None
    assert instance._streams[THREAD].owner_client_id == 'owner-2'


def test_disconnected_owner_status_drops_snapshot_without_waiting_for_new_owner_event(harness):
    instance, state, _ = harness
    desktop = state['desktop'] = Desktop()
    old = instance.subscribe(THREAD)
    assert instance.snapshot(THREAD, timeout=1)
    desktop.owner = 'owner-after-disconnect'
    desktop.events.put({'type': 'broadcast', 'method': 'client-status-changed', 'version': 0,
                       'sourceClientId': 'owner-1', 'params': {'clientId': 'owner-1',
                       'clientType': 'desktop', 'status': 'disconnected'}})
    eventually(lambda: (instance.snapshot(THREAD, timeout=0) or {}).get('owner') == 'owner-after-disconnect')
    assert old.get_state() is None


def test_desktop_restart_resubscribes_without_gui_or_model_retry(harness):
    instance, state, _ = harness
    first = state['desktop'] = Desktop()
    instance.subscribe(THREAD)
    assert instance.snapshot(THREAD, timeout=1)
    second = Desktop(pid=43, owner='restarted-owner')
    state['desktop'] = second
    first.events.put(IpcDisconnected('ipc_eof', 'desktop restarted'))
    eventually(lambda: (instance.snapshot(THREAD, timeout=0) or {}).get('owner') == 'restarted-owner')
    assert first.closed and state['activations'] == 0
    assert all(call[0] == 'broadcast' or call[1] == 'thread-owner-discovery' for call in second.calls)


def test_close_only_detaches_and_invalidates_snapshot(harness):
    instance, state, _ = harness
    desktop = state['desktop'] = Desktop()
    stream = instance.subscribe(THREAD)
    assert instance.snapshot(THREAD, timeout=1)
    before = list(desktop.calls)
    instance.close()
    assert desktop.closed and not instance._worker.is_alive()
    assert stream.get_state() is None and desktop.calls == before


def test_gateway_recreation_reattaches_without_activating_desktop(harness):
    first, state, core = harness
    state['desktop'] = Desktop()
    assert first.status()['ready']
    first.close()
    # A fresh pipe connection to the same still-running app has a fresh client.
    state['desktop'] = Desktop(pid=42)
    second = gateway.DesktopGateway(core, project_ready=lambda: True)
    try:
        assert second.status()['ready'] and state['activations'] == 0
    finally:
        second.close()


def test_unrelated_stream_events_do_not_populate_or_return_private_state(harness):
    instance, state, _ = harness
    desktop = state['desktop'] = Desktop()
    assert instance.status()['ready']
    desktop.emit_snapshot()
    time.sleep(0.04)
    assert instance.snapshot(THREAD, timeout=0) is None and not instance._streams


def test_subscription_limit_is_bounded(harness, monkeypatch):
    instance, state, _ = harness
    state['desktop'] = Desktop()
    monkeypatch.setattr(gateway, 'MAX_SUBSCRIPTIONS', 0)
    with pytest.raises(IpcError) as caught:
        instance.subscribe(THREAD)
    assert caught.value.code == 'desktop_subscriptions_full'
