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
    from .desktop_dispatch import initialize as initialize_dispatch
    initialize_dispatch(c)
    c.execute("""CREATE TABLE IF NOT EXISTS conversation_operations(
        job_id TEXT PRIMARY KEY REFERENCES jobs(id),conversation_id TEXT NOT NULL,
        epoch TEXT NOT NULL,phase TEXT NOT NULL,provider_thread_id TEXT,provider_turn_id TEXT,
        project_path TEXT,contract TEXT,candidate TEXT,error TEXT,updated_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS conversation_native_requests(
        conversation_id TEXT NOT NULL,epoch TEXT NOT NULL,request_id TEXT NOT NULL,job_id TEXT NOT NULL,
        PRIMARY KEY(conversation_id,epoch,request_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS conversation_native_turns(
        epoch TEXT NOT NULL,provider_thread_id TEXT NOT NULL,provider_turn_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL,job_id TEXT NOT NULL REFERENCES jobs(id),
        PRIMARY KEY(epoch,provider_thread_id,provider_turn_id))""")
    if 'candidate_fingerprint' not in {r[1] for r in c.execute('PRAGMA table_info(conversation_operations)')}:
        c.execute('ALTER TABLE conversation_operations ADD COLUMN candidate_fingerprint TEXT')
    if 'pending_terminal' not in {r[1] for r in c.execute('PRAGMA table_info(conversation_operations)')}:
        c.execute('ALTER TABLE conversation_operations ADD COLUMN pending_terminal INTEGER NOT NULL DEFAULT 0')
    if 'provider_generation' not in {r[1] for r in c.execute('PRAGMA table_info(conversation_operations)')}:
        c.execute('ALTER TABLE conversation_operations ADD COLUMN provider_generation INTEGER')
        # Old operations have no generation stamp. Only matching durable
        # progress proves it; never assign a resumed job's new generation to an
        # old turn. Manual native turns have same-generation progress from begin.
        c.execute('''UPDATE conversation_operations AS o SET provider_generation=(
            SELECT p.generation FROM conversation_progress p JOIN jobs j ON j.id=p.job_id
            WHERE p.job_id=o.job_id AND p.epoch=o.epoch AND j.epoch=o.epoch
              AND p.generation=j.generation AND o.provider_turn_id IS NOT NULL
              AND (p.provider_turn_id=o.provider_turn_id OR
                (p.provider_turn_id IS NULL AND j.status='running'
                 AND json_extract(j.input,'$.execution_owner')='desktop'
                 AND json_extract(j.input,'$.desktop_transport')='native_ipc_v1')))''')
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
        provider_turn_id=coalesce(?,provider_turn_id),
        provider_generation=CASE WHEN ? IS NOT NULL THEN ? ELSE provider_generation END,
        project_path=?,contract=coalesce(?,contract),updated_at=? WHERE job_id=?""",
        (values['phase'],values.get('provider_thread_id'),values.get('provider_turn_id'),
         values.get('provider_turn_id'),job['generation'],project_path,
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
        WHERE j.status='running' AND o.candidate IS NOT NULL AND j.epoch=?
        AND coalesce(json_extract(j.input,'$.desktop_transport'),'')!='native_ipc_v1'""",
        (core.store.meta(c,'epoch'),)).fetchall():
        complete_candidate(core,c,dict(row),get_candidate(c,row['id']))


def recover_pending_native_candidates(core,c):
    """Exception observers survive service restart without becoming dispatches."""
    c.execute("""UPDATE jobs SET status='running' WHERE status='queued' AND epoch=?
        AND json_extract(input,'$.desktop_transport')='native_ipc_v1'
        AND EXISTS(SELECT 1 FROM conversation_operations o WHERE o.job_id=jobs.id
            AND o.epoch=jobs.epoch AND o.provider_generation=jobs.generation
            AND o.pending_terminal=1 AND o.candidate IS NOT NULL)
        AND (json_extract(input,'$.conversation_id') IS NULL OR EXISTS(
            SELECT 1 FROM conversations v WHERE v.active_job_id=jobs.id
              AND v.id=json_extract(jobs.input,'$.conversation_id')))""", (core.store.meta(c,'epoch'),))


