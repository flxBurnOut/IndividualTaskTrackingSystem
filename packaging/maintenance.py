"""Inspect or organize generated files. Mutations require an explicit --apply."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import sqlite3

import psutil

from workflow import (ROOT, checked_path, digest, remove_generated, scoped_processes,
                      terminate_verified, version_directory, write_json)

LEGACY_TESTS = ('packaged-business-data', 'business-flow-data', 'business-flow-data-v3')


def assert_idle(data):
    with sqlite3.connect((data / 'database.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        jobs = dict(db.execute('SELECT status,count(*) FROM jobs GROUP BY status'))
        operations = dict(db.execute('SELECT status,count(*) FROM io_operations GROUP BY status'))
    if any(jobs.get(key, 0) for key in ('running', 'queued')) or any(operations.get(key, 0) for key in ('preparing', 'ready')):
        raise RuntimeError('A test operation is still active; leaving this process and its data intact')
    return {'jobs': jobs, 'file_operations': operations}


def stop_legacy_tests(apply=False, root=ROOT):
    root = Path(root).resolve()
    report = []
    for name in LEGACY_TESTS:
        data = checked_path(root / '.build' / 'native-ipc-research' / name, root / '.build')
        if not (data / 'runtime.json').is_file():
            continue
        runtime = json.loads((data / 'runtime.json').read_text('utf-8'))
        if Path(runtime['data_dir']).resolve() != data:
            raise ValueError('Test runtime points to a different data space')
        state = assert_idle(data)
        try:
            process = psutil.Process(runtime['pid'])
            command = process.cmdline()
            if '--service' not in command or '--data-dir' not in command:
                raise RuntimeError('PID no longer belongs to the test service')
            if Path(command[command.index('--data-dir') + 1]).resolve() != data:
                raise RuntimeError('Test PID now belongs to another data space')
            record = {'pid': process.pid, 'created': process.create_time(), 'exe': process.exe(), 'args': command}
        except psutil.NoSuchProcess:
            report.append({'data': str(data), 'already_stopped': True})
            continue
        if apply:
            assert_idle(data)
            terminate_verified(record)
        report.append({'data': str(data), 'pid': record['pid'], 'stopped': apply, **state})
    return report


def pytest_directory(path):
    return path.is_dir() and any(p.is_dir() and p.name.startswith('test_') for p in path.iterdir())


def cleanup_candidates(root=ROOT):
    root = Path(root).resolve()
    values = []
    test_output = root / '.test-output'
    if test_output.exists():
        values.extend(p for p in test_output.iterdir() if p.is_dir() and not p.is_symlink() and not p.is_junction())
    build = root / '.build'
    if build.exists():
        for path in build.iterdir():
            if not path.is_dir() or path.is_symlink() or path.is_junction():
                continue
            if path.name.startswith(('pre-', 'migration-', 'native-ipc-')) or path.name in {'checks', 'reports', 'tools', 'pyinstaller', 'third-party-license-cache', 'legacy-scripts'}:
                continue
            if pytest_directory(path) or re.fullmatch(r'(?:tray-native|upgrade-acceptance|installer-full-1\.1\.0)-[a-f0-9]+', path.name):
                values.append(path)
            elif path.name in {'replaced-v12-prelauncher', 'superseded-1.1.0-interim', 'release-download-verify-v1', 'release-download-verify-v101'}:
                values.append(path)
            elif path.name in {'handoff-p1-fix-20260927', 'handoff-audit-20260927'}:
                values.extend(p for p in path.iterdir() if p.is_dir() and (pytest_directory(p)
                    or p.name.startswith(('context-data-', 'scope-data-', 'lifecycle-fixture-', 'mcp-fixtures-'))))
    release = root / 'release'
    if release.exists():
        for path in release.iterdir():
            match = re.fullmatch(r'PersonalManagement-(\d+)\.(\d+)\.(\d+)', path.name)
            if match and int(match[1]) < 1:
                values.append(path)
    return sorted(set(values))


def prune(apply=False, root=ROOT, scope='all-generated', plan=None):
    root = Path(root).resolve()
    removed, blocked, freed = [], [], 0
    candidates = cleanup_candidates(root)
    if plan is not None:
        plan = checked_path(plan, root / '.build' / 'reports' / 'maintenance')
        selected = json.loads(plan.read_text('utf-8'))['directories']
        if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
            raise ValueError('Invalid cleanup plan')
        requested = [checked_path(root / item, root) for item in selected]
        if not set(requested).issubset(set(candidates)):
            raise ValueError('Plan includes a protected, moved or unrecognized directory; inspect again')
        candidates = requested
    elif apply and scope == 'all-generated':
        raise ValueError('Bulk cleanup requires an explicitly reviewed --plan file')
    processes = scoped_processes(root)
    for path in candidates:
        if scope == 'obsolete-releases' and path.parent != root / 'release':
            continue
        checked_path(path, root)
        active = scoped_processes(path, records=processes)
        if active:
            blocked.append({'path': str(path), 'pids': [p['pid'] for p in active]})
            continue
        if apply:
            freed += remove_generated(path, root, process_records=processes)
        removed.append(str(path.relative_to(root)))
    return {'applied': apply, 'directories': removed, 'blocked': blocked, 'freed_bytes': freed,
            'kept': ['data', 'pre-upgrade backups', 'migration records', 'toolchain and license cache', 'release versions >= 1.0']}


def organize(apply=False, root=ROOT):
    root = Path(root).resolve()
    release = root / 'release'
    actions = []
    for folder in sorted(release.glob('PersonalManagement-*')):
        match = re.fullmatch(r'PersonalManagement-(\d+\.\d+\.\d+)', folder.name)
        if not folder.is_dir() or not match or int(match[1].split('.')[0]) < 1:
            continue
        version = match[1]
        checked_path(folder, release)
        receipt = folder / 'manifest.json'
        old_receipt = root / '.build' / ('installer-' + version + '.json')
        if not receipt.exists() and not old_receipt.exists():
            raise FileNotFoundError('Cannot organize an unattested release: ' + folder.name)
        manifest = json.loads((receipt if receipt.exists() else old_receipt).read_text('utf-8'))
        if manifest['version'] != version:
            raise ValueError('Manifest version mismatch')
        active = scoped_processes(folder)
        runtime = folder / 'app' if (folder / 'app').is_dir() else folder
        for item in manifest['files']:
            path = checked_path(runtime / item['path'], runtime)
            if not path.is_file() or digest(path) != item['sha256']:
                raise ValueError('Runtime content differs from verified manifest: ' + str(path))
        if runtime == folder and not active:
            names = sorted({Path(item['path']).parts[0] for item in manifest['files']})
            for name in names:
                source = checked_path(folder / name, folder)
                target = checked_path(folder / 'app' / name, folder)
                if target.exists():
                    raise FileExistsError(str(target))
                if apply:
                    target.parent.mkdir(exist_ok=True)
                    source.rename(target)
            runtime = folder / 'app'
        installer_name = f'PersonalManagement-{version}-Setup-x64.exe'
        for name in (installer_name, installer_name + '.sha256'):
            source, target = release / name, folder / name
            checked_path(source, release)
            checked_path(target, folder)
            if source.exists():
                if target.exists():
                    raise FileExistsError('Destination already exists: ' + str(target))
                if apply:
                    source.rename(target)
        installer = folder / installer_name
        source_installer = installer if installer.exists() else release / installer_name
        actual_hash = digest(source_installer)
        if actual_hash != manifest['installer_sha256']:
            raise ValueError('Installer checksum differs from release manifest')
        if apply:
            manifest['runtime_directory'] = 'app' if runtime.name == 'app' else '.'
            manifest['layout_version'] = 1
            write_json(receipt, manifest)
            (folder / 'SHA256SUMS.txt').write_text(actual_hash + '  ' + installer_name + '\n', 'ascii')
        actions.append({'version': version, 'runtime': str(runtime.relative_to(release)),
                        'active_runtime_kept': bool(active), 'installer': str(installer.relative_to(release)),
                        'sha256': actual_hash})
    loose = release / 'SHA256SUMS.txt'
    if apply and loose.is_file():
        # Delete only a legacy checksum whose referenced installer was verified.
        line = loose.read_text('ascii').strip().split()
        if len(line) == 2 and any(line == [item['sha256'], Path(item['installer']).name] for item in actions):
            loose.unlink()
    return {'applied': apply, 'versions': actions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('stop-legacy-tests', 'prune', 'organize'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--scope', choices=('all-generated', 'obsolete-releases'), default='all-generated')
    parser.add_argument('--plan', type=Path, help='Restrict bulk cleanup to a reviewed preview JSON')
    args = parser.parse_args()
    result = (prune(args.apply, scope=args.scope, plan=args.plan) if args.action == 'prune' else
              {'stop-legacy-tests': stop_legacy_tests, 'organize': organize}[args.action](args.apply))
    label = args.action + ('-' + args.scope if args.action == 'prune' else '')
    path = ROOT / '.build' / 'reports' / 'maintenance' / (label + '.json')
    write_json(path, result)
    if isinstance(result, dict) and 'directories' in result:
        print(json.dumps({**{k: v for k, v in result.items() if k != 'directories'},
                          'directory_count': len(result['directories']), 'report': str(path)}, ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
