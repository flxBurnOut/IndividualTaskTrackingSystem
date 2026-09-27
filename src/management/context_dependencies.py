"""Versioned dependency evidence using the existing bounded context reader."""
from __future__ import annotations

from .daily_flow import completed_on_or_before
from .schemas import BusinessError

PREFIX = 'dependencies:'
PLAN_TYPES = {'task', 'event', 'milestone'}


def reference(identifier):
    # Stable even when links change: entity bodies keep their own entity version.
    # The linked dependency item has an independent fingerprint and page cursor.
    return {'reader': 'read_context_item', 'id': PREFIX + identifier}


def snapshot(core, c, identifier, day):
    entity = core.store.get(c, identifier)

    def related(direction):
        own, other = ('source_id', 'target_id') if direction == 'predecessors' else ('target_id', 'source_id')
        rows = c.execute('SELECT e.* FROM links l JOIN entities e ON e.id=l.' + other
                         + ' WHERE l.' + own + "=? AND l.kind='depends_on' ORDER BY e.id", (identifier,))
        result = []
        for row in rows:
            target = core.store.entity(row)
            confirmed = completed_on_or_before(core, c, target, day)
            from .task_views import state as task_state
            completion = (task_state(core, c, target['id'], day) or 'unknown') if target['type']=='task' else ('done' if confirmed else 'unknown')
            result.append({key: target[key] for key in ('id', 'type', 'title', 'version', 'status', 'archived')}
                          | {'completed': confirmed, 'completion_state': completion,
                             'details': {'reader': 'read_context_item', 'id': target['id'], 'version': target['version']}})
        return result

    return {'entity_id': identifier, 'entity_version': entity['version'], 'as_of_date': day,
            'direction': 'This entity depends on predecessors; dependents depend on this entity.',
            'predecessors': related('predecessors'), 'dependents': related('dependents'),
            'completion_basis': 'Stored completion evidence as of this date; a planned block is not completion. '
                                'completed=false means completion is not confirmed; unknown is not an incomplete report.'}


def validate_plan_reads(core, c, op, actions):
    """Require current dependency evidence only for targets used by a plan.

    The business validator still enforces the dependency itself. This prevents a
    candidate from relying on unread or stale links even when its order happens
    to pass that check. No business records or completion states are changed.
    """
    from .context_service import digest
    targets = set()
    for action in actions:
        payload = action['payload']
        protected = {}
        current = None
        if action['command'] in {'revise_plan', 'add_to_plan'}:
            from .reviews import _latest_plan
            from .daily_flow import _protected_blocks
            day = payload.get('date') or op['scope']['date']
            current = _latest_plan(core, c, day)
            if current:
                protected = _protected_blocks(core, c, day, current['data'].get('blocks', []))
        if action['command'] in {'create_plan', 'revise_plan'}:
            targets.update(block['target_id'] for block in payload.get('blocks', [])
                           if isinstance(block, dict) and isinstance(block.get('target_id'), str)
                           and block['target_id'] not in protected)
        elif action['command'] == 'add_to_plan' and isinstance(payload.get('target_id'), str):
            if payload['target_id'] not in protected:targets.add(payload['target_id'])
            if current:
                targets.update(block['target_id'] for block in current['data'].get('blocks', [])
                               if block['target_id'] not in protected)
    for identifier in sorted(targets):
        value = snapshot(core, c, identifier, op['scope']['date'])
        reader = reference(identifier)
        read = c.execute('SELECT version,complete FROM context_parts WHERE operation_id=? AND item_id=?',
                         (op['id'], reader['id'])).fetchone()
        if not read and not value['predecessors']:
            continue
        if read and read['version'] != digest(value):
            raise BusinessError('context_changed', '计划事项的依赖或相关完成状态已变化，请重新读取依赖后核对候选。', reader)
        if not read or not read['complete']:
            raise BusinessError('context_coverage', '计划事项的前置依赖尚未完整读取，请核对后再提交候选。', reader)