def _require_native_turn(payload):
    if not payload.get('_native_provider_thread_id') or not payload.get('_native_provider_turn_id'):
        raise BusinessError('discussion_turn_identity_required',
            '这轮缺少 Codex 提供的真实回合身份。原任务已保留，未创建重复作业或结束仍在处理的回合。')


def _native_turn_job(core, c, payload, conversation):
    """One trusted actual turn cannot become a second job under a new request ID.

    Manual turns have no software dispatch journal, so their durable operation
    identity is equally authoritative. This runs inside BEGIN IMMEDIATE.
    """
    if not payload.get('_native_provider_thread_id'):
        return None  # Explicit legacy shim/local discussion entry.
    _require_native_turn(payload)
    key = (payload['epoch'], payload['_native_provider_thread_id'], payload['_native_provider_turn_id'])
    matches = c.execute("""SELECT job_id FROM conversation_native_turns
        WHERE epoch=? AND provider_thread_id=? AND provider_turn_id=?
        UNION SELECT job_id FROM conversation_operations
        WHERE epoch=? AND provider_thread_id=? AND provider_turn_id=?
        UNION SELECT job_id FROM desktop_dispatches
        WHERE epoch=? AND provider_thread_id=? AND provider_turn_id=?
        AND method='thread-follower-start-turn'""", key + key + key).fetchall()
    if len(matches) > 1:
        raise BusinessError('conversation_binding_conflict', '此真实回合已有冲突的作业记录，未创建或猜测归属。')
    if not matches:
        return None
    original = core._job(c, matches[0]['job_id'])
    if json.loads(original['input']).get('conversation_id') != conversation['id']:
        raise BusinessError('conversation_binding_conflict', '此真实回合属于其他事项，未改写归属。')
    if original['status'] not in {'queued', 'running'} or conversation['active_job_id'] != original['id']:
        raise BusinessError('discussion_turn_finished', '这次桌面调用对应的作业已经结束，未另建重复作业。')
    return original


