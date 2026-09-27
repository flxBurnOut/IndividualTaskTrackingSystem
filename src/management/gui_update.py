"""User-visible update readiness, without silent process termination."""
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout

from . import __version__


def version_summary(state):
    contract = state.get('service_contract') or {}
    version = contract.get('app_version', contract.get('version', '未核验'))
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
    return f'软件版本：{__version__}\n后台版本：{version}\n{mcp_text}'


class UpdateDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.preparing = False
        self.setWindowTitle('版本与更新')
        self.resize(680, 470)
        layout = QVBoxLayout(self)
        self.versions = QLabel(f'软件版本：{__version__}\n正在核验后台版本…')
        self.versions.setWordWrap(True)
        self.versions.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.versions)
        self.location = QLabel('当前数据位置（更新后继续使用）\n' + str(Path(window.data_dir).resolve()))
        self.location.setWordWrap(True)
        self.location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.location)
        steps = QLabel(
            '1. 保存并关闭其他编辑窗口，等待后台及 Codex 任务完成。\n'
            '2. 点击下方“退出并准备更新”，然后运行新版安装包。安装时核对原数据位置。\n'
            '3. 从更新后的快捷方式启动。已有记录、设置、资料和会话关联继续使用。\n'
            '4. 若安装器提示 Codex 接口占用文件，任务结束后暂时退出 Codex。'
            '更新后重新打开并通过一次接口调用核验版本。\n\n'
            '新版首次升级旧数据库前会保存数据库快照；资料原件保留在原目录。'
            '需要完整业务备份时，可先在“设置 → 数据与高级”创建备份。'
            '安装器不会自动下载新版本。')
        steps.setWordWrap(True)
        layout.addWidget(steps)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.prepare = QPushButton('退出并准备更新')
        self.prepare.clicked.connect(self.prepare_update)
        self.refresh = QPushButton('重新核验版本')
        self.refresh.clicked.connect(self.check_versions)
        self.cancel = QPushButton('关闭')
        self.cancel.clicked.connect(self.reject)
        for button in (self.prepare, self.refresh, self.cancel):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.check_versions()

    def check_versions(self):
        self.window.bridge.query('runtime_status', lambda state: self.versions.setText(version_summary(state)),
                                 lambda error: self.versions.setText(f'软件版本：{__version__}\n' + error.get('message', '版本核验失败')))

    def prepare_update(self):
        if self.preparing:
            return
        others = [widget for widget in QApplication.topLevelWidgets()
                  if widget.isVisible() and widget not in (self, self.window)]
        if others or self.window.review_pending:
            self.status.setText('请先保存并关闭其他编辑窗口，再准备更新。尚未停止后台。')
            return
        if self.window.bridge.callbacks or self.window.bridge.uncertain_writes:
            self.status.setText('仍有读取、保存或待核对的保存回执，请等处理结束后重试。')
            return
        self.preparing = True
        self.prepare.setEnabled(False)
        self.refresh.setEnabled(False)
        self.cancel.setEnabled(False)
        self.window.poll.stop()
        self.window.codex_connection.timer.stop()
        self.status.setText('正在检查后台任务并准备退出…')
        self.window.bridge.prepare_update(self._prepared, self._failed)

    def _prepared(self, result):
        self.status.setText('后台已进入更新准备状态，窗口即将关闭。请运行新版安装包。')
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
