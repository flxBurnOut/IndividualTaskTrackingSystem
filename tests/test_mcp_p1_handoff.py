"""P1 adapter regressions across the real SDK, HTTP service and Core/SQLite.

Every fixture owns an isolated loopback server. No scheduler, provider or GUI.
"""
import asyncio
import copy
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import uuid

import pytest
from mcp import Client as MCPClient, StdioServerParameters

from management import ai
from management.client import Client
from management.context_service import MAX_PAGE_BYTES, size
from management.core import Core
from management.mcp_server import create_server
from management.service import Server
from management.storage import encode, now


DAY = '2038-05-01'
LONG = 'Synthetic 中文 "quoted" \\ material\n' * 1800
FORMAT = 'business-query-json/1'
THREAD = '00000000-0000-4000-8000-000000000331'


@pytest.fixture
def business(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, 'generate', lambda *a, **k: pytest.fail('MCP reads must not start a model'))
    core = Core(tmp_path / 'synthetic')
    server = Server(core, secrets.token_urlsafe(32))
    runtime = core.root / 'runtime.json'
    runtime.write_text(encode({'host': '127.0.0.1', 'port': server.server_port, 'token': server.token,
                              'pid': os.getpid(), 'data_dir': str(core.root), 'started_at': now()}), encoding='utf-8')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(core.root, autostart=False)
    try:
        yield core, client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        runtime.unlink()
        assert core._desktop_gateway is None


def guards(client):
    state = client.state()
    return {'request_id': str(uuid.uuid4()), 'epoch': state['epoch'], 'expected_revision': state['revision']}


def create(client, title='Synthetic note', **fields):
    return client.command('create', {'type': 'note', 'title': title, **fields}, **guards(client))['result']['entity']


async def call(sdk, tool, args):
    result = await sdk.call_tool(tool, args)
    assert not result.is_error, '\n'.join(b.text for b in result.content if hasattr(b, 'text'))
    return result.structured_content


def error(result, code):
    assert result.is_error
    text = '\n'.join(b.text for b in result.content if hasattr(b, 'text'))
    assert '"code": "' + code + '"' in text, text


async def collect(sdk, page, fragments=None):
    if page.get('format') != FORMAT:
        return page
    pieces = list(fragments or [])
    expected_query = (page['query'], page['query_params'], page['version'])
    for _ in range(300):
        assert size(page) <= MAX_PAGE_BYTES
        assert (page['query'], page['query_params'], page['version']) == expected_query
        pieces.append(page['json_fragment'])
        if page['complete']:
            assert page['next_fragment_offset'] is None and page['continuation'] is None
            return json.loads(''.join(pieces))
        step = page['continuation']
        assert step['tool'] == 'read_business_page'
        assert step['arguments']['offset'] == page['next_fragment_offset'] > page['fragment_offset']
        page = await call(sdk, step['tool'], step['arguments'])
    pytest.fail('Business-result fragments did not terminate')


def test_integer_minutes_survive_real_sdk_http_round_trip(business):
    core, client = business
    task = client.command('create', {'type': 'task', 'title': 'Synthetic plan task'}, **guards(client))['result']['entity']
    async def scenario():
        async with MCPClient(create_server(core.root, client=client)) as sdk:
            receipt = await call(sdk, 'save_plan', {'date': DAY, 'mode': 'no_precise_time',
                'blocks': [{'target_id': task['id'], 'minutes': 20}], **guards(client)})
            saved = receipt['result']['entity']['data']['blocks'][0]['minutes']
            assert saved == 20 and type(saved) is int
    asyncio.run(scenario())


@pytest.mark.parametrize('minutes', [True, False, 20.0, 20.5, '20', 0, -1, 1441])
def test_invalid_minute_types_and_bounds_leave_no_plan(business, minutes):
    core, client = business
    task = client.command('create', {'type': 'task', 'title': 'Synthetic task'}, **guards(client))['result']['entity']
    before = client.state()
    async def scenario():
        async with MCPClient(create_server(core.root, client=client)) as sdk:
            response = await sdk.call_tool('save_plan', {'date': DAY, 'mode': 'no_precise_time',
                'blocks': [{'target_id': task['id'], 'minutes': minutes}], **guards(client)})
            assert response.is_error
    asyncio.run(scenario())
    assert client.state() == before


@pytest.mark.parametrize('extra', [{}, {'minutes': None}, {'start': '09:00', 'end': '09:20'}])
def test_optional_and_timed_plan_blocks_keep_existing_behavior(business, extra):
    core, client = business
    task = client.command('create', {'type': 'task', 'title': 'Synthetic task'}, **guards(client))['result']['entity']
    async def scenario():
        async with MCPClient(create_server(core.root, client=client)) as sdk:
            receipt = await call(sdk, 'save_plan', {'date': DAY,
                'mode': 'standard' if extra.get('start') else 'no_precise_time',
                'blocks': [{'target_id': task['id'], **extra}], **guards(client)})
            block = receipt['result']['entity']['data']['blocks'][0]
            assert block.get('minutes') == (20 if extra.get('start') else None)
    asyncio.run(scenario())


