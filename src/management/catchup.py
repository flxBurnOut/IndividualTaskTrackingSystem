"""Evidence-backed catch-up work on ordinary task and immutable feedback entities.

There is no parallel progress table or cached completed quantity. Queries project
cumulative reports from feedback; commands run in the caller's transaction and
never schedule a task or change attendance, mastery or submission implicitly.
"""
from __future__ import annotations
import copy
import datetime as dt
import hashlib
import math
from collections import Counter
from .schemas import BusinessError
from .storage import encode, now

MAX_SCOPE = 10000
MAX_QUANTITY = 1_000_000_000
META = {
    'unit': 'catchup_unit', 'total_quantity': 'catchup_total_quantity',
    'lesson_key': 'catchup_lesson_key', 'topic_ids': 'catchup_topic_ids',
    'lesson_topics': 'catchup_lesson_topics', 'reason': 'catchup_reason',
}


def _day(value, default=None):
    value = default if value in (None, '') else value
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError()
        day = dt.date.fromisoformat(value).isoformat()
        if value != day:
            raise ValueError()
        return day
    except (TypeError, ValueError) as exc:
        raise BusinessError('validation', '请填写明确的业务日期 YYYY-MM-DD。') from exc


def _text(value, label, maximum=12000, optional=False):
    if optional and value in (None, ''):
        return ''
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BusinessError('source_required' if label == '来源' else 'validation', label + '不能为空且不能超过 ' + str(maximum) + ' 字。')
    return value.strip()


def _number(value, label, maximum=MAX_QUANTITY):
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise BusinessError('validation', label + '需要非负有限数量；未知请留空。')
    return value


def _course(core, c, course_id):
    entity = core.store.get(c, course_id)
    if entity['type'] != 'course':
        raise BusinessError('scope_type', '补课事项需要归属一门明确课程。')
    return entity


def _belongs(core, c, entity, course_id):
    seen = set()
    while entity:
        if entity['id'] == course_id:
            return True
        if entity['id'] in seen or len(seen) > 32:
            raise BusinessError('cycle', '任务归属层级异常。')
        seen.add(entity['id'])
        entity = core.store.get(c, entity['parent_id']) if entity.get('parent_id') else None
    return False


def _is_recovery(task):
    return task['type'] == 'task' and (task['data'].get('catchup_enabled') is True or task['data'].get('task_kind') == 'catchup')


def _quantity_record(core, c, task_id, as_of):
    row = c.execute("""SELECT *,rowid AS evidence_order FROM entities WHERE type='feedback' AND archived=0
        AND json_extract(data,'$.target_id')=? AND json_extract(data,'$.business_date')<=?
        AND json_type(data,'$.catchup_progress.completed_quantity') IS NOT NULL
        ORDER BY json_extract(data,'$.business_date') DESC,created_at DESC,rowid DESC LIMIT 1""", (task_id, as_of)).fetchone()
    return core.store.entity(row) if row else None


def _completion_record(core, c, task_id, as_of):
    row = c.execute("""SELECT *,rowid AS evidence_order FROM entities WHERE type='feedback' AND archived=0
        AND json_extract(data,'$.target_id')=? AND json_extract(data,'$.business_date')<=?
        AND json_type(data,'$.dimensions.completion') IS NOT NULL
        ORDER BY json_extract(data,'$.business_date') DESC,created_at DESC,rowid DESC LIMIT 1""", (task_id, as_of)).fetchone()
    return core.store.entity(row) if row else None


