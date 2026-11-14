"""Client config fixtures only; never edit an installed Agent's settings."""
import asyncio
import json
from pathlib import Path
import tomllib

import pytest

from management.agent_connections import (apply_merge, configuration, launch_spec,
    preview_merge, server_name)
from management.codex_workspace import WorkspaceError
from management.data_space import CHANNEL, BetaIsolationError


def test_profiles_keep_absolute_beta_program_and_space_and_no_credentials(tmp_path):
    root = tmp_path / '独立 Beta 空间'
    for profile in ('antigravity', 'generic', 'codex'):
        text = configuration(root, profile)
        spec = launch_spec(root, profile)
        assert Path(spec['command']).is_absolute()
        assert spec['args'][-2:] == ['--mcp-client', profile]
        assert spec['args'][spec['args'].index('--data-dir') + 1] == str(root.resolve())
        assert spec['env']['PYTHONUTF8'] == '1'
        assert Path(spec['env']['PYTHONPATH']).resolve() == Path(__file__).resolve().parents[1] / 'src'
        parsed = tomllib.loads(text) if profile == 'codex' else json.loads(text)
        assert server_name(root) in parsed['mcp_servers' if profile == 'codex' else 'mcpServers']
        assert 'token' not in text.lower()
    assert not root.exists()
    assert server_name(root) != server_name(tmp_path / 'other-space')


def test_formal_space_rejected_without_creating_any_files(tmp_path):
    workspace = Path(__file__).resolve().parents[1]
    formal = workspace.parent / 'other-stable-data' if CHANNEL == 'beta' else workspace / '.beta-data' / 'foreign'
    with pytest.raises(BetaIsolationError):
        configuration(formal)
    assert not (tmp_path / 'mcp_config.json').exists()


def test_merge_preserves_formal_and_other_settings_backups_and_is_idempotent(tmp_path):
    root = tmp_path / 'beta-space'
    path = tmp_path / 'agent' / 'mcp_config.json'
    path.parent.mkdir()
    document = {'mcpServers': {'personal_management': {'command': 'stable.exe', 'args': ['original-data']},
                              'other': {'serverUrl': 'https://synthetic.invalid/mcp', 'headers': {'Authorization': 'synthetic'}}},
                'theme': 'unchanged'}
    original = json.dumps(document, ensure_ascii=False).encode()
    path.write_bytes(original)
    plan = preview_merge(root, 'antigravity', path)
    assert path.read_bytes() == original and not root.exists()
    result = apply_merge(plan)
    after = json.loads(path.read_bytes())
    for key, value in document['mcpServers'].items():
        assert after['mcpServers'][key] == value
    assert after['theme'] == document['theme']
    assert len(after['mcpServers']) == 3
    assert Path(result['backup']).read_bytes() == original
    before = path.stat().st_mtime_ns
    assert apply_merge(preview_merge(root, 'antigravity', path))['changed'] is False
    assert path.stat().st_mtime_ns == before


@pytest.mark.parametrize('text', ['{bad', '[]', '{"mcpServers": []}',
    '{"mcpServers": {}, "mcpServers": {}}', '{// comment\n}', '{"x": NaN}'])
def test_unrecognized_config_is_preserved(tmp_path, text):
    path = tmp_path / 'mcp_config.json'
    path.write_text(text, encoding='utf-8')
    with pytest.raises(WorkspaceError):
        preview_merge(tmp_path / 'beta', 'antigravity', path)
    assert path.read_text('utf-8') == text


def test_changed_connection_and_preview_conflicts_are_not_overwritten(tmp_path):
    path = tmp_path / 'mcp_config.json'
    root = tmp_path / 'beta'
    path.write_text(configuration(root), encoding='utf-8')
    doc = json.loads(path.read_bytes())
    doc['mcpServers'][server_name(root)]['command'] = 'user-selected.exe'
    path.write_text(json.dumps(doc), encoding='utf-8')
    with pytest.raises(WorkspaceError, match='同名'):
        preview_merge(root, 'antigravity', path)
    path.write_text('{}', encoding='utf-8')
    plan = preview_merge(root, 'antigravity', path)
    path.write_text('{"newUserSetting":true}', encoding='utf-8')
    with pytest.raises(WorkspaceError, match='预览后'):
        apply_merge(plan)
    assert json.loads(path.read_bytes()) == {'newUserSetting': True}
    assert not path.with_name(path.name + '.personal-management-' + CHANNEL + '.bak').exists()


def test_failed_atomic_replace_retains_original(tmp_path, monkeypatch):
    import management.codex_workspace as workspace
    path = tmp_path / 'mcp_config.json'
    path.write_bytes(b'{}')
    plan = preview_merge(tmp_path / 'beta', 'generic', path)
    def fail(*args):
        raise OSError('synthetic replace failure')
    monkeypatch.setattr(workspace.os, 'replace', fail)
    with pytest.raises(OSError):
        apply_merge(plan)
    assert path.read_bytes() == b'{}'
    assert not list(tmp_path.glob('.pm-workspace-*'))


