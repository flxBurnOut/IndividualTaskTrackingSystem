"""First-run registration through the public Codex app-server protocol.

No model turns, private desktop-state edits, or shell/UI automation are used.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlencode
import tomllib
import uuid

from . import __version__, ai
from .schemas import BusinessError

_LOCK = threading.Lock()
BINDING_FILE = '.personal-management-project.json'


def _key(path):
    return os.path.normcase(str(Path(path).resolve()))


def _error(message):
    return BusinessError('codex_project_incomplete', 'Codex 项目连接尚未完成：' + message)


def _project(value, workspace):
    if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not value['id'] or len(value['id']) > 200:
        raise _error('项目接口返回了无效标识。')
    roots = value.get('roots')
    if not isinstance(roots, list) or not roots or not isinstance(roots[0], dict):
        raise _error('项目未返回主目录。')
    path = roots[0].get('path')
    if not isinstance(path, str) or not Path(path).is_absolute() or _key(path) != _key(workspace):
        raise _error('已注册项目的主目录与当前数据空间不一致。')
    if not isinstance(value.get('name'), str) or not value['name'] or len(value['name']) > 500:
        raise _error('项目接口返回了无效名称。')
    return value


def _matching_projects(rpc, workspace):
    cursor, seen, matches = None, set(), []
    for _ in range(50):
        params = {'limit': 100}
        if cursor is not None:
            params['cursor'] = cursor
        result = rpc.request('project/list', params)
        rows = result.get('data') if isinstance(result, dict) else None
        if not isinstance(rows, list) or len(rows) > 100:
            raise _error('无法完整读取项目列表，未重复创建项目。')
        for row in rows:
            roots = row.get('roots') if isinstance(row, dict) else None
            if isinstance(roots, list) and roots and isinstance(roots[0], dict):
                path = roots[0].get('path')
                if isinstance(path, str) and Path(path).is_absolute() and _key(path) == _key(workspace):
                    item = _project(row, workspace)
                    if item['id'] not in {m['id'] for m in matches}:
                        matches.append(item)
        cursor = result.get('nextCursor')
        if cursor is None:
            break
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in seen:
            raise _error('项目列表分页无效，未重复创建项目。')
        seen.add(cursor)
    else:
        raise _error('项目数量超出检查上限，未重复创建项目。')
    return matches


def open_desktop_workspace(workspace):
    """Use the application's registered OS protocol; opens an empty composer."""
    if os.name != 'nt':
        raise _error('当前桌面项目接入只支持 Windows。')
    url = 'codex://new?' + urlencode({'path': str(Path(workspace).resolve())})
    try:
        os.startfile(url)
    except OSError as exc:
        raise _error('无法打开 Codex 桌面应用，请安装并启动 Codex 后重新保存设置。') from exc


def _desktop_project(rpc, workspace):
    before = _matching_projects(rpc, workspace)
    known_ids = {row['id'] for row in before}
    binding = project_binding(workspace)
    preferred = binding['project_id'] if binding else None
    chosen = [row for row in before if row['id'] == preferred]
    opened = False
    if len(chosen) == 1:
        selected = chosen[0]
    elif len(before) == 1:
        selected = before[0]
    elif before:
        raise _error('多个项目同时使用此目录，请在 Codex 核对项目后重新保存。')
    else:
        open_desktop_workspace(workspace)
        opened = True
        deadline = time.monotonic() + 12
        while True:
            matches = _matching_projects(rpc, workspace)
            if len(matches) == 1:
                selected = matches[0]
                break
            if len(matches) > 1:
                raise _error('多个项目同时使用此目录，请在 Codex 核对项目后重新保存。')
            if time.monotonic() >= deadline:
                raise _error('Codex 桌面尚未完成项目登记。请检查 Codex 提示后重新保存设置。')
            time.sleep(.2)
    verified = rpc.request('project/read', {'projectId': selected['id']})
    confirmed = _project(verified.get('project'), workspace)
    if confirmed['id'] != selected['id']:
        raise _error('项目读取结果与登记标识不一致。')
    return confirmed, confirmed['id'] not in known_ids, opened


def _active_project_layer(result, workspace):
    for layer in result.get('layers', []):
        source = layer.get('name', {})
        if source.get('type') == 'project' and isinstance(source.get('dotCodexFolder'), str):
            if _key(source['dotCodexFolder']) == _key(workspace / '.codex'):
                return layer
    return None


def _activate_project_config(rpc, workspace):
    result = rpc.request('config/read', {'cwd': str(workspace), 'includeLayers': True})
    layer = _active_project_layer(result, workspace)
    # Trust only our dedicated workspace. An explicit user denial is preserved.
    users = [x for x in result.get('layers', []) if x.get('name', {}).get('type') == 'user' and not x.get('name', {}).get('profile')]
    if len(users) != 1:
        raise _error('无法定位 Codex 的项目权限设置。')
    user = users[0]
    projects = user.get('config', {}).get('projects', {})
    for path, config in projects.items():
        if _key(path) != _key(workspace) or not isinstance(config, dict):
            continue
        if config.get('trust_level') == 'untrusted':
            raise _error('此项目被明确设为不信任；请先在 Codex 中确认项目权限，再保存设置。')
        if config.get('trust_level') == 'trusted' and layer is not None and not layer.get('disabledReason'):
            return
    file = user.get('name', {}).get('file')
    version = user.get('version')
    if not isinstance(file, str) or not Path(file).is_absolute() or not isinstance(version, str):
        raise _error('无法验证 Codex 配置版本，未修改权限设置。')
    rpc.request('config/value/write', {
        'filePath': file, 'expectedVersion': version,
        'keyPath': 'projects.' + json.dumps(str(workspace), ensure_ascii=False) + '.trust_level',
        'value': 'trusted', 'mergeStrategy': 'replace',
    })
    result = rpc.request('config/read', {'cwd': str(workspace), 'includeLayers': True})
    layer = _active_project_layer(result, workspace)
    if layer is None or layer.get('disabledReason'):
        raise _error('Codex 尚未加载项目连接配置，请在 Codex 确认项目权限后重试。')


