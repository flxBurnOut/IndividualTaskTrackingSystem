"""Project-level integrations must remain disabled in bounded internal reasoning."""
import subprocess
import threading
from pathlib import Path

import pytest

from management import ai


def write_config(directory, text):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'config.toml'
    path.write_text(text, encoding='utf-8')
    return path


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / 'codex-home'
    home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home))
    return home


def test_project_mcp_and_ancestor_profile_layers_are_all_isolated(tmp_path, isolated_home):
    project = tmp_path / 'project'
    workspace = project / 'nested'
    workspace.mkdir(parents=True)
    paths = [
        write_config(isolated_home, '[mcp_servers.global_server]\ncommand="never-run-global"\n'
            '[profiles.alternative.mcp_servers.profile_server]\ncommand="never-run-profile"\n'),
        write_config(project / '.codex', '[mcp_servers.ancestor_server]\ncommand="never-run-parent"\n'
            '[plugins."example@marketplace"]\nenabled=true\n[apps.calendar]\nenabled=true\n'),
        write_config(workspace / '.codex', '[mcp_servers.personal_management]\ncommand="never-run-personal"\n'
            '[profiles.alternative.mcp_servers.nested_profile]\ncommand="never-run-nested"\n'),
    ]
    before = {path: path.read_bytes() for path in paths}
    values = ai._isolation_overrides(workspace)
    for server in ('global_server', 'profile_server', 'ancestor_server', 'personal_management', 'nested_profile'):
        assert values[f'mcp_servers.{server}.enabled'] is False
    assert values['plugins.example@marketplace.enabled'] is False
    assert values['apps.calendar.enabled'] is False
    assert values['features.shell_tool'] is False
    assert values['project_doc_max_bytes'] == 0
    assert values['memories.use_memories'] is False
    assert all(path.read_bytes() == before[path] for path in paths)
    assert 'never-run' not in repr(values)


def test_unrelated_sibling_project_is_not_loaded(tmp_path, isolated_home):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    write_config(tmp_path / 'other' / '.codex', '[mcp_servers.unrelated]\ncommand="never-run"\n')
    assert 'mcp_servers.unrelated.enabled' not in ai._isolation_overrides(workspace)


@pytest.mark.parametrize('name', ['with.dot', 'with space', 'with=assignment', 'with"quote', ''])
def test_ambiguous_integration_names_refuse_to_start(tmp_path, isolated_home, name):
    import json
    write_config(tmp_path / 'project' / '.codex', '[mcp_servers.' + json.dumps(name) + ']\ncommand="never-run"\n')
    with pytest.raises(ai.AIError) as failure:
        ai._arguments('codex', tmp_path / 'project')
    assert failure.value.code == 'AI_CONFIG_UNSAFE'


@pytest.mark.parametrize('text', ['[mcp_servers.broken', 'mcp_servers = true\n',
    'profiles = false\n', '[profiles]\ninvalid = true\n', '[profiles.example]\napps = []\n'])
def test_malformed_project_configuration_refuses_to_start(tmp_path, isolated_home, text):
    write_config(tmp_path / 'project' / '.codex', text)
    with pytest.raises(ai.AIError) as failure:
        ai._isolation_overrides(tmp_path / 'project')
    assert failure.value.code == 'AI_CONFIG_UNSAFE'


def test_oversized_project_configuration_is_bounded(tmp_path, isolated_home):
    write_config(tmp_path / 'project' / '.codex', '#' + 'x' * 2_097_152)
    with pytest.raises(ai.AIError) as failure:
        ai._isolation_overrides(tmp_path / 'project')
    assert failure.value.code == 'AI_CONFIG_UNSAFE'


def test_cumulative_configuration_size_is_bounded(tmp_path, isolated_home):
    workspace = tmp_path / 'project'
    for _ in range(5):
        workspace /= 'nested'
        write_config(workspace / '.codex', '#' + 'x' * 2_000_000)
    with pytest.raises(ai.AIError) as failure:
        ai._isolation_overrides(workspace)
    assert failure.value.code == 'AI_CONFIG_UNSAFE'


