"""Asynchronous settings, in-dialog Codex candidates and lossless simple forms."""
from __future__ import annotations
import copy
import os
import time
import uuid
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QDate, QTime, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QPushButton
from management.core import Core
from management.gui_assistant import AssistanceDialog
from management.gui_forms import EntityForm
from management.gui_workflows import SettingsDialog
from management.schemas import BusinessError
from management.storage import encode


@pytest.fixture(scope='session')
def app():
    return QApplication.instance() or QApplication([])


def wait(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('Asynchronous UI did not reach the expected state')


class ControlledBridge:
    epoch, revision = 'synthetic-epoch', 10
    def __init__(self):
        self.queries, self.commands = [], []
    def query(self, name, callback=None, error=None, **params):
        self.queries.append({'name':name,'callback':callback,'error':error,'params':params,'delivered':False})
    def command(self, name, payload, callback=None, error=None, **options):
        self.commands.append({'name':name,'payload':copy.deepcopy(payload),'callback':callback,'error':error,'options':options})
    def take(self, name, *, first=False, **params):
        matches=[q for q in self.queries if q['name']==name and not q['delivered'] and all(q['params'].get(k)==v for k,v in params.items())]
        query=matches[0] if first else matches[-1]
        query['delivered']=True
        return query
    def deliver(self, name, result, **selection):
        query=self.take(name,**selection)
        query['callback'](copy.deepcopy(result))
        return query


class QueuedCoreBridge:
    """UI event-loop scheduling against the real business API in a synthetic root."""
    def __init__(self, core):
        self.core=core
        state=core.query('state')
        self.epoch,self.revision=state['epoch'],state['revision']
        self.pending=0
        self.commands=[]
    def _later(self, operation, callback, error):
        self.pending+=1
        def execute():
            try:
                result=operation()
                if 'epoch' in result:
                    self.epoch,self.revision=result['epoch'],result['revision']
                if callback:
                    callback(result)
            except BusinessError as exc:
                if error:
                    error({'code':exc.code,'message':exc.message,'details':exc.details})
                else:
                    raise
            finally:
                self.pending-=1
        QTimer.singleShot(0,execute)
    def query(self, name, callback=None, error=None, **params):
        self._later(lambda:self.core.query(name,**params),callback,error)
    def command(self, name, payload, callback=None, error=None, **options):
        self.commands.append((name,copy.deepcopy(payload),dict(options)))
        options.setdefault('epoch',self.epoch)
        options.setdefault('expected_revision',self.revision)
        options.setdefault('request_id',str(uuid.uuid4()))
        self._later(lambda:self.core.command(name,payload,**options),callback,error)


def cmd(core,name,payload):
    state=core.query('state')
    return core.command(name,payload,request_id=str(uuid.uuid4()),epoch=state['epoch'],expected_revision=state['revision'])


def preference(revision=10,time='22:10'):
    return {'daily':{'enabled':True,'time':time},'weekly':{'enabled':False,'weekday':5,'time':'18:45'},
            'timezone':'Asia/Shanghai','epoch':'synthetic-epoch','revision':revision}


def save_button(dialog):
    return next(b for b in dialog.findChildren(QPushButton) if b.text()=='保存复盘时间')


def proposal(id='job-a',title='Synthetic candidate',status='awaiting_review'):
    return {'id':id,'kind':'ai','status':status,'input':{'prompt':'Synthetic prompt','context':{}},
            'result':{'summary':'Review before writing: '+title,'unknowns':['Not reported remains unknown'],
                      'actions':[{'command':'create','payload':{'type':'task','title':title},'reason':'Explicit request'}]} if status=='awaiting_review' else None}


@pytest.fixture
def settings_dialog(app,tmp_path):
    bridge=ControlledBridge()
    dialog=SettingsDialog(bridge,{'types':[]},tmp_path/'synthetic')
    dialog.show()
    yield dialog,bridge
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


@pytest.fixture
def assistant_dialog(app):
    bridge=ControlledBridge()
    dialog=AssistanceDialog(bridge,prompt='Please organize this synthetic task',business_date='2030-01-07')
    dialog.show()
    yield dialog,bridge
    dialog.close()
    dialog.deleteLater()
    app.processEvents()




def test_reminder_save_waits_for_initial_asynchronous_read(settings_dialog):
    dialog,bridge=settings_dialog
    assert not save_button(dialog).isEnabled()
    save_button(dialog).click()
    dialog.save_review_times()
    assert bridge.commands==[]
    bridge.deliver('review_preferences',preference())
    assert save_button(dialog).isEnabled()
    assert dialog.daily_time.time().toString('HH:mm')=='22:10'
    assert dialog.weekly_day.currentIndex()==5
    assert not dialog.weekly_time.isEnabled()
    assert not dialog.weekly_enabled.isChecked()


def test_reminder_save_uses_loaded_snapshot_and_disables_duplicate_submission(settings_dialog):
    dialog,bridge=settings_dialog
    bridge.deliver('review_preferences',preference(revision=17))
    dialog.daily_time.setTime(QTime(23,5))
    dialog.weekly_enabled.setChecked(True)
    dialog.weekly_day.setCurrentIndex(6)
    dialog.weekly_time.setTime(QTime(20,25))
    save_button(dialog).click()
    save_button(dialog).click()
    dialog.save_review_times()
    assert len(bridge.commands)==1
    call=bridge.commands[0]
    assert call['name']=='set_review_preferences'
    assert call['payload']=={'daily':{'enabled':True,'time':'23:05'},'weekly':{'enabled':True,'weekday':6,'time':'20:25'},'timezone':'Asia/Shanghai'}
    assert call['options']=={'epoch':'synthetic-epoch','expected_revision':17}
    assert not dialog.daily_time.isEnabled() and not dialog.weekly_day.isEnabled()
    call['callback']({'epoch':'synthetic-epoch','revision':18,'result':{}})
    assert '已保存' in dialog.preference_note.text()
    assert save_button(dialog).isEnabled()


def test_late_preferences_response_cannot_overwrite_newer_read(settings_dialog):
    dialog,bridge=settings_dialog
    dialog.load_review_times()
    bridge.deliver('review_preferences',preference(revision=20,time='23:40'))
    dialog.daily_time.setTime(QTime(23,50))
    bridge.deliver('review_preferences',preference(revision=10,time='19:00'),first=True)
    assert dialog.daily_time.time().toString('HH:mm')=='23:50'
    assert dialog.preferences_revision==20


def test_reminder_failed_save_retains_user_values(settings_dialog):
    dialog,bridge=settings_dialog
    bridge.deliver('review_preferences',preference())
    dialog.daily_time.setTime(QTime(23,15))
    save_button(dialog).click()
    bridge.commands[0]['error']({'code':'revision_conflict','message':'Settings changed elsewhere; read again'})
    assert dialog.daily_time.time().toString('HH:mm')=='23:15'
    assert 'changed' in dialog.message.text()
    assert bridge.commands[0]['payload']['daily']['time']=='23:15'


def test_real_settings_round_trip_reuses_two_schedules_and_does_not_enable_defaults(app,tmp_path):
    core=Core(tmp_path/'settings-core')
    bridge=QueuedCoreBridge(core)
    dialog=SettingsDialog(bridge,core.query('capabilities'),core.root)
    reopened=None
    try:
        wait(app,lambda:bridge.pending==0)
        assert not dialog.daily_enabled.isChecked() and not dialog.weekly_enabled.isChecked()
        assert core.query('list',type='schedule')['total']==0
        dialog.daily_enabled.setChecked(True)
        dialog.daily_time.setTime(QTime(22,35))
        dialog.weekly_enabled.setChecked(True)
        dialog.weekly_day.setCurrentIndex(4)
        dialog.weekly_time.setTime(QTime(20,10))
        save_button(dialog).click()
        wait(app,lambda:bridge.pending==0)
        assert '已保存' in dialog.preference_note.text(),dialog.message.text()
        schedules=core.query('list',type='schedule')['items']
        ids={s['id'] for s in schedules}
        assert len(ids)==2
        save_button(dialog).click()
        wait(app,lambda:bridge.pending==0)
        assert {s['id'] for s in core.query('list',type='schedule')['items']}==ids
        reopened=SettingsDialog(bridge,core.query('capabilities'),core.root)
        wait(app,lambda:bridge.pending==0)
        assert reopened.daily_time.time().toString('HH:mm')=='22:35'
        assert reopened.weekly_day.currentIndex()==4
        assert reopened.weekly_time.time().toString('HH:mm')=='20:10'
    finally:
        dialog.close()
        if reopened:
            reopened.close()














def test_simplified_form_edit_preserves_hidden_fields_status_and_unlisted_data(app,tmp_path):
    core=Core(tmp_path/'form-core')
    entity=cmd(core,'create',{'type':'task','title':'Original','status':'blocked','data':{
        'completion_gate':'Explicit gate','estimated_minutes':35,'priority':'high','notes':'Keep existing note',
        'custom_extension_data':{'source':'retained','count':7}}})['result']['entity']
    bridge=QueuedCoreBridge(core)
    dialog=EntityForm(bridge,core.query('capabilities'),entity=entity)
    try:
        assert not dialog.form.isRowVisible(dialog.status_box)
        assert not dialog.field_layout.isRowVisible(dialog.fields['priority'])
        dialog.title_edit.setText('Renamed without data loss')
        dialog.more_fields.click()
        dialog.more_fields.click()
        dialog.save()
        wait(app,lambda:bridge.pending==0)
        assert dialog.result()==QDialog.DialogCode.Accepted,dialog.error_label.text()
        updated=core.query('get',id=entity['id'])['entity']
        assert updated['title']=='Renamed without data loss'
        assert updated['status']=='blocked'
        for key,value in entity['data'].items():
            assert updated['data'][key]==value
    finally:
        dialog.close()


def test_new_simple_form_does_not_invent_unknown_date_or_minutes(app,tmp_path):
    core=Core(tmp_path/'new-form')
    bridge=QueuedCoreBridge(core)
    dialog=EntityForm(bridge,core.query('capabilities'),default_type='task')
    try:
        dialog.title_edit.setText('Only a known title')
        dialog.save()
        wait(app,lambda:bridge.pending==0)
        assert dialog.result()==QDialog.DialogCode.Accepted,dialog.error_label.text()
        entity=core.query('list',type='task')['items'][0]
        assert 'estimated_minutes' not in entity['data']
        assert 'due_date' not in entity['data']
        assert entity['status']=='active'
    finally:
        dialog.close()



def test_reminder_reload_during_save_cannot_overwrite_saved_value(settings_dialog):
    dialog,bridge=settings_dialog
    bridge.deliver('review_preferences',preference(revision=10,time='22:10'))
    dialog.daily_time.setTime(QTime(23,25))
    save_button(dialog).click()
    dialog.load_review_times()
    bridge.commands[0]['callback']({'epoch':'synthetic-epoch','revision':11,'result':{}})
    for query in bridge.queries:
        if query['name']=='review_preferences' and not query['delivered']:
            query['delivered']=True
            query['callback'](preference(revision=10,time='22:10'))
    assert dialog.daily_time.time().toString('HH:mm')=='23:25'
    assert dialog.preferences_revision==11




def test_disabled_extension_field_survives_simplified_builtin_edit(app,tmp_path):
    core=Core(tmp_path/'extension-edit')
    cmd(core,'install_module',{'manifest':{'id':'ux_test','version':1,'fields':[
        {'id':'external_score','label':'外部记录分数','target_type':'task','type':'integer'}]}})
    entity=cmd(core,'create',{'type':'task','title':'Original','data':{'external_score':7,'notes':'Confirmed source'}})['result']['entity']
    cmd(core,'disable_module',{'id':'ux_test'})
    bridge=QueuedCoreBridge(core)
    dialog=EntityForm(bridge,core.query('capabilities'),entity=entity)
    try:
        dialog.more_fields.click()
        assert not dialog.fields['external_score'].isEnabled()
        dialog.title_edit.setText('Renamed with module disabled')
        dialog.save()
        wait(app,lambda:bridge.pending==0)
        assert dialog.result()==QDialog.DialogCode.Accepted,dialog.error_label.text()
        submitted=bridge.commands[-1][1]['patch']['data']
        assert 'external_score' not in submitted
        result=core.query('get',id=entity['id'])['entity']
        assert result['data']['external_score']==7
        assert result['data']['notes']=='Confirmed source'
    finally:
        dialog.close()


def conversation_result(job=None,state='available',active=None,text='Synthetic reply'):
    return {'conversation':{'id':'conversation-a','scope':{'kind':'daily_plan','date':'2030-01-07'},'active_job_id':active,'current_proposal_job_id':job,'source_ids':[],'sources':[]},
            'messages':[{'id':'reply-'+str(job),'seq':2,'role':'assistant','text':text,'job_id':job,'state':'completed','proposal_state':state}], 'has_more':False}


def ready_conversation(dialog,bridge):
    bridge.deliver('settings',{'settings':{'ai':{'enabled':True}}})
    bridge.deliver('conversation',{'conversation':None,'messages':[],'has_more':False})


def ready_candidate(dialog,bridge,id='job-a'):
    ready_conversation(dialog,bridge)
    dialog.send.click()
    assert bridge.commands[-1]['name']=='send_message'
    bridge.commands[-1]['callback']({'result':{'job':{'id':id},'conversation':{'active_job_id':id}}})
    bridge.deliver('conversation',conversation_result(id))
    bridge.deliver('job',{'job':proposal(id)},id=id)
    assert dialog.apply.isEnabled()


def assistant_text(dialog):
    from PySide6.QtWidgets import QTextBrowser
    return '\n'.join(w.toPlainText() for w in dialog.history_body.findChildren(QTextBrowser))


def test_codex_candidate_and_confirmation_stay_under_same_reply(assistant_dialog):
    dialog,bridge=assistant_dialog;ready_candidate(dialog,bridge)
    assert 'Synthetic candidate' in assistant_text(dialog)
    assert 'Not reported remains unknown' in assistant_text(dialog)
    assert dialog.apply.parent() is not dialog
    dialog.apply.click()
    assert bridge.commands[-1]['name']=='apply_proposal'
    assert bridge.commands[-1]['payload']=={'id':'job-a'}
    assert not dialog.apply.isEnabled()
    bridge.commands[-1]['callback']({'result':{'job_id':'job-a'}})
    bridge.deliver('conversation',conversation_result('job-a',state='applied'))
    bridge.deliver('job',{'job':proposal('job-a',status='applied')},id='job-a')
    assert not dialog.apply.isVisible()


def test_new_current_candidate_needs_its_complete_content_before_apply(assistant_dialog):
    dialog,bridge=assistant_dialog;ready_candidate(dialog,bridge)
    dialog.poll();bridge.deliver('conversation',conversation_result('job-b'))
    assert dialog.job_id=='job-b' and not dialog.apply.isVisible()
    dialog.apply_result()
    assert not any(c['name']=='apply_proposal' for c in bridge.commands)
    bridge.deliver('job',{'job':proposal('job-b','New candidate read in full')},id='job-b')
    assert 'New candidate read in full' in assistant_text(dialog)
    dialog.apply.click();assert bridge.commands[-1]['payload']=={'id':'job-b'}


def test_late_old_conversation_error_does_not_replace_new_send(assistant_dialog):
    dialog,bridge=assistant_dialog;ready_conversation(dialog,bridge)
    dialog.poll();old=bridge.take('conversation')
    dialog.start_job();bridge.commands[-1]['callback']({'result':{'job':{'id':'new-job'}}})
    old['error']({'message':'Old error must stay invisible'})
    assert dialog.active_job_id=='new-job'
    assert 'Old error' not in dialog.status.text()
    assert dialog.send.text()=='停止'


def test_unconfigured_chat_has_one_settings_path_and_no_legacy_toolbar(app):
    bridge=ControlledBridge();opened=[];dialog=AssistanceDialog(bridge,on_open_settings=lambda:opened.append(True))
    try:
        bridge.deliver('settings',{'settings':{'ai':{'enabled':False}}})
        bridge.deliver('conversation',{'conversation':None,'messages':[]})
        assert not dialog.send.isEnabled()
        labels=[w.text() for w in dialog.findChildren(QPushButton)]
        assert '连接 Codex' in labels
        assert all('复制到' not in t and '继续上次' not in t for t in labels)
        dialog.connect_button.click();assert opened==[True] and bridge.commands==[]
    finally:dialog.close()


def test_plain_source_text_is_not_html_and_cancelled_candidate_cannot_apply(assistant_dialog):
    dialog,bridge=assistant_dialog;ready_conversation(dialog,bridge)
    dialog.poll();bridge.deliver('conversation',conversation_result(None,state='none',text='<b>Literal source text</b>'))
    assert '<b>Literal source text</b>' in assistant_text(dialog)
    dialog.apply_result();assert bridge.commands==[]
    dialog.poll();bridge.deliver('conversation',conversation_result('old',state='cancelled'))
    bridge.deliver('job',{'job':proposal('old',status='cancelled')},id='old')
    dialog.apply_result();assert bridge.commands==[]


def test_late_candidate_after_apply_cannot_reenable_old_proposal(assistant_dialog):
    dialog,bridge=assistant_dialog;ready_candidate(dialog,bridge)
    dialog.load_proposal('job-a');old=bridge.take('job',id='job-a')
    dialog.apply.click();bridge.commands[-1]['callback']({'result':{'job_id':'job-a'}})
    old['callback']({'job':proposal('job-a')})
    assert not dialog.apply.isEnabled()


def test_real_candidate_confirmation_and_same_scope_reopen_without_model(app,tmp_path):
    from management import conversations
    core=Core(tmp_path/'conversation-ui-core');cmd(core,'settings',{'settings':{'ai':{'enabled':True}}})
    bridge=QueuedCoreBridge(core);saved=[]
    dialog=AssistanceDialog(bridge,prompt='Create one synthetic task',scope={'kind':'daily_plan','date':'2030-01-07'},on_saved=saved.append)
    reopened=None
    try:
        wait(app,lambda:bridge.pending==0)
        dialog.start_job();wait(app,lambda:bridge.pending==0)
        job_id=dialog.active_job_id;assert job_id
        dialog.timer.stop();assert core.query('list',type='task')['total']==0
        result=proposal(job_id,title='Confirmed synthetic result')['result']
        with core.store.connect() as c:
            c.execute("UPDATE jobs SET status='awaiting_review',result=? WHERE id=?",(encode(result),job_id))
            job=dict(c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
            conversations.complete_job(core,c,job,result)
        dialog.poll();wait(app,lambda:bridge.pending==0)
        assert 'Confirmed synthetic result' in assistant_text(dialog)
        assert core.query('list',type='task')['total']==0
        dialog.apply_result();wait(app,lambda:bridge.pending==0)
        assert len(saved)==1
        assert core.query('list',type='task')['items'][0]['title']=='Confirmed synthetic result'
        assert core.query('job',id=job_id)['job']['status']=='applied'
        dialog.close()
        reopened=AssistanceDialog(bridge,scope={'kind':'daily_plan','date':'2030-01-07'})
        wait(app,lambda:bridge.pending==0)
        assert 'Create one synthetic task' in assistant_text(reopened)
        assert 'Confirmed synthetic result' in assistant_text(reopened)
        assert len([c for c in bridge.commands if c[0]=='send_message'])==1
    finally:
        dialog.close()
        if reopened:reopened.close()
