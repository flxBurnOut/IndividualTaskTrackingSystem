"""The Beta showcase uses real business rules and never overwrites edited data."""
import json
import uuid
from pathlib import Path

import pytest

from management import demo_data
from management.client import ClientError
from management.core import Core
from management.runtime import OwnerLock


class LocalClient:
    def __init__(self, core):
        self.core, self.data_dir = core, core.root

    def state(self):
        return self.core.query('state')

    def query(self, name, **parameters):
        return self.core.query(name, **parameters)

    def command(self, name, payload, **envelope):
        return self.core.command(name, payload, **envelope)


@pytest.fixture
def client(tmp_path):
    return LocalClient(Core(tmp_path / 'demo'))


def command(client, name, payload):
    state = client.state()
    return client.command(name, payload, request_id=uuid.uuid4().hex,
                          epoch=state['epoch'], expected_revision=state['revision'])


@pytest.fixture
def small_recipe(monkeypatch):
    def recipe(builder):
        project = builder.entity('project', 'project', '测试项目')
        note = builder.entity('note', 'note', '测试说明', project, content=demo_data.FICTION)
        return {'project_id': project['id'], 'guide_id': note['id']}
    monkeypatch.setattr(demo_data, '_recipe', recipe)
    return recipe


def test_showcase_covers_real_features_without_assistant_or_enabled_schedules(client):
    result = demo_data.seed_demo(client, client.data_dir, reference_date='2026-10-06')
    assert not result['reused']
    state = client.state()
    assert state['counts']['course'] == 2
    assert state['counts']['task'] >= 130
    assert state['counts']['plan'] == 3
    assert state['counts']['feedback'] >= 5
    assert state['counts']['question'] == 1 and state['counts']['gap'] == 1
    tasks = client.query('list', type='task', search='分页样例', limit=20)
    assert tasks['total'] == 120 and len(tasks['items']) == 20
    plan = client.query('today', date='2026-10-06')
    assert plan['current_plan_id']
    ids = result['scenarios']['task_ids']
    blocks = next(p for p in plan['plans'] if p['id'] == plan['current_plan_id'])['data']['blocks']
    assert [block['target_id'] for block in blocks][:2] == [ids['before'], ids['after']]
    settings = client.query('settings')['settings']
    assert settings['ai']['enabled'] is False and settings['ai']['executable'] == ''
    assert all(not e['data']['enabled'] for e in client.query('list', type='recurring_rule')['items'])
    assert all(not e['data']['enabled'] for e in client.query('list', type='schedule')['items'])
    assets = client.query('list', type='asset')['items']
    assert len(assets) == 4 and all(e['data']['managed_copy'] for e in assets)
    jobs = client.query('jobs')['items']
    assert len(jobs) == 1 and jobs[0]['kind'] == 'artifact'
    journal = json.loads((client.data_dir / demo_data.MARKER).read_text('utf-8'))
    assert journal['fictional'] is True and journal['status'] == 'complete'
    assert all(step.get('result') is not None for step in journal['steps'].values())


def test_repeat_is_zero_write_and_preserves_manual_edit(client, small_recipe):
    first = demo_data.seed_demo(client, client.data_dir, reference_date='2026-10-06')
    item = client.query('get', id=first['scenarios']['project_id'])['entity']
    command(client, 'update', {'id': item['id'], 'version': item['version'], 'patch': {'title': 'My edited demo'}})
    before = client.state()
    again = demo_data.seed_demo(client, client.data_dir)
    assert again['reused'] and again['reference_date'] == '2026-10-06'
    assert client.state() == before
    assert client.query('get', id=item['id'])['entity']['title'] == 'My edited demo'


@pytest.mark.parametrize('commit', [False, True])
def test_interrupted_request_resumes_same_identity_without_duplicates(client, small_recipe, monkeypatch, commit):
    original = client.command
    requests = []
    def interrupt(name, payload, **envelope):
        requests.append(envelope['request_id'])
        if commit:
            original(name, payload, **envelope)
        raise ClientError('connection_lost', 'Synthetic lost response')
    monkeypatch.setattr(client, 'command', interrupt)
    with pytest.raises(ClientError):
        demo_data.seed_demo(client, client.data_dir, reference_date='2026-10-06')
    def resume(name, payload, **envelope):
        requests.append(envelope['request_id'])
        return original(name, payload, **envelope)
    monkeypatch.setattr(client, 'command', resume)
    result = demo_data.seed_demo(client, client.data_dir)
    assert requests[0] == requests[1]
    assert result['reference_date'] == '2026-10-06'
    assert client.state()['counts'] == {'project': 1, 'note': 1}


@pytest.mark.parametrize('existing', ['record', 'settings', 'archived'])
def test_unmarked_nonempty_space_is_preserved(client, small_recipe, existing):
    if existing == 'settings':
        command(client, 'settings', {'settings': {'page_size': 50}})
    else:
        item = command(client, 'create', {'type': 'project', 'title': 'Existing'})['result']['entity']
        if existing == 'archived':
            command(client, 'archive', {'id': item['id'], 'version': item['version'], 'archived': True})
    before = client.state()
    with pytest.raises(demo_data.DemoError, match='已有'):
        demo_data.seed_demo(client, client.data_dir)
    assert client.state() == before and not (client.data_dir / demo_data.MARKER).exists()


