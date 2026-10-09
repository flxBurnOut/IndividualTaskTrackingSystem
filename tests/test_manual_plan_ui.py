"""Manual day editing, independent drafts and safe interleaved windows."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')

import pytest
from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QPushButton

from management.core import Core
from management.gui_plan_drafts import PlanDrafts, DraftConflict
from management.gui_workflows import PlanDialog
from management.gui_today import TodayPage
from management.gui_visual_profile import set_visual_style, visual_style
from test_ux_workflows_v2 import ControlledBridge, QueuedCoreBridge, wait, cmd


@pytest.fixture(scope='session')
def app():
    return QApplication.instance() or QApplication([])


def ready(bridge):
    bridge.deliver('daily_review',{'plan':None})
    bridge.deliver('plan_context',{'hard_events':[],'unknowns':[],'epoch':bridge.epoch,'revision':10})


def close(dialog,app):
    dialog.dirty=False
    dialog.draft_timer.stop()
    dialog.close();dialog.deleteLater();app.processEvents()


def task(identifier,title=None):
    return {'id':identifier,'title':title or identifier,'type':'task','version':1,'data':{'completion_gate':'Known standard'},'owner_id':'course','owner_label':'Course'}


def test_selection_survives_pages_and_filters_without_duplicate_blocks(app):
    bridge=ControlledBridge();dialog=PlanDialog(bridge,date=QDate(2030,1,7))
    try:
        ready(bridge)
        bridge.deliver('task_pool',{'items':[task('a')],'total':101,'next_offset':100})
        dialog.candidates.topLevelItem(0).child(0).setCheckState(0,Qt.CheckState.Checked)
        dialog.load_candidates(100)
        bridge.deliver('task_pool',{'items':[task('b')],'total':101,'next_offset':None})
        dialog.candidates.topLevelItem(0).child(0).setCheckState(0,Qt.CheckState.Checked)
        dialog.candidate_group.setCurrentIndex(dialog.candidate_group.findData('undated'))
        call=bridge.take('task_pool');assert call['params']['group']=='undated' and call['params']['offset']==0
        call['callback']({'items':[task('a')],'total':1})
        assert dialog.candidates.topLevelItem(0).child(0).checkState(0)==Qt.CheckState.Checked
        dialog.add_checked();dialog.insert_block(task('a'))
        assert [b['target_id'] for b in dialog.block_values()]==['a','b']
        assert not dialog.checked_candidates
        assert dialog.table.cellWidget(0,4).isReadOnly()
    finally:close(dialog,app)


def test_candidates_cannot_modify_editor_before_plan_and_draft_load(app):
    bridge=ControlledBridge();dialog=PlanDialog(bridge,date=QDate(2030,1,7))
    try:
        bridge.deliver('task_pool',{'items':[task('a')],'total':1})
        item=dialog.candidates.topLevelItem(0).child(0)
        item.setCheckState(0,Qt.CheckState.Checked)
        dialog.add_candidate(item);dialog.add_checked();dialog.new_task();dialog.add_block()
        assert not dialog.candidates.isEnabled() and not dialog.add_candidate_button.isEnabled()
        assert all(not button.isEnabled() for button in dialog.edit_actions)
        assert dialog.table.rowCount()==0
        assert not any(q['name']=='capabilities' for q in bridge.queries)
        ready(bridge)
        assert dialog.candidates.isEnabled() and dialog.add_candidate_button.isEnabled()
    finally:close(dialog,app)


def test_candidate_paging_uses_returned_offsets_and_resets_on_filter_and_date(app):
    bridge=ControlledBridge();dialog=PlanDialog(bridge,date=QDate(2030,1,7))
    try:
        ready(bridge)
        bridge.deliver('task_pool',{'items':[task('a')],'total':90,'next_offset':37})
        dialog.candidate_more.click()
        assert bridge.queries[-1]['params']['offset']==37
        bridge.deliver('task_pool',{'items':[task('b')],'total':90,'next_offset':64})
        dialog.candidate_more.click()
        assert bridge.queries[-1]['params']['offset']==64
        bridge.deliver('task_pool',{'items':[task('c')],'total':90,'next_offset':None})
        dialog.candidate_previous.click()
        assert bridge.queries[-1]['params']['offset']==37
        bridge.deliver('task_pool',{'items':[task('b')],'total':90,'next_offset':64})
        dialog.candidate_previous.click()
        assert bridge.queries[-1]['params']['offset']==0
        bridge.deliver('task_pool',{'items':[task('a')],'total':90,'next_offset':37})
        dialog.candidate_more.click()
        bridge.deliver('task_pool',{'items':[task('b')],'total':90,'next_offset':64})
        dialog.candidate_group.setCurrentIndex(dialog.candidate_group.findData('undated'))
        assert dialog.candidate_history==[] and bridge.queries[-1]['params']['offset']==0
        bridge.deliver('task_pool',{'items':[task('a')],'total':90,'next_offset':37})
        dialog.candidate_more.click()
        bridge.deliver('task_pool',{'items':[task('b')],'total':90,'next_offset':64})
        dialog.date.setDate(QDate(2030,1,8))
        assert dialog.candidate_history==[] and dialog.candidate_offset==0
    finally:close(dialog,app)


def test_drafts_survive_date_change_and_reopen_without_business_write(app,tmp_path):
    core=Core(tmp_path);bridge=QueuedCoreBridge(core)
    before=core.query('state');dialog=PlanDialog(bridge,date=QDate(2030,1,7));reopened=None
    try:
        wait(app,lambda:bridge.pending==0)
        example=task('example');example['data']['estimated_minutes']=30
        dialog.insert_block(example)
        dialog.table.cellWidget(0,3).setValue(0)
        dialog.source.setPlainText('Keep this exact draft')
        dialog.mode.setCurrentIndex(dialog.mode.findData('no_precise_time'))
        wait(app,lambda:bridge.pending==0)
        dialog.date.setDate(QDate(2030,1,8));wait(app,lambda:bridge.pending==0)
        assert dialog.table.rowCount()==0
        dialog.date.setDate(QDate(2030,1,7));wait(app,lambda:bridge.pending==0)
        assert dialog.table.rowCount()==1 and dialog.source.toPlainText()=='Keep this exact draft'
        assert dialog.mode.currentData()=='no_precise_time'
        dialog.close()
        reopened=PlanDialog(QueuedCoreBridge(core),date=QDate(2030,1,7))
        wait(app,lambda:reopened.bridge.pending==0)
        assert reopened.block_values()[0]['target_id']=='example'
        assert reopened.table.cellWidget(0,3).value()==0
        assert 'minutes' not in reopened.block_values()[0]
        assert reopened.source.toPlainText()=='Keep this exact draft'
        assert core.query('state')==before and bridge.commands==[]
        assert PlanDrafts(core.root).read('different-epoch','2030-01-07') is None
    finally:
        close(dialog,app)
        if reopened:close(reopened,app)


def test_concurrent_drafts_compare_tokens_and_clear_tombstone(tmp_path):
    left,right=PlanDrafts(tmp_path),PlanDrafts(tmp_path)
    token=left.write('epoch','2030-01-07',{'source_text':'first'},None)
    assert right.read('epoch','2030-01-07')['token']==token
    with pytest.raises(DraftConflict):right.write('epoch','2030-01-07',{'source_text':'stale'},None)
    left.clear('epoch','2030-01-07',token)
    with pytest.raises(DraftConflict):right.write('epoch','2030-01-07',{'source_text':'resurrect'},token)
    assert left.read('epoch','2030-01-07')['payload'] is None


def test_second_window_cannot_silently_overwrite_draft_or_close(app,tmp_path):
    bridge=ControlledBridge();bridge.data_dir=tmp_path
    left=PlanDialog(bridge,date=QDate(2030,1,7));ready(bridge)
    right=PlanDialog(bridge,date=QDate(2030,1,7));ready(bridge)
    try:
        left.source.setPlainText('left');assert left.persist_draft()
        right.source.setPlainText('right');assert not right.persist_draft()
        right.reject();assert not right.closed
        assert right.source.toPlainText()=='right'
        assert bridge._plan_drafts.read(bridge.epoch,'2030-01-07')['payload']['source_text']=='left'
        right.save();assert not bridge.commands
    finally:close(left,app);close(right,app)


def test_data_epoch_change_does_not_move_old_input_into_new_space(app,tmp_path):
    bridge=ControlledBridge();bridge.data_dir=tmp_path
    dialog=PlanDialog(bridge,date=QDate(2030,1,7))
    try:
        ready(bridge);dialog.insert_block(task('old-space-task'))
        dialog.load_context()
        bridge.deliver('plan_context',{'hard_events':[],'epoch':'replacement-space','revision':20})
        assert dialog.draft_blocked and dialog.epoch=='synthetic-epoch'
        assert dialog.persist_draft()
        assert bridge._plan_drafts.read('replacement-space','2030-01-07') is None
        dialog.save();assert not bridge.commands
    finally:close(dialog,app)


def test_old_plan_draft_cannot_overwrite_new_plan(app,tmp_path):
    core=Core(tmp_path);bridge=QueuedCoreBridge(core)
    entity=cmd(core,'create',{'type':'task','title':'A','data':{'completion_gate':'Original'}})['result']['entity']
    dialog=PlanDialog(bridge,date=QDate(2030,1,7));reopened=None
    try:
        wait(app,lambda:bridge.pending==0)
        dialog.insert_block(entity);dialog.source.setPlainText('Draft before other edit');assert dialog.persist_draft();dialog.close()
        current=cmd(core,'create_plan',{'date':'2030-01-07','blocks':[{'target_id':entity['id']}],'mode':'no_precise_time'})['result']['entity']
        reopened=PlanDialog(bridge,date=QDate(2030,1,7));wait(app,lambda:bridge.pending==0)
        assert reopened.draft_blocked
        assert not reopened.buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled()
        reopened.save();assert bridge.commands==[]
        assert reopened.persist_draft()
        assert bridge._plan_drafts.read(bridge.epoch,'2030-01-07')['payload']['plan'] is None
        assert core.query('daily_review',date='2030-01-07')['plan']['id']==current['id']
    finally:
        close(dialog,app)
        if reopened:close(reopened,app)


def test_save_is_single_flight_and_failure_keeps_draft(app):
    bridge=ControlledBridge();dialog=PlanDialog(bridge,date=QDate(2030,1,7))
    try:
        ready(bridge);dialog.insert_block(task('a'));dialog.save();dialog.save();dialog.reject()
        assert len(bridge.commands)==1 and not dialog.closed
        bridge.deliver('task_pool',{'items':[],'total':0})
        dialog.load_candidates(0)
        bridge.take('task_pool')['error']({'message':'A late read failed'})
        assert dialog.saving and not dialog.body.isEnabled()
        dialog.save();dialog.reject();assert len(bridge.commands)==1 and not dialog.closed
        bridge.commands[0]['error']({'code':'dependency','message':'先完成前置任务'})
        assert dialog.table.rowCount()==1 and dialog.dirty and not dialog.saving
        assert '前置任务' in dialog.error_label.text()
        assert bridge._plan_drafts.read(bridge.epoch,'2030-01-07')['payload']['rows'][0]['block']['target_id']=='a'
    finally:close(dialog,app)


@pytest.mark.parametrize('style',['classic','glass'])
def test_today_empty_state_is_manually_actionable_without_assistant(app,style):
    previous=visual_style();set_visual_style(style)
    bridge=ControlledBridge();opened=[];page=TodayPage(bridge,on_manual=opened.append)
    try:
        page.review_result={'has_plan':False,'can_review':False}
        page.render_plan(page.review_result)
        assert page.plan_button.isHidden()
        buttons=page.plan_box.findChildren(QPushButton)
        manual=next(w for w in buttons if isinstance(w,QPushButton) and w.text()=='安排这一天')
        manual.click();assert opened==[page.date_iso()]
        page.set_assistants_visible(True);assert not page.plan_button.isHidden()
        page.set_assistants_visible(False);assert page.plan_button.isHidden()
        assert page.manual_button.isEnabled()
    finally:
        page.close();page.deleteLater();app.processEvents()
        set_visual_style(previous)
