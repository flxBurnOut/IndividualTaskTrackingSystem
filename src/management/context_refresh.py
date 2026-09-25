"""Refreshing evidence invalidates only changed material batches, within one operation."""
import json
from .storage import encode,now
from .schemas import BusinessError


def refresh(core,c,op,include_new_sources=False):
    from .materials import register
    if op['phase']=='ready':raise BusinessError('context_finished','候选已封存；请在原事项补充消息后生成新的候选。')
    if include_new_sources and op['scope'].get('entity_id'):
        owner=op['scope']['entity_id']
        c.execute("""INSERT OR IGNORE INTO context_sources(operation_id,entity_id,version)
            SELECT ?,e.id,e.version FROM entities e WHERE e.type IN ('asset','artifact') AND e.archived=0
            AND (json_extract(e.data,'$.source_owner_id')=? OR e.parent_id=? OR
              EXISTS(SELECT 1 FROM links l WHERE l.source_id=? AND l.target_id=e.id))""",(op['id'],owner,owner,owner))
    new_sources=c.execute('SELECT count(*) FROM context_sources WHERE operation_id=?',(op['id'],)).fetchone()[0]
    op['scope']['_timezone']=core.store.meta(c,'settings')['timezone']
    c.execute('UPDATE context_operations SET scope=? WHERE id=?',(encode(op['scope']),op['id']))
    changed=0
    for row in c.execute("""SELECT s.*,e.data,e.version current_version,e.archived FROM context_sources s
        LEFT JOIN entities e ON e.id=s.entity_id WHERE s.operation_id=?""",(op['id'],)).fetchall():
        if row['version']==row['current_version'] and not row['archived']:continue
        changed+=1;data=json.loads(row['data'] or '{}')
        key=register(c,data['sha256'],data.get('original_name','source.bin'),'timetable' if op['scope'].get('kind')=='timetable' else None) if data.get('sha256') else None
        if key!=row['material_key'] or row['archived'] or row['current_version'] is None:
            for step in c.execute('SELECT * FROM context_steps WHERE operation_id=? AND source_id=?',(op['id'],row['entity_id'])).fetchall():
                c.execute('INSERT INTO context_step_history(operation_id,step_key,snapshot,created_at) VALUES (?,?,?,?)',
                    (op['id'],step['step_key'],encode(dict(step)),now()))
            c.execute('DELETE FROM context_steps WHERE operation_id=? AND source_id=?',(op['id'],row['entity_id']))
        else:
            c.execute('UPDATE context_steps SET source_version=? WHERE operation_id=? AND source_id=?',(row['current_version'],op['id'],row['entity_id']))
        if row['archived'] or row['current_version'] is None:
            c.execute('DELETE FROM context_sources WHERE operation_id=? AND entity_id=?',(op['id'],row['entity_id']))
        else:
            c.execute('UPDATE context_sources SET version=?,material_key=?,indexed_chunks=0 WHERE operation_id=? AND entity_id=?',
                (row['current_version'],key,op['id'],row['entity_id']))
    if op['job_id']:
        job=core._job(c,op['job_id']);value=json.loads(job['input'])
        if value.get('source_versions'):
            versions={r['entity_id']:r['version'] for r in c.execute('SELECT entity_id,version FROM context_sources WHERE operation_id=?',(op['id'],))}
            value['source_versions']={identifier:versions[identifier] for identifier in value['source_versions'] if identifier in versions}
            value['source_ids']=[identifier for identifier in value.get('source_ids',[]) if identifier in versions]
            c.execute('UPDATE jobs SET input=? WHERE id=?',(encode(value),op['job_id']))
    # Preserve superseded draft actions for audit, but do not silently reapply
    # conclusions built from evidence that the model asked to refresh.
    for row in c.execute('SELECT * FROM context_actions WHERE operation_id=?',(op['id'],)).fetchall():
        c.execute('INSERT INTO context_step_history(operation_id,step_key,snapshot,created_at) VALUES (?,?,?,?)',
            (op['id'],'candidate:'+str(row['seq']),encode(dict(row)),now()))
    c.execute('DELETE FROM context_actions WHERE operation_id=?',(op['id'],))
    c.execute('DELETE FROM context_queries WHERE operation_id=?',(op['id'],))
    c.execute('DELETE FROM context_reads WHERE operation_id=?',(op['id'],))
    c.execute('DELETE FROM context_parts WHERE operation_id=?',(op['id'],))
    c.execute("UPDATE context_operations SET validation=NULL,phase='reading',updated_at=? WHERE id=?",(now(),op['id']))
    return {'operation_id':op['id'],'changed_sources':changed,'old_candidates_preserved_in_history':True,
            'instruction':'仍是同一操作。重新读取相关约束和 facts；未变更资料的处理结果已保留。请重新形成候选。'}
