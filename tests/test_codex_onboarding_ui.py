"""Saving is local; explicit connection validates the project and live desktop."""
import copy
import os
from concurrent.futures import ThreadPoolExecutor
import uuid

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication

from management import codex_project
from management.ai import AIError
from management.core import Core
from management.gui_workflows import SettingsDialog
from management.schemas import BusinessError


CONFIG = {'enabled': True, 'executable': 'synthetic-codex.exe', 'model': 'synthetic-model', 'timeout_seconds': 180}
SHARED_CONFIG = {**CONFIG, 'execution_mode': 'desktop_shared'}
PROJECT = {'status': 'ready', 'name': 'Codex事务助手', 'workspace': 'C:/synthetic/Codex事务助手', 'project_path': 'C:/synthetic/Codex事务助手', 'project_id': 'synthetic-project', 'mcp_verified': True, 'instructions_verified': True, 'created': True}


def command(core, name, payload, *, request_id=None, state=None):
    state = state or core.query('state')
    return core.command(name, payload, request_id=request_id or str(uuid.uuid4()), epoch=state['epoch'], expected_revision=state['revision'])


@pytest.fixture
def core(tmp_path):
    return Core(tmp_path / 'synthetic-data')


@pytest.fixture
def setup(monkeypatch):
    calls = []
    def ensure(data_dir, config):
        calls.append((data_dir, copy.deepcopy(config)))
        return {**copy.deepcopy(PROJECT), 'workspace': str(data_dir/'Codex事务助手'),
                'project_path': str(data_dir/'Codex事务助手')}
    monkeypatch.setattr(codex_project, 'ensure_project', ensure)
    return calls


class Gateway:
    def __init__(self):
        self.connections = 0
    def connect(self):
        self.connections += 1
        return {'state': 'ready', 'ready': True, 'project_ready': True, 'mcp_ready': None}
    def close(self):
        pass


@pytest.fixture
def gateway(core):
    core._desktop_gateway = Gateway()
    return core._desktop_gateway


def test_first_save_only_prepares_local_project_and_persists_settings(core, setup, gateway):
    result = command(core, 'configure_codex', {'ai': CONFIG})
    assert setup == [] and gateway.connections == 0
    project = result['result']['codex_project']
    assert project['status'] == 'prepared' and not project.get('mcp_verified')
    assert (core.root/'Codex事务助手/.codex/config.toml').is_file()
    settings = Core(core.root).query('settings')['settings']
    assert settings['ai'] == CONFIG and settings['codex_project'] == project
    assert core.query('list')['total'] == 0
    assert 'configure_codex' in core.query('capabilities')['commands']


def test_explicit_connect_checks_project_before_normal_desktop_connection(core, setup, gateway):
    command(core, 'configure_codex', {'ai': SHARED_CONFIG})
    result = command(core, 'connect_codex', {})
    assert setup == [(core.root, SHARED_CONFIG)] and gateway.connections == 1
    assert result['result']['ready'] is True
    project = core.query('settings')['settings']['codex_project']
    assert project['project_id'] == PROJECT['project_id'] and project['mcp_verified'] is True
    assert project['workspace'] == str(core.root/'Codex事务助手')
    assert core.query('list')['total'] == 0


def test_explicit_connection_releases_business_lock_and_preserves_revision_guard(core, monkeypatch, gateway):
    command(core, 'configure_codex', {'ai': SHARED_CONFIG})
    def ensure(*args):
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(command, core, 'create', {'type': 'task', 'title': 'Concurrent synthetic update'})
            future.result(timeout=3)
        return {**copy.deepcopy(PROJECT), 'workspace': str(core.root/'Codex事务助手'),
                'project_path': str(core.root/'Codex事务助手')}
    monkeypatch.setattr(codex_project, 'ensure_project', ensure)
    with pytest.raises(BusinessError) as exc:
        command(core, 'connect_codex', {})
    assert exc.value.code == 'revision_conflict'
    assert core.query('settings')['settings']['ai'] == SHARED_CONFIG
    assert core.query('list')['total'] == 1


def test_disable_preserves_independent_dialogue_connection(core, setup, gateway):
    command(core, 'configure_codex', {'ai': SHARED_CONFIG})
    command(core, 'connect_codex', {})
    project = copy.deepcopy(core.query('settings')['settings']['codex_project'])
    result = command(core, 'configure_codex', {'ai': {**CONFIG, 'enabled': False}})
    assert len(setup) == 1
    assert result['result']['codex_project']['status'] == 'disabled'
    settings = core.query('settings')['settings']
    assert not settings['ai']['enabled'] and settings['codex_project'] == project
    assert gateway.connections == 1