def _check_native_caller(core, c, payload):
    caller = payload.get('_native_provider_thread_id')
    if caller is None:
        return
    row = c.execute("""SELECT conversation_id FROM conversation_bindings WHERE
        provider_thread_id=? AND epoch=? AND state='active'""", (caller, payload.get('epoch'))).fetchone()
    if not row or row['conversation_id'] != payload.get('conversation_id'):
        raise BusinessError('conversation_binding_stale', '调用会话的关联已改变，未写入旧事项。')
    if not row['conversation_id'].startswith('operation:'):
        current = c.execute('SELECT provider_thread_id FROM conversations WHERE id=?',
                            (row['conversation_id'],)).fetchone()
        if not current or current['provider_thread_id'] != caller:
            raise BusinessError('conversation_binding_stale', '调用会话的关联已改变，未写入旧事项。')


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
            _check_native_caller(core, c, payload)
            row=c.execute('''SELECT o.epoch,json_extract(j.input,'$.conversation_id') conversation_id
                FROM context_operations o JOIN jobs j ON j.id=o.job_id WHERE o.id=?''',(params.get('operation_id'),)).fetchone()
            if not row or row['epoch']!=payload.get('epoch') or row['conversation_id']!=conversation_id:
                raise BusinessError('conversation_binding_conflict','读取操作不属于当前事项。')
        from .discussion_identity import bound_context
        with bound_context(core, payload, params.get('operation_id')):
            return core.query(name,**params)
    # Material extraction runs before the transaction, like ordinary send_message.
    prepared=None
    if action=='begin':
        if payload.get('_native_provider_thread_id'):
            _require_native_turn(payload)
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
            native = c.execute("""SELECT 1 FROM conversation_operations o JOIN jobs j ON j.id=o.job_id
                WHERE o.conversation_id=? AND o.epoch=? AND json_extract(j.input,'$.desktop_transport')='native_ipc_v1'
                LIMIT 1""", (conversation_id, payload.get('epoch'))).fetchone()
            if native:
                _require_native_turn(payload)
        if not active:prepared=core._prepare('send_message',request)
    with core.store.lock, core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            _check_native_caller(core, c, payload)
            if payload.get('epoch') != core.store.meta(c,'epoch'):
                raise BusinessError('epoch_mismatch','数据空间已恢复或切换，请刷新连接。')
            conversation=c.execute('SELECT * FROM conversations WHERE id=?',(conversation_id,)).fetchone()
            if not conversation:raise BusinessError('not_found','业务会话不存在。')
            if action=='begin':
                original = _native_turn_job(core, c, payload, conversation)
                prior=c.execute('SELECT job_id FROM conversation_native_requests WHERE conversation_id=? AND epoch=? AND request_id=?',
                                (conversation_id,payload['epoch'],payload['request_id'])).fetchone()
                active=core._job(c,prior['job_id']) if prior else core._job(c,conversation['active_job_id']) if conversation['active_job_id'] else None
                if prior and active['status'] not in {'queued','running'}:
                    raise BusinessError('discussion_turn_finished','这个请求已经结束，未重复创建；新的用户消息请使用新请求编号。')

                if active and active['status'] in {'queued','running'}:
                    if original and original['id'] != active['id']:
                        raise BusinessError('conversation_binding_conflict', '请求编号与真实回合属于不同作业，未切换归属。')
                    value=json.loads(active['input'])
                    if value.get('desktop_transport') == 'native_ipc_v1':
                        _require_native_turn(payload)
                        bound = c.execute('SELECT provider_turn_id,provider_generation FROM conversation_operations WHERE job_id=?',
                                          (active['id'],)).fetchone()
                        if bound and bound['provider_turn_id'] not in (None, payload['_native_provider_turn_id']):
                            raise BusinessError('conversation_busy', '此事项仍绑定另一真实回合，未用新的调用替换它。')
                        if bound and bound['provider_turn_id'] is not None and bound['provider_generation'] != active['generation']:
                            raise BusinessError('discussion_turn_stale', '原回合属于旧处理代次，不能领取恢复后的操作。')
                    owner=c.execute('SELECT request_id FROM conversation_native_requests WHERE job_id=? LIMIT 1',(active['id'],)).fetchone()
                    if owner and owner['request_id']!=payload['request_id']:
                        raise BusinessError('conversation_busy','这一轮已经开始；相同调用重试请复用原请求编号。')

                    if value['prompt'].strip()!=payload['text'].strip():
                        raise BusinessError('conversation_busy','这个事项正在处理另一条消息，请等待返回。')
                    job=active
                else:
                    if prepared is None:raise BusinessError('conversation_conflict','会话状态已经改变，请重新开始这一轮。')
                    from . import conversations,conversation_progress
                    result=conversations.send(core,c,request,'desktop:'+new_id(),prepared)
                    job=core._job(c,result['job']['id'])
                    value=json.loads(job['input']);value['execution_owner']='desktop'
                    if payload.get('_native_provider_thread_id'):
                        value['desktop_transport'] = 'native_ipc_v1'
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
                if payload.get('_native_provider_turn_id'):
                    # Immutable identity survives operation progress/compaction
                    # replacing its mutable provider_turn_id with a later turn.
                    c.execute('''INSERT OR IGNORE INTO conversation_native_turns
                        (epoch,provider_thread_id,provider_turn_id,conversation_id,job_id) VALUES (?,?,?,?,?)''',
                        (job['epoch'], payload['_native_provider_thread_id'], payload['_native_provider_turn_id'],
                         conversation_id, job['id']))
                    observe(c, job, conversation_id, {'phase': 'waiting_model',
                        'provider_thread_id': payload['_native_provider_thread_id'],
                        'provider_turn_id': payload['_native_provider_turn_id'],
                        'provider_contract': 'desktop_mcp_v3'}, str(core.root/'Codex事务助手'))
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
                if value.get('desktop_transport') == 'native_ipc_v1' or payload.get('_native_provider_thread_id'):
                    from .discussion_identity import check_turn
                    check_turn(core,c,job,payload,active=False,generation=payload.get('generation'))
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
                if value.get('execution_owner')=='desktop' and value.get('desktop_transport') != 'native_ipc_v1':
                    complete_candidate(core,c,job,candidate)
                # Native candidate ACK does not finish its provider turn. Keep
                # running/active until desktop_native_turns verifies exact end.
                c.commit()
                return {'received':True,'job_id':job['id'],'applied':False,
                        'message':'候选已持久保存，等待用户在软件中核对确认；尚未修改业务数据。'}
            raise BusinessError('not_found','未注册的会话接口。')
        except Exception:
            c.rollback();raise


