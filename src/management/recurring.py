"""Bounded, deterministic preparation tasks. All public queries are read-only.

set_rule/materialize run inside the caller's command transaction; they never commit
or advance the revision. initialize is startup-only. Generated tasks are immutable
snapshots from this module's perspective: edits, feedback and plans always survive
rule changes. Reconciliation is explicit, never an invented completion.
"""
from __future__ import annotations
import calendar
import copy
import datetime as dt
import hashlib
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .schemas import BusinessError
from .storage import encode, now

MAX_WINDOW = 31
MAX_RULES = 200
MAX_RESULTS = 500
ANCHOR_TYPES = {'event', 'milestone', 'assessment', 'task'}
CLOSED = {'done', 'cancelled'}


def initialize(c):
    # execute, not executescript: safe for callers already in a startup transaction.
    c.execute('''CREATE TABLE IF NOT EXISTS recurring_occurrences(
        rule_id TEXT NOT NULL,occurrence_key TEXT NOT NULL,anchor_id TEXT NOT NULL,
        occurrence_date TEXT NOT NULL,business_date TEXT NOT NULL,task_id TEXT NOT NULL UNIQUE,
        snapshot TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(rule_id,occurrence_key))''')
    c.execute('CREATE INDEX IF NOT EXISTS recurring_dates ON recurring_occurrences(rule_id,business_date)')
    c.execute('''CREATE TABLE IF NOT EXISTS recurring_alerts(
        rule_id TEXT PRIMARY KEY,signature TEXT NOT NULL,updated_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS recurring_cursors(
        rule_id TEXT PRIMARY KEY,last_business_date TEXT NOT NULL,business_timezone TEXT NOT NULL,updated_at TEXT NOT NULL)''')


