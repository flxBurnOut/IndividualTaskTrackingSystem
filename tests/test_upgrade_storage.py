import json
from pathlib import Path
import sqlite3

import pytest

import management
from management.schemas import BusinessError
from management.storage import Store
from management import upgrade_storage as upgrades


def marker(path):
    with sqlite3.connect(path / 'database.sqlite3') as c:
        row = c.execute('SELECT value FROM meta WHERE key=?', (upgrades.VERSION_KEY,)).fetchone()
        return json.loads(row[0]) if row else None


def records(path):
    with sqlite3.connect(path / 'database.sqlite3') as c:
        return c.execute('SELECT * FROM entities ORDER BY id').fetchall()


def seed(path, monkeypatch, version='1.0.2', legacy=False):
    monkeypatch.setattr(management, '__version__', version)
    store = Store(path)
    with store.connect() as c:
        c.execute("INSERT INTO entities VALUES ('personal','note','保留的笔记',NULL,'active',0,'{\"content\":\"我的原文\"}',7,'before','before')")
        if legacy:
            c.execute('DELETE FROM meta WHERE key=?', (upgrades.VERSION_KEY,))
    return store


def test_legacy_upgrade_preserves_records_epoch_revision_assets_and_makes_one_snapshot(tmp_path, monkeypatch):
    store = seed(tmp_path, monkeypatch, legacy=True)
    with store.connect() as c:
        before_state = store.state(c)
    before = records(tmp_path)
    files = {'blobs/object': b'original binary\0\1', '原文件/笔记.txt': '原文'.encode(),
             'Codex事务助手/.codex/config.toml': b'preserve configuration'}
    for name, value in files.items():
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(value)
    monkeypatch.setattr(management, '__version__', '1.1.0')
    upgraded = Store(tmp_path)
    assert marker(tmp_path) == '1.1.0'
    assert records(tmp_path) == before
    with upgraded.connect() as c:
        assert upgraded.state(c) == before_state
    for name, value in files.items():
        assert (tmp_path / name).read_bytes() == value
    status = json.loads((tmp_path / upgrades.STATUS_FILE).read_text(encoding='utf-8'))
    assert status['status'] == 'completed'
    assert status['backup_scope'] == 'database_only'
    assert status['assets'] == 'retained_at_original_paths'
    with sqlite3.connect(tmp_path / status['snapshot']) as c:
        assert c.execute('SELECT * FROM entities ORDER BY id').fetchall() == before
        assert c.execute('SELECT value FROM meta WHERE key=?', (upgrades.VERSION_KEY,)).fetchone() is None
    Store(tmp_path)
    assert len(list((tmp_path / upgrades.BACKUP_DIR).iterdir())) == 1


