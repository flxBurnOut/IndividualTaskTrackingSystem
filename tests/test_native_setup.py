"""Saved preferences and explicit desktop connection have separate contracts."""
import copy
import tomllib
import uuid

import pytest

from management import codex_project
from management.core import Core
from management.schemas import BusinessError


AI = {'enabled': True, 'execution_mode': 'desktop_shared', 'executable': '',
      'model': 'user-chosen-model', 'timeout_seconds': 180}


def command(core, name, payload, *, request_id=None, state=None):
    state = state or core.query('state')
    return core.command(name, payload, request_id=request_id or str(uuid.uuid4()),
                        epoch=state['epoch'], expected_revision=state['revision'])


class Gateway:
    def __init__(self):
        self.calls = []
        self.value = {'ready': False, 'state': 'desktop_closed', 'message': 'Synthetic desktop closed'}
    def status(self, **kwargs):
        self.calls.append(('status', kwargs))
        return dict(self.value)
    def connect(self):
        self.calls.append(('connect', {}))
        self.value = {'ready': True, 'state': 'ready', 'message': 'Synthetic verified channel'}
        return dict(self.value)


@pytest.fixture
def setup(tmp_path):
    core = Core(tmp_path / 'synthetic-data')
    core._desktop_gateway = Gateway()
    return core, core._desktop_gateway


def test_save_only_prepares_local_files_never_launches_or_probes_desktop(setup, monkeypatch):
    core, gateway = setup
    monkeypatch.setattr(codex_project, 'ensure_project', lambda *args: pytest.fail('Save must not connect'))
    monkeypatch.setattr(codex_project, 'open_desktop_workspace', lambda *args: pytest.fail('Save must not launch'))
    reply = command(core, 'configure_codex', {'ai': AI})
    saved = reply['result']
    assert saved['settings']['ai'] == AI
    assert saved['codex_project']['status'] == 'prepared'
    assert not saved['codex_project'].get('mcp_verified')
    assert (core.root / 'Codex事务助手' / '.codex' / 'config.toml').is_file()
    assert gateway.calls == []
    with core.store.connect() as c:
        assert c.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_explicit_connect_runs_once_and_replayed_receipt_does_not_reactivate(setup, monkeypatch):
    core, gateway = setup
    command(core, 'configure_codex', {'ai': AI})
    calls = []
    def ensure(root, ai):
        calls.append((root, copy.deepcopy(ai)))
        return {'status': 'ready', 'project_id': 'same-fixed-project', 'mcp_verified': True,
                'instructions_verified': True,
                'workspace': str(core.root / 'Codex事务助手')}
    monkeypatch.setattr(codex_project, 'ensure_project', ensure)
    state, request_id = core.query('state'), str(uuid.uuid4())
    first = command(core, 'connect_codex', {}, request_id=request_id, state=state)
    second = command(core, 'connect_codex', {}, request_id=request_id, state=state)
    assert first['result']['state'] == 'ready' and first['result']['ready']
    assert second['result'] == first['result'] and second['replayed']
    assert len(calls) == 1 and calls[0][1] == AI
    assert gateway.calls == [('connect', {})]
    with core.store.connect() as c:
        assert core.store.meta(c, 'settings')['codex_project']['project_id'] == 'same-fixed-project'
        assert c.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_saved_project_metadata_never_makes_connection_ready(setup, monkeypatch):
    core, gateway = setup
    workspace = core.root / 'Codex事务助手'
    workspace.mkdir()
    codex_project._save_binding(workspace, {'status': 'ready', 'workspace': str(workspace),
        'project_id': 'cached-project', 'mcp_verified': True})
    monkeypatch.setattr(codex_project, 'ensure_project', lambda *args: pytest.fail('Save only'))
    reply = command(core, 'configure_codex', {'ai': AI})
    assert reply['result']['codex_project']['project_id'] == 'cached-project'
    assert not core.query('codex_connection')['ready']
    assert gateway.calls == [('status', {'force': False})]


def test_repeated_save_preserves_custom_project_config_without_activation(setup, monkeypatch):
    core, gateway = setup
    monkeypatch.setattr(codex_project, 'ensure_project', lambda *args: pytest.fail('Save only'))
    command(core, 'configure_codex', {'ai': AI})
    path = core.root / 'Codex事务助手' / '.codex' / 'config.toml'
    original = path.read_text(encoding='utf-8')
    path.write_text('model = "existing-project-model"\n# Keep this user comment\n' + original
        + '\n[mcp_servers.user_notes]\ncommand="existing-user-helper"\nenabled=true\n', encoding='utf-8')
    command(core, 'configure_codex', {'ai': {**AI, 'model': 'new-preference'}})
    text = path.read_text(encoding='utf-8')
    saved = tomllib.loads(text)
    assert saved['model'] == 'existing-project-model' and '# Keep this user comment' in text
    assert saved['mcp_servers']['user_notes']['command'] == 'existing-user-helper'
    assert core.query('settings')['settings']['ai']['model'] == 'new-preference'
    assert gateway.calls == []


def test_status_reads_never_prepare_files_or_connect(setup, monkeypatch):
    core, gateway = setup
    monkeypatch.setattr(codex_project, 'ensure_project', lambda *args: pytest.fail('Read only'))
    for _ in range(3):
        assert core.query('codex_connection')['state'] == 'desktop_closed'
    assert all(name == 'status' for name, _ in gateway.calls)
    assert not (core.root / 'Codex事务助手').exists()


def test_disabled_or_invalid_settings_never_activate(setup, monkeypatch):
    core, gateway = setup
    monkeypatch.setattr(codex_project, 'ensure_project', lambda *args: pytest.fail('No activation'))
    with pytest.raises(BusinessError) as error:
        command(core, 'connect_codex', {})
    assert error.value.code == 'ai_not_configured'
    with pytest.raises(BusinessError):
        command(core, 'configure_codex', {'ai': {**AI, 'timeout_seconds': 0}})
    assert gateway.calls == []


def test_failed_explicit_connect_does_not_cache_false_readiness(setup, monkeypatch):
    core, gateway = setup
    command(core, 'configure_codex', {'ai': AI})
    def fail(*args):
        raise BusinessError('synthetic_project_failure', 'Synthetic setup failure')
    monkeypatch.setattr(codex_project, 'ensure_project', fail)
    with pytest.raises(BusinessError):
        command(core, 'connect_codex', {})
    assert not gateway.calls
    assert core.query('settings')['settings']['codex_project']['status'] == 'prepared'