def _hash(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def _date(value, label='日期', optional=False):
    if optional and value in (None, ''):
        return None
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError()
        result = dt.date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError()
        return result
    except (TypeError, ValueError) as exc:
        raise BusinessError('validation', label + '需要明确的 YYYY-MM-DD 日期。') from exc


def _zone(core, c):
    settings = core.store.meta(c, 'settings') or {}
    try:
        return ZoneInfo(settings.get('timezone') or 'Asia/Shanghai')
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise BusinessError('validation', '业务时区无效。') from exc


def _today(core, c, instant=None):
    instant = instant or dt.datetime.now(dt.timezone.utc)
    if instant.tzinfo is None:
        raise BusinessError('validation', '调度时刻必须包含时区。')
    return instant.astimezone(_zone(core, c)).date()


def _window(core, c, p, instant=None):
    today = _today(core, c, instant)
    start = _date(p.get('start') or today.isoformat(), '窗口开始')
    end = _date(p.get('end') or start.isoformat(), '窗口结束')
    if end < start or (end - start).days >= MAX_WINDOW:
        raise BusinessError('recurring_window', '每次生成或预览限连续 1 到 31 天；不会无限回填历史。')
    if start < dt.date(2, 1, 1) or end > dt.date(9998, 1, 1):
        raise BusinessError('validation', '准备任务日期超出支持范围。')
    return start, end, today


def _scope(core, c, anchor):
    current = anchor['data'].get('owner_id') if anchor['type'] == 'event' else anchor.get('parent_id')
    seen = {anchor['id']}
    while current:
        if current in seen or len(seen) > 32:
            raise BusinessError('cycle', '关联节点的归属层级异常。')
        seen.add(current)
        owner = core.store.get(c, current)
        if owner['type'] in {'course', 'project', 'activity', 'domain'}:
            return owner['id']
        current = owner.get('parent_id')
    return None


def _series(anchor):
    d = anchor['data']
    if anchor['type'] != 'event':
        return {'anchor_id': anchor['id'], 'kind': 'deadline'}
    signature = {'anchor_id': anchor['id'], **{k: d.get(k) for k in ('date', 'recurrence', 'start', 'timezone', 'time_kind')}}
    if d.get('timetable_id'):
        signature.update({k: d.get(k) for k in ('timetable_id', 'semester_start', 'semester_end', 'teaching_weeks', 'timetable_enabled')})
    if 'week_numbering' in d or 'recess_weeks' in d:
        signature.update({'week_numbering': d.get('week_numbering', 'calendar'), 'recess_weeks': d.get('recess_weeks', [])})
    return signature


def _active(rule):
    return not rule['archived'] and rule['status'] not in CLOSED and rule['data'].get('enabled') is True


def _rule_issue(core, c, rule, anchor, as_of=None):
    count = c.execute('SELECT count(*) FROM recurring_occurrences WHERE rule_id=?', (rule['id'],)).fetchone()[0]
    result = []
    if not _active(rule):
        if count:
            result.append({'code': 'rule_disabled', 'message': '规则已停用；已生成任务保留，未自动取消或完成。'})
        return result
    if anchor['archived'] or anchor['status'] in CLOSED:
        result.append({'code': 'anchor_closed', 'message': '关联节点已完成、取消或归档；停止生成，已有任务保留待核对。'})
    elif anchor['type'] != 'event':
        from .daily_flow import completed_on_or_before
        if completed_on_or_before(core, c, anchor, as_of or _today(core, c).isoformat()):
            result.append({'code': 'anchor_completed', 'message': '关联截止节点已有明确完成反馈；停止补生成，已有准备任务保留待核对。'})
    if count and anchor['type'] == 'event' and rule['data'].get('anchor_series') != _series(anchor):
        result.append({'code': 'anchor_changed', 'message': '日程日期、频率、时区或教学周规则已改变；确认重新绑定后才能生成新系列，旧任务不会改写。'})
    if anchor['type'] == 'event' and not anchor['data'].get('date') or anchor['type'] != 'event' and not anchor['data'].get('due_date'):
        result.append({'code': 'anchor_date_unknown', 'message': '关联节点没有明确日期，未猜测日期或生成任务。'})
    owner = _scope(core, c, anchor)
    if owner and core.store.get(c, owner)['archived']:
        result.append({'code': 'scope_archived', 'message': '所属范围已归档，未生成任务。'})
    return result


def query_rules(core, c, p):
    limit, offset = p.get('limit', 50), p.get('offset', 0)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise BusinessError('validation', '规则分页范围无效。')
    args, clauses = [], ["type='recurring_rule'", 'archived=0']
    if p.get('anchor_id'):
        core.store.get(c, p['anchor_id'])
        clauses.append("json_extract(data,'$.anchor_id')=?")
        args.append(p['anchor_id'])
    if p.get('scope_id'):
        scope = core.store.get(c, p['scope_id'])
        clauses.append("""json_extract(data,'$.anchor_id') IN (
            WITH RECURSIVE scope(id) AS (SELECT ? UNION ALL SELECT e.id FROM entities e JOIN scope s ON e.parent_id=s.id)
            SELECT id FROM scope UNION SELECT e.id FROM entities e WHERE e.type='event' AND json_extract(e.data,'$.owner_id') IN (SELECT id FROM scope))""")
        args.append(scope['id'])
    where = ' AND '.join(clauses)
    total = c.execute('SELECT count(*) FROM entities WHERE ' + where, args).fetchone()[0]
    items = []
    for row in c.execute('SELECT * FROM entities WHERE ' + where + ' ORDER BY created_at,id LIMIT ? OFFSET ?', (*args, limit, offset)):
        rule = core.store.entity(row)
        try:
            anchor = core.store.get(c, rule['data']['anchor_id'])
            rule_issues = _rule_issue(core, c, rule, anchor)
        except (BusinessError, KeyError) as error:
            rule_issues = [{'code': 'rule_invalid', 'message': '规则或关联节点需要修复：' + str(error)[:500]}]
        rule['materialized_count'] = c.execute('SELECT count(*) FROM recurring_occurrences WHERE rule_id=?', (rule['id'],)).fetchone()[0]
        rule['issues'] = rule_issues
        items.append(rule)
    return {'items': items, 'total': total, 'next_offset': offset + len(items) if offset + len(items) < total else None}


def _rules(core, c, p):
    if p.get('rule_id'):
        rule = core.store.get(c, p['rule_id'])
        if rule['type'] != 'recurring_rule':
            raise BusinessError('recurring_rule', '请选择节点前准备规则。')
        return [rule]
    rows = c.execute("SELECT * FROM entities WHERE type='recurring_rule' AND archived=0 ORDER BY id LIMIT ?", (MAX_RULES + 1,)).fetchall()
    if len(rows) > MAX_RULES:
        raise BusinessError('recurring_limit', '规则超过单批上限，请按规则分别生成或预览。')
    return [core.store.entity(row) for row in rows]


def _occurrences(anchor, low, high):
    from .timetable import occurs_on
    for key, day in _unfiltered__occurrences(anchor, low, high):
        if anchor['type'] != 'event' or occurs_on(anchor['data'], day):
            yield key, day


def _unfiltered__occurrences(anchor, low, high):
    """Jump to the window, including monthly skipped days; never walk history."""
    d = anchor['data']
    origin = _date(d.get('date') if anchor['type'] == 'event' else d.get('due_date'), optional=True)
    if origin is None:
        return
    if anchor['type'] != 'event':
        yield 'deadline', origin
        return
    until = _date(d.get('until'), optional=True)
    if until:
        high = min(high, until)
    low = max(low, origin)
    if low > high:
        return
    recurrence = d.get('recurrence') or 'none'
    if recurrence == 'none':
        if low <= origin <= high:
            yield origin.isoformat(), origin
    elif recurrence in {'daily', 'weekly'}:
        step = 1 if recurrence == 'daily' else 7
        n = max(0, ((low - origin).days + step - 1) // step)
        candidate = origin + dt.timedelta(days=n * step)
        while candidate <= high:
            yield candidate.isoformat(), candidate
            if (high - candidate).days < step:
                break
            candidate += dt.timedelta(days=step)
    elif recurrence == 'monthly':
        index, last = low.year * 12 + low.month - 1, high.year * 12 + high.month - 1
        while index <= last:
            year, month = divmod(index, 12)
            month += 1
            if origin.day <= calendar.monthrange(year, month)[1]:
                candidate = dt.date(year, month, origin.day)
                if low <= candidate <= high:
                    yield candidate.isoformat(), candidate
            index += 1
    else:
        raise BusinessError('validation', '不支持此日程重复规则。')


def _business_date(anchor, occurrence, zone):
    d = anchor['data']
    if anchor['type'] != 'event':
        return occurrence
    exception = d.get('exceptions', {}).get(occurrence.isoformat(), {})
    value = exception.get('start', d.get('start'))
    # A date-only/unknown-time event still has an explicit local calendar date;
    # do not fabricate a midnight or convert a time that was never supplied.
    if not value or d.get('time_kind') in {'date_only', 'unknown', 'approximate'}:
        return occurrence
    try:
        local_zone = ZoneInfo(d.get('timezone') or str(zone))
        clock = dt.time.fromisoformat(value)
        naive = dt.datetime.combine(occurrence, clock)
        local = naive.replace(tzinfo=local_zone)
        returned = local.astimezone(dt.timezone.utc).astimezone(local_zone).replace(tzinfo=None)
        if returned != naive:
            raise ValueError('nonexistent local time')
        if local.utcoffset() != naive.replace(tzinfo=local_zone, fold=1).utcoffset():
            raise ValueError('ambiguous local time')
        return local.astimezone(zone).date()
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise BusinessError('recurring_time_unknown', '该次日程时间存在时区歧义，未猜测准备日期。') from exc


def _snapshot(core, c, rule, anchor, key, occurrence):
    business_anchor = _business_date(anchor, occurrence, _zone(core, c))
    try:
        day = business_anchor - dt.timedelta(days=rule['data']['days_before'])
    except OverflowError as exc:
        raise BusinessError('recurring_date_range', '提前日期超出支持范围，未生成任务。') from exc
    d = rule['data']
    exception = anchor['data'].get('exceptions', {}).get(occurrence.isoformat(), {}) if anchor['type'] == 'event' else {}
    return {'rule_id': rule['id'], 'anchor_id': anchor['id'], 'occurrence_key': key,
            'occurrence_date': occurrence.isoformat(), 'anchor_business_date': business_anchor.isoformat(),
            'business_date': day.isoformat(), 'business_timezone': str(_zone(core, c)),
            'parent_id': _scope(core, c, anchor), 'title': rule['title'],
            'content': d['content'], 'completion_gate': d['completion_gate'],
            'estimated_minutes': d.get('estimated_minutes'), 'series': _series(anchor),
            'exception': {k: exception.get(k) for k in ('cancelled', 'start', 'end', 'personal_choice', 'replacement_target_id') if k in exception}}


def _protected(core, c, task_id):
    row = c.execute('SELECT * FROM entities WHERE id=?', (task_id,)).fetchone()
    if not row:
        return ['task_missing']
    task = core.store.entity(row)
    reasons = []
    if task['archived'] or task['status'] != 'pending' or task['version'] != 1:
        reasons.append('task_changed')
    if c.execute("SELECT 1 FROM entities WHERE type='feedback' AND json_extract(data,'$.target_id')=? LIMIT 1", (task_id,)).fetchone():
        reasons.append('has_feedback')
    if c.execute("SELECT 1 FROM entities p, json_each(p.data,'$.blocks') b WHERE p.type='plan' AND json_extract(b.value,'$.target_id')=? LIMIT 1", (task_id,)).fetchone():
        reasons.append('has_plan')
    return reasons


def _issue(rule, code, message, **details):
    return {'rule_id': rule['id'], 'code': code, 'message': message, **details}


def _evaluate(core, c, p, instant=None):
    start, end, today = _window(core, c, p, instant)
    candidates, existing, issues = [], [], []
    for rule in _rules(core, c, p):
        anchor = core.store.get(c, rule['data']['anchor_id'])
        base_issues = _rule_issue(core, c, rule, anchor, today.isoformat())
        issues.extend(dict(issue, rule_id=rule['id']) for issue in base_issues)
        blocked = bool(base_issues) or not _active(rule)
        offset = rule['data']['days_before']
        low, high = start + dt.timedelta(days=offset - 2), end + dt.timedelta(days=offset + 2)
        desired = {}
        for key, occurrence in _occurrences(anchor, low, high):
            exception = anchor['data'].get('exceptions', {}).get(occurrence.isoformat(), {}) if anchor['type'] == 'event' else {}
            try:
                snapshot = _snapshot(core, c, rule, anchor, key, occurrence)
            except BusinessError as error:
                issues.append(_issue(rule, error.code, error.message, occurrence_key=key))
                continue
            day = snapshot['business_date']
            if not start.isoformat() <= day <= end.isoformat():
                continue
            d = rule['data']
            in_effect = (not d.get('effective_from') or day >= d['effective_from']) and (not d.get('effective_until') or day <= d['effective_until'])
            if exception.get('cancelled') or exception.get('personal_choice') == 'skip':
                issues.append(_issue(rule, 'occurrence_cancelled', '该次日程取消或选择跳过，未生成准备任务。', occurrence_key=key))
                desired[key] = (snapshot, False)
            else:
                occurrence_completed = False
                if anchor['type'] == 'event' and snapshot['anchor_business_date'] <= today.isoformat():
                    feedback = c.execute("""SELECT json_extract(data,'$.dimensions.completion') FROM entities
                        WHERE type='feedback' AND archived=0 AND json_extract(data,'$.target_id')=?
                        AND json_extract(data,'$.business_date')=? AND json_type(data,'$.dimensions.completion') IS NOT NULL
                        ORDER BY created_at DESC,rowid DESC LIMIT 1""", (anchor['id'], snapshot['anchor_business_date'])).fetchone()
                    occurrence_completed = bool(feedback and feedback[0] == 'done')
                    if occurrence_completed:
                        issues.append(_issue(rule, 'anchor_occurrence_completed', '这一次日程已有明确完成反馈；未补生成该次准备任务，其他次照常。', occurrence_key=key))
                desired[key] = (snapshot, not blocked and in_effect and not occurrence_completed)
        ledger = c.execute('SELECT * FROM recurring_occurrences WHERE rule_id=? AND (business_date BETWEEN ? AND ? OR occurrence_key=?) ORDER BY business_date LIMIT ?', (rule['id'], start.isoformat(), end.isoformat(), 'deadline' if anchor['type'] != 'event' else '', MAX_RESULTS + 1)).fetchall()
        known = {row['occurrence_key']: row for row in ledger}
        # A changed offset can bring an old occurrence into this new window.
        for key in desired:
            if key not in known:
                row = c.execute('SELECT * FROM recurring_occurrences WHERE rule_id=? AND occurrence_key=?', (rule['id'], key)).fetchone()
                if row:
                    known[key] = row
        if len(known) > MAX_RESULTS:
            raise BusinessError('recurring_limit', '本次已有任务超过核对上限，请缩短日期窗口。')
        for key, row in known.items():
            old = json.loads(row['snapshot'])
            missing = not c.execute('SELECT 1 FROM entities WHERE id=?', (row['task_id'],)).fetchone()
            record = {'rule_id': rule['id'], 'occurrence_key': key, 'task_id': row['task_id'], 'business_date': row['business_date'], 'protected_reasons': [], 'protection_checked': False}
            existing.append(record)
            current = desired.get(key)
            if current is None:
                # Deadline keys and old window rows still require reconciliation,
                # even when the changed date now lies outside the requested window.
                occurrence = _date(anchor['data'].get('due_date'), optional=True) if anchor['type'] != 'event' else _date(row['occurrence_date'])
                current_snapshot = None
                if occurrence:
                    try:
                        current_snapshot = _snapshot(core, c, rule, anchor, key, occurrence)
                    except BusinessError:
                        pass
            else:
                current_snapshot = current[0]
            still_occurs = anchor['type'] != 'event' or any(k == key for k, _ in _occurrences(anchor, _date(row['occurrence_date']), _date(row['occurrence_date'])))
            inactive_occurrence = bool(current and not current[1]) or blocked or not still_occurs
            if current_snapshot != old or inactive_occurrence or missing:
                reasons = _protected(core, c, row['task_id'])
                record.update(protected_reasons=reasons, protection_checked=True)
                code = 'task_missing' if missing else 'generated_task_needs_review'
                issues.append(_issue(rule, code, '已有准备任务保留原日期、内容和执行记录，请核对变化后明确调整。', task_id=row['task_id'], occurrence_key=key, protected_reasons=reasons, original_business_date=row['business_date'], current_business_date=current_snapshot.get('business_date') if current_snapshot else None))
        for key, (snapshot, allowed) in desired.items():
            if key not in known and allowed:
                candidates.append({**snapshot, 'late_generation': snapshot['business_date'] < today.isoformat()})
        if len(candidates) + len(existing) > MAX_RESULTS:
            raise BusinessError('recurring_limit', '本次任务超过 500 项，请缩短窗口或按规则生成。')
    return {'candidates': candidates, 'existing': existing, 'issues': issues,
            'coverage': {'start': start.isoformat(), 'end': end.isoformat(), 'complete': True, 'scope': 'requested_preparation_dates', 'history_backfilled': False}, 'today': today.isoformat()}


def preview(core, c, p):
    return _evaluate(core, c, p)


def materialize(core, c, p, rid, *, instant=None):
    result = _evaluate(core, c, p, instant)
    created = []
    for candidate in result.pop('candidates'):
        snapshot = {k: v for k, v in candidate.items() if k != 'late_generation'}
        task = core._create(c, {'type': 'task', 'title': candidate['title'], 'parent_id': candidate['parent_id'], 'status': 'pending', 'data': {
            'notes': candidate['content'], 'completion_gate': candidate['completion_gate'], 'estimated_minutes': candidate['estimated_minutes'],
            'scheduled_date': candidate['business_date'], 'due_date': candidate['anchor_business_date'],
            'task_kind': 'recurring_preparation', 'recurring_rule_id': candidate['rule_id'], 'anchor_id': candidate['anchor_id'],
            'anchor_occurrence': candidate['occurrence_key'], 'business_date': candidate['business_date'],
            'late_generation': candidate['late_generation'], 'generated_snapshot': snapshot}}, rid)
        c.execute('INSERT INTO recurring_occurrences VALUES (?,?,?,?,?,?,?,?)', (candidate['rule_id'], candidate['occurrence_key'], candidate['anchor_id'], candidate['occurrence_date'], candidate['business_date'], task['id'], encode(snapshot), now()))
        created.append(task)
    return {**result, 'created': created, 'created_count': len(created)}


def set_rule(core, c, p, rid):
    old = core._versioned(c, p) if p.get('id') else None
    if old and old['type'] != 'recurring_rule':
        raise BusinessError('recurring_rule', '只能编辑节点前准备规则。')
    anchor_id = p.get('anchor_id') or (old['data']['anchor_id'] if old else None)
    anchor = core.store.get(c, anchor_id)
    if anchor['type'] not in ANCHOR_TYPES:
        raise BusinessError('recurring_anchor', '准备规则需要绑定一个日程、里程碑、考核或任务截止节点。')
    if old and anchor_id != old['data']['anchor_id']:
        raise BusinessError('recurring_anchor', '已有规则不能更换关联节点；请停用旧规则并创建新规则。')
    data = copy.deepcopy(old['data']) if old else {}
    title = p.get('title', old['title'] if old else None)
    for key in ('content', 'completion_gate', 'estimated_minutes', 'days_before', 'enabled', 'effective_from', 'effective_until', 'source_text'):
        if key in p:
            data[key] = p[key]
    for key in ('content', 'completion_gate'):
        if not isinstance(data.get(key), str) or not data[key].strip() or len(data[key]) > 12000:
            raise BusinessError('validation', '请明确填写准备内容和完成条件（每项不超过 12000 字）。')
        data[key] = data[key].strip()
    if type(data.get('days_before')) is not int or not 0 <= data['days_before'] <= 366:
        raise BusinessError('validation', '提前天数必须为 0 到 366 的整数。')
    data.setdefault('enabled', True)
    if type(data['enabled']) is not bool:
        raise BusinessError('validation', '启用状态必须明确。')
    data.setdefault('estimated_minutes', None)
    if data['estimated_minutes'] is not None and (type(data['estimated_minutes']) is not int or not 0 <= data['estimated_minutes'] <= 1440):
        raise BusinessError('validation', '估时应为 0 到 1440 分钟；未知请留空。')
    begin = _date(data.get('effective_from'), '规则开始日期', optional=True)
    end = _date(data.get('effective_until'), '规则结束日期', optional=True)
    if begin and end and end < begin:
        raise BusinessError('validation', '规则结束日期不能早于开始日期。')
    data['effective_from'], data['effective_until'] = begin.isoformat() if begin else None, end.isoformat() if end else None
    signature = _series(anchor)
    if old and data['enabled'] and old['data'].get('anchor_series') != signature and c.execute('SELECT 1 FROM recurring_occurrences WHERE rule_id=? LIMIT 1', (old['id'],)).fetchone():
        if p.get('acknowledge_anchor_change') is not True:
            raise BusinessError('recurring_anchor_changed', '关联日程已经改期或改变频率、教学周规则。请明确确认重新绑定；已生成任务仍保留待核对。', {'anchor_id': anchor_id, 'original_series': old['data'].get('anchor_series'), 'current_series': signature})
    if data.get('source_text') is not None and (not isinstance(data['source_text'], str) or len(data['source_text']) > 12000):
        raise BusinessError('validation', '规则来源说明应为不超过 12000 字的文字。')
    data['anchor_id'] = anchor_id
    # Disabling does not implicitly acknowledge a changed event series.
    data['anchor_series'] = signature if data['enabled'] or not old else old['data'].get('anchor_series', signature)
    parent = _scope(core, c, anchor)
    if not old and isinstance(title, str):
        duplicate = c.execute("SELECT * FROM entities WHERE type='recurring_rule' AND json_extract(data,'$.anchor_id')=? AND trim(title)=? COLLATE NOCASE ORDER BY created_at,id LIMIT 1", (anchor_id, title.strip())).fetchone()
        if duplicate:
            found = core.store.entity(duplicate)
            semantic_fields = ('anchor_id','content','completion_gate','estimated_minutes','days_before','enabled','effective_from','effective_until')
            same = all(found['data'].get(k) == data.get(k) for k in semantic_fields)
            if not same or found['archived']:
                raise BusinessError('recurring_rule_exists', '同一节点已有同名准备规则；请读取后按版本明确编辑，避免重复生成。', {'id':found['id'],'version':found['version']})
            window = {'rule_id': found['id']}
            if p.get('materialize_date'):
                window['start'] = window['end'] = _date(p['materialize_date'], '生成日期').isoformat()
            return {'entity':found, 'reused':True, 'materialization':materialize(core,c,window,rid)}
    if old:
        updated = {**old, 'title': title, 'parent_id': parent, 'data': data}
        core._validate(c, updated)
        entity = core._save(c, old, updated, rid, 'set_recurring_rule')
        if old['title'] != entity['title'] or old['parent_id'] != entity['parent_id'] or old['data'] != entity['data']:
            # A revised definition starts today, never retroactively fills the
            # disabled period or applies changed instructions to missed history.
            c.execute('DELETE FROM recurring_cursors WHERE rule_id=?', (entity['id'],))
    else:
        entity = core._create(c, {'type': 'recurring_rule', 'title': title, 'parent_id': parent, 'data': data}, rid)
    window = {'rule_id': entity['id']}
    if p.get('materialize_date'):
        window['start'] = window['end'] = _date(p['materialize_date'], '生成日期').isoformat()
    return {'entity': entity, 'reused': False, 'materialization': materialize(core, c, window, rid)}


def tick(core, instant=None, *, after_id=None, limit=40):
    """Bounded background catch-up with durable per-rule technical cursors.

    New rules start today. Established rules catch up at most 31 preparation days.
    Cursors and resolved alert signatures can commit without changing the business
    revision. Failed rules retain their cursor; idle same-day ticks write nothing.
    """
    if type(limit) is not int or not 1 <= limit <= 40:
        raise ValueError('background rule batch must be 1..40')
    with core.store.lock, core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            rows = c.execute("SELECT id FROM entities WHERE type='recurring_rule' AND archived=0 AND id>? ORDER BY id LIMIT ?", (after_id or '', limit + 1)).fetchall()
            ids = [r['id'] for r in rows[:limit]]
            today = _today(core, c, instant)
            day, zone = today.isoformat(), str(_zone(core, c))
            rid = 'recurring:' + day
            created, notices, technical_changed = [], [], False
            for rule_id in ids:
                c.execute('SAVEPOINT recurring_rule_tick')
                try:
                    cursor = c.execute('SELECT * FROM recurring_cursors WHERE rule_id=?', (rule_id,)).fetchone()
                    start, cursor_issues = day, []
                    can_advance = True
                    if cursor:
                        previous = _date(cursor['last_business_date'])
                        if cursor['business_timezone'] != zone:
                            cursor_issues.append({'rule_id':rule_id, 'code':'business_timezone_changed', 'message':'业务时区已改变，本次仅检查今天；此前日期需要按原时区核对。', 'previous_date':cursor['last_business_date'], 'previous_timezone':cursor['business_timezone']})
                        elif previous > today:
                            can_advance = False
                            cursor_issues.append({'rule_id':rule_id, 'code':'business_date_reversed', 'message':'当前业务日期早于已检查日期，本次仅检查今天并保留原进度，请核对系统时钟。', 'previous_date':cursor['last_business_date']})
                        else:
                            bounded_start = max(previous, today - dt.timedelta(days=MAX_WINDOW - 1))
                            start = bounded_start.isoformat()
                            if bounded_start > previous + dt.timedelta(days=1):
                                cursor_issues.append({'rule_id':rule_id, 'code':'catchup_limited', 'message':'离线期间超过本次 31 天补查范围，更早的准备事项需手动核对；未把它们记为完成。', 'unscanned_start':(previous+dt.timedelta(days=1)).isoformat(), 'unscanned_end':(bounded_start-dt.timedelta(days=1)).isoformat()})
                    result = materialize(core, c, {'rule_id': rule_id, 'start': start, 'end': day}, rid, instant=instant)
                    # Missing facts and ambiguous schedules remain retryable gaps.
                    blocking = {'anchor_changed','anchor_date_unknown','scope_archived','recurring_time_unknown','recurring_date_range'}
                    can_advance = can_advance and not any(i['code'] in blocking for i in result['issues'])
                    result['issues'].extend(cursor_issues)
                    if can_advance and (not cursor or cursor['last_business_date'] != day or cursor['business_timezone'] != zone):
                        c.execute('INSERT INTO recurring_cursors VALUES (?,?,?,?) ON CONFLICT(rule_id) DO UPDATE SET last_business_date=excluded.last_business_date,business_timezone=excluded.business_timezone,updated_at=excluded.updated_at', (rule_id, day, zone, now()))
                        technical_changed = True
                    c.execute('RELEASE recurring_rule_tick')
                except Exception as error:
                    c.execute('ROLLBACK TO recurring_rule_tick')
                    c.execute('RELEASE recurring_rule_tick')
                    result = {'created': [], 'issues': [{'rule_id':rule_id, 'code':getattr(error,'code','recurring_error'), 'message':'本条准备规则未执行，需要核对配置：' + str(error)[:1000]}]}
                created.extend(result['created'])
                if result['issues']:
                    signature = _hash(result['issues'])
                    previous_alert = c.execute('SELECT signature FROM recurring_alerts WHERE rule_id=?', (rule_id,)).fetchone()
                    if previous_alert is None or previous_alert['signature'] != signature:
                        rule = core.store.get(c, rule_id)
                        context = '节点不可用 · ' + rule['title']
                        try:
                            anchor = core.store.get(c, rule['data']['anchor_id'])
                            context = anchor['title']
                            owner_id = _scope(core, c, anchor)
                            if owner_id:
                                context = core.store.get(c, owner_id)['title'] + ' · ' + context
                        except (BusinessError, KeyError):
                            pass
                        notices.append(core._create(c, {'type': 'notification', 'title': rule['title'] + '需要核对', 'data': {'content': '关联：' + context + '\n' + '\n'.join(dict.fromkeys(i['message'] for i in result['issues'])), 'business_date': day, 'seen': False, 'recurring_rule_id': rule_id, 'issues': result['issues']}}, rid))
                        c.execute('INSERT INTO recurring_alerts VALUES (?,?,?) ON CONFLICT(rule_id) DO UPDATE SET signature=excluded.signature,updated_at=excluded.updated_at', (rule_id, signature, now()))
                else:
                    # A resolved alert must not suppress a later recurrence of
                    # the same missing fact merely because its text is identical.
                    removed = c.execute('DELETE FROM recurring_alerts WHERE rule_id=?', (rule_id,)).rowcount
                    technical_changed = technical_changed or bool(removed)
            if created or notices:
                core.store.set_meta(c, 'revision', core.store.meta(c, 'revision') + 1)
            if created or notices or technical_changed:
                c.commit()
            else:
                c.rollback()
            return {'created_count': len(created), 'notification_count': len(notices), 'next_after_id': ids[-1] if len(rows) > limit else None}
        except Exception:
            c.rollback()
            raise
