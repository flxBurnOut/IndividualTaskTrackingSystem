"""Synthetic scoped conversations: durable continuity, exact attachments, fencing."""
import json
import threading
import time
import uuid

import pytest

from management import ai, conversations
from management.core import Core
from management.scheduler import Background
from management.schemas import BusinessError
from management.storage import encode

SCOPE = {'kind': 'daily_plan', 'date': '2030-01-01'}


def command(core, name, payload):
    state = core.query('state')
    return core.command(name, payload, request_id=str(uuid.uuid4()), epoch=state['epoch'], expected_revision=state['revision'])['result']


@pytest.fixture
def core(tmp_path):
    core = Core(tmp_path / 'synthetic-conversations')
    command(core, 'settings', {'settings': {'ai': {'enabled': True}}})
    return core


def send(core, text='Synthetic discussion', scope=None, **kwargs):
    return command(core, 'send_message', {'scope': scope or SCOPE, 'text': text, **kwargs})


def result(actions=True):
    return {'summary': 'Synthetic assistant reply', 'unknowns': [], 'sources': [],
        'actions': [{'command': 'create', 'payload': {'type': 'task', 'title': 'Synthetic candidate'}, 'reason': 'Explicit test'}] if actions else [],
        'provider': {'kind': 'synthetic', 'thread_id': 'provider-synthetic', 'recovery': 'new'}}


def complete(core, id, actions=True):
    value = result(actions)
    with core.store.lock, core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        job = core._job(c, id)
        c.execute('UPDATE jobs SET status=?,result=? WHERE id=?', ('awaiting_review' if actions else 'completed', encode(value), id))
        assert conversations.complete_job(core, c, job, value)
        c.commit()


def asset(core, title):
    with core.store.lock, core.store.connect() as c:
        return core._create(c, {'type': 'asset', 'title': title,
            'data': {'source_kind': 'notice', 'extraction': {'status': 'text', 'coverage': {'complete': True}, 'warnings': []}}}, str(uuid.uuid4()))


def wait_job(core, id, status):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        job = core.query('job', id=id)['job']
        if job['status'] == status:
            return job
        time.sleep(.01)
    raise AssertionError(core.query('job', id=id)['job'])


def test_read_does_not_create_conversation(core):
    before = core.query('state')
    found = core.query('conversation', scope=SCOPE)
    assert found['conversation'] is None and found['messages'] == []
    with core.store.connect() as c:
        assert c.execute('SELECT count(*) FROM conversations').fetchone()[0] == 0
    assert core.query('state')['revision'] == before['revision']


def test_stable_scope_reopen_and_single_active_job(core):
    first = send(core)
    with pytest.raises(BusinessError) as error:
        send(core, 'Cannot overlap')
    assert error.value.code == 'conversation_busy'
    command(core, 'cancel_job', {'id': first['job']['id']})
    reopened = Core(core.root)
    second = send(reopened, 'Continue after reopening')
    assert second['conversation']['id'] == first['conversation']['id']
    assert second['job']['id'] != first['job']['id']
    other = send(reopened, scope={'kind': 'weekly_review', 'date': SCOPE['date']})
    assert other['conversation']['id'] != first['conversation']['id']


def test_durable_messages_and_current_candidate_replaced(core):
    first = send(core)
    complete(core, first['job']['id'])
    read = core.query('conversation', scope=SCOPE)
    assert [m['role'] for m in read['messages']] == ['user', 'assistant']
    assert read['conversation']['active_job_id'] is None
    assert read['conversation']['current_proposal_job_id'] == first['job']['id']
    second = send(core, 'Adjust this same proposal')
    value = second['job']['input']
    assert value['provider_thread_id'] == 'provider-synthetic'
    assert value['history']['messages'][-1]['proposal_state'] == 'superseded'
    assert value['history']['messages'][-1]['candidate_actions']
    assert core.query('job', id=first['job']['id'])['job']['status'] == 'superseded'
    with pytest.raises(BusinessError) as error:
        command(core, 'apply_proposal', {'id': first['job']['id']})
    assert error.value.code == 'stale_proposal'
    complete(core, second['job']['id'])
    command(core, 'apply_proposal', {'id': second['job']['id']})
    latest = core.query('conversation', scope=SCOPE)
    assert latest['messages'][-1]['proposal_state'] == 'applied'
    assert core.query('list', type='task')['total'] == 1


