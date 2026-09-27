"""Bounded, resumable context reads shared by the GUI, MCP and job runners.

Business writes still go through Core. Operational reads record versions and
coverage but never advance the business revision or keep a read transaction open.
"""
from __future__ import annotations
import copy
import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Callable
from .schemas import BusinessError
from .storage import encode, new_id, now

VERSION = 1
PAGE_BYTES = 12 * 1024
MAX_PAGE_BYTES = 16 * 1024
ENVELOPE_BYTES = 24 * 1024
STAGE_BYTES = 64 * 1024
RESERVE_BYTES = 8 * 1024
QUERY_NAMES = {'prepare_context', 'query_context', 'read_context_item', 'read_material',
               'describe_action', 'context_coverage', 'operation_status',
               'next_context_step', 'checkpoint_context', 'validate_candidate','refresh_context'}
ACTIVE = "e.archived=0 AND e.status NOT IN ('cancelled','draft')"


def size(value):
    return len(encode(value).encode('utf-8'))


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def _fragment(text, offset, budget):
    """Character cursor; actual escaped UTF-8 JSON determines the byte budget."""
    if type(offset) is not int or offset < 0 or offset > len(text):
        raise BusinessError('context_cursor', '读取位置无效，请使用上一页返回的位置。')
    lo, hi = 0, min(len(text)-offset, budget)
    while lo < hi:
        mid = (lo+hi+1)//2
        if size(text[offset:offset+mid]) <= budget: lo = mid
        else: hi = mid-1
    if lo == 0 and offset < len(text):
        raise BusinessError('context_budget', '当前页预算不足，请先保存检查点。')
    end = offset+lo
    return text[offset:end], end if end < len(text) else None


@dataclass(frozen=True)
class Collection:
    label: str
    types: tuple[str, ...] = ()
    predicate: Callable | None = None
    global_scope: bool = False


COLLECTIONS = {
    'records': Collection('范围内记录'),
    'selected': Collection('用户指定记录',global_scope=True),
    'tasks': Collection('可执行任务', ('task',)),
    'deadlines': Collection('全部临近及逾期截止事项', ('task','milestone','assessment','event'), global_scope=True),
    'rules': Collection('生效规则', ('rule',), global_scope=True),
    'events': Collection('固定与临近日程', ('event',), global_scope=True),
    'materials': Collection('已选资料', ('asset','artifact')),
    'feedback': Collection('执行事实', ('feedback',), global_scope=True),
    'inbox': Collection('未整理信息', ('inbox',), global_scope=True),
    'courses': Collection('课程与项目', ('course','project','domain'), global_scope=True),
    'plans': Collection('当日计划', ('plan',), global_scope=True),
}


def register_collection(name, *, label, types=(), predicate=None, global_scope=False):
    """Trusted code extensions supply a parameterized SQL predicate, never users."""
    if not name.isidentifier() or name in COLLECTIONS:
        raise ValueError('Duplicate or invalid context collection')
    COLLECTIONS[name] = Collection(label, tuple(types), predicate, global_scope)


def _operation(core, c, identifier, *, writable=False):
    row = c.execute('SELECT * FROM context_operations WHERE id=?', (identifier,)).fetchone()
    if not row: raise BusinessError('context_not_found', '读取操作不存在，请重新取得上下文入口。')
    op = dict(row); op['scope'] = json.loads(op['scope']); op['checkpoint'] = json.loads(op['checkpoint'])
    if op['epoch'] != core.store.meta(c, 'epoch'):
        raise BusinessError('epoch_mismatch', '数据空间已切换，旧读取引用不能继续使用。')
    from .discussion_identity import check_context
    check_context(core, c, op)
    if writable and op['phase']=='ready':raise BusinessError('context_finished','候选已经封存，不能再修改其批次。')
    if writable and op['job_id']:
        job = core._job(c, op['job_id'])
        if job['status'] not in {'queued', 'running'}:
            raise BusinessError('context_finished', '本轮已结束或取消，不能再写入检查点。')
    return op