@pytest.mark.parametrize('change', ['date', 'epoch', 'path'])
def test_changed_demo_identity_cannot_continue(client, small_recipe, change):
    demo_data.seed_demo(client, client.data_dir, reference_date='2026-10-06')
    before = client.state()
    path = client.data_dir / demo_data.MARKER
    journal = json.loads(path.read_text('utf-8'))
    if change != 'date':
        journal['epoch' if change == 'epoch' else 'data_dir'] = 'another-space'
        path.write_text(json.dumps(journal), encoding='utf-8')
    with pytest.raises(demo_data.DemoError):
        demo_data.seed_demo(client, client.data_dir, reference_date='2026-10-07' if change == 'date' else None)
    assert client.state() == before


def test_concurrent_builder_cannot_write(client, small_recipe):
    lock = OwnerLock(client.data_dir / 'beta-demo.lock')
    assert lock.acquire()
    try:
        with pytest.raises(demo_data.DemoError, match='另一处'):
            demo_data.seed_demo(client, client.data_dir)
        assert client.state()['revision'] == 0
    finally:
        lock.release()


def test_intervening_write_does_not_rebase_an_interrupted_step(client, small_recipe, monkeypatch):
    original = client.command
    monkeypatch.setattr(client, 'command', lambda *a, **k: (_ for _ in ()).throw(ClientError('connection_lost', 'Synthetic')))
    with pytest.raises(ClientError):
        demo_data.seed_demo(client, client.data_dir)
    monkeypatch.setattr(client, 'command', original)
    command(client, 'create', {'type': 'project', 'title': 'Separate user edit'})
    before = client.state()
    with pytest.raises(demo_data.DemoError, match='其他操作'):
        demo_data.seed_demo(client, client.data_dir)
    assert client.state() == before


def test_changed_original_file_is_not_overwritten(client, monkeypatch):
    folder = client.data_dir / 'demo-inputs'
    folder.mkdir()
    file = folder / 'fixture.txt'
    file.write_text('User modification')
    monkeypatch.setattr(demo_data, '_recipe', lambda b: b.file('fixture.txt', 'Demo content'))
    with pytest.raises(demo_data.DemoError, match='未覆盖'):
        demo_data.seed_demo(client, client.data_dir)
    assert file.read_text() == 'User modification' and client.state()['revision'] == 0


def test_failed_input_file_publication_can_resume(client, monkeypatch):
    def recipe(builder):
        builder.file('fixture.txt', 'Complete synthetic file')
        return {'fixture': True}
    monkeypatch.setattr(demo_data, '_recipe', recipe)
    original = demo_data.os.link
    monkeypatch.setattr(demo_data.os, 'link', lambda *a, **k: (_ for _ in ()).throw(OSError('Synthetic interrupted publication')))
    with pytest.raises(OSError):
        demo_data.seed_demo(client, client.data_dir)
    folder = client.data_dir / 'demo-inputs'
    assert not (folder / 'fixture.txt').exists()
    assert list(folder.iterdir()) == []
    monkeypatch.setattr(demo_data.os, 'link', original)
    demo_data.seed_demo(client, client.data_dir)
    assert (folder / 'fixture.txt').read_text() == 'Complete synthetic file'


def test_failed_io_with_zero_revision_is_not_a_fresh_database(client, small_recipe):
    with pytest.raises(Exception):
        command(client, 'import_asset', {'path': str(client.data_dir / 'missing.txt')})
    assert client.state()['revision'] == 0
    assert client.query('diagnostics')['unfinished_file_operations'] == 1
    with pytest.raises(demo_data.DemoError, match='文件操作'):
        demo_data.seed_demo(client, client.data_dir)
    assert not (client.data_dir / demo_data.MARKER).exists()


def test_lost_last_receipt_can_resume_after_background_revision_change(client, monkeypatch):
    def recipe(builder):
        return builder.run('last', 'create', {'type': 'project', 'title': 'Synthetic last step'})
    monkeypatch.setattr(demo_data, '_recipe', recipe)
    original = client.command
    def lost(name, payload, **envelope):
        original(name, payload, **envelope)
        raise ClientError('connection_lost', 'Synthetic lost final response')
    monkeypatch.setattr(client, 'command', lost)
    with pytest.raises(ClientError):
        demo_data.seed_demo(client, client.data_dir)
    monkeypatch.setattr(client, 'command', original)
    command(client, 'create', {'type': 'note', 'title': 'Synthetic later background record'})
    before = client.state()
    demo_data.seed_demo(client, client.data_dir)
    assert client.state() == before


def test_cli_demo_explicitly_resumes_selected_beta_space(client, small_recipe, monkeypatch, capsys):
    from management import __main__, installation_state, client as client_module
    resumed = []
    monkeypatch.setattr(installation_state, 'resume_after_update', lambda root: resumed.append(root))
    monkeypatch.setattr(client_module, 'Client', lambda root: client)
    monkeypatch.setattr('sys.argv', ['management', '--seed-demo', '--data-dir', str(client.data_dir), '--demo-date', '2026-10-06'])
    assert __main__.main() == 0
    assert resumed == [client.data_dir]
    assert json.loads(capsys.readouterr().out)['reference_date'] == '2026-10-06'
