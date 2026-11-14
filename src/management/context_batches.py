"""Bounded batch transport over the existing durable context ledger.

Reads never acknowledge analysis. Unacknowledged deliveries are returned first,
so a lost response needs no volatile cursor. Checkpoints share one transaction.
"""
from __future__ import annotations
import json
from . import context_service as ctx
from .schemas import BusinessError
from .storage import now

MAX_ITEMS = 32
MAX_BYTES = 64 * 1024
DEFAULT_BYTES = 48 * 1024
MAX_IMAGES = 4


def integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise BusinessError('context_batch_parameters', f'{name} must be an integer in [{minimum}, {maximum}].')
    return value


def budget(op, requested):
    requested = integer(requested, 'byte_limit', 2048, MAX_BYTES)
    ctx._budget(op, 2048)  # Retain managed-stage/phase/call protections.
    return min(requested, ctx.STAGE_BYTES-op['delivered_bytes']-ctx.RESERVE_BYTES) if op['job_id'] else requested


def _step(core, c, op, row):
    from . import materials
    step = dict(row)
    materials._check_source_version(c, step['source_id'], step['source_version'])
    if step['chunk_key'] == 'gap':
        saved = c.execute("""SELECT m.error FROM context_sources s LEFT JOIN material_catalog m
            ON m.key=s.material_key WHERE s.operation_id=? AND s.entity_id=?""",
            (op['id'], step['source_id'])).fetchone()
        failure = json.loads(saved[0]) if saved and saved[0] else {
            'code': 'source_unavailable', 'message': '原件缺少可读快照。', 'details': {}}
        content = {'kind': 'gap', 'text': failure['message'], 'error': failure, 'locator': '原件读取缺口'}
        chunk = None
    else:
        key, seq = step['chunk_key'].rsplit(':', 1)
        chunk = int(seq)
        unit = c.execute('SELECT * FROM material_chunks WHERE material_key=? AND seq=?', (key, chunk)).fetchone()
        content = {'kind': unit['kind'], 'locator': unit['locator'], 'sha256': unit['sha256']}
        if unit['kind'] == 'image':
            content['image_ref'] = step['chunk_key']
        else:
            content['text'] = materials.verified_bytes(core, unit['sha256']).decode('utf-8')
    return {'step_key': step['step_key'], 'delivery_token': materials.delivery_token(core, c, op, step),
            'source_id': step['source_id'], 'source_version': step['source_version'],
            'chunk': chunk, 'content': content}


