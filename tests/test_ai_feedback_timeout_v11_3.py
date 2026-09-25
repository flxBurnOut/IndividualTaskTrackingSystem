"""Feedback intent and actionable timeout regressions; no real account/data."""
import os
import queue
import threading
import time
import uuid
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from management import ai
from management.core import Core
from management.plan_assistance import requested_day
from management.gui_assistant import AssistanceDialog
from management.scheduler import Background

SCOPE={'kind':'daily_plan','date':'2030-01-01'}

def command(core,name,payload):
    s=core.query('state')
    return core.command(name,payload,request_id=str(uuid.uuid4()),epoch=s['epoch'],expected_revision=s['revision'])['result']

@pytest.mark.parametrize('text',['ML4的OQ已经搞定了','ML4的OQ已经搞定了。','我已经完成Tutorial 4','PQ3还没完成','Lecture 2看完了','今天补了2节课','作业已经提交了'])
def test_feedback_overrides_stale_launch_hint(text):
    assert requested_day(text,SCOPE,'2030-01-01',explicit=True) is None

@pytest.mark.parametrize('text',['OQ已经搞定了，请调整今天计划','今天主要完成未完成的PQ3，请生成计划','请根据已有任务生成每日计划'])
def test_explicit_planning_request_still_routes_to_plan(text):
    assert requested_day(text,SCOPE,'2030-01-01',explicit=True)=='2030-01-01'


def test_feedback_job_keeps_feedback_capability_and_does_not_force_new_plan(tmp_path):
    core=Core(tmp_path);command(core,'settings',{'settings':{'ai':{'enabled':True}}})
    job=command(core,'send_message',{'scope':SCOPE,'text':'ML4的OQ已经搞定了。','request_plan':True})['job']
    assert not job['input'].get('plan_requested')
    assert 'record_feedback' in job['input']['allowed_commands']
    assert 'plan_request' not in job['input']['context']
    assert core.query('list',type='plan')['total']==0
    assert core.query('list',type='feedback')['total']==0


class Bridge:
    epoch='synthetic-epoch'
    def __init__(self):self.commands=[]
    def query(self,*a,**k):pass
    def command(self,*a,**k):self.commands.append((a,k))

@pytest.fixture(scope='session')
def app():return QApplication.instance() or QApplication([])

@pytest.mark.parametrize('replacement,flag',[('ML4的OQ已经搞定了',False),('请生成今天计划',True)])
def test_gui_replaced_planning_prefill_is_not_an_order_to_replan(app,replacement,flag):
    bridge=Bridge();view=AssistanceDialog(bridge,prompt='请生成今天计划',intent='daily_plan',scope=SCOPE)
    view.timer.stop();view.settings_ready=view.conversation_ready=view.ai_enabled=True
    view.prompt.setPlainText(replacement);view.start_job()
    payload=bridge.commands[-1][0][1]
    assert bool(payload.get('request_plan')) is flag
    bridge.commands[-1][0][2]({'result':{'job':{'id':'synthetic-job'}}})
    assert view.intent is None
    view.close();view.deleteLater();app.processEvents()


def receiver():
    rpc=object.__new__(ai._AppServer);rpc.cancel=threading.Event();rpc.deadline=time.monotonic()-1
    rpc.started_at=time.monotonic()-180;rpc.timeout_seconds=180
    rpc.events=queue.Queue();rpc.waiting_method=None;rpc.phase='awaiting_model';rpc.last_event=None
    return rpc

@pytest.mark.parametrize('method,label',[('thread/resume','恢复讨论'),('turn/start','提交消息'),('initialize','连接 Codex')])
def test_timeout_identifies_request_stage(method,label):
    rpc=receiver();rpc.waiting_method=method
    with pytest.raises(ai.AIError) as e:rpc._receive()
    assert e.value.code=='AI_TIMEOUT' and label in e.value.message
    assert e.value.details['request_method']==method
    assert e.value.details['elapsed_seconds']>=179

@pytest.mark.parametrize('kind,label',[('reasoning','推理'),('agentMessage','返回内容')])
def test_timeout_distinguishes_live_model_from_setup_failure(kind,label):
    rpc=receiver();rpc._observe({'method':'item/started','params':{'item':{'type':kind,'text':'private input should never appear in diagnostic'}}})
    error=rpc._timeout_error()
    assert label in error.message and '180' in error.message
    assert 'private input' not in str(error.details)
    assert error.details['last_event']=='item/started'


def test_retry_notice_is_not_mislabelled_as_success():
    rpc=receiver();rpc._observe({'method':'error','params':{'willRetry':True,'error':{'message':'secret'}}})
    assert rpc.phase=='provider_retry'
    assert '重试上游' in rpc._timeout_error().message
    assert 'secret' not in str(rpc._timeout_error().details)


def test_worker_persists_safe_timeout_details_without_upstream_payloads(tmp_path,monkeypatch):
    core=Core(tmp_path);command(core,'settings',{'settings':{'ai':{'enabled':True}}})
    job=command(core,'send_message',{'scope':SCOPE,'text':'Synthetic discussion'})['job']
    stop=threading.Event()
    def generate(*args):
        stop.set()
        raise ai.AIError('AI_TIMEOUT','Synthetic pending model reply',{'stage':'awaiting_model','elapsed_seconds':180,'timeout_seconds':180,'request_method':'turn/start','last_event':'turn/started','raw_stderr':'do not persist','prompt':'private'})
    monkeypatch.setattr(ai,'generate',generate)
    Background(core,stop).worker()
    result=core.query('job',id=job['id'])['job']
    assert result['status']=='failed'
    assert result['error']['details']=={'stage':'awaiting_model','elapsed_seconds':180,'timeout_seconds':180,'request_method':'turn/start','last_event':'turn/started'}
    assert core.query('conversation',scope=SCOPE)['conversation']['active_job_id'] is None
    assert core.query('list',type='feedback')['total']==0
