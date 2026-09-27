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
    from .installation_state import installed_data_dir
    installed = installed_data_dir()
    if installed is not None:
        return installed
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'PersonalManagement' / 'data'


class DataSpaceMismatch(ValueError):
    """Discovery or service state does not identify the selected data space."""


class UpdatePending(RuntimeError):
    code = 'update_pending'
    message = '此数据空间已暂停后台以准备更新。请完成安装后打开个人事务管理；若取消更新，也请主动打开软件恢复使用。'

    def __init__(self):
        super().__init__(self.message)


def require_no_pending_update(root, resume_token=None):
    # A malformed marker is still a maintenance fence, never permission to
    # silently restart an old MCP-owned binary during file replacement.
    marker = Path(root) / 'update_pending.json'
    if not marker.exists():
        return
    if isinstance(resume_token, str) and 16 <= len(resume_token) <= 128:
        try:
            value = json.loads(marker.read_text('utf-8'))
            require_data_dir(value.get('data_dir'), root)
            if (value.get('format') == 'personal-management-update/1'
                    and value.get('resume_token') == resume_token):
                return
        except (OSError, ValueError, AttributeError):
            pass
    raise UpdatePending()


def read_startup_failure(root, not_before):
    """Return only a fresh failure from this release and selected data space.

    A failed previous launch must not prevent a later corrected launch. Callers
    start their own attempt before polling this record; no business data or
    exception payload is exposed here.
    """
    from . import __version__
    path = Path(root) / 'startup_failure.json'
    try:
        if path.stat().st_size > 16 * 1024:
            return None
        value = json.loads(path.read_text('utf-8'))
        if (not isinstance(value, dict) or value.get('format') != 'personal-management-startup-failure/1'
                or value.get('app_version') != __version__):
            return None
        failed = value.get('failed_at_unix')
        if type(failed) not in {int, float} or not not_before <= failed <= time.time() + 5:
            return None
        require_data_dir(value.get('data_dir'), root)
        if (not isinstance(value.get('code'), str) or not 1 <= len(value['code']) <= 80
                or not all(character in 'abcdefghijklmnopqrstuvwxyz0123456789_' for character in value['code'])
                or not isinstance(value.get('message'), str) or not 1 <= len(value['message']) <= 2000):
            return None
        return {'code': value['code'], 'message': value['message']}
    except (OSError, ValueError, TypeError):
        return None


def require_data_dir(value, expected):
    """Match an advertised absolute path, preserving valid filesystem aliases.

    resolve() handles junctions/symlinks and normcase() handles Windows casing.
    samefile() also accepts an existing directory's alternate Windows spelling
    (for example an extended-length or short path) without guessing identities.
    """
    try:
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError()
        actual, selected = Path(value).resolve(), Path(expected).resolve()
        try:
            same = actual.samefile(selected)
        except OSError:
            # A not-yet-created selected directory can still be compared, but
            # never let case folding override two known, distinct directories.
            same = os.path.normcase(str(actual)) == os.path.normcase(str(selected))
        if same:
            return
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    raise DataSpaceMismatch('业务服务的数据目录与所选数据空间不一致或无法确认；未接受该连接，请核对所选数据目录及连接信息。')


def discovery(root):
    try:
        value = json.loads((Path(root) / 'runtime.json').read_text('utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get('host') != '127.0.0.1' or type(value.get('port')) is not int or not 1 <= value['port'] <= 65535 or not isinstance(value.get('token'), str):
        return None
    # A copied live discovery file is not an absent/dead service. Do not silently
    # bootstrap or send its credentials to the other data space's endpoint.
    require_data_dir(value.get('data_dir'), root)
    return value


def _service_args(root, bootstrap=False, resume_token=None):
    executable = Path(sys.executable)
    if getattr(sys, 'frozen', False):
        sibling = executable.with_name('PersonalManagementService.exe')
        if sibling.exists():
            executable = sibling
    args = [str(executable)]
    if not getattr(sys, 'frozen', False):
        args += ['-m', 'management']
    args += ['--bootstrap-service' if bootstrap else '--service', '--data-dir', str(root)]
    if resume_token is not None:
        args += ['--resume-update', resume_token]
    return args


def bootstrap_service(root, resume_token=None):
    # A short-lived broker prevents MCP SDK process-tree cleanup from owning
    # the long-lived data service. Break away from a Windows job when permitted.
    require_no_pending_update(root, resume_token)
    options = {'cwd': str(Path(sys.executable).parent), 'stdin': subprocess.DEVNULL,
               'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL, 'close_fds': True}
    if os.name == 'nt':
        # An MCP host may put the entire lineage in nested kill-on-close Jobs.
        # A short-lived broker and CREATE_NEW_PROCESS_GROUP do not detach it.
        # Use Windows' existing same-user process provider; no registration or
        # changes to services, scheduled tasks, or security policy are needed.
        _windows_broker_start(root, resume_token=resume_token)
        return
    else:
        options['start_new_session'] = True
    subprocess.Popen(_service_args(root, resume_token=resume_token), **options)


def _windows_broker_start(root, resume_token=None):
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
        params.CommandLine = subprocess.list2cmdline(_service_args(root, resume_token=resume_token))
        params.CurrentDirectory = str(Path(sys.executable).parent)
        params.ProcessStartupInformation = startup
        outcome = process_class.ExecMethod_('Create', params)
        if int(outcome.ReturnValue) != 0:
            raise OSError('Windows could not start the independent business service (WMI %s).' % outcome.ReturnValue)
        return int(outcome.ProcessId)
    finally:
        outcome = params = process_class = startup = provider = locator = None
        pythoncom.CoUninitialize()


def start_service(root, resume_token=None):
    root = Path(root).resolve()
    require_no_pending_update(root, resume_token)
    root.mkdir(parents=True, exist_ok=True)
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
               'stderr': subprocess.DEVNULL, 'close_fds': True}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        subprocess.run(_service_args(root, bootstrap=True, resume_token=resume_token), timeout=10, check=True, **options)
    except (OSError, subprocess.SubprocessError) as error:
        raise OSError('后台启动程序未能运行。请检查安装文件、磁盘可用空间和所选目录权限后重试。') from error
