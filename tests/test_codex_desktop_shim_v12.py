"""Synthetic protocol and lifecycle checks; never starts a real Codex turn."""
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from management import codex_desktop_shim as shim


@pytest.mark.parametrize('args,expected', [
    (['app-server'], ['app-server']),
    (['-c','features.code_mode_host=true','app-server','--analytics-default-enabled','-c','x=true'],
     ['-c','features.code_mode_host=true','app-server','--analytics-default-enabled','-c','x=true']),
    (['app-server','--stdio'], ['app-server']),
    (['app-server','--listen','stdio://'], ['app-server']),
    (['app-server','--listen=stdio://'], ['app-server']),
    (['--config=x=1','app-server'], ['--config=x=1','app-server']),
    (['-cx=1','app-server'], ['-cx=1','app-server']),
])
def test_normal_stdio_only(args, expected):
    assert shim.engine_arguments(args) == expected


@pytest.mark.parametrize('args', [
    ['--version'], ['app','x'], ['exec','app-server'], ['app-server','--help'],
    ['app-server','daemon','start'], ['app-server','proxy'],
    ['app-server','generate-json-schema','--out','x'],
    ['-c','some_key=app-server','exec','task'],
    ['app-server','--listen','ws://127.0.0.1:4500'],
    ['app-server','--listen=unix://'], ['app-server','--stdio','--stdio'],
    ['app-server','--stdio','--listen','stdio://'],
    ['app-server','--ws-auth','capability-token'], ['--unknown','app-server'],
    ['-c'], ['app-server','--listen'],
])
def test_other_invocations_are_forwarded(args):
    assert shim.engine_arguments(args) is None


def test_forward_preserves_arguments_environment_and_exit(monkeypatch, tmp_path):
    real = tmp_path/'real.exe'; real.write_bytes(b'dummy')
    monkeypatch.setenv('PM_CODEX_REAL_CLI', str(real))
    monkeypatch.setenv('PM_CODEX_BRIDGE_DIR', str(tmp_path/'bridge'))
    monkeypatch.setenv('CODEX_APP_TOOLS_PIPE_PATH', 'synthetic-only')
    calls=[]
    def run(args, **kwargs):
        calls.append((args,kwargs))
        return 7
    monkeypatch.setattr(shim.subprocess,'call',run)
    assert shim.main(['app-server','generate-json-schema','--out','synthetic']) == 7
    assert calls[0][0] == [str(real),'app-server','generate-json-schema','--out','synthetic']
    assert calls[0][1]['env']['CODEX_APP_TOOLS_PIPE_PATH'] == 'synthetic-only'


def test_configuration_rejects_recursive_executable(monkeypatch,tmp_path):
    monkeypatch.setenv('PM_CODEX_REAL_CLI',shim.sys.executable)
    monkeypatch.setenv('PM_CODEX_BRIDGE_DIR',str(tmp_path))
    with pytest.raises(shim.ShimError,match='adapter'):
        shim._configuration(os.environ)


def test_runtime_atomic_bound_and_identity_cleanup(tmp_path):
    record={'pid':123,'instance_id':'one','token':'test-only'}
    shim._write_runtime(tmp_path,record)
    assert shim._read_runtime(tmp_path)==record
    shim._remove_runtime(tmp_path,{'pid':123,'instance_id':'two'})
    assert (tmp_path/'runtime.json').exists()
    shim._remove_runtime(tmp_path,record)
    assert not (tmp_path/'runtime.json').exists()
    with pytest.raises(shim.ShimError,match='too large'):
        shim._write_runtime(tmp_path,{'text':'x'*shim.MAX_RUNTIME})
    assert not list(tmp_path.glob('runtime.*.tmp'))


def test_runtime_read_is_bounded(tmp_path):
    (tmp_path/'runtime.json').write_bytes(b'x'*(shim.MAX_RUNTIME+1))
    assert shim._read_runtime(tmp_path) is None


def test_failed_atomic_replace_preserves_record_and_cleans_temp(tmp_path,monkeypatch):
    shim._write_runtime(tmp_path,{'pid':1})
    monkeypatch.setattr(shim.os,'replace',lambda *a:(_ for _ in ()).throw(OSError('synthetic')))
    with pytest.raises(OSError):shim._write_runtime(tmp_path,{'pid':2})
    assert shim._read_runtime(tmp_path)=={'pid':1}
    assert not list(tmp_path.glob('runtime.*.tmp'))


