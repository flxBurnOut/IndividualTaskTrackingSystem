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
from management.runtime_contract import client_headers


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
    base = {'Authorization': 'Bearer ' + runtime['token'], 'Content-Type': 'application/json', **client_headers()}
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


def test_two_backends_keep_process_database_receipt_and_exit_independent(tmp_path):
    roots = [tmp_path / 'isolated-one', tmp_path / 'isolated-two']
    clients = []
    try:
        clients = [Client(root) for root in roots]
        a, b = clients
        assert a.runtime['pid'] != b.runtime['pid']
        assert a.runtime['port'] != b.runtime['port']
        assert a.runtime['token'] != b.runtime['token']
        assert a.epoch != b.epoch
        before_b = b.state()
        receipt = a.command('create', {'type': 'task', 'title': 'Synthetic isolated record'}, request_id='isolated-shared-request-01')
        assert receipt['revision'] == 1 and b.state() == before_b
        assert b.query('list', type='task')['total'] == 0
        a.stop_service()
        deadline = time.monotonic() + 10
        while psutil.pid_exists(a.runtime['pid']) and time.monotonic() < deadline:
            time.sleep(.05)
        assert not psutil.pid_exists(a.runtime['pid'])
        assert b.state() == before_b
        assert b.command('create', {'type': 'task', 'title': 'Still available'}, request_id='isolated-shared-request-01')['revision'] == 1
    finally:
        for root in roots:
            stop_owned_service(root)


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


def test_unrelated_new_task_does_not_invalidate_independent_candidate(tmp_path, monkeypatch):
    from management import ai
    core = Core(tmp_path / 'stale-proposal-synthetic')
    cmd(core, 'settings', {'settings': {'ai': {'enabled': True}}})
    monkeypatch.setattr(ai, 'generate', lambda *args: fake_proposal())
    id = cmd(core, 'create_job', {'kind': 'ai', 'input': {'prompt': 'Synthetic'}})['result']['job']['id']
    stop, worker = make_worker(core)
    try:
        wait_core_job(core, id, 'awaiting_review')
        cmd(core, 'create', {'type': 'task', 'title': 'Synthetic later edit'})
        cmd(core,'apply_proposal',{'id':id})
        assert {e['title'] for e in core.query('list',type='task')['items']}=={'Synthetic later edit','Synthetic candidate'}
        assert core.query('job',id=id)['job']['status']=='applied'
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
    expected = '完成或未完成' if has_plan else '记录实际情况'
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


@pytest.mark.parametrize('workflow', ['checkin', 'weekly_review'])
def test_review_reminder_without_ai_points_to_manual_feedback_and_notes(tmp_path, workflow):
    core = Core(tmp_path / 'manual-reminder-synthetic')
    assert core.query('settings')['settings']['ai']['enabled'] is False
    add_schedule(core, 'Synthetic manual reminder', workflow=workflow)
    Background(core, threading.Event()).tick(dt.datetime(2030, 2, 5, 2, tzinfo=dt.timezone.utc))
    notices = core.query('list', type='notification')['items']
    assert len(notices) == 1
    text = notices[0]['data']['content']
    assert '记录实际情况' in text and '文字小结' in text and 'Codex' not in text
    assert notices[0]['data']['business_date'] == '2030-02-05'
    assert core.query('jobs')['items'] == []
    for kind in ('plan', 'feedback', 'review', 'checkin'):
        assert core.query('list', type=kind)['total'] == 0


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


@pytest.fixture
def bounded_service(tmp_path, monkeypatch):
    """Real HTTP handler with controllable admission; no production process."""
    import os
    from types import SimpleNamespace
    from management import client as client_module
    from management.service import Server
    from management.runtime_contract import service_contract
    core = Core(tmp_path / 'bounded-service')
    server = Server(core, 'synthetic-bounded-token')
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    record = {'host': '127.0.0.1', 'port': server.server_port, 'pid': os.getpid(),
              'token': server.token, 'data_dir': str(core.root), 'service_contract': service_contract()}
    (core.root / 'runtime.json').write_text(json.dumps(record), encoding='utf-8')
    starts = []
    monkeypatch.setattr(client_module, 'start_service', lambda root: starts.append(root))
    client = Client(core.root, autostart=False)
    try:
        yield SimpleNamespace(core=core, server=server, client=client, starts=starts)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_saturated_service_returns_busy_before_accepting_write_and_recovers(bounded_service):
    live = bounded_service
    client = live.client
    initial = client.state()
    for _ in range(8):
        assert live.server.slots.acquire(timeout=2)
    try:
        payload = {'request_id': 'synthetic-overload-request', 'epoch': initial['epoch'],
                   'expected_revision': initial['revision'],
                   'payload': {'type': 'task', 'title': 'Must not be accepted'}}
        status, result, headers = raw_post(client, json.dumps(payload).encode(), path='/v1/commands/create')
        assert status == 503 and headers['Retry-After'] == '1'
        assert result['error']['code'] == 'service_busy'
        assert result['error']['details']['request_accepted'] is False
        assert live.core.query('state')['revision'] == initial['revision']
    finally:
        for _ in range(8):
            live.server.slots.release()
    assert client.state() == initial
    assert not client.query('receipt', request_id='synthetic-overload-request')['found']
    assert client.command('create', payload['payload'], request_id=payload['request_id'])['revision'] == 1


