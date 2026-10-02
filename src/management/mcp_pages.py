"""Stateless, byte-bounded transport for ordinary business read views.

This does not change a query into a different context collection. Every fragment
comes from the exact original query, including its defaults, explicit nulls,
filters and business-page offsets. A new MCP process can continue by replaying
only that read; any result, query or epoch change invalidates its fingerprint.
Caller routing/authorization must be checked by the MCP tool before every read.
"""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Literal

from .context_service import MAX_PAGE_BYTES, size
from .schemas import BusinessError
from .storage import encode


PageQuery = Literal['get', 'receipt', 'list', 'source_content', 'task_pool', 'actual_feedback', 'preview_task_batch']
PAGE_QUERIES = frozenset({'get', 'receipt', 'list', 'source_content', 'task_pool', 'actual_feedback', 'preview_task_batch'})
FORMAT = 'business-query-json/1'


def validate_request(name, params, offset, version):
    if name not in PAGE_QUERIES or not isinstance(params, dict):
        raise BusinessError('business_page_query', '此续读入口支持对象、回执、记录列表、资料正文、任务池、实际反馈和批量任务预览。')
    if type(offset) is not int or offset < 0:
        raise BusinessError('business_page_cursor', '片段位置必须是返回的非负整数。')
    if version is not None and (not isinstance(version, str) or re.fullmatch(r'[0-9a-f]{64}', version) is None):
        raise BusinessError('business_page_version', '读取版本无效，请使用首片返回的完整版本。')
    if offset and version is None:
        raise BusinessError('business_page_version', '续读必须携带首片返回的版本，不能拼接未经核对的内容。')


def page(name, params, value, *, offset=0, version=None):
    """Return an exactly reconstructible original response within MAX_PAGE_BYTES.

    Fragment offsets count Python/JSON text characters, not bytes or business
    rows. Only after all fragments are joined and parsed should the caller use
    the original response's next_offset to request another business page.
    No cache, snapshot table, context operation or business write is created.
    """
    validate_request(name, params, offset, version)
    text = encode(value)
    fingerprint = hashlib.sha256(encode({'query': name, 'params': params, 'result': value}).encode('utf-8')).hexdigest()
    if version is not None and version != fingerprint:
        raise BusinessError('business_page_changed', '原查询、结果或数据空间已变化，请重新读取首片；不能继续拼接旧结果。')
    if offset > len(text):
        raise BusinessError('business_page_cursor', '片段位置超出原结果，请使用返回的续读位置。')
    original_params = copy.deepcopy(params)
    header = {'format': FORMAT, 'query': name, 'query_params': original_params,
              'version': fingerprint, 'fragment_offset': offset}
    for key in ('epoch', 'revision'):
        if key in value:
            header[key] = value[key]
    if name == 'receipt':
        header.update(found=value['found'], request_id=params['request_id'])
        if value['found']:
            header['receipt_metadata'] = {key: value['receipt'][key] for key in ('request_id', 'epoch', 'revision')}
    elif name == 'source_content':
        header['entity_id'] = value['entity_id']
    elif name == 'get':
        header['entity_id'] = value['entity']['id']
    elif name == 'list':
        header['total'] = value['total']

    def envelope(end, complete):
        return {**header, 'json_fragment': text[offset:end], 'complete': complete,
                'next_fragment_offset': None if complete else end,
                'continuation': None if complete else {'tool': 'read_business_page', 'arguments': {
                    'name': name, 'params': original_params, 'offset': end, 'version': fingerprint}}}

    # Count the whole escaped JSON envelope, including both query identity and
    # a directly callable continuation. Reserve the larger nonterminal form.
    lo, hi = 0, min(len(text) - offset, MAX_PAGE_BYTES)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if size(envelope(offset + mid, False)) <= MAX_PAGE_BYTES:
            lo = mid
        else:
            hi = mid - 1
    if not lo and offset < len(text):
        raise BusinessError('business_page_budget', '原查询参数过长，无法在单页中保留可续读身份；请缩短筛选条件。')
    end = offset + lo
    result = envelope(end, end == len(text))
    if size(result) > MAX_PAGE_BYTES:
        raise BusinessError('business_page_budget', '原查询参数超过单页预算，请缩短筛选条件。')
    return result