def _verify_project_instructions(rpc, workspace):
    result = rpc.request('config/read', {'cwd': str(workspace), 'includeLayers': True})
    configured = (result.get('config') or {}).get('model_instructions_file')
    expected = workspace / '.codex/management-instructions.md'
    if (not isinstance(configured, str) or not Path(configured).is_absolute()
            or _key(configured) != _key(expected) or not expected.is_file()):
        raise _error('当前 Codex 尚未加载本项目的新协助规则；请检查该项目的自定义设置。')


async def _probe_mcp_async(workspace, data_dir):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    config = tomllib.loads((workspace / '.codex/config.toml').read_text('utf-8-sig'))['mcp_servers']['personal_management']
    params = StdioServerParameters(command=config['command'], args=config.get('args', []), env=config.get('env'))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            available = await session.list_tools()
            required = {'begin_context', 'query_business', 'execute_command', 'save_plan', 'record_feedback',
                        'begin_discussion', 'submit_candidate'}
            if not required.issubset({t.name for t in available.tools}):
                raise _error('业务接口缺少必要的读写能力。')
            reply = await session.call_tool('begin_context', {})
            if reply.is_error:
                raise _error('业务接口读取当前上下文失败。')
            data = reply.structured_content
            if data is None:
                data = json.loads(next(c.text for c in reply.content if c.type == 'text'))
            if not isinstance(data.get('data_dir'), str) or _key(data['data_dir']) != _key(data_dir) or not data.get('epoch'):
                raise _error('接口连接了其他数据空间。')


def _probe_mcp(workspace, data_dir):
    async def bounded():
        async with asyncio.timeout(25):
            await _probe_mcp_async(workspace, data_dir)
    try:
        asyncio.run(bounded())
    except Exception as exc:
        raise _error('业务接口验证失败，请检查软件服务或重新保存设置。') from exc


def _save_binding(workspace, value):
    path = workspace / BINDING_FILE
    if path.is_symlink():
        raise _error('项目连接记录不能是符号链接。')
    temp = workspace / (BINDING_FILE + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def project_binding(workspace):
    """Local binding cache, never a claim that the remote project still exists."""
    workspace = Path(workspace).resolve()
    path = workspace / BINDING_FILE
    try:
        if path.is_symlink() or path.stat().st_size > 16384:
            return None
        value = json.loads(path.read_text('utf-8'))
        if value.get('status') != 'ready' or _key(value['workspace']) != _key(workspace):
            return None
        ident = value.get('project_id')
        return value if isinstance(ident, str) and 0 < len(ident) <= 200 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def require_ready_project(value, workspace):
    """A setup acknowledgement must identify this verified business project."""
    try:
        if (not isinstance(value, dict) or value.get('status') != 'ready'
                or value.get('mcp_verified') is not True
                or value.get('instructions_verified') is not True
                or not isinstance(value.get('project_id'), str)
                or not 0 < len(value['project_id']) <= 200
                or not isinstance(value.get('workspace'), str)
                or not Path(value['workspace']).is_absolute()
                or _key(value['workspace']) != _key(workspace)):
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise _error('当前项目和业务接口尚未完成核对，未报告连接成功。') from None
    return value


def ensure_project(data_dir, ai_config):
    """Prepare, register, activate and verify one project for this data space."""
    from .codex_workspace import prepare_workspace, WorkspaceError
    if not _LOCK.acquire(blocking=False):
        raise _error('另一次配置正在进行，请稍后重试。')
    rpc, workspace = None, None
    try:
        prepared = prepare_workspace(data_dir)
        workspace = Path(prepared['workspace']).resolve()
        from .desktop_seed import PlainAppServer
        rpc = PlainAppServer(workspace, timeout=25)
        rpc.__enter__()
        _activate_project_config(rpc, workspace)
        _verify_project_instructions(rpc, workspace)
        _probe_mcp(workspace, Path(data_dir).resolve())
        project, created, opened = _desktop_project(rpc, workspace)
        rpc.close()
        rpc = None
        result = {**prepared, 'status': 'ready', 'name': project['name'], 'project_path': str(workspace),
                  'project_id': project['id'], 'created': created, 'mcp_verified': True,
                  'instructions_verified': True, 'desktop_open_requested': opened}
        _save_binding(workspace, result)
        return result
    except (ai.AIError, BusinessError, OSError, ValueError, KeyError, TypeError) as exc:
        if workspace is not None:
            try:
                _save_binding(workspace, {**(project_binding(workspace) or {}), 'status': 'needs_repair', 'workspace': str(workspace)})
            except (OSError, BusinessError):
                pass
        if isinstance(exc, BusinessError):
            raise
        if isinstance(exc, WorkspaceError):
            raise _error(str(exc)) from exc
        if isinstance(exc, ai.AIError):
            raise _error('本机 Codex 未完成项目注册或配置检查；请更新 Codex 后重试。') from exc
        raise _error('无法准备项目文件；已有自定义内容会保留，请检查连接说明。') from exc
    finally:
        if rpc is not None:
            rpc.close()
        _LOCK.release()
