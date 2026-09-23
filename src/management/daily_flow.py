"""Read-only dated task candidates and an explicit, versioned plan append."""
from __future__ import annotations
import copy
from .core import date_value
from .reviews import _latest_plan
from .schemas import BusinessError


def query_tasks(core, c, p):
    day = date_value(p.get('date') or core.today(c)).isoformat()
    limit = max(1, min(100, int(p.get('limit', 30))))
    offset = max(0, int(p.get('offset', 0)))
    plan = _latest_plan(core, c, day)
    planned = {b['target_id'] for b in plan['data'].get('blocks', [])} if plan else set()
    from .task_views import rows_sql, ENTITY_COLUMNS
    from .presentation import Presenter, owner_sort_sql
    clauses = ["e.status NOT IN ('cancelled','draft')", "((nullif(json_extract(e.data,'$.scheduled_date'),'') IS NOT NULL AND json_extract(e.data,'$.scheduled_date')<=?) OR (nullif(json_extract(e.data,'$.due_date'),'') IS NOT NULL AND json_extract(e.data,'$.due_date')<=?))"]
    args=[day,day]
    if planned and not p.get('include_planned',False):
        clauses.append('e.id NOT IN ('+','.join('?' for _ in planned)+')');args.extend(sorted(planned))
    sql='SELECT * FROM ('+rows_sql(' AND '.join(clauses),day)+") e WHERE completion_state IS NULL OR completion_state!='done'"
    total=c.execute('SELECT count(*) FROM ('+sql+')',args).fetchone()[0]
    date_sql="min(coalesce(nullif(json_extract(e.data,'$.scheduled_date'),''),'9999-12-31'),coalesce(nullif(json_extract(e.data,'$.due_date'),''),'9999-12-31'))"
    sql+=' ORDER BY '+owner_sort_sql('e')+' COLLATE NOCASE,'+date_sql+',e.created_at,e.id LIMIT ? OFFSET ?'
    items=[];groups={};presenter=Presenter(core,c)
    for row in c.execute(sql,[*args,limit,offset]):
        entity=presenter.entity(core.store.entity({key:row[key] for key in ENTITY_COLUMNS}))
        due,scheduled=entity['data'].get('due_date'),entity['data'].get('scheduled_date')
        if due and due < day:reason,code='截止日期已过，完成情况仍待处理','overdue'
        elif scheduled==day:reason,code='已指定在这一天处理','scheduled_today'
        elif due==day:reason,code='截止在这一天','due_today'
        else:reason,code='此前安排，仍待处理','scheduled_overdue'
        item={**entity,'completion_state':row['completion_state'],'candidate_date':min(x for x in (due,scheduled) if x),'reason':reason,'reason_code':code,'in_plan':entity['id'] in planned}
        items.append(item)
        group=groups.setdefault(entity['owner_id'],{'owner_id':entity['owner_id'],'owner_label':entity['owner_label'],'items':[]})
        group['items'].append(item)
    return {'date':day,'items':items,'groups':list(groups.values()),'total':total,
            'next_offset':offset+len(items) if offset+len(items)<total else None,
            'plan':{'id':plan['id'],'version':plan['version'],'mode':plan['data']['mode']} if plan else None}


