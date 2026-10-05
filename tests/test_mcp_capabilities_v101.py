"""Real Core + MCP SDK regressions; only isolated synthetic SQLite spaces."""
import asyncio
import json
import uuid

import pytest
from mcp import Client as MCPClient

from management import ai, session_coordinator
from management.context_service import MAX_PAGE_BYTES, size
from management.core import Core
from management.extensions import CommandDefinition, QueryDefinition
from management.mcp_server import create_server
from management.storage import encode, now


DAY = "2030-01-07"
THREAD = "00000000-0000-4000-8000-000000000111"


class Business:
    """Actual Core transaction/routing, without a service or provider process."""
    def __init__(self, core):
        self.core, self.commands, self.queries = core, [], []

    def query(self, name, **params):
        self.queries.append(name)
        return self.core.query(name, **params)

    def command(self, name, payload, **guards):
        self.commands.append(name)
        return self.core.command(name, payload, **guards)

    def _request(self, category, action, data, timeout=35):
        assert category == "native-discussion"
        return session_coordinator.handle_native(self.core, action, data)


@pytest.fixture
def business(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "generate", lambda *a, **k: pytest.fail("MCP discovery must not start a model"))
    core = Core(tmp_path / "synthetic-data")
    value = Business(core)
    yield value
    assert core._desktop_gateway is None


def guards(core, **override):
    state = core.query("state")
    return {"request_id": str(uuid.uuid4()), "epoch": state["epoch"],
            "expected_revision": state["revision"], **override}


def command(core, name, payload):
    return core.command(name, payload, **guards(core))["result"]


def text(result):
    return "\n".join(item.text for item in result.content if hasattr(item, "text"))


def error(result, code):
    assert result.is_error, result
    assert '"code": "' + code + '"' in text(result), text(result)


def test_real_large_capabilities_revise_plan_reaches_core_and_keeps_guards(business):
    core = business.core
    raw = core.query("capabilities")
    assert size(raw) > MAX_PAGE_BYTES and "revise_plan" in raw["commands"]
    a = command(core, "create", {"type": "task", "title": "Synthetic retained task"})["entity"]
    b = command(core, "create", {"type": "task", "title": "Synthetic additional task"})["entity"]
    plan = command(core, "create_plan", {"date": DAY, "mode": "no_precise_time",
                                        "blocks": [{"target_id": a["id"]}]})["entity"]
    payload = {"date": DAY, "plan_id": plan["id"], "plan_version": plan["version"],
               "mode": "no_precise_time", "blocks": [{"target_id": a["id"]}, {"target_id": b["id"]}]}

    async def scenario():
        async with MCPClient(create_server(core.root, client=business)) as client:
            args = {"name": "revise_plan", "payload": payload, **guards(core)}
            first = await client.call_tool("execute_command", args)
            assert not first.is_error, text(first)
            revised = first.structured_content["result"]["entity"]
            assert revised["id"] != plan["id"]
            assert [block["target_id"] for block in revised["data"]["blocks"]] == [a["id"], b["id"]]
            replay = await client.call_tool("execute_command", args)
            assert not replay.is_error and replay.structured_content["replayed"]
            assert replay.structured_content["result"]["entity"]["id"] == revised["id"]
            stale = await client.call_tool("execute_command", {**args, "request_id": "stale-revision"})
            error(stale, "revision_conflict")
            stale_plan = await client.call_tool("execute_command", {"name": "revise_plan", "payload": payload, **guards(core)})
            error(stale_plan, "plan_conflict")
            wrong_epoch = await client.call_tool("execute_command", {"name": "revise_plan", "payload": payload,
                                                                      **guards(core, epoch="old-synthetic-epoch")})
            error(wrong_epoch, "epoch_conflict")
    asyncio.run(scenario())
    assert business.commands == ["revise_plan"] * 5
    assert "prepare_context" not in business.queries


def test_generic_registered_command_unknown_rejection_and_typed_tool(business):
    core = business.core
    async def scenario():
        async with MCPClient(create_server(core.root, client=business)) as client:
            generic = await client.call_tool("execute_command", {"name": "create", "payload": {
                "type": "task", "title": "Synthetic generic task"}, **guards(core)})
            assert not generic.is_error, text(generic)
            unknown = await client.call_tool("execute_command", {"name": "unregistered.command", "payload": {}, **guards(core)})
            error(unknown, "unknown_command")
            typed = await client.call_tool("create_entity", {"type": "task", "title": "Synthetic typed task", **guards(core)})
            assert not typed.is_error, text(typed)
    asyncio.run(scenario())
    assert business.commands == ["create", "create"]
    assert core.query("list", type="task")["total"] == 2


