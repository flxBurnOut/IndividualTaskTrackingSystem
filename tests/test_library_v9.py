"""Readable originals preserve paths/bytes independently of database memories."""
from pathlib import Path
import os
import uuid
import pytest
from management.core import Core
from management import library
from management.schemas import BusinessError
from management.resources import ResourceError
from test_plan_assistance_v8 import cmd


def migrated(core,tmp_path,rel='02_课程/TEST101/课件/Week 1.pdf',owner=None):
    source=tmp_path/'original.bin';source.write_bytes(b'Synthetic original bytes only')
    metadata=core.resources.import_file(source)
    with core.store.connect() as c:
        return core._create(c,{'type':'asset','title':Path(rel).name,'data':{**metadata,
            'source_kind':'file','source_owner_id':owner,'original_name':Path(rel).name,
            'source_identity':'y2s1:'+rel,'source_relative_path':rel}},'synthetic-import')


def test_legacy_tree_preserves_hidden_paths_and_independent_bytes(tmp_path):
    core=Core(tmp_path/'data');e=migrated(core,tmp_path,rel='.agents/skills/example/SKILL.md')
    before=core.query('state');opened=core.query('open_resource',id=e['id']);path=Path(opened['path'])
    assert path.relative_to(core.root).as_posix()=='原文件/Y2S1/.agents/skills/example/SKILL.md'
    assert path.read_bytes()==core.resources._blob(e['data']['sha256']).read_bytes()
    assert not os.path.samefile(path,core.resources._blob(e['data']['sha256']))
    assert core.query('state')==before


def test_owner_folder_and_new_imports_follow_existing_course_structure(tmp_path):
    core=Core(tmp_path/'data');owner=cmd(core,'create',{'type':'course','title':'Synthetic course'})['entity']
    migrated(core,tmp_path,'02_课程/TEST101/课件/Week 1.pdf',owner['id'])
    migrated(core,tmp_path,'02_课程/TEST101/00_课程主页.md',owner['id'])
    result=core.query('library_folder',owner_id=owner['id']);assert result['total']==2 and not result['issues']
    assert Path(result['path']).relative_to(core.root).as_posix()=='原文件/Y2S1/02_课程/TEST101'
    f=tmp_path/'New handout.txt';f.write_text('New course source','utf-8')
    e=cmd(core,'add_source',{'owner_id':owner['id'],'path':str(f),'library_subdir':''})['entity'];f.unlink()
    opened=core.query('open_resource',id=e['id'])
    assert Path(opened['path'])==Path(result['path'])/'New handout.txt'
    assert Path(opened['path']).read_text('utf-8')=='New course source'


def test_different_versions_never_replace_same_name_and_repeat_capture_can_share_copy(tmp_path):
    core=Core(tmp_path/'data');owner=cmd(core,'create',{'type':'course','title':'Synthetic'})['entity']
    source=tmp_path/'lesson.txt';source.write_text('Version one','utf-8')
    a=cmd(core,'add_source',{'path':str(source),'owner_id':owner['id']})['entity']
    source.write_text('Version two','utf-8')
    b=cmd(core,'add_source',{'path':str(source),'owner_id':owner['id']})['entity']
    paths=[Path(core.query('open_resource',id=e['id'])['path']) for e in (a,b)]
    assert paths[0]!=paths[1] and [p.read_text('utf-8') for p in paths]==['Version one','Version two']
    for title in ['notice-one','notice-two']:
        cmd(core,'add_source',{'kind':'notice','text':'Same preserved notice','title':title,'owner_id':owner['id']})
    assert core.query('library_folder',owner_id=owner['id'])['issues']==[]


def test_missing_readable_file_is_rebuilt_and_database_memories_are_independent(tmp_path):
    core=Core(tmp_path/'data');e=migrated(core,tmp_path)
    memory=cmd(core,'create',{'type':'note','title':'Independent memory','data':{'content':'Confirmed personal context'}})['entity']
    original=core.query('open_resource',id=e['id']);path=Path(original['path']);path.chmod(0o666);path.unlink()
    before=core.query('state');restored=core.query('open_resource',id=e['id'])
    assert Path(restored['path']).is_file() and core.query('get',id=memory['id'])['entity']==memory
    assert core.query('state')==before


