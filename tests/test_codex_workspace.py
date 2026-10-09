"""First-run workspace preparation must be portable and preserve user files."""
import json
from pathlib import Path
import sys
import tomllib

import pytest

from management import codex_workspace as workspace


def project(data):
    return Path(data) / workspace.WORKSPACE_NAME


def write_config(data, text):
    path = project(data) / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def snapshots(data):
    return {str(path.relative_to(project(data))): path.read_bytes() for path in project(data).rglob("*") if path.is_file()}


def test_fresh_data_creates_guidance_and_connection_without_business_database(tmp_path):
    data = tmp_path / "可移动数据 空间"
    result = workspace.prepare_workspace(data)
    assert result["workspace"] == str(project(data))
    assert result["changed"] is True
    assert len(result["changed_files"]) == 4
    assert not (data / "database.sqlite3").exists()
    assert 'Beta 测试版' in Path(result['readme_path']).read_text('utf-8')
    assert set(snapshots(data)) == {"AGENTS.md", "开始使用.md", str(Path(".codex") / "config.toml"),
                                   str(Path(".codex") / "management-instructions.md")}
    config = tomllib.loads(Path(result["config_path"]).read_text("utf-8"))["mcp_servers"]["personal_management"]
    assert Path(config["command"]).is_file()
    assert config["args"] == ["-m", "management", "--mcp", "--data-dir", str(data.resolve())]
    assert config["enabled"] is True
    assert config["startup_timeout_sec"] == 30
    assert config["tool_timeout_sec"] == 300
    assert (Path(config["env"]["PYTHONPATH"]) / "management" / "mcp_server.py").is_file()
    instructions = Path(result["agents_path"]).read_text("utf-8")
    assert "begin_context" in instructions
    assert "request_id、epoch、expected_revision" in instructions
    assert "重新保存 Codex 设置" in instructions
    assert "tools/prepare_codex_workspace.py" not in instructions
    assert "project_registered" not in result
    assert "instructions_loaded" not in result
    assert tomllib.loads(Path(result["config_path"]).read_text("utf-8"))["model_instructions_file"] == result["instructions_path"]
    from management.workspace_instructions import instructions as expected_instructions
    instruction_file = Path(result["instructions_path"]).read_text("utf-8")
    assert workspace._managed_document(instruction_file)
    assert instruction_file.split("\n", 1)[1] == expected_instructions()


def test_repeat_is_idempotent_and_does_not_rewrite_files(tmp_path):
    result = workspace.prepare_workspace(tmp_path)
    before = {path: (Path(path).read_bytes(), Path(path).stat().st_mtime_ns) for path in result["changed_files"]}
    repeated = workspace.prepare_workspace(tmp_path)
    assert repeated["changed"] is False
    assert repeated["changed_files"] == []
    assert {path: (Path(path).read_bytes(), Path(path).stat().st_mtime_ns) for path in before} == before


def test_frozen_runtime_uses_current_sibling_service(tmp_path, monkeypatch):
    installed = tmp_path / "portable app"
    installed.mkdir()
    ui = installed / "PersonalManagement.exe"
    service = installed / "PersonalManagementService.exe"
    service.write_bytes(b"test fixture")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(ui))
    data = tmp_path / "external data"
    result = workspace.prepare_workspace(data)
    connection = tomllib.loads(Path(result["config_path"]).read_text("utf-8"))["mcp_servers"]["personal_management"]
    assert connection["command"] == str(service)
    assert connection["args"] == ["--mcp", "--data-dir", str(data.resolve())]
    assert "env" not in connection


def test_frozen_missing_service_does_not_leave_partially_prepared_files(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "PersonalManagement.exe"))
    with pytest.raises(workspace.WorkspaceError, match="未找到.*业务服务"):
        workspace.prepare_workspace(tmp_path / "data")
    assert not project(tmp_path / "data").exists()


