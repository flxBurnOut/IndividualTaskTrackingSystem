import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtWidgets import QApplication,QLabel,QProgressBar
from PySide6.QtCore import QCoreApplication,QEvent
from management.core import Core
from management.gui_recovery import RecoveryTaskDialog,RecoveryProgressDialog,RecoveryPanel
from test_ux_workflows_v2 import ControlledBridge,QueuedCoreBridge,cmd,wait

@pytest.fixture(scope='session')
def app():return QApplication.instance() or QApplication([])
@pytest.fixture
def core(tmp_path):return Core(tmp_path/'recovery-ui')
def course(core):return cmd(core,'create',{'type':'course','title':'Synthetic Course'})['result']['entity']
def register(core,owner,**extra):
    p={'course_id':owner['id'],'title':'Missed lectures','completion_gate':'Watch and solve exercises','unit':'lessons','total_quantity':8,'completed_quantity':3,'source_text':'I explicitly report 3 of 8','reason':'self_reported'};p.update(extra)
    return cmd(core,'set_recovery_task',p)['result']['entity']
def close(dialog):dialog.finished_ok=True;dialog.saving=False;dialog.close()


@pytest.mark.parametrize('font_size', [13, 20])
def test_recovery_panel_long_task_title_wraps_in_narrow_course_column(app, font_size):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QScrollArea, QPushButton
    from management.gui_theme import apply_appearance, current_appearance

    previous = current_appearance()
    apply_appearance(app, {**previous, 'font_size': font_size})
    bridge = ControlledBridge()
    panel = RecoveryPanel(bridge, {'id': 'course', 'title': '演示课程'})
    title = '需要补完的长课程名称与对应练习，保留完整的任务范围。' * 5
    bridge.deliver('recovery_summary', {'items': [
        {'id': 'catchup', 'title': title, 'data': {}, 'version': 1,
         'progress': {'completed_quantity': 2, 'total_quantity': 8, 'unit': '节课'}},
    ], 'total': 1})
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(panel)
    try:
        scroll.show()
        for width in (900, 469, 900):
            scroll.resize(width, 680)
            QTest.qWait(70)
            assert scroll.horizontalScrollBar().maximum() == 0
            labels = [w for w in panel.findChildren(QLabel) if w.text() == title]
            assert len(labels) == 1 and labels[0].wordWrap()
            assert labels[0].height() >= labels[0].heightForWidth(labels[0].width())
            for control in panel.findChildren(QPushButton):
                if control.isVisible():
                    assert control.width() >= control.sizeHint().width()
                    assert control.parentWidget().rect().contains(control.geometry())
        assert bridge.commands == []
    finally:
        scroll.close()
        scroll.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        apply_appearance(app, previous)

def test_form_registers_three_of_eight_without_plan_or_completion(app,core):
    owner=course(core);bridge=QueuedCoreBridge(core);dialog=RecoveryTaskDialog(bridge,owner)
    try:
        dialog.title.setText('Catch up lectures 1-8');dialog.gate.setPlainText('View all lectures and solve practice');dialog.total.setValue(8);dialog.completed.setValue(3);dialog.source.setPlainText('I have completed three of eight lectures.')
        dialog.save();wait(app,lambda:bridge.pending==0)
        task=core.query('recovery_summary',course_id=owner['id'])['items'][0]
        assert task['progress']['completed_quantity']==3 and task['progress']['ratio']==3/8
        assert not task['progress']['completion_confirmed']
        assert core.query('list',type='task')['total']==1 and core.query('list',type='plan')['total']==0
        assert core.query('list',type='feedback')['items'][0]['data']['dimensions']=={}
    finally:close(dialog)

def test_progress_reaching_quantity_does_not_confirm_entire_gate(app,core):
    owner=course(core);task=register(core,owner);bridge=QueuedCoreBridge(core);dialog=RecoveryProgressDialog(bridge,task)
    try:
        wait(app,lambda:dialog.loaded)
        dialog.completed.setValue(8);dialog.source.setPlainText('I finished viewing all eight; practice still pending.')
        dialog.save();wait(app,lambda:bridge.pending==0)
        p=core.query('recovery_summary',task_id=task['id'])['items'][0]['progress']
        assert p['ratio']==1 and p['completion'] is None and not p['completion_confirmed']
    finally:close(dialog)

def test_explicit_incomplete_and_quantity_correction_preserve_history(app,core):
    owner=course(core);task=register(core,owner);bridge=QueuedCoreBridge(core);dialog=RecoveryProgressDialog(bridge,task)
    try:
        wait(app,lambda:dialog.loaded);dialog.completed.setValue(2);dialog.source.setPlainText('One lecture had been counted twice.')
        dialog.save();assert not bridge.commands
        dialog.correction.setChecked(True);dialog.correction_reason.setText('Duplicate was included before');dialog.completion.setCurrentIndex(1)
        dialog.save();wait(app,lambda:bridge.pending==0)
        p=core.query('recovery_summary',task_id=task['id'])['items'][0]['progress']
        assert p['completed_quantity']==2 and p['completion']=='incomplete'
        assert core.query('list',type='feedback')['total']==2
    finally:close(dialog)

def test_unknown_quantities_render_without_fake_zero_bar(app,core):
    owner=course(core);register(core,owner,total_quantity=None,completed_quantity=None)
    bridge=QueuedCoreBridge(core);panel=RecoveryPanel(bridge,owner)
    try:
        wait(app,lambda:bridge.pending==0);QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        assert panel.findChildren(QProgressBar)==[]
        assert any('待确认' in w.text() for w in panel.findChildren(QLabel))
        assert bridge.commands==[]
    finally:panel.close()

