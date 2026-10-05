"""Repeatable checks with cached evidence and automatically reclaimed fixtures."""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from workflow import ROOT, SuiteBusy, TestRun, digest, runtime_directory, write_json
from management import __version__

PROFILES = ('focused', 'full', 'gui', 'package', 'upgrade', 'tray', 'installer')


def fingerprint(profile, paths):
    files = [ROOT / 'pyproject.toml']
    for directory in ('src', 'tests', 'packaging'):
        files.extend(p for p in (ROOT / directory).rglob('*') if p.is_file()
                     and '__pycache__' not in p.parts and p.suffix not in {'.pyc', '.log'})
    packages = []
    if profile in {'package', 'tray', 'upgrade'}:
        packages = [runtime_directory(__version__)]
        if profile == 'upgrade':
            packages.append(runtime_directory('1.0.2'))
        for package in packages:
            if not (package / 'PersonalManagementService.exe').is_file():
                raise FileNotFoundError('Required existing package is missing: ' + str(package))
            files.extend(p for p in package.rglob('*') if p.is_file())
    versions = {}
    for name in ('pytest', 'PySide6', 'mcp', 'psutil', 'pyinstaller'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    value = {'profile': profile, 'paths': paths, 'python': sys.version, 'dependencies': versions,
             'files': {str(p.relative_to(ROOT)): digest(p) for p in sorted(set(files))}}
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def commands(profile, paths, run):
    py = [sys.executable, '-X', 'utf8']
    def pytest_command(name, selection):
        return py + ['-m', 'pytest', '-q', *selection, '--basetemp', str(run.work / name),
                     '--junitxml', str(run.folder / (name + '.xml'))]
    if profile == 'focused':
        return [pytest_command('pytest', paths)]
    if profile == 'full':
        return [pytest_command('pytest', ['--ignore=tests/test_gui_shell_v2.py']),
                pytest_command('gui', ['tests/test_gui_shell_v2.py'])]
    if profile == 'gui':
        return [pytest_command('gui', ['tests/test_gui_shell_v2.py'])]
    if profile == 'installer':
        return [pytest_command('installer', ['tests/test_installer_payload.py', 'tests/test_installer_upgrade.py'])]
    script = {'package': 'smoke.py', 'upgrade': 'upgrade_smoke.py', 'tray': 'tray_smoke.py'}[profile]
    return [py + [str(ROOT / 'packaging' / script), '--work-dir', str(run.work),
                  '--report', str(run.folder / 'result.json')]]


def test_counts(folder):
    counts = {'tests': 0, 'failures': 0, 'errors': 0, 'skipped': 0}
    for path in folder.glob('*.xml'):
        for suite in ET.parse(path).iter('testsuite'):
            for key in counts:
                counts[key] += int(suite.get(key, 0))
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', choices=PROFILES)
    parser.add_argument('paths', nargs='*', help='Focused pytest files or node IDs')
    parser.add_argument('--force', action='store_true', help='Run again even when identical inputs passed before')
    parser.add_argument('--keep-work', action='store_true', help='Keep one fixture workspace for explicit debugging')
    args = parser.parse_args(argv)
    if args.profile == 'focused':
        if not args.paths:
            parser.error('focused requires specific test files; full is an explicit separate command')
        for node in args.paths:
            path = (ROOT / node.split('::', 1)[0]).resolve()
            if not path.is_relative_to(ROOT / 'tests') or not path.is_file() or path.suffix != '.py':
                parser.error('Focused selection must name files under tests/')
    elif args.paths:
        parser.error('Only focused accepts additional test paths')
    try:
        with TestRun(args.profile, root=ROOT, keep_work=args.keep_work) as run:
            return run_checked(args, run)
    except SuiteBusy as error:
        print(json.dumps({'status': 'busy', 'profile': args.profile, 'message': str(error)}))
        return 1
    except Exception as error:
        print(json.dumps({'status': 'blocked', 'profile': args.profile, 'message': str(error)}, ensure_ascii=False))
        return 1


def run_checked(args, run):
    summary = None
    log_created = False
    started = time.monotonic()
    folder = run.folder
    try:
        signature = fingerprint(args.profile, args.paths)
        latest = run.folder / 'latest.json'
        if latest.is_file() and not args.force:
            old = json.loads(latest.read_text('utf-8'))
            if old.get('passed') and old.get('fingerprint') == signature:
                print(json.dumps({'status': 'reused', 'tested_at': old['tested_at'],
                      'profile': args.profile, 'counts': old.get('counts'), 'report': str(latest)}, ensure_ascii=False))
                return 0
        run.prepare()
        log_created = True
        for path in [*run.folder.glob('*.xml'), run.folder / 'result.json']:
            path.unlink(missing_ok=True)
        environment = dict(os.environ, PERSONAL_MANAGEMENT_NO_TRAY='1', PERSONAL_MANAGEMENT_NO_ONBOARDING='1', PYTHONUTF8='1')
        # unittest and helper processes also use tempfile outside pytest's
        # --basetemp unless all platform temp variables point into our work tree.
        temporary = run.work / 'temp'
        temporary.mkdir()
        environment.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary))
        environment['PERSONAL_MANAGEMENT_CHECK_REPORT_DIR'] = str(run.folder)
        if args.profile == 'installer':
            environment['PERSONAL_MANAGEMENT_INSTALLER_TEST'] = '1'
        results = []
        with (run.folder / 'latest.log').open('w', encoding='utf-8') as log:
            for command in commands(args.profile, args.paths, run):
                log.write('CHECK ' + Path(command[3] if command[3] != '-m' else command[4]).name + '\n')
                log.flush()
                result = subprocess.run(command, cwd=ROOT, env=environment, stdout=log,
                    stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                results.append(result.returncode)
                if result.returncode:
                    break
        summary = {'profile': args.profile, 'version': __version__, 'passed': all(code == 0 for code in results),
                   'fingerprint': signature, 'tested_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                   'seconds': round(time.monotonic() - started, 2), 'counts': test_counts(run.folder),
                   'exit_codes': results, 'work_retained': args.keep_work}
    except BaseException as error:
        summary = summary or {'profile': args.profile, 'version': __version__, 'passed': False}
        summary.update(passed=False, error=str(error), error_type=type(error).__name__,
                       tested_at=dt.datetime.now(dt.timezone.utc).isoformat())
        if not log_created:
            (folder / 'latest.log').write_text(str(error) + '\n', 'utf-8')
    try:
        run.cleanup()
        summary['processes_reclaimed'] = True
    except Exception as error:
        summary.update(passed=False, cleanup_error=str(error), processes_reclaimed=False)
    write_json(folder / 'latest.json', summary)
    log_path = folder / 'latest.log'
    if log_path.is_file() and log_path.stat().st_size > 2 * 1024 * 1024:
        with log_path.open('rb') as stream:
            stream.seek(-2 * 1024 * 1024, 2)
            tail = stream.read()
        log_path.write_bytes(b'[Earlier output truncated; retained the last 2 MiB]\n' + tail)
    if not summary['passed']:
        write_json(folder / 'failure.json', summary)
        if log_path.is_file():
            import shutil
            shutil.copyfile(log_path, folder / 'failure.log')
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
