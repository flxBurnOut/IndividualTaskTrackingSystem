"""Discover and activate the current user's registered Windows Codex application.

This is independent of the desktop bridge. Package activation starts the normal
application; it neither injects a CLI adapter nor establishes a task connection.
No desktop executable, package file, or global environment is modified.
"""
from __future__ import annotations

import ctypes
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import uuid


PACKAGE_NAME = 'OpenAI.Codex'
PACKAGE_FAMILY = 'OpenAI.Codex_2p2nqsd0c76g0'
MAX_REGISTRATION_BYTES = 64 * 1024
_PACKAGE_PATTERN = re.compile(
    r'OpenAI\.Codex_(\d+\.\d+\.\d+\.\d+)_(x64|x86|arm64|neutral)__2p2nqsd0c76g0')
_APPLICATION_PATTERN = re.compile(r'[A-Za-z][A-Za-z0-9.]{0,63}')
_WINDOWS = os.name == 'nt'

# A constant script: neither a path nor an application ID is interpolated into it.
# Omitting -AllUsers/-User is deliberate: only this user's registration is valid.
_REGISTRATION_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$entries = @(foreach ($package in @(Get-AppxPackage -Name OpenAI.Codex -PackageTypeFilter Main)) {
    $manifest = Get-AppxPackageManifest -Package $package.PackageFullName
    $applications = @($manifest.Package.Applications.Application | ForEach-Object {
        [pscustomobject]@{ id = [string]$_.Id; executable = [string]$_.Executable;
                          entry_point = [string]$_.EntryPoint }
    })
    [pscustomobject]@{
        name = [string]$package.Name; package_full_name = [string]$package.PackageFullName
        package_family = [string]$package.PackageFamilyName; version = [string]$package.Version
        install_location = [string]$package.InstallLocation; status = [int]$package.Status
        is_framework = [bool]$package.IsFramework; is_resource = [bool]$package.IsResourcePackage
        applications = $applications
    }
})
ConvertTo-Json -InputObject @($entries) -Depth 5 -Compress
"""


class CodexInstallationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class CodexInstallation:
    package_full_name: str
    package_family: str
    version: str
    install_location: str
    application_id: str
    executable: str

    @property
    def aumid(self) -> str:
        return f'{self.package_family}!{self.application_id}'

    def as_dict(self) -> dict:
        return {**asdict(self), 'aumid': self.aumid, 'source': 'current_user_registration'}


def _require_windows():
    if not _WINDOWS:
        raise CodexInstallationError('codex_windows_only', '此启动方式只适用于 Windows 上注册的 Codex。')


def _read_registration() -> list[dict]:
    _require_windows()
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    try:
        completed = subprocess.run(
            [str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', _REGISTRATION_SCRIPT],
            capture_output=True, text=True, encoding='utf-8', errors='strict', timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
    except subprocess.TimeoutExpired as exc:
        raise CodexInstallationError('codex_registration_timeout', '读取 Windows 中的 Codex 注册信息超时，请稍后重试。') from exc
    except (OSError, UnicodeError) as exc:
        raise CodexInstallationError('codex_registration_unavailable', '无法读取 Windows 中的 Codex 注册信息。') from exc
    if completed.returncode:
        raise CodexInstallationError('codex_registration_unavailable', 'Windows 未能返回 Codex 注册信息。')
    try:
        if len(completed.stdout.encode('utf-8')) > MAX_REGISTRATION_BYTES:
            raise ValueError('registration too large')
        result = json.loads(completed.stdout.lstrip('\ufeff'))
        if not isinstance(result, list) or len(result) > 16 or any(not isinstance(row, dict) for row in result):
            raise ValueError('registration is not a bounded list')
        return result
    except (ValueError, TypeError) as exc:
        raise CodexInstallationError('codex_registration_invalid', 'Windows 返回的 Codex 注册信息无法确认。') from exc


def _validate_registration(row: dict) -> CodexInstallation:
    try:
        full_name = row['package_full_name']
        match = _PACKAGE_PATTERN.fullmatch(full_name)
        version = row['version']
        if (row['name'] != PACKAGE_NAME or row['package_family'] != PACKAGE_FAMILY
                or not match or match[1] != version
                or any(int(part) > 65535 for part in version.split('.'))
                or type(row['status']) is not int or row['status'] != 0
                or row['is_framework'] is not False or row['is_resource'] is not False):
            raise ValueError('unexpected package identity or status')
        location = Path(row['install_location'])
        if not location.is_absolute() or location.name != full_name or not location.is_dir():
            raise ValueError('invalid installation path')
        applications = row['applications']
        if not isinstance(applications, list) or len(applications) > 32:
            raise ValueError('invalid manifest applications')
        candidates = []
        for app in applications:
            relative = PureWindowsPath(app['executable'])
            if (str(relative).lower() not in {'app\\chatgpt.exe', 'app\\codex.exe'}
                    or app['entry_point'] != 'Windows.FullTrustApplication'):
                continue
            if not _APPLICATION_PATTERN.fullmatch(app['id']):
                raise ValueError('invalid application id')
            executable = location.joinpath(*relative.parts)
            resolved = executable.resolve(strict=True)
            if not resolved.is_relative_to(location.resolve(strict=True)) or not resolved.is_file():
                raise ValueError('executable is outside its package')
            candidates.append((app['id'], str(executable)))
        if len(candidates) != 1:
            raise ValueError('missing or ambiguous desktop application')
        application_id, executable = candidates[0]
        return CodexInstallation(full_name, PACKAGE_FAMILY, version, str(location), application_id, executable)
    except (KeyError, ValueError, TypeError, OSError, AttributeError) as exc:
        raise CodexInstallationError(
            'codex_registration_invalid', 'Codex 的注册身份、安装文件或桌面入口不完整，请检查 Windows 中的 Codex 安装。') from exc


def discover_codex() -> CodexInstallation:
    """Read the current registration; never choose a leftover package by mtime."""
    rows = _read_registration()
    if not rows:
        raise CodexInstallationError('codex_not_registered', '当前 Windows 用户尚未注册 Codex，请先正常安装并打开 Codex。')
    if len(rows) != 1:
        raise CodexInstallationError('codex_registration_ambiguous', '当前用户有多个 Codex 注册包，无法确认正常启动入口。')
    return _validate_registration(rows[0])


class _GUID(ctypes.Structure):
    _fields_ = [('data1', ctypes.c_uint32), ('data2', ctypes.c_uint16),
                ('data3', ctypes.c_uint16), ('data4', ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value: str):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


def _hresult_failed(value: int) -> bool:
    return bool(value & 0x80000000)


def _activation_error(stage: str, result: int) -> CodexInstallationError:
    return CodexInstallationError('codex_activation_failed', f'Windows 未能{stage} Codex（0x{result & 0xffffffff:08X}）。')


def _activate_aumid(aumid: str) -> int:
    """Use the normal Windows.Launch contract, with no executable or env override.

    API: https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/
         nf-shobjidl_core-iapplicationactivationmanager-activateapplication
    """
    _require_windows()
    prefix = PACKAGE_FAMILY + '!'
    if not aumid.startswith(prefix) or not _APPLICATION_PATTERN.fullmatch(aumid[len(prefix):]):
        raise CodexInstallationError('codex_registration_invalid', '无法确认 Codex 的 Windows 启动身份。')
    ole32 = ctypes.WinDLL('ole32')
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_int32
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32,
                                     ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = ctypes.c_int32
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    initialized = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    # An existing Qt/COM apartment with another mode is still usable; we must
    # only undo our own successful CoInitializeEx, including its S_FALSE case.
    if _hresult_failed(initialized) and initialized & 0xffffffff != 0x80010106:
        raise _activation_error('准备打开', initialized)
    manager = ctypes.c_void_p()
    release = None
    try:
        class_id = _GUID.parse('45ba127d-10a8-46ea-8ab7-56ea9078943c')
        interface_id = _GUID.parse('2e941141-7f97-4756-ba1d-9decde894a3d')
        # Out-of-process activation also survives the management window closing.
        result = ole32.CoCreateInstance(ctypes.byref(class_id), None, 4,  # CLSCTX_LOCAL_SERVER
                                        ctypes.byref(interface_id), ctypes.byref(manager))
        if _hresult_failed(result) or not manager.value:
            raise _activation_error('准备打开', result)
        vtable = ctypes.cast(manager, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        release = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(vtable[2])
        activate = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_wchar_p,
                                     ctypes.c_wchar_p, ctypes.c_uint32,
                                     ctypes.POINTER(ctypes.c_uint32))(vtable[3])
        process_id = ctypes.c_uint32()
        result = activate(manager, aumid, None, 0, ctypes.byref(process_id))  # AO_NONE
        if _hresult_failed(result) or not process_id.value:
            raise _activation_error('打开', result)
        return process_id.value
    finally:
        if release is not None:
            release(manager)
        if not _hresult_failed(initialized):
            ole32.CoUninitialize()


def activate_codex() -> dict:
    """Request normal activation, re-reading registration to avoid stale versions.

    The returned process may be an existing app instance. This is not a readiness
    check and must not be presented as confirmation that task sending will work.
    """
    installation = discover_codex()
    try:
        process_id = _activate_aumid(installation.aumid)
    except OSError as exc:
        raise CodexInstallationError('codex_activation_failed', 'Windows 无法打开 Codex，请从开始菜单检查应用。') from exc
    return {'status': 'activation_requested', 'process_id': process_id,
            'installation': installation.as_dict(),
            'message': '已请求 Windows 打开当前注册的 Codex；任务连接状态尚待检查。'}
