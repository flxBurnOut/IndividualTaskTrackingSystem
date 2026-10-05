"""Only synthetic files and a replaceable config RPC; no user config/model I/O."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tomllib

import pytest

from management.discussion_workspace import prepare_discussion_workspace, SERVER, DOC_BUDGET
from management.schemas import BusinessError


def merge(left, right):
    result = copy.deepcopy(left)
    for key, value in right.items():
        result[key] = merge(result.get(key, {}), value) if isinstance(value, dict) else copy.deepcopy(value)
    return result


class ConfigRPC:
    def __init__(self, tmp_path, *, trusted=False):
        self.file = tmp_path / 'synthetic-user-config.toml'
        self.file.write_text('model="user-model"\n', encoding='utf-8')
        self.trusted = trusted
        self.calls = []
        self.projects = {}
        self.user = {'model': 'user-model', 'model_provider': 'user-provider',
            'mcp_servers': {'personal_management': {'command': 'old-business', 'enabled': True},
                            'external_http': {'url': 'https://example.invalid/private', 'enabled': True}},
            'plugins': {'user.plugin': {'enabled': True}}, 'apps': {'calendar': {'enabled': True}}}
        self.profile = {'model': 'selected-profile-model',
            'mcp_servers': {'profile_mcp': {'command': 'private-profile-command', 'enabled': True}}}
        self.fail_verify = False
        self.keep_disabled = False
        self.effective_override = {}
        self.session_overrides = {}

    def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'config/value/write':
            assert params['filePath'] == str(self.file) and params['expectedVersion'] == 'version-1'
            assert params['value'] == 'trusted' and params['mergeStrategy'] == 'replace'
            self.trusted = True
            return {'status': 'ok'}
        assert method == 'config/read'
        cwd = Path(params['cwd'])
        user = {**copy.deepcopy(self.user), 'projects': copy.deepcopy(self.projects)}
        config = merge(user, self.profile)
        layers = [{'name': {'type': 'user', 'file': str(self.file)}, 'version': 'version-1', 'config': user},
                  {'name': {'type': 'user', 'file': str(self.file.with_name('selected.config.toml')), 'profile': 'selected'},
                   'version': 'profile-1', 'config': copy.deepcopy(self.profile)}]
        path = cwd / '.codex' / 'config.toml'
        if path.exists():
            child = tomllib.loads(path.read_text(encoding='utf-8'))
            active = self.trusted and not self.keep_disabled
            layers.insert(0, {'name': {'type': 'project', 'dotCodexFolder': str(path.parent)},
                              'config': child, 'disabledReason': None if active else 'synthetic untrusted child'})
            if active:
                config = merge(config, child)
        if self.fail_verify and path.exists():
            config['mcp_servers']['external_http']['enabled'] = True
        if path.exists():
            config = merge(config, self.effective_override)
        if self.session_overrides:
            config = merge(config, self.session_overrides)
            layers.insert(0, {'name': {'type': 'sessionFlags'}, 'config': self.session_overrides})
        return {'config': config, 'layers': layers}


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path / 'data' / 'Codex事务助手'
    root.mkdir(parents=True)
    runtime = {'command': str(Path(sys.executable).resolve()),
               'args': ['-m', 'management', '--mcp', '--data-dir', str(root.parent)],
               'env': {'PYTHONPATH': 'synthetic-runtime-path'}}
    return root, runtime, ConfigRPC(tmp_path)


def prepare(inputs, **overrides):
    root, runtime, rpc = inputs
    return prepare_discussion_workspace(**{'workspace_root': root, 'conversation_id': 'conversation-one',
        'epoch': 'epoch-one', 'runtime': runtime, 'rpc': rpc, **overrides})


def test_child_owns_only_discussion_mcp_and_only_exact_child_trust_is_written(inputs):
    root, runtime, rpc = inputs
    before = rpc.file.read_bytes()
    result = prepare(inputs)
    config = tomllib.loads(Path(result['config_path']).read_text(encoding='utf-8'))
    assert result['root_workspace'] == str(root) and result['verified']
    assert result['cwd'] == str(root / '.discussions' / 'conversation-one')
    assert 'model' not in config and 'profile' not in config and 'profiles' not in config
    assert config['sandbox_mode'] == 'read-only' and config['approval_policy'] == 'never'
    assert config['project_doc_max_bytes'] == DOC_BUDGET
    assert {name for name, entry in config['mcp_servers'].items() if entry['enabled']} == {SERVER}
    assert config['mcp_servers']['personal_management']['command'].startswith('__personal_management')
    assert config['mcp_servers']['external_http']['url'].startswith('http://127.0.0.1:1/')
    assert config['mcp_servers']['profile_mcp']['enabled'] is False
    contents = Path(result['config_path']).read_text(encoding='utf-8')
    assert 'private-profile-command' not in contents and 'https://example.invalid/private' not in contents
    assert config['plugins']['user.plugin']['enabled'] is False
    assert config['apps']['calendar']['enabled'] is False
    target = config['mcp_servers'][SERVER]
    assert target['command'] == runtime['command']
    assert target['args'][-4:] == ['--mcp-discussion', 'conversation-one', '--discussion-epoch', 'epoch-one']
    assert '--mcp' not in target['args']
    writes = [params for method, params in rpc.calls if method == 'config/value/write']
    assert len(writes) == 1
    assert writes[0]['keyPath'] == 'projects.' + json.dumps(result['cwd'], ensure_ascii=False) + '.trust_level'
    assert rpc.file.read_bytes() == before  # Mock RPC never mutates even synthetic user configuration.
    assert not any(method.startswith(('thread/', 'project/')) for method, _ in rpc.calls)
    agents = Path(result['agents_path']).read_text(encoding='utf-8')
    assert 'Beta 测试版' in agents and '独立 Beta 数据空间' in agents
    assert '覆盖父工作区' in agents and 'submit_candidate' in agents and '等待用户在管理软件核对确认' in agents


def test_idempotent_prepare_keeps_profile_model_and_avoids_repeat_trust_write(inputs):
    first = prepare(inputs)
    second = prepare(inputs)
    assert first['changed'] and not second['changed'] and not second['trust_changed']
    _, _, rpc = inputs
    value = rpc.request('config/read', {'cwd': first['cwd'], 'includeLayers': True})
    assert value['config']['model'] == 'selected-profile-model'
    assert sum(method == 'config/value/write' for method, _ in rpc.calls) == 1


@pytest.mark.parametrize('target', ['child', 'root', 'ancestor'])
def test_explicit_untrusted_is_preserved_without_config_or_trust_writes(inputs, target):
    root, _, rpc = inputs
    denied = root / '.discussions' / 'conversation-one' if target == 'child' else root if target == 'root' else root.parent
    rpc.projects[str(denied)] = {'trust_level': 'untrusted'}
    with pytest.raises(BusinessError) as error:
        prepare(inputs)
    assert error.value.code == 'discussion_workspace_untrusted'
    assert not (root / '.discussions' / 'conversation-one' / '.codex' / 'config.toml').exists()
    assert not any(method == 'config/value/write' for method, _ in rpc.calls)


@pytest.mark.parametrize('field', ['config_path', 'agents_path'])
def test_user_changes_block_entire_preflight_before_rpc_or_other_file_write(inputs, field):
    original = prepare(inputs)
    path = Path(original[field])
    path.write_text(path.read_text(encoding='utf-8') + '\nUser changes\n', encoding='utf-8')
    other = Path(original['agents_path' if field == 'config_path' else 'config_path'])
    unchanged = other.read_bytes()
    inputs[2].calls.clear()
    with pytest.raises(BusinessError) as error:
        prepare(inputs, epoch='epoch-two')
    assert error.value.code == 'discussion_workspace_ownership_conflict'
    assert path.read_text(encoding='utf-8').endswith('User changes\n')
    assert other.read_bytes() == unchanged and not inputs[2].calls


def test_move_and_runtime_upgrade_updates_only_owned_child_files(inputs, tmp_path):
    first = prepare(inputs)
    root, runtime, _ = inputs
    moved = tmp_path / 'moved-data' / 'Codex事务助手'
    shutil.copytree(root, moved)
    (moved / 'user-note.md').write_text('keep me', encoding='utf-8')
    executable = tmp_path / 'new-service.exe'
    executable.write_bytes(b'synthetic-not-executed')
    fresh = {**runtime, 'command': str(executable), 'args': ['--mcp', '--data-dir', str(moved.parent)]}
    rpc = ConfigRPC(tmp_path)
    result = prepare((moved, fresh, rpc), epoch='epoch-two')
    config = tomllib.loads(Path(result['config_path']).read_text(encoding='utf-8'))
    target = config['mcp_servers'][SERVER]
    assert result['changed'] and target['command'] == str(executable)
    assert str(moved.parent) in target['args'] and str(root.parent) not in target['args']
    assert target['args'][-1] == 'epoch-two'
    assert (moved / 'user-note.md').read_text(encoding='utf-8') == 'keep me'
    assert Path(first['agents_path']).read_bytes() == Path(result['agents_path']).read_bytes()


@pytest.mark.parametrize('problem', ['still_disabled', 'external_enabled'])
def test_incomplete_effective_config_never_reports_verified(inputs, problem):
    inputs[2].keep_disabled = problem == 'still_disabled'
    inputs[2].fail_verify = problem == 'external_enabled'
    with pytest.raises(BusinessError) as error:
        prepare(inputs)
    assert error.value.code in {'discussion_workspace_config_disabled', 'discussion_workspace_isolation_failed'}


def test_inert_business_transport_is_present_even_without_parent_layer(inputs):
    inputs[2].user['mcp_servers'].pop('personal_management')
    result = prepare(inputs)
    config = tomllib.loads(Path(result['config_path']).read_text(encoding='utf-8'))
    assert config['mcp_servers']['personal_management']['enabled'] is False
    assert config['mcp_servers']['personal_management']['command']


@pytest.mark.parametrize('override', [
    {'sandbox_mode': 'workspace-write'}, {'approval_policy': 'on-request'},
    {'project_doc_max_bytes': 0}, {'features': {'shell_tool': True}},
    {'plugins': {'user.plugin': {'enabled': True}}}, {'apps': {'_default': {'enabled': True}}},
])
def test_effective_permission_doc_budget_and_tool_overrides_fail_closed(inputs, override):
    inputs[2].effective_override = override
    with pytest.raises(BusinessError) as error:
        prepare(inputs)
    assert error.value.code == 'discussion_workspace_isolation_failed'


def test_preexisting_unmanaged_files_are_preserved_before_any_rpc(inputs):
    root, _, rpc = inputs
    child = root / '.discussions' / 'conversation-one'
    child.mkdir(parents=True)
    agents = child / 'AGENTS.md'
    agents.write_text('User-authored instructions', encoding='utf-8')
    with pytest.raises(BusinessError) as error:
        prepare(inputs)
    assert error.value.code == 'discussion_workspace_ownership_conflict'
    assert agents.read_text(encoding='utf-8') == 'User-authored instructions'
    assert not rpc.calls


@pytest.mark.parametrize('flags', [{'project_doc_max_bytes': 0},
                                  {'sandbox_mode': 'read-only', 'approval_policy': 'never'}])
def test_temporary_cli_isolation_cannot_verify_normal_desktop_config(inputs, flags):
    root, _, rpc = inputs
    rpc.session_overrides = flags
    with pytest.raises(BusinessError) as error:
        prepare(inputs)
    assert error.value.code == 'discussion_workspace_temporary_overrides'
    assert not (root / '.discussions' / 'conversation-one' / '.codex' / 'config.toml').exists()
    assert not any(method == 'config/value/write' for method, _ in rpc.calls)


@pytest.mark.parametrize('identifier', ['../escape', 'a/b', 'a\\b', 'CON', 'operation:uuid'])
def test_invalid_directory_identifiers_are_rejected_before_rpc(inputs, identifier):
    with pytest.raises(BusinessError):
        prepare(inputs, conversation_id=identifier)
    assert not inputs[2].calls


def test_unrelated_mcp_transport_conflict_is_not_silently_overridden(inputs):
    inputs[2].profile['mcp_servers']['external_http'] = {'command': 'conflicting-command'}
    with pytest.raises(BusinessError) as error:
        prepare(inputs)
    assert error.value.code == 'discussion_workspace_integration_conflict'
    assert not any(method == 'config/value/write' for method, _ in inputs[2].calls)