def test_attachment_replace_clear_and_omission_are_distinct(core):
    a, b = asset(core, 'Synthetic A'), asset(core, 'Synthetic B')
    first = send(core, source_ids=[a['id']])
    assert first['conversation']['source_ids'] == [a['id']]
    command(core, 'cancel_job', {'id': first['job']['id']})
    inherited = send(core)
    assert inherited['job']['input']['source_ids'] == [a['id']]
    command(core, 'cancel_job', {'id': inherited['job']['id']})
    replaced = send(core, source_ids=[b['id']])
    assert replaced['conversation']['source_ids'] == [b['id']]
    command(core, 'cancel_job', {'id': replaced['job']['id']})
    cleared = send(core, source_ids=[])
    assert cleared['conversation']['source_ids'] == []
    assert cleared['job']['input']['context']['materials'] == []
    assert core.query('conversation', scope=SCOPE)['conversation']['sources'] == []


def test_course_context_includes_children_events_and_autoselected_sources(core):
    course = command(core, 'create', {'type': 'course', 'title': 'Synthetic course', 'data': {'semester_start': '2030-01-07'}})['entity']
    task = command(core, 'create', {'type': 'task', 'title': 'Synthetic course task', 'parent_id': course['id']})['entity']
    event = command(core, 'create', {'type': 'event', 'title': 'Synthetic exam', 'data': {'owner_id': course['id'], 'event_kind': 'exam'}})['entity']
    source = asset(core, 'Synthetic course source')
    command(core, 'link', {'source_id': course['id'], 'target_id': source['id'], 'kind': 'uses'})
    scope = {'kind': 'course', 'entity_id': course['id']}
    first = send(core, scope=scope)
    assert first['conversation']['source_ids'] == [source['id']]
    records = first['job']['input']['context']['discussion_scope']['records']
    assert {course['id'], task['id'], event['id']} <= {record['id'] for record in records}
    assert first['conversation']['sources'][0]['title'] == source['title']


def test_prepare_version_race_rolls_back_entire_send(core, monkeypatch):
    source = asset(core, 'Synthetic source')
    from management import sources
    monkeypatch.setattr(sources, 'prepare_context', lambda *a: {'source_versions': {source['id']: 0}, 'source_context': [], 'local_images': []})
    with pytest.raises(BusinessError) as error:
        send(core, source_ids=[source['id']])
    assert error.value.code == 'source_conflict'
    assert core.query('conversation', scope=SCOPE)['conversation'] is None
    assert core.query('jobs')['items'] == []


def test_candidate_material_version_rechecked_before_apply(core):
    source = asset(core, 'Synthetic source')
    sent = send(core, source_ids=[source['id']])
    complete(core, sent['job']['id'])
    with core.store.connect() as c:
        c.execute('UPDATE entities SET version=version+1 WHERE id=?', (source['id'],))
    with pytest.raises(BusinessError) as error:
        command(core, 'apply_proposal', {'id': sent['job']['id']})
    assert error.value.code == 'source_conflict'
    assert core.query('list', type='task')['total'] == 0


def test_page_cursor_is_stable_and_history_bounded(core):
    for i in range(8):
        sent = send(core, 'Synthetic turn ' + str(i))
        complete(core, sent['job']['id'], actions=False)
    page = core.query('conversation', scope=SCOPE, limit=3)
    older = core.query('conversation', id=page['conversation']['id'], limit=3, before=page['next_before'])
    assert page['has_more'] and len(page['messages']) == len(older['messages']) == 3
    assert older['messages'][-1]['seq'] < page['messages'][0]['seq']
    latest = send(core, 'Continue')
    assert len(latest['job']['input']['history']['messages']) <= conversations.MAX_HISTORY_MESSAGES
    assert latest['job']['input']['history']['older_messages_omitted'] is True


def test_worker_reply_persists_without_changing_facts_revision(core, monkeypatch):
    monkeypatch.setattr(ai, 'generate', lambda *a: result(actions=False))
    sent = send(core)
    revision = core.query('state')['revision']
    stop = threading.Event()
    worker = threading.Thread(target=Background(core, stop).worker)
    worker.start()
    try:
        wait_job(core, sent['job']['id'], 'completed')
        state = core.query('conversation', scope=SCOPE)
        assert state['messages'][-1]['text'] == result(False)['summary']
        assert state['conversation']['active_job_id'] is None
        assert state['conversation']['current_proposal_job_id'] is None
        assert core.query('state')['revision'] == revision
    finally:
        stop.set(); worker.join(5)