def test_engine_is_suspended_before_job_attachment_and_preserves_env(tmp_path,monkeypatch):
    events=[]
    proc=SimpleNamespace(pid=99,poll=lambda:None)
    class Job:
        def __init__(self):events.append('job')
        def assign_and_resume(self,p):
            assert p is proc
            events.extend(['assign','resume'])
        def close(self):events.append('close')
    def popen(args,**kwargs):
        assert kwargs['creationflags'] & 4
        assert kwargs['env']==dict(os.environ)
        assert args==[str(tmp_path/'codex.exe'),'app-server']
        events.append('suspended')
        return proc
    monkeypatch.setattr(shim,'_Job',Job)
    monkeypatch.setattr(shim.subprocess,'Popen',popen)
    assert shim._start_engine(tmp_path/'codex.exe',['app-server'])[0] is proc
    assert events==['job','suspended','assign','resume']


def test_job_is_closed_when_attachment_fails(monkeypatch,tmp_path):
    events=[]
    proc=SimpleNamespace(pid=99,poll=lambda:None,kill=lambda:events.append('kill'),wait=lambda **k:events.append('wait'))
    class Job:
        def assign_and_resume(self,p):raise shim.ShimError('cannot assign')
        def close(self):events.append('close')
    monkeypatch.setattr(shim,'_Job',Job)
    monkeypatch.setattr(shim.subprocess,'Popen',lambda *a,**k:proc)
    with pytest.raises(shim.ShimError):shim._start_engine(tmp_path/'real.exe',[])
    assert events==['kill','wait','close']


def test_diagnostics_redact_split_token(monkeypatch):
    token='synthetic-secret-12345'
    output=io.StringIO();monkeypatch.setattr(shim.sys,'stderr',output)
    chunks=iter([b'failure synthetic-sec',b'ret-12345 happened\n',b''])
    shim._forward_stderr(SimpleNamespace(read=lambda n:next(chunks)),token,shim.threading.Event())
    assert token not in output.getvalue()
    assert '[redacted]' in output.getvalue()
    assert 'failure' in output.getvalue()


class Engine:
    def __init__(self):self.sent=[];self.closed=False
    def send(self,text):self.sent.append(json.loads(text))
    def close(self):self.closed=True


@pytest.fixture
def relay():
    engine=Engine();published=[]
    value=shim._Relay(engine,SimpleNamespace(poll=lambda:None),SimpleNamespace(alive=lambda:True),
                      't'*64,published.append,io.BytesIO(),io.BytesIO())
    sent=[]
    value.enqueue=lambda actor,msg:sent.append((actor['name'],msg))
    def actor(name,initialized=True):
        result={'name':name,'active':True,'initialized':initialized,'close':lambda:None,'queue':shim.queue.Queue(4)}
        value.actors[name]=result
        return result
    desktop=actor('desktop');management=actor('management',False)
    return value,engine,sent,published,desktop,management


def initialize(value,desktop):
    value.endpoint='ws://127.0.0.1:1234'
    value.accept(desktop,{'id':1,'method':'initialize','params':{'capabilities':{'experimentalApi':True}}})
    mapped=value.engine.sent[-1]['id']
    value.receive_engine({'id':mapped,'result':{'userAgent':'synthetic','platformFamily':'windows'}})
    value.accept(desktop,{'method':'initialized'})


def test_runtime_waits_for_real_desktop_initialized(relay):
    value,engine,sent,published,desktop,manager=relay
    value.endpoint='ws://127.0.0.1:1234'
    value.accept(desktop,{'id':1,'method':'initialize','params':{}})
    assert not published
    value.receive_engine({'id':engine.sent[-1]['id'],'result':{'userAgent':'synthetic'}})
    assert not published
    value.accept(desktop,{'method':'initialized'})
    assert published==[value.endpoint]
    value.publish_if_ready()
    assert published==[value.endpoint]


