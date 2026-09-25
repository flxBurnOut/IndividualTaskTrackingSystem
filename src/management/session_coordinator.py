"""Durable provider bindings, send intents and candidate receipts.

Operational records never advance the business revision. Candidate application
still uses Core's epoch/revision checks and the shared command registry.
"""
from __future__ import annotations
import json
import hashlib
from .schemas import BusinessError
from .storage import encode, now, new_id


def initialize(c):
    c.execute("""CREATE TABLE IF NOT EXISTS conversation_operations(
        job_id TEXT PRIMARY KEY REFERENCES jobs(id),conversation_id TEXT NOT NULL,
        epoch TEXT NOT NULL,phase TEXT NOT NULL,provider_thread_id TEXT,provider_turn_id TEXT,
        project_path TEXT,contract TEXT,candidate TEXT,error TEXT,updated_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS conversation_native_requests(
        conversation_id TEXT NOT NULL,epoch TEXT NOT NULL,request_id TEXT NOT NULL,job_id TEXT NOT NULL,
        PRIMARY KEY(conversation_id,epoch,request_id))""")
    if 'candidate_fingerprint' not in {r[1] for r in c.execute('PRAGMA table_info(conversation_operations)')}:
        c.execute('ALTER TABLE conversation_operations ADD COLUMN candidate_fingerprint TEXT')
    c.execute('CREATE INDEX IF NOT EXISTS conversation_operation_scope ON conversation_operations(conversation_id,updated_at)')
    c.execute("""CREATE TABLE IF NOT EXISTS conversation_bindings(
        conversation_id TEXT NOT NULL,provider_thread_id TEXT NOT NULL,epoch TEXT NOT NULL,
        project_path TEXT,contract TEXT,state TEXT NOT NULL,reason TEXT,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        PRIMARY KEY(conversation_id,provider_thread_id,epoch))""")
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS conversation_binding_owner
        ON conversation_bindings(provider_thread_id,epoch) WHERE state='active'""")

    epoch_row=c.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
    if epoch_row:
        epoch=json.loads(epoch_row[0])
        c.execute("""INSERT OR IGNORE INTO conversation_bindings
            SELECT json_extract(j.input,'$.conversation_id'),json_extract(j.result,'$.provider.thread_id'),j.epoch,
                json_extract(j.result,'$.provider.project_path'),json_extract(j.result,'$.provider.contract'),
                'historical','imported_record',j.created_at,j.updated_at FROM jobs j JOIN conversations v
                ON v.id=json_extract(j.input,'$.conversation_id')
            WHERE j.epoch=? AND json_extract(j.result,'$.provider.thread_id') IS NOT NULL""",(epoch,))
        c.execute("""UPDATE conversation_bindings SET state='active' WHERE epoch=? AND
            provider_thread_id=(SELECT provider_thread_id FROM conversations WHERE id=conversation_id)
            AND (SELECT count(*) FROM conversations v WHERE v.provider_thread_id=conversation_bindings.provider_thread_id)=1""",(epoch,))


def observe(c, job, conversation_id, values, project_path):
    if not conversation_id: return
    stamp = now()
    c.execute("""INSERT INTO conversation_operations(job_id,conversation_id,epoch,phase,updated_at)
        VALUES (?,?,?,?,?) ON CONFLICT(job_id) DO NOTHING""",
        (job['id'],conversation_id,job['epoch'],'prepared',stamp))
    c.execute("""UPDATE conversation_operations SET phase=?,provider_thread_id=coalesce(?,provider_thread_id),
        provider_turn_id=coalesce(?,provider_turn_id),project_path=?,contract=coalesce(?,contract),updated_at=? WHERE job_id=?""",
        (values['phase'],values.get('provider_thread_id'),values.get('provider_turn_id'),project_path,
         values.get('provider_contract'),stamp,job['id']))
    thread = values.get('provider_thread_id')
    if thread:
        row = c.execute("""SELECT conversation_id FROM conversation_bindings WHERE provider_thread_id=? AND epoch=? AND state='active'""",
                        (thread,job['epoch'])).fetchone()
        if row and row['conversation_id'] != conversation_id:
            raise BusinessError('conversation_binding_conflict','这个 Codex 任务已经关联其他事项，未改写归属。')
        c.execute("UPDATE conversation_bindings SET state='historical',updated_at=? WHERE conversation_id=? AND epoch=? AND provider_thread_id!=? AND state='active'",
                  (stamp,conversation_id,job['epoch'],thread))
        c.execute("""INSERT INTO conversation_bindings VALUES (?,?,?,?,?,'active',?,?,?)
            ON CONFLICT(conversation_id,provider_thread_id,epoch) DO UPDATE SET
            project_path=excluded.project_path,contract=coalesce(excluded.contract,contract),state='active',updated_at=excluded.updated_at""",
            (conversation_id,thread,job['epoch'],project_path,values.get('provider_contract'),values.get('recovery'),stamp,stamp))


def previous(c, conversation_id, epoch):
    row = c.execute("""SELECT * FROM conversation_operations WHERE conversation_id=? AND epoch=?
        ORDER BY CASE WHEN phase IN ('creating_thread','sending','waiting_model','reasoning','receiving','validating','provider_retry') THEN 0 ELSE 1 END,updated_at DESC,rowid DESC LIMIT 1""",(conversation_id,epoch)).fetchone()
    if not row:return None
    return {k:row[k] for k in ('job_id','phase','provider_thread_id','provider_turn_id','error')}


def settle(c, job_id, phase, error=None):
    c.execute('UPDATE conversation_operations SET phase=?,error=?,updated_at=? WHERE job_id=?',
              (phase,encode(error) if error else None,now(),job_id))


def get_candidate(c, job_id):
    row=c.execute('SELECT candidate FROM conversation_operations WHERE job_id=?',(job_id,)).fetchone()
    return json.loads(row['candidate']) if row and row['candidate'] else None


def complete_candidate(core,c,job,candidate):
    from . import conversations, conversation_progress
    value=json.loads(job['input'])
    if value.get('plan_requested') and not candidate.get('_candidate_validated'):
        from .plan_assistance import normalize
        candidate=normalize(core,c,job,candidate)
    c.execute("UPDATE jobs SET status=?,result=?,error=NULL,updated_at=? WHERE id=?",
              ('awaiting_review' if candidate['actions'] else 'completed',encode(candidate),now(),job['id']))
    conversations.complete_job(core,c,job,candidate)
    settle(c,job['id'],'completed')
    conversation_progress.finish(c,job['id'])
    return candidate


def recover_candidates(core,c):
    for row in c.execute("""SELECT j.* FROM jobs j JOIN conversation_operations o ON o.job_id=j.id
        JOIN conversations v ON v.active_job_id=j.id
        WHERE j.status='running' AND o.candidate IS NOT NULL AND j.epoch=?""",
        (core.store.meta(c,'epoch'),)).fetchall():
        complete_candidate(core,c,dict(row),get_candidate(c,row['id']))


def handle(core, action, payload):
    conversation_id=payload.get('conversation_id')
    if not isinstance(conversation_id,str):raise BusinessError('validation','缺少会话归属。')
    if conversation_id.startswith('operation:'):
        from .context_driver import operation_handle
        return operation_handle(core,conversation_id[10:],action,payload)
    if action=='context':
        from .context_service import QUERY_NAMES
        name=payload.get('name');params=payload.get('params') or {}
        if name not in (QUERY_NAMES-{'prepare_context'})|{'material_image'}:raise BusinessError('context_query','此讨论没有开放该接口。')
        with core.store.connect() as c:
            row=c.execute('''SELECT o.epoch,json_extract(j.input,'$.conversation_id') conversation_id
                FROM context_operations o JOIN jobs j ON j.id=o.job_id WHERE o.id=?''',(params.get('operation_id'),)).fetchone()
            if not row or row['epoch']!=payload.get('epoch') or row['conversation_id']!=conversation_id:
                raise BusinessError('conversation_binding_conflict','读取操作不属于当前事项。')
        return core.query(name,**params)
    # Material extraction runs before the transaction, like ordinary send_message.
    prepared=None
    if action=='begin':
        request_id=payload.get('request_id')
        if not isinstance(request_id,str) or not 1<=len(request_id)<=128:
            raise BusinessError('validation','每轮需要稳定的请求编号，重试时复用原编号。')
        text=payload.get('text')
        if not isinstance(text,str) or not text.strip() or len(text.encode('utf-8'))>800000:
            raise BusinessError('validation','请提供当前用户消息。')
        with core.store.connect() as c:
            row=c.execute('SELECT * FROM conversations WHERE id=?',(conversation_id,)).fetchone()
            if not row:raise BusinessError('not_found','业务会话不存在。')
            request={'scope':json.loads(row['scope']),'text':text}
            active=core._job(c,row['active_job_id']) if row['active_job_id'] else None
        if not active:prepared=core._prepare('send_message',request)
    with core.store.lock, core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            if payload.get('epoch') != core.store.meta(c,'epoch'):
                raise BusinessError('epoch_mismatch','数据空间已恢复或切换，请刷新连接。')
            conversation=c.execute('SELECT * FROM conversations WHERE id=?',(conversation_id,)).fetchone()
            if not conversation:raise BusinessError('not_found','业务会话不存在。')
            if action=='begin':
                prior=c.execute('SELECT job_id FROM conversation_native_requests WHERE conversation_id=? AND epoch=? AND request_id=?',
                                (conversation_id,payload['epoch'],payload['request_id'])).fetchone()
                active=core._job(c,prior['job_id']) if prior else core._job(c,conversation['active_job_id']) if conversation['active_job_id'] else None
                if prior and active['status'] not in {'queued','running'}:
                    raise BusinessError('discussion_turn_finished','这个请求已经结束，未重复创建；新的用户消息请使用新请求编号。')

                if active and active['status'] in {'queued','running'}:
                    owner=c.execute('SELECT request_id FROM conversation_native_requests WHERE job_id=? LIMIT 1',(active['id'],)).fetchone()
                    if owner and owner['request_id']!=payload['request_id']:
                        raise BusinessError('conversation_busy','这一轮已经开始；相同调用重试请复用原请求编号。')

                    value=json.loads(active['input'])
                    if value['prompt'].strip()!=payload['text'].strip():
                        raise BusinessError('conversation_busy','这个事项正在处理另一条消息，请等待返回。')
                    job=active
                else:
                    if prepared is None:raise BusinessError('conversation_conflict','会话状态已经改变，请重新开始这一轮。')
                    from . import conversations,conversation_progress
                    result=conversations.send(core,c,request,'desktop:'+new_id(),prepared)
                    job=core._job(c,result['job']['id'])
                    value=json.loads(job['input']);value['execution_owner']='desktop'
                    revision=core.store.meta(c,'revision')+1
                    core.store.set_meta(c,'revision',revision)
                    c.execute("UPDATE jobs SET status='running',input=?,snapshot_revision=? WHERE id=?",
                              (encode(value),revision,job['id']))
                    job=core._job(c,job['id'])
                    conversations.mark_running(c,job)
                    conversation_progress.start(c,job)
                    observe(c,job,conversation_id,{'phase':'waiting_model','provider_thread_id':conversation['provider_thread_id'],
                        'provider_contract':'desktop_mcp_v3'},str(core.root/'Codex事务助手'))
                c.execute('INSERT OR IGNORE INTO conversation_native_requests VALUES (?,?,?,?)',
                          (conversation_id,payload['epoch'],payload['request_id'],job['id']))
                c.commit()
                value=json.loads(job['input'])
                return {'job_id':job['id'],'epoch':job['epoch'],'generation':job['generation'],
                        'context':value['context'],'allowed_commands':value['allowed_commands'],
                        'history':{'reader':'query_context','collection':'history'},
                        'sources':{'reader':'query_context','collection':'materials'},
                        'instruction':'候选不是事实。只向 submit_candidate 返回候选，等待用户在软件中确认。'}
            if action=='submit':
                job=core._job(c,payload.get('job_id'))
                if json.loads(job['input']).get('conversation_id') != conversation_id or job['epoch']!=payload['epoch']:
                    raise BusinessError('conversation_binding_conflict','回传不属于当前事项。')
                from .ai import validate_proposal
                value=json.loads(job['input'])
                candidate=validate_proposal(payload.get('proposal'),set(value['allowed_commands']))
                fingerprint=hashlib.sha256(encode(payload.get('proposal')).encode()).hexdigest()
                old=c.execute('SELECT candidate_fingerprint FROM conversation_operations WHERE job_id=?',(job['id'],)).fetchone()
                if old and old['candidate_fingerprint']==fingerprint:
                    c.commit();return {'received':True,'job_id':job['id'],'applied':job['status']=='applied','status':job['status']}
                if get_candidate(c,job['id']):raise BusinessError('candidate_conflict','同一轮候选已经接收，不能悄悄替换。')
                if conversation['active_job_id']!=job['id'] or job['status']!='running':
                    raise BusinessError('stale_proposal','这轮已结束或被取消，旧结果不能回写。')
                if job['generation'] != payload.get('generation'):
                    raise BusinessError('stale_proposal','本轮处理版本已改变。')
                if value.get('context_operation_id'):
                    from .context_service import _operation,validate
                    candidate,proof=validate(core,c,_operation(core,c,value['context_operation_id']),candidate)
                    candidate['context_validation']=proof
                elif value.get('plan_requested'):
                    from .plan_assistance import normalize
                    candidate=normalize(core,c,job,candidate)
                if value.get('context_operation_id'):
                    c.execute("UPDATE context_operations SET phase='ready' WHERE id=?",(value['context_operation_id'],))
                candidate['_candidate_validated']=True
                candidate['provider']={'kind':'codex_app_server','thread_id':conversation['provider_thread_id'],
                    'contract':'desktop_mcp_v3','project_path':str(core.root/'Codex事务助手'),'recovery':'resumed'}
                c.execute('UPDATE conversation_operations SET candidate=?,candidate_fingerprint=?,updated_at=? WHERE job_id=?',(encode(candidate),fingerprint,now(),job['id']))
                if value.get('execution_owner')=='desktop':
                    complete_candidate(core,c,job,candidate)
                c.commit()
                return {'received':True,'job_id':job['id'],'applied':False,
                        'message':'候选已持久保存，等待用户在软件中核对确认；尚未修改业务数据。'}
            raise BusinessError('not_found','未注册的会话接口。')
        except Exception:
            c.rollback();raise


def check_native_turns(core, rpc_factory=None):
    """Reconcile finished desktop turns that never returned a candidate.
    Read-only provider requests only. Network uncertainty remains pending.
    """
    import threading
    from pathlib import Path
    from .ai import find_codex
    from .ai_shared import SharedAppServer
    with core.store.connect() as c:
        jobs=[dict(r) for r in c.execute("""SELECT j.* FROM jobs j JOIN conversations v ON v.active_job_id=j.id
            WHERE j.status='running' AND json_extract(j.input,'$.execution_owner')='desktop' LIMIT 4""")]
        settings=core.store.meta(c,'settings');epoch=core.store.meta(c,'epoch')
    if not jobs:return
    rpc=None
    try:
        rpc=(rpc_factory or SharedAppServer)(find_codex(settings.get('ai',{}).get('executable')),
            Path(core.root/'Codex事务助手'),threading.Event(),5,core.root/'codex-desktop-bridge')
        rpc.request('initialize',{'clientInfo':{'name':'personal_management_reconcile','version':'1'},'capabilities':{'experimentalApi':True}})
        rpc.send({'method':'initialized','params':{}})
        for job in jobs:
            with core.store.connect() as c:
                op=c.execute('SELECT * FROM conversation_operations WHERE job_id=?',(job['id'],)).fetchone()
            if not op or not op['provider_thread_id']:continue
            remote=rpc.request('thread/read',{'threadId':op['provider_thread_id'],'includeTurns':False}).get('thread',{})
            if (remote.get('status') or {}).get('type')!='idle':continue
            with core.store.lock,core.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                current=core._job(c,job['id'])
                if current['status']=='running' and current['generation']==job['generation'] and current['epoch']==epoch==core.store.meta(c,'epoch'):
                    candidate=get_candidate(c,job['id'])
                    if candidate:complete_candidate(core,c,current,candidate)
                    else:
                        value=json.loads(current['input'])
                        context=c.execute('SELECT phase FROM context_operations WHERE id=?',(value.get('context_operation_id'),)).fetchone()
                        if context and context['phase']=='checkpointed':
                            from .context_driver import requeue
                            requeue(core,c,current)
                            c.commit()
                            continue
                        from .conversations import update_job
                        error={'code':'discussion_no_candidate','message':'Codex 这一轮已结束，但未返回可保存候选。原会话保留，可继续补充。'}
                        c.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?",(encode(error),now(),job['id']))
                        update_job(core,c,current,'failed',error)
                        settle(c,job['id'],'reconciled',error)
                c.commit()
    except Exception:
        return  # Unavailable provider cannot prove either success or failure.
    finally:
        if rpc:rpc.close()


def _provider(core):
    import threading
    from .ai import _AppServer,find_codex
    with core.store.connect() as c:settings=core.store.meta(c,'settings')
    rpc=_AppServer(find_codex(settings.get('ai',{}).get('executable')),core.root/'Codex事务助手',threading.Event(),12)
    try:
        rpc.request('initialize',{'clientInfo':{'name':'personal_management_repair','version':'1'},'capabilities':{'experimentalApi':True}})
        rpc.send({'method':'initialized','params':{}})
        return rpc
    except Exception:
        rpc.close();raise


def candidate_threads(core,conversation_id):
    from .codex_project import project_binding
    with core.store.connect() as c:
        if not c.execute('SELECT 1 FROM conversations WHERE id=?',(conversation_id,)).fetchone():
            raise BusinessError('not_found','会话不存在。')
        owned={r[0] for r in c.execute("SELECT provider_thread_id FROM conversation_bindings WHERE state='active' AND conversation_id!=? AND epoch=?",
               (conversation_id,core.store.meta(c,'epoch')))}
    binding=project_binding(core.root/'Codex事务助手')
    if not binding:raise BusinessError('codex_project_incomplete','请先在设置中修复固定项目连接。')
    rpc=_provider(core)
    try:
        response=rpc.request('thread/list',{'projectId':binding['project_id'],'cwd':str(core.root/'Codex事务助手'),
            'limit':100,'archived':False,'sortKey':'updated_at','sortDirection':'desc'})
        items=[{'id':t['id'],'title':t.get('name') or t.get('preview') or '尚未命名的讨论',
                'created_at':t.get('createdAt'),'preview':str(t.get('preview') or '')[:120]}
               for t in response.get('data',[]) if t.get('id') and t['id'] not in owned]
        return {'items':items,'has_more':bool(response.get('nextCursor'))}
    finally:rpc.close()


def prepare_attachment(core,p):
    import os
    from .codex_project import project_binding
    thread_id=p.get('provider_thread_id')
    if not isinstance(thread_id,str) or not 1<=len(thread_id)<=200:
        raise BusinessError('validation','请选择已有的 Codex 任务。')
    binding=project_binding(core.root/'Codex事务助手')
    if not binding:raise BusinessError('codex_project_incomplete','请先修复固定项目连接。')
    rpc=_provider(core)
    try:
        thread=rpc.request('thread/read',{'threadId':thread_id,'includeTurns':False})['thread']
        if (thread.get('id')!=thread_id or thread.get('projectId')!=binding['project_id']
                or os.path.normcase(str(thread.get('cwd'))) != os.path.normcase(str(core.root/'Codex事务助手'))):
            raise BusinessError('conversation_binding_conflict','所选任务不属于此数据空间的固定项目，未修改关联。')
        return {'thread_id':thread_id,'project_path':str(core.root/'Codex事务助手')}
    finally:rpc.close()


def attach(core,c,p,rid,prepared):
    from .conversation_progress import finish
    conversation=c.execute('SELECT * FROM conversations WHERE id=?',(p.get('conversation_id'),)).fetchone()
    if not conversation:raise BusinessError('not_found','会话不存在。')
    if conversation['version']!=p.get('version'):raise BusinessError('conversation_conflict','会话已变化，请重新核对。')
    if conversation['active_job_id']:raise BusinessError('conversation_busy','请等待当前请求结束或明确取消后再恢复关联。')
    thread=prepared['thread_id'];epoch=core.store.meta(c,'epoch');stamp=now()
    owner=c.execute("SELECT conversation_id FROM conversation_bindings WHERE provider_thread_id=? AND epoch=? AND state='active'",(thread,epoch)).fetchone()
    if owner and owner['conversation_id']!=conversation['id']:
        raise BusinessError('conversation_binding_conflict','这个任务已经关联其他事项，未改写归属。')
    candidate=conversation['current_proposal_job_id']
    if candidate:
        c.execute("UPDATE jobs SET status='superseded',generation=generation+1 WHERE id=? AND status='awaiting_review'",(candidate,))
        c.execute("UPDATE conversation_messages SET proposal_state='superseded' WHERE job_id=? AND role='assistant'",(candidate,))
        finish(c,candidate)
    c.execute("UPDATE conversation_bindings SET state='historical',updated_at=? WHERE conversation_id=? AND epoch=?",(stamp,conversation['id'],epoch))
    c.execute("""INSERT INTO conversation_bindings VALUES (?,?,?,?,?,'active','explicit_repair',?,?)
        ON CONFLICT(conversation_id,provider_thread_id,epoch) DO UPDATE SET
        state='active',reason='explicit_repair',updated_at=excluded.updated_at""",
        (conversation['id'],thread,epoch,prepared['project_path'],'desktop_mcp_v3',stamp,stamp))
    c.execute("""UPDATE conversation_operations SET phase='reconciled',updated_at=? WHERE conversation_id=? AND epoch=?
        AND phase NOT IN ('completed','rejected','reconciled')""",(stamp,conversation['id'],epoch))
    c.execute("""UPDATE conversations SET provider_thread_id=?,current_proposal_job_id=NULL,
        recovery='resumed',version=version+1,updated_at=? WHERE id=?""",(thread,stamp,conversation['id']))
    core.store.change(c,rid,'repair_conversation_binding')
    return {'conversation_id':conversation['id'],'provider_thread_id':thread,'reused':True,'new_thread_created':False}
