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
        page.open_library()
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
            assert dialog.table.isHidden() and not dialog.paste.isVisible()
            assert dialog.pages.currentWidget() is dialog.editor_scroll
            assert len(dialog.cards)==2 and all(card.gate.isVisible() for card in dialog.cards)
            dialog.editor_scroll.ensureWidgetVisible(dialog.relation);app.processEvents()
            assert dialog.editor_scroll.viewport().rect().contains(dialog.relation.mapTo(dialog.editor_scroll.viewport(),dialog.relation.rect().bottomRight()))
            assert 90<=dialog.context_scroll.height()<=130
            assert dialog.context_scroll.verticalScrollBar().maximum()>0
            assert dialog.save_button.isVisible() and dialog.rect().contains(dialog.save_button.mapTo(dialog,dialog.save_button.rect().bottomRight()))
            assert dialog.width()<=1100 and dialog.height()<=1000
            assert dialog.grab().save(str(folder/f'task-batch-{theme}.png'))
            dispose(app,dialog);dialog=None
    finally:
        if dialog:dispose(app,dialog)
        apply_appearance(app,previous)


def test_batch_steps_wrap_at_large_font_without_losing_optional_fields(app):
    from PySide6.QtTest import QTest
    from management.gui_theme import apply_appearance, current_appearance

    previous = current_appearance()
    apply_appearance(app, {**previous, 'font_size': 20})
    bridge = ControlledBridge()
    dialog = TaskBatchDialog(bridge)
    try:
        dialog.paste.setPlainText('整理阅读笔记\n购买活动材料')
        dialog.show();QTest.qWait(60)
        assert dialog.pages.currentWidget() is dialog.input_page
        assert not dialog.table.isVisible() and not dialog.save_button.isVisible()
        dialog.preview_button.click();preview(bridge,dialog)
        assert dialog.pages.currentWidget() is dialog.editor_scroll
        assert not dialog.paste.isVisible() and not dialog.table.isVisible()
        card=dialog.cards[0];card.details_button.click();card.minutes.setText('30')
        assert not dialog.save_button.isEnabled()
        for width in (980,650,980):
            dialog.resize(width,680);QTest.qWait(60)
            assert dialog.editor_scroll.horizontalScrollBar().maximum()==0
            assert card.title.width()>=160
            for action in (dialog.preview_button,dialog.save_button,dialog.back_button,dialog.close_button):
                assert action.height()>=action.sizeHint().height()
                assert action.parentWidget().rect().contains(action.geometry())
        dialog.back_button.click()
        assert dialog.pages.currentWidget() is dialog.input_page
        assert dialog.table.item(0,2).text()=='30'
        dialog.paste.setPlainText('联系场地负责人');dialog.preview_button.click();preview(bridge,dialog)
        assert dialog.table.rowCount()==3 and dialog.table.item(0,2).text()=='30'
        assert bridge.commands==[]
    finally:
        dispose(app,dialog);apply_appearance(app,previous)


def test_plain_batch_next_step_checks_current_titles_and_saved_navigation(app):
    opened=[];planned=[];shown=[]
    bridge=ControlledBridge()
    dialog=TaskBatchDialog(bridge,on_open=opened.append,on_plan=planned.append,on_saved_ids=shown.append)
    try:
        dialog.paste.setPlainText('整理阅读笔记\n购买活动材料\n联系场地负责人')
        dialog.next_step();first=preview(bridge,dialog)
        assert [item['title'] for item in first['items']]==['整理阅读笔记','购买活动材料','联系场地负责人']
        assert all('scheduled_date' not in item and 'estimated_minutes' not in item for item in first['items'])
        assert len(dialog.cards)==3 and dialog.step=='review'
        dialog.cards[0].title.setText('整理两篇阅读笔记')
        assert dialog.preview_result is None
        dialog.request_preview();second=preview(bridge,dialog)
        assert second['items'][0]['title']=='整理两篇阅读笔记'
        dialog.save()
        assert bridge.commands[0]['payload']['items'][0]['title']=='整理两篇阅读笔记'
        items=[task(str(i),item['title']) for i,item in enumerate(second['items'])]
        bridge.commands[0]['callback']({'result':{'items':items,'links':[]}})
        assert dialog.pages.currentWidget() is dialog.results and '尚未加入每日计划' in dialog.status.text()
        dialog.view_saved();dialog.plan_saved()
        assert shown==[['0','1','2']] and planned==[['0','1','2']]
        assert len(bridge.commands)==1  # Navigation does not implicitly arrange tasks.
    finally:dispose(app,dialog)