def native_binding(core, provider_thread_id):
    """Resolve the MCP caller supplied by Codex, never a model's scope guess."""
    import uuid
    try:
        if not isinstance(provider_thread_id, str) or str(uuid.UUID(provider_thread_id)) != provider_thread_id:
            raise ValueError()
    except (ValueError, AttributeError):
        raise BusinessError('conversation_caller_invalid', '缺少有效的 Codex 调用会话身份。') from None
    with core.store.connect() as c:
        epoch = core.store.meta(c, 'epoch')
        row = c.execute("""SELECT conversation_id FROM conversation_bindings
            WHERE provider_thread_id=? AND epoch=? AND state='active'""",
            (provider_thread_id, epoch)).fetchone()
        if not row:
            if c.execute('SELECT 1 FROM conversation_bindings WHERE provider_thread_id=? LIMIT 1',
                         (provider_thread_id,)).fetchone():
                raise BusinessError('conversation_binding_stale', '此 Codex 会话的旧关联已失效，请在软件中核对原事项；未开放普通写入。')
            return {'managed': False, 'epoch': epoch}
        ident = row['conversation_id']
        if ident.startswith('operation:'):
            current = c.execute('SELECT id FROM context_operations WHERE id=? AND epoch=?',
                                (ident[10:], epoch)).fetchone()
        else:
            current = c.execute('SELECT id FROM conversations WHERE id=? AND provider_thread_id=?',
                                (ident, provider_thread_id)).fetchone()
        if not current:
            raise BusinessError('conversation_binding_conflict', '当前会话关联需要核对，未改用其他事项。')
        return {'managed': True, 'epoch': epoch, 'conversation_id': ident,
                'provider_thread_id': provider_thread_id}


