"""Transaction-local fences for trusted native discussion callbacks.

The bound context is an internal call scope, never a model-supplied parameter.
Every context transaction rechecks the captured job generation and actual turn.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json

from .schemas import BusinessError


@dataclass(frozen=True)
class _BoundContext:
    operation_id: str
    job_id: str
    generation: int
    payload: dict


_BOUND = ContextVar('management_native_context_identity', default=None)


def check_turn(core, c, job, payload, *, active=True, generation=None):
    """Require an exact durable native identity; explicit legacy stays legacy."""
    value = json.loads(job['input']) if isinstance(job['input'], str) else job['input']
    if value.get('desktop_transport') != 'native_ipc_v1' and not payload.get('_native_provider_thread_id'):
        return False
    from .session_coordinator import _check_native_caller, _require_native_turn
    _check_native_caller(core, c, payload)
    _require_native_turn(payload)
    if job['epoch'] != payload.get('epoch') or job['epoch'] != core.store.meta(c, 'epoch'):
        raise BusinessError('epoch_mismatch', '数据空间身份已变化，旧回合不能继续。')
    generation = job['generation'] if generation is None else generation
    bound = c.execute('''SELECT conversation_id,provider_thread_id,provider_turn_id,provider_generation
        FROM conversation_operations WHERE job_id=? AND epoch=?''', (job['id'], job['epoch'])).fetchone()
    if (not bound or bound['conversation_id'] != payload.get('conversation_id')
            or bound['provider_thread_id'] != payload['_native_provider_thread_id']
            or bound['provider_turn_id'] != payload['_native_provider_turn_id']
            or bound['provider_generation'] != generation):
        raise BusinessError('discussion_turn_stale', '调用回合或处理代次与当前操作不一致，旧回调未写入。')
    if active:
        if job['status'] not in {'queued', 'running'}:
            raise BusinessError('stale_proposal', '这轮已经结束或被取消，旧回调未写入。')
        conversation_id = value.get('conversation_id')
        if conversation_id:
            current = c.execute('SELECT active_job_id FROM conversations WHERE id=?', (conversation_id,)).fetchone()
            if not current or current['active_job_id'] != job['id']:
                raise BusinessError('stale_proposal', '这段讨论已有其他操作，旧回调未写入。')
    return True


@contextmanager
def bound_context(core, payload, operation_id):
    with core.store.connect() as c:
        row = c.execute('SELECT job_id FROM context_operations WHERE id=?', (operation_id,)).fetchone()
        if not row or not row['job_id']:
            raise BusinessError('context_scope', '当前讨论的作业上下文无效。')
        job = core._job(c, row['job_id'])
        native = check_turn(core, c, job, payload)
        identity = _BoundContext(operation_id, job['id'], job['generation'], {
            key: payload.get(key) for key in ('conversation_id', 'epoch',
                '_native_provider_thread_id', '_native_provider_turn_id')}) if native else None
    token = _BOUND.set(identity)
    try:
        yield
    finally:
        _BOUND.reset(token)


def check_context(core, c, operation):
    """Called by _operation inside the transaction that reads/writes its state."""
    identity = _BOUND.get()
    if identity is None:
        return
    if operation['id'] != identity.operation_id or operation['job_id'] != identity.job_id:
        raise BusinessError('context_scope', '上下文不属于当前真实回合。')
    job = core._job(c, identity.job_id)
    if job['generation'] != identity.generation:
        raise BusinessError('discussion_turn_stale', '处理代次已改变，旧回调未写入。')
    check_turn(core, c, job, identity.payload)
