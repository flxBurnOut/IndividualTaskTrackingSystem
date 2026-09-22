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
        AND json_extract(data,'$.plan_id')=? ORDER BY created_at DESC,rowid DESC LIMIT 1""", (day, plan_id)).fetchone()
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
              'other_reported': 0, 'original_results': {}, 'archived': 0, 'pending_review': 0}
    for item in items:
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
    return counts


def _daily(core, c, day, *, plan=None, feedback=None, plan_loaded=False):
    if not plan_loaded:
        plan = _latest_plan(core, c, day)
    if plan is None:
        return {'date': day, 'has_plan': False, 'plan': None, 'items': [],
                'summary': _summary([]), 'needs_codex': True,
                'review_id': None, 'review_version': None}
    targets = {(day, b['target_id']) for b in plan['data'].get('blocks', []) if isinstance(b.get('target_id'), str) and b['target_id']}
    feedback = feedback if feedback is not None else _completion_feedback(core, c, day, day, targets)
    items, seen = [], set()
    for block in plan['data'].get('blocks', []):
        target_id = block.get('target_id')
        if not isinstance(target_id, str) or not target_id or target_id in seen:
            continue
        seen.add(target_id)
        row = c.execute('SELECT * FROM entities WHERE id=?', (target_id,)).fetchone()
        target = core.store.entity(row) if row else None
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
        for field in ('start', 'end'):
            if block.get(field) is not None:
                item[field] = block[field]
        if record:
            item['feedback_id'] = record['id']
        items.append(item)
    review = _daily_record(core, c, day, plan['id'])
    return {'date': day, 'has_plan': True,
            'plan': {k: plan[k] for k in ('id', 'version', 'title')} | {'mode': plan['data'].get('mode', 'standard')},
            'items': items, 'summary': _summary(items), 'needs_codex': False,
            'review_id': review['id'] if review else None, 'review_version': review['version'] if review else None}


def query_daily(core, c, p):
    return _daily(core, c, _date(p['date']))


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
             'days_with_plan': 0, 'days_without_plan': 0, 'original_results': {}, 'archived': 0, 'pending_review': 0}
    item_index = 0
    for number in range((last - first).days + 1):
        day = (first + dt.timedelta(days=number)).isoformat()
        daily = _daily(core, c, day, plan=plans.get(day), plan_loaded=True, feedback=feedback)
        counts = daily['summary']
        days.append({'date': day, 'has_plan': daily['has_plan'], 'summary': counts,
                     'needs_codex': daily['needs_codex'], 'plan_id': daily['plan']['id'] if daily['plan'] else None})
        total['days_with_plan' if daily['has_plan'] else 'days_without_plan'] += 1
        total['planned'] += counts['total']
        for key in ('done', 'incomplete', 'unreported', 'other_reported', 'archived', 'pending_review'):
            total[key] += counts[key]
        for key, value in counts['original_results'].items():
            total['original_results'][key] = total['original_results'].get(key, 0) + value
        for item in daily['items']:
            if offset <= item_index < offset + limit:
                items.append({'date': day, **item})
            item_index += 1
    reported = total['done'] + total['incomplete'] + total['other_reported']
    binary = total['done'] + total['incomplete']
    return {'start': start, 'end': end, 'days': days, 'summary': total, 'items': items,
            'coverage': {'days_total': len(days), 'planned_items': total['planned'], 'reported_items': reported,
                         'items_total': item_index, 'items_returned': len(items),
                         'items_complete': offset == 0 and len(items) == item_index,
                         'next_offset': offset + len(items) if offset + len(items) < item_index else None,
                         'metrics_complete': True, 'denominator': 'latest_plan_unique_target_per_business_date',
                         'feedback_coverage': reported / total['planned'] if total['planned'] else None,
                         'confirmed_completion_rate': total['done'] / binary if binary else None,
                         'missing_plan_is_failure': False, 'unknown_is_zero': False}}


def submit_daily(core, c, p, rid):
    day = _date(p['date'])
    plan = _latest_plan(core, c, day)
    if plan is None:
        raise BusinessError('review_no_plan', '这一天没有每日计划，请到 Codex 按实际情况复盘。', {'date': day, 'needs_codex': True})
    if p.get('plan_id') != plan['id'] or type(p.get('plan_version')) is not int or p['plan_version'] != plan['version']:
        raise BusinessError('review_plan_conflict', '每日计划已经更新，请保留你的选择并重新读取后核对。',
                            {'date': day, 'plan_id': plan['id'], 'plan_version': plan['version']})
    daily = _daily(core, c, day, plan=plan, plan_loaded=True)
    targets = {item['target_id']: item for item in daily['items']}
    answers = p.get('answers')
    if not isinstance(answers, list) or len(answers) > 100:
        raise BusinessError('validation', '每日复盘需要不超过 100 个明确结果。')
    seen = set()
    for answer in answers:
        if not isinstance(answer, dict) or set(answer) != {'target_id', 'result'}:
            raise BusinessError('validation', '复盘只接受对象与明确完成结果。')
        target = answer['target_id']
        if target not in targets or target in seen or answer['result'] not in RESULTS:
            raise BusinessError('validation', '复盘对象需属于当前计划且不能重复，结果请选择完成或未完成。')
        if targets[target].get('target_archived'):
            raise BusinessError('task_deleted', '这项任务已删除，原计划和反馈保留。请先恢复任务再记录或更正反馈。')
        if not targets[target]['available']:
            raise BusinessError('not_found', '原计划对象已不可用，请到 Codex 核对。')
        seen.add(target)
    review = _daily_record(core, c, day, plan['id'])
    if not answers:
        return {'review': review, 'feedback_ids': [], 'changed_count': 0, 'summary': daily['summary']}
    feedback_ids = []
    for answer in answers:
        existing = targets[answer['target_id']]
        if existing['raw_result'] == answer['result']:
            continue
        payload = {'target_id': answer['target_id'], 'business_date': day,
                   'dimensions': {'completion': answer['result']},
                   'source_text': '用户在 ' + day + ' 每日复盘中明确选择：' + ('完成' if answer['result'] == 'done' else '未完成') + '。'}
        if existing.get('feedback_id'):
            payload['supersedes_id'] = existing['feedback_id']
        record = core.feedback(c, payload, rid)
        feedback_ids.append(record['id'])
    after = _daily(core, c, day, plan=plan, plan_loaded=True)
    results = {item['target_id']: {'completion': item['raw_result'], 'feedback_id': item.get('feedback_id')}
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
