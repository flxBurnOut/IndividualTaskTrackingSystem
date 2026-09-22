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
    # Both date indexes are usable; only a bounded result page becomes Python data.
    sql = """WITH candidate_ids AS (
        SELECT id FROM entities WHERE type='task' AND archived=0 AND json_extract(data,'$.scheduled_date')!='' AND json_extract(data,'$.scheduled_date')<=?
        UNION
        SELECT id FROM entities WHERE type='task' AND archived=0 AND json_extract(data,'$.due_date')!='' AND json_extract(data,'$.due_date')<=?
    ) SELECT e.* FROM candidate_ids k JOIN entities e ON e.id=k.id
    WHERE e.status IN ('active','planned','pending','blocked')
    AND COALESCE((SELECT json_extract(f.data,'$.dimensions.completion') FROM entities f
        WHERE f.type='feedback' AND f.archived=0
        AND json_extract(f.data,'$.target_id')=+e.id
        AND json_extract(f.data,'$.business_date')<=?
        AND json_type(f.data,'$.dimensions.completion') IS NOT NULL
        ORDER BY json_extract(f.data,'$.business_date') DESC,f.created_at DESC,f.rowid DESC LIMIT 1),'unknown')!='done'
    """
    args = [day, day, day]
    if planned:
        sql += ' AND e.id NOT IN (' + ','.join('?' for _ in planned) + ')'
        args.extend(sorted(planned))
    total = c.execute('SELECT count(*) FROM (' + sql + ')', args).fetchone()[0]
    sql += " ORDER BY COALESCE(NULLIF(json_extract(e.data,'$.due_date'),''),NULLIF(json_extract(e.data,'$.scheduled_date'),'')),e.created_at,e.id LIMIT ? OFFSET ?"
    items = []
    for row in c.execute(sql, [*args, limit, offset]):
        entity = core.store.entity(row)
        due, scheduled = entity['data'].get('due_date'), entity['data'].get('scheduled_date')
        if due and due < day:
            reason, code = '截止日期已过，完成情况仍待处理', 'overdue'
        elif scheduled and scheduled == day:
            reason, code = '已指定在这一天处理', 'scheduled_today'
        elif due == day:
            reason, code = '截止在这一天', 'due_today'
        else:
            reason, code = '此前安排，仍待处理', 'scheduled_overdue'
        items.append({**entity, 'reason': reason, 'reason_code': code})
    return {'date': day, 'items': items, 'total': total,
            'next_offset': offset + limit if offset + limit < total else None,
            'plan': {'id': plan['id'], 'version': plan['version'], 'mode': plan['data']['mode']} if plan else None}


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
    if target['archived'] or target['status'] not in {'active', 'planned', 'pending', 'blocked'}:
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
    """Completion evidence is independent of attendance, submission and mastery."""
    if target['status'] == 'done':
        return True
    row = c.execute("""SELECT json_extract(data,'$.dimensions.completion') FROM entities
        WHERE type='feedback' AND archived=0 AND json_extract(data,'$.target_id')=?
        AND json_extract(data,'$.business_date')<=?
        AND json_type(data,'$.dimensions.completion') IS NOT NULL
        ORDER BY json_extract(data,'$.business_date') DESC,created_at DESC,rowid DESC LIMIT 1""",
        (target['id'], day)).fetchone()
    return bool(row and row[0] == 'done')
