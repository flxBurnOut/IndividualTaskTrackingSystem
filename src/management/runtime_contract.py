"""Release identity for disposable entrances and the data-owning service.

This is deliberately independent of the database schema. A schema-compatible
database does not imply that an older MCP process exposes current commands.
"""
from __future__ import annotations

from . import __version__
from .data_space import CHANNEL


PROTOCOL_VERSION = 1
CAPABILITIES = ('release_handshake', 'idle_update_shutdown', 'appearance_accent')
VERSION_HEADER = 'X-PersonalManagement-Version'
PROTOCOL_HEADER = 'X-PersonalManagement-Protocol'
ENTRANCE_HEADER = 'X-PersonalManagement-Entrance'
CHANNEL_HEADER = 'X-PersonalManagement-Channel'


def service_contract():
    return {'app_version': __version__, 'protocol_version': PROTOCOL_VERSION, 'channel': CHANNEL,
            'capabilities': list(CAPABILITIES)}


def client_headers(entrance='gui'):
    return {VERSION_HEADER: __version__, PROTOCOL_HEADER: str(PROTOCOL_VERSION),
            ENTRANCE_HEADER: entrance, CHANNEL_HEADER: CHANNEL}


def matches_contract(value):
    return (isinstance(value, dict) and value.get('app_version') == __version__
            and value.get('channel') == CHANNEL
            and type(value.get('protocol_version')) is int
            and value['protocol_version'] == PROTOCOL_VERSION
            and isinstance(value.get('capabilities'), list)
            and all(capability in value['capabilities'] for capability in CAPABILITIES))


def mismatch_details(value):
    value = value if isinstance(value, dict) else {}
    return {'client_version': __version__, 'service_version': value.get('app_version'),
            'client_channel': CHANNEL, 'service_channel': value.get('channel'),
            'client_protocol': PROTOCOL_VERSION, 'service_protocol': value.get('protocol_version'),
            'required_capabilities': list(CAPABILITIES)}


def service_mismatch_message(value):
    version = value.get('app_version') if isinstance(value, dict) else None
    advertised = version if isinstance(version, str) and len(version) <= 64 else '无法识别的旧版本'
    return (f'软件入口为 Beta {__version__}，后台为 {advertised}，通道、版本或接口能力尚未对齐；未执行本次操作。'
            '请先保存并关闭 Beta 主窗口；若 Beta 后台还有任务，等待完成后，在 Beta 托盘选择“退出 Beta 软件与后台”。'
            '确认 Beta 后台退出后，再从本 Beta 工作目录的“启动个人事务管理Beta.vbs”重新启动。'
            '仅重新打开窗口不会替换仍在运行的旧后台；当前尚未提供独立 Beta 安装包。'
            '如果提示来自 Codex，请在任务空闲后重启其 MCP 连接，加载新版事务助手接口。')
