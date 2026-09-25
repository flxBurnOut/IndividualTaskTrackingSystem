"""Delivery, native continuation, candidate receipt and concurrency regressions."""
import json
import threading
import uuid
import pytest
from management import conversations,conversation_progress
from management.core import Core
from management.session_coordinator import handle,get_candidate,recover_candidates,previous
from management.schemas import BusinessError
from test_conversation_progress_v12 import command,claimed
from test_codex_desktop_shim_v12 import relay

@pytest.fixture
def core(tmp_path):
    value=Core(tmp_path/'data')
    command(value,'settings',{'settings':{'ai':{'enabled':True}}})
    return value

def begin(core,job,text=None,request_id=None):
    data=json.loads(job['input'])
    return handle(core,'begin',{'conversation_id':data['conversation_id'],'epoch':job['epoch'],
        'text':text or data['prompt'],'request_id':request_id or str(uuid.uuid4())})

def proposal(summary='Synthetic result',actions=None):
    return {'summary':summary,'unknowns':[],'sources':[],'actions':actions or []}

def submit(core,job,value,reply=None):
    return handle(core,'submit',{'conversation_id':json.loads(job['input'])['conversation_id'],'epoch':job['epoch'],
        'job_id':value['job_id'],'generation':value['generation'],'proposal':reply or proposal()})

def test_same_gui_turn_reuses_job_and_persists_before_ack(core):
    job,publish=claimed(core)
    publish({'phase':'thread_ready','provider_thread_id':'thread1','provider_contract':'desktop_mcp_v2'})
    publish({'phase':'waiting_model','provider_turn_id':'turn1'})
    first=begin(core,job,request_id='one')
    second=begin(core,job,request_id='one')
    assert first['job_id']==second['job_id']==job['id']
    submit(core,job,first)
    with core.store.connect() as c:
        assert get_candidate(c,job['id'])['summary']=='Synthetic result'
        assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
        recover_candidates(core,c)
    view=core.query('conversation',scope={'kind':'general'})
    assert view['conversation']['provider_thread_id']=='thread1'
    assert view['messages'][-1]['text']=='Synthetic result'
    assert view['active_progress'] is None

def test_native_desktop_followup_returns_to_same_conversation_without_model_dispatch(core):
    job,publish=claimed(core)
    publish({'phase':'thread_ready','provider_thread_id':'thread1','provider_contract':'desktop_mcp_v2'})
    first=begin(core,job);submit(core,job,first)
    with core.store.connect() as c:recover_candidates(core,c)
    second=begin(core,job,'New native desktop message',request_id='native-2')
    assert second['job_id']!=first['job_id']
    with core.store.connect() as c:
        native=core._job(c,second['job_id'])
        assert native['status']=='running' and json.loads(native['input'])['execution_owner']=='desktop'
    submit(core,native,second,proposal('Native reply'))
    view=core.query('conversation',scope={'kind':'general'})
    assert view['conversation']['provider_thread_id']=='thread1'
    assert [m['text'] for m in view['messages']][-2:]==['New native desktop message','Native reply']
    with pytest.raises(BusinessError) as e:begin(core,job,'New native desktop message',request_id='native-2')
    assert e.value.code=='discussion_turn_finished'

def test_cancel_and_other_conversation_cannot_receive_late_candidate(core):
    job,publish=claimed(core)
    publish({'phase':'thread_ready','provider_thread_id':'thread1','provider_contract':'desktop_mcp_v2'})
    value=begin(core,job)
    command(core,'cancel_job',{'id':job['id']})
    with pytest.raises(BusinessError) as e:submit(core,job,value)
    assert e.value.code=='stale_proposal'
    with core.store.connect() as c:assert get_candidate(c,job['id']) is None

def test_unknown_creation_blocks_duplicate_until_identity_is_reconciled(core):
    job,publish=claimed(core)
    publish({'phase':'creating_thread'})
    with core.store.connect() as c:
        c.execute("UPDATE jobs SET status='failed' WHERE id=?",(job['id'],))
        conversations.update_job(core,c,job,'failed',{'message':'connection interrupted'})
    with pytest.raises(BusinessError) as e:
        command(core,'send_message',{'scope':{'kind':'general'},'text':'Retry'})
    assert e.value.code=='conversation_delivery_unknown'
    with core.store.connect() as c:assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==1

