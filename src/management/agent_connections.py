"""Local MCP configuration and read-only self-checks for independent clients.

Never reads business data files or service discovery credentials. JSON merge
plans contain the original bytes only in memory and never execute a command
read from an external client's configuration.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import stat
import tomllib

from .codex_workspace import MAX_FILE_BYTES, WorkspaceError, _atomic_write, mcp_config
from .data_space import require_beta_dir

PROFILES = {'antigravity': 'Antigravity', 'generic': '通用 MCP', 'codex': 'Codex'}


def server_name(data_dir):
    root = require_beta_dir(data_dir)
    digest = hashlib.sha256(os.path.normcase(str(root)).encode('utf-8')).hexdigest()[:12]
    return 'personal_management_beta_' + digest


def launch_spec(data_dir, profile='antigravity'):
    if profile not in PROFILES:
        raise WorkspaceError('未知的助手连接类型。')
    base = tomllib.loads(mcp_config(data_dir))['mcp_servers']['personal_management']
    return {'command': base['command'],
            'args': [*base['args'], '--mcp-client', profile],
            'env': {**base.get('env', {}), 'PYTHONUTF8': '1'}}


def configuration(data_dir, profile='antigravity'):
    name = server_name(data_dir)
    if profile == 'codex':
        # Keep the existing managed Codex workspace completely independent.
        return mcp_config(data_dir).replace('mcp_servers.personal_management', 'mcp_servers.' + name)
    spec = launch_spec(data_dir, profile)
    entry = {**spec, 'timeoutSeconds': 360} if profile == 'antigravity' else spec
    return json.dumps({'mcpServers': {name: entry}}, ensure_ascii=False, indent=2) + '\n'


def _plain_target(path):
    target = Path(path).expanduser().absolute()
    if target.name != 'mcp_config.json':
        raise WorkspaceError('请选择客户端的 mcp_config.json；其他格式请复制配置后手动添加。')
    for item in (target, *target.parents):
        if item.is_symlink() or item.is_junction():
            raise WorkspaceError('配置路径包含链接，已保留原文件；请选择实际配置文件。')
    if target.exists():
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkspaceError('配置不是独立的普通文件，未修改。')
    return target


def _read_config(path):
    _plain_target(path)
    try:
        with path.open('rb') as stream:
            value = stream.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(value) > MAX_FILE_BYTES:
        raise WorkspaceError('配置文件超过读取上限，原文件已保留。')
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkspaceError('配置中有重复字段，原文件已保留，请先处理重复项。')
        result[key] = value
    return result


@dataclass(frozen=True)
class MergePlan:
    path: Path
    previous: bytes | None = field(repr=False)
    content: bytes = field(repr=False)
    server: str
    profile: str
    added: bool


def preview_merge(data_dir, profile, target):
    if profile not in {'antigravity', 'generic'}:
        raise WorkspaceError('Codex 配置请复制使用，或在原 Codex 协助页管理；此处只合并 JSON。')
    path = _plain_target(target)
    previous = _read_config(path)
    try:
        document = json.loads(previous.decode('utf-8-sig'), object_pairs_hook=_unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError())) if previous else {}
    except (UnicodeError, ValueError) as error:
        raise WorkspaceError('此文件不是可合并的标准 JSON（例如含注释、尾随逗号或格式错误）。'
                             '原文件已保留，可复制本连接条目手动添加。') from error
    if not isinstance(document, dict) or not isinstance(document.get('mcpServers', {}), dict):
        raise WorkspaceError('配置根对象或 mcpServers 格式不正确，原文件已保留。')
    name = server_name(data_dir)
    desired = json.loads(configuration(data_dir, profile))['mcpServers'][name]
    servers = document.setdefault('mcpServers', {})
    if name in servers:
        if servers[name] != desired:
            raise WorkspaceError('已存在同名但内容不同的 Beta 连接，未覆盖。请核对原条目或手动合并。')
        return MergePlan(path, previous, previous, name, profile, False)
    servers[name] = desired
    content = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    if len(content) > MAX_FILE_BYTES:
        raise WorkspaceError('合并后的配置超过大小上限，原文件已保留。')
    return MergePlan(path, previous, content, name, profile, True)


def apply_merge(plan):
    """Serialize cooperating writers and recheck the user's preview under the lock."""
    from .runtime import OwnerLock
    path = _plain_target(plan.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + '.personal-management-beta.lock')
    if (lock_path.is_symlink() or lock_path.is_junction()
            or lock_path.exists() and (not lock_path.is_file() or lock_path.stat().st_nlink != 1)):
        raise WorkspaceError('配置锁路径不是独立普通文件，未保存配置。')
    lock = OwnerLock(lock_path)
    if not lock.acquire():
        raise WorkspaceError('另一处正在保存此配置；请稍后重新预览。')
    try:
        return _apply_merge_locked(plan)
    finally:
        lock.release()


