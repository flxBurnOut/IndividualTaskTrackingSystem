"""Durable scoped discussions; model history is never the business source of truth.

Core owns transaction/epoch/revision checks. This module neither opens a database
nor calls a model. Background hooks run inside the worker's existing transaction.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from .schemas import BusinessError
from .storage import encode, new_id, now

MAX_SOURCES = 12
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CHARACTERS = 18000


def initialize(c):
    # Individual execute calls do not implicitly commit the caller's transaction.
    c.execute("""CREATE TABLE IF NOT EXISTS conversations(
        id TEXT PRIMARY KEY,scope_key TEXT NOT NULL UNIQUE,scope TEXT NOT NULL,
        source_ids TEXT NOT NULL DEFAULT '[]',version INTEGER NOT NULL DEFAULT 1,
        active_job_id TEXT,current_proposal_job_id TEXT,provider_thread_id TEXT,
        recovery TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS conversation_messages(
        seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
        conversation_id TEXT NOT NULL REFERENCES conversations(id),role TEXT NOT NULL,
        text TEXT NOT NULL,job_id TEXT,state TEXT NOT NULL,proposal_state TEXT NOT NULL DEFAULT 'none',
        source_ids TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,
        UNIQUE(conversation_id,job_id,role))""")
    c.execute('CREATE INDEX IF NOT EXISTS conversation_message_page ON conversation_messages(conversation_id,seq)')
    c.execute('CREATE INDEX IF NOT EXISTS conversation_message_job ON conversation_messages(job_id,role)')


def _scope(c, value):
    if not isinstance(value, dict) or set(value) - {'kind', 'entity_id', 'date'}:
        raise BusinessError('conversation_scope', '请选择有效的讨论范围。')
    kind = value.get('kind')
    if kind in {'daily_plan', 'daily_review', 'weekly_review'}:
        try:
            day = dt.date.fromisoformat(value.get('date')).isoformat()
        except (TypeError, ValueError) as exc:
            raise BusinessError('conversation_scope', '每日讨论需要明确日期。') from exc
        return {'kind': kind, 'date': day}
    if kind in {'course', 'object', 'timetable'}:
        id = value.get('entity_id')
        if not isinstance(id, str) or not id:
            raise BusinessError('conversation_scope', '请明确讨论的课程或事项。')
        row = c.execute('SELECT type FROM entities WHERE id=?', (id,)).fetchone()
        if not row or kind in {'course', 'timetable'} and row['type'] != kind:
            raise BusinessError('conversation_scope', '讨论对象不存在或不是所选课程。')
        return {'kind': kind, 'entity_id': id}
    if kind == 'general':
        return {'kind': 'general'}
    raise BusinessError('conversation_scope', '未注册的讨论范围。')


def _scope_key(scope):
    return hashlib.sha256(encode(scope).encode('utf-8')).hexdigest()


def _conversation(row):
    if row is None:
        return None
    result = dict(row)
    result['scope'] = json.loads(result['scope'])
    result['source_ids'] = json.loads(result['source_ids'])
    result.pop('scope_key', None)
    return result


def _message(row):
    result = dict(row)
    result['source_ids'] = json.loads(result['source_ids'])
    return result


def _sources(c, ids):
    """Small display metadata only; source text stays outside chat pagination."""
    result = []
    for id in ids:
        row = c.execute("SELECT id,title,json_extract(data,'$.extraction.status') AS extraction_status FROM entities WHERE id=?", (id,)).fetchone()
        result.append(dict(row) if row else {'id': id, 'title': '资料记录已不可用', 'extraction_status': 'unavailable'})
    return result


def source_ids_for_scope(c, scope):
    canonical = _scope(c, scope)
    row = c.execute('SELECT source_ids FROM conversations WHERE scope_key=?', (_scope_key(canonical),)).fetchone()
    return json.loads(row['source_ids']) if row else []


def query(core, c, p):
    if bool(p.get('id')) == bool(p.get('scope')):
        raise BusinessError('conversation_scope', '请按会话编号或讨论范围读取。')
    limit, before = p.get('limit', 50), p.get('before')
    if type(limit) is not int or not 1 <= limit <= 100 or before is not None and (type(before) is not int or before <= 0):
        raise BusinessError('validation', '会话分页参数无效。')
    if p.get('id'):
        row = c.execute('SELECT * FROM conversations WHERE id=?', (p['id'],)).fetchone()
        if row is None:
            raise BusinessError('not_found', '会话不存在。')
    else:
        scope = _scope(c, p['scope'])
        row = c.execute('SELECT * FROM conversations WHERE scope_key=?', (_scope_key(scope),)).fetchone()
    if row is None:
        return {'conversation': None, 'messages': [], 'next_before': None, 'has_more': False}
    args = [row['id']]
    where = 'conversation_id=?'
    if before is not None:
        where += ' AND seq<?'
        args.append(before)
    records = list(c.execute('SELECT * FROM conversation_messages WHERE ' + where + ' ORDER BY seq DESC LIMIT ?', [*args, limit + 1]))
    has_more = len(records) > limit
    messages = [_message(item) for item in reversed(records[:limit])]
    conversation = _conversation(row)
    conversation['sources'] = _sources(c, conversation['source_ids'])
    return {'conversation': conversation, 'messages': messages,
            'next_before': messages[0]['seq'] if has_more and messages else None, 'has_more': has_more}


def _history(c, id):
    rows = list(c.execute('SELECT * FROM conversation_messages WHERE conversation_id=? ORDER BY seq DESC LIMIT ?', (id, MAX_HISTORY_MESSAGES + 1)))
    result, count, omitted = [], 0, len(rows) > MAX_HISTORY_MESSAGES
    for row in rows[:MAX_HISTORY_MESSAGES]:
        if row['role'] not in {'user', 'assistant'}:
            continue
        item = {'role': row['role'], 'text': row['text'], 'state': row['state'], 'proposal_state': row['proposal_state']}
        if row['role'] == 'assistant' and row['job_id']:
            job = c.execute('SELECT result FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
            if job and job['result']:
                actions = json.loads(job['result']).get('actions', [])
                if actions:
                    item['candidate_actions'] = actions
        length = len(encode(item))
        if count + length > MAX_HISTORY_CHARACTERS:
            if not result:
                # A large prior candidate must not permanently block this scope.
                # Keep a labelled bounded excerpt; the durable original remains
                # queryable and a resumed provider still has its actual turn.
                item['text'] = item['text'][:6000]
                item['content_excerpted'] = True
                if 'candidate_actions' in item:
                    item['candidate_actions_omitted'] = len(item.pop('candidate_actions'))
                result.append(item)
            omitted = True
            break
        result.append(item)
        count += length
    return {'messages': list(reversed(result)), 'older_messages_omitted': omitted,
            'meaning': 'Discussion only. Older model proposals are not confirmed business facts.'}


def _scope_facts(core, c, scope):
    if scope['kind'] == 'timetable':
        from .timetable import timetables
        owner = core.store.get(c, scope['entity_id'])
        current = timetables(core, c, {'id': owner['id']})
        courses = [core.store.entity(r) for r in c.execute("SELECT * FROM entities WHERE type='course' AND archived=0 ORDER BY title LIMIT 101")]
        return {'scope': scope, 'owner': owner, 'timetable': current,
                'available_courses': [{'id': e['id'], 'title': e['title']} for e in courses[:100]],
                'course_coverage_complete': len(courses) <= 100,
                'guidance': 'Use apply_timetable only. Teaching week 1 starts on the explicit semester_start Monday. Read week_numbering and recess_weeks from this timetable snapshot: teaching skips recess weeks, calendar keeps calendar numbering. Recess weeks have no classes. Missing anchors or unclear teaching weeks remain unknown. Course matching is optional; never invent an owner ID.'}
    if scope['kind'] not in {'course', 'object'}:
        return {'scope': scope}
    owner = core.store.get(c, scope['entity_id'])
    rows = c.execute("""WITH RECURSIVE tree(id) AS (
        SELECT ? UNION SELECT e.id FROM entities e JOIN tree t ON e.parent_id=t.id WHERE e.archived=0)
        SELECT e.* FROM entities e WHERE e.archived=0 AND
        (e.id IN (SELECT id FROM tree) OR e.type='event' AND json_extract(e.data,'$.owner_id') IN (SELECT id FROM tree))
        ORDER BY e.type,e.created_at,e.rowid LIMIT 501""", (owner['id'],))
    records = [core.store.entity(row) for row in rows]
    if len(records) > 500:
        raise BusinessError('context_limit', '该范围包含超过 500 项记录，请选择更具体的项目或事项分批讨论。')
    if owner['type'] in {'event', 'milestone', 'assessment', 'task'}:
        from .recurring import query_rules
        rules = query_rules(core, c, {'anchor_id': owner['id'], 'limit': 100})
        if rules['next_offset'] is not None:
            raise BusinessError('context_limit', '此节点规则过多，请缩小范围后分批整理。')
        ids = {r['id'] for r in records}
        records.extend(r for r in rules['items'] if r['id'] not in ids)
    result = {'scope': scope, 'owner': owner, 'records': records, 'coverage': {'complete': True, 'count': len(records)}}
    # Large imported projects receive an explicitly incomplete identity index.
    if len(encode(result).encode('utf-8')) > 28000:
        summaries = []
        ordered = sorted(records, key=lambda e: (e['id'] != owner['id'], e['status'] in {'done','cancelled'}, e['type'] not in {'task','event','rule'}, e['title']))
        for entity in ordered:
            item = {k:entity[k] for k in ('id','type','title','parent_id','status','version')}
            item['data'] = {k:v for k,v in entity['data'].items() if k in {'code','date','due_date','scheduled_date','earliest_start','start','end','time_kind','recurrence','until','event_kind','priority','weight','catchup_enabled','catchup_total_quantity','catchup_unit'}}
            item['details_available_via'] = {'query':'get','id':entity['id']}
            summaries.append(item)
            if len(encode({'scope':scope,'owner':owner,'records':summaries}).encode('utf-8')) > 25000:
                summaries.pop();break
        result['records'] = summaries
        result['coverage'] = {'complete':False,'count':len(summaries),'total':len(records),
            'record_details_omitted':True,'guidance':'Identity index only. Do not infer completion gates, progress, rules or source content. Select a specific task or its source before changing details.'}
    return result


def send(core, c, p, rid, prepared=None):
    if not isinstance(p, dict) or set(p) - {'scope', 'text', 'source_ids', 'skill_id', 'request_plan'}:
        raise BusinessError('validation', '讨论只接受范围、消息、已登记资料编号和已安装技能。')
    scope = _scope(c, p.get('scope'))
    from .skill_workflows import select_skill, skill_context
    selected_skill = select_skill(p)
    if type(p.get('request_plan',False)) is not bool or p.get('request_plan') and scope['kind']!='daily_plan':
        raise BusinessError('validation','生成计划需要明确的每日计划范围。')
    text = p.get('text')
    if not isinstance(text, str) or not text.strip() or len(text) > 12000:
        raise BusinessError('validation', '请输入不超过 12000 字符的讨论内容；长资料请作为附件选择。')
    source_ids = p.get('source_ids', [])
    if not isinstance(source_ids, list) or any(not isinstance(id, str) or not id for id in source_ids):
        raise BusinessError('validation', '资料编号格式无效。')
    existing = c.execute('SELECT * FROM conversations WHERE scope_key=?', (_scope_key(scope),)).fetchone()
    conversation = _conversation(existing)
    requested = source_ids if 'source_ids' in p else conversation['source_ids'] if conversation else []
    prepared = prepared or {}
    source_versions = prepared.get('source_versions', {})
    if not isinstance(source_versions, dict) or any(not isinstance(id, str) or type(version) is not int for id, version in source_versions.items()):
        raise BusinessError('source_context_missing', '资料读取结果无效，未开始推理。')
    # Prepared keys include automatic first-course selection. Explicit [] clears
    # attachments; omitted source_ids reuses the current set without accumulating.
    sources = list(source_versions)
    if len(sources) > MAX_SOURCES:
        raise BusinessError('context_limit', '每轮讨论最多附带 12 项资料，请替换附件后分批整理。')
    if (conversation or 'source_ids' in p) and set(requested) != set(sources):
        raise BusinessError('source_context_missing', '已选资料与本轮读取结果不一致，未开始推理。')
    for id, version in source_versions.items():
        if core.store.get(c, id)['version'] != version:
            raise BusinessError('source_conflict', '准备消息期间资料版本已变化，请重新发送。', {'id': id})
    if conversation and conversation['active_job_id']:
        job = c.execute('SELECT status FROM jobs WHERE id=?', (conversation['active_job_id'],)).fetchone()
        if job and job['status'] in {'queued', 'running'}:
            raise BusinessError('conversation_busy', '这段讨论正在处理上一条消息，请等待结果或取消后继续。')
    if conversation is None:
        id, stamp = new_id(), now()
        c.execute('INSERT INTO conversations(id,scope_key,scope,source_ids,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                  (id, _scope_key(scope), encode(scope), encode(sources), stamp, stamp))
        conversation = _conversation(c.execute('SELECT * FROM conversations WHERE id=?', (id,)).fetchone())
    old_candidate = conversation['current_proposal_job_id']
    if old_candidate:
        c.execute("UPDATE jobs SET status='superseded',generation=generation+1,updated_at=? WHERE id=? AND status='awaiting_review'", (now(), old_candidate))
        c.execute("UPDATE conversation_messages SET proposal_state='superseded' WHERE conversation_id=? AND job_id=? AND proposal_state='available'", (conversation['id'], old_candidate))
    history = _history(c, conversation['id'])
    value = {'prompt': text.strip(), 'conversation_id': conversation['id'], 'conversation_scope': scope,
             'history': history, 'provider_thread_id': conversation['provider_thread_id'],
             'context_ids': [scope['entity_id']] if scope.get('entity_id') else []}
    if conversation['provider_thread_id']:
        previous = c.execute('''SELECT j.result FROM conversation_messages m JOIN jobs j ON j.id=m.job_id
            WHERE m.conversation_id=? AND m.role='assistant'
            AND json_extract(j.result,'$.provider.thread_id')=? ORDER BY m.seq DESC LIMIT 1''',
            (conversation['id'],conversation['provider_thread_id'])).fetchone()
        if previous:
            value['provider_project_path'] = json.loads(previous['result']).get('provider',{}).get('project_path')
    if scope.get('date'):
        value['date'] = scope['date']
    from .plan_assistance import requested_day
    planning_day=requested_day(text,scope,core.today(c),p.get('request_plan',False))
    if planning_day:value.update(plan_requested=True,date=planning_day)
    job = core.create_job(c, 'create_job', {'kind': 'ai', 'input': value}, rid)
    value = job['input']
    value['context']['discussion_scope'] = _scope_facts(core, c, scope)
    if scope.get('entity_id'):
        owner = core.store.get(c, scope['entity_id'])
        if owner['type'] == 'course' or owner['type'] == 'task' and (owner['data'].get('catchup_enabled') or owner['data'].get('task_kind') == 'catchup'):
            from .catchup import recovery_summary
            recovery = recovery_summary(core, c, {'course_id': owner['id']} if owner['type'] == 'course' else {'task_id': owner['id']})
            value['context']['recovery'] = {**recovery, 'items': [{k: item.get(k) for k in ('id','version','title','progress')} for item in recovery.get('items', [])]}
    value['context']['materials'] = prepared.get('source_context', [])
    value['context']['asset_content_coverage'] = 'Selected material content and per-source coverage are attached; do not claim unprovided coverage.'
    if selected_skill:
        value['context']['selected_skill'] = skill_context(selected_skill)
        value['skill_id'] = selected_skill
    if scope['kind'] == 'timetable':
        value['allowed_commands'] = ['apply_timetable']
    if planning_day:value['allowed_commands']=['create_plan']
    value['source_versions'] = source_versions
    value['local_images'] = prepared.get('local_images', [])
    value['source_ids'] = sources
    if len(encode({'prompt': value['prompt'], 'context': value['context'], 'history': history}).encode('utf-8')) > 90000:
        raise BusinessError('context_limit', '本轮资料、事实和讨论历史超过预算，请缩小资料或对象范围。')
    c.execute('UPDATE jobs SET input=? WHERE id=?', (encode(value), job['id']))
    message_id, stamp = new_id(), now()
    c.execute("INSERT INTO conversation_messages(id,conversation_id,role,text,job_id,state,source_ids,created_at) VALUES (?,?,'user',?,?,'queued',?,?)",
              (message_id, conversation['id'], text.strip(), job['id'], encode(value['source_ids']), stamp))
    c.execute('UPDATE conversations SET source_ids=?,active_job_id=?,current_proposal_job_id=NULL,version=version+1,updated_at=? WHERE id=?',
              (encode(value['source_ids']), job['id'], stamp, conversation['id']))
    conversation = _conversation(c.execute('SELECT * FROM conversations WHERE id=?', (conversation['id'],)).fetchone())
    conversation['sources'] = _sources(c, sources)
    message = _message(c.execute('SELECT * FROM conversation_messages WHERE id=?', (message_id,)).fetchone())
    return {'conversation': conversation, 'user_message': message, 'job': job}


def _conversation_for_job(c, job):
    if isinstance(job, str):
        row = c.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
        if not row:
            return None, None
        job = dict(row)
    data = json.loads(job['input']) if isinstance(job['input'], str) else job['input']
    id = data.get('conversation_id')
    return (c.execute('SELECT * FROM conversations WHERE id=?', (id,)).fetchone(), data) if id else (None, data)


def mark_running(c, job):
    conversation, _ = _conversation_for_job(c, job)
    if conversation and conversation['active_job_id'] == job['id']:
        c.execute("UPDATE conversation_messages SET state='running' WHERE job_id=? AND role='user'", (job['id'],))


def complete_job(core, c, job, result):
    conversation, value = _conversation_for_job(c, job)
    if not conversation or conversation['active_job_id'] != job['id']:
        return False
    actions = result.get('actions', [])
    proposal_state = 'available' if actions else 'none'
    provider = result.get('provider', {})
    text = result.get('summary', '')
    if result.get('unknowns'):
        text += '\n\n待确认：\n' + '\n'.join('• ' + item for item in result['unknowns'])
    c.execute("UPDATE conversation_messages SET state='completed' WHERE job_id=? AND role='user'", (job['id'],))
    c.execute("INSERT OR IGNORE INTO conversation_messages(id,conversation_id,role,text,job_id,state,proposal_state,source_ids,created_at) VALUES (?,?,'assistant',?,?,'completed',?,?,?)",
              (new_id(), conversation['id'], text, job['id'], proposal_state, encode(value.get('source_ids', [])), now()))
    c.execute('UPDATE conversations SET active_job_id=NULL,current_proposal_job_id=?,provider_thread_id=?,recovery=?,version=version+1,updated_at=? WHERE id=?',
              (job['id'] if actions else None, provider.get('thread_id'), provider.get('recovery'), now(), conversation['id']))
    return True


def update_job(core, c, job, status, error=None):
    """Call after persisted cancellation/failure, including restart reconciliation."""
    if isinstance(job, str):
        job = dict(c.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone())
    conversation, value = _conversation_for_job(c, job)
    if not conversation or job['id'] not in {conversation['active_job_id'], conversation['current_proposal_job_id']}:
        return False
    text = (error or {}).get('message') or ('本次处理已取消，可以在这段讨论中继续。' if status == 'cancelled' else '本次处理未完成，可以检查原因后继续讨论。')
    c.execute('UPDATE conversation_messages SET state=? WHERE job_id=? AND role=\'user\'', (status, job['id']))
    existing = c.execute("SELECT id FROM conversation_messages WHERE job_id=? AND role='assistant'", (job['id'],)).fetchone()
    if existing:
        c.execute('UPDATE conversation_messages SET proposal_state=? WHERE id=?', ('cancelled' if status == 'cancelled' else 'superseded', existing['id']))
    else:
        c.execute("INSERT INTO conversation_messages(id,conversation_id,role,text,job_id,state,source_ids,created_at) VALUES (?,?,'assistant',?,?,?, ?,?)",
                  (new_id(), conversation['id'], text, job['id'], status, encode(value.get('source_ids', [])), now()))
    c.execute('UPDATE conversations SET active_job_id=NULL,current_proposal_job_id=NULL,version=version+1,updated_at=? WHERE id=?', (now(), conversation['id']))
    return True


def validate_current_proposal(core, c, job):
    conversation, value = _conversation_for_job(c, job)
    if value.get('conversation_id') and (not conversation or conversation['current_proposal_job_id'] != job['id'] or conversation['active_job_id']):
        raise BusinessError('stale_proposal', '这段讨论已有新消息，只能采用当前显示的候选。')
    for id, version in value.get('source_versions', {}).items():
        if core.store.get(c, id)['version'] != version:
            raise BusinessError('source_conflict', '候选引用的资料已变化，请在当前讨论中重新核对。', {'id': id})


def mark_applied(core, c, job):
    conversation, _ = _conversation_for_job(c, job)
    if conversation:
        c.execute("UPDATE conversation_messages SET proposal_state='applied' WHERE job_id=? AND role='assistant'", (job['id'],))
        c.execute('UPDATE conversations SET version=version+1,updated_at=? WHERE id=?', (now(), conversation['id']))


def reconcile(core, c):
    for row in c.execute('SELECT j.* FROM conversations v JOIN jobs j ON j.id=v.active_job_id WHERE j.status NOT IN (\'queued\',\'running\')').fetchall():
        job = dict(row)
        error = json.loads(job['error']) if job['error'] else {'message': '先前处理已结束，请在当前讨论继续。'}
        update_job(core, c, job, job['status'], error)


def reset_after_restore(c):
    c.execute("UPDATE conversation_messages SET state='cancelled' WHERE state IN ('queued','running')")
    c.execute("UPDATE conversation_messages SET proposal_state='superseded' WHERE proposal_state='available'")
    c.execute("UPDATE conversations SET active_job_id=NULL,current_proposal_job_id=NULL,provider_thread_id=NULL,recovery='restored_history',version=version+1")
