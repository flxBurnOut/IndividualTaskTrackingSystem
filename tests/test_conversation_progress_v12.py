"""Progress durability and late-event fences using synthetic local data only."""
import json
import threading
import uuid

import pytest

from management import conversation_progress as progress, conversations
from management.core import Core
from management.storage import now


SCOPE = {'kind': 'general'}


def command(core, name, payload):
    state = core.query('state')
    return core.command(name, payload, request_id=str(uuid.uuid4()), epoch=state['epoch'],
                        expected_revision=state['revision'])['result']


@pytest.fixture
def core(tmp_path):
    core = Core(tmp_path)
    command(core, 'settings', {'settings': {'ai': {'enabled': True}}})
    return core


def sent(core, text='Synthetic progress request'):
    return command(core, 'send_message', {'scope': SCOPE, 'text': text})


def claimed(core):
    result = sent(core)
    job = result['job']
    with core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        job = dict(c.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
        c.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (now(), job['id']))
        conversations.mark_running(c, job)
        assert progress.start(c, job)
        c.commit()
    publisher = progress.Publisher(core, job, threading.Event(), threading.Event())
    return job, publisher


def snapshot(core):
    return core.query('conversation', scope=SCOPE)


def row(core, job):
    with core.store.connect() as c:
        return dict(c.execute('SELECT * FROM conversation_progress WHERE job_id=?', (job['id'],)).fetchone())


def test_queued_and_running_progress_have_no_fake_completion(core):
    result = sent(core)
    read = snapshot(core)
    assert read['active_progress']['job_id'] == result['job']['id']
    assert read['active_progress']['phase'] == 'queued'
    assert read['active_progress']['preview_text'] == ''
    assert read['active_progress']['provider_thread_id'] is None
    assert read['latest_progress'] == read['active_progress']
    assert [m['role'] for m in read['messages']] == ['user']


def test_operational_snapshots_do_not_create_business_changes_or_message_history(core, monkeypatch):
    job, publish = claimed(core)
    before = core.query('state')
    with core.store.connect() as c:
        counts = {table: c.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                  for table in ('changes', 'receipts', 'entities', 'conversation_messages')}
    clock = [1000.0]
    monkeypatch.setattr(progress.time, 'monotonic', lambda: clock[0])
    publish({'phase': 'thread_ready', 'provider_thread_id': 'provider-thread', 'recovery': 'new'})
    for index in range(100):
        clock[0] += 1
        publish({'phase': 'receiving', 'provider_turn_id': 'provider-turn', 'preview_text': str(index)})
    assert core.query('state') == before
    with core.store.connect() as c:
        assert c.execute('SELECT count(*) FROM conversation_progress').fetchone()[0] == 1
        assert counts == {table: c.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in counts}
    assert snapshot(core)['active_progress']['preview_text'] == '99'
    assert snapshot(core)['conversation']['provider_thread_id'] == 'provider-thread'
    assert row(core, job)['provider_project_path'] == str(core.root / 'Codex事务助手')


def test_phase_and_id_updates_bypass_250ms_preview_throttle(core, monkeypatch):
    job, publish = claimed(core)
    clock = [100.0]
    monkeypatch.setattr(progress.time, 'monotonic', lambda: clock[0])
    assert publish({'phase': 'receiving', 'preview_text': 'a'})
    clock[0] += .10
    assert not publish({'phase': 'receiving', 'preview_text': 'ab'})
    assert row(core, job)['preview_text'] == 'a'
    clock[0] += .10
    assert not publish({'phase': 'receiving', 'preview_text': 'abc'})
    clock[0] += .06
    assert publish({'phase': 'receiving', 'preview_text': 'abcd'})
    clock[0] += .01
    assert publish({'phase': 'receiving', 'provider_thread_id': 'thread-1', 'preview_text': 'abcde'})
    clock[0] += .01
    assert not publish({'phase': 'receiving', 'preview_text': 'abcdef'})
    assert publish({'phase': 'validating'})
    assert row(core, job)['preview_text'] == 'abcdef'
    assert not publish({'phase': 'validating'})


