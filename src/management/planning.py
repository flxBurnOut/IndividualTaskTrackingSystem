"""Scoped planning constraints and evidence-sensitive recurring warnings.

Both entry points are read-only. Actual feedback is stored separately and is
never rejected because it disagrees with a plan or protected time.
"""
from __future__ import annotations

import calendar
import datetime as dt
from zoneinfo import ZoneInfo

from .schemas import BusinessError


ATTENDANCE_CLOSES = frozenset({'exam', 'lecture', 'tutorial', 'lab', 'meeting', 'interview', 'appointment'})
MAX_OCCURRENCES_PER_ENTITY = 20_000


def _date(value):
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise BusinessError('validation', '规则或日程日期无效。') from exc


def _minute(value):
    try:
        time = dt.time.fromisoformat(value)
        if time.second or time.microsecond or time.tzinfo:
            raise ValueError()
        return time.hour * 60 + time.minute
    except (TypeError, ValueError) as exc:
        raise BusinessError('validation', '规则时段需要有效的小时:分钟。') from exc


def _selection(value):
    if value is None or value == '':
        return None
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise BusinessError('validation', '规则的类型筛选应为名称或名称列表。')
    return set(values)


class _Scope:
    def __init__(self, core, connection):
        self.core, self.c = core, connection
        self.entities, self.ancestors = {}, {}

    def get(self, id):
        if id not in self.entities:
            self.entities[id] = self.core.store.get(self.c, id)
        return self.entities[id]

    def ancestry(self, entity):
        if entity['id'] not in self.ancestors:
            ancestors = {entity['id']}
            parent = entity['data'].get('owner_id') if entity['type'] == 'event' else entity.get('parent_id')
            while parent:
                if parent in ancestors or len(ancestors) > 64:
                    raise BusinessError('cycle', '规则作用域的对象层级异常。')
                ancestors.add(parent)
                current = self.get(parent)
                parent = current.get('parent_id')
            self.ancestors[entity['id']] = ancestors
        return self.ancestors[entity['id']]

    def matches(self, rule, entity):
        data = rule['data']
        if rule.get('parent_id') and rule['parent_id'] not in self.ancestry(entity):
            return False
        types = _selection(data.get('target_types'))
        if types is not None and entity['type'] not in types:
            return False
        for field, expected_type in [('event_kind', 'event'), ('task_kind', 'task')]:
            selected = _selection(data.get(field))
            if selected is not None and (entity['type'] != expected_type or entity['data'].get(field) not in selected):
                return False
        return True


def _rules(core, c, day, kinds):
    return [r for r in core._rules(c, day) if r['data'].get('rule_kind') in kinds
            and r['status'] not in {'done', 'cancelled', 'draft'} and r['data'].get('enabled', True)]


def validate_scoped_rules(core, c, day, blocks, context):
    """Validate each rule only against matching ownership descendants and kinds.

    Invoke after create_plan has normalized block durations. The returned
    diagnostics belong in the saved proposal/plan; unknown duration or untimed
    blocks cannot be represented as proven compliance with time constraints.
    """
    _date(day)
    scope = _Scope(core, c)
    targets = [(block, scope.get(block['target_id'])) for block in blocks]
    diagnostics = {'applied_rules': [], 'unknowns': []}
    for rule in _rules(core, c, day, {'capacity', 'protected_time'}):
        matches = [(b, e) for b, e in targets if scope.matches(rule, e)]
        if not matches:
            continue
        data, kind = rule['data'], rule['data']['rule_kind']
        ids = [e['id'] for _, e in matches]
        detail = {'rule_id': rule['id'], 'rule_kind': kind, 'target_ids': ids}
        if kind == 'capacity':
            capacity = data.get('minutes')
            if type(capacity) is not int or capacity < 0:
                raise BusinessError('validation', '容量规则需要非负整数分钟。', {'rule_id': rule['id']})
            known, unknown = 0, []
            for block, entity in matches:
                minutes = block.get('minutes')
                if minutes is None and block.get('start') and block.get('end'):
                    minutes = _minute(block['end']) - _minute(block['start'])
                if minutes is None:
                    unknown.append(entity['id'])
                elif type(minutes) is not int or minutes <= 0:
                    raise BusinessError('validation', '计划块分钟必须为正整数。')
                else:
                    known += minutes
            detail.update(known_minutes=known, capacity_minutes=capacity)
            if known > capacity:
                raise BusinessError('capacity', '所选对象的估时超过容量规则“' + rule['title'] + '”。', detail)
            if unknown:
                diagnostics['unknowns'].append({'rule_id': rule['id'], 'target_ids': unknown,
                                               'reason': '部分匹配对象估时未知，容量是否足够仍待确认。'})
        else:
            start, end = _minute(data.get('start')), _minute(data.get('end'))
            spans = [(start, end)] if end > start else [(0, end), (start, 1440)]
            for block, entity in matches:
                if not block.get('start') or not block.get('end'):
                    diagnostics['unknowns'].append({'rule_id': rule['id'], 'target_ids': [entity['id']],
                                                   'reason': '未指定具体时段，保护时段约束将在排时时核对。'})
                    continue
                a, b = _minute(block['start']), _minute(block['end'])
                if any(a < hi and b > lo for lo, hi in spans):
                    raise BusinessError('schedule_conflict', '对象“' + entity['title'] + '”与保护时段“' + rule['title'] + '”冲突。', detail)
        diagnostics['applied_rules'].append(detail)
    return diagnostics


