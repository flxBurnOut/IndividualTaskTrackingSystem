"""Synthetic model adapter for tests of business rules under context/1.

Reads real pages, acknowledges synthetic material analysis, and models a confirmed
compaction only when quota is exhausted. It never weakens production validators.
"""
import json
from management import context_service as cs


def op_id(core,job):
    if isinstance(job,str):job=core.query('job',id=job)['job']
    value=json.loads(job['input']) if isinstance(job['input'],str) else job['input']
    return value.get('context_operation_id')


def read(core,name,**params):
    while True:
        try:return core.query(name,**params)
        except Exception as error:
            if getattr(error,'code',None)!='context_checkpoint_required':raise
            cs.compacted(core,params['operation_id'])


def rows(core,job,collection,**filters):
    identifier=op_id(core,job);cursor=None;result=[]
    while True:
        page=read(core,'query_context',operation_id=identifier,collection=collection,filters=filters,cursor=cursor)
        result.extend(page['items']);cursor=page.get('next_cursor')
        if not cursor:return result


def item(core,job,identifier):
    op=op_id(core,job);offset=0;version=None;parts=[]
    while True:
        value=read(core,'read_context_item',operation_id=op,id=identifier,offset=offset,version=version)
        if 'item' in value:return value['item']
        parts.append(value['json_fragment']);offset=value.get('next_offset');version=value['version']
        if offset is None:return ''.join(parts) if value['format']=='text' else json.loads(''.join(parts))


def materials(core,job):
    op=op_id(core,job);result=[]
    while True:
        step=read(core,'next_context_step',operation_id=op)
        if step.get('all_steps_processed'):break
        if not step.get('step_key'):continue
        result.append(step)
        core.query('checkpoint_context',operation_id=op,step_key=step['step_key'],delivery_token=step['delivery_token'],result={'facts':[],'notes':'Synthetic model fixture acknowledges the supplied chunk.'})
    if result:rows(core,job,'facts')
    return result


def ready(core,job,*,planning=False):
    materials(core,job)
    if planning:
        for name in ('deadlines','rules','events','plans'):rows(core,job,name)
        item(core,job,'@plan_request')
