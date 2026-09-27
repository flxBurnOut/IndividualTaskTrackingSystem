"""Bounded planning context and validated, reviewable daily-plan proposals."""
from __future__ import annotations
import copy
import datetime as dt
import json
import re
from zoneinfo import ZoneInfo
from .schemas import BusinessError
from .storage import encode


GUIDANCE = """The user explicitly requests a daily plan, not a questionnaire.
Produce one complete plan candidate for the supplied date using real existing IDs.
If existing_plan is absent, use create_plan. If it exists, use revise_plan with its
exact plan_id and plan_version and the complete revised blocks. Do not create a
parallel plan or use any task/feedback mutation. Follow this job's allowed_commands.
Search the paged task collections for relevant candidates; do not require every historical body. Read all pages of deadlines, active rules and applicable events before choosing priorities.
Use existing records first. Unknown completion is not zero progress. Never change
feedback, task scope, status or deadlines while planning. Keep original gates.
If today's available hours or sleep anchors are unconfirmed, use no_precise_time:
give an ordered short list (normally up to 3 priorities), not invented clock times
or assumed hours. Fixed events remain visible separately in Today's screen.
Missing unrelated future dates, grades, attendance or total catch-up quantities
must not block this initial plan. Make checking a relevant unknown part of an
existing task where appropriate. Ask at most ONE short question only if no safe
initial plan can be formed; other uncertainty belongs in plan notes, not a form.
Do not require the user to report every course before generating a plan.
For an existing plan, preserve completed/reported or past blocks shown in
protected_blocks unchanged. Other changes remain reviewable before adoption.
The output is a candidate until the user presses Save; never claim it was saved.
"""


def requested_day(text,scope,today,explicit=False):
    """Explicit planning requests use the workflow; explanatory chat stays chat."""
    if re.search(r'(?:不要|先别|暂不|不用|先不).{0,8}(?:生成|安排|制定|调整|修改|更新|重排|重做).{0,8}(?:计划|日程)|(?:不要|先别|暂不|不用|先不).{0,8}(?:计划|日程).{0,12}(?:改为|改成|修改|调整|更新|重排)|(?:只|仅)(?:讨论|解释|了解)',text):return None
    if re.match(r'^(?:请)?(?:解释|介绍|说明).{0,12}(?:计划|生成)|^(?:怎么|如何|怎样|为什么).{0,12}(?:计划|生成)',text):return None
    request_words=re.search(r'(?:生成|安排|制定|重做|调整|修改|更新|重排|排).{0,24}(?:计划|今天|今日|明天)|(?:计划|日程).{0,16}(?:改为|改成|修改|调整|更新|重排)|(?:今天|今日)(?:该|要)?(?:干什么|做什么)',text)
    # A launch-button hint must not turn a replacement feedback message into a
    # plan-only request. Preserve normal feedback commands and separate facts.
    feedback=re.search(r'(?:已经|已|刚刚|刚).{0,12}(?:完成|做完|搞定|提交|看完|到课)|(?:完成|做完|搞定|提交|看完)了|(?:还没|没有|未)(?:完成|做完|提交|到课)|(?:完成了|做了|看了|补了).{0,10}(?:题|页|节|讲|章|分钟|小时|%)',text)
    if feedback and not request_words:return None
    requested=explicit or request_words
    if not requested:return None
    if scope['kind']=='daily_plan':return scope['date']
    if scope['kind']!='general':return None
    explicit_date=re.search(r'为\s*(\d{4}-\d{2}-\d{2})\s*(?:生成|安排|制定|调整|修改|更新|重排|重做)',text)
    if explicit_date:
        try:return dt.date.fromisoformat(explicit_date[1]).isoformat()
        except ValueError as error:raise BusinessError('validation','计划日期无效。') from error
    if '明天' in text:return (dt.date.fromisoformat(today)+dt.timedelta(days=1)).isoformat()
    if '今天' in text or '今日' in text:return today
    return None


