import json
from pathlib import Path
import uuid
import pytest
from management.core import Core
from management.schemas import BusinessError


def cmd(core,name,p):
    s=core.query('state')
    return core.command(name,p,request_id=str(uuid.uuid4()),epoch=s['epoch'],expected_revision=s['revision'])['result']


def create(core,type='project',title='Synthetic',**kw):
    return cmd(core,'create',dict(type=type,title=title,**kw))['entity']


def prefs():
    return {'daily':{'enabled':True,'time':'22:15'},'weekly':{'enabled':True,'weekday':5,'time':'19:45'},'timezone':'Asia/Shanghai'}


def test_preferences_read_has_no_side_effect_and_repeat_save_keeps_ids(tmp_path):
    c=Core(tmp_path)
    assert c.query('review_preferences')['daily']['enabled'] is False
    assert c.query('list')['total']==0
    first=cmd(c,'set_review_preferences',prefs())
    for _ in range(5):
        result=cmd(c,'set_review_preferences',prefs())
        assert result['schedule_ids']==first['schedule_ids']
    assert c.query('list',type='schedule')['total']==2
    actual=c.query('review_preferences')
    assert actual['daily']['time']=='22:15' and actual['weekly']['weekday']==5
    reopened=Core(tmp_path)
    assert reopened.query('review_preferences')['weekly']['time']=='19:45'


def test_old_global_duplicates_disabled_not_deleted_and_warning_untouched(tmp_path):
    c=Core(tmp_path)
    old=[]
    for i in range(2):
        old.append(create(c,'schedule',f'Old {i}',data={'workflow':'checkin','frequency':'daily','time':'20:00','enabled':True})['id'])
    warning=create(c,'schedule','Warning',data={'workflow':'warnings','frequency':'daily','time':'08:00','enabled':True})
    assert len(c.query('review_preferences')['duplicate_schedules'])==1
    result=cmd(c,'set_review_preferences',prefs())
    assert result['schedule_ids']['daily']==old[0]
    assert c.query('get',id=old[1])['entity']['data']['enabled'] is False
    assert c.query('get',id=warning['id'])['entity']['data']['enabled'] is True
    assert all(c.query('get',id=id)['entity'] for id in old)


@pytest.mark.parametrize('change',[{'daily':{'enabled':True,'time':'99:00'}},{'weekly':{'enabled':True,'weekday':7,'time':'19:00'}},{'timezone':'No/SuchZone'}])
def test_invalid_reminder_settings_roll_back_both_schedules(tmp_path,change):
    c=Core(tmp_path)
    with pytest.raises(BusinessError):
        cmd(c,'set_review_preferences',{**prefs(),**change})
    assert c.query('list')['total']==0


def test_local_files_are_references_open_original_and_reuse_same_owner(tmp_path):
    source=tmp_path/'original.txt'; source.write_text('original bytes',encoding='utf-8')
    c=Core(tmp_path/'data')
    owner=create(c,'course')
    first=cmd(c,'attach_local_file',{'path':str(source),'owner_id':owner['id']})
    again=cmd(c,'attach_local_file',{'path':str(source),'owner_id':owner['id']})
    assert again['reused'] and first['entity']['id']==again['entity']['id']
    assert not (c.root/'blobs').exists() or not list((c.root/'blobs').iterdir())
    opened=c.query('open_resource',id=first['entity']['id'])
    assert Path(opened['path'])==source and opened['exists'] and opened['reference_only']
    ws=c.query('object_workspace',id=owner['id'])
    assert len(ws['files'])==1 and ws['files'][0]['exists']
    source.rename(tmp_path/'moved.txt')
    assert c.query('open_resource',id=first['entity']['id'])['exists'] is False
    assert (tmp_path/'moved.txt').read_text('utf-8')=='original bytes'


def test_workspace_counts_known_results_and_unknown_without_guessing(tmp_path):
    c=Core(tmp_path)
    owner=create(c)
    targets=[create(c,'task',f'Task {i}',parent_id=owner['id']) for i in range(3)]
    for target,result in zip(targets,['done','incomplete']):
        cmd(c,'record_feedback',{'target_id':target['id'],'business_date':'2030-01-01','dimensions':{'completion':result},'source_text':'Explicit synthetic choice'})
    summary=c.query('object_workspace',id=owner['id'])['summary']
    assert summary['total_tasks']==3
    assert summary['done_tasks']==summary['incomplete_tasks']==summary['unknown_tasks']==1
    assert '任务条目' in summary['coverage_label']


def test_missing_local_file_gives_actionable_error(tmp_path):
    c=Core(tmp_path/'data'); owner=create(c)
    with pytest.raises(BusinessError) as e:
        cmd(c,'attach_local_file',{'path':str(tmp_path/'absent.txt'),'owner_id':owner['id']})
    assert e.value.code=='file_unavailable'
    assert c.query('list',type='file_reference')['total']==0


def test_files_paginate_independently_from_child_tasks(tmp_path):
    c=Core(tmp_path/'data'); owner=create(c)
    task=create(c,'task','Visible task',parent_id=owner['id'])
    for i in range(8):
        source=tmp_path/f'file-{i:02d}.txt'; source.write_text('synthetic')
        cmd(c,'attach_local_file',{'path':str(source),'owner_id':owner['id']})
    seen=[]; offset=0
    while offset is not None:
        result=c.query('object_workspace',id=owner['id'],files_offset=offset,files_limit=3,limit=1)
        assert result['children_total']==1 and result['children'][0]['id']==task['id']
        assert result['next_offset'] is None and len(result['files'])<=3
        seen.extend(f['id'] for f in result['files'])
        offset=result['files_next_offset']
    assert len(seen)==len(set(seen))==8
