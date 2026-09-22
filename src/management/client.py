from __future__ import annotations

import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid

from .runtime import default_data_dir, discovery, start_service


class ClientError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


class Client:
    def __init__(self, data_dir=None, autostart=True):
        self.data_dir = Path(data_dir or default_data_dir()).resolve()
        self.autostart = autostart
        self.epoch, self.revision = None, None
        self.runtime = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._connect()

    def _connect(self):
        started = False
        deadline = time.monotonic() + (15 if self.autostart else 1)
        while True:
            self.runtime = discovery(self.data_dir)
            if self.runtime:
                try:
                    response = self._request('query', 'state', {}, timeout=2)
                    self._remember(response)
                    return
                except ClientError:
                    pass
            if not self.autostart or time.monotonic() >= deadline:
                raise ClientError('service_unavailable', '业务服务没有响应。请检查所选数据目录与软件运行环境。')
            if not started:
                start_service(self.data_dir)
                started = True
            time.sleep(.1)

    def _request(self, category, name, data, timeout=35):
        if not self.runtime:
            raise ClientError('service_unavailable', '尚未连接业务服务。')
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        url = 'http://127.0.0.1:%d/v1/%s/%s' % (self.runtime['port'], category, name)
        request = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.runtime['token']}, method='POST')
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ClientError('response_limit', '查询返回过多内容，请减少每页数量。')
            return json.loads(raw)
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
        epoch, revision = response.get('epoch'), response.get('revision')
        if epoch and revision is not None:
            if self.epoch != epoch:
                self.epoch, self.revision = epoch, revision
            else:
                self.revision = max(self.revision or 0, revision)
        return response

    def query(self, name, **params):
        try:
            return self._remember(self._request('query', name, params))
        except ClientError as error:
            if error.code == 'connection_lost' and self.autostart:
                self._connect()
                return self._remember(self._request('query', name, params))
            raise

    def state(self):
        return self.query('state')

    def command(self, name, payload, *, request_id=None, expected_revision=None, epoch=None):
        if self.epoch is None:
            self.state()
        envelope = {'request_id': request_id or str(uuid.uuid4()), 'epoch': epoch if epoch is not None else self.epoch, 'expected_revision': expected_revision if expected_revision is not None else self.revision, 'payload': payload}
        try:
            return self._remember(self._request('commands', name, envelope, timeout=300 if name in {'add_source', 'backup', 'restore_backup', 'import_asset', 'export_asset'} else 35))
        except ClientError as error:
            error.details['request_id'] = envelope['request_id']
            # Never silently retry a write or discard the caller's old version.
            raise

    def close(self):
        pass  # Service lifetime belongs to the data space, not this entrance.