@pytest.mark.parametrize('view', ['get', 'receipt'])
def test_large_get_and_receipt_reconstruct_the_exact_original_view(business, view):
    core, client = business
    receipt = client.command('create', {'type': 'note', 'title': 'Synthetic large note',
                                      'data': {'content': LONG}}, **guards(client))
    target = receipt['result']['entity']
    child = create(client, 'Synthetic child', parent_id=target['id'])
    client.command('link', {'source_id': target['id'], 'target_id': child['id'], 'kind': 'references'}, **guards(client))
    params = {'id': target['id']} if view == 'get' else {'request_id': receipt['request_id']}
    raw, before = client.query(view, **params), client.state()
    assert size(raw) > MAX_PAGE_BYTES
    async def scenario():
        async with MCPClient(create_server(core.root, client=client)) as sdk:
            first = await call(sdk, 'get_entity' if view == 'get' else 'recover_receipt', params)
            assert first['format'] == FORMAT and not first['complete']
            if view == 'receipt':
                assert first['found'] is True and first['request_id'] == receipt['request_id']
                assert first['receipt_metadata'] == {key: raw['receipt'][key] for key in ('request_id', 'epoch', 'revision')}
            assert await collect(sdk, first) == raw
    asyncio.run(scenario())
    assert client.state() == before
    with core.store.connect() as c:
        assert c.execute('SELECT count(*) FROM context_operations').fetchone()[0] == 0


@pytest.mark.parametrize('case', ['archived', 'draft', 'cancelled', 'offset', 'parent', 'parent_null', 'default', 'extra_filters'])
def test_large_lists_preserve_original_filters_order_and_business_page(business, case):
    core, client = business
    parent = create(client, 'Synthetic root', data={'content': LONG})
    child = create(client, 'Synthetic child', parent_id=parent['id'], data={'content': LONG})
    create(client, 'Synthetic grandchild', parent_id=child['id'], data={'content': LONG})
    create(client, 'Synthetic draft', status='draft', data={'content': LONG})
    create(client, 'Synthetic cancelled', status='cancelled', data={'content': LONG})
    archived = create(client, 'Synthetic archived', data={'content': LONG})
    client.command('archive', {'id': archived['id'], 'version': archived['version'], 'archived': True}, **guards(client))
    options = {'archived': {'archived': True}, 'draft': {'status': 'draft'}, 'cancelled': {'status': 'cancelled'},
        'offset': {'offset': 1, 'limit': 1}, 'parent': {'parent_id': parent['id']},
        'parent_null': {'parent_id': None}, 'default': {},
        'extra_filters': {'types': ['note'], 'exclude_statuses': ['draft'], 'search': 'Synthetic'}}
    params = options[case]
    raw = client.query('list', **params)
    assert size(raw) > MAX_PAGE_BYTES
    async def scenario():
        async with MCPClient(create_server(core.root, client=client)) as sdk:
            # Explicit parent_id=None differs from omitting parent_id in Core;
            # query_business must preserve that distinction, including on resume.
            tool, args = ('query_business', {'name': 'list', 'params': params}) if case in {'parent_null', 'extra_filters'} else ('list_entities', params)
            first = await call(sdk, tool, args)
            restored = await collect(sdk, first)
            assert restored == raw
            assert [item['id'] for item in restored['items']] == [item['id'] for item in raw['items']]
            assert restored['next_offset'] == raw['next_offset']
    asyncio.run(scenario())


def test_source_content_keeps_source_position_and_next_business_page(business):
    core, client = business
    source = client.command('add_source', {'kind': 'notice', 'title': 'Synthetic material',
        'text': '合成读取文本，不是用户资料。' * 4000}, **guards(client))['result']['entity']
    params = {'id': source['id'], 'offset': 123, 'limit': 18000}
    raw, before = client.query('source_content', **params), client.state()
    assert size(raw) > MAX_PAGE_BYTES and raw['next_offset'] is not None
    async def scenario():
        async with MCPClient(create_server(core.root, client=client)) as sdk:
            first = await call(sdk, 'query_business', {'name': 'source_content', 'params': params})
            assert first['query_params'] == params and first['entity_id'] == source['id']
            restored = await collect(sdk, first)
            assert restored == raw
            following = {**params, 'offset': restored['next_offset']}
            next_page = await call(sdk, 'query_business', {'name': 'source_content', 'params': following})
            assert await collect(sdk, next_page) == client.query('source_content', **following)
    asyncio.run(scenario())
    assert client.state() == before


