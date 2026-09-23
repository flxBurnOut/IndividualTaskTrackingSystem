"""Readable originals preserve paths/bytes independently of database memories."""
from pathlib import Path
import os
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
    e=cmd(core,'add_source',{'owner_id':owner['id'],'path':str(f)})['entity'];f.unlink()
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
