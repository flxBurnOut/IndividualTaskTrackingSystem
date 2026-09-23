"""Direct task feedback, readable ownership and concise catch-up interaction."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import QDate,QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication,QLabel,QPushButton,QFrame
from management.core import Core
from management.gui_today import TodayPage
from management.gui_task_list import TaskListPanel
from management.gui_recovery import RecoveryProgressDialog
from test_ux_workflows_v2 import ControlledBridge,QueuedCoreBridge,cmd,wait

@pytest.fixture(scope='session')
def app():return QApplication.instance() or QApplication([])

def create(core,kind,title,**fields):return cmd(core,'create',{'type':kind,'title':title,**fields})['result']['entity']
def dispose(app,widget):
    if hasattr(widget,'finished_ok'):widget.finished_ok=True;widget.saving=False
    widget.close();widget.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);app.processEvents()
def words(widget):return '\n'.join(w.text() for w in widget.findChildren(QLabel))+'\n'+'\n'.join(w.text() for w in widget.findChildren(QPushButton))


def test_today_completion_is_immediate_shared_and_only_changes_completion(app,tmp_path):
    core=Core(tmp_path);day=QDate.currentDate().toString('yyyy-MM-dd')
    owner=create(core,'course','Course without its code in the name',data={'code':'SC3060'})
    task=create(core,'task','Tutorial 4',parent_id=owner['id'],data={'completion_gate':'Complete and check problems'})
    cmd(core,'create_plan',{'date':day,'mode':'no_precise_time','blocks':[{'target_id':task['id'],'minutes':30}]})
    cmd(core,'record_feedback',{'target_id':task['id'],'business_date':day,'dimensions':{'attendance':'absent'},'source_text':'Explicitly absent'})
    bridge=QueuedCoreBridge(core);page=TodayPage(bridge);page.refresh()
    try:
        wait(app,lambda:bridge.pending==0)
        assert 'SC3060' in words(page.plan_box) and '星期' in page.date_heading.text()
        item=page.review_result['items'][0];page.complete_plan_item(item,'done');page.complete_plan_item(item,'done')
        assert len(bridge.commands)==1
        wait(app,lambda:bridge.pending==0)
        assert page.review_result['items'][0]['result']=='done'
        assert core.query('workspace_tasks',id=owner['id'],group='done')['counts']['done']==1
        feedback=core.query('list',type='feedback')['items']
        assert len(feedback)==2
        assert {tuple(e['data']['dimensions'].items()) for e in feedback}=={(('attendance','absent'),),(('completion','done'),)}
        assert core.query('get',id=task['id'])['entity']['status']=='active'
    finally:dispose(app,page)


def test_course_circle_moves_task_to_collapsed_history_and_can_reopen(app,tmp_path):
    core=Core(tmp_path);owner=create(core,'course','Course',data={'code':'IE2108'})
    task=create(core,'task','Lecture 3 practice',parent_id=owner['id']);bridge=QueuedCoreBridge(core);panel=TaskListPanel(bridge,owner);panel.show()
    try:
        wait(app,lambda:bridge.pending==0)
        mark=panel.findChild(QPushButton,'TaskMark');mark.click();mark.click();wait(app,lambda:bridge.pending==0)
        assert len(bridge.commands)==1 and task['id'] not in panel.ids['open']
        assert panel.done_count==1 and not panel.done_body.isVisible()
        panel.toggle.click();wait(app,lambda:bridge.pending==0);QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        mark=panel.done_body.findChild(QPushButton,'TaskMark');mark.click();wait(app,lambda:bridge.pending==0)
        assert task['id'] in panel.ids['open'] and panel.done_count==0
        assert core.query('list',type='feedback')['total']==2
    finally:dispose(app,panel)


def test_recovery_number_and_completion_need_no_written_explanation(app,tmp_path):
    core=Core(tmp_path);owner=create(core,'course','Course')
    target=cmd(core,'set_recovery_task',{'course_id':owner['id'],'title':'Lecture 1-3','completion_gate':'Read lectures and complete practice','unit':'讲','total_quantity':3,'completed_quantity':2,'source_text':'Explicitly two of three read','reason':'self_reported'})['result']['entity']
    bridge=QueuedCoreBridge(core);dialog=RecoveryProgressDialog(bridge,target);dialog.show()
    try:
        wait(app,lambda:dialog.loaded);assert not dialog.advanced.isVisible()
        dialog.add_one.click();dialog.save();wait(app,lambda:bridge.pending==0)
        result=core.query('recovery_summary',task_id=target['id'])['items'][0]['progress']
        assert result['completed_quantity']==3 and result['completion'] is None
        assert '用户在补欠进度界面确认累计已补 3 讲' in bridge.commands[-1][1]['source_text']
    finally:dispose(app,dialog)
    dialog=RecoveryProgressDialog(bridge,target)
    try:
        wait(app,lambda:dialog.loaded);dialog.completion.setCurrentIndex(2);dialog.save();wait(app,lambda:bridge.pending==0)
        assert core.query('workspace_tasks',id=owner['id'],group='done')['counts']['done']==1
        assert core.query('recovery_summary',course_id=owner['id'],open_only=True)['total']==0
    finally:dispose(app,dialog)


def test_recovery_stale_write_preserves_quantity_without_overwriting_new_feedback(app,tmp_path):
    core=Core(tmp_path);owner=create(core,'course','Course')
    target=cmd(core,'set_recovery_task',{'course_id':owner['id'],'title':'Tutorial 4','completion_gate':'Finish exercises','unit':'题','total_quantity':5,'completed_quantity':1,'source_text':'One completed','reason':'self_reported'})['result']['entity']
    bridge=QueuedCoreBridge(core);dialog=RecoveryProgressDialog(bridge,target)
    try:
        wait(app,lambda:dialog.loaded);dialog.completed.setValue(3)
        cmd(core,'record_recovery_progress',{'task_id':target['id'],'version':target['version'],'completed_quantity':4,'source_text':'Newer explicit progress'})
        dialog.save();wait(app,lambda:bridge.pending==0)
        assert dialog.completed.value()==3 and not dialog.finished_ok
        assert core.query('recovery_summary',task_id=target['id'])['items'][0]['progress']['completed_quantity']==4
    finally:dispose(app,dialog)


def test_historical_warnings_are_lazy_folded_and_old_day_callback_is_ignored(app):
    bridge=ControlledBridge();page=TodayPage(bridge);page.set_date('2030-01-07')
    try:
        bridge.deliver('warnings',{'items':[{'title':'Upcoming exam','reason':'In two days'}],'counts':{'current':1,'past':8},'total':1},group='current')
        assert 'Upcoming exam' in words(page.attention_box) and not page.past_expanded
        assert not any(q['params'].get('group')=='past' for q in bridge.queries)
        page.toggle_past_warnings();old=bridge.take('warnings',group='past')
        page.set_date('2030-01-08');old['callback']({'items':[{'title':'Wrong date historical warning'}]})
        assert page.past_warnings is None and not page.past_expanded
        assert 'Wrong date historical warning' not in words(page.attention_box)
        assert bridge.commands==[]
    finally:dispose(app,page)


def test_today_snapshot_feedback_is_version_checked(app):
    bridge=ControlledBridge();page=TodayPage(bridge);page.set_date('2030-01-07')
    item={'target_id':'task','title':'Tutorial 4','owner_label':'SC3060','can_review':True}
    snapshot={'epoch':'captured','revision':3,'has_plan':True,'plan':{'id':'plan','version':4},'items':[item]}
    try:
        bridge.deliver('daily_review',snapshot);bridge.revision=9;page.complete_plan_item(item,'done')
        request=bridge.commands[-1]
        assert request['payload']['plan_version']==4 and request['options']=={'epoch':'captured','expected_revision':3}
        assert request['payload']['answers']==[{'target_id':'task','result':'done'}]
    finally:dispose(app,page)


def test_today_and_recovery_late_replies_ignore_destroyed_widgets(app):
    bridge=ControlledBridge();page=TodayPage(bridge);page.refresh();requests=list(bridge.queries);dispose(app,page)
    for request in requests:
        request['callback']({})
    bridge=ControlledBridge();dialog=RecoveryProgressDialog(bridge,{'id':'t','title':'Tutorial 4','type':'task','version':1,'data':{}})
    request=bridge.take('recovery_summary');dispose(app,dialog);request['callback']({'items':[]})
