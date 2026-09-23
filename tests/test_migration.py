import copy
import hashlib
import json
import pytest
from management.core import Core
from management.migration import apply_snapshot
from management.schemas import BusinessError


def snapshot():
    return {'format':'management-migration/1', 'source':{'system':'fixture'},
            'records':[
                {'key':'course','type':'course','title':'Course','data':{'code':'C1'}},
                {'key':'task','type':'task','title':'Unconfirmed work','parent_id':{'$ref':'course'},'status':'pending','data':{}},
                {'key':'feedback','type':'feedback','title':'Explicit unknown','data':{
                    'target_id':{'$ref':'task'},'business_date':'2026-08-10',
                    'dimensions':{'completion':'unknown'},'source_text':'Original row'}},
                {'key':'plan','type':'plan','title':'Old plan','status':'planned','data':{
                    'date':'2026-08-10','mode':'standard','historical_import':True,
                    'blocks':[{'target_id':{'$ref':'task'},'completion_gate':'Original condition'}]}}
            ]}


def test_atomic_import_idempotency_and_unknowns(tmp_path):
    s=snapshot();result=apply_snapshot(tmp_path,s)
    again=apply_snapshot(tmp_path,s)
    assert again['replayed'] and again['ids']==result['ids']
    core=Core(tmp_path)
    review=core.query('daily_review',date='2026-08-10')
    assert review['summary']['unreported']==1
    assert review['items'][0]['completion_gate']=='Original condition'
    with core.store.connect() as c:
        task=core.store.get(c,result['ids']['task'])
        assert task['status']=='pending' and 'actual_minutes' not in task['data']
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_invalid_last_record_rolls_back_all_business_rows(tmp_path):
    s=snapshot();s['records'][-1]['data']['blocks'][0]['target_id']={'$ref':'missing'}
    with pytest.raises(BusinessError,match='未创建的来源引用'):apply_snapshot(tmp_path,s)
    with Core(tmp_path).store.connect() as c:
        assert c.execute('SELECT count(*) FROM entities').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM changes').fetchone()[0]==0


def test_refuses_live_and_nonempty_spaces(tmp_path):
    (tmp_path/'runtime.json').write_text('{}')
    with pytest.raises(BusinessError) as error:apply_snapshot(tmp_path,snapshot())
    assert error.value.code=='migration_live'
    (tmp_path/'runtime.json').unlink()
    core=Core(tmp_path);state=core.query('state')
    core.command('create',{'type':'task','title':'Keep me'},request_id='existing',epoch=state['epoch'],expected_revision=state['revision'])
    with pytest.raises(BusinessError) as error:apply_snapshot(tmp_path,snapshot())
    assert error.value.code=='migration_not_empty'
    assert core.query('list',type='task')['items'][0]['title']=='Keep me'


def test_managed_copy_does_not_depend_on_original(tmp_path):
    from management.resources import ResourceManager
    root=tmp_path/'data';original=tmp_path/'original.txt';original.write_text('Evidence',encoding='utf-8')
    metadata=ResourceManager(root).import_file(original)
    s={'format':'management-migration/1','records':[{'key':'asset','type':'asset','title':'Original',
        'data':{**metadata,'original_name':'original.txt','original_path':str(original),'managed_copy':True}}]}
    original.unlink()
    result=apply_snapshot(root,s)
    assert result['asset_objects_verified']==1
    assert ResourceManager(root)._blob(metadata['sha256']).read_text()=='Evidence'
    different=copy.deepcopy(s);different['records'][0]['title']='Changed'
    with pytest.raises(BusinessError) as error:apply_snapshot(root,different)
    assert error.value.code=='migration_conflict'