def test_management_initialize_is_cached_never_reinitializes_engine(relay):
    value,engine,sent,_,desktop,manager=relay
    initialize(value,desktop)
    count=len(engine.sent)
    value.accept(manager,{'id':1,'method':'initialize','params':{'capabilities':{'experimentalApi':True}}})
    value.accept(manager,{'method':'initialized'})
    assert len(engine.sent)==count
    assert sent[-1][1]['result']['userAgent']=='synthetic'
    value.accept(manager,{'id':2,'method':'initialize','params':{}})
    assert sent[-1][1]['error']['message']=='Already initialized'


def test_management_init_rejects_unready_or_unsupported_capabilities(relay):
    value,engine,sent,_,desktop,manager=relay
    value.accept(manager,{'id':1,'method':'initialize','params':{}})
    assert 'not ready' in sent[-1][1]['error']['message']
    initialize(value,desktop)
    value.accept(manager,{'id':2,'method':'initialize','params':{'capabilities':{'requestAttestation':True}}})
    assert 'capabilities' in sent[-1][1]['error']['message']


def test_request_ids_do_not_collide_and_reply_goes_to_origin(relay):
    value,engine,sent,_,desktop,manager=relay
    manager['initialized']=True
    for actor in (desktop,manager):
        value.accept(actor,{'id':7,'method':'thread/read','params':{'threadId':'t'}})
    first,second=[m['id'] for m in engine.sent]
    assert first!=second
    value.receive_engine({'id':second,'result':{'which':'management'}})
    value.receive_engine({'id':first,'result':{'which':'desktop'}})
    assert sent==[('management',{'id':7,'result':{'which':'management'}}),('desktop',{'id':7,'result':{'which':'desktop'}})]


def test_turn_and_message_notifications_reach_desktop_and_initialized_manager(relay):
    value,engine,sent,_,desktop,manager=relay
    manager['initialized']=True
    for method in ('thread/started','turn/started','item/agentMessage/delta','turn/completed'):
        value.receive_engine({'method':method,'params':{'threadId':'t','turn':{'id':'turn1'},'delta':'synthetic'}})
    assert [name for name,msg in sent]==['desktop','management']*4


def test_management_allowlist_and_pending_bound(relay):
    value,engine,sent,_,desktop,manager=relay
    manager['initialized']=True
    value.accept(manager,{'id':1,'method':'config/value/write','params':{}})
    assert not engine.sent and sent[-1][1]['error']['code']==-32601
    value.MAX_PENDING=1
    value.accept(manager,{'id':2,'method':'thread/read','params':{'threadId':'t'}})
    value.accept(desktop,{'id':3,'method':'thread/read','params':{'threadId':'t'}})
    assert len(engine.sent)==1 and sent[-1][1]['error']['code']==-32001


def start_turn(value,actor,thread_id,turn_id):
    value.accept(actor,{'id':1,'method':'turn/start','params':{'threadId':thread_id}})
    request=value.engine.sent[-1]
    value.receive_engine({'method':'turn/started','params':{'threadId':thread_id,'turn':{'id':turn_id}}})
    value.receive_engine({'id':request['id'],'result':{'turn':{'id':turn_id}}})


def test_server_requests_follow_turn_owner_and_never_other_desktop_threads(relay):
    value,engine,sent,_,desktop,manager=relay
    manager['initialized']=True
    start_turn(value,manager,'managed','m-turn')
    start_turn(value,desktop,'managed','d-turn')
    for ident,thread,turn in [(8,'managed','m-turn'),(9,'managed','d-turn'),(10,'unrelated','another')]:
        value.receive_engine({'id':ident,'method':'item/tool/call','params':{'threadId':thread,'turnId':turn}})
    assert [name for name,msg in sent[-3:]]==['management','desktop','desktop']
    before=len(engine.sent)
    value.accept(manager,{'id':10,'result':{'success':False}})
    assert len(engine.sent)==before
    value.accept(desktop,{'id':10,'result':{'success':True}})
    assert engine.sent[-1]=={'id':10,'result':{'success':True}}


def test_turn_owner_available_before_turn_start_ack(relay):
    value,engine,sent,_,desktop,manager=relay
    manager['initialized']=True
    value.accept(manager,{'id':1,'method':'turn/start','params':{'threadId':'managed'}})
    value.receive_engine({'method':'turn/started','params':{'threadId':'managed','turn':{'id':'early'}}})
    value.receive_engine({'id':81,'method':'item/tool/call','params':{'threadId':'managed','turnId':'early'}})
    assert sent[-1][0]=='management'