def test_snapshot_captures_committed_wal_not_only_main_database(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    keeper = sqlite3.connect(tmp_path / 'database.sqlite3', isolation_level=None)
    try:
        keeper.execute('PRAGMA wal_autocheckpoint=0')
        keeper.execute("UPDATE entities SET title='仅在 WAL 中的新名称'")
        assert (tmp_path / 'database.sqlite3-wal').stat().st_size > 0
        monkeypatch.setattr(management, '__version__', '1.1.0')
        Store(tmp_path)
        status = json.loads((tmp_path / upgrades.STATUS_FILE).read_text(encoding='utf-8'))
        with sqlite3.connect(tmp_path / status['snapshot']) as c:
            assert c.execute('SELECT title FROM entities').fetchone()[0] == '仅在 WAL 中的新名称'
    finally:
        keeper.close()


def test_future_framework_refused_before_mutation(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch, version='1.2.0')
    before = (tmp_path / 'database.sqlite3').read_bytes()
    monkeypatch.setattr(management, '__version__', '1.1.0')
    with pytest.raises(BusinessError) as error:
        Store(tmp_path)
    assert error.value.code == 'upgrade_downgrade'
    assert marker(tmp_path) == '1.2.0'
    assert (tmp_path / 'database.sqlite3').read_bytes() == before
    assert not (tmp_path / upgrades.BACKUP_DIR).exists()


def test_backup_disk_failure_prevents_schema_changes(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    before = records(tmp_path)
    monkeypatch.setattr(management, '__version__', '1.1.0')
    original_write = upgrades._json_write

    def no_space(path, value):
        if path.name == upgrades.STATUS_FILE:
            raise OSError('disk full')
        return original_write(path, value)

    monkeypatch.setattr(upgrades, '_json_write', no_space)
    with pytest.raises(BusinessError) as error:
        Store(tmp_path)
    assert error.value.code == 'upgrade_backup_failed'
    assert marker(tmp_path) == '1.0.2'
    assert records(tmp_path) == before


def test_failed_nested_migration_rolls_back_ddl_dml_and_retry_uses_fresh_snapshot(tmp_path, monkeypatch):
    from management import context_schema
    seed(tmp_path, monkeypatch)
    before = records(tmp_path)
    original_initialize = context_schema.initialize

    def fail(c):
        c.executescript("CREATE TABLE temporary_migration_step(value TEXT); UPDATE entities SET title='不应保存';")
        raise RuntimeError('migration interrupted')

    monkeypatch.setattr(context_schema, 'initialize', fail)
    monkeypatch.setattr(management, '__version__', '1.1.0')
    with pytest.raises(RuntimeError, match='migration interrupted'):
        Store(tmp_path)
    assert marker(tmp_path) == '1.0.2'
    assert records(tmp_path) == before
    with sqlite3.connect(tmp_path / 'database.sqlite3') as c:
        assert c.execute("SELECT name FROM sqlite_master WHERE name='temporary_migration_step'").fetchone() is None
        c.execute("UPDATE entities SET title='失败后用户新增编辑'")
    failed = json.loads((tmp_path / upgrades.STATUS_FILE).read_text(encoding='utf-8'))
    assert failed['status'] == 'failed'
    monkeypatch.setattr(context_schema, 'initialize', original_initialize)
    Store(tmp_path)
    succeeded = json.loads((tmp_path / upgrades.STATUS_FILE).read_text(encoding='utf-8'))
    assert succeeded['status'] == 'completed'
    assert succeeded['snapshot'] != failed['snapshot']
    with sqlite3.connect(tmp_path / succeeded['snapshot']) as c:
        assert c.execute('SELECT title FROM entities').fetchone()[0] == '失败后用户新增编辑'
    assert len(list((tmp_path / upgrades.BACKUP_DIR).iterdir())) == 2


def test_commit_receipt_interruption_recovers_without_overwriting_later_edits(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    original_record = upgrades._record
    monkeypatch.setattr(management, '__version__', '1.1.0')

    def fail_receipt(root, record, status):
        if status == 'completed':
            raise OSError('interrupted receipt write')
        return original_record(root, record, status)

    monkeypatch.setattr(upgrades, '_record', fail_receipt)
    with pytest.raises(OSError, match='interrupted receipt'):
        Store(tmp_path)
    assert marker(tmp_path) == '1.1.0'
    with sqlite3.connect(tmp_path / 'database.sqlite3') as c:
        c.execute("UPDATE entities SET title='提交之后的修改'")
    monkeypatch.setattr(upgrades, '_record', original_record)
    Store(tmp_path)
    status = json.loads((tmp_path / upgrades.STATUS_FILE).read_text(encoding='utf-8'))
    assert status['status'] == 'completed'
    assert records(tmp_path)[0][2] == '提交之后的修改'
    assert len(list((tmp_path / upgrades.BACKUP_DIR).iterdir())) == 1


def test_damaged_pending_snapshot_is_not_used_or_restored(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    monkeypatch.setattr(management, '__version__', '1.1.0')
    Store(tmp_path)
    status_path = tmp_path / upgrades.STATUS_FILE
    status = json.loads(status_path.read_text(encoding='utf-8'))
    status['status'] = 'prepared'
    status_path.write_text(json.dumps(status), encoding='utf-8')
    (tmp_path / status['snapshot']).write_bytes(b'corrupted snapshot')
    before = records(tmp_path)
    with pytest.raises(BusinessError) as error:
        Store(tmp_path)
    assert error.value.code == 'upgrade_snapshot_changed'
    assert records(tmp_path) == before
    assert marker(tmp_path) == '1.1.0'


def test_new_data_space_has_no_fake_upgrade_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(management, '__version__', '1.1.0')
    Store(tmp_path)
    assert marker(tmp_path) == '1.1.0'
    assert not (tmp_path / upgrades.BACKUP_DIR).exists()
    assert not (tmp_path / upgrades.STATUS_FILE).exists()


def test_concurrent_schema_start_is_rejected_without_mutation(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    lock = upgrades.OwnerLock(tmp_path / 'schema-upgrade.lock')
    assert lock.acquire()
    try:
        monkeypatch.setattr(management, '__version__', '1.1.0')
        with pytest.raises(BusinessError) as error:
            Store(tmp_path)
        assert error.value.code == 'upgrade_busy'
        assert marker(tmp_path) == '1.0.2'
    finally:
        lock.release()


def test_transaction_script_keeps_trigger_semicolons_and_literals():
    c = sqlite3.connect(':memory:', isolation_level=None, factory=upgrades._StartupConnection)
    try:
        c.execute('BEGIN IMMEDIATE')
        c.executescript("""CREATE TABLE a(x TEXT); CREATE TABLE b(y TEXT);
            CREATE TRIGGER added AFTER INSERT ON a BEGIN
                INSERT INTO b VALUES ('a; b'); INSERT INTO b VALUES (new.x);
            END;
            INSERT INTO a VALUES ('value; two'); -- trailing comment""")
        assert c.execute('SELECT * FROM b').fetchall() == [('a; b',), ('value; two',)]
        c.rollback()
        assert c.execute("SELECT name FROM sqlite_master WHERE name='a'").fetchone() is None
    finally:
        c.close()


def test_writers_excluded_from_snapshot_until_migration_commit(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    monkeypatch.setattr(management, '__version__', '1.1.0')
    original_snapshot = upgrades._snapshot
    checked = []

    def snapshot_with_competing_writer(root, previous, current):
        with sqlite3.connect(root / 'database.sqlite3', timeout=0) as writer:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                writer.execute("UPDATE entities SET title='racing write'")
        checked.append(True)
        return original_snapshot(root, previous, current)

    monkeypatch.setattr(upgrades, '_snapshot', snapshot_with_competing_writer)
    Store(tmp_path)
    assert checked == [True]
    assert records(tmp_path)[0][2] == '保留的笔记'


def test_foreign_key_damage_stops_upgrade_before_marking_new_version(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    with sqlite3.connect(tmp_path / 'database.sqlite3') as c:
        c.execute("INSERT INTO links VALUES ('broken','missing','personal','relates','before')")
    monkeypatch.setattr(management, '__version__', '1.1.0')
    with pytest.raises(BusinessError) as error:
        Store(tmp_path)
    assert error.value.code == 'upgrade_database_check'
    assert marker(tmp_path) == '1.0.2'
    assert not (tmp_path / upgrades.STATUS_FILE).exists()


def test_interrupted_new_database_can_retry_without_manual_deletion(tmp_path, monkeypatch):
    from management import context_schema
    original = context_schema.initialize
    monkeypatch.setattr(context_schema, 'initialize', lambda c: (_ for _ in ()).throw(RuntimeError('interrupt')))
    with pytest.raises(RuntimeError, match='interrupt'):
        Store(tmp_path)
    monkeypatch.setattr(context_schema, 'initialize', original)
    Store(tmp_path)
    assert marker(tmp_path) == management.__version__
    assert not (tmp_path / upgrades.BACKUP_DIR).exists()
