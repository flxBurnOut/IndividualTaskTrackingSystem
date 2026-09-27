"""Release identity for disposable entrances and the data-owning service.

This is deliberately independent of the database schema. A schema-compatible
database does not imply that an older MCP process exposes current commands.
"""
from __future__ import annotations

from . import __version__


PROTOCOL_VERSION = 1
CAPABILITIES = ('release_handshake', 'idle_update_shutdown')
VERSION_HEADER = 'X-PersonalManagement-Version'
PROTOCOL_HEADER = 'X-PersonalManagement-Protocol'
ENTRANCE_HEADER = 'X-PersonalManagement-Entrance'


def service_contract():
    return {'app_version': __version__, 'protocol_version': PROTOCOL_VERSION,
            'capabilities': list(CAPABILITIES)}


def client_headers(entrance='gui'):
    return {VERSION_HEADER: __version__, PROTOCOL_HEADER: str(PROTOCOL_VERSION), ENTRANCE_HEADER: entrance}


def matches_contract(value):
    return (isinstance(value, dict) and value.get('app_version') == __version__
            and type(value.get('protocol_version')) is int
            and value['protocol_version'] == PROTOCOL_VERSION
            and isinstance(value.get('capabilities'), list)
            and all(capability in value['capabilities'] for capability in CAPABILITIES))


def mismatch_details(value):
    value = value if isinstance(value, dict) else {}
    return {'client_version': __version__, 'service_version': value.get('app_version'),
            'client_protocol': PROTOCOL_VERSION, 'service_protocol': value.get('protocol_version'),
            'required_capabilities': list(CAPABILITIES)}


def service_mismatch_message(value):
    version = value.get('app_version') if isinstance(value, dict) else None
    advertised = version if isinstance(version, str) and len(version) <= 64 else '无法识别的旧版本'
    return (f'软件入口为 {__version__}，后台为 {advertised}，版本或接口能力尚未对齐；未执行本次操作。'
            '请使用新版安装包完成更新，再从同一数据空间打开软件。'
            '如果提示来自 Codex，请在任务空闲后重启其 MCP 连接，加载新版事务助手接口。')
