"""Release skew and idle update shutdown on isolated loopback data spaces."""
from concurrent.futures import ThreadPoolExecutor
import errno
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from management import __version__, client as client_module, runtime, service as service_module
from management.client import Client, ClientError
from management.core import Core
from management.runtime_contract import client_headers, service_contract
from management.schemas import BusinessError
from management.service import Handler, Server
from management.storage import now


@pytest.fixture
def live(tmp_path, monkeypatch):
    core = Core(tmp_path / 'isolated-user-data')
    server = Server(core, 'synthetic-upgrade-credential')
    paths = []
    transform = {'state': lambda value: value}

    class TrackingHandler(Handler):
        def do_POST(self):
            paths.append(self.path)
            super().do_POST()

        def send_json(self, value, status=200):
            if self.path == '/v1/query/state' and status == 200:
                value = transform['state'](value)
            super().send_json(value, status)

    server.RequestHandlerClass = TrackingHandler
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    record = {'host': '127.0.0.1', 'port': server.server_port, 'token': server.token,
              'pid': os.getpid(), 'data_dir': str(core.root), 'service_contract': service_contract()}
    (core.root / 'runtime.json').write_text(json.dumps(record), encoding='utf-8')
    starts = []
    monkeypatch.setattr(client_module, 'start_service', lambda root: starts.append(root))
    yield SimpleNamespace(core=core, server=server, thread=thread, paths=paths,
                          transform=transform, starts=starts, record=record)
    server.stopping.set()
    server.shutdown()
    server.server_close()
    thread.join(2)


def raw(live, path, payload=None, *, headers=None, token=None):
    options = {'Content-Type': 'application/json',
               'Authorization': 'Bearer ' + (live.server.token if token is None else token)}
    options.update(headers or {})
    connection = http.client.HTTPConnection('127.0.0.1', live.server.server_port, timeout=3)
    try:
        connection.request('POST', '/v1/' + path, json.dumps(payload or {}), options)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize('contract', [None, {},
    {**service_contract(), 'app_version': '0.15.2'},
    {**service_contract(), 'app_version': '99.0.0'},
    {**service_contract(), 'protocol_version': 99},
    {**service_contract(), 'protocol_version': True},
    {**service_contract(), 'capabilities': []},
    {**service_contract(), 'capabilities': ['release_handshake','idle_update_shutdown']},
])
@pytest.mark.parametrize('autostart', [True, False])
def test_skewed_or_legacy_live_service_is_rejected_without_bootstrap(live, contract, autostart):
    live.transform['state'] = lambda value: {**value, 'service_contract': contract}
    client = Client.__new__(Client)
    before = live.core.query('state')
    with pytest.raises(ClientError) as error:
        client.__init__(live.core.root, autostart=autostart)
    assert error.value.code == 'service_version_mismatch'
    assert '未执行' in error.value.message and 'MCP' in error.value.message
    assert '先保存并关闭 Beta 主窗口' in error.value.message
    assert '等待完成后，在 Beta 托盘选择“退出 Beta 软件与后台”' in error.value.message
    assert '确认 Beta 后台退出后' in error.value.message and '启动个人事务管理Beta.vbs' in error.value.message
    assert '仅重新打开窗口不会替换仍在运行的旧后台' in error.value.message
    assert client.runtime is None and client.epoch is None and client.revision is None
    assert live.paths == ['/v1/query/state'] and not live.starts
    assert live.core.query('state') == before


def test_matching_release_and_mcp_observation_are_explicit(live):
    client = Client(live.core.root, autostart=False, entrance='mcp')
    state = client.state()
    assert state['service_contract'] == service_contract()
    assert state['maintenance'] is False
    assert 'client_entrances' not in state
    observed = client.query('runtime_status')['client_entrances']
    assert observed['mcp']['verified'] is True
    assert observed['mcp']['app_version'] == __version__
    assert observed['mcp']['checked_at']
    receipt = client.command('create', {'type': 'task', 'title': 'Synthetic preserved record'}, request_id='matching-create')
    assert receipt['revision'] == 1 and not live.starts


