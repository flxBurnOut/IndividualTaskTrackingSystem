"""Model discovery and settings picker; synthetic catalogs, no model inference."""
import copy
import os
import threading

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication, QComboBox

from management import ai, model_catalog, __version__
from management.gui_workflows import SettingsDialog


def remote(model='exact-model-v1', *, id='catalog-row-id', default=False):
    return {'id': id, 'model': model, 'displayName': 'Friendly Model', 'description': 'Synthetic catalog entry',
            'hidden': False, 'isDefault': default, 'inputModalities': ['text','image']}


class FakeRPC:
    instances = []
    pages = []
    error = None
    def __init__(self, *args):
        self.requests=[]; self.closed=False; self.index=0
        self.instances.append(self)
    def request(self, method, params):
        self.requests.append((method, copy.deepcopy(params)))
        if method=='initialize':return {}
        assert method=='model/list', 'Discovery must never request inference, account data, or quota'
        if self.error:raise ai.AIError(self.error,'Synthetic disconnected catalog')
        result=self.pages[self.index];self.index+=1
        return copy.deepcopy(result)
    def send(self, value):
        assert value['method']=='initialized'
    def close(self):self.closed=True


@pytest.fixture
def rpc(monkeypatch):
    monkeypatch.setattr(ai,'find_codex',lambda *a:'synthetic.exe')
    monkeypatch.setattr(ai,'_AppServer',FakeRPC)
    monkeypatch.setattr(FakeRPC,'error',None)
    monkeypatch.setattr(FakeRPC,'pages',[{'data':[remote()], 'nextCursor':None}])
    return FakeRPC


def test_catalog_works_before_enabling_and_uses_exact_model_not_row_id(rpc):
    settings={'ai':{'enabled':False,'model':'unchanged'}}
    original=copy.deepcopy(settings)
    result=model_catalog.list_models(settings)
    assert result['models'][0]['model']=='exact-model-v1'
    assert result['models'][0]['id']=='catalog-row-id'
    assert result['models'][0]['display_name']=='Friendly Model'
    assert settings==original
    assert rpc.instances[-1].requests==[('initialize',{'clientInfo':{'name':'personal_management_model_picker','version':__version__},'capabilities':{'experimentalApi':True}}),('model/list',{'limit':50,'includeHidden':False})]
    assert rpc.instances[-1].closed


def test_catalog_pagination_dedup_and_hidden_filter(rpc,monkeypatch):
    a=remote('one');b=remote('two',id='row2',default=True);hidden={**remote('secret'),'hidden':True}
    monkeypatch.setattr(FakeRPC,'pages',[{'data':[a,hidden],'nextCursor':'page-2'},{'data':[a,b],'nextCursor':None}])
    result=model_catalog.list_models({})
    assert [m['model'] for m in result['models']]==['one','two']
    assert result['recommended_model']=='two'
    assert rpc.instances[-1].requests[-1][1]['cursor']=='page-2'


@pytest.mark.parametrize('pages,code',[
    ([{'data':[],'nextCursor':'same'},{'data':[],'nextCursor':'same'}],'AI_MODEL_CATALOG_INVALID'),
    ([{'data':[{'model':'missing-fields'}]}],'AI_MODEL_CATALOG_INVALID'),
    ([{'data':[], 'nextCursor':42}],'AI_MODEL_CATALOG_INVALID'),
    ([{'data':'not-array'}],'AI_MODEL_CATALOG_INVALID'),
])
def test_catalog_invalid_response_never_returns_partial_list(rpc,monkeypatch,pages,code):
    monkeypatch.setattr(FakeRPC,'pages',pages)
    with pytest.raises(ai.AIError) as exc:model_catalog.list_models({})
    assert exc.value.code==code and rpc.instances[-1].closed


