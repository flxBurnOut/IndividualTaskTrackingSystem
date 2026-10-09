"""Manual batch preview, atomic commit UI and deterministic uncertain retries."""
import copy
import os
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from management.core import Core
from management.gui_task_batch import TaskBatchDialog, parse_task_rows, can_split_task
from management.gui_tasks import TasksPage
from management.gui_workspace import TaskDetailDialog
from test_ux_workflows_v2 import ControlledBridge, QueuedCoreBridge, cmd, wait


@pytest.fixture(scope='session')
def app():
    application=QApplication.instance() or QApplication([])
    # Windows' offscreen plugin needs explicit real glyph loading for useful QA.
    if application.platformName()=='offscreen' and os.name=='nt':
        for name in ('msyh.ttc','msyhbd.ttc'):
            path=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/name
            if path.is_file():QFontDatabase.addApplicationFont(str(path))
    return application


def dispose(app, dialog):
    if not isValid(dialog):return
    if isinstance(dialog, TaskBatchDialog):
        dialog.dirty = dialog.saving = dialog.uncertain = False
    for child in getattr(dialog, 'dialogs', [])[:]:
        if isinstance(child, TaskBatchDialog):
            child.dirty = child.saving = child.uncertain = False
            child.close()
    dialog.close(); dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete); app.processEvents()


def task(identifier='source', title='Original large task', **updates):
    value = {'id':identifier, 'title':title, 'type':'task', 'version':3, 'status':'active', 'data':{'completion_gate':'Original full standard'}}
    value.update(updates); return value


def fill(dialog, text='Read notes\tExplain two concepts\t20\t2030-01-10\nDo exercises\tCorrect every error'):
    dialog.paste.setPlainText(text); dialog.import_rows()


def preview(bridge, dialog):
    call = bridge.take('preview_task_batch')
    items = copy.deepcopy(call['params']['items'])
    result = {'command':'split_task' if dialog.source else 'create_task_batch',
              'payload':{**copy.deepcopy(call['params']), 'preview_token':'reviewed'},
              'items':items, 'source':dialog.source, 'warnings':['原计划和完成记录保留。'],
              'epoch':bridge.epoch, 'revision':10}
    call['callback'](result); return result


def test_paste_is_literal_and_row_limit_failure_is_lossless(app):
    assert parse_task_rows('1. A\tRead <b>literally</b>\t30\t\t2030-01-07')[0] == ['1. A','Read <b>literally</b>','30','','2030-01-07']
    bridge=ControlledBridge(); dialog=TaskBatchDialog(bridge)
    try:
        fill(dialog); assert dialog.table.rowCount()==2
        text='\n'.join(f'Task {i}' for i in range(49)); dialog.paste.setPlainText(text); dialog.import_rows()
        assert dialog.table.rowCount()==2 and dialog.paste.toPlainText()==text
        assert '50' in dialog.status.text()
        dialog.request_preview(); assert not any(q['name']=='preview_task_batch' for q in bridge.queries)
        dialog.paste.clear(); dialog.request_preview()
        query=bridge.take('preview_task_batch')
        assert query['params']['items'][0]['estimated_minutes']==20
        assert query['params']['items'][0]['due_date']=='2030-01-10'
        assert 'scheduled_date' not in query['params']['items'][0]
        assert 'estimated_minutes' not in query['params']['items'][1]
        assert bridge.commands==[]
    finally:dispose(app,dialog)


def test_split_waits_for_source_and_requires_independent_standards(app):
    bridge=ControlledBridge(); dialog=TaskBatchDialog(bridge,source=task())
    try:
        fill(dialog,'First\nSecond'); dialog.request_preview()
        assert not any(q['name']=='preview_task_batch' for q in bridge.queries)
        bridge.deliver('get',{'entity':task(version=4)},id='source')
        bridge.deliver('list',{'items':[],'total':3},parent_id='source')
        assert '已有 3 项子任务' in dialog.context.text() and 'Original full standard' in dialog.context.text()
        dialog.request_preview(); assert '完成标准' in dialog.status.text()
        assert not any(q['name']=='preview_task_batch' for q in bridge.queries)
        dialog.table.item(0,1).setText('First evidence');dialog.table.item(1,1).setText('Second evidence')
        dialog.sequential.setChecked(True);dialog.request_preview()
        value=preview(bridge,dialog)
        assert value['payload']['source_version']==4 and value['payload']['sequential'] is True
        assert '原计划和完成记录保留' in dialog.preview.toPlainText()
        assert dialog.save_button.isEnabled() and not bridge.commands
    finally:dispose(app,dialog)


