from __future__ import annotations
import json
import os
import argparse
from pathlib import Path
import subprocess
import sys
from workflow import version_directory, scoped_processes

ROOT = Path(__file__).resolve().parents[1]

def main():
    from management.data_space import require_packaged_channel
    require_packaged_channel()
    from build_installer import source_version
    version=source_version()
    parser=argparse.ArgumentParser()
    parser.add_argument('--name',default='PersonalManagement-'+version)
    args=parser.parse_args()
    if args.name != 'PersonalManagement-' + version:
        parser.error('Use the canonical version folder: PersonalManagement-' + version)
    folder = version_directory(version)
    package = folder / 'app'
    if package.exists() or (folder / 'PersonalManagement.exe').exists():
        raise FileExistsError('A versioned runtime already exists; keep its verified files and use a new release version.')
    if scoped_processes(folder):
        raise RuntimeError('This version is currently in use')
    from version_info import write_versions
    write_versions()
    os.environ['PM_PACKAGE_NAME']='app'
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', str(folder), '--workpath', str(ROOT / '.build' / 'pyinstaller'), str(ROOT / 'packaging' / 'personal_management.spec')], check=True, cwd=ROOT)
    (package / '使用说明.txt').write_text('个人事务管理 '+version+'\n\n打开 PersonalManagement.exe。已有安装版继续使用登记的数据目录；新空目录才会建立空数据空间。\n\n更新前打开“版本与更新”，保存工作并准备退出后台；运行新版安装包时核对原数据位置。旧便携版请选择原数据目录，不需重新导入。升级前自动保存数据库快照，资料文件留在原处。\n\n默认数据位置：%LOCALAPPDATA%\\PersonalManagement\\data\n如需使用其他目录，运行 PersonalManagement.exe --choose-data，或使用 --data-dir 指定目录。\n\n在设置 → Codex 协助中启用并保存。主窗口、设置和讨论窗口都会显示连接状态；点击“连接 Codex”可打开并连接当前安装的 Codex。已打开的普通 Codex 会自动连接，无需切换启动方式。讨论中的“连接并发送”只发送本次点击时的草稿。需要本机已安装并登录 Codex。软件包不包含账号或个人记录。\n\n同一事项沿用原 Codex 会话；候选回到软件核对后保存。关闭主窗口后后台继续运行，Windows 系统托盘会保留图标；双击可重新打开，右键可查看后台状态或退出软件与后台。断开连接不会重发旧消息或停止 Codex 正在处理的任务。电脑关闭期间不执行定时任务。\n\n使用方法与功能边界见软件包中的 详细使用说明.md。\n', encoding='utf-8')
    (package / 'mcp-config-example.toml').write_text('# 用实际绝对路径替换下面内容；不含用户账号信息。\n[mcp_servers.personal_management]\ncommand = "D:\\\\YourSoftware\\\\PersonalManagementService.exe"\nargs = ["--mcp", "--data-dir", "D:\\\\YourData\\\\PersonalManagement"]\n', encoding='utf-8')
    (package / '选择数据空间.vbs').write_text('Set shell = CreateObject("WScript.Shell")\nSet fso = CreateObject("Scripting.FileSystemObject")\nbase = fso.GetParentFolderName(WScript.ScriptFullName)\nshell.Run Chr(34) & base & "\\PersonalManagement.exe" & Chr(34) & " --choose-data", 1, False\n', encoding='ascii')
    (package / '创建快捷方式.vbs').write_text('Set shell = CreateObject("WScript.Shell")\nSet fso = CreateObject("Scripting.FileSystemObject")\nbase = fso.GetParentFolderName(WScript.ScriptFullName)\nshell.Run Chr(34) & base & "\\PersonalManagement.exe" & Chr(34) & " --install-shortcuts --choose-data", 1, False\n', encoding='ascii')
    (package / '详细使用说明.md').write_text((ROOT / 'docs' / '使用说明.md').read_text('utf-8-sig'), encoding='utf-8')
    (package / '新手引导.md').write_text((ROOT / 'docs' / '新手引导.md').read_text('utf-8-sig'), encoding='utf-8')
    (package / '发布说明.md').write_text((ROOT / 'docs' / ('发布说明_'+version+'.md')).read_text('utf-8-sig'), encoding='utf-8')
    from license_notices import write_notices
    write_notices(package)
    files = [p for p in package.rglob('*') if p.is_file()]
    forbidden = {'.analysis', '.test-output', '.venv', 'database.sqlite3', 'runtime.json', 'auth.json', 'restore_pending.json', 'update_pending.json', 'upgrade-state.json', 'upgrade-backups', 'schema-upgrade.lock', 'startup_failure.json', 'gui.lock', 'tray.lock', 'tray-status.json', 'tray-status.json.new', 'ui-onboarding.json', 'ui-plan-drafts'}
    for path in files:
        if forbidden.intersection(path.relative_to(package).parts):
            raise RuntimeError('Private/runtime data present in release: ' + str(path))
    report = {'file_count': len(files), 'bytes': sum(p.stat().st_size for p in files), 'private_data_found': False, 'business_data_included': False}
    (folder / 'runtime-manifest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))

if __name__ == '__main__':
    main()
