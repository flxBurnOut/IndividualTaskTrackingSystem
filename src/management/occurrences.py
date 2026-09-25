"""Dated fixed-schedule execution, separate from recurring event definitions.

Projection is read-only. Only explicit feedback persists an occurrence snapshot;
future weeks never create task rows. All mutations share Core's transaction.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json

from .schemas import BusinessError
from .storage import encode

CLASS_KINDS = {'lecture', 'tutorial', 'lab'}
ATTENDANCE_CHOICES = [('attended', '已参加'), ('missed_needs_catchup', '缺课需补'), ('absent', '未参加')]
TASK_CHOICES = [('done', '完成'), ('incomplete', '未完成')]


def initialize(c):
    c.execute("""CREATE TABLE IF NOT EXISTS occurrence_reviews(
        item_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,occurrence_date TEXT NOT NULL,
        business_date TEXT NOT NULL,snapshot TEXT NOT NULL,result TEXT NOT NULL,
        feedback_id TEXT NOT NULL,catchup_task_id TEXT,version INTEGER NOT NULL DEFAULT 1,
        UNIQUE(event_id,occurrence_date))""")
    c.execute('CREATE INDEX IF NOT EXISTS occurrence_review_day ON occurrence_reviews(business_date)')


def identity(event_id, occurrence_date):
    return 'occurrence:' + hashlib.sha256(encode([event_id, occurrence_date]).encode()).hexdigest()


def _clock(value):
    return f'{value // 60:02d}:{value % 60:02d}' if value is not None else None


def _item(core, c, event, day, presenter):
    data = event.get('effective', event['data'])
    fields = presenter.fields({**event, 'data': data})
    course = fields.get('owner_type') == 'course'
    attendance = course and data.get('event_kind') in CLASS_KINDS
    unit = fields.get('learning_unit_label')
    # Numbered units must come from source evidence, never week arithmetic.
    if attendance and not unit:
        fields['display_title'] += ' · 课次待确认'
        fields['learning_unit_missing'] = True
    from zoneinfo import ZoneInfo
    instant=dt.datetime.now(ZoneInfo(core.store.meta(c,'settings')['timezone']))
    started=(day<instant.date().isoformat() or day==instant.date().isoformat() and
             (event.get('start_minute') is None or event['start_minute']<=instant.hour*60+instant.minute))
    return {**fields, 'item_id': identity(event['id'], event['occurrence_date']),
            'target_id': event['id'], 'target_type': 'event', 'target_version': event['version'],
            'occurrence_date': event['occurrence_date'], 'business_date': day, 'fixed_schedule': True,
            'title': event['title'], 'location':data.get('location'), 'start': _clock(event.get('start_minute')),
            'end': _clock(event.get('end_minute')), 'planned_minutes': None,
            'completion_gate': '到场与补课分别记录，不代表掌握或提交。' if attendance else '只记录这一次日程的执行情况。',
            'review_dimension': 'attendance' if attendance else 'completion',
            'choices': ATTENDANCE_CHOICES if attendance else TASK_CHOICES,
            'result': None, 'raw_result': None, 'original_completion': None, 'reported': False,
            'target_archived': bool(event['archived']), 'available': True,
            'can_review': started and not event['archived']}


def project(core, c, day):
    from .presentation import Presenter
    presenter = Presenter(core, c)
    events, _ = core._events(c, day)
    from zoneinfo import ZoneInfo
    from .event_time import local_datetime
    zone=ZoneInfo(core.store.meta(c,'settings')['timezone'])
    def starts_today(event):
        data=event.get('effective',event['data'])
        if not data.get('start') or data.get('time_kind') in {'unknown','approximate','date_only'}:return True
        try:
            h,m=map(int,data['start'].split(':'))
            instant=local_datetime(dt.date.fromisoformat(event['occurrence_date']),h*60+m,ZoneInfo(data.get('timezone') or str(zone)))
            return instant.astimezone(zone).date().isoformat()==day
        except ValueError:return True
    items = {identity(e['id'], e['occurrence_date']): _item(core,c,e,day,presenter) for e in events if starts_today(e)}
    # Include recorded history even after the source schedule changed or cancelled.
    records = {row['item_id']: row for row in c.execute(
        'SELECT * FROM occurrence_reviews WHERE business_date=?', (day,))}
    for key in list(items):
        if key not in records:
            row = c.execute('SELECT * FROM occurrence_reviews WHERE item_id=?', (key,)).fetchone()
            if row: records[key] = row
    for key, row in records.items():
        current = items.get(key)
        item = json.loads(row['snapshot'])
        source = presenter.get(row['event_id'])
        item.update(result=row['result'], raw_result=row['result'], reported=True,
                    feedback_id=row['feedback_id'], occurrence_version=row['version'],
                    catchup_task_id=row['catchup_task_id'], schedule_changed=bool(not current or current['target_version'] != item['target_version']),
                    target_version=source['version'] if source else item['target_version'],
                    target_archived=bool(source and source['archived']), available=bool(source),
                    can_review=bool(source and not source['archived'] and day <= core.today(c)))
        if row['catchup_task_id']:
            from .catchup import _project
            task = presenter.get(row['catchup_task_id'])
            item['catchup_progress'] = _project(core,c,task,core.today(c)) if task else None
        items[key] = item
    # Direct MCP record_feedback and GUI feedback share the same evidence.
    # The occurrence ledger adds identity/snapshot/recovery links; it must never
    # conceal a newer explicit correction stored through another entrance.
    for item in items.values():
        dimension=item['review_dimension']
        latest=c.execute('''SELECT id,json_extract(data,?) AS value FROM entities
            WHERE type='feedback' AND archived=0 AND json_extract(data,'$.target_id')=?
            AND json_extract(data,'$.business_date')=? AND json_type(data,?) IS NOT NULL
            ORDER BY created_at DESC,rowid DESC LIMIT 1''',
            ('$.dimensions.'+dimension,item['target_id'],item['business_date'],'$.dimensions.'+dimension)).fetchone()
        if latest and latest['id']!=item.get('feedback_id'):
            item.update(result=latest['value'],raw_result=latest['value'],feedback_id=latest['id'],
                        reported=latest['value'] not in (None,'unknown'),
                        original_completion=latest['value'] if dimension=='completion' else None)
    return sorted(items.values(), key=lambda i: (i.get('start') or '99:99', i['title'], i['item_id']))


def signature(items):
    return hashlib.sha256(encode([{k:i.get(k) for k in
        ('item_id','target_id','target_version','occurrence_version','feedback_id','can_review')}
        for i in items]).encode()).hexdigest()


def record(core, c, item, result, rid):
    choices = dict(item['choices'])
    if result not in choices or not item['can_review']:
        raise BusinessError('occurrence_feedback', '请选择本次日程的有效结果；未来日程不能提前记为实际出勤。')
    if item['raw_result'] == result:
        return None
    day = item['business_date']
    dimension = item['review_dimension']
    value = 'absent' if result == 'missed_needs_catchup' else result
    payload = {'target_id':item['target_id'], 'business_date':day,
               'dimensions':{dimension:value},
               'source_text':f"用户明确选择 {day} {item['owner_label']} {item['title']}：{choices[result]}。"}
    if item.get('feedback_id'): payload['supersedes_id'] = item['feedback_id']
    feedback = core.feedback(c,payload,rid)
    task_id = item.get('catchup_task_id')
    if result == 'missed_needs_catchup' and not task_id:
        from .catchup import set_recovery_task
        unit = item.get('learning_unit_label')
        label = unit or (item['title'] + ' · 课次待确认 · ' + item['occurrence_date'])
        # A stable known learning unit can reuse its existing recovery task.
        lesson_key = unit or item['item_id']
        existing = c.execute("""SELECT * FROM entities WHERE type='task' AND parent_id=?
            AND json_extract(data,'$.catchup_enabled')=1
            AND json_extract(data,'$.catchup_lesson_key')=? LIMIT 1""",
            (item['owner_id'], lesson_key)).fetchone()
        if existing:
            task_id = existing['id']
        else:
            created = set_recovery_task(core,c,{'course_id':item['owner_id'],
                'title':'补课 · '+label, 'lesson_key':lesson_key, 'unit':'次', 'total_quantity':1,
                'completion_gate':'补完这次课程内容，并明确确认完成。', 'reason':'self_reported',
                'source_text':payload['source_text']},rid)
            task_id = created['entity']['id']
    snapshot = {k:v for k,v in item.items() if k not in
                {'catchup_progress','result','raw_result','feedback_id','occurrence_version'}}
    c.execute("""INSERT INTO occurrence_reviews(item_id,event_id,occurrence_date,business_date,snapshot,result,feedback_id,catchup_task_id)
        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET
        result=excluded.result,feedback_id=excluded.feedback_id,catchup_task_id=excluded.catchup_task_id,version=version+1""",
        (item['item_id'],item['target_id'],item['occurrence_date'],day,encode(snapshot),result,feedback['id'],task_id))
    return feedback['id']
