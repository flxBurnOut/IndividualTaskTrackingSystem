"""Explicitly collected native binding and cross-job dispatch race regressions."""
import json

import pytest

from management import conversations, desktop_dispatch as journal, native_desktop as native
from management import session_coordinator as coordinator
from management.ai import AIError
from management.schemas import BusinessError
from management.storage import encode
from test_conversation_progress_v12 import claimed, command
from test_native_desktop import setup, THREAD, TURN, Reader


REPLACEMENT = '00000000-0000-4000-8000-000000000030'


def rebind(core, conversation_id):
    with core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE conversation_bindings SET state='historical' WHERE conversation_id=?", (conversation_id,))
        c.execute('''INSERT INTO conversation_bindings
            SELECT conversation_id,?,epoch,project_path,contract,'active','synthetic-race',created_at,updated_at
            FROM conversation_bindings WHERE conversation_id=? AND provider_thread_id=?''',
            (REPLACEMENT, conversation_id, THREAD))
        c.execute('UPDATE conversations SET provider_thread_id=? WHERE id=?', (REPLACEMENT, conversation_id))
        c.commit()


def test_rebinding_between_native_lookup_and_begin_transaction_rejects_old_caller(setup, monkeypatch):
    core, job, progress, gateway, seeds, run, value = setup
    progress({'phase': 'thread_ready', 'provider_thread_id': THREAD, 'provider_contract': native.CONTRACT})
    reply = {'summary': 'Synthetic finished turn', 'unknowns': [], 'sources': [], 'actions': [],
             'provider': {'thread_id': THREAD, 'project_path': str(core.root / 'Codex事务助手')}}
    with core.store.connect() as c:
        c.execute("UPDATE jobs SET status='completed',result=? WHERE id=?", (encode(reply), job['id']))
        conversations.complete_job(core, c, job, reply)
    original = coordinator.native_binding
    def race(owner, thread):
        bound = original(owner, thread)
        rebind(owner, value['conversation_id'])
        return bound
    monkeypatch.setattr(coordinator, 'native_binding', race)
    with pytest.raises(BusinessError) as error:
        coordinator.handle_native(core, 'begin', {'provider_thread_id': THREAD, 'provider_turn_id': TURN,
            'text': 'New native turn', 'request_id': 'late-old-caller'})
    assert error.value.code == 'conversation_binding_stale'
    with core.store.connect() as c:
        assert c.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM conversation_native_requests').fetchone()[0] == 0
        assert c.execute('SELECT provider_thread_id FROM conversations').fetchone()[0] == REPLACEMENT


def test_rebinding_between_native_lookup_and_submit_rejects_late_candidate(setup, monkeypatch):
    core, job, progress, gateway, seeds, run, value = setup
    progress({'phase': 'thread_ready', 'provider_thread_id': THREAD, 'provider_contract': native.CONTRACT})
    begun = coordinator.handle_native(core, 'begin', {'provider_thread_id': THREAD, 'provider_turn_id': TURN,
        'text': value['prompt'], 'request_id': 'first-user-turn'})
    original = coordinator.native_binding
    def race(owner, thread):
        bound = original(owner, thread)
        command(owner, 'cancel_job', {'id': job['id']})
        rebind(owner, value['conversation_id'])
        return bound
    monkeypatch.setattr(coordinator, 'native_binding', race)
    with pytest.raises(BusinessError) as error:
        coordinator.handle_native(core, 'submit', {'provider_thread_id': THREAD, 'provider_turn_id': TURN,
            'job_id': begun['job_id'], 'generation': begun['generation'],
            'proposal': {'summary': 'Late result', 'unknowns': [], 'sources': [], 'actions': []}})
    assert error.value.code == 'conversation_binding_stale'
    with core.store.connect() as c:
        assert coordinator.get_candidate(c, job['id']) is None


@pytest.mark.parametrize('state', ['sending', 'uncertain'])
@pytest.mark.parametrize('phase', ['sending', 'thread_ready'])
def test_previous_job_unresolved_journal_blocks_new_dispatch_even_if_phase_was_overwritten(setup, state, phase):
    core, old_job, old_progress, gateway, seeds, run, old_value = setup
    old_progress({'phase': 'thread_ready', 'provider_thread_id': THREAD, 'provider_contract': native.CONTRACT})
    old_progress({'phase': phase})
    with core.store.connect() as c:
        intent = journal.prepare(c, epoch=old_job['epoch'], job_id=old_job['id'], step='model-stage-0',
                                 method=native.START, payload={'synthetic_prior_message': True})
        claim = journal.claim(c, epoch=old_job['epoch'], operation_id=intent['operation_id'])
        if state == 'uncertain':
            journal.mark_uncertain(c, epoch=old_job['epoch'], operation_id=intent['operation_id'],
                                   claim_id=claim['claim_id'], provider_thread_id=THREAD)
        c.execute("UPDATE jobs SET status='failed' WHERE id=?", (old_job['id'],))
        conversations.update_job(core, c, old_job, 'failed', {'code': 'AI_DELIVERY_UNKNOWN', 'message': 'Synthetic unknown'})
    job, progress = claimed(core)
    value = json.loads(job['input'])
    assert job['id'] != old_job['id'] and value['provider_thread_id'] == THREAD
    business_tables = ('entities', 'links', 'receipts', 'changes')
    with core.store.connect() as c:
        before = {table: [tuple(row) for row in c.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                  for table in business_tables}
        # generate publishes thread_ready before reconciliation; its binding
        # freshness timestamp may change while ownership must stay identical.
        bindings_before = [{key: row[key] for key in row.keys() if key != 'updated_at'}
                           for row in c.execute('SELECT * FROM conversation_bindings ORDER BY rowid')]
        before_state = core.store.state(c)
    with pytest.raises(AIError) as error:
        native.generate(core, job, value, core.query('settings')['settings'], progress.cancel,
                        progress.stop, progress, seed=lambda *a, **k: pytest.fail('No replacement thread'),
                        reader_factory=Reader, wait=lambda _: None)
    assert error.value.code == 'AI_DELIVERY_UNKNOWN'
    assert [method for method, _ in gateway.calls] == ['thread-follower-load-complete-history']
    assert not any(method == native.START for method, _ in gateway.calls) and not seeds
    with core.store.connect() as c:
        assert {table: [tuple(row) for row in c.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in business_tables} == before
        assert core.store.state(c) == before_state
        assert [{key: row[key] for key in row.keys() if key != 'updated_at'}
                for row in c.execute('SELECT * FROM conversation_bindings ORDER BY rowid')] == bindings_before
        assert coordinator.get_candidate(c, job['id']) is None
        assert c.execute('SELECT count(*) FROM desktop_dispatches WHERE job_id=?', (job['id'],)).fetchone()[0] == 0
        assert c.execute('SELECT state FROM desktop_dispatches WHERE operation_id=?', (intent['operation_id'],)).fetchone()[0] == state