def _apply_merge_locked(plan):
    """Apply exactly the preview; preserve a first-merge backup of an existing file."""
    path = _plain_target(plan.path)
    if _read_config(path) != plan.previous:
        raise WorkspaceError('配置在预览后已变化，未保存；请重新预览。')
    if not plan.added:
        return {'changed': False, 'path': str(path), 'server': plan.server}
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + '.personal-management-beta.bak')
    if plan.previous is not None:
        if backup.is_symlink() or backup.is_junction():
            raise WorkspaceError('备份路径包含链接，未保存配置。')
        if not backup.exists():
            _atomic_write(backup, plan.previous, None)
        elif not backup.is_file():
            raise WorkspaceError('备份位置不可用，未保存配置。')
        # Preserve the first original, never overwrite a user's backup.
    _plain_target(path)
    _atomic_write(path, plan.content, plan.previous)
    return {'changed': True, 'path': str(path), 'server': plan.server,
            'backup': str(backup) if plan.previous is not None else None}


async def probe_connection(data_dir, profile='antigravity', *, timeout=45):
    """Check our own generated launch spec, not the external Agent application's state."""
    from mcp import Client as MCPClient, StdioServerParameters
    from .runtime import require_data_dir
    from .runtime_contract import matches_contract
    root = require_beta_dir(data_dir)
    spec = launch_spec(root, profile)
    params = StdioServerParameters(command=spec['command'], args=spec['args'],
        env={**os.environ, **spec['env'], 'PERSONAL_MANAGEMENT_NO_TRAY': '1'})

    async def check():
        # A stale Beta backend would otherwise make the stdio child exit before
        # handshake, hiding the actionable version error behind an SDK EOF.
        from .client import Client
        await asyncio.to_thread(Client, root, entrance='mcp', mcp_client=profile)
        async with MCPClient(params) as sdk:
            listing = await sdk.list_tools()
            names = {tool.name for tool in listing.tools}
            if not {'query_business', 'create_entity', 'recover_receipt'} <= names:
                raise WorkspaceError('MCP 已连接，但业务工具不完整。')
            if profile != 'codex' and names & {'begin_discussion', 'submit_candidate'}:
                raise WorkspaceError('通用 MCP 连接意外暴露受管会话工具。')
            result = await sdk.call_tool('query_business', {'name': 'state'})
            if result.is_error:
                message = '\n'.join(item.text for item in result.content if hasattr(item, 'text'))
                raise WorkspaceError(message[:1500] or '业务读取失败。')
            state = result.structured_content
            if not isinstance(state, dict):
                raise WorkspaceError('业务状态响应格式不正确。')
            require_data_dir(state.get('data_dir'), root)
            if not matches_contract(state.get('service_contract')):
                raise WorkspaceError('Beta MCP 与业务服务版本或能力不一致。')
            return {'ok': True, 'profile': profile, 'handshake': True, 'tools_listed': True,
                    'business_read': True, 'external_agent_verified': False,
                    'data_dir': str(root), 'service_contract': state['service_contract'],
                    'tool_count': len(names), 'managed_discussions': profile == 'codex'}
    try:
        return await asyncio.wait_for(check(), timeout=timeout)
    except TimeoutError as error:
        raise WorkspaceError('MCP 自检超时；未确认客户端连接成功，请检查 Beta 后台状态。') from error
