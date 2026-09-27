"""An independently identified peer of the ordinary Windows Codex desktop.

The desktop owns this local pipe. This client verifies its registered package
before sending anything, identifies itself as personal-management, and declines
all incoming work discovery. It never impersonates desktop/tool-call clients.
Closing only releases our pipe; requests are never retried or interrupted here.
"""
from __future__ import annotations

from collections import deque
import ctypes
from ctypes import wintypes
import json
import math
import os
from pathlib import Path
import queue
import struct
import threading
import time
import uuid

from .codex_installation import CodexInstallation, discover_codex


PIPE_NAME = r'\\.\pipe\codex-ipc'
CLIENT_TYPE = 'personal-management'
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 128
MAX_EVENT_BYTES = 16 * 1024 * 1024
MAX_PENDING_REQUESTS = 32
READ_CHUNK_BYTES = 64 * 1024
_WINDOWS = os.name == 'nt'


class IpcError(RuntimeError):
    def __init__(self, code: str, message: str, *, request_id: str | None = None,
                 outcome_unknown: bool = False):
        super().__init__(message)
        self.code, self.message = code, message
        self.request_id, self.outcome_unknown = request_id, outcome_unknown


class IpcDisconnected(IpcError):
    pass


class IpcTimeout(IpcError):
    pass


class IpcProtocolError(IpcError):
    pass


class IpcIdentityError(IpcError):
    pass


def _seconds(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError('timeout must be greater than zero and at most 300 seconds')
    return float(timeout)


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _encode_frame(message: dict) -> bytes:
    if not isinstance(message, dict):
        raise IpcProtocolError('ipc_invalid_frame', 'Codex 连接消息必须是对象。')
    try:
        payload = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise IpcProtocolError('ipc_invalid_frame', 'Codex 连接消息无法编码。') from exc
    if not 0 < len(payload) <= MAX_FRAME_BYTES:
        raise IpcProtocolError('ipc_frame_too_large', 'Codex 连接消息超过大小限制。')
    return struct.pack('<I', len(payload)) + payload


class _FrameDecoder:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk: bytes):
        self.buffer.extend(chunk)
        while len(self.buffer) >= 4:
            length = struct.unpack_from('<I', self.buffer)[0]
            if not 0 < length <= MAX_FRAME_BYTES:
                raise IpcProtocolError('ipc_frame_too_large', 'Codex 返回了无效或过大的连接消息。')
            if len(self.buffer) < length + 4:
                return
            payload = bytes(self.buffer[4:4 + length])
            del self.buffer[:4 + length]
            try:
                def reject_constant(value):
                    raise ValueError('nonfinite JSON')
                message = json.loads(payload.decode('utf-8'), parse_constant=reject_constant)
                if not isinstance(message, dict):
                    raise ValueError('not an object')
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise IpcProtocolError('ipc_invalid_frame', 'Codex 返回了无法解析的连接消息。') from exc
            yield message, length


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]


def _kernel32():
    if not _WINDOWS:
        raise IpcError('ipc_windows_only', '此桌面连接只适用于 Windows。')
    library = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateFileW': ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
        'WaitNamedPipeW': ([wintypes.LPCWSTR, wintypes.DWORD], wintypes.BOOL),
        'CreateEventW': ([ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        'ReadFile': ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                      ctypes.POINTER(_OVERLAPPED)], wintypes.BOOL),
        'WriteFile': ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                       ctypes.POINTER(_OVERLAPPED)], wintypes.BOOL),
        'GetOverlappedResultEx': ([wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED), ctypes.POINTER(wintypes.DWORD),
                                    wintypes.DWORD, wintypes.BOOL], wintypes.BOOL),
        'CancelIoEx': ([wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)], wintypes.BOOL),
        'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        'GetNamedPipeServerProcessId': ([wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)], wintypes.BOOL),
        'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        'QueryFullProcessImageNameW': ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                       ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        'GetPackageFullName': ([wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR], wintypes.LONG),
    }
    for name, (args, result) in signatures.items():
        function = getattr(library, name)
        function.argtypes, function.restype = args, result
    return library


def _win_failure(code: int, action: str) -> IpcError:
    if code in {2, 6, 109, 232, 233, 995}:
        return IpcDisconnected('ipc_disconnected', f'Codex 桌面连接已断开（Windows {code}）。')
    return IpcError('ipc_io_failed', f'Codex 桌面连接{action}失败（Windows {code}）。')