def test_busy_discovery_does_not_bootstrap_a_live_service(bounded_service):
    from management.client import ClientError
    live = bounded_service
    for _ in range(8):
        assert live.server.slots.acquire(timeout=2)
    try:
        with pytest.raises(ClientError, match='正忙') as caught:
            Client(live.core.root, autostart=True)
        assert caught.value.code == 'service_busy'
        assert live.starts == []
    finally:
        for _ in range(8):
            live.server.slots.release()
    assert live.client.state()['revision'] == 0


def test_busy_retry_is_bounded_and_reuses_exact_write_envelope(monkeypatch):
    from management import client as module
    from management.client import ClientError
    client = Client.__new__(Client)
    calls, sleeps = [], []
    import copy
    body = {'request_id': 'synthetic-stable-request', 'epoch': 'old-epoch', 'expected_revision': 7,
            'payload': {'title': 'Original edit'}}
    original = copy.deepcopy(body)
    def send(category, name, data, timeout):
        calls.append((category, name, data, copy.deepcopy(data)))
        body['payload']['title'] = 'Changed by another UI callback'
        if len(calls) < 3:
            raise ClientError('service_busy', 'busy', {'request_accepted': False, 'retry_after_seconds': 1})
        return {'replayed': False}
    monkeypatch.setattr(client, '_request_once', send)
    monkeypatch.setattr(module.time, 'sleep', sleeps.append)
    assert client._request('commands', 'create', body) == {'replayed': False}
    assert len(calls) == 3 and len(sleeps) == 2
    assert all(call[2] is calls[0][2] and call[3] == original for call in calls)
    assert calls[0][2] is not body
    calls.clear()
    def busy(*args, **kwargs):
        calls.append(1)
        raise ClientError('service_busy', 'busy', {'request_accepted': False, 'retry_after_seconds': 1})
    monkeypatch.setattr(client, '_request_once', busy)
    with pytest.raises(ClientError):
        client._request('commands', 'create', body)
    assert len(calls) == 3


@pytest.mark.parametrize('code,details', [
    ('connection_lost', {}), ('internal_error', {}), ('service_busy', {}),
    ('service_busy', {'request_accepted': True}), ('revision_conflict', {}),
    ('service_busy', {'request_accepted': False, 'retry_after_seconds': float('nan')})])
def test_uncertain_or_conflicting_write_is_not_replayed(monkeypatch, code, details):
    from management.client import ClientError
    client = Client.__new__(Client)
    calls = []
    def send(*args, **kwargs):
        calls.append(1)
        raise ClientError(code, 'synthetic', details)
    monkeypatch.setattr(client, '_request_once', send)
    with pytest.raises(ClientError):
        client._request('commands', 'create', {'request_id': 'synthetic-original'})
    assert len(calls) == 1


def test_commit_then_disconnect_recovers_one_receipt(bounded_service):
    from management.client import ClientError
    from management.service import Handler
    live = bounded_service
    dropped = []
    class DropFirstReply(Handler):
        def send_json(self, value, status=200):
            if self.path == '/v1/commands/create' and status == 200 and not dropped:
                dropped.append(True)
                self.close_connection = True
                return
            super().send_json(value, status)
    live.server.RequestHandlerClass = DropFirstReply
    state = live.client.state()
    options = {'request_id': 'synthetic-lost-response', 'epoch': state['epoch'], 'expected_revision': state['revision']}
    payload = {'type': 'task', 'title': 'Committed exactly once'}
    with pytest.raises(ClientError) as caught:
        live.client.command('create', payload, **options)
    assert caught.value.code == 'connection_lost'
    assert caught.value.details['request_id'] == options['request_id']
    assert live.core.query('state')['revision'] == 1
    assert live.client.query('receipt', request_id=options['request_id'])['found']
    assert live.client.command('create', payload, **options)['replayed']
    assert live.core.query('list', type='task')['total'] == 1