def _where(core, c, op, name, params):
    if name not in COLLECTIONS: raise BusinessError('context_collection', '没有注册这个读取集合。')
    if set(params)-{'type','status','search','parent_id','date_from','date_to'}:
        raise BusinessError('context_filter', '查询只接受类型、状态、归属、日期和文字筛选。')
    spec = COLLECTIONS[name]; conditions = [ACTIVE]; args = []; day = op['scope'].get('date') or core.today(c)
    if spec.types:
        conditions.append('e.type IN ('+','.join('?' for _ in spec.types)+')'); args.extend(spec.types)
    if name=='inbox':conditions.append("e.status NOT IN ('done','failed')")
    if name in {'tasks','deadlines'}:conditions.append("e.status!='failed' AND (e.type='task' OR e.status!='done')")
    if name == 'deadlines':
        end = (dt.date.fromisoformat(day)+dt.timedelta(days=14)).isoformat()
        conditions.append("""((e.type!='event' AND nullif(json_extract(e.data,'$.due_date'),'')<=?)
            OR (e.type='event' AND coalesce(json_extract(e.data,'$.recurrence'),'none')='none'
              AND json_extract(e.data,'$.date') BETWEEN ? AND ?))""")
        args.extend([end,day,end])
    if name=='events' and op['scope'].get('kind')=='timetable':
        conditions.append("json_extract(e.data,'$.timetable_id')=?");args.append(op['scope'].get('entity_id'))
    if name == 'events' and op['scope'].get('kind')!='timetable':
        end=(dt.date.fromisoformat(day)+dt.timedelta(days=14)).isoformat()
        before=(dt.date.fromisoformat(day)-dt.timedelta(days=1)).isoformat()
        conditions.append("""(nullif(json_extract(e.data,'$.date'),'') IS NULL OR
            (coalesce(json_extract(e.data,'$.recurrence'),'none')='none' AND json_extract(e.data,'$.date') BETWEEN ? AND ?)
            OR (coalesce(json_extract(e.data,'$.recurrence'),'none')!='none' AND json_extract(e.data,'$.date')<=?
              AND coalesce(nullif(json_extract(e.data,'$.until'),''),?)>=?))""")
        args.extend([before,end,end,day,before])
    if name == 'rules':
        conditions.append("""coalesce(json_extract(e.data,'$.enabled'),1)!=0
            AND coalesce(nullif(json_extract(e.data,'$.effective_from'),''),?)<=?
            AND coalesce(nullif(json_extract(e.data,'$.effective_until'),''),?)>=?
            AND e.status!='done'"""); args.extend([day]*4)
    if name == 'plans':
        conditions.append("json_extract(e.data,'$.date')=?"); args.append(day)
    if name == 'feedback':
        conditions.append("json_extract(e.data,'$.business_date')=?"); args.append(day)
    has_completion=bool(c.execute("SELECT 1 FROM entities WHERE type='feedback' AND archived=0 AND json_type(data,'$.dimensions.completion') IS NOT NULL LIMIT 1").fetchone())
    if name in {'tasks','deadlines'} and not has_completion:conditions.append("e.status!='done'")
    if name in {'tasks','deadlines'} and c.execute("SELECT 1 FROM entities WHERE type='feedback' AND archived=0 AND json_type(data,'$.dimensions.completion') IS NOT NULL LIMIT 1").fetchone():
        from .task_views import rows_sql
        completion=rows_sql('e.id=__outer_id__',day).replace('e.','context_task.').replace('entities e ','entities context_task ').replace('__outer_id__','e.id')
        conditions.append("(e.type!='task' OR NOT EXISTS(SELECT 1 FROM ("+completion+") WHERE completion_state='done'))")
    if name == 'selected':
        conditions.append('e.id IN (SELECT entity_id FROM context_selection WHERE operation_id=?)');args.append(op['id'])
    if name == 'materials':
        if op['scope'].get('source_owner_scope'):
            from .sources import scope_source_sql
            conditions.append(scope_source_sql());args.extend([op['scope']['source_owner_scope']]*4)
        else:
            conditions.append('e.id IN (SELECT entity_id FROM context_sources WHERE operation_id=?)'); args.append(op['id'])
    owner = params.get('parent_id') or (None if spec.global_scope or name=='materials' else op['scope'].get('entity_id'))
    if owner:
        conditions.append("""(e.id IN (WITH RECURSIVE tree(id) AS
            (SELECT ? UNION SELECT x.id FROM entities x JOIN tree t ON x.parent_id=t.id WHERE x.archived=0)
            SELECT id FROM tree) OR json_extract(e.data,'$.owner_id')=? OR json_extract(e.data,'$.source_owner_id')=?)""")
        args.extend([owner,owner,owner])
    for key in ('type','status'):
        if params.get(key): conditions.append('e.'+key+'=?'); args.append(params[key])
    if params.get('search'):
        if not isinstance(params['search'],str) or len(params['search'])>300: raise BusinessError('context_filter','查询文字过长。')
        conditions.append("(instr(lower(e.title),lower(?))>0 OR e.id=? OR instr(lower(coalesce(json_extract(e.data,'$.code'),'')),lower(?))>0)"); args.extend([params['search']]*3)
    for key, symbol in [('date_from','>='),('date_to','<=')]:
        if params.get(key):
            dt.date.fromisoformat(params[key])
            conditions.append("coalesce(json_extract(e.data,'$.due_date'),json_extract(e.data,'$.date'))"+symbol+'?'); args.append(params[key])
    if spec.predicate:
        sql, values = spec.predicate(op['scope'], params); conditions.append('('+sql+')'); args.extend(values)
    return ' AND '.join(conditions), args


def _stamp(c, name):
    spec = COLLECTIONS[name]
    keys = ['type:'+t for t in spec.types] if spec.types else ['entities']
    keys += ['links']
    return encode({key:(c.execute('SELECT version FROM context_generations WHERE key=?',(key,)).fetchone() or [0])[0] for key in keys})


def _count(core,c,op,name,params=None):
    where,args = _where(core,c,op,name,params or {})
    return c.execute('SELECT count(*) FROM entities e WHERE '+where,args).fetchone()[0]


def create(core,c,*,scope,goal,job_id=None,sources=(),selected=(),identifier=None):
    if not isinstance(goal,str) or not goal.strip(): raise BusinessError('validation','请提供本次目标。')
    if not isinstance(scope,dict) or set(scope)-{'kind','date','entity_id','conversation_id','source_owner_scope','analyze_materials'}: raise BusinessError('context_scope','范围格式无效。')
    scope = dict(scope)
    if scope.get('conversation_id'):
        row=c.execute('SELECT scope FROM conversations WHERE id=?',(scope['conversation_id'],)).fetchone()
        if not row:raise BusinessError('context_scope','所选会话不存在。')
        scope={**json.loads(row[0]),**scope}
    scope.setdefault('date',core.today(c))
    scope['_timezone']=core.store.meta(c,'settings')['timezone']
    dt.date.fromisoformat(scope['date'])
    if scope.get('entity_id'): core.store.get(c,scope['entity_id'])
    identifier = identifier or new_id(); stamp=now()
    c.execute("""INSERT INTO context_operations(id,epoch,scope,goal,job_id,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?)""",(identifier,core.store.meta(c,'epoch'),encode(scope),goal,job_id,stamp,stamp))
    scope.setdefault('analyze_materials',bool(sources or selected))
    # Persist the default in the operational scope too.
    c.execute('UPDATE context_operations SET scope=? WHERE id=?',(encode(scope),identifier))
    source_set=dict.fromkeys(sources)
    if not isinstance(selected,(list,tuple)):raise BusinessError('context_selection','所选记录需要编号列表。')
    for selected_id in dict.fromkeys(selected):
        entity=core.store.get(c,selected_id)
        c.execute('INSERT INTO context_selection VALUES (?,?,?)',(identifier,entity['id'],entity['version']))
        if entity['type'] in {'asset','artifact'}:source_set[entity['id']]=None
    for source_id in source_set:
        entity=core.store.get(c,source_id)
        if entity['archived'] or entity['type'] not in {'asset','artifact'}: raise BusinessError('source_unavailable','资料未保存为可用软件副本。')
        c.execute('INSERT OR IGNORE INTO context_sources(operation_id,entity_id,version) VALUES (?,?,?)',(identifier,entity['id'],entity['version']))
    if scope.get('source_owner_scope') and scope['analyze_materials']:
        from .sources import scope_source_sql
        owner=scope['source_owner_scope']
        c.execute("INSERT OR IGNORE INTO context_sources(operation_id,entity_id,version) SELECT ?,e.id,e.version FROM entities e WHERE e.type IN ('asset','artifact') AND e.archived=0 AND "+scope_source_sql(),[identifier,*([owner]*4)])
    result=envelope(core,c,_operation(core,c,identifier))
    c.execute('UPDATE context_operations SET delivered_bytes=? WHERE id=?',(size(result),identifier))
    return result


