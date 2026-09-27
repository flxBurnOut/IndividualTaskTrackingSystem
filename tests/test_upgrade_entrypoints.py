import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from management import __version__
from management import installation_state as selection
from management.runtime import OwnerLock


def test_registered_installer_selection_only_applies_to_its_executable(tmp_path, monkeypatch):
    application = tmp_path / 'app'
    custom = tmp_path / '个人资料'
    monkeypatch.setattr(selection, '_registered_location', lambda: {
        'InstallLocation': str(application), 'DataDirectory': str(custom)})
    assert selection.installed_data_dir(application / 'PersonalManagement.exe') == custom
    assert selection.installed_data_dir(tmp_path / 'portable' / 'PersonalManagement.exe') is None


def test_explicit_launch_resumes_only_after_old_service_releases_owner(tmp_path, monkeypatch):
    marker = tmp_path / 'update_pending.json'
    marker.write_text(json.dumps({'format': 'personal-management-update/1'}), encoding='utf-8')
    old = OwnerLock(tmp_path / 'service.lock')
    assert old.acquire()
    try:
        with pytest.raises(ValueError, match='仍在退出'):
            selection.resume_after_update(tmp_path)
        assert marker.exists()
    finally:
        old.release()
    def selected_start(root, *, resume_token):
        assert json.loads(marker.read_text('utf-8'))['resume_token'] == resume_token
        new = OwnerLock(root / 'service.lock')
        assert new.acquire()
        try:
            marker.unlink()
        finally:
            new.release()
    monkeypatch.setattr('management.runtime.start_service', selected_start)
    selection.resume_after_update(tmp_path)
    assert not marker.exists()


def test_unknown_update_marker_is_never_removed(tmp_path):
    marker = tmp_path / 'update_pending.json'
    content = '{"format":"future-format"}'
    marker.write_text(content, encoding='utf-8')
    with pytest.raises(ValueError):
        selection.resume_after_update(tmp_path)
    assert marker.read_text('utf-8') == content


def test_version_summary_distinguishes_mcp_observation_from_configuration():
    from management.gui_update import version_summary
    state = {'service_contract': {'app_version': __version__}}
    assert '尚未核验' in version_summary(state)
    state['client_entrances'] = {'mcp': {'app_version': __version__, 'verified': True}}
    assert '其他已打开会话仍可能需要刷新' in version_summary(state)
    state['client_entrances']['mcp']['app_version'] = '1.0.2'
    assert '仍有旧接口请求' in version_summary(state)


def test_update_dialog_does_not_stop_backend_with_unsaved_editor(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QWidget, QDialog
    from management.gui_update import UpdateDialog
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    calls = []
    parent.data_dir = tmp_path
    parent.review_pending = False
    parent.bridge = SimpleNamespace(callbacks={}, uncertain_writes={},
        query=lambda name, done, failed: done({'service_contract': {'app_version': __version__}}),
        prepare_update=lambda *args: calls.append('stop'))
    dialog = UpdateDialog(parent)
    editor = QDialog(parent)
    editor.show()
    try:
        app.processEvents()
        dialog.prepare_update()
        assert calls == []
        assert '保存并关闭' in dialog.status.text()
        editor.close()
        parent.bridge.uncertain_writes = {'pending': {}}
        dialog.prepare_update()
        assert calls == []
        assert '回执' in dialog.status.text()
    finally:
        editor.close()
        dialog.close()
        parent.close()
