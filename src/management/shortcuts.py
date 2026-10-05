"""Per-user Windows launchers, tied explicitly to one chosen data space."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys
import uuid
from .branding import APP_NAME, APP_USER_MODEL_ID


def read_shortcut(path):
    import pythoncom
    from win32com.shell import shell
    from win32com.propsys import propsys, pscon
    pythoncom.CoInitialize()
    try:
        link=pythoncom.CoCreateInstance(shell.CLSID_ShellLink,None,pythoncom.CLSCTX_INPROC_SERVER,shell.IID_IShellLink)
        link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(Path(path).resolve()))
        properties=link.QueryInterface(propsys.IID_IPropertyStore)
        return {'path':str(Path(path).resolve()),'target':link.GetPath(0)[0], 'arguments':link.GetArguments(),
                'working_directory':link.GetWorkingDirectory(), 'icon':link.GetIconLocation(),
                'app_id':properties.GetValue(pscon.PKEY_AppUserModel_ID).GetValue()}
    finally:
        pythoncom.CoUninitialize()


def write_shortcut(path, executable, data_dir):
    from .data_space import require_beta_dir
    data_dir = require_beta_dir(data_dir)
    if os.name!='nt':raise OSError('快捷方式创建仅用于 Windows。')
    path=Path(path).absolute();executable=Path(executable).resolve();data_dir=Path(data_dir).resolve()
    if path.suffix.lower()!='.lnk' or not executable.is_file():
        raise ValueError('请选择有效的程序文件和 .lnk 快捷方式位置。')
    if any(x in str(data_dir) for x in ('\x00','\r','\n')):raise ValueError('数据空间路径无效。')
    if path.exists():
        previous=read_shortcut(path)
        if previous['app_id']!=APP_USER_MODEL_ID:
            raise FileExistsError('该位置已有其他快捷方式，未覆盖：'+str(path))
    import pythoncom
    from win32com.shell import shell
    from win32com.propsys import propsys, pscon
    pythoncom.CoInitialize();temporary=None
    try:
        path.parent.mkdir(parents=True,exist_ok=True)
        link=pythoncom.CoCreateInstance(shell.CLSID_ShellLink,None,pythoncom.CLSCTX_INPROC_SERVER,shell.IID_IShellLink)
        link.SetPath(str(executable))
        link.SetArguments(subprocess.list2cmdline(['--data-dir',str(data_dir)]))
        link.SetWorkingDirectory(str(executable.parent))
        link.SetDescription(APP_NAME+' · 打开已选择的 Beta 数据空间')
        link.SetIconLocation(str(executable),0)
        link.SetShowCmd(1)
        properties=link.QueryInterface(propsys.IID_IPropertyStore)
        properties.SetValue(pscon.PKEY_AppUserModel_ID,propsys.PROPVARIANTType(APP_USER_MODEL_ID))
        properties.Commit()
        temporary=path.with_name('.'+path.stem+'.'+uuid.uuid4().hex+'.lnk')
        link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(temporary),1)
        # A read-back validates actual shell data, including quoted Unicode paths.
        saved=read_shortcut(temporary)
        if Path(saved['target']).resolve()!=executable or saved['app_id']!=APP_USER_MODEL_ID:
            raise OSError('快捷方式核对未通过，未覆盖已有入口。')
        os.replace(temporary,path);temporary=None
        return read_shortcut(path)
    finally:
        if temporary is not None and temporary.exists():temporary.unlink()
        pythoncom.CoUninitialize()


def install_shortcuts(data_dir, executable=None):
    if os.name!='nt':raise OSError('此功能用于 Windows 桌面与开始菜单。')
    if executable is None:
        if not getattr(sys,'frozen',False):raise ValueError('请使用已打包程序创建快捷方式。')
        executable=Path(sys.executable).with_name('PersonalManagement.exe')
    from win32com.shell import shell
    # The shell resolves redirected Desktop/OneDrive locations for the current user.
    desktop=Path(shell.SHGetKnownFolderPath(shell.FOLDERID_Desktop))
    programs=Path(shell.SHGetKnownFolderPath(shell.FOLDERID_Programs))
    destinations=[desktop/(APP_NAME+'.lnk'),programs/APP_NAME/(APP_NAME+'.lnk')]
    items=[]
    for destination in destinations:
        if destination.exists() and read_shortcut(destination)['app_id']!=APP_USER_MODEL_ID:
            destination=destination.with_name(APP_NAME+'（本机数据）.lnk')
        items.append(write_shortcut(destination,executable,data_dir))
    from .installation_state import remember_installed_data_dir
    remember_installed_data_dir(data_dir, executable)
    return {'shortcuts':items,'data_dir':str(Path(data_dir).resolve())}