def envelope(core,c,op):
    goal, following = _fragment(op['goal'],0,3000)
    result = {'protocol':'context/1','operation_id':op['id'],'epoch':op['epoch'],'scope':op['scope'],
        'goal':goal,'goal_next_offset':following,'goal_reader':{'id':'@goal'},
        'collections':[{'name':name,'label':spec.label,'total':_count(core,c,op,name)} for name,spec in COLLECTIONS.items()],
        'special_items':['@plan_request','@daily_review','@schedule','@capabilities','@history','@goal'],
        'readers':sorted(QUERY_NAMES-{'prepare_context'}),'page_bytes':PAGE_BYTES,
        'coverage':'Indexes only; no record body or material has been read by the model.',
        'stage':op['stage'],'checkpoint':op['checkpoint'],
        'instructions':'先查询与目标相关的记录，按 next_cursor 继续；详情用 read_context_item。计划必须核对完整 deadlines、rules、events 和当前计划；入选事项的 dependencies 入口提供前置、后置及实际完成状态，须完整读取。资料用 next_context_step 自动逐批读取并 checkpoint_context；检查点不修改业务。'}
    if size(result)>ENVELOPE_BYTES: raise BusinessError('context_envelope','上下文索引扩展过大，请检查已注册集合。')
    return result


def _summary(row):
    e=dict(row); data=json.loads(e.pop('data')); e.pop('created_at',None); e.pop('updated_at',None);e.pop('archived',None)
    e['data']={k:data[k] for k in ('code','due_date','date','scheduled_date','priority','owner_id','task_kind',
        'earliest_start','start','end','recurrence','time_kind','until','enabled','rule_kind') if data.get(k) is not None}
    e['details']={'reader':'read_context_item','id':e['id'],'version':e['version']}
    from .context_dependencies import PLAN_TYPES, reference
    if e['type'] in PLAN_TYPES:e['dependencies']=reference(e['id'])
    return e


def _budget(op, requested):
    requested=max(2048,min(MAX_PAGE_BYTES,int(requested or PAGE_BYTES)))
    remaining=STAGE_BYTES-op['delivered_bytes']-RESERVE_BYTES
    if remaining<2048 or op['phase'] in {'checkpointed','budget_stop'} or op['tool_calls']>=128:
        raise BusinessError('context_checkpoint_required','本阶段读取预算将满。请保存检查点并结束本轮，软件会在原任务压缩上下文后自动接续。',
                            {'operation_id':op['id'],'stage':op['stage'],'remaining_bytes':max(0,remaining)})
    return min(requested,remaining)


def _charge(c,op,result):
    amount=size(result)
    c.execute('UPDATE context_operations SET delivered_bytes=delivered_bytes+?,updated_at=? WHERE id=?',(amount,now(),op['id']))
    return result


def _signature(core,c,op,name,params):
    where,args=_where(core,c,op,name,params)
    h=hashlib.sha256()
    for row in c.execute('SELECT e.id,e.version FROM entities e INDEXED BY sqlite_autoindex_entities_1 WHERE '+where+' ORDER BY e.id',args):
        h.update(encode(list(row)).encode('utf-8'))
    return h.hexdigest()


def _fresh_query(core,c,op,query):
    stamp=_stamp(c,query['collection'])
    if query['stamp']==stamp:return True
    current=_signature(core,c,op,query['collection'],json.loads(query['params']))
    if query['signature']==current:
        c.execute('UPDATE context_queries SET stamp=? WHERE id=?',(stamp,query['id']))
        return True
    return False


