"""Read-only, bounded model discovery from the configured local Codex runtime.

The model/list request and response fields are checked against the installed
0.155.0-alpha.9.2 schema. Discovery never opens a thread or requests inference.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
import tempfile
import threading

from . import ai

PAGE_SIZE = 50
MAX_PAGES = 10
MAX_MODELS = 250
TIMEOUT_SECONDS = 15
_DISCOVERY_LOCK = threading.Lock()


def _model(row):
    if not isinstance(row, dict):
        raise ai.AIError('AI_MODEL_CATALOG_INVALID', 'Codex 返回的模型列表格式无效。')
    value = {}
    for remote, local, max_length in [('id', 'id', 300), ('model', 'model', 300), ('displayName', 'display_name', 300)]:
        text = row.get(remote)
        if not isinstance(text, str) or not text.strip() or len(text) > max_length:
            raise ai.AIError('AI_MODEL_CATALOG_INVALID', 'Codex 返回的模型名称或标识无效。')
        value[local] = text
    description = row.get('description', '')
    modalities = row.get('inputModalities', ['text', 'image'])
    if not isinstance(description, str) or len(description) > 4000 or not isinstance(modalities, list) or len(modalities) > 20 or any(not isinstance(x, str) or len(x) > 100 for x in modalities):
        raise ai.AIError('AI_MODEL_CATALOG_INVALID', 'Codex 返回的模型说明格式无效。')
    value.update(description=description, is_default=row.get('isDefault') is True, input_modalities=modalities)
    return value


def list_models(settings: dict, cancel: threading.Event | None = None) -> dict:
    """Return the complete visible catalog or raise; never return a partial list.

    ai.enabled is intentionally irrelevant: discovery is needed before enabling
    assistance. The caller may override executable in an in-memory settings copy.
    No model is selected, no settings are written, and no quota endpoint is used.
    """
    config = settings.get('ai', settings)
    if not isinstance(config, dict):
        raise ai.AIError('AI_CONFIG_INVALID', 'Codex 设置格式无效。')
    cancel = cancel or threading.Event()
    if cancel.is_set():
        raise ai.AIError('AI_CANCELLED', '模型列表读取已取消。')
    executable = ai.find_codex(config.get('executable'))
    if not executable:
        raise ai.AIError('AI_RUNTIME_MISSING', '未找到 Codex 程序，请选择本机 Codex 程序后刷新。')
    if not _DISCOVERY_LOCK.acquire(blocking=False):
        raise ai.AIError('AI_MODEL_CATALOG_BUSY', '另一次模型列表读取尚未结束，请稍后刷新。')
    try:
        with tempfile.TemporaryDirectory(prefix='personal-management-models-') as workspace:
            rpc = ai._AppServer(executable, Path(workspace), cancel, TIMEOUT_SECONDS)
            try:
                rpc.request('initialize', {'clientInfo': {'name': 'personal_management_model_picker', 'version': '0.7.0'}, 'capabilities': {'experimentalApi': True}})
                rpc.send({'method': 'initialized', 'params': {}})
                models, seen_models, seen_cursors = [], set(), set()
                cursor = None
                for page in range(MAX_PAGES):
                    params = {'limit': PAGE_SIZE, 'includeHidden': False}
                    if cursor is not None:
                        params['cursor'] = cursor
                    response = rpc.request('model/list', params)
                    rows = response.get('data') if isinstance(response, dict) else None
                    if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                        raise ai.AIError('AI_MODEL_CATALOG_INVALID', 'Codex 返回的模型分页无效。')
                    for row in rows:
                        if isinstance(row, dict) and row.get('hidden') is True:
                            continue
                        item = _model(row)
                        if item['model'] in seen_models:
                            continue
                        seen_models.add(item['model'])
                        models.append(item)
                        if len(models) > MAX_MODELS:
                            raise ai.AIError('AI_MODEL_CATALOG_LIMIT', '模型列表超出读取上限，原选择已保留。')
                    cursor = response.get('nextCursor')
                    if cursor is None:
                        return {'models': models, 'recommended_model': next((m['model'] for m in models if m['is_default']), None),
                            'resolved_executable': str(executable), 'source': 'codex_app_server_model_list',
                            'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}
                    if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in seen_cursors:
                        raise ai.AIError('AI_MODEL_CATALOG_INVALID', '模型分页重复或无效，原选择已保留。')
                    seen_cursors.add(cursor)
                raise ai.AIError('AI_MODEL_CATALOG_LIMIT', '模型分页超出读取上限，原选择已保留。')
            finally:
                rpc.close()
    finally:
        _DISCOVERY_LOCK.release()
