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
Produce one create_plan action for the supplied date using real existing IDs.
Read the complete task index and nearby deadlines before choosing priorities.
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
    if re.search(r'(?:不要|先别|暂不|不用|先不).{0,8}(?:生成|安排|制定|调整|重做).{0,8}(?:计划|日程)|(?:只|仅)(?:讨论|解释|了解)',text):return None
    if re.match(r'^(?:请)?(?:解释|介绍|说明).{0,12}(?:计划|生成)|^(?:怎么|如何|怎样|为什么).{0,12}(?:计划|生成)',text):return None
    requested=explicit or re.search(r'(?:生成|安排|制定|重做|调整|排).{0,24}(?:计划|今天|今日|明天)|(?:今天|今日)(?:该|要)?(?:干什么|做什么)',text)
    if not requested:return None
    if scope['kind']=='daily_plan':return scope['date']
    if scope['kind']!='general':return None
    explicit_date=re.search(r'为\s*(\d{4}-\d{2}-\d{2})\s*(?:生成|安排|制定|调整)',text)
    if explicit_date:
        try:return dt.date.fromisoformat(explicit_date[1]).isoformat()
        except ValueError as error:raise BusinessError('validation','计划日期无效。') from error
    if '明天' in text:return (dt.date.fromisoformat(today)+dt.timedelta(days=1)).isoformat()
    if '今天' in text or '今日' in text:return today
    return None


def context(core, c, day, mode):
    from .reviews import _latest_plan
    base = core.plan_context(c, day, mode)
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
    return {'planning':base, 'nearby_events':nearby[:100], 'nearby_events_complete':len(nearby)<=100,
        'plan_request':{'date':day,'guidance':GUIDANCE,'local_now':local.isoformat(timespec='minutes'),
            'existing_plan':current,'protected_blocks':protected}, **core.store.state(c)}


def normalize(core, c, job, result):
    """Dry-run the exact candidate; no business mutation or invented time facts."""
    value=json.loads(job['input']) if isinstance(job['input'],str) else job['input']
    if not value.get('plan_requested'):return result
    request=value['context']['plan_request'];day=request['date']
    result=copy.deepcopy(result)
    actions=result.get('actions',[])
    if not actions:
        if value['context']['planning']['tasks']:
            raise BusinessError('plan_missing_candidate','这次未返回可保存的计划，原记录未改动。请重新生成计划。')
        return result
    if len(actions)!=1 or actions[0]['command']!='create_plan':
        raise BusinessError('plan_proposal_scope','这次只生成每日计划，请重新生成。')
    payload=actions[0]['payload']
    if payload.get('date')!=day:
        raise BusinessError('plan_proposal_date','候选日期与所选日期不一致，尚未保存。')
    existing=request.get('existing_plan');protected=request.get('protected_blocks',[])
    if existing:
        payload['supersedes_id']=existing['id']
        if payload.get('mode')=='no_precise_time' and any(b.get('start') for b in protected):payload['mode']='standard'
        targets={b['target_id'] for b in protected}
        payload['blocks']=[*copy.deepcopy(protected),*[b for b in payload.get('blocks',[]) if b.get('target_id') not in targets]]
    elif payload.get('supersedes_id'):
        raise BusinessError('plan_proposal_scope','没有可替换的旧计划，尚未保存。')
    # A text reply is never a saved plan; candidates use the very same validator
    # as adoption. The savepoint also rolls back change/audit rows and new IDs.
    c.execute('SAVEPOINT validate_daily_plan')
    try:
        core.create_plan(c,payload,'preview:'+job['id'],_retained=protected,_historical_retained=protected)
    finally:
        c.execute('ROLLBACK TO validate_daily_plan');c.execute('RELEASE validate_daily_plan')
    result['plan_preview']={'date':day,'mode':payload.get('mode','standard'),
        'items':[{'title':core.store.get(c,b['target_id'])['title'],**b} for b in payload.get('blocks',[])],
        'validated':True,'saved':False}
    # These are notes, not a compulsory multi-course questionnaire.
    result['planning_notes']=result.get('unknowns',[])
    result['unknowns']=[]
    return result


def apply(core,c,job,action,rid):
    value=json.loads(job['input']) if isinstance(job['input'],str) else job['input']
    request=value['context']['plan_request'];payload=action['payload']
    if action['command']!='create_plan' or payload.get('date')!=request['date']:
        raise BusinessError('plan_proposal_scope','候选不是本次请求的每日计划。')
    return {'entity':core.create_plan(c,payload,rid,_retained=request.get('protected_blocks',[]),_historical_retained=request.get('protected_blocks',[]))}