def test_pythonw_is_changed_to_console_python_if_available(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    python.write_bytes(b"fixture")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    connection = tomllib.loads(workspace.mcp_config(tmp_path))["mcp_servers"]["personal_management"]
    assert connection["command"] == str(python)


def test_update_after_move_and_upgrade_keeps_custom_sections_exactly(tmp_path, monkeypatch):
    data = tmp_path / "data"
    original = 'model = "chosen-model"\r\n# Keep this comment.\r\n[mcp_servers.custom]\r\ncommand = "custom-tool"\r\nargs = ["--read-only"]\r\n'
    path = write_config(data, original)
    first = workspace.prepare_workspace(data)
    assert original.encode("utf-8") in path.read_bytes()
    old_config = tomllib.loads(path.read_text("utf-8"))
    moved = tmp_path / "moved data"
    data.rename(moved)
    service = tmp_path / "new-release" / "PersonalManagementService.exe"
    service.parent.mkdir()
    service.write_bytes(b"fixture")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(service.with_name("PersonalManagement.exe")))
    result = workspace.prepare_workspace(moved)
    changed_config = Path(result["config_path"])
    assert original.encode("utf-8") in changed_config.read_bytes()
    parsed = tomllib.loads(changed_config.read_text("utf-8"))
    assert parsed["model"] == old_config["model"]
    assert parsed["mcp_servers"]["custom"] == old_config["mcp_servers"]["custom"]
    assert parsed["mcp_servers"]["personal_management"]["command"] == str(service)
    assert parsed["mcp_servers"]["personal_management"]["args"][-1] == str(moved)
    assert parsed["model_instructions_file"] == str(project(moved) / ".codex/management-instructions.md")
    assert result["changed_files"] == [str(changed_config)]


def test_custom_sections_added_after_managed_block_survive(tmp_path):
    result = workspace.prepare_workspace(tmp_path)
    path = Path(result["config_path"])
    additions = '\n[features]\nmy_feature = true\n\n[mcp_servers.other]\nurl = "http://127.0.0.1:4567/mcp"\n'
    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write(additions)
    before = path.read_bytes()
    assert workspace.prepare_workspace(tmp_path)["changed"] is False
    assert path.read_bytes() == before


def test_migrates_exact_previous_helper_guidance_and_config(tmp_path):
    data = tmp_path / "data"
    path = write_config(data, workspace._LEGACY_CONFIG_MARKER + '\n[mcp_servers.personal_management]\ncommand = "C:\\\\old-release\\\\PersonalManagementService.exe"\nargs = ["--mcp", "--data-dir", "C:\\\\old-data"]\nenabled = true\nstartup_timeout_sec = 30\ntool_timeout_sec = 300\n\n[mcp_servers.user_tool]\ncommand="other-tool"\n')
    old_template = (Path(__file__).parent / "fixtures/legacy_codex_workspace/AGENTS.md").read_text("utf-8")
    (project(data) / "AGENTS.md").write_text(old_template, encoding="utf-8")
    (project(data) / "开始使用.md").write_text(workspace._LEGACY_README, encoding="utf-8")
    result = workspace.prepare_workspace(data)
    assert result["changed"] is True
    assert len(result["changed_files"]) == 4
    current = path.read_text("utf-8")
    assert workspace._LEGACY_CONFIG_MARKER not in current
    assert tomllib.loads(current)["mcp_servers"]["user_tool"] == {"command": "other-tool"}
    assert "tools/prepare_codex_workspace.py" not in (project(data) / "AGENTS.md").read_text("utf-8")


def test_old_managed_document_version_can_be_upgraded(tmp_path):
    workspace.prepare_workspace(tmp_path)
    path = project(tmp_path) / "AGENTS.md"
    path.write_text(workspace._document("Previous managed instructions.\n"), encoding="utf-8", newline="")
    result = workspace.prepare_workspace(tmp_path)
    assert result["changed_files"] == [str(path)]
    assert "begin_context" in path.read_text("utf-8")


@pytest.mark.parametrize("change", ["guidance", "connection", "unmanaged_connection", "invalid_toml", "incomplete_marker"])
def test_conflicts_preserve_all_files_before_any_write(tmp_path, change):
    data = tmp_path / "data"
    result = workspace.prepare_workspace(data)
    agents = Path(result["agents_path"])
    config = Path(result["config_path"])
    if change == "guidance":
        agents.write_text(agents.read_text("utf-8") + "\nPersonal instructions.\n", encoding="utf-8")
    elif change == "connection":
        config.write_text(config.read_text("utf-8").replace("enabled = true", "enabled = false"), encoding="utf-8")
    elif change == "unmanaged_connection":
        config.write_text('[mcp_servers.personal_management]\ncommand="my-service"\n', encoding="utf-8")
    elif change == "invalid_toml":
        config.write_text("[mcp_servers.invalid\n", encoding="utf-8")
    else:
        config.write_text(config.read_text("utf-8").replace(workspace._CONFIG_END, "# end removed"), encoding="utf-8")
    before = snapshots(data)
    with pytest.raises(workspace.WorkspaceError):
        workspace.prepare_workspace(data)
    assert snapshots(data) == before


