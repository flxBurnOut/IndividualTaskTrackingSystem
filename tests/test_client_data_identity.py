"""Data-space identity across real loopback connections; no background/models."""
import asyncio
import json
import os
from pathlib import Path
import threading

import pytest
from mcp import Client as MCPClient

from management import client as client_module
from management.client import Client, ClientError
from management.core import Core
from management.mcp_server import create_server
from management.runtime import DataSpaceMismatch, discovery, require_data_dir
from management.service import Handler, Server


class LiveService:
    def __init__(self, core):
        self.core, self.paths, self.closed = core, [], False
        self.server = Server(core, 'synthetic-identity-' + str(id(self)))
        owner = self

        class TrackingHandler(Handler):
            def do_POST(self):
                owner.paths.append(self.path)
                super().do_POST()

        self.server.RequestHandlerClass = TrackingHandler
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def advertise(self, target, *, data_dir=None):
        value = {'host': '127.0.0.1', 'port': self.server.server_port,
                 'token': self.server.token, 'pid': os.getpid(),
                 'data_dir': str(self.core.root if data_dir is None else data_dir)}
        (target / 'runtime.json').write_text(json.dumps(value), encoding='utf-8')
        return value

    def close(self):
        if not self.closed:
            self.closed = True
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)


@pytest.fixture
def spaces(tmp_path, monkeypatch):
    a, b = Core(tmp_path / 'A'), Core(tmp_path / 'B')
    servers = []

    def start(core):
        service = LiveService(core)
        servers.append(service)
        return service

    # Discovery failures must never launch an unrelated real/background service.
    starts = []
    monkeypatch.setattr(client_module, 'start_service', lambda path: starts.append(path))
    yield a, b, start, starts
    for service in servers:
        service.close()


@pytest.mark.parametrize('autostart', [False, True])
def test_copied_runtime_rejected_before_contact_and_without_adopting_epoch(spaces, autostart):
    a, b, start, starts = spaces
    source = start(a)
    source.advertise(b.root)
    client = Client.__new__(Client)
    with pytest.raises(ClientError) as error:
        client.__init__(b.root, autostart=autostart)
    assert error.value.code == 'data_space_mismatch'
    assert client.epoch is None and client.revision is None and client.runtime is None
    assert source.paths == [] and starts == []
    assert a.query('list')['total'] == b.query('list')['total'] == 0


def test_state_identity_catches_runtime_which_claims_selected_path(spaces):
    a, b, start, starts = spaces
    source = start(a)
    source.advertise(b.root, data_dir=b.root)
    client = Client.__new__(Client)
    with pytest.raises(ClientError) as error:
        client.__init__(b.root)
    assert error.value.code == 'data_space_mismatch'
    assert source.paths == ['/v1/query/state'] and starts == []
    assert client.epoch is None and client.revision is None and client.runtime is None
    assert a.query('list')['total'] == b.query('list')['total'] == 0


def test_correct_directory_can_read_and_write_its_own_space(spaces):
    a, b, start, starts = spaces
    source = start(b)
    source.advertise(b.root)
    client = Client(b.root, autostart=False)
    assert Path(client.state()['data_dir']) == b.root
    receipt = client.command('create', {'type': 'task', 'title': 'Only B'}, request_id='identity-write-B')
    assert receipt['epoch'] == b.query('state')['epoch']
    assert a.query('list')['total'] == 0 and b.query('list')['total'] == 1
    assert starts == [] and source.paths.count('/v1/commands/create') == 1


def test_correct_directory_reconnects_to_new_port_and_token(spaces):
    _, b, start, starts = spaces
    old = start(b)
    old.advertise(b.root)
    client = Client(b.root, autostart=False)
    epoch = client.epoch
    old.close()
    new = start(b)
    new.advertise(b.root)
    assert client.state()['epoch'] == epoch
    assert client.runtime['port'] == new.server.server_port
    assert client.runtime['token'] == new.server.token and not starts


@pytest.mark.parametrize('operation', ['query', 'ensure_connected', 'command'])
def test_reconnect_never_adopts_another_data_space(spaces, operation):
    a, b, start, starts = spaces
    old, other = start(b), start(a)
    old.advertise(b.root)
    client = Client(b.root, autostart=False)
    before = client.epoch, client.revision
    old.close()
    other.advertise(b.root)
    with pytest.raises(ClientError) as error:
        if operation == 'query':
            client.query('list', type='task')
        elif operation == 'ensure_connected':
            client.ensure_connected()
        else:
            client.command('create', {'type': 'task', 'title': 'Must not save'}, request_id='wrong-space-reconnect')
    assert error.value.code == 'data_space_mismatch'
    assert (client.epoch, client.revision) == before and client.runtime is None
    assert other.paths == [] and starts == []
    assert a.query('list')['total'] == b.query('list')['total'] == 0