def read_material_batch(core, p):
    from . import materials
    limit = integer(p.get('limit', 20), 'limit', 1, MAX_ITEMS)
    image_limit = integer(p.get('max_images', 4), 'max_images', 1, MAX_IMAGES)
    requested = integer(p.get('byte_limit', DEFAULT_BYTES), 'byte_limit', 2048, MAX_BYTES)
    with core.store.connect() as c:
        op = ctx._operation(core, c, p.get('operation_id'), writable=True)
        budget(op, requested)
    # Binding and bounded extraction use their own transactions, never the read lock.
    source = materials._source_for_read(core, p)
    with core.store.connect() as c:
        delivered = c.execute("""SELECT 1 FROM context_steps WHERE operation_id=? AND status='delivered'
            AND (? IS NULL OR source_id=?) LIMIT 1""", (op['id'], p.get('source_id'), p.get('source_id'))).fetchone()
    if not delivered and source and source['material_key'] not in {None, 'unavailable'}:
        materials.extract_batch(core, source['material_key'])
    with core.store.lock, core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        op = ctx._operation(core, c, p['operation_id'], writable=True)
        maximum = budget(op, requested)
        materials.sync_steps(c, op)
        rows = c.execute("""SELECT * FROM context_steps WHERE operation_id=? AND status!='completed'
            AND (? IS NULL OR source_id=?) ORDER BY CASE WHEN status='delivered' THEN 0 ELSE 1 END,
            source_id, CASE WHEN chunk_key='gap' THEN 1 ELSE 0 END,
            CAST(substr(step_key,instr(step_key,':')+1) AS INTEGER) LIMIT ?""",
            (op['id'], p.get('source_id'), p.get('source_id'), limit+1)).fetchall()
        items = []
        images = 0
        for row in rows:
            if len(items) >= limit:
                break
            item = _step(core, c, op, row)
            extra_image = int(item['content']['kind'] == 'image')
            cost = ctx.size({'items': items+[item]}) + 1024 + (images+extra_image)*ctx.IMAGE_CONTEXT_BYTES
            if images+extra_image > image_limit or cost > maximum:
                if not items:
                    if op['job_id'] and cost <= requested:
                        raise ctx._checkpoint_required(op)
                    raise BusinessError('context_batch_limit', '单个资料块超过本批容量；请增大 byte_limit 后重试，未跳过内容。',
                        {'required_bytes': cost, 'byte_limit': maximum, 'progress_preserved': True})
                break
            items.append(item)
            images += extra_image
        pending_extraction = materials.incomplete_sources(core, c, op)
        if not items:
            paused = c.execute("""SELECT 1 FROM context_sources s JOIN material_catalog m ON m.key=s.material_key
                WHERE s.operation_id=? AND m.status='paused' AND (? IS NULL OR s.entity_id=?) LIMIT 1""",
                (op['id'], p.get('source_id'), p.get('source_id'))).fetchone()
            if paused:
                raise BusinessError('material_paused', '资料提取已暂停，原进度保留；资源恢复后继续。')
        for item in items:
            c.execute("UPDATE context_steps SET status='delivered',updated_at=? WHERE operation_id=? AND step_key=? AND status='pending'",
                      (now(), op['id'], item['step_key']))
        remaining = c.execute("SELECT count(*) FROM context_steps WHERE operation_id=? AND status!='completed'", (op['id'],)).fetchone()[0]
        selected_remaining = c.execute("""SELECT count(*) FROM context_steps WHERE operation_id=? AND status!='completed'
            AND (? IS NULL OR source_id=?)""", (op['id'], p.get('source_id'), p.get('source_id'))).fetchone()[0]
        source_pending = bool(pending_extraction)
        if p.get('source_id'):
            source_pending = bool(c.execute("""SELECT 1 FROM context_sources s LEFT JOIN material_catalog m ON m.key=s.material_key
                WHERE s.operation_id=? AND s.entity_id=? AND (s.material_key IS NULL OR m.status IN ('pending','paused'))""",
                (op['id'], p['source_id'])).fetchone())
        result = {'operation_id': op['id'], 'items': items, 'all_steps_processed': not remaining and not pending_extraction,
                  'source_complete': not selected_remaining and not source_pending,
                  'extraction_pending': bool(pending_extraction), 'outstanding_steps': remaining,
                  'next_action': 'analyze_then_checkpoint_batch' if items else 'continue_extraction' if pending_extraction else 'merge_saved_facts',
                  'instruction': '每项文字、图像或缺口分别分析；用 checkpoint_context_batch 保存。未确认项会优先重发，确认前不算完成。'}
        if ctx.size(result)+images*ctx.IMAGE_CONTEXT_BYTES > maximum:
            raise BusinessError('context_batch_limit', '本批信封超过容量；进度未推进，请增大 byte_limit。')
        c.execute('UPDATE context_operations SET delivered_bytes=delivered_bytes+? WHERE id=?', (images*ctx.IMAGE_CONTEXT_BYTES, op['id']))
        result = ctx._charge(c, op, result)
        c.commit()
        return result


def checkpoint_batch(core, c, op, p):
    entries = p.get('items')
    if not isinstance(entries, list) or len(entries) > MAX_ITEMS or ctx.size(p) > MAX_BYTES:
        raise BusinessError('context_batch_parameters', '检查点批次最多 32 项、64 KiB；未保存部分批次。')
    if not entries and 'summary' not in p and not p.get('yield'):
        raise BusinessError('context_batch_parameters', '空批次需要摘要或 yield。')
    seen = set()
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry)-{'step_key','delivery_token','result','expected_fingerprint'}
                or not isinstance(entry.get('step_key'), str) or not entry['step_key']
                or not isinstance(entry.get('delivery_token'), str) or not isinstance(entry.get('result'), dict)):
            raise BusinessError('context_batch_parameters', '每项需要 step_key、delivery_token 和 result。')
        if entry['step_key'] in seen:
            raise BusinessError('context_batch_parameters', '同一批次不能重复同一个 step_key。')
        seen.add(entry['step_key'])
    reused = 0
    remerge = False
    acknowledgement = None
    for index, entry in enumerate(entries or [{}]):
        current = ctx._operation(core, c, op['id'], writable=True)
        payload = dict(entry)
        if index == max(0, len(entries)-1):
            if 'summary' in p:
                payload['summary'] = p['summary']
            payload['yield'] = p.get('yield', False)
        acknowledgement = ctx.checkpoint(core, c, current, payload)
        reused += int(bool(entry) and acknowledgement['reused'])
        remerge |= acknowledgement['remerge_required']
    return {**acknowledgement, 'acknowledged': len(entries), 'reused_count': reused,
            'reused': bool(entries) and reused == len(entries), 'remerge_required': remerge,
            'atomic': True}


