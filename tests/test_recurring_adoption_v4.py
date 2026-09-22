import uuid
import pytest
from management.core import Core
from management.schemas import BusinessError
from management import sources
from test_source_boundaries_v3 import source_fixture,candidate,create,cmd

@pytest.mark.parametrize('mode',['valid','wrong_anchor','wrong_rule','missing_source'])
def test_course_recurring_candidate_scope_and_evidence(tmp_path,mode):
    core=Core(tmp_path/'recurring-course');owner=create(core);other=create(core,title='Other course')
    anchor=create(core,'event',data={'owner_id':owner['id'],'date':'2030-01-09','recurrence':'weekly'})
    alien=create(core,'event',data={'owner_id':other['id'],'date':'2030-01-10','recurrence':'weekly'})
    source=source_fixture(core,tmp_path,'Explicit synthetic lab preparation requirement.',owner)
    p={'anchor_id':anchor['id'],'title':'Prepare','content':'Attempt exercises','completion_gate':'Check exercise answers','days_before':2,'source_text':'Synthetic source page 1'}
    if mode=='wrong_anchor':p['anchor_id']=alien['id']
    if mode=='wrong_rule':
        other_rule=cmd(core,'set_recurring_rule',{**p,'anchor_id':alien['id']})['entity'];p.update(id=other_rule['id'],version=other_rule['version'])
    if mode=='missing_source':p.pop('source_text')
    cmd(core,'settings',{'settings':{'ai':{'enabled':True}}})
    job=candidate(core,owner,source,[{'command':'set_recurring_rule','payload':p}])
    if mode=='valid':
        result=cmd(core,'apply_proposal',{'id':job})
        assert result['results'][0]['entity']['data']['source_text']=='Synthetic source page 1'
    else:
        before=core.query('state')['revision']
        with pytest.raises(BusinessError) as error:cmd(core,'apply_proposal',{'id':job})
        assert error.value.code==('source_evidence' if mode=='missing_source' else 'course_scope')
        assert core.query('state')['revision']==before


def test_rule_cannot_bypass_validation_and_restore_pauses_without_losing_ledger(tmp_path):
    core=Core(tmp_path/'original')
    anchor=create(core,'event',data={'date':'2030-01-09','recurrence':'weekly'})
    rule=cmd(core,'set_recurring_rule',{'anchor_id':anchor['id'],'title':'Prepare','content':'Do exercises','completion_gate':'Check answers','days_before':2,'materialize_date':'2030-01-07'})['entity']
    for name,p in [('update',{'id':rule['id'],'version':1,'patch':{'data':{'days_before':999}}}),('move',{'id':rule['id'],'version':1,'parent_id':None})]:
        with pytest.raises(BusinessError):cmd(core,name,p)
    backup=cmd(core,'backup',{})
    # Same public backup/restore path as the GUI.
    path=backup.get('path') or backup.get('backup_path')
    if path is None:
        assert False,backup
    target=tmp_path/'restored';cmd(core,'restore_backup',{'path':path,'target_dir':str(target)})
    restored=Core(target)
    value=restored.query('get',id=rule['id'])['entity']
    assert value['data']['enabled'] is False and value['data']['restore_review_required'] is True
    with restored.store.connect() as c:
        assert c.execute('SELECT count(*) FROM recurring_occurrences').fetchone()[0]==1
    assert restored.query('list',type='task')['total']==1
