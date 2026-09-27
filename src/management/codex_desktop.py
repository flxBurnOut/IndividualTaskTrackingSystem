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
import time
import uuid
import psutil
from .ai import find_codex
from .schemas import BusinessError

CONFIG_NAME = 'desktop-launch.json'


def _error(text, code='codex_desktop_setup'):
    return BusinessError(code, text)


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


def _owned_config(path, root):
    _plain(path)
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 16384:
            raise ValueError()
        previous = json.loads(path.read_text('utf-8'))
        if (not isinstance(previous, dict) or previous.get('managed_by') != 'personal-management'
                or previous.get('schema_version') != 1
                or Path(previous['data_dir']).resolve() != root):
            raise ValueError()
        return previous
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise _error('桌面启动配置无法确认归属，已保留原文件。请在设置中检查连接。') from exc


def _write_config(path, config):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), 'utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _prepare(data_dir, executable=''):
    root = Path(data_dir).resolve()
    bridge = root / 'codex-desktop-bridge'
    _plain(bridge)
    path = bridge / CONFIG_NAME
    previous = _owned_config(path, root)
    if executable is None:
        # Legacy callers have no fresh settings snapshot. Keep the saved user
        # selection; older metadata only knew the last discovered executable.
        executable = previous.get('executable') if previous else ''
        if executable is None:
            old_real = previous.get('real_cli', '')
            executable = old_real if Path(old_real).is_file() else ''
    real = find_codex(executable)
    shim = _shim_path()
    if not real or Path(real).resolve() == shim:
        raise _error('未找到原始 Codex 程序，无法准备桌面连接。')
    desktop = _desktop_paths()
    if not desktop:
        raise _error('未找到已安装的 Codex 桌面应用。请先安装并登录 Codex。')
    for target in (Path(real).resolve(), shim, desktop[0]):
        _plain(target)
        if not target.is_absolute() or not target.is_file():
            raise _error('Codex 程序位置已失效，请在设置中检查程序位置。')
    bridge.mkdir(exist_ok=True)
    config = {'schema_version': 1, 'managed_by': 'personal-management', 'data_dir': str(root),
              'real_cli': str(Path(real).resolve()), 'shim': str(shim), 'desktop': str(desktop[0]),
              'executable': executable}
    if previous != config:
        _write_config(path, config)
    return config


def _launch_lock(data_dir):
    from .runtime import OwnerLock
    path = Path(data_dir).resolve() / 'codex-desktop-bridge' / 'launch.lock'
    _plain(path)
    return OwnerLock(path)


def prepare(data_dir, executable=''):
    lock = _launch_lock(data_dir)
    if not lock.acquire():
        raise _error('正在准备 Codex 连接，请稍后重试。', 'codex_desktop_busy')
    try:
        _prepare(data_dir, executable)
    finally:
        lock.release()
    return {'prepared': True, 'requires_desktop_restart': True}


def _launch(data_dir, executable='', cancel=None):
    bridge = Path(data_dir).resolve() / 'codex-desktop-bridge'
    from .ai_shared import connection_status
    config = _prepare(data_dir, executable)
    desktop, shim, real = (Path(config[k]) for k in ('desktop', 'shim', 'real_cli'))
    pending = bridge / 'launch-pending.json'
    _plain(pending)
    if connection_status(bridge, expected_bridge_executable=shim, expected_executable=real).get('ready'):
        pending.unlink(missing_ok=True)
        return {'launched': False, 'ready': True}
    # Persist the brief startup window so another GUI or the scheduler cannot
    # dispatch a second desktop before Windows exposes the first process.
    if pending.exists():
        try:
            if pending.stat().st_size > 16384:
                raise ValueError()
            attempt = json.loads(pending.read_text('utf-8'))
            age = time.time() - float(attempt['started_at'])
            same = attempt.get('config') == config
        except (OSError, ValueError, TypeError, KeyError):
            raise _error('Codex 启动记录无法读取，请在设置中重新检查连接。') from None
        if same and 0 <= age < 30:
            return {'launched': False, 'ready': False}
        pending.unlink(missing_ok=True)
        if same:
            raise _error('Codex 已启动但未建立连接。请检查 Codex 窗口，然后在软件中点“重新检查连接”。', 'codex_desktop_start_timeout')
    for process in psutil.process_iter(['exe']):
        try:
            running = Path(process.info['exe'] or '')
            if running == desktop or (running.name.lower() in {'codex.exe', 'chatgpt.exe'} and running.parent.name.lower() == 'app' and running.parent.parent.name.startswith('OpenAI.Codex_')):
                raise _error('请保存并退出 Codex，保持管理软件打开；退出后会自动连接。', 'codex_desktop_restart_required')
        except (psutil.Error, OSError):
            continue
    environment = dict(os.environ)
    environment['CODEX_CLI_PATH'] = str(shim)
    environment['PM_CODEX_REAL_CLI'] = str(real)
    environment['PM_CODEX_BRIDGE_DIR'] = str(bridge)
    # Explicitly use the normal desktop stdio path through our local shim.
    environment.pop('CODEX_APP_SERVER_WS_URL', None)
    environment.pop('CODEX_APP_SERVER_USE_LOCAL_DAEMON', None)
    if cancel is not None and cancel.is_set():
        raise _error('本次连接已取消。', 'codex_desktop_cancelled')
    _write_config(pending, {'started_at': time.time(), 'config': config})
    try:
        subprocess.Popen([str(desktop)], cwd=desktop.parent, env=environment,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
    except OSError as exc:
        pending.unlink(missing_ok=True)
        raise _error('无法打开 Codex，请检查安装后重新检查连接。') from exc
    return {'launched': True, 'ready': False}


def launch(data_dir, executable=None, cancel=None):
    if cancel is not None and cancel.is_set():
        raise _error('本次连接已取消。', 'codex_desktop_cancelled')
    lock = _launch_lock(data_dir)
    if not lock.acquire():
        return {'launched': False, 'ready': False}
    try:
        return _launch(data_dir, executable, cancel)
    finally:
        lock.release()


def connect_step(data_dir, executable='', cancel=None):
    """One bounded, model-free step for the GUI's automatic connection flow."""
    try:
        result = launch(data_dir, executable, cancel)
    except BusinessError as error:
        if error.code != 'codex_desktop_restart_required':
            raise
        return {'state': 'waiting_for_exit', 'ready': False, 'message': error.message}
    if result.get('ready'):
        return {'state': 'ready', 'ready': True, 'message': 'Codex 已连接，可以直接发送。'}
    return {'state': 'starting', 'ready': False, 'message': '正在打开并连接 Codex…'}


def ensure_connection(data_dir, cancel, stop=None, timeout=30, executable=''):
    """A configured send may start a closed desktop, never terminate an open one."""
    import time
    from .ai_shared import connection_status
    bridge = Path(data_dir).resolve() / 'codex-desktop-bridge'
    def cancelled():
        return cancel.is_set() or (stop is not None and stop.is_set())
    if cancelled():
        raise _error('本次请求已取消。')
    if launch(data_dir, executable, cancel).get('ready'):
        return
    expected_real = Path(find_codex(executable)).resolve()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancelled():
            raise _error('本次请求已取消。')
        if connection_status(bridge, expected_bridge_executable=_shim_path(), expected_executable=expected_real).get('ready'):
            return
        cancel.wait(.2)
    raise _error('Codex 桌面已启动但尚未建立同步连接。请检查 Codex 的启动提示，再重新发送；消息没有交给独立后台连接。')