def test_library_destinations_and_refile_are_available_through_actual_mcp(business,tmp_path):
    core=business.core
    owner=command(core,'create',{'type':'course','title':'Synthetic MCP archive course'})['entity']
    source=tmp_path/'material.txt';source.write_text('Synthetic MCP material','utf-8')
    asset=command(core,'import_asset',{'path':str(source),'owner_id':owner['id'],'library_subdir':''})['entity']
    async def scenario():
        async with MCPClient(create_server(core.root,client=business)) as client:
            query=await client.call_tool('query_business',{'name':'library_destinations','params':{'id':asset['id']}})
            assert not query.is_error,text(query)
            assert query.structured_content['current']['id']==asset['id']
            assert query.structured_content['owner_id']==owner['id']
            args={'name':'refile_source','payload':{'id':asset['id'],'version':asset['version'],'library_subdir':'课件'},**guards(core)}
            saved=await client.call_tool('execute_command',args)
            assert not saved.is_error,text(saved)
            assert saved.structured_content['result']['entity']['data']['library_subdir']=='课件'
            again=await client.call_tool('execute_command',args)
            assert not again.is_error and again.structured_content['replayed']
            assert again.structured_content['result']==saved.structured_content['result']
    asyncio.run(scenario())
    assert business.commands==['refile_source','refile_source']
    assert 'library_destinations' in business.queries


def test_dynamic_extensions_remain_discoverable_and_executable_beyond_first_page(business):
    core = business.core
    schema = {"type": "object", "additionalProperties": False}
    for i in range(105):
        core.extensions.register(QueryDefinition(f"synthetic.summary_{i:03}", "Synthetic query",
            lambda core, c, p: {"synthetic_count": 0}, schema))
    core.extensions.register(CommandDefinition("synthetic.create_task", "Synthetic command",
        lambda core, c, p, rid: core._create(c, {"type": "task", "title": "Synthetic extension"}, rid), schema))
    async def scenario():
        async with MCPClient(create_server(core.root, client=business)) as client:
            public = await client.call_tool("query_business", {"name": "capabilities"})
            assert not public.is_error
            assert "synthetic.summary_104" not in public.structured_content["queries"]
            assert public.structured_content["next_offset"] is not None
            query = await client.call_tool("query_business", {"name": "synthetic.summary_104"})
            assert not query.is_error and query.structured_content["synthetic_count"] == 0
            write = await client.call_tool("execute_command", {"name": "synthetic.create_task", "payload": {}, **guards(core)})
            assert not write.is_error, text(write)
            unknown = await client.call_tool("query_business", {"name": "synthetic.missing"})
            error(unknown, "unknown_query")
    asyncio.run(scenario())
    assert business.commands == ["synthetic.create_task"]
    assert "synthetic.missing" not in business.queries


def test_capability_index_is_bounded_complete_and_creates_no_context(business):
    core = business.core
    async def scenario():
        async with MCPClient(create_server(core.root, client=business)) as client:
            commands, queries, params = [], [], {"limit": 7}
            while True:
                response = await client.call_tool("query_business", {"name": "capabilities", "params": params})
                assert not response.is_error, text(response)
                value = response.structured_content
                assert size(value) <= MAX_PAGE_BYTES and "context" not in value
                commands.extend(value["commands"])
                queries.extend(value["queries"])
                if value["next_offset"] is None:
                    break
                params.update(offset=value["next_offset"], version=value["version"])
            assert commands == sorted(core.query("capabilities")["commands"])
            assert len(queries) == value["totals"]["queries"] == len(set(queries))
            assert {"types", "code_extensions", "workflows", "modules"} == set(value["detail_sections"])
            types = await client.call_tool("query_business", {"name": "capabilities", "params": {"section": "types"}})
            assert "task" in types.structured_content["names"]
            task = await client.call_tool("query_business", {"name": "capabilities", "params": {
                "section": "types", "name": "task", "version": types.structured_content["version"]}})
            assert not task.is_error
            detail = json.loads(task.structured_content["json_fragment"])
            assert detail["id"] == "task" and detail["fields"]
    asyncio.run(scenario())
    with core.store.connect() as c:
        assert c.execute("SELECT count(*) FROM context_operations").fetchone()[0] == 0