def test_state_reads_remain_available_to_inspect_legacy_entrance(live):
    status, value = raw(live, 'query/state')
    assert status == 200 and value['service_contract'] == service_contract()
    assert 'client_entrances' not in value
    status, value = raw(live, 'query/runtime_status')
    assert status == 200
    assert value['client_entrances']['unknown']['verified'] is False
    assert 'mcp' not in value['client_entrances']


@pytest.mark.parametrize('headers', [{}, client_headers() | {'X-PersonalManagement-Version': '0.15.2'},
                                  client_headers() | {'X-PersonalManagement-Protocol': '99'}])
@pytest.mark.parametrize('route', ['commands/create', 'native-discussion/begin', 'maintenance/shutdown-if-idle'])
def test_old_entrance_is_rejected_before_any_write_or_shutdown(live, headers, route):
    before = live.core.query('state')
    status, value = raw(live, route, headers=headers)
    assert status == 400 and value['error']['code'] == 'client_version_mismatch'
    assert live.core.query('state') == before and not live.server.stopping.is_set()
    assert not (live.core.root / 'update_pending.json').exists()


def test_changed_service_version_does_not_send_cached_write(live):
    client = Client(live.core.root, autostart=False)
    before = client.epoch, client.revision
    live.transform['state'] = lambda value: {**value, 'service_contract': {**service_contract(), 'app_version': '99.0.0'}}
    with pytest.raises(ClientError) as error:
        client.command('create', {'type': 'task', 'title': 'Do not save'}, request_id='skewed-create')
    assert error.value.code == 'service_version_mismatch'
    assert (client.epoch, client.revision) == before
    assert not any('/commands/' in path for path in live.paths) and not live.starts
    assert live.core.query('list')['total'] == 0


@pytest.mark.parametrize('channel', [None, 'stable'])
def test_same_version_wrong_channel_service_rejects_beta_client(live, channel):
    live.transform['state'] = lambda value: {**value, 'service_contract': {**service_contract(), 'channel': channel}}
    with pytest.raises(ClientError) as error:
        Client(live.core.root, autostart=False)
    assert error.value.code == 'service_version_mismatch'
    assert live.paths == ['/v1/query/state'] and not live.starts


@pytest.mark.parametrize('channel', ['', 'stable'])
@pytest.mark.parametrize('route', ['commands/create', 'maintenance/shutdown-if-idle'])
def test_same_version_non_beta_entrance_cannot_write_or_stop_beta(live, channel, route):
    from management.runtime_contract import CHANNEL_HEADER
    before = live.core.query('state')
    status, value = raw(live, route, headers=client_headers() | {CHANNEL_HEADER: channel})
    assert status == 400 and value['error']['code'] == 'client_version_mismatch'
    assert live.core.query('state') == before and not live.server.stopping.is_set()


def test_shutdown_is_authenticated_and_has_no_business_payload(live):
    for options, payload, code in [({'token': 'wrong'}, {}, 'unauthorized'),
                                    ({'headers': client_headers()}, {'erase': True}, 'validation')]:
        status, value = raw(live, 'maintenance/shutdown-if-idle', payload, **options)
        assert status in {400, 401} and value['error']['code'] == code
    assert not live.server.stopping.is_set() and not live.server.maintenance


def test_explicit_tray_exit_preserves_data_and_does_not_auto_restart(live):
    client = Client(live.core.root, autostart=False, entrance='tray')
    before = live.core.query('state')
    assert client.stop_service()['prepared']
    assert live.core.query('state') == before
    marker = json.loads((live.core.root / 'update_pending.json').read_text('utf-8'))
    assert marker['reason'] == 'exit' and marker['status'] == 'ready'
    with pytest.raises(runtime.UpdatePending) as error:
        runtime.require_no_pending_update(live.core.root)
    assert '由用户退出' in error.value.message