def context(core, c, day, mode):
    from .reviews import _latest_plan
    base = core.plan_context(c, day, mode)
    # Ordinary occurrences duplicate the complete source data. Retain a second
    # copy only when an exception changes the effective values for this date.
    for event in base['hard_events']:
        if 'effective' in event and event['effective'] == event['data']:
            event.pop('effective')
    # Keep safety constraints intact; replace verbose task bodies with a complete
    # bounded index, rather than dropping most tasks to fit the prompt budget.
    from .task_views import rows_sql, ENTITY_COLUMNS
    from .presentation import Presenter
    presenter=Presenter(core,c)
    sql="SELECT * FROM ("+rows_sql("e.status NOT IN ('cancelled','draft','failed')",day)+") e WHERE completion_state IS NULL OR completion_state!='done'"
    sql+=" ORDER BY CASE json_extract(e.data,'$.priority') WHEN 'high' THEN 0 WHEN 'low' THEN 2 ELSE 1 END,coalesce(nullif(json_extract(e.data,'$.due_date'),''),'9999'),e.id"
    total=c.execute('SELECT count(*) FROM ('+sql+')').fetchone()[0]
    tasks=[];budget=0
    for row in c.execute(sql+' LIMIT 501'):
        e=core.store.entity({key:row[key] for key in ENTITY_COLUMNS});d=e['data']
        item={k:e[k] for k in ('id','version','title','status','parent_id')}
        item.update({key:value for key,value in presenter.fields(e).items() if key in {'owner_id','owner_label','display_title','learning_unit_label'}})
        item['data']={k:d[k] for k in ('due_date','earliest_start','scheduled_date','priority','estimated_minutes','task_kind','catchup_enabled') if d.get(k) is not None}
        # Completion gates are looked up from the canonical target on save.
        action=str(d.get('next_action') or '')
        if action:item['next_action_excerpt']=action[:320]
        item['details_abridged']=True
        item['depends_on']=[r[0] for r in c.execute("SELECT target_id FROM links WHERE source_id=? AND kind='depends_on'",(e['id'],))]
        cost=len(encode(item))
        if len(tasks)>=500 or budget+cost>23500:break
        budget+=cost;tasks.append(item)
    base['tasks']=tasks
    base['coverage'].update(tasks_total=total,tasks_returned=len(tasks),tasks_paged=len(tasks)<total,
        task_index_complete=len(tasks)==total,task_details_abridged=True)
    # Do not silently claim a truncated index is the full selection universe.
    base['guidance']='任务列表是范围和下一步摘要；完成标准由保存时从原任务读取，禁止缩减。未知时间时先生成有序清单。'
    nearby=[]
    end=(dt.date.fromisoformat(day)+dt.timedelta(days=14)).isoformat()
    for row in c.execute("""SELECT id,version,title,status,data FROM entities WHERE type='event' AND archived=0
        AND status NOT IN ('done','cancelled','draft') AND coalesce(json_extract(data,'$.recurrence'),'none')='none'
        AND json_extract(data,'$.date') BETWEEN ? AND ? ORDER BY json_extract(data,'$.date'),id LIMIT 101""",(day,end)):
        d=json.loads(row['data'])
        nearby.append({k:row[k] for k in ('id','version','title','status')}|{'data':{k:d[k] for k in ('date','start','end','owner_id','event_kind','preparation_start') if d.get(k) is not None}})
    current=_latest_plan(core,c,day)
    local=dt.datetime.now(ZoneInfo(core.store.meta(c,'settings')['timezone']))
    protected=[]
    if current:
        for block in current['data'].get('blocks',[]):
            reported=c.execute("SELECT 1 FROM entities WHERE type='feedback' AND archived=0 AND json_extract(data,'$.target_id')=? AND json_extract(data,'$.business_date')=? LIMIT 1",(block['target_id'],day)).fetchone()
            past=day<local.date().isoformat() or day==local.date().isoformat() and block.get('end','99:99')<=local.strftime('%H:%M')
            if reported or past:protected.append(block)
    # Completed captures already belong to canonical tasks/facts. Their old
    # imported bodies are not outstanding work for every subsequent daily plan.
    inbox_where="type='inbox' AND archived=0 AND status NOT IN ('done','cancelled')"
    inbox_total=c.execute('SELECT count(*) FROM entities WHERE '+inbox_where).fetchone()[0]
    inbox=[core.store.entity(row) for row in c.execute('SELECT * FROM entities WHERE '+inbox_where+' ORDER BY updated_at DESC,rowid DESC LIMIT 10')]
    return {'planning':base, 'nearby_events':nearby[:100], 'nearby_events_complete':len(nearby)<=100,
        'recent_inbox':inbox, 'recent_inbox_coverage':{'only_unresolved':True,'records_total':inbox_total,
            'records_returned':len(inbox),'complete':len(inbox)==inbox_total},
        'plan_request':{'date':day,'guidance':GUIDANCE,'local_now':local.isoformat(timespec='minutes'),
            'existing_plan':current,'protected_blocks':protected}, **core.store.state(c)}


