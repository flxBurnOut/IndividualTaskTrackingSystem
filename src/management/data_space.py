"""Beta source-workspace boundaries, checked before database or service access.

This is deliberately not a switch supplied by an environment variable. The
current Beta distribution may only own spaces in this checkout, or disposable
fixtures created by packaging/check.py. A separate packaged channel is pending.
"""
from __future__ import annotations

import json
from pathlib import Path


CHANNEL = 'beta'
CHECK_PROFILES = {'focused', 'full', 'gui', 'package', 'upgrade', 'tray', 'installer'}


class BetaIsolationError(ValueError):
    pass


def workspace_root():
    return Path(__file__).resolve().parents[2]


def default_beta_dir():
    return workspace_root() / '.beta-data' / 'default'


def require_packaged_channel():
    raise BetaIsolationError('Beta 安装与发布通道尚未实现；已阻止使用正式版打包或发布入口。')


def require_beta_dir(data_dir):
    """Validate without creating anything, opening SQLite or contacting a port."""
    workspace = workspace_root()
    try:
        selected = Path(data_dir).expanduser().absolute()
        root = selected.resolve()
        permitted = root.is_relative_to(workspace / '.beta-data')
        if not permitted and root.is_relative_to(workspace / '.build' / 'checks'):
            parts = root.relative_to(workspace / '.build' / 'checks').parts
            if len(parts) >= 2 and parts[0] in CHECK_PROFILES and parts[1] == 'work':
                work = workspace / '.build' / 'checks' / parts[0] / 'work'
                marker = work / '.disposable-test-work.json'
                if marker.stat().st_size <= 4096:
                    record = json.loads(marker.read_text('utf-8'))
                    permitted = (isinstance(record, dict)
                                 and record.get('format') == 'personal-management-test-work/1'
                                 and Path(record.get('path', '')).resolve() == work)
        if not permitted:
            raise ValueError('outside Beta workspace')
        # Do not let an apparently local directory, discovery file or SQLite
        # hard link redirect this distribution into the daily-use installation.
        for path in (selected, *selected.parents):
            if path == workspace:
                break
            if path.is_symlink() or path.is_junction():
                raise ValueError('linked data directory')
        if root.exists():
            for path in root.iterdir():
                if (path.is_symlink() or path.is_junction()
                        or path.is_file() and path.stat().st_nlink > 1):
                    raise ValueError('linked data file')
        return root
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        raise BetaIsolationError(
            'Beta 只能打开本工作目录 .beta-data 内的独立数据空间，'
            '或标准检查入口创建的临时空间；不接受目录链接或共享的数据文件。'
            '未连接后台或打开数据库。请选择 Beta 数据目录。') from error