def _offset(rule):
    data = rule['data']
    days, months = data.get('days_before', 0), data.get('calendar_months_before', 0)
    if type(days) is not int or days < 0 or type(months) is not int or months < 0:
        raise BusinessError('validation', '警戒提前量需要非负整数。', {'rule_id': rule['id']})
    return days, months


def _month_shift(date, months):
    ordinal = date.year * 12 + date.month - 1 + months
    year, month = divmod(ordinal, 12)
    if year < 1:
        return dt.date.min
    if year > 9999:
        return dt.date.max
    month += 1
    return dt.date(year, month, min(date.day, calendar.monthrange(year, month)[1]))


def _threshold(due, rule):
    days, months = _offset(rule)
    by_days = dt.date.fromordinal(max(1, due.toordinal() - days))
    return min(by_days, _month_shift(due, -months))


def _occurrences(data, through):
    from .timetable import occurs_on
    for day in _unfiltered__occurrences(data, through):
        if occurs_on(data, day):
            yield day


def _unfiltered__occurrences(data, through):
    origin = _date(data['date'])
    limit = min(through, _date(data['until'])) if data.get('until') else through
    repeat = data.get('recurrence', 'none')
    if origin > limit:
        return
    if repeat == 'none':
        yield origin
        return
    if repeat in {'daily', 'weekly'}:
        step = 1 if repeat == 'daily' else 7
        for ordinal in range(origin.toordinal(), limit.toordinal() + 1, step):
            yield dt.date.fromordinal(ordinal)
        return
    if repeat != 'monthly':
        raise BusinessError('validation', '不支持的周期类型。')
    start = origin.year * 12 + origin.month - 1
    end = limit.year * 12 + limit.month - 1
    for ordinal in range(start, end + 1):
        year, month = divmod(ordinal, 12)
        month += 1
        if origin.day <= calendar.monthrange(year, month)[1]:
            date = dt.date(year, month, origin.day)
            if date <= limit:
                yield date


def _event_feedback(c):
    """One indexed source scan; preserve independent latest explicit dimensions."""
    result = {}
    rows = c.execute("""SELECT f.data FROM entities f JOIN entities e ON e.id=json_extract(f.data,'$.target_id')
        WHERE f.type='feedback' AND f.archived=0 AND e.type='event'
        ORDER BY f.created_at DESC,f.rowid DESC""")
    for row in rows:
        import json
        data = json.loads(row[0])
        key = (data.get('target_id'), data.get('business_date'))
        dimensions = result.setdefault(key, {})
        for name in ('attendance', 'completion'):
            if name in data.get('dimensions', {}) and name not in dimensions:
                dimensions[name] = data['dimensions'][name]
    return result


