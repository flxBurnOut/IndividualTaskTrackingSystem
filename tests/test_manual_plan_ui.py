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
    dialog._prefill_pending=False;dialog._prefill_timer.stop()
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


def test_batch_arrangement_prefills_in_reviewed_order_without_saving_a_plan(app):
    bridge=ControlledBridge();dialog=PlanDialog(bridge,date=QDate(2030,1,7),task_ids=['a','b','a'])
    try:
        ready(bridge);dialog._prefill_tasks()
        bridge.deliver('get',{'entity':task('b')},id='b')
        assert dialog.table.rowCount()==0
        bridge.deliver('get',{'entity':task('a')},id='a')
        assert [block['target_id'] for block in dialog.block_values()]==['a','b']
        assert not bridge.commands and dialog.dirty
        assert '草稿' in dialog.draft_note.text() and '保存计划' in dialog.draft_note.text()
    finally:close(dialog,app)


def test_batch_arrangement_reports_partial_reads_and_drops_stale_date_results(app):
    bridge=ControlledBridge();dialog=PlanDialog(bridge,date=QDate(2030,1,7),task_ids=['a','missing'])
    try:
        ready(bridge);dialog._prefill_tasks()
        bridge.deliver('get',{'entity':task('a')},id='a')
        bridge.take('get',id='missing')['error']({'code':'not_found','message':'Removed'})
        assert [block['target_id'] for block in dialog.block_values()]==['a']
        assert '1 项未能读取' in dialog.error_label.text()
        assert not bridge.commands
    finally:close(dialog,app)
    bridge=ControlledBridge();dialog=PlanDialog(bridge,date=QDate(2030,1,7),task_ids=['a'])
    try:
        ready(bridge);dialog._prefill_tasks()
        dialog.date.setDate(QDate(2030,1,8));ready(bridge)
        bridge.deliver('get',{'entity':task('a')},id='a')
        assert dialog.table.rowCount()==0 and not bridge.commands
    finally:close(dialog,app)


@pytest.mark.parametrize('code',['connection_lost','timeout','protocol_error'])
def test_plan_unknown_save_retains_original_request_date_and_blocks_close(app,code):
    bridge=ControlledBridge();dialog=PlanDialog(bridge,date=QDate(2030,1,7))
    try:
        ready(bridge);dialog.insert_block(task('a'));dialog.save()
        original=bridge.commands[-1]
        original['error']({'code':code,'message':'Receipt unknown'})
        dialog.reject();assert not dialog.closed and not dialog.body.isEnabled()
        dialog.date.setDate(QDate(2030,1,8))
        assert dialog.date.date().toString('yyyy-MM-dd')=='2030-01-07'
        dialog.save();retry=bridge.commands[-1]
        assert retry['options']==original['options'] and retry['payload']==original['payload']
        retry['error']({'code':'authentication','message':'Temporarily unavailable'})
        dialog.reject();assert not dialog.closed and dialog._plan_uncertain
        dialog.save();bridge.commands[-1]['callback']({'result':{},'epoch':bridge.epoch,'revision':11})
        assert dialog.closed and not dialog.dirty
    finally:
        dialog.saving=False;dialog._plan_uncertain=False
        close(dialog,app)


@pytest.mark.parametrize('style',['classic','glass'])
def test_today_empty_state_is_manually_actionable_without_assistant(app,style):
    previous=visual_style();set_visual_style(style)
    bridge=ControlledBridge();opened=[];page=TodayPage(bridge,on_manual=opened.append)
    try:
        page.review_result={'has_plan':False,'can_review':False}
        page.render_plan(page.review_result)
        assert page.plan_button.isHidden()
        buttons=page.plan_box.findChildren(QPushButton)
        manual=next(w for w in buttons if isinstance(w,QPushButton) and w.text()=='详细安排…')
        manual.click();assert opened==[page.date_iso()]
        page.set_assistants_visible(True);assert not page.plan_button.isHidden()
        page.set_assistants_visible(False);assert page.plan_button.isHidden()
        assert page.manual_button.isEnabled()
    finally:
        page.close();page.deleteLater();app.processEvents()
        set_visual_style(previous)


def test_today_quick_capture_creates_then_joins_selected_date_without_guessing_dates(app, tmp_path):
    core = Core(tmp_path / 'quick-capture')
    bridge = QueuedCoreBridge(core); page = TodayPage(bridge); page.set_date('2038-05-01')
    try:
        wait(app, lambda: bridge.pending == 0)
        page.quick_input.setText('Synthetic one-step capture')
        page.save_quick(); page.save_quick()
        wait(app, lambda: bridge.pending == 0)
        items = core.query('task_pool', date='2038-05-01')['items']
        assert len(items) == 1
        assert items[0]['data'] == {} and items[0]['in_plan']
        plan = core.query('daily_review', date='2038-05-01')['plan']
        assert plan['mode'] == 'no_precise_time'
        assert [name for name, _, _ in bridge.commands] == ['create', 'add_to_plan']
        assert '已加入 2038-05-01' in page.quick_note.text() and not page.pending_writes()
        assert page.quick_input.text() == ''
    finally:
        page.close(); page.deleteLater(); app.processEvents()