def test_edited_original_is_preserved_and_can_be_imported_as_new_version(tmp_path):
    core=Core(tmp_path/'data');e=migrated(core,tmp_path)
    original_bytes=core.resources._blob(e['data']['sha256']).read_bytes()
    path=Path(core.query('open_resource',id=e['id'])['path']);path.chmod(0o666);path.write_bytes(b'User edited copy')
    with pytest.raises(BusinessError,match='已被修改'):core.query('open_resource',id=e['id'])
    result=core.query('library_folder');assert len(result['issues'])==1
    assert path.read_bytes()==b'User edited copy' and core.resources._blob(e['data']['sha256']).read_bytes()==original_bytes
    new=cmd(core,'add_source',{'path':str(path)})['entity']
    assert new['data']['sha256']!=e['data']['sha256']


def test_failed_copy_never_publishes_truncated_file_and_retry_is_safe(tmp_path,monkeypatch):
    core=Core(tmp_path/'data');e=migrated(core,tmp_path)
    original=core.resources._copy_verified
    def fail(src,dest,*_):
        dest.write_bytes(b'incomplete');raise ResourceError('LOW_DISK_SPACE','Synthetic failure')
    monkeypatch.setattr(core.resources,'_copy_verified',fail)
    with pytest.raises(ResourceError):core.query('open_resource',id=e['id'])
    assert not list((core.root/'原文件').rglob('*.partial')) and not list((core.root/'原文件').rglob('Week 1.pdf'))
    monkeypatch.setattr(core.resources,'_copy_verified',original)
    assert core.query('open_resource',id=e['id'])['exists']


@pytest.mark.parametrize('path',['../outside.txt','/absolute.txt','C:/drive.txt','a/../../escape','a\\b','bad:ads','dir/CON','dir/filename.'])
def test_untrusted_legacy_paths_cannot_escape_originals(tmp_path,path):
    core=Core(tmp_path/'data');e=migrated(core,tmp_path,rel=path)
    with pytest.raises(BusinessError):core.query('open_resource',id=e['id'])


def test_backups_restore_both_memories_and_rebuildable_original_structure(tmp_path):
    core=Core(tmp_path/'data');e=migrated(core,tmp_path);core.query('open_resource',id=e['id'])
    backup=cmd(core,'backup',{})
    target=tmp_path/'restored'
    cmd(core,'restore_backup',{'path':backup['path'],'target_dir':str(target)})
    restored=Core(target);opened=restored.query('open_resource',id=e['id'])
    assert Path(opened['path']).relative_to(target).as_posix()=='原文件/Y2S1/02_课程/TEST101/课件/Week 1.pdf'
    assert Path(opened['path']).read_bytes()==b'Synthetic original bytes only'


def test_legacy_root_files_and_shared_folder_aliases_point_to_real_files(tmp_path):
    core=Core(tmp_path/'data')
    owners=[cmd(core,'create',{'type':'course','title':f'Course {i}'})['entity'] for i in range(2)]
    files=[migrated(core,tmp_path,f'Original {i}.pdf',owner['id']) for i,owner in enumerate(owners)]
    views=[core.query('library_folder',owner_id=e['id']) for e in owners]
    assert views[0]['path']==views[1]['path']==str(core.root/'原文件'/'Y2S1')
    assert all((Path(view['path'])/e['data']['original_name']).is_file() for view,e in zip(views,files))


def test_long_parent_does_not_require_a_longer_temporary_filename(tmp_path,monkeypatch):
    core=Core(tmp_path/'data');rel='02_课程/TEST101/'+('long-parent-'*5)+'/._.DS_Store';e=migrated(core,tmp_path,rel)
    target=core.root/'原文件'/'Y2S1'/rel;maximum=len(str(target))+5
    original=core.resources._copy_verified
    def limited_copy(source,destination,digest,size):
        if len(str(destination))>maximum:raise FileNotFoundError('Simulated frozen Win32 MAX_PATH')
        return original(source,destination,digest,size)
    monkeypatch.setattr(core.resources,'_copy_verified',limited_copy)
    opened=core.query('open_resource',id=e['id'])
    assert Path(opened['path']).read_bytes()==b'Synthetic original bytes only'
    assert not list((core.root/'.staging').glob('library-*.partial'))


