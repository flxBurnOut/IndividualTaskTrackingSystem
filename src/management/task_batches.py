"""Previewed, bounded task creation/splitting inside Core's single transaction."""
from __future__ import annotations

import copy
import hashlib
import hmac

from .schemas import BusinessError
from .storage import encode


MAX_TASKS = 50
MAX_LINKS = 500
MAX_BATCH_BYTES = 180 * 1024
ITEM_KEYS = {'title', 'completion_gate', 'estimated_minutes', 'due_date', 'scheduled_date'}
INPUT_KEYS = {'mode', 'items', 'sequential', 'parent_id', 'parent_version', 'source_id', 'source_version'}


def _error(message):
    raise BusinessError('validation', message)


def _versioned_parent(core, c, identifier, version):
    if not isinstance(identifier, str) or not identifier:
        _error('请选择有效的归属或原任务。')
    if type(version) is not int or version < 1:
        _error('请重新读取归属或原任务的版本，再预览。')
    entity = core._versioned(c, {'id': identifier, 'version': version})
    if entity['archived'] or entity['status'] in {'cancelled', 'draft'}:
        _error('归属或原任务已删除、取消或仍是草稿，请先核对状态。')
    return entity


def _normalise(core, c, parameters):
    from .core import date_value
    from .task_views import state as completion_state
    from .daily_flow import completed_on_or_before
    if not isinstance(parameters, dict) or set(parameters) - INPUT_KEYS:
        _error('批量任务包含不支持的参数。')
    mode = parameters.get('mode', 'create')
    if not isinstance(mode, str) or mode not in {'create', 'split'}:
        _error('请选择批量录入或拆分任务。')
    sequential = parameters.get('sequential', False)
    if type(sequential) is not bool:
        _error('是否依次执行需要明确勾选。')
    rows = parameters.get('items')
    minimum = 2 if mode == 'split' else 1
    if not isinstance(rows, list) or not minimum <= len(rows) <= MAX_TASKS:
        _error(f'本次需要 {minimum} 至 {MAX_TASKS} 项任务；更大的内容请明确分批。')
    normal = {'mode': mode, 'items': [], 'sequential': sequential}
    source = parent = None
    dependencies = []
    if mode == 'split':
        if parameters.get('parent_id') is not None or parameters.get('parent_version') is not None:
            _error('拆分任务的归属固定为原任务。')
        source = _versioned_parent(core, c, parameters.get('source_id'), parameters.get('source_version'))
        if source['type'] != 'task':
            _error('只能拆分实际任务。')
        if completion_state(core, c, source['id']) == 'done':
            _error('原任务已经完成；如有新工作请新建任务，或先明确重新打开原任务。')
        parent = source
        normal.update(source_id=source['id'], source_version=source['version'])
        dependency_count = c.execute("SELECT count(*) FROM links WHERE source_id=? AND kind='depends_on'", (source['id'],)).fetchone()[0]
        required_links = len(rows) * (1 + dependency_count) + (len(rows) - 1 if sequential else 0)
        if required_links > MAX_LINKS:
            _error(f'本批需要 {required_links} 项关联，超过 {MAX_LINKS} 项上限；请减少本批任务数。')
        dependency_rows = c.execute("SELECT e.* FROM links l JOIN entities e ON e.id=l.target_id WHERE l.source_id=? AND l.kind='depends_on' ORDER BY e.id", (source['id'],)).fetchall()
        for row in dependency_rows:
            dep = core.store.entity(row)
            core._ensure_mutable(c, dep)
            if dep['archived'] or dep['status'] in {'cancelled', 'draft'}:
                _error('原任务的前置事项“' + dep['title'] + '”已删除、取消或仍是草稿，请先核对。')
            dependencies.append({'id': dep['id'], 'title': dep['title'], 'version': dep['version'],
                                 'completed': completed_on_or_before(core, c, dep, core.today(c))})
    else:
        if parameters.get('source_id') is not None or parameters.get('source_version') is not None:
            _error('批量录入不接受拆分来源。')
        if parameters.get('parent_id') is not None:
            parent = _versioned_parent(core, c, parameters['parent_id'], parameters.get('parent_version'))
            normal.update(parent_id=parent['id'], parent_version=parent['version'])
        elif parameters.get('parent_version') is not None:
            _error('未选择归属时不应填写归属版本。')
    links_count = (len(rows) * (1 + len(dependencies)) if source else 0) + (len(rows) - 1 if sequential else 0)
    if links_count > MAX_LINKS:
        _error(f'本批需要 {links_count} 项关联，超过 {MAX_LINKS} 项上限；请减少本批任务数。')
    for index, row in enumerate(rows, 1):
        prefix = f'第 {index} 项：'
        if not isinstance(row, dict) or set(row) - ITEM_KEYS:
            _error(prefix + '仅支持任务标题、完成标准、估时和日期。')
        title = row.get('title')
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 300:
            _error(prefix + '标题需要 1 至 300 个字符。')
        gate = row.get('completion_gate', '')
        if not isinstance(gate, str) or len(gate) > 6000:
            _error(prefix + '完成标准需要不超过 6,000 字符的文字。')
        gate = gate.strip()
        if source and not gate:
            _error(prefix + '拆分后的任务需要明确完成标准。')
        item = {'title': title.strip(), 'completion_gate': gate}
        if row.get('estimated_minutes') is not None:
            minutes = row['estimated_minutes']
            if type(minutes) is not int or not 1 <= minutes <= 1440:
                _error(prefix + '预计分钟需要 1 至 1440 的整数；未知请留空。')
            item['estimated_minutes'] = minutes
        for key in ('due_date', 'scheduled_date'):
            value = row.get(key)
            if value is not None and value != '':
                item[key] = date_value(value, prefix + '日期').isoformat()
        if item.get('due_date') and item.get('scheduled_date') and item['scheduled_date'] > item['due_date']:
            _error(prefix + '明确安排日期晚于截止日期，请先调整。')
        entity = {'id': 'task-batch-preview-' + str(index), 'type': 'task', 'title': item['title'],
                  'parent_id': parent['id'] if parent else None, 'status': 'active', 'archived': False,
                  'data': {key: value for key, value in item.items() if key != 'title'}}
        # Validate the same type, parent/depth and field rules as individual create.
        core._validate(c, entity)
        normal['items'].append(item)
    if len(encode(normal).encode('utf-8')) > MAX_BATCH_BYTES:
        _error('本批文字过多，请减少本批条目或缩短说明，尚未写入。')
    return normal, source, parent, dependencies, links_count