def _business_date(data, occurrence, zone):
    if not data.get('start') or not data.get('timezone'):
        return occurrence
    minutes = _minute(data['start'])
    instant = dt.datetime.combine(occurrence, dt.time(minutes // 60, minutes % 60), ZoneInfo(data['timezone']))
    return instant.astimezone(zone).date()


def _closed(entity, due, effective, rules, feedback):
    dimensions = feedback.get((entity['id'], due.isoformat()), {})
    if dimensions.get('completion') == 'done':
        return True
    return (effective.get('event_kind') in ATTENDANCE_CLOSES and dimensions.get('attendance') == 'attended'
            and all(rule['data'].get('attendance_closes_occurrence', True) for rule in rules))


def warning_scan(core, c, day):
    """Scan all source entities and matching rule scopes, without changing facts.

    Recurring unknown outcomes are aggregated per entity/severity with occurrence
    counts. This keeps history visible without one in-memory row per old lesson.
    An exceptional expansion limit is explicitly diagnosed, never called complete.
    """
    today = _date(day)
    rules = _rules(core, c, day, {'warning'})
    if not rules:
        return []
    scope, feedback = _Scope(core, c), _event_feedback(c)
    zone = ZoneInfo(core.store.meta(c, 'settings')['timezone'])
    risks = []
    rows = c.execute("SELECT * FROM entities WHERE type IN ('task','project','assessment','milestone','event') AND archived=0 AND status NOT IN ('done','cancelled','draft')")
    for row in rows:
        entity = core.store.entity(row)
        matching = [rule for rule in rules if scope.matches(rule, entity)]
        if not matching:
            continue
        data = entity['data']
        base = {'id': entity['id'], 'title': entity['title'], 'rule_ids': [r['id'] for r in matching],
                'occurrences_complete': True}
        due_text = data.get('date') if entity['type'] == 'event' else data.get('due_date')
        if not due_text:
            risks.append({**base, 'reason': '日期待确认', 'severity': 'unknown', 'occurrence_date': None})
            continue
        if entity['type'] != 'event':
            due = _date(due_text)
            if any(today >= _threshold(due, rule) for rule in matching):
                risks.append({**base, 'reason': ('已过日期，结果待核对：' if today > due else '临近日期：') + due.isoformat(),
                              'severity': 'urgent' if today >= due else 'warning', 'due_date': due.isoformat(), 'occurrence_date': due.isoformat()})
            continue
        max_days = max(_offset(rule)[0] for rule in matching)
        max_months = max(_offset(rule)[1] for rule in matching)
        horizon = max(dt.date.fromordinal(min(dt.date.max.toordinal(), today.toordinal() + max_days)), _month_shift(today, max_months))
        # Calendar subtraction clips the day at month end, so include the entire
        # horizon month before applying the exact per-occurrence threshold.
        if max_months:
            horizon = horizon.replace(day=calendar.monthrange(horizon.year, horizon.month)[1])
        horizon = dt.date.fromordinal(min(dt.date.max.toordinal(), horizon.toordinal() + 1))
        buckets, scanned = {}, 0
        for occurrence in _occurrences(data, horizon):
            scanned += 1
            if scanned > MAX_OCCURRENCES_PER_ENTITY:
                risks.append({**base, 'severity': 'unknown', 'reason': '周期发生次数超过单次核查预算，请缩小周期范围或核对历史。',
                              'occurrence_date': occurrence.isoformat(), 'occurrences_complete': False,
                              'occurrences_scanned': MAX_OCCURRENCES_PER_ENTITY})
                break
            exception = data.get('exceptions', {}).get(occurrence.isoformat(), {})
            if exception.get('cancelled'):
                continue
            effective = {**data, **exception}
            due = _business_date(effective, occurrence, zone)
            if _closed(entity, due, effective, matching, feedback):
                continue
            if not any(today >= _threshold(due, rule) for rule in matching):
                continue
            key = 'past' if due < today else 'today' if due == today else 'future'
            bucket = buckets.setdefault(key, {'count': 0, 'first': due, 'last': due, 'occurrence_date': occurrence})
            bucket['count'] += 1
            bucket['first'], bucket['last'] = min(bucket['first'], due), max(bucket['last'], due)
        for key, bucket in buckets.items():
            first, last = bucket['first'].isoformat(), bucket['last'].isoformat()
            reason = ('已过日期，结果待核对：' if key == 'past' else '临近日期：') + first
            if bucket['count'] > 1:
                reason += ' 至 ' + last + '，共 ' + str(bucket['count']) + ' 次发生待核对'
            risks.append({**base, 'severity': 'urgent' if key in {'past', 'today'} else 'warning', 'reason': reason,
                          'occurrence_date': bucket['occurrence_date'].isoformat(), 'due_date': first,
                          'last_due_date': last, 'occurrence_count': bucket['count']})
    return sorted(risks, key=lambda r: ({'urgent': 0, 'warning': 1, 'unknown': 2}[r['severity']], r.get('due_date', ''), r['id']))
