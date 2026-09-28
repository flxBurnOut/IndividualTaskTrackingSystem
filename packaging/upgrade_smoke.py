"""Frozen legacy-to-current upgrade acceptance; synthetic data and owned processes only.

Run from the repository with its Python environment. This does not install the
product, start Codex, invoke a model, open a GUI, or read a real data space.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import psutil
from workflow import runtime_directory, require_test_workspace, checked_path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from management import __version__
from management.runtime_contract import client_headers

DAY = '2038-05-01'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def environment():
    hidden = {'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', '_PYI_ARCHIVE_FILE',
              '_PYI_APPLICATION_HOME_DIR', '_PYI_PARENT_PROCESS_LEVEL', 'PERSONAL_MANAGEMENT_DATA'}
    result = {key: value for key, value in os.environ.items() if key not in hidden}
    result.update(PYINSTALLER_RESET_ENVIRONMENT='1', PYTHONUTF8='1', PERSONAL_MANAGEMENT_NO_TRAY='1')
    return result


def synthetic_path(path):
    selected = Path(path).resolve()
    assert selected.is_relative_to((ROOT / '.build').resolve()), 'Fixture must remain under .build'
    assert selected != (ROOT / '.build').resolve(), 'Never use build root as a data space'
    return selected


class OwnedService:
    def __init__(self, executable, data, *, resume_token=None):
        self.executable, self.data = Path(executable).resolve(), synthetic_path(data)
        self.arguments = [str(self.executable), '--service', '--data-dir', str(self.data)]
        if resume_token is not None:
            self.arguments += ['--resume-update', resume_token]
        self.process = subprocess.Popen(self.arguments, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment(),
            creationflags=subprocess.CREATE_NO_WINDOW)
        observed = psutil.Process(self.process.pid)
        self.created = observed.create_time()
        self.parent = os.getpid()
        self.verify()

    def verify(self):
        process = psutil.Process(self.process.pid)
        assert process.create_time() == self.created, 'Owned PID identity changed'
        assert process.ppid() == self.parent, 'Owned process lineage changed'
        assert Path(process.exe()).resolve() == self.executable, 'Owned executable changed'
        assert process.cmdline() == self.arguments, 'Owned process command line changed'
        return process

    def stop_owned(self):
        if self.process.poll() is None:
            self.verify().terminate()
            self.process.wait(timeout=15)
        assert self.process.poll() is not None

    def wait_closed(self):
        self.process.wait(timeout=20)
        assert self.process.returncode == 0, 'New service did not exit cleanly'

    def runtime(self):
        value = json.loads((self.data / 'runtime.json').read_text('utf-8'))
        assert value.get('pid') == self.process.pid, 'Discovery belongs to another process'
        assert Path(value['data_dir']).resolve() == self.data
        assert value['host'] == '127.0.0.1'
        self.verify()
        return value

    def request(self, category, name, payload=None, *, current=False, expect_error=None):
        runtime = self.runtime()
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + runtime['token']}
        if current:
            headers.update(client_headers('installer'))
        request = urllib.request.Request(
            'http://127.0.0.1:%s/v1/%s/%s' % (runtime['port'], category, name),
            data=json.dumps(payload or {}, ensure_ascii=False).encode('utf-8'), headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=40) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            result = json.loads(error.read())
            assert expect_error and result.get('error', {}).get('code') == expect_error, result
            return result
        assert expect_error is None, 'Expected rejection: ' + str(expect_error)
        return result

    def wait_ready(self, *, current=False):
        deadline = time.monotonic() + 45
        last = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError('Owned service exited before readiness; see synthetic diagnostic.log')
            try:
                state = self.request('query', 'state', current=current)
                assert not state.get('maintenance')
                return state
            except (OSError, ValueError, AssertionError, urllib.error.URLError) as error:
                last = error
                time.sleep(.15)
        raise AssertionError('Owned service did not become ready') from last

    def command(self, name, payload):
        state = self.request('query', 'state')
        return self.request('commands', name, {'request_id': str(uuid.uuid4()),
            'epoch': state['epoch'], 'expected_revision': state['revision'], 'payload': payload})


def seed(service, work):
    assert not service.request('query', 'settings')['settings']['ai']['enabled']
    course = service.command('create', {'type': 'course', 'title': '合成课程', 'data': {'code': 'TEST101'}})['result']['entity']
    task = service.command('create', {'type': 'task', 'title': '升级后仍保留的任务', 'parent_id': course['id'],
        'data': {'completion_gate': '合成验收条目', 'notes': '用户原始说明'}})['result']['entity']
    plan = service.command('create_plan', {'date': DAY, 'mode': 'no_precise_time',
        'title': '合成每日计划', 'blocks': [{'target_id': task['id'], 'minutes': 25}]})['result']['entity']
    feedback = service.command('record_feedback', {'target_id': task['id'], 'business_date': DAY,
        'dimensions': {'completion': 'unknown'}, 'source_text': '合成明确反馈：尚未确认完成'})['result']['entity']
    original = work / '合成资料 原文.txt'
    original.write_text('升级验收资料。保持中文、换行和内容。\n' * 150, encoding='utf-8')
    asset = service.command('import_asset', {'path': str(original), 'owner_id': course['id'],
        'source_text': '合成资料导入'})['result']['entity']
    service.command('settings', {'settings': {'appearance': {'theme': 'dark', 'font_size': 15}, 'favorites': [course['id']]}})
    assert service.request('query', 'jobs')['total'] == 0
    state = service.request('query', 'state')
    return {'course': course['id'], 'task': task['id'], 'plan': plan['id'],
            'feedback': feedback['id'], 'asset': asset['id'], 'epoch': state['epoch'], 'revision': state['revision']}


def seed_saved_history(data, epoch):
    """Offline synthetic saved history, never a real Codex thread or model turn."""
    conversation_id = str(uuid.uuid4())
    scope = json.dumps({'kind': 'daily_plan', 'date': DAY}, sort_keys=True, separators=(',', ':'))
    thread_id = 'synthetic-only-' + uuid.uuid4().hex
    with sqlite3.connect(data / 'database.sqlite3') as connection:
        connection.execute('INSERT INTO conversations(id,scope_key,scope,provider_thread_id,created_at,updated_at) VALUES (?,?,?,?,?,?)',
            (conversation_id, hashlib.sha256(scope.encode()).hexdigest(), scope, thread_id, '2038-01-01', '2038-01-01'))
        connection.execute('INSERT INTO conversation_messages(id,conversation_id,role,text,state,created_at) VALUES (?,?,?,?,?,?)',
            (str(uuid.uuid4()), conversation_id, 'user', '合成历史消息原文', 'completed', '2038-01-01'))
        connection.execute('INSERT INTO conversation_bindings VALUES (?,?,?,?,?,?,?,?,?)',
            (conversation_id, thread_id, epoch, str(data / 'Codex事务助手'), 'desktop_mcp_v3', 'active',
             'synthetic offline acceptance fixture', '2038-01-01', '2038-01-01'))
    for relative, contents in {
        'Codex事务助手/.codex/fixture.toml': b'[fixture]\nsynthetic_only=true\n',
        '用户自存资料/不删除.bin': bytes(range(256)) * 4,
    }.items():
        file = data / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(contents)
    return conversation_id


def tables(path, layout=None):
    with contextlib.closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as connection:
        if layout is None:
            names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            layout = {name: [row[1] for row in connection.execute('PRAGMA table_info("' + name.replace('"', '""') + '")')]
                      for name in names}
        result = {}
        for name, columns in layout.items():
            quote = lambda value: '"' + value.replace('"', '""') + '"'
            query = 'SELECT ' + ','.join(map(quote, columns)) + ' FROM ' + quote(name)
            if name == 'meta':
                query += " WHERE key != 'framework_version'"
            rows = connection.execute(query).fetchall()
            result[name] = sorted(rows, key=repr)
        return layout, result


def asset_files(data):
    excluded = {'database.sqlite3', 'database.sqlite3-wal', 'database.sqlite3-shm',
                'runtime.json', 'diagnostic.log', 'service.lock', 'schema-upgrade.lock',
                'update_pending.json', 'upgrade-state.json', 'gui.lock', 'tray.lock',
                'tray-status.json', 'tray-status.json.new'}
    return {str(path.relative_to(data)): digest(path) for path in data.rglob('*')
            if path.is_file() and str(path.relative_to(data)) not in excluded
            and path.relative_to(data).parts[0] != 'upgrade-backups'
            and not path.name.startswith('diagnostic.log.')}


async def verify_mcp(executable, data):
    parameters = StdioServerParameters(command=str(executable), args=['--mcp', '--data-dir', str(data)], env=environment())
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            server_version = initialized.model_dump(by_alias=True)['serverInfo']['version']
            assert server_version == __version__
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert 'read_business_page' in names and 'save_plan' in names
            response = await session.call_tool('query_business', {'name': 'state'})
            assert not response.is_error and isinstance(response.structured_content, dict)
            value = response.structured_content
            assert value['service_contract']['app_version'] == __version__
            return {'version': server_version, 'tools': len(names)}


def binaries(package):
    spec = importlib.util.spec_from_file_location('upgrade_installer_metadata', ROOT / 'packaging/build_installer.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {name: {'sha256': digest(package / name), 'pe_version': module.pe_version(package / name)}
            for name in module.EXES}


async def run(args):
    if os.name != 'nt':
        raise SystemExit('This frozen upgrade acceptance requires Windows.')
    old_package, new_package = Path(args.old_package).resolve(), Path(args.new_package).resolve()
    for package in (old_package, new_package):
        assert package.is_relative_to(ROOT / 'release'), 'Only repository release packages are accepted'
        assert (package / 'PersonalManagementService.exe').is_file(), str(package)
    work = require_test_workspace(args.work_dir) / 'upgrade'
    work.mkdir(parents=True)
    data = work / '自选资料 有空格'
    data.mkdir()
    report_path = checked_path(args.report, ROOT / '.build' / 'checks')
    report = {'synthetic_only': True, 'models_started': 0, 'codex_threads_created': 0,
              'installed_product': False, 'data_dir': str(data), 'passed': False, 'checks': {}}
    owned = []
    try:
        report['old_binaries'], report['new_binaries'] = binaries(old_package), binaries(new_package)
        assert all(item['pe_version'] == '1.0.2.0' for item in report['old_binaries'].values())
        assert all(item['pe_version'] == __version__ + '.0' for item in report['new_binaries'].values())
        old = OwnedService(old_package / 'PersonalManagementService.exe', data)
        owned.append(old)
        old.wait_ready()
        identifiers = seed(old, work)
        old.stop_owned()  # Legacy has no idle-shutdown route; this process is our own empty-work fixture.
        report['checks']['old_process_identity_verified_before_stop'] = True
        identifiers['conversation'] = seed_saved_history(data, identifiers['epoch'])
        layout, before = tables(data / 'database.sqlite3')
        before_files = asset_files(data)
        assert before_files and any(name.startswith('blobs') for name in before_files)
        (work / 'before-tables.json').write_text(json.dumps({'columns': layout, 'rows': before}, ensure_ascii=False, indent=2), 'utf-8')
        report['preserved_table_count'], report['preserved_original_file_count'] = len(layout), len(before_files)
        report['fixture_identity'] = identifiers
        report['checks']['legacy_seed_includes_history_binding_settings_and_material'] = True

        new = OwnedService(new_package / 'PersonalManagementService.exe', data)
        owned.append(new)
        state = new.wait_ready(current=True)
        assert state['service_contract']['app_version'] == __version__
        assert state['epoch'] == identifiers['epoch'] and state['revision'] == identifiers['revision']
        assert tables(data / 'database.sqlite3', layout)[1] == before, 'Legacy table values changed during upgrade'
        assert asset_files(data) == before_files, 'Original files changed during upgrade'
        receipt = json.loads((data / 'upgrade-state.json').read_text('utf-8'))
        assert receipt['status'] == 'completed' and receipt['backup_scope'] == 'database_only'
        assert receipt['to_version'] == __version__ and receipt['assets'] == 'retained_at_original_paths'
        snapshot = (data / receipt['snapshot']).resolve()
        assert snapshot.is_relative_to(data / 'upgrade-backups')
        assert digest(snapshot) == receipt['snapshot_sha256']
        assert tables(snapshot, layout)[1] == before
        report['checks']['database_snapshot_matches_every_legacy_column'] = True
        report['checks']['epoch_revision_data_files_and_all_legacy_tables_preserved'] = True

        new.request('commands', 'create', {}, expect_error='client_version_mismatch')
        assert tables(data / 'database.sqlite3', layout)[1] == before
        report['checks']['legacy_entrance_rejected_without_write'] = True
        report['mcp'] = await verify_mcp(new.executable, data)
        observed = new.request('query', 'runtime_status', current=True)['client_entrances']['mcp']
        assert observed['verified'] and observed['app_version'] == __version__
        report['mcp']['verified_entrance'] = True
        assert new.request('query', 'jobs', current=True)['total'] == 0
        assert new.request('maintenance', 'shutdown-if-idle', current=True)['prepared']
        new.wait_closed()
        marker = data / 'update_pending.json'
        assert json.loads(marker.read_text('utf-8'))['status'] == 'ready'
        report['checks']['idle_update_graceful_shutdown'] = True

        blocked = OwnedService(new.executable, data)
        owned.append(blocked)
        blocked.process.wait(timeout=15)
        assert blocked.process.returncode != 0 and marker.exists(), 'Background restarted through update fence'
        report['checks']['background_restart_fenced'] = True
        value = json.loads(marker.read_text('utf-8'))
        token = uuid.uuid4().hex
        temporary = marker.with_name(marker.name + '.acceptance-new')
        temporary.write_text(json.dumps({**value, 'resume_token': token}), encoding='utf-8')
        os.replace(temporary, marker)
        resumed = OwnedService(new.executable, data, resume_token=token)
        owned.append(resumed)
        state = resumed.wait_ready(current=True)
        assert not marker.exists() and state['service_contract']['app_version'] == __version__
        assert state['epoch'] == identifiers['epoch'] and state['revision'] == identifiers['revision']
        assert tables(data / 'database.sqlite3', layout)[1] == before
        assert asset_files(data) == before_files
        assert len(list((data / 'upgrade-backups').iterdir())) == 1, 'Ordinary restart created a second upgrade snapshot'
        assert resumed.request('maintenance', 'shutdown-if-idle', current=True)['prepared']
        resumed.wait_closed()
        report['checks']['explicit_resume_token_cli_preserves_data_and_one_snapshot'] = True
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)}
        raise
    finally:
        cleanup_errors = []
        for process in reversed(owned):
            try:
                process.stop_owned()
            except Exception as error:
                cleanup_errors.append(type(error).__name__ + ': ' + str(error))
        report['owned_processes_stopped'] = all(process.process.poll() is not None for process in owned)
        if cleanup_errors:
            report['cleanup_errors'] = cleanup_errors
            report['passed'] = False
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'report': str(report_path), 'passed': report['passed'],
                          'checks': report['checks'], 'owned_processes_stopped': report['owned_processes_stopped']}, ensure_ascii=False))
        if cleanup_errors:
            raise RuntimeError('Owned synthetic process cleanup failed; see report')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-package', type=Path, default=runtime_directory('1.0.2'))
    parser.add_argument('--new-package', type=Path, default=runtime_directory(__version__))
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
