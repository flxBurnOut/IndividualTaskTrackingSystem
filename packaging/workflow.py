"""Shared locations and bounded, owned test workspaces. No business data writes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from management.runtime import OwnerLock


class SuiteBusy(RuntimeError):
    pass


def version_directory(version, root=ROOT):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Expected a numeric release version')
    return Path(root) / 'release' / ('PersonalManagement-' + version)


def runtime_directory(version, root=ROOT):
    folder = version_directory(version, root)
    # Older active installations retain their exact executable paths.
    if not (folder / 'app').exists() and (folder / 'PersonalManagement.exe').is_file():
        return folder
    return folder / 'app'


def checked_path(path, parent, *, allow_parent=False):
    parent = Path(parent).absolute()
    path = Path(path).absolute()
    if not path.is_relative_to(parent) or (path == parent and not allow_parent):
        raise ValueError('Path is outside the selected workspace: ' + str(path))
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise ValueError('Linked workspace path: ' + str(item))
    resolved = path.resolve()
    if not resolved.is_relative_to(parent.resolve()) or (resolved == parent.resolve() and not allow_parent):
        raise ValueError('Resolved path escapes workspace')
    return resolved


def scoped_processes(folder, records=None):
    """Find live processes with an executable or direct path argument here."""
    folder = Path(folder).resolve()
    if records is not None:
        return [record for record in records if any(Path(p).is_relative_to(folder) for p in record['paths'])]
    found = []
    for process in psutil.process_iter(['pid', 'exe', 'cmdline', 'create_time']):
        if process.pid == os.getpid():
            continue
        try:
            info = process.info
            values = [info['exe'], *[a for a in (info['cmdline'] or []) if not a.startswith('-')]]
            paths = []
            for value in values:
                if not value or not Path(value).is_absolute():
                    continue
                try:
                    paths.append(str(Path(value).resolve()))
                except (OSError, ValueError):
                    pass
            if any(Path(value).is_relative_to(folder) for value in paths):
                found.append({'pid': process.pid, 'created': info['create_time'],
                              'exe': info['exe'], 'args': info['cmdline'], 'paths': paths})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def terminate_verified(record):
    try:
        process = psutil.Process(record['pid'])
        if (process.create_time() != record['created'] or process.exe() != record['exe']
                or process.cmdline() != record['args']):
            raise RuntimeError('Process identity changed; refusing to stop it')
        process.terminate()
        process.wait(10)
    except psutil.NoSuchProcess:
        pass


def remove_generated(path, parent, *, process_records=None):
    """Remove only a validated generated tree; never traverse reparse links."""
    path = checked_path(path, parent)
    active = scoped_processes(path, records=process_records)
    if active:
        raise RuntimeError('Generated files still in use by PID(s): ' + ', '.join(str(p['pid']) for p in active))
    def remove(item):
        if item.is_symlink():
            item.unlink()
            return 0
        if item.is_junction():
            item.rmdir()
            return 0
        if item.is_dir():
            size = sum(remove(Path(entry.path)) for entry in os.scandir(item))
            item.rmdir()
            return size
        if not item.exists():
            return 0
        size = item.stat().st_size
        try:
            item.unlink()
        except PermissionError:
            item.chmod(stat.S_IWRITE | stat.S_IREAD)
            item.unlink()
        return size
    return remove(path)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
    os.replace(temporary, path)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require_test_workspace(path):
    path = checked_path(path, ROOT / '.build' / 'checks')
    marker = path / '.disposable-test-work.json'
    value = json.loads(marker.read_text('utf-8'))
    if value.get('format') != 'personal-management-test-work/1' or Path(value['path']).resolve() != path:
        raise ValueError('Not an owned disposable test workspace')
    return path


class TestRun:
    def __init__(self, name, *, root=ROOT, keep_work=False):
        if not re.fullmatch(r'[a-z][a-z0-9-]*', name):
            raise ValueError('Invalid suite name')
        self.root = Path(root).resolve()
        self.folder = self.root / '.build' / 'checks' / name
        self.work = self.folder / 'work'
        self.keep_work = keep_work
        self.lock = OwnerLock(self.folder / 'run.lock')
        self.started = 0

    def __enter__(self):
        checked_path(self.folder, self.root / '.build')
        if not self.lock.acquire():
            raise SuiteBusy('This test suite is already running')
        return self

    def prepare(self):
        if self.work.exists():
            marker = json.loads((self.work / '.disposable-test-work.json').read_text('utf-8'))
            if marker.get('format') != 'personal-management-test-work/1' or Path(marker['path']).resolve() != self.work:
                raise ValueError('Unknown work directory; refusing to replace it')
            remove_generated(self.work, self.folder)
        self.work.mkdir()
        self.started = time.time()
        write_json(self.work / '.disposable-test-work.json', {'format': 'personal-management-test-work/1',
                   'path': str(self.work), 'started_at': self.started})

    def cleanup(self):
        if not self.started:
            return
        for _ in range(3):
            records = scoped_processes(self.work)
            if not records:
                break
            for record in records:
                if record['created'] < self.started - 2:
                    raise RuntimeError('An older process owns this workspace; leaving its files intact')
                terminate_verified(record)
            time.sleep(.1)
        if not self.keep_work:
            remove_generated(self.work, self.folder)
        self.started = 0

    def __exit__(self, *_):
        try:
            self.cleanup()
        finally:
            self.lock.release()