def test_progress_old_response_cannot_replace_new_date_snapshot(app):
    bridge=ControlledBridge();task={'id':'t','version':1,'title':'Task','type':'task','data':{}}
    dialog=RecoveryProgressDialog(bridge,task)
    try:
        old=bridge.take('recovery_summary');dialog.day.setDate(dialog.day.date().addDays(-1));current={**task,'version':3,'progress':{'completed_quantity':2,'unit':'lessons','total_quantity':8,'latest_feedback_id':'f-new'}}
        bridge.deliver('recovery_summary',{'items':[current]});old['callback']({'items':[{**task,'progress':{'completed_quantity':99,'latest_feedback_id':'old'}}]})
        assert dialog.task['version']==3 and dialog.completed.value()==2
        dialog.completed.setValue(4);dialog.source.setPlainText('Explicit cumulative four');dialog.save()
        assert bridge.commands[-1]['payload']['version']==3
        bridge.commands[-1]['error']({'code':'revision_conflict','message':'Changed elsewhere'})
        assert dialog.completed.value()==4 and dialog.source.toPlainText()=='Explicit cumulative four'
    finally:close(dialog)


def test_total_quantity_correction_has_usable_reason_field(app,core):
    owner=course(core);task=register(core,owner);bridge=QueuedCoreBridge(core);dialog=RecoveryTaskDialog(bridge,owner,entity=task)
    try:
        dialog.total.setValue(9);dialog.source.setPlainText('Course list confirms nine lessons.')
        dialog.save();assert not bridge.commands
        dialog.definition_reason.setText('One lesson was omitted from original scope');dialog.save();wait(app,lambda:bridge.pending==0)
        p=core.query('recovery_summary',task_id=task['id'])['items'][0]['progress']
        assert p['total_quantity']==9 and p['completed_quantity']==3
    finally:close(dialog)


def test_completion_can_be_corrected_without_prior_quantity_feedback(app,core):
    owner=course(core)
    task=cmd(core,'set_recovery_task',{'course_id':owner['id'],'title':'Unknown quantity','completion_gate':'All defined work complete','unit':'lessons','total_quantity':None,'source_text':'Explicit missed lessons','reason':'self_reported'})['result']['entity']
    cmd(core,'record_recovery_progress',{'task_id':task['id'],'version':task['version'],'source_text':'I explicitly confirm completion','completion_confirmed':True})
    bridge=QueuedCoreBridge(core);dialog=RecoveryProgressDialog(bridge,task)
    try:
        wait(app,lambda:dialog.loaded)
        assert dialog.progress['latest_feedback_id'] is None and dialog.progress['completion']=='done'
        dialog.completion.setCurrentIndex(1);dialog.source.setPlainText('One required exercise remains');dialog.correction.setChecked(True);dialog.correction_reason.setText('Earlier completion report missed an exercise')
        dialog.save();wait(app,lambda:bridge.pending==0)
        assert core.query('recovery_summary',task_id=task['id'])['items'][0]['progress']['completion']=='incomplete'
    finally:close(dialog)


def test_reusing_original_task_preserves_existing_estimate(app,core,monkeypatch):
    import management.gui_recovery as ui
    from PySide6.QtWidgets import QDialog
    owner=course(core);original=cmd(core,'create',{'type':'task','title':'Existing work','parent_id':owner['id'],'data':{'estimated_minutes':45,'completion_gate':'Original condition','scheduled_date':'2030-01-07'}})['result']['entity']
    class Picker:
        def __init__(self,*a,**kw):self.selected=original
        def exec(self):return QDialog.DialogCode.Accepted
    monkeypatch.setattr(ui,'EntityPicker',Picker)
    bridge=QueuedCoreBridge(core);dialog=RecoveryTaskDialog(bridge,owner)
    try:
        dialog.choose_existing();assert dialog.minutes.value()==45
        dialog.source.setPlainText('I explicitly confirm this original task still needs catching up.');dialog.save();wait(app,lambda:bridge.pending==0)
        current=core.query('get',id=original['id'])['entity']
        assert current['data']['estimated_minutes']==45 and current['data']['scheduled_date']=='2030-01-07'
        assert current['data']['completion_gate']=='Original condition'
        assert core.query('list',type='task')['total']==1
    finally:close(dialog)


def test_recovery_modal_is_released_under_persistent_parent(app):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QWidget
    from shiboken6 import isValid
    from management.gui_recovery import DraftDialog,run_dialog
    parent=QWidget()
    try:
        for index in range(12):
            dialog=DraftDialog('Synthetic transient',parent);QTimer.singleShot(0,dialog.reject)
            run_dialog(dialog);QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            assert not isValid(dialog)
        assert parent.findChildren(DraftDialog)==[]
    finally:parent.close()


def test_normal_task_edit_preserves_dedicated_recovery_metadata(app,core):
    from management.gui_forms import EntityForm
    owner=course(core);task=register(core,owner);bridge=QueuedCoreBridge(core)
    form=EntityForm(bridge,core.query('capabilities'),entity=task)
    try:
        assert not any(k.startswith('catchup_') for k in form.fields)
        form.title_edit.setText('Clearer catch-up task title');form.save();wait(app,lambda:bridge.pending==0)
        current=core.query('get',id=task['id'])['entity']
        assert current['title']=='Clearer catch-up task title'
        assert current['data']['catchup_enabled'] and current['data']['catchup_total_quantity']==8
        assert core.query('recovery_summary',task_id=task['id'])['items'][0]['progress']['completed_quantity']==3
    finally:form.close()