def query_page(core, c, op, p, byte_limit=None):
    collection = p.get('collection')
    view = p.get('view', 'results')
    if view not in {'results','facts'} or view != 'results' and collection != 'facts':
        raise BusinessError('context_filter', 'view=facts 仅用于候选事实集合。')
    if collection == 'actions':
        from .context_candidates import page
        ctx._budget(op, byte_limit)
        result = page(core, {'operation_id': op['id'], 'after': p.get('cursor') or 0})
        ctx._fit_page(op, ctx.size(result), ctx._budget(op, byte_limit))
        return ctx._charge(c, op, result)
    if collection == 'facts':
        if (p.get('filters') or {}).get('job_id'):
            from .context_history import facts
            return facts(core, c, op, p['filters']['job_id'], p.get('cursor'), byte_limit=byte_limit)
        from .materials import facts_page
        return facts_page(core, c, op, p.get('cursor'), byte_limit, view=view)
    if collection == 'history':
        return ctx.history_page(core, c, op, p.get('cursor'), byte_limit)
    return ctx.page(core, c, op, collection, p.get('filters'), p.get('cursor'), p.get('limit',80), byte_limit)


def read_context_batch(core, c, op, p):
    requests = p.get('requests')
    if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_ITEMS or ctx.size(p) > MAX_BYTES:
        raise BusinessError('context_batch_parameters', '查询批次需要 1–32 项，总输入不超过 64 KiB。')
    start = integer(p.get('start',0), 'start', 0, len(requests)-1)
    for request in requests:
        if not isinstance(request, dict) or ('id' in request) == ('collection' in request):
            raise BusinessError('context_batch_parameters', '每项只选 id 详情或 collection 集合。')
        allowed = {'id','offset','version'} if 'id' in request else {'collection','filters','cursor','limit','view'}
        if set(request)-allowed:
            raise BusinessError('context_batch_parameters', '批量入口只接受受控的详情与集合查询参数。')
        if 'id' in request:
            if not isinstance(request['id'], str) or not request['id']:
                raise BusinessError('context_batch_parameters', '详情编号必须是非空字符串。')
            integer(request.get('offset',0), 'offset', 0, 2**53-1)
            if request.get('offset') and request.get('version') is None:
                raise BusinessError('context_batch_parameters', '分段续读必须保留原版本。')
    requested=integer(p.get('byte_limit', DEFAULT_BYTES), 'byte_limit', 4096, MAX_BYTES)
    maximum = budget(op, requested)
    items = []
    charged = 0
    following = None
    for index in range(start, len(requests)):
        remaining = maximum-ctx.size({'items': items})-1024
        if remaining < 2048:
            if not items:
                if op['job_id']:raise ctx._checkpoint_required(op)
                raise BusinessError('context_batch_limit','本批容量不足以容纳一项查询，请增大 byte_limit。')
            following = index
            break
        current = ctx._operation(core, c, op['id'])
        request = requests[index]
        c.execute('SAVEPOINT context_batch_item')
        try:
            if 'id' in request:
                value = ctx.read_item(core, c, current, request['id'], request.get('offset',0), request.get('version'), min(remaining,ctx.MAX_PAGE_BYTES))
            else:
                value = query_page(core, c, current, request, min(remaining,ctx.MAX_PAGE_BYTES))
            if ctx.size({'items': items+[{'index': index, 'result': value}]})+512 > maximum:
                raise BusinessError('context_batch_limit', '本批容量不足，未跳过查询。')
        except BusinessError as error:
            c.execute('ROLLBACK TO context_batch_item')
            c.execute('RELEASE context_batch_item')
            if items and error.code in {'context_checkpoint_required','context_page_limit','context_batch_limit'}:
                following = index
                break
            raise
        else:
            c.execute('RELEASE context_batch_item')
        items.append({'index': index, 'result': value})
        charged += ctx.size(value)
    result = {'operation_id': op['id'], 'items': items, 'next_request_index': following,
              'requests_complete': following is None, 'business_revision': core.store.meta(c,'revision'),
              'instruction': '每项保留自己的 next_cursor/next_offset 和版本；requests_complete 仅指本次请求列表，不能代表每项全文已读。'}
    ctx._fit_page(op, ctx.size(result), maximum)
    # Inner readers already accounted for their data; charge only the envelope.
    c.execute('UPDATE context_operations SET delivered_bytes=delivered_bytes+? WHERE id=?', (max(0,ctx.size(result)-charged),op['id']))
    return result