def _project(core, c, task, as_of):
    d = task['data']
    record = _quantity_record(core, c, task['id'], as_of)
    confirmation = _completion_record(core, c, task['id'], as_of)
    quantity = record['data']['catchup_progress'].get('completed_quantity') if record else None
    total, unit = d.get('catchup_total_quantity'), d.get('catchup_unit')
    issues = []
    if record and record['data']['catchup_progress'].get('unit') != unit:
        quantity = None
        issues.append('进展记录的单位与当前定义不同，未强行换算。')
    if not record:
        issues.append('已补量尚未明确，未按零处理。')
    if total is None:
        issues.append('总量尚未明确，无法计算比例。')
    valid_numbers = all(value is None or type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in (quantity, total))
    if not valid_numbers:
        raise BusinessError('recovery_data_invalid', '补课数量记录不合法，请明确核对后更正。')
    ratio = quantity / total if quantity is not None and total is not None and total > 0 and quantity <= total else None
    if quantity is not None and total is not None and quantity > total:
        issues.append('已补量大于当前总量，需要核对；未截断成 100%。')
    completion = confirmation['data']['dimensions'].get('completion') if confirmation else None
    confirmed = completion == 'done' and bool(str(confirmation['data'].get('source_text') or '').strip()) if confirmation else False
    scope_version=d.get('catchup_scope_version',0)
    if scope_version and (not confirmation or confirmation['data'].get('target_version',0)<scope_version or confirmation['data'].get('business_date','')<d.get('catchup_scope_correction',{}).get('business_date','') or completion in (None,'unknown')):
        completion=None;confirmed=False
        issues.append('补欠范围已更正；旧范围的完成反馈不代表当前范围，当前完成情况待确认。')
    gate = d.get('completion_gate') or ''
    if confirmed and not str(gate).strip():
        confirmed = False
        issues.append('缺少明确完成条件，原完成反馈需要核对。')
    if confirmed and quantity is not None and total is not None and quantity < total:
        def evidence_order(entity):
            return (entity['data'].get('business_date',''),entity['created_at'],entity.get('evidence_order',0))
        quantity_is_newer = bool(record and evidence_order(record) > evidence_order(confirmation))
        correction = d.get('catchup_definition_correction',{})
        definition_is_newer = bool(correction and correction.get('recorded_at','') >= confirmation['created_at'] and confirmation['data'].get('target_version',0) < task['version'])
        if quantity_is_newer or definition_is_newer:
            confirmed = False
            issues.append('数量或范围更正与原完成确认不一致；保留原反馈，需明确重新核对完成条件。')
        else:
            issues.append('完成条件已有较新的明确确认；数量仍保留此前记录，未自动补成总量。')
    if confirmed and confirmation['data'].get('catchup_progress', {}).get('completion_gate') not in (None, gate):
        confirmed = False
        issues.append('完成条件已改变，原确认不能直接代表新的完成条件。')
    if task['status'] == 'done' and not confirmed:
        issues.append('任务状态写为完成，但缺少完成条件的明确确认反馈。')
    if ratio == 1 and not confirmed:
        issues.append('数量已经达到总量，完成条件仍需明确确认。')
    return {'completed_quantity': quantity, 'total_quantity': total, 'unit': unit,
            'ratio': ratio, 'remaining_quantity': max(0, total - quantity) if quantity is not None and total is not None and quantity <= total else None,
            'quantity_known': quantity is not None, 'total_known': total is not None,
            'completion': completion, 'completion_confirmed': confirmed,
            'latest_feedback_id': record['id'] if record else None,
            'completion_feedback_id': confirmation['id'] if confirmation else None,
            'source_text': record['data'].get('source_text', '') if record else '',
            'business_date': record['data'].get('business_date') if record else None,
            'completion_source_text': confirmation['data'].get('source_text', '') if confirmation else '',
            'completion_gate': d.get('completion_gate', ''), 'recorded_total_quantity':record['data']['catchup_progress'].get('total_quantity') if record else None,
            'definition_version':task['version'], 'denominator_basis':'current_task_definition', 'issues': issues}


