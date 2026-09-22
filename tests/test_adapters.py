"""Adapter contract tests use synthetic records only, never the production data root."""
import asyncio
import json
import queue
import threading
import time

import pytest
from mcp import Client as MCPClient

from management import ai
from management.mcp_server import create_server


class MemoryClient:
    def __init__(self):
        self.epoch, self.revision = "synthetic-epoch", 0
        self.calls = []
        self.receipts = {}

    def state(self):
        return {"epoch": self.epoch, "revision": self.revision, "counts": {}}

    def query(self, name, **params):
        self.calls.append(("query", name, params))
        if name == "receipt":
            return {**self.state(), "found": params["request_id"] in self.receipts,
                    "receipt": self.receipts.get(params["request_id"])}
        if name == "capabilities":
            return {**self.state(), "types": [], "commands": ["create", "update"]}
        return {**self.state(), "items": []}

    def command(self, name, payload, **guard):
        self.calls.append(("command", name, payload, guard))
        if guard["request_id"] in self.receipts:
            return {**self.receipts[guard["request_id"]], "replayed": True}
        if guard["epoch"] != self.epoch or guard["expected_revision"] != self.revision:
            raise ValueError("stale synthetic context")
        self.revision += 1
        receipt = {**self.state(), "request_id": guard["request_id"], "result": payload, "replayed": False}
        self.receipts[guard["request_id"]] = receipt
        return receipt


def run(coro):
    return asyncio.run(coro)


def test_mcp_typed_tools_real_sdk_and_no_recursive_ai(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "generate", lambda *a, **k: pytest.fail("ordinary MCP operation started AI"))
    business = MemoryClient()
    server = create_server(tmp_path, client=business)

    async def scenario():
        async with MCPClient(server) as client:
            tools = await client.list_tools()
            tools = tools.tools if hasattr(tools, "tools") else tools
            names = {t.name for t in tools}
            assert {"begin_context", "create_entity", "record_feedback", "save_plan", "recover_receipt"} <= names
            create = next(t for t in tools if t.name == "create_entity")
            assert {"epoch", "expected_revision", "request_id"} <= set(create.input_schema["required"])
            read = await client.call_tool("begin_context", {})
            assert not read.is_error
            assert read.structured_content["epoch"] == "synthetic-epoch"
            args = {"type": "task", "title": "Synthetic task", "request_id": "test-1", "epoch": "synthetic-epoch", "expected_revision": 0}
            created = await client.call_tool("create_entity", args)
            assert not created.is_error
            assert created.structured_content["revision"] == 1
            again = await client.call_tool("create_entity", args)
            assert again.structured_content["replayed"]
            recovered = await client.call_tool("recover_receipt", {"request_id": "test-1"})
            assert recovered.structured_content["found"]
            stale = await client.call_tool("create_entity", {**args, "request_id": "test-2"})
            assert stale.is_error
    run(scenario())


def test_mcp_feedback_omits_unknown_dimensions(tmp_path):
    business = MemoryClient()
    server = create_server(tmp_path, client=business)

    async def scenario():
        async with MCPClient(server) as client:
            result = await client.call_tool("record_feedback", {
                "target_id": "synthetic-task", "business_date": "2026-01-01",
                "dimensions": {"attendance": "absent"}, "source_text": "Synthetic explicit report",
                "request_id": "feedback-1", "epoch": business.epoch, "expected_revision": 0,
            })
            assert not result.is_error
            assert business.calls[-1][2]["dimensions"] == {"attendance": "absent"}
            invalid = await client.call_tool("record_feedback", {
                "target_id": "synthetic-task", "business_date": "2026-01-01",
                "dimensions": {"actual_minutes": -1}, "source_text": "Synthetic report",
                "request_id": "feedback-2", "epoch": business.epoch, "expected_revision": 1,
            })
            assert invalid.is_error
    run(scenario())


def proposal(command="create", payload=None):
    return {"summary": "Synthetic result", "unknowns": [], "sources": [], "actions": [
        {"command": command, "payload_json": json.dumps(payload or {"type": "task", "title": "Synthetic"}), "reason": "Explicit test request"}
    ]}


@pytest.mark.parametrize("command,payload,code", [
    ("settings", {}, "AI_SCOPE_ERROR"),
    ("create", {"authorized": True}, "AI_SCOPE_ERROR"),
    ("update", {"sql": "select 1"}, "AI_SCOPE_ERROR"),
    ("create", {"expected_revision": 5}, "AI_SCOPE_ERROR"),
])
def test_ai_rejects_authority_or_control_injection(command, payload, code):
    with pytest.raises(ai.AIError) as error:
        ai.validate_proposal(proposal(command, payload))
    assert error.value.code == code