def owned_original(tmp_path):
    core=Core(tmp_path/'data')
    owner=cmd(core,'create',{'type':'course','title':'Synthetic archive course'})['entity']
    source=tmp_path/'lesson.txt';source.write_bytes(b'Synthetic immutable lesson')
    entity=cmd(core,'import_asset',{'path':str(source),'owner_id':owner['id'],'library_subdir':''})['entity']
    return core,owner,entity,Path(core.query('open_resource',id=entity['id'])['path'])


def archive_command(core,entity,subdir,*,state=None,request_id=None):
    state=state or core.query('state')
    return core.command('refile_source',{'id':entity['id'],'version':entity['version'],'library_subdir':subdir},
        request_id=request_id or str(uuid.uuid4()),epoch=state['epoch'],expected_revision=state['revision'])


def revise_asset_fixture(core,entity,title):
    """Simulate an independent committed version in this synthetic test space."""
    with core.store.lock,core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old=core.store.get(c,entity['id'])
        changed=core._save(c,old,{**old,'title':title},'synthetic-version-change','synthetic_fixture')
        core.store.set_meta(c,'revision',core.store.meta(c,'revision')+1)
        c.commit()
        return changed


def test_destination_pages_and_refile_keep_identity_index_and_restored_path(tmp_path):
    core,owner,entity,old_path=owned_original(tmp_path)
    root=old_path.parent
    (root/'课件'/'Week 3').mkdir(parents=True)
    (root/'作业').mkdir()
    before=core.query('state');items=[];offset=0
    while True:
        page=core.query('library_destinations',owner_id=owner['id'],limit=2,offset=offset)
        assert page['owner_id']==owner['id'] and Path(page['path'])==root
        assert page['root_relative_path']==root.relative_to(core.root/'原文件').as_posix()
        assert page['requires_choice'] and len(page['items'])<=2
        items.extend(page['items'])
        if page['next_offset'] is None:break
        assert page['next_offset']>offset
        offset=page['next_offset']
    assert len(items)==page['total']==len({item['subdir'] for item in items})
    assert {'','课件','课件/Week 3','作业'} <= {item['subdir'] for item in items}
    assert all(isinstance(item['label'],str) and item['label'] for item in items)
    assert core.query('state')==before

    receipt=archive_command(core,entity,'课件/Week 3')
    result=receipt['result'];moved=result['entity'];new_path=root/'课件'/'Week 3'/old_path.name
    assert receipt['archive_cleanup']['status']=='removed'
    assert result['previous_relative_path']==entity['data']['library_relative_path']
    assert moved['id']==entity['id'] and moved['version']==entity['version']+1
    assert moved['parent_id']==entity['parent_id'] and moved['title']==entity['title']
    changed={'library_subdir','library_relative_path'}
    assert {k:v for k,v in moved['data'].items() if k not in changed} == {k:v for k,v in entity['data'].items() if k not in changed}
    assert moved['data']['library_subdir']=='课件/Week 3'
    assert moved['data']['library_relative_path']==new_path.relative_to(core.root/'原文件').as_posix()
    assert new_path.read_bytes()==b'Synthetic immutable lesson' and not old_path.exists()
    assert not os.path.samefile(new_path,core.resources._blob(moved['data']['sha256']))
    with core.store.connect() as c:
        indexed=dict(c.execute('SELECT * FROM library_files WHERE entity_id=?',(entity['id'],)).fetchone())
    assert indexed['relative_path']==moved['data']['library_relative_path'] and indexed['sha256']==entity['data']['sha256']
    current=core.query('library_destinations',id=entity['id'])['current']
    assert current['id']==entity['id'] and current['version']==moved['version']
    assert current['library_subdir']=='课件/Week 3' and Path(current['path'])==new_path
    assert current['relative_path']==moved['data']['library_relative_path']
    backup=cmd(core,'backup',{});target=tmp_path/'restored'
    cmd(core,'restore_backup',{'path':backup['path'],'target_dir':str(target)})
    restored=Core(target);opened=Path(restored.query('open_resource',id=entity['id'])['path'])
    assert opened.relative_to(target/'原文件').as_posix()==moved['data']['library_relative_path']
    assert opened.read_bytes()==new_path.read_bytes()