def test_split_has_independent_task_cards_and_parent_navigation(app):
    bridge=ControlledBridge();opened=[]
    source=task();dialog=TaskBatchDialog(bridge,source=source,on_open=opened.append)
    try:
        bridge.deliver('get',{'entity':source},id='source')
        bridge.deliver('list',{'items':[],'total':2},parent_id='source')
        assert dialog.step=='review' and len(dialog.cards)==2
        assert dialog.mode=='split' and dialog.table.isHidden()
        dialog.cards[0].title.setText('确认场地');dialog.cards[0].gate.setText('收到场地确认')
        dialog.cards[1].title.setText('准备材料');dialog.cards[1].gate.setText('材料清单齐全')
        dialog.relation.setCurrentIndex(1)
        assert '确认场地 → 准备材料' in dialog.sequence_hint.text()
        assert '前后依赖' in dialog.sequence_hint.text()
        dialog.request_preview();value=preview(bridge,dialog)
        assert value['payload']['source_id']=='source' and value['payload']['sequential'] is True
        dialog.save();bridge.commands[0]['callback']({'result':{'items':[task('c1'),task('c2')],'links':[]}})
        dialog.view_saved();assert opened==['source']
        assert '原任务仍需单独确认完成' in dialog.status.text()
    finally:dispose(app,dialog)


def test_plain_task_remove_preserves_remaining_metadata_and_invalidates_review(app):
    bridge=ControlledBridge();dialog=TaskBatchDialog(bridge)
    try:
        fill(dialog);dialog.request_preview();preview(bridge,dialog)
        dialog.cards[0].remove()
        assert dialog.table.rowCount()==1 and len(dialog.cards)==1
        assert dialog.cards[0].title.text()=='Do exercises'
        assert dialog.cards[0].gate.text()=='Correct every error'
        assert dialog.preview_result is None and not dialog.save_button.isEnabled()
        dialog.cards[0].details_button.setChecked(True)
        dialog.cards[0].due.setText('2030-01-10');dialog.cards[0].due.clear()
        assert 'due_date' not in dialog.collect_items()[0]
        assert bridge.commands==[]
    finally:dispose(app,dialog)


def test_advanced_table_paste_retains_five_columns_and_checks_limit_before_import(app):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QPushButton, QTextEdit
    from management.gui_task_batch import SpreadsheetDraftDialog
    bridge=ControlledBridge();dialog=TaskBatchDialog(bridge)
    try:
        def paste_modal():
            modal=next(widget for widget in app.topLevelWidgets() if isinstance(widget,SpreadsheetDraftDialog))
            text=modal.findChild(QTextEdit)
            confirm=next(button for button in modal.findChildren(QPushButton) if button.text()=='下一步：核对这些任务')
            text.setPlainText('\n'.join('Task '+str(i) for i in range(51)))
            confirm.click()
            assert dialog.table.rowCount()==0 and len(text.toPlainText().splitlines())==51
            text.setPlainText('整理资料\t能找到三份原始文件\t30\t2030-01-10\t2030-01-09')
            confirm.click()
        QTimer.singleShot(0,paste_modal);dialog.paste_spreadsheet()
        value=preview(bridge,dialog)
        assert value['items']==[{'title':'整理资料','completion_gate':'能找到三份原始文件','estimated_minutes':30,'due_date':'2030-01-10','scheduled_date':'2030-01-09'}]
        assert dialog.step=='review' and not bridge.commands
    finally:dispose(app,dialog)