def test_continuation_survives_mcp_process_restart(business):
    core, client = business
    item = create(client, data={'content': LONG})
    raw = client.query('get', id=item['id'])
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'management', '--mcp', '--data-dir', str(core.root)], env=env)
    async def scenario():
        async with MCPClient(params) as first_process:
            first = await call(first_process, 'get_entity', {'id': item['id']})
        async with MCPClient(params) as second_process:
            continuation = first['continuation']
            second = await call(second_process, continuation['tool'], continuation['arguments'])
            assert await collect(second_process, second, [first['json_fragment']]) == raw
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['result', 'query', 'epoch'])
def test_changed_result_query_or_data_epoch_cannot_splice_old_fragments(business, change):
    core, client = business
    item = create(client, data={'content': LONG})
    async def scenario():
        async with MCPClient(create_server(core.root, client=client)) as sdk:
            first = await call(sdk, 'get_entity', {'id': item['id']})
            step = copy.deepcopy(first['continuation'])
            if change == 'result':
                client.command('update', {'id': item['id'], 'version': item['version'],
                    'patch': {'title': 'Changed synthetic title'}}, **guards(client))
            elif change == 'query':
                step['arguments']['params']['display'] = False
            else:
                with core.store.connect() as c:
                    core.store.set_meta(c, 'epoch', str(uuid.uuid4()))
            error(await sdk.call_tool(step['tool'], step['arguments']), 'business_page_changed')
    asyncio.run(scenario())


@pytest.mark.parametrize('changes', [{'offset': True}, {'offset': 1.5}, {'offset': -1},
    {'offset': 10**9}, {'offset': 1, 'version': None}, {'version': ''}, {'version': 'bad'}])
def test_reader_rejects_invalid_fragment_cursors(business, changes):
    core, client = business
    item = create(client, data={'content': LONG})
    async def scenario():
        async with MCPClient(create_server(core.root, client=client)) as sdk:
            first = await call(sdk, 'get_entity', {'id': item['id']})
            args = first['continuation']['arguments'] | changes
            response = await sdk.call_tool('read_business_page', args)
            assert response.is_error
    asyncio.run(scenario())


def test_managed_caller_cannot_use_ordinary_result_reader(business):
    core, client = business
    item = create(client, data={'content': LONG})
    async def scenario():
        async with MCPClient(create_server(core.root, client=client)) as sdk:
            first = await call(sdk, 'get_entity', {'id': item['id']})
            # Link after the first read: permission must be rechecked per page.
            stamp, state = now(), client.state()
            with core.store.connect() as c:
                c.execute('INSERT INTO conversations(id,scope_key,scope,provider_thread_id,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                    ('synthetic', 'synthetic', encode({'kind': 'general'}), THREAD, stamp, stamp))
                c.execute("INSERT INTO conversation_bindings VALUES (?,?,?,?,?,'active',NULL,?,?)",
                    ('synthetic', THREAD, state['epoch'], str(core.root / 'synthetic-workspace'), 'desktop_mcp_v3', stamp, stamp))
            step = first['continuation']
            error(await sdk.session.call_tool(step['tool'], step['arguments'], meta={'threadId': THREAD}), 'discussion_query_forbidden')
    asyncio.run(scenario())


def test_small_views_are_unchanged_and_unrelated_queries_not_opened(business):
    core, client = business
    item = create(client)
    async def scenario():
        async with MCPClient(create_server(core.root, client=client)) as sdk:
            assert await call(sdk, 'get_entity', {'id': item['id']}) == client.query('get', id=item['id'])
            assert await call(sdk, 'recover_receipt', {'request_id': 'missing'}) == client.query('receipt', request_id='missing')
            result = await sdk.call_tool('read_business_page', {'name': 'settings', 'params': {}})
            assert result.is_error
    asyncio.run(scenario())


def test_large_today_retains_explicit_current_plan_identity(business):
    core, client = business
    plan = client.command('create_plan', {'date':DAY,'mode':'rest','blocks':[],
                          'source_text':LONG}, **guards(client))['result']['entity']
    assert size(client.query('today', date=DAY)) > MAX_PAGE_BYTES
    async def scenario():
        async with MCPClient(create_server(core.root, client=client)) as sdk:
            result = await call(sdk,'query_business',{'name':'today','params':{'date':DAY}})
            assert result['requested_view']=='today' and 'context' in result
            assert result['current_plan_id']==plan['id']
            assert result['current_plan_version']==plan['version']
            assert size(result)<=MAX_PAGE_BYTES
    asyncio.run(scenario())
