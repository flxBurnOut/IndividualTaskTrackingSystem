"""Transparent desktop stdio adapter to an authenticated local Codex engine.

This executable is opt-in through a dedicated desktop launcher. It does not
modify Codex files or impersonate a desktop tool executor.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import queue
from http import HTTPStatus
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid

import psutil

MAX_FRAME = 4 * 1024 * 1024
MAX_RUNTIME = 16 * 1024
_VALUE_OPTIONS = {'-c', '--config', '--enable', '--disable', '-p', '--profile', '-C', '--cd',
                  '-m', '--model', '--remote', '--remote-auth-token-env', '--code-mode-host'}
_FLAG_OPTIONS = {'--strict-config', '--analytics-default-enabled', '--search', '--no-daemon',
                 '--approve-for-me', '--dangerously-bypass-approvals-and-sandbox',
                 '--dangerously-bypass-hook-trust'}


class ShimError(RuntimeError):
    pass


def engine_arguments(argv):
    """Return unchanged non-transport args only for a plain stdio app-server."""
    args = list(argv)
    command = None
    remove = set()
    transport_count = 0
    index = 0
    while index < len(args):
        value = args[index]
        if value in {'-h', '--help', '-V', '--version'}:
            return None
        if value in _VALUE_OPTIONS:
            if index + 1 >= len(args):
                return None
            index += 2
            continue
        if value.startswith('--') and '=' in value:
            name, content = value.split('=', 1)
            if name in _VALUE_OPTIONS:
                index += 1
                continue
            if name == '--listen' and command is not None and content == 'stdio://':
                transport_count += 1
                remove.add(index)
                index += 1
                continue
            return None
        if value.startswith('-c') and len(value) > 2:
            index += 1
            continue
        if value in _FLAG_OPTIONS:
            index += 1
            continue
        if command is not None and value == '--stdio':
            transport_count += 1
            remove.add(index)
            index += 1
            continue
        if command is not None and value == '--listen':
            if index + 1 >= len(args) or args[index + 1] != 'stdio://':
                return None
            transport_count += 1
            remove.update((index, index + 1))
            index += 2
            continue
        if command is None and value == 'app-server':
            command = index
            index += 1
            continue
        return None
    if command is None or transport_count > 1:
        return None
    return [value for index, value in enumerate(args) if index not in remove]


def _absolute_path(value, label):
    if not value or not Path(value).is_absolute():
        raise ShimError(label + ' must be an absolute path')
    path = Path(value)
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise ShimError(label + ' cannot contain links')
    return path.resolve()


def _configuration(env):
    executable = _absolute_path(env.get('PM_CODEX_REAL_CLI'), 'Real Codex executable')
    if not executable.is_file() or os.path.normcase(str(executable)) == os.path.normcase(str(Path(sys.executable).resolve())):
        raise ShimError('Real Codex executable is missing or points to this adapter')
    directory = _absolute_path(env.get('PM_CODEX_BRIDGE_DIR'), 'Bridge directory')
    return executable, directory


def _kernel():
    if os.name != 'nt':
        raise ShimError('Desktop bridge currently requires Windows')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    return kernel


def _win_error(message):
    # Error numbers are safe diagnostics; no path, bearer or protocol content.
    return ShimError(f'{message} (Windows error {ctypes.get_last_error()})')


def _secure_directory(directory):
    """Protect discovery secrets with a current-user/SYSTEM-only inherited DACL."""
    directory.mkdir(parents=True, exist_ok=True)
    _absolute_path(str(directory), 'Bridge directory')
    kernel = _kernel()
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    token = wintypes.HANDLE()
    descriptor = ctypes.c_void_p()
    sid_text = wintypes.LPWSTR()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise _win_error('Cannot read current user identity')
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value or needed.value > MAX_RUNTIME:
            raise ShimError('Invalid current user identity size')
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise _win_error('Cannot read current user identity')
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise _win_error('Cannot identify current user')
        sddl = f'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{sid_text.value})'
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise _win_error('Cannot prepare private bridge permissions')
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present:
            raise _win_error('Cannot read private bridge permissions')
        result = advapi.SetNamedSecurityInfoW(str(directory), 1, 0x80000004, None, None, dacl, None)
        if result:
            raise ShimError(f'Cannot protect bridge directory (Windows error {result})')
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        if token:
            kernel.CloseHandle(token)


class _DirectoryLock:
    def __init__(self, directory):
        self.handle = None
        path = directory / 'desktop-engine.lock'
        _absolute_path(str(path), 'Bridge lock')
        import msvcrt
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o600)
        handle = os.fdopen(descriptor, 'r+b', buffering=0)
        try:
            if os.fstat(descriptor).st_size == 0:
                handle.write(b'0')
            handle.seek(0)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except Exception:
            handle.close()
            raise ShimError('Another desktop bridge already owns this directory') from None
        self.handle = handle

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class _Job:
    def __init__(self):
        self.kernel = _kernel()
        class BASIC(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class EXTENDED(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BASIC), ('IoInfo', IO), ('ProcessMemoryLimit', ctypes.c_size_t),
                        ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise _win_error('Cannot create engine lifecycle job')
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise _win_error('Cannot protect engine lifecycle')

    def assign_and_resume(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise _win_error('Cannot attach engine to lifecycle job')
        class THREADENTRY32(ctypes.Structure):
            _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ThreadID', wintypes.DWORD),
                        ('th32OwnerProcessID', wintypes.DWORD), ('tpBasePri', wintypes.LONG),
                        ('tpDeltaPri', wintypes.LONG), ('dwFlags', wintypes.DWORD)]
        self.kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
        self.kernel.Thread32First.restype = wintypes.BOOL
        self.kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
        self.kernel.Thread32Next.restype = wintypes.BOOL
        self.kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenThread.restype = wintypes.HANDLE
        self.kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        self.kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = self.kernel.CreateToolhelp32Snapshot(4, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise _win_error('Cannot locate suspended engine thread')
        found = False
        try:
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(entry)
            valid = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
            while valid:
                if entry.th32OwnerProcessID == process.pid:
                    thread = self.kernel.OpenThread(2, False, entry.th32ThreadID)
                    if not thread:
                        raise _win_error('Cannot open suspended engine thread')
                    try:
                        if self.kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise _win_error('Cannot resume engine')
                        found = True
                    finally:
                        self.kernel.CloseHandle(thread)
                valid = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self.kernel.CloseHandle(snapshot)
        if not found:
            raise ShimError('Suspended engine has no primary thread')

    def close(self):
        handle, self.handle = getattr(self, 'handle', None), None
        if handle:
            self.kernel.CloseHandle(handle)


class _ParentWatch:
    def __init__(self):
        self.parent = psutil.Process(os.getppid())
        self.created = self.parent.create_time()

    def alive(self):
        try:
            return self.parent.is_running() and self.parent.create_time() == self.created
        except psutil.Error:
            return False


def _start_engine(executable, arguments):
    job = _Job()
    process = None
    try:
        process = subprocess.Popen([str(executable), *arguments], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   creationflags=0x00000004 | subprocess.CREATE_NO_WINDOW,
                                   env=dict(os.environ), bufsize=0)
        job.assign_and_resume(process)
        return process, job
    except BaseException:
        try:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        finally:
            job.close()
        raise


def _read_runtime(directory):
    path = directory / 'runtime.json'
    try:
        _absolute_path(str(path), 'Runtime record')
        if path.stat().st_size > MAX_RUNTIME:
            return None
        with path.open('rb') as handle:
            raw = handle.read(MAX_RUNTIME + 1)
        if len(raw) > MAX_RUNTIME:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _write_runtime(directory, record):
    _absolute_path(str(directory / 'runtime.json'), 'Runtime record')
    raw = json.dumps(record, ensure_ascii=False).encode('utf-8')
    if len(raw) > MAX_RUNTIME:
        raise ShimError('Bridge runtime record is too large')
    temporary = directory / ('runtime.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / 'runtime.json')
    finally:
        temporary.unlink(missing_ok=True)


def _remove_runtime(directory, record):
    current = _read_runtime(directory)
    if current and current.get('pid') == record.get('pid') and current.get('instance_id') == record.get('instance_id'):
        (directory / 'runtime.json').unlink(missing_ok=True)


def _runtime_record(executable, process, endpoint, token):
    current = psutil.Process()
    return {'schema_version': 1, 'pid': current.pid, 'executable': str(Path(current.exe()).resolve()),
            'shim_create_time': current.create_time(), 'engine_pid': process.pid,
            'engine_executable': str(executable), 'engine_create_time': psutil.Process(process.pid).create_time(),
            'created_at': datetime.now(timezone.utc).isoformat(), 'endpoint': endpoint, 'token': token,
            'instance_id': uuid.uuid4().hex}


def _endpoint():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if os.name == 'nt':
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(('127.0.0.1', 0))
        return f'ws://127.0.0.1:{listener.getsockname()[1]}'


def _diagnostic(message, token=None, stream=None):
    text = str(message)
    if token:
        text = text.replace(token, '[redacted]')
    target = stream if stream is not None else sys.stderr
    try:
        target.write('Codex desktop bridge: ' + text[:4096] + '\n')
        target.flush()
    except (OSError, ValueError):
        pass


def _forward_stderr(source, token, stop):
    # Retain enough suffix to redact a bearer split across pipe reads.
    pending = ''
    import codecs
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    try:
        while True:
            chunk = source.read(4096)
            if not chunk:
                break
            pending += decoder.decode(chunk)
            pending = pending.replace(token, '[redacted]')
            boundary = max(0, len(pending) - len(token))
            if boundary:
                sys.stderr.write(pending[:boundary])
                sys.stderr.flush()
                pending = pending[boundary:]
        pending += decoder.decode(b'', final=True)
        if pending:
            sys.stderr.write(pending.replace(token, '[redacted]'))
            sys.stderr.flush()
    except (OSError, ValueError):
        if not stop.is_set():
            _diagnostic('Engine diagnostics pipe closed')


def _connect(endpoint, token, process, parent, stop):
    from websockets.sync.client import connect
    logger = logging.getLogger('personal_management.codex_desktop_transport')
    logger.disabled = True
    deadline = time.monotonic() + 20
    while not stop.is_set() and time.monotonic() < deadline:
        if process.poll() is not None:
            raise ShimError('Codex engine exited before accepting the desktop connection')
        if not parent.alive():
            raise ShimError('Desktop parent closed during connection setup')
        try:
            return connect(endpoint, additional_headers={'Authorization': 'Bearer ' + token},
                           open_timeout=1, close_timeout=.5, max_size=MAX_FRAME, max_queue=4,
                           proxy=None, compression=None, logger=logger)
        except (OSError, TimeoutError):
            stop.wait(.1)
    raise ShimError('Timed out waiting for the authenticated Codex engine')


def _desktop_lines(stream, stop):
    """Read desktop pipe frames without holding Python's stdin buffer lock.

    The desktop may keep its write handle open after the engine disconnects.
    Peek before raw reads so cancellation never depends on desktop stdin EOF.
    In-memory streams used by protocol tests keep their normal line reader.
    """
    descriptor = None
    if os.name == 'nt':
        try:
            descriptor = stream.fileno()
        except (AttributeError, OSError, ValueError):
            pass
    if descriptor is not None:
        import msvcrt
        handle = msvcrt.get_osfhandle(descriptor)
        kernel = _kernel()
        kernel.GetFileType.argtypes = [wintypes.HANDLE]
        kernel.GetFileType.restype = wintypes.DWORD
        if kernel.GetFileType(handle) != 3:  # FILE_TYPE_PIPE
            descriptor = None
    if descriptor is None:
        while not stop.is_set():
            raw = stream.readline(MAX_FRAME + 2)
            yield raw
            if not raw:
                return
        return

    kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.PeekNamedPipe.restype = wintypes.BOOL
    pending = bytearray()
    while not stop.is_set():
        newline = pending.find(b'\n')
        if newline >= 0 or len(pending) >= MAX_FRAME + 2:
            end = min(newline + 1, MAX_FRAME + 2) if newline >= 0 else len(pending)
            raw = bytes(pending[:end])
            del pending[:end]
            yield raw
            continue
        available = wintypes.DWORD()
        if not kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            if ctypes.get_last_error() in {109, 232}:  # Broken pipe / no data.
                yield bytes(pending)
                return
            raise _win_error('Cannot read desktop input pipe')
        if not available.value:
            stop.wait(.05)
            continue
        # This is the sole stdin reader; read only bytes already in the pipe.
        chunk = os.read(descriptor, min(available.value, 65536, MAX_FRAME + 2 - len(pending)))
        if not chunk:
            yield bytes(pending)
            return
        pending.extend(chunk)


class _Relay:
    """Multiplex clients over one engine subscription without claiming tool identity."""
    ALLOWED_MANAGEMENT = {'thread/start', 'thread/resume', 'thread/read', 'thread/unsubscribe', 'thread/turns/list', 'thread/compact/start', 'thread/name/set',
                          'thread/metadata/update', 'turn/start', 'turn/interrupt'}
    MAX_PENDING = 512
    MAX_MANAGEMENT = 4
    MAX_QUEUED_BYTES = 16 * 1024 * 1024
    MAX_MANAGEMENT_QUEUED_BYTES = 8 * 1024 * 1024
    OUTPUT_BACKPRESSURE_SECONDS = 5.0

    def __init__(self, engine, process, parent, token, on_ready, stdin, stdout):
        self.engine, self.process, self.parent = engine, process, parent
        self.token, self.on_ready, self.stdin, self.stdout = token, on_ready, stdin, stdout
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.space = threading.Condition(self.lock)
        self.pending = {}
        self.server_pending = {}
        self.owners = {}
        self.turn_owners = {}
        self.pending_turn_owners = {}
        self.compacting_threads = set()
        self.actors = {}
        self.sequence = 0
        self.queued_bytes = 0
        self.management_queued_bytes = 0
        self.initialize_result = None
        self.desktop_capabilities = {}
        self.desktop_initialized = False
        self.published = False
        self.error = None
        self.front = None
        self.endpoint = None

    def fail(self, message):
        with self.lock:
            self.error = self.error or message
        self.shutdown()

    def shutdown(self):
        if not self.stop.is_set():
            self.stop.set()
            try:
                self.engine.close()
            except Exception:
                pass

    def add_actor(self, name, send, close):
        actor = {'name': name, 'queue': queue.Queue(maxsize=128), 'send': send, 'close': close, 'active': True,
                 'initialized': name == 'desktop'}
        with self.lock:
            self.actors[name] = actor
        threading.Thread(target=self.write_actor, args=(actor,), daemon=True, name='codex-bridge-writer').start()
        return actor

    def _account_queue(self, actor, amount):
        self.queued_bytes += amount
        if actor['name'] != 'desktop':
            self.management_queued_bytes += amount
        if amount < 0:
            self.space.notify_all()

    def drop_actor(self, actor):
        with self.lock:
            if not actor['active']:
                return
            actor['active'] = False
            self.actors.pop(actor['name'], None)
            self.pending = {key: value for key, value in self.pending.items() if value[0] is not actor or value[2] in {'turn/start','thread/compact/start'}}
            abandoned = [key for key, owner in self.server_pending.items() if owner is actor]
            for key in abandoned:
                self.server_pending.pop(key, None)
            active_threads = {key[0] for key, owner in self.turn_owners.items() if owner is actor}
            active_threads.update(key for key, owner in self.pending_turn_owners.items() if owner is actor)
            self.owners = {key: owner for key, owner in self.owners.items()
                           if owner is not actor or key in active_threads}
            # Keep inactive turn ownership until turn/completed: a later tool
            # request from this same turn must fail, never move to desktop.
            self.space.notify_all()
        if actor['name'] != 'desktop':
            for _, ident in abandoned:
                try:
                    self.send_engine({'id': ident, 'error': {'code': -32000,
                                      'message': 'Management connection closed before responding'}})
                except Exception:
                    pass
        try:
            actor['close']()
        except Exception:
            pass
        while True:
            try:
                _, size = actor['queue'].get_nowait()
            except queue.Empty:
                break
            with self.lock:
                self._account_queue(actor, -size)

    def enqueue(self, actor, message):
        text = json.dumps(message, ensure_ascii=False, separators=(',', ':'))
        size = len(text.encode('utf-8'))
        desktop = actor['name'] == 'desktop'
        deadline = time.monotonic() + self.OUTPUT_BACKPRESSURE_SECONDS
        with self.space:
            while actor['active'] and not self.stop.is_set():
                overflow = size > MAX_FRAME or self.queued_bytes + size > self.MAX_QUEUED_BYTES
                if not desktop:
                    overflow = overflow or self.management_queued_bytes + size > self.MAX_MANAGEMENT_QUEUED_BYTES
                if not overflow:
                    try:
                        actor['queue'].put_nowait((text, size))
                        self._account_queue(actor, size)
                        return
                    except queue.Full:
                        pass
                remaining = deadline - time.monotonic()
                if not desktop or size > MAX_FRAME or remaining <= 0:
                    break
                self.space.wait(timeout=min(.05, remaining))
            else:
                return
        if desktop:
            self.fail('Desktop output remained blocked beyond the bounded backpressure window')
        else:
            self.drop_actor(actor)

    def write_actor(self, actor):
        while not self.stop.is_set() and actor['active']:
            try:
                text, size = actor['queue'].get(timeout=.1)
            except queue.Empty:
                continue
            try:
                actor['send'](text)
            except Exception:
                if actor['name'] == 'desktop':
                    self.fail('Desktop output pipe closed')
                else:
                    self.drop_actor(actor)
                return
            finally:
                with self.lock:
                    self._account_queue(actor, -size)

    @staticmethod
    def decode(frame):
        if isinstance(frame, bytes):
            frame = frame.decode('utf-8')
        if not isinstance(frame, str) or len(frame.encode('utf-8')) > MAX_FRAME:
            raise ShimError('Protocol frame exceeded the limit')
        message = json.loads(frame)
        if not isinstance(message, dict):
            raise ShimError('Protocol frame must be an object')
        if 'id' in message and (not isinstance(message['id'], (int, str)) or isinstance(message['id'], bool)):
            raise ShimError('Invalid protocol request identifier')
        return message

    @staticmethod
    def id_key(ident):
        return type(ident).__name__, ident

    def send_engine(self, message):
        encoded = json.dumps(message, ensure_ascii=False, separators=(',', ':'))
        if len(encoded.encode('utf-8')) > MAX_FRAME:
            raise ShimError('Protocol request exceeded the frame limit')
        self.engine.send(encoded)

    def error_response(self, actor, ident, text, code=-32600):
        self.enqueue(actor, {'id': ident, 'error': {'code': code, 'message': text}})

    def accept(self, actor, message):
        method = message.get('method')
        desktop = actor['name'] == 'desktop'
        if isinstance(method, str):
            if not desktop and method == 'initialize':
                with self.lock:
                    result = self.initialize_result if self.desktop_initialized else None
                    caps = message.get('params', {}).get('capabilities') or {}
                    unsupported = any(bool(value) and not self.desktop_capabilities.get(key) for key, value in caps.items()
                                      if key != 'optOutNotificationMethods')
                if actor['initialized']:
                    self.error_response(actor, message.get('id'), 'Already initialized')
                elif result is None:
                    self.error_response(actor, message.get('id'), 'Desktop connection is not ready', -32001)
                elif unsupported:
                    self.error_response(actor, message.get('id'), 'Desktop connection does not support requested capabilities')
                else:
                    actor['initialized'] = True
                    self.enqueue(actor, {'id': message.get('id'), 'result': result})
                return
            if not desktop and not actor['initialized']:
                self.error_response(actor, message.get('id'), 'Not initialized')
                return
            if method == 'initialized' and 'id' not in message:
                if desktop:
                    self.send_engine(message)
                    with self.lock:
                        self.desktop_initialized = True
                    self.publish_if_ready()
                return
            if 'id' not in message:
                if desktop:
                    self.send_engine(message)
                return
            if not desktop and method not in self.ALLOWED_MANAGEMENT:
                self.error_response(actor, message['id'], 'Method is not available through the management bridge', -32601)
                return
            with self.lock:
                if len(self.pending) >= self.MAX_PENDING:
                    self.error_response(actor, message['id'], 'Bridge request queue is full', -32001)
                    return
                thread = (message.get('params') or {}).get('threadId')
                if method in {'turn/start','thread/compact/start'} and isinstance(thread,str) and thread in self.pending_turn_owners:
                    self.error_response(actor,message['id'],'A turn start on this thread is awaiting acknowledgement',-32002)
                    return
                self.sequence += 1
                mapped = 'pm-desktop-bridge-' + str(self.sequence)
                self.pending[mapped] = (actor, message['id'], method, message.get('params') or {})
                if method in {'turn/start','thread/compact/start'}:
                    thread_id = (message.get('params') or {}).get('threadId')
                    if isinstance(thread_id, str):
                        self.pending_turn_owners[thread_id] = actor
                        if method=='thread/compact/start':self.compacting_threads.add(thread_id)
                if desktop and method == 'initialize':
                    self.desktop_capabilities = dict((message.get('params') or {}).get('capabilities') or {})
            self.send_engine({**message, 'id': mapped})
            return
        if 'id' in message and ('result' in message or 'error' in message):
            with self.lock:
                key = self.id_key(message['id'])
                expected = self.server_pending.get(key)
                if expected is actor:
                    self.server_pending.pop(key, None)
            if expected is actor:
                self.send_engine(message)

    def receive_engine(self, message):
        if 'method' not in message and 'id' in message:
            with self.lock:
                entry = self.pending.pop(message['id'], None)
            if entry is None:
                return
            actor, ident, method, params = entry
            result = message.get('result')
            if method in {'thread/start', 'thread/resume'} and isinstance(result, dict):
                thread = result.get('thread') or {}
                thread_id = thread.get('id')
                if isinstance(thread_id, str) and actor['active']:
                    with self.lock:
                        self.owners.setdefault(thread_id, actor)
            if method=='thread/compact/start' and 'error' in message:
                with self.lock:
                    thread_id=params.get('threadId')
                    if self.pending_turn_owners.get(thread_id) is actor:self.pending_turn_owners.pop(thread_id,None)
                    self.compacting_threads.discard(thread_id)
            if method == 'turn/start':
                thread_id = params.get('threadId')
                turn_id = (result.get('turn') or {}).get('id') if isinstance(result, dict) else None
                with self.lock:
                    if self.pending_turn_owners.get(thread_id) is actor:
                        self.pending_turn_owners.pop(thread_id, None)
                    if isinstance(turn_id, str) and actor['active']:
                        self.turn_owners[(thread_id, turn_id)] = actor
                        self.owners[thread_id] = actor
            if method == 'initialize' and actor['name'] == 'desktop' and isinstance(result, dict):
                with self.lock:
                    self.initialize_result = result
                    # Current desktop clients finish initialization on the
                    # successful response; only their short-lived startup
                    # probe also sends the optional initialized notification.
                    # Publish the authenticated main connection in both cases.
                    self.desktop_initialized = True
                self.publish_if_ready()
            self.enqueue(actor, {**message, 'id': ident})
            return
        if 'method' not in message:
            return
        params = message.get('params') or {}
        if message.get('method') == 'serverRequest/resolved':
            request_id = params.get('requestId')
            if isinstance(request_id, (str, int)) and not isinstance(request_id, bool):
                with self.lock:
                    self.server_pending.pop(self.id_key(request_id), None)
        thread_id = params.get('threadId')
        turn_id = (params.get('turn') or {}).get('id') if isinstance(params.get('turn'), dict) else params.get('turnId')
        if message.get('method') == 'turn/started' and isinstance(thread_id, str) and isinstance(turn_id, str):
            with self.lock:
                owner = self.pending_turn_owners.get(thread_id)
                if owner is not None:
                    self.turn_owners[(thread_id, turn_id)] = owner
                    self.owners[thread_id] = owner
        with self.lock:
            desktop = self.actors.get('desktop')
            actors = list(self.actors.values())
        if 'id' in message:
            params = message.get('params') or {}
            thread_id = params.get('threadId')
            with self.lock:
                owner = self.turn_owners.get((thread_id, params.get('turnId')))
                if owner is None and params.get('turnId') is None:
                    owner = self.owners.get(thread_id)
                disconnected_owner = owner is not None and not owner['active']
                target = owner if owner is not None else desktop
                if len(self.server_pending) >= self.MAX_PENDING:
                    self.fail('Too many outstanding engine tool or approval requests')
                    return
                if target is not None and not disconnected_owner:
                    self.server_pending[self.id_key(message['id'])] = target
            if disconnected_owner:
                self.send_engine({'id': message['id'], 'error': {'code': -32000,
                                  'message': 'Management connection closed before responding'}})
            elif target is not None:
                self.enqueue(target, message)
            return
        for actor in actors:
            if actor['initialized']:
                self.enqueue(actor, message)
        if message.get('method') == 'turn/completed':
            with self.lock:
                owner = self.turn_owners.pop((thread_id, turn_id), None)
                if owner is not None and (not owner['active'] or thread_id in self.compacting_threads) and self.pending_turn_owners.get(thread_id) is owner:
                    self.pending_turn_owners.pop(thread_id,None)
                self.compacting_threads.discard(thread_id)
                if owner is not None and self.owners.get(thread_id) is owner:
                    self.owners.pop(thread_id, None)

    def publish_if_ready(self):
        with self.lock:
            if self.published or not self.desktop_initialized or self.initialize_result is None or self.endpoint is None:
                return
            self.on_ready(self.endpoint)
            self.published = True

    def authentication(self, connection, request):
        try:
            origins = request.headers.get_all('Origin')
            auth = request.headers.get_all('Authorization')
            expected = 'Bearer ' + self.token
            accepted = not origins and len(auth) == 1 and secrets.compare_digest(auth[0], expected)
        except (TypeError, ValueError):
            accepted = False
        if not accepted:
            return connection.respond(HTTPStatus.UNAUTHORIZED, 'Authenticated local bridge connection required\n')
        with self.lock:
            if sum(actor['name'] != 'desktop' for actor in self.actors.values()) >= self.MAX_MANAGEMENT:
                return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, 'Bridge connection limit reached\n')
        return None

    def management_handler(self, connection):
        with self.lock:
            if sum(actor['name'] != 'desktop' for actor in self.actors.values()) >= self.MAX_MANAGEMENT:
                connection.close(code=1013, reason='Bridge connection limit reached')
                return
            actor = self.add_actor('management-' + uuid.uuid4().hex, connection.send, connection.close)
        try:
            for frame in connection:
                if self.stop.is_set():
                    break
                self.accept(actor, self.decode(frame))
        except Exception:
            pass
        finally:
            self.drop_actor(actor)

    def read_desktop(self, actor):
        try:
            for raw in _desktop_lines(self.stdin, self.stop):
                if self.stop.is_set():
                    return
                if not raw:
                    self.shutdown()
                    return
                frame = raw.rstrip(b'\r\n')
                if len(frame) > MAX_FRAME or not raw.endswith(b'\n'):
                    raise ShimError('Desktop request exceeded the frame limit or ended mid-frame')
                if frame:
                    self.accept(actor, self.decode(frame))
        except Exception as exc:
            self.fail(str(exc) if isinstance(exc, ShimError) else 'Desktop input pipe or protocol closed')

    def watch(self):
        while not self.stop.wait(.1):
            if self.process.poll() is not None:
                self.fail('Codex engine exited')
                return
            if not self.parent.alive():
                self.fail('Desktop parent closed')
                return

    def run(self):
        from websockets.sync.server import serve
        logger = logging.getLogger('personal_management.codex_desktop_frontend')
        logger.disabled = True
        def write(text):
            self.stdout.write(text.encode('utf-8') + b'\n')
            self.stdout.flush()
        desktop = self.add_actor('desktop', write, lambda: None)
        with serve(self.management_handler, '127.0.0.1', 0, process_request=self.authentication,
                   max_size=MAX_FRAME, max_queue=4, compression=None, logger=logger,
                   open_timeout=5, close_timeout=.5) as server:
            self.front = server
            self.endpoint = f'ws://127.0.0.1:{server.socket.getsockname()[1]}'
            threading.Thread(target=server.serve_forever, daemon=True, name='codex-management-listener').start()
            input_thread = threading.Thread(target=self.read_desktop, args=(desktop,), daemon=True, name='codex-desktop-input')
            input_thread.start()
            threading.Thread(target=self.watch, daemon=True, name='codex-desktop-watch').start()
            try:
                while not self.stop.is_set():
                    try:
                        frame = self.engine.recv(timeout=.2)
                    except TimeoutError:
                        continue
                    self.receive_engine(self.decode(frame))
            except Exception as exc:
                if not self.stop.is_set():
                    self.fail(str(exc) if isinstance(exc, ShimError) else 'Codex engine protocol connection closed')
            finally:
                self.shutdown()
                with self.lock:
                    actors = list(self.actors.values())
                for actor in actors:
                    self.drop_actor(actor)
                server.shutdown()
                input_thread.join(timeout=2)
        if self.error:
            raise ShimError(self.error)


def run_bridge(executable, directory, arguments, stdin=None, stdout=None):
    _secure_directory(directory)
    lock = _DirectoryLock(directory)
    process = job = connection = record = None
    stop = threading.Event()
    diagnostics = None
    engine_token = secrets.token_urlsafe(48)
    front_token = secrets.token_urlsafe(48)
    try:
        endpoint = _endpoint()
        parent = _ParentWatch()
        args = [*arguments, '--listen', endpoint, '--ws-auth', 'capability-token',
                '--ws-token-sha256', hashlib.sha256(engine_token.encode('ascii')).hexdigest()]
        process, job = _start_engine(executable, args)
        diagnostics = threading.Thread(target=_forward_stderr, args=(process.stderr, engine_token, stop), daemon=True)
        diagnostics.start()
        connection = _connect(endpoint, engine_token, process, parent, stop)
        def ready(front_endpoint):
            nonlocal record
            record = {**_runtime_record(executable, process, front_endpoint, front_token), 'ready': True}
            _write_runtime(directory, record)
        _Relay(connection, process, parent, front_token, ready,
               stdin or sys.stdin.buffer, stdout or sys.stdout.buffer).run()
        return 0
    finally:
        stop.set()
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if job is not None:
            job.close()
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if diagnostics is not None:
                diagnostics.join(timeout=1)
            if process.stderr is not None:
                process.stderr.close()
        if record is not None:
            _remove_runtime(directory, record)
        lock.close()


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        executable, directory = _configuration(os.environ)
        arguments = engine_arguments(args)
        if arguments is None:
            return subprocess.call([str(executable), *args], env=dict(os.environ))
        return run_bridge(executable, directory, arguments)
    except (ShimError, OSError, ValueError) as exc:
        _diagnostic(str(exc) if isinstance(exc, ShimError) else 'Adapter setup failed: ' + type(exc).__name__)
        return 1
    except Exception as exc:
        _diagnostic('Adapter failed: ' + type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