def normalize(core, c, job, result):
    """Dry-run the exact candidate; no business mutation or invented time facts."""
    value=json.loads(job['input']) if isinstance(job['input'],str) else job['input']
    if not value.get('plan_requested'):return result
    request = _request(core, c, value)
    day=request['date']
    result=copy.deepcopy(result)
    actions=result.get('actions',[])
    if not isinstance(actions,list):
        raise BusinessError('plan_proposal_scope','每日计划候选需要明确的操作列表。')
    if not actions:
        if value.get('context_operation_id'):
            from .context_service import _count,_operation
            available=_count(core,c,_operation(core,c,value['context_operation_id']),'tasks')
        else:available=len(value['context']['planning']['tasks'])
        if available or request.get('existing_plan'):
            raise BusinessError('plan_missing_candidate','这次未返回可保存的计划，原记录未改动。请重新生成计划。')
        return result
    if len(actions)!=1:
        raise BusinessError('plan_proposal_scope','每日计划需要一份完整候选，不能混入其他操作。')
    action = _prepare_action(core, c, value, request, actions[0], preserve_legacy=True)
    result['actions'] = [action]
    payload = action['payload']
    # A text reply is never a saved plan; candidates use the very same validator
    # as adoption. The savepoint also rolls back change/audit rows and new IDs.
    c.execute('SAVEPOINT validate_daily_plan')
    try:
        checked=core._dispatch(c,action['command'],payload,'preview:'+job['id'])['entity']
        payload['blocks']=checked['data']['blocks']
    finally:
        c.execute('ROLLBACK TO validate_daily_plan');c.execute('RELEASE validate_daily_plan')
    result['plan_preview']={'date':day,'mode':payload.get('mode','standard'),'command':action['command'],
        'plan_id':payload.get('plan_id'),'plan_version':payload.get('plan_version'),
        'items':[{'title':core.store.get(c,b['target_id'])['title'],**b} for b in payload.get('blocks',[])],
        'validated':True,'saved':False}
    # These are notes, not a compulsory multi-course questionnaire.
    result['planning_notes']=result.get('unknowns',[])
    result['unknowns']=[]
    return result


def apply(core,c,job,action,rid):
    value=json.loads(job['input']) if isinstance(job['input'],str) else job['input']
    request = _request(core, c, value)
    prepared = _prepare_action(core, c, value, request, action)
    return core._dispatch(c,prepared['command'],prepared['payload'],rid)


def _request(core, c, value):
    if value.get('context_operation_id'):
        from .context_service import plan_request
        request=plan_request(core,c,value.get('date') or value['context']['scope']['date'])
    else:request=value['context']['plan_request']
    scope = value.get('conversation_scope') or {}
    if scope.get('kind') == 'daily_plan' and scope.get('date') != request['date']:
        raise BusinessError('plan_proposal_date','候选日期与原讨论范围不一致，尚未保存。')
    # Legacy non-context jobs carry their original planning snapshot. Always
    # compare it with the current plan instead of treating it as current state.
    if not value.get('context_operation_id'):
        from .context_service import plan_request
        request = plan_request(core, c, request['date'])
    return request


