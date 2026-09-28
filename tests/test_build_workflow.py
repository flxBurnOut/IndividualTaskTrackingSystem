"""Generated outputs must remain bounded and must never reach business data."""
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging'))
import workflow
import maintenance
import check


def test_cleanup_cannot_delete_parent_or_escape_it(tmp_path):
    work = tmp_path / 'generated'
    work.mkdir()
    other = tmp_path / 'business.txt'
    other.write_text('keep')
    for target in (work, other, work / '..' / 'business.txt'):
        with pytest.raises(ValueError):
            workflow.remove_generated(target, work)
    assert other.read_text() == 'keep' and work.is_dir()


def test_owned_fixture_is_reclaimed_even_when_test_fails(tmp_path):
    with pytest.raises(RuntimeError, match='test failed'):
        with workflow.TestRun('synthetic', root=tmp_path) as run:
            run.prepare()
            (run.work / 'database.sqlite3').write_bytes(b'synthetic database')
            raise RuntimeError('test failed')
    assert not run.work.exists()
    assert (run.folder / 'run.lock').exists()


def test_test_run_reclaims_detached_child_only_in_its_workspace(tmp_path):
    with workflow.TestRun('child', root=tmp_path) as run:
        run.prepare()
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', str(run.work)],
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
    assert child.wait(timeout=5) is not None
    assert not run.work.exists()


def test_unknown_existing_workspace_is_preserved(tmp_path):
    run = workflow.TestRun('existing', root=tmp_path)
    run.work.mkdir(parents=True)
    sentinel = run.work / 'personal.txt'
    sentinel.write_text('keep')
    with pytest.raises(FileNotFoundError):
        with run:
            run.prepare()
    assert sentinel.read_text() == 'keep'


def test_active_process_blocks_generated_cleanup(tmp_path, monkeypatch):
    path = tmp_path / 'work'
    path.mkdir()
    monkeypatch.setattr(workflow, 'scoped_processes', lambda *a, **kw: [{'pid': 123}])
    with pytest.raises(RuntimeError, match='123'):
        workflow.remove_generated(path, tmp_path)
    assert path.exists()


def test_identical_pass_is_reused_but_changed_inputs_and_failures_rerun(tmp_path, monkeypatch):
    tests = tmp_path / 'tests'
    tests.mkdir()
    (tests / 'test_example.py').write_text('')
    monkeypatch.setattr(check, 'ROOT', tmp_path)
    signature = ['one']
    monkeypatch.setattr(check, 'fingerprint', lambda *args: signature[0])
    calls = []
    codes = [0, 1, 0]
    def execute(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=codes[len(calls) - 1])
    monkeypatch.setattr(check.subprocess, 'run', execute)
    args = ['focused', 'tests/test_example.py']
    assert check.main(args) == check.main(args) == 0
    assert len(calls) == 1
    signature[0] = 'changed'
    assert check.main(args) == 1
    assert check.main(args) == 0 and len(calls) == 3
    folder = tmp_path / '.build/checks/focused'
    assert not (folder / 'work').exists()
    assert json.loads((folder / 'failure.json').read_text())['passed'] is False
    assert json.loads((folder / 'latest.json').read_text())['passed'] is True


def test_prune_preserves_business_data_and_upgrade_backups(tmp_path):
    fixture = tmp_path / '.build/old-pytest/test_example0'
    fixture.mkdir(parents=True)
    (fixture / 'synthetic.bin').write_bytes(b'123')
    backup = tmp_path / '.build/pre-v110-update-keep'
    backup.mkdir()
    (backup / 'database.sqlite3').write_bytes(b'backup')
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'database.sqlite3').write_bytes(b'personal')
    preview = maintenance.prune(False, tmp_path)
    plan = tmp_path / '.build/reports/maintenance/preview.json'
    workflow.write_json(plan, preview)
    result = maintenance.prune(True, tmp_path, plan=plan)
    assert result['freed_bytes'] == 3 and not fixture.parent.exists()
    assert (backup / 'database.sqlite3').read_bytes() == b'backup'
    assert (data / 'database.sqlite3').read_bytes() == b'personal'


def test_bulk_cleanup_requires_plan_and_rejects_protected_paths(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    sentinel = data / 'record.txt'
    sentinel.write_text('personal')
    with pytest.raises(ValueError, match='reviewed'):
        maintenance.prune(True, tmp_path)
    plan = tmp_path / '.build/reports/maintenance/unsafe.json'
    workflow.write_json(plan, {'directories': ['data']})
    with pytest.raises(ValueError, match='protected'):
        maintenance.prune(True, tmp_path, plan=plan)
    assert sentinel.read_text() == 'personal'


@pytest.mark.parametrize('active', [False, True])
def test_organize_groups_assets_and_preserves_active_runtime_path(tmp_path, monkeypatch, active):
    folder = workflow.version_directory('1.2.3', tmp_path)
    folder.mkdir(parents=True)
    runtime = folder / 'PersonalManagement.exe'
    runtime.write_bytes(b'program')
    installer = folder.parent / 'PersonalManagement-1.2.3-Setup-x64.exe'
    installer.write_bytes(b'installer')
    receipt = tmp_path / '.build/installer-1.2.3.json'
    workflow.write_json(receipt, {'version': '1.2.3', 'installer_sha256': workflow.digest(installer),
        'files': [{'path': runtime.name, 'sha256': workflow.digest(runtime)}]})
    monkeypatch.setattr(maintenance, 'scoped_processes', lambda path: [{'pid': 1}] if active else [])
    result = maintenance.organize(True, tmp_path)
    assert result['versions'][0]['active_runtime_kept'] is active
    expected = runtime if active else folder / 'app' / runtime.name
    assert expected.read_bytes() == b'program'
    assert (folder / installer.name).read_bytes() == b'installer'
    assert not installer.exists() and (folder / 'SHA256SUMS.txt').is_file()
    assert workflow.runtime_directory('1.2.3', tmp_path) == expected.parent


def test_running_job_prevents_legacy_service_stop(tmp_path):
    with sqlite3.connect(tmp_path / 'database.sqlite3') as db:
        db.execute('create table jobs (status text)')
        db.execute('create table io_operations (status text)')
        db.execute("insert into jobs values ('running')")
    with pytest.raises(RuntimeError, match='still active'):
        maintenance.assert_idle(tmp_path)
