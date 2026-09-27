"""Observe the service-owned Codex connection; activate only on an explicit click."""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

from .gui_gc import install_gui_gc


MESSAGES = {
    'disabled': 'Codex 连接未启用。',
    'checking': '正在检查 Codex 连接…',
    'connecting': '正在连接 Codex…',
    'ready': '已连接，可以发送并在 Codex 中继续同一讨论。',
    'desktop_closed': 'Codex 未打开。点击“连接 Codex”即可打开并连接。',
    'disconnected': '连接暂时中断，正在自动检查；草稿会保留。',
    'unsupported': '当前 Codex 版本暂不支持此连接，请查看连接提示。',
    'error': '暂时无法确认连接，正在自动重试。',
}


class CodexConnectionController(QObject):
    """A backend connection snapshot; polling never launches or sends a message.

    Compatibility constructor arguments never invoke the old desktop step.
    Async replies are fenced after settings changes or window closure.
    """
    changed = Signal(object)

    def __init__(self, data_dir=None, parent=None, *, bridge=None, step=None,
                 automatic=True, interval_ms=1500, probe_interval_ms=5000):
        manager = install_gui_gc()
        super().__init__(parent)
        self.bridge, self.automatic = bridge, automatic
        self.available = bridge is not None
        self.state, self.message, self.error_code = 'disabled', MESSAGES['disabled'], ''
        self._signature = None
        self._enabled = self._closing = False
        self._generation = self._serial = self._failures = 0
        self._probe_pending = self._connect_pending = None
        self._details = {}
        self.interval_ms = max(1, min(interval_ms, 15000))
        self.probe_interval_ms = max(1, min(probe_interval_ms, 15000))
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._tick)
        manager.register_bridge(self)

    @property
    def required(self):
        return self.available and self._enabled and not self._closing

    @property
    def ready(self):
        return self.required and self.state == 'ready'

    @property
    def connecting(self):
        return self._connect_pending is not None

    def snapshot(self):
        return {**self._details, 'state': self.state, 'ready': self.ready,
                'required': self.required, 'message': self.message,
                'code': self.error_code, 'connect_pending': self.connecting}

    def _set_state(self, state, message='', code=''):
        self.state = state if state in MESSAGES else 'error'
        self.message = message or MESSAGES[self.state]
        self.error_code = code
        self.changed.emit(self.snapshot())

    def configure(self, settings, *, force=False):
        if self._closing:
            return
        ai = settings.get('ai', {})
        signature = (bool(ai.get('enabled')), ai.get('execution_mode', 'background'),
                     ai.get('executable') or '')
        if signature == self._signature and not force:
            return
        self._signature = signature
        self._enabled = signature[0] and signature[1] == 'desktop_shared'
        self._generation += 1
        self._probe_pending = self._connect_pending = None
        self._failures = 0
        self._details = {}
        self.timer.stop()
        if not self.required:
            self._set_state('disabled')
            return
        self._set_state('checking')
        if self.automatic:
            self._probe()

    def request_check(self, *_):
        """Refresh status only; this action never activates an application."""
        if self.required and not self.connecting:
            self.timer.stop()
            self._probe()

    def request_connect(self, *_):
        """Explicit user request to the service's normal desktop connector."""
        if not self.required or self.connecting:
            return
        if self.ready:
            self.request_check()
            return
        self.timer.stop()
        self._serial += 1
        token = (self._generation, self._serial)
        self._connect_pending = token
        self._probe_pending = None
        self._set_state('connecting')

        def done(value=None, error=None):
            if self._closing or self._connect_pending != token or token[0] != self._generation:
                return
            self._connect_pending = None
            if error:
                self._failed(error)
            else:
                value = value or {}
                value = value.get('result', value)
                if isinstance(value, dict):
                    value = value.get('connection', value)
                if isinstance(value, dict) and ('state' in value or 'ready' in value):
                    self._accept(value)
                else:
                    self._set_state('checking')
                    self._probe()

        self.bridge.command('connect_codex', {}, lambda value: done(value), lambda error: done(error=error))

    def _tick(self):
        if self.required and not self.connecting:
            self._probe()

    def _schedule(self, retry=False):
        if not self.required or not self.automatic:
            return
        if retry:
            self._failures = min(self._failures + 1, 8)
            delay = min(15000, self.probe_interval_ms * 2 ** (self._failures - 1))
        else:
            self._failures = 0
            delay = self.interval_ms if self.state in {'checking', 'connecting'} else self.probe_interval_ms
        self.timer.start(delay)

    def _failed(self, error):
        self._set_state('error', error.get('message') or MESSAGES['error'], error.get('code', ''))
        self._schedule(retry=True)

    def _accept(self, value):
        state = value.get('state') or ('ready' if value.get('ready') else 'disconnected')
        if state == 'ready' and value.get('ready') is not True:
            state = 'disconnected'
        self._details = {key: value[key] for key in ('desktop_version', 'checked_at', 'project_ready', 'mcp_ready') if key in value}
        self._set_state(state, value.get('message', ''), value.get('code', ''))
        self._schedule(retry=state in {'error', 'disconnected', 'unsupported'})

    def _probe(self):
        if not self.required or self._probe_pending is not None or self.connecting:
            return
        self._serial += 1
        token = (self._generation, self._serial)
        self._probe_pending = token

        def done(value=None, error=None):
            if self._closing or self._probe_pending != token or token[0] != self._generation:
                return
            self._probe_pending = None
            if error:
                self._failed(error)
            else:
                self._accept(value or {})

        self.bridge.query('codex_connection', lambda value: done(value), lambda error: done(error=error))

    def workers_running(self):
        return False

    def request_stop(self):
        self._closing = True
        self._generation += 1
        self._probe_pending = self._connect_pending = None
        self.timer.stop()

    def close(self, timeout_ms=0):
        self.request_stop()
        return True