@pytest.mark.parametrize('reason', ['epoch', 'generation', 'status', 'active_job', 'cancel', 'stop', 'finished'])
def test_late_callback_cannot_write_after_job_or_dataspace_changes(core, reason):
    job, publish = claimed(core)
    publish({'phase': 'waiting_model', 'provider_thread_id': 'thread-1', 'provider_turn_id': 'turn-1'})
    with core.store.connect() as c:
        if reason == 'epoch':
            core.store.set_meta(c, 'epoch', 'restored-data-epoch')
        elif reason == 'generation':
            c.execute('UPDATE jobs SET generation=generation+1 WHERE id=?', (job['id'],))
        elif reason == 'status':
            c.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job['id'],))
        elif reason == 'active_job':
            c.execute('UPDATE conversations SET active_job_id=NULL')
        elif reason == 'finished':
            c.execute('UPDATE conversation_progress SET finished_at=?', (now(),))
    if reason == 'cancel':
        publish.cancel.set()
    if reason == 'stop':
        publish.stop.set()
    original = row(core, job)
    with pytest.raises(progress.ProgressRejected):
        publish({'phase': 'reasoning'})
    assert row(core, job) == original


def test_cancel_during_publish_rolls_back_preview_and_early_provider_binding(core, monkeypatch):
    job, publish = claimed(core)
    original = row(core, job)
    check = publish._check_stop
    count = [0]
    def interrupted():
        count[0] += 1
        if count[0] == 4:
            publish.cancel.set()
        check()
    monkeypatch.setattr(publish, '_check_stop', interrupted)
    with pytest.raises(progress.ProgressRejected):
        publish({'phase': 'thread_ready', 'provider_thread_id': 'thread-late', 'recovery': 'new'})
    assert row(core, job) == original
    assert snapshot(core)['conversation']['provider_thread_id'] is None


def test_early_thread_binding_survives_timeout_and_next_message_resumes_it(core):
    job, publish = claimed(core)
    publish({'phase': 'thread_ready', 'provider_thread_id': 'thread-resume', 'recovery': 'new'})
    with core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE jobs SET status='failed',updated_at=? WHERE id=?", (now(), job['id']))
        conversations.update_job(core, c, job, 'failed', {'message': 'Synthetic provider timeout'})
        c.commit()
    read = snapshot(core)
    assert read['active_progress'] is None
    assert read['latest_progress']['provider_thread_id'] == 'thread-resume'
    assert read['latest_progress']['status'] == 'failed'
    assert read['conversation']['provider_thread_id'] == 'thread-resume'
    following = sent(core, 'Synthetic follow-up after timeout')['job']['input']
    assert following['provider_thread_id'] == 'thread-resume'
    assert following['provider_project_path'] == str(core.root / 'Codex事务助手')
    from context_harness import rows
    history=rows(core,{'input':following},'history')
    assert any(m['state']=='failed' for m in history)


def test_elapsed_updates_while_running_and_freezes_at_terminal_state(core, monkeypatch):
    clock = ['2030-01-01T00:00:00+00:00']
    monkeypatch.setattr(progress, 'now', lambda: clock[0])
    job, publish = claimed(core)
    clock[0] = '2030-01-01T00:00:12+00:00'
    assert snapshot(core)['active_progress']['elapsed_seconds'] == 12
    clock[0] = '2030-01-01T00:00:18+00:00'
    command(core, 'cancel_job', {'id': job['id']})
    assert snapshot(core)['active_progress'] is None
    assert snapshot(core)['latest_progress']['elapsed_seconds'] == 18
    clock[0] = '2030-01-01T00:59:00+00:00'
    with core.store.connect() as c:
        progress.finish(c, job['id'])
    assert snapshot(core)['latest_progress']['elapsed_seconds'] == 18
    assert row(core, job)['finished_at'] == '2030-01-01T00:00:18+00:00'


def test_success_finalizes_progress_without_appending_stream_fragments(core):
    job, publish = claimed(core)
    publish({'phase': 'receiving', 'provider_thread_id': 'thread-1', 'provider_turn_id': 'turn-1', 'preview_text': '仅预览'})
    result = {'summary': '最终回复', 'unknowns': [], 'sources': [], 'actions': [],
              'provider': {'thread_id': 'thread-1', 'turn_id': 'turn-1', 'project_path': str(core.root / 'Codex事务助手')}}
    with core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE jobs SET status='completed',result=?,updated_at=? WHERE id=?", (json.dumps(result), now(), job['id']))
        assert conversations.complete_job(core, c, job, result)
        c.commit()
    read = snapshot(core)
    assert read['active_progress'] is None
    assert read['latest_progress']['status'] == 'completed'
    assert read['latest_progress']['finished_at']
    assert [message['text'] for message in read['messages']] == ['Synthetic progress request', '最终回复']


def test_reset_removes_preview_provider_path_and_rejects_old_publisher(core):
    job, publish = claimed(core)
    publish({'phase': 'receiving', 'provider_thread_id': 'old-thread', 'preview_text': 'old preview'})
    with core.store.connect() as c:
        conversations.reset_after_restore(c)
    read = snapshot(core)
    assert read['active_progress'] is None and read['latest_progress'] is None
    assert read['conversation']['provider_thread_id'] is None
    with pytest.raises(progress.ProgressRejected):
        publish({'phase': 'validating'})


