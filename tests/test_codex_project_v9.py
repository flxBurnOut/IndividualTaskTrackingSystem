"""Stable Codex project, scoped independent chats and explicit migration."""
import json,threading
from management import ai,conversations
from management.core import Core
from management.storage import encode
from test_ai_conversations_v3 import rpc,request
from test_plan_assistance_v8 import cmd


def test_distinct_conversations_share_project_but_do_not_resume_each_other(rpc,tmp_path):
    settings={'enabled':True,'_codex_project_dir':str(tmp_path/'fixed-project')}
    first=ai.generate(request(conversation_id='one'),settings,threading.Event())
    second=ai.generate(request(conversation_id='two'),settings,threading.Event())
    starts=[next(p for name,p in r.requests if name=='thread/start') for r in rpc.instances[-2:]]
    assert starts[0]['cwd']==starts[1]['cwd']==str(tmp_path/'fixed-project')
    assert not any(name=='thread/resume' for r in rpc.instances[-2:] for name,p in r.requests)
    assert first['provider']['project_path']==second['provider']['project_path']
    assert (tmp_path/'fixed-project').is_dir()


def test_old_temporary_project_rebuilds_history_in_fixed_project_then_resumes(rpc,tmp_path):
    path=str(tmp_path/'fixed-project');settings={'enabled':True,'_codex_project_dir':path}
    first=ai.generate(request(provider_thread_id='old-thread',provider_project_path='old-temporary-directory'),settings,threading.Event())
    assert 'thread/resume' not in [n for n,p in rpc.instances[-1].requests]
    assert first['provider']['recovery']=='history_rebuilt'
    ai.generate(request(provider_thread_id=first['provider']['thread_id'],provider_project_path=path),settings,threading.Event())
    assert [n for n,p in rpc.instances[-1].requests]==['initialize','thread/resume','turn/start']
    assert all(p['cwd']==path for n,p in rpc.instances[-1].requests if n in {'thread/resume','turn/start'})


def test_software_persists_provider_project_for_next_turn(tmp_path):
    core=Core(tmp_path/'data');cmd(core,'settings',{'settings':{'ai':{'enabled':True}}})
    sent=cmd(core,'send_message',{'scope':{'kind':'general'},'text':'Synthetic message'})
    job=sent['job'];path=str(core.root/'Codex事务助手')
    result={'summary':'Synthetic reply','actions':[],'unknowns':[],'sources':[],
        'provider':{'thread_id':'synthetic-thread','project_path':path,'recovery':'new'}}
    with core.store.connect() as c:
        c.execute("UPDATE jobs SET status='completed',result=? WHERE id=?",(encode(result),job['id']))
        conversations.complete_job(core,c,job,result)
    next_job=cmd(core,'send_message',{'scope':{'kind':'general'},'text':'Continue synthetic discussion'})['job']
    assert next_job['input']['provider_thread_id']=='synthetic-thread'
    assert next_job['input']['provider_project_path']==path