def page(core,c,op,collection,params=None,cursor=None,limit=80,byte_limit=None):
    if cursor and not params:
        prior=c.execute('SELECT params FROM context_queries WHERE id=? AND operation_id=? AND collection=?',(str(cursor).split(':',1)[0],op['id'],collection)).fetchone()
        if prior:params=json.loads(prior['params'])
    params=params or {}; where,args=_where(core,c,op,collection,params); stamp=_stamp(c,collection)
    query=c.execute('SELECT * FROM context_queries WHERE operation_id=? AND collection=? AND params=?',(op['id'],collection,encode(params))).fetchone()
    if query and query['stamp']!=stamp and _fresh_query(core,c,op,query):
        query=c.execute('SELECT * FROM context_queries WHERE id=?',(query['id'],)).fetchone()
    if query and query['stamp']==stamp and query['last_page'] and (cursor or '')==query['last_cursor']:
        result=json.loads(query['last_page'])
        if size(result)>_budget(op,byte_limit):raise BusinessError('context_checkpoint_required','请保存检查点后重试原游标。')
        return _charge(c,op,result)
    if cursor and query and query['stamp']!=stamp:raise BusinessError('context_changed','这个集合已变化，请重新从第一页读取。',{'collection':collection})
    if cursor:
        if not query or cursor!=query['id']+':'+query['after_key']: raise BusinessError('context_cursor','游标不属于当前顺序读取位置。')
        if query['stamp']!=stamp: raise BusinessError('context_changed','这个集合在读取期间发生变化，请从该集合第一页重新读取。',{'collection':collection})
    elif not query or query['stamp']!=stamp:
        if query: c.execute('DELETE FROM context_queries WHERE id=?',(query['id'],))
        identifier=new_id(); total=_count(core,c,op,collection,params)
        c.execute('INSERT INTO context_queries(id,operation_id,collection,params,stamp,total,signature) VALUES (?,?,?,?,?,?,?)',(identifier,op['id'],collection,encode(params),stamp,total,_signature(core,c,op,collection,params)))
        query=c.execute('SELECT * FROM context_queries WHERE id=?',(identifier,)).fetchone()
    elif query['after_key']:
        # Explicit first-page request replays the first page, without falsely
        # advancing sequential coverage or losing a durable later cursor.
        cursor=None
    maximum=_budget(op,byte_limit); after=query['after_key'] if cursor else ''
    rows=c.execute('SELECT e.* FROM entities e INDEXED BY sqlite_autoindex_entities_1 WHERE '+where+' AND e.id>? ORDER BY e.id LIMIT ?',[*args,after,min(200,max(1,int(limit)))+1])
    items=[];last=after; exhausted=True
    for row in rows:
        item=_summary(row)
        detail=0
        if row['type']=='rule':
            item['data']=json.loads(row['data']);item['body_complete']=True;detail=2
        if row['type']=='task':
            from .task_views import state as task_state
            item['completion_state']=task_state(core,c,row['id'],op['scope']['date'])
        if size(item)>maximum-1600: detail=0;item={k:row[k] for k in ('id','type','version')}|{'details':{'reader':'read_context_item','id':row['id']},'large_item':True}
        trial={'items':items+[item],'query_id':query['id'],'total':query['total'],'next_cursor':query['id']+':'+row['id'],'coverage_complete':False}
        if len(items)>=min(200,max(1,int(limit))) or size(trial)>maximum-512: exhausted=False;break
        items.append(item);last=row['id']
    sequential=after==query['after_key']
    delivered=query['delivered']+len(items) if sequential else query['delivered']
    complete=exhausted and sequential and delivered==query['total']
    if sequential:
        c.execute('UPDATE context_queries SET after_key=?,delivered=?,complete=? WHERE id=?',(last,delivered,int(complete),query['id']))
    result={'items':items,'query_id':query['id'],'total':query['total'],
        'next_cursor':None if exhausted else query['id']+':'+last,'coverage_complete':bool(complete or query['complete']),
        'details_complete':False,'records_delivered':delivered}
    c.execute('UPDATE context_queries SET last_cursor=?,last_page=? WHERE id=?', (cursor or '',encode(result),query['id']))
    c.executemany('INSERT INTO context_reads(operation_id,entity_id,version,detail) VALUES (?,?,?,?) ON CONFLICT(operation_id,entity_id) DO UPDATE SET detail=CASE WHEN version=excluded.version THEN max(detail,excluded.detail) ELSE excluded.detail END,version=excluded.version',
        [(op['id'],item['id'],item['version'],2 if item.get('body_complete') else 0) for item in items])
    return _charge(c,op,result)


def plan_request(core,c,day):
    from .reviews import _latest_plan
    from .plan_assistance import GUIDANCE
    from zoneinfo import ZoneInfo
    current=_latest_plan(core,c,day)
    local=dt.datetime.now(ZoneInfo(core.store.meta(c,'settings')['timezone'])); protected=[]
    if current:
        for block in current['data'].get('blocks',[]):
            reported=c.execute("SELECT 1 FROM entities WHERE type='feedback' AND archived=0 AND json_extract(data,'$.target_id')=? AND json_extract(data,'$.business_date')=? LIMIT 1",(block['target_id'],day)).fetchone()
            past=day<local.date().isoformat() or day==local.date().isoformat() and block.get('end','99:99')<=local.strftime('%H:%M')
            if reported or past: protected.append(block)
    return {'date':day,'guidance':GUIDANCE,'local_now':local.isoformat(timespec='minutes'),
            'existing_plan':current,'protected_blocks':protected}


def _special(core,c,op,identifier):
    day=op['scope']['date']
    if identifier=='@goal': return op['goal']
    if identifier=='@skill':
        from .skill_workflows import skill_context
        value=json.loads(core._job(c,op['job_id'])['input']) if op['job_id'] else {}
        if not value.get('skill_id'):raise BusinessError('skill_missing','本轮未选择整理技能。')
        return skill_context(value['skill_id'])
    if identifier=='@plan_request': return plan_request(core,c,day)
    if identifier=='@daily_review':
        from .reviews import query_daily
        return query_daily(core,c,{'date':day})
    if identifier=='@schedule':
        events,unknowns=core._events(c,day)
        return {'events':events,'unknowns':unknowns,'rules':core._rules(c,day)}
    if identifier=='@capabilities': return core.types(c)
    if identifier=='@history':
        # Each historical message has its own reader; no 12-message final horizon.
        return {'collection':'history','instruction':'query_context(collection=history) 后按消息 ID 读取正文。'}
    raise BusinessError('context_item','这个特殊记录未注册。')


