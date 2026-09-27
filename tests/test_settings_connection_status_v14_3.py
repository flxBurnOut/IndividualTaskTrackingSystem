"""The settings footer reports the live desktop connection at scroll position zero."""
import copy
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QWidget

from management.gui_codex_connection import CodexConnectionController
from management.gui_theme import apply_appearance, current_appearance
from management.gui_workflows import SettingsDialog
from test_gui_codex_connection_v14_2 import app, stop, wait
from test_ux_workflows_v2 import ControlledBridge


SETTINGS = {'ai': {'enabled': True, 'execution_mode': 'desktop_shared', 'executable': ''}}
PROJECT = {'status': 'ready', 'mcp_verified': True, 'name': 'Synthetic assistant',
           'workspace': 'synthetic-workspace'}


@pytest.fixture
def connected_settings(app, tmp_path):
    previous_appearance = current_appearance()
    bridge, parent, calls = ControlledBridge(), QWidget(), []
    connection = parent.codex_connection = CodexConnectionController(
        tmp_path, bridge=bridge)
    dialog = SettingsDialog(bridge, {'types': []}, tmp_path, parent)
    bridge.deliver('settings', {'settings': copy.deepcopy(SETTINGS)})
    dialog.tabs.setCurrentIndex(dialog._provider_tab)
    dialog.show()
    bridge.deliver('codex_connection', {'state': 'desktop_closed', 'ready': False})
    try:
        yield dialog, bridge, connection, calls
    finally:
        dialog.close()
        stop(app, connection)
        dialog.deleteLater()
        parent.deleteLater()
        apply_appearance(app, previous_appearance)
        app.processEvents()


def finish_save(dialog, bridge):
    dialog.save_ai()
    command = bridge.commands[-1]
    assert command['name'] == 'configure_codex'
    command['callback']({'result': {'codex_project': dict(PROJECT)}})


@pytest.mark.parametrize('font_size,width,height', [(13, 790, 650), (20, 790, 650), (20, 1188, 976)])
def test_connection_status_and_action_are_visible_without_scrolling(
        app, connected_settings, font_size, width, height):
    dialog, bridge, connection, calls = connected_settings
    apply_appearance(app, {'theme': 'dark', 'font_size': font_size})
    dialog.resize(width, height)
    finish_save(dialog, bridge)
    app.processEvents()
    page = dialog.tabs.widget(dialog._provider_tab)
    assert page.verticalScrollBar().value() == page.verticalScrollBar().minimum() == 0
    if width == 790 and font_size == 20:
        assert page.verticalScrollBar().maximum() > 0
    for widget in (dialog.codex_bridge_note, dialog.codex_bridge_start):
        assert not page.isAncestorOf(widget)
        assert widget.isVisible()
        assert widget.visibleRegion().boundingRect().contains(widget.rect())
        bounds = QRect(widget.mapTo(dialog, QPoint(0, 0)), widget.size())
        assert dialog.rect().contains(bounds)
        assert bounds.top() >= dialog.tabs.geometry().bottom()
    assert dialog.codex_bridge_note.height() >= dialog.codex_bridge_note.heightForWidth(
        dialog.codex_bridge_note.width())
    note = dialog.codex_bridge_note.text()
    for instruction in ('Codex 连接', '未打开', '连接 Codex'):
        assert instruction in note
    assert '退出' not in note and '普通方式' not in note
    assert dialog.codex_bridge_start.isEnabled()


def test_repeat_save_preserves_waiting_attempt_and_does_not_claim_connected(
        app, connected_settings):
    dialog, bridge, connection, calls = connected_settings
    generation = connection._generation
    finish_save(dialog, bridge)
    finish_save(dialog, bridge)
    app.processEvents()
    assert connection._generation == generation
    assert connection.state == 'desktop_closed'
    assert not any(c['name']=='connect_codex' for c in bridge.commands)
    assert '设置已保存' in dialog.message.text()
    assert '已保存的项目' in dialog.codex_project_note.text()
    assert '已连接' not in dialog.message.text()
    assert '已连接' not in dialog.codex_project_note.text()
    assert '未打开' in dialog.codex_bridge_note.text()


def test_footer_updates_after_save_for_ready_error_and_disconnected(
        connected_settings):
    dialog, bridge, connection, calls = connected_settings
    finish_save(dialog, bridge)
    connection._set_state('ready')
    assert '已连接' in dialog.codex_bridge_note.text()
    assert dialog.codex_bridge_start.isEnabled()
    connection._set_state('error', 'Synthetic connection failed; retry.')
    assert dialog.codex_bridge_note.text() == 'Codex 连接：Synthetic connection failed; retry.'
    assert dialog.codex_bridge_start.isEnabled()
    connection._set_state('disconnected', 'Synthetic connection disconnected.')
    assert dialog.codex_bridge_note.text() == 'Codex 连接：Synthetic connection disconnected.'
    assert '已连接' not in dialog.codex_bridge_note.text()


@pytest.mark.parametrize('state', ['error', 'disconnected'])
def test_save_does_not_activate_a_failed_or_lost_connection(app, connected_settings, state):
    dialog, bridge, connection, calls = connected_settings
    connection._set_state(state, 'Synthetic connection failure.')
    generation = connection._generation
    finish_save(dialog, bridge)
    assert connection._generation == generation
    assert connection.state == state
    assert not any(c['name']=='connect_codex' for c in bridge.commands)
    assert 'Synthetic connection failure' in dialog.codex_bridge_note.text()


def test_connection_updates_do_not_replace_another_settings_error(
        app, connected_settings):
    dialog, bridge, connection, calls = connected_settings
    finish_save(dialog, bridge)
    dialog.tabs.setCurrentIndex(1)
    dialog.error({'message': 'Synthetic display preferences could not be saved.'})
    connection._set_state('ready')
    app.processEvents()
    assert dialog.message.text() == 'Synthetic display preferences could not be saved.'
    assert dialog.message.objectName() == 'Error'
    assert not dialog.codex_bridge_note.isVisible()
    assert not dialog.codex_bridge_start.isVisible()
    dialog.tabs.setCurrentIndex(dialog._provider_tab)
    assert dialog.codex_bridge_note.isVisible()
    assert '已连接' in dialog.codex_bridge_note.text()
    assert dialog.message.text() == 'Synthetic display preferences could not be saved.'


def test_project_receipt_alone_never_reports_desktop_readiness(app, tmp_path):
    bridge = ControlledBridge()
    dialog = SettingsDialog(bridge, {'types': []}, tmp_path)
    try:
        bridge.deliver('settings', {'settings': copy.deepcopy(SETTINGS)})
        finish_save(dialog, bridge)
        assert '设置已保存' in dialog.message.text()
        assert '正在检查' in dialog.codex_bridge_note.text()
        assert '已连接' not in dialog.message.text()
        assert '已连接' not in dialog.codex_project_note.text()
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()