@pytest.mark.parametrize('operation', ['state', 'command'])
def test_changed_state_identity_cannot_change_epoch_or_send_write(spaces, monkeypatch, operation):
    a, b, start, _ = spaces
    source = start(b)
    source.advertise(b.root)
    client = Client(b.root, autostart=False)
    before = client.epoch, client.revision
    original = b.query

    def changed(name, **params):
        value = original(name, **params)
        return {**value, 'data_dir': str(a.root), 'epoch': 'wrong-space-epoch', 'revision': 999} if name == 'state' else value

    monkeypatch.setattr(b, 'query', changed)
    with pytest.raises(ClientError) as error:
        if operation == 'state':
            client.state()
        else:
            client.command('create', {'type': 'task', 'title': 'Must not save'}, request_id='state-identity-changed')
    assert error.value.code == 'data_space_mismatch'
    assert (client.epoch, client.revision) == before and client.runtime is None
    assert not any('/commands/' in path for path in source.paths)
    assert a.query('list')['total'] == original('list')['total'] == 0


@pytest.mark.parametrize('value', [None, '', 'relative/data', True, 42, 'bad\x00path'])
def test_missing_or_invalid_advertised_identity_is_not_a_valid_discovery(tmp_path, value):
    runtime = {'host': '127.0.0.1', 'port': 12345, 'token': 'synthetic', 'data_dir': value}
    (tmp_path / 'runtime.json').write_text(json.dumps(runtime), encoding='utf-8')
    with pytest.raises(DataSpaceMismatch):
        discovery(tmp_path)


def test_state_missing_data_dir_is_rejected_even_with_valid_runtime(spaces, monkeypatch):
    _, b, start, starts = spaces
    source = start(b)
    source.advertise(b.root)
    original = b.query

    def missing(name, **params):
        value = original(name, **params)
        if name == 'state':
            value.pop('data_dir')
        return value

    monkeypatch.setattr(b, 'query', missing)
    with pytest.raises(ClientError) as error:
        Client(b.root)
    assert error.value.code == 'data_space_mismatch'
    assert source.paths == ['/v1/query/state'] and starts == []


def test_normalized_path_variants_are_accepted(spaces):
    _, b, start, _ = spaces
    source = start(b)
    advertised = b.root / '..' / b.root.name
    source.advertise(b.root, data_dir=advertised)
    assert Client(b.root, autostart=False).state()['data_dir'] == str(b.root)


@pytest.mark.skipif(os.name != 'nt', reason='Windows path spelling contract')
@pytest.mark.parametrize('kind', ['case', 'extended'])
def test_windows_alternate_path_spelling_is_accepted(spaces, kind):
    _, b, start, _ = spaces
    source = start(b)
    advertised = str(b.root).upper() if kind == 'case' else '\\\\?\\' + str(b.root)
    source.advertise(b.root, data_dir=advertised)
    assert Client(b.root, autostart=False).state()['data_dir'] == str(b.root)


def test_symlink_alias_to_same_directory_is_accepted(tmp_path):
    actual, alias = tmp_path / 'actual', tmp_path / 'alias'
    actual.mkdir()
    try:
        alias.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip('This host does not allow creating a directory symlink')
    require_data_dir(str(alias), actual)
    require_data_dir(str(actual), alias)
    other = tmp_path / 'other'
    other.mkdir()
    with pytest.raises(DataSpaceMismatch):
        require_data_dir(str(alias), other)


def test_mcp_error_preserves_readable_identity_failure_and_sends_no_business_write(spaces):
    a, b, start, _ = spaces
    old, other = start(b), start(a)
    old.advertise(b.root)
    business = Client(b.root, autostart=False)
    before = business.epoch, business.revision
    server = create_server(b.root, client=business)
    old.close()
    other.advertise(b.root)

    async def scenario():
        async with MCPClient(server) as mcp:
            result = await mcp.session.call_tool('query_business', {'name': 'state'})
            assert result.is_error
            text = '\n'.join(block.text for block in result.content if block.type == 'text')
            assert 'data_space_mismatch' in text and '所选数据空间' in text

    asyncio.run(scenario())
    assert (business.epoch, business.revision) == before
    assert other.paths == []
    assert a.query('list')['total'] == b.query('list')['total'] == 0


def test_beta_default_ignores_formal_environment_and_installation(tmp_path, monkeypatch):
    from management import data_space, installation_state, runtime
    workspace = tmp_path / 'Beta'
    monkeypatch.setattr(data_space, 'workspace_root', lambda: workspace)
    monkeypatch.setenv('PERSONAL_MANAGEMENT_DATA', str(tmp_path / 'formal'))
    monkeypatch.setattr(installation_state, 'installed_data_dir', lambda: pytest.fail('Read formal installation'))
    assert runtime.default_data_dir() == workspace / '.beta-data' / 'default'
    assert not workspace.exists()