def test_today_partial_success_retries_only_join_and_preserves_original_day(app):
    bridge = ControlledBridge(); page = TodayPage(bridge); page.set_date('2038-05-01')
    try:
        page.quick_input.setText('Already captured'); page.save_quick()
        create_request = bridge.commands[-1]
        page.set_date('2038-05-02')
        create_request['callback']({'result': {'entity': {'id': 'new-task', 'title': 'Already captured'}}})
        query = bridge.take('task_pool'); assert query['params']['date'] == '2038-05-01'
        query['callback']({'plan': None, 'epoch': bridge.epoch, 'revision': 11})
        join = bridge.commands[-1]; assert join['payload']['date'] == '2038-05-01'
        join['error']({'code': 'plan_conflict', 'message': 'Changed plan'})
        assert '任务已记下，尚未加入 2038-05-01' in page.quick_note.text()
        assert not page.quick_retry.isHidden() and not page.quick_keep.isHidden()
        page.retry_join()
        bridge.deliver('task_pool', {'plan': {'id': 'latest', 'version': 2}, 'epoch': bridge.epoch, 'revision': 12})
        assert [request['name'] for request in bridge.commands] == ['create', 'add_to_plan', 'add_to_plan']
        assert bridge.commands[-1]['payload'] == {'date': '2038-05-01', 'target_id': 'new-task', 'plan_id': 'latest', 'plan_version': 2}
        bridge.commands[-1]['callback']({'result': {}})
        assert page.date_iso() == '2038-05-02' and not page.pending_writes()
    finally:
        page.close(); page.deleteLater(); app.processEvents()


@pytest.mark.parametrize('step', ['create', 'join'])
def test_today_unknown_write_reuses_exact_request_even_after_date_changes(app, step):
    import copy
    bridge = ControlledBridge(); page = TodayPage(bridge); page.set_date('2038-05-01')
    try:
        page.quick_input.setText('Original capture'); page.save_quick()
        if step == 'join':
            bridge.commands[-1]['callback']({'result': {'entity': {'id': 'task-one', 'title': 'Original capture'}}})
            bridge.deliver('task_pool', {'plan': None})
        first = bridge.commands[-1]
        expected = copy.deepcopy({key: first[key] for key in ('name', 'payload', 'options')})
        first['error']({'code': 'connection_lost', 'message': 'Lost response'})
        page.set_date('2038-05-02')
        assert page.pending_writes()
        if step == 'join':
            page.keep_task_only(); assert page.join_target
            page.retry_join()
        else:
            assert page.quick_input.isReadOnly()
            page.save_quick()
        second = {key: bridge.commands[-1][key] for key in ('name', 'payload', 'options')}
        assert second == expected
        bridge.commands[-1]['error']({'code': 'authentication', 'message': 'Not yet reconnected'})
        assert page.pending_writes()
        assert len([item for item in bridge.commands if item['name'] == 'create']) == (2 if step == 'create' else 1)
    finally:
        page.close(); page.deleteLater(); app.processEvents()


def test_today_rest_day_does_not_cancel_rest_and_new_task_remains_available(app, tmp_path):
    core = Core(tmp_path / 'rest-day'); day = '2038-05-01'
    cmd(core, 'create_plan', {'date': day, 'mode': 'rest', 'blocks': []})
    original = core.query('daily_review', date=day)['plan']
    bridge = QueuedCoreBridge(core); page = TodayPage(bridge); page.set_date(day)
    try:
        wait(app, lambda: bridge.pending == 0)
        page.quick_input.setText('Keep for later'); page.save_quick()
        wait(app, lambda: bridge.pending == 0)
        assert core.query('daily_review', date=day)['plan'] == original
        assert core.query('list', type='task')['total'] == 1
        assert [name for name, _, _ in bridge.commands] == ['create']
        assert '休息日' in page.quick_note.text()
        page.keep_task_only(); assert page.quick_button.isEnabled() and not page.pending_writes()
    finally:
        page.close(); page.deleteLater(); app.processEvents()


def test_today_existing_task_picker_includes_undated_tasks_and_does_not_write_on_open(app, tmp_path):
    from management.gui_today import DayTaskPicker
    core = Core(tmp_path / 'task-picker'); day = '2038-05-01'
    undated = cmd(core, 'create', {'type': 'task', 'title': 'No date needed'})['result']['entity']
    bridge = QueuedCoreBridge(core); picker = DayTaskPicker(bridge, day)
    try:
        wait(app, lambda: bridge.pending == 0)
        assert picker.items.count() == 1 and not bridge.commands
        picker.items.setCurrentRow(0); picker.choose()
        assert picker.selected['id'] == undated['id'] and not bridge.commands
    finally:
        picker.close(); picker.deleteLater(); app.processEvents()