def test_large_extension_detail_fragments_round_trip_and_reject_stale_catalog(business):
    core = business.core
    schema = {"type": "object", "description": 'Synthetic 中文 "schema" ' * 2000}
    core.extensions.register(QueryDefinition("synthetic.large", "Synthetic detail", lambda core, c, p: {}, schema))
    async def scenario():
        async with MCPClient(create_server(core.root, client=business)) as client:
            params = {"section": "code_extensions", "name": "query:synthetic.large"}
            fragments = []
            while True:
                response = await client.call_tool("query_business", {"name": "capabilities", "params": params})
                assert not response.is_error, text(response)
                page = response.structured_content
                assert size(page) <= MAX_PAGE_BYTES
                fragments.append(page["json_fragment"])
                if page["next_offset"] is None:
                    break
                params.update(offset=page["next_offset"], version=page["version"])
            assert len(fragments) > 1
            assert json.loads("".join(fragments))["input_schema"] == schema
            core.extensions.register(QueryDefinition("synthetic.new", "New synthetic detail", lambda core, c, p: {}, {"type": "object"}))
            changed = await client.call_tool("query_business", {"name": "capabilities", "params": params})
            error(changed, "capabilities_changed")
    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["commands", "code_extensions"])
@pytest.mark.parametrize("tool", ["execute_command", "query_business"])
def test_incomplete_registry_is_explicit_error_not_false_unknown(business, missing, tool):
    original = business.query
    def query(name, **params):
        result = original(name, **params)
        if name == "capabilities":
            result.pop(missing)
        return result
    business.query = query
    async def scenario():
        async with MCPClient(create_server(business.core.root, client=business)) as client:
            args = {"name": "create", "payload": {}, **guards(business.core)} if tool == "execute_command" else {"name": "synthetic.query"}
            response = await client.call_tool(tool, args)
            error(response, "capabilities_invalid")
            assert "unknown_command" not in text(response) and "unknown_query" not in text(response)
    asyncio.run(scenario())
    assert not business.commands


@pytest.mark.parametrize("patch", [
    {"commands": "create"},
    {"commands": [{}]},
    {"commands": ["create", "create"]},
    {"code_extensions": [{"name": "synthetic.query", "kind": []}]},
    {"next_offset": 80},
])
def test_malformed_or_partial_registry_fails_closed(business, patch):
    original = business.query
    def query(name, **params):
        result = original(name, **params)
        return {**result, **patch} if name == "capabilities" else result
    business.query = query
    async def scenario():
        async with MCPClient(create_server(business.core.root, client=business)) as client:
            response = await client.call_tool("execute_command", {
                "name": "create", "payload": {"type": "task", "title": "Must not write"}, **guards(business.core)})
            error(response, "capabilities_invalid")
    asyncio.run(scenario())
    assert not business.commands


@pytest.mark.parametrize("params", [{"section": []}, {"offset": True}, {"limit": 101}, {"section": "types", "name": []}])
def test_bad_public_catalog_parameters_return_typed_error(business, params):
    async def scenario():
        async with MCPClient(create_server(business.core.root, client=business)) as client:
            response = await client.call_tool("query_business", {"name": "capabilities", "params": params})
            error(response, "capabilities_parameters")
    asyncio.run(scenario())


def test_managed_catalog_and_direct_write_guard_use_actual_metadata_and_core(business):
    core = business.core
    stamp, state = now(), core.query("state")
    with core.store.connect() as c:
        c.execute("""INSERT INTO conversations(id,scope_key,scope,provider_thread_id,created_at,updated_at)
            VALUES (?,?,?,?,?,?)""", ("synthetic-matter", "synthetic-scope", encode({"kind": "general"}), THREAD, stamp, stamp))
        c.execute("INSERT INTO conversation_bindings VALUES (?,?,?,?,?,'active',NULL,?,?)",
            ("synthetic-matter", THREAD, state["epoch"], str(core.root / "synthetic-workspace"), "desktop_mcp_v3", stamp, stamp))
    async def scenario():
        async with MCPClient(create_server(core.root, client=business)) as client:
            catalog = await client.session.call_tool("query_business", {"name": "capabilities"}, meta={"threadId": THREAD})
            assert not catalog.is_error, text(catalog)
            value = catalog.structured_content
            assert value["direct_business_writes"] is False and "revise_plan" in value["allowed_commands"]
            denied = await client.session.call_tool("execute_command", {"name": "revise_plan", "payload": {}, **guards(core)}, meta={"threadId": THREAD})
            error(denied, "discussion_confirmation_required")
            refile = await client.session.call_tool("execute_command", {"name": "refile_source", "payload": {
                "id": "synthetic-source", "version": 1, "library_subdir": "课件"}, **guards(core)}, meta={"threadId": THREAD})
            error(refile, "discussion_confirmation_required")
            typed = await client.session.call_tool("create_entity", {"type": "task", "title": "Must not write", **guards(core)}, meta={"threadId": THREAD})
            error(typed, "discussion_confirmation_required")
            extension = await client.session.call_tool("query_business", {"name": "synthetic.private_query"}, meta={"threadId": THREAD})
            error(extension, "discussion_not_started")
    asyncio.run(scenario())
    assert not business.commands and "capabilities" not in business.queries
    assert core.query("list")["total"] == 0