def test_disabled_first_save_never_creates_project(core, setup):
    result = command(core, 'configure_codex', {'ai': {**CONFIG, 'enabled': False}})
    assert not setup and result['result']['codex_project']['status'] == 'disabled'
    assert 'codex_project' not in core.query('settings')['settings']


@pytest.mark.parametrize('change', [
    {'enabled': 'true'}, {'enabled': 1}, {'model': None}, {'model': 'x' * 257},
    {'model': 'bad\nmodel'}, {'executable': 42}, {'executable': 'x' * 4097},
    {'executable': 'bad\0path'}, {'timeout_seconds': True}, {'timeout_seconds': 9},
    {'timeout_seconds': 901}, {'timeout_seconds': 180.0}, {'timeout_seconds': '180'},
    {'unexpected': 'value'},
])
def test_invalid_configuration_cannot_start_codex_or_save(core, setup, change):
    before = core.query('settings')
    with pytest.raises(BusinessError) as exc:
        command(core, 'configure_codex', {'ai': {**CONFIG, **change}})
    assert exc.value.code == 'validation'
    assert not setup and core.query('settings') == before


@pytest.mark.parametrize('payload', [{'ai': {}}, {'ai': True}, {'settings': {'ai': CONFIG}}, {'ai': CONFIG, 'extra': True}])
def test_configuration_requires_complete_known_payload(core, setup, payload):
    with pytest.raises(BusinessError):
        command(core, 'configure_codex', payload)
    assert not setup and core.query('state')['revision'] == 0


@pytest.mark.parametrize('error', [BusinessError('codex_unavailable', 'Synthetic unavailable'), AIError('AI_START_FAILED', 'Synthetic start failure')])
def test_explicit_connection_failure_preserves_saved_configuration_and_has_no_receipt(core, monkeypatch, gateway, error):
    command(core, 'configure_codex', {'ai': SHARED_CONFIG})
    before = core.query('settings')
    def ensure(*args):
        raise error
    monkeypatch.setattr(codex_project, 'ensure_project', ensure)
    request_id = str(uuid.uuid4())
    with pytest.raises(type(error)):
        command(core, 'connect_codex', {}, request_id=request_id)
    assert core.query('settings') == before
    assert not core.query('receipt', request_id=request_id)['found']
    assert gateway.connections == 0


@pytest.mark.parametrize('project', [None, {}, {'status': 'ready'}, {'status': 'pending', 'mcp_verified': True}])
def test_partial_explicit_setup_is_not_reported_as_success(core, monkeypatch, gateway, project):
    command(core, 'configure_codex', {'ai': SHARED_CONFIG})
    before = core.query('settings')
    request_id = str(uuid.uuid4())
    monkeypatch.setattr(codex_project, 'ensure_project', lambda *args: project)
    with pytest.raises(BusinessError) as exc:
        command(core, 'connect_codex', {}, request_id=request_id)
    assert exc.value.code == 'codex_project_incomplete'
    assert core.query('settings') == before and gateway.connections == 0
    assert not core.query('receipt', request_id=request_id)['found']


def test_repeated_save_replays_receipt_without_preparing_again_or_connecting(core, setup, monkeypatch):
    from management import codex_workspace
    original = codex_workspace.prepare_workspace
    prepared = []
    def prepare(data_dir):
        prepared.append(data_dir)
        return original(data_dir)
    monkeypatch.setattr(codex_workspace, 'prepare_workspace', prepare)
    state = core.query('state')
    rid = str(uuid.uuid4())
    first = command(core, 'configure_codex', {'ai': CONFIG}, request_id=rid, state=state)
    second = command(core, 'configure_codex', {'ai': CONFIG}, request_id=rid, state=state)
    assert second['replayed'] and second['result'] == first['result']
    assert not setup and prepared == [core.root]
    command(core, 'configure_codex', {'ai': CONFIG})
    assert not setup and prepared == [core.root, core.root]