def test_edit_invalidates_preview_and_late_read_cannot_restore_it(app):
    bridge=ControlledBridge();dialog=TaskBatchDialog(bridge)
    try:
        fill(dialog);dialog.request_preview();old=bridge.take('preview_task_batch')
        dialog.table.item(0,0).setText('New title')
        old['callback']({'command':'create_task_batch','payload':{'old':True},'items':[],'epoch':bridge.epoch,'revision':10})
        assert dialog.preview_result is None and not dialog.save_button.isEnabled()
        dialog.request_preview();preview(bridge,dialog)
        dialog.sequential.setChecked(True)
        assert dialog.preview_result is None and not dialog.save_button.isEnabled()
        dialog.save(); assert bridge.commands==[]
    finally:dispose(app,dialog)


def test_reordering_changes_explicit_preview_order_and_invalidates_prior_preview(app):
    bridge=ControlledBridge();dialog=TaskBatchDialog(bridge)
    try:
        fill(dialog);dialog.request_preview();preview(bridge,dialog)
        dialog.table.selectRow(1);dialog.up_button.click()
        assert dialog.table.item(0,0).text()=='Do exercises'
        assert dialog.table.item(0,1).text()=='Correct every error'
        assert dialog.preview_result is None and not dialog.save_button.isEnabled()
        dialog.sequential.setChecked(True);dialog.request_preview()
        call=bridge.take('preview_task_batch')
        assert [item['title'] for item in call['params']['items']]==['Do exercises','Read notes']
        assert call['params']['sequential'] is True
    finally:dispose(app,dialog)


@pytest.mark.parametrize('error_code',['connection_lost','connection_error','timeout','request_timeout','protocol_error','response_limit','internal_error'])
def test_uncertain_write_retries_exact_request_and_keeps_inputs_locked(app,error_code):
    bridge=ControlledBridge();dialog=TaskBatchDialog(bridge)
    try:
        fill(dialog);dialog.request_preview();preview(bridge,dialog)
        dialog.save();dialog.save()
        assert len(bridge.commands)==1 and dialog.saving and not dialog.editor.isEnabled()
        first=bridge.commands[0]
        first['error']({'code':error_code,'message':'No response'})
        assert dialog.uncertain and not dialog.editor.isEnabled()
        original=copy.deepcopy(first['payload']);rid=first['options']['request_id']
        dialog.table.item(0,0).setText('Programmatic input after timeout')
        dialog.request_preview();dialog.reject();assert not dialog.closed
        dialog.save()
        assert len(bridge.commands)==2 and bridge.commands[-1]['payload']==original
        assert bridge.commands[-1]['options']==first['options'] and rid
    finally:dispose(app,dialog)


def test_definite_failure_requires_new_preview_and_preserves_rows(app):
    bridge=ControlledBridge();dialog=TaskBatchDialog(bridge)
    try:
        fill(dialog);dialog.request_preview();preview(bridge,dialog);dialog.save()
        bridge.commands[0]['error']({'code':'stale_revision','message':'Changed elsewhere'})
        assert dialog.table.rowCount()==2 and dialog.editor.isEnabled()
        assert dialog.preview_result is None and not dialog.save_button.isEnabled()
        dialog.save();assert len(bridge.commands)==1
    finally:dispose(app,dialog)


@pytest.mark.parametrize('retry_code',['service_unavailable','upgrade_pending','version_mismatch','authentication','epoch_conflict'])
def test_uncertain_result_stays_locked_across_failed_reconnect_until_original_receipt(app,retry_code):
    bridge=ControlledBridge();saved=[];dialog=TaskBatchDialog(bridge,on_saved=saved.append)
    try:
        fill(dialog);dialog.request_preview();preview(bridge,dialog);dialog.save()
        first=bridge.commands[0]
        first['error']({'code':'connection_lost','message':'Original response lost'})
        dialog.save();bridge.commands[1]['error']({'code':retry_code,'message':'Reconnect not ready'})
        assert dialog.uncertain and not dialog.editor.isEnabled()
        assert dialog.request_id==first['options']['request_id']
        dialog.request_preview();dialog.reject();assert not dialog.closed
        dialog.save();third=bridge.commands[2]
        assert third['payload']==first['payload'] and third['options']==first['options']
        receipt={'result':{'items':[task('a','Saved first'),task('b','Saved second')],'links':[]},'replayed':True}
        third['callback'](receipt)
        assert dialog.saved and not dialog.uncertain and saved==[receipt]
        dialog.save();assert len(bridge.commands)==3
    finally:dispose(app,dialog)


