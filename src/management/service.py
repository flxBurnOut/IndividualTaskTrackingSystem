"""Authenticated bounded loopback transport for the shared business service."""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import threading

from .core import Core
from .runtime import OwnerLock
from .schemas import BusinessError
from .storage import encode, now


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, core, token):
        self.core, self.token = core, token
        self.slots = threading.BoundedSemaphore(8)
        self.stopping = threading.Event()
        super().__init__(('127.0.0.1', 0), Handler)

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
    server_version = 'PersonalManagement/0.7.1'
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
            if not 0 < length <= (1024 * 1024 if self.path in {'/v1/commands/send_message','/v1/commands/add_source','/v1/discussion/begin'} else 256 * 1024):
                raise BusinessError('request_limit', '请求过大，请将大内容存为附件。')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise BusinessError('validation', '请求需要结构化参数。')
            parts = self.path.split('/')
            if len(parts) != 4 or parts[1] != 'v1':
                raise BusinessError('not_found', '接口不存在。')
            category, name = parts[2:]
            if category == 'query':
                value = self.server.core.query(name, **data)
            elif category == 'discussion':
                from .session_coordinator import handle
                value = handle(self.server.core,name,data)
            elif category == 'commands':
                value = self.server.core.command(name, data['payload'], request_id=data['request_id'], epoch=data['epoch'], expected_revision=data['expected_revision'])
            else:
                raise BusinessError('not_found', '接口不存在。')
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


def run_service(data_dir):
    root = Path(data_dir).resolve()
    lock = OwnerLock(root / 'service.lock')
    if not lock.acquire():
        return 0
    server = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        log = logging.getLogger('management')
        handler = RotatingFileHandler(root / 'diagnostic.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        core = Core(root)
        server = Server(core, secrets.token_urlsafe(32))
        runtime = {'host': '127.0.0.1', 'port': server.server_port, 'token': server.token, 'pid': os.getpid(), 'started_at': now(), 'data_dir': str(root)}
        temp = root / 'runtime.json.new'
        temp.write_text(encode(runtime), encoding='utf-8')
        os.replace(temp, root / 'runtime.json')
        from .scheduler import Background
        background = Background(core, server.stopping)
        background.start()
        server.serve_forever(poll_interval=.2)
        return 0
    finally:
        if server:
            server.stopping.set()
            for event in server.core.cancel_events.values():
                event.set()
            server.server_close()
        lock.release()