def test_unreadable_project_configuration_refuses_to_start(tmp_path, isolated_home, monkeypatch):
    config = write_config(tmp_path / 'project' / '.codex', '[mcp_servers.example]\ncommand="never-run"\n')
    original = Path.open
    def denied(path, *args, **kwargs):
        if path == config:
            raise PermissionError('synthetic unreadable configuration')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', denied)
    with pytest.raises(ai.AIError) as failure:
        ai._isolation_overrides(tmp_path / 'project')
    assert failure.value.code == 'AI_CONFIG_UNSAFE'


def test_default_cwd_and_explicit_arguments_are_compatible(tmp_path, isolated_home, monkeypatch):
    workspace = tmp_path / 'project'
    write_config(workspace / '.codex', '[mcp_servers.personal_management]\ncommand="never-run"\n')
    monkeypatch.chdir(workspace)
    assert ai._isolation_overrides()['mcp_servers.personal_management.enabled'] is False
    assert 'mcp_servers.personal_management.enabled=false' in ai._arguments('codex', workspace)


def test_app_server_passes_actual_process_cwd_to_isolation(tmp_path, monkeypatch):
    inspected = []
    def arguments(executable, cwd=None):
        inspected.append((executable, cwd))
        return ['synthetic-codex', 'app-server']
    def stop_before_spawn(*args, **kwargs):
        assert kwargs['cwd'] == tmp_path
        raise RuntimeError('synthetic stop before process creation')
    monkeypatch.setattr(ai, '_arguments', arguments)
    monkeypatch.setattr(subprocess, 'Popen', stop_before_spawn)
    with pytest.raises(RuntimeError, match='synthetic stop'):
        ai._AppServer('synthetic-codex', tmp_path, threading.Event(), 10)
    assert inspected == [('synthetic-codex', tmp_path)]

def test_ignored_project_or_profile_has_inert_transport_without_copying_secrets(tmp_path, isolated_home):
    write_config(isolated_home, '[profiles.alternative.mcp_servers.profile_only]\ncommand="private/path/program"\nargs=["secret"]\n')
    write_config(tmp_path / 'project' / '.codex', '[mcp_servers.project_http]\nurl="https://private.invalid/secret-token"\n')
    values = ai._isolation_overrides(tmp_path / 'project')
    assert values['mcp_servers.profile_only.enabled'] is False
    assert values['mcp_servers.profile_only.command'] == '__personal_management_internal_tools_disabled__'
    assert values['mcp_servers.project_http.enabled'] is False
    assert values['mcp_servers.project_http.url'] == 'http://127.0.0.1:1/personal-management-disabled'
    assert 'secret' not in repr(values)
    assert 'private' not in repr(values)


def test_partial_project_override_retains_known_transport_kind(tmp_path, isolated_home):
    write_config(isolated_home, '[mcp_servers.shared]\ncommand="private/path/program"\n')
    write_config(tmp_path / 'project' / '.codex', '[mcp_servers.shared]\nenabled=true\n')
    values = ai._isolation_overrides(tmp_path / 'project')
    assert values['mcp_servers.shared.enabled'] is False
    assert values['mcp_servers.shared.command'] == '__personal_management_internal_tools_disabled__'


@pytest.mark.parametrize('definition', ['enabled = false', 'command = 123', 'command = ""',
    'command = "example"\nurl = "https://example.invalid"'])
def test_missing_or_ambiguous_transport_fails_closed(tmp_path, isolated_home, definition):
    write_config(tmp_path / 'project' / '.codex', '[mcp_servers.example]\n' + definition + '\n')
    with pytest.raises(ai.AIError) as failure:
        ai._isolation_overrides(tmp_path / 'project')
    assert failure.value.code == 'AI_CONFIG_UNSAFE'


def test_transport_conflict_across_config_layers_fails_closed(tmp_path, isolated_home):
    write_config(isolated_home, '[mcp_servers.shared]\ncommand="example"\n')
    write_config(tmp_path / 'project' / '.codex', '[mcp_servers.shared]\nurl="https://example.invalid"\n')
    with pytest.raises(ai.AIError) as failure:
        ai._isolation_overrides(tmp_path / 'project')
    assert failure.value.code == 'AI_CONFIG_UNSAFE'