@pytest.mark.parametrize('case',['edited_original','different_target'])
def test_refile_rejects_changed_or_conflicting_bytes_without_overwrite(tmp_path,case):
    core,owner,entity,old_path=owned_original(tmp_path)
    target=old_path.parent/'课件'/old_path.name
    if case=='edited_original':
        old_path.chmod(0o666);old_path.write_bytes(b'User edited original')
        expected='edited_copy'
    else:
        target.parent.mkdir();target.write_bytes(b'Other existing material');expected='library_conflict'
    before=core.query('state')
    with pytest.raises(BusinessError) as caught:archive_command(core,entity,'课件')
    assert caught.value.code==expected
    assert core.query('state')==before and core.query('get',id=entity['id'])['entity']==entity
    assert old_path.read_bytes()==(b'User edited original' if case=='edited_original' else b'Synthetic immutable lesson')
    if case=='different_target':assert target.read_bytes()==b'Other existing material'
    assert core.resources._blob(entity['data']['sha256']).read_bytes()==b'Synthetic immutable lesson'


def test_refile_preserves_old_path_shared_by_another_asset(tmp_path):
    core,owner,entity,old_path=owned_original(tmp_path)
    with core.store.lock,core.store.connect() as c:
        shared=core._create(c,{'type':'asset','title':'Synthetic shared original','data':entity['data']},'synthetic-shared-asset')
    assert Path(core.query('open_resource',id=shared['id'])['path'])==old_path
    receipt=archive_command(core,entity,'课件')
    assert receipt['archive_cleanup']['status']=='retained_shared'
    assert old_path.read_bytes()==b'Synthetic immutable lesson'
    assert Path(core.query('open_resource',id=shared['id'])['path'])==old_path
    assert Path(core.query('open_resource',id=entity['id'])['path'])!=old_path


@pytest.mark.parametrize('subdir',['../escape','/absolute','C:/drive','a\\b','a/../b','a//b','dir/NUL','bad:ads','tail.'])
def test_refile_rejects_unsafe_classification_without_mutation(tmp_path,subdir):
    core,owner,entity,old_path=owned_original(tmp_path);before=core.query('state')
    with pytest.raises((BusinessError,ResourceError)):archive_command(core,entity,subdir)
    assert core.query('state')==before and core.query('get',id=entity['id'])['entity']==entity
    assert old_path.read_bytes()==b'Synthetic immutable lesson'


def test_refile_rejects_linked_destination_without_link_creation_privilege(tmp_path,monkeypatch):
    core,owner,entity,old_path=owned_original(tmp_path)
    target=old_path.parent/'linked';target.mkdir();actual=Path.is_symlink
    monkeypatch.setattr(Path,'is_symlink',lambda path:path==target or actual(path))
    with pytest.raises((ResourceError,BusinessError)) as caught:archive_command(core,entity,'linked')
    assert caught.value.code in {'UNSAFE_PATH','library_path'}
    assert not list(target.iterdir()) and old_path.is_file()
    assert core.query('get',id=entity['id'])['entity']==entity


@pytest.mark.parametrize('guard',['version','epoch'])
def test_stale_refile_guards_fail_before_creating_destination(tmp_path,guard):
    core,owner,entity,old_path=owned_original(tmp_path);state=core.query('state')
    if guard=='version':
        revise_asset_fixture(core,entity,'Updated synthetic lesson')
        state=core.query('state');expected='entity_conflict'
    else:state={**state,'epoch':'another-data-space'};expected='epoch_conflict'
    with pytest.raises(BusinessError) as caught:archive_command(core,entity,'课件',state=state)
    assert caught.value.code==expected
    assert old_path.is_file() and not (old_path.parent/'课件'/old_path.name).exists()


