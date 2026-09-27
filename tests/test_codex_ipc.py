"""Synthetic pipe/router peers: no real desktop startup or model requests."""
import concurrent.futures
import ctypes
from ctypes import wintypes
import json
import queue
import struct
import threading
import time

import pytest

from management import codex_ipc as ipc
from management.codex_installation import CodexInstallation


def frame(message):
    return ipc._encode_frame(message)


def response(request, result=None, **extra):
    return {'type': 'response', 'requestId': request['requestId'], 'resultType': 'success',
            'result': result or {}, **extra}


def eventually(predicate, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


class FakePipe:
    def __init__(self, identity):
        self.identity = identity
        self.incoming = queue.Queue()
        self.sent = []
        self.closed = False
        self.on_request = None
        self.assigned_id = 'router-assigned-personal-management-id'

    def server_identity(self):
        return self.identity

    def read(self, timeout):
        try:
            result = self.incoming.get(timeout=timeout)
        except queue.Empty:
            raise ipc.IpcTimeout('ipc_read_timeout', 'synthetic idle')
        if isinstance(result, BaseException):
            raise result
        return result

    def write(self, data, timeout):
        assert timeout > 0
        message = list(ipc._FrameDecoder().feed(data))[0][0]
        self.sent.append(message)
        if message.get('method') == 'initialize':
            self.push(response(message, {'clientId': self.assigned_id}))
        elif self.on_request:
            self.on_request(message)

    def push(self, message):
        self.incoming.put(frame(message))

    def close(self):
        if not self.closed:
            self.closed = True
            self.incoming.put(b'')


@pytest.fixture
def factory(monkeypatch, tmp_path):
    installation = CodexInstallation('OpenAI.Codex_26.924.2738.0_x64__2p2nqsd0c76g0',
                                     'OpenAI.Codex_2p2nqsd0c76g0', '26.924.2738.0',
                                     str(tmp_path), 'App', str(tmp_path / 'ChatGPT.exe'))
    pipe = FakePipe({'pid': 4242, 'executable': installation.executable,
                     'package_full_name': installation.package_full_name})
    monkeypatch.setattr(ipc, 'discover_codex', lambda: installation)
    monkeypatch.setattr(ipc._WindowsNamedPipe, 'open', lambda timeout: pipe)
    clients = []
    def connect():
        client = ipc.NativeDesktopIpc.connect(timeout=1)
        clients.append(client)
        return client
    yield connect, pipe, installation
    for client in clients:
        client.close()


def test_split_headers_payloads_and_multiple_unicode_frames():
    messages = [{'type': 'broadcast', 'params': {'text': '中文🙂'}}, {'type': 'response', 'requestId': 'two'}]
    raw = b''.join(frame(message) for message in messages)
    decoder = ipc._FrameDecoder()
    decoded = []
    for byte in raw:
        decoded.extend(decoder.feed(bytes([byte])))
    assert [message for message, size in decoded] == messages
    assert not decoder.buffer
    assert [item[0] for item in ipc._FrameDecoder().feed(raw)] == messages


@pytest.mark.parametrize('length', [0, ipc.MAX_FRAME_BYTES + 1, 0xffffffff])
def test_oversized_frame_is_rejected_from_header_before_payload(length):
    with pytest.raises(ipc.IpcProtocolError) as caught:
        list(ipc._FrameDecoder().feed(struct.pack('<I', length)))
    assert caught.value.code == 'ipc_frame_too_large'


@pytest.mark.parametrize('payload', [b'null', b'[]', b'broken', b'{"x":NaN}', b'\xff'])
def test_invalid_json_is_not_returned_or_exposed(payload):
    with pytest.raises(ipc.IpcProtocolError) as caught:
        list(ipc._FrameDecoder().feed(struct.pack('<I', len(payload)) + payload))
    assert caught.value.code == 'ipc_invalid_frame'
    assert 'broken' not in caught.value.message


def test_outgoing_message_bounds_and_nonfinite_values(monkeypatch):
    monkeypatch.setattr(ipc, 'MAX_FRAME_BYTES', 10)
    with pytest.raises(ipc.IpcProtocolError, match='大小限制'):
        frame({'secret': 'never print this payload'})
    with pytest.raises(ipc.IpcProtocolError, match='编码'):
        frame({'number': float('nan')})


def test_connect_verifies_identity_before_sending_and_uses_assigned_id(factory):
    connect, pipe, installation = factory
    client = connect()
    init = pipe.sent[0]
    assert init['method'] == 'initialize' and init['version'] == 0
    assert init['params'] == {'clientType': 'personal-management'}
    assert init['sourceClientId'] == 'initializing-client'
    assert client.client_id == pipe.assigned_id
    assert client.server_identity['pid'] == 4242
    assert client.server_pid == 4242
    with pytest.raises(AttributeError):
        client.client_id = 'desktop'
    client.server_identity['pid'] = 99
    assert client.server_pid == 4242
    pipe.on_request = lambda request: pipe.push(response(request, {'owner': True}, handledByClientId='real-owner'))
    result = client.request('thread-owner-discovery', {'hostId': 'local', 'conversationId': 'synthetic'}, 1)
    assert result['resultType'] == 'success' and result['handledByClientId'] == 'real-owner'
    assert result['result'] == {'owner': True}
    assert pipe.sent[-1]['sourceClientId'] == pipe.assigned_id


@pytest.mark.parametrize('field,value', [('pid', 0), ('package_full_name', 'Other.Package_1'),
                                        ('executable', 'C:/unrelated/ChatGPT.exe')])
def test_fake_server_is_rejected_without_initialize(factory, field, value):
    connect, pipe, installation = factory
    pipe.identity[field] = value
    with pytest.raises(ipc.IpcIdentityError):
        connect()
    assert pipe.closed and not pipe.sent


def test_stale_registration_is_not_accepted(factory):
    connect, pipe, installation = factory
    stale = CodexInstallation('old-package', installation.package_family, '1.0.0.0',
                               installation.install_location, 'App', installation.executable)
    with pytest.raises(ipc.IpcIdentityError) as caught:
        ipc.NativeDesktopIpc.connect(stale)
    assert caught.value.code == 'ipc_installation_changed'
    assert not pipe.sent


@pytest.mark.parametrize('assigned', ['', None, 42, 'bad\x00identity'])
def test_initialize_must_assign_a_valid_peer_identity(factory, assigned):
    connect, pipe, _ = factory
    pipe.assigned_id = assigned
    with pytest.raises(ipc.IpcProtocolError) as caught:
        connect()
    assert caught.value.code == 'ipc_initialize_failed' and pipe.closed


def test_concurrent_requests_match_ids_not_arrival_order(factory):
    connect, pipe, _ = factory
    client = connect()
    requests = []
    def on_request(request):
        requests.append(request)
        if len(requests) == 2:
            for item in reversed(requests):
                pipe.push(response(item, {'echo': item['params']['value']}))
    pipe.on_request = on_request
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.request, 'synthetic-read', {'value': number}, 7, 'real-owner') for number in [1, 2]]
        assert [future.result(timeout=2)['result']['echo'] for future in futures] == [1, 2]
    assert len({request['requestId'] for request in requests}) == 2
    assert all(request['targetClientId'] == 'real-owner' for request in requests)


