from __future__ import annotations

import json
from pathlib import Path
import time
import threading
import urllib.error
import urllib.request
import uuid

from .runtime import DataSpaceMismatch, UpdatePending, default_data_dir, discovery, read_startup_failure, require_data_dir, start_service
from .runtime_contract import client_headers, matches_contract, mismatch_details, service_mismatch_message
from .data_space import BetaIsolationError, require_beta_dir


class ClientError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


class Client:
    def __init__(self, data_dir=None, autostart=True, *, entrance='gui'):
        if entrance not in {'gui', 'mcp', 'installer', 'tray'}:
            raise ValueError('Unknown client entrance')
        self.entrance = entrance
        try:
            self.data_dir = require_beta_dir(data_dir if data_dir is not None else default_data_dir())
        except BetaIsolationError as error:
            raise ClientError('beta_data_isolation', str(error)) from error
        self.autostart = autostart
        self.epoch, self.revision = None, None
        self.runtime = None
        self._connection_lock = threading.RLock()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._connect()

    def _connect(self):
        with self._connection_lock:
            return self._discover_and_connect()

    def _discover_and_connect(self):
        started = False
        attempted_at = None
        deadline = time.monotonic() + (15 if self.autostart else 1)
        while True:
            try:
                runtime = discovery(self.data_dir)
                if runtime is not None:
                    self._require_data_dir(runtime.get('data_dir') if isinstance(runtime, dict) else None)
                self.runtime = runtime
            except (DataSpaceMismatch, BetaIsolationError) as error:
                self.runtime = None
                raise ClientError('data_space_mismatch', str(error)) from error
            if self.runtime:
                try:
                    response = self._request('query', 'state', {}, timeout=2)
                    return self._remember(response)
                except ClientError as error:
                    if error.code in {'data_space_mismatch', 'service_version_mismatch', 'client_version_mismatch', 'service_updating'}:
                        raise
            if attempted_at is not None:
                failure = read_startup_failure(self.data_dir, not_before=attempted_at)
                if failure:
                    raise ClientError(failure['code'], failure['message'])
            if not self.autostart or time.monotonic() >= deadline:
                raise ClientError('service_unavailable', '业务服务没有响应。请检查所选数据目录、磁盘可用空间、目录读写权限与安装文件。')
            if not started:
                attempted_at = time.time()
                try:
                    start_service(self.data_dir)
                except UpdatePending as error:
                    raise ClientError(error.code, error.message) from error
                except OSError as error:
                    raise ClientError('service_start_failed', '后台启动程序未能运行。请检查安装文件、磁盘可用空间和所选目录权限后重试。') from error
                started = True
            time.sleep(.1)

    def ensure_connected(self):
        """Refresh this data space with a read-only probe before a new operation.

        A persistent MCP client can outlive the service's process, port and
        credential. Only state reads are retried here; a caller's later write
        body is never replayed by this method.
        """
        with self._connection_lock:
            try:
                return self._remember(self._request('query', 'state', {}, timeout=2))
            except ClientError as error:
                if error.code not in {'connection_lost', 'unauthorized', 'service_unavailable'}:
                    raise
                return self._connect()

    def _require_data_dir(self, value):
        try:
            require_beta_dir(self.data_dir)
            require_data_dir(value, self.data_dir)
        except (DataSpaceMismatch, BetaIsolationError) as error:
            self.runtime = None
            raise ClientError('data_space_mismatch', str(error)) from error

    def _request(self, category, name, data, timeout=35):
        runtime = self.runtime
        if not runtime:
            raise ClientError('service_unavailable', '尚未连接业务服务。')
        self._require_data_dir(runtime.get('data_dir'))
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        url = 'http://127.0.0.1:%d/v1/%s/%s' % (runtime['port'], category, name)
        request = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + runtime['token'], **client_headers(getattr(self, 'entrance', 'gui'))}, method='POST')
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ClientError('response_limit', '查询返回过多内容，请减少每页数量。')
            result = json.loads(raw)
            if category == 'query' and name == 'state':
                self._require_data_dir(result.get('data_dir') if isinstance(result, dict) else None)
                contract = result.get('service_contract') if isinstance(result, dict) else None
                if not matches_contract(contract):
                    self.runtime = None
                    raise ClientError('service_version_mismatch', service_mismatch_message(contract), mismatch_details(contract))
                if result.get('maintenance'):
                    message = ('后台正在退出，暂不接收新操作；稍后重新打开管理软件可恢复使用。'
                               if result.get('shutdown_reason') == 'exit' else
                               '后台正在准备更新，暂不接收新操作；请完成更新后重新打开软件。')
                    raise ClientError('service_updating', message)
            return result
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read(65536))['error']
                raise ClientError(detail['code'], detail['message'], detail.get('details')) from error
            except (ValueError, KeyError):
                raise ClientError('protocol_error', '业务服务返回了无法识别的错误。') from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ClientError('connection_lost', '连接中断；写入结果尚未确认。请使用原请求编号查询回执后再重试。') from error
        except ValueError as error:
            raise ClientError('protocol_error', '业务服务返回内容无法解析。') from error

    def _remember(self, response):
        if 'data_dir' in response:
            self._require_data_dir(response['data_dir'])
        epoch, revision = response.get('epoch'), response.get('revision')
        if epoch and revision is not None:
            if self.epoch != epoch:
                self.epoch, self.revision = epoch, revision
            else:
                self.revision = max(self.revision or 0, revision)
        return response

    def query(self, name, **params):
        try:
            return self._remember(self._request('query', name, params, timeout=300 if name=='library_folder' else 100 if name in {'next_context_step','read_material'} else 35))
        except ClientError as error:
            if error.code in {'connection_lost', 'unauthorized', 'service_unavailable'}:
                self._connect()
                return self._remember(self._request('query', name, params, timeout=300 if name=='library_folder' else 100 if name in {'next_context_step','read_material'} else 35))
            raise

    def state(self):
        return self.query('state')

    def command(self, name, payload, *, request_id=None, expected_revision=None, epoch=None):
        prior_epoch, prior_revision = self.epoch, self.revision
        current = self.ensure_connected()
        envelope = {'request_id': request_id or str(uuid.uuid4()),
                    'epoch': epoch if epoch is not None else prior_epoch if prior_epoch is not None else current.get('epoch', self.epoch),
                    'expected_revision': expected_revision if expected_revision is not None else prior_revision if prior_revision is not None else current.get('revision', self.revision),
                    'payload': payload}
        try:
            return self._remember(self._request('commands', name, envelope, timeout=300 if name in {'configure_codex', 'connect_codex', 'add_source', 'backup', 'restore_backup', 'import_asset', 'export_asset'} else 35))
        except ClientError as error:
            error.details['request_id'] = envelope['request_id']
            # Never silently retry a write or discard the caller's old version.
            raise

    def close(self):
        pass  # Service lifetime belongs to the data space, not this entrance.

    def prepare_update(self):
        """Ask this exact service to drain only if idle; never retry a write.

        Retain the connection when a drain needs another user-requested check.
        An ordinary state preflight would intentionally reject maintenance.
        """
        return self._request('maintenance', 'shutdown-if-idle', {}, timeout=30)

    def stop_service(self):
        return self._request('maintenance', 'shutdown-if-idle', {'reason': 'exit'}, timeout=30)