def test_late_materialize_uses_current_archive_and_never_revives_old_path(tmp_path):
    core,owner,stale,old_path=owned_original(tmp_path)
    moved=archive_command(core,stale,'课件')['result']['entity']
    late=library.materialize(core,stale)
    assert late['relative_path']==moved['data']['library_relative_path']
    assert not old_path.exists() and Path(late['path']).is_file()
    with core.store.connect() as c:
        assert c.execute('SELECT relative_path FROM library_files WHERE entity_id=?',(stale['id'],)).fetchone()[0]==late['relative_path']


def test_refile_ready_intent_survives_restart_without_repeating_copy(tmp_path,monkeypatch):
    core,owner,entity,old_path=owned_original(tmp_path)
    rid=str(uuid.uuid4());state=core.query('state');dispatch=core._dispatch
    def interrupted(c,name,payload,request_id,prepared=None):
        if name=='refile_source':raise BusinessError('synthetic_interruption','Synthetic commit interruption')
        return dispatch(c,name,payload,request_id,prepared)
    monkeypatch.setattr(core,'_dispatch',interrupted)
    with pytest.raises(BusinessError,match='Synthetic commit interruption'):
        archive_command(core,entity,'课件',state=state,request_id=rid)
    assert core.query('operation',request_id=rid)['operation']['status']=='ready'
    assert not core.query('receipt',request_id=rid)['found'] and old_path.is_file()
    target=old_path.parent/'课件'/old_path.name;assert target.read_bytes()==old_path.read_bytes()
    reopened=Core(core.root)
    def forbidden_prepare(*args,**kwargs):pytest.fail('Ready retry must reuse the durable prepared copy')
    monkeypatch.setattr(reopened,'_prepare',forbidden_prepare)
    receipt=archive_command(reopened,entity,'课件',state=state,request_id=rid)
    assert receipt['archive_cleanup']['status']=='removed' and not old_path.exists()
    replay=archive_command(reopened,entity,'课件',state=state,request_id=rid)
    assert replay['replayed'] and replay['result']==receipt['result']


@pytest.mark.parametrize('interruption',['reported','process_exit'])
def test_interrupted_refile_prepare_can_resume_same_request_safely(tmp_path,monkeypatch,interruption):
    core,owner,entity,old_path=owned_original(tmp_path)
    rid=str(uuid.uuid4());state=core.query('state');prepare=core._prepare
    class SyntheticProcessExit(BaseException):pass
    failure=(BusinessError('synthetic_interruption','Synthetic prepare interruption') if interruption=='reported'
             else SyntheticProcessExit('Synthetic prepare interruption'))
    def interrupted(name,payload):
        prepared=prepare(name,payload)
        if name=='refile_source':raise failure
        return prepared
    monkeypatch.setattr(core,'_prepare',interrupted)
    with pytest.raises(type(failure),match='Synthetic prepare interruption'):
        archive_command(core,entity,'课件',state=state,request_id=rid)
    assert core.query('operation',request_id=rid)['operation']['status']==('needs_reconciliation' if interruption=='reported' else 'preparing')
    assert old_path.is_file() and core.query('get',id=entity['id'])['entity']==entity
    reopened=Core(core.root)
    receipt=archive_command(reopened,entity,'课件',state=state,request_id=rid)
    assert receipt['archive_cleanup']['status']=='removed'
    assert receipt['result']['entity']['id']==entity['id']
    assert len(list((old_path.parent/'课件').iterdir()))==1 and not old_path.exists()


def test_refile_version_change_during_prepare_preserves_current_entity_and_old_copy(tmp_path,monkeypatch):
    core,owner,entity,old_path=owned_original(tmp_path);prepare=core._prepare;current={}
    def competing_edit(name,payload):
        prepared=prepare(name,payload)
        if name=='refile_source':
            current.update(revise_asset_fixture(core,entity,'Concurrent synthetic edit'))
        return prepared
    monkeypatch.setattr(core,'_prepare',competing_edit)
    with pytest.raises(BusinessError) as caught:archive_command(core,entity,'课件')
    assert caught.value.code=='entity_conflict'
    assert core.query('get',id=entity['id'])['entity']==current
    assert old_path.read_bytes()==b'Synthetic immutable lesson'
    assert Path(core.query('open_resource',id=entity['id'])['path'])==old_path