@pytest.mark.parametrize('code',['revision_conflict','preview_conflict','entity_conflict','validation'])
def test_same_epoch_business_rejection_releases_uncertain_request_for_new_preview(app,code):
    bridge=ControlledBridge();dialog=TaskBatchDialog(bridge)
    try:
        fill(dialog);dialog.request_preview();preview(bridge,dialog);dialog.save()
        bridge.commands[0]['error']({'code':'connection_lost','message':'Original response lost'})
        dialog.save();bridge.commands[1]['error']({'code':code,'message':'Not applied'})
        assert not dialog.uncertain and dialog.editor.isEnabled()
        assert dialog.request_id is None and dialog.submission is None and dialog.preview_result is None
        assert not dialog.save_button.isEnabled()
    finally:dispose(app,dialog)


def test_success_displays_real_saved_tasks_and_relationships(app):
    bridge=ControlledBridge();saved=[];opened=[]
    dialog=TaskBatchDialog(bridge,on_saved=saved.append,on_open=opened.append)
    try:
        fill(dialog);dialog.sequential.setChecked(True);dialog.request_preview();preview(bridge,dialog);dialog.save()
        first,second=task('child-1','Read notes'),task('child-2','Do exercises')
        receipt={'result':{'items':[first,second], 'links':[{'id':'link','source_id':second['id'],'target_id':first['id'],'kind':'depends_on'}]}}
        bridge.commands[0]['callback'](receipt)
        assert dialog.saved and dialog.results.topLevelItemCount()==2 and saved==[receipt]
        assert dialog.results.topLevelItem(1).text(2)=='Read notes'
        dialog.results.setCurrentItem(dialog.results.topLevelItem(1));dialog.open_result();assert opened==['child-2']
        dialog.save();assert len(bridge.commands)==1
    finally:dispose(app,dialog)


def test_task_page_exposes_batch_and_disables_split_for_completed_task(app):
    bridge=ControlledBridge();page=TasksPage(bridge)
    try:
        page.owner={'id':'goal','title':'Goal filter','type':'goal','version':1}
        page.batch_button.click();assert len(page.dialogs)==1 and page.dialogs[0].mode=='create'
        assert page.dialogs[0].parent_entity is None
        page.dialogs[0].close()
        page.refresh()
        row={**task(), 'owner_label':'未归属','in_plan':False,'completion_state':'done'}
        bridge.deliver('task_pool',{'items':[row],'total':1,'group':'all','date':'2030-01-07','next_offset':None})
        assert not page.split_button.isEnabled()
        page.split_selected();assert not page.dialogs
    finally:dispose(app,page)


@pytest.mark.parametrize('status,projection,expected',[
    ('done','partial',True),('done','incomplete',True),('done',None,False),
    ('active','done',False),('active',None,True),('cancelled','incomplete',False),
    ('draft','partial',False),
])
def test_split_eligibility_uses_completion_projection_before_raw_status(status,projection,expected):
    assert can_split_task(task(status=status,completion_state=projection)) is expected


def test_real_split_is_previewed_then_visible_in_parent_and_results(app,tmp_path):
    core=Core(tmp_path)
    source=cmd(core,'create',{'type':'task','title':'Large task','data':{'completion_gate':'Full standard'}})['result']['entity']
    bridge=QueuedCoreBridge(core);detail=TaskDetailDialog(bridge,source);dialog=None
    try:
        wait(app,lambda:bridge.pending==0)
        detail.split_task();dialog=detail.dialogs[-1];wait(app,lambda:bridge.pending==0)
        fill(dialog);dialog.sequential.setChecked(True)
        before=core.query('state');dialog.request_preview();wait(app,lambda:bridge.pending==0)
        assert dialog.preview_result,dialog.status.text()
        assert core.query('state')==before
        dialog.save();wait(app,lambda:bridge.pending==0)
        assert dialog.saved,dialog.status.text()
        assert detail.child_tasks.topLevelItemCount()==2
        children=core.query('list',type='task',parent_id=source['id'])['items']
        assert len(children)==2 and {item['id'] for item in children}=={dialog.results.topLevelItem(i).data(0,Qt.ItemDataRole.UserRole)['id'] for i in range(2)}
        assert core.query('get',id=source['id'])['entity']==source
        assert core.query('list',type='plan')['total']==0 and core.query('list',type='feedback')['total']==0
        detail.open_child();wait(app,lambda:bridge.pending==0)
        assert isinstance(detail.dialogs[-1],TaskDetailDialog)
        assert detail.dialogs[-1].entity['parent_id']==source['id']
    finally:
        wait(app,lambda:bridge.pending==0)
        if dialog:dispose(app,dialog)
        dispose(app,detail)


