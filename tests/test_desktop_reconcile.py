"""Persistent journal with synthetic read-only desktop and official history APIs."""
import copy
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from management import desktop_dispatch as journal, desktop_reconcile as reconcile
from management.ai import AIError
from management.native_desktop import START
from management.storage import Store, new_id, now


THREAD = '00000000-0000-4000-8000-000000000010'
TURN = '00000000-0000-4000-8000-000000000020'


class Gateway:
    def __init__(self, state):
        self.state = state
        self.history_state = copy.deepcopy(state)
        self.stream = SimpleNamespace(revision=1, owner_client_id='trusted-owner')
        self.calls = []
        self.loaded = False
        self.advance_revision = True
        self.reply = {'resultType': 'success', 'result': {'revision': 3}}
    def snapshot(self, thread, timeout=1):
        assert thread == THREAD
        self.calls.append(('snapshot', timeout))
        if self.loaded:
            if self.advance_revision:self.stream.revision=3
            return self.history_state
        return self.state
    def subscribe(self, thread):
        assert thread == THREAD
        self.calls.append(('subscribe', thread))
        return self.stream
    def request_owner(self, thread, method, params, version, timeout):
        assert thread == THREAD and method == 'thread-follower-load-complete-history'
        assert params == {'conversationId': THREAD} and version == 1 and timeout == 10
        self.calls.append((method, params))
        self.loaded=True
        return self.reply


class Reader:
    def __init__(self, turn):
        self.turn=turn;self.calls=[];self.closed=False
    def __enter__(self):return self
    def request(self, method, params):
        assert method == 'thread/turns/list' and params['threadId'] == THREAD
        self.calls.append((method, params))
        return {'data': [self.turn] if self.turn else [], 'nextCursor': None}
    def close(self):self.closed=True


