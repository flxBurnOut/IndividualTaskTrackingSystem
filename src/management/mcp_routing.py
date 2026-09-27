"""Route MCP calls using transport metadata, never model-supplied identities."""
from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
import json
import uuid

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


class RoutedMCPServer(MCPServer):
    """Carry the SDK's public call context through existing tool implementations.

    SDK 2.2 has no get_context method. Its public call_tool(context=...) entry is
    also used by incoming ClientSession calls. A ContextVar keeps concurrent
    calls separate without adding identity arguments to any tool schema.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_metadata = ContextVar('mcp_request_metadata', default=None)

    async def call_tool(self, name, arguments, context=None):
        try:
            metadata = context.request_context.meta if context is not None else None
        except ValueError:
            metadata = None  # Direct local call without transport context.
        token = self.request_metadata.set(metadata)
        try:
            return await super().call_tool(name, arguments, context)
        finally:
            self.request_metadata.reset(token)


def _error(code, message):
    return ToolError(json.dumps({'code': code, 'message': message}, ensure_ascii=False))


class DiscussionRouting:
    def __init__(self, server: RoutedMCPServer, client):
        self.server, self.client = server, client

    def thread_id(self, *, required=False):
        meta = self.server.request_metadata.get()
        if meta is None or isinstance(meta, Mapping) and 'threadId' not in meta:
            if required:
                raise _error('discussion_identity_required', '受管讨论需要 Codex 提供真实会话身份，请从关联的 Codex 会话调用。')
            return None
        try:
            value = meta['threadId']
            if not isinstance(value, str) or len(value) > 64 or str(uuid.UUID(value)) != value:
                raise ValueError()
            return value
        except (KeyError, TypeError, ValueError, AttributeError):
            raise _error('discussion_identity_invalid', 'Codex 调用提供的会话身份无效，未执行业务操作。') from None

    def _request(self, action, **params):
        thread_id = self.thread_id(required=True)
        turn_id = self.turn_id()
        try:
            self._ensure_client()
            # Put the trusted identity last so caller parameters cannot replace it.
            return self.client._request('native-discussion', action,
                                        {**params, 'provider_thread_id': thread_id, 'provider_turn_id': turn_id},
                                        timeout=100 if params.get('name') in {'next_context_step', 'read_material'} else 35)
        except Exception as exc:
            if hasattr(exc, 'code'):
                raise ToolError(json.dumps({'code': exc.code, 'message': str(exc),
                                           'details': getattr(exc, 'details', {})}, ensure_ascii=False)) from exc
            raise

    def _ensure_client(self):
        # Production Client always provides this public read-only preflight.
        # Lightweight in-process test adapters may omit connection management.
        ensure = getattr(self.client, 'ensure_connected', None)
        if ensure is not None:
            ensure()

    def turn_id(self):
        """Read the harness-owned nested turn ID; never accept a tool argument.

        Codex 0.158 emits an object. A bounded JSON string is accepted for older
        serializers. Missing nested metadata is explicit legacy compatibility;
        malformed present metadata must not silently downgrade that guarantee.
        """
        meta = self.server.request_metadata.get()
        if meta is None or isinstance(meta, Mapping) and 'x-codex-turn-metadata' not in meta:
            return None
        try:
            nested = meta['x-codex-turn-metadata']
            if isinstance(nested, str):
                if len(nested.encode('utf-8')) > 65536:
                    raise ValueError()
                nested = json.loads(nested)
            if not isinstance(nested, Mapping):
                raise ValueError()
            value = nested['turn_id']
            if not isinstance(value, str) or len(value) > 64 or str(uuid.UUID(value)) != value:
                raise ValueError()
            if 'thread_id' in nested and nested['thread_id'] != self.thread_id(required=True):
                raise ValueError()
            return value
        except (KeyError, TypeError, ValueError, AttributeError, UnicodeError, RecursionError):
            raise _error('discussion_turn_identity_invalid', 'Codex 调用提供的回合身份无效或与会话不一致，未执行业务操作。') from None

    def binding(self):
        if self.thread_id() is None:
            return {'managed': False}
        result = self._request('binding')
        if not isinstance(result, dict) or type(result.get('managed')) is not bool:
            raise _error('discussion_binding_invalid', '无法确认当前会话关联，未执行业务操作。')
        if result['managed'] and (not isinstance(result.get('conversation_id'), str)
                                  or not result['conversation_id'] or not isinstance(result.get('epoch'), str)
                                  or not result['epoch']):
            raise _error('discussion_binding_invalid', '当前会话关联信息不完整，未执行业务操作。')
        return result

    def is_managed(self):
        # Never cache this result: links and data-space epochs can change while
        # a fixed project MCP process remains alive across many user turns.
        return self.binding()['managed']

    def request(self, action, **params):
        if action not in {'begin', 'submit', 'context', 'query'}:
            raise ValueError('unsupported discussion action')
        # The business service resolves binding and epoch again atomically.
        return self._request(action, **params)

    def guard_write(self):
        if self.is_managed():
            raise _error('discussion_confirmation_required',
                         '本会话属于软件中的受管事项。请先 begin_discussion，再 submit_candidate；'
                         '候选必须由用户在管理软件确认，不能直接执行业务写入、启动其他 AI 或采用候选。')

    def guard_fixed_binding(self, conversation_id, epoch):
        if self.thread_id() is None:
            self._ensure_client()
            return  # Retain the explicitly configured legacy/local entry.
        binding = self.binding()
        if (not binding['managed'] or binding['conversation_id'] != conversation_id
                or binding['epoch'] != epoch):
            raise _error('discussion_binding_mismatch', '当前 Codex 会话与此旧版讨论接口的事项或数据版本不一致，已拒绝访问。')


def is_context_query(name):
    from .context_mcp import TOOL_NAMES
    return name in TOOL_NAMES or name in {'prepare_context', 'material_image'}
