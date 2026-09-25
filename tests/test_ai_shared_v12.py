"""Desktop shared adapter tests with synthetic runtime and protocol peers."""
import datetime as dt
import json
from pathlib import Path
import threading
import time

import pytest

from management import ai, ai_shared as shared


def event(method, *, thread='thread-1', turn_id='turn-1', **params):
    return {'method': method, 'params': {'threadId': thread, 'turnId': turn_id, **params}}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    executable = tmp_path / 'shim.exe'
    engine = tmp_path / 'codex.exe'
    executable.write_text('synthetic test file')
    engine.write_text('synthetic test file')
    stamp = time.time() - 10
    value = {'schema_version': 1, 'ready': True, 'pid': 10001, 'engine_pid': 10002,
             'executable': str(executable), 'engine_executable': str(engine),
             'shim_create_time': stamp, 'engine_create_time': stamp + 1,
             'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
             'endpoint': 'ws://127.0.0.1:49231', 'token': 'SyntheticSecret' * 5}
    process_values = {10001: {'path': executable, 'created': stamp},
                      10002: {'path': engine, 'created': stamp + 1}}
    class Process:
        def __init__(self, pid):
            self.value = process_values[pid]
        def is_running(self): return self.value.get('running', True)
        def status(self): return self.value.get('status', 'running')
        def exe(self): return str(self.value['path'])
        def create_time(self): return self.value['created']
    monkeypatch.setattr(shared.psutil, 'Process', Process)
    path = tmp_path / 'runtime.json'
    def save():
        path.write_text(json.dumps(value), encoding='utf-8')
    save()
    return tmp_path, value, process_values, save


def test_runtime_requires_live_matching_processes_and_connection_status_is_secret_free(runtime):
    root, value, processes, save = runtime
    assert shared.read_runtime(root, value['engine_executable']) == value
    state = shared.connection_status(root)
    assert state['ready'] is True
    assert set(state) == {'ready', 'message'}
    assert value['token'] not in json.dumps(state)
    assert value['endpoint'] not in json.dumps(state)
    assert value['executable'] not in json.dumps(state)


@pytest.mark.parametrize('endpoint', [
    'ws://localhost:49231', 'ws://127.0.0.2:49231', 'ws://192.168.1.1:49231',
    'wss://127.0.0.1:49231', 'ws://[::1]:49231', 'ws://127.0.0.1',
    'ws://127.0.0.1:0', 'ws://127.0.0.1:65536', 'ws://127.0.0.1:abc',
    'ws://user@127.0.0.1:49231', 'ws://127.0.0.1:49231/?token=secret',
    'ws://127.0.0.1:49231/#secret', 'ws://127.0.0.1:49231/path',
])
def test_remote_ambiguous_or_token_in_url_endpoints_are_rejected(runtime, endpoint):
    root, value, processes, save = runtime
    value['endpoint'] = endpoint
    save()
    with pytest.raises(ai.AIError) as error:
        shared.read_runtime(root)
    assert error.value.code == 'AI_SHARED_NOT_READY'
    assert value['token'] not in str(error.value)


@pytest.mark.parametrize('change', [
    {'ready': False}, {'schema_version': True}, {'schema_version': 2},
    {'token': 'short'}, {'token': 'x' * 1025}, {'token': 'x' * 40 + '\r\n'},
    {'token': '密' * 40}, {'pid': True}, {'engine_pid': 10001},
    {'shim_create_time': float('inf')}, {'engine_create_time': float('nan')},
    {'created_at': '2000-01-01T00:00:00+00:00'}, {'created_at': '2030-01-01T00:00:00'},
    {'created_at': '9999-01-01T00:00:00+00:00'}, {'created_at': 'invalid'},
])
def test_malformed_runtime_never_exposes_secret_or_succeeds(runtime, change):
    root, value, processes, save = runtime
    value.update(change)
    save()
    status = shared.connection_status(root)
    assert status['ready'] is False
    assert 'token' not in status and value['endpoint'] not in status['message']


