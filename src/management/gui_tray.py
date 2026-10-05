"""A visible, read-only observer of the data service with explicit exit actions."""
from __future__ import annotations

from pathlib import Path
import json
import os
import time

from PySide6.QtCore import QObject, QTimer, QThread
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import __version__
from .branding import APP_NAME, configure_application, icon_path, set_windows_identity
from .client import Client
from .gui_async import ServiceBridge
from .gui_gc import install_gui_gc
from .gui_instance import notify_window, window_running
from .runtime import OwnerLock
from .runtime_contract import matches_contract
from .tray_runtime import launch_gui


class ServiceTray(QObject):
    def __init__(self, data_dir, app, *, bridge=None, icon_factory=QSystemTrayIcon,
                 notifier=notify_window, launcher=launch_gui):
        super().__init__(app)
        self.data_dir = Path(data_dir).resolve()
        self.app, self.notifier, self.launcher = app, notifier, launcher
        self.pending = self.closing = self.maintenance_seen = self.stop_requested = False
        self.launching = None
        self.bridge = bridge or ServiceBridge(self.data_dir, self,
            client_factory=lambda root: Client(root, autostart=False, entrance='tray'))
        self.icon = icon_factory(QIcon(str(icon_path())), self)
        self.normal_icon = QIcon(str(icon_path()))
        self.offline_icon = QIcon(self.normal_icon.pixmap(32, 32, QIcon.Mode.Disabled))
        self.menu = QMenu()
        self.status = self.menu.addAction('正在检查后台…')
        self.status.setEnabled(False)
        self.version = self.menu.addAction('Beta 测试版：' + __version__)
        self.version.setEnabled(False)
        self.location = self.menu.addAction('Beta 数据位置：' + str(self.data_dir))
        self.location.setEnabled(False)
        self.menu.addSeparator()
        self.open_action = self.menu.addAction('打开' + APP_NAME)
        self.open_action.triggered.connect(self.open_window)
        self.update_action = self.menu.addAction('Beta 版本与更新')
        self.update_action.triggered.connect(lambda: self.open_window(show_update=True))
        self.check_action = self.menu.addAction('检查后台状态')
        self.check_action.triggered.connect(self.check)
        self.menu.addSeparator()
        self.exit_action = self.menu.addAction('退出 Beta 软件与后台')
        self.exit_action.triggered.connect(self.exit_requested)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self.activated)
        self.icon.messageClicked.connect(self.open_window)
        self.icon.setToolTip(APP_NAME + ' · 正在检查后台')
        self.icon.show()
        self._publish_observation('正在检查后台')
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.check)
        self.timer.start()
        QTimer.singleShot(0, self.check)

    def activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_window()

    def set_status(self, text, *, online=True):
        self.status.setText(text)
        self.icon.setToolTip(APP_NAME + ' ' + __version__ + '\n' + text + '\n' + str(self.data_dir))
        self.icon.setIcon(self.normal_icon if online else self.offline_icon)
        self._publish_observation(text)

    def _publish_observation(self, text):
        if not hasattr(self.icon, 'isVisible'):
            return
        rectangle = self.icon.geometry()
        value = {'version': __version__, 'pid': os.getpid(), 'data_dir': str(self.data_dir),
                 'status': text, 'checked_at': time.time(), 'icon_registered': self.icon.isVisible(),
                 'system_tray_available': QSystemTrayIcon.isSystemTrayAvailable(),
                 'geometry': [rectangle.x(), rectangle.y(), rectangle.width(), rectangle.height()]}
        path = self.data_dir / 'tray-status.json'
        temporary = path.with_name(path.name + '.new')
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, path)
        except OSError:
            pass  # Observation failure must never terminate the tray itself.

    def inform(self, message):
        self.icon.showMessage(APP_NAME, message, QSystemTrayIcon.MessageIcon.Information, 6000)

    def check(self):
        if self.pending or self.closing:
            return
        self.pending = True
        self.bridge.query('runtime_status', self._checked, self._failed)

    def _checked(self, value):
        self.pending = False
        if self.closing:
            return
        if not matches_contract(value.get('service_contract')):
            # A newly installed service starts its own matching observer.
            self.shutdown()
            return
        if value.get('maintenance'):
            self.maintenance_seen = True
            self.set_status('后台正在退出…')
            self.exit_action.setEnabled(False)
            return
        counts = value.get('jobs') or {}
        count = lambda name: counts.get(name, 0) if type(counts.get(name, 0)) is int else 0
        running, queued, review = count('running'), count('queued'), count('awaiting_review')
        text = '后台运行中'
        text += f' · {running} 项执行中，{queued} 项排队' if running or queued else ' · 暂无执行中的任务'
        if review:
            text += f' · {review} 项待核对'
        self.set_status(text)
        if self.stop_requested:
            self._stop_backend()

    def _failed(self, error):
        self.pending = False
        if self.closing:
            return
        code = error.get('code')
        if code in {'service_version_mismatch', 'client_version_mismatch'}:
            self.shutdown()
            return
        if code in {'service_updating', 'update_draining', 'update_pending'}:
            self.maintenance_seen = True
        lock = OwnerLock(self.data_dir / 'service.lock')
        stopped = lock.acquire()
        if stopped:
            lock.release()
        if stopped and (self.maintenance_seen or self.stop_requested or (self.data_dir / 'update_pending.json').exists()):
            self.shutdown()
            return
        if self.maintenance_seen:
            self.set_status('后台正在退出…')
            return
        self.set_status('后台未运行 · 打开软件可恢复' if stopped else '暂时无法确认后台状态 · 请检查连接', online=False)

    def open_window(self, *_args, show_update=False):
        if self.closing:
            return
        command = 'update' if show_update else 'show'
        if self.notifier(self.data_dir, command):
            return
        if window_running(self.data_dir):
            self.inform('窗口正在启动或退出，请稍后再打开。')
            return
        if self.launching is not None and self.launching.poll() is None:
            return
        try:
            self.launching = self.launcher(self.data_dir, show_update=show_update)
        except OSError:
            self.inform('无法打开窗口，请检查当前版本的程序文件。')

    def exit_requested(self):
        if self.closing or self.maintenance_seen:
            return
        if window_running(self.data_dir):
            if not self.notifier(self.data_dir, 'exit'):
                self.inform('请先保存并关闭软件中的编辑窗口，再退出后台。')
            return
        self.stop_requested = True
        if not self.pending:
            self.check()

    def _stop_backend(self):
        self.stop_requested = False
        # A window may have opened while the status request was in flight.
        if window_running(self.data_dir):
            self.notifier(self.data_dir, 'exit')
            return
        self.pending = True
        self.exit_action.setEnabled(False)
        self.set_status('正在检查任务并退出后台…')
        self.bridge.stop_service(self._stopping, self._stop_failed)

    def _stopping(self, _value):
        self.pending = False
        self.maintenance_seen = True
        self.set_status('后台正在退出…')

    def _stop_failed(self, error):
        self.pending = False
        if error.get('code') in {'update_draining', 'update_pending', 'service_updating'}:
            self.maintenance_seen = True
            self.set_status('后台正在完成读取，随后自动退出…')
        else:
            self.exit_action.setEnabled(True)
            self.set_status('后台仍在运行 · 退出未完成')
            self.inform(error.get('message', '后台退出未完成，请稍后重试。'))

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.timer.stop()
        self.icon.hide()
        path = self.data_dir / 'tray-status.json'
        try:
            if json.loads(path.read_text('utf-8')).get('pid') == os.getpid():
                path.unlink()
        except (OSError, ValueError, AttributeError):
            pass
        self.bridge.request_stop()
        self.app.quit()


def run(data_dir):
    from .data_space import require_beta_dir
    root = require_beta_dir(data_dir)
    lock = OwnerLock(root / 'tray.lock')
    if not lock.acquire():
        return 0
    try:
        set_windows_identity()
        app = QApplication.instance() or QApplication([])
        configure_application(app)
        app.setQuitOnLastWindowClosed(False)
        manager = install_gui_gc(app)
        tray = ServiceTray(root, app)
        result = app.exec()
        tray.icon.hide()
        while not manager.shutdown():
            app.processEvents()
            manager.poll()
            QThread.msleep(10)
        return result
    finally:
        lock.release()
