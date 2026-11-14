"""Real stdio processes sharing synthetic Beta HTTP/Core state.

All service crashes below target only a verified process in the standard
check workspace. No external Agent account, daily-use service or data is used.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import threading
import uuid

import psutil
import pytest
from mcp import Client as MCPClient, StdioServerParameters

from management.agent_connections import launch_spec, probe_connection
from management.client import Client, ClientError
from management.data_space import require_beta_dir


def stop_test_service(root):
    root = require_beta_dir(root)
    assert root.is_relative_to(Path(__file__).resolve().parents[1] / '.build' / 'checks')
    path = root / 'runtime.json'
    if not path.exists():
        return
    record = json.loads(path.read_text('utf-8'))
    assert Path(record['data_dir']).resolve() == root
    try:
        process = psutil.Process(record['pid'])
        args = process.cmdline()
        assert '--service' in args and str(root) in args
        assert Path(args[0]).resolve() in {Path(sys.executable).resolve(), Path(sys._base_executable).resolve()}
        assert Path(process.environ()['PYTHONPATH']).resolve() == Path(__file__).resolve().parents[1] / 'src'
        process.terminate()
        process.wait(timeout=10)
    except psutil.NoSuchProcess:
        pass


@pytest.fixture
def space(tmp_path):
    root = tmp_path / 'multi-client-synthetic'
    try:
        yield root
    finally:
        stop_test_service(root)


def parameters(root, profile):
    spec = launch_spec(root, profile)
    return StdioServerParameters(command=spec['command'], args=spec['args'],
        env={**os.environ, **spec['env'], 'PERSONAL_MANAGEMENT_NO_TRAY': '1'})


def value(result):
    assert not result.is_error, result.content
    assert isinstance(result.structured_content, dict)
    return result.structured_content


def error_text(result):
    return '\n'.join(item.text for item in result.content if hasattr(item, 'text'))


def test_real_mcp_cold_start_idempotency_conflicts_disconnect_and_backend_restart(space):
    profiles = ['antigravity', 'generic', 'codex', 'antigravity']
    initial, creates, updates, final = {}, {}, {}, {}
    pids = []

    async def scenario():
        barrier = asyncio.Barrier(len(profiles))
        first_closed, backend_stopped = asyncio.Event(), asyncio.Event()

        async def agent(index, profile):
            async with MCPClient(parameters(space, profile)) as sdk:
                listing = await sdk.list_tools()
                names = {tool.name for tool in listing.tools}
                assert ('begin_discussion' in names) == (profile == 'codex')
                assert ('submit_candidate' in names) == (profile == 'codex')
                # Other hosts need not use UUID thread IDs or Codex metadata.
                metadata = None if profile == 'codex' else {'threadId': 'foreign-host-thread',
                    'x-codex-turn-metadata': {'turn_id': 'not-a-codex-turn'}}
                initial[index] = value(await sdk.session.call_tool('query_business', {'name': 'state'}, meta=metadata))
                await barrier.wait()
                state = initial[index]
                creates[index] = value(await sdk.call_tool('create_entity', {
                    'type': 'task', 'title': 'Concurrent synthetic task',
                    'request_id': 'multi-client-shared-request', 'epoch': state['epoch'],
                    'expected_revision': state['revision']}))
                await barrier.wait()
                entity = creates[index]['result']['entity']
                updates[index] = await sdk.call_tool('update_entity', {
                    'id': entity['id'], 'version': entity['version'],
                    'patch': {'title': 'Edit from synthetic client ' + str(index)},
                    'request_id': 'multi-client-edit-' + str(index), 'epoch': state['epoch'],
                    'expected_revision': 1})
                await barrier.wait()
                if index == 0:
                    status = await asyncio.to_thread(Client(space, autostart=False)._request, 'query', 'runtime_status', {})
                    assert {item['agent'] for item in status['client_connections']} >= {'antigravity', 'codex', 'generic'}
                    ids = [item['client_instance_id'] for item in status['client_connections']]
                    assert len(ids) == len(set(ids))
                    pids.append(json.loads((space / 'runtime.json').read_text('utf-8'))['pid'])
                else:
                    await first_closed.wait()
                    # The remaining MCP processes must still work after one exits.
                    still_live = value(await sdk.call_tool('query_business', {'name': 'state'}))
                    assert still_live['revision'] == 2
                    # Only client 1 triggers a verified synthetic service crash.
                    # All remaining MCP processes then reconnect concurrently.
                    await survivors.wait()
                    if index == 1:
                        await asyncio.to_thread(stop_test_service, space)
                        backend_stopped.set()
                    await backend_stopped.wait()
                    final[index] = value(await sdk.call_tool('query_business', {'name': 'state'}))
                    recovered = value(await sdk.call_tool('recover_receipt', {'request_id': 'multi-client-shared-request'}))
                    assert recovered['found'] and recovered['receipt']['result']['entity']['id'] == entity['id']
            if index == 0:
                first_closed.set()
        survivors = asyncio.Barrier(len(profiles) - 1)
        await asyncio.wait_for(asyncio.gather(*(agent(i, p) for i, p in enumerate(profiles))), timeout=90)

    asyncio.run(scenario())
    assert {item['epoch'] for item in initial.values()} == {initial[0]['epoch']}
    assert {item['revision'] for item in initial.values()} == {0}
    assert len({item['result']['entity']['id'] for item in creates.values()}) == 1
    assert sum(not item['replayed'] for item in creates.values()) == 1
    assert sum(not item.is_error for item in updates.values()) == 1
    assert all('revision_conflict' in error_text(item) for item in updates.values() if item.is_error)
    assert all(item['revision'] == 2 and item['epoch'] == initial[0]['epoch'] for item in final.values())
    client = Client(space, autostart=False)
    assert client.runtime['pid'] != pids[0]
    assert client.query('list', type='task')['total'] == 1


def test_twelve_http_clients_recover_after_concurrent_admission(space):
    first = Client(space)
    clients = [Client(space, entrance='mcp', mcp_client='generic') for _ in range(12)]
    barrier = threading.Barrier(len(clients))
    def read(client):
        barrier.wait(timeout=10)
        return [client.query('list', type='task') for _ in range(5)]
    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        results = list(pool.map(read, clients))
    assert all(page['total'] == 0 for pages in results for page in pages)
    assert first.state()['revision'] == 0
    assert len({client.runtime['pid'] for client in clients}) == 1


@pytest.mark.parametrize('profile', ['antigravity', 'generic', 'codex'])
def test_self_check_is_read_only_and_not_external_agent_verification(space, profile):
    client = Client(space)
    before = client.state()
    result = asyncio.run(probe_connection(space, profile))
    assert result['ok'] and result['handshake'] and result['tools_listed'] and result['business_read']
    assert result['external_agent_verified'] is False
    assert result['managed_discussions'] == (profile == 'codex')
    assert result['service_contract']['channel'] == 'beta'
    assert client.state() == before



def test_antigravity_profile_accepts_standard_initialize_protocol(space):
    async def scenario():
        async with MCPClient(parameters(space, 'antigravity'), mode='legacy') as sdk:
            listing = await sdk.list_tools()
            assert {'query_business', 'recover_receipt'} <= {tool.name for tool in listing.tools}
            state = value(await sdk.call_tool('query_business', {'name': 'state'}))
            assert state['service_contract']['channel'] == 'beta'
            bad = await sdk.call_tool('update_entity', {'id': 'missing-fields'})
            assert bad.is_error
            # Invalid tool arguments affect this call only; the transport survives.
            assert value(await sdk.call_tool('query_business', {'name': 'state'})) == state
    asyncio.run(scenario())