def test_tray_status_reports_jobs_without_changing_business_state(live):
    client = Client(live.core.root, autostart=False, entrance='tray')
    client.command('create_artifact_job', {'kind': 'text', 'relative_path': 'synthetic.txt', 'content': 'kept'})
    before = live.core.query('state')
    result = client.query('runtime_status')
    assert result['jobs'] == {'queued': 1}
    assert result['client_entrances']['tray']['verified']
    assert live.core.query('state') == before


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_update_refuses_pending_jobs_without_changing_them(live, status):
    client = Client(live.core.root, autostart=False)
    job = client.command('create_artifact_job', {'kind': 'text', 'relative_path': 'synthetic.txt', 'content': 'kept'})['result']['job']
    with live.core.store.connect() as c:
        c.execute('UPDATE jobs SET status=? WHERE id=?', (status, job['id']))
        before = dict(c.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
    with pytest.raises(ClientError) as error:
        client.prepare_update()
    assert error.value.code == 'update_busy' and error.value.details['pending_jobs'] == 1
    assert not live.server.maintenance and not live.server.stopping.is_set()
    assert not (live.core.root / 'update_pending.json').exists()
    with live.core.store.connect() as c:
        assert dict(c.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()) == before
    assert client.state()['revision'] == 1


@pytest.mark.parametrize('status', ['preparing', 'ready'])
def test_update_refuses_unfinished_file_operation(live, status):
    client = Client(live.core.root, autostart=False)
    with live.core.store.connect() as c:
        c.execute('INSERT INTO io_operations VALUES (?,?,?,?,?,?,?,?,?)',
                  ('synthetic-io', 'hash', 'import_asset', status, '{}', None, None, now(), now()))
    with pytest.raises(ClientError) as error:
        client.prepare_update()
    assert error.value.code == 'update_busy' and error.value.details['pending_file_operations'] == 1
    assert not live.server.maintenance and not live.server.stopping.is_set()


def test_update_refuses_in_flight_request_without_stopping_service(live, monkeypatch):
    client = Client(live.core.root, autostart=False)
    entered, release = threading.Event(), threading.Event()
    original = live.core.query

    def slow(name, **kwargs):
        if name == 'settings':
            entered.set()
            assert release.wait(3)
        return original(name, **kwargs)

    monkeypatch.setattr(live.core, 'query', slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.query, 'settings')
        try:
            assert entered.wait(3)
            with pytest.raises(ClientError) as error:
                client.prepare_update()
            assert error.value.code == 'update_busy' and error.value.details['active_requests'] == 1
            assert not live.server.maintenance and not live.server.stopping.is_set()
        finally:
            release.set()
        assert 'settings' in pending.result(3)


def test_draining_background_blocks_new_writes_and_only_reports_ready_after_join(live):
    client = Client(live.core.root, autostart=False)
    entered, release = threading.Event(), threading.Event()

    def finishing_batch():
        entered.set()
        assert release.wait(3)

    worker = threading.Thread(target=finishing_batch, daemon=True)
    worker.start()
    live.server.background = SimpleNamespace(threads=[worker])
    try:
        assert entered.wait(3)
        with pytest.raises(BusinessError) as error:
            live.server.prepare_update(drain_timeout=.01)
        assert error.value.code == 'update_draining'
        assert live.server.maintenance and live.server.stopping.is_set()
        marker = json.loads((live.core.root / 'update_pending.json').read_text('utf-8'))
        assert marker['status'] == 'draining'
        status, value = raw(live, 'commands/create', headers=client_headers())
        assert status == 400 and value['error']['code'] == 'service_updating'
        with pytest.raises(ClientError) as error:
            client.state()
        assert error.value.code == 'service_updating' and not live.starts
        release.set()
        worker.join(3)
        live.server._update_finisher.join(3)
        assert not live.server._update_finisher.is_alive()
        assert live.server._update_finished
        marker = json.loads((live.core.root / 'update_pending.json').read_text('utf-8'))
        assert marker['status'] == 'ready'
    finally:
        release.set()
        worker.join(3)


def test_idle_shutdown_preserves_database_identity_pending_review_and_files(live):
    client = Client(live.core.root, autostart=False)
    entity = client.command('create', {'type': 'task', 'title': 'Synthetic user task'}, request_id='user-task')
    job = client.command('create_artifact_job', {'kind': 'text', 'relative_path': 'synthetic.txt', 'content': 'kept'})['result']['job']
    with live.core.store.connect() as c:
        c.execute("UPDATE jobs SET status='awaiting_review' WHERE id=?", (job['id'],))
        c.execute('INSERT INTO conversation_operations(job_id,conversation_id,epoch,phase,pending_terminal,updated_at) VALUES (?,?,?,?,?,?)',
                  (job['id'], 'synthetic-conversation', client.epoch, 'completed', 1, now()))
    sentinel = live.core.root / 'synthetic-original.txt'
    sentinel.write_bytes(b'original user attachment bytes')
    before = live.core.query('state')
    value = client.prepare_update()
    assert value['prepared'] and value['status'] == 'shutdown_requested'
    live.thread.join(3)
    assert not live.thread.is_alive()
    marker = json.loads((live.core.root / 'update_pending.json').read_text('utf-8'))
    assert marker['format'] == 'personal-management-update/1'
    assert marker['data_dir'] == str(live.core.root) and marker['status'] == 'ready'
    assert live.core.query('state') == before
    assert live.core.query('get', id=entity['result']['entity']['id'])['entity']['title'] == 'Synthetic user task'
    assert live.core.query('job', id=job['id'])['job']['status'] == 'awaiting_review'
    assert sentinel.read_bytes() == b'original user attachment bytes'


@pytest.mark.parametrize('entry', ['start_service', 'bootstrap_service', 'run_service'])
@pytest.mark.parametrize('marker', ['{"format":"personal-management-update/1"}', '{broken'])
def test_update_marker_prevents_background_or_old_mcp_autostart(tmp_path, monkeypatch, entry, marker):
    (tmp_path / 'update_pending.json').write_text(marker, encoding='utf-8')
    calls = []
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *a, **kw: calls.append('process'))
    monkeypatch.setattr(service_module, 'Core', lambda *a: calls.append('core'))
    function = getattr(service_module if entry == 'run_service' else runtime, entry)
    with pytest.raises(runtime.UpdatePending):
        function(tmp_path)
    assert not calls and not (tmp_path / 'database.sqlite3').exists()