@pytest.mark.parametrize('mutation', ['shim_reused', 'engine_reused', 'shim_dead', 'engine_dead', 'wrong_engine_path', 'wrong_shim_path'])
def test_stale_pid_or_changed_executable_is_rejected(runtime, mutation):
    root, value, processes, save = runtime
    if mutation == 'shim_reused': processes[10001]['created'] += 5
    if mutation == 'engine_reused': processes[10002]['created'] += 5
    if mutation == 'shim_dead': processes[10001]['running'] = False
    if mutation == 'engine_dead': processes[10002]['status'] = shared.psutil.STATUS_ZOMBIE
    if mutation == 'wrong_engine_path': processes[10002]['path'] = root / 'unrelated.exe'
    if mutation == 'wrong_shim_path': processes[10001]['path'] = root / 'unrelated.exe'
    assert shared.connection_status(root)['ready'] is False


def test_runtime_size_symlink_and_configured_engine_binding(runtime, monkeypatch):
    root, value, processes, save = runtime
    with pytest.raises(ai.AIError):
        shared.read_runtime(root, root / 'another-codex.exe')
    (root / 'runtime.json').write_text('x' * (shared.MAX_RUNTIME_BYTES + 1))
    assert shared.connection_status(root)['ready'] is False
    save()
    original = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda path: path == root / 'runtime.json' or original(path))
    assert shared.connection_status(root)['ready'] is False


def test_missing_runtime_is_clear_without_starting_an_engine(tmp_path):
    assert shared.connection_status(tmp_path)['ready'] is False
    with pytest.raises(ai.AIError) as error:
        shared.read_runtime(None)
    assert error.value.code == 'AI_SHARED_NOT_READY'
    assert '桌面实时连接' in error.value.message


class Socket:
    def __init__(self):
        self.sent = []
        self.events = []
        self.closed = False
        self.before_turn_ack = []
        self.after_turn_ack = []
        self.thread_start_prefix = []
    def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        method = message.get('method')
        if 'id' not in message or method is None:
            return
        result = {}
        if method in {'thread/start', 'thread/resume'}:
            result = {'thread': {'id': message['params'].get('threadId', 'thread-1')}}
            self.events.extend(self.thread_start_prefix)
        if method == 'turn/start':
            self.events.extend(self.before_turn_ack)
            result = {'turn': {'id': 'turn-1'}}
        self.events.append({'id': message['id'], 'result': result})
        if method == 'turn/start':
            self.events.extend(self.after_turn_ack)
    def recv(self, timeout):
        if not self.events:
            raise OSError('Synthetic connection closed')
        item = self.events.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item, ensure_ascii=False)
    def close(self):
        self.closed = True


