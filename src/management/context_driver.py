"""Provider stage boundaries and recovery without creating successor tasks."""
from __future__ import annotations
import json
import time
from .storage import encode,now
from .schemas import BusinessError

TRANSIENT={'AI_DISCONNECTED','AI_TIMEOUT','AI_SHARED_NOT_READY','AI_CONVERSATION_BUSY'}


def read_turn(rpc,thread_id,turn_id):
    """Bounded provider history, never includeTurns=True for long conversations."""
    cursor=None
    while True:
        page=rpc.request('thread/turns/list',{'threadId':thread_id,'cursor':cursor,'limit':50,'sortDirection':'desc','itemsView':'notLoaded'})
        for turn in page.get('data',[]):
            if turn.get('id')==turn_id:return turn
        cursor=page.get('nextCursor')
        if not cursor:return None


def compact(rpc,report,progress):
    """Acknowledgement alone is not proof that the context has been compacted."""
    from .ai import AIError
    report('compacting')
    rpc.turn_id=None
    if hasattr(rpc,'_owned_turn_id'):rpc._owned_turn_id=None
    rpc.request('thread/compact/start',{'threadId':rpc.thread_id})
    compacted=False;turn_id=None
    while True:
        event=rpc.next_event();body=event.get('params') or {}
        if body.get('threadId')!=rpc.thread_id:continue
        if event.get('method')=='turn/started':
            turn_id=(body.get('turn') or {}).get('id')
            rpc.turn_id=turn_id
            if turn_id:report('compacting',provider_turn_id=turn_id)
        if event.get('method')=='item/completed' and (body.get('item') or {}).get('type')=='contextCompaction':
            compacted=True
        if event.get('method')=='turn/completed':
            if turn_id and (body.get('turn') or {}).get('id')!=turn_id:continue
            if not compacted or body.get('turn',{}).get('status')!='completed':
                raise AIError('AI_COMPACTION_FAILED','上下文接续尚未确认完成，已保留检查点和原任务。')
            rpc.turn_id=None
            if hasattr(rpc,'_owned_turn_id'):rpc._owned_turn_id=None
            if hasattr(rpc,'_compacting'):rpc._compacting=False
            progress.compact_context()
            rpc.deadline=time.monotonic()+rpc.timeout_seconds
            return


def requeue(core,c,job,*,provider=None,error=None,restart=False):
    current=core._job(c,job['id'])
    if current['epoch']!=job['epoch'] or current['generation']!=job['generation']:
        return False
    if current['status']!='running' and not (restart and current['status'] in {'failed','cancelled'}):
        return False
    value=json.loads(job['input']);operation=value.get('context_operation_id')
    if not operation:return False
    row=c.execute('SELECT * FROM context_operations WHERE id=?',(operation,)).fetchone()
    if not row or row['epoch']!=core.store.meta(c,'epoch'):return False
    known=c.execute('SELECT * FROM conversation_operations WHERE job_id=?',(job['id'],)).fetchone()
    if not known or not known['provider_thread_id']:
        # An unacknowledged creation is never evidence that a second is safe.
        return False
    failures=json.loads(row['checkpoint']).get('connection_retries',0)
    if error and (error.get('code') not in TRANSIENT or failures>=3):return False
    checkpoint=json.loads(row['checkpoint'])
    if error:checkpoint['connection_retries']=failures+1
    phase='checkpointed' if row['phase']=='checkpointed' or not error and not restart else 'reconciling'
    value.update(execution_owner='software',provider_thread_id=known['provider_thread_id'],
        provider_project_path=known['project_path'],provider_contract=known['contract'],
        context_continuation=True,previous_operation={k:known[k] for k in ('job_id','phase','provider_thread_id','provider_turn_id')})
    c.execute('UPDATE jobs SET status=?,input=?,error=?,updated_at=? WHERE id=?',
        ('queued',encode(value),encode(error) if error else None,now(),job['id']))
    c.execute('UPDATE context_operations SET phase=?,checkpoint=?,updated_at=? WHERE id=?',(phase,encode(checkpoint),now(),operation))
    # Keep the original active_job_id and generation. Late writes remain guarded
    # by the original epoch/operation, never a freshly invented conversation.
    c.execute("UPDATE conversation_progress SET phase='resuming',updated_at=? WHERE job_id=?",(now(),job['id']))
    return True


def recover(core,c):
    for row in c.execute("""SELECT j.* FROM jobs j JOIN context_operations o ON o.job_id=j.id
        WHERE j.status='running' AND j.epoch=?""",(core.store.meta(c,'epoch'),)).fetchall():
        if json.loads(row['input']).get('desktop_transport') == 'native_ipc_v1':
            continue
        requeue(core,c,dict(row),restart=True)


def resume(core,c,p,rid):
    job=core._job(c,p['id']);value=json.loads(job['input'])
    if job['status'] not in {'failed','cancelled'} or not value.get('context_operation_id') or job['epoch']!=core.store.meta(c,'epoch'):
        raise BusinessError('job_state','这项操作当前不能从检查点继续。')
    conversation=c.execute('SELECT * FROM conversations WHERE id=?',(value.get('conversation_id'),)).fetchone()
    if conversation and conversation['active_job_id']:raise BusinessError('conversation_busy','该事项还有另一项操作在进行。')
    if not requeue(core,c,job,restart=True):raise BusinessError('delivery_unknown','原任务身份尚未核实，请先恢复原会话关联。')
    c.execute('UPDATE jobs SET generation=generation+1 WHERE id=?',(job['id'],))
    c.execute("UPDATE material_catalog SET status='pending',error=NULL WHERE status='paused' AND key IN (SELECT material_key FROM context_sources WHERE operation_id=?)",(value['context_operation_id'],))
    c.execute("UPDATE context_operations SET checkpoint=json_remove(checkpoint,'$.connection_retries') WHERE id=?",(value['context_operation_id'],))
    if conversation:c.execute('UPDATE conversations SET active_job_id=?,version=version+1 WHERE id=?',(job['id'],conversation['id']))
    core.store.change(c,rid,'resume_context_operation')
    return {'job_id':job['id'],'resumed':True,'same_operation':True}


