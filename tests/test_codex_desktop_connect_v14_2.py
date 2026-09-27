"""Automatic desktop connection uses fake executables and never opens Codex."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from management import ai_shared, codex_desktop
from management.schemas import BusinessError


@pytest.fixture
def desktop_environment(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    root.mkdir()
    paths = {}
    for name in ('real', 'shim', 'desktop', 'custom'):
        path = tmp_path / (name + '.exe')
        path.write_bytes(b'synthetic test executable; never run')
        paths[name] = path
    state = SimpleNamespace(root=root, paths=paths, running=[], ready=False,
                            launches=[], checks=[], selected=[])

    def find(executable=''):
        state.selected.append(executable)
        return executable or str(paths['real'])

    def status(*args, **kwargs):
        state.checks.append(kwargs)
        return {'ready': state.ready}

    monkeypatch.setattr(codex_desktop, 'find_codex', find)
    monkeypatch.setattr(codex_desktop, '_shim_path', lambda: paths['shim'])
    monkeypatch.setattr(codex_desktop, '_desktop_paths', lambda: [paths['desktop']])
    monkeypatch.setattr(codex_desktop.psutil, 'process_iter', lambda *args: list(state.running))
    monkeypatch.setattr(ai_shared, 'connection_status', status)
    monkeypatch.setattr(codex_desktop.subprocess, 'Popen',
                        lambda *args, **kwargs: state.launches.append((args, kwargs)))
    return state


def config_path(state):
    return state.root / 'codex-desktop-bridge' / codex_desktop.CONFIG_NAME


def pending_path(state):
    return state.root / 'codex-desktop-bridge' / 'launch-pending.json'


def test_first_open_prepares_and_starts_then_reuses_ready_connection(desktop_environment):
    state = desktop_environment
    first = codex_desktop.connect_step(state.root)
    assert first['state'] == 'starting' and first['ready'] is False
    assert len(state.launches) == 1
    assert config_path(state).is_file() and pending_path(state).is_file()
    state.ready = True
    connected = codex_desktop.connect_step(state.root)
    assert connected['state'] == 'ready' and connected['ready'] is True
    assert len(state.launches) == 1
    assert not pending_path(state).exists()
    assert state.checks[-1]['expected_bridge_executable'] == state.paths['shim']
    assert state.checks[-1]['expected_executable'] == state.paths['real']


def test_normal_desktop_waits_and_automatically_starts_after_exit(desktop_environment):
    state = desktop_environment
    state.running = [SimpleNamespace(info={'exe': str(state.paths['desktop'])})]
    waiting = codex_desktop.connect_step(state.root)
    assert waiting['state'] == 'waiting_for_exit' and waiting['ready'] is False
    assert not state.launches and not pending_path(state).exists()
    state.running.clear()
    assert codex_desktop.connect_step(state.root)['state'] == 'starting'
    assert len(state.launches) == 1


def test_pending_launch_is_starting_even_when_desktop_process_appears(desktop_environment):
    state = desktop_environment
    codex_desktop.connect_step(state.root)
    state.running = [SimpleNamespace(info={'exe': str(state.paths['desktop'])})]
    for _ in range(3):
        assert codex_desktop.connect_step(state.root)['state'] == 'starting'
    assert len(state.launches) == 1


def test_upgrade_repairs_managed_paths_without_settings_save(desktop_environment):
    state = desktop_environment
    codex_desktop.prepare(state.root)
    path = config_path(state)
    previous = json.loads(path.read_text('utf-8'))
    previous.update(shim=str(state.root / 'old-release' / 'shim.exe'),
                    desktop=str(state.root / 'removed-package' / 'desktop.exe'))
    path.write_text(json.dumps(previous), 'utf-8')
    assert codex_desktop.connect_step(state.root)['state'] == 'starting'
    updated = json.loads(path.read_text('utf-8'))
    assert updated['shim'] == str(state.paths['shim'])
    assert updated['desktop'] == str(state.paths['desktop'])


@pytest.mark.parametrize('damage', ['foreign', 'invalid_json', 'wrong_root', 'new_schema', 'array'])
def test_untrusted_configuration_is_preserved_and_never_launched(desktop_environment, damage):
    state = desktop_environment
    codex_desktop.prepare(state.root)
    path = config_path(state)
    value = json.loads(path.read_text('utf-8'))
    if damage == 'foreign':
        value['managed_by'] = 'someone-else'
    elif damage == 'wrong_root':
        value['data_dir'] = str(state.root / 'other')
    elif damage == 'new_schema':
        value['schema_version'] = 999
    elif damage == 'array':
        value = []
    content = '{invalid json' if damage == 'invalid_json' else json.dumps(value)
    path.write_text(content, 'utf-8')
    with pytest.raises(BusinessError):
        codex_desktop.connect_step(state.root)
    assert path.read_text('utf-8') == content
    assert not state.launches


def test_explicit_custom_cli_is_used_for_launch_and_readiness(desktop_environment):
    state = desktop_environment
    custom = str(state.paths['custom'])
    codex_desktop.connect_step(state.root, executable=custom)
    assert state.selected[-1] == custom
    assert state.launches[0][1]['env']['PM_CODEX_REAL_CLI'] == custom
    assert state.checks[-1]['expected_executable'] == state.paths['custom']


def test_legacy_launch_retains_prepared_custom_cli(desktop_environment):
    state = desktop_environment
    custom = str(state.paths['custom'])
    codex_desktop.prepare(state.root, executable=custom)
    codex_desktop.launch(state.root)
    assert state.launches[0][1]['env']['PM_CODEX_REAL_CLI'] == custom


def test_cancelled_request_does_not_prepare_or_launch(desktop_environment):
    state = desktop_environment
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(BusinessError):
        codex_desktop.connect_step(state.root, cancel=cancel)
    assert not config_path(state).exists()
    assert not state.launches


def test_cancel_during_discovery_prevents_late_launch(desktop_environment, monkeypatch):
    state = desktop_environment
    cancel = threading.Event()

    def discover():
        cancel.set()
        return [state.paths['desktop']]

    monkeypatch.setattr(codex_desktop, '_desktop_paths', discover)
    with pytest.raises(BusinessError):
        codex_desktop.connect_step(state.root, cancel=cancel)
    assert not state.launches and not pending_path(state).exists()


def test_concurrent_windows_and_delayed_process_visibility_start_only_once(desktop_environment, monkeypatch):
    state = desktop_environment
    entered, release = threading.Event(), threading.Event()

    def spawn(*args, **kwargs):
        state.launches.append((args, kwargs))
        entered.set()
        assert release.wait(5), 'test did not release mocked launch'

    monkeypatch.setattr(codex_desktop.subprocess, 'Popen', spawn)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(codex_desktop.connect_step, state.root)
        try:
            assert entered.wait(5), 'mocked launch was not reached'
            assert codex_desktop.connect_step(state.root)['state'] == 'starting'
        finally:
            release.set()
        assert first.result(timeout=5)['state'] == 'starting'
    assert not state.running
    assert codex_desktop.connect_step(state.root)['state'] == 'starting'
    assert len(state.launches) == 1


def test_failed_launch_cleans_pending_and_unlocks_for_retry(desktop_environment, monkeypatch):
    state = desktop_environment

    def fail(*args, **kwargs):
        raise OSError('synthetic denied launch')

    monkeypatch.setattr(codex_desktop.subprocess, 'Popen', fail)
    with pytest.raises(BusinessError, match='无法打开'):
        codex_desktop.connect_step(state.root)
    assert not pending_path(state).exists()
    monkeypatch.setattr(codex_desktop.subprocess, 'Popen',
                        lambda *args, **kwargs: state.launches.append((args, kwargs)))
    assert codex_desktop.connect_step(state.root)['state'] == 'starting'
    assert len(state.launches) == 1


def test_start_timeout_is_reported_without_launch_loop_and_can_retry(desktop_environment, monkeypatch):
    state = desktop_environment
    clock = [1000.0]
    monkeypatch.setattr(codex_desktop.time, 'time', lambda: clock[0])
    codex_desktop.connect_step(state.root)
    clock[0] += 31
    with pytest.raises(BusinessError) as error:
        codex_desktop.connect_step(state.root)
    assert error.value.code == 'codex_desktop_start_timeout'
    assert not pending_path(state).exists() and len(state.launches) == 1
    assert codex_desktop.connect_step(state.root)['state'] == 'starting'
    assert len(state.launches) == 2


def test_send_wait_checks_both_cli_and_shim(desktop_environment, monkeypatch):
    state = desktop_environment

    def status(*args, **kwargs):
        state.checks.append(kwargs)
        return {'ready': len(state.checks) >= 2}

    monkeypatch.setattr(ai_shared, 'connection_status', status)
    codex_desktop.ensure_connection(state.root, threading.Event(), executable=str(state.paths['custom']))
    assert len(state.checks) >= 2
    assert all(item.get('expected_bridge_executable') == state.paths['shim'] for item in state.checks)
    assert all(item.get('expected_executable') == state.paths['custom'] for item in state.checks)
