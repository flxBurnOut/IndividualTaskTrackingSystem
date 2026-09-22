"""Synthetic recovery boundaries: durable files, schema refusal, resources, hierarchy."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
import uuid

import pytest

from management.core import Core
from management.resources import ResourceError
from management.schemas import BusinessError


def command(core, name, payload, *, state=None, request_id=None):
    state = state or core.query('state')
    return core.command(name, payload, request_id=request_id or str(uuid.uuid4()),
                        epoch=state['epoch'], expected_revision=state['revision'])


def create(core, kind='task', title='Synthetic', **kwargs):
    return command(core, 'create', {'type': kind, 'title': title, **kwargs})['result']['entity']


def file_request(core, root, name):
    if name == 'backup':
        return {}
    source = root / 'evidence.txt'
    source.write_text('Synthetic immutable evidence', encoding='utf-8')
    entity = command(core, 'import_asset', {'path': str(source)})['result']['entity']
    return {'id': entity['id'], 'target_dir': str(root / 'export')}


@pytest.mark.parametrize('name', ['backup', 'export_asset'])
def test_file_receipt_commits_after_concurrent_ordinary_write(tmp_path, monkeypatch, name):
    core = Core(tmp_path / 'data')
    payload = file_request(core, tmp_path, name)
    state, rid = core.query('state'), str(uuid.uuid4())
    prepared, release = threading.Event(), threading.Event()
    original = core._prepare
    calls = []

    def slow_prepare(operation, value):
        result = original(operation, value)
        if operation == name:
            calls.append(result)
            prepared.set()
            assert release.wait(10), 'Test did not release prepared I/O'
        return result

    monkeypatch.setattr(core, '_prepare', slow_prepare)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(command, core, name, payload, state=state, request_id=rid)
        try:
            assert prepared.wait(10), 'File preparation failed or blocked'
            ordinary = pool.submit(create, core, title='Write during file operation').result(5)
            assert core.query('get', id=ordinary['id'])['entity']['title'] == ordinary['title']
        finally:
            release.set()
        receipt = future.result(10)
    assert receipt['revision'] == state['revision'] + 2
    assert core.query('receipt', request_id=rid)['receipt']['result'] == receipt['result']
    assert core.query('operation', request_id=rid)['operation']['status'] == 'committed'
    assert len(calls) == 1
    if name == 'backup':
        assert core.resources.verify_backup(receipt['result']['path'])['valid']
    else:
        assert (Path(receipt['result']['path']) / 'evidence.txt').read_text('utf-8') == 'Synthetic immutable evidence'
    replay = command(core, name, payload, state=state, request_id=rid)
    assert replay['replayed'] and replay['result'] == receipt['result']
    assert len(calls) == 1


@pytest.mark.parametrize('name', ['backup', 'export_asset'])
def test_ready_io_survives_restart_and_retry_does_not_repeat_files(tmp_path, monkeypatch, name):
    root = tmp_path / 'data'
    core = Core(root)
    payload = file_request(core, tmp_path, name)
    state, rid = core.query('state'), str(uuid.uuid4())
    dispatch = core._dispatch

    def fail_commit(c, operation, value, request_id, prepared=None):
        if operation == name:
            raise BusinessError('injected_commit_failure', 'Synthetic commit interruption')
        return dispatch(c, operation, value, request_id, prepared)

    monkeypatch.setattr(core, '_dispatch', fail_commit)
    with pytest.raises(BusinessError, match='Synthetic commit interruption'):
        command(core, name, payload, state=state, request_id=rid)
    operation = core.query('operation', request_id=rid)['operation']
    assert operation['status'] == 'ready'
    assert not core.query('receipt', request_id=rid)['found']
    prepared_result = operation['result']
    paths_before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in Path(prepared_result['path']).rglob('*') if p.is_file()}

    reopened = Core(root)
    create(reopened, title='Committed after interruption')
    def forbidden_prepare(*args, **kwargs):
        raise AssertionError('Retry must recover durable prepared result without running file I/O')
    monkeypatch.setattr(reopened, '_prepare', forbidden_prepare)
    recovered = command(reopened, name, payload, state=state, request_id=rid)
    assert recovered['result'] == prepared_result
    assert recovered['revision'] == state['revision'] + 2
    assert reopened.query('operation', request_id=rid)['operation']['status'] == 'committed'
    paths_after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in Path(prepared_result['path']).rglob('*') if p.is_file()}
    assert paths_after == paths_before
    assert command(reopened, name, payload, state=state, request_id=rid)['replayed']


def test_restore_same_request_replays_without_replacing_restored_files(tmp_path, monkeypatch):
    core = Core(tmp_path / 'data')
    original = create(core, title='Before backup')
    backup = command(core, 'backup', {})['result']
    state, rid = core.query('state'), str(uuid.uuid4())
    target = tmp_path / 'restored'
    payload = {'path': backup['path'], 'target_dir': str(target)}
    first = command(core, 'restore_backup', payload, state=state, request_id=rid)
    restored = Core(target)
    restored_epoch = restored.query('state')['epoch']
    added = create(restored, title='Created after restore')
    def cannot_restore_twice(*args, **kwargs):
        raise AssertionError('A receipt replay must not perform a second restore')
    monkeypatch.setattr(core, '_prepare', cannot_restore_twice)
    second = command(core, 'restore_backup', payload, state=state, request_id=rid)
    assert second['replayed'] and second['result'] == first['result']
    assert Core(target).query('state')['epoch'] == restored_epoch
    assert restored.query('get', id=added['id'])['entity']['title'] == added['title']
    assert restored.query('get', id=original['id'])['entity']['title'] == original['title']


@pytest.mark.parametrize('schema', [999, None, 'missing_meta'])
def test_unknown_schema_refused_without_changing_database_bytes_or_tables(tmp_path, schema):
    root = tmp_path / 'foreign'
    root.mkdir()
    db = root / 'database.sqlite3'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE sentinel(value TEXT)')
        c.execute('INSERT INTO sentinel VALUES (?)', ('must survive refusal',))
        if schema != 'missing_meta':
            c.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            if schema is not None:
                c.execute('INSERT INTO meta VALUES (?,?)', ('schema_version', json.dumps(schema)))
    before = db.read_bytes()
    names = {p.name for p in root.iterdir()}
    with pytest.raises(BusinessError) as raised:
        Core(root)
    assert raised.value.code == ('unknown_database' if schema == 'missing_meta' else 'schema_version')
    assert db.read_bytes() == before
    assert {p.name for p in root.iterdir()} == names
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == ({'sentinel'} if schema == 'missing_meta' else {'sentinel', 'meta'})
        assert c.execute('SELECT value FROM sentinel').fetchone()[0] == 'must survive refusal'


def test_pending_restore_rejects_startup_before_database_mutation(tmp_path):
    root = tmp_path / 'data'
    core = Core(root)
    create(core, title='Restored but not reconciled')
    marker = root / 'restore_pending.json'
    marker.write_text('{"state":"awaiting_service_reconciliation"}', encoding='utf-8')
    before = core.store.path.read_bytes()
    names = {p.name for p in root.iterdir()}
    with pytest.raises(BusinessError) as raised:
        Core(root)
    assert raised.value.code == 'restore_pending'
    assert core.store.path.read_bytes() == before
    assert {p.name for p in root.iterdir()} == names
    assert marker.read_text('utf-8') == '{"state":"awaiting_service_reconciliation"}'


def test_changed_reserve_applies_to_existing_resource_manager_and_actual_import(tmp_path, monkeypatch):
    core = Core(tmp_path / 'data')
    manager = core.resources
    gib = 1024 ** 3
    monkeypatch.setattr('management.resources.shutil.disk_usage', lambda _: SimpleNamespace(free=2*gib, total=4*gib, used=2*gib))
    source = tmp_path / 'evidence.txt'
    source.write_text('synthetic', encoding='utf-8')
    command(core, 'settings', {'settings': {'reserve_bytes': 3*gib}})
    assert core.resources is manager and manager.reserve_bytes == 3*gib
    with pytest.raises(ResourceError) as raised:
        command(core, 'import_asset', {'path': str(source)})
    assert raised.value.code == 'LOW_DISK_SPACE'
    assert core.query('list', type='asset')['total'] == 0
    assert not list((core.root / 'blobs').iterdir())
    command(core, 'settings', {'settings': {'reserve_bytes': gib}})
    imported = command(core, 'import_asset', {'path': str(source)})['result']['entity']
    assert manager.reserve_bytes == gib
    assert (core.root / imported['data']['path']).read_text('utf-8') == 'synthetic'


def test_task_and_milestone_move_into_phase_change_project_scope_once(tmp_path):
    core = Core(tmp_path / 'data')
    first = create(core, 'project', title='Main project', data={'track': 'main'})
    second = create(core, 'project', title='Side project', data={'track': 'side'})
    phase = create(core, 'phase', title='Phase', parent_id=second['id'])
    task = create(core, parent_id=first['id'])
    milestone = create(core, 'milestone', title='Gate', parent_id=first['id'])
    for minutes in (25, 15):
        command(core, 'record_feedback', {'target_id': task['id'], 'business_date': '2030-01-01', 'dimensions': {'actual_minutes': minutes}, 'source_text': 'Explicit synthetic correction'})
    for entity in (task, milestone):
        moved = command(core, 'move', {'id': entity['id'], 'version': entity['version'], 'parent_id': phase['id']})['result']['entity']
        assert moved['id'] == entity['id'] and moved['parent_id'] == phase['id']
    command(core, 'link', {'source_id': first['id'], 'target_id': task['id'], 'kind': 'references'})
    a = core.query('project_summary', project_id=first['id'])
    b = core.query('project_summary', project_id=second['id'])
    assert {x['id'] for x in a['items']} == {first['id']}
    assert {x['id'] for x in b['items']} == {second['id'], phase['id'], task['id'], milestone['id']}
    assert a['tracks']['main']['leaf_actual_minutes'] == 0
    assert b['tracks']['side']['leaf_actual_minutes'] == 15
    assert b['unknown_work_time_targets'] == 0
    assert b['coverage']['total'] == 4
