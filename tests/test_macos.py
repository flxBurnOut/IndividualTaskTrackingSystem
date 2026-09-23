"""Portability contracts plus real Mac font and venv integration checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tomllib
import uuid

import pytest

from management import runtime


@pytest.fixture
def mac_home(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('PERSONAL_MANAGEMENT_DATA', raising=False)
    return tmp_path


def test_mac_data_default_legacy_and_override(mac_home, monkeypatch):
    expected = mac_home / 'Library/Application Support/PersonalManagement/data'
    assert runtime.default_data_dir() == expected
    legacy = mac_home / 'PersonalManagement/data'
    legacy.mkdir(parents=True)
    assert runtime.default_data_dir() == expected
    (legacy / 'database.sqlite3').touch()
    assert runtime.default_data_dir() == legacy
    selected = mac_home / '中文 空间'
    monkeypatch.setenv('PERSONAL_MANAGEMENT_DATA', str(selected))
    assert runtime.default_data_dir() == selected


def test_source_mcp_keeps_venv_python(tmp_path, monkeypatch):
    from management.gui_workflows import codex_mcp_config
    executable = tmp_path / 'venv with spaces/bin/python'
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    monkeypatch.setattr(sys, 'executable', str(executable))
    config = tomllib.loads(codex_mcp_config(tmp_path))['mcp_servers']['personal_management']
    assert config['command'] == str(executable)


def test_generated_source_mcp_command_can_import_dependencies(tmp_path):
    from management.gui_workflows import codex_mcp_config
    config = tomllib.loads(codex_mcp_config(tmp_path))['mcp_servers']['personal_management']
    result = subprocess.run([config['command'], '-c', 'import mcp, PySide6, management'],
                            env={**os.environ, **config['env']}, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_mac_bundle_helper_shared_by_bootstrap_and_mcp(tmp_path, monkeypatch):
    from management.gui_workflows import codex_mcp_config
    directory = tmp_path / '中文 App.app/Contents/MacOS'
    directory.mkdir(parents=True)
    helper = directory / 'PersonalManagementService'
    helper.touch()
    monkeypatch.setattr(sys, 'executable', str(directory / 'PersonalManagement'))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert runtime._service_args(tmp_path) == [str(helper), '--service', '--data-dir', str(tmp_path)]
    config = tomllib.loads(codex_mcp_config(tmp_path))['mcp_servers']['personal_management']
    assert config['command'] == str(helper) and 'env' not in config
    helper.unlink()
    with pytest.raises(ValueError, match='业务服务'):
        runtime.service_executable()


def test_finder_codex_discovery_without_terminal_path(mac_home, monkeypatch):
    from management import ai
    monkeypatch.setattr(ai.shutil, 'which', lambda _: None)
    candidate = Path('/opt/homebrew/bin/codex')
    monkeypatch.setattr(Path, 'is_file', lambda path: path == candidate)
    monkeypatch.setattr(os, 'access', lambda path, mode: path == candidate)
    assert ai.find_codex() == str(candidate)


@pytest.mark.skipif(sys.platform != 'darwin', reason='Requires macOS installed Chinese fonts')
def test_chinese_pdf_embeds_font_and_roundtrips_text(tmp_path):
    from management.document_worker import produce
    from management.resources import ResourceManager
    from pypdf import PdfReader
    manager = ResourceManager(tmp_path / '中文 数据')
    title, content = '中文导出验证', '课程计划与任务复盘。计算 2 + 3。'
    result = produce(manager, str(uuid.uuid4()), {'kind':'pdf', 'relative_path':'中文 文件.pdf',
                     'title': title, 'content': content}, threading.Event())
    reader = PdfReader(manager._blob(result['sha256']))
    extracted = ''.join(page.extract_text() for page in reader.pages)
    assert title in extracted and content in extracted
    fonts = reader.pages[0]['/Resources']['/Font'].get_object()
    assert any('/FontFile2' in item.get_object()['/FontDescriptor'].get_object()
               for item in fonts.values() if '/FontDescriptor' in item.get_object())


def test_unavailable_pdf_glyph_reports_error(monkeypatch):
    from management.document_fonts import pdf_font
    from management.resources import ResourceError
    monkeypatch.setattr(Path, 'is_file', lambda path: False)
    with pytest.raises(ResourceError) as error:
        pdf_font('中文')
    assert error.value.code == 'FONT_REQUIRED'


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS root filesystem aliases')
@pytest.mark.parametrize('alias', ['/tmp', '/var/tmp'])
def test_system_temp_alias_allowed_but_nested_symlinks_rejected(alias):
    import tempfile
    from management.resources import _plain_path, ResourceError
    with tempfile.TemporaryDirectory(dir=alias) as directory:
        root = Path(directory)
        source = root / '中文.txt'
        source.write_text('合成资料', encoding='utf-8')
        assert _plain_path(source, file=True) == source.resolve()
        (root / 'link.txt').symlink_to(source)
        with pytest.raises(ResourceError, match='符号链接'):
            _plain_path(root / 'link.txt', file=True)
        (root / 'nested').symlink_to(root, target_is_directory=True)
        with pytest.raises(ResourceError, match='符号链接'):
            _plain_path(root / 'nested' / '中文.txt', file=True)


@pytest.mark.parametrize('version,accepted', [((3, 50, 4), False), ((3, 44, 5), False),
    ((3, 51, 2), False), ((3, 50, 7), True), ((3, 44, 6), True), ((3, 51, 3), True), ((3, 53, 1), True)])
def test_runtime_preflight_keeps_original_wal_requirement(monkeypatch, version, accepted):
    from management import runtime_check
    from management.schemas import BusinessError
    monkeypatch.setattr(runtime_check.sqlite3, 'sqlite_version_info', version)
    if accepted:
        runtime_check.check_sqlite()
    else:
        with pytest.raises(BusinessError, match='WAL'):
            runtime_check.check_sqlite()
