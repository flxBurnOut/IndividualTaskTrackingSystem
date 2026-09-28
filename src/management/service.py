"""Authenticated bounded loopback transport for the shared business service."""
from __future__ import annotations

import hmac
import errno
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import threading
import time

from .core import Core
from .runtime import OwnerLock, require_no_pending_update
from .runtime_contract import ENTRANCE_HEADER, PROTOCOL_HEADER, VERSION_HEADER, service_contract
from . import __version__
from .schemas import BusinessError
from .storage import encode, now


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, core, token):
        self.core, self.token = core, token
        self.slots = threading.BoundedSemaphore(8)
        self.stopping = threading.Event()
        self.background = None
        self.contract = service_contract()
        self._activity_lock = threading.RLock()
        self._maintenance_lock = threading.Lock()
        self.active_requests = 0
        self.maintenance = False
        self.shutdown_reason = 'update'
        self._update_finisher = None
        self._update_finished = False
        self.client_entrances = {}
        super().__init__(('127.0.0.1', 0), Handler)

    @contextmanager
    def activity(self):
        with self._activity_lock:
            if self.maintenance:
                raise BusinessError('service_updating', '后台已暂停接收操作，正在准备更新；请完成更新后重新打开软件。')
            self.active_requests += 1
        try:
            yield
        finally:
            with self._activity_lock:
                self.active_requests -= 1

    def observe_entrance(self, headers):
        version, protocol = headers.get(VERSION_HEADER), headers.get(PROTOCOL_HEADER)
        entrance = headers.get(ENTRANCE_HEADER)
        entrance = entrance if entrance in {'gui', 'mcp', 'installer', 'tray'} else 'unknown'
        verified = version == self.contract['app_version'] and protocol == str(self.contract['protocol_version'])
        # Diagnostics only: caller-declared role is not an authorization claim.
        with self._activity_lock:
            self.client_entrances[entrance] = {
                'app_version': version[:64] if isinstance(version, str) else None,
                'protocol_version': protocol[:16] if isinstance(protocol, str) else None,
                'verified': verified, 'checked_at': now()}
        return verified

    def prepare_update(self, *, drain_timeout=10, reason='update'):
        if reason not in {'exit', 'update'}:
            raise BusinessError('validation', '退出方式无效。')
        if not self._maintenance_lock.acquire(blocking=False):
            raise BusinessError('update_draining', '正在等待后台退出，请稍后再次检查。')
        try:
            with self._activity_lock:
                if self.active_requests:
                    raise BusinessError('update_busy', '还有操作正在执行，请等待保存、导入或查询完成后再退出后台。',
                                        {'active_requests': self.active_requests})
                self.maintenance = True
            if not self.stopping.is_set():
                try:
                    with self.core.store.lock, self.core.store.connect() as c:
                        counts = {
                            'pending_jobs': c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0],
                            'pending_native_turns': c.execute("""SELECT count(*) FROM conversation_operations o
                                JOIN jobs j ON j.id=o.job_id AND j.epoch=o.epoch
                                WHERE o.pending_terminal=1 AND j.status IN ('queued','running')
                                  AND (o.provider_generation IS NULL OR o.provider_generation=j.generation)
                                  AND o.phase NOT IN ('completed','rejected','reconciled')""").fetchone()[0],
                            'pending_file_operations': c.execute("SELECT count(*) FROM io_operations WHERE status IN ('preparing','ready')").fetchone()[0],
                        }
                        if any(counts.values()) or self.core.cancel_events:
                            raise BusinessError('update_busy', '还有任务或资料操作尚未结束，请等待完成后再退出；本次没有停止任务。', counts)
                        self.shutdown_reason = reason
                        self._write_update_marker('draining')
                        self.stopping.set()
                except Exception:
                    with self._activity_lock:
                        self.maintenance = False
                    raise
            deadline = time.monotonic() + drain_timeout
            threads = self.background.threads if self.background is not None else []
            for thread in threads:
                thread.join(max(0, deadline - time.monotonic()))
            if any(thread.is_alive() for thread in threads):
                if self._update_finisher is None:
                    self._update_finisher = threading.Thread(target=self._finish_update_after_drain,
                        name='management-update-drain', daemon=True)
                    self._update_finisher.start()
                raise BusinessError('update_draining', '后台正在完成当前资料读取，结束后会自动退出。可以关闭软件，等待安装器允许继续；不要强制关闭后台进程。')
            self._complete_update()
            return {'prepared': True, 'data_dir': str(self.core.root),
                    'service_version': self.contract['app_version'], 'status': 'shutdown_requested'}
        finally:
            self._maintenance_lock.release()

    def _complete_update(self):
        """Called under the maintenance lock after every writer has stopped."""
        if self._update_finished:
            return
        # The idle observer never owns a model turn and cannot write data.
        gateway = self.core._desktop_gateway
        if gateway is not None:
            gateway.close()
        self._write_update_marker('ready')
        self._update_finished = True

    def _finish_update_after_drain(self):
        try:
            for thread in self.background.threads if self.background is not None else []:
                thread.join()
            with self._maintenance_lock:
                self._complete_update()
        except Exception as error:
            # Keep the existing maintenance marker if its final write failed.
            # OwnerLock still fences data until the service exits, and an
            # explicit GUI launch can safely resume that stopped data space.
            logging.getLogger('management').error('Update drain completion failed (%s).', type(error).__name__)
        finally:
            self.shutdown()

    def _write_update_marker(self, status):
        marker = {'format': 'personal-management-update/1', 'data_dir': str(self.core.root),
                  'app_version': self.contract['app_version'], 'pid': os.getpid(),
                  'status': status, 'prepared_at': now(), 'reason': self.shutdown_reason}
        temporary = self.core.root / 'update_pending.json.new'
        temporary.write_text(encode(marker), encoding='utf-8')
        os.replace(temporary, self.core.root / 'update_pending.json')

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'PersonalManagement/' + __version__
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        pass  # No source text, credentials or request bodies in access logs.

    def send_json(self, value, status=200):
        data = encode(value).encode('utf-8')
        if len(data) > 2 * 1024 * 1024:
            status, data = 413, encode({'error': {'code': 'response_limit', 'message': '查询结果过大，请减小每页数量。', 'details': {}}}).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        try:
            expected_host = '127.0.0.1:%d' % self.server.server_port
            if self.headers.get('Host') != expected_host or self.headers.get('Origin'):
                raise BusinessError('forbidden', '此接口仅供受信任的本机客户端使用。')
            token = self.headers.get('Authorization', '')
            if not hmac.compare_digest(token, 'Bearer ' + self.server.token):
                raise BusinessError('unauthorized', '客户端凭据已过期，请重新连接。')
            if self.headers.get('Transfer-Encoding') or self.headers.get_content_type() != 'application/json':
                raise BusinessError('protocol', '仅接受有长度的 JSON 请求。')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= (1024 * 1024 if self.path in {'/v1/commands/send_message','/v1/commands/add_source','/v1/discussion/begin','/v1/native-discussion/begin'} else 256 * 1024):
                raise BusinessError('request_limit', '请求过大，请将大内容存为附件。')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise BusinessError('validation', '请求需要结构化参数。')
            parts = self.path.split('/')
            if len(parts) != 4 or parts[1] != 'v1':
                raise BusinessError('not_found', '接口不存在。')
            category, name = parts[2:]
            verified = self.server.observe_entrance(self.headers)
            diagnostic = category == 'query' and name in {'state', 'runtime_status'}
            if not diagnostic:
                if not verified:
                    raise BusinessError('client_version_mismatch',
                        '当前入口仍在使用旧版或不兼容的接口，后台已拒绝本次操作。请打开新版软件；'
                        '若从 Codex 发起，请在任务空闲后重启 MCP 连接以加载新版接口。',
                        {'service_version': self.server.contract['app_version'], 'required_protocol': self.server.contract['protocol_version']})
            if (category, name) == ('maintenance', 'shutdown-if-idle'):
                if data not in ({}, {'reason': 'exit'}):
                    raise BusinessError('validation', '退出后台不接受业务修改参数。')
                value = self.server.prepare_update(reason='exit') if data else self.server.prepare_update()
                self.send_json(value)
                threading.Thread(target=self.server.shutdown, name='management-update-shutdown', daemon=True).start()
                return
            if diagnostic:
                value = self.server.core.query('state', **data) if name == 'state' else {'data_dir': str(self.server.core.root)}
                with self.server._activity_lock:
                    value = {**value, 'service_contract': self.server.contract,
                             'maintenance': self.server.maintenance,
                             'shutdown_reason': self.server.shutdown_reason if self.server.maintenance else None}
                    if name == 'runtime_status':
                        value['client_entrances'] = dict(self.server.client_entrances)
                if name == 'runtime_status':
                    with self.server.core.store.connect() as connection:
                        value['jobs'] = dict(connection.execute("SELECT status,count(*) FROM jobs WHERE status IN ('queued','running','awaiting_review') GROUP BY status"))
                    from .tray_runtime import tray_status
                    value['tray'] = tray_status(self.server.core.root)
            else:
                with self.server.activity():
                    value = self.dispatch(category, name, data)
            self.send_json(value)
        except Exception as error:
            if isinstance(error, (KeyError, ValueError, TypeError)):
                error = BusinessError('validation', '请求格式或字段不正确。')
            if hasattr(error, 'code') and hasattr(error, 'message'):
                code = error.code
                status = 409 if 'conflict' in code or code == 'stale_proposal' else 401 if code == 'unauthorized' else 403 if code == 'forbidden' else 400
                self.send_json({'error': {'code': code, 'message': error.message, 'details': getattr(error, 'details', {})}}, status)
            else:
                logging.getLogger('management').exception('Unhandled service error')
                self.send_json({'error': {'code': 'internal_error', 'message': '操作未确认成功，请查询回执或查看诊断。', 'details': {}}}, 500)

    def dispatch(self, category, name, data):
        if category == 'query':
            return self.server.core.query(name, **data)
        if category == 'discussion':
            from .session_coordinator import handle
            return handle(self.server.core, name, data)
        if category == 'native-discussion':
            from .session_coordinator import handle_native
            return handle_native(self.server.core, name, data)
        if category == 'commands':
            return self.server.core.command(name, data['payload'], request_id=data['request_id'], epoch=data['epoch'], expected_revision=data['expected_revision'])
        raise BusinessError('not_found', '接口不存在。')


