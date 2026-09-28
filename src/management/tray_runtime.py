"""Launch the Windows tray observer independently of GUI and MCP lifetimes."""
from __future__ import annotations

import logging
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .runtime import OwnerLock


def tray_enabled():
    return (os.name == 'nt' and os.environ.get('PERSONAL_MANAGEMENT_NO_TRAY') != '1'
            and os.environ.get('QT_QPA_PLATFORM', '').split(':')[0] not in {'offscreen', 'minimal'})


def gui_args(data_dir, *, tray=False, show_update=False):
    if getattr(sys, 'frozen', False):
        executable = Path(sys.executable).with_name('PersonalManagement.exe')
        if not executable.is_file():
            raise FileNotFoundError('找不到当前版本的管理软件窗口程序。')
        args = [str(executable)]
    else:
        args = [sys.executable, '-m', 'management']
    args += ['--data-dir', str(Path(data_dir).resolve())]
    if tray:
        args.append('--tray')
    if show_update:
        args.append('--show-update')
    return args


def launch_gui(data_dir, *, tray=False, show_update=False):
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
               'stderr': subprocess.DEVNULL, 'close_fds': True, 'env': env}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(gui_args(data_dir, tray=tray, show_update=show_update), **options)


def observer_running(data_dir):
    lock = OwnerLock(Path(data_dir) / 'tray.lock')
    if not lock.acquire():
        return True
    lock.release()
    return False


def tray_status(data_dir):
    path = Path(data_dir) / 'tray-status.json'
    try:
        if path.stat().st_size > 4096:
            return {'running': False}
        value = json.loads(path.read_text('utf-8'))
        if (not isinstance(value, dict) or Path(value['data_dir']).resolve() != Path(data_dir).resolve()
                or not isinstance(value.get('checked_at'), (int, float))
                or not 0 <= time.time() - value['checked_at'] < 15):
            return {'running': False}
        return {'running': observer_running(data_dir), **{key: value.get(key) for key in
                ('version', 'icon_registered', 'system_tray_available', 'status')}}
    except (OSError, ValueError, KeyError, TypeError):
        return {'running': False}


def start_tray_supervisor(data_dir, stop):
    if not tray_enabled():
        return None
    def supervise():
        child = None
        while not stop.is_set():
            try:
                if (child is None or child.poll() is not None) and not observer_running(data_dir):
                    if stop.is_set():
                        break
                    child = launch_gui(data_dir, tray=True)
            except Exception as error:
                logging.getLogger('management').warning('Tray launch failed (%s).', type(error).__name__)
            stop.wait(5)
        # The observer reads the service's state and exits after normal stop.
        # Never terminate a GUI process or discard a user's pending edit here.
    thread = threading.Thread(target=supervise, name='management-tray-supervisor', daemon=True)
    thread.start()
    return thread
