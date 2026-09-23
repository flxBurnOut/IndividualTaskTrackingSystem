"""First-use Codex setup crosses one validated business command, using synthetic data."""
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
PROJECT = {'status': 'ready', 'name': 'Codex事务助手', 'workspace': 'C:/synthetic/Codex事务助手', 'project_path': 'C:/synthetic/Codex事务助手', 'project_id': 'synthetic-project', 'mcp_verified': True, 'created': True}


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
        return copy.deepcopy(PROJECT)
    monkeypatch.setattr(codex_project, 'ensure_project', ensure)
    return calls


def test_first_save_connects_project_before_persisting_settings(core, setup):
    result = command(core, 'configure_codex', {'ai': CONFIG})
    assert setup == [(core.root, CONFIG)]
    assert result['result']['codex_project'] == PROJECT
    settings = Core(core.root).query('settings')['settings']
    assert settings['ai'] == CONFIG and settings['codex_project'] == PROJECT
    assert core.query('list')['total'] == 0
    assert 'configure_codex' in core.query('capabilities')['commands']


def test_setup_releases_business_lock_while_connecting(core, monkeypatch):
    def ensure(*args):
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(command, core, 'create', {'type': 'task', 'title': 'Concurrent synthetic update'})
            future.result(timeout=3)
        return copy.deepcopy(PROJECT)
    monkeypatch.setattr(codex_project, 'ensure_project', ensure)
    with pytest.raises(BusinessError) as exc:
        command(core, 'configure_codex', {'ai': CONFIG})
    assert exc.value.code == 'revision_conflict'
    assert not core.query('settings')['settings']['ai']['enabled']
    assert core.query('list')['total'] == 1


def test_disable_preserves_independent_dialogue_connection(core, setup):
    command(core, 'configure_codex', {'ai': CONFIG})
    result = command(core, 'configure_codex', {'ai': {**CONFIG, 'enabled': False}})
    assert len(setup) == 1
    assert result['result']['codex_project']['status'] == 'disabled'
    settings = core.query('settings')['settings']
    assert not settings['ai']['enabled'] and settings['codex_project'] == PROJECT


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
def test_setup_failure_preserves_previous_configuration_and_has_no_receipt(core, monkeypatch, error):
    before = core.query('settings')
    def ensure(*args):
        raise error
    monkeypatch.setattr(codex_project, 'ensure_project', ensure)
    request_id = str(uuid.uuid4())
    with pytest.raises(type(error)):
        command(core, 'configure_codex', {'ai': CONFIG}, request_id=request_id)
    assert core.query('settings') == before
    assert not core.query('receipt', request_id=request_id)['found']


@pytest.mark.parametrize('project', [None, {}, {'status': 'ready'}, {'status': 'pending', 'mcp_verified': True}])
def test_partial_setup_is_not_reported_as_success(core, monkeypatch, project):
    monkeypatch.setattr(codex_project, 'ensure_project', lambda *args: project)
    with pytest.raises(BusinessError) as exc:
        command(core, 'configure_codex', {'ai': CONFIG})
    assert exc.value.code == 'codex_project_incomplete'
    assert core.query('state')['revision'] == 0


def test_repeated_request_replays_receipt_without_reconnecting(core, setup):
    state = core.query('state')
    rid = str(uuid.uuid4())
    first = command(core, 'configure_codex', {'ai': CONFIG}, request_id=rid, state=state)
    second = command(core, 'configure_codex', {'ai': CONFIG}, request_id=rid, state=state)
    assert second['replayed'] and second['result'] == first['result']
    assert len(setup) == 1
    command(core, 'configure_codex', {'ai': CONFIG})
    assert len(setup) == 2


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


def test_gui_saves_configuration_via_first_use_command_and_displays_receipt(dialog):
    view, bridge = dialog
    assert '尚未完成' in view.codex_project_note.text()
    view.save_ai()
    call = bridge.commands[-1]
    assert call['name'] == 'configure_codex' and call['payload'] == {'ai': CONFIG}
    assert not view.ai_save.isEnabled() and not view.executable.isEnabled()
    view.save_ai()
    assert len(bridge.commands) == 1
    call['callback']({'result': {'settings': {'ai': CONFIG}, 'codex_project': PROJECT}})
    assert view.ai_save.isEnabled() and view.executable.isEnabled()
    assert '上次已连接' in view.codex_project_note.text()
    assert PROJECT['workspace'] in view.codex_project_note.text()
    assert '已连接' in view.message.text()


def test_gui_setup_failure_preserves_entered_values_and_last_saved_configuration(dialog):
    view, bridge = dialog
    view.executable.setText('new-synthetic.exe')
    view.save_ai()
    bridge.commands[-1]['error']({'message': 'Synthetic repair required'})
    assert view.executable.text() == 'new-synthetic.exe'
    assert view.current_settings['ai'] == CONFIG
    assert view.ai_save.isEnabled() and '未完成' in view.codex_project_note.text()
    assert view.message.text() == 'Synthetic repair required'


def test_gui_missing_completion_receipt_does_not_claim_success(dialog):
    view, bridge = dialog
    view.save_ai()
    bridge.commands[-1]['callback']({'result': {'settings': {'ai': CONFIG}}})
    assert '完成回执' in view.message.text() and '未完成' in view.codex_project_note.text()


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