def handle_native(core, action, payload):
    """Project MCP routing. Caller identity is transport metadata, not tool input."""
    binding = native_binding(core, payload.get('provider_thread_id'))
    if action == 'binding':
        return binding
    if not binding['managed']:
        raise BusinessError('conversation_not_bound', '此 Codex 会话尚未关联软件事项，未猜测或新建关联。')
    params = {key: value for key, value in payload.items()
              if key not in {'provider_thread_id', 'provider_turn_id', 'conversation_id', 'epoch'}
              and not key.startswith('_native_')}
    params.update(conversation_id=binding['conversation_id'], epoch=binding['epoch'],
                  _native_provider_thread_id=binding['provider_thread_id'])
    if payload.get('provider_turn_id') is not None:
        import uuid
        turn = payload['provider_turn_id']
        try:
            if not isinstance(turn, str) or str(uuid.UUID(turn)) != turn:
                raise ValueError()
        except (ValueError, AttributeError):
            raise BusinessError('conversation_caller_invalid', 'Codex 调用轮次无效。') from None
        params['_native_provider_turn_id'] = turn
    if action in {'begin', 'submit'}:
        _require_native_turn(params)
    if action in {'begin', 'submit', 'context'}:
        return handle(core, action, params)
    if action != 'query':
        raise BusinessError('not_found', '未注册的受管讨论接口。')
    name, query = params.get('name'), params.get('params') or {}
    if not isinstance(query, dict):
        raise BusinessError('validation', '受管查询参数无效。')
    instruction = ('这是软件绑定的受管事项。每轮先 begin_discussion 读取当前事实，'
                   '再通过 submit_candidate 返回候选，实际修改须在软件确认。')
    if name == 'state':
        return {**core.query('state'), **binding, 'instructions': instruction}
    if name == 'capabilities':
        from .ai_commands import CANDIDATE_COMMANDS
        return {**binding, 'allowed_commands': sorted(CANDIDATE_COMMANDS),
                'direct_business_writes': False, 'instructions': instruction}
    with core.store.connect() as c:
        ident = binding['conversation_id']
        if ident.startswith('operation:'):
            row = c.execute('SELECT job_id FROM context_operations WHERE id=? AND epoch=?',
                            (ident[10:], binding['epoch'])).fetchone()
            job_id = row['job_id'] if row else None
        else:
            row = c.execute('SELECT active_job_id FROM conversations WHERE id=?', (ident,)).fetchone()
            job_id = row['active_job_id'] if row else None
        job = core._job(c, job_id) if job_id else None
        value = json.loads(job['input']) if job else {}
        operation = value.get('context_operation_id')
        if job and (job['epoch'] != binding['epoch'] or job['status'] not in {'queued', 'running'}):
            operation = None
    if name in {'begin_context', 'prepare_context'}:
        if not operation:
            return {**binding, 'next_tool': 'begin_discussion', 'instructions': instruction}
        from .context_service import _operation, envelope
        with core.store.connect() as c:
            context = envelope(core, c, _operation(core, c, operation))
        return {**binding, 'job_id': job['id'], 'generation': job['generation'],
                'context': context, 'instructions': instruction}
    if not operation:
        raise BusinessError('discussion_not_started', '请先 begin_discussion 开始本事项的当前回合。')
    allowed = {'get', 'list', 'source_content', 'sources', 'daily_review', 'weekly_review',
               'plan_context', 'daily_tasks', 'recovery_summary', 'object_workspace',
               'workspace_tasks', 'timetables', 'timetable_week', 'receipt'}
    if name not in allowed:
        raise BusinessError('context_query', '此受管事项没有开放该查询。')
    if name == 'get':
        return handle(core, 'context', {**params, 'name': 'read_context_item',
            'params': {'operation_id': operation, 'id': query.get('id')}})
    if name == 'receipt':
        return handle(core, 'context', {**params, 'name': 'read_context_item',
            'params': {'operation_id': operation, 'id': 'receipt:' + str(query.get('request_id', ''))}})
    return {**binding, 'operation_id': operation, 'reader': 'query_context',
            'collection': {'sources': 'materials', 'list': 'records', 'daily_tasks': 'tasks',
                           'workspace_tasks': 'tasks'}.get(name, 'records'),
            'instruction': '按本事项统一分页读取；计划、课表和复盘见 @plan_request、@schedule、@daily_review。'}


def check_native_turns(core, rpc_factory=None):
    """Reconcile finished desktop turns that never returned a candidate.
    Read-only provider requests only. Network uncertainty remains pending.
    """
    import threading
    from pathlib import Path
    from .ai import find_codex
    from .ai_shared import SharedAppServer
    with core.store.connect() as c:
        jobs=[dict(r) for r in c.execute("""SELECT j.* FROM jobs j
            LEFT JOIN conversations v ON v.active_job_id=j.id
            LEFT JOIN conversation_operations o ON o.job_id=j.id
            WHERE j.status='running' AND (
                (json_extract(j.input,'$.execution_owner')='desktop' AND v.id IS NOT NULL)
                OR (json_extract(j.input,'$.desktop_transport')='native_ipc_v1'
                    AND o.pending_terminal=1 AND o.candidate IS NOT NULL
                    AND (v.id IS NOT NULL OR json_extract(j.input,'$.conversation_id') IS NULL))) LIMIT 4""")]
        settings=core.store.meta(c,'settings');epoch=core.store.meta(c,'epoch')
    if not jobs:return
    native_jobs = [job for job in jobs if json.loads(job['input']).get('desktop_transport') == 'native_ipc_v1']
    if native_jobs:
        from .desktop_native_turns import check_native_turns as check_ordinary_desktop
        check_ordinary_desktop(core, native_jobs)
        jobs = [job for job in jobs if job not in native_jobs]
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
    from .desktop_seed import PlainAppServer
    rpc=PlainAppServer(core.root/'Codex事务助手',timeout=12)
    try:
        rpc.__enter__()
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