def test_timeout_is_unknown_and_never_retries_or_consumes_late_reply(factory):
    connect, pipe, _ = factory
    client = connect()
    with pytest.raises(ipc.IpcTimeout) as caught:
        client.request('synthetic-read', {}, 1, timeout=0.03)
    first = pipe.sent[-1]
    assert caught.value.code == 'ipc_response_timeout'
    assert caught.value.outcome_unknown and caught.value.request_id == first['requestId']
    pipe.push(response(first, {'wrong': 'late'}))
    pipe.on_request = lambda request: pipe.push(response(request, {'fresh': True}))
    assert client.request('synthetic-read', {}, 1)['result'] == {'fresh': True}
    assert len(pipe.sent) == 3  # Initialize and exactly two caller requests.


def test_disconnect_wakes_pending_request_without_retry(factory):
    connect, pipe, _ = factory
    client = connect()
    pipe.on_request = lambda request: pipe.close()
    with pytest.raises(ipc.IpcDisconnected) as caught:
        client.request('synthetic-read', {}, 1, timeout=1)
    assert caught.value.outcome_unknown and len(pipe.sent) == 2


def test_failed_write_is_unknown_even_without_response_wait(factory, monkeypatch):
    connect, pipe, _ = factory
    client = connect()
    def write(data, timeout):
        raise ipc.IpcDisconnected('ipc_disconnected', 'synthetic partial write')
    monkeypatch.setattr(pipe, 'write', write)
    with pytest.raises(ipc.IpcDisconnected) as caught:
        client.request('synthetic-read', {}, 1)
    assert caught.value.outcome_unknown and caught.value.request_id and pipe.closed


