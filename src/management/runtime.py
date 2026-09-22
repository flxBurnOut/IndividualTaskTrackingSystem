"""Service discovery and OS-owned locking; GUI and MCP are disposable clients."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


class OwnerLock:
    def __init__(self, path):
        self.path, self.stream = Path(path), None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b')
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def release(self):
        if self.stream:
            self.stream.close()
            self.stream = None


def default_data_dir():
    if os.environ.get('PERSONAL_MANAGEMENT_DATA'):
        return Path(os.environ['PERSONAL_MANAGEMENT_DATA']).resolve()
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'PersonalManagement' / 'data'


def discovery(root):
    try:
        value = json.loads((Path(root) / 'runtime.json').read_text('utf-8'))
        if value.get('host') != '127.0.0.1' or type(value.get('port')) is not int or not 1 <= value['port'] <= 65535 or not isinstance(value.get('token'), str):
            return None
        return value
    except (OSError, ValueError):
        return None


def _service_args(root, bootstrap=False):
    executable = Path(sys.executable)
    if getattr(sys, 'frozen', False):
        sibling = executable.with_name('PersonalManagementService.exe')
        if sibling.exists():
            executable = sibling
    args = [str(executable)]
    if not getattr(sys, 'frozen', False):
        args += ['-m', 'management']
    return args + ['--bootstrap-service' if bootstrap else '--service', '--data-dir', str(root)]


def bootstrap_service(root):
    # A short-lived broker prevents MCP SDK process-tree cleanup from owning
    # the long-lived data service. Break away from a Windows job when permitted.
    options = {'cwd': str(Path(sys.executable).parent), 'stdin': subprocess.DEVNULL,
               'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL, 'close_fds': True}
    if os.name == 'nt':
        # An MCP host may put the entire lineage in nested kill-on-close Jobs.
        # A short-lived broker and CREATE_NEW_PROCESS_GROUP do not detach it.
        # Use Windows' existing same-user process provider; no registration or
        # changes to services, scheduled tasks, or security policy are needed.
        _windows_broker_start(root)
        return
    else:
        options['start_new_session'] = True
    subprocess.Popen(_service_args(root), **options)


def _windows_broker_start(root):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    locator = provider = startup = process_class = params = outcome = None
    try:
        locator = win32com.client.Dispatch('WbemScripting.SWbemLocator')
        provider = locator.ConnectServer('.', r'root\cimv2')
        startup = provider.Get('Win32_ProcessStartup').SpawnInstance_()
        startup.ShowWindow = 0
        startup.CreateFlags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB
        allowed = {'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP',
                   'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'PROGRAMDATA',
                   'PROGRAMFILES', 'PROGRAMFILES(X86)', 'HOMEDRIVE', 'HOMEPATH',
                   'USERNAME', 'USERDOMAIN', 'CODEX_HOME', 'PYTHONPATH'}
        startup.EnvironmentVariables = [key + '=' + value for key, value in os.environ.items()
                                        if key.upper() in allowed] + ['PYTHONUTF8=1']
        process_class = provider.Get('Win32_Process')
        params = process_class.Methods_('Create').InParameters.SpawnInstance_()
        params.CommandLine = subprocess.list2cmdline(_service_args(root))
        params.CurrentDirectory = str(Path(sys.executable).parent)
        params.ProcessStartupInformation = startup
        outcome = process_class.ExecMethod_('Create', params)
        if int(outcome.ReturnValue) != 0:
            raise OSError('Windows could not start the independent business service (WMI %s).' % outcome.ReturnValue)
        return int(outcome.ProcessId)
    finally:
        outcome = params = process_class = startup = provider = locator = None
        pythoncom.CoUninitialize()


def start_service(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
               'stderr': subprocess.DEVNULL, 'close_fds': True}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    subprocess.run(_service_args(root, bootstrap=True), timeout=10, check=True, **options)
