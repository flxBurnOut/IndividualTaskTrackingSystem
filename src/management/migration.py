"""Offline, atomic import of a reviewed snapshot into a fresh data space.

This is deliberately not an HTTP command. It cannot replace or clear a live
database. Historical plans are imported as evidence, without applying today's
planning constraints retroactively. File copies must already be verified.
"""
from __future__ import annotations
import copy
import hashlib
from pathlib import Path

from .core import Core, date_value
from .schemas import BusinessError, validate_dimensions
from .storage import encode


def apply_snapshot(data_dir, snapshot):
    root = Path(data_dir).resolve()
    if (root / 'runtime.json').exists() or (root / 'service.lock').exists():
        raise BusinessError('migration_live', '迁移只允许写入独立的离线数据空间。')
    if snapshot.get('format') != 'management-migration/1':
        raise BusinessError('migration_format', '迁移包格式无效。')
    records = snapshot.get('records')
    if not isinstance(records, list) or len(records) > 20000:
        raise BusinessError('migration_limit', '迁移记录超限。')
    keys = [r['key'] for r in records]
    if len(set(keys)) != len(keys):
        raise BusinessError('migration_duplicate', '迁移来源键重复。')
    fingerprint = hashlib.sha256(encode(snapshot).encode('utf-8')).hexdigest()
    core = Core(root)
    with core.store.connect() as c:
        previous = core.store.meta(c, 'migration')
        if previous:
            if previous['fingerprint'] == fingerprint:
                return {**previous, 'replayed': True}
            raise BusinessError('migration_conflict', '此空间已经导入过不同快照。')
        if c.execute('SELECT count(*) FROM entities').fetchone()[0] or c.execute('SELECT count(*) FROM jobs').fetchone()[0]:
            raise BusinessError('migration_not_empty', '迁移目标必须为空；现有数据未改动。')
    # Check each physical object once, streaming outside the transaction.
    checked = set()
    for record in records:
        if record['type'] == 'asset':
            data = record['data']
            for item in [data, *data.get('entries', [])]:
                digest = item.get('sha256')
                if digest and digest not in checked:
                    path = core.resources._blob(digest)
                    if path.stat().st_size != item['size']:
                        raise BusinessError('migration_asset', '迁移附件大小不一致。')
                    checked.add(digest)
    ids = {}
    def resolve(value):
        if isinstance(value, dict):
            if set(value) == {'$ref'}:
                try: return ids[value['$ref']]
                except KeyError as exc:
                    raise BusinessError('migration_reference', '未创建的来源引用：'+value['$ref']) from exc
            return {k: resolve(v) for k,v in value.items()}
        if isinstance(value, list): return [resolve(v) for v in value]
        return value
    with core.store.lock, core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            if c.execute('SELECT count(*) FROM entities').fetchone()[0]:
                raise BusinessError('migration_not_empty', '迁移期间目标发生变化。')
            settings = copy.deepcopy(snapshot.get('settings') or core.store.meta(c,'settings'))
            core._dispatch(c, 'settings', {'settings': settings}, fingerprint)
            for record in records:
                data = resolve(record.get('data', {}))
                if record['type'] == 'feedback':
                    date_value(data['business_date']); validate_dimensions(data['dimensions'])
                    if not data.get('source_text'):
                        raise BusinessError('migration_feedback', '历史反馈必须保留来源。')
                    core.store.get(c, data['target_id'])
                if record['type'] == 'plan':
                    if data.get('historical_import') is not True:
                        raise BusinessError('migration_plan', '历史计划需要显式标记。')
                    date_value(data['date'])
                    targets = set()
                    for block in data['blocks']:
                        core.store.get(c, block['target_id'])
                        if block['target_id'] in targets:
                            raise BusinessError('migration_plan', '历史计划条目重复。')
                        targets.add(block['target_id'])
                entity = core._create(c, {'type':record['type'], 'title':record['title'],
                    'parent_id':resolve(record.get('parent_id')), 'status':record.get('status','active'),
                    'data':data}, fingerprint)
                ids[record['key']] = entity['id']
                if record.get('archived'):
                    core._save(c, entity, {**entity,'archived':True}, fingerprint, 'historical_archive')
            for link in snapshot.get('links', []):
                core._dispatch(c, 'link', resolve(link), fingerprint)
            for recovery in snapshot.get('recovery', []):
                core._dispatch(c, 'set_recovery_task', resolve(recovery), fingerprint)
            report = {'fingerprint':fingerprint, 'source':snapshot.get('source'), 'record_count':len(ids),
                      'asset_objects_verified':len(checked), 'ids':ids, 'replayed':False}
            core.store.set_meta(c,'migration',report)
            core.store.set_meta(c,'revision',1)
            if c.execute('PRAGMA foreign_key_check').fetchone():
                raise BusinessError('migration_reference', '迁移后的关系校验失败。')
            c.commit()
        except BaseException:
            c.rollback()
            raise
    return report
