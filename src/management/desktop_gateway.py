"""Service-owned connection to an ordinary Codex desktop, independent of GUI life.

Only an explicit connect() can activate Windows' registered application. Stream
recovery discovers owners and subscribes again; it never retries a model turn.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import threading
import time
import tomllib
import uuid

from .codex_installation import activate_codex
from .codex_ipc import (NativeDesktopIpc, IpcError, IpcDisconnected, IpcTimeout,
                        IpcIdentityError, IpcProtocolError)
from .desktop_state import ThreadSnapshot


MAX_SUBSCRIPTIONS = 32
_CONTROL_THREAD = '00000000-0000-4000-8000-000000000000'


def _thread_id(value):
    try:
        if not isinstance(value, str) or len(value) != 36 or str(uuid.UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, AttributeError):
        raise IpcProtocolError('desktop_thread_invalid', 'Codex 会话标识无效，未打开或发送消息。') from None


def _open_thread(thread_id):
    thread_id = _thread_id(thread_id)
    if os.name != 'nt':
        raise IpcError('desktop_windows_only', '此连接只支持 Windows Codex 桌面。')
    try:
        os.startfile(f'codex://threads/{thread_id}?hostId=local')
    except OSError as exc:
        raise IpcError('desktop_open_failed', '无法通过 Windows 注册入口打开已有 Codex 会话。') from exc


def _invalidate_snapshot(stream):
    # Use the projection's public identity-change contract to drop stale state.
    stream.accept({'type': 'broadcast', 'method': 'thread-stream-state-changed', 'version': 11,
                   'sourceClientId': None, 'params': {'conversationId': stream.thread_id, 'hostId': 'local'}})


class DesktopGateway:
    def __init__(self, core, *, project_ready=None):
        self.core = core
        self._project_check = project_ready
        self._condition = threading.Condition(threading.RLock())
        self._attach_lock = threading.Lock()
        self._client = None
        self._last_error = None
        self._enabled = False
        self._closing = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker = None
        self._streams = {}
        self._needs_resubscribe = set()
        self._next_subscribe = {}
        self._last_version = None

    def _settings_enabled(self):
        value = self.core.query('settings')
        settings = value.get('settings', value)
        ai = settings.get('ai', {})
        enabled = bool(ai.get('enabled')) and ai.get('execution_mode', 'background') == 'desktop_shared'
        with self._condition:
            self._enabled = enabled and not self._closing
        if not enabled:
            self.invalidate()
        else:
            # Begin observing even if the desktop is absent on the first check.
            # A later ordinary launch must not require a GUI polling window.
            self._start_worker()
        self._wake.set()
        return enabled

    def _project_status(self):
        if self._project_check is not None:
            result = self._project_check()
            if isinstance(result, dict):
                return bool(result.get('project_ready')), bool(result.get('mcp_config_ready'))
            return bool(result), bool(result)
        from .codex_project import project_binding
        workspace = Path(self.core.root) / 'Codex事务助手'
        try:
            path = workspace / '.codex/config.toml'
            if path.stat().st_size > 2 * 1024 * 1024:
                return False, False
            config = tomllib.loads(path.read_text('utf-8-sig'))['mcp_servers']['personal_management']
            configured = (isinstance(config.get('command'), str) and bool(config['command'])
                          and isinstance(config.get('args', []), list) and config.get('enabled', True) is True)
            return bool(configured and project_binding(workspace)), bool(configured)
        except (OSError, ValueError, KeyError, TypeError):
            return False, False

    def _start_worker(self):
        with self._condition:
            if self._worker is None and not self._closing:
                self._worker = threading.Thread(target=self._event_loop, name='management-desktop-events', daemon=True)
                self._worker.start()

    def _attach(self, timeout=10):
        if not self._attach_lock.acquire(timeout=max(0.001, timeout)):
            raise IpcTimeout('desktop_connect_busy', '正在连接 Codex，请稍后重试。')
        try:
            with self._condition:
                if self._closing:
                    raise IpcDisconnected('desktop_gateway_closed', '软件的 Codex 连接已关闭。')
                if not self._enabled:
                    raise IpcError('desktop_disabled', '尚未启用 Codex 桌面连接。')
                if self._client is not None:
                    return self._client
            client = NativeDesktopIpc.connect(timeout=max(0.001, timeout))
            with self._condition:
                if self._closing or not self._enabled:
                    client.close()
                    raise IpcDisconnected('desktop_gateway_closed', '软件的 Codex 连接已关闭。')
                self._client = client
                self._last_error = None
                self._last_version = client.installation.version
                self._needs_resubscribe.update(self._streams)
                self._condition.notify_all()
            self._start_worker()
            self._wake.set()
            return client
        except Exception as exc:
            with self._condition:
                self._last_error = exc
            raise
        finally:
            self._attach_lock.release()

    def _detach(self, client=None, error=None):
        with self._condition:
            if client is not None and self._client is not client:
                return
            previous, self._client = self._client, None
            if error is not None:
                self._last_error = error
            for stream in self._streams.values():
                _invalidate_snapshot(stream)
            self._needs_resubscribe.update(self._streams)
            self._condition.notify_all()
        if previous is not None:
            previous.close()
        self._wake.set()

    def invalidate(self):
        """Discard local connection/state; a later observation may attach again."""
        self._detach()

    def status(self, force=False):
        checked = datetime.now(timezone.utc).isoformat(timespec='seconds')
        try:
            enabled = self._settings_enabled()
            project_ready, configured = self._project_status()
        except Exception as exc:
            return {'state': 'error', 'ready': False, 'message': '暂时无法读取 Codex 连接设置。',
                    'code': getattr(exc, 'code', 'desktop_settings_unavailable'), 'checked_at': checked,
                    'project_ready': False, 'mcp_ready': None, 'mcp_config_ready': False}
        base = {'checked_at': checked, 'project_ready': project_ready, 'mcp_ready': None,
                'mcp_config_ready': configured, 'mcp_status': 'configured_not_verified' if configured else 'not_configured',
                'desktop_version': self._last_version}
        if not enabled or self._closing:
            return {**base, 'state': 'disabled', 'ready': False, 'message': 'Codex 桌面连接未启用。', 'code': ''}
        try:
            client = self._attach(timeout=5)
            if force:
                # Read-only round trip: a missing synthetic thread is a valid router response.
                reply = client.request('thread-owner-discovery', {'hostId': 'local', 'conversationId': _CONTROL_THREAD}, 1, timeout=3)
                if reply.get('resultType') not in {'success', 'error'} or (
                        reply.get('resultType') == 'error' and reply.get('error') != 'no-client-found'):
                    raise IpcProtocolError('desktop_protocol_unsupported', '当前 Codex 桌面连接协议暂不兼容。')
            return {**base, 'desktop_version': client.installation.version, 'desktop_pid': client.server_pid,
                    'desktop_ready': True, 'state': 'ready' if project_ready else 'error', 'ready': project_ready,
                    'message': ('已连接，可以发送并在 Codex 中继续同一讨论。'
                                if project_ready else 'Codex 已打开。点击“连接 Codex”准备当前数据空间后即可发送。'),
                    'code': '' if project_ready else 'codex_project_incomplete'}
        except Exception as exc:
            if isinstance(exc, (IpcProtocolError, IpcIdentityError)):
                state = 'unsupported'
            elif isinstance(exc, IpcDisconnected):
                state = 'desktop_closed'
            else:
                state = 'disconnected'
            self._detach(error=exc)
            return {**base, 'state': state, 'ready': False, 'desktop_ready': False,
                    'message': ('Codex 未打开或连接已关闭。打开当前 Codex 后会自动连接，也可点击“连接 Codex”。'
                                if state == 'desktop_closed' else str(exc)),
                    'code': getattr(exc, 'code', 'desktop_connection_failed')}

    def connect(self):
        """Explicit user action: normal activation only when no pipe is available."""
        status = self.status(force=True)
        if status['state'] in {'disabled', 'unsupported'} or status.get('desktop_ready'):
            return status
        activate_codex()
        deadline = time.monotonic() + 15
        while not self._stop.is_set():
            status = self.status()
            if status.get('desktop_ready') or status['state'] in {'disabled', 'unsupported'}:
                return status
            if time.monotonic() >= deadline:
                return status
            self._stop.wait(0.2)
        return status

    def _require_client(self, timeout=10):
        if not self._settings_enabled():
            raise IpcError('desktop_disabled', '尚未启用 Codex 桌面连接。')
        return self._attach(timeout)

    def _owner_on_client(self, client, thread_id, open_if_missing, timeout):
        deadline = time.monotonic() + timeout
        opened = False
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IpcTimeout('desktop_owner_timeout', 'Codex 尚未接管此会话；没有发送新的任务。')
            reply = client.request('thread-owner-discovery', {'hostId': 'local', 'conversationId': thread_id},
                                   1, timeout=min(5, remaining))
            if reply.get('resultType') == 'success':
                owner = reply.get('handledByClientId')
                if (not isinstance(owner, str) or not 0 < len(owner) <= 256
                        or any(ord(c) < 32 for c in owner)
                        or not isinstance(reply.get('result'), dict)
                        or reply['result'].get('supportsUntrustedAppInput') is not True):
                    raise IpcProtocolError('desktop_protocol_unsupported', '当前 Codex 会话不支持独立客户端的任务输入。')
                return owner
            if reply.get('resultType') != 'error' or reply.get('error') != 'no-client-found':
                raise IpcProtocolError('desktop_protocol_unsupported', '当前 Codex 无法按已知协议确认会话归属。')
            if not open_if_missing:
                raise IpcError('desktop_owner_missing', 'Codex 桌面尚未打开此会话。')
            if not opened:
                _open_thread(thread_id)
                opened = True
            self._stop.wait(min(0.2, max(0, deadline - time.monotonic())))
        raise IpcDisconnected('desktop_gateway_closed', '软件的 Codex 连接已关闭。')

    def owner(self, thread_id, open_if_missing=True, timeout=15):
        thread_id = _thread_id(thread_id)
        deadline = time.monotonic() + timeout
        client = self._require_client(min(timeout, 10))
        return self._owner_on_client(client, thread_id, open_if_missing, max(0.001, deadline - time.monotonic()))

    def request_owner(self, thread_id, method, params, version, timeout=10):
        thread_id = _thread_id(thread_id)
        if not isinstance(params, dict):
            raise ValueError('params must be an object')
        for field in ('conversationId', 'threadId'):
            if field in params and params[field] != thread_id:
                raise IpcIdentityError('desktop_request_thread_mismatch', '请求内容与目标 Codex 会话不一致。')
        if 'hostId' in params and params['hostId'] != 'local':
            raise IpcIdentityError('desktop_request_host_mismatch', '此连接只接受本机 Codex 会话。')
        client = self._require_client(min(timeout, 10))
        owner = self._owner_on_client(client, thread_id, True, 15)
        try:
            # Exactly one mutation. Neither errors nor unknown acknowledgements
            # trigger rediscovery-and-resend; the caller must reconcile its journal.
            return client.request(method, params, version, target_client_id=owner, timeout=timeout)
        except IpcError as exc:
            if isinstance(exc, IpcDisconnected):
                self._detach(client, exc)
            raise

    def _subscribe_on_client(self, client, thread_id):
        owner = self._owner_on_client(client, thread_id, False, 5)
        with self._condition:
            if self._client is not client:
                raise IpcDisconnected('desktop_connection_changed', 'Codex 连接已变化，请重新读取会话状态。')
            stream = self._streams.get(thread_id)
            if stream is None or stream.owner_client_id != owner:
                if stream is not None:
                    _invalidate_snapshot(stream)
                stream = ThreadSnapshot(thread_id, 'local', owner)
                self._streams[thread_id] = stream
            self._needs_resubscribe.discard(thread_id)
        try:
            client.broadcast('thread-stream-following-changed',
                             {'conversationId': thread_id, 'hostId': 'local', 'following': True}, 1, [owner], timeout=3)
        except IpcError:
            with self._condition:
                self._needs_resubscribe.add(thread_id)
            raise
        return stream

    def subscribe(self, thread_id):
        thread_id = _thread_id(thread_id)
        client = self._require_client()
        owner = self._owner_on_client(client, thread_id, True, 15)
        with self._condition:
            if thread_id not in self._streams:
                if len(self._streams) >= MAX_SUBSCRIPTIONS:
                    raise IpcError('desktop_subscriptions_full', '同时跟踪的 Codex 会话已达上限。')
                self._streams[thread_id] = ThreadSnapshot(thread_id, 'local', owner)
        return self._subscribe_on_client(client, thread_id)

    def snapshot(self, thread_id, timeout=1):
        thread_id = _thread_id(thread_id)
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            if thread_id not in self._streams:
                return None
            while not self._closing:
                state = self._streams[thread_id].get_state()
                if state is not None:
                    return state
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
        return None

    def _event_loop(self):
        backoff = 0.5
        while not self._stop.is_set():
            with self._condition:
                enabled, client = self._enabled, self._client
            if not enabled:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            if client is None:
                try:
                    client = self._attach(3)
                    backoff = 0.5
                except Exception:
                    self._stop.wait(backoff)
                    backoff = min(10, backoff * 2)
                    continue
            try:
                try:
                    event = client.next_event(timeout=0.2)
                except IpcTimeout as exc:
                    if exc.code != 'ipc_event_timeout':
                        raise
                    event = None
                if event is not None:
                    self._event(client, event)
                with self._condition:
                    pending = [thread_id for thread_id in self._needs_resubscribe
                               if self._next_subscribe.get(thread_id, 0) <= time.monotonic()]
                for thread_id in pending:
                    try:
                        self._subscribe_on_client(client, thread_id)
                    except IpcError:
                        with self._condition:
                            self._next_subscribe[thread_id] = time.monotonic() + 2
            except IpcError as exc:
                self._detach(client, exc)
            except Exception:
                self._detach(client, IpcError('desktop_stream_failed', 'Codex 会话状态连接中断。'))

    def _event(self, client, event):
        if not isinstance(event, dict):
            raise IpcProtocolError('desktop_protocol_unsupported', 'Codex 返回了不支持的状态消息。')
        method, params = event.get('method'), event.get('params')
        if method == 'ipc-connection-reset':
            raise IpcDisconnected('desktop_connection_reset', 'Codex 桌面连接已重置。')
        if method == 'client-status-changed' and isinstance(params, dict):
            if params.get('status') == 'disconnected':
                with self._condition:
                    if self._client is client:
                        for thread_id, stream in self._streams.items():
                            if stream.owner_client_id == params.get('clientId'):
                                _invalidate_snapshot(stream)
                                self._needs_resubscribe.add(thread_id)
                        self._condition.notify_all()
            return
        if not isinstance(params, dict) or params.get('hostId') != 'local':
            return
        thread_id = params.get('conversationId')
        with self._condition:
            if self._client is not client or thread_id not in self._streams:
                return
            stream = self._streams[thread_id]
            if method == 'thread-stream-state-changed':
                if stream.accept(event) == 'resubscribe':
                    self._needs_resubscribe.add(thread_id)
                self._condition.notify_all()
            elif method == 'thread-stream-following-status-requested':
                self._needs_resubscribe.add(thread_id)

    def close(self):
        with self._condition:
            self._closing = True
            self._enabled = False
        self._stop.set()
        self._wake.set()
        self._detach()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2)
