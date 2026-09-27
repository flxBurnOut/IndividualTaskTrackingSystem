"""Registered Windows application discovery; all activation is synthetic."""
import copy
import ctypes
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from management import codex_installation as installation


@pytest.fixture
def registered(tmp_path):
    full_name = 'OpenAI.Codex_26.924.2738.0_x64__2p2nqsd0c76g0'
    root = tmp_path / full_name
    executable = root / 'app' / 'ChatGPT.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'synthetic fixture; never executed')
    return {'name': 'OpenAI.Codex', 'package_full_name': full_name,
            'package_family': installation.PACKAGE_FAMILY, 'version': '26.924.2738.0',
            'install_location': str(root), 'status': 0, 'is_framework': False, 'is_resource': False,
            'applications': [{'id': 'App', 'executable': 'app/ChatGPT.exe',
                              'entry_point': 'Windows.FullTrustApplication'},
                             {'id': 'CoreCommandRunner', 'executable': 'app/resources/runner.exe',
                              'entry_point': 'Windows.FullTrustApplication'}]}


def test_discovery_uses_registration_manifest_and_not_leftover_mtime(monkeypatch, registered, tmp_path):
    leftover = tmp_path / 'OpenAI.Codex_99.999.9999.0_x64__2p2nqsd0c76g0' / 'app' / 'ChatGPT.exe'
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b'unregistered newer leftover')
    os.utime(leftover, (2_000_000_000, 2_000_000_000))
    monkeypatch.setattr(installation, '_read_registration', lambda: [registered])
    selected = installation.discover_codex()
    assert selected.version == '26.924.2738.0'
    assert selected.aumid == 'OpenAI.Codex_2p2nqsd0c76g0!App'
    assert selected.executable == str(Path(registered['install_location']) / 'app' / 'ChatGPT.exe')
    assert selected.as_dict()['source'] == 'current_user_registration'


@pytest.mark.parametrize('rows,code', [([], 'codex_not_registered'), ([{}, {}], 'codex_registration_ambiguous')])
def test_discovery_does_not_guess_for_missing_or_ambiguous_registration(monkeypatch, rows, code):
    monkeypatch.setattr(installation, '_read_registration', lambda: rows)
    with pytest.raises(installation.CodexInstallationError) as caught:
        installation.discover_codex()
    assert caught.value.code == code


@pytest.mark.parametrize('field,value', [
    ('name', 'Other.Codex'), ('package_family', 'OpenAI.Codex_anotherpublisher'),
    ('package_full_name', 'OpenAI.Codex_26.924.2738.0_x64__otherpublisher'),
    ('version', '26.924.9999.0'), ('status', 1), ('status', False),
    ('is_framework', True), ('is_resource', True), ('install_location', 'relative/path'),
    ('applications', []), ('applications', {}),
])
def test_registration_must_be_healthy_and_consistent(registered, field, value):
    registered[field] = value
    with pytest.raises(installation.CodexInstallationError) as caught:
        installation._validate_registration(registered)
    assert caught.value.code == 'codex_registration_invalid'


@pytest.mark.parametrize('field,value', [
    ('id', 'App!Other'), ('id', 'App;malicious'), ('id', '../App'),
    ('executable', 'app/../outside.exe'), ('executable', 'C:/outside.exe'),
    ('executable', '//host/share/Codex.exe'), ('entry_point', 'unexpected'),
])
def test_manifest_cannot_select_arbitrary_target(registered, field, value):
    registered['applications'][0][field] = value
    with pytest.raises(installation.CodexInstallationError):
        installation._validate_registration(registered)


def test_missing_desktop_file_is_diagnostic(registered):
    (Path(registered['install_location']) / 'app' / 'ChatGPT.exe').unlink()
    with pytest.raises(installation.CodexInstallationError):
        installation._validate_registration(registered)


def test_ambiguous_main_entries_are_not_selected_by_order(registered):
    registered['applications'].append(copy.deepcopy(registered['applications'][0]))
    with pytest.raises(installation.CodexInstallationError):
        installation._validate_registration(registered)


def test_registration_read_uses_current_user_and_never_injects_environment(monkeypatch, registered):
    monkeypatch.setattr(installation, '_WINDOWS', True)
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps([registered]))
    monkeypatch.setattr(installation.subprocess, 'run', run)
    assert installation._read_registration() == [registered]
    args, options = calls[0]
    assert '-NoProfile' in args and '-NonInteractive' in args
    script = args[-1]
    assert 'Get-AppxPackage -Name OpenAI.Codex -PackageTypeFilter Main' in script
    assert '-AllUsers' not in script and '-User' not in script
    assert 'Get-AppxPackageManifest' in script
    assert 'CODEX_CLI_PATH' not in script and 'PM_CODEX' not in script
    assert 'env' not in options and not options.get('shell', False)


@pytest.mark.parametrize('stdout', ['{}', 'null', 'invalid JSON', '[null]', '[{}]' * 20, ' ' * 65537],
                         ids=['object', 'null', 'text', 'null-item', 'concatenated', 'oversized'])