def test_batch_dialog_light_dark_large_font_and_long_source_layout(app,tmp_path):
    from management.gui_theme import apply_appearance, current_appearance
    previous=current_appearance();dialog=None
    folder=Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR',str(tmp_path)))
    folder.mkdir(parents=True,exist_ok=True)
    try:
        for theme in ('light','dark'):
            apply_appearance(app,{'theme':theme,'font_size':20})
            bridge=ControlledBridge()
            source=task(title='一个需要明确分步完成的大任务',data={'completion_gate':'原任务完整完成标准需要保留，逐项说明验收范围。'*100})
            dialog=TaskBatchDialog(bridge,source=source)
            bridge.deliver('get',{'entity':source},id='source')
            bridge.deliver('list',{'items':[],'total':4},parent_id='source')
            fill(dialog,'补齐先修笔记\t能复述三个关键概念\t30\n完成练习\t独立完成并纠正错题')
            dialog.sequential.setChecked(True);dialog.request_preview();preview(bridge,dialog)
            dialog.show();app.processEvents()
            assert dialog.table.geometry().bottom()<dialog.sequential.geometry().top()
            dialog.editor_scroll.ensureWidgetVisible(dialog.sequential);app.processEvents()
            assert dialog.editor_scroll.viewport().rect().contains(dialog.sequential.mapTo(dialog.editor_scroll.viewport(),dialog.sequential.rect().bottomRight()))
            assert 90<=dialog.context_scroll.height()<=100
            assert dialog.context_scroll.verticalScrollBar().maximum()>0
            header=dialog.table.horizontalHeader()
            for column in (2,3,4):
                assert header.sectionSize(column)>=header.fontMetrics().horizontalAdvance(dialog.table.horizontalHeaderItem(column).text())+28
            assert dialog.save_button.isVisible() and dialog.rect().contains(dialog.save_button.mapTo(dialog,dialog.save_button.rect().bottomRight()))
            assert dialog.width()<=1100 and dialog.height()<=1000
            assert dialog.grab().save(str(folder/f'task-batch-{theme}.png'))
            dispose(app,dialog);dialog=None
    finally:
        if dialog:dispose(app,dialog)
        apply_appearance(app,previous)


def test_batch_toolbar_wraps_at_large_font_without_hiding_actions(app):
    from PySide6.QtTest import QTest
    from management.gui_theme import apply_appearance, current_appearance

    previous = current_appearance()
    apply_appearance(app, {**previous, 'font_size': 20})
    bridge = ControlledBridge()
    dialog = TaskBatchDialog(bridge)
    try:
        dialog.paste.setPlainText('保留尚未导入的任务草稿')
        dialog.show()
        for width in (980, 650, 980):
            dialog.resize(width, 680)
            QTest.qWait(60)
            assert dialog.editor_scroll.horizontalScrollBar().maximum() == 0
            actions = (dialog.import_button, dialog.add_button, dialog.remove_button,
                       dialog.up_button, dialog.down_button)
            for action in actions:
                assert action.width() >= action.sizeHint().width()
                assert action.height() >= action.sizeHint().height()
                assert action.parentWidget().rect().contains(action.geometry())
            toolbar = dialog.import_button.parentWidget()
            if toolbar.width() < toolbar.layout().sizeHint().width():
                assert len({action.y() for action in (*actions, dialog.count)}) > 1
            assert dialog.paste.toPlainText() == '保留尚未导入的任务草稿'
        assert bridge.commands == []
    finally:
        dispose(app, dialog)
        apply_appearance(app, previous)
