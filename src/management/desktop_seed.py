"""Official App Server helper for configuration, empty seeds and history reads.

The normal desktop owns every model turn. This helper cannot resume an existing
thread or start a model. Its own child receives plain ``app-server`` arguments,
uses ordinary configuration layers, and is shut down through stdin EOF before a
new empty thread is handed to the desktop.
"""
from __future__ import annotations

import copy
import ctypes
import json
import os
from pathlib import Path
import queue
import re
import select
import subprocess
import threading
import time
from typing import Any, Callable, Sequence

from . import codex_installation

_READ_METHODS = frozenset({
    'config/read', 'configRequirements/read', 'permissionProfile/list',
    'model/list', 'mcpServerStatus/list', 'thread/read', 'thread/list',
    'thread/turns/list', 'thread/items/list', 'project/list', 'project/read',
})
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,199}\Z')


class DesktopSeedError(RuntimeError):
    """Safe metadata only; backend error text and transcripts are never logged."""

    def __init__(self, code: str, message: str, *, method: str | None = None,
                 thread_id: str | None = None, outcome_unknown: bool = False,
                 details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.method = method
        self.thread_id = thread_id
        self.outcome_unknown = outcome_unknown
        self.details = {**(details or {}), 'outcome_unknown': outcome_unknown}
        if method is not None:
            self.details['request_method'] = method
        if thread_id is not None:
            self.details['provider_thread_id'] = thread_id


def resolve_codex_cli(installation=None) -> str:
    """Use the current user's registered package, never an old cache by mtime."""
    installation = installation or codex_installation.discover_codex()
    try:
        root = Path(installation.install_location).resolve(strict=True)
        desktop = Path(installation.executable).resolve(strict=True)
        candidate = (desktop.parent / 'resources' / 'codex.exe').resolve(strict=True)
        if (not desktop.is_relative_to(root) or not candidate.is_relative_to(root)
                or not candidate.is_file()):
            raise ValueError()
    except (OSError, ValueError, TypeError) as exc:
        raise DesktopSeedError('seed_cli_unavailable',
            '当前注册的 Codex 安装中没有可确认的处理程序，请检查该安装。') from exc
    return str(candidate)


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


class PlainAppServer:
    """One private child for plain configuration and read-only history RPC.

    Context entry initializes automatically. ``command`` and ``environment``
    are explicit fixture/runtime injection seams; no command is run via a shell.
    The public request allowlist deliberately excludes every model/ownership
    mutation, including thread/resume and turn/start.
    """

    def __init__(self, workspace: str | Path, *, executable: str | None = None,
                 command: Sequence[str] | None = None, timeout: float = 30,
                 close_timeout: float = 5, cancel: threading.Event | None = None,
                 environment: dict[str, str] | None = None,
                 max_message_bytes: int = 8 * 1024 * 1024, max_queue: int = 128,
                 popen_factory: Callable | None = None):
        if executable is not None and command is not None:
            raise ValueError('Specify executable or command, not both')
        if timeout <= 0 or close_timeout <= 0:
            raise ValueError('Timeouts must be positive')
        if (type(max_message_bytes) is not int or max_message_bytes <= 0
                or type(max_queue) is not int or max_queue <= 0):
            raise ValueError('Message and queue bounds must be positive integers')
        if command is not None and (isinstance(command, (str, bytes)) or not command
                or any(not isinstance(part, str) or not part for part in command)):
            raise ValueError('The injected command must be an argv sequence')
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError('Workspace must be an existing directory')
        self.timeout, self.close_timeout = timeout, close_timeout
        self.cancel = cancel or threading.Event()
        self.max_message_bytes = max_message_bytes
        self.events: queue.Queue[dict] = queue.Queue(maxsize=max_queue)
        self._command = list(command) if command is not None else None
        self._executable = executable
        self._environment = dict(os.environ if environment is None else environment)
        for key in ('CODEX_THREAD_ID', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE',
                    'CODEX_MANAGED_BY_NPM', 'CODEX_CLI_PATH', 'PM_CODEX_BRIDGE_DIR'):
            self._environment.pop(key, None)
        self._popen = popen_factory or subprocess.Popen
        self._stop_reader = threading.Event()
        self._reader_error: DesktopSeedError | None = None
        self._request_lock = threading.Lock()
        self._closed = False
        self._initialized = False
        self._creation_attempted = False
        self._seq = 0
        self.process = None
        self.reader: threading.Thread | None = None
        self.created_thread_id: str | None = None
        self.close_forced = False
        self.process_exited = False

    def __enter__(self):
        self._ensure_started()
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.close()
        except DesktopSeedError:
            if exc is None:
                raise
        return False

    def _ensure_started(self):
        if self._closed:
            raise DesktopSeedError('seed_closed', '这次辅助连接已经关闭。')
        if self._initialized:
            return
        if self.process is not None:
            raise DesktopSeedError('seed_initialization_failed', '辅助连接尚未完成初始化。')
        command = self._command or [self._executable or resolve_codex_cli(), 'app-server']
        try:
            self.process = self._popen(command, cwd=self.workspace,
                env=self._environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
            # Python 3.12+ supports nonblocking Windows pipes. A stalled child
            # must not make a large configuration request bypass its deadline.
            os.set_blocking(self.process.stdin.fileno(), False)
            self.reader = threading.Thread(target=self._read,
                name='management-desktop-seed-reader', daemon=True)
            self.reader.start()
            self._exchange('initialize', {'clientInfo': {
                'name': 'personal_management_desktop_seed', 'version': '1'},
                'capabilities': {'experimentalApi': True}})
            self._write({'method': 'initialized'})
            self._initialized = True
        except BaseException as exc:
            try:
                self.close()
            except DesktopSeedError:
                pass
            if isinstance(exc, OSError):
                raise DesktopSeedError('seed_start_failed', '无法启动当前 Codex 的辅助连接。') from exc
            raise

    def _read_available(self, fd: int) -> bytes | None:
        """None means try again; b'' means EOF, without a buffered-reader lock."""
        if os.name == 'nt':
            import msvcrt
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            peek = kernel.PeekNamedPipe
            peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            peek.restype = wintypes.BOOL
            available = wintypes.DWORD()
            if not peek(msvcrt.get_osfhandle(fd), None, 0, None, ctypes.byref(available), None):
                if ctypes.get_last_error() in (6, 109, 232):
                    return b''
                raise OSError('Cannot read child output')
            if available.value:
                return os.read(fd, min(65536, available.value))
            return b'' if self.process.poll() is not None else None
        if select.select([fd], [], [], 0)[0]:
            return os.read(fd, 65536)
        return b'' if self.process.poll() is not None else None

    def _read(self):
        pending = bytearray()
        try:
            fd = self.process.stdout.fileno()
            while not self._stop_reader.is_set():
                chunk = self._read_available(fd)
                if chunk is None:
                    self._stop_reader.wait(.02)
                    continue
                if not chunk:
                    self._reader_error = DesktopSeedError('seed_disconnected', '辅助连接已结束。')
                    return
                pending.extend(chunk)
                while (end := pending.find(b'\n')) >= 0:
                    if end > self.max_message_bytes:
                        raise DesktopSeedError('seed_output_limit', 'Codex 返回内容超过读取上限。')
                    raw = bytes(pending[:end])
                    del pending[:end + 1]
                    message = json.loads(raw.decode('utf-8'),
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if type(message) is not dict:
                        raise ValueError()
                    try:
                        self.events.put_nowait(message)
                    except queue.Full:
                        raise DesktopSeedError('seed_output_limit', 'Codex 返回事件过多，已停止这次辅助读取。') from None
                if len(pending) > self.max_message_bytes:
                    raise DesktopSeedError('seed_output_limit', 'Codex 返回内容超过读取上限。')
        except DesktopSeedError as exc:
            self._reader_error = exc
        except (OSError, ValueError, TypeError, RecursionError):
            if not self._stop_reader.is_set():
                self._reader_error = DesktopSeedError('seed_protocol_error', 'Codex 返回的辅助连接消息无法确认。')

    def _write(self, message: dict) -> None:
        try:
            raw = (json.dumps(message, ensure_ascii=False, allow_nan=False,
                separators=(',', ':')) + '\n').encode('utf-8')
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise DesktopSeedError('seed_invalid_request', '辅助请求必须是有效 JSON。') from exc
        if len(raw) > self.max_message_bytes:
            raise DesktopSeedError('seed_input_limit', '辅助请求超过容量上限。')
        stream = self.process.stdin
        if stream is None or stream.closed:
            raise DesktopSeedError('seed_disconnected', '辅助连接无法继续接收请求。')
        sent_total = 0
        deadline = time.monotonic() + self.timeout
        try:
            remaining = memoryview(raw)
            while remaining:
                if self.cancel.is_set():
                    raise DesktopSeedError('seed_cancelled', '辅助连接请求已取消。',
                        outcome_unknown=sent_total > 0)
                if time.monotonic() >= deadline:
                    raise DesktopSeedError('seed_timeout', 'Codex 辅助请求的写入超过等待上限。',
                        outcome_unknown=sent_total > 0)
                try:
                    sent = os.write(stream.fileno(), remaining)
                except BlockingIOError:
                    self.cancel.wait(min(.02, max(0, deadline - time.monotonic())))
                    continue
                if not sent:
                    raise OSError('Child input closed')
                sent_total += sent
                remaining = remaining[sent:]
        except (OSError, ValueError) as exc:
            raise DesktopSeedError('seed_disconnected', '辅助请求的发送结果无法确认。',
                outcome_unknown=True) from exc

    def _exchange(self, method: str, params: dict) -> dict:
        with self._request_lock:
            self._seq += 1
            request_id = self._seq
            wrote = False
            try:
                if self.cancel.is_set():
                    raise DesktopSeedError('seed_cancelled', '辅助连接请求已取消。')
                deadline = time.monotonic() + self.timeout
                self._write({'id': request_id, 'method': method, 'params': params})
                wrote = True
                while True:
                    if self.cancel.is_set():
                        raise DesktopSeedError('seed_cancelled', '辅助连接请求已取消。')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DesktopSeedError('seed_timeout', 'Codex 辅助请求超过等待上限。')
                    try:
                        message = self.events.get(timeout=min(.05, remaining))
                    except queue.Empty:
                        if self._reader_error is not None:
                            raise self._reader_error
                        continue
                    if 'method' in message:
                        if 'id' in message:
                            raise DesktopSeedError('seed_unexpected_request', '这次只读辅助连接收到了需要交互的请求。')
                        continue
                    if message.get('id') != request_id:
                        continue
                    if 'error' in message:
                        error = message['error']
                        code = error.get('code') if type(error) is dict else None
                        raise DesktopSeedError('seed_request_rejected', 'Codex 未接受这次辅助请求。',
                            details={'protocol_code': code} if type(code) is int else None)
                    result = message.get('result')
                    if type(result) is not dict:
                        raise DesktopSeedError('seed_protocol_error', 'Codex 未返回有效的辅助请求回执。')
                    return result
            except DesktopSeedError as exc:
                unknown = method == 'thread/start' and (
                    exc.outcome_unknown or wrote and exc.code != 'seed_request_rejected')
                raise DesktopSeedError(exc.code, exc.message, method=method,
                    thread_id=self.created_thread_id, outcome_unknown=unknown,
                    details=exc.details) from exc

    def request(self, method: str, params: dict | None = None) -> dict:
        if method not in _READ_METHODS and method != 'config/value/write':
            raise DesktopSeedError('seed_method_forbidden', '这条辅助连接只开放配置与历史读取，不能发起或接管任务。', method=method)
        if params is not None and type(params) is not dict:
            raise DesktopSeedError('seed_invalid_request', '辅助请求参数必须是对象。', method=method)
        if method == 'config/value/write':
            expected_key = 'projects.' + json.dumps(str(self.workspace), ensure_ascii=False) + '.trust_level'
            if (not params or params.get('keyPath') != expected_key
                    or params.get('value') != 'trusted' or params.get('mergeStrategy') != 'replace'
                    or not isinstance(params.get('expectedVersion'), str) or not params['expectedVersion']
                    or not isinstance(params.get('filePath'), str)
                    or not Path(params['filePath']).is_absolute()):
                raise DesktopSeedError('seed_config_scope', '辅助连接只允许为当前专用工作区提交已核对版本的信任设置。', method=method)
        self._ensure_started()
        return self._exchange(method, params or {})

    def read_thread(self, thread_id: str, *, include_turns: bool = True) -> dict:
        if not _valid_id(thread_id):
            raise DesktopSeedError('seed_invalid_thread', '讨论标识无效。')
        return self.request('thread/read', {'threadId': thread_id, 'includeTurns': include_turns})

    def read_turns(self, thread_id: str, *, cursor: str | None = None, limit: int = 100) -> dict:
        if not _valid_id(thread_id) or type(limit) is not int or not 1 <= limit <= 100:
            raise DesktopSeedError('seed_invalid_request', '讨论分页读取参数无效。')
        return self.request('thread/turns/list', {'threadId': thread_id, 'cursor': cursor,
            'limit': limit, 'sortDirection': 'desc', 'itemsView': 'full'})

    def _create_empty(self, *, project_id: str | None, base_instructions: str | None,
                      model: str | None, on_created: Callable[[str], None],
                      title: str | None) -> tuple[dict, Path]:
        if self._creation_attempted:
            raise DesktopSeedError('seed_already_attempted', '这次会话创建已尝试，不能因结果未知而重复创建。',
                thread_id=self.created_thread_id)
        if not callable(on_created):
            raise ValueError('A durable on_created callback is required')
        self._ensure_started()
        self._creation_attempted = True
        params = {'cwd': str(self.workspace), 'ephemeral': False, 'historyMode': 'legacy',
            'environments': [], 'serviceName': 'personal_management_desktop_seed'}
        for name, value in (('projectId', project_id), ('baseInstructions', base_instructions), ('model', model)):
            if value is not None:
                if not isinstance(value, str) or not value:
                    raise DesktopSeedError('seed_invalid_request', '新讨论的配置字段无效。')
                params[name] = value
        result = self._exchange('thread/start', params)
        thread = result.get('thread')
        thread_id = thread.get('id') if type(thread) is dict else None
        if not _valid_id(thread_id):
            raise DesktopSeedError('seed_invalid_acknowledgement', 'Codex 已返回创建回执，但没有可持久保存的讨论标识。',
                method='thread/start', outcome_unknown=True)
        self.created_thread_id = thread_id
        # No naming, reading, or yielding before the caller durably binds the ID.
        try:
            on_created(thread_id)
        except Exception as exc:
            raise DesktopSeedError('seed_identity_not_saved', '讨论已创建，但关联标识未保存；不能重新创建。',
                method='thread/start', thread_id=thread_id) from exc
        if title is not None:
            if not isinstance(title, str) or not title.strip() or len(title) > 200:
                raise DesktopSeedError('seed_invalid_title', '讨论标题无效。', thread_id=thread_id)
            self._exchange('thread/name/set', {'threadId': thread_id, 'name': title})
        persisted = self.read_thread(thread_id, include_turns=True).get('thread')
        if (type(persisted) is not dict or persisted.get('id') != thread_id
                or type(persisted.get('turns')) is not list or persisted['turns']):
            raise DesktopSeedError('seed_persistence_unconfirmed', '空讨论的完整读取回执无法确认，暂不交给桌面。', thread_id=thread_id)
        path_value = persisted.get('path') or persisted.get('rolloutPath')
        if not isinstance(path_value, str) or not Path(path_value).is_absolute():
            raise DesktopSeedError('seed_persistence_unconfirmed', '空讨论尚未返回有效历史路径，暂不交给桌面。', thread_id=thread_id)
        return result, Path(path_value)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self.process
        if process is None:
            return
        error = None
        try:
            if process.stdin is not None:
                try:
                    process.stdin.close()  # EOF lets the owned server flush its rollout.
                except OSError:
                    pass
            try:
                process.wait(timeout=self.close_timeout)
            except subprocess.TimeoutExpired:
                self.close_forced = True
                # Only this exact Popen child is eligible for termination.
                process.terminate()
                try:
                    process.wait(timeout=min(2, self.close_timeout))
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=min(2, self.close_timeout))
            self.process_exited = process.poll() is not None
            if self.close_forced or process.returncode != 0:
                error = DesktopSeedError('seed_shutdown_incomplete',
                    '辅助连接未正常完成退出；已保留讨论标识，暂不交给桌面。',
                    thread_id=self.created_thread_id,
                    details={'forced': self.close_forced, 'process_exited': self.process_exited})
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.process_exited = process.poll() is not None
            error = DesktopSeedError('seed_shutdown_incomplete', '辅助连接退出尚未确认，暂不交给桌面。',
                thread_id=self.created_thread_id,
                details={'forced': self.close_forced, 'process_exited': self.process_exited})
        finally:
            self._stop_reader.set()
            if self.reader is not None:
                self.reader.join(timeout=2)
                if self.reader.is_alive():
                    error = DesktopSeedError('seed_reader_not_stopped', '辅助连接读取尚未结束，暂不交给桌面。',
                        thread_id=self.created_thread_id)
            # There is no blocking buffered readline lock to acquire here.
            if self.reader is None or not self.reader.is_alive():
                if process.stdout is not None:
                    process.stdout.close()
            while True:
                try:
                    self.events.get_nowait()
                except queue.Empty:
                    break
        if error is not None:
            raise error


def _verify_rollout(path: Path, thread_id: str, timeout: float) -> Path:
    """Inspect only the acknowledged seed's first metadata record, not history."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_file():
                raise ValueError()
            with resolved.open('rb') as stream:
                line = stream.readline(512 * 1024 + 1)
            if not line or len(line) > 512 * 1024:
                raise ValueError()
            record = json.loads(line)
            if record.get('type') != 'session_meta' or record.get('payload', {}).get('id') != thread_id:
                raise ValueError()
            return resolved
        except (OSError, ValueError, AttributeError, TypeError):
            if time.monotonic() >= deadline:
                raise DesktopSeedError('seed_persistence_unconfirmed',
                    '空讨论还没有可确认的持久历史，已保留关联并暂停交接。', thread_id=thread_id) from None
            time.sleep(min(.05, max(0, deadline - time.monotonic())))


def create_empty_thread(workspace: str | Path, project_id: str | None = None,
                        base_instructions: str | None = None, model: str | None = None,
                        on_created: Callable[[str], None] | None = None, *,
                        title: str | None = None, persistence_timeout: float = 2,
                        server_factory: Callable = PlainAppServer, **server_options) -> dict:
    """Create once, bind the real ID immediately, flush and verify before handoff.

    ``on_created(thread_id)`` must synchronously persist the acknowledged ID.
    An error after it runs does not authorize a second thread/start. The caller's
    dispatch journal must likewise forbid retries after an unknown start result.
    This function returns only after the seed writer has exited gracefully and
    the acknowledged rollout metadata exists. It never opens the desktop itself.
    """
    if not callable(on_created) or persistence_timeout <= 0:
        raise ValueError('A durable callback and positive persistence timeout are required')
    with server_factory(workspace, **server_options) as server:
        result, path = server._create_empty(project_id=project_id,
            base_instructions=base_instructions, model=model, on_created=on_created, title=title)
        thread_id = server.created_thread_id
    path = _verify_rollout(path, thread_id, persistence_timeout)
    return {**copy.deepcopy(result), 'thread_id': thread_id,
            'rollout_path': str(path), 'handoff_ready': True}