def test_batch_prefill_blocks_empty_save_and_close_until_all_tasks_are_read(app):
    bridge=ControlledBridge();dialog=PlanDialog(bridge,date=QDate(2030,1,7),task_ids=['a','b'])
    try:
        ready(bridge);dialog.show();app.processEvents()
        assert dialog._prefill_pending and not dialog.buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled()
        dialog.save();dialog.reject();assert not dialog.closed and not bridge.commands
        dialog._prefill_tasks();bridge.deliver('get',{'entity':task('b')},id='b')
        dialog.save();dialog.close();assert not dialog.closed and not bridge.commands and dialog.table.rowCount()==0
        bridge.deliver('get',{'entity':task('a')},id='a')
        assert not dialog._prefill_pending and [block['target_id'] for block in dialog.block_values()]==['a','b']
        dialog.save();assert len(bridge.commands)==1
        assert [block['target_id'] for block in bridge.commands[0]['payload']['blocks']]==['a','b']
        bridge.commands[0]['callback']({'result':{},'epoch':bridge.epoch,'revision':11})
        assert dialog.closed
    finally:
        dialog.saving=False;dialog._plan_uncertain=False;close(dialog,app)


def test_batch_prefill_survives_same_day_mode_refresh_and_keeps_reviewed_order(app):
    bridge=ControlledBridge();dialog=PlanDialog(bridge,date=QDate(2030,1,7),task_ids=['a','b'])
    try:
        ready(bridge);dialog._prefill_tasks()
        dialog.mode.setCurrentIndex(dialog.mode.findData('no_precise_time'))
        bridge.deliver('get',{'entity':task('b')},id='b')
        bridge.deliver('get',{'entity':task('a')},id='a')
        assert dialog._prefill_pending and dialog.table.rowCount()==0 and not dialog.context_ready
        bridge.deliver('plan_context',{'hard_events':[],'unknowns':[],'epoch':bridge.epoch,'revision':12})
        dialog._prefill_tasks()
        assert not dialog._prefill_pending and [block['target_id'] for block in dialog.block_values()]==['a','b']
        assert len([query for query in bridge.queries if query['name']=='get'])==2
        dialog.save();assert bridge.commands[-1]['payload']['mode']=='no_precise_time'
        assert bridge.commands[-1]['options']['expected_revision']==12
        bridge.commands[-1]['error']({'code':'validation','message':'Synthetic rejection for cleanup'})
    finally:close(dialog,app)


def test_batch_prefill_explicit_cancel_ignores_late_reads_without_creating_plan(app):
    bridge=ControlledBridge();dialog=PlanDialog(bridge,date=QDate(2030,1,7),task_ids=['a'])
    try:
        ready(bridge);dialog._prefill_tasks();dialog.cancel_prefill_button.click()
        bridge.deliver('get',{'entity':task('a')},id='a')
        assert not dialog._prefill_pending and dialog.table.rowCount()==0 and not bridge.commands
        assert '仍保存在任务库' in dialog.prefill_note.text()
        dialog.reject();assert dialog.closed
    finally:close(dialog,app)


@pytest.mark.parametrize('code',['plan_conflict','plan_history','context_changed','schedule_conflict','capacity','completion_gate','uncertain_time','plan_target','limit','duplicate','dependency'])
def test_plan_receipt_retry_business_rejection_unlocks_preserved_draft(app,code):
    bridge=ControlledBridge();dialog=PlanDialog(bridge,date=QDate(2030,1,7))
    try:
        ready(bridge);dialog.insert_block(task('a'));dialog.source.setPlainText('Preserve this input');dialog.save()
        first=bridge.commands[-1];first['error']({'code':'connection_lost','message':'Unknown'})
        dialog.save();retry=bridge.commands[-1]
        assert retry['payload']==first['payload'] and retry['options']==first['options']
        retry['error']({'code':code,'message':'Explicit business rejection'})
        assert not dialog._plan_uncertain and dialog._plan_submission is None and dialog.body.isEnabled()
        assert dialog.source.toPlainText()=='Preserve this input' and dialog.table.rowCount()==1
        dialog.reject();assert dialog.closed
        assert bridge._plan_drafts.read(bridge.epoch,'2030-01-07')['payload']['rows'][0]['block']['target_id']=='a'
    finally:close(dialog,app)


@pytest.mark.parametrize('code',['service_unavailable','authentication','upgrade_pending','epoch_conflict','stale_epoch','unrecognized_error'])
def test_plan_receipt_retry_reconnect_errors_cannot_unlock_original_request(app,code):
    bridge=ControlledBridge();dialog=PlanDialog(bridge,date=QDate(2030,1,7))
    try:
        ready(bridge);dialog.insert_block(task('a'));dialog.save()
        first=bridge.commands[-1];first['error']({'code':'timeout','message':'Unknown'})
        dialog.save();bridge.commands[-1]['error']({'code':code,'message':'Cannot confirm receipt'})
        dialog.reject();assert dialog._plan_uncertain and not dialog.closed and not dialog.body.isEnabled()
        dialog.save();assert bridge.commands[-1]['payload']==first['payload'] and bridge.commands[-1]['options']==first['options']
    finally:
        dialog.saving=False;dialog._plan_uncertain=False;close(dialog,app)