def test_remote_error_retains_envelope(factory):
    connect, pipe, _ = factory
    client = connect()
    pipe.on_request = lambda request: pipe.push({**response(request), 'resultType': 'error', 'error': 'no-client-found'})
    result = client.request('thread-owner-discovery', {}, 1)
    assert result['resultType'] == 'error' and result['error'] == 'no-client-found'


def test_discovery_always_declines_work_and_broadcast_is_queued(factory):
    connect, pipe, _ = factory
    client = connect()
    pipe.push({'type': 'client-discovery-request', 'requestId': 'discover', 'method': 'start-turn'})
    event = {'type': 'broadcast', 'method': 'thread-stream-state-changed', 'version': 11,
             'params': {'private': 'kept in memory only'}}
    pipe.push(event)
    assert client.next_event(timeout=1) == event
    assert pipe.sent[-1] == {'type': 'client-discovery-response', 'requestId': 'discover',
                            'response': {'canHandle': False}}


def test_targeted_broadcast_uses_own_identity_and_close_never_interrupts(factory):
    connect, pipe, _ = factory
    client = connect()
    assert client.broadcast('thread-stream-following-changed', {'following': True}, 1, ['real-owner']) is None
    sent = pipe.sent[-1]
    assert sent['type'] == 'broadcast' and sent['sourceClientId'] == pipe.assigned_id
    assert sent['targetClientIds'] == ['real-owner'] and 'requestId' not in sent
    before = list(pipe.sent)
    client.close()
    client.close()
    assert pipe.sent == before and pipe.closed and not client._reader.is_alive()


@pytest.mark.parametrize('targets', [[], [''], ['owner', 'owner'], 'owner'])
def test_broadcast_needs_explicit_valid_distinct_targets(factory, targets):
    connect, pipe, _ = factory
    client = connect()
    with pytest.raises(ValueError):
        client.broadcast('synthetic', {}, 1, targets)
    assert len(pipe.sent) == 1


@pytest.mark.parametrize('limit_name,limit', [('MAX_EVENTS', 2), ('MAX_EVENT_BYTES', 80)])
def test_event_backlog_has_count_and_byte_bounds(factory, monkeypatch, limit_name, limit):
    connect, pipe, _ = factory
    monkeypatch.setattr(ipc, limit_name, limit)
    client = connect()
    for index in range(3):
        pipe.push({'type': 'broadcast', 'method': 'synthetic', 'params': {'index': index}})
    eventually(lambda: client._stop.is_set())
    with pytest.raises(ipc.IpcProtocolError) as caught:
        client.next_event(0.1)
    assert caught.value.code == 'ipc_events_full'
    assert client._event_bytes == 0 and not client._events and pipe.closed


