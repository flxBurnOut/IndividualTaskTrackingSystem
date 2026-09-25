import json,uuid
import pytest
from management.core import Core
from management.schemas import BusinessError
from management.sources import apply_source_action
from management.storage import encode
from management import conversations


def cmd(core,name,p):
 s=core.query('state');return core.command(name,p,request_id=str(uuid.uuid4()),epoch=s['epoch'],expected_revision=s['revision'])['result']

def owner(core):return cmd(core,'create',{'type':'timetable','title':'Synthetic table'})['entity']

def payload(table):
 return {'id':table['id'],'version':table['version'],'title':table['title'],'semester_start':'2030-01-07','semester_end':'2030-02-03','timezone':'Asia/Shanghai','source_text':'User supplied synthetic lesson times','rows':[{'key':'class-a','title':'Synthetic class','weekday':0,'start':'09:00','end':'10:00'}]}


def test_fresh_timetable_discussion_uploads_sources_and_rereads_current_records(tmp_path):
 core=Core(tmp_path/'data');table=owner(core)
 source=cmd(core,'add_source',{'owner_id':table['id'],'kind':'notice','text':'Every Monday 09:00-10:00. Term dates not supplied.'})['entity']
 cmd(core,'settings',{'settings':{'ai':{'enabled':True}}})
 result=cmd(core,'send_message',{'scope':{'kind':'timetable','entity_id':table['id']},'text':'Read my weekly timetable'})
 context=result['job']['input']
 assert context['allowed_commands']==['apply_timetable']
 assert context['source_owner_scope']==table['id'] and context['source_versions']=={}
 from context_harness import materials,item,rows
 chunks=materials(core,result['job'])
 assert any('Every Monday' in part['content'].get('text','') for part in chunks)
 assert item(core,result['job'],table['id'])['id']==table['id']
 assert not core.query('list',type='event')['total']
 assert not core.query('list',type='plan')['total']
 cmd(core,'cancel_job',{'id':result['job']['id']})
 cmd(core,'apply_timetable',payload(table))
 again=cmd(core,'send_message',{'scope':{'kind':'timetable','entity_id':table['id']},'text':'Read current adopted schedule','source_ids':[]})
 assert again['conversation']['id']==result['conversation']['id']
 assert item(core,again['job'],table['id'])['version']>table['version']
 assert len(rows(core,again['job'],'events'))==1


def test_source_scope_rejects_unrelated_mutations_and_keeps_provenance(tmp_path):
 core=Core(tmp_path/'data');table=owner(core);other=owner(core)
 source=cmd(core,'add_source',{'owner_id':table['id'],'kind':'notice','text':'Synthetic explicit weekly source'})['entity']
 job={'input':{'conversation_scope':{'kind':'timetable','entity_id':table['id']},'source_versions':{source['id']:source['version']}}}
 with core.store.connect() as c:
  for action in [{'command':'create','payload':{'type':'task','title':'Not authorized'}},{'command':'apply_timetable','payload':payload(other)}]:
   with pytest.raises(BusinessError,match='当前课表'):apply_source_action(core,c,job,action,'synthetic')
  result=apply_source_action(core,c,job,{'command':'apply_timetable','payload':payload(table)},'synthetic')
  assert result['entity']['data']['source_versions']=={source['id']:source['version']}
  assert result['rows'][0]['data']['source_versions']=={source['id']:source['version']}


def test_applied_metadata_and_membership_cannot_bypass_shared_flow(tmp_path):
 core=Core(tmp_path/'data');table=owner(core)
 applied=cmd(core,'apply_timetable',payload(table));table=applied['entity'];event=applied['rows'][0]
 for name,value in [('update',{'id':table['id'],'version':table['version'],'patch':{'data':{'semester_start':'2030-01-14'}}}),('update',{'id':event['id'],'version':event['version'],'patch':{'data':{'teaching_weeks':[1]}}}),('create',{'type':'event','title':'Bypass','data':{'timetable_id':table['id']}}),('archive',{'id':table['id'],'version':table['version'],'archived':True})]:
  with pytest.raises(BusinessError):cmd(core,name,value)
 changed=cmd(core,'update',{'id':event['id'],'version':event['version'],'patch':{'data':{'start':'10:00','end':'11:00','location':'Room 2'}}})['entity']
 assert changed['data']['timetable_id']==table['id']
 assert core.query('timetables',id=table['id'])['rows'][0]['data']['start']=='10:00'
