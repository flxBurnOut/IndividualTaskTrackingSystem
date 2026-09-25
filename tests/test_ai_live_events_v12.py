"""Synthetic provider streaming; no model, account or personal database access."""
import json
import queue
import threading
import time

import pytest

from management import ai
from management.ai_stream import ProposalStream, StreamLimitError, SummaryPreview


def proposal(summary='已根据明确反馈整理。', **extra):
    return {'summary': summary, 'unknowns': [], 'sources': [], 'actions': [], **extra}


def notification(method, **body):
    return {'method': method, 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', **body}}


def completed(summary='已根据明确反馈整理。', item_id='message-1', **extra):
    return notification('item/completed', item={'id': item_id, 'type': 'agentMessage', 'text': json.dumps(proposal(summary), ensure_ascii=False)}, **extra)


def done(**extra):
    return notification('turn/completed', turn={'id': 'turn-1', 'status': 'completed'}, **extra)


class RPC:
    instances = []
    events_template = None
    rejected_resume = False
    fail_metadata = False

    def __init__(self, executable, cwd, cancel, timeout):
        self.requests, self.closed = [], False
        self.thread_id = self.turn_id = None
        self.events = list(self.events_template or [completed(), done()])
        self.instances.append(self)

    def request(self, method, params):
        self.requests.append((method, params))
        if method == 'thread/resume':
            if self.rejected_resume:
                raise ai.AIError('AI_REQUEST_REJECTED', 'synthetic missing thread')
            return {'thread': {'id': params['threadId']}}
        if method == 'thread/start':
            return {'thread': {'id': 'thread-1'}}
        if method == 'thread/metadata/update' and self.fail_metadata:
            raise ai.AIError('AI_TIMEOUT', 'synthetic metadata timeout')
        if method == 'turn/start':
            return {'turn': {'id': 'turn-1'}}
        return {}

    def send(self, value):
        pass

    def next_event(self):
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event

    def close(self):
        self.closed = True


@pytest.fixture
def rpc(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, 'find_codex', lambda *a: 'synthetic.exe')
    monkeypatch.setattr(ai, '_AppServer', RPC)
    monkeypatch.setattr(ai, 'project_directory', lambda *a: tmp_path)
    monkeypatch.setattr(RPC, 'events_template', None)
    monkeypatch.setattr(RPC, 'rejected_resume', False)
    monkeypatch.setattr(RPC, 'fail_metadata', False)
    RPC.instances.clear()
    return RPC


def request(**extra):
    return {'prompt': 'Synthetic current request', 'conversation_id': 'software-1', 'conversation_scope': {'kind': 'general'}, **extra}


@pytest.mark.parametrize('chunk', [1, 2, 3, 7, 29, 1000])
def test_only_top_level_summary_is_decoded_across_all_escape_boundaries(chunk):
    expected = 'Lecture 4：已完成\n引用 "与" \\ 路径 / 😀 𐐷'
    data = {'actions': [{'payload_json': json.dumps({'summary': 'PAYLOAD SECRET'}), 'reason': 'REASON SECRET'}],
            'unknowns': ['STRING SECRET {"summary":"fake"}'],
            'other': {'summary': 'NESTED SECRET', 'array': [True, False, None, -1.24e-3, {'summary': 'DEEP SECRET'}]},
            'summary': expected, 'sources': []}
    raw = json.dumps(data, ensure_ascii=True)
    parser = SummaryPreview()
    for i in range(0, len(raw), chunk):
        view = parser.feed(raw[i:i+chunk])
        assert expected.startswith(view)
        assert 'SECRET' not in view and 'payload_json' not in view
        view.encode('utf-8')
    assert parser.text == expected
    assert parser.finished and not parser.invalid


@pytest.mark.parametrize('raw', [
    '{"other":{"summary":"nested"}}',
    '{"summary":42}',
    '[{"summary":"root-array"}]',
    '{"x":tru, "summary":"bad-prefix"}',
    '{"x":01, "summary":"bad-prefix"}',
    '{"x" "summary":"bad-prefix"}',
    '{"x":{}, "summary":"first", "summary":"duplicate"}',
    '{"summary":"prefix\\uD800"}',
    '{"summary":"prefix\\uDC00"}',
    '{"summary":"prefix\\uD800x"}',
    '{"summary":"prefix\\uD800\\n"}',
    '{"summary":"prefix\\uD800\\uD800"}',
    '{"summary":"prefix\\q"}',
    '{"summary":"prefix\nraw-control"}',
    '{"summary":"first"} trailing',
])
def test_malformed_or_nested_summary_never_becomes_final_preview(raw):
    parser = SummaryPreview()
    assert parser.feed(raw) == ''


def test_incomplete_escaped_surrogate_never_escapes_to_gui():
    parser = SummaryPreview()
    assert parser.feed('{"summary":"开始\\uD83D') == '开始'
    assert parser.feed('\\uDE') == '开始'
    assert parser.feed('00') == '开始😀'
    assert parser.feed('"}') == '开始😀'


def test_escaped_top_level_key_and_long_decoy_key():
    parser = SummaryPreview()
    assert parser.feed('{"' + ('summary' * 30) + '":"decoy","summ\\u0061ry":"real"}') == 'real'


def test_preview_memory_is_bounded_by_bytes_characters_and_nesting():
    with pytest.raises(StreamLimitError):
        SummaryPreview(max_bytes=4).feed('你好')
    assert SummaryPreview(max_chars=2).feed('{"summary":"abc"}') == ''
    parser = SummaryPreview(max_depth=4)
    assert parser.feed('{"x":' + '[' * 20 + '{"summary":"fake"}') == ''
    assert parser.invalid and len(parser.stack) == 0


def test_completed_message_replaces_stale_delta_and_ignores_late_delta():
    stream = ProposalStream()
    assert stream.delta('a', '{"summary":"草稿') == '草稿'
    assert stream.complete('a', json.dumps(proposal('权威结果'))) == '权威结果'
    assert stream.delta('a', '迟到分片') == '权威结果'
    assert stream.start('b') == ''
    assert stream.delta('b', '{"summary":"第二条') == '第二条'
    assert stream.complete('b', json.dumps(proposal('第二条完整'))) == '第二条完整'
    assert json.loads(next(reversed(stream.completed.values())))['summary'] == '第二条完整'


def test_cross_item_bytes_and_item_count_are_bounded():
    stream = ProposalStream(max_bytes=20)
    stream.delta('a', '{"summary":"123')
    with pytest.raises(StreamLimitError):
        stream.delta('b', '{"summary":"456')
    stream = ProposalStream(max_items=2)
    stream.start('a'); stream.start('b')
    with pytest.raises(StreamLimitError):
        stream.start('c')
    with pytest.raises(ValueError):
        stream.start('x' * 201)
    with pytest.raises(ValueError):
        stream.delta('a', {'not': 'a string'})


def test_thread_and_turn_ids_are_reported_before_following_work(rpc):
    snapshots = []
    def progress(value):
        snapshots.append(value)
        if value['phase'] == 'thread_ready':
            assert value['provider_thread_id'] == 'thread-1'
            assert rpc.instances[-1].requests[-1][0] == 'thread/start'
            assert not any(method == 'turn/start' for method, p in rpc.instances[-1].requests)
        if value['phase'] == 'waiting_model':
            assert value['provider_turn_id'] == 'turn-1'
            assert rpc.instances[-1].requests[-1][0] == 'turn/start'
            assert rpc.instances[-1].events
    result = ai.generate(request(), {'enabled': True}, threading.Event(), progress=progress)
    assert [s['phase'] for s in snapshots] == ['connecting', 'creating_thread', 'thread_ready', 'sending', 'waiting_model', 'receiving', 'validating']
    assert snapshots[-1]['preview_text'] == result['summary']
    assert set(snapshots[-1]) == {'phase', 'provider_thread_id', 'provider_turn_id', 'recovery', 'preview_text', 'provider_contract'}
    assert rpc.instances[-1].closed


def test_resumed_thread_is_reported_even_when_metadata_later_times_out(rpc, tmp_path, monkeypatch):
    from management.codex_project import _save_binding
    _save_binding(tmp_path, {'status': 'ready', 'workspace': str(tmp_path), 'project_id': 'test-project'})
    monkeypatch.setattr(rpc, 'fail_metadata', True)
    snapshots = []
    with pytest.raises(ai.AIError) as e:
        ai.generate(request(provider_thread_id='thread-1', provider_project_path=str(tmp_path)), {'enabled': True}, threading.Event(), snapshots.append)
    assert e.value.code == 'AI_TIMEOUT'
    assert [s['phase'] for s in snapshots] == ['connecting', 'resuming', 'thread_ready']
    assert snapshots[-1]['recovery'] == 'resumed'
    assert not any(method == 'turn/start' for method, p in rpc.instances[-1].requests)


def test_resume_rejection_keeps_original_identity(rpc, tmp_path, monkeypatch):
    monkeypatch.setattr(rpc,'rejected_resume',True)
    snapshots=[]
    with pytest.raises(ai.AIError):
        ai.generate(request(provider_thread_id='missing-thread',provider_project_path=str(tmp_path)),{'enabled':True},threading.Event(),snapshots.append)
    assert [s['phase'] for s in snapshots]==['connecting','resuming']
    assert not any(method=='thread/start' for method,params in rpc.instances[-1].requests)


def test_live_deltas_reasoning_retry_and_authoritative_replacement(rpc, monkeypatch):
    events = [notification('item/started', item={'id': 'reasoning-1', 'type': 'reasoning', 'text': 'PRIVATE CHAIN'}),
              notification('item/reasoning/summaryTextDelta', delta='PRIVATE CHAIN'),
              notification('error', willRetry=True, error={'message': 'PRIVATE ERROR'}),
              notification('item/started', item={'id': 'message-1', 'type': 'agentMessage'})]
    delta = json.dumps(proposal('增量摘要', actions=[{'command': 'create', 'payload_json': '{"secret":"PAYLOAD SECRET"}', 'reason': 'PRIVATE REASON'}]), ensure_ascii=True)
    events.extend(notification('item/agentMessage/delta', itemId='message-1', delta=delta[i:i+3]) for i in range(0, len(delta), 3))
    events.extend([completed('校验前的权威摘要'), done()])
    monkeypatch.setattr(rpc, 'events_template', events)
    snapshots = []
    result = ai.generate(request(), {'enabled': True}, threading.Event(), snapshots.append)
    assert any(s.get('preview_text') == '增量摘要' for s in snapshots)
    assert snapshots[-1]['preview_text'] == result['summary'] == '校验前的权威摘要'
    encoded = json.dumps(snapshots, ensure_ascii=False)
    assert all(secret not in encoded for secret in ['PRIVATE', 'PAYLOAD', 'payload_json'])
    assert {'reasoning', 'provider_retry', 'receiving', 'validating'}.issubset(s['phase'] for s in snapshots)
    assert not result['actions']


def test_other_thread_or_turn_cannot_change_preview_or_end_generation(rpc, monkeypatch):
    wrong = [notification('item/agentMessage/delta', itemId='foreign', delta='{"summary":"PRIVATE FOREIGN', threadId='thread-other'),
             completed('PRIVATE FOREIGN', turnId='turn-other'),
             notification('item/reasoning/textDelta', delta='PRIVATE REASON', turnId='turn-other'),
             done(threadId='thread-other'), done(turnId='turn-other')]
    monkeypatch.setattr(rpc, 'events_template', wrong + [completed('仅当前轮'), done()])
    snapshots = []
    result = ai.generate(request(), {'enabled': True}, threading.Event(), snapshots.append)
    assert result['summary'] == '仅当前轮'
    assert not any(s['phase'] == 'reasoning' for s in snapshots)
    assert 'PRIVATE' not in json.dumps(snapshots)


@pytest.mark.parametrize('stage', ['connecting', 'thread_ready', 'sending', 'waiting_model', 'receiving', 'validating'])
def test_progress_failure_stops_or_interrupts_without_silently_continuing(rpc, stage):
    cancel = threading.Event()
    def failing(value):
        if value['phase'] == stage:
            raise OSError('PRIVATE STORAGE ERROR')
    with pytest.raises(ai.AIError) as e:
        ai.generate(request(), {'enabled': True}, cancel, failing)
    assert e.value.code == 'AI_PROGRESS_FAILED' and cancel.is_set()
    assert 'PRIVATE' not in str(e.value)
    if stage == 'connecting':
        assert not rpc.instances
    else:
        instance = rpc.instances[-1]
        assert instance.closed
        if stage in {'thread_ready', 'sending'}:
            assert not any(m == 'turn/start' for m, p in instance.requests)
        else:
            assert instance.turn_id == 'turn-1'


def test_callback_cannot_mutate_later_snapshot_and_can_cancel_before_send(rpc):
    snapshots = []
    cancel = threading.Event()
    def callback(value):
        snapshots.append(dict(value))
        if value['phase'] == 'thread_ready':
            value['provider_thread_id'] = 'tampered'
        if value['phase'] == 'sending':
            cancel.set()
    with pytest.raises(ai.AIError) as e:
        ai.generate(request(), {'enabled': True}, cancel, callback)
    assert e.value.code == 'AI_CANCELLED'
    assert snapshots[-1]['provider_thread_id'] == 'thread-1'
    assert not any(m == 'turn/start' for m, p in rpc.instances[-1].requests)


def test_partial_preview_never_becomes_candidate_when_provider_fails(rpc, monkeypatch):
    monkeypatch.setattr(rpc, 'events_template', [notification('item/agentMessage/delta', itemId='message-1', delta='{"summary":"预览而已'), ai.AIError('AI_TIMEOUT', 'synthetic timeout')])
    snapshots = []
    with pytest.raises(ai.AIError) as e:
        ai.generate(request(), {'enabled': True}, threading.Event(), snapshots.append)
    assert e.value.code == 'AI_TIMEOUT'
    assert snapshots[-1]['preview_text'] == '预览而已'
    assert rpc.instances[-1].closed
    assert sum(m == 'turn/start' for m, p in rpc.instances[-1].requests) == 1


def test_delta_without_authoritative_completed_message_cannot_succeed(rpc, monkeypatch):
    monkeypatch.setattr(rpc, 'events_template', [notification('item/agentMessage/delta', itemId='message-1', delta=json.dumps(proposal())), done()])
    with pytest.raises(ai.AIError) as e:
        ai.generate(request(), {'enabled': True}, threading.Event(), lambda value: None)
    assert e.value.code == 'AI_EMPTY_OUTPUT'


def test_final_proposal_still_validates_allowed_commands(rpc, monkeypatch):
    bad = proposal(actions=[{'command': 'settings', 'payload_json': '{}', 'reason': 'not authorized'}])
    monkeypatch.setattr(rpc, 'events_template', [notification('item/completed', item={'id': 'message-1', 'type': 'agentMessage', 'text': json.dumps(bad)}), done()])
    with pytest.raises(ai.AIError) as e:
        ai.generate(request(), {'enabled': True}, threading.Event(), lambda value: None)
    assert e.value.code == 'AI_SCOPE_ERROR'


def test_delta_output_limits_fail_before_unbounded_accumulation(rpc, monkeypatch):
    monkeypatch.setattr(ai, 'MAX_OUTPUT_BYTES', 50)
    monkeypatch.setattr(rpc, 'events_template', [notification('item/agentMessage/delta', itemId='message-1', delta='x' * 51)])
    with pytest.raises(ai.AIError) as e:
        ai.generate(request(), {'enabled': True}, threading.Event(), lambda value: None)
    assert e.value.code == 'AI_OUTPUT_LIMIT' and rpc.instances[-1].closed


def test_timeout_observation_ignores_an_explicit_foreign_thread():
    rpc = object.__new__(ai._AppServer)
    rpc.thread_id, rpc.turn_id = 'mine', 'my-turn'
    rpc.phase, rpc.last_event = 'awaiting_model', None
    rpc._observe({'method': 'item/agentMessage/delta', 'params': {'threadId': 'foreign', 'turnId': 'my-turn', 'delta': 'private'}})
    assert rpc.phase == 'awaiting_model' and rpc.last_event is None