class _WindowsNamedPipe:
    """One overlapped read and one overlapped write, cancellable on close."""
    def __init__(self, kernel, handle):
        self.kernel, self.handle = kernel, handle
        self._lock = threading.Lock()
        self._closed = False
        self._handle_closed = False
        self._active = 0

    @classmethod
    def open(cls, timeout: float):
        deadline = time.monotonic() + _seconds(timeout)
        kernel = _kernel32()
        while True:
            # FILE_FLAG_OVERLAPPED: synchronous WriteFile can otherwise wait forever.
            handle = kernel.CreateFileW(PIPE_NAME, 0xC0000000, 0, None, 3, 0x40000000, None)
            if handle != wintypes.HANDLE(-1).value:
                return cls(kernel, handle)
            error = ctypes.get_last_error()
            if error != 231:  # ERROR_PIPE_BUSY
                raise _win_failure(error, '打开')
            remaining = _remaining(deadline)
            if remaining <= 0:
                raise IpcTimeout('ipc_connect_timeout', '等待 Codex 桌面连接超时。')
            kernel.WaitNamedPipeW(PIPE_NAME, max(1, min(100, math.ceil(remaining * 1000))))

    def server_identity(self) -> dict:
        kernel = self.kernel
        pid = wintypes.ULONG()
        if not kernel.GetNamedPipeServerProcessId(self.handle, ctypes.byref(pid)):
            raise IpcIdentityError('ipc_identity_unavailable', '无法确认 Codex 连接所属进程。')
        process = kernel.OpenProcess(0x1000, False, pid.value)
        if not process:
            raise IpcIdentityError('ipc_identity_unavailable', '无法读取 Codex 连接所属进程。')
        try:
            path = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(path))
            if not kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                raise IpcIdentityError('ipc_identity_unavailable', '无法核对 Codex 桌面程序位置。')
            count = wintypes.UINT()
            result = kernel.GetPackageFullName(process, ctypes.byref(count), None)
            if result != 122 or not 0 < count.value <= 4096:
                raise IpcIdentityError('ipc_unregistered_server', '此 Codex 进程没有有效的 Windows 包身份，请使用正常桌面客户端。')
            package = ctypes.create_unicode_buffer(count.value)
            if kernel.GetPackageFullName(process, ctypes.byref(count), package) != 0:
                raise IpcIdentityError('ipc_identity_unavailable', '无法核对 Codex 的 Windows 包身份。')
            return {'pid': pid.value, 'executable': path.value, 'package_full_name': package.value}
        finally:
            kernel.CloseHandle(process)

    def _io(self, buffer, size: int, *, writing: bool, timeout: float) -> int:
        kernel = self.kernel
        event = kernel.CreateEventW(None, True, False, None)
        if not event:
            raise _win_failure(ctypes.get_last_error(), '准备')
        operation = _OVERLAPPED(hEvent=event)
        count = wintypes.DWORD()
        completed = True
        registered = False
        try:
            with self._lock:
                if self._closed:
                    raise IpcDisconnected('ipc_closed', 'Codex 桌面连接已关闭。')
                self._active += 1
                registered = True
                function = kernel.WriteFile if writing else kernel.ReadFile
                result = function(self.handle, buffer, size, ctypes.byref(count), ctypes.byref(operation))
                error = 0 if result else ctypes.get_last_error()
                completed = error != 997  # ERROR_IO_PENDING
            if completed:
                if not result:
                    raise _win_failure(error, '发送' if writing else '读取')
                return count.value
            result = kernel.GetOverlappedResultEx(self.handle, ctypes.byref(operation), ctypes.byref(count),
                                                   max(1, math.ceil(timeout * 1000)), False)
            error = 0 if result else ctypes.get_last_error()
            completed = error not in {258, 996}  # WAIT_TIMEOUT / ERROR_IO_INCOMPLETE
            if result:
                return count.value
            if not completed:
                kernel.CancelIoEx(self.handle, ctypes.byref(operation))
                # Cancellation is asynchronous. The buffer/OVERLAPPED must stay
                # alive until Windows signals completion, even after a timeout.
                result = kernel.GetOverlappedResultEx(self.handle, ctypes.byref(operation), ctypes.byref(count), 250, False)
                error = 0 if result else ctypes.get_last_error()
                completed = error not in {258, 996}
                if result:
                    return count.value  # Completion won the cancellation race.
                if not completed:
                    self.close()
                if error in {995, 258, 996}:
                    raise IpcTimeout('ipc_write_timeout' if writing else 'ipc_read_timeout',
                                     '发送到 Codex 超时，未自动重发。' if writing else '等待 Codex 数据超时。',
                                     outcome_unknown=writing)
            raise _win_failure(error, '发送' if writing else '读取')
        finally:
            if completed:
                kernel.CloseHandle(event)
                if registered:
                    self._finish_operation()
            else:
                # A faulty/stalled driver must not block the caller or leave
                # Python freeing memory which the kernel still owns. At most
                # two retained operations exist for this now-closed transport.
                def release_when_finished(kept_buffer=buffer, kept_operation=operation):
                    kernel.WaitForSingleObject(kept_operation.hEvent, 0xffffffff)
                    kernel.CloseHandle(kept_operation.hEvent)
                    self._finish_operation()
                threading.Thread(target=release_when_finished, name='codex-ipc-io-cleanup', daemon=True).start()

    def _finish_operation(self):
        with self._lock:
            self._active -= 1
            if self._closed and not self._active and not self._handle_closed:
                self.kernel.CloseHandle(self.handle)
                self._handle_closed = True

    def read(self, timeout: float) -> bytes:
        buffer = ctypes.create_string_buffer(READ_CHUNK_BYTES)
        count = self._io(buffer, len(buffer), writing=False, timeout=_seconds(timeout))
        if not count:
            raise IpcDisconnected('ipc_eof', 'Codex 桌面已关闭连接。')
        return buffer.raw[:count]

    def write(self, frame: bytes, timeout: float):
        deadline = time.monotonic() + _seconds(timeout)
        offset = 0
        while offset < len(frame):
            remaining = _remaining(deadline)
            if not remaining:
                raise IpcTimeout('ipc_write_timeout', '发送到 Codex 超时，未自动重发。', outcome_unknown=True)
            buffer = ctypes.create_string_buffer(frame[offset:])
            count = self._io(buffer, len(frame) - offset, writing=True, timeout=remaining)
            if not count:
                raise IpcDisconnected('ipc_eof', 'Codex 桌面已关闭连接。', outcome_unknown=offset > 0)
            offset += count

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.kernel.CancelIoEx(self.handle, None)
            if not self._active:
                self.kernel.CloseHandle(self.handle)
                self._handle_closed = True