def test_management_disconnect_does_not_close_engine_or_other_clients(relay):
    value,engine,sent,_,desktop,manager=relay
    manager['initialized']=True
    value.accept(manager,{'id':2,'method':'thread/read','params':{}})
    value.drop_actor(manager)
    assert not engine.closed and desktop['active'] and not value.pending


@pytest.mark.parametrize('origins,authorization,accepted', [([],['Bearer '+'t'*64],True),
    ([],[],False),([],['Bearer wrong'],False),(['http://127.0.0.1'],['Bearer '+'t'*64],False),
    ([],['Bearer '+'t'*64,'Bearer '+'t'*64],False)])
def test_frontend_authentication_rejects_origins_and_bad_tokens(relay,origins,authorization,accepted):
    value,*_=relay
    headers=SimpleNamespace(get_all=lambda name:origins if name=='Origin' else authorization)
    connection=SimpleNamespace(respond=lambda status,text:(status,text))
    assert (value.authentication(connection,SimpleNamespace(headers=headers)) is None) is accepted


def test_bound_slow_desktop_output_fails_instead_of_unbounded_queue(monkeypatch):
    value=shim._Relay(Engine(),None,None,'t'*64,lambda e:None,io.BytesIO(),io.BytesIO())
    value.OUTPUT_BACKPRESSURE_SECONDS=0
    actor={'name':'desktop','active':True,'initialized':True,'queue':shim.queue.Queue(1),'close':lambda:None}
    value.actors['desktop']=actor
    value.enqueue(actor,{'method':'one'})
    value.enqueue(actor,{'method':'two'})
    assert value.stop.is_set() and value.engine.closed
    assert actor['queue'].qsize()==1


def test_bounded_invalid_protocol_frames():
    with pytest.raises(shim.ShimError):shim._Relay.decode('x'*(shim.MAX_FRAME+1))
    with pytest.raises(shim.ShimError):shim._Relay.decode('[]')
    with pytest.raises(shim.ShimError):shim._Relay.decode('{"id":true}')


def test_run_bridge_uses_separate_tokens_and_cleans_own_runtime(monkeypatch,tmp_path):
    calls={};events=[]
    proc=SimpleNamespace(pid=99,stderr=io.BytesIO(),wait=lambda **k:events.append('wait'))
    job=SimpleNamespace(close=lambda:events.append('job-close'))
    connection=Engine()
    monkeypatch.setattr(shim,'_secure_directory',lambda d:None)
    monkeypatch.setattr(shim,'_DirectoryLock',lambda d:SimpleNamespace(close=lambda:events.append('unlock')))
    monkeypatch.setattr(shim,'_ParentWatch',lambda:SimpleNamespace(alive=lambda:True))
    monkeypatch.setattr(shim,'_endpoint',lambda:'ws://127.0.0.1:1001')
    tokens=iter(['engine-token','front-token']);monkeypatch.setattr(shim.secrets,'token_urlsafe',lambda n:next(tokens))
    def start(exe,args):calls['args']=args;return proc,job
    monkeypatch.setattr(shim,'_start_engine',start)
    def connect(endpoint,token,*args):calls['engine_token']=token;return connection
    monkeypatch.setattr(shim,'_connect',connect)
    monkeypatch.setattr(shim,'_runtime_record',lambda exe,p,e,t:{'pid':123,'instance_id':'own','endpoint':e,'token':t})
    class Relay:
        def __init__(self,c,p,watch,token,on_ready,stdin,stdout):
            assert c is connection and token=='front-token'
            self.on_ready=on_ready
        def run(self):
            assert not (tmp_path/'runtime.json').exists()
            self.on_ready('ws://127.0.0.1:1002')
            record=shim._read_runtime(tmp_path)
            assert record['ready'] is True and record['token']=='front-token'
            assert record['endpoint']=='ws://127.0.0.1:1002'
    monkeypatch.setattr(shim,'_Relay',Relay)
    assert shim.run_bridge(tmp_path/'real.exe',tmp_path,['app-server'],io.BytesIO(),io.BytesIO())==0
    assert calls['engine_token']=='engine-token'
    assert 'engine-token' not in calls['args'] and 'front-token' not in calls['args']
    assert calls['args'][-1]==shim.hashlib.sha256(b'engine-token').hexdigest()
    assert not (tmp_path/'runtime.json').exists()
    assert events==['job-close','wait','unlock'] and connection.closed


