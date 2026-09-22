"""Protocol-level continuation tests; no account or network access."""
import json
import threading

import pytest

from management import ai


class RPC:
    instances = []
    resume_error = None

    def __init__(self, executable, cwd, cancel, timeout):
        self.requests = []; self.closed = False
        self.thread_id = self.turn_id = None
        self.instances.append(self)
        self.events = [
            {'method': 'item/completed', 'params': {'item': {'id': 'message', 'type': 'agentMessage', 'text': json.dumps({'summary': 'Synthetic response', 'unknowns': [], 'sources': [], 'actions': []})}}},
            {'method': 'turn/completed', 'params': {'turn': {'id': 'turn-2', 'status': 'completed'}}},
        ]

    def request(self, method, params):
        self.requests.append((method, params))
        if method == 'thread/resume':
            if self.resume_error:
                raise ai.AIError(self.resume_error, 'Synthetic resume failure')
            return {'thread': {'id': params['threadId']}, 'model': 'test-model'}
        if method == 'thread/start':
            return {'thread': {'id': 'new-provider-thread'}, 'model': 'test-model'}
        if method == 'turn/start':
            return {'turn': {'id': 'turn-2'}}
        return {}

    def send(self, value):
        pass

    def next_event(self):
        return self.events.pop(0)

    def close(self):
        self.closed = True


@pytest.fixture
def rpc(monkeypatch):
    monkeypatch.setattr(ai, 'find_codex', lambda *a: 'synthetic.exe')
    monkeypatch.setattr(ai, '_AppServer', RPC)
    monkeypatch.setattr(RPC, 'resume_error', None)
    return RPC


def request(**extra):
    return {'prompt': 'Synthetic current request', 'context': {'facts': 'current'}, 'conversation_id': 'software-scope-1',
            'conversation_scope': {'kind': 'course', 'entity_id': 'course-1'},
            'history': {'messages': [{'role': 'assistant', 'text': 'Old discussion, not a fact', 'state': 'completed', 'proposal_state': 'superseded'}], 'older_messages_omitted': False}, **extra}


def test_start_persistent_and_supply_bounded_software_history(rpc):
    result = ai.generate(request(), {'enabled': True}, threading.Event())
    instance = rpc.instances[-1]
    start = next(p for method, p in instance.requests if method == 'thread/start')
    turn = next(p for method, p in instance.requests if method == 'turn/start')
    assert start['ephemeral'] is False
    assert start['environments'] == start['dynamicTools'] == start['selectedCapabilityRoots'] == []
    assert 'software_discussion_history' in json.loads(turn['input'][0]['text'])
    assert result['provider']['recovery'] == 'history_rebuilt'
    assert '恢复会话' in result['summary']
    assert instance.closed


def test_resume_same_id_without_unstable_history_or_start_only_fields(rpc):
    result = ai.generate(request(provider_thread_id='existing-provider-thread'), {'enabled': True}, threading.Event())
    instance = rpc.instances[-1]
    assert [name for name, p in instance.requests] == ['initialize', 'thread/resume', 'turn/start']
    resume = instance.requests[1][1]
    assert resume['threadId'] == 'existing-provider-thread' and resume['excludeTurns'] is True
    assert not {'history', 'ephemeral', 'environments', 'dynamicTools', 'selectedCapabilityRoots', 'serviceName'} & resume.keys()
    content = json.loads(instance.requests[2][1]['input'][0]['text'])
    assert 'software_discussion_history' not in content
    assert content['software_discussion_state'][0]['proposal_state'] == 'superseded'
    assert result['provider']['thread_id'] == 'existing-provider-thread'
    assert result['provider']['recovery'] == 'resumed'


def test_missing_provider_thread_rebuilds_once_from_software_history(rpc, monkeypatch):
    monkeypatch.setattr(RPC, 'resume_error', 'AI_REQUEST_REJECTED')
    result = ai.generate(request(provider_thread_id='unavailable'), {'enabled': True}, threading.Event())
    instance = rpc.instances[-1]
    assert [name for name, p in instance.requests] == ['initialize', 'thread/resume', 'thread/start', 'turn/start']
    assert result['provider']['recovery'] == 'history_rebuilt'
    assert result['provider']['thread_id'] == 'new-provider-thread'
    content = json.loads(instance.requests[-1][1]['input'][0]['text'])
    assert content['software_discussion_history']['messages'][0]['text'] == 'Old discussion, not a fact'


@pytest.mark.parametrize('code', ['AI_CANCELLED', 'AI_TIMEOUT', 'AI_TOOL_BLOCKED', 'AI_DISCONNECTED'])
def test_resume_cancel_timeout_or_tool_request_never_retries(rpc, monkeypatch, code):
    monkeypatch.setattr(RPC, 'resume_error', code)
    with pytest.raises(ai.AIError) as error:
        ai.generate(request(provider_thread_id='existing'), {'enabled': True}, threading.Event())
    assert error.value.code == code
    assert [name for name, p in rpc.instances[-1].requests] == ['initialize', 'thread/resume']
    assert rpc.instances[-1].closed


def test_trusted_images_use_localimage_without_file_tools(rpc, tmp_path):
    image = tmp_path / 'synthetic.png'
    image.write_bytes(b'placeholder-for-protocol-only')
    ai.generate(request(local_images=[str(image)]), {'enabled': True}, threading.Event())
    turn = next(p for name, p in rpc.instances[-1].requests if name == 'turn/start')
    assert turn['input'][1] == {'type': 'localImage', 'path': str(image)}
    assert turn['sandboxPolicy'] == {'type': 'readOnly', 'networkAccess': False}


def test_excess_or_relative_images_rejected_before_spawn(rpc):
    before = len(rpc.instances)
    for paths in (['relative.png'], ['image'] * 9):
        with pytest.raises(ai.AIError):
            ai.generate(request(local_images=paths), {'enabled': True}, threading.Event())
    assert len(rpc.instances) == before


def test_large_fallback_context_rejected_before_spawn(rpc):
    before = len(rpc.instances)
    with pytest.raises(ai.AIError) as error:
        ai.generate(request(history={'messages': [{'role': 'user', 'text': 'x' * ai.MAX_CONTEXT_BYTES}]}, provider_thread_id='exists'), {'enabled': True}, threading.Event())
    assert error.value.code == 'AI_CONTEXT_LIMIT'
    assert len(rpc.instances) == before
