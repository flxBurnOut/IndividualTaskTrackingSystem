"""Registration edge cases without changing a desktop profile or starting a model."""
import json
from pathlib import Path
import pytest
from management import codex_project as cp
from management.schemas import BusinessError


def project(path, ident='project-1', name='Codex 事务助手'):
    return {'id': ident, 'name': name, 'roots': [{'path': str(path)}]}


class RPC:
    def __init__(self, pages, selected):
        self.pages = iter(pages)
        self.selected = selected
        self.calls = []
    def request(self, method, params):
        self.calls.append((method, params))
        if method == 'project/list': return next(self.pages)
        if method in {'project/create', 'project/read'}: return {'project': self.selected}
        raise AssertionError(method)


def test_paginate_before_desktop_handoff(tmp_path):
    target=project(tmp_path)
    rpc=RPC([{'data': [], 'nextCursor': 'page2'}, {'data':[target]}],target)
    assert cp._matching_projects(rpc,tmp_path)==[target]
    assert rpc.calls[1][1]['cursor']=='page2'


def test_desktop_launch_creates_through_native_registration(tmp_path,monkeypatch):
    target=project(tmp_path)
    rpc=RPC([{'data':[]},{'data':[target]}],target)
    launches=[];monkeypatch.setattr(cp,'open_desktop_workspace',launches.append)
    result,created=cp._desktop_project(rpc,tmp_path)
    assert result==target and created and launches==[tmp_path]
    assert not any(m=='project/create' for m,p in rpc.calls)
    assert rpc.calls[-1][0]=='project/read'


def test_secondary_root_is_not_default_workspace(tmp_path):
    other=project(tmp_path/'other');other['roots'].append({'path':str(tmp_path)})
    rpc=RPC([{'data':[other]}],project(tmp_path))
    assert cp._matching_projects(rpc,tmp_path)==[]


@pytest.mark.parametrize('pages', [
    [{'data':{},'nextCursor':None}],
    [{'data':[],'nextCursor':'a'},{'data':[],'nextCursor':'a'}],
    [{'data':[],'nextCursor':''}],
    [{'data':[],'nextCursor':42}],
    [{'data':[],'nextCursor':str(i)} for i in range(50)],
])
def test_incomplete_listing_never_creates(tmp_path,pages):
    rpc=RPC(pages,project(tmp_path))
    with pytest.raises(BusinessError):cp._matching_projects(rpc,tmp_path)
    assert not any(m=='project/create' for m,p in rpc.calls)


def test_existing_binding_reused_after_native_open(tmp_path,monkeypatch):
    target=project(tmp_path)
    cp._save_binding(tmp_path,{'status':'ready','workspace':str(tmp_path),'project_id':target['id']})
    rpc=RPC([{'data':[target]},{'data':[target]}],target)
    monkeypatch.setattr(cp,'open_desktop_workspace',lambda p:None)
    result,created=cp._desktop_project(rpc,tmp_path)
    assert result==target and not created


def test_new_desktop_project_wins_over_legacy_orphan(tmp_path,monkeypatch):
    orphan=project(tmp_path,'orphan');target=project(tmp_path,'desktop')
    rpc=RPC([{'data':[orphan]},{'data':[orphan,target]}],target)
    monkeypatch.setattr(cp,'open_desktop_workspace',lambda p:None)
    assert cp._desktop_project(rpc,tmp_path)==(target,True)


def test_wrong_readback_root_rejected(tmp_path,monkeypatch):
    rpc=RPC([{'data':[]},{'data':[project(tmp_path)]}],project(tmp_path/'elsewhere'))
    monkeypatch.setattr(cp,'open_desktop_workspace',lambda p:None)
    with pytest.raises(BusinessError,match='主目录'):cp._desktop_project(rpc,tmp_path)


def test_native_launch_failure_never_claims_ready(tmp_path,monkeypatch):
    rpc=RPC([{'data':[]}],project(tmp_path))
    def fail(p):raise cp._error('无法打开')
    monkeypatch.setattr(cp,'open_desktop_workspace',fail)
    with pytest.raises(BusinessError,match='无法打开'):cp._desktop_project(rpc,tmp_path)
    assert len(rpc.calls)==1


def test_desktop_registration_timeout(tmp_path,monkeypatch):
    rpc=RPC([{'data':[]},{'data':[]}],project(tmp_path))
    monkeypatch.setattr(cp,'open_desktop_workspace',lambda p:None)
    clock=iter([0,13]);monkeypatch.setattr(cp.time,'monotonic',lambda:next(clock))
    with pytest.raises(BusinessError,match='尚未完成'):cp._desktop_project(rpc,tmp_path)


