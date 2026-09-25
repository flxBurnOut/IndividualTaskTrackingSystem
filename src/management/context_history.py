"""Read immutable previous analysis through the CURRENT operation's budget."""
import json
from .schemas import BusinessError
from .storage import encode


def origin(core,c,job_id):
    job=core._job(c,job_id);value=json.loads(job['input'])
    identifier=value.get('context_operation_id')
    if not identifier:raise BusinessError('legacy_history','这轮旧记录没有分批分析索引；请用 read_context_item(job:编号) 读取其保存结果。')
    return identifier,job['epoch']


def facts(core,c,op,job_id,cursor=None):
    from .context_service import _budget,_charge,size
    target,epoch=origin(core,c,job_id);budget=_budget(op,None);items=[];following=None
    for row in c.execute("SELECT step_key,source_id,source_version,result,fingerprint FROM context_steps WHERE operation_id=? AND status='completed' AND step_key>? ORDER BY step_key LIMIT 81",(target,cursor or '')):
        value=dict(row);value['result']=json.loads(value['result'] or '{}')
        if len(items)>=80 or size({'items':items+[value]})>budget-900:
            if not items:raise BusinessError('context_checkpoint_required','请保存当前操作检查点后继续读取历史。')
            following=items[-1]['step_key'];break
        items.append(value)
    return _charge(c,op,{'items':items,'next_cursor':following,'historical':True,'source_job_id':job_id,
        'source_epoch':epoch,'instruction':'这是当时的分析记录，不是当前业务事实。按游标继续可读取所有保存批次。'})


def material(core,p):
    from .context_service import _operation,_budget,_charge,size
    from .materials import verified_bytes
    with core.store.lock,core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE');op=_operation(core,c,p['operation_id']);budget=_budget(op,None)
        target,epoch=origin(core,c,p['from_job']);number=int(p.get('chunk') or 0)
        row=c.execute("""SELECT m.*,s.version source_version FROM context_sources s JOIN material_chunks m ON m.material_key=s.material_key
            WHERE s.operation_id=? AND s.entity_id=? AND m.seq=?""",(target,p['source_id'],number)).fetchone()
        if not row:raise BusinessError('material_history','当时的原件索引没有这个内容块；旧结果仍可通过 job 记录读取。')
        content={'kind':row['kind'],'locator':row['locator'],'sha256':row['sha256']}
        if row['kind']=='image':
            if op['delivered_bytes']+12000>65536-8192:raise BusinessError('context_checkpoint_required','请保存检查点后继续看历史图像。')
            content['image_ref']=row['material_key']+':'+str(number)
            c.execute('UPDATE context_operations SET delivered_bytes=delivered_bytes+12000 WHERE id=?',(op['id'],))
        else:content['text']=verified_bytes(core,row['sha256']).decode('utf-8')
        following=c.execute('SELECT 1 FROM material_chunks WHERE material_key=? AND seq=?',(row['material_key'],number+1)).fetchone()
        result={'source_id':p['source_id'],'source_version':row['source_version'],'chunk':number,'next_chunk':number+1 if following else None,
            'content':content,'historical':True,'from_job':p['from_job'],'source_epoch':epoch}
        if size(result)>budget:raise BusinessError('context_checkpoint_required','请保存检查点后继续读取旧原文。')
        result=_charge(c,op,result);c.commit();return result


def image(core,operation_id,job_id,image_ref):
    from .context_service import _operation
    from .materials import verified_bytes
    key,sequence=image_ref.rsplit(':',1)
    with core.store.connect() as c:
        _operation(core,c,operation_id);target,_=origin(core,c,job_id)
        row=c.execute("""SELECT m.sha256 FROM material_chunks m JOIN context_sources s ON s.material_key=m.material_key
            WHERE s.operation_id=? AND m.material_key=? AND m.seq=? AND m.kind='image'""",(target,key,int(sequence))).fetchone()
        if not row:raise BusinessError('material_history','此图不属于所选旧操作。')
    return verified_bytes(core,row[0])
