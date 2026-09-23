# Run on the target Mac architecture using packaging/build_macos.py.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH).parent
datas = collect_data_files('management') + collect_data_files('tzdata')
datas += copy_metadata('mcp') + copy_metadata('personal-management')
a = Analysis([str(root / 'packaging' / 'entry.py')], pathex=[str(root / 'src')],
             datas=datas, hiddenimports=['PySide6.QtPdf'],
             excludes=['pytest', 'tkinter', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets'])
pyz = PYZ(a.pure)
options = dict(exclude_binaries=True, strip=False, upx=False,
               target_arch=os.environ.get('PM_TARGET_ARCH'),
               codesign_identity=os.environ.get('PM_CODESIGN_IDENTITY') or None)
gui = EXE(pyz, a.scripts, [], name='PersonalManagement', console=False, **options)
service = EXE(pyz, a.scripts, [], name='PersonalManagementService', console=True, **options)
collection = COLLECT(gui, service, a.binaries, a.datas, strip=False, upx=False,
                     name='PersonalManagement')
app = BUNDLE(collection, name='PersonalManagement.app',
             bundle_identifier='design.aaaarthur666.personal-management',
             info_plist={'CFBundleDisplayName': '个人事务管理',
                         'CFBundleShortVersionString': '0.7.0', 'CFBundleVersion': '7',
                         'LSMinimumSystemVersion': '13.0', 'NSHighResolutionCapable': True})