def _baseline(c, value, action, current):
    if 'plan_baseline' in value:
        baseline = value['plan_baseline']
        if not isinstance(baseline,dict) or set(baseline) != {'plan_id','plan_version'}:
            raise BusinessError('plan_conflict','原计划基线无效，请重新读取后发起计划请求。')
        identifier, version = baseline['plan_id'], baseline['plan_version']
    elif 'plan_request' in value.get('context', {}):
        old = value['context']['plan_request'].get('existing_plan')
        identifier, version = (old['id'], old['version']) if old else (None, None)
    else:
        # Existing pre-upgrade create_plan candidates name their predecessor in
        # supersedes_id. Its recorded read version supplies the missing fence.
        payload = action['payload']
        identifier = payload.get('plan_id') if action['command']=='revise_plan' else payload.get('supersedes_id')
        version = payload.get('plan_version')
        if identifier and version is None and value.get('context_operation_id'):
            read = c.execute('SELECT version FROM context_reads WHERE operation_id=? AND entity_id=?',
                             (value['context_operation_id'],identifier)).fetchone()
            version = read['version'] if read else None
        if identifier is None and current is not None:
            raise BusinessError('plan_conflict','旧候选未保存可核对的原计划身份，请重新读取并生成修改候选。')
    if identifier is None:
        valid = version is None
    else:
        valid = isinstance(identifier,str) and bool(identifier) and type(version) is int and version>0
    actual = (current['id'], current['version']) if current else (None, None)
    if not valid or (identifier, version) != actual:
        raise BusinessError('plan_conflict','当日计划已变化，原记录未修改；请在原讨论重新生成修改候选。')
    return identifier, version


def _prepare_action(core, c, value, request, action, *, preserve_legacy=False):
    if not isinstance(action,dict) or action.get('command') not in {'create_plan','revise_plan'} or not isinstance(action.get('payload'),dict):
        raise BusinessError('plan_proposal_scope','这次只允许创建或修改所选日期的一份每日计划。')
    action = copy.deepcopy(action)
    payload = action['payload']
    if payload.get('date') != request['date']:
        raise BusinessError('plan_proposal_date','候选日期与所选日期不一致，尚未保存。')
    if set(payload)-{'date','mode','title','blocks','source_text','supersedes_id','plan_id','plan_version'}:
        raise BusinessError('plan_proposal_scope','计划候选包含不属于本次计划的字段。')
    current = request.get('existing_plan')
    identifier, version = _baseline(c, value, action, current)
    if action['command'] == 'revise_plan':
        if current is None or type(payload.get('plan_version')) is not int or (payload.get('plan_id'),payload.get('plan_version')) != (identifier,version):
            raise BusinessError('plan_conflict','修改候选必须引用当前原计划及其准确版本。')
        if 'supersedes_id' in payload:
            raise BusinessError('plan_proposal_scope','修改计划应使用 plan_id 和 plan_version，不另指定替换目标。')
    elif current is not None:
        if payload.get('supersedes_id') not in (None,identifier):
            raise BusinessError('plan_conflict','旧候选指定的替换计划与原计划不一致。')
        if 'plan_id' in payload or 'plan_version' in payload:
            raise BusinessError('plan_proposal_scope','旧创建候选不能混入另一份修改计划身份。')
        action['command'] = 'revise_plan'
        payload.pop('supersedes_id',None)
        payload.update(plan_id=identifier,plan_version=version)
        # Only the pre-existing create-plan compatibility path restores omitted
        # historical blocks. A normal revision must explicitly preserve them.
        if preserve_legacy:
            protected = request.get('protected_blocks',[])
            blocks = payload.get('blocks',[])
            if not isinstance(blocks,list) or any(not isinstance(b,dict) for b in blocks):
                raise BusinessError('validation','计划事项格式无效。')
            targets = {b['target_id'] for b in protected}
            payload['blocks'] = [*copy.deepcopy(protected),*[b for b in blocks if b.get('target_id') not in targets]]
    elif any(key in payload for key in ('supersedes_id','plan_id','plan_version')):
        raise BusinessError('plan_proposal_scope','首次计划不能指定其他计划作为替换目标。')
    if action['command']=='revise_plan' and payload.get('mode')=='no_precise_time' and any(b.get('start') for b in request.get('protected_blocks',[])):
        payload['mode']='standard'
    return action
