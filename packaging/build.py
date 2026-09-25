from __future__ import annotations
import json
import os
import argparse
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--name',default='PersonalManagement-0.14.1')
    args=parser.parse_args()
    if not args.name.startswith('PersonalManagement') or any(x in args.name for x in '/\\:'):
        parser.error('Invalid package directory name')
    from version_info import write_versions
    write_versions()
    os.environ['PM_PACKAGE_NAME']=args.name
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', str(ROOT / 'release'), '--workpath', str(ROOT / '.build' / 'pyinstaller'), str(ROOT / 'packaging' / 'personal_management.spec')], check=True, cwd=ROOT)
    package = ROOT / 'release' / args.name
    (package / '使用说明.txt').write_text('个人事务管理 0.14.1\n\n打开 PersonalManagement.exe。首次启动会建立空数据空间。\n\n默认数据位置：%LOCALAPPDATA%\\PersonalManagement\\data\n如需使用其他目录，运行 PersonalManagement.exe --choose-data，或使用 --data-dir 指定目录。\n\n在设置 → Codex 协助中启用并保存，软件会准备固定的 Codex事务助手项目与当前数据连接。选择“在 Codex 桌面同步显示”后，首次需退出 Codex 并点击“启动 Codex 连接模式”；之后在软件发送一次即可派发。需要本机已安装并登录 Codex。软件包不包含账号或个人记录。\n\n界面关闭后本机业务服务继续运行。电脑关闭期间不执行定时任务。\n\n使用方法与功能边界见软件包中的 详细使用说明.md。\n', encoding='utf-8')
    (package / 'mcp-config-example.toml').write_text('# 用实际绝对路径替换下面内容；不含用户账号信息。\n[mcp_servers.personal_management]\ncommand = "D:\\\\YourSoftware\\\\PersonalManagementService.exe"\nargs = ["--mcp", "--data-dir", "D:\\\\YourData\\\\PersonalManagement"]\n', encoding='utf-8')
    (package / '选择数据空间.vbs').write_text('Set shell = CreateObject("WScript.Shell")\nSet fso = CreateObject("Scripting.FileSystemObject")\nbase = fso.GetParentFolderName(WScript.ScriptFullName)\nshell.Run Chr(34) & base & "\\PersonalManagement.exe" & Chr(34) & " --choose-data", 1, False\n', encoding='ascii')
    (package / '创建快捷方式.vbs').write_text('Set shell = CreateObject("WScript.Shell")\nSet fso = CreateObject("Scripting.FileSystemObject")\nbase = fso.GetParentFolderName(WScript.ScriptFullName)\nshell.Run Chr(34) & base & "\\PersonalManagement.exe" & Chr(34) & " --install-shortcuts --choose-data", 1, False\n', encoding='ascii')
    (package / '详细使用说明.md').write_text((ROOT / 'docs' / '使用说明.md').read_text('utf-8-sig'), encoding='utf-8')
    files = [p for p in package.rglob('*') if p.is_file()]
    forbidden = {'.analysis', '.test-output', '.venv', 'database.sqlite3', 'runtime.json', 'auth.json', 'restore_pending.json'}
    for path in files:
        if forbidden.intersection(path.relative_to(package).parts):
            raise RuntimeError('Private/runtime data present in release: ' + str(path))
    report = {'file_count': len(files), 'bytes': sum(p.stat().st_size for p in files), 'private_data_found': False, 'business_data_included': False}
    (ROOT / '.build' / 'release-manifest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))

if __name__ == '__main__':
    main()