def add_to_plan(core, c, p, rid):
    day = date_value(p['date']).isoformat()
    current = _latest_plan(core, c, day)
    if 'plan_id' not in p or 'plan_version' not in p:
        raise BusinessError('plan_conflict', '请先读取这一天的最新计划，再加入任务。')
    if (p['plan_id'], p['plan_version']) != ((current['id'], current['version']) if current else (None, None)):
        raise BusinessError('plan_conflict', '这一天的计划已变化，请重新读取后再加入；原编辑仍可保留。')
    target = core.store.get(c, p['target_id'])
    if target['type'] != 'task':
        raise BusinessError('plan_target', '请先把节点要求整理成实际任务，再加入每日计划。')
    old_blocks = copy.deepcopy(current['data'].get('blocks', [])) if current else []
    if any(block.get('target_id') == target['id'] for block in old_blocks):
        return {'entity': current, 'reused': True}
    if target['archived'] or target['status'] not in {'active', 'planned', 'pending', 'blocked', 'done'} or completed_on_or_before(core,c,target,day):
        raise BusinessError('plan_target', '该任务目前不可安排，请先核对任务状态。')
    if current and current['data']['mode'] == 'rest':
        raise BusinessError('rest_plan', '这一天已设为休整。请先明确调整计划方式，再添加任务。')
    new_block = {'target_id': target['id']}
    minutes = target['data'].get('estimated_minutes')
    if type(minutes) is int and minutes > 0:
        new_block['minutes'] = minutes
    payload = {'date': day, 'mode': current['data']['mode'] if current else 'no_precise_time',
               'blocks': [*old_blocks, new_block],
               'source_text': current['data'].get('source_text', '') if current else '用户明确将任务加入这一天的计划。'}
    if current:
        payload.update(title=current['title'], supersedes_id=current['id'])
    entity = core.create_plan(c, payload, rid, _retained=old_blocks)
    return {'entity': entity, 'reused': False}


def completed_on_or_before(core, c, target, day):
    """The shared state projection honors explicit reopen and dated feedback."""
    if target['type']=='task':
        from .task_views import state
        return state(core,c,target['id'],day)=='done'
    if target['status']=='done':return True
    row=c.execute("""SELECT json_extract(data,'$.dimensions.completion') FROM entities
        WHERE type='feedback' AND archived=0 AND json_extract(data,'$.target_id')=?
        AND json_extract(data,'$.business_date')<=? AND json_type(data,'$.dimensions.completion') IS NOT NULL
        ORDER BY json_extract(data,'$.business_date') DESC,created_at DESC,rowid DESC LIMIT 1""",(target['id'],day)).fetchone()
    return bool(row and row[0]=='done')

def revise_plan(core,c,p,rid):
    """Explicit manual editing of the latest plan, preserving reported history."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    day=date_value(p.get('date')).isoformat()
    current=_latest_plan(core,c,day)
    if not current or (p.get('plan_id'),p.get('plan_version'))!=(current['id'],current['version']):
        raise BusinessError('plan_conflict','当日计划已经变化，请保留编辑并重新读取核对。')
    blocks=copy.deepcopy(p.get('blocks',[]))
    if not isinstance(blocks,list) or len(blocks)>100:
        raise BusinessError('validation','单日计划需要不超过 100 个事项。')
    old_by_id={b['target_id']:b for b in current['data'].get('blocks',[])}
    local=dt.datetime.now(ZoneInfo(core.store.meta(c,'settings')['timezone']))
    protected={}
    for target,block in old_by_id.items():
        reported=c.execute("SELECT 1 FROM entities WHERE type='feedback' AND archived=0 AND json_extract(data,'$.target_id')=? AND json_extract(data,'$.business_date')=? LIMIT 1",(target,day)).fetchone()
        past=day<local.date().isoformat() or day==local.date().isoformat() and block.get('end','99:99')<=local.strftime('%H:%M')
        if reported or past:protected[target]=block
    # GUI-only names never become part of a saved block. Target versions and
    # completion gates from the source block are retained when unchanged.
    for block in blocks:
        if not isinstance(block,dict) or 'target_id' not in block:
            raise BusinessError('validation','计划事项格式无效。')
        old=old_by_id.get(block['target_id'])
        if old:
            for key in ('target_version','completion_gate'):
                if key not in block and key in old:block[key]=old[key]
    incoming={b['target_id']:b for b in blocks}
    if any(incoming.get(target)!=block for target,block in protected.items()):
        raise BusinessError('plan_history','已经记录结果或经过的计划事项需要原样保留；实际结果可在今天或复盘中更正。')
    retained=[old_by_id.get(b['target_id']) if old_by_id.get(b['target_id'])==b else None for b in blocks]
    historical=[protected.get(b['target_id']) for b in blocks]
    payload={key:p[key] for key in ('date','mode','source_text','title') if key in p}
    payload.update(blocks=blocks,supersedes_id=current['id'])
    return {'entity':core.create_plan(c,payload,rid,_retained=retained,_historical_retained=historical)}