def test_cancelled_worker_late_result_cannot_replace_messages(core, monkeypatch):
    started, release = threading.Event(), threading.Event()
    def generate(*args):
        started.set()
        assert release.wait(5)
        return result()
    monkeypatch.setattr(ai, 'generate', generate)
    sent = send(core)
    stop = threading.Event()
    worker = threading.Thread(target=Background(core, stop).worker)
    worker.start()
    try:
        assert started.wait(5)
        command(core, 'cancel_job', {'id': sent['job']['id']})
        following = send(core, 'Next user input')
        stop.set(); release.set(); worker.join(5)
        query = core.query('conversation', scope=SCOPE)
        assert query['conversation']['active_job_id'] == following['job']['id']
        assert query['conversation']['current_proposal_job_id'] is None
        assert [m['state'] for m in query['messages'][:-1]] == ['cancelled', 'cancelled']
        assert core.query('job', id=sent['job']['id'])['job']['result'] is None
    finally:
        stop.set(); release.set(); worker.join(5)


def test_worker_failure_and_restart_release_conversation(core, monkeypatch):
    def fail(*args):
        raise ai.AIError('AI_SYNTHETIC', 'Synthetic failure')
    monkeypatch.setattr(ai, 'generate', fail)
    sent = send(core)
    stop = threading.Event(); background = Background(core, stop)
    worker = threading.Thread(target=background.worker); worker.start()
    try:
        wait_job(core, sent['job']['id'], 'failed')
        read = core.query('conversation', scope=SCOPE)
        assert read['conversation']['active_job_id'] is None
        assert read['messages'][-1]['text'] == 'Synthetic failure'
    finally:
        stop.set(); worker.join(5)
    other = send(core)
    with core.store.connect() as c:
        c.execute("UPDATE jobs SET status='running' WHERE id=?", (other['job']['id'],))
    already_stopped = threading.Event(); already_stopped.set()
    restarted = Background(Core(core.root), already_stopped); restarted.start()
    for thread in restarted.threads:
        thread.join(5)
    read = core.query('conversation', scope=SCOPE)
    assert read['conversation']['active_job_id'] is None
    assert read['messages'][-1]['state'] == 'failed'


def test_restore_drops_provider_binding_and_candidate(core):
    sent = send(core); complete(core, sent['job']['id'])
    with core.store.connect() as c:
        conversations.reset_after_restore(c)
    read = core.query('conversation', scope=SCOPE)
    assert read['conversation']['provider_thread_id'] is None
    assert read['conversation']['current_proposal_job_id'] is None
    assert read['messages'][-1]['proposal_state'] == 'superseded'
    with pytest.raises(BusinessError) as error:
        command(core, 'apply_proposal', {'id': sent['job']['id']})
    assert error.value.code == 'stale_proposal'


@pytest.mark.parametrize('field', ['conversation_id', 'provider_thread_id', 'history', 'local_images', 'source_versions'])
def test_legacy_public_job_cannot_inject_conversation_internals(core, field):
    with pytest.raises(BusinessError) as error:
        command(core, 'create_job', {'kind': 'ai', 'input': {'prompt': 'Synthetic', field: 'not trusted'}})
    assert error.value.code == 'proposal_scope'


def test_large_previous_candidate_does_not_permanently_block_scope(core):
    first = send(core)
    oversized = result()
    oversized['summary'] = 'Synthetic long text ' * 2000
    with core.store.connect() as c:
        c.execute("UPDATE jobs SET status='awaiting_review',result=? WHERE id=?", (encode(oversized), first['job']['id']))
        conversations.complete_job(core,c,core._job(c,first['job']['id']),oversized)
    next_turn = send(core,'Discard the long candidate and answer this small question')
    history = next_turn['job']['input']['history']
    assert history['older_messages_omitted']
    assert history['messages'][-1]['content_excerpted']
    assert history['messages'][-1]['candidate_actions_omitted'] == 1
    assert len(encode(history)) < conversations.MAX_HISTORY_CHARACTERS


def test_questions_without_actions_are_visible_in_chat(core):
    sent = send(core)
    reply = result(False)
    reply['unknowns'] = ['Synthetic question that must not disappear']
    with core.store.connect() as c:
        conversations.complete_job(core,c,core._job(c,sent['job']['id']),reply)
    assert reply['unknowns'][0] in core.query('conversation',scope=SCOPE)['messages'][-1]['text']


def test_restore_marks_queued_message_cancelled(core):
    send(core)
    with core.store.connect() as c:
        conversations.reset_after_restore(c)
    read=core.query('conversation',scope=SCOPE)
    assert read['messages'][0]['state']=='cancelled'
    assert read['conversation']['active_job_id'] is None
