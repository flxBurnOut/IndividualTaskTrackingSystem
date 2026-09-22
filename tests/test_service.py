"""Synthetic loopback HTTP tests and deterministic worker failure injection."""
import datetime as dt
import hashlib
import http.client
import json
from pathlib import Path
import threading
import time
import uuid

import psutil
import pytest

from management.client import Client
from management.core import Core
from management.scheduler import Background
from management.schemas import BusinessError


def stop_owned_service(root):
    path = Path(root) / 'runtime.json'
    if not path.exists():
        return
    state = json.loads(path.read_text(encoding='utf-8'))
    if not psutil.pid_exists(state['pid']):
        return
    process = psutil.Process(state['pid'])
    args = process.cmdline()
    assert '--service' in args and str(Path(root).resolve()) in args
    process.terminate()
    process.wait(timeout=10)


@pytest.fixture
def service(tmp_path):
    root = (tmp_path / 'http-synthetic').resolve()
    client = Client(root)
    try:
        yield client
    finally:
        stop_owned_service(root)


def raw_post(client, body=b'{}', headers=None, path='/v1/query/state'):
    runtime = client.runtime
    base = {'Authorization': 'Bearer ' + runtime['token'], 'Content-Type': 'application/json'}
    base.update(headers or {})
    conn = http.client.HTTPConnection('127.0.0.1', runtime['port'], timeout=5)
    try:
        conn.request('POST', path, body=body, headers=base)
        response = conn.getresponse()
        status, response_headers = response.status, dict(response.getheaders())
        return status, json.loads(response.read()), response_headers
    finally:
        conn.close()


@pytest.mark.parametrize('headers,status,code', [
    ({'Authorization': 'Bearer wrong'}, 401, 'unauthorized'),
    ({'Authorization': ''}, 401, 'unauthorized'),
    ({'Origin': 'https://synthetic.invalid'}, 403, 'forbidden'),
    ({'Host': 'synthetic.invalid'}, 403, 'forbidden'),
    ({'Content-Type': 'text/plain'}, 400, 'protocol'),
    ({'Transfer-Encoding': 'chunked'}, 400, 'protocol'),
])
def test_http_rejects_untrusted_request(service, headers, status, code):
    received, result, _ = raw_post(service, headers=headers)
    assert received == status
    assert result['error']['code'] == code
    assert service.state()['revision'] == 0


def test_http_body_limits_and_valid_headers(service):
    status, value, headers = raw_post(service)
    assert status == 200 and value['counts'] == {}
    assert headers['Cache-Control'] == 'no-store'
    assert 'Access-Control-Allow-Origin' not in headers
    status, value, _ = raw_post(service, headers={'Content-Length': str(256 * 1024 + 1)})
    assert status == 400 and value['error']['code'] == 'request_limit'
    for body in (b'[]', b'{broken', b'{"limit":NaN}'):
        status, value, _ = raw_post(service, body=body, path='/v1/query/list')
        assert status == 400 and value['error']['code'] == 'validation'