def test_unacknowledged_start_owner_is_not_overwritten_before_response(relay):
    value,engine,sent,_,desktop,manager=relay
    manager['initialized']=True
    value.accept(manager,{'id':1,'method':'turn/start','params':{'threadId':'t'}})
    mapped=engine.sent[-1]['id']
    value.receive_engine({'method':'turn/started','params':{'threadId':'t','turn':{'id':'first'}}})
    value.accept(desktop,{'id':2,'method':'turn/start','params':{'threadId':'t'}})
    assert len(engine.sent)==1 and sent[-1][1]['error']['code']==-32002
    value.receive_engine({'id':3,'method':'item/tool/call','params':{'threadId':'t','turnId':'first'}})
    assert sent[-1][0]=='management'
    value.receive_engine({'id':mapped,'result':{'turn':{'id':'first'}}})
    value.accept(desktop,{'id':4,'method':'turn/start','params':{'threadId':'t'}})
    assert len(engine.sent)==2

def test_native_finished_without_candidate_does_not_stay_busy(core):
    from management.session_coordinator import check_native_turns
    job,publish=claimed(core)
    publish({'phase':'thread_ready','provider_thread_id':'thread1','provider_contract':'desktop_mcp_v2'})
    first=begin(core,job);submit(core,job,first)
    with core.store.connect() as c:recover_candidates(core,c)
    native=begin(core,job,'A native turn with no candidate',request_id='missing')
    class RPC:
        def __init__(self,*args):pass
        def request(self,method,params):
            return {'thread':{'id':'thread1','status':{'type':'idle'}}} if method=='thread/read' else {}
        def send(self,value):pass
        def close(self):pass
    check_native_turns(core,RPC)
    assert core.query('conversation',scope={'kind':'general'})['conversation']['active_job_id'] is None
    with core.store.connect() as c:
        assert core._job(c,native['job_id'])['status']=='failed'

def test_mcp_tool_schemas_are_serializable_and_expose_only_candidate_workflow():
    import asyncio
    from management.discussion_mcp import create_server
    server=create_server('synthetic','conversation','epoch',client=object())
    tools=asyncio.run(server.list_tools())
    from management.context_mcp import TOOL_NAMES
    assert {t.name for t in tools}=={'begin_discussion','query_business','submit_candidate'}|TOOL_NAMES
    assert all(t.output_schema for t in tools if t.name not in {'next_context_step','read_material'})

def test_native_different_request_cannot_adopt_an_active_gui_turn(core):
    job,publish=claimed(core)
    publish({'phase':'thread_ready','provider_thread_id':'thread1','provider_contract':'desktop_mcp_v2'})
    begin(core,job,request_id='one')
    with pytest.raises(BusinessError) as error:begin(core,job,request_id='two')
    assert error.value.code=='conversation_busy'

def test_repair_unacknowledged_creation_reuses_selected_task(core,monkeypatch):
    from management import session_coordinator
    job,publish=claimed(core);publish({'phase':'creating_thread'})
    with core.store.connect() as c:
        c.execute("UPDATE jobs SET status='failed' WHERE id=?",(job['id'],))
        conversations.update_job(core,c,job,'failed',{'message':'lost response'})
    view=core.query('conversation',scope={'kind':'general'})['conversation']
    monkeypatch.setattr(session_coordinator,'prepare_attachment',lambda core,p:{'thread_id':p['provider_thread_id'],'project_path':str(core.root/'Codex事务助手')})
    result=command(core,'attach_conversation',{'conversation_id':view['id'],'version':view['version'],'provider_thread_id':'actual-existing-thread'})
    assert result['reused'] and not result['new_thread_created']
    sent=command(core,'send_message',{'scope':{'kind':'general'},'text':'continue after repair'})
    assert sent['job']['input']['provider_thread_id']=='actual-existing-thread'
    with core.store.connect() as c:
        assert c.execute('SELECT count(*) FROM conversation_bindings').fetchone()[0]==1

@pytest.mark.parametrize('field',['ai_config','previous_operation','execution_owner'])
def test_public_ai_jobs_cannot_forge_internal_delivery_configuration(core,field):
    with pytest.raises(BusinessError) as error:
        command(core,'create_job',{'kind':'ai','input':{'prompt':'Synthetic',field:{}}})
    assert error.value.code=='proposal_scope'

