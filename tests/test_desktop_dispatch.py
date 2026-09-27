"""Dispatch durability/fencing across real SQLite connections and Store owners."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest

from management import desktop_dispatch as dispatch
from management.schemas import BusinessError
from management.storage import Store, new_id, now


@pytest.fixture
def space(tmp_path):
    store = Store(tmp_path / 'data')
    with store.connect() as c:
        dispatch.initialize(c)
        epoch = store.meta(c, 'epoch')
        job = new_id()
        c.execute('''INSERT INTO jobs(id,kind,status,input,epoch,snapshot_revision,created_at,updated_at)
            VALUES (?,'ai','queued','{}',?,0,?,?)''', (job, epoch, now(), now()))
    return store, epoch, job


def prepare(c, space, **overrides):
    _, epoch, job = space
    return dispatch.prepare(c, **{'epoch': epoch, 'job_id': job, 'step': 'turn:1',
        'method': 'turn/start', 'payload': {'threadId': 'thread-one', 'input': []}, **overrides})


def key(record):
    return {name: record[name] for name in ('epoch', 'operation_id')}


def owned(record):
    return {**key(record), 'claim_id': record['claim_id']}


def test_canonical_idempotent_prepare_does_not_store_content_or_credentials(space):
    store, _, _ = space
    with store.connect() as c:
        before = store.state(c)
        first = prepare(c, space, payload={'text': 'private business content', 'auth': 'Bearer fake-test-secret'})
        second = prepare(c, space, payload={'auth': 'Bearer fake-test-secret', 'text': 'private business content'})
        assert first == second and first['state'] == 'prepared'
        assert len(first['payload_sha256']) == 64
        stored = json.dumps(dict(c.execute('SELECT * FROM desktop_dispatches').fetchone()))
        assert 'private business content' not in stored and 'fake-test-secret' not in stored
        assert c.execute('SELECT count(*) FROM desktop_dispatches').fetchone()[0] == 1
        assert store.state(c) == before
        assert c.execute('SELECT count(*) FROM changes').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM receipts').fetchone()[0] == 0


@pytest.mark.parametrize('change', [{'method': 'thread/start'}, {'payload': {'input': ['changed']}}])
def test_same_key_changed_content_is_rejected(space, change):
    store, _, _ = space
    with store.connect() as c:
        first = prepare(c, space)
        with pytest.raises(BusinessError, match='内容已变化') as error:
            prepare(c, space, **change)
        assert error.value.code == 'dispatch_content_conflict'
        assert dispatch.get(c, **key(first)) == first


def test_claim_is_committed_before_return_and_survives_plain_sqlite_reopen(space):
    store, _, _ = space
    with store.connect() as c:
        first = prepare(c, space)
        claimed = dispatch.claim(c, **key(first))
        assert claimed['state'] == 'sending' and claimed['attempt'] == 1
        assert not c.in_transaction
        with sqlite3.connect(store.path) as other:
            assert other.execute('SELECT state FROM desktop_dispatches').fetchone()[0] == 'sending'
            assert dispatch.claim(other, **key(first)) is None
    reopened = Store(store.root)
    with reopened.connect() as c:
        dispatch.initialize(c)
        assert dispatch.claim(c, **key(first)) is None
        assert dispatch.get(c, **key(first))['claim_id'] == claimed['claim_id']


def test_multiple_store_instances_only_one_claim_wins(space):
    store, _, _ = space
    with store.connect() as c:
        first = prepare(c, space)
    stores = [Store(store.root) for _ in range(6)]
    barrier = threading.Barrier(len(stores))
    def claim(owner):
        with owner.connect() as c:
            barrier.wait(timeout=5)
            return dispatch.claim(c, **key(first))
    with ThreadPoolExecutor(max_workers=len(stores)) as executor:
        results = list(executor.map(claim, stores))
    assert sum(value is not None for value in results) == 1
    with store.connect() as c:
        assert dispatch.get(c, **key(first))['attempt'] == 1


def test_concurrent_prepare_creates_one_operation_and_steps_are_separate(space):
    store, _, _ = space
    barrier = threading.Barrier(4)
    def create(_):
        with store.connect() as c:
            barrier.wait(timeout=5)
            return prepare(c, space)['operation_id']
    with ThreadPoolExecutor(max_workers=4) as executor:
        operations = list(executor.map(create, range(4)))
    assert len(set(operations)) == 1
    with store.connect() as c:
        second = prepare(c, space, step='turn:2')
        assert second['operation_id'] != operations[0]
        assert c.execute('SELECT count(*) FROM desktop_dispatches').fetchone()[0] == 2


def test_failed_commit_does_not_grant_dispatch_permission(space):
    store, _, _ = space
    with store.connect() as c:
        record = prepare(c, space)
        class FailingCommit:
            def __getattr__(self, name):
                return getattr(c, name)
            def commit(self):
                raise sqlite3.OperationalError('isolated commit failure')
        with pytest.raises(sqlite3.OperationalError):
            dispatch.claim(FailingCommit(), **key(record))
        assert not c.in_transaction
        assert dispatch.get(c, **key(record))['state'] == 'prepared'
        assert dispatch.claim(c, **key(record))['attempt'] == 1


def test_uncertain_never_reclaims_on_reprepare_or_restart_but_accepts_late_ack(space):
    store, _, _ = space
    with store.connect() as c:
        claimed = dispatch.claim(c, **key(prepare(c, space)))
        uncertain = dispatch.mark_uncertain(c, **owned(claimed), reason='connection_lost')
        assert uncertain['state'] == 'uncertain'
        assert prepare(c, space)['state'] == 'uncertain'
    with Store(store.root).connect() as c:
        assert dispatch.claim(c, **key(claimed)) is None
        acknowledged = dispatch.acknowledge(c, **owned(claimed),
            provider_thread_id='thread-one', provider_turn_id='turn-one')
        assert acknowledged['state'] == 'acknowledged'
        assert dispatch.claim(c, **key(claimed)) is None
        assert prepare(c, space)['state'] == 'acknowledged'
        assert dispatch.acknowledge(c, **owned(claimed))['provider_turn_id'] == 'turn-one'
        with pytest.raises(BusinessError) as error:
            dispatch.mark_uncertain(c, **owned(claimed), reason='unknown')
        assert error.value.code == 'dispatch_state_conflict'


def test_only_proven_not_sent_resets_and_old_attempt_cannot_acknowledge(space):
    store, _, _ = space
    with store.connect() as c:
        first = dispatch.claim(c, **key(prepare(c, space)))
        dispatch.mark_uncertain(c, **owned(first))
        with pytest.raises(BusinessError) as error:
            dispatch.confirm_not_sent(c, **owned(first), evidence='ack_timeout')
        assert error.value.code == 'dispatch_not_sent_unproven'
        assert dispatch.claim(c, **key(first)) is None
        reset = dispatch.confirm_not_sent(c, **owned(first), evidence='request_not_written')
        assert reset['state'] == 'prepared' and reset['claim_id'] is None
        second = dispatch.claim(c, **key(first))
        assert second['attempt'] == 2 and first['claim_id'] != second['claim_id']
        for function, extra in ((dispatch.acknowledge, {}),
                                (dispatch.mark_uncertain, {}),
                                (dispatch.confirm_not_sent, {'evidence': 'request_not_written'})):
            with pytest.raises(BusinessError) as error:
                function(c, **owned(first), **extra)
            assert error.value.code == 'dispatch_claim_mismatch'
        assert dispatch.get(c, **key(second))['state'] == 'sending'


def test_known_remote_identifiers_survive_uncertainty_and_prevent_not_sent_reset(space):
    store, _, _ = space
    with store.connect() as c:
        record = dispatch.claim(c, **key(prepare(c, space)))
        dispatch.mark_uncertain(c, **owned(record), provider_thread_id='thread-one')
        with pytest.raises(BusinessError) as error:
            dispatch.confirm_not_sent(c, **owned(record), evidence='provider_confirmed_not_received')
        assert error.value.code == 'dispatch_not_sent_unproven'
        with pytest.raises(BusinessError) as error:
            dispatch.acknowledge(c, **owned(record), provider_thread_id='different')
        assert error.value.code == 'dispatch_result_conflict'
        done = dispatch.acknowledge(c, **owned(record), provider_turn_id='turn-one')
        assert (done['provider_thread_id'], done['provider_turn_id']) == ('thread-one', 'turn-one')


def test_epoch_change_blocks_all_old_operations_and_job_cannot_cross_epoch(space):
    store, old_epoch, _ = space
    with store.connect() as c:
        record = dispatch.claim(c, **key(prepare(c, space)))
        epoch = new_id()
        store.set_meta(c, 'epoch', epoch)
        calls = [lambda: prepare(c, space), lambda: prepare(c, space, epoch=epoch),
            lambda: dispatch.get(c, **key(record)), lambda: dispatch.claim(c, **key(record)),
            lambda: dispatch.get(c, epoch=epoch, operation_id=record['operation_id']),
            lambda: dispatch.acknowledge(c, **owned(record)),
            lambda: dispatch.mark_uncertain(c, **owned(record)),
            lambda: dispatch.confirm_not_sent(c, **owned(record), evidence='request_not_written')]
        for call in calls:
            with pytest.raises(BusinessError) as error:
                call()
            assert error.value.code == 'dispatch_epoch_mismatch'
        assert c.execute('SELECT epoch,state FROM desktop_dispatches').fetchone()[:] == (old_epoch, 'sending')


def test_existing_transaction_is_rejected_without_committing_caller_changes(space):
    store, _, _ = space
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        store.set_meta(c, 'revision', 99)
        with pytest.raises(BusinessError) as error:
            prepare(c, space)
        assert error.value.code == 'dispatch_transaction_active'
        assert c.in_transaction
        c.rollback()
        assert store.meta(c, 'revision') == 0
        assert c.execute('SELECT count(*) FROM desktop_dispatches').fetchone()[0] == 0


@pytest.mark.parametrize('payload', [{'value': float('nan')}, {1: 'numeric key'},
                                     {'value': b'bytes'}, {'value': ('tuple',)}, []])
def test_invalid_json_payload_never_creates_intent(space, payload):
    store, _, _ = space
    with store.connect() as c:
        with pytest.raises(BusinessError) as error:
            prepare(c, space, payload=payload)
        assert error.value.code == 'dispatch_invalid_payload'
        assert c.execute('SELECT count(*) FROM desktop_dispatches').fetchone()[0] == 0


def test_freeform_diagnostic_and_provider_response_are_not_accepted(space):
    store, _, _ = space
    with store.connect() as c:
        record = dispatch.claim(c, **key(prepare(c, space)))
        with pytest.raises(BusinessError):
            dispatch.mark_uncertain(c, **owned(record), reason='Bearer private-secret')
        with pytest.raises(BusinessError):
            dispatch.acknowledge(c, **owned(record), provider_turn_id='private business response text')
        assert dispatch.get(c, **key(record))['state'] == 'sending'
        assert 'private-secret' not in json.dumps(dict(c.execute('SELECT * FROM desktop_dispatches').fetchone()))
