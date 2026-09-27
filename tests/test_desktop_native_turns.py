"""Native manual-turn reconciliation with synthetic receipts and real business DB."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
import uuid

import pytest

from management import desktop_native_turns as native
from management.core import Core
from management.storage import encode
from test_conversation_progress_v12 import claimed, command


THREAD, TURN, OTHER = [str(uuid.uuid4()) for _ in range(3)]


def receipt(*, ident=TURN, status='completed', completed_at=1800000000):
    return {'id': ident, 'status': status, 'completedAt': completed_at,
            'items': [], 'itemsView': 'notLoaded'}


def candidate(actions=None):
    return {'summary': 'Synthetic manual candidate', 'unknowns': [], 'sources': [],
            'actions': actions or [], '_candidate_validated': True}


class Reader:
    def __init__(self, factory):
        self.factory, self.closed = factory, False
    def __enter__(self):
        if self.factory.unavailable:
            raise ConnectionError('Synthetic unavailable reader')
        return self
    def request(self, method, params):
        self.factory.calls.append((method, copy.deepcopy(params)))
        assert method == 'thread/turns/list'
        assert params['threadId'] == THREAD
        assert params['itemsView'] == 'notLoaded'
        if self.factory.on_read:
            self.factory.on_read()
        return copy.deepcopy(self.factory.pages.get(params.get('cursor'), self.factory.page))
    def close(self):
        self.closed = True


class Readers:
    def __init__(self):
        self.calls, self.instances = [], []
        self.unavailable = False
        self.on_read = None
        self.page = {'data': [receipt()], 'nextCursor': None}
        self.pages = {}
    def __call__(self, workspace, *, timeout):
        assert timeout == 3
        value = Reader(self)
        self.instances.append(value)
        return value


@pytest.fixture
def space(tmp_path, monkeypatch):
    core = Core(tmp_path / 'data')
    command(core, 'settings', {'settings': {'ai': {'enabled': True}}})
    job, publish = claimed(core)
    publish({'phase': 'thread_ready', 'provider_thread_id': THREAD,
             'provider_contract': 'desktop_mcp_v3'})
    publish({'phase': 'waiting_model', 'provider_turn_id': TURN})
    with core.store.connect() as c:
        value = json.loads(job['input'])
        value.update(execution_owner='desktop', desktop_transport='native_ipc_v1')
        c.execute('UPDATE jobs SET input=? WHERE id=?', (encode(value), job['id']))
        job = dict(c.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
    readers = Readers()
    monkeypatch.setattr(native, 'PlainAppServer', readers)
    return SimpleNamespace(core=core, job=job, value=value, readers=readers,
        conversation_id=value['conversation_id'], context_id=value['context_operation_id'])


def rows(space):
    with space.core.store.connect() as c:
        return {
            'job': dict(c.execute('SELECT * FROM jobs WHERE id=?', (space.job['id'],)).fetchone()),
            'operation': dict(c.execute('SELECT * FROM conversation_operations WHERE job_id=?', (space.job['id'],)).fetchone()),
            'conversation': dict(c.execute('SELECT * FROM conversations WHERE id=?', (space.conversation_id,)).fetchone()),
            'progress': dict(c.execute('SELECT * FROM conversation_progress WHERE job_id=?', (space.job['id'],)).fetchone()),
            'context': dict(c.execute('SELECT * FROM context_operations WHERE id=?', (space.context_id,)).fetchone()),
            'job_count': c.execute('SELECT count(*) FROM jobs').fetchone()[0],
            'entity_count': c.execute('SELECT count(*) FROM entities').fetchone()[0],
            'assistant_count': c.execute("SELECT count(*) FROM conversation_messages WHERE job_id=? AND role='assistant'",
                                         (space.job['id'],)).fetchone()[0],
        }


def save_candidate(space, result):
    with space.core.store.connect() as c:
        c.execute('UPDATE conversation_operations SET candidate=? WHERE job_id=?',
                  (encode(result), space.job['id']))


def test_confirmed_exact_turn_without_candidate_settles_and_finishes_once(space):
    native.check_native_turns(space.core, [space.job])
    final = rows(space)
    assert final['job']['status'] == 'failed'
    assert json.loads(final['job']['error'])['code'] == 'discussion_no_candidate'
    assert final['operation']['phase'] == 'reconciled'
    assert final['conversation']['active_job_id'] is None
    assert final['progress']['finished_at']
    assert final['assistant_count'] == 1
    read_count = len(space.readers.calls)
    native.check_native_turns(space.core, [space.job])
    assert rows(space) == final and len(space.readers.calls) == read_count
    assert all(reader.closed for reader in space.readers.instances)


@pytest.mark.parametrize('status', ['completed', 'failed', 'interrupted'])
def test_partial_terminal_status_without_completion_timestamp_remains_running(space, status):
    space.readers.page['data'] = [receipt(status=status, completed_at=None)]
    before = rows(space)
    native.check_native_turns(space.core, [space.job])
    assert rows(space) == before


@pytest.mark.parametrize('value', [None, 0, -1, True, '1800000000'])
def test_unverified_completion_time_cannot_finish_native_job(space, value):
    space.readers.page['data'] = [receipt(completed_at=value)]
    before = rows(space)
    native.check_native_turns(space.core, [space.job])
    assert rows(space) == before


@pytest.mark.parametrize('remote', [
    {'data': [receipt(ident=OTHER)], 'nextCursor': None},
    {'data': [], 'nextCursor': None, 'status': {'type': 'idle'}},
    {'thread': {'status': {'type': 'idle'}}},
    {'data': [receipt(status='inProgress')], 'nextCursor': None},
    {'data': [receipt(), receipt()], 'nextCursor': None},
])
def test_unknown_other_turn_idle_and_duplicate_rows_are_not_completion(space, remote):
    space.readers.page = remote
    before = rows(space)
    native.check_native_turns(space.core, [space.job])
    assert rows(space) == before


def test_unavailable_reader_leaves_job_running_and_cleans_up(space):
    space.readers.unavailable = True
    before = rows(space)
    native.check_native_turns(space.core, [space.job])
    assert rows(space) == before
    assert all(reader.closed for reader in space.readers.instances)


def test_exact_turn_can_be_read_from_a_later_page_without_resuming_it(space):
    space.readers.pages = {
        None: {'data': [receipt(ident=OTHER)], 'nextCursor': 'older'},
        'older': {'data': [receipt()], 'nextCursor': None},
    }
    native.check_native_turns(space.core, [space.job])
    assert rows(space)['job']['status'] == 'failed'
    assert [method for method, _ in space.readers.calls] == ['thread/turns/list'] * 2
    assert space.readers.calls[1][1]['cursor'] == 'older'


def test_candidate_completes_through_shared_service_and_never_applies_actions(space):
    result = candidate([{'command': 'create', 'payload': {'type': 'task', 'title': 'Synthetic'}, 'reason': 'Test'}])
    save_candidate(space, result)
    before = rows(space)
    native.check_native_turns(space.core, [space.job])
    final = rows(space)
    assert final['job']['status'] == 'awaiting_review'
    assert json.loads(final['job']['result']) == result
    assert final['conversation']['current_proposal_job_id'] == space.job['id']
    assert final['conversation']['active_job_id'] is None
    assert final['progress']['finished_at']
    assert final['entity_count'] == before['entity_count']


def test_candidate_received_during_history_read_wins_over_no_candidate_failure(space):
    space.readers.on_read = lambda: save_candidate(space, candidate())
    native.check_native_turns(space.core, [space.job])
    final = rows(space)
    assert final['job']['status'] == 'completed'
    assert json.loads(final['job']['result'])['summary'] == 'Synthetic manual candidate'


@pytest.mark.parametrize('phase', ['checkpointed', 'budget_stop'])
def test_manual_checkpoint_requeues_the_same_job_for_software_continuation(space, phase):
    with space.core.store.connect() as c:
        c.execute('UPDATE context_operations SET phase=?,stage=0,delivered_bytes=12000,tool_calls=128 WHERE id=?',
                  (phase, space.context_id))
    before = rows(space)
    native.check_native_turns(space.core, [space.job])
    final = rows(space)
    assert final['job']['status'] == 'queued'
    value = json.loads(final['job']['input'])
    assert value['execution_owner'] == 'software' and value['context_continuation']
    assert value['desktop_transport'] == 'native_ipc_v1'
    assert value['provider_thread_id'] == THREAD
    assert value['previous_operation']['provider_turn_id'] == TURN
    assert final['job']['id'] == before['job']['id']
    assert final['job']['generation'] == before['job']['generation']
    assert final['job_count'] == before['job_count'] == 1
    assert final['conversation']['active_job_id'] == space.job['id']
    assert final['context']['stage'] == 0 and final['context']['phase'] == 'checkpointed'
    assert final['context']['delivered_bytes'] == 12000 and final['context']['tool_calls'] == 128
    assert final['progress']['finished_at'] is None


@pytest.mark.parametrize('race', ['generation', 'epoch', 'active_job', 'turn', 'thread', 'owner'])
def test_transaction_rechecks_identity_after_provider_read(space, race):
    def change():
        with space.core.store.connect() as c:
            if race == 'generation':
                c.execute('UPDATE jobs SET generation=generation+1 WHERE id=?', (space.job['id'],))
            elif race == 'epoch':
                space.core.store.set_meta(c, 'epoch', str(uuid.uuid4()))
            elif race == 'active_job':
                c.execute('UPDATE conversations SET active_job_id=NULL WHERE id=?', (space.conversation_id,))
            elif race == 'turn':
                c.execute('UPDATE conversation_operations SET provider_turn_id=? WHERE job_id=?', (OTHER, space.job['id']))
            elif race == 'thread':
                c.execute('UPDATE conversation_operations SET provider_thread_id=? WHERE job_id=?', (OTHER, space.job['id']))
            else:
                value = {**space.value, 'execution_owner': 'software'}
                c.execute('UPDATE jobs SET input=? WHERE id=?', (encode(value), space.job['id']))
    space.readers.on_read = change
    native.check_native_turns(space.core, [space.job])
    final = rows(space)
    assert final['job']['status'] == 'running'
    assert final['job']['error'] is None and final['assistant_count'] == 0
    assert final['progress']['finished_at'] is None


def test_missing_trusted_turn_metadata_never_guesses_latest_turn(space):
    with space.core.store.connect() as c:
        c.execute('UPDATE conversation_operations SET provider_turn_id=NULL WHERE job_id=?', (space.job['id'],))
    native.check_native_turns(space.core, [space.job])
    assert space.readers.calls == [] and space.readers.instances == []
    assert rows(space)['job']['status'] == 'running'


def test_old_transport_job_is_not_accidentally_reconciled_by_new_observer(space):
    with space.core.store.connect() as c:
        value = {**space.value, 'desktop_transport': 'legacy'}
        c.execute('UPDATE jobs SET input=? WHERE id=?', (encode(value), space.job['id']))
    native.check_native_turns(space.core, [space.job])
    assert space.readers.instances == [] and rows(space)['job']['status'] == 'running'