@pytest.fixture
def transport(monkeypatch):
    socket = Socket()
    calls = []
    runtime = {'endpoint': 'ws://127.0.0.1:49231', 'token': 'secret-only-in-header'}
    monkeypatch.setattr(shared, 'read_runtime', lambda *a, **k: runtime)
    def connect(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return socket
    monkeypatch.setattr(shared, 'connect', connect)
    return socket, calls


def opened(transport, tmp_path):
    return shared.SharedAppServer('synthetic.exe', tmp_path, threading.Event(), 180, tmp_path)


def begin(rpc):
    rpc.request('initialize', {'clientInfo': {'name': 'original', 'version': '0.12'}, 'capabilities': {'experimentalApi': True}})
    rpc.send({'method': 'initialized', 'params': {}})
    rpc.request('thread/start', {})
    rpc.request('turn/start', {'threadId': rpc.thread_id})


def test_websocket_auth_bounded_transport_no_proxy_and_private_logging(transport, tmp_path):
    socket, calls = transport
    rpc = opened(transport, tmp_path)
    rpc.request('initialize', {'clientInfo': {'name': 'original', 'version': '0.12'}})
    kwargs = calls[0][1]
    assert kwargs['additional_headers'] == {'Authorization': 'Bearer secret-only-in-header'}
    assert kwargs['proxy'] is None and kwargs['logger'].disabled
    assert kwargs['max_queue'] == 16 and kwargs['max_size'] == ai.MAX_MESSAGE_BYTES
    assert socket.sent[0]['params']['clientInfo']['name'] == 'personal_management_shared'
    rpc.close()
    assert socket.closed
    assert not any(m.get('method') in {'turn/interrupt', 'thread/close', 'thread/unsubscribe'} for m in socket.sent)


def test_foreign_approvals_and_notifications_are_ignored_without_responses(transport, tmp_path):
    socket, calls = transport
    rpc = opened(transport, tmp_path)
    begin(rpc)
    socket.events.extend([
        {'id': 800, **event('item/commandExecution/requestApproval', thread='desktop-thread')},
        {'id': 801, **event('item/commandExecution/requestApproval', turn_id='desktop-turn')},
        {'id': 802, 'method': 'account/refresh', 'params': {}},
        event('item/started', thread='desktop-thread', item={'type': 'commandExecution'}),
        event('item/started', turn_id='desktop-turn', item={'type': 'mcpToolCall'}),
        event('item/agentMessage/delta', thread='desktop-thread', itemId='foreign', delta='PRIVATE'),
        event('item/agentMessage/delta', itemId='mine', delta='{"summary":"公开摘要'),
    ])
    message = rpc.next_event()
    assert message['params']['itemId'] == 'mine'
    assert not any(m.get('id') in {800, 801, 802} for m in socket.sent)
    rpc.turn_id = None
    rpc.close()
    assert not any(m.get('method') == 'turn/interrupt' for m in socket.sent)


def test_owned_approval_is_rejected_and_only_owned_turn_is_interrupted(transport, tmp_path):
    socket, calls = transport
    rpc = opened(transport, tmp_path)
    begin(rpc)
    socket.events.append({'id': 900, **event('item/commandExecution/requestApproval')})
    with pytest.raises(ai.AIError) as error:
        rpc.next_event()
    assert error.value.code == 'AI_TOOL_BLOCKED'
    assert next(m for m in socket.sent if m.get('id') == 900)['error']['code'] == -32601
    rpc.close(); rpc.close()
    interrupt = [m for m in socket.sent if m.get('method') == 'turn/interrupt']
    assert len(interrupt) == 1
    assert interrupt[0]['params'] == {'threadId': 'thread-1', 'turnId': 'turn-1'}
    assert socket.closed
    assert not any(m.get('method') in {'thread/close', 'thread/unsubscribe'} for m in socket.sent)


def test_pre_ack_owned_approval_is_deferred_until_turn_identity_is_known(transport, tmp_path):
    socket, calls = transport
    socket.before_turn_ack = [
        {'id': 901, **event('item/commandExecution/requestApproval', turn_id='desktop-turn')},
        {'id': 902, **event('item/commandExecution/requestApproval')},
    ]
    rpc = opened(transport, tmp_path)
    begin(rpc)
    assert not any(m.get('id') in {901, 902} for m in socket.sent)
    with pytest.raises(ai.AIError) as error:
        rpc.next_event()
    assert error.value.code == 'AI_TOOL_BLOCKED'
    assert not any(m.get('id') == 901 for m in socket.sent)
    assert any(m.get('id') == 902 and 'error' in m for m in socket.sent)
    rpc.close()


def test_other_turn_tool_before_our_start_response_cannot_abort_or_be_consumed(transport, tmp_path):
    socket, calls = transport
    socket.before_turn_ack = [event('item/started', turn_id='desktop-turn', item={'type': 'commandExecution'}),
                             event('item/agentMessage/delta', itemId='mine', delta='{"summary":"a')]
    rpc = opened(transport, tmp_path)
    begin(rpc)
    assert rpc.next_event()['params']['itemId'] == 'mine'
    rpc.turn_id = None
    rpc.close()


def test_uncorrelated_approval_on_same_thread_is_left_for_desktop(transport, tmp_path):
    socket, calls = transport
    rpc = opened(transport, tmp_path)
    begin(rpc)
    socket.events.extend([{'id': 999, 'method': 'item/mcpServer/elicitation/request', 'params': {'threadId': 'thread-1', 'turnId': None}},
                          event('item/agentMessage/delta', itemId='mine', delta='a')])
    rpc.next_event()
    assert not any(m.get('id') == 999 for m in socket.sent)
    rpc.turn_id = None; rpc.close()


def test_close_cannot_interrupt_an_id_not_started_by_this_connection(transport, tmp_path):
    socket, calls = transport
    rpc = opened(transport, tmp_path)
    rpc.thread_id, rpc.turn_id = 'someone-elses-thread', 'someone-elses-turn'
    rpc.close()
    assert socket.closed
    assert not socket.sent


def test_foreign_event_flood_does_not_fill_backlog(transport, tmp_path):
    socket, calls = transport
    socket.thread_start_prefix = [event('item/started', thread='foreign-' + str(i), item={'type': 'agentMessage'}) for i in range(400)]
    rpc = opened(transport, tmp_path)
    rpc.request('initialize', {'clientInfo': {}})
    rpc.request('thread/start', {})
    assert not rpc._backlog
    rpc.close()


def test_timeout_cancel_and_transport_errors_are_bounded(transport, tmp_path, monkeypatch):
    socket, calls = transport
    rpc = opened(transport, tmp_path)
    rpc.deadline = time.monotonic() - 1
    with pytest.raises(ai.AIError) as error:
        rpc.next_event()
    assert error.value.code == 'AI_TIMEOUT'
    rpc.cancel.set()
    with pytest.raises(ai.AIError) as error:
        rpc.next_event()
    assert error.value.code == 'AI_CANCELLED'
    rpc.close()
    monkeypatch.setattr(shared, 'connect', lambda *a, **k: (_ for _ in ()).throw(OSError('Authorization: Bearer SECRET')))
    with pytest.raises(ai.AIError) as error:
        opened(transport, tmp_path)
    assert error.value.code == 'AI_SHARED_NOT_READY'
    assert 'SECRET' not in str(error.value)
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize('previous', [False, True])
def test_generate_shared_uses_thread_scoped_isolation_and_streams_same_candidate(transport, tmp_path, monkeypatch, previous):
    socket, calls = transport
    socket.after_turn_ack = [
        {'id': 70, **event('item/tool/call', callId='candidate-1', tool='submit_management_candidate', arguments={'summary': '权威摘要', 'unknowns': [], 'sources': [], 'actions': []})},
        event('item/agentMessage/delta', itemId='m', delta='安全预览'),
        event('item/completed', item={'type': 'agentMessage', 'id': 'm', 'text': '权威摘要'}),
        event('turn/completed', turn={'id': 'turn-1', 'status': 'completed'}),
    ]
    monkeypatch.setattr(ai, 'find_codex', lambda *a: 'synthetic.exe')
    monkeypatch.setattr(ai, 'project_directory', lambda *a: tmp_path)
    monkeypatch.setattr(ai, '_AppServer', lambda *a: pytest.fail('must not start a separate engine'))
    overrides = {'features.shell_tool': False, 'mcp_servers.business.enabled': False}
    monkeypatch.setattr(ai, '_isolation_overrides', lambda cwd: overrides)
    value = {'prompt': 'Synthetic request', 'conversation_id': 'software-chat', 'conversation_scope': {'kind': 'general'}}
    if previous:
        value.update(provider_thread_id='thread-1', provider_project_path=str(tmp_path), provider_contract='desktop_candidate_tool_v1')
    snapshots = []
    result = ai.generate(value, {'ai': {'enabled': True, 'execution_mode': 'desktop_shared'}, '_codex_bridge_dir': str(tmp_path)}, threading.Event(), snapshots.append)
    method = 'thread/resume' if previous else 'thread/start'
    configured = next(m for m in socket.sent if m.get('method') == method)['params']
    assert configured['config'] == overrides
    assert configured['sandbox'] == 'read-only'
    assert 'BEGIN_UNTRUSTED_SOFTWARE_CONTEXT' in configured['developerInstructions']
    turn = next(m for m in socket.sent if m.get('method') == 'turn/start')['params']
    assert turn['input'][0]['text'] == 'Synthetic request'
    assert 'outputSchema' not in turn
    if not previous:
        assert [t['name'] for t in configured['dynamicTools']] == ['submit_management_candidate']
    else:
        assert 'dynamicTools' not in configured
    assert any(p.get('preview_text') == '安全预览' for p in snapshots)
    assert result['summary'] == '权威摘要'
    assert socket.closed
    assert not any(m.get('method') in {'config/value/write', 'config/batchWrite', 'turn/interrupt'} for m in socket.sent)


def test_shared_not_ready_never_falls_back_to_independent_stdio(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, 'find_codex', lambda *a: 'synthetic.exe')
    monkeypatch.setattr(ai, 'project_directory', lambda *a: tmp_path)
    monkeypatch.setattr(ai, '_AppServer', lambda *a: pytest.fail('silent fallback forbidden'))
    with pytest.raises(ai.AIError) as error:
        ai.generate({'prompt': 'Synthetic'}, {'enabled': True, 'execution_mode': 'desktop_shared'}, threading.Event())
    assert error.value.code == 'AI_SHARED_NOT_READY'


def test_actual_loopback_websocket_handshake_uses_auth_and_filters_desktop_events(tmp_path, monkeypatch):
    from websockets.sync.server import serve
    received = []
    authenticated = threading.Event()
    token = 'synthetic-local-only-token-1234567890'
    def authorize(connection, request):
        if request.headers.get('Authorization') != 'Bearer ' + token:
            return connection.respond(401, 'Unauthorized')
        authenticated.set()
    def handler(connection):
        try:
            for raw in connection:
                message = json.loads(raw)
                received.append(message)
                if message.get('method') == 'initialize':
                    connection.send(json.dumps({'id': message['id'], 'result': {}}))
                elif message.get('method') == 'thread/start':
                    connection.send(json.dumps({'id': message['id'], 'result': {'thread': {'id': 'thread-1'}}}))
                elif message.get('method') == 'turn/start':
                    connection.send(json.dumps({'id': message['id'], 'result': {'turn': {'id': 'turn-1'}}}))
                    connection.send(json.dumps(event('item/agentMessage/delta', thread='other-desktop-thread', delta='PRIVATE', itemId='other')))
                    connection.send(json.dumps(event('item/agentMessage/delta', delta='visible', itemId='mine')))
        except Exception:
            pass
    with serve(handler, '127.0.0.1', 0, process_request=authorize, compression=None) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = 'ws://127.0.0.1:' + str(server.socket.getsockname()[1])
        monkeypatch.setattr(shared, 'read_runtime', lambda *a, **k: {'endpoint': endpoint, 'token': token})
        rpc = shared.SharedAppServer('synthetic.exe', tmp_path, threading.Event(), 10, tmp_path)
        try:
            begin(rpc)
            assert authenticated.is_set()
            result = rpc.next_event()
            assert result['params']['delta'] == 'visible'
            assert received[0]['params']['clientInfo']['name'] == 'personal_management_shared'
            rpc.turn_id = None
        finally:
            rpc.close()
            server.shutdown()
            thread.join(5)
        assert not thread.is_alive()


def test_upgraded_release_rejects_an_alive_old_bridge(runtime):
    root, value, processes, save = runtime
    assert shared.connection_status(root, expected_bridge_executable=value['executable'])['ready']
    assert not shared.connection_status(root, expected_bridge_executable=root / 'new-release' / 'PersonalManagementCodex.exe')['ready']
    with pytest.raises(ai.AIError) as error:
        shared.read_runtime(root, expected_bridge_executable=root / 'new-release' / 'PersonalManagementCodex.exe')
    assert error.value.code == 'AI_SHARED_NOT_READY'


def test_frozen_service_requires_its_own_release_bridge(transport, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(shared.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(shared.sys, 'executable', str(tmp_path / 'PersonalManagementService.exe'))
    def runtime(*args, **kwargs):
        seen.append(kwargs)
        return {'endpoint': 'ws://127.0.0.1:49231', 'token': 'SyntheticSecret' * 4}
    monkeypatch.setattr(shared, 'read_runtime', runtime)
    rpc = opened(transport, tmp_path)
    assert seen[0]['expected_bridge_executable'] == tmp_path / 'PersonalManagementCodex.exe'
    rpc.close()
