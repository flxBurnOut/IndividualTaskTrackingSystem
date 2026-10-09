"""User-visible update readiness, without silent process termination."""
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QLabel, QPushButton,
                              QScrollArea, QFrame, QWidget, QLayout)

from . import __version__
from .branding import APP_NAME
from .gui_layout import ActionRow
from .gui_settings_controls import SelectableText


def version_summary(state):
    contract = state.get('service_contract') or {}
    version = contract.get('app_version', contract.get('version', '未核验'))
    channel = 'Beta 测试版' if contract.get('channel') == 'beta' else '未核验为 Beta'
    entrances = state.get('client_entrances') or {}
    mcp = entrances.get('mcp') or {}
    if mcp:
        mcp_version = mcp.get('app_version', mcp.get('version', '未知'))
        mcp_text = f"最近一次 MCP 接口请求：{mcp_version}。"
        if mcp_version != __version__ or mcp.get('verified') is not True:
            mcp_text += '仍有旧接口请求，请在 Codex 任务空闲后刷新 MCP。'
        else:
            mcp_text += '该次请求版本一致；其他已打开会话仍可能需要刷新。'
    else:
        mcp_text = 'MCP 接口：本次后台启动后尚未核验；连接成功不代表旧会话已加载新接口。'
    return f'{APP_NAME}\n软件版本：{__version__}\n后台版本：{version}（{channel}）\n{mcp_text}'


class UpdateDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.preparing = False
        self.setWindowTitle('Beta 版本与更新')
        self.resize(680, 470)
        layout = QVBoxLayout(self)
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content_scroll.setMinimumSize(0, 0)
        body = QWidget()
        content = QVBoxLayout(body)
        content.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.content_scroll.setWidget(body)
        layout.addWidget(self.content_scroll, 1)
        self.versions = QLabel(f'{APP_NAME}\n软件版本：{__version__}\n正在核验 Beta 后台版本…')
        self.versions.setWordWrap(True)
        self.versions.setTextFormat(Qt.TextFormat.PlainText)
        content.addWidget(self.versions)
        self.location = SelectableText('Beta 数据位置（更新后继续使用）\n' + str(Path(window.data_dir).resolve()))
        content.addWidget(self.location)
        steps = QLabel(
            '当前为 Beta 源码测试版，尚未提供独立 Beta 安装包。\n\n'
            '1. 保存并关闭 Beta 编辑窗口，等待 Beta 后台及关联的 Codex 任务完成。\n'
            '2. 点击下方“退出 Beta 并准备更新”，再更新本 Beta 工作目录的源码和依赖。\n'
            '3. 使用“启动个人事务管理Beta.vbs”重新打开，继续使用当前 Beta 数据位置。\n'
            '4. 如已连接 Beta MCP，在关联任务空闲后刷新该连接，并通过接口调用核验版本。\n\n'
            'Beta 更新不使用正式版安装包。需要保留测试记录时，可先在“设置 → 数据与高级”创建备份。')
        steps.setWordWrap(True)
        content.addWidget(steps)
        content.addStretch()
        self.status = QLabel('')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        buttons = ActionRow()
        self.prepare = QPushButton('退出 Beta 并准备更新')
        self.prepare.clicked.connect(self.prepare_update)
        self.refresh = QPushButton('重新核验版本')
        self.refresh.clicked.connect(self.check_versions)
        self.cancel = QPushButton('关闭')
        self.cancel.clicked.connect(self.reject)
        for button in (self.prepare, self.refresh, self.cancel):
            buttons.addWidget(button)
        layout.addWidget(buttons)
        self.check_versions()

    def check_versions(self):
        self.window.bridge.query('runtime_status', lambda state: self.versions.setText(version_summary(state)),
                                 lambda error: self.versions.setText(f'{APP_NAME}\n软件版本：{__version__}\n' + error.get('message', '版本核验失败')))

    def prepare_update(self):
        if self.preparing:
            return
        from .gui_shutdown import shutdown_blocker
        problem = shutdown_blocker(self.window, self)
        if problem:
            self.status.setText(problem)
            return
        self.preparing = True
        self.prepare.setEnabled(False)
        self.refresh.setEnabled(False)
        self.cancel.setEnabled(False)
        self.window.poll.stop()
        self.window.codex_connection.timer.stop()
        self.status.setText('正在检查 Beta 后台任务并准备退出…')
        self.window.bridge.prepare_update(self._prepared, self._failed)

    def _prepared(self, result):
        self.status.setText('Beta 后台已进入更新准备状态，窗口即将关闭。请更新 Beta 源码后重新启动。')
        self.window.codex_connection.request_stop()
        self.accept()
        QTimer.singleShot(0, self.window.close)

    def _failed(self, error):
        self.preparing = False
        self.prepare.setEnabled(True)
        self.refresh.setEnabled(True)
        self.cancel.setEnabled(True)
        self.status.setText(error.get('message', '未完成更新准备，请重试。'))
        if error.get('code') not in {'update_draining', 'update_pending'}:
            self.window.poll.start()
            self.window.codex_connection.request_check()

    def reject(self):
        if not self.preparing:
            super().reject()