def read_item(core,c,op,identifier,offset=0,version=None,byte_limit=None):
    from .context_dependencies import PREFIX, PLAN_TYPES, reference, snapshot
    dependency_reader=None
    if identifier.startswith(PREFIX):
        value=snapshot(core,c,identifier[len(PREFIX):],op['scope']['date']);current=digest(value)
    elif identifier.startswith('receipt:'):
        value=core._query(c,'receipt',{'request_id':identifier[8:]});current=digest(value)
    elif identifier.startswith('@'):
        value=_special(core,c,op,identifier);current=digest(value)
    elif identifier.startswith(('message:','job:')):
        conversation=op['scope'].get('conversation_id')
        if op['job_id']:conversation=json.loads(core._job(c,op['job_id'])['input']).get('conversation_id')
        if identifier.startswith('message:'):
            row=c.execute('SELECT * FROM conversation_messages WHERE id=? AND conversation_id=?',(identifier[8:],conversation)).fetchone()
            if not row:raise BusinessError('context_item','这条历史消息不属于所选事项。')
            value=dict(row)
            if value.get('job_id'):value['candidate_ref']='job:'+value['job_id']
        else:
            job=core._job(c,identifier[4:]);value={**job,'input':json.loads(job['input']),'result':json.loads(job['result']) if job['result'] else None}
            if conversation and value['input'].get('conversation_id')!=conversation:raise BusinessError('context_item','这个处理结果不属于所选事项。')
        current=digest(value)
    else:
        value=core.store.get(c,identifier);current=value['version']
        if value['type'] in PLAN_TYPES:dependency_reader=reference(identifier)
    if version is not None and version!=current: raise BusinessError('context_changed','该记录在分段读取期间已改变，请重新读取这条记录。')
    budget=_budget(op,byte_limit)
    if not offset and size({'item':value,'version':current})<budget-512:
        result={'id':identifier,'item':value,'version':current,'next_offset':None,'complete':True}
    else:
        text=value if isinstance(value,str) else encode(value)
        fragment,next_offset=_fragment(text,offset,budget-1024)
        result={'id':identifier,'json_fragment':fragment,'format':'text' if isinstance(value,str) else 'json',
                'offset':offset,'next_offset':next_offset,'version':current,'complete':next_offset is None}
    if dependency_reader:result['dependencies']=dependency_reader
    old=c.execute('SELECT * FROM context_parts WHERE operation_id=? AND item_id=?',(op['id'],identifier)).fetchone()
    text=value if isinstance(value,str) else encode(value)
    until=result.get('next_offset') or len(text)
    if not offset or old and old['version']==str(current) and offset<=old['read_until']:
        read_until=max(until,old['read_until'] if old and old['version']==str(current) else 0)
        c.execute('INSERT INTO context_parts VALUES (?,?,?,?,?) ON CONFLICT(operation_id,item_id) DO UPDATE SET version=excluded.version,read_until=excluded.read_until,complete=excluded.complete',
            (op['id'],identifier,str(current),read_until,int(read_until==len(text))))
    if not identifier.startswith(('@','message:','job:','receipt:',PREFIX)):
        c.execute('INSERT INTO context_reads VALUES (?,?,?,?) ON CONFLICT(operation_id,entity_id) DO UPDATE SET detail=CASE WHEN version=excluded.version THEN max(detail,excluded.detail) ELSE excluded.detail END,version=excluded.version',(op['id'],identifier,current,2 if result['complete'] else 1))
    return _charge(c,op,result)


def coverage(core,c,op,query_cursor=''):
    queries=[dict(row) for row in c.execute('SELECT id,collection,total,delivered,complete,after_key FROM context_queries WHERE operation_id=? AND id>? ORDER BY id LIMIT 25',(op['id'],query_cursor or ''))]
    next_query_cursor=queries[23]['id'] if len(queries)>24 else None
    queries=queries[:24]
    for query in queries:
        query['next_cursor']=None if query['complete'] else query['id']+':'+query['after_key']
    steps={row['status']:row['n'] for row in c.execute('SELECT status,count(*) n FROM context_steps WHERE operation_id=? GROUP BY status',(op['id'],))}
    sources=c.execute('SELECT count(*) FROM context_sources WHERE operation_id=?',(op['id'],)).fetchone()[0]
    return {'operation_id':op['id'],'phase':op['phase'],'stage':op['stage'],'scope':op['scope'],
        'delivered_bytes':op['delivered_bytes'],'stage_budget_bytes':STAGE_BYTES,
        'collections':queries,'next_query_cursor':next_query_cursor,'tool_calls':op['tool_calls'],'materials':{'sources':sources,'steps':steps},
        'checkpoint':op['checkpoint'],'validation':json.loads(op['validation']) if op['validation'] else None}


def history_page(core,c,op,cursor=None,byte_limit=None):
    budget=_budget(op,byte_limit);after=int(cursor or 0);items=[];next_cursor=None
    conversation=op['scope'].get('conversation_id')
    if op['job_id']:conversation=json.loads(core._job(c,op['job_id'])['input']).get('conversation_id')
    rows=c.execute("""SELECT m.id,m.seq,m.role,substr(m.text,1,180) AS excerpt,m.state,m.proposal_state
        FROM conversation_messages m WHERE m.conversation_id=? AND m.seq>? ORDER BY m.seq LIMIT 81""",(conversation,after))
    for row in rows:
        item=dict(row);item['id']='message:'+item['id']
        if size({'items':items+[item]})>budget-1024 or len(items)>=80:
            next_cursor=str(items[-1]['seq']);break
        items.append(item)
    return _charge(c,op,{'items':items,'next_cursor':next_cursor,'coverage_complete':next_cursor is None,'details_complete':False})