def _rows(core, c, course_id=None, task_id=None):
    args = []
    prefix = ''
    if course_id:
        _course(core, c, course_id)
        prefix = 'WITH RECURSIVE scope(id) AS (SELECT ? UNION SELECT e.id FROM entities e JOIN scope s ON e.parent_id=s.id) '
        args.append(course_id)
    sql = prefix + "SELECT e.* FROM entities e WHERE e.type='task' AND e.archived=0 AND (json_extract(e.data,'$.catchup_enabled')=1 OR json_extract(e.data,'$.task_kind')='catchup')"
    if course_id:
        sql += ' AND e.id IN (SELECT id FROM scope)'
    if task_id:
        task = core.store.get(c, task_id)
        if not _is_recovery(task):
            raise BusinessError('recovery_task', '该任务尚未登记为待补事项。')
        if course_id and not _belongs(core, c, task, course_id):
            raise BusinessError('scope_mismatch', '该待补任务不属于所选课程。')
        sql += ' AND e.id=?'
        args.append(task_id)
    sql += ' ORDER BY e.created_at,e.id'
    for index, row in enumerate(c.execute(sql, args)):
        if index >= MAX_SCOPE:
            raise BusinessError('scope_limit', '补课汇总范围超过 10000 项，请选择较小范围。')
        yield core.store.entity(row)


def recovery_summary(core, c, p):
    limit, offset = p.get('limit', 50), p.get('offset', 0)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise BusinessError('validation', '分页范围需要 1 到 100 项，起点不能为负。')
    as_of = _day(p.get('as_of'), core.today(c))
    open_only = p.get('open_only', False)
    if type(open_only) is not bool:
        raise BusinessError('validation', '未完成筛选需要布尔值。')
    completed_ids = set()
    if open_only:
        from .task_views import rows_sql
        scope = "(json_extract(e.data,'$.catchup_enabled')=1 OR json_extract(e.data,'$.task_kind')='catchup')"
        args = []
        if p.get('course_id'):
            scope += ' AND e.id IN (WITH RECURSIVE scope(id) AS (SELECT ? UNION SELECT child.id FROM entities child JOIN scope ON child.parent_id=scope.id) SELECT id FROM scope)'
            args.append(p['course_id'])
        if p.get('task_id'):
            scope += ' AND e.id=?';args.append(p['task_id'])
        completed_ids = {row['id'] for row in c.execute("SELECT id FROM ("+rows_sql(scope)+") WHERE completion_state='done'", args)}
    metrics = Counter({'total_tasks':0, 'active_tasks':0, 'cancelled_tasks':0, 'completion_confirmed':0,
                       'quantity_unknown':0, 'total_unknown':0, 'awaiting_confirmation':0})
    from .presentation import Presenter
    presenter=Presenter(core,c)
    groups, items, total, completed_hidden = {}, [], 0, 0
    for task in _rows(core, c, p.get('course_id'), p.get('task_id')):
        progress = _project(core, c, task, as_of)
        metrics['total_tasks'] += 1
        cancelled = task['status'] == 'cancelled'
        metrics['cancelled_tasks' if cancelled else 'active_tasks'] += 1
        if not cancelled:
            metrics['completion_confirmed'] += int(progress['completion_confirmed'])
            metrics['quantity_unknown'] += int(not progress['quantity_known'])
            metrics['total_unknown'] += int(not progress['total_known'])
            metrics['awaiting_confirmation'] += int(progress['ratio'] == 1 and not progress['completion_confirmed'])
            unit = progress['unit'] or '未注明单位'
            group = groups.setdefault(unit, {'unit':unit,'tasks':0,'known_completed_quantity':0,'known_total_quantity':0,'completed_known_tasks':0,'total_known_tasks':0,'quantity_unknown':0,'total_unknown':0})
            group['tasks'] += 1
            for quantity_key, sum_key, known_key, unknown_key in (
                    ('completed_quantity','known_completed_quantity','completed_known_tasks','quantity_unknown'),
                    ('total_quantity','known_total_quantity','total_known_tasks','total_unknown')):
                if progress[quantity_key] is None:
                    group[unknown_key] += 1
                else:
                    group[sum_key] += progress[quantity_key]
                    group[known_key] += 1
        # Filter before paging, without changing historical quantity aggregates.
        if task['id'] in completed_ids:
            completed_hidden += 1
            continue
        if offset <= total < offset + limit:
            items.append({**presenter.entity(task), 'progress':progress})
        total += 1
    for group in groups.values():
        if not group['completed_known_tasks']:
            group['known_completed_quantity'] = None
        if not group['total_known_tasks']:
            group['known_total_quantity'] = None
        complete = not group['quantity_unknown'] and not group['total_unknown']
        denominator, numerator = group['known_total_quantity'], group['known_completed_quantity']
        group['ratio'] = numerator / denominator if complete and denominator and numerator is not None and numerator <= denominator else None
        group['coverage_complete'] = complete
    return {'course_id':p.get('course_id'), 'task_id':p.get('task_id'), 'as_of':as_of, 'items':items, 'metrics':dict(metrics), 'groups':list(groups.values()),
            'total':total, 'next_offset':offset+len(items) if offset+len(items)<total else None,
            'completed_hidden':completed_hidden,
            'coverage':{'metrics_complete':True,'returned':len(items),'total':total,'cancelled_excluded_from_quantities':True},
            'unknowns':['不同单位分别汇总；数量比例不是工作量比例，也不是掌握、提交或出勤比例。','补完只来自明确完成反馈，数量达到总量不会自动确认。','查询不会建立待补任务，也不会安排每日计划。']}


