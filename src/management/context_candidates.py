"""Streaming candidate manifests: bounded transport, atomic business adoption."""
from __future__ import annotations
import hashlib
import json
from .storage import encode
from .schemas import BusinessError


def iter_actions(c,operation_id):
    for row in c.execute('SELECT command,payload,reason FROM context_actions WHERE operation_id=? ORDER BY seq',(operation_id,)):
        yield {'command':row['command'],'payload':json.loads(row['payload']),'reason':row['reason']}


def manifest(c,operation_id):
    h=hashlib.sha256();total=0
    for row in c.execute('SELECT seq,command,payload,reason FROM context_actions WHERE operation_id=? ORDER BY seq',(operation_id,)):
        h.update(encode(list(row)).encode('utf-8'));total+=1
    return {'operation_id':operation_id,'sha256':h.hexdigest(),'total':total}


def stage(c,op,candidate):
    from .context_service import digest,size
    if candidate.get('action_manifest'):
        if candidate['action_manifest']!=manifest(c,op['id']):
            raise BusinessError('candidate_conflict','候选清单已改变，请重新核对后确认。')
    else:
        for action in candidate.get('actions',[]):
            c.execute('INSERT OR IGNORE INTO context_actions(operation_id,fingerprint,command,payload,reason) VALUES (?,?,?,?,?)',
                (op['id'],digest([action['command'],action['payload']]),action['command'],encode(action['payload']),action.get('reason','')))
    preview=[]
    for row in c.execute('SELECT seq,command,payload,reason FROM context_actions WHERE operation_id=? ORDER BY seq',(op['id'],)):
        action={'candidate_row':row['seq'],'command':row['command'],'payload':json.loads(row['payload']),'reason':row['reason']}
        if len(preview)>=30 or preview and size(preview+[action])>12000:break
        if size(action)>14000:
            preview.append({'candidate_row':row['seq'],'command':row['command'],'payload':{'title':'内容较长，请展开完整候选'},'reason':row['reason'][:120],'large_candidate':True})
            break
        preview.append(action)
    return {**candidate,'actions':preview,'action_manifest':manifest(c,op['id']),
            'actions_total':c.execute('SELECT count(*) FROM context_actions WHERE operation_id=?',(op['id'],)).fetchone()[0],
            'action_reader':{'query':'candidate_actions','operation_id':op['id']}}


def page(core,p):
    from .context_service import _operation,size,_fragment
    with core.store.connect() as c:
        op=_operation(core,c,p['operation_id'])
        after=max(0,int(p.get('after',0)));items=[];following=None
        total=c.execute('SELECT count(*) FROM context_actions WHERE operation_id=?',(op['id'],)).fetchone()[0]
        for row in c.execute('SELECT * FROM context_actions WHERE operation_id=? AND seq>? ORDER BY seq LIMIT 31',(op['id'],after)):
            action={'candidate_row':row['seq'],'command':row['command'],'payload':json.loads(row['payload']),'reason':row['reason']}
            if size(action)>11000:
                if items:following=items[-1]['candidate_row'];break
                fragment,offset=_fragment(encode(action),int(p.get('offset',0)),11000)
                return {'json_fragment':fragment,'next_offset':offset,'candidate_row':row['seq'],'next_after':row['seq'] if offset is None else after,
                        'total':total}
            if len(items)==30 or size(items+[action])>12000:following=items[-1]['candidate_row'];break
            items.append(action)
        return {'items':items,'next_after':following,'total':total}