def test_client_reports_update_pending_without_bootstrap_loop(tmp_path):
    (tmp_path / 'update_pending.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ClientError) as error:
        Client(tmp_path)
    assert error.value.code == 'update_pending' and '主动打开 Beta 软件' in error.value.message


def test_resume_token_is_bound_to_marker_and_data_space(tmp_path):
    token = 'synthetic-resume-token-for-this-space'
    path = tmp_path / 'update_pending.json'
    marker = {'format': 'personal-management-update/1', 'data_dir': str(tmp_path), 'resume_token': token}
    path.write_text(json.dumps(marker), encoding='utf-8')
    runtime.require_no_pending_update(tmp_path, resume_token=token)
    for attempted in (None, '', 'other-resume-token-for-this-space'):
        with pytest.raises(runtime.UpdatePending):
            runtime.require_no_pending_update(tmp_path, resume_token=attempted)
    marker['data_dir'] = str(tmp_path / 'other')
    path.write_text(json.dumps(marker), encoding='utf-8')
    with pytest.raises(runtime.UpdatePending):
        runtime.require_no_pending_update(tmp_path, resume_token=token)


def test_bootstrap_propagates_explicit_resume_token(tmp_path, monkeypatch):
    token = 'synthetic-resume-token-for-startup'
    marker = {'format': 'personal-management-update/1', 'data_dir': str(tmp_path), 'resume_token': token}
    (tmp_path / 'update_pending.json').write_text(json.dumps(marker), encoding='utf-8')
    calls = []
    monkeypatch.setattr(runtime.subprocess, 'run', lambda args, **kw: calls.append(args))
    runtime.start_service(tmp_path, resume_token=token)
    assert calls[0][-2:] == ['--resume-update', token]
    assert '--bootstrap-service' in calls[0]


def test_cancelled_native_operation_does_not_permanently_block_update(live):
    client = Client(live.core.root, autostart=False)
    job = client.command('create_artifact_job', {'kind': 'text', 'relative_path': 'synthetic.txt', 'content': 'kept'})['result']['job']
    job = client.query('job', id=job['id'])['job']
    with live.core.store.connect() as c:
        c.execute('INSERT INTO conversation_operations(job_id,conversation_id,epoch,phase,pending_terminal,provider_generation,updated_at) VALUES (?,?,?,?,?,?,?)',
                  (job['id'], 'synthetic-conversation', client.epoch, 'waiting_model', 1, job['generation'], now()))
    client.command('cancel_job', {'id': job['id']})
    result = client.prepare_update()
    assert result['prepared']
    assert live.core.query('job', id=job['id'])['job']['status'] == 'cancelled'


def test_real_service_exits_releases_owner_lock_and_preserves_records(tmp_path):
    root = tmp_path / 'isolated-process-data'
    options = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen([sys.executable, '-m', 'management', '--service', '--data-dir', str(root)], **options)
    try:
        deadline = time.monotonic() + 15
        while not (root / 'runtime.json').exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError('Isolated service did not start')
            time.sleep(.05)
        client = Client(root, autostart=False)
        entity = client.command('create', {'type': 'task', 'title': 'Synthetic process task'}, request_id='process-user-task')['result']['entity']
        identity = client.epoch, client.revision
        result = client.prepare_update()
        assert result['prepared']
        assert process.wait(timeout=5) == 0
        lock = runtime.OwnerLock(root / 'service.lock')
        try:
            assert lock.acquire()
        finally:
            lock.release()
        current = Core(root)
        state = current.query('state')
        assert (state['epoch'], state['revision']) == identity
        assert current.query('get', id=entity['id'])['entity']['title'] == 'Synthetic process task'
        with pytest.raises(ClientError) as error:
            Client(root, entrance='mcp')
        assert error.value.code == 'update_pending'
        assert Path(result['data_dir']) == root
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        process.communicate(timeout=2)


@pytest.mark.parametrize('failure,code', [
    (BusinessError('upgrade_backup_failed', '无法完成升级前的数据库快照；原资料未迁移。', {'private': 'secret'}), 'upgrade_backup_failed'),
    (OSError(errno.ENOSPC, 'private raw error secret'), 'startup_storage_full'),
    (PermissionError('private raw error secret'), 'startup_permission'),
])
def test_client_surfaces_safe_current_startup_failure(tmp_path, monkeypatch, failure, code):
    starts = []

    def fail_core(root):
        raise failure

    def attempt(root):
        starts.append(root)
        try:
            service_module.run_service(root)
        except Exception as error:
            assert error is failure

    monkeypatch.setattr(service_module, 'Core', fail_core)
    monkeypatch.setattr(client_module, 'start_service', attempt)
    client = Client.__new__(Client)
    with pytest.raises(ClientError) as error:
        client.__init__(tmp_path)
    assert error.value.code == code and 'secret' not in error.value.message
    assert client.epoch is None and client.revision is None and len(starts) == 1
    record = (tmp_path / 'startup_failure.json').read_text('utf-8')
    assert 'secret' not in record and 'details' not in record
    assert code in (tmp_path / 'diagnostic.log').read_text('utf-8')


@pytest.mark.parametrize('changed', [{'failed_at_unix': 0}, {'app_version': '0.15.2'},
                                    {'data_dir': 'unrelated'}, {'format': 'other'},
                                    {'failed_at_unix': float('nan')}])
def test_old_or_unrelated_failure_does_not_poison_new_attempt(tmp_path, changed):
    record = {'format': 'personal-management-startup-failure/1', 'app_version': __version__,
              'data_dir': str(tmp_path), 'failed_at_unix': time.time(),
              'code': 'upgrade_backup_failed', 'message': 'Synthetic failure'}
    record.update(changed)
    (tmp_path / 'startup_failure.json').write_text(json.dumps(record), encoding='utf-8')
    assert runtime.read_startup_failure(tmp_path, not_before=time.time() - 1) is None


def test_real_failed_startup_can_be_fixed_and_retried_without_stale_failure(tmp_path, monkeypatch):
    root = tmp_path / 'invalid-upgrade-record'
    root.mkdir()
    # The upgrade guard must reject this invalid status before any migration.
    (root / 'upgrade-state.json').write_text('{broken', encoding='utf-8')
    processes = []

    def attempt(data_dir):
        options = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen([sys.executable, '-m', 'management', '--service', '--data-dir', str(data_dir)], **options)
        processes.append(process)

    monkeypatch.setattr(client_module, 'start_service', attempt)
    try:
        with pytest.raises(ClientError) as error:
            Client(root)
        assert error.value.code == 'upgrade_record'
        assert len(processes) == 1 and processes[0].wait(5) != 0
        assert (root / 'startup_failure.json').exists()
        (root / 'upgrade-state.json').unlink()
        client = Client(root)
        assert client.state()['service_contract']['app_version'] == __version__
        assert len(processes) == 2 and not (root / 'startup_failure.json').exists()
        client.prepare_update()
        assert processes[1].wait(5) == 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(5)
            process.communicate(timeout=2)


def test_draining_service_exits_without_gui_retry_and_explicit_launch_can_resume(tmp_path, monkeypatch):
    from management import scheduler
    from management.installation_state import resume_after_update
    root = tmp_path / 'detached-update-drain'
    release, entered = threading.Event(), threading.Event()
    servers, services, errors = [], [], []
    original_prepare = Server.prepare_update

    class SlowBackground:
        def __init__(self, core, stop):
            self.threads = []

        def start(self):
            def finish_current_read():
                entered.set()
                release.wait(10)
            worker = threading.Thread(target=finish_current_read, daemon=True)
            self.threads.append(worker)
            worker.start()

    def server_factory(*args):
        server = Server(*args)
        servers.append(server)
        return server

    def launch(data_dir, resume_token=None):
        def run():
            try:
                service_module.run_service(data_dir, resume_token=resume_token)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        services.append(thread)
        thread.start()

    monkeypatch.setattr(scheduler, 'Background', SlowBackground)
    monkeypatch.setattr(service_module, 'Server', server_factory)
    monkeypatch.setattr(Server, 'prepare_update', lambda server: original_prepare(server, drain_timeout=.01))
    monkeypatch.setattr(runtime, 'start_service', launch)
    try:
        launch(root)
        assert entered.wait(5)
        client = Client(root, autostart=False)
        identity = client.epoch, client.revision
        with pytest.raises(ClientError) as error:
            client.prepare_update()
        assert error.value.code == 'update_draining'
        assert servers[0]._update_finisher is not None
        client.close()  # The GUI disappears; no second prepare request is sent.
        release.set()
        services[0].join(5)
        assert not services[0].is_alive() and not errors
        marker = json.loads((root / 'update_pending.json').read_text('utf-8'))
        assert marker['status'] == 'ready'
        lock = runtime.OwnerLock(root / 'service.lock')
        try:
            assert lock.acquire()
        finally:
            lock.release()
        resume_after_update(root)
        resumed = Client(root, autostart=False)
        assert (resumed.epoch, resumed.revision) == identity
        assert not (root / 'update_pending.json').exists()
        assert resumed.prepare_update()['prepared']
        services[1].join(5)
        assert not services[1].is_alive() and not errors
    finally:
        release.set()
        for server in servers:
            server.stopping.set()
            server.shutdown()
        for thread in services:
            thread.join(5)