def test_config_target_rejects_data_files_and_hardlinks(tmp_path):
    for name in ('runtime.json', 'database.sqlite3', 'config.toml'):
        with pytest.raises(WorkspaceError):
            preview_merge(tmp_path / 'beta', 'antigravity', tmp_path / name)
    source = tmp_path / 'original.json'
    source.write_text('{}', encoding='utf-8')
    alias = tmp_path / 'mcp_config.json'
    alias.hardlink_to(source)
    with pytest.raises(WorkspaceError):
        preview_merge(tmp_path / 'beta', 'antigravity', alias)
    assert source.read_text('utf-8') == '{}'



def test_concurrent_config_merges_cannot_drop_another_connection(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    path = tmp_path / 'mcp_config.json'
    path.write_text('{"mcpServers":{"formal":{"command":"keep.exe"}}}', encoding='utf-8')
    plans = [preview_merge(tmp_path / ('beta-' + str(i)), 'antigravity', path) for i in range(2)]
    barrier = threading.Barrier(2)
    def apply(plan):
        barrier.wait(timeout=5)
        try:
            return apply_merge(plan)
        except WorkspaceError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, plans))
    assert sum(result is not None for result in results) == 1
    servers = json.loads(path.read_text('utf-8'))['mcpServers']
    assert len(servers) == 2 and servers['formal'] == {'command': 'keep.exe'}
    # A new preview incorporates the other writer's result instead of losing it.
    missed = next(plan for plan, result in zip(plans, results) if result is None)
    data = next(tmp_path / ('beta-' + str(i)) for i in range(2) if server_name(tmp_path / ('beta-' + str(i))) == missed.server)
    apply_merge(preview_merge(data, 'antigravity', path))
    assert len(json.loads(path.read_text('utf-8'))['mcpServers']) == 3



def test_configuration_never_reads_runtime_credentials(tmp_path, monkeypatch):
    root = tmp_path / 'space'
    root.mkdir()
    runtime = root / 'runtime.json'
    runtime.write_text('{"token":"synthetic-private-value"}', encoding='utf-8')
    original = Path.open
    def guarded(path, *args, **kwargs):
        if path.resolve() == runtime.resolve():
            pytest.fail('Config generation must not read runtime credentials')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded)
    assert 'synthetic-private-value' not in configuration(root)
    assert 'personal_management_' + CHANNEL + '_' in configuration(root)


def test_frozen_agent_configuration_uses_service_executable_without_source_environment(tmp_path, monkeypatch):
    import sys
    gui = tmp_path / 'program' / 'PersonalManagement.exe'
    service = gui.with_name('PersonalManagementService.exe')
    gui.parent.mkdir()
    gui.write_bytes(b'synthetic-gui')
    service.write_bytes(b'synthetic-service')
    monkeypatch.setattr(sys, 'executable', str(gui))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    spec = launch_spec(tmp_path / 'data', 'antigravity')
    assert spec['command'] == str(service)
    assert spec['args'][0] == '--mcp' and '-m' not in spec['args']
    assert 'PYTHONPATH' not in spec['env']



def test_cli_configuration_does_not_read_discovery_credentials(tmp_path, monkeypatch, capsys):
    import sys
    from management.__main__ import main
    root = tmp_path / 'selected-data'
    root.mkdir()
    runtime = root / 'runtime.json'
    runtime.write_text('{"token":"synthetic-secret"}', encoding='utf-8')
    original = Path.open
    def guarded(path, *args, **kwargs):
        if path.resolve() == runtime.resolve():
            pytest.fail('CLI config export must not read discovery credentials')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded)
    monkeypatch.setattr(sys, 'argv', ['management', '--data-dir', str(root), '--mcp-config', 'antigravity'])
    assert main() == 0
    rendered = capsys.readouterr().out
    assert json.loads(rendered)['mcpServers'] and 'synthetic-secret' not in rendered

@pytest.mark.parametrize("mode,ok", [("--mcp-config", True), ("--check-mcp", True), ("--check-mcp", False)])
def test_cli_json_uses_utf8_even_when_windows_stdout_is_locale_encoded(tmp_path, monkeypatch, mode, ok):
    import io
    import sys
    from management import agent_connections
    from management.__main__ import main
    root = tmp_path / '中文资料空间'
    async def probe(*args, **kwargs):
        if not ok:
            raise WorkspaceError('中文路径读取失败')
        return {'ok': True, 'data_dir': str(root)}
    monkeypatch.setattr(agent_connections, 'probe_connection', probe)
    monkeypatch.setattr(sys, 'argv', ['management', '--data-dir', str(root), mode, 'antigravity'])
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding='cp936')
    with monkeypatch.context() as output_patch:
        output_patch.setattr(sys, 'stdout', stream)
        code = main()
        stream.flush()
        raw = buffer.getvalue()
    stream.close()
    assert code == (0 if ok else 1)
    rendered = json.loads(raw.decode('utf-8'))
    if mode == '--mcp-config':
        entry = next(iter(rendered['mcpServers'].values()))
        assert str(root) in entry['args']
    elif ok:
        assert rendered['data_dir'] == str(root)
    else:
        assert rendered == {'ok': False, 'message': '中文路径读取失败'}
