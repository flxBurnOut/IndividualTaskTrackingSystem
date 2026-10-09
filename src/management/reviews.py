"""Plan-based daily review and structured weekly coverage.

Queries never create questions or records. Mutations run in Core's transaction,
and only explicitly selected completion results become immutable feedback.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from .schemas import BusinessError
from .storage import encode, new_id


RESULTS = frozenset({'done', 'incomplete'})


def _date(value):
    try:
        return dt.date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise BusinessError('validation', '复盘需要有效日期。') from exc


def _latest_plan(core, c, day):
    row = c.execute("""SELECT * FROM entities WHERE type='plan' AND archived=0
        AND status NOT IN ('cancelled','draft') AND json_extract(data,'$.date')=?
        ORDER BY created_at DESC,rowid DESC LIMIT 1""", (day,)).fetchone()
    return core.store.entity(row) if row else None


def _daily_record(core, c, day, plan_id):
    row = c.execute("""SELECT * FROM entities WHERE type='review' AND archived=0
        AND json_extract(data,'$.review_kind')='daily' AND json_extract(data,'$.date')=?
        AND json_extract(data,'$.plan_id') IS ? ORDER BY created_at DESC,rowid DESC LIMIT 1""", (day, plan_id)).fetchone()
    return core.store.entity(row) if row else None


def _completion_feedback(core, c, start, end, targets):
    """Read only the latest completion scalar for each planned (date, target).

    Each seek uses the existing feedback_target_date index with two bound
    parameters, so large unrelated evidence bodies and older corrections never
    become Python entities or accumulate in memory. No multi-thousand-ID SQL.
    """
    result = {}
    for day, target_id in sorted(set(targets)):
        if not start <= day <= end:
            continue
        row = c.execute("""SELECT id,json_extract(data,'$.dimensions.completion') AS completion
            FROM entities WHERE type='feedback' AND archived=0
            AND json_extract(data,'$.target_id')=? AND json_extract(data,'$.business_date')=?
            AND json_type(data,'$.dimensions.completion') IS NOT NULL
            ORDER BY created_at DESC,rowid DESC LIMIT 1""", (target_id, day)).fetchone()
        if row:
            result[(day, target_id)] = {'id': row['id'], 'data': {'dimensions': {'completion': row['completion']}}}
    return result


def _summary(items):
    counts = {'total': len(items), 'done': 0, 'incomplete': 0, 'unreported': 0,
              'other_reported': 0, 'original_results': {}, 'archived': 0, 'pending_review': 0,
              'fixed_scheduled':0, 'attended':0, 'absent':0, 'catchup_needed':0}
    for item in items:
        counts['fixed_scheduled'] += bool(item.get('fixed_schedule'))
        counts['attended'] += item.get('result') == 'attended'
        counts['absent'] += item.get('result') in {'absent','missed_needs_catchup'}
        counts['catchup_needed'] += bool(item.get('catchup_task_id') and not (item.get('catchup_progress') or {}).get('completion_confirmed'))
        counts['archived'] += bool(item.get('target_archived'))
        if item.get('can_review', True) and not item['reported']:
            counts['pending_review'] += 1
        if item['result'] in RESULTS:
            counts[item['result']] += 1
        elif item['reported']:
            counts['other_reported'] += 1
            raw = item['raw_result']
            counts['original_results'][raw] = counts['original_results'].get(raw, 0) + 1
        else:
            counts['unreported'] += 1
    from .review_display import aggregate
    counts['breakdown']=aggregate(items)
    return counts


def _daily(core, c, day, *, plan=None, feedback=None, plan_loaded=False):
    if not plan_loaded:
        plan = _latest_plan(core, c, day)
    from . import occurrences
    fixed = occurrences.project(core,c,day)
    manual_plan = plan
    plan = plan or {'id':None,'version':None,'title':day+' 固定安排','data':{}}
    targets = {(day, b['target_id']) for b in plan['data'].get('blocks', []) if isinstance(b.get('target_id'), str) and b['target_id']}
    feedback = feedback if feedback is not None else _completion_feedback(core, c, day, day, targets)
    from .presentation import Presenter
    presenter=Presenter(core,c)
    items, seen = [], set()
    for block in plan['data'].get('blocks', []):
        target_id = block.get('target_id')
        if not isinstance(target_id, str) or not target_id or target_id in seen:
            continue
        seen.add(target_id)
        row = c.execute('SELECT * FROM entities WHERE id=?', (target_id,)).fetchone()
        target = core.store.entity(row) if row else None
        if target and target['type'] == 'event':
            # Fixed events are projected once, even when AI included a time block.
            continue
        record = feedback.get((day, target_id))
        raw = record['data']['dimensions']['completion'] if record else None
        item = {'target_id': target_id,
                'title': target['title'] if target else block.get('title') or '原计划对象暂不可用',
                'completion_gate': block.get('completion_gate', ''),
                'planned_minutes': block.get('minutes'),
                'result': raw if raw in RESULTS else None,
                'raw_result': raw, 'original_completion': raw,
                'reported': raw is not None and raw != 'unknown',
                'target_version': target['version'] if target else block.get('target_version'),
                'plan_target_version': block.get('target_version'),
                'available': target is not None,
                'target_archived': bool(target and target.get('archived')),
                'can_review': bool(target and not target.get('archived'))}
        item['target_type']=target['type'] if target else None
        if target:item.update(presenter.fields(target))
        else:item.update(owner_id=None,owner_label='原归属待核对',display_title=item['title'])
        for field in ('start', 'end'):
            if block.get(field) is not None:
                item[field] = block[field]
        if record:
            item['feedback_id'] = record['id']
        items.append(item)
    items.extend(fixed)
    review = _daily_record(core, c, day, plan['id'])
    return {'date': day, 'has_plan': manual_plan is not None,
            'can_review': bool(manual_plan or fixed), 'has_fixed_schedule': bool(fixed),
            'schedule_signature': occurrences.signature(items),
            'plan': ({k: plan[k] for k in ('id', 'version', 'title')} | {'mode': plan['data'].get('mode', 'standard')}) if manual_plan else None,
            'items': items, 'summary': _summary(items), 'needs_codex': not bool(manual_plan or fixed),
            'historical_import':bool(plan['data'].get('historical_import')),
            'historical_source_asset_id':plan['data'].get('source_asset_id'),
            'review_id': review['id'] if review else None, 'review_version': review['version'] if review else None}


def query_daily(core, c, p):
    return _daily(core, c, _date(p['date']))


def query_actual_feedback(core, c, p):
    """Dated, immutable feedback history, separate from planned completion rates."""
    day = _date(p.get('date'))
    limit, offset = p.get('limit', 30), p.get('offset', 0)
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or offset < 0:
        raise BusinessError('validation', '实际记录分页参数无效。')
    where = "f.type='feedback' AND f.archived=0 AND json_extract(f.data,'$.business_date')=?"
    total = c.execute('SELECT count(*) FROM entities f WHERE ' + where, (day,)).fetchone()[0]
    items = []
    for row in c.execute("SELECT f.*,t.title AS target_title,t.type AS target_type,t.archived AS target_archived "
                         "FROM entities f LEFT JOIN entities t ON t.id=json_extract(f.data,'$.target_id') WHERE "
                         + where + ' ORDER BY f.created_at DESC,f.rowid DESC LIMIT ? OFFSET ?', (day, limit, offset)):
        feedback = core.store.entity(row)
        data = feedback['data']
        dimensions = data.get('dimensions') or {}
        items.append({'id': feedback['id'], 'business_date': day, 'target_id': data.get('target_id'),
                      'target_title': row['target_title'] or feedback['title'], 'target_type': row['target_type'],
                      'target_available': row['target_title'] is not None,
                      'target_archived': bool(row['target_archived']), 'dimensions': dimensions,
                      'actual_minutes': dimensions.get('actual_minutes'), 'source_text': data.get('source_text', ''),
                      'reported_at': data.get('reported_at') or feedback['created_at'],
                      'supersedes_id': data.get('supersedes_id')})
    return {'date': day, 'items': items, 'total': total,
            'next_offset': offset + len(items) if offset + len(items) < total else None}


def query_weekly(core, c, p):
    start, end = _date(p['start']), _date(p['end'])
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if last < first or (last - first).days > 370:
        raise BusinessError('validation', '回顾日期范围需要为 0 至 370 天。')
    limit, offset = p.get('limit', 200), p.get('offset', 0)
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 500 or offset < 0:
        raise BusinessError('validation', '回顾分页参数无效。')
    plans = {}
    for row in c.execute("""SELECT * FROM entities WHERE type='plan' AND archived=0
        AND status NOT IN ('cancelled','draft') AND json_extract(data,'$.date') BETWEEN ? AND ?
        ORDER BY created_at DESC,rowid DESC""", (start, end)):
        entity = core.store.entity(row)
        plans.setdefault(entity['data']['date'], entity)
    targets = {(day, b['target_id']) for day, plan in plans.items() for b in plan['data'].get('blocks', []) if isinstance(b.get('target_id'), str) and b['target_id']}
    feedback = _completion_feedback(core, c, start, end, targets)
    days, items = [], []
    total = {'planned': 0, 'done': 0, 'incomplete': 0, 'unreported': 0, 'other_reported': 0,
             'days_with_plan': 0, 'days_without_plan': 0, 'original_results': {}, 'archived': 0, 'pending_review': 0,
             'fixed_scheduled':0, 'attended':0, 'absent':0, 'catchup_needed':0, 'days_with_fixed_schedule':0}
    item_index = 0
    for number in range((last - first).days + 1):
        day = (first + dt.timedelta(days=number)).isoformat()
        daily = _daily(core, c, day, plan=plans.get(day), plan_loaded=True, feedback=feedback)
        counts = daily['summary']
        days.append({'date': day, 'has_plan': daily['has_plan'], 'can_review':daily['can_review'], 'has_fixed_schedule':daily['has_fixed_schedule'], 'summary': counts,
                     'historical_import':daily.get('historical_import',False),
                     'needs_codex': daily['needs_codex'], 'plan_id': daily['plan']['id'] if daily['plan'] else None})
        total['days_with_plan' if daily['has_plan'] else 'days_without_plan'] += 1
        total['days_with_fixed_schedule'] += daily['has_fixed_schedule']
        total['planned'] += counts['total']
        for key in ('done', 'incomplete', 'unreported', 'other_reported', 'archived', 'pending_review', 'fixed_scheduled','attended','absent','catchup_needed'):
            total[key] += counts[key]
        for key, value in counts['original_results'].items():
            total['original_results'][key] = total['original_results'].get(key, 0) + value
        for item in daily['items']:
            if offset <= item_index < offset + limit:
                items.append({'date': day, **item})
            item_index += 1
    from .review_display import merge
    total['breakdown']=merge(row for day in days for row in day['summary']['breakdown'])
    reported = total['done'] + total['incomplete'] + total['other_reported']
    binary = total['done'] + total['incomplete']
    return {'start': start, 'end': end, 'days': days, 'summary': total, 'items': items,
            'coverage': {'days_total': len(days), 'planned_items': total['planned'], 'reported_items': reported,
                         'items_total': item_index, 'items_returned': len(items),
                         'items_complete': offset == 0 and len(items) == item_index,
                         'next_offset': offset + len(items) if offset + len(items) < item_index else None,
                         'metrics_complete': not any(d.get('historical_import') for d in days),
                         'historical_import_days':sum(bool(d.get('historical_import')) for d in days), 'denominator': ('daily_plan_and_dated_fixed_occurrences' if total['fixed_scheduled'] else 'latest_plan_unique_target_per_business_date'),
                         'feedback_coverage': reported / total['planned'] if total['planned'] else None,
                         'confirmed_completion_rate': total['done'] / binary if binary else None,
                         'missing_plan_is_failure': False, 'unknown_is_zero': False}}


def submit_daily(core, c, p, rid):
    day = _date(p['date'])
    plan = _latest_plan(core, c, day)
    daily = _daily(core,c,day,plan=plan,plan_loaded=True)
    if not daily['can_review']:
        raise BusinessError('review_no_plan','这一天没有计划或固定安排。请在复盘页选择“记录实际情况”，为任务或日程填写反馈，也可以写下文字小结。',{'date':day,'needs_codex':True})
    if (plan and (p.get('plan_id') != plan['id'] or type(p.get('plan_version')) is not int or p['plan_version'] != plan['version'])) or (not plan and p.get('plan_id') is not None):
        raise BusinessError('review_plan_conflict','每日计划已经更新，请保留你的选择并重新读取后核对。')
    from . import occurrences
    targets = {item.get('item_id',item['target_id']): item for item in daily['items']}
    answers = p.get('answers')
    if not isinstance(answers,list) or len(answers)>100:
        raise BusinessError('validation','每日复盘需要不超过 100 个明确结果。')
    seen = set()
    for answer in answers:
        if not isinstance(answer,dict) or set(answer) not in ({'target_id','result'}, {'item_id','result'}):
            raise BusinessError('validation','复盘只接受对象与明确结果。')
        key = answer.get('item_id',answer.get('target_id'))
        item = targets.get(key)
        if not item or key in seen or answer['result'] not in dict(item.get('choices',occurrences.TASK_CHOICES)):
            raise BusinessError('validation','复盘对象需属于当前日程且不能重复，请选择有效结果。')
        if item.get('target_archived'):
            raise BusinessError('task_deleted','这项记录已删除，历史反馈保留。请先恢复再更正。')
        if not item.get('available'):
            raise BusinessError('not_found','原计划对象已不可用，请先核对原任务或日程；实际发生的情况仍可保存为文字小结。')
        if item.get('fixed_schedule'):
            if p.get('schedule_signature') != daily['schedule_signature']:
                raise BusinessError('review_plan_conflict','课表或本次反馈已更新，请重新读取后核对。')
            if not item['can_review']:
                raise BusinessError('occurrence_feedback','未来课程不能提前登记为实际出勤或缺课。')
        seen.add(key)
    plan = plan or {'id':None,'version':None}
    review = _daily_record(core, c, day, plan['id'])
    if not answers:
        return {'review': review, 'feedback_ids': [], 'changed_count': 0, 'summary': daily['summary']}
    feedback_ids = []
    for answer in answers:
        existing = targets[answer.get('item_id',answer.get('target_id'))]
        if existing.get('fixed_schedule'):
            identifier = occurrences.record(core,c,existing,answer['result'],rid)
            if identifier: feedback_ids.append(identifier)
            continue
        if existing['raw_result'] == answer['result']:
            continue
        payload = {'target_id': answer['target_id'], 'business_date': day,
                   'dimensions': {'completion': answer['result']},
                   'source_text': '用户在 ' + day + ' 每日复盘中明确选择：' + ('完成' if answer['result'] == 'done' else '未完成') + '。'}
        if existing.get('feedback_id'):
            payload['supersedes_id'] = existing['feedback_id']
        record = core.feedback(c, payload, rid)
        feedback_ids.append(record['id'])
    after = _daily(core, c, day)
    results = {item.get('item_id',item['target_id']): {item.get('review_dimension','completion'): item['raw_result'], 'feedback_id': item.get('feedback_id')}
               for item in after['items'] if item['reported']}
    data = {'review_kind': 'daily', 'date': day, 'start': day, 'end': day,
            'plan_id': plan['id'], 'plan_version': plan['version'], 'results': results,
            'summary': after['summary'], 'no_reply_means': 'unknown'}
    if review is None:
        review = core._create(c, {'type': 'review', 'title': day + ' 每日复盘', 'data': data}, rid)
    elif review['data'] != data:
        review = core._save(c, review, {**review, 'data': data}, rid, 'daily_review')
    return {'review': review, 'feedback_ids': feedback_ids, 'changed_count': len(feedback_ids), 'summary': after['summary']}


def legacy_checkin(core, c, p, rid):
    """Keep the explicit Codex workflow while making repeated clicks idempotent."""
    day = _date(p['date'])
    plan = _latest_plan(core, c, day)
    explicit = 'target_ids' in p and p['target_ids'] is not None
    if not explicit and plan is None:
        return {'entity': None, 'needs_codex': True, 'reused': False}
    target_ids = p['target_ids'] if explicit else [b['target_id'] for b in plan['data'].get('blocks', [])]
    if not isinstance(target_ids, list) or len(target_ids) > 100 or any(not isinstance(id, str) or not id for id in target_ids):
        raise BusinessError('validation', '复核对象需要为不超过 100 个对象的明确列表。')
    target_ids = list(dict.fromkeys(target_ids))
    active_targets = []
    for target_id in target_ids:
        target = core.store.get(c, target_id)
        if target.get('archived'):
            if explicit:
                raise BusinessError('task_deleted', '这项任务已删除，请先恢复再复核；原记录仍然保留。')
            continue
        active_targets.append(target_id)
    target_ids = active_targets
    if not target_ids:
        return {'entity': None, 'needs_codex': plan is None, 'reused': False}
    scope = {'date': day, 'plan_id': plan['id'] if plan else None,
             'plan_version': plan['version'] if plan else None, 'target_ids': sorted(target_ids),
             'source_kind': 'explicit_targets' if explicit else 'plan'}
    key = hashlib.sha256(encode({k: v for k, v in scope.items() if k != 'source_kind'}).encode('utf-8')).hexdigest()
    rows = c.execute("""SELECT * FROM entities WHERE type='checkin' AND archived=0
        AND status NOT IN ('cancelled','draft') AND json_extract(data,'$.date')=?
        ORDER BY created_at DESC,rowid DESC""", (day,))
    for row in rows:
        existing = core.store.entity(row)
        data = existing['data']
        if data.get('scope_key') == key:
            return {'entity': existing, 'needs_codex': False, 'reused': True}
        if data.get('scope_key'):
            continue
        ids = {q.get('target_id') for q in data.get('questions', [])}
        if ids != set(target_ids):
            continue
        if data.get('plan_id') and data['plan_id'] != scope['plan_id']:
            continue
        if data.get('plan_version') is not None and data['plan_version'] != scope['plan_version']:
            continue
        # Pre-redesign records lack plan identifiers. Only reuse a plan-derived
        # record when it was created after the selected plan, never an older plan.
        if plan:
            existing_order = c.execute('SELECT rowid FROM entities WHERE id=?', (existing['id'],)).fetchone()[0]
            plan_order = c.execute('SELECT rowid FROM entities WHERE id=?', (plan['id'],)).fetchone()[0]
            if (existing['created_at'], existing_order) < (plan['created_at'], plan_order):
                continue
        return {'entity': existing, 'needs_codex': False, 'reused': True}
    questions = []
    for id in target_ids:
        target = core.store.get(c, id)
        gates = target['data'].get('gates') or [{'id': 'completion', 'label': target['data'].get('completion_gate') or '完成情况'}]
        for gate in gates:
            if len(questions) >= 200:
                raise BusinessError('limit', '完成门超过上限，请分批选择；未截断问题集。')
            questions.append({'id': new_id(), 'target_id': id, 'target_version': target['version'],
                              'gate_id': gate['id'], 'title': target['title'], 'question': gate['label'],
                              'number': len(questions) + 1})
    entity = core._create(c, {'type': 'checkin', 'title': day + ' 完成情况', 'data': {
        **scope, 'scope_key': key, 'questions': questions,
        'coverage': {'targets': len(target_ids), 'questions': len(questions), 'complete': True},
        'delivery_state': 'not_delivered', 'source_revision': core.store.meta(c, 'revision'),
        'no_reply_means': 'unknown'}}, rid)
    return {'entity': entity, 'needs_codex': False, 'reused': False}


def set_task_completion(core,c,p,rid):
    """One explicit completion fact, shared by daily review and task views."""
    day=_date(p.get('business_date'))
    if not isinstance(p.get('result'),str) or p['result'] not in RESULTS:
        raise BusinessError('validation','请选择完成或未完成。')
    target=core._versioned(c,{'id':p.get('target_id'),'version':p.get('target_version')})
    if target['type'] not in {'task','event','milestone'} or target['archived'] or target['status']=='cancelled':
        raise BusinessError('completion_target','请选择仍可记录完成情况的任务、日程或节点。')
    if 'plan_id' in p or 'plan_version' in p:
        plan=_latest_plan(core,c,day)
        if not plan or (p.get('plan_id'),p.get('plan_version'))!=(plan['id'],plan['version']):
            raise BusinessError('review_plan_conflict','当日计划已变化，请重新读取后核对。')
        if target['id'] not in {b.get('target_id') for b in plan['data'].get('blocks',[])}:
            raise BusinessError('review_target','这项任务不在当前每日计划中。')
    row=c.execute("""SELECT * FROM entities WHERE type='feedback' AND archived=0
        AND json_extract(data,'$.target_id')=? AND json_extract(data,'$.business_date')=?
        AND json_type(data,'$.dimensions.completion') IS NOT NULL
        ORDER BY created_at DESC,rowid DESC LIMIT 1""",(target['id'],day)).fetchone()
    prior=core.store.entity(row) if row else None
    from .task_views import state
    matches_current=target['type']!='task' or state(core,c,target['id'],day)==p['result']
    if prior and prior['data']['dimensions']['completion']==p['result'] and matches_current:
        return {'entity':target,'feedback':prior,'changed':False,'completion_state':p['result']}
    payload={'target_id':target['id'],'business_date':day,'dimensions':{'completion':p['result']},
             'source_text':'用户在 '+day+' 明确点击：'+('完成' if p['result']=='done' else '未完成')+'。'}
    if prior:payload['supersedes_id']=prior['id']
    record=core.feedback(c,payload,rid)
    return {'entity':target,'feedback':record,'changed':True,'completion_state':p['result']}