def test_local_preparation_failure_preserves_custom_files_and_previous_settings(core, monkeypatch, setup):
    from management import codex_workspace
    command(core, 'configure_codex', {'ai': CONFIG})
    custom = core.root/'Codex事务助手/AGENTS.md'
    custom.write_text('Synthetic user-authored instructions', encoding='utf-8')
    before = core.query('settings')
    request_id = str(uuid.uuid4())
    with pytest.raises(BusinessError) as error:
        command(core, 'configure_codex', {'ai': {**CONFIG, 'executable': 'new-explicit.exe'}}, request_id=request_id)
    assert error.value.code == 'codex_project_incomplete'
    assert custom.read_text('utf-8') == 'Synthetic user-authored instructions'
    assert core.query('settings') == before and not setup
    assert not core.query('receipt', request_id=request_id)['found']


def test_legacy_settings_remains_compatible_without_spawning(core, setup):
    command(core, 'settings', {'settings': {'ai': CONFIG}})
    assert not setup and core.query('settings')['settings']['ai'] == CONFIG
    with pytest.raises(BusinessError):
        command(core, 'settings', {'settings': {'codex_project': PROJECT}})
    assert not setup


class Bridge:
    epoch = 'synthetic'
    revision = 1
    def __init__(self):
        self.queries, self.commands = [], []
    def query(self, name, callback=None, error=None, **params):
        self.queries.append({'name': name, 'callback': callback, 'error': error, 'params': params})
    def command(self, name, payload, callback=None, error=None, **options):
        self.commands.append({'name': name, 'payload': copy.deepcopy(payload), 'callback': callback, 'error': error})


@pytest.fixture(scope='session')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(app, tmp_path):
    bridge = Bridge()
    view = SettingsDialog(bridge, {'types': []}, tmp_path / 'synthetic-space')
    next(q for q in bridge.queries if q['name'] == 'settings')['callback']({'settings': {'ai': dict(CONFIG)}})
    yield view, bridge
    view.close()
    view.deleteLater()
    app.processEvents()


def test_gui_saves_configuration_without_claiming_a_desktop_connection(dialog):
    view, bridge = dialog
    assert '首次连接或发送' in view.codex_project_note.text()
    view.save_ai()
    call = bridge.commands[-1]
    assert call['name'] == 'configure_codex' and call['payload'] == {'ai': CONFIG}
    assert not view.ai_save.isEnabled() and not view.executable.isEnabled()
    view.save_ai()
    assert len(bridge.commands) == 1
    call['callback']({'result': {'settings': {'ai': CONFIG}, 'codex_project': PROJECT}})
    assert view.ai_save.isEnabled() and view.executable.isEnabled()
    assert '已保存的项目' in view.codex_project_note.text()
    assert PROJECT['workspace'] in view.codex_project_note.text()
    assert '设置已保存' in view.message.text() and '已连接' not in view.message.text()


def test_gui_setup_failure_preserves_entered_values_and_last_saved_configuration(dialog):
    view, bridge = dialog
    view.executable.setText('new-synthetic.exe')
    view.save_ai()
    bridge.commands[-1]['error']({'message': 'Synthetic repair required'})
    assert view.executable.text() == 'new-synthetic.exe'
    assert view.current_settings['ai'] == CONFIG
    assert view.ai_save.isEnabled() and '尚未确认保存' in view.codex_project_note.text()
    assert view.message.text() == 'Synthetic repair required'


def test_gui_settings_receipt_needs_no_connection_receipt_and_does_not_claim_connected(dialog):
    view, bridge = dialog
    view.save_ai()
    bridge.commands[-1]['callback']({'result': {'settings': {'ai': CONFIG}}})
    assert '设置已保存' in view.message.text() and '已连接' not in view.message.text()
    assert '首次连接或发送' in view.codex_project_note.text()


def test_gui_disabling_keeps_project_record_visible(dialog):
    view, bridge = dialog
    view.current_settings['codex_project'] = copy.deepcopy(PROJECT)
    view.ai_enabled.setChecked(False)
    view.save_ai()
    bridge.commands[-1]['callback']({'result': {'codex_project': {'status': 'disabled'}}})
    assert '仍可独立使用' in view.codex_project_note.text()
    assert view.current_settings['codex_project'] == PROJECT
    assert '接口保留' in view.message.text()


def test_closed_settings_ignores_late_setup_result(dialog):
    view, bridge = dialog
    view.save_ai()
    view.reject()
    before = view.message.text()
    bridge.commands[-1]['callback']({'result': {'codex_project': PROJECT}})
    assert view.message.text() == before