def validate(core,c,op,candidate,*,require_coverage=True):
    """Complete local checks, inside the same transaction as candidate adoption."""
    from .ai_commands import CANDIDATE_COMMANDS
    if op['scope'].get('_timezone') and op['scope']['_timezone']!=core.store.meta(c,'settings')['timezone']:
        raise BusinessError('context_changed','使用时区已改变，请刷新本操作后重新核对日期与时段。')
    actions=candidate.get('actions',[])
    if not isinstance(actions,list) or len(actions)>30: raise BusinessError('proposal_scope','候选格式无效。')
    value=json.loads(core._job(c,op['job_id'])['input']) if op['job_id'] else {}
    if value.get('plan_requested') and not candidate.get('action_manifest'):
        from .plan_assistance import normalize
        candidate=normalize(core,c,core._job(c,op['job_id']),candidate)
    from .context_candidates import stage,iter_actions
    candidate=stage(c,op,candidate)
    action_count=candidate['actions_total']
    changed=[]
    # Only changed evidence actually used by the candidate blocks adoption.
    # Unrelated business revisions never force a whole-operation replay.
    for row in c.execute("""SELECT r.entity_id FROM context_reads r LEFT JOIN entities e ON e.id=r.entity_id
        WHERE r.operation_id=? AND (e.id IS NULL OR e.version!=r.version)
        AND (r.detail>0 OR EXISTS(SELECT 1 FROM context_actions a WHERE a.operation_id=r.operation_id AND instr(a.payload,r.entity_id)>0)) LIMIT 20""",(op['id'],)):
        changed.append(row[0])
    for row in c.execute("""SELECT s.entity_id FROM context_sources s LEFT JOIN entities e ON e.id=s.entity_id
        WHERE s.operation_id=? AND (e.id IS NULL OR e.archived=1 OR e.version!=s.version)
        AND (? OR EXISTS(SELECT 1 FROM context_reads r WHERE r.operation_id=s.operation_id AND r.entity_id=s.entity_id)
            OR EXISTS(SELECT 1 FROM context_steps t WHERE t.operation_id=s.operation_id AND t.source_id=s.entity_id AND t.status IN ('delivered','completed'))) LIMIT 20""",(op['id'],int(op['scope'].get('analyze_materials',True)))):
        changed.append(row[0])
    unread=c.execute("""SELECT r.entity_id FROM context_reads r JOIN context_parts p ON p.operation_id=r.operation_id AND p.item_id=r.entity_id
        WHERE r.operation_id=? AND r.detail=1 AND p.complete=0
        AND EXISTS(SELECT 1 FROM context_actions a WHERE a.operation_id=r.operation_id AND instr(a.payload,r.entity_id)>0) LIMIT 1""",(op['id'],)).fetchone()
    if unread:raise BusinessError('context_coverage','候选使用的长记录尚未读完，请继续读取。',{'id':unread[0]})
    if changed: raise BusinessError('context_changed','已读取的相关记录发生变化，请在原操作重新读取并修订。',{'ids':changed})
    if size(op['goal'])>3000:
        read=c.execute("SELECT complete,version FROM context_parts WHERE operation_id=? AND item_id='@goal'",(op['id'],)).fetchone()
        if not read or not read['complete'] or read['version']!=digest(op['goal']):
            raise BusinessError('context_coverage','长请求原文尚未完整读取，请继续读取 @goal。')
    is_plan=bool(c.execute("SELECT 1 FROM context_actions WHERE operation_id=? AND command IN ('create_plan','revise_plan','add_to_plan') LIMIT 1",(op['id'],)).fetchone())
    required=['deadlines','rules','events','plans'] if is_plan and require_coverage else []
    missing=[]
    for name in required:
        total=_count(core,c,op,name)
        query=c.execute("SELECT * FROM context_queries WHERE operation_id=? AND collection=? AND params='{}'",(op['id'],name)).fetchone()
        if total and (not query or not query['complete'] or not _fresh_query(core,c,op,query)):
            missing.append({'collection':name,'total':total})
    if is_plan and require_coverage:
        where,args=_where(core,c,op,'rules',{})
        unread_rules=[r[0] for r in c.execute('SELECT e.id FROM entities e WHERE '+where+
            ' AND NOT EXISTS(SELECT 1 FROM context_reads r WHERE r.operation_id=? AND r.entity_id=e.id AND r.version=e.version AND r.detail=2) LIMIT 20',[*args,op['id']])]
        if unread_rules:raise BusinessError('context_coverage','有生效规则的完整正文尚未核对，请读取这些记录。',{'ids':unread_rules})
    if missing: raise BusinessError('context_coverage','完整约束尚未核对或集合已有新增/删除，请继续读取这些集合。',{'required':missing})
    if is_plan and require_coverage:
        from .context_dependencies import validate_plan_reads
        validate_plan_reads(core,c,op,iter_actions(c,op['id']))
    # All supplied original materials must reach a terminal extraction state.
    from .materials import incomplete_sources
    analyze_all=op['scope'].get('analyze_materials',True)
    if analyze_all and op['scope'].get('source_owner_scope'):
        from .sources import scope_source_sql
        owner=op['scope']['source_owner_scope']
        added=c.execute("SELECT e.id FROM entities e WHERE e.type IN ('asset','artifact') AND e.archived=0 AND "+scope_source_sql()+" AND NOT EXISTS(SELECT 1 FROM context_sources s WHERE s.operation_id=? AND s.entity_id=e.id) LIMIT 1",[*([owner]*4),op['id']]).fetchone()
        if added:raise BusinessError('context_changed','课程新增了相关资料，请 refresh_context(include_new_sources=true) 后继续。',{'collection':'materials'})
    incomplete=incomplete_sources(core,c,op) if analyze_all else []
    pending=c.execute("SELECT count(*) FROM context_steps WHERE operation_id=? AND "+("status!='completed'" if analyze_all else "status='delivered'"),(op['id'],)).fetchone()[0]
    if pending: raise BusinessError('material_incomplete','已选资料还有待处理批次，请完成后合并结果。',{'pending_steps':pending})
    if incomplete: raise BusinessError('material_incomplete','资料仍在分批读取，请继续 next_context_step。',{'sources':incomplete})
    completed_steps=c.execute("SELECT count(*) FROM context_steps WHERE operation_id=? AND status='completed'",(op['id'],)).fetchone()[0]
    if completed_steps>1:
        merge=c.execute("SELECT complete,total FROM context_queries WHERE operation_id=? AND collection='facts' AND params='{}'",(op['id'],)).fetchone()
        if not merge or not merge['complete'] or merge['total']!=completed_steps:
            raise BusinessError('material_merge_required','各批分析已保存，请分页读取 facts 合并全部候选事实后再提交。')

    c.execute('SAVEPOINT validate_context_candidate')
    try:
        job=core._job(c,op['job_id']) if op['job_id'] else None
        value=json.loads(job['input']) if job else {}
        if value.get('plan_requested') and action_count!=1 and (action_count or _count(core,c,op,'tasks')):
            raise BusinessError('plan_proposal_scope','每日计划需要一份完整候选，不能把同一天拆成多份新计划。')
        for action in iter_actions(c,op['id']):
            if action.get('command') not in CANDIDATE_COMMANDS: raise BusinessError('proposal_scope','候选操作不在允许范围。')
            if value.get('plan_requested'):
                from .plan_assistance import apply
                apply(core,c,job,action,'context-validation:'+op['id'])
            elif job:
                from .sources import apply_source_action
                apply_source_action(core,c,job,action,'context-validation:'+op['id'])
            else:
                core._dispatch(c,action['command'],action['payload'],'context-validation:'+op['id'])
    finally:
        c.execute('ROLLBACK TO validate_context_candidate');c.execute('RELEASE validate_context_candidate')
    coverage_rows=c.execute("""SELECT s.entity_id,m.status,m.coverage,m.error FROM context_sources s
        LEFT JOIN material_catalog m ON m.key=s.material_key WHERE s.operation_id=?""",(op['id'],))
    total_sources=0;partial_sources=0
    for row in coverage_rows:
        total_sources+=1
        if row['status']!='complete':partial_sources+=1
    conflicts=list(c.execute("""SELECT json_extract(f.value,'$.key') fact_key,count(DISTINCT json_extract(f.value,'$.value')) variants
        FROM context_steps s,json_each(s.result,'$.facts') f WHERE s.operation_id=? AND s.status='completed'
        GROUP BY fact_key HAVING variants>1 LIMIT 21""",(op['id'],)))
    candidate=copy.deepcopy(candidate)
    candidate['material_coverage']={'sources':total_sources,'partial_sources':partial_sources,
        'analysis_required':bool(analyze_all),'analysis_steps':c.execute("SELECT count(*) FROM context_steps WHERE operation_id=? AND status='completed'",(op['id'],)).fetchone()[0],
        'conflicts':[dict(row) for row in conflicts[:20]],'more_conflicts':len(conflicts)>20}
    if partial_sources and analyze_all:
        candidate.setdefault('unknowns',[]).append(f'{partial_sources} 份原件包含明确的解析缺口；可读取部分已处理，不能视作全文已核对。')
    if conflicts:
        candidate.setdefault('unknowns',[]).append('多份资料存在不同值，已保留来源：'+', '.join(str(row['fact_key']) for row in conflicts[:10]))
    proof={'valid':True,'business_revision':core.store.meta(c,'revision'),'checked_at':now(),
           'constraints':'complete local rule/schedule/dependency/completion checks','actions':action_count}
    c.execute('UPDATE context_operations SET validation=?,updated_at=? WHERE id=?',(encode(proof),now(),op['id']))
    return candidate, proof