@pytest.mark.parametrize('snapshot_value', [
    {'phase': 'not-registered'},
    {'phase': 'receiving', 'preview_text': 'x' * 32001},
    {'phase': 'receiving', 'preview_text': '\ud800'},
    {'phase': 'receiving', 'preview_text': {'raw': 'JSON'}},
    {'phase': 'thread_ready', 'provider_thread_id': 'x' * 201},
    {'phase': 'thread_ready', 'provider_thread_id': ''},
    {'phase': 'thread_ready', 'provider_thread_id': 100},
    {'phase': 'thread_ready', 'recovery': 'unknown'},
])
def test_invalid_snapshots_fail_closed_without_changing_saved_row(core, snapshot_value):
    job, publish = claimed(core)
    before = row(core, job)
    with pytest.raises(progress.ProgressRejected):
        publish(snapshot_value)
    assert row(core, job) == before


def test_unregistered_fields_never_enter_progress_storage(core):
    job, publish = claimed(core)
    publish({'phase': 'receiving', 'preview_text': '可展示摘要', 'reasoning': 'PRIVATE',
             'payload_json': 'PRIVATE', 'raw_event': {'delta': 'PRIVATE'}, 'progress_percent': 98})
    encoded = json.dumps(row(core, job), ensure_ascii=False)
    assert '可展示摘要' in encoded
    assert 'PRIVATE' not in encoded and '98' not in row(core, job)['preview_text']


def test_preview_at_limit_remains_a_single_snapshot(core):
    job, publish = claimed(core)
    preview = '字' * 32000
    publish({'phase': 'receiving', 'preview_text': preview})
    assert snapshot(core)['active_progress']['preview_text'] == preview
    with core.store.connect() as c:
        assert c.execute('SELECT count(*) FROM conversation_progress').fetchone()[0] == 1


def test_running_ai_job_without_software_conversation_is_supported(core):
    job = command(core, 'create_job', {'kind': 'ai', 'input': {'prompt': 'Synthetic standalone candidate'}})['job']
    with core.store.connect() as c:
        job = dict(c.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
        c.execute("UPDATE jobs SET status='running' WHERE id=?", (job['id'],))
        progress.start(c, job)
    publish = progress.Publisher(core, job, threading.Event(), threading.Event())
    publish({'phase': 'waiting_model', 'provider_thread_id': 'ephemeral-thread', 'provider_turn_id': 'ephemeral-turn'})
    assert row(core, job)['conversation_id'] is None
    with core.store.connect() as c:
        assert progress.query(c, None) == {'active_progress': None, 'latest_progress': None}


def test_schema_initialization_and_start_are_idempotent(core):
    job, publish = claimed(core)
    publish({'phase': 'reasoning'})
    before = row(core, job)
    with core.store.connect() as c:
        progress.initialize(c)
        progress.start(c, job)
    assert row(core, job) == before


def test_worker_progress_is_queryable_before_generation_returns(core, monkeypatch):
    from management import ai
    from management.scheduler import Background
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    def fake_generate(value, settings, cancel, publish):
        publish({'phase': 'thread_ready', 'provider_thread_id': 'live-thread', 'recovery': 'new'})
        publish({'phase': 'waiting_model', 'provider_turn_id': 'live-turn'})
        publish({'phase': 'receiving', 'preview_text': '正在形成的摘要'})
        entered.set()
        assert release.wait(5)
        stop.set()
        raise ai.AIError('AI_TIMEOUT', 'Synthetic timeout after visible progress')
    monkeypatch.setattr(ai, 'generate', fake_generate)
    job = sent(core)['job']
    original = core.query('state')
    worker = threading.Thread(target=Background(core, stop).worker)
    worker.start()
    try:
        assert entered.wait(5)
        read = snapshot(core)
        assert read['active_progress']['phase'] == 'receiving'
        assert read['active_progress']['preview_text'] == '正在形成的摘要'
        assert read['active_progress']['provider_thread_id'] == 'live-thread'
        assert read['active_progress']['provider_turn_id'] == 'live-turn'
        assert read['messages'][-1]['role'] == 'user'
        assert read['messages'][-1]['state'] == 'running'
        assert core.query('job', id=job['id'])['job']['result'] is None
        assert core.query('state') == original
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        read = snapshot(core)
        assert read['active_progress']['job_id']==job['id']
        assert read['active_progress']['status']=='queued'
        assert read['conversation']['provider_thread_id']=='live-thread'
    finally:
        stop.set(); release.set(); worker.join(5)