def _topics(core, c, value, course_id):
    if not isinstance(value, list) or len(value)>30 or any(not isinstance(i,str) for i in value) or len(set(value)) != len(value):
        raise BusinessError('validation', '知识点需要至多 30 个不重复的已登记标识。')
    for topic_id in value:
        topic = core.store.get(c, topic_id)
        if topic['type'] != 'topic' or topic['archived'] or not _belongs(core,c,topic,course_id):
            raise BusinessError('scope_mismatch', '知识点必须属于同一课程且仍在使用。')
    return value


def _key(course_id, title, lesson_key):
    normalized = ' '.join((lesson_key or title).split()).casefold()
    return hashlib.sha256((course_id+'\n'+normalized).encode('utf-8')).hexdigest()


def set_recovery_task(core, c, p, rid):
    course = _course(core, c, p.get('course_id'))
    if course['archived']:
        raise BusinessError('archived_parent', '课程已归档，请先明确恢复。')
    source = _text(p.get('source_text'), '来源')
    target_id = p.get('id') or p.get('original_task_id')
    if p.get('id') and p.get('original_task_id') and p['id'] != p['original_task_id']:
        raise BusinessError('validation', '原任务与编辑目标必须是同一记录。')
    old = core._versioned(c, {'id':target_id,'version':p.get('version')}) if target_id else None
    if old and (old['type'] != 'task' or not _belongs(core,c,old,course['id'])):
        raise BusinessError('scope_mismatch', '原任务必须属于所选课程，不能复制或跨课程改写。')
    if old and (old['archived'] or old['status'] in {'done','cancelled'}):
        raise BusinessError('recovery_closed', '原任务已完成、取消或归档；请先明确核对或更正其状态。')
    if old and not _is_recovery(old):
        prior_completion = _completion_record(core,c,old['id'],core.today(c))
        if prior_completion and prior_completion['data']['dimensions']['completion']=='done':
            raise BusinessError('recovery_closed','原任务已有明确完成反馈；若反馈有误，请先明确更正，不建立平行待补状态。')
    d = copy.deepcopy(old['data']) if old else {}
    was_recovery = bool(old and _is_recovery(old))
    title = _text(p.get('title',old['title'] if old else None),'事项名称',300)
    gate = _text(p.get('completion_gate',d.get('completion_gate')),'完成条件')
    if old and d.get('completion_gate') and gate != d['completion_gate']:
        raise BusinessError('completion_gate', '登记补课不会降低或改写原任务完成条件；请保留原完成条件。')
    unit = _text(p.get('unit',d.get('catchup_unit')),'数量单位',60)
    total_quantity = _number(p.get('total_quantity',d.get('catchup_total_quantity')), '总量')
    lesson_key = _text(p.get('lesson_key',d.get('catchup_lesson_key')),'学习单元标识',200,optional=True)
    topics = _topics(core,c,p.get('topic_ids',d.get('catchup_topic_ids',[])),course['id'])
    lesson_topics = p.get('lesson_topics',d.get('catchup_lesson_topics',[]))
    if not isinstance(lesson_topics,list) or len(lesson_topics)>30 or any(not isinstance(t,str) or not t.strip() or len(t)>100 for t in lesson_topics):
        raise BusinessError('validation','具体学习内容应为至多 30 条简短文字。')
    reason = p.get('reason',d.get('catchup_reason','self_reported'))
    if reason not in {'self_reported','confirmed_incomplete'}:
        raise BusinessError('validation','请明确使用自述落下或已有未完成证据。')
    if reason == 'confirmed_incomplete':
        evidence = _completion_record(core,c,old['id'],core.today(c)) if old else None
        if not evidence or evidence['data']['dimensions']['completion'] not in {'not_started','partial','blocked','incomplete'}:
            raise BusinessError('recovery_evidence','已有未完成需要明确的完成维度反馈；未到课、没提交或未知不能代替。')
    estimate = p.get('estimated_minutes',d.get('estimated_minutes'))
    if estimate is not None and (type(estimate) is not int or not 0<=estimate<=1440):
        raise BusinessError('validation','估时需要 0 到 1440 分钟；未知请留空。')
    if old:
        quantity_record = _quantity_record(core,c,old['id'],'9999-12-31')
        if quantity_record and d.get('catchup_unit') != unit:
            raise BusinessError('recovery_unit_locked','已有数量进展，不能把历史数量悄悄换成另一单位。')
        current = _project(core,c,old,core.today(c)) if was_recovery else None
        if current and current['completed_quantity'] is not None and total_quantity is not None and total_quantity < current['completed_quantity']:
            raise BusinessError('recovery_total_below_progress','总量不能小于已确认累计量；请先明确更正进展。')
        if was_recovery and d.get('catchup_total_quantity') != total_quantity:
            _text(p.get('correction_reason'),'总量更正原因',2000)
        if was_recovery and 'completed_quantity' in p:
            raise BusinessError('recovery_progress_command','已有进展请通过记录进度或明确更正更新，不在编辑定义时覆盖。')
    key = _key(course['id'],title,lesson_key)
    values = {'catchup_enabled':True,'catchup_unit':unit,'catchup_total_quantity':total_quantity,
              'catchup_lesson_key':lesson_key,'catchup_topic_ids':topics,'catchup_lesson_topics':lesson_topics,
              'catchup_reason':reason,'catchup_source_text':source,'catchup_key':key,
              'completion_gate':gate,'estimated_minutes':estimate}
    if old:
        for candidate in _rows(core,c,course['id']):
            if candidate['id'] != old['id'] and candidate['data'].get('catchup_key') == key:
                raise BusinessError('recovery_exists','当前学习单元已有另一条待补事项，不能把两条独立记录改为同一个标识。',{'id':candidate['id'],'version':candidate['version']})
    if not old:
        for candidate in _rows(core,c,course['id']):
            if candidate['data'].get('catchup_key') != key:
                continue
            compare = ('catchup_unit','catchup_total_quantity','catchup_lesson_key','catchup_topic_ids','catchup_lesson_topics','catchup_reason','completion_gate','estimated_minutes')
            if ' '.join(candidate['title'].split()).casefold() != ' '.join(title.split()).casefold() or any(candidate['data'].get(k) != values[k] for k in compare):
                raise BusinessError('recovery_exists','同一课程已有这项待补事项；请读取版本后明确编辑，避免重复。',{'id':candidate['id'],'version':candidate['version']})
            return {'entity':candidate,'reused':True,'feedback':None,'progress':_project(core,c,candidate,core.today(c)),
                    'warnings':['复用了已有事项，未用初始数量覆盖已有进展。'] if 'completed_quantity' in p else []}
        # A plainly matching ordinary task is surfaced for explicit reuse, rather
        # than silently creating a parallel copy with a new completion history.
        match = c.execute("""WITH RECURSIVE scope(id) AS (SELECT ? UNION SELECT e.id FROM entities e JOIN scope s ON e.parent_id=s.id)
            SELECT e.id,e.version FROM entities e WHERE e.type='task' AND e.archived=0 AND e.id IN (SELECT id FROM scope)
            AND trim(e.title)=? COLLATE NOCASE LIMIT 1""", (course['id'],title)).fetchone()
        if match:
            raise BusinessError('recovery_existing_task','课程已有同名任务，请选择原任务继续登记，不建立第二份任务。',dict(match))
    if old and was_recovery and old['data'].get('catchup_total_quantity') != total_quantity:
        d['catchup_definition_correction'] = {'reason':p['correction_reason'].strip(),'source_text':source,
            'previous_total_quantity':old['data'].get('catchup_total_quantity'),'new_total_quantity':total_quantity,'recorded_at':now()}
    d.update(values)
    if not old:
        d['task_kind']='catchup'
        entity = core._create(c,{'type':'task','title':title,'parent_id':course['id'],'status':'pending','data':d},rid)
    else:
        updated = {**old,'title':title,'data':d}
        core._validate(c,updated)
        entity = core._save(c,old,updated,rid,'set_recovery_task')
    feedback = None
    if 'completed_quantity' in p:
        result = record_recovery_progress(core,c,{'task_id':entity['id'],'version':entity['version'],
            'business_date':p.get('business_date') or core.today(c),'completed_quantity':p['completed_quantity'],
            'source_text':source},rid)
        feedback = result['feedback']
    return {'entity':entity,'reused':False,'feedback':feedback,'progress':_project(core,c,entity,core.today(c)),'warnings':[]}


