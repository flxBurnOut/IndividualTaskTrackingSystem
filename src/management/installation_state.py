"""Per-user installed data selection; portable invocations stay independent."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import uuid

INSTALL_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{EA7A3A48-A445-44C0-A56D-3F5275DD323E}_is1'


def _registered_location():
    if os.name != 'nt':
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INSTALL_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return {name: winreg.QueryValueEx(key, name)[0]
                    for name in ('InstallLocation', 'DataDirectory')}
    except FileNotFoundError:
        return None


def installed_data_dir(executable=None):
    if executable is None:
        if not getattr(sys, 'frozen', False):
            return None
        executable = sys.executable
    record = _registered_location()
    if not record or Path(executable).resolve().parent != Path(record['InstallLocation']).resolve():
        return None
    path = Path(record['DataDirectory'])
    if not path.is_absolute():
        raise ValueError('已登记的数据位置无效，请使用“选择数据空间”指定原目录。')
    return path.resolve()


def remember_installed_data_dir(data_dir, executable):
    """Only a registered installation may change its next-upgrade selection."""
    from .data_space import CHANNEL
    if CHANNEL == 'beta':
        return  # Beta must never change the stable installation's registry.
    if installed_data_dir(executable) is None:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INSTALL_KEY, 0,
                        winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
        winreg.SetValueEx(key, 'DataDirectory', 0, winreg.REG_SZ, str(Path(data_dir).resolve()))


def resume_after_update(data_dir):
    """An explicit GUI launch resumes a stopped space, never a background MCP."""
    from .runtime import OwnerLock, read_startup_failure, start_service
    from .data_space import require_beta_dir
    root = require_beta_dir(data_dir)
    marker = root / 'update_pending.json'
    if not marker.exists():
        return
    lock = OwnerLock(root / 'service.lock')
    if not lock.acquire():
        raise ValueError('后台仍在退出以准备更新。请稍后重新打开软件；数据仍保留在原目录。')
    try:
        value = json.loads(marker.read_text('utf-8'))
        if not isinstance(value, dict) or value.get('format') != 'personal-management-update/1':
            raise ValueError('更新状态无法确认，已保留原文件。请检查数据目录中的 update_pending.json。')
        token = uuid.uuid4().hex
        temporary = marker.with_name('update_pending.json.new')
        temporary.write_text(json.dumps({**value, 'resume_token': token}, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, marker)
    finally:
        lock.release()
    # Only the explicitly selected executable can consume this token while it
    # owns service.lock. Other aware MCP clients remain blocked throughout.
    attempted_at = time.time()
    start_service(root, resume_token=token)
    deadline = time.monotonic() + 20
    while marker.exists() and time.monotonic() < deadline:
        failure = read_startup_failure(root, not_before=attempted_at)
        if failure:
            raise ValueError(failure['message'])
        time.sleep(.1)
    if marker.exists():
        raise ValueError('新版后台尚未完成启动，更新暂停标记已保留。请检查数据目录中的 diagnostic.log 后重试。')
