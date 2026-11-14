"""Release-channel boundaries checked before database or service access.

The channel is part of the shipped code, never an environment override. Stable
keeps the user's selected data space; Beta owns only its isolated checkout.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path


CHANNEL = 'stable'
CHECK_PROFILES = {'focused', 'full', 'gui', 'package', 'upgrade', 'tray', 'installer'}


class BetaIsolationError(ValueError):
    pass


def workspace_root():
    return Path(__file__).resolve().parents[2]


def default_beta_dir():
    return workspace_root() / '.beta-data' / 'default'


def require_packaged_channel():
    if CHANNEL != 'stable':
        raise BetaIsolationError('Beta 安装与发布通道尚未实现；已阻止使用正式版打包或发布入口。')


def require_channel_dir(data_dir, *, inspect_runtime=True):
    if CHANNEL == 'beta':
        return require_beta_dir(data_dir)
    selected = Path(data_dir).expanduser().absolute()
    root = selected.resolve()
    if any(part.casefold() == '.beta-data' for part in (*selected.parts, *root.parts)):
        raise BetaIsolationError('正式版不能打开 Beta 测试数据，请选择原正式数据目录；未打开数据库或连接后台。')
    # Rendering workspace configuration must not read discovery credentials.
    # Any resulting client/storage access still uses the default full check.
    if not inspect_runtime:
        return root
    # The discovery record is only an additional isolation fence, never an
    # authorization to use an arbitrary port or to overwrite existing data.
    try:
        record = json.loads((root / 'runtime.json').read_text('utf-8'))
    except (OSError, ValueError):
        record = {}
    if isinstance(record, dict) and isinstance(record.get('service_contract'), dict):
        if record['service_contract'].get('channel') == 'beta':
            raise BetaIsolationError('所选空间属于 Beta 后台；正式版未连接，也未修改其中的数据。')
    return root


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
                try:
                    metadata = path.lstat()
                except FileNotFoundError:
                    # SQLite may remove WAL/SHM files while a client checks the
                    # directory. A vanished entry cannot redirect this access.
                    continue
                if (stat.S_ISLNK(metadata.st_mode)
                        or getattr(metadata, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                        or stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1):
                    raise ValueError('linked data file')
        return root
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        raise BetaIsolationError(
            'Beta 只能打开本工作目录 .beta-data 内的独立数据空间，'
            '或标准检查入口创建的临时空间；不接受目录链接或共享的数据文件。'
            '未连接后台或打开数据库。请选择 Beta 数据目录。') from error
