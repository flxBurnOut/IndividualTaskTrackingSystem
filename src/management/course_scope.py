"""Course candidate targets are checked at preview and transactional adoption.

Source selection describes evidence, not permission to modify another course.
New candidate commands must explicitly declare their target interpretation here.
"""
import json

from .schemas import BusinessError


TARGET_FIELDS = {
    'update': 'id',
    'record_feedback': 'target_id',
    'set_task_completion': 'target_id',
    'record_recovery_progress': 'task_id',
    'correct_recovery_scope': 'id',
}


def check(core, c, owner, action):
    command, payload = action['command'], action['payload']

    def reject():
        raise BusinessError('course_scope', '当前课程候选不能修改另一课程或范围外的事项；请在对应范围讨论。')

    def belongs(identifier):
        if not isinstance(identifier, str) or not identifier:
            return False
        return bool(c.execute('''WITH RECURSIVE ancestors(id,parent_id) AS
            (SELECT id,parent_id FROM entities WHERE id=? UNION
             SELECT e.id,e.parent_id FROM entities e JOIN ancestors a ON e.id=a.parent_id)
            SELECT 1 FROM ancestors WHERE id=? LIMIT 1''', (identifier, owner)).fetchone())

    def require_target(identifier):
        if not isinstance(identifier, str) or not identifier:
            reject()
        if belongs(identifier):
            return
        row = c.execute("SELECT data FROM entities WHERE id=? AND type='event'", (identifier,)).fetchone()
        if row and belongs(json.loads(row[0]).get('owner_id')):
            return
        reject()

    def require_blocks(blocks):
        if not isinstance(blocks, list):
            reject()
        for block in blocks:
            if not isinstance(block, dict):
                reject()
            require_target(block.get('target_id'))

    def require_plan(identifier):
        if not isinstance(identifier, str) or not identifier:
            reject()
        plan = core.store.get(c, identifier)
        if plan['type'] != 'plan':
            reject()
        blocks=plan['data'].get('blocks', [])
        if not blocks:
            reject()  # An empty global plan has no provable course ownership.
        require_blocks(blocks)

    if command in TARGET_FIELDS:
        require_target(payload.get(TARGET_FIELDS[command]))
        if command == 'update':
            data = (payload.get('patch') or {}).get('data') or {}
            if 'owner_id' in data and not belongs(data['owner_id']):
                reject()
        return
    if command == 'create':
        kind, data = payload.get('type'), payload.get('data') or {}
        if kind not in {'assessment','milestone','event','task','topic','note'}:
            reject()
        if not belongs(data.get('owner_id') if kind == 'event' else payload.get('parent_id')):
            reject()
        return
    if command == 'set_recovery_task':
        if payload.get('course_id') != owner:
            reject()
        for field in ('id', 'original_task_id'):
            if payload.get(field):
                require_target(payload[field])
        return
    if command == 'set_recurring_rule':
        require_target(payload.get('anchor_id'))
        if payload.get('id'):
            require_target(payload['id'])
        return
    if command in {'create_plan', 'revise_plan', 'add_to_plan'}:
        # A new plan also becomes the day's current version. Replacing a mixed
        # or other-course plan is a global plan action, even if the new blocks
        # themselves only mention this course.
        from .core import date_value
        from .reviews import _latest_plan
        if payload.get('mode') == 'rest':
            reject()  # A whole-day rest decision is not a single-course edit.
        current = _latest_plan(core, c, date_value(payload.get('date')).isoformat())
        if current:
            require_plan(current['id'])
        for field in ('plan_id', 'supersedes_id'):
            if payload.get(field):
                require_plan(payload[field])
        if command == 'add_to_plan':
            require_target(payload.get('target_id'))
        else:
            require_blocks(payload.get('blocks', []))
        return
    if command == 'submit_daily_review':
        from .core import date_value
        from .reviews import _daily
        daily = _daily(core, c, date_value(payload.get('date')).isoformat())
        targets = {item.get('item_id', item['target_id']): item['target_id'] for item in daily['items']}
        answers = payload.get('answers')
        if not isinstance(answers, list):
            reject()
        for answer in answers:
            if not isinstance(answer, dict):
                reject()
            key = answer.get('item_id', answer.get('target_id'))
            if not isinstance(key, str):
                reject()
            require_target(targets.get(key))
        return
    # Global timetable/period review and future commands do not acquire course
    # write authority merely by joining the generic candidate command registry.
    reject()