@pytest.mark.parametrize('retry_modified',[False,True])
def test_refile_cleanup_failure_replay_preserves_receipt_and_modified_old_file(tmp_path,monkeypatch,retry_modified):
    core,owner,entity,old_path=owned_original(tmp_path)
    rid=str(uuid.uuid4());state=core.query('state');unlink=Path.unlink
    def denied(path,*args,**kwargs):
        if path==old_path:raise PermissionError('Synthetic old file locked')
        return unlink(path,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',denied)
    receipt=archive_command(core,entity,'课件',state=state,request_id=rid)
    assert receipt['archive_cleanup']['status']=='pending' and old_path.is_file()
    assert core.query('operation',request_id=rid)['operation']['result']['cleanup']['status']=='pending'
    assert core.query('receipt',request_id=rid)['receipt']['result']==receipt['result']
    if retry_modified:
        old_path.chmod(0o666);old_path.write_bytes(b'Edited after committed archive')
    monkeypatch.setattr(Path,'unlink',unlink)
    replay=archive_command(core,entity,'课件',state=state,request_id=rid)
    assert replay['replayed'] and replay['result']==receipt['result']
    assert replay['archive_cleanup']['status']==('retained_modified' if retry_modified else 'removed')
    if retry_modified:assert old_path.read_bytes()==b'Edited after committed archive'
    else:assert not old_path.exists()
    assert Path(core.query('open_resource',id=entity['id'])['path']).read_bytes()==b'Synthetic immutable lesson'


@pytest.mark.parametrize('target_change',['missing','modified'])
def test_pending_cleanup_rechecks_current_target_before_removing_old_copy(tmp_path,monkeypatch,target_change):
    core,owner,entity,old_path=owned_original(tmp_path)
    rid=str(uuid.uuid4());state=core.query('state');unlink=Path.unlink
    def denied(path,*args,**kwargs):
        if path==old_path:raise PermissionError('Synthetic old file locked')
        return unlink(path,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',denied)
    receipt=archive_command(core,entity,'课件',state=state,request_id=rid)
    assert receipt['archive_cleanup']['status']=='pending'
    monkeypatch.setattr(Path,'unlink',unlink)
    target=Path(receipt['result']['library']['path']);target.chmod(0o666)
    if target_change=='missing':target.unlink()
    else:target.write_bytes(b'User modified destination')
    replay=archive_command(core,entity,'课件',state=state,request_id=rid)
    assert replay['replayed'] and replay['result']==receipt['result']
    assert replay['archive_cleanup']['status']=='pending'
    assert old_path.read_bytes()==b'Synthetic immutable lesson'
    if target_change=='missing':assert not target.exists()
    else:assert target.read_bytes()==b'User modified destination'
    assert core.query('operation',request_id=rid)['operation']['result']['cleanup']['status']=='pending'


def test_refile_preserves_collision_suffix_filename(tmp_path):
    core,owner,first,first_path=owned_original(tmp_path)
    source=tmp_path/'lesson.txt';source.write_bytes(b'Synthetic newer lesson')
    second=cmd(core,'import_asset',{'path':str(source),'owner_id':owner['id'],'library_subdir':''})['entity']
    previous=Path(core.query('open_resource',id=second['id'])['path'])
    assert previous.name!=source.name and previous!=first_path
    moved=archive_command(core,second,'课件')
    target=Path(moved['result']['library']['path'])
    assert target.name==previous.name and target.parent==previous.parent/'课件'
    assert target.read_bytes()==b'Synthetic newer lesson' and not previous.exists()
    assert first_path.read_bytes()==b'Synthetic immutable lesson'
    assert moved['result']['entity']['data']['original_name']==source.name