def test_ai_preserves_candidate_only_and_limits_scope():
    result = ai.validate_proposal(proposal())
    assert result["actions"][0]["payload"] == {"type": "task", "title": "Synthetic"}
    assert "payload_json" not in result["actions"][0]
    with pytest.raises(ai.AIError, match="范围"):
        ai.validate_proposal(proposal(), set())
    invalid = proposal()
    invalid["actions"][0]["payload_json"] = '{"x": NaN}'
    with pytest.raises(ai.AIError):
        ai.validate_proposal(invalid)


def test_ai_missing_configuration_and_precancel_do_not_spawn(monkeypatch):
    monkeypatch.setattr(ai, "_AppServer", lambda *a: pytest.fail("must not spawn"))
    with pytest.raises(ai.AIError) as error:
        ai.generate({"prompt": "Synthetic"}, {}, threading.Event())
    assert error.value.code == "AI_NOT_CONFIGURED"
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ai.AIError) as error:
        ai.generate({"prompt": "Synthetic"}, {"enabled": True}, cancel)
    assert error.value.code == "AI_CANCELLED"


class FakeRPC:
    instances = []
    final_status = "completed"
    def __init__(self, executable, cwd, cancel, timeout):
        self.requests, self.sent = [], []
        self.thread_id = self.turn_id = None
        self.closed = False
        self.events = [
            {"method": "item/completed", "params": {"item": {"id": "item-1", "type": "agentMessage", "text": json.dumps(proposal())}}},
            {"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": self.final_status}}},
        ]
        self.instances.append(self)
    def request(self, method, params):
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}, "model": "configured-model"}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}
    def send(self, message):
        self.sent.append(message)
    def next_event(self):
        return self.events.pop(0)
    def close(self):
        self.closed = True