def run_service(data_dir, resume_token=None):
    root = Path(data_dir).resolve()
    require_no_pending_update(root, resume_token)
    lock = OwnerLock(root / 'service.lock')
    if not lock.acquire():
        return 0
    server = None
    handler = None
    serving = False
    log = logging.getLogger('management')
    try:
        require_no_pending_update(root, resume_token)
        root.mkdir(parents=True, exist_ok=True)
        (root / 'startup_failure.json').unlink(missing_ok=True)
        handler = RotatingFileHandler(root / 'diagnostic.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        core = Core(root)
        server = Server(core, secrets.token_urlsafe(32))
        runtime = {'host': '127.0.0.1', 'port': server.server_port, 'token': server.token, 'pid': os.getpid(), 'started_at': now(), 'data_dir': str(root), 'service_contract': service_contract()}
        temp = root / 'runtime.json.new'
        temp.write_text(encode(runtime), encoding='utf-8')
        os.replace(temp, root / 'runtime.json')
        from .scheduler import Background
        background = Background(core, server.stopping)
        server.background = background
        background.start()
        from .tray_runtime import start_tray_supervisor
        start_tray_supervisor(root, server.stopping)
        if resume_token is not None:
            # Clear only after this new process owns the data, publishes its
            # connection and completes recovery. Failed startup keeps the fence.
            require_no_pending_update(root, resume_token)
            (root / 'update_pending.json').unlink(missing_ok=True)
        serving = True
        server.serve_forever(poll_interval=.2)
        return 0
    except Exception as error:
        if not serving:
            failure = _startup_failure(error)
            log.error('Service startup failed (%s).', failure['code'])
            try:
                temporary = root / 'startup_failure.json.new'
                temporary.write_text(encode({'format': 'personal-management-startup-failure/1',
                    'app_version': __version__, 'data_dir': str(root),
                    'failed_at_unix': time.time(), **failure}), encoding='utf-8')
                os.replace(temporary, root / 'startup_failure.json')
            except OSError:
                # An entirely unwritable/full filesystem cannot save a receipt.
                # The client's fallback still names the disk/permission checks.
                pass
        raise
    finally:
        if server:
            server.stopping.set()
            for event in server.core.cancel_events.values():
                event.set()
            gateway = server.core._desktop_gateway
            if gateway is not None:
                gateway.close()
            server.server_close()
        lock.release()
        if handler is not None:
            log.removeHandler(handler)
            handler.close()


def _startup_failure(error):
    if isinstance(error, BusinessError):
        # BusinessError startup messages are authored for the UI; never include
        # its details or the raw underlying exception (paths/payloads/secrets).
        return {'code': error.code, 'message': error.message}
    if isinstance(error, OSError) and (error.errno == errno.ENOSPC or getattr(error, 'winerror', None) == 112):
        return {'code': 'startup_storage_full', 'message': '磁盘可用空间不足，后台未能启动。请释放数据目录所在磁盘的空间后重新打开软件。'}
    if isinstance(error, PermissionError):
        return {'code': 'startup_permission', 'message': '后台无法读写所选数据目录。请检查目录权限与文件占用后重新打开软件。'}
    return {'code': 'service_start_failed', 'message': '后台未能启动。请检查数据目录、磁盘空间和安装文件；诊断日志已记录启动失败类型。'}