def record_recovery_progress(core, c, p, rid):
    task = core._versioned(c,{'id':p.get('task_id'),'version':p.get('version')})
    if not _is_recovery(task) or task['archived'] or task['status']=='cancelled':
        raise BusinessError('recovery_task','请选择仍可记录进展的待补任务。')
    source = _text(p.get('source_text'),'来源')
    day = _day(p.get('business_date'),core.today(c))
    has_quantity = 'completed_quantity' in p
    confirmed = p.get('completion_confirmed')
    if 'completion_confirmed' in p and type(confirmed) is not bool:
        raise BusinessError('validation','完成确认必须明确选择；没有表达请省略。')
    if not has_quantity and 'completion_confirmed' not in p and 'actual_minutes' not in p:
        raise BusinessError('validation','请至少明确记录累计已补量、完成确认或实际用时。')
    quantity = _number(p.get('completed_quantity'),'累计已补量') if has_quantity else None
    total, unit = task['data'].get('catchup_total_quantity'),task['data'].get('catchup_unit')
    if p.get('unit') is not None and p['unit'] != unit:
        raise BusinessError('recovery_unit_locked','记录单位与任务定义不一致。')
    if has_quantity and quantity is not None and total is not None and quantity > total:
        raise BusinessError('recovery_quantity','累计已补量超过已知总量，请先有来源地核对总量。')
    semantic = {'task_id':task['id'],'business_date':day,'source_text':source,'unit':unit,
                'total_quantity':total,'completion_gate':task['data'].get('completion_gate',''),
                **{key:p[key] for key in ('completed_quantity','completion_confirmed','actual_minutes','correction_of','correction_reason') if key in p}}
    fingerprint = hashlib.sha256(encode(semantic).encode('utf-8')).hexdigest()
    last = c.execute("""SELECT * FROM entities WHERE type='feedback' AND archived=0
        AND json_extract(data,'$.target_id')=? AND json_extract(data,'$.business_date')=?
        AND json_type(data,'$.catchup_progress')='object'
        ORDER BY created_at DESC,rowid DESC LIMIT 1""",(task['id'],day)).fetchone()
    if last:
        previous_report = core.store.entity(last)
        same = previous_report['data']['catchup_progress'].get('input_fingerprint') == fingerprint
        current_completion = _completion_record(core,c,task['id'],day) if 'completion_confirmed' in p else None
        if current_completion and current_completion['data']['dimensions']['completion'] != ('done' if confirmed else 'incomplete'):
            same = False
        if same:
            return {'entity':task,'feedback':previous_report,'reused':True,'progress':_project(core,c,task,core.today(c))}
    prior = _quantity_record(core,c,task['id'],day)
    before = prior['data']['catchup_progress'].get('completed_quantity') if prior else None
    decreasing = has_quantity and before is not None and (quantity is None or quantity < before)
    if p.get('correction_of') or decreasing:
        if not prior or p.get('correction_of') != prior['id']:
            raise BusinessError('recovery_correction','减少或清除已补量需要引用该业务日最新的数量记录并写明更正原因。',{'latest_feedback_id':prior['id'] if prior else None})
        _text(p.get('correction_reason'),'更正原因',2000)
    completion_prior = _completion_record(core,c,task['id'],day)
    if confirmed is False and completion_prior and completion_prior['data']['dimensions']['completion']=='done':
        _text(p.get('correction_reason'),'完成更正原因',2000)
    effective_quantity = quantity if has_quantity else before
    if confirmed is True and effective_quantity is not None and total is not None and effective_quantity < total:
        raise BusinessError('recovery_incomplete','已补量尚未达到所登记总量，请核对进展和完整完成条件。')
    dimensions, progress = {}, {'unit':unit,'total_quantity':total,'completion_gate':task['data'].get('completion_gate',''),'input_fingerprint':fingerprint}
    if has_quantity:
        progress['completed_quantity']=quantity
    if confirmed is True:
        _text(task['data'].get('completion_gate'),'完成条件')
    if 'completion_confirmed' in p:
        dimensions['completion']='done' if confirmed else 'incomplete'
        progress['completion_gate_confirmed']=confirmed
    if 'actual_minutes' in p:
        dimensions['actual_minutes']=_number(p['actual_minutes'],'实际分钟',1440)
        if dimensions['actual_minutes'] is None:
            raise BusinessError('validation','实际时长未知请省略，不写成零。')
    if p.get('correction_of'):
        progress['correction_of']=p['correction_of']
    if p.get('correction_reason'):
        progress['correction_reason']=_text(p['correction_reason'],'更正原因',2000)
    if confirmed is False and completion_prior:
        progress['corrects_completion_id']=completion_prior['id']
    data = {'target_id':task['id'],'target_version':task['version'],'business_date':day,
            'dimensions':dimensions,'source_text':source,'reported_at':now(),'catchup_progress':progress}
    # Empty dimensions is intentional for a quantity-only report. It neither
    # overwrites an independent completion result nor invents another dimension.
    feedback = core._create(c,{'type':'feedback','title':(day+' · 补课进展 · '+task['title'])[:300],'data':data},rid)
    return {'entity':task,'feedback':feedback,'reused':False,'progress':_project(core,c,task,core.today(c))}