def test_partial_frame_then_eof_is_reported(factory):
    connect, pipe, _ = factory
    client = connect()
    pipe.incoming.put(struct.pack('<I', 50) + b'{')
    pipe.close()
    eventually(lambda: client._stop.is_set())
    with pytest.raises(ipc.IpcProtocolError) as caught:
        client.next_event(0.1)
    assert caught.value.code == 'ipc_truncated_frame'


def test_event_timeout_is_not_a_disconnect(factory):
    connect, pipe, _ = factory
    client = connect()
    with pytest.raises(ipc.IpcTimeout) as caught:
        client.next_event(0.02)
    assert caught.value.code == 'ipc_event_timeout' and not pipe.closed


def test_request_limit_and_reserved_initialization(factory, monkeypatch):
    connect, pipe, _ = factory
    client = connect()
    monkeypatch.setattr(ipc, 'MAX_PENDING_REQUESTS', 0)
    with pytest.raises(ipc.IpcError) as caught:
        client.request('synthetic', {}, 1)
    assert caught.value.code == 'ipc_requests_full'
    with pytest.raises(ipc.IpcProtocolError, match='统一完成'):
        client.request('initialize', {'clientType': 'desktop'}, 0)
    assert len(pipe.sent) == 1


@pytest.mark.parametrize('timeout', [0, -1, True, float('inf'), float('nan'), 301])
def test_timeouts_are_finite_positive_and_bounded(timeout):
    with pytest.raises(ValueError):
        ipc._seconds(timeout)


@pytest.mark.skipif(not ipc._WINDOWS, reason='Synthetic Windows overlapped I/O')
def test_native_write_timeout_cancels_then_reaps_before_freeing_handle():
    calls = []
    class Kernel:
        def CreateEventW(self, *args):
            return 22
        def WriteFile(self, handle, buffer, size, count, overlapped):
            calls.append(('write-overlapped', overlapped is not None))
            ctypes.set_last_error(997)
            return False
        def GetOverlappedResultEx(self, handle, overlapped, count, timeout, alertable):
            calls.append(('wait', timeout))
            ctypes.set_last_error(258 if len([call for call in calls if call[0] == 'wait']) == 1 else 995)
            return False
        def CancelIoEx(self, handle, overlapped):
            calls.append(('cancel', handle))
            return True
        def CloseHandle(self, handle):
            calls.append(('close', handle))
            return True
    transport = ipc._WindowsNamedPipe(Kernel(), 11)
    with pytest.raises(ipc.IpcTimeout) as caught:
        transport.write(b'frame', 0.01)
    assert caught.value.code == 'ipc_write_timeout' and caught.value.outcome_unknown
    assert ('write-overlapped', True) in calls
    assert ('cancel', 11) in calls and ('wait', 250) in calls
    assert calls.index(('cancel', 11)) < calls.index(('close', 22))
    assert transport._active == 0
    transport.close()
    assert ('close', 11) in calls


@pytest.mark.skipif(not ipc._WINDOWS, reason='Synthetic Windows package identity')
def test_native_identity_requires_package_identity():
    class Kernel:
        def GetNamedPipeServerProcessId(self, handle, target):
            ctypes.cast(target, ctypes.POINTER(wintypes.ULONG))[0] = 42
            return True
        def OpenProcess(self, *args):
            return 12
        def QueryFullProcessImageNameW(self, handle, flags, path, size):
            path.value = 'C:/same/ChatGPT.exe'
            return True
        def GetPackageFullName(self, *args):
            return 15700  # APPMODEL_ERROR_NO_PACKAGE
        def CloseHandle(self, handle):
            self.closed = handle
    kernel = Kernel()
    with pytest.raises(ipc.IpcIdentityError) as caught:
        ipc._WindowsNamedPipe(kernel, 11).server_identity()
    assert caught.value.code == 'ipc_unregistered_server' and kernel.closed == 12