def _verify_identity(identity: dict, installation: CodexInstallation):
    try:
        matches = (type(identity['pid']) is int and identity['pid'] > 0
                   and identity['package_full_name'] == installation.package_full_name
                   and Path(identity['executable']).resolve() == Path(installation.executable).resolve())
    except (ValueError, KeyError, TypeError, OSError):
        matches = False
    if not matches:
        raise IpcIdentityError('ipc_server_mismatch', '桌面连接所属程序与当前注册的 Codex 不一致。')


def _identifier(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and all(ord(char) >= 32 for char in value)


class NativeDesktopIpc:
    """Thread-safe request matching; response envelopes retain routing metadata.

    A remote resultType='error' is returned to the caller (for example, owner
    discovery's no-client-found). Transport/timeout/identity failures raise an
    IpcError. No request or broadcast body is printed or persisted by this class.
    """
    def __init__(self, transport, installation: CodexInstallation, identity: dict):
        self._transport = transport
        self.installation, self._server_identity = installation, dict(identity)
        self._client_id = None
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._pending = {}
        self._events = deque()
        self._event_bytes = 0
        self._failure = None
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name='codex-ipc-reader', daemon=True)

    @property
    def client_id(self):
        return self._client_id

    @property
    def server_identity(self):
        return dict(self._server_identity)

    @property
    def server_pid(self):
        return self._server_identity['pid']

    @classmethod
    def connect(cls, installation: CodexInstallation | None = None, timeout: float = 10):
        deadline = time.monotonic() + _seconds(timeout)
        current = discover_codex()
        if installation is not None and installation != current:
            raise IpcIdentityError('ipc_installation_changed', 'Codex 安装已变化，请重新读取当前客户端。')
        if not _remaining(deadline):
            raise IpcTimeout('ipc_connect_timeout', '读取 Codex 注册信息后连接已超时。')
        transport = _WindowsNamedPipe.open(_remaining(deadline))
        client = None
        try:
            identity = transport.server_identity()
            _verify_identity(identity, current)
            client = cls(transport, current, identity)
            client._reader.start()
            if not _remaining(deadline):
                raise IpcTimeout('ipc_connect_timeout', '核对 Codex 桌面身份后连接已超时。')
            result = client._request('initialize', {'clientType': CLIENT_TYPE}, 0, None,
                                     _remaining(deadline), initializing=True)
            assigned = result.get('result', {}).get('clientId') if isinstance(result.get('result'), dict) else None
            if result.get('resultType') != 'success' or not _identifier(assigned):
                raise IpcProtocolError('ipc_initialize_failed', 'Codex 未分配有效的独立连接身份。')
            client._client_id = assigned
            return client
        except BaseException:
            if client is not None:
                client.close()
            else:
                transport.close()
            raise

    def _send(self, message: dict, deadline: float):
        frame = _encode_frame(message)
        if not self._write_lock.acquire(timeout=_remaining(deadline)):
            raise IpcTimeout('ipc_send_busy', '等待发送 Codex 消息超时。')
        try:
            if self._stop.is_set():
                raise self._failure or IpcDisconnected('ipc_closed', 'Codex 桌面连接已关闭。')
            remaining = _remaining(deadline)
            if not remaining:
                raise IpcTimeout('ipc_send_timeout', '发送 Codex 消息前已超时。')
            try:
                self._transport.write(frame, remaining)
            except IpcError as exc:
                # A partial frame must never be reused as a healthy connection.
                unknown = type(exc)(exc.code, exc.message, outcome_unknown=True)
                self._fail(unknown)
                raise unknown from exc
        finally:
            self._write_lock.release()

    def request(self, method: str, params: dict, version: int, target_client_id: str | None = None,
                timeout: float = 10) -> dict:
        if method == 'initialize':
            raise IpcProtocolError('ipc_reserved_method', '连接初始化由客户端统一完成。')
        return self._request(method, params, version, target_client_id, timeout)

    def broadcast(self, method: str, params: dict, version: int, target_client_ids: list[str],
                  timeout: float = 10):
        """Send once to caller-selected real owners, without claiming a receipt.

        Callers must obtain targets by owner discovery. Unsubscription is an
        explicit upper-layer broadcast; close() never sends one or interrupts.
        """
        deadline = time.monotonic() + _seconds(timeout)
        if self.client_id is None:
            raise IpcProtocolError('ipc_not_initialized', 'Codex 桌面连接尚未初始化。')
        if (not _identifier(method) or not isinstance(params, dict) or type(version) is not int
                or not 0 <= version <= 65535 or not isinstance(target_client_ids, list)
                or not 0 < len(target_client_ids) <= 32
                or any(not _identifier(target) for target in target_client_ids)
                or len(set(target_client_ids)) != len(target_client_ids)):
            raise ValueError('broadcast requires explicit version and distinct owner client IDs')
        self._send({'type': 'broadcast', 'sourceClientId': self.client_id, 'method': method,
                    'params': params, 'version': version, 'targetClientIds': list(target_client_ids)}, deadline)

    def _request(self, method, params, version, target_client_id, timeout, *, initializing=False):
        deadline = time.monotonic() + _seconds(timeout)
        if not _identifier(method) or not isinstance(params, dict) or type(version) is not int or not 0 <= version <= 65535:
            raise ValueError('request requires a method, object params, and an explicit protocol version')
        if target_client_id is not None and not _identifier(target_client_id):
            raise ValueError('invalid target client ID')
        if self.client_id is None and not initializing:
            raise IpcProtocolError('ipc_not_initialized', 'Codex 桌面连接尚未初始化。')
        request_id = str(uuid.uuid4())
        response_queue = queue.Queue(maxsize=1)
        with self._condition:
            if self._failure:
                raise self._failure
            if len(self._pending) >= MAX_PENDING_REQUESTS:
                raise IpcError('ipc_requests_full', '等待 Codex 的请求过多，请先等待已有请求完成。')
            self._pending[request_id] = response_queue
        message = {'type': 'request', 'requestId': request_id,
                   'sourceClientId': 'initializing-client' if initializing else self.client_id,
                   'version': version, 'method': method, 'params': params,
                   'timeoutMs': max(1, math.ceil(timeout * 1000))}
        if target_client_id is not None:
            message['targetClientId'] = target_client_id
        sent = False
        try:
            self._send(message, deadline)
            sent = True
            try:
                response = response_queue.get(timeout=_remaining(deadline))
            except queue.Empty:
                raise IpcTimeout('ipc_response_timeout', 'Codex 未在时限内确认请求；结果未知，未自动重发。',
                                 request_id=request_id, outcome_unknown=True) from None
            if isinstance(response, IpcError):
                raise response
            return response
        except IpcError as exc:
            raise type(exc)(exc.code, exc.message, request_id=request_id,
                            outcome_unknown=sent or exc.outcome_unknown) from exc
        finally:
            with self._condition:
                self._pending.pop(request_id, None)

    def _receive(self, message: dict, size: int):
        kind = message.get('type')
        if kind == 'client-discovery-request':
            request_id = message.get('requestId')
            if not _identifier(request_id):
                raise IpcProtocolError('ipc_invalid_discovery', 'Codex 返回了无效的能力查询。')
            self._send({'type': 'client-discovery-response', 'requestId': request_id,
                        'response': {'canHandle': False}}, time.monotonic() + 1)
        elif kind == 'response':
            request_id = message.get('requestId')
            if not _identifier(request_id):
                raise IpcProtocolError('ipc_invalid_response', 'Codex 返回了无效的请求回执。')
            with self._condition:
                target = self._pending.get(request_id)
                if target is not None:
                    try:
                        target.put_nowait(message)
                    except queue.Full:
                        pass  # A duplicate response cannot replace the first one.
        elif kind == 'broadcast':
            with self._condition:
                if len(self._events) >= MAX_EVENTS or self._event_bytes + size > MAX_EVENT_BYTES:
                    raise IpcProtocolError('ipc_events_full', 'Codex 事件积压已达到上限；连接已关闭，请重新核对状态。')
                self._events.append((message, size))
                self._event_bytes += size
                self._condition.notify_all()
        else:
            raise IpcProtocolError('ipc_unknown_message', 'Codex 返回了当前客户端不支持的连接消息。')

    def _read_loop(self):
        decoder = _FrameDecoder()
        try:
            while not self._stop.is_set():
                try:
                    chunk = self._transport.read(0.25)
                except IpcTimeout as exc:
                    if exc.code == 'ipc_read_timeout':
                        continue
                    raise
                if not chunk:
                    raise IpcDisconnected('ipc_eof', 'Codex 桌面已关闭连接。')
                for message, size in decoder.feed(chunk):
                    self._receive(message, size)
        except IpcError as exc:
            if isinstance(exc, IpcDisconnected) and decoder.buffer:
                self._fail(IpcProtocolError('ipc_truncated_frame', 'Codex 连接在消息接收完成前断开。'))
            else:
                self._fail(exc)
        except Exception:
            self._fail(IpcError('ipc_reader_failed', '读取 Codex 桌面连接失败。'))

    def _fail(self, error: IpcError):
        with self._condition:
            if self._failure is None:
                self._failure = error
            self._stop.set()
            self._events.clear()
            self._event_bytes = 0
            for target in self._pending.values():
                try:
                    target.put_nowait(self._failure)
                except queue.Full:
                    pass
            self._condition.notify_all()
        self._transport.close()

    def next_event(self, timeout: float = 10) -> dict:
        deadline = time.monotonic() + _seconds(timeout)
        with self._condition:
            while not self._events:
                if self._failure:
                    raise self._failure
                remaining = _remaining(deadline)
                if not remaining:
                    raise IpcTimeout('ipc_event_timeout', '等待 Codex 事件超时。')
                self._condition.wait(remaining)
            message, size = self._events.popleft()
            self._event_bytes -= size
            return message

    def close(self):
        self._fail(IpcDisconnected('ipc_closed', 'Codex 桌面连接已关闭。'))
        if self._reader.is_alive() and threading.current_thread() is not self._reader:
            self._reader.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