def test_catalog_page_and_model_limits(rpc,monkeypatch):
    monkeypatch.setattr(model_catalog,'MAX_PAGES',1)
    monkeypatch.setattr(FakeRPC,'pages',[{'data':[],'nextCursor':'more'}])
    with pytest.raises(ai.AIError) as exc:model_catalog.list_models({})
    assert exc.value.code=='AI_MODEL_CATALOG_LIMIT'
    monkeypatch.setattr(model_catalog,'MAX_MODELS',1)
    monkeypatch.setattr(FakeRPC,'pages',[{'data':[remote('one'),remote('two')]}])
    with pytest.raises(ai.AIError) as exc:model_catalog.list_models({})
    assert exc.value.code=='AI_MODEL_CATALOG_LIMIT'


def test_cancel_and_busy_do_not_spawn(rpc):
    previous=len(rpc.instances);cancel=threading.Event();cancel.set()
    with pytest.raises(ai.AIError) as exc:model_catalog.list_models({},cancel)
    assert exc.value.code=='AI_CANCELLED'
    model_catalog._DISCOVERY_LOCK.acquire()
    try:
        with pytest.raises(ai.AIError) as exc:model_catalog.list_models({})
        assert exc.value.code=='AI_MODEL_CATALOG_BUSY'
    finally:model_catalog._DISCOVERY_LOCK.release()
    assert len(rpc.instances)==previous


def test_catalog_failure_preserves_settings_and_releases_process(rpc,monkeypatch):
    monkeypatch.setattr(FakeRPC,'error','AI_REQUEST_REJECTED')
    config={'model':'saved-offline-model'}
    with pytest.raises(ai.AIError):model_catalog.list_models(config)
    assert config=={'model':'saved-offline-model'} and rpc.instances[-1].closed
    assert model_catalog._DISCOVERY_LOCK.acquire(blocking=False)
    model_catalog._DISCOVERY_LOCK.release()


class Bridge:
    epoch='synthetic';revision=1
    def __init__(self):self.queries=[];self.commands=[]
    def query(self,name,callback=None,error=None,**params):
        self.queries.append({'name':name,'callback':callback,'error':error,'params':params,'delivered':False})
    def command(self,name,payload,callback=None,error=None,**options):
        self.commands.append({'name':name,'payload':copy.deepcopy(payload),'callback':callback,'error':error})
    def take(self,name):
        item=next(q for q in self.queries if q['name']==name and not q['delivered']);item['delivered']=True;return item


@pytest.fixture(scope='session')
def app():return QApplication.instance() or QApplication([])


@pytest.fixture
def picker(app,tmp_path):
    bridge=Bridge();dialog=SettingsDialog(bridge,{'types':[]},tmp_path/'synthetic-picker')
    yield dialog,bridge
    dialog.close();dialog.deleteLater();app.processEvents()


def load(dialog,bridge,model='saved-model',path='old.exe'):
    bridge.take('settings')['callback']({'settings':{'ai':{'enabled':True,'model':model,'executable':path}},'epoch':'synthetic','revision':1})


def catalog(*models):
    return {'models':[{'model':model,'id':'row-'+model,'display_name':'Display '+model,'description':'Synthetic','is_default':i==0} for i,model in enumerate(models)]}


def test_picker_noneditable_preserves_unverified_original_and_exact_save(picker):
    dialog,bridge=picker
    assert isinstance(dialog.model,QComboBox) and not dialog.model.isEditable()
    dialog.save_ai();assert not bridge.commands
    load(dialog,bridge)
    assert dialog.model.currentData()=='saved-model' and '未验证' in dialog.model.currentText()
    dialog.tabs.setCurrentIndex(dialog._provider_tab)
    bridge.take('codex_models')['callback'](catalog('exact-provider-model'))
    assert dialog.model.currentData()=='saved-model'
    assert '不在当前列表' in dialog.model_note.text()
    dialog.model.setCurrentIndex(dialog.model.findData('exact-provider-model'))
    dialog.save_ai()
    assert bridge.commands[-1]['payload']['ai']['model']=='exact-provider-model'