def test_same_name_different_path_does_not_hijack(tmp_path):
    rpc=RPC([{'data':[project(tmp_path/'other')]}],project(tmp_path))
    assert cp._matching_projects(rpc,tmp_path)==[]


def test_protocol_link_is_path_only_and_encoded(tmp_path,monkeypatch):
    from urllib.parse import urlparse,parse_qs
    calls=[];monkeypatch.setattr(cp.os,'startfile',calls.append,raising=False)
    cp.open_desktop_workspace(tmp_path/'中文 # & 项目')
    url=urlparse(calls[0]);assert url.scheme=='codex' and url.netloc=='new'
    assert parse_qs(url.query)=={'path':[str((tmp_path/'中文 # & 项目').resolve())]}


def test_binding_only_accepts_current_path_ready_and_small_file(tmp_path):
    value={'status':'ready','workspace':str(tmp_path),'project_id':'project-1'}
    cp._save_binding(tmp_path,value)
    assert cp.project_binding(tmp_path)==value
    cp._save_binding(tmp_path,{**value,'workspace':str(tmp_path/'old')})
    assert cp.project_binding(tmp_path) is None
    cp._save_binding(tmp_path,{**value,'status':'needs_repair'})
    assert cp.project_binding(tmp_path) is None
    (tmp_path/cp.BINDING_FILE).write_text('x'*16385)
    assert cp.project_binding(tmp_path) is None


def test_active_config_never_changes_user_settings(tmp_path):
    class ConfigRPC:
        def request(self,method,params):
            assert method=='config/read'
            return {'layers':[{'name':{'type':'project','dotCodexFolder':str(tmp_path/'.codex')},'disabledReason':None},{'name':{'type':'user','file':str(tmp_path/'config.toml')},'version':'v1','config':{'projects':{str(tmp_path):{'trust_level':'trusted'}}}}]}
    cp._activate_project_config(ConfigRPC(),tmp_path)


def test_explicit_untrusted_is_preserved(tmp_path):
    class ConfigRPC:
        def request(self,method,params):
            assert method=='config/read'
            return {'layers':[{'name':{'type':'user','file':str(tmp_path/'config.toml')},'version':'v1','config':{'projects':{str(tmp_path):{'trust_level':'untrusted'}}}}]}
    with pytest.raises(BusinessError,match='不信任'):cp._activate_project_config(ConfigRPC(),tmp_path)


def test_fresh_trust_uses_version_and_only_workspace_key(tmp_path):
    calls=[]
    class ConfigRPC:
        def request(self,method,params):
            calls.append((method,params))
            if len(calls)==1:return {'layers':[{'name':{'type':'user','file':str(tmp_path/'config.toml')},'version':'v1','config':{}}]}
            if method=='config/value/write':return {}
            return {'layers':[{'name':{'type':'project','dotCodexFolder':str(tmp_path/'.codex')}}]}
    cp._activate_project_config(ConfigRPC(),tmp_path)
    p=calls[1][1]
    assert p['expectedVersion']=='v1' and p['value']=='trusted'
    assert p['keyPath']=='projects.'+json.dumps(str(tmp_path),ensure_ascii=False)+'.trust_level'


def test_failed_activation_is_not_ready(tmp_path):
    class ConfigRPC:
        def request(self,method,params):
            return {'layers':[{'name':{'type':'user','file':str(tmp_path/'config.toml')},'version':'v1','config':{}}]}
    with pytest.raises(BusinessError,match='尚未加载'):cp._activate_project_config(ConfigRPC(),tmp_path)


def test_failed_setup_marks_existing_binding_unready(tmp_path,monkeypatch):
    from management import codex_workspace
    workspace=tmp_path/'Codex事务助手';workspace.mkdir()
    cp._save_binding(workspace,{'status':'ready','workspace':str(workspace),'project_id':'old'})
    monkeypatch.setattr(codex_workspace,'prepare_workspace',lambda d:{'workspace':str(workspace)})
    monkeypatch.setattr(cp.ai,'find_codex',lambda *a:'codex.exe')
    class Failed:
        def __init__(self,*args):raise cp.ai.AIError('AI_START_FAILED','not available')
    monkeypatch.setattr(cp.ai,'_AppServer',Failed)
    with pytest.raises(BusinessError):cp.ensure_project(tmp_path,{'enabled':True})
    assert cp.project_binding(workspace) is None
    assert not cp._LOCK.locked()
