"""Real SDK ClientSession metadata with a synthetic business service only."""
import asyncio
import json

import pytest
from mcp import Client as MCPClient

from management.client import ClientError
from management.discussion_mcp import create_server as legacy_server
from management.mcp_server import create_server


THREAD_A = '00000000-0000-4000-8000-000000000001'
THREAD_B = '00000000-0000-4000-8000-000000000002'
TURN_A = '00000000-0000-4000-8000-000000000003'


class Business:
    def __init__(self):
        self.calls = []
        self.bindings = {THREAD_A: {'managed': True, 'conversation_id': 'matter-a', 'epoch': 'epoch-a'},
                         THREAD_B: {'managed': True, 'conversation_id': 'matter-b', 'epoch': 'epoch-b'}}

    def _request(self, category, action, data, timeout=35):
        self.calls.append((category, action, data))
        if category == 'discussion':
            return {'legacy': True, 'action': action}
        assert category == 'native-discussion'
        binding = self.bindings.get(data['provider_thread_id'], {'managed': False})
        if action == 'binding':
            return dict(binding)
        if not binding['managed']:
            raise ClientError('discussion_binding_required', 'This synthetic thread has no matter binding')
        if action in {'query', 'context'} and data['name'] == 'settings':
            raise ClientError('discussion_query_forbidden', 'Managed settings are not exposed')
        if action == 'context' and data['params'].get('operation_id') == 'other-matter-operation':
            raise ClientError('discussion_context_mismatch', 'Operation belongs to another matter')
        return {**binding, 'action': action, 'name': data.get('name'),
                'provider_thread_id': data['provider_thread_id'], 'job_id': 'synthetic-job', 'generation': 1}

    def query(self, name, **params):
        self.calls.append(('query', name, params))
        if name == 'capabilities':
            return {'commands': ['create', 'settings', 'create_job', 'apply_proposal']}
        return {'epoch': 'ordinary-epoch', 'revision': 0, 'items': []}

    def command(self, name, payload, **guard):
        self.calls.append(('command', name, {'payload': payload, **guard}))
        return {'ordinary_write': True, 'name': name}


def run(scenario):
    return asyncio.run(scenario())


def error_text(result):
    return '\n'.join(block.text for block in result.content if hasattr(block, 'text'))


def test_metadata_routes_begin_candidate_and_context_without_identity_parameters(tmp_path):
    business = Business()
    server = create_server(tmp_path, client=business)
    async def scenario():
        async with MCPClient(server) as client:
            tools = await client.list_tools()
            tools = tools.tools if hasattr(tools, 'tools') else tools
            for tool in tools:
                assert not {'provider_thread_id', 'provider_turn_id', 'turn_id', 'thread_id', 'conversation_id'} & set(tool.input_schema.get('properties', {}))
            started = await client.session.call_tool('begin_discussion', {'text': 'synthetic request', 'request_id': 'request-a'},
                                                    meta={'threadId': THREAD_A, 'sessionId': 'synthetic-session'})
            assert not started.is_error
            assert started.structured_content['conversation_id'] == 'matter-a'
            submitted = await client.session.call_tool('submit_candidate', {'job_id': 'synthetic-job', 'generation': 1,
                                      'proposal': {'summary': 'synthetic', 'actions': [], 'unknowns': [], 'sources': []}},
                                      meta={'threadId': THREAD_A})
            assert not submitted.is_error and submitted.structured_content['action'] == 'submit'
            read = await client.session.call_tool('query_context', {'operation_id': 'owned-operation', 'collection': 'records'},
                                                 meta={'threadId': THREAD_A})
            assert read.structured_content['action'] == 'context'
    run(scenario)
    assert not any(call[0] in {'query', 'command'} for call in business.calls)