def test_ai_protocol_handshake_and_no_tools(monkeypatch):
    monkeypatch.setattr(ai, "find_codex", lambda *a: "synthetic.exe")
    monkeypatch.setattr(ai, "_AppServer", FakeRPC)
    result = ai.generate({"prompt": "Synthetic", "context": {}}, {"enabled": True}, threading.Event())
    rpc = FakeRPC.instances[-1]
    assert rpc.closed
    assert [method for method, params in rpc.requests] == ["initialize", "thread/start", "turn/start"]
    thread = rpc.requests[1][1]
    turn = rpc.requests[2][1]
    assert thread["ephemeral"] and thread["environments"] == [] and thread["dynamicTools"] == []
    assert turn["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
    assert turn["outputSchema"] == ai.PROPOSAL_SCHEMA
    assert result["provider"]["thread_id"] == "thread-1"


@pytest.mark.parametrize("status,code", [("interrupted", "AI_CANCELLED"), ("failed", "AI_GENERATION_FAILED")])
def test_ai_failed_turn_is_not_a_proposal(monkeypatch, status, code):
    monkeypatch.setattr(ai, "find_codex", lambda *a: "synthetic.exe")
    monkeypatch.setattr(ai, "_AppServer", FakeRPC)
    monkeypatch.setattr(FakeRPC, "final_status", status)
    with pytest.raises(ai.AIError) as error:
        ai.generate({"prompt": "Synthetic"}, {"enabled": True}, threading.Event())
    assert error.value.code == code
    assert FakeRPC.instances[-1].closed


def rpc_receiver():
    rpc = object.__new__(ai._AppServer)
    rpc.cancel = threading.Event()
    rpc.deadline = time.monotonic() + 10
    rpc.events = queue.Queue()
    rpc.sent = []
    rpc.send = rpc.sent.append
    return rpc


def test_ai_refuses_tool_approval_requests():
    rpc = rpc_receiver()
    rpc.events.put({"id": 70, "method": "item/commandExecution/requestApproval", "params": {}})
    with pytest.raises(ai.AIError) as error:
        rpc._receive()
    assert error.value.code == "AI_TOOL_BLOCKED"
    assert rpc.sent[0]["error"]["code"] == -32601


def test_ai_timeout_and_late_cancel():
    rpc = rpc_receiver()
    rpc.deadline = time.monotonic() - 1
    with pytest.raises(ai.AIError) as error:
        rpc._receive()
    assert error.value.code == "AI_TIMEOUT"
    rpc.cancel.set()
    with pytest.raises(ai.AIError) as error:
        rpc._receive()
    assert error.value.code == "AI_CANCELLED"


def test_inherited_tools_disabled_without_copying_credentials(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[mcp_servers.synthetic]\ncommand="synthetic"\n[plugins.synthetic]\nenabled=true\n[apps.synthetic]\nenabled=true\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    overrides = ai._isolation_overrides()
    assert overrides["mcp_servers.synthetic.enabled"] is False
    assert overrides["plugins.synthetic.enabled"] is False
    assert overrides["apps.synthetic.enabled"] is False
    assert overrides["features.shell_tool"] is False
    assert overrides["memories.use_memories"] is False
    assert 'command="synthetic"' in config.read_text(encoding="utf-8")

def test_real_stdio_clients_share_service_and_survive_exit(tmp_path):
    """Regression for Windows MCP Job cleanup accidentally killing the service."""
    import os
    from pathlib import Path
    import sys
    import psutil
    from mcp import StdioServerParameters
    from management.client import Client
    root = (tmp_path / 'synthetic-service').resolve()
    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'management', '--mcp', '--data-dir', str(root)],
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})

    async def scenario():
        async with MCPClient(params) as first, MCPClient(params) as second:
            first_state, second_state = await asyncio.gather(first.call_tool('begin_context', {}), second.call_tool('begin_context', {}))
            a, b = first_state.structured_content, second_state.structured_content
            assert a['epoch'] == b['epoch'] and a['revision'] == b['revision'] == 0
            args = {'type': 'task', 'title': 'Synthetic stdio task', 'request_id': 'stdio-create-1',
                    'epoch': a['epoch'], 'expected_revision': a['revision']}
            result = await first.call_tool('create_entity', args)
            assert not result.is_error
            entity_id = result.structured_content['result']['entity']['id']
            other = await second.call_tool('get_entity', {'id': entity_id})
            assert other.structured_content['entity']['title'] == 'Synthetic stdio task'
            conflict = await second.call_tool('create_entity', {**args, 'request_id': 'stdio-stale-2'})
            assert conflict.is_error
            assert 'revision_conflict' in conflict.content[0].text
            receipt = await second.call_tool('recover_receipt', {'request_id': 'stdio-create-1'})
            assert receipt.structured_content['found']
            retry = await second.call_tool('create_entity', args)
            assert retry.structured_content['replayed']
        independent = Client(root, autostart=False)
        assert independent.state()['counts']['task'] == 1
        async with MCPClient(params) as fresh:
            result = await fresh.call_tool('get_entity', {'id': entity_id})
            assert result.structured_content['entity']['id'] == entity_id

    try:
        run(scenario())
    finally:
        runtime = root / 'runtime.json'
        if runtime.exists():
            state = json.loads(runtime.read_text(encoding='utf-8'))
            if psutil.pid_exists(state['pid']):
                process = psutil.Process(state['pid'])
                args = process.cmdline()
                assert '--service' in args and str(root) in args
                process.terminate()
                process.wait(timeout=10)

def test_mcp_allows_only_runtime_registered_extension_handlers(tmp_path):
    business = MemoryClient()
    previous = business.query
    def query(name, **params):
        result = previous(name, **params)
        if name == 'capabilities':
            result['commands'].append('synthetic.execute')
            result['code_extensions'] = [{'name':'synthetic.summary','kind':'query'}]
        return result
    business.query = query
    server = create_server(tmp_path, client=business)
    async def scenario():
        async with MCPClient(server) as client:
            result = await client.call_tool('query_business', {'name':'synthetic.summary','params':{}})
            assert not result.is_error
            result = await client.call_tool('execute_command', {'name':'synthetic.execute','payload':{},'request_id':'synthetic-extension-1','epoch':business.epoch,'expected_revision':0})
            assert not result.is_error
            denied = await client.call_tool('execute_command', {'name':'sql.execute','payload':{},'request_id':'synthetic-extension-2','epoch':business.epoch,'expected_revision':1})
            assert denied.is_error and 'unknown_command' in denied.content[0].text
            denied = await client.call_tool('query_business', {'name':'sql.query'})
            assert denied.is_error and 'unknown_query' in denied.content[0].text
            assert not any(call[1].startswith('sql.') for call in business.calls)
    run(scenario())