@pytest.mark.skipif(os.name!='nt',reason='Windows permissions only')
def test_private_directory_acl_and_exclusive_lock(tmp_path):
    directory=tmp_path/'private'
    shim._secure_directory(directory)
    first=shim._DirectoryLock(directory)
    try:
        with pytest.raises(shim.ShimError,match='owns'):
            shim._DirectoryLock(directory)
    finally:
        first.close()
    second=shim._DirectoryLock(directory)
    second.close()


def test_disconnected_management_cancels_only_its_pending_tool_requests(relay):
    value,engine,sent,_,desktop,manager=relay
    manager['initialized']=True
    start_turn(value,manager,'managed','m-turn')
    start_turn(value,desktop,'other','d-turn')
    for ident,thread,turn in [(21,'managed','m-turn'),(22,'other','d-turn')]:
        value.receive_engine({'id':ident,'method':'item/tool/call','params':{'threadId':thread,'turnId':turn}})
    value.drop_actor(manager)
    assert engine.sent[-1]['id']==21 and 'error' in engine.sent[-1]
    assert value.server_pending=={('int',22):desktop}
    before=len(sent)
    value.receive_engine({'id':23,'method':'item/tool/call','params':{'threadId':'managed','turnId':'m-turn'}})
    assert len(sent)==before
    assert engine.sent[-1]['id']==23 and 'error' in engine.sent[-1]
    assert not engine.closed


def test_normal_desktop_burst_has_bounded_backpressure():
    import time
    value=shim._Relay(Engine(),None,None,'t'*64,lambda e:None,io.BytesIO(),io.BytesIO())
    delivered=[]
    def send(text):
        time.sleep(.0005)
        delivered.append(json.loads(text))
    actor=value.add_actor('desktop',send,lambda:None)
    try:
        for number in range(250):
            value.enqueue(actor,{'method':'item/agentMessage/delta','params':{'delta':str(number)}})
        deadline=time.monotonic()+2
        while len(delivered)<250 and time.monotonic()<deadline:
            time.sleep(.005)
        assert len(delivered)==250 and not value.stop.is_set()
        assert value.queued_bytes==0
    finally:
        value.shutdown()
        value.drop_actor(actor)


def test_management_cannot_consume_desktop_reserved_queue_budget():
    value=shim._Relay(Engine(),None,None,'t'*64,lambda e:None,io.BytesIO(),io.BytesIO())
    value.MAX_MANAGEMENT_QUEUED_BYTES=100
    manager={'name':'management','active':True,'initialized':True,'queue':shim.queue.Queue(128),'close':lambda:None}
    desktop={'name':'desktop','active':True,'initialized':True,'queue':shim.queue.Queue(128),'close':lambda:None}
    value.actors={'management':manager,'desktop':desktop}
    value.enqueue(manager,{'method':'a','text':'x'*110})
    assert not manager['active'] and desktop['active'] and not value.engine.closed
    value.enqueue(desktop,{'method':'a','text':'x'*110})
    assert desktop['queue'].qsize()==1 and not value.stop.is_set()


def test_runtime_records_actual_process_executable_not_venv_launcher(tmp_path,monkeypatch):
    actual=tmp_path/'base-python.exe'
    launcher=tmp_path/'venv'/'python.exe'
    engine=tmp_path/'codex.exe'
    current=SimpleNamespace(pid=41,exe=lambda:str(actual),create_time=lambda:1000.0)
    child=SimpleNamespace(create_time=lambda:1001.0)
    monkeypatch.setattr(shim.sys,'executable',str(launcher))
    monkeypatch.setattr(shim.psutil,'Process',lambda pid=None:current if pid is None else child)
    record=shim._runtime_record(engine,SimpleNamespace(pid=42),'ws://127.0.0.1:9000','synthetic-token')
    assert record['pid']==41 and record['executable']==str(actual.resolve())
    assert record['shim_create_time']==1000.0
    assert record['engine_pid']==42 and record['engine_create_time']==1001.0
    assert record['engine_executable']==str(engine)