def test_bound_begin_context_never_creates_general_context(tmp_path):
    business = Business()
    async def scenario():
        async with MCPClient(create_server(tmp_path, client=business)) as client:
            result = await client.session.call_tool('begin_context', {}, meta={'threadId': THREAD_A})
            assert not result.is_error and result.structured_content['name'] == 'begin_context'
    run(scenario)
    assert [call[1] for call in business.calls] == ['binding', 'query']
    assert business.calls[-1][2]['name'] == 'begin_context'


WRITE_CASES = [
    ('create_entity', {'type': 'task', 'title': 'synthetic'}),
    ('update_entity', {'id': 'synthetic', 'version': 1, 'patch': {'title': 'synthetic'}}),
    ('record_feedback', {'target_id': 'synthetic', 'business_date': '2026-01-01', 'dimensions': {}, 'source_text': 'synthetic'}),
    ('save_plan', {'date': '2026-01-01', 'mode': 'standard', 'blocks': []}),
    ('create_checkin', {'date': '2026-01-01'}),
    ('respond_checkin', {'id': 'synthetic', 'answers': [], 'source_text': 'synthetic'}),
    ('save_review', {'start': '2026-01-01', 'end': '2026-01-01', 'text': 'synthetic'}),
    ('execute_command', {'name': 'create', 'payload': {}}),
    ('execute_command', {'name': 'settings', 'payload': {}}),
    ('execute_command', {'name': 'create_job', 'payload': {'kind': 'ai'}}),
    ('execute_command', {'name': 'apply_proposal', 'payload': {}}),
    ('execute_command', {'name': 'create_task_batch', 'payload': {}}),
    ('execute_command', {'name': 'split_task', 'payload': {}}),
    ('execute_command', {'name': 'refile_source', 'payload': {'id': 'synthetic-source', 'version': 1, 'library_subdir': '课件'}}),
]


@pytest.mark.parametrize('tool,args', WRITE_CASES)
def test_every_ordinary_write_requires_candidate_confirmation_when_bound(tmp_path, tool, args):
    business = Business()
    async def scenario():
        async with MCPClient(create_server(tmp_path, client=business)) as client:
            result = await client.session.call_tool(tool, {**args, 'request_id': 'synthetic', 'epoch': 'epoch-a',
                                                          'expected_revision': 0}, meta={'threadId': THREAD_A})
            assert result.is_error
            assert 'discussion_confirmation_required' in error_text(result)
    run(scenario)
    assert not any(call[0] in {'query', 'command'} for call in business.calls)


def test_unbound_and_no_metadata_calls_retain_ordinary_business_capability(tmp_path):
    business = Business()
    del business.bindings[THREAD_B]
    async def scenario():
        async with MCPClient(create_server(tmp_path, client=business)) as client:
            for meta in [None, {'threadId': THREAD_B}]:
                result = await client.session.call_tool('create_entity', {'type': 'task', 'title': 'synthetic',
                         'request_id': 'synthetic', 'epoch': 'ordinary-epoch', 'expected_revision': 0}, meta=meta)
                assert not result.is_error and result.structured_content['ordinary_write']
    run(scenario)
    assert len([call for call in business.calls if call[0] == 'command']) == 2


def test_metadata_is_request_local_and_binding_is_not_cached(tmp_path):
    business = Business()
    async def scenario():
        async with MCPClient(create_server(tmp_path, client=business)) as client:
            results = await asyncio.gather(*[client.session.call_tool('begin_discussion', {'text': 'synthetic', 'request_id': thread},
                                                                       meta={'threadId': thread}) for thread in [THREAD_A, THREAD_B]])
            assert [r.structured_content['conversation_id'] for r in results] == ['matter-a', 'matter-b']
            business.bindings[THREAD_A] = {'managed': True, 'conversation_id': 'changed', 'epoch': 'new-epoch'}
            result = await client.session.call_tool('begin_context', {}, meta={'threadId': THREAD_A})
            assert result.structured_content['conversation_id'] == 'changed'
            result = await client.session.call_tool('begin_context', {})
            assert result.structured_content['epoch'] == 'ordinary-epoch'
    run(scenario)


