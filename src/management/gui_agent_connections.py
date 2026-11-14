"""Client-specific MCP setup, without external account or managed-session binding."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFormLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)
from shiboken6 import isValid

from .agent_connections import CHANNEL_LABEL, PROFILES, apply_merge, configuration, launch_spec, preview_merge
from .gui_layout import ActionRow


class AgentConnectionsPage(QWidget):
    def __init__(self, data_dir, parent=None):
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        self.merge_plan = None
        self._probe_process = None
        self.last_probe = None
        layout = QVBoxLayout(self)
        note = QLabel('让外部助手读取和更新当前' + CHANNEL_LABEL + '数据空间。Antigravity 和通用连接支持业务工具；受管会话仍由原 Codex 协助入口管理。')
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.profile = QComboBox()
        for key, name in PROFILES.items():
            self.profile.addItem(name, key)
        form.addRow('连接到', self.profile)
        space = QLabel(str(self.data_dir))
        space.setWordWrap(True)
        form.addRow(CHANNEL_LABEL + '数据空间', space)
        layout.addLayout(form)
        self.config_preview = QPlainTextEdit()
        self.config_preview.setReadOnly(True)
        self.config_preview.setMinimumHeight(150)
        self.config_preview.setMaximumHeight(240)
        self.config_preview.setAccessibleName('当前助手的 MCP 配置')
        layout.addWidget(self.config_preview)
        row = ActionRow()
        self.copy_button = QPushButton('复制配置')
        self.probe_button = QPushButton('检测 MCP')
        self.copy_button.clicked.connect(self.copy_configuration)
        self.probe_button.clicked.connect(self.probe)
        row.addWidget(self.copy_button)
        row.addWidget(self.probe_button)
        layout.addWidget(row)
        self.help = QLabel()
        self.help.setWordWrap(True)
        layout.addWidget(self.help)
        self.target = QLineEdit()
        self.target.setPlaceholderText('从客户端打开原始配置，选择其 mcp_config.json')
        self.target.setAccessibleName('客户端 MCP 配置文件')
        layout.addWidget(self.target)
        row = ActionRow()
        self.choose_button = QPushButton('选择配置文件')
        self.preview_button = QPushButton('预览合并')
        self.apply_button = QPushButton('写入这项连接')
        self.apply_button.setEnabled(False)
        self.choose_button.clicked.connect(self.choose_target)
        self.preview_button.clicked.connect(self.preview_merge)
        self.apply_button.clicked.connect(self.apply_merge)
        for button in (self.choose_button, self.preview_button, self.apply_button):
            row.addWidget(button)
        layout.addWidget(row)
        self.merge_status = QLabel('只添加当前' + CHANNEL_LABEL + '连接，保留其他服务器和设置。已有同名不同内容时会停止。')
        self.merge_status.setWordWrap(True)
        layout.addWidget(self.merge_status)
        self.status = QLabel('配置尚未检测。')
        self.status.setWordWrap(True)
        self.status.setObjectName('Hint')
        layout.addWidget(self.status)
        layout.addStretch()
        self.profile.currentIndexChanged.connect(self.refresh)
        self.target.textChanged.connect(self.invalidate_merge)
        self.refresh()

    def invalidate_merge(self, *_):
        self.merge_plan = None
        self.apply_button.setEnabled(False)

    def refresh(self, *_):
        self.invalidate_merge()
        self.last_probe = None
        profile = self.profile.currentData()
        try:
            self.config_preview.setPlainText(configuration(self.data_dir, profile))
            self.copy_button.setEnabled(True)
            self.probe_button.setEnabled(True)
            self.status.setText('配置已生成，尚未检测；生成配置不代表客户端已连接。')
        except (OSError, ValueError) as error:
            self.config_preview.clear()
            self.copy_button.setEnabled(False)
            self.probe_button.setEnabled(False)
            self.status.setText(str(error))
        mergeable = profile != 'codex'
        for control in (self.target, self.choose_button, self.preview_button):
            control.setEnabled(mergeable)
        self.help.setText(
            '在 Antigravity 的 MCP 设置中打开原始配置，合并后刷新连接。自动合并支持标准 JSON；含注释或尾随逗号的配置请复制条目手动添加。'
            if profile == 'antigravity' else
            '通用 JSON 是启动参数模板；请按所用客户端的字段要求添加。此处可合并 mcp_config.json。'
            if profile == 'generic' else
            '这里提供当前' + CHANNEL_LABEL + '连接的 TOML。已有 Codex 事务助手项目请继续在“Codex 协助”页管理，不需要重复添加。')

    def copy_configuration(self):
        QApplication.clipboard().setText(self.config_preview.toPlainText())
        self.status.setText('配置已复制，不包含业务服务令牌。添加后请在客户端刷新 MCP。')

    def choose_target(self):
        path, _ = QFileDialog.getSaveFileName(self, '选择客户端 MCP 配置文件',
            self.target.text() or 'mcp_config.json', 'MCP 配置 (mcp_config.json)',
            options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            self.target.setText(path)

    def preview_merge(self):
        self.invalidate_merge()
        if not self.target.text().strip():
            self.merge_status.setText('请先选择客户端实际使用的 mcp_config.json。')
            return
        try:
            self.merge_plan = preview_merge(self.data_dir, self.profile.currentData(), self.target.text())
            self.merge_status.setText(('将添加连接：' if self.merge_plan.added else '此连接已存在且一致，无需写入：') +
                self.merge_plan.server + '\n' + str(self.merge_plan.path) +
                ('\n保留其他字段；已有文件会保留首次合并前的备份。' if self.merge_plan.added else ''))
            self.apply_button.setEnabled(self.merge_plan.added)
        except (OSError, ValueError) as error:
            self.merge_status.setText(str(error))

    def apply_merge(self):
        if self.merge_plan is None:
            return
        try:
            result = apply_merge(self.merge_plan)
            self.merge_status.setText('配置已保存，请在客户端刷新 MCP。其他连接已保留。' +
                ('\n首次备份：' + result['backup'] if result.get('backup') else ''))
        except (OSError, ValueError) as error:
            self.merge_status.setText(str(error))
        finally:
            self.invalidate_merge()

    def probe(self):
        if self._probe_process is not None:
            return
        profile = self.profile.currentData()
        try:
            spec = launch_spec(self.data_dir, profile)
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return
        args = list(spec['args'])
        index = args.index('--mcp')
        args[index:index+1] = ['--check-mcp', profile]
        process = QProcess(QApplication.instance())
        environment = QProcessEnvironment.systemEnvironment()
        for key, value in spec['env'].items():
            environment.insert(key, value)
        environment.insert('PERSONAL_MANAGEMENT_NO_TRAY', '1')
        process.setProcessEnvironment(environment)
        process.setWorkingDirectory(str(Path(__file__).resolve().parents[2]))
        self._probe_process = process
        self.probe_button.setEnabled(False)
        self.profile.setEnabled(False)
        self.status.setText('正在检测 MCP 握手、工具列表与业务读取…')
        process.finished.connect(lambda code, _status: self.probe_finished(process, code))
        process.errorOccurred.connect(lambda error: self.probe_failed(process, error))
        process.finished.connect(process.deleteLater)
        process.start(spec['command'], args)

    def probe_failed(self, process, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.probe_finished(process, -1)
            process.deleteLater()

    def probe_finished(self, process, code):
        # The short-lived self-check is app-owned and may finish after this page
        # closes; it never stops the independent business service.
        if not isValid(self) or self._probe_process is not process:
            return
        self._probe_process = None
        self.probe_button.setEnabled(True)
        self.profile.setEnabled(True)
        try:
            raw = bytes(process.readAllStandardOutput())
            if len(raw) > 65536:
                raise ValueError()
            result = json.loads(raw.decode('utf-8'))
            if not isinstance(result, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            result = {'ok': False, 'message': '自检程序未返回有效结果，请检查' + CHANNEL_LABEL + '运行环境。'}
        self.last_probe = result
        if code == 0 and result.get('ok'):
            self.status.setText('MCP 自检通过：握手、工具列表和业务读取正常。尚未验证外部助手实际加载；请在助手中读取一次当前状态。')
        else:
            self.status.setText('MCP 自检未通过：' + str(result.get('message', '请检查' + CHANNEL_LABEL + '后台状态。'))[:1500])