def _token(core, c, payload):
    # A content/version fence, not an authorization credential. All writes still
    # pass through Core's identity, revision, transaction and receipt boundary.
    return hashlib.sha256(encode({'batch': payload, **core.store.state(c)}).encode('utf-8')).hexdigest()


def preview(core, c, parameters):
    payload, source, parent, dependencies, links_count = _normalise(core, c, parameters)
    warnings = ['本批一次保存；任一条目失败时不会留下半批任务。',
                '本操作不调整每日计划；保存后可分别编辑或删除任务，暂不提供批次一键撤销。']
    titles = [item['title'].casefold() for item in payload['items']]
    if len(titles) != len(set(titles)):
        warnings.append('存在重复标题；仍将创建不同任务，请核对每一项。')
    if any(not item['completion_gate'] for item in payload['items']):
        warnings.append('有任务尚未填写完成标准，可在创建后补充。')
    if source:
        warnings.append('原任务、完成标准、计划和历史反馈保持不变；完成所有子任务不会自动完成原任务。')
        warnings.append('原有后续任务仍依赖原任务；每个子任务都会继承原任务的前置要求。')
        source = {**copy.deepcopy(source),
                  'children_count': c.execute("SELECT count(*) FROM entities WHERE type='task' AND archived=0 AND parent_id=?", (source['id'],)).fetchone()[0],
                  'dependencies': dependencies}
        if source['children_count']:
            warnings.append(f"原任务已有 {source['children_count']} 项子任务；本次是补充新子任务，不替换它们。")
        if source['data'].get('catchup_enabled'):
            warnings.append('原任务的补课范围和累计进度仍单独保留，不分摊到新子任务。')
    if payload['sequential']:
        warnings.append('按当前行顺序建立前后依赖：后一项以前一项为前置任务。')
    else:
        warnings.append('没有勾选依次执行，不会仅凭行顺序推断任务依赖。')
    return {'command': 'split_task' if source else 'create_task_batch',
            'payload': {**payload, 'preview_token': _token(core, c, payload)},
            'items': copy.deepcopy(payload['items']), 'count': len(payload['items']),
            'source': source, 'parent': copy.deepcopy(parent), 'dependencies': dependencies,
            'link_count': links_count, 'warnings': warnings}


def apply(core, c, parameters, rid, *, mode):
    if not isinstance(parameters, dict) or set(parameters) - (INPUT_KEYS | {'preview_token'}):
        _error('批量任务提交格式无效，请重新预览。')
    token = parameters.get('preview_token')
    if not isinstance(token, str) or len(token) != 64 or parameters.get('mode') != mode:
        raise BusinessError('preview_conflict', '请先预览当前这一批，再确认保存。')
    raw = {key: value for key, value in parameters.items() if key != 'preview_token'}
    payload, source, parent, dependencies, _ = _normalise(core, c, raw)
    if not hmac.compare_digest(token, _token(core, c, payload)):
        raise BusinessError('preview_conflict', '预览内容或记录版本已变化，请保留输入，重新预览后再确认。')
    items, links = [], []

    def link(source_id, target_id, kind):
        value = {'source_id': source_id, 'target_id': target_id, 'kind': kind}
        result = core._dispatch(c, 'link', value, rid)
        links.append({**value, 'id': result['id']})

    for item in payload['items']:
        data = {key: value for key, value in item.items() if key != 'title'}
        if source:
            data['source_text'] = f"由用户拆分原任务“{source['title']}”（{source['id']}）。原计划和反馈保留。"
        entity = core._dispatch(c, 'create', {'type': 'task', 'title': item['title'],
            'parent_id': parent['id'] if parent else None, 'status': 'active', 'data': data}, rid)
        task = entity['entity']
        if source:
            link(task['id'], source['id'], 'contributes')
            for dependency in dependencies:
                link(task['id'], dependency['id'], 'depends_on')
        if items and payload['sequential']:
            link(task['id'], items[-1]['id'], 'depends_on')
        items.append(task)
    # Generic undo cannot safely account for subsequent feedback/plan references.
    # Retain one durable batch audit even when this batch created no links.
    core.store.change(c, rid, 'task_batch', after={'id': source['id'] if source else None,
        'mode': mode, 'task_ids': [item['id'] for item in items], 'sequential': payload['sequential']})
    return {'items': items, 'links': links, 'source': source, 'count': len(items),
            'sequential': payload['sequential'], 'batch_request_id': rid, 'source_unchanged': True}
