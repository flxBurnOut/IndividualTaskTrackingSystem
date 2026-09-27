"""Service-owned desktop status and explicit activation, without external apps."""
import os
import time
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from management.gui_codex_connection import CodexConnectionController
from management.gui_assistant import AssistanceDialog
from management.gui_workflows import SettingsDialog
from test_ux_workflows_v2 import ControlledBridge


SETTINGS = {'ai': {'enabled': True, 'execution_mode': 'desktop_shared', 'executable': ''}}


@pytest.fixture(scope='session')
def app():
    return QApplication.instance() or QApplication([])


def wait(app, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():return
        time.sleep(.005)
    raise AssertionError('Connection state did not settle')


def stop(app, controller):
    controller.request_stop();controller.deleteLater();app.processEvents()


@pytest.fixture
def connection(app, tmp_path):
    bridge = ControlledBridge()
    value = CodexConnectionController(tmp_path, bridge=bridge)
    value.configure(SETTINGS)
    try:yield value, bridge
    finally:stop(app, value)


def test_startup_and_both_application_orders_only_query_until_explicit_click(connection):
    controller, bridge = connection
    bridge.deliver('codex_connection', {'ready': False, 'state': 'desktop_closed'})
    assert controller.timer.isActive() and bridge.commands == []
    controller._tick()
    bridge.deliver('codex_connection', {'ready': True, 'state': 'ready', 'desktop_version': 'synthetic'})
    assert controller.ready and controller.snapshot()['desktop_version'] == 'synthetic'
    controller._tick()
    bridge.deliver('codex_connection', {'ready': False, 'state': 'desktop_closed'})
    controller._tick()
    bridge.deliver('codex_connection', {'ready': True, 'state': 'ready'})
    assert controller.ready and bridge.commands == []


def test_transient_errors_keep_retrying_with_bounded_backoff(connection):
    controller, bridge = connection
    for expected in (5000, 10000, 15000, 15000):
        query = bridge.take('codex_connection')
        query['error']({'code': 'timeout', 'message': 'Synthetic temporary interruption'})
        assert controller.timer.isActive() and controller.timer.interval() == expected
        controller._tick()
    bridge.deliver('codex_connection', {'ready': True, 'state': 'ready'})
    assert controller.ready and controller.timer.interval() == 5000 and bridge.commands == []


def test_connect_is_explicit_and_old_probe_cannot_overwrite_command_result(connection):
    controller, bridge = connection
    old = bridge.take('codex_connection')
    controller.request_connect();controller.request_connect()
    assert len(bridge.commands) == 1 and bridge.commands[0]['name'] == 'connect_codex'
    bridge.commands[0]['callback']({'result': {'ready': True, 'state': 'ready'}})
    old['callback']({'ready': False, 'state': 'desktop_closed'})
    assert controller.ready
    controller.request_connect()
    assert len(bridge.commands) == 1  # Ready button checks; it does not relaunch.


@pytest.mark.parametrize('action', ['close', 'disable'])
def test_late_connection_reply_cannot_restore_closed_or_disabled_observer(connection, action):
    controller, bridge = connection
    controller.request_connect()
    callback = bridge.commands[-1]['callback']
    if action == 'close':controller.request_stop()
    else:controller.configure({'ai': {'enabled': False, 'execution_mode': 'desktop_shared'}})
    callback({'ready': True, 'state': 'ready'})
    assert not controller.ready and not controller.timer.isActive()
    assert [c['name'] for c in bridge.commands] == ['connect_codex']


def test_same_saved_settings_do_not_activate_and_legacy_step_is_never_called(app, tmp_path):
    bridge = ControlledBridge()
    def forbidden(*args, **kwargs):raise AssertionError('GUI must never start a shim')
    controller = CodexConnectionController(tmp_path, bridge=bridge, step=forbidden)
    try:
        controller.configure(SETTINGS);controller.configure(SETTINGS)
        assert len(bridge.queries) == 1 and not bridge.commands and not controller.workers_running()
    finally:stop(app, controller)


@pytest.fixture
def draft(app, tmp_path):
    bridge, parent = ControlledBridge(), QWidget()
    controller = parent.codex_connection = CodexConnectionController(tmp_path, bridge=bridge)
    dialog = AssistanceDialog(bridge, parent, prompt='Keep this draft', source_ids=['source-one'])
    bridge.deliver('settings', {'settings': SETTINGS})
    bridge.deliver('conversation', {'conversation': None, 'messages': [], 'has_more': False})
    bridge.deliver('codex_connection', {'ready': False, 'state': 'desktop_closed'})
    dialog.timer.stop()
    try:yield dialog, bridge, controller
    finally:
        dialog.close();stop(app, controller);parent.deleteLater();app.processEvents()


def test_connect_and_send_dispatches_clicked_draft_exactly_once(draft):
    dialog, bridge, controller = draft
    assert dialog.send.text() == '连接并发送'
    dialog.start_job();dialog.start_job()
    assert [item['name'] for item in bridge.commands] == ['connect_codex']
    assert dialog.send.text() == '取消等待发送'
    callback = bridge.commands[0]['callback']
    callback({'result': {'ready': True, 'state': 'ready'}})
    callback({'result': {'ready': True, 'state': 'ready'}})
    controller._set_state('ready')
    assert [item['name'] for item in bridge.commands] == ['connect_codex', 'send_message']
    assert bridge.commands[-1]['payload']['text'] == 'Keep this draft'
    assert bridge.commands[-1]['payload']['source_ids'] == ['source-one']


def test_connect_and_send_waits_for_verified_status_after_connect_command(draft):
    dialog, bridge, controller = draft
    dialog.start_job()
    bridge.commands[0]['callback']({'result': {'ready': False, 'state': 'connecting'}})
    assert dialog._pending_connection_send is not None and len(bridge.commands) == 1
    controller._tick()
    bridge.deliver('codex_connection', {'ready': True, 'state': 'ready'})
    assert [item['name'] for item in bridge.commands] == ['connect_codex', 'send_message']


@pytest.mark.parametrize('change', ['edit', 'edit_back', 'attachment', 'cancel', 'close', 'epoch', 'timeout'])
def test_changed_or_cancelled_draft_is_not_late_sent(draft, change):
    dialog, bridge, controller = draft
    dialog.start_job()
    callback = bridge.commands[0]['callback']
    if change in {'edit', 'edit_back'}:
        dialog.prompt.setPlainText('Changed draft')
        if change == 'edit_back':dialog.prompt.setPlainText('Keep this draft')
    elif change == 'attachment':dialog.remove_source('source-one')
    elif change == 'cancel':dialog.send_or_stop()
    elif change == 'close':dialog.close()
    elif change == 'epoch':bridge.epoch='new-space'
    else:dialog.connection_send_timer.timeout.emit()
    callback({'result': {'ready': True, 'state': 'ready'}})
    assert [item['name'] for item in bridge.commands] == ['connect_codex']
    assert dialog.outgoing is None


def test_connect_failure_does_not_replay_draft_on_later_automatic_recovery(draft):
    dialog, bridge, controller = draft
    dialog.start_job()
    bridge.commands[0]['error']({'message': 'Synthetic failure', 'code': 'connection_lost'})
    assert dialog._pending_connection_send is None and dialog.prompt.toPlainText() == 'Keep this draft'
    controller._tick()
    bridge.deliver('codex_connection', {'ready': True, 'state': 'ready'})
    assert [item['name'] for item in bridge.commands] == ['connect_codex']


def test_auto_send_request_waiting_for_connection_keeps_draft_without_activation(draft):
    dialog, bridge, controller = draft
    dialog.auto_send=True;dialog.maybe_auto_send()
    controller._tick();bridge.deliver('codex_connection', {'ready': True, 'state': 'ready'})
    dialog.maybe_auto_send()
    assert not bridge.commands and dialog.prompt.toPlainText() == 'Keep this draft'


def test_closing_discussion_only_detaches_ui_and_keeps_running_job(draft):
    dialog, bridge, controller = draft
    dialog.active_job_id='running-job';dialog.close()
    assert not bridge.commands and controller.required


def test_settings_save_and_explicit_connect_are_separate(app, tmp_path):
    bridge = ControlledBridge()
    dialog = SettingsDialog(bridge, {'types': []}, tmp_path)
    try:
        bridge.deliver('settings', {'settings': {'ai': {'enabled': False, 'execution_mode': 'desktop_shared'}}})
        dialog.ai_enabled.setChecked(True);dialog.save_ai()
        bridge.commands[-1]['callback']({'result': {'settings': SETTINGS}})
        bridge.deliver('codex_connection', {'ready': False, 'state': 'desktop_closed'})
        assert dialog.ai_save.text() == '保存设置'
        assert '首次连接或发送' in dialog.codex_project_note.text()
        assert [item['name'] for item in bridge.commands] == ['configure_codex']
        dialog.codex_bridge_start.click()
        assert [item['name'] for item in bridge.commands] == ['configure_codex', 'connect_codex']
        bridge.commands[-1]['callback']({'result': {'ready': True, 'state': 'ready'}})
        assert '已连接' in dialog.codex_bridge_note.text()
    finally:dialog.close();dialog.deleteLater();app.processEvents()
