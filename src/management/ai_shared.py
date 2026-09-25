"""Authenticated loopback attachment to the desktop-owned Codex connection.

This module never starts or kills the shared engine, changes its global config,
or answers approvals belonging to another desktop thread/turn.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import sys
from pathlib import Path
import time
from urllib.parse import urlsplit

import psutil
from websockets.sync.client import connect

from .ai import AIError, MAX_MESSAGE_BYTES, _AppServer
from .resources import ResourceError, _plain_path


MAX_RUNTIME_BYTES = 16 * 1024
NOT_READY = '尚未连接 Codex 桌面。请在设置中启用“桌面实时连接”，重新启动 Codex 后重试。'


def _positive_time(value):
    return type(value) in {float, int} and math.isfinite(value) and value > 0


def _process_matches(pid, executable, created):
    if type(pid) is not int or pid <= 0 or not _positive_time(created):
        return False
    if not isinstance(executable, str) or not executable or len(executable) > 4096:
        return False
    path = Path(executable)
    if not path.is_absolute():
        return False
    process = psutil.Process(pid)
    return (process.is_running() and process.status() != psutil.STATUS_ZOMBIE and
            abs(process.create_time() - created) <= .02 and
            os.path.normcase(str(Path(process.exe()).resolve())) == os.path.normcase(str(path.resolve())))


def read_runtime(bridge_dir, expected_executable=None, expected_bridge_executable=None):
    """Read a bounded runtime record. It contains a secret: never log/return it to UI."""
    try:
        if bridge_dir is None:
            raise ValueError('Missing bridge directory')
        directory = _plain_path(bridge_dir)
        runtime = _plain_path(directory / 'runtime.json', file=True)
        before = runtime.stat()
        if before.st_size > MAX_RUNTIME_BYTES:
            raise ValueError('Runtime too large')
        with runtime.open('rb') as stream:
            raw = stream.read(MAX_RUNTIME_BYTES + 1)
        after = _plain_path(runtime, file=True).stat()
        if len(raw) > MAX_RUNTIME_BYTES or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError('Runtime changed during read')
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1 or value.get('ready') is not True:
            raise ValueError('Runtime not ready')
        endpoint, token = value.get('endpoint'), value.get('token')
        if not isinstance(endpoint, str) or len(endpoint) > 200:
            raise ValueError('Invalid endpoint')
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'ws' or parsed.hostname != '127.0.0.1' or
                parsed.username is not None or parsed.password is not None or
                parsed.query or parsed.fragment or parsed.path not in {'', '/'} or
                parsed.port is None or not 1 <= parsed.port <= 65535 or
                parsed.netloc != '127.0.0.1:' + str(parsed.port)):
            raise ValueError('Endpoint must be explicit IPv4 loopback')
        if (not isinstance(token, str) or not 32 <= len(token) <= 1024 or
                any(not 33 <= ord(char) <= 126 for char in token)):
            raise ValueError('Invalid authentication token')
        if value.get('pid') == value.get('engine_pid'):
            raise ValueError('Invalid process binding')
        if not _process_matches(value.get('pid'), value.get('executable'), value.get('shim_create_time')):
            raise ValueError('Stale bridge process')
        if not _process_matches(value.get('engine_pid'), value.get('engine_executable'), value.get('engine_create_time')):
            raise ValueError('Stale engine process')
        if expected_executable and os.path.normcase(str(Path(value['engine_executable']).resolve())) != os.path.normcase(str(Path(expected_executable).resolve())):
            raise ValueError('Wrong Codex engine')
        if expected_bridge_executable and os.path.normcase(str(Path(value['executable']).resolve())) != os.path.normcase(str(Path(expected_bridge_executable).resolve())):
            raise ValueError('Outdated desktop bridge')
        created = dt.datetime.fromisoformat(value['created_at'])
        if (created.tzinfo is None or created.timestamp() < max(value['shim_create_time'], value['engine_create_time']) - 5 or
                created.timestamp() > time.time() + 30):
            raise ValueError('Invalid runtime creation time')
        return value
    except (OSError, ValueError, TypeError, KeyError, RecursionError, ResourceError, psutil.Error):
        raise AIError('AI_SHARED_NOT_READY', NOT_READY) from None


def connection_status(bridge_dir, expected_bridge_executable=None, expected_executable=None):
    """A secret-free local readiness check; does not connect or start a model."""
    try:
        read_runtime(bridge_dir, expected_executable=expected_executable, expected_bridge_executable=expected_bridge_executable)
    except AIError as error:
        return {'ready': False, 'message': error.message}
    return {'ready': True, 'message': '已连接 Codex 桌面，可实时查看软件派发的任务。'}


def _thread(body):
    value = body.get('threadId')
    return value if isinstance(value, str) else None


def _turn(body):
    value = body.get('turnId')
    if value is None and isinstance(body.get('turn'), dict):
        value = body['turn'].get('id')
    return value if isinstance(value, str) else None


class SharedAppServer(_AppServer):
    """The proposal adapter's API over its own authenticated desktop WS client."""

    def __init__(self, executable, cwd, cancel, timeout, bridge_dir):
        self.started_at = time.monotonic()
        self.timeout_seconds = timeout
        self.cancel, self.deadline = cancel, self.started_at + timeout
        self.waiting_method, self.phase, self.last_event = None, 'preparing', None
        self.seq = 0
        self.thread_id = self.turn_id = None
        self._owned_thread_id = self._owned_turn_id = None
        self._backlog = []
        self.allowed_dynamic_tool = None
        self._closed = False
        expected_bridge = Path(sys.executable).with_name('PersonalManagementCodex.exe') if getattr(sys, 'frozen', False) else None
        runtime = read_runtime(bridge_dir, expected_executable=executable, expected_bridge_executable=expected_bridge)
        # A private disabled logger prevents a transport debug setting from
        # recording the Authorization header or the user's message frames.
        logger = logging.Logger('management.shared.transport')
        logger.disabled = True
        if cancel.is_set():
            raise AIError('AI_CANCELLED', '本次辅助处理已取消。')
        try:
            self.socket = connect(runtime['endpoint'],
                additional_headers={'Authorization': 'Bearer ' + runtime['token']},
                proxy=None, compression=None, legacy=True, open_timeout=min(10, timeout),
                close_timeout=1, max_size=MAX_MESSAGE_BYTES, max_queue=16, logger=logger)
        except Exception:
            raise AIError('AI_SHARED_NOT_READY', NOT_READY) from None

    def send(self, message):
        try:
            encoded = json.dumps(message, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode('utf-8')) > MAX_MESSAGE_BYTES:
                raise AIError('AI_OUTPUT_LIMIT', '发送给 Codex 的协议消息超过允许大小。')
            self.socket.send(encoded)
        except AIError:
            raise
        except Exception:
            raise AIError('AI_DISCONNECTED', '与 Codex 桌面的连接已中断，请检查桌面连接后重试。') from None

    def _belongs(self, event, *, approval=False):
        body = event.get('params')
        if not isinstance(body, dict):
            return False
        owner = self._owned_thread_id or self.thread_id
        if not owner or _thread(body) != owner:
            return False
        turn = _turn(body)
        if approval:
            return bool(self._owned_turn_id and turn == self._owned_turn_id)
        return not self._owned_turn_id or turn is None or turn == self._owned_turn_id

    def _admit(self, event):
        if 'method' not in event:
            return event
        if not self._belongs(event):
            return None
        body = event['params']
        event_turn = _turn(body)
        if self._owned_turn_id is None and event_turn and getattr(self,'_compacting',False) and event.get('method')=='turn/started':
            self._owned_turn_id=event_turn;self.turn_id=event_turn
        if self._owned_turn_id is None and event_turn:
            # A turn notification/request can precede its start response. Hold
            # it until that response establishes which turn this client owns.
            return event if self.waiting_method == 'turn/start' else None
        if 'id' in event:
            if self._belongs(event, approval=True):
                if (event.get('method') == 'item/tool/call' and self.allowed_dynamic_tool == 'submit_management_candidate' and
                        body.get('tool') == self.allowed_dynamic_tool and body.get('namespace') is None):
                    return event
                self.send({'id': event['id'], 'error': {'code': -32601, 'message': 'Tools and approvals are disabled in the proposal adapter.'}})
                raise AIError('AI_TOOL_BLOCKED', '辅助推理请求了未开放工具，已停止本次处理。')
            # A request with no correlated owned turn belongs to the desktop's
            # approval flow. This client must neither answer nor reject it.
            return None
        if event.get('method') in {'item/started', 'item/completed'}:
            if not self._owned_turn_id or event_turn != self._owned_turn_id:
                return None
            item = event['params'].get('item', {})
            if not isinstance(item, dict):
                raise AIError('AI_PROTOCOL_ERROR', 'Codex 返回的事件格式错误。')
            if (item.get('type') == 'dynamicToolCall' and self.allowed_dynamic_tool == 'submit_management_candidate' and
                    item.get('tool') == self.allowed_dynamic_tool and item.get('namespace') is None):
                return event
            if (item.get('type') == 'mcpToolCall' and getattr(self,'allowed_mcp_server',None)
                    and item.get('server') == self.allowed_mcp_server
                    and item.get('tool') in ({'begin_discussion','query_business','submit_candidate'} | __import__('management.context_mcp',fromlist=['TOOL_NAMES']).TOOL_NAMES)):
                self._observe(event)
                return event
            if item.get('type') in {'commandExecution', 'fileChange', 'mcpToolCall', 'dynamicToolCall', 'webSearch', 'collabAgentToolCall'}:
                raise AIError('AI_TOOL_BLOCKED', '辅助推理尝试执行工具，已停止本次处理。')
        self._observe(event)
        return event

    def _receive(self):
        while True:
            if self.cancel.is_set():
                raise AIError('AI_CANCELLED', '本次辅助处理已取消。')
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise self._timeout_error()
            try:
                raw = self.socket.recv(timeout=min(.1, remaining))
            except TimeoutError:
                continue
            except Exception:
                raise AIError('AI_DISCONNECTED', '与 Codex 桌面的连接已中断，未生成可保存结果。') from None
            if not isinstance(raw, str):
                raise AIError('AI_PROTOCOL_ERROR', 'Codex 桌面返回了无法识别的协议消息。')
            try:
                if len(raw.encode('utf-8')) > MAX_MESSAGE_BYTES:
                    raise AIError('AI_OUTPUT_LIMIT', 'Codex 单条输出超过允许大小。')
                event = json.loads(raw)
            except (ValueError, RecursionError):
                raise AIError('AI_PROTOCOL_ERROR', 'Codex 桌面返回了无法识别的协议消息。') from None
            if not isinstance(event, dict):
                raise AIError('AI_PROTOCOL_ERROR', 'Codex 桌面返回的协议消息格式错误。')
            admitted = self._admit(event)
            if admitted is not None:
                return admitted

    def request(self, method, params):
        if method=='thread/compact/start':
            self._compacting=True;self._owned_turn_id=None;self.turn_id=None
        if method == 'initialize':
            params = {**params, 'clientInfo': {**params.get('clientInfo', {}), 'name': 'personal_management_shared'}}
        if method == 'thread/resume':
            self.thread_id = params['threadId']
        self.waiting_method = method
        self.seq += 1
        ident = self.seq
        self.send({'id': ident, 'method': method, 'params': params})
        while True:
            message = self._receive()
            if 'method' not in message and message.get('id') == ident:
                if 'error' in message:
                    error = message['error'] if isinstance(message['error'], dict) else {}
                    code = error.get('code')
                    safe = {'request_method':method, **({'protocol_code':code} if type(code) is int else {})}
                    raise AIError('AI_REQUEST_REJECTED', 'Codex 桌面未接受请求，请检查连接、模型或当前任务状态。', safe)
                result = message.get('result', {})
                if not isinstance(result, dict):
                    raise AIError('AI_PROTOCOL_ERROR', 'Codex 桌面返回的请求回执格式错误。')
                if method in {'thread/start', 'thread/resume'}:
                    self._owned_thread_id = result.get('thread', {}).get('id')
                    self.thread_id = self._owned_thread_id
                elif method == 'turn/start':
                    self._owned_turn_id = result.get('turn', {}).get('id')
                    self.turn_id = self._owned_turn_id
                    self.phase = 'awaiting_model'
                self.waiting_method = None
                return result
            if 'method' not in message:
                continue
            if len(self._backlog) >= 128:
                raise AIError('AI_OUTPUT_LIMIT', 'Codex 待处理事件过多。')
            self._backlog.append(message)

    def next_event(self):
        while self._backlog:
            admitted = self._admit(self._backlog.pop(0))
            if admitted is not None:
                return admitted
        return self._receive()

    def close(self):
        if self._closed:
            return
        self._closed = True
        # Identifiers must come from this connection's successful start/resume
        # and turn/start responses. Never close/unsubscribe a shared thread or
        # interrupt a desktop user's different turn on the same thread.
        if (self.thread_id and self.turn_id and self.thread_id == self._owned_thread_id and
                self.turn_id == self._owned_turn_id):
            try:
                self.send({'id': 999999, 'method': 'turn/interrupt',
                           'params': {'threadId': self.thread_id, 'turnId': self.turn_id}})
            except AIError:
                pass
        try:
            self.socket.close()
        except Exception:
            pass