def test_existing_unmanaged_instructions_are_not_replaced(tmp_path):
    agents = project(tmp_path) / "AGENTS.md"
    agents.parent.mkdir()
    agents.write_text("My own instructions.\n", encoding="utf-8")
    with pytest.raises(workspace.WorkspaceError, match="AGENTS.md"):
        workspace.prepare_workspace(tmp_path)
    assert not (agents.parent / ".codex").exists()
    assert agents.read_text("utf-8") == "My own instructions.\n"


def test_user_readme_is_preserved_byte_for_byte(tmp_path):
    readme = project(tmp_path) / "开始使用.md"
    readme.parent.mkdir()
    content = b"\xef\xbb\xbfMy own notes.\r\n"
    readme.write_bytes(content)
    result = workspace.prepare_workspace(tmp_path)
    assert result["readme_preserved"] is True
    assert readme.read_bytes() == content
    assert str(readme) not in result["changed_files"]


def test_user_change_to_legacy_connection_is_not_discarded(tmp_path):
    path = write_config(tmp_path, workspace._LEGACY_CONFIG_MARKER + '\n[mcp_servers.personal_management]\ncommand="PersonalManagementService.exe"\nargs=["--mcp", "--data-dir", "old-data"]\nenabled=false\nstartup_timeout_sec=30\ntool_timeout_sec=300\n')
    before = path.read_bytes()
    with pytest.raises(workspace.WorkspaceError, match="旧版.*用户修改"):
        workspace.prepare_workspace(tmp_path)
    assert path.read_bytes() == before
    assert not (project(tmp_path) / "AGENTS.md").exists()


@pytest.mark.parametrize("content", [b"a" * (workspace.MAX_FILE_BYTES + 1), b"\xff\xfe\x00\xd8"], ids=["oversized", "binary"])
def test_limited_utf8_read_rejects_oversize_or_binary_configuration(tmp_path, content):
    path = write_config(tmp_path, "")
    path.write_bytes(content)
    with pytest.raises(workspace.WorkspaceError):
        workspace.prepare_workspace(tmp_path)
    assert path.read_bytes() == content
    assert not (project(tmp_path) / "AGENTS.md").exists()


def test_symlinked_workspace_is_rejected_before_writing(tmp_path, monkeypatch):
    target = project(tmp_path)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == target or original(self))
    with pytest.raises(workspace.WorkspaceError, match="链接路径"):
        workspace.prepare_workspace(tmp_path)
    assert not target.exists()


def test_atomic_write_keeps_original_when_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_bytes(b"original")
    def fail_replace(*args):
        raise OSError("simulated filesystem failure")
    monkeypatch.setattr(workspace.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        workspace._atomic_write(target, b"new configuration", b"original")
    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob(".pm-workspace-*.tmp")) == []


def test_atomic_write_rejects_newer_user_content(tmp_path):
    target = tmp_path / "config.toml"
    target.write_bytes(b"newer user edit")
    with pytest.raises(workspace.WorkspaceError, match="发生变化"):
        workspace._atomic_write(target, b"managed update", b"original")
    assert target.read_bytes() == b"newer user edit"
    assert list(tmp_path.glob(".pm-workspace-*.tmp")) == []