def test_start_reply_after_disconnect_releases_unacknowledged_fence(relay):
    value,engine,sent,_,desktop,manager=relay;manager['initialized']=True
    value.accept(manager,{'id':1,'method':'turn/start','params':{'threadId':'t'}})
    mapped=engine.sent[-1]['id']
    value.drop_actor(manager)
    value.receive_engine({'id':mapped,'error':{'code':-32600,'message':'synthetic rejection'}})
    assert 't' not in value.pending_turn_owners
    before=len(engine.sent)
    value.accept(desktop,{'id':2,'method':'turn/start','params':{'threadId':'t'}})
    assert len(engine.sent)==before+1

@pytest.mark.parametrize('active',[False,True])
def test_loaded_legacy_thread_reloads_only_its_own_idle_tool_connection(tmp_path,monkeypatch,active):
    from management import ai,ai_shared,discussion_mcp,codex_project
    calls=[]
    class RPC:
        def __init__(self,*args):self.thread_id=self.turn_id=None
        def request(self,method,params):
            calls.append((method,params))
            if method=='thread/read':return {'thread':{'id':'old','status':{'type':'active' if active else 'idle'},'turns':[]}}
            if method=='thread/unsubscribe':return {'status':'unsubscribed'}
            if method=='thread/resume':return {'thread':{'id':'old'}}
            return {}
        def send(self,value):pass
        def close(self):pass
    class Progress:
        def __call__(self,value):pass
        def candidate_result(self):return proposal()
    monkeypatch.setattr(ai,'find_codex',lambda *a:'synthetic')
    monkeypatch.setattr(ai,'project_directory',lambda *a:tmp_path)
    monkeypatch.setattr(ai,'_isolation_overrides',lambda *a:{})
    monkeypatch.setattr(ai_shared,'SharedAppServer',RPC)
    monkeypatch.setattr(codex_project,'project_binding',lambda *a:{'project_id':'project'})
    monkeypatch.setattr(discussion_mcp,'configuration',lambda *a:{})
    monkeypatch.setattr(discussion_mcp,'run',lambda *a:proposal())
    value={'prompt':'Continue','context':{},'conversation_id':'conversation','provider_thread_id':'old',
           'provider_project_path':str(tmp_path),'provider_contract':'desktop_candidate_tool_v1'}
    settings={'ai':{'enabled':True,'execution_mode':'desktop_shared'},'_discussion_epoch':'epoch'}
    if active:
        with pytest.raises(ai.AIError) as error:ai.generate(value,settings,threading.Event(),Progress())
        assert error.value.code=='AI_CONVERSATION_BUSY'
        assert not any(m in {'thread/unsubscribe','thread/resume','thread/start','turn/start'} for m,p in calls)
    else:
        result=ai.generate(value,settings,threading.Event(),Progress())
        assert result['provider']['thread_id']=='old'
        methods=[m for m,p in calls]
        assert methods.index('thread/unsubscribe')<methods.index('thread/resume') and 'thread/start' not in methods
        assert next(p for m,p in calls if m=='thread/unsubscribe')=={'threadId':'old'}

@pytest.mark.parametrize('same_epoch',[True,False])
def test_gui_new_message_refreshes_revision_without_switching_data_space(same_epoch):
    from management.gui_async import _Worker
    calls=[]
    class Client:
        def query(self,name,**params):
            calls.append(('query',name))
            return {'epoch':'original' if same_epoch else 'restored','revision':9}
        def command(self,name,payload,**options):
            calls.append(('command',name,options))
            return {'epoch':'original','revision':10}
    worker=_Worker('synthetic',client_factory=lambda path:Client())
    worker.execute(1,'command','send_message',{'payload':{'text':'new message'},'options':{'epoch':'original','expected_revision':2,'request_id':'stable-id'}})
    options=calls[-1][2]
    assert options['epoch']=='original' and options['request_id']=='stable-id'
    assert options['expected_revision']==(9 if same_epoch else 2)
    calls.clear()
    worker.execute(2,'command','update',{'payload':{},'options':{'epoch':'original','expected_revision':2}})
    assert len(calls)==1 and calls[0][2]['expected_revision']==2
