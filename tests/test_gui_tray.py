import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from management.gui_instance import WindowInstance, notify_window, pipe_name, window_running
from management.gui_tray import ServiceTray
from management.gui_shutdown import request_exit
from management.runtime import OwnerLock
from management.runtime_contract import service_contract
from management import tray_runtime


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


class Icon(QObject):
    activated = Signal(object)
    messageClicked = Signal()
    def __init__(self, icon, parent):
        super().__init__(parent)
        self.messages = []
        self.visible = False
    def setContextMenu(self, menu): self.menu = menu
    def setToolTip(self, text): self.tooltip = text
    def setIcon(self, icon): pass
    def show(self): self.visible = True
    def hide(self): self.visible = False
    def showMessage(self, *args): self.messages.append(args)


class Bridge:
    def __init__(self):
        self.callbacks = {}
        self.uncertain_writes = {}
        self.reads = []
        self.stops = []
        self.closed = False
    def query(self, name, done, failed): self.reads.append((name, done, failed))
    def stop_service(self, done, failed): self.stops.append((done, failed))
    def request_stop(self): self.closed = True


@pytest.fixture
def tray(app, tmp_path):
    bridge = Bridge()
    item = ServiceTray(tmp_path, app, bridge=bridge, icon_factory=Icon, notifier=lambda *a: False)
    item.timer.stop()
    app.processEvents()
    yield item, bridge
    item.shutdown()
    item.menu.close()
    item.deleteLater()
    app.processEvents()


def online(jobs=None):
    return {'service_contract': service_contract(), 'jobs': jobs or {}, 'maintenance': False}


def test_status_shows_real_running_queued_and_pending_review_counts(tray):
    item, bridge = tray
    assert 'Beta 测试版' in item.open_action.text()
    assert 'Beta' in item.exit_action.text() and 'Beta' in item.version.text()
    assert 'Beta 测试版' in item.icon.tooltip
    assert bridge.reads[0][0] == 'runtime_status'
    bridge.reads[-1][1](online({'running': 2, 'queued': 3, 'awaiting_review': 4}))
    assert '2 项执行中，3 项排队' in item.status.text()
    assert '4 项待核对' in item.icon.tooltip
    assert item.icon.visible and not bridge.stops
    item.check()
    bridge.reads[-1][1](online())
    assert '暂无执行中的任务' in item.status.text()


def test_poll_failure_does_not_bootstrap_or_claim_running(tray):
    item, bridge = tray
    bridge.reads[-1][2]({'code': 'service_unavailable'})
    assert '后台未运行' in item.status.text()
    assert item.icon.visible and not item.closing and not bridge.stops


def test_exit_after_window_closed_stops_backend_then_removes_icon(tray):
    item, bridge = tray
    item._checked(online())
    item.exit_requested()
    bridge.reads[-1][1](online())
    assert len(bridge.stops) == 1
    bridge.stops[0][0]({'prepared': True})
    assert item.maintenance_seen and item.icon.visible
    item._failed({'code': 'connection_lost'})
    assert item.closing and not item.icon.visible and bridge.closed


def test_busy_backend_keeps_icon_and_rejects_force_exit(tray):
    item, bridge = tray
    item._checked(online({'running': 1}))
    item.exit_requested()
    bridge.reads[-1][1](online({'running': 1}))
    bridge.stops[-1][1]({'code': 'update_busy', 'message': '还有任务正在执行'})
    assert item.icon.visible and not item.closing
    assert item.exit_action.isEnabled()
    assert item.icon.messages[-1][1] == '还有任务正在执行'


def test_existing_window_receives_exit_request_and_backend_is_not_stopped(tray):
    item, bridge = tray
    lock = OwnerLock(item.data_dir / 'gui.lock')
    assert lock.acquire()
    sent = []
    item.notifier = lambda root, command: sent.append(command) or True
    try:
        item.exit_requested()
        assert sent == ['exit'] and not bridge.stops
    finally:
        lock.release()


def test_window_appearing_during_poll_gets_unsaved_edit_guard(tray):
    item, bridge = tray
    item._checked(online())
    item.exit_requested()
    lock = OwnerLock(item.data_dir / 'gui.lock')
    assert lock.acquire()
    sent = []
    item.notifier = lambda root, command: sent.append(command) or True
    try:
        bridge.reads[-1][1](online())
        assert sent == ['exit'] and not bridge.stops
    finally:
        lock.release()


def test_mismatched_version_observer_exits_without_stopping_service(tray):
    item, bridge = tray
    item._checked({'service_contract': {**service_contract(), 'app_version': '99.0.0'}})
    assert item.closing and bridge.closed and not bridge.stops


def test_repeated_open_uses_existing_window_instead_of_new_process(tray):
    item, bridge = tray
    sent = []
    item.notifier = lambda root, command: sent.append(command) or True
    item.launcher = lambda *a, **kw: pytest.fail('Must reuse the existing window')
    item.open_window()
    item.open_window(show_update=True)
    assert sent == ['show', 'update']


def test_gui_exit_preserves_unsaved_edit_and_uncertain_write(app):
    window = QWidget()
    window.review_pending = False
    window.bridge = Bridge()
    errors = []
    window.show_error = errors.append
    editor = QDialog(window)
    editor.show()
    try:
        request_exit(window)
        assert not window.bridge.stops and '保存并关闭' in errors[-1]['message']
        editor.close()
        window.bridge.uncertain_writes = {'pending': {}}
        request_exit(window)
        assert not window.bridge.stops and '回执' in errors[-1]['message']
    finally:
        editor.close()
        window.close()


@pytest.mark.parametrize('command', ['show', 'visual-classic', 'visual-glass'])
def test_single_window_pipe_is_scoped_and_rejects_unrecognized_commands(app, tmp_path, command):
    first = WindowInstance(tmp_path / 'one')
    duplicate = WindowInstance(tmp_path / 'one')
    other = WindowInstance(tmp_path / 'two')
    seen = []
    first.requested.connect(seen.append)
    try:
        assert first.acquire() and not duplicate.acquire() and other.acquire()
        assert window_running(tmp_path / 'one')
        assert pipe_name(tmp_path / 'one') != pipe_name(tmp_path / 'two')
        program = "from PySide6.QtCore import QCoreApplication; from management.gui_instance import notify_window; import sys; app=QCoreApplication([]); print(notify_window(sys.argv[1],sys.argv[2]),flush=True)"
        child = subprocess.Popen([sys.executable, '-c', program, str(tmp_path / 'one'), command],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        deadline = time.monotonic() + 5
        while child.poll() is None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        output, errors = child.communicate(timeout=3)
        assert child.returncode == 0 and output.strip() == 'True', errors
        app.processEvents()
        assert seen == [command]
    finally:
        first.close()
        other.close()
        duplicate.close()


def test_tray_process_lock_has_single_owner(tmp_path):
    assert not tray_runtime.observer_running(tmp_path)
    lock = OwnerLock(tmp_path / 'tray.lock')
    assert lock.acquire()
    try:
        assert tray_runtime.observer_running(tmp_path)
    finally:
        lock.release()
    assert not tray_runtime.observer_running(tmp_path)


@pytest.mark.parametrize('command', ['visual-future', 'visual-classic\nexit'])
def test_visual_window_protocol_rejects_unknown_or_combined_commands(tmp_path, command):
    with pytest.raises(ValueError, match='Unsupported window request'):
        notify_window(tmp_path, command)
