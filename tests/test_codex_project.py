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


class SetupRPC(RPC):
    def __init__(self, pages, selected, *, instruction_path=None):
        super().__init__(pages, selected)
        self.workspace = Path(selected['roots'][0]['path'])
        self.instruction_path = (self.workspace / '.codex/management-instructions.md'
                                 if instruction_path is None else instruction_path)
        self.closed = False
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def close(self):
        self.closed = True

    def request(self, method, params):
        if method != 'config/read':
            return super().request(method, params)
        self.calls.append((method, params))
        return {'config': {'model_instructions_file': str(self.instruction_path)},
                'layers': [
                    {'name': {'type': 'project', 'dotCodexFolder': str(self.workspace / '.codex')},
                     'disabledReason': None,
                     'config': {'model_instructions_file': str(self.workspace / '.codex/management-instructions.md')}},
                    {'name': {'type': 'user', 'file': str(self.workspace.parent / 'config.toml')},
                     'version': 'synthetic-v1',
                     'config': {'projects': {str(self.workspace): {'trust_level': 'trusted'}}}},
                ]}


def prepare_instructions(workspace):
    path = workspace / '.codex/management-instructions.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('Synthetic managed project instructions.\n', encoding='utf-8')
    return path


def test_paginate_before_desktop_handoff(tmp_path):
    target=project(tmp_path)
    rpc=RPC([{'data': [], 'nextCursor': 'page2'}, {'data':[target]}],target)
    assert cp._matching_projects(rpc,tmp_path)==[target]
    assert rpc.calls[1][1]['cursor']=='page2'


def test_desktop_launch_creates_through_native_registration(tmp_path,monkeypatch):
    target=project(tmp_path)
    rpc=RPC([{'data':[]},{'data':[target]}],target)
    launches=[];monkeypatch.setattr(cp,'open_desktop_workspace',launches.append)
    result,created,opened=cp._desktop_project(rpc,tmp_path)
    assert result==target and created and opened and launches==[tmp_path]
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


@pytest.mark.parametrize('bound,duplicate', [(False,False),(True,False),(True,True)])
def test_existing_project_is_verified_without_opening_composer(tmp_path,monkeypatch,bound,duplicate):
    target=project(tmp_path)
    if bound:
        cp._save_binding(tmp_path,{'status':'ready','workspace':str(tmp_path),'project_id':target['id']})
    rows=[target,project(tmp_path,'other')] if duplicate else [target]
    rpc=RPC([{'data':rows}],target)
    monkeypatch.setattr(cp,'open_desktop_workspace',lambda p:pytest.fail('Existing project must not open a new composer'))
    assert cp._desktop_project(rpc,tmp_path)==(target,False,False)
    assert rpc.calls==[('project/list',{'limit':100}),('project/read',{'projectId':target['id']})]


def test_stale_binding_reuses_unique_existing_project(tmp_path,monkeypatch):
    target=project(tmp_path)
    cp._save_binding(tmp_path,{'status':'ready','workspace':str(tmp_path),'project_id':'removed'})
    rpc=RPC([{'data':[target]}],target)
    monkeypatch.setattr(cp,'open_desktop_workspace',lambda p:pytest.fail('Unique project must be reused'))
    assert cp._desktop_project(rpc,tmp_path)==(target,False,False)


def test_ambiguous_existing_projects_never_open_another_composer(tmp_path,monkeypatch):
    target=project(tmp_path)
    rpc=RPC([{'data':[target,project(tmp_path,'other')]}],target)
    monkeypatch.setattr(cp,'open_desktop_workspace',lambda p:pytest.fail('Ambiguity must not create another project'))
    with pytest.raises(BusinessError,match='多个项目'):cp._desktop_project(rpc,tmp_path)
    assert len(rpc.calls)==1