def wait_job(client, id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.query('job', id=id)['job']
        if job['status'] not in {'queued', 'running'}:
            return job
        time.sleep(.025)
    raise AssertionError('synthetic job did not settle')


def test_http_artifact_registered_frozen_and_adopted(service):
    content = 'Synthetic persistent evidence\n'
    receipt = service.command('create_artifact_job', {'kind': 'markdown', 'relative_path': 'report.md', 'content': content})
    id = receipt['result']['job']['id']
    job = wait_job(service, id)
    assert job['status'] == 'completed', job['error']
    entity = job['result']['entity']
    data = entity['data']
    assert data['sha256'] == hashlib.sha256(content.encode()).hexdigest()
    assert data['validation']['execution'] == 'not_run'
    assert data['adopted_at'] is None and data['externally_submitted'] is False
    manifest = json.loads((service.data_dir / 'jobs' / id / '.workspace.json').read_text(encoding='utf-8'))
    assert manifest['job_id'] == id and manifest['files'][0]['state'] == 'frozen'
    assert (service.data_dir / data['path']).read_text(encoding='utf-8') == content
    service.state()
    adopted = service.command('adopt_artifact', {'id': entity['id'], 'version': entity['version']})
    assert adopted['result']['entity']['data']['adopted_at']
    assert adopted['result']['entity']['data']['externally_submitted'] is False


def test_http_checkin_keeps_original_business_date(service):
    task = service.command('create', {'type': 'task', 'title': 'Synthetic late reply'})['result']['entity']
    check = service.command('create_checkin', {'date': '2001-01-02', 'target_ids': [task['id']]})['result']['entity']
    assert service.query('list', type='feedback')['total'] == 0
    reply = service.command('respond_checkin', {'id': check['id'], 'answers': [
        {'question_id': check['data']['questions'][0]['id'], 'dimensions': {'completion': 'partial'}}
    ], 'source_text': 'Synthetic reply received after midnight'})['result']
    assert reply['business_date'] == '2001-01-02'
    assert reply['entities'][0]['data']['business_date'] == '2001-01-02'
    assert 'actual_minutes' not in reply['entities'][0]['data']['dimensions']
    assert service.query('review', start='2001-01-03', end='2001-01-03')['coverage']['feedback_records'] == 0


def cmd(core, name, payload, state=None):
    state = state or core.query('state')
    return core.command(name, payload, request_id=str(uuid.uuid4()), epoch=state['epoch'], expected_revision=state['revision'])


def make_worker(core):
    stop = threading.Event()
    background = Background(core, stop)
    thread = threading.Thread(target=background.worker, daemon=True)
    thread.start()
    return stop, thread


def wait_core_job(core, id, status, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = core.query('job', id=id)['job']
        if job['status'] == status:
            return job
        time.sleep(.01)
    raise AssertionError('expected job status: ' + status)


def fake_proposal():
    return {'summary': 'Synthetic proposal', 'unknowns': [], 'sources': [], 'actions': [
        {'command': 'create', 'payload': {'type': 'task', 'title': 'Synthetic candidate'}, 'reason': 'Synthetic'}
    ], 'provider': {'kind': 'synthetic_test_double'}}


def test_cancel_generation_discards_late_model_output(tmp_path, monkeypatch):
    from management import ai
    core = Core(tmp_path / 'cancel-synthetic')
    cmd(core, 'settings', {'settings': {'ai': {'enabled': True}}})
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def stubborn_generator(*args):
        started.set()
        assert release.wait(5)
        finished.set()
        return fake_proposal()
    monkeypatch.setattr(ai, 'generate', stubborn_generator)
    id = cmd(core, 'create_job', {'kind': 'ai', 'input': {'prompt': 'Synthetic'}})['result']['job']['id']
    stop, worker = make_worker(core)
    try:
        assert started.wait(5)
        before = core.query('job', id=id)['job']['generation']
        cmd(core, 'cancel_job', {'id': id})
        release.set()
        assert finished.wait(5)
        stop.set()
        worker.join(5)
        assert not worker.is_alive()
        job = core.query('job', id=id)['job']
        assert job['status'] == 'cancelled' and job['generation'] == before + 1
        assert job['result'] is None
        assert core.query('list', type='task')['total'] == 0
    finally:
        release.set()
        stop.set()
        worker.join(5)


def test_old_ai_proposal_cannot_overwrite_new_state(tmp_path, monkeypatch):
    from management import ai
    core = Core(tmp_path / 'stale-proposal-synthetic')
    cmd(core, 'settings', {'settings': {'ai': {'enabled': True}}})
    monkeypatch.setattr(ai, 'generate', lambda *args: fake_proposal())
    id = cmd(core, 'create_job', {'kind': 'ai', 'input': {'prompt': 'Synthetic'}})['result']['job']['id']
    stop, worker = make_worker(core)
    try:
        wait_core_job(core, id, 'awaiting_review')
        cmd(core, 'create', {'type': 'task', 'title': 'Synthetic later edit'})
        with pytest.raises(BusinessError) as error:
            cmd(core, 'apply_proposal', {'id': id})
        assert error.value.code == 'stale_proposal'
        assert [e['title'] for e in core.query('list', type='task')['items']] == ['Synthetic later edit']
        assert core.query('job', id=id)['job']['status'] == 'awaiting_review'
    finally:
        stop.set()
        worker.join(5)


def add_schedule(core, title, time_of_day='08:00', workflow='checkin'):
    return cmd(core, 'create', {'type': 'schedule', 'title': title, 'data': {
        'workflow': workflow, 'time': time_of_day, 'timezone': 'Asia/Shanghai',
        'frequency': 'daily', 'enabled': True, 'notify_unchanged': True,
    }})['result']['entity']


@pytest.mark.parametrize('has_plan', [False, True])
def test_scheduler_two_rules_same_day_remind_once_without_creating_checkins(tmp_path, has_plan):
    core = Core(tmp_path / 'scheduler-synthetic')
    target = cmd(core, 'create', {'type': 'task', 'title': 'Synthetic target'})['result']['entity']
    if has_plan:
        cmd(core, 'create_plan', {'date': '2030-02-05', 'mode': 'no_precise_time',
                                'blocks': [{'target_id': target['id'], 'minutes': 20}]})
    a, b = add_schedule(core, 'Synthetic morning'), add_schedule(core, 'Synthetic later', '09:00')
    scheduler = Background(core, threading.Event())
    instant = dt.datetime(2030, 2, 5, 2, tzinfo=dt.timezone.utc)
    scheduler.tick(instant)
    scheduler.tick(instant + dt.timedelta(hours=3))
    Background(Core(core.root), threading.Event()).tick(instant + dt.timedelta(hours=4))
    assert core.query('list', type='checkin')['total'] == 0
    assert core.query('list', type='review')['total'] == 0
    notices = core.query('list', type='notification')['items']
    assert len(notices) == 2
    assert {n['data']['schedule_id'] for n in notices} == {a['id'], b['id']}
    assert {n['data']['business_date'] for n in notices} == {'2030-02-05'}
    assert all(n['data']['review_mode'] == 'daily' and n['data']['target_id'] is None for n in notices)
    expected = '完成或未完成' if has_plan else 'Codex'
    assert all(expected in n['data']['content'] for n in notices)
    assert core.query('list', type='feedback')['total'] == 0
    with core.store.connect() as c:
        assert c.execute('SELECT count(*) FROM schedule_runs').fetchone()[0] == 2


def test_scheduled_weekly_review_only_reminds_without_saving_empty_report(tmp_path):
    core = Core(tmp_path / 'weekly-reminder-synthetic')
    add_schedule(core, 'Synthetic weekly reminder', workflow='weekly_review')
    worker = Background(core, threading.Event())
    instant = dt.datetime(2030, 2, 5, 2, tzinfo=dt.timezone.utc)
    worker.tick(instant)
    worker.tick(instant)
    notices = core.query('list', type='notification')['items']
    assert len(notices) == 1 and notices[0]['data']['review_mode'] == 'weekly'
    assert core.query('list', type='review')['total'] == 0
    assert core.query('list', type='checkin')['total'] == 0
    assert core.query('list', type='feedback')['total'] == 0


def test_restore_epoch_cancels_jobs_and_disables_replay(tmp_path):
    core = Core(tmp_path / 'source-synthetic')
    add_schedule(core, 'Synthetic daily')
    job_id = cmd(core, 'create_artifact_job', {'kind': 'text', 'relative_path': 'kept.txt', 'content': 'Synthetic pending'})['result']['job']['id']
    old = core.query('state')
    backup = cmd(core, 'backup', {})['result']
    target = tmp_path / 'restored-synthetic'
    cmd(core, 'restore_backup', {'path': backup['path'], 'target_dir': str(target)})
    restored = Core(target)
    assert restored.query('state')['epoch'] != old['epoch']
    assert restored.query('job', id=job_id)['job']['status'] == 'cancelled'
    schedule = restored.query('list', type='schedule')['items'][0]
    assert schedule['data']['enabled'] is False and schedule['data']['restore_review_required'] is True
    Background(restored, threading.Event()).tick(dt.datetime(2030, 2, 6, 2, tzinfo=dt.timezone.utc))
    assert restored.query('list', type='checkin')['total'] == 0
    assert restored.query('list', type='notification')['total'] == 0
    with pytest.raises(BusinessError) as error:
        cmd(restored, 'create', {'type': 'task', 'title': 'Synthetic old client'}, state=old)
    assert error.value.code == 'epoch_conflict'


def test_interrupted_restore_blocked_before_old_jobs_run(tmp_path):
    core = Core(tmp_path / 'restore-crash-source')
    add_schedule(core, 'Synthetic daily')
    cmd(core, 'create_artifact_job', {'kind': 'text', 'relative_path': 'kept.txt', 'content': 'Synthetic pending'})
    backup = cmd(core, 'backup', {})['result']
    target = tmp_path / 'restore-crash-target'
    core.resources.restore_backup(backup['path'], target)
    assert (target / 'restore_pending.json').exists()
    with pytest.raises(BusinessError):
        Core(target)