"""Prepare a reversible launcher for the desktop-owned shared Codex connection.

No global environment, Codex settings, running processes or app files are edited.
The launcher only changes the environment of the desktop process it starts.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import psutil
from .ai import find_codex
from .schemas import BusinessError

CONFIG_NAME = 'desktop-launch.json'


def _error(text):
    return BusinessError('codex_desktop_setup', text)


def _desktop_paths():
    paths = set()
    for process in psutil.process_iter(['name', 'exe']):
        try:
            path = Path(process.info['exe'] or '')
            if path.name.lower() in {'codex.exe', 'chatgpt.exe'} and path.parent.name.lower() == 'app' and path.parent.parent.name.startswith('OpenAI.Codex_'):
                paths.add(path)
        except (psutil.Error, OSError):
            pass
    if paths:
        return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
    apps = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'WindowsApps'
    try:
        for name in ('ChatGPT.exe', 'Codex.exe'):
            paths.update(apps.glob('OpenAI.Codex_*/app/' + name))
    except OSError:
        pass
    existing = [p for p in paths if p.is_file()]
    if not existing and os.name == 'nt':
        # WindowsApps may deny directory enumeration while allowing package files.
        command = 'Get-AppxPackage -Name OpenAI.Codex | Select-Object -ExpandProperty InstallLocation'
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                    capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            for line in result.stdout.splitlines():
                location = Path(line.strip())
                if location.is_absolute() and location.name.startswith('OpenAI.Codex_'):
                    for name in ('ChatGPT.exe', 'Codex.exe'):
                        path = location / 'app' / name
                        if path.is_file():
                            existing.append(path)
                            break
        except (OSError, subprocess.TimeoutExpired):
            pass
    return sorted(existing, key=lambda p: p.stat().st_mtime, reverse=True)


def _shim_path():
    if not getattr(sys, 'frozen', False):
        raise _error('桌面连接模式需要使用完整发布版运行，请先安装新版软件。')
    path = Path(sys.executable).with_name('PersonalManagementCodex.exe')
    if not path.is_file():
        raise _error('软件包缺少 Codex 连接程序，请保持发布目录完整。')
    return path.resolve()


def _plain(path):
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            raise _error('连接目录不能使用符号链接或目录联接。')


def prepare(data_dir, executable=''):
    root = Path(data_dir).resolve()
    bridge = root / 'codex-desktop-bridge'
    _plain(bridge)
    real = find_codex(executable)
    shim = _shim_path()
    if not real or Path(real).resolve() == shim:
        raise _error('未找到原始 Codex 程序，无法准备桌面连接。')
    desktop = _desktop_paths()
    if not desktop:
        raise _error('未找到已安装的 Codex 桌面应用。请先安装并登录 Codex。')
    bridge.mkdir(exist_ok=True)
    config = {'schema_version': 1, 'managed_by': 'personal-management', 'data_dir': str(root),
              'real_cli': str(Path(real).resolve()), 'shim': str(shim), 'desktop': str(desktop[0])}
    path = bridge / CONFIG_NAME
    _plain(path)
    if path.exists():
        if path.stat().st_size > 16384:
            raise _error('桌面启动配置异常，已保留原文件。')
        try:
            previous = json.loads(path.read_text('utf-8'))
        except (OSError, ValueError) as exc:
            raise _error('桌面启动配置无法读取，已保留原文件。') from exc
        if previous.get('managed_by') != 'personal-management':
            raise _error('该位置已有其他启动配置，未覆盖。')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), 'utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {'prepared': True, 'requires_desktop_restart': True}


def launch(data_dir):
    bridge = Path(data_dir).resolve() / 'codex-desktop-bridge'
    from .ai_shared import connection_status
    path = bridge / CONFIG_NAME
    _plain(path)
    try:
        if path.stat().st_size > 16384:
            raise ValueError()
        config = json.loads(path.read_text('utf-8'))
        if config.get('managed_by') != 'personal-management' or config.get('schema_version') != 1 or Path(config['data_dir']).resolve() != bridge.parent:
            raise ValueError()
        desktop, shim, real = (Path(config[k]) for k in ('desktop', 'shim', 'real_cli'))
        if any(not p.is_absolute() or not p.is_file() for p in (desktop, shim, real)) or shim.resolve() == real.resolve():
            raise ValueError()
        for executable in (desktop, shim, real):
            _plain(executable)
        if shim.resolve() != _shim_path():
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise _error('请先在 Codex 协助设置中保存一次，准备当前版本的桌面连接。') from exc
    if connection_status(bridge, expected_bridge_executable=shim, expected_executable=real).get('ready'):
        return {'launched': False, 'ready': True}
    for process in psutil.process_iter(['exe']):
        try:
            running = Path(process.info['exe'] or '')
            if running == desktop or (running.name.lower() in {'codex.exe', 'chatgpt.exe'} and running.parent.name.lower() == 'app' and running.parent.parent.name.startswith('OpenAI.Codex_')):
                raise _error('请先保存并退出 Codex 桌面，再点“启动 Codex 连接模式”。这会让桌面和管理软件共用连接；不会自动关闭你正在进行的任务。')
        except (psutil.Error, OSError):
            continue
    environment = dict(os.environ)
    environment['CODEX_CLI_PATH'] = str(shim)
    environment['PM_CODEX_REAL_CLI'] = str(real)
    environment['PM_CODEX_BRIDGE_DIR'] = str(bridge)
    # Explicitly use the normal desktop stdio path through our local shim.
    environment.pop('CODEX_APP_SERVER_WS_URL', None)
    environment.pop('CODEX_APP_SERVER_USE_LOCAL_DAEMON', None)
    subprocess.Popen([str(desktop)], cwd=desktop.parent, env=environment,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)
    return {'launched': True, 'ready': False}


def ensure_connection(data_dir, cancel, stop=None, timeout=20):
    """A configured send may start a closed desktop, never terminate an open one."""
    import time
    from .ai_shared import connection_status
    bridge = Path(data_dir).resolve() / 'codex-desktop-bridge'
    def cancelled():
        return cancel.is_set() or (stop is not None and stop.is_set())
    if cancelled():
        raise _error('本次请求已取消。')
    if launch(data_dir).get('ready'):
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancelled():
            raise _error('本次请求已取消。')
        if connection_status(bridge, expected_bridge_executable=_shim_path()).get('ready'):
            return
        cancel.wait(.2)
    raise _error('Codex 桌面已启动但尚未建立同步连接。请检查 Codex 的启动提示，再重新发送；消息没有交给独立后台连接。')