def test_malformed_or_excessive_registration_is_rejected(monkeypatch, stdout):
    monkeypatch.setattr(installation, '_WINDOWS', True)
    monkeypatch.setattr(installation.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout))
    with pytest.raises(installation.CodexInstallationError) as caught:
        installation._read_registration()
    assert caught.value.code == 'codex_registration_invalid'


@pytest.mark.parametrize('failure,code', [
    (OSError('synthetic'), 'codex_registration_unavailable'),
    (installation.subprocess.TimeoutExpired('synthetic', 15), 'codex_registration_timeout'),
])
def test_registration_query_failures_are_readable(monkeypatch, failure, code):
    monkeypatch.setattr(installation, '_WINDOWS', True)
    def run(*args, **kwargs):
        raise failure
    monkeypatch.setattr(installation.subprocess, 'run', run)
    with pytest.raises(installation.CodexInstallationError) as caught:
        installation._read_registration()
    assert caught.value.code == code


def test_normal_activation_refreshes_registration_and_reports_no_connection(monkeypatch, registered):
    calls = []
    monkeypatch.setattr(installation, '_read_registration', lambda: calls.append('read') or [registered])
    monkeypatch.setattr(installation, '_activate_aumid', lambda aumid: calls.append(aumid) or 4242)
    before = dict(os.environ)
    result = installation.activate_codex()
    assert calls == ['read', 'OpenAI.Codex_2p2nqsd0c76g0!App']
    assert result['status'] == 'activation_requested' and result['process_id'] == 4242
    assert 'ready' not in result and 'connected' not in result
    assert '尚待检查' in result['message']
    assert dict(os.environ) == before


def test_activation_failure_is_not_success(monkeypatch, registered):
    monkeypatch.setattr(installation, '_read_registration', lambda: [registered])
    def activate(aumid):
        raise OSError('synthetic')
    monkeypatch.setattr(installation, '_activate_aumid', activate)
    with pytest.raises(installation.CodexInstallationError) as caught:
        installation.activate_codex()
    assert caught.value.code == 'codex_activation_failed'


def test_non_windows_does_not_query_or_activate(monkeypatch):
    monkeypatch.setattr(installation, '_WINDOWS', False)
    with pytest.raises(installation.CodexInstallationError, match='Windows'):
        installation.discover_codex()
    with pytest.raises(installation.CodexInstallationError, match='Windows'):
        installation._activate_aumid(installation.PACKAGE_FAMILY + '!App')


@pytest.mark.parametrize('aumid', ['Other.Package!App', installation.PACKAGE_FAMILY + '!App;other', installation.PACKAGE_FAMILY + '!'])
def test_native_activation_rejects_untrusted_aumid_before_windows_calls(monkeypatch, aumid):
    monkeypatch.setattr(installation, '_WINDOWS', True)
    with pytest.raises(installation.CodexInstallationError) as caught:
        installation._activate_aumid(aumid)
    assert caught.value.code == 'codex_registration_invalid'


@pytest.mark.skipif(os.name != 'nt', reason='Synthetic Windows COM ABI test')
@pytest.mark.parametrize('initialized,activation_result,expect_uninitialize', [
    (0, 0, True), (1, 0, True), (-2147417850, 0, False), (0, -2147024891, True),
])
def test_native_activation_uses_normal_package_contract_and_cleans_com(
        monkeypatch, initialized, activation_result, expect_uninitialize):
    calls = []
    @ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)
    def release(manager):
        calls.append('release')
        return 0
    @ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                       ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32))
    def activate(manager, aumid, arguments, options, process_id):
        calls.append(('activate', aumid, arguments, options))
        process_id[0] = 4242
        return activation_result
    vtable = (ctypes.c_void_p * 4)(0, 0, ctypes.cast(release, ctypes.c_void_p).value,
                                 ctypes.cast(activate, ctypes.c_void_p).value)
    manager = ctypes.pointer(ctypes.pointer(vtable))
    class Function:
        def __init__(self, function):
            self.function = function
        def __call__(self, *args):
            return self.function(*args)
    def create(class_id, outer, context, interface_id, target):
        calls.append(('create', context))
        ctypes.cast(target, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(manager, ctypes.c_void_p)
        return 0
    fake = SimpleNamespace(CoInitializeEx=Function(lambda *args: initialized),
                           CoCreateInstance=Function(create),
                           CoUninitialize=Function(lambda: calls.append('uninitialize')))
    monkeypatch.setattr(installation.ctypes, 'WinDLL', lambda name: fake)
    if activation_result:
        with pytest.raises(installation.CodexInstallationError) as caught:
            installation._activate_aumid(installation.PACKAGE_FAMILY + '!App')
        assert '0x80070005' in caught.value.message
    else:
        assert installation._activate_aumid(installation.PACKAGE_FAMILY + '!App') == 4242
    assert ('create', 4) in calls
    assert ('activate', installation.PACKAGE_FAMILY + '!App', None, 0) in calls
    assert 'release' in calls
    assert ('uninitialize' in calls) is expect_uninitialize