@pytest.mark.parametrize('entrance', ['client', 'store', 'service', 'bootstrap', 'resume', 'resources', 'restore', 'tray'])
def test_beta_rejects_formal_directory_before_open_connect_or_write(tmp_path, monkeypatch, entrance):
    from management import data_space, runtime
    from management.installation_state import resume_after_update
    from management.resources import ResourceManager
    from management.service import run_service
    from management.storage import Store
    from management.tray_runtime import gui_args
    formal = tmp_path / 'formal'
    formal.mkdir()
    (formal / 'database.sqlite3').write_bytes(b'Synthetic formal database; must never be opened')
    (formal / 'runtime.json').write_text('{"preserve":"formal discovery"}', encoding='utf-8')
    before = {p.name: p.read_bytes() for p in formal.iterdir()}
    workspace = tmp_path / 'Beta'
    monkeypatch.setattr(data_space, 'workspace_root', lambda: workspace)
    monkeypatch.setattr(client_module, 'discovery', lambda *_: pytest.fail('Contacted formal service'))
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *a, **k: pytest.fail('Started a process'))
    own = workspace / '.beta-data' / 'default'
    operations = {
        'client': lambda: Client(formal), 'store': lambda: Store(formal),
        'service': lambda: run_service(formal), 'bootstrap': lambda: runtime.start_service(formal),
        'resume': lambda: resume_after_update(formal), 'resources': lambda: ResourceManager(formal),
        'restore': lambda: ResourceManager(own).restore_backup(formal, formal / 'restore'),
        'tray': lambda: gui_args(formal),
    }
    with pytest.raises((data_space.BetaIsolationError, ClientError), match='Beta'):
        operations[entrance]()
    assert {p.name: p.read_bytes() for p in formal.iterdir()} == before


def test_beta_cli_rejects_formal_directory_without_starting_it(tmp_path, monkeypatch, capsys):
    from management import data_space, __main__
    formal = tmp_path / 'formal-not-created'
    monkeypatch.setattr(data_space, 'workspace_root', lambda: tmp_path / 'Beta')
    monkeypatch.setattr('sys.argv', ['management', '--service', '--data-dir', str(formal)])
    with pytest.raises(SystemExit) as error:
        __main__.main()
    assert error.value.code == 2 and 'Beta' in capsys.readouterr().err
    assert not formal.exists()


def test_beta_rejects_linked_database_before_sqlite_open(tmp_path, monkeypatch):
    from management import data_space
    from management.storage import Store
    workspace = tmp_path / 'Beta'
    own = workspace / '.beta-data' / 'default'
    own.mkdir(parents=True)
    formal = tmp_path / 'formal.sqlite3'
    formal.write_bytes(b'Synthetic formal database')
    (own / 'database.sqlite3').hardlink_to(formal)
    monkeypatch.setattr(data_space, 'workspace_root', lambda: workspace)
    with pytest.raises(data_space.BetaIsolationError):
        Store(own)
    assert formal.read_bytes() == b'Synthetic formal database'


def test_beta_rejects_directory_junction(tmp_path, monkeypatch):
    from management import data_space
    import subprocess
    if os.name != 'nt':
        pytest.skip('Windows junction')
    workspace, formal = tmp_path / 'Beta', tmp_path / 'formal'
    workspace.mkdir()
    formal.mkdir()
    junction = workspace / '.beta-data'
    # A synthetic junction only; unlink the junction without traversing target.
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(formal)],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    monkeypatch.setattr(data_space, 'workspace_root', lambda: workspace)
    try:
        with pytest.raises(data_space.BetaIsolationError):
            data_space.require_beta_dir(junction / 'default')
        assert list(formal.iterdir()) == []
    finally:
        junction.rmdir()


def test_beta_test_directory_requires_owned_check_marker(tmp_path, monkeypatch):
    from management import data_space
    workspace = tmp_path / 'Beta'
    work = workspace / '.build' / 'checks' / 'focused' / 'work'
    work.mkdir(parents=True)
    monkeypatch.setattr(data_space, 'workspace_root', lambda: workspace)
    with pytest.raises(data_space.BetaIsolationError):
        data_space.require_beta_dir(work / 'fixture')
    marker = work / '.disposable-test-work.json'
    marker.write_text('[]')
    with pytest.raises(data_space.BetaIsolationError):
        data_space.require_beta_dir(work / 'fixture')
    marker.write_text(json.dumps({'format': 'personal-management-test-work/1', 'path': str(work)}))
    assert data_space.require_beta_dir(work / 'fixture') == work / 'fixture'


def test_beta_does_not_change_formal_installation_selection(monkeypatch):
    from management import installation_state
    monkeypatch.setattr(installation_state, 'installed_data_dir', lambda *_: pytest.fail('Touched formal registry'))
    installation_state.remember_installed_data_dir('unused', 'unused')
