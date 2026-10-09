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
    assert '个人事务管理 · Beta 测试版' in version_summary(state)
    assert '未核验为 Beta' in version_summary(state)
    state['service_contract']['channel'] = 'beta'
    assert f'后台版本：{__version__}（Beta 测试版）' in version_summary(state)
    state['client_entrances'] = {'mcp': {'app_version': __version__, 'verified': True}}
    assert '其他已打开会话仍可能需要刷新' in version_summary(state)
    state['client_entrances']['mcp']['app_version'] = '1.0.2'
    assert '仍有旧接口请求' in version_summary(state)


def test_update_dialog_does_not_stop_backend_with_unsaved_editor(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QWidget, QDialog, QLabel
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
    assert 'Beta' in dialog.windowTitle()
    instructions = '\n'.join(label.text() for label in dialog.findChildren(QLabel))
    assert '尚未提供独立 Beta 安装包' in instructions
    assert '启动个人事务管理Beta.vbs' in instructions
    assert '请运行新版安装包' not in instructions
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


@pytest.mark.parametrize('theme,font_size',[('light',13),('dark',20)])
def test_version_dialog_wraps_long_paths_and_keeps_actions_reachable(tmp_path,theme,font_size):
    import os
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtWidgets import QApplication,QWidget,QLabel
    from management.gui_update import UpdateDialog
    from management.gui_theme import apply_appearance,current_appearance
    from management.gui_visual_profile import set_visual_style,visual_style
    app=QApplication.instance() or QApplication([]);previous=current_appearance();profile=visual_style()
    if app.platformName()=='offscreen' and os.name=='nt':
        for name in ('msyh.ttc','msyhbd.ttc'):
            font_path=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/name
            if font_path.is_file():QFontDatabase.addApplicationFont(str(font_path))
    parent=QWidget();parent.data_dir=tmp_path/('long_local_profile_name_'*4)
    calls=[];parent.bridge=SimpleNamespace(query=lambda name,done,failed:done({'service_contract':{'app_version':__version__,'channel':'beta'}}),prepare_update=lambda *_:calls.append('stop'))
    dialog=None
    try:
        set_visual_style('glass');apply_appearance(app,{'theme':theme,'font_size':font_size})
        dialog=UpdateDialog(parent);dialog.resize(560,610);dialog.show()
        for _ in range(5):app.processEvents()
        assert dialog.width()<=560 and dialog.height()<=610
        failures=[]
        for label in dialog.findChildren(QLabel):
            if label.isVisibleTo(dialog) and label.text().strip():
                needed=label.heightForWidth(label.width()) if label.wordWrap() else label.sizeHint().height()
                if label.height()+1<needed:failures.append((label.text(),label.height(),needed))
        assert not failures,failures
        assert dialog.location.document().size().height()<=dialog.location.viewport().height()+1
        block=dialog.location.document().begin()
        while block.isValid():
            text_layout=block.layout()
            for line in range(text_layout.lineCount()):
                assert text_layout.lineAt(line).naturalTextWidth()<=dialog.location.viewport().width()+1
            block=block.next()
        dialog.location.selectAll()
        assert dialog.location.textCursor().selectedText().replace('\u2029','\n')==dialog.location.text()
        for button in (dialog.prepare,dialog.refresh,dialog.cancel):
            assert not button.visibleRegion().isEmpty()
        report=os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR')
        if report:assert dialog.grab().save(str(Path(report)/f'text-version-{theme}.png'))
        dialog.content_scroll.verticalScrollBar().setValue(dialog.content_scroll.verticalScrollBar().maximum())
        app.processEvents()
        for button in (dialog.prepare,dialog.refresh,dialog.cancel):assert not button.visibleRegion().isEmpty()
        assert dialog.content_scroll.horizontalScrollBar().maximum()==0
        assert not calls
    finally:
        if dialog:dialog.close();dialog.deleteLater()
        parent.close();parent.deleteLater();set_visual_style(profile);apply_appearance(app,previous);app.processEvents()