def test_default_remains_follow_local_config_not_catalog_recommendation(picker):
    dialog,bridge=picker;load(dialog,bridge,model='')
    dialog.tabs.setCurrentIndex(dialog._provider_tab)
    bridge.take('codex_models')['callback'](catalog('recommended'))
    assert dialog.model.currentData()==''
    assert '跟随本机' in dialog.model.currentText()
    dialog.save_ai()
    assert bridge.commands[-1]['payload']['ai']['model']==''


def test_offline_refresh_keeps_original_selection_and_allows_saving(picker):
    dialog,bridge=picker;load(dialog,bridge)
    dialog.tabs.setCurrentIndex(dialog._provider_tab)
    bridge.take('codex_models')['error']({'message':'Synthetic offline'})
    assert dialog.model.currentData()=='saved-model'
    assert '未切换模型' in dialog.model_note.text()
    dialog.save_ai()
    assert bridge.commands[-1]['payload']['ai']['model']=='saved-model'


def test_old_path_response_cannot_populate_new_path_catalog(picker):
    dialog,bridge=picker;load(dialog,bridge)
    dialog.tabs.setCurrentIndex(dialog._provider_tab)
    old=bridge.take('codex_models')
    dialog.executable.setText('new.exe');dialog.refresh_models()
    assert not [q for q in bridge.queries if q['name']=='codex_models' and not q['delivered']]
    old['callback'](catalog('wrong-old-model'))
    assert dialog.model.findData('wrong-old-model')<0
    current=bridge.take('codex_models');assert current['params']['executable']=='new.exe'
    current['callback'](catalog('new-path-model'))
    assert dialog.model.findData('new-path-model')>=0
    assert dialog.model.currentData()=='saved-model'


def test_stale_path_error_ignored_and_next_path_still_read(picker):
    dialog,bridge=picker;load(dialog,bridge)
    dialog.tabs.setCurrentIndex(dialog._provider_tab);old=bridge.take('codex_models')
    dialog.executable.setText('new.exe')
    old['error']({'message':'Old path error must not display'})
    assert 'Old path error' not in dialog.model_note.text()
    assert bridge.take('codex_models')['params']['executable']=='new.exe'


def test_refresh_does_not_overwrite_selection_changed_while_loading(picker):
    dialog,bridge=picker;load(dialog,bridge,model='one')
    dialog.tabs.setCurrentIndex(dialog._provider_tab)
    bridge.take('codex_models')['callback'](catalog('one','two'))
    dialog.refresh_models();request=bridge.take('codex_models')
    dialog.model.setCurrentIndex(dialog.model.findData('two'))
    request['callback'](catalog('one','two','three'))
    assert dialog.model.currentData()=='two'


def test_closed_dialog_ignores_late_model_response(picker):
    dialog,bridge=picker;load(dialog,bridge)
    dialog.tabs.setCurrentIndex(dialog._provider_tab);request=bridge.take('codex_models')
    dialog.reject();request['callback'](catalog('late'))
    assert dialog.model.findData('late')<0


def test_explicit_recurring_candidate_allowed_only_in_callers_scope():
    import json
    payload={'anchor_id':'existing-synthetic-anchor','title':'Synthetic preparation','content':'Review supplied notes','completion_gate':'Answer three supplied questions','days_before':1}
    proposed={'summary':'Synthetic rule candidate','unknowns':[],'sources':['existing-synthetic-anchor'],'actions':[{'command':'set_recurring_rule','payload_json':json.dumps(payload),'reason':'User explicitly requested recurring preparation'}]}
    result=ai.validate_proposal(proposed,{'set_recurring_rule'})
    assert result['actions'][0]['payload']==payload
    with pytest.raises(ai.AIError) as exc:ai.validate_proposal(proposed,{'create'})
    assert exc.value.code=='AI_SCOPE_ERROR'