@pytest.fixture
def pending(tmp_path, monkeypatch):
    store=Store(tmp_path/'data');core=SimpleNamespace(store=store,root=store.root)
    with store.connect() as c:
        journal.initialize(c)
        epoch=store.meta(c,'epoch');old,new=new_id(),new_id()
        for identifier,status in ((old,'failed'),(new,'running')):
            c.execute('''INSERT INTO jobs(id,kind,status,input,epoch,snapshot_revision,created_at,updated_at)
                VALUES (?,'ai',?,'{}',?,0,?,?)''',(identifier,status,epoch,now(),now()))
        c.execute('''INSERT INTO conversation_operations(job_id,conversation_id,epoch,phase,provider_thread_id,updated_at)
            VALUES (?,'synthetic-conversation',?,'thread_ready',?,?)''',(old,epoch,THREAD,now()))
        record=journal.prepare(c,epoch=epoch,job_id=old,step='model-stage-2',method=START,payload={'test':'private-not-retained'})
        claim=journal.claim(c,epoch=epoch,operation_id=record['operation_id'])
        record=journal.mark_uncertain(c,epoch=epoch,operation_id=record['operation_id'],claim_id=claim['claim_id'])
    message=str(uuid.uuid5(uuid.NAMESPACE_URL,f'personal-management/{epoch}/{old}/stage/2'))
    turn={'turnId':TURN,'status':'completed','params':{'clientUserMessageId':message}}
    gateway=Gateway({'id':THREAD,'turns':[turn]})
    reader=Reader({'id':TURN,'status':'completed','completedAt':12345})
    clock=[0.0]
    monkeypatch.setattr(reconcile,'_clock',lambda:clock[0])
    monkeypatch.setattr(reconcile,'_pause',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    job={'id':new,'epoch':epoch}
    def run():return reconcile.reconcile_pending(core,job,THREAD,gateway,Path(store.root),lambda *a,**k:reader)
    def saved():
        with store.connect() as c:return dict(c.execute('SELECT * FROM desktop_dispatches WHERE operation_id=?',(record['operation_id'],)).fetchone())
    return core,job,record,gateway,reader,run,saved


def test_unknown_snapshot_match_and_terminal_ack_without_business_changes(pending):
    core,job,record,gateway,reader,run,saved=pending
    with core.store.connect() as c:
        jobs=[tuple(row) for row in c.execute('SELECT * FROM jobs ORDER BY id')]
        state=core.store.state(c)
    assert run() is None
    assert saved()['state']=='acknowledged' and saved()['provider_turn_id']==TURN
    assert reader.closed and not any(call[0].startswith('thread-follower') for call in gateway.calls)
    with core.store.connect() as c:
        assert jobs==[tuple(row) for row in c.execute('SELECT * FROM jobs ORDER BY id')]
        assert core.store.state(c)==state
        assert c.execute('SELECT count(*) FROM receipts').fetchone()[0]==0


def test_missing_snapshot_loads_history_and_waits_for_its_revision(pending):
    core,job,record,gateway,reader,run,saved=pending
    gateway.state={'id':THREAD,'threadRuntimeStatus':{'type':'idle'},'turns':[]}
    assert run() is None
    assert saved()['state']=='acknowledged'
    assert sum(call[0]=='thread-follower-load-complete-history' for call in gateway.calls)==1
    assert gateway.stream.revision==3


def test_missing_turn_is_not_proof_of_non_delivery_even_when_idle(pending):
    core,job,record,gateway,reader,run,saved=pending
    gateway.state=gateway.history_state={'id':THREAD,'threadRuntimeStatus':{'type':'idle'},'turns':[]}
    with pytest.raises(AIError) as error:run()
    assert error.value.code=='AI_DELIVERY_UNKNOWN'
    assert saved()['state']=='uncertain' and saved()['provider_turn_id'] is None
    assert reader.calls==[]


def test_active_match_retains_turn_identity_and_remains_busy_on_repeated_calls(pending):
    core,job,record,gateway,reader,run,saved=pending
    gateway.state['turns'][0]['status']='inProgress'
    reader.turn={'id':TURN,'status':'failed','completedAt':None}
    for _ in range(2):
        with pytest.raises(AIError) as error:run()
        assert error.value.code=='AI_CONVERSATION_BUSY'
        assert saved()['state']=='uncertain' and saved()['provider_turn_id']==TURN
    assert not any(call[0]=='thread-follower-load-complete-history' for call in gateway.calls)


def test_acknowledged_terminal_is_idempotent_and_does_not_read_or_send_again(pending):
    core,job,record,gateway,reader,run,saved=pending
    run();count=len(gateway.calls);reads=len(reader.calls)
    run()
    assert len(gateway.calls)==count and len(reader.calls)==reads


@pytest.mark.parametrize('kind',['protocol_error','missing_revision','stale_revision'])
def test_load_ack_without_valid_revised_snapshot_does_not_reconcile(pending,kind):
    core,job,record,gateway,reader,run,saved=pending
    gateway.state={'id':THREAD,'turns':[]}
    if kind=='protocol_error':gateway.reply={'resultType':'error','error':'synthetic'}
    elif kind=='missing_revision':gateway.reply={'resultType':'success','result':{}}
    else:gateway.advance_revision=False
    with pytest.raises(AIError) as error:run()
    assert error.value.code=='AI_DELIVERY_UNKNOWN'
    assert saved()['state']=='uncertain' and saved()['provider_turn_id'] is None


def test_ambiguous_client_message_match_is_never_guessed(pending):
    core,job,record,gateway,reader,run,saved=pending
    duplicate=copy.deepcopy(gateway.state['turns'][0]);duplicate['turnId']=str(uuid.uuid4())
    gateway.state['turns'].append(duplicate);gateway.history_state=copy.deepcopy(gateway.state)
    with pytest.raises(AIError) as error:run()
    assert error.value.code=='AI_DELIVERY_UNKNOWN' and saved()['provider_turn_id'] is None


def test_unfinished_rollout_failed_status_without_completed_at_is_not_terminal(pending):
    core,job,record,gateway,reader,run,saved=pending
    gateway.state['turns'][0].pop('status')
    reader.turn={'id':TURN,'status':'failed','completedAt':None}
    with pytest.raises(AIError) as error:run()
    assert error.value.code=='AI_DELIVERY_UNKNOWN' and saved()['state']=='uncertain'


def test_exact_persisted_terminal_is_sufficient_when_stream_has_no_terminal_status(pending):
    core,job,record,gateway,reader,run,saved=pending
    gateway.state['turns'][0].pop('status')
    assert run() is None and saved()['state']=='acknowledged'


def test_trusted_terminal_snapshot_survives_temporary_official_reader_failure(pending, monkeypatch):
    core,job,record,gateway,reader,run,saved=pending
    def unavailable(*args, **kwargs):raise ConnectionError('synthetic reader unavailable')
    monkeypatch.setattr(reader,'request',unavailable)
    assert run() is None and saved()['state']=='acknowledged'


def test_other_turn_terminal_receipt_cannot_complete_this_dispatch(pending):
    core,job,record,gateway,reader,run,saved=pending
    gateway.state['turns'][0].pop('status')
    reader.turn={'id':str(uuid.uuid4()),'status':'completed','completedAt':12345}
    with pytest.raises(AIError) as error:run()
    assert error.value.code=='AI_DELIVERY_UNKNOWN' and saved()['state']=='uncertain'


def test_malformed_correlated_identity_remains_unknown(pending):
    core,job,record,gateway,reader,run,saved=pending
    gateway.state['turns'][0]['turnId']='not-a-provider-uuid'
    with pytest.raises(AIError) as error:run()
    assert error.value.code=='AI_DELIVERY_UNKNOWN' and saved()['provider_turn_id'] is None


@pytest.mark.parametrize('outside',['other_epoch','other_thread','other_method','current_job','prepared','acknowledged'])
def test_outside_requested_scope_is_never_reconciled(pending,outside):
    core,job,record,gateway,reader,run,saved=pending
    with core.store.connect() as c:
        if outside=='other_epoch':c.execute('UPDATE desktop_dispatches SET epoch=?',(new_id(),))
        elif outside=='other_thread':c.execute('UPDATE conversation_operations SET provider_thread_id=?',(str(uuid.uuid4()),))
        elif outside=='other_method':c.execute("UPDATE desktop_dispatches SET method='thread/start'")
        elif outside=='current_job':job['id']=record['job_id']
        else:c.execute('UPDATE desktop_dispatches SET state=?',(outside,))
    assert run() is None and not gateway.calls and not reader.calls


def test_invalid_stage_never_guesses_correlation(pending):
    core,job,record,gateway,reader,run,saved=pending
    with core.store.connect() as c:c.execute("UPDATE desktop_dispatches SET step='model-stage-not-a-number'")
    with pytest.raises(AIError) as error:run()
    assert error.value.code=='AI_DELIVERY_UNKNOWN' and saved()['provider_turn_id'] is None