def checkpoint(core,c,op,p):
    step_key=p.get('step_key'); result=p.get('result') or {}
    if not isinstance(result,dict) or size(result)>PAGE_BYTES-1800: raise BusinessError('checkpoint_limit','单批检查点超过预算，请保存较小批次。')
    if step_key:
        row=c.execute('SELECT * FROM context_steps WHERE operation_id=? AND step_key=?',(op['id'],step_key)).fetchone()
        if not row or row['status']=='pending': raise BusinessError('checkpoint_step','该批内容尚未送达，不能记为已处理。')
        fingerprint=digest(result)
        if row['status']=='completed' and row['fingerprint']==fingerprint:
            return {'saved':True,'reused':True,'step_key':step_key}
        from .materials import delivery_token
        if p.get('delivery_token')!=delivery_token(core,c,op,row):
            raise BusinessError('context_changed','本批读取版本或运行代次已变化，请重新读取原文后再保存检查点。')
        actual=c.execute('SELECT version,archived FROM entities WHERE id=?',(row['source_id'],)).fetchone()
        if not actual or actual['archived'] or actual['version']!=row['source_version']:
            raise BusinessError('context_changed','原件版本已变化，请刷新此操作后重新读取。')
        if row['status']=='completed':
            if p.get('expected_fingerprint')!=row['fingerprint']:
                raise BusinessError('checkpoint_conflict','该批已有不同结果；请回读原文并用原检查点指纹明确修订。')
            c.execute('INSERT INTO context_step_history(operation_id,step_key,snapshot,created_at) VALUES (?,?,?,?)',(op['id'],step_key,encode(dict(row)),now()))
            for draft in c.execute('SELECT * FROM context_actions WHERE operation_id=?',(op['id'],)).fetchall():
                c.execute('INSERT INTO context_step_history(operation_id,step_key,snapshot,created_at) VALUES (?,?,?,?)',(op['id'],'candidate:'+str(draft['seq']),encode(dict(draft)),now()))
            c.execute('DELETE FROM context_actions WHERE operation_id=?',(op['id'],))
            c.execute("DELETE FROM context_queries WHERE operation_id=? AND collection='facts'",(op['id'],))
        validate_evidence(c,op,row,result)
        c.execute("UPDATE context_steps SET status='completed',result=?,fingerprint=?,updated_at=? WHERE operation_id=? AND step_key=?",(encode(result),fingerprint,now(),op['id'],step_key))
    if result.get('actions') is not None:
        from .ai import validate_proposal
        allowed=None
        if op['job_id']:allowed=set(json.loads(core._job(c,op['job_id'])['input'])['allowed_commands'])
        batch=validate_proposal({'summary':'batch','sources':[],'unknowns':[],'actions':result['actions']},**({'allowed_commands':allowed} if allowed else {}))
        for identifier in result.get('replace_actions',[]):
            if type(identifier) is not int:raise BusinessError('checkpoint_actions','候选行编号无效。')
            if not c.execute('DELETE FROM context_actions WHERE operation_id=? AND seq=?',(op['id'],identifier)).rowcount:
                raise BusinessError('checkpoint_actions','要修订的候选行不属于当前操作。')
        for action in batch['actions']:
            c.execute('INSERT OR IGNORE INTO context_actions(operation_id,fingerprint,command,payload,reason) VALUES (?,?,?,?,?)',
                (op['id'],digest([action['command'],action['payload']]),action['command'],encode(action['payload']),action['reason']))
    summary=p.get('summary','')
    if not isinstance(summary,str) or size(summary)>4000: raise BusinessError('checkpoint_limit','接续摘要最多 4000 字节，事实请存入批次结果。')
    saved={'summary':summary,'last_step':step_key,'stage':op['stage']}
    yield_requested=bool(p.get('yield',False))
    c.execute("UPDATE context_operations SET checkpoint=?,phase=?,updated_at=? WHERE id=?",
        (encode(saved),'checkpointed' if yield_requested else 'reading',now(),op['id']))
    return {'saved':True,'step_key':step_key,'continue_in_same_operation':True,'end_turn_for_compaction':yield_requested,'remerge_required':bool(p.get('expected_fingerprint'))}