def correct_recovery_scope(core,c,p,rid):
    """Correct an imported catch-up scope without reusing old completion facts.

    This deliberately refuses any historical measured quantity. Replacing a
    measured learning scope requires a distinct task so its units cannot drift.
    """
    allowed={'id','version','title','completion_gate','lesson_key','source_text','correction_reason','business_date','total_quantity'}
    if set(p)-allowed:
        raise BusinessError('validation','范围更正仅接受任务标识、版本、明确范围、来源和更正原因。')
    old=core._versioned(c,{'id':p.get('id'),'version':p.get('version')})
    if not _is_recovery(old) or old['archived'] or old['status'] in {'cancelled','draft'}:
        raise BusinessError('recovery_task','请选择仍可更正的待补任务。')
    core._ensure_mutable(c,old)
    title=_text(p.get('title'),'事项名称',300)
    gate=_text(p.get('completion_gate'),'完成条件')
    lesson_key=_text(p.get('lesson_key'),'学习单元标识',200)
    source=_text(p.get('source_text'),'来源')
    reason=_text(p.get('correction_reason'),'范围更正原因',2000)
    day=_day(p.get('business_date'),core.today(c))
    if day>core.today(c):
        raise BusinessError('recovery_scope_date','范围更正不能预先写成未来已生效。')
    d=copy.deepcopy(old['data'])
    if (title,gate,lesson_key)==(old['title'],d.get('completion_gate'),d.get('catchup_lesson_key')):
        return {'entity':old,'reused':True,'feedback':None,'progress':_project(core,c,old,day)}
    measured=c.execute("""SELECT id FROM entities WHERE type='feedback'
        AND json_extract(data,'$.target_id')=?
        AND json_extract(data,'$.catchup_progress.completed_quantity') IS NOT NULL LIMIT 1""",(old['id'],)).fetchone()
    if measured:
        raise BusinessError('recovery_scope_has_quantities','该任务已经记录过明确累计数量。请为新范围建立独立待补任务，保留原任务及进度，不能换范围后复用旧数量。',{'feedback_id':measured['id']})
    future=c.execute("""SELECT id FROM entities WHERE type='feedback' AND archived=0
        AND json_extract(data,'$.target_id')=? AND json_extract(data,'$.business_date')>?
        AND json_type(data,'$.dimensions.completion') IS NOT NULL LIMIT 1""",(old['id'],day)).fetchone()
    if future:
        raise BusinessError('recovery_scope_date','更正日期之后已有完成反馈，请先核对其业务日期和适用范围。',{'feedback_id':future['id']})
    from .presentation import Presenter
    course=Presenter(core,c).owner(old)
    if not course or course['type']!='course':
        raise BusinessError('scope_type','待补事项需要保留一门明确课程归属。')
    key=_key(course['id'],title,lesson_key)
    for candidate in _rows(core,c,course['id']):
        if candidate['id']!=old['id'] and candidate['data'].get('catchup_key')==key:
            raise BusinessError('recovery_exists','同一课程已有此学习单元的待补记录，请使用已有记录，避免重复。',{'id':candidate['id'],'version':candidate['version']})
    total=_number(p.get('total_quantity'),'新范围总量')
    previous={'title':old['title'],'completion_gate':d.get('completion_gate'),'lesson_key':d.get('catchup_lesson_key'),
              'total_quantity':d.get('catchup_total_quantity'),'topic_ids':d.get('catchup_topic_ids',[]),'lesson_topics':d.get('catchup_lesson_topics',[])}
    d.update(completion_gate=gate,catchup_lesson_key=lesson_key,catchup_key=key,catchup_source_text=source,
             catchup_total_quantity=total,catchup_topic_ids=[],catchup_lesson_topics=[],catchup_scope_version=old['version']+1,
             catchup_scope_correction={'previous_scope':previous,'source_text':source,'reason':reason,'recorded_at':now(),
                'business_date':day,'previous_version':old['version'],'completion_reset_to':'unknown'})
    updated={**old,'title':title,'status':'pending' if old['status']=='done' else old['status'],'data':d}
    core._validate(c,updated)
    entity=core._save(c,old,updated,rid,'correct_recovery_scope')
    feedback=core.feedback(c,{'target_id':entity['id'],'business_date':day,'dimensions':{'completion':'unknown'},
        'source_text':'范围更正：'+reason+'。新范围的完成情况待确认，旧完成记录不沿用。依据：'+source},rid)
    return {'entity':entity,'reused':False,'feedback':feedback,'progress':_project(core,c,entity,day),
            'warnings':['旧任务、反馈及原范围审计保留；当前总量仅采用本次明确提供的新范围总量。']}