def operation_handle(core,identifier,action,payload):
    """The same bounded protocol for jobs created through the public API.
    These handles cannot create a second job or turn a read into a business write."""
    from .context_service import _operation,QUERY_NAMES,envelope,validate
    with core.store.connect() as c:
        from .session_coordinator import _check_native_caller
        _check_native_caller(core, c, payload)
        op=_operation(core,c,identifier)
        if op['epoch']!=payload.get('epoch') or not op['job_id']:raise BusinessError('context_scope','作业上下文无效。')
        job=core._job(c,op['job_id'])
        if action=='context':
            params=payload.get('params') or {};name=payload.get('name')
            if params.get('operation_id')!=identifier or name not in (QUERY_NAMES-{'prepare_context'})|{'material_image'}:
                raise BusinessError('context_scope','查询超出当前作业。')
        elif action=='begin':
            value=json.loads(job['input'])
            if payload.get('text','').strip()!=value['prompt'].strip() or job['status']!='running':
                raise BusinessError('context_scope','这条消息不属于当前作业。')
            return {'job_id':job['id'],'generation':job['generation'],'epoch':job['epoch'],
                    'context':envelope(core,c,op),'allowed_commands':value['allowed_commands']}
        elif action!='submit':raise BusinessError('context_query','未注册作业接口。')
    if action=='context':return core.query(name,**params)
    from .ai import validate_proposal
    from .session_coordinator import get_candidate
    from .context_service import digest
    with core.store.lock,core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE');_check_native_caller(core,c,payload);op=_operation(core,c,identifier,writable=True);job=core._job(c,op['job_id'])
        if payload.get('job_id')!=job['id'] or payload.get('generation')!=job['generation'] or job['status']!='running':
            raise BusinessError('stale_proposal','这轮已经失效。')
        fingerprint=digest(payload.get('proposal'))
        prior=c.execute('SELECT candidate_fingerprint FROM conversation_operations WHERE job_id=?',(job['id'],)).fetchone()
        if prior and prior['candidate_fingerprint']==fingerprint:c.commit();return {'received':True,'job_id':job['id'],'reused':True}
        if get_candidate(c,job['id']):raise BusinessError('candidate_conflict','同一作业已经返回另一份候选。')
        candidate=validate_proposal(payload.get('proposal'),set(json.loads(job['input'])['allowed_commands']))
        candidate,proof=validate(core,c,op,candidate)
        candidate.update(_candidate_validated=True,context_validation=proof)
        c.execute("UPDATE context_operations SET phase='ready' WHERE id=?",(identifier,))
        c.execute('UPDATE conversation_operations SET candidate=?,candidate_fingerprint=?,updated_at=? WHERE job_id=?',(encode(candidate),fingerprint,now(),job['id']))
        c.commit()
        return {'received':True,'job_id':job['id'],'applied':False}


def prune_caches(core):
    with core.store.connect() as c:
        c.execute("""UPDATE context_queries SET last_page=NULL,last_cursor=NULL WHERE last_page IS NOT NULL
            AND operation_id IN (SELECT o.id FROM context_operations o LEFT JOIN jobs j ON j.id=o.job_id
              WHERE j.status IN ('completed','applied','superseded','cancelled')
                OR o.job_id IS NULL AND o.updated_at<datetime('now','-2 days'))""")
        c.execute("""DELETE FROM context_reads WHERE detail=0 AND operation_id IN
            (SELECT o.id FROM context_operations o LEFT JOIN jobs j ON j.id=o.job_id
             WHERE j.status IN ('completed','applied','superseded','cancelled')
                OR o.job_id IS NULL AND o.updated_at<datetime('now','-2 days'))
            AND NOT EXISTS(SELECT 1 FROM context_actions a WHERE a.operation_id=context_reads.operation_id AND instr(a.payload,context_reads.entity_id)>0)""")


def upgrade_queued(core,c):
    from .context_service import create
    for row in c.execute("SELECT * FROM jobs WHERE kind='ai' AND status='queued' AND json_extract(input,'$.context_operation_id') IS NULL").fetchall():
        value=json.loads(row['input']);day=value.get('date') or core.today(c)
        envelope=create(core,c,scope={**(value.get('conversation_scope') or {'kind':'general'}),'date':day},
            goal=value['prompt'],job_id=row['id'],sources=list(value.get('source_versions') or {}),selected=value.get('context_ids') or [])
        value.update(context=envelope,context_operation_id=envelope['operation_id'],history={'reader':'query_context','collection':'history','messages':[]},local_images=[])
        if len(value['prompt'].encode('utf-8'))>6000:value['prompt']=envelope['goal']+'（完整原文请继续读取 @goal）'
        c.execute('UPDATE jobs SET input=? WHERE id=?',(encode(value),row['id']))
