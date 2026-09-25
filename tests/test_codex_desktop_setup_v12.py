import copy
import json
from pathlib import Path
import threading
import pytest
from management import codex_desktop
from management.schemas import BusinessError
from test_codex_onboarding_ui import command, CONFIG, PROJECT
from management.core import Core


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    root = tmp_path / 'data'; root.mkdir()
    real = tmp_path / 'real.exe'; real.write_bytes(b'synthetic')
    shim = tmp_path / 'shim.exe'; shim.write_bytes(b'synthetic')
    desktop = tmp_path / 'desktop.exe'; desktop.write_bytes(b'synthetic')
    monkeypatch.setattr(codex_desktop, 'find_codex', lambda value: str(real))
    monkeypatch.setattr(codex_desktop, '_shim_path', lambda: shim)
    monkeypatch.setattr(codex_desktop, '_desktop_paths', lambda: [desktop])
    monkeypatch.setattr(codex_desktop.psutil, 'process_iter', lambda *a: [])
    from management import ai_shared
    monkeypatch.setattr(ai_shared, 'connection_status', lambda *a, **kw: {'ready': False})
    codex_desktop.prepare(root)
    return root, real, shim, desktop


def test_prepare_is_reversible_local_metadata_only(prepared):
    root, real, shim, desktop = prepared
    config = json.loads((root/'codex-desktop-bridge/desktop-launch.json').read_text('utf-8'))
    assert config['real_cli'] == str(real) and config['shim'] == str(shim)
    assert config['desktop'] == str(desktop) and config['data_dir'] == str(root)
    assert not (root/'database.sqlite3').exists()


def test_launch_scopes_environment_and_does_not_change_parent(prepared, monkeypatch):
    root, real, shim, desktop = prepared
    calls=[]; before=dict(codex_desktop.os.environ)
    monkeypatch.setattr(codex_desktop.subprocess, 'Popen', lambda *a, **kw: calls.append((a, kw)))
    result=codex_desktop.launch(root)
    assert result == {'launched': True, 'ready': False}
    assert calls[0][0][0] == [str(desktop)]
    env=calls[0][1]['env']
    assert env['CODEX_CLI_PATH'] == str(shim) and env['PM_CODEX_REAL_CLI'] == str(real)
    assert 'CODEX_APP_SERVER_WS_URL' not in env
    assert dict(codex_desktop.os.environ) == before


def test_running_normal_desktop_is_not_killed_or_restarted(prepared, monkeypatch):
    root, real, shim, desktop = prepared
    class Process: info={'exe':str(desktop)}
    monkeypatch.setattr(codex_desktop.psutil, 'process_iter', lambda *a: [Process()])
    monkeypatch.setattr(codex_desktop.subprocess, 'Popen', lambda *a, **kw: pytest.fail('must not start a second desktop'))
    with pytest.raises(BusinessError, match='保存并退出'):
        codex_desktop.launch(root)


def test_live_shared_connection_needs_no_restart(prepared, monkeypatch):
    from management import ai_shared
    root, *_ = prepared
    monkeypatch.setattr(ai_shared, 'connection_status', lambda *a, **kw: {'ready':True})
    monkeypatch.setattr(codex_desktop.subprocess, 'Popen', lambda *a, **kw: pytest.fail('must reuse'))
    assert codex_desktop.launch(root) == {'launched':False,'ready':True}


def test_missing_or_mismatched_launcher_configuration_fails_closed(prepared):
    root, *_ = prepared
    p=root/'codex-desktop-bridge/desktop-launch.json'
    value=json.loads(p.read_text('utf-8')); value['data_dir']=str(root/'wrong')
    p.write_text(json.dumps(value),'utf-8')
    with pytest.raises(BusinessError): codex_desktop.launch(root)


def test_prepare_does_not_overwrite_foreign_configuration(prepared):
    root, *_=prepared
    p=root/'codex-desktop-bridge/desktop-launch.json';p.write_text('{}','utf-8')
    with pytest.raises(BusinessError):codex_desktop.prepare(root)
    assert p.read_text('utf-8')=='{}'


def test_cancelled_send_does_not_launch(prepared, monkeypatch):
    event=threading.Event();event.set()
    monkeypatch.setattr(codex_desktop, 'launch', lambda *a: pytest.fail('cancelled'))
    with pytest.raises(BusinessError):codex_desktop.ensure_connection(prepared[0], event)


def test_configure_shared_mode_prepares_once_and_preserves_mode(tmp_path, monkeypatch):
    from management import codex_project
    core=Core(tmp_path/'core');calls=[]
    monkeypatch.setattr(codex_project,'ensure_project',lambda *a:copy.deepcopy(PROJECT))
    monkeypatch.setattr(codex_desktop,'prepare',lambda *a:calls.append(a))
    result=command(core,'configure_codex',{'ai':{**CONFIG,'execution_mode':'desktop_shared'}})
    assert result['result']['settings']['ai']['execution_mode']=='desktop_shared'
    assert calls == [(core.root, CONFIG['executable'])]


def test_invalid_execution_mode_cannot_connect(tmp_path, monkeypatch):
    from management import codex_project
    core=Core(tmp_path/'core')
    monkeypatch.setattr(codex_project,'ensure_project',lambda *a:pytest.fail('invalid mode'))
    with pytest.raises(BusinessError):command(core,'configure_codex',{'ai':{**CONFIG,'execution_mode':'unknown'}})


def test_current_codex_desktop_named_chatgpt_is_discovered(tmp_path, monkeypatch):
    desktop=tmp_path/'OpenAI.Codex_test'/'app'/'ChatGPT.exe'
    desktop.parent.mkdir(parents=True);desktop.write_bytes(b'synthetic')
    class Process: info={'exe':str(desktop),'name':'ChatGPT.exe'}
    monkeypatch.setattr(codex_desktop.psutil,'process_iter',lambda *a:[Process()])
    assert codex_desktop._desktop_paths()==[desktop]


def test_package_metadata_fallback_when_windowsapps_cannot_be_enumerated(tmp_path,monkeypatch):
    location=tmp_path/'OpenAI.Codex_test'; desktop=location/'app/ChatGPT.exe'
    desktop.parent.mkdir(parents=True);desktop.write_bytes(b'synthetic')
    monkeypatch.setattr(codex_desktop.psutil,'process_iter',lambda *a:[])
    monkeypatch.setenv('ProgramFiles',str(tmp_path/'empty'))
    class Result: stdout=str(location)+'\n';returncode=0
    monkeypatch.setattr(codex_desktop.subprocess,'run',lambda *a,**kw:Result())
    assert codex_desktop._desktop_paths()==[desktop]