def test_does_not_read_business_database_or_runtime_token(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    database = data / "database.sqlite3"
    runtime = data / "runtime.json"
    database.write_bytes(b"opaque database fixture")
    runtime.write_text(json.dumps({"token": "secret-runtime-token"}), encoding="utf-8")
    before = {path: path.read_bytes() for path in (database, runtime)}
    original_open = Path.open
    def guarded_open(self, *args, **kwargs):
        if self in before:
            pytest.fail("Workspace setup must not inspect business data or discovery credentials")
        return original_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    workspace.prepare_workspace(data)
    monkeypatch.setattr(Path, "open", original_open)
    assert {path: path.read_bytes() for path in before} == before
    assert all(b"secret-runtime-token" not in content for content in snapshots(data).values())


def test_crlf_conversion_does_not_look_like_user_edit(tmp_path):
    result = workspace.prepare_workspace(tmp_path)
    for key in ("agents_path", "instructions_path", "config_path", "readme_path"):
        path = Path(result[key])
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert workspace.prepare_workspace(tmp_path)["changed"] is True


def test_same_name_inline_table_conflict_is_rejected_without_losing_other_settings(tmp_path):
    path = write_config(tmp_path, 'mcp_servers = { other = { command = "mine" } }\n')
    original = path.read_bytes()
    with pytest.raises(workspace.WorkspaceError):
        workspace.prepare_workspace(tmp_path)
    assert path.read_bytes() == original
    assert not (project(tmp_path) / "AGENTS.md").exists()


@pytest.mark.parametrize('placement', ['before_tables', 'after_tables', 'between_tables'])
def test_instructions_root_key_precedes_tables_independently_of_mcp_block(tmp_path, placement):
    managed = workspace._configuration('', workspace.mcp_config(tmp_path))
    custom = '[custom]\nkeep = "untouched"\nmodel_instructions_file = "nested-user-setting"\n'
    other = '[features]\nmy_feature = true\n'
    original = {'before_tables': managed + custom + other,
                'after_tables': custom + other + managed,
                'between_tables': custom + managed + other}[placement]
    path = write_config(tmp_path, original)
    result = workspace.prepare_workspace(tmp_path)
    text = path.read_text('utf-8')
    parsed = tomllib.loads(text)
    assert text.startswith(workspace._INSTRUCTIONS_PREFIX)
    assert original in text
    assert parsed['model_instructions_file'] == result['instructions_path']
    assert Path(parsed['model_instructions_file']).is_absolute()
    assert parsed['custom'] == {'keep': 'untouched', 'model_instructions_file': 'nested-user-setting'}
    assert parsed['features'] == {'my_feature': True}
    assert 'model_instructions_file' not in parsed['mcp_servers']['personal_management']


@pytest.mark.parametrize('same_path', [False, True])
def test_unowned_project_model_instructions_setting_and_file_are_preserved(tmp_path, same_path):
    custom_file = project(tmp_path) / '.codex/management-instructions.md' if same_path else tmp_path / 'personal-instructions.md'
    custom_file.parent.mkdir(parents=True, exist_ok=True)
    custom_file.write_bytes(b'User-owned instructions.\r\n')
    original = 'model_instructions_file = ' + json.dumps(str(custom_file), ensure_ascii=False) + '\n[custom]\nkeep=true\n'
    path = write_config(tmp_path, original)
    before = snapshots(tmp_path)
    with pytest.raises(workspace.WorkspaceError, match='model_instructions_file|management-instructions.md'):
        workspace.prepare_workspace(tmp_path)
    assert snapshots(tmp_path) == before
    assert path.read_text('utf-8') == original
    assert custom_file.read_bytes() == b'User-owned instructions.\r\n'


@pytest.mark.parametrize('damage', ['body', 'unmarked', 'empty', 'config_hash', 'config_marker', 'duplicate_marker', 'nonroot_block'])
def test_instruction_conflicts_preflight_all_files_before_any_write(tmp_path, damage):
    result = workspace.prepare_workspace(tmp_path)
    agents = Path(result['agents_path'])
    agents.write_text(workspace._document('Old intact managed guidance.\n'), encoding='utf-8')
    instructions = Path(result['instructions_path'])
    config = Path(result['config_path'])
    if damage == 'body':
        instructions.write_text(instructions.read_text('utf-8') + '\nPersonal addition.\n', encoding='utf-8')
    elif damage == 'unmarked':
        instructions.write_text('Personal instructions.\n', encoding='utf-8')
    elif damage == 'empty':
        instructions.write_bytes(b'')
    elif damage == 'config_hash':
        config.write_text(config.read_text('utf-8').replace('management-instructions.md', 'personal-instructions.md'), encoding='utf-8')
    elif damage == 'config_marker':
        config.write_text(config.read_text('utf-8').replace(workspace._INSTRUCTIONS_END, '# marker removed'), encoding='utf-8')
    elif damage == 'duplicate_marker':
        with config.open('a', encoding='utf-8') as stream:
            stream.write('\n' + workspace._INSTRUCTIONS_END + '\n')
    else:
        config.write_text('[custom]\nkeep=true\n' + config.read_text('utf-8'), encoding='utf-8')
    before = snapshots(tmp_path)
    with pytest.raises(workspace.WorkspaceError):
        workspace.prepare_workspace(tmp_path)
    assert snapshots(tmp_path) == before


def test_intact_previous_managed_model_instructions_upgrade_without_changing_user_sections(tmp_path):
    from management.workspace_instructions import instructions as expected_instructions
    result = workspace.prepare_workspace(tmp_path)
    path = Path(result['instructions_path'])
    path.write_text(workspace._document('Previous managed model instructions.\n'), encoding='utf-8')
    config = Path(result['config_path'])
    custom = '\n[custom]\nkeep = "same"\n'
    with config.open('a', encoding='utf-8') as stream:
        stream.write(custom)
    config_before = config.read_bytes()
    upgraded = workspace.prepare_workspace(tmp_path)
    assert upgraded['changed_files'] == [str(path)]
    assert path.read_text('utf-8').split('\n', 1)[1] == expected_instructions()
    assert config.read_bytes() == config_before
    assert workspace.prepare_workspace(tmp_path)['changed_files'] == []