def validate_evidence(c,op,step,result):
    """Candidate facts are versioned evidence, never live facts or write authority."""
    facts=result.get('facts',[])
    if not isinstance(facts,list) or len(facts)>40: raise BusinessError('checkpoint_facts','每批最多 40 条候选事实。')
    for fact in facts:
        if not isinstance(fact,dict) or not all(isinstance(fact.get(k),str) and fact[k].strip() for k in ('key','value','evidence')):
            raise BusinessError('checkpoint_evidence','候选事实必须有 key、value 和原文位置 evidence。')


def compacted(core,identifier):
    """Only a confirmed provider contextCompaction completion may reset quota."""
    with core.store.lock,core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE');op=_operation(core,c,identifier,writable=True)
        c.execute("UPDATE context_operations SET stage=stage+1,delivered_bytes=0,tool_calls=0,phase='reading',updated_at=? WHERE id=?",(now(),identifier))
        c.commit()


def handle(core,name,p):
    # Both directions enter the model window. Count argument bytes even when a
    # read fails/replays; checkpoint facts and actions are not free context.
    identifier=p.get('operation_id')
    if identifier:
        with core.store.lock,core.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            _operation(core,c,identifier)
            c.execute('UPDATE context_operations SET delivered_bytes=delivered_bytes+?,tool_calls=tool_calls+1 WHERE id=?',
                      (size(p),identifier))
            c.commit()
    try:
        return _handle(core,name,p)
    finally:
        if identifier:
            _stop_budget(core, identifier)


def _stop_budget(core, identifier, *, required=False):
    # A stale callback must not change the new stage even in error/finally paths.
    with core.store.lock,core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            _operation(core,c,identifier)
            if required:
                c.execute("UPDATE context_operations SET phase='budget_stop' WHERE id=? AND phase NOT IN ('ready','checkpointed')",(identifier,))
            else:
                c.execute("""UPDATE context_operations SET phase='budget_stop'
                    WHERE id=? AND phase='reading' AND (delivered_bytes>=? OR tool_calls>=128)""",
                    (identifier,STAGE_BYTES-RESERVE_BYTES-2048))
            c.commit()
        except BusinessError:
            c.rollback()


def _handle(core,name,p):
    if name not in QUERY_NAMES: raise BusinessError('context_query','未注册的读取方法。')
    # Extraction is bounded I/O and must never run while holding a DB transaction.
    if name in {'read_material','next_context_step'}:
        from .materials import read_material, next_step
        if name=='read_material' and p.get('from_job'):
            from .context_history import material
            return material(core,p)
        return (read_material if name=='read_material' else next_step)(core,p)
    with core.store.lock,core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            if name=='prepare_context':
                identifier=p.get('operation_id')
                result=envelope(core,c,_operation(core,c,identifier)) if identifier else create(core,c,scope=p.get('scope') or {'kind':'general'},goal=p.get('goal','读取当前业务'),sources=p.get('source_ids') or [])
            else:
                op=_operation(core,c,p.get('operation_id'),writable=name in {'checkpoint_context','refresh_context'})
                if name=='query_context' and p.get('collection')=='actions':
                    from .context_candidates import page as action_page
                    _budget(op,p.get('byte_limit'))
                    result=_charge(c,op,action_page(core,{'operation_id':op['id'],'after':p.get('cursor') or 0}))
                elif name=='query_context' and p.get('collection')=='facts' and (p.get('filters') or {}).get('job_id'):
                    from .context_history import facts
                    result=facts(core,c,op,p['filters']['job_id'],p.get('cursor'))
                elif name=='query_context':
                    result=history_page(core,c,op,p.get('cursor'),p.get('byte_limit')) if p.get('collection')=='history' else __import__('management.materials',fromlist=['facts_page']).facts_page(core,c,op,p.get('cursor'),p.get('byte_limit')) if p.get('collection')=='facts' else page(core,c,op,p.get('collection'),p.get('filters'),p.get('cursor'),p.get('limit',80),p.get('byte_limit'))
                elif name=='read_context_item': result=read_item(core,c,op,p['id'],p.get('offset',0),p.get('version'),p.get('byte_limit'))
                elif name in {'context_coverage','operation_status'}:
                    if op['delivered_bytes']>STAGE_BYTES-RESERVE_BYTES or op['tool_calls']>=128:
                        result={'operation_id':op['id'],'phase':op['phase'],'stage':op['stage'],'checkpoint_required':True}
                    else:result=coverage(core,c,op,p.get('query_cursor',''))
                    result=_charge(c,op,result)
                elif name=='checkpoint_context': result=_charge(c,op,checkpoint(core,c,op,p))
                elif name=='refresh_context':
                    from .context_refresh import refresh
                    result=_charge(c,op,refresh(core,c,op,bool(p.get('include_new_sources'))))
                elif name=='describe_action':
                    from .ai_commands import CANDIDATE_COMMANDS
                    command=p.get('command')
                    if command not in CANDIDATE_COMMANDS: raise BusinessError('proposal_scope','该操作未开放。')
                    value={'command':command,'types':core.types(c).get(p.get('type'),{}),
                           'contract':'Use command/reason/payload_json in submit_candidate; Core validates all actions.'}
                    budget=_budget(op,p.get('byte_limit'));text,following=_fragment(encode(value),p.get('offset',0),budget-512)
                    result=_charge(c,op,{'json_fragment':text,'next_offset':following})
                elif name=='validate_candidate':
                    from .ai import validate_proposal
                    candidate=validate_proposal(p.get('proposal'))
                    _,result=validate(core,c,op,candidate)
                    result=_charge(c,op,result)
                else: raise BusinessError('context_query','未注册方法。')
            c.commit();return result
        except Exception as error:
            c.rollback()
            if getattr(error,'code',None)=='context_checkpoint_required' and p.get('operation_id'):
                _stop_budget(core,p['operation_id'],required=True)
            raise