@pytest.mark.parametrize('changed', ['root','id'])
def test_existing_project_still_requires_matching_readback(tmp_path,monkeypatch,changed):
    target=project(tmp_path)
    readback=project(tmp_path/'moved') if changed=='root' else project(tmp_path,'different')
    rpc=RPC([{'data':[target]}],readback)
    monkeypatch.setattr(cp,'open_desktop_workspace',lambda p:pytest.fail('Existing project must be read directly'))
    with pytest.raises(BusinessError):cp._desktop_project(rpc,tmp_path)


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
    from management import codex_workspace, desktop_seed
    workspace=tmp_path/'Codex事务助手';workspace.mkdir()
    cp._save_binding(workspace,{'status':'ready','workspace':str(workspace),'project_id':'old'})
    monkeypatch.setattr(codex_workspace,'prepare_workspace',lambda d:{'workspace':str(workspace)})
    class Failed:
        def __init__(self,*args,**kwargs):raise cp.ai.AIError('AI_START_FAILED','not available')
    monkeypatch.setattr(desktop_seed,'PlainAppServer',Failed)
    with pytest.raises(BusinessError):cp.ensure_project(tmp_path,{'enabled':True})
    assert cp.project_binding(workspace) is None
    assert not cp._LOCK.locked()


@pytest.mark.parametrize('existing',[False,True])
def test_ensure_project_reports_whether_desktop_open_was_requested(tmp_path,monkeypatch,existing):
    from management import codex_workspace, desktop_seed
    workspace=tmp_path/'Codex事务助手';workspace.mkdir()
    prepare_instructions(workspace)
    target=project(workspace)
    pages=[{'data':[target]}] if existing else [{'data':[]},{'data':[target]}]
    rpc=SetupRPC(pages,target)
    monkeypatch.setattr(codex_workspace,'prepare_workspace',lambda d:{'workspace':str(workspace)})
    monkeypatch.setattr(desktop_seed,'PlainAppServer',lambda *a,**kwargs:rpc)
    monkeypatch.setattr(cp,'_probe_mcp',lambda *a:None)
    opened=[];monkeypatch.setattr(cp,'open_desktop_workspace',opened.append)
    result=cp.ensure_project(tmp_path,{'enabled':True})
    assert result['desktop_open_requested'] is (not existing)
    assert result['created'] is (not existing)
    assert result['instructions_verified'] is True
    assert opened==([] if existing else [workspace])
    assert cp.project_binding(workspace)['desktop_open_requested'] is (not existing)
    assert rpc.entered and rpc.closed and not cp._LOCK.locked()


def test_active_project_layer_with_overridden_effective_instructions_is_rejected(tmp_path,monkeypatch):
    from management import codex_workspace, desktop_seed
    workspace=tmp_path/'Codex事务助手';workspace.mkdir()
    expected=prepare_instructions(workspace)
    other=tmp_path/'other-instructions.md';other.write_text('Synthetic override.',encoding='utf-8')
    cp._save_binding(workspace,{'status':'ready','workspace':str(workspace),'project_id':'old'})
    rpc=SetupRPC([{'data':[project(workspace)]}],project(workspace),instruction_path=other)
    monkeypatch.setattr(codex_workspace,'prepare_workspace',lambda d:{'workspace':str(workspace)})
    monkeypatch.setattr(desktop_seed,'PlainAppServer',lambda *a,**kwargs:rpc)
    monkeypatch.setattr(cp,'_probe_mcp',lambda *a:pytest.fail('Instruction validation must precede the MCP probe'))
    monkeypatch.setattr(cp,'open_desktop_workspace',lambda *a:pytest.fail('Invalid rules must not open a composer'))
    with pytest.raises(BusinessError,match='新协助规则') as error:
        cp.ensure_project(tmp_path,{'enabled':True})
    assert error.value.code=='codex_project_incomplete'
    assert expected.is_file() and cp.project_binding(workspace) is None
    assert [method for method,_ in rpc.calls]==['config/read','config/read']
    assert rpc.entered and rpc.closed and not cp._LOCK.locked()


def test_effective_instruction_path_without_local_file_is_rejected(tmp_path):
    rpc=SetupRPC([],project(tmp_path))
    with pytest.raises(BusinessError,match='新协助规则'):
        cp._verify_project_instructions(rpc,tmp_path)
