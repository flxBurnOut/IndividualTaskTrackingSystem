import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtWidgets import QApplication,QFrame,QPushButton
from PySide6.QtCore import QCoreApplication,QEvent
from management.core import Core
from management.gui_task_list import TaskListPanel
from management.gui_workspace import WorkspacePage
from management.schemas import TYPES
from test_ux_workflows_v2 import QueuedCoreBridge,ControlledBridge,wait
from test_plan_assistance_v8 import cmd,task


@pytest.fixture(scope='session')
def app():return QApplication.instance() or QApplication([])


def test_completed_rows_are_lazy_collapsed_and_fully_pageable(app,tmp_path):
    core=Core(tmp_path);owner=cmd(core,'create',{'type':'course','title':'Synthetic'})['entity']
    task(core,'Still open',parent_id=owner['id'])
    for i in range(65):task(core,f'Done {i}',parent_id=owner['id'],status='done')
    state=core.query('state');bridge=QueuedCoreBridge(core);panel=TaskListPanel(bridge,owner);panel.show()
    try:
        wait(app,lambda:bridge.pending==0)
        assert panel.toggle.text()=='已完成（65） · 展开'
        assert not panel.done_body.isVisible() and not panel.findChildren(QFrame,'CompletedTaskRow')
        assert len(panel.ids['open'])==1
        panel.toggle.click();wait(app,lambda:bridge.pending==0)
        assert len(panel.findChildren(QFrame,'CompletedTaskRow'))==30
        panel.done_more.click();wait(app,lambda:bridge.pending==0)
        panel.done_more.click();wait(app,lambda:bridge.pending==0)
        assert len(panel.ids['done'])==65 and not panel.done_more.isVisible()
        panel.toggle.click();assert not panel.done_body.isVisible()
        panel.toggle.click();assert len(panel.ids['done'])==65
        assert core.query('state')['revision']==state['revision']
    finally:panel.close();panel.deleteLater();app.processEvents()


def test_status_update_moves_row_into_fold_and_reopen_restores_it(app,tmp_path):
    core=Core(tmp_path);owner=cmd(core,'create',{'type':'course','title':'Synthetic'})['entity'];target=task(core,parent_id=owner['id'])
    bridge=QueuedCoreBridge(core);page=WorkspacePage(bridge,on_create=lambda *_:None,on_edit=lambda *_:None);page.set_types(TYPES);page.show();page.open_object(owner)
    try:
        wait(app,lambda:bridge.pending==0)
        assert target['id'] in page.task_panel.ids['open']
        cmd(core,'update',{'id':target['id'],'version':1,'patch':{'status':'done'}});page.refresh();wait(app,lambda:bridge.pending==0)
        assert not page.task_panel.ids['open'] and page.task_panel.done_count==1
        assert not page.task_panel.done_body.isVisible()
        cmd(core,'update',{'id':target['id'],'version':2,'patch':{'status':'active'}});page.refresh();wait(app,lambda:bridge.pending==0)
        assert target['id'] in page.task_panel.ids['open'] and page.task_panel.done_count==0
    finally:page.close();page.deleteLater();app.processEvents()


def test_late_page_response_does_not_touch_destroyed_panel(app):
    bridge=ControlledBridge();panel=TaskListPanel(bridge,{'id':'owner'})
    query=bridge.take('workspace_tasks');panel.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    assert panel.dead
    query['callback']({'items':[],'counts':{'done':0},'next_offset':None})


def test_completed_recovery_does_not_remain_above_the_fold(app,tmp_path):
    from test_catchup_v5 import register,report
    from management.gui_recovery import RecoveryPanel
    core=Core(tmp_path);course=cmd(core,'create',{'type':'course','title':'Recovery course'})['entity']
    completed=register(core,course,title='Finished catchup')['entity'];report(core,completed,8,completion_confirmed=True)
    opened=register(core,course,title='Still catching up')['entity']
    bridge=QueuedCoreBridge(core);panel=RecoveryPanel(bridge,course);panel.show()
    try:
        wait(app,lambda:bridge.pending==0)
        titles=[b.text() for b in panel.findChildren(QPushButton)]
        assert opened['title'] in titles and completed['title'] not in titles
        assert '已完成的 1 项' in panel.summary.text()
    finally:panel.close();panel.deleteLater();app.processEvents()