@pytest.mark.parametrize('thread_id', ['', 42, 'not-a-real-thread'])
def test_invalid_transport_identity_never_falls_back_to_ordinary_write(tmp_path, thread_id):
    business = Business()
    async def scenario():
        async with MCPClient(create_server(tmp_path, client=business)) as client:
            result = await client.session.call_tool('create_entity', {'type': 'task', 'title': 'synthetic',
                         'request_id': 'synthetic', 'epoch': 'ordinary-epoch', 'expected_revision': 0}, meta={'threadId': thread_id})
            assert result.is_error and 'discussion_identity_invalid' in error_text(result)
    run(scenario)
    assert not business.calls


def test_missing_or_unbound_metadata_cannot_submit_managed_candidate(tmp_path):
    business = Business()
    del business.bindings[THREAD_B]
    async def scenario():
        async with MCPClient(create_server(tmp_path, client=business)) as client:
            for meta, code in [(None, 'discussion_identity_required'), ({'threadId': THREAD_B}, 'discussion_binding_required')]:
                result = await client.session.call_tool('submit_candidate', {'job_id': 'synthetic', 'generation': 1, 'proposal': {}}, meta=meta)
                assert result.is_error and code in error_text(result)
    run(scenario)
    assert not any(call[0] == 'command' for call in business.calls)


def test_bound_queries_cannot_bypass_service_scope_rejection(tmp_path):
    business = Business()
    async def scenario():
        async with MCPClient(create_server(tmp_path, client=business)) as client:
            settings = await client.session.call_tool('query_business', {'name': 'settings'}, meta={'threadId': THREAD_A})
            assert settings.is_error and 'discussion_query_forbidden' in error_text(settings)
            cross = await client.session.call_tool('query_context', {'operation_id': 'other-matter-operation', 'collection': 'records'},
                                                  meta={'threadId': THREAD_A})
            assert cross.is_error and 'discussion_context_mismatch' in error_text(cross)
    run(scenario)
    assert not any(call[0] in {'query', 'command'} for call in business.calls)


@pytest.mark.parametrize('meta,accepted', [(None, True), ({'threadId': THREAD_A}, True), ({'threadId': THREAD_B}, False)])
def test_legacy_fixed_interface_checks_present_identity_and_keeps_local_compatibility(tmp_path, meta, accepted):
    business = Business()
    async def scenario():
        async with MCPClient(legacy_server(tmp_path, 'matter-a', 'epoch-a', client=business)) as client:
            result = await client.session.call_tool('begin_discussion', {'text': 'synthetic', 'request_id': 'synthetic'}, meta=meta)
            assert result.is_error is not accepted
            if not accepted:
                assert 'discussion_binding_mismatch' in error_text(result)
    run(scenario)
    assert any(call[0] == 'discussion' for call in business.calls) is accepted


def test_legacy_interface_rejects_changed_epoch_before_reading_context(tmp_path):
    business = Business()
    business.bindings[THREAD_A]['epoch'] = 'restored-epoch'
    async def scenario():
        async with MCPClient(legacy_server(tmp_path, 'matter-a', 'epoch-a', client=business)) as client:
            result = await client.session.call_tool('query_business', {'name': 'get', 'params': {'id': 'synthetic'}},
                                                    meta={'threadId': THREAD_A})
            assert result.is_error and 'discussion_binding_mismatch' in error_text(result)
    run(scenario)
    assert [call[1] for call in business.calls] == ['binding']


