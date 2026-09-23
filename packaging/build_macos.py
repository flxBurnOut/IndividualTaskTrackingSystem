"""Build a relocatable, self-contained macOS app on its target architecture."""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--arch', choices=['arm64', 'x86_64'], default=platform.machine())
    parser.add_argument('--codesign-identity', default=os.environ.get('PM_CODESIGN_IDENTITY'))
    args = parser.parse_args()
    if sys.platform != 'darwin' or args.arch != platform.machine():
        parser.error('请在目标架构的 Mac/Python 环境构建；Intel 版本需要独立 x86_64 环境。')
    from management.runtime_check import check_sqlite
    check_sqlite()
    package = ROOT / 'release' / f'PersonalManagement-0.7-macos-{args.arch}'
    env = {**os.environ, 'PM_TARGET_ARCH': args.arch,
           'PYINSTALLER_CONFIG_DIR': str(ROOT / '.build' / 'pyinstaller-cache')}
    if args.codesign_identity:
        env['PM_CODESIGN_IDENTITY'] = args.codesign_identity
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                    '--distpath', str(package), '--workpath', str(ROOT / '.build' / 'macos'),
                    str(ROOT / 'packaging' / 'macos.spec')], cwd=ROOT, env=env, check=True)
    app = package / 'PersonalManagement.app'
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    forbidden = {'.analysis', '.test-output', '.venv', 'database.sqlite3', 'runtime.json', 'auth.json', 'restore_pending.json'}
    files = [p for p in app.rglob('*') if p.is_file()]
    for path in files:
        if forbidden.intersection(path.relative_to(app).parts):
            raise RuntimeError('发布包混入了运行数据：' + str(path))
    (package / '使用说明.md').write_text((ROOT / 'docs' / 'macOS适配方案.md').read_text('utf-8'), encoding='utf-8')
    chooser = package / '选择数据空间.command'
    chooser.write_text('#!/bin/zsh\nset -eu\nopen "${0:A:h}/PersonalManagement.app" --args --choose-data\n', encoding='utf-8')
    chooser.chmod(0o755)
    # ditto preserves the framework symlinks required by a macOS application.
    archive = Path(str(package) + '.zip')
    subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(app), str(archive)], check=True)
    report = {'app': str(app), 'archive': str(archive), 'architecture': args.arch,
              'signature': 'developer-id' if args.codesign_identity else 'ad-hoc',
              'notarized': False, 'private_data_found': False, 'file_count': len(files)}
    (ROOT / '.build' / 'macos-build.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