def test_spreadsheet_draft_escape_and_window_close_keep_input_when_declined(app,monkeypatch):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox, QTextEdit, QVBoxLayout
    from management.gui_task_batch import SpreadsheetDraftDialog
    dialog=SpreadsheetDraftDialog();dialog.input=QTextEdit();QVBoxLayout(dialog).addWidget(dialog.input)
    dialog.input.setPlainText('尚未核对的任务');dialog.show();app.processEvents()
    prompts=[]
    monkeypatch.setattr(QMessageBox,'question',lambda *args:prompts.append(args) or QMessageBox.StandardButton.No)
    try:
        QTest.keyClick(dialog,Qt.Key.Key_Escape);assert dialog.isVisible()
        dialog.close();assert dialog.isVisible() and dialog.input.toPlainText()=='尚未核对的任务'
        assert len(prompts)==2
    finally:
        dialog.input.clear();dialog.close();dialog.deleteLater();app.processEvents()


def test_nested_detail_split_results_share_library_and_plan_actions(app, tmp_path):
    from PySide6.QtWidgets import QWidget
    core = Core(tmp_path)
    parent = cmd(core,'create',{'type':'task','title':'Parent task','data':{'completion_gate':'Parent condition'}})['result']['entity']
    source = cmd(core,'create',{'type':'task','title':'Nested task','parent_id':parent['id'],'data':{'completion_gate':'Nested condition'}})['result']['entity']
    bridge=QueuedCoreBridge(core)
    class Library:
        def __init__(self): self.ids=[]
        def show_batch_tasks(self, ids): self.ids=list(ids)
    class Host(QWidget):
        def __init__(self):
            super().__init__(); self.tasks_page=Library(); self.plans=[]; self.sections=[]
        def navigate(self, section): self.sections.append(section)
        def manual_plan(self, day, task_ids=None): self.plans.append((day,list(task_ids or [])))
    host=Host(); outer=TaskDetailDialog(bridge,parent,host); inner=TaskDetailDialog(bridge,source,outer); dialog=None
    try:
        wait(app,lambda:bridge.pending==0);inner.split_task();dialog=inner.dialogs[-1]
        wait(app,lambda:bridge.pending==0);fill(dialog);dialog.request_preview();wait(app,lambda:bridge.pending==0)
        dialog.save();wait(app,lambda:bridge.pending==0)
        identifiers=list(dialog.saved_ids);before=core.query('state')
        assert not dialog.plan_button.isHidden() and dialog.view_batch_button.text()=='查看这批子任务'
        dialog.plan_button.click();wait(app,lambda:bridge.pending==0)
        assert host.plans==[(core.query('task_pool')['date'],identifiers)]
        assert core.query('list',type='plan')['total']==0 and core.query('state')==before
        dialog.view_batch_button.click();wait(app,lambda:bridge.pending==0)
        assert host.sections==['tasks'] and host.tasks_page.ids==identifiers
        assert inner.closed and outer.closed and core.query('state')==before
    finally:
        wait(app,lambda:bridge.pending==0)
        if dialog:dispose(app,dialog)
        dispose(app,inner);dispose(app,outer);host.close();host.deleteLater()


def test_standalone_detail_split_plan_opens_unsaved_editor(app, tmp_path):
    from management.gui_workflows import PlanDialog
    core=Core(tmp_path)
    source=cmd(core,'create',{'type':'task','title':'Standalone task','data':{'completion_gate':'Condition'}})['result']['entity']
    bridge=QueuedCoreBridge(core);detail=TaskDetailDialog(bridge,source);dialog=None
    try:
        wait(app,lambda:bridge.pending==0);detail.split_task();dialog=detail.dialogs[-1]
        wait(app,lambda:bridge.pending==0);fill(dialog);dialog.request_preview();wait(app,lambda:bridge.pending==0)
        dialog.save();wait(app,lambda:bridge.pending==0);identifiers=list(dialog.saved_ids)
        before=core.query('state');dialog.plan_button.click();wait(app,lambda:bridge.pending==0)
        editor=next(child for child in detail.dialogs if isinstance(child,PlanDialog))
        wait(app,lambda:editor.table.rowCount()==2 and bridge.pending==0)
        assert editor.isVisible() and editor.table.rowCount()==2
        assert {editor.table.cellWidget(i,0).property('entity')['id'] for i in range(editor.table.rowCount())}==set(identifiers)
        assert core.query('list',type='plan')['total']==0 and core.query('state')==before
        editor.close();wait(app,lambda:bridge.pending==0)
    finally:
        wait(app,lambda:bridge.pending==0)
        if dialog:dispose(app,dialog)
        dispose(app,detail)
