"""Content-addressed extraction and durable material-analysis steps."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from .schemas import BusinessError
from .storage import encode,now

EXTRACTOR='material/1'
TERMINAL={'complete','partial','unsupported','failed','paused'}


def material_key(sha,name,layout=None):
    return hashlib.sha256(encode([EXTRACTOR,sha,Path(name).suffix.lower(),layout]).encode()).hexdigest()


def register(c,sha,name,layout=None):
    key=material_key(sha,name,layout)
    c.execute("""INSERT OR IGNORE INTO material_catalog(key,sha256,name,layout,updated_at)
        VALUES (?,?,?,?,?)""",(key,sha,name,layout,now()))
    return key


def _lock(core):
    with core.store.lock:
        if not hasattr(core,'_material_lock'):core._material_lock=threading.Lock()
        return core._material_lock


def extract_batch(core,key):
    """Only one disposable reader at a time; no DB locks across file I/O."""
    with _lock(core):
        with core.store.connect() as c:
            row=c.execute('SELECT * FROM material_catalog WHERE key=?',(key,)).fetchone()
            if not row: raise BusinessError('material_missing','资料提取索引不存在。')
            state=dict(row)
        if state['status'] in TERMINAL:return state
        try:
            blob=core.resources._blob(state['sha256'],verify=False)
            from .resources import _digest
            stat=blob.stat();signature=[stat.st_size,stat.st_mtime_ns]
            prior=json.loads(state['coverage'])
            if prior.get('original_signature')!=signature:
                if _digest(blob)[0]!=state['sha256']:raise BusinessError('source_integrity','原件哈希不符，已暂停读取。')
                prior['original_signature']=signature
                state['coverage']=encode(prior)
            with tempfile.TemporaryDirectory(prefix='material-',dir=core.resources.root/'.staging') as folder:
                root=Path(folder)
                request={'batch':True,'source_path':str(blob),'original_name':state['name'],
                         'layout_mode':state['layout'],'cursor':json.loads(state['cursor']),'batch_units':json.loads(state['coverage']).get('batch_units',40)}
                (root/'input.json').write_text(encode(request),encoding='utf-8')
                args=[sys.executable,'-m','management.source_worker',str(root)]
                if getattr(sys,'frozen',False):args=[str(Path(sys.executable).with_name('PersonalManagementService.exe')),'--source-worker',str(root)]
                env=dict(os.environ,QT_QPA_PLATFORM='offscreen',PYTHONUTF8='1')
                import psutil
                with core.resources._reservation(64*1024*1024):
                    process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                        env=env,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    deadline=time.monotonic()+45
                    try:
                        while process.poll() is None:
                            if time.monotonic()>deadline:raise BusinessError('source_timeout','当前批读取超时，原件和此前检查点已保留。')
                            try:
                                parent=psutil.Process(process.pid);members=[parent,*parent.children(recursive=True)]
                                if sum(getattr(p.memory_info(),'private',p.memory_info().rss) for p in members if p.is_running())>768*1024*1024:
                                    raise BusinessError('source_memory','当前批达到内存保护值，已保留原件和此前进度。')
                            except psutil.NoSuchProcess:pass
                            time.sleep(.03)
                        result_path=root/'result.json'
                        if not result_path.is_file() or result_path.stat().st_size>128*1024:
                            raise BusinessError('source_parse','读取器未返回有效的批次清单。')
                        result=json.loads(result_path.read_text('utf-8'))
                        if not result.get('ok'):raise BusinessError('source_parse','；'.join(result.get('warnings',[]))[:500])
                    finally:
                        if process.poll() is None:
                            try:
                                parent=psutil.Process(process.pid)
                                for child in parent.children(recursive=True):child.kill()
                                parent.kill();process.wait(timeout=5)
                            except psutil.NoSuchProcess:pass
                entries=[]
                for item in result.get('chunks',[]):
                    relative=Path(item['path'])
                    if str(relative)!=relative.name:raise BusinessError('source_parse','读取器返回无效内容路径。')
                    maximum=12*1024*1024 if item['kind']=='image' else 32000
                    meta=core.resources.import_file(root/relative,max_bytes=maximum,
                        media_type='image/png' if item['kind']=='image' else 'text/plain')
                    entries.append({**meta,'kind':item['kind'],'locator':item['locator'][:600]})
            with core.store.lock,core.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                current=c.execute('SELECT * FROM material_catalog WHERE key=?',(key,)).fetchone()
                if current['cursor']!=state['cursor']:c.rollback();return dict(current)
                first=state['chunks']
                c.executemany('INSERT INTO material_chunks VALUES (?,?,?,?,?,?)',
                    [(key,first+i,item['kind'],item['locator'],item['sha256'],item['size']) for i,item in enumerate(entries)])
                coverage=json.loads(state['coverage'])
                coverage['unsupported_units']=coverage.get('unsupported_units',0)+result['coverage'].get('unsupported_units',0)
                coverage['images_extracted']=coverage.get('images_extracted',0)+result['coverage'].get('images_extracted',0)
                coverage['layout_mode']=state['layout']
                coverage['text_characters']=coverage.get('text_characters',0)+result.get('characters',0)
                coverage['text_complete']=bool(result['finished'])
                coverage['complete']=bool(result['finished'] and not coverage['unsupported_units'])
                status=('partial' if coverage['unsupported_units'] else 'complete') if result['finished'] else 'pending'
                if status=='partial' and not c.execute("SELECT 1 FROM material_chunks WHERE material_key=? AND kind!='gap' LIMIT 1",(key,)).fetchone():status='unsupported'
                c.execute("""UPDATE material_catalog SET cursor=?,status=?,units_done=units_done+?,
                    chunks=chunks+?,coverage=?,error=NULL,updated_at=? WHERE key=?""",
                    (encode(result['cursor']),status,result['units_done'],len(entries),encode(coverage),now(),key))
                c.commit()
        except Exception as error:
            failure={'code':getattr(error,'code','source_parse'),'message':str(error)[:600]}
            with core.store.lock,core.store.connect() as c:
                limited=failure['code'] in {'source_memory','source_timeout','DISK_RESERVE'}
                coverage=json.loads(state['coverage']);coverage['complete']=False;coverage['text_complete']=False;units=coverage.get('batch_units',40)
                if limited and units>1 and failure['code']!='DISK_RESERVE':
                    coverage['batch_units']=max(1,units//2);status='pending'
                else:status='paused' if limited else 'failed'
                c.execute("UPDATE material_catalog SET status=?,error=?,coverage=?,updated_at=? WHERE key=?",
                    (status,encode(failure),encode(coverage),now(),key))
        with core.store.connect() as c:return dict(c.execute('SELECT * FROM material_catalog WHERE key=?',(key,)).fetchone())


def extraction_summary(core,key):
    with core.store.connect() as c:
        row=c.execute('SELECT * FROM material_catalog WHERE key=?',(key,)).fetchone()
        chunks=[dict(r) for r in c.execute('SELECT * FROM material_chunks WHERE material_key=? ORDER BY seq LIMIT 40',(key,))]
    state=dict(row);coverage=json.loads(state['coverage'])
    text_chunks=[r for r in chunks if r['kind']=='text'];images=[r for r in chunks if r['kind']=='image']
    warnings=[]
    if state['status']=='pending':warnings.append('原件已保存，其余内容将自动分批读取；当前预览不是全文。')
    if coverage.get('unsupported_units'):warnings.append('部分嵌入对象暂不支持，原件和具体缺口已保留。')
    if state['error']:warnings.append(json.loads(state['error'])['message'])
    return {'material_key':key,'extractor':EXTRACTOR,'status':('readable' if not images else 'vision_ready') if state['status']=='complete' else 'partial' if state['status']=='pending' else state['status'],
        'coverage':{**coverage,'units_read':state['units_done'],'chunks_extracted':state['chunks'],'extraction_status':state['status'],'analysis_complete':False},
        'text_sha256':text_chunks[0]['sha256'] if text_chunks else None,
        'images':[{'sha256':r['sha256'],'size':r['size'],'label':r['locator']} for r in images[:4]],
        'characters':coverage.get('text_characters',0),'warnings':warnings}


def prepare_extraction(core,metadata,name,*,layout=None):
    with core.store.lock,core.store.connect() as c:key=register(c,metadata['sha256'],name,layout)
    extract_batch(core,key)
    return extraction_summary(core,key),[]


def bind_sources(core,c,op,source_id=None):
    for row in c.execute("""SELECT s.entity_id,e.title,e.data,s.material_key FROM context_sources s
        JOIN entities e ON e.id=s.entity_id WHERE s.operation_id=? AND s.material_key IS NULL AND (? IS NULL OR s.entity_id=?) LIMIT 32""",(op['id'],source_id,source_id)):
        data=json.loads(row['data']);sha=data.get('sha256')
        if not sha:
            c.execute('INSERT OR IGNORE INTO context_steps(operation_id,step_key,source_id,source_version,chunk_key,updated_at) VALUES (?,?,?,?,?,?)',
                (op['id'],row['entity_id']+':extraction-gap',row['entity_id'],c.execute('SELECT version FROM context_sources WHERE operation_id=? AND entity_id=?',(op['id'],row['entity_id'])).fetchone()[0],'gap',now()))
            # A sentinel has no catalog row and is explicitly reported as unavailable.
            c.execute("UPDATE context_sources SET material_key='unavailable' WHERE operation_id=? AND entity_id=?",(op['id'],row['entity_id']))
            continue
        name=data.get('original_name') or row['title']
        layout='timetable' if op['scope'].get('kind')=='timetable' else None
        key=register(c,sha,name,layout)
        c.execute('UPDATE context_sources SET material_key=? WHERE operation_id=? AND entity_id=?',(key,op['id'],row['entity_id']))


def sync_steps(c,op):
    for source in c.execute('''SELECT s.*,m.chunks FROM context_sources s JOIN material_catalog m ON m.key=s.material_key
        WHERE s.operation_id=? AND s.indexed_chunks<m.chunks''',(op['id'],)).fetchall():
        c.execute('''INSERT OR IGNORE INTO context_steps(operation_id,step_key,source_id,source_version,chunk_key,updated_at)
            SELECT ?,?||':'||m.seq,?,?,m.material_key||':'||m.seq,? FROM material_chunks m
            WHERE m.material_key=? AND m.seq>=?''',
            (op['id'],source['entity_id'],source['entity_id'],source['version'],now(),source['material_key'],source['indexed_chunks']))
        c.execute('UPDATE context_sources SET indexed_chunks=? WHERE operation_id=? AND entity_id=?',(source['chunks'],op['id'],source['entity_id']))
    for row in c.execute("""SELECT s.*,m.status,m.error FROM context_sources s LEFT JOIN material_catalog m ON m.key=s.material_key
        WHERE s.operation_id=? AND (m.status IN ('failed'))""",(op['id'],)):
        key=row['entity_id']+':extraction-gap'
        c.execute("""INSERT OR IGNORE INTO context_steps(operation_id,step_key,source_id,source_version,chunk_key,updated_at)
            VALUES (?,?,?,?,?,?)""",(op['id'],key,row['entity_id'],row['version'],'gap',now()))


def incomplete_sources(core,c,op):
    bind_sources(core,c,op);sync_steps(c,op)
    return [r[0] for r in c.execute("""SELECT entity_id FROM context_sources s LEFT JOIN material_catalog m ON m.key=s.material_key
        WHERE s.operation_id=? AND (s.material_key IS NULL OR m.status IN ('pending','paused')) LIMIT 20""",(op['id'],))]


def _source_for_read(core,p):
    from .context_service import _operation
    with core.store.lock,core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE');op=_operation(core,c,p.get('operation_id'),writable=True)
        selected=p.get('source_id');owner=op['scope'].get('source_owner_scope')
        if owner:
            from .sources import scope_source_sql
            where=scope_source_sql();args=[op['id'],*([owner]*4)]
            if selected:where+=' AND e.id=?';args.append(selected)
            c.execute("INSERT OR IGNORE INTO context_sources(operation_id,entity_id,version) SELECT ?,e.id,e.version FROM entities e WHERE e.type IN ('asset','artifact') AND e.archived=0 AND "+where,args)
        bind_sources(core,c,op,selected)
        if selected:
            source=c.execute('SELECT * FROM context_sources WHERE operation_id=? AND entity_id=?',(op['id'],selected)).fetchone()
            if not source:raise BusinessError('source_scope','这个原件不在本轮所选范围。')
        else:
            source=c.execute("""SELECT s.* FROM context_sources s JOIN material_catalog m ON m.key=s.material_key
                WHERE s.operation_id=? AND m.status='pending' ORDER BY m.updated_at,s.entity_id LIMIT 1""",(op['id'],)).fetchone()
        c.commit();return dict(source) if source else None



def delivery_token(core,c,op,step):
    generation=core._job(c,op['job_id'])['generation'] if op['job_id'] else 0
    return hashlib.sha256(encode([op['id'],generation,step['source_id'],step['source_version'],step['chunk_key']]).encode()).hexdigest()


def next_step(core,p):
    from .context_service import _operation,_budget,_charge,coverage,size
    with core.store.connect() as c:
        op=_operation(core,c,p.get('operation_id'));_budget(op,None)
    source=_source_for_read(core,p)
    if source and source['material_key'] not in {None,'unavailable'}:extract_batch(core,source['material_key'])
    with core.store.lock,core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE');op=_operation(core,c,p['operation_id'],writable=True);sync_steps(c,op)
        row=c.execute("""SELECT * FROM context_steps WHERE operation_id=? AND status!='completed' AND (? IS NULL OR source_id=?)
            ORDER BY source_id,CASE WHEN chunk_key='gap' THEN 1 ELSE 0 END,CAST(substr(step_key,instr(step_key,':')+1) AS INTEGER) LIMIT 1""",(op['id'],p.get('source_id'),p.get('source_id'))).fetchone()
        if not row:
            paused=c.execute("SELECT m.error FROM context_sources s JOIN material_catalog m ON m.key=s.material_key WHERE s.operation_id=? AND m.status='paused' LIMIT 1",(op['id'],)).fetchone()
            if paused:raise BusinessError('material_paused','资料处理达到资源保护值，已保存进度；资源恢复后可点击从已保存进度继续。')
            incomplete=incomplete_sources(core,c,op)
            result={'all_steps_processed':not incomplete,'extraction_pending':bool(incomplete),'coverage':coverage(core,c,op),
                    'next':'query_context(collection=facts) to merge durable results, then validate_candidate'}
            result=_charge(c,op,result);c.commit();return result
        step=dict(row)
        if step['chunk_key']=='gap':
            error=c.execute("""SELECT m.error FROM context_sources s LEFT JOIN material_catalog m ON m.key=s.material_key
                WHERE s.operation_id=? AND s.entity_id=?""",(op['id'],step['source_id'])).fetchone()
            content={'kind':'gap','text':json.loads(error[0])['message'] if error and error[0] else '原件缺少可读快照，未假称已分析。',
                     'locator':'原件解析缺口'}
        else:
            key,seq=step['chunk_key'].rsplit(':',1)
            chunk=dict(c.execute('SELECT * FROM material_chunks WHERE material_key=? AND seq=?',(key,int(seq))).fetchone())
            content={'kind':chunk['kind'],'locator':chunk['locator'],'sha256':chunk['sha256']}
            if chunk['kind']=='image':
                # The MCP adapter returns a real ImageContent block; it never
                # gives the model a filesystem path and claims an image was read.
                content['image_ref']=step['chunk_key']
            else:content['text']=verified_bytes(core,chunk['sha256']).decode('utf-8')
        result={'step_key':step['step_key'],'delivery_token':delivery_token(core,c,op,step),'source_id':step['source_id'],'source_version':step['source_version'],
                'chunk':int(step['chunk_key'].rsplit(':',1)[1]) if step['chunk_key']!='gap' else None,'content':content,'instruction':'读取后 checkpoint_context(step_key,result={facts:[{key,value,evidence}],notes:...})；图片、缺口与文字分别记录。'}
        budget=_budget(op,None)
        if size(result)>budget-512:raise BusinessError('context_checkpoint_required','先保存检查点并结束本阶段，再继续读取该资料块。')
        # Reserve vision context conservatively, independent of compressed bytes.
        if content['kind']=='image':
            if op['delivered_bytes']+size(result)+12000>65536-8192:raise BusinessError('context_checkpoint_required','本阶段图片预算已用完，请保存检查点后接续。')
            c.execute('UPDATE context_operations SET delivered_bytes=delivered_bytes+12000 WHERE id=?',(op['id'],))
        c.execute("UPDATE context_steps SET status='delivered',updated_at=? WHERE operation_id=? AND step_key=?",(now(),op['id'],step['step_key']))
        result=_charge(c,op,result);c.commit();return result


def read_material(core,p):
    if p.get('chunk') is None:return next_step(core,p)
    from .context_service import _operation,_budget,_charge
    if type(p['chunk']) is not int or p['chunk']<0:raise BusinessError('material_cursor','内容块编号无效。')
    source=_source_for_read(core,p)
    if not source or source['material_key'] in {None,'unavailable'}:raise BusinessError('source_unavailable','原件没有可读取内容。')
    with core.store.connect() as c:
        present=c.execute('SELECT 1 FROM material_chunks WHERE material_key=? AND seq=?',(source['material_key'],p['chunk'])).fetchone()
    if not present:extract_batch(core,source['material_key'])
    with core.store.lock,core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE');op=_operation(core,c,p['operation_id']);budget=_budget(op,None);sync_steps(c,op)
        row=c.execute('SELECT * FROM material_chunks WHERE material_key=? AND seq=?',(source['material_key'],p['chunk'])).fetchone()
        if not row:
            state=c.execute('SELECT status FROM material_catalog WHERE key=?',(source['material_key'],)).fetchone()[0]
            result=_charge(c,op,{'source_id':source['entity_id'],'chunk':p['chunk'],'extraction_pending':state=='pending','status':state,'instruction':'提取仍在进行时，重试相同内容块；没有把未提取部分当作空内容。'})
            c.commit();return result
        chunk=dict(row);key=source['entity_id']+':'+str(p['chunk'])
        content={'kind':chunk['kind'],'locator':chunk['locator'],'sha256':chunk['sha256']}
        if chunk['kind']=='image':
            if op['delivered_bytes']+12000>65536-8192:raise BusinessError('context_checkpoint_required','图片预算需要先保存检查点后接续。')
            content['image_ref']=chunk['material_key']+':'+str(chunk['seq'])
            c.execute('UPDATE context_operations SET delivered_bytes=delivered_bytes+12000 WHERE id=?',(op['id'],))
        else:content['text']=verified_bytes(core,chunk['sha256']).decode('utf-8')
        step=c.execute('SELECT * FROM context_steps WHERE operation_id=? AND step_key=?',(op['id'],key)).fetchone()
        result={'step_key':key,'delivery_token':delivery_token(core,c,op,step),'checkpoint_fingerprint':step['fingerprint'],'source_id':source['entity_id'],'source_version':source['version'],'chunk':p['chunk'],
                'content':content,'next_chunk':p['chunk']+1}
        from .context_service import size
        if size(result)>budget:raise BusinessError('context_checkpoint_required','请保存检查点后重新读取当前块。')
        c.execute("UPDATE context_steps SET status='delivered' WHERE operation_id=? AND step_key=? AND status='pending'",(op['id'],key))
        result=_charge(c,op,result);c.commit();return result


def verified_bytes(core,sha):
    from .resources import ResourceError
    try:raw=core.resources._blob(sha,verify=False).read_bytes()
    except ResourceError as error:raise BusinessError('source_integrity',str(error)) from error
    if hashlib.sha256(raw).hexdigest()!=sha:raise BusinessError('source_integrity','资料内容与保存版本不一致，未将损坏缓存作为证据。')
    return raw


def image_bytes(core,operation_id,image_ref):
    from .context_service import _operation
    with core.store.connect() as c:
        op=_operation(core,c,operation_id)
        row=c.execute("""SELECT m.sha256 FROM context_steps s JOIN material_chunks m
            ON s.chunk_key=m.material_key||':'||m.seq
            WHERE s.operation_id=? AND s.chunk_key=? AND s.status IN ('delivered','completed') AND m.kind='image'""",(op['id'],image_ref)).fetchone()
        if not row:raise BusinessError('source_scope','未向当前操作提供这张图片。')
    return verified_bytes(core,row[0])


def facts_page(core,c,op,cursor=None,byte_limit=None):
    from .context_service import _budget,_charge,size,digest
    budget=_budget(op,byte_limit)
    count=c.execute("SELECT count(*) FROM context_steps WHERE operation_id=? AND status='completed'",(op['id'],)).fetchone()[0]
    stamp=str(count)
    query=c.execute("SELECT * FROM context_queries WHERE operation_id=? AND collection='facts' AND params='{}'",(op['id'],)).fetchone()
    if query and query['stamp']!=stamp:
        if cursor:raise BusinessError('context_changed','分析结果有新批次，请从 facts 第一页重新合并。')
        c.execute('DELETE FROM context_queries WHERE id=?',(query['id'],));query=None
    if not query:
        from .storage import new_id
        identifier=new_id()
        c.execute("INSERT INTO context_queries(id,operation_id,collection,params,stamp,total) VALUES (?,?,'facts','{}',?,?)",(identifier,op['id'],stamp,count))
        query=c.execute('SELECT * FROM context_queries WHERE id=?',(identifier,)).fetchone()
    if query['last_page'] and (cursor or '')==query['last_cursor']:
        value=json.loads(query['last_page'])
        if size(value)>budget:raise BusinessError('context_checkpoint_required','请保存检查点后继续合并。')
        return _charge(c,op,value)
    if cursor and cursor!=query['id']+':'+query['after_key']:raise BusinessError('context_cursor','请使用 facts 返回的顺序游标。')
    after=query['after_key'] if cursor else '';items=[];last=after;exhausted=True
    for row in c.execute("""SELECT step_key,source_id,source_version,result FROM context_steps
        WHERE operation_id=? AND status='completed' AND step_key>? ORDER BY step_key LIMIT 81""",(op['id'],after)):
        item=dict(row);item['result']=json.loads(item['result'] or '{}')
        if size({'items':items+[item]})>budget-1024 or len(items)>=80:
            if not items:raise BusinessError('context_checkpoint_required','剩余预算不足以读取该检查点，请压缩后继续。')
            exhausted=False;break
        items.append(item);last=row['step_key']
    sequential=after==query['after_key']
    delivered=query['delivered']+len(items) if sequential else query['delivered']
    result={'items':items,'total':count,'next_cursor':None if exhausted else query['id']+':'+last,
        'complete':bool(exhausted and sequential and delivered==count),
        'instruction':'同 key 的不同 value 是待核对冲突，保留各自来源；不能用最后一份摘要覆盖。'}
    if sequential:
        c.execute('UPDATE context_queries SET after_key=?,delivered=?,complete=?,last_cursor=?,last_page=? WHERE id=?',
            (last,delivered,int(result['complete']),cursor or '',encode(result),query['id']))
    return _charge(c,op,result)

def idle_batch(core):
    """Fair extraction backpressure: one bounded local batch between AI jobs."""
    with core.store.connect() as c:
        row=c.execute("SELECT key FROM material_catalog WHERE status='pending' ORDER BY updated_at LIMIT 1").fetchone()
    if row:extract_batch(core,row[0]);return True
    return False
