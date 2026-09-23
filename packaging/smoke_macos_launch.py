"""Verify source launcher and Finder launch of a relocated, read-only .app."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import uuid

import psutil

ROOT = Path(__file__).resolve().parents[1]


def stop_service(data):
    runtime = data / 'runtime.json'
    if runtime.is_file():
        pid = json.loads(runtime.read_text('utf-8'))['pid']
        try:
            process = psutil.Process(pid)
            if '--service' in process.cmdline() and str(data) in process.cmdline():
                process.terminate()
                process.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass


def main():
    if sys.platform != 'darwin':
        raise SystemExit('macOS only')
    root = ROOT / '.test-output' / ('mac-launch-' + uuid.uuid4().hex[:10])
    root.mkdir(parents=True)
    source_data, packaged_data = root / '源码 空间', root / '应用 空间'
    report = {'synthetic_only': True, 'passed': False}
    app = root / '中文 软件目录' / '个人事务管理.app'
    original_modes = {}
    try:
        source_report = root / 'source-gui.json'
        subprocess.run([str(ROOT / '启动个人事务管理.command'), '--data-dir', str(source_data),
                        '--verify-ui', str(source_report)], cwd='/private/tmp',
                       env={**os.environ, 'QT_QPA_PLATFORM': 'cocoa'}, check=True, timeout=40)
        report['source_gui'] = json.loads(source_report.read_text('utf-8'))
        assert report['source_gui']['loaded'] and report['source_gui']['platform'] == 'cocoa'
        source = ROOT / 'release' / f'PersonalManagement-0.7-macos-{platform.machine()}' / 'PersonalManagement.app'
        subprocess.run(['ditto', str(source), str(app)], check=True)
        for path in [app, *app.rglob('*')]:
            if not path.is_symlink():
                original_modes[path] = path.stat().st_mode & 0o777
                path.chmod(original_modes[path] & ~0o222)
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
        environment = {key: value for key, value in os.environ.items()
                       if key in {'HOME', 'TMPDIR', 'USER', 'LOGNAME', '__CF_USER_TEXT_ENCODING'}}
        environment.update(PATH='/usr/bin:/bin', QT_QPA_PLATFORM='cocoa')
        binary = app / 'Contents/MacOS/PersonalManagementService'
        result = subprocess.run([str(binary), '--diagnose', '--data-dir', str(packaged_data)],
                                cwd='/private/tmp', env=environment, capture_output=True,
                                text=True, check=True, timeout=30)
        report['relocated_diagnostics'] = json.loads(result.stdout)
        finder_report = root / 'finder-gui.json'
        subprocess.run(['open', '-n', '-W', str(app), '--args', '--data-dir', str(packaged_data),
                        '--verify-ui', str(finder_report)], cwd='/private/tmp',
                       env=environment, check=True, timeout=40)
        report['finder_gui'] = json.loads(finder_report.read_text('utf-8'))
        assert report['finder_gui']['loaded'] and report['finder_gui']['platform'] == 'cocoa'
        assert report['finder_gui']['menu_shortcuts']['设置…']
        report.update(read_only_app=True, relocated_chinese_space_path=True,
                      clean_cli_environment=True, passed=True)
    finally:
        stop_service(source_data)
        stop_service(packaged_data)
        for path, mode in original_modes.items():
            path.chmod(mode)
        if app.exists():
            shutil.rmtree(app)
        (ROOT / '.build/macos-launch-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