@pytest.mark.parametrize('tool,args', [
    ('begin_context', {}),
    ('create_entity', {'type': 'task', 'title': 'synthetic', 'request_id': 'request',
                       'epoch': 'old-epoch', 'expected_revision': 0}),
    ('execute_command', {'name': 'apply_proposal', 'payload': {}, 'request_id': 'request',
                         'epoch': 'old-epoch', 'expected_revision': 0}),
    ('submit_candidate', {'job_id': 'old-job', 'generation': 1, 'proposal': {}}),
])
def test_stale_historical_binding_error_never_falls_back_to_ordinary_capability(tmp_path, tool, args):
    class StaleBusiness(Business):
        def _request(self, category, action, data, timeout=35):
            self.calls.append((category, action, data))
            raise ClientError('conversation_binding_stale', 'Synthetic historical binding is no longer current')
    business = StaleBusiness()
    async def scenario():
        async with MCPClient(create_server(tmp_path, client=business)) as client:
            result = await client.session.call_tool(tool, args, meta={'threadId': THREAD_A})
            assert result.is_error and 'conversation_binding_stale' in error_text(result)
    run(scenario)
    assert business.calls and all(call[0] == 'native-discussion' for call in business.calls)


@pytest.mark.parametrize('as_json', [False, True])
def test_nested_transport_turn_identity_overrides_fake_tool_arguments_and_top_level_meta(tmp_path, as_json):
    business = Business()
    nested = {'turn_id': TURN_A, 'thread_id': THREAD_A}
    if as_json:
        nested = json.dumps(nested)
    async def scenario():
        async with MCPClient(create_server(tmp_path, client=business)) as client:
            result = await client.session.call_tool('begin_discussion',
                {'text': 'synthetic', 'request_id': 'request', 'provider_turn_id': THREAD_B},
                meta={'threadId': THREAD_A, 'turnId': THREAD_B, 'provider_turn_id': THREAD_B,
                      'x-codex-turn-metadata': nested})
            assert not result.is_error
    run(scenario)
    assert business.calls[-1][2]['provider_turn_id'] == TURN_A
    assert business.calls[-1][2]['provider_thread_id'] == THREAD_A


@pytest.mark.parametrize('nested', ['null', {}, 'invalid-json', [], {'turn_id': 'bad'},
    {'turn_id': 42}, {'turn_id': TURN_A, 'thread_id': THREAD_B},
    'x' * 65537], ids=['null', 'empty', 'invalid-json', 'list', 'invalid-uuid', 'not-string', 'thread-mismatch', 'too-large'])
def test_present_invalid_nested_turn_metadata_never_downgrades_to_legacy(tmp_path, nested):
    business = Business()
    async def scenario():
        async with MCPClient(create_server(tmp_path, client=business)) as client:
            result = await client.session.call_tool('begin_discussion', {'text': 'synthetic', 'request_id': 'request'},
                meta={'threadId': THREAD_A, 'x-codex-turn-metadata': nested})
            assert result.is_error and 'discussion_turn_identity_invalid' in error_text(result)
    run(scenario)
    assert not business.calls


def test_absent_nested_turn_metadata_is_explicit_legacy_none(tmp_path):
    business = Business()
    async def scenario():
        async with MCPClient(create_server(tmp_path, client=business)) as client:
            result = await client.session.call_tool('begin_discussion', {'text': 'synthetic', 'request_id': 'request'},
                                                    meta={'threadId': THREAD_A, 'turnId': TURN_A})
            assert not result.is_error
    run(scenario)
    assert business.calls[-1][2]['provider_turn_id'] is None


def test_present_null_in_server_context_is_rejected(tmp_path):
    # ClientSession's exclude_none serialization omits a None-valued metadata
    # key. Exercise a genuinely present inbound null through the public context.
    from mcp.server.context import ServerRequestContext
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError
    business = Business()
    server = create_server(tmp_path, client=business)
    context = Context(request_context=ServerRequestContext(session=None, lifespan_context={},
        protocol_version='2026-07-28', method='tools/call',
        meta={'threadId': THREAD_A, 'x-codex-turn-metadata': None}), mcp_server=server)
    async def scenario():
        with pytest.raises(ToolError, match='discussion_turn_identity_invalid'):
            await server.call_tool('begin_discussion', {'text': 'synthetic', 'request_id': 'request'}, context)
    run(scenario)
    assert not business.calls
