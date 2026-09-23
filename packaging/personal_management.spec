# Two entrypoints share one dependency directory: a native GUI and a stdio/service executable.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from pathlib import Path
import os
root = Path(SPECPATH).parent
hidden = ['pythoncom','pywintypes','win32com.client','win32timezone','win32com.shell.shell','win32com.propsys.propsys','win32com.propsys.pscon','mcp.server','mcp.server.mcpserver','reportlab.pdfbase.ttfonts','pypdf','docx']
hidden += collect_submodules('management')
a = Analysis([str(root / 'packaging' / 'entry.py')], pathex=[str(root / 'src')], binaries=[], datas=collect_data_files('tzdata') + collect_data_files('management', includes=['builtin_skills/**/*.md','assets/*.ico','assets/*.png','assets/*.svg']), hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=['PySide6.QtWebEngineCore','PySide6.QtWebEngineWidgets','PySide6.QtQml','PySide6.QtQuick','PySide6.QtMultimedia','pytest','tkinter'], noarchive=False)
# Qt 6.11 uses Windows system ICU with unversioned exports (e.g. ucnv_open).
# A third-party ICU found on the tool environment PATH exports *_78 instead;
# packaging it shadows System32 ICU and causes QtWidgets loader error 127.
# No bundled consumer other than Qt6Core imports icuuc; icudt78 belongs only
# to that incorrect third-party ICU. Let Windows resolve its own system ICU.
_icu_conflicts = {'icuuc.dll', 'icudt78.dll'}
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() not in _icu_conflicts]
p = PYZ(a.pure)
gui = EXE(p, a.scripts, [], exclude_binaries=True, name='PersonalManagement', version=str(root / '.build' / 'version-gui.txt'), icon=str(root / 'src' / 'management' / 'assets' / 'app-icon.ico'), debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False)
service = EXE(p, a.scripts, [], exclude_binaries=True, name='PersonalManagementService', version=str(root / '.build' / 'version-service.txt'), icon=str(root / 'src' / 'management' / 'assets' / 'app-icon.ico'), debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True)
coll = COLLECT(gui, service, a.binaries, a.datas, strip=False, upx=False, name=os.environ.get('PM_PACKAGE_NAME','PersonalManagement'))
