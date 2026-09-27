"""Persistent clients against rotating synthetic localhost service instances."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading

import pytest
from mcp import Client as MCPClient

from management import client as client_module
from management.client import Client, ClientError
from management.mcp_server import create_server
from management.runtime_contract import service_contract


THREAD = '00000000-0000-4000-8000-000000000001'


class Service:
    def __init__(self, data_dir, *, token='synthetic-initial', epoch='epoch-one', revision=3):
        self.data_dir = str(data_dir.resolve())
        self.token, self.epoch, self.revision = token, epoch, revision
        self.requests = []
        self.mutations = []
        self.drop_mutation_response = False
        self.reject_state = None
        service = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                authorized = self.headers.get('Authorization') == 'Bearer ' + service.token
                service.requests.append((self.path, authorized, payload))
                if not authorized:
                    return self.reply(401, {'error': {'code': 'unauthorized', 'message': 'Expired synthetic credential'}})
                if self.path == '/v1/query/state':
                    if service.reject_state:
                        return self.reply(400, {'error': {'code': service.reject_state, 'message': 'Synthetic state error'}})
                    return self.reply(200, {'epoch': service.epoch, 'revision': service.revision,
                                            'data_dir': service.data_dir, 'service_contract': service_contract()})
                if self.path.startswith('/v1/query/'):
                    return self.reply(200, {'epoch': service.epoch, 'revision': service.revision, 'items': []})
                if self.path == '/v1/native-discussion/binding':
                    return self.reply(200, {'managed': True, 'epoch': service.epoch, 'conversation_id': 'synthetic-matter'})
                service.mutations.append((self.path, payload))
                if service.drop_mutation_response:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if self.path.startswith('/v1/commands/') and (payload['epoch'] != service.epoch or payload['expected_revision'] != service.revision):
                    return self.reply(409, {'error': {'code': 'epoch_conflict', 'message': 'Synthetic stale context'}})
                return self.reply(200, {'epoch': service.epoch, 'revision': service.revision,
                                       'job_id': 'synthetic-job', 'generation': 1, 'saved': True})
            def reply(self, status, value):
                body = json.dumps(value).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.closed = False

    def runtime(self):
        return {'port': self.http.server_address[1], 'token': self.token, 'data_dir': self.data_dir}

    def close(self):
        if not self.closed:
            self.closed = True
            self.http.shutdown()
            self.http.server_close()
            self.thread.join(timeout=1)


@pytest.fixture
def services(monkeypatch, tmp_path):
    path = tmp_path / 'synthetic-data'
    first = Service(path)
    created = [first]
    state = {'current': first, 'discovery_calls': [], 'starts': []}
    def discover(path):
        state['discovery_calls'].append(path)
        return state['current'].runtime() if state['current'] else None
    def start(path):
        state['starts'].append(path)
        next_service = Service(path)
        created.append(next_service)
        state['current'] = next_service
    monkeypatch.setattr(client_module, 'discovery', discover)
    monkeypatch.setattr(client_module, 'start_service', start)
    def replace(**kwargs):
        next_service = Service(path, **kwargs)
        created.append(next_service)
        state['current'] = next_service
        return next_service
    yield state, replace, path
    for service in created:
        service.close()


def test_state_preflight_refreshes_rotated_token_without_any_write(services):
    state, replace, path = services
    client = Client(path, autostart=False)
    state['current'].token = 'synthetic-rotated'
    reply = client.ensure_connected()
    assert reply == {'epoch': 'epoch-one', 'revision': 3, 'data_dir': str(path.resolve()), 'service_contract': service_contract()}
    assert client.runtime == state['current'].runtime()
    assert not state['current'].mutations and not state['starts']
    assert any(not authorized for _, authorized, _ in state['current'].requests)
    assert all(p == path.resolve() for p in state['discovery_calls'])


def test_same_client_reconnects_to_new_service_port_and_remembers_epoch(services):
    state, replace, path = services
    client = Client(path, autostart=False)
    previous = state['current']
    new = replace(token='synthetic-new-service', epoch='epoch-restored', revision=1)
    previous.close()
    result = client.ensure_connected()
    assert client.runtime == new.runtime()
    assert (client.epoch, client.revision) == ('epoch-restored', 1)
    assert result['epoch'] == 'epoch-restored' and not new.mutations and not state['starts']


def test_preflight_can_start_same_data_service_when_autostart_enabled(services):
    state, replace, path = services
    client = Client(path)
    state['current'].close()
    state['current'] = None
    assert client.ensure_connected()['epoch'] == 'epoch-one'
    assert state['starts'] == [path.resolve()]
    assert not state['current'].mutations


def test_query_recovers_unauthorized_without_requiring_autostart(services):
    state, replace, path = services
    client = Client(path, autostart=False)
    service = state['current']
    service.token, service.revision = 'synthetic-query-rotation', 8
    result = client.query('list', type='task')
    assert result['revision'] == 8 and client.revision == 8
    queries = [entry for entry in service.requests if entry[0] == '/v1/query/list']
    assert [entry[1] for entry in queries] == [False, True]
    assert not service.mutations


def test_command_preflight_keeps_explicit_stale_epoch_and_revision(services):
    state, replace, path = services
    client = Client(path, autostart=False)
    old = state['current']
    new = replace(token='synthetic-restored', epoch='epoch-two', revision=9)
    old.close()
    with pytest.raises(ClientError) as caught:
        client.command('create', {'title': 'synthetic'}, request_id='same-request', epoch='epoch-one', expected_revision=3)
    assert caught.value.code == 'epoch_conflict'
    assert caught.value.details['request_id'] == 'same-request'
    assert len(new.mutations) == 1
    envelope = new.mutations[0][1]
    assert envelope['epoch'] == 'epoch-one' and envelope['expected_revision'] == 3
    assert (client.epoch, client.revision) == ('epoch-two', 9)


def test_command_body_is_attempted_once_when_response_is_lost(services):
    state, replace, path = services
    client = Client(path, autostart=False)
    service = state['current']
    service.drop_mutation_response = True
    with pytest.raises(ClientError) as caught:
        client.command('create', {'title': 'synthetic'}, request_id='durable-request', epoch='epoch-one', expected_revision=3)
    assert caught.value.code == 'connection_lost'
    assert caught.value.details['request_id'] == 'durable-request'
    assert len(service.mutations) == 1


@pytest.mark.parametrize('new_epoch,new_revision', [('epoch-restored', 3), ('epoch-one', 4)])
def test_default_command_preserves_cached_business_guard_across_state_preflight(services, new_epoch, new_revision):
    state, replace, path = services
    client = Client(path, autostart=False)
    service = state['current']
    service.epoch, service.revision = new_epoch, new_revision
    with pytest.raises(ClientError) as caught:
        client.command('create', {'title': 'synthetic'}, request_id='cached-intent')
    assert caught.value.code == 'epoch_conflict'
    assert len(service.mutations) == 1
    assert service.mutations[0][1]['epoch'] == 'epoch-one'
    assert service.mutations[0][1]['expected_revision'] == 3
    assert (client.epoch, client.revision) == (new_epoch, new_revision)


@pytest.mark.parametrize('action,args', [('begin_discussion', {'text': 'synthetic', 'request_id': 'same-id'}),
                                       ('submit_candidate', {'job_id': 'synthetic-job', 'generation': 1, 'proposal': {}})])
def test_persistent_mcp_process_recovers_service_before_single_mutation(services, action, args):
    state, replace, path = services
    business = Client(path, autostart=False)
    server = create_server(path, client=business)
    async def scenario():
        async with MCPClient(server) as mcp:
            # The MCP server remains alive while its business service is replaced.
            first = state['current']
            new = replace(token='synthetic-mcp-new', epoch='epoch-one', revision=3)
            first.close()
            result = await mcp.session.call_tool(action, args, meta={'threadId': THREAD})
            assert not result.is_error and result.structured_content['saved']
            assert len(new.mutations) == 1
            assert new.mutations[0][0] == '/v1/native-discussion/' + ('begin' if action == 'begin_discussion' else 'submit')
            assert new.mutations[0][1]['provider_thread_id'] == THREAD
            assert business.runtime == new.runtime()
    asyncio.run(scenario())


def test_mcp_preflight_does_not_replay_mutation_that_loses_response(services):
    state, replace, path = services
    business = Client(path, autostart=False)
    service = state['current']
    service.drop_mutation_response = True
    async def scenario():
        async with MCPClient(create_server(path, client=business)) as mcp:
            result = await mcp.session.call_tool('begin_discussion', {'text': 'synthetic', 'request_id': 'same-durable-id'},
                                               meta={'threadId': THREAD})
            assert result.is_error
    asyncio.run(scenario())
    assert len(service.mutations) == 1
    assert service.mutations[0][1]['request_id'] == 'same-durable-id'


def test_non_transport_preflight_error_is_not_retried(services):
    state, replace, path = services
    client = Client(path, autostart=False)
    service = state['current']
    service.reject_state = 'data_unavailable'
    previous_discovery_count = len(state['discovery_calls'])
    with pytest.raises(ClientError) as caught:
        client.command('create', {}, request_id='request')
    assert caught.value.code == 'data_unavailable'
    assert not service.mutations and len(state['discovery_calls']) == previous_discovery_count
