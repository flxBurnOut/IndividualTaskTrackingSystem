"""Task -> explicit day plan -> review UX and anchored preparation form checks."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication,QLabel,QPushButton
from management.gui_today import TodayPage
from management.gui_workspace import TaskDetailDialog,WorkspacePage
from test_ux_workflows_v2 import ControlledBridge,QueuedCoreBridge,cmd,wait

@pytest.fixture(scope='session')
def app():return QApplication.instance() or QApplication([])

def task(identifier='task-a',title='Synthetic preparation',**data):return {'id':identifier,'type':'task','title':title,'version':1,'status':'active','data':data}

def texts(widget):return '\n'.join(w.text() for w in widget.findChildren(QLabel))+ '\n'+'\n'.join(w.text() for w in widget.findChildren(QPushButton))

def test_task_detail_waits_for_snapshot_and_creates_only_explicit_plan(app):
    bridge=ControlledBridge();dialog=TaskDetailDialog(bridge,task(),business_date='2030-01-07')
    try:
        assert not dialog.join_plan_button.isEnabled();assert bridge.commands==[]
        bridge.deliver('daily_tasks',{'date':'2030-01-07','items':[],'plan':None})
        dialog.join_plan_button.click();dialog.join_plan_button.click()
        assert len(bridge.commands)==1
        assert bridge.commands[0]['name']=='add_to_plan'
        assert bridge.commands[0]['payload']=={'date':'2030-01-07','target_id':'task-a','plan_id':None,'plan_version':None}
    finally:dialog.close()

def test_task_detail_old_date_reply_cannot_replace_current_plan_version(app):
    bridge=ControlledBridge();dialog=TaskDetailDialog(bridge,task(),business_date='2030-01-07')
    try:
        old=bridge.take('daily_tasks');dialog.plan_date.setDate(QDate(2030,1,8))
        bridge.deliver('daily_tasks',{'date':'2030-01-08','items':[],'plan':{'id':'plan-new','version':7}})
        old['callback']({'plan':{'id':'plan-old','version':1}})
        dialog.join_plan();assert bridge.commands[-1]['payload']['plan_id']=='plan-new'
        assert bridge.commands[-1]['payload']['plan_version']==7
        assert bridge.commands[-1]['payload']['date']=='2030-01-08'
    finally:dialog.close()

def test_task_detail_failed_write_preserves_date_and_requires_fresh_read(app):
    bridge=ControlledBridge();dialog=TaskDetailDialog(bridge,task(),business_date='2030-01-07')
    try:
        bridge.deliver('daily_tasks',{'plan':{'id':'plan','version':2}});dialog.join_plan()
        bridge.commands[-1]['error']({'code':'conflict','message':'Plan changed elsewhere'})
        assert dialog.plan_date.date()==QDate(2030,1,7)
        assert dialog.join_plan_button.text()=='重新读取安排'
        dialog.join_plan_button.click();assert len(bridge.commands)==1
        bridge.deliver('daily_tasks',{'plan':{'id':'plan','version':3}});dialog.join_plan_button.click()
        assert bridge.commands[-1]['payload']['plan_version']==3
    finally:dialog.close()

def test_today_candidates_show_reason_and_join_exact_selected_day(app):
    bridge=ControlledBridge();page=TodayPage(bridge);page.set_date('2030-01-07')
    try:
        result={'date':'2030-01-07','items':[dict(task(),reason='截止在这一天',reason_code='due_today')],'plan':{'id':'plan','version':4},'total':12,'next_offset':10}
        bridge.deliver('daily_tasks',result)
        assert '当天候选待办' in texts(page.tasks_box) and '截止在这一天' in texts(page.tasks_box)
        assert '是否今天处理由你决定' in texts(page.tasks_box)
        add=next(w for w in page.tasks_box.findChildren(QPushButton) if w.text()=='加入这天计划');add.click();page.add_task(task(),'2030-01-07',result['plan'])
        assert len(bridge.commands)==1
        assert bridge.commands[-1]['payload']=={'date':'2030-01-07','target_id':'task-a','plan_id':'plan','plan_version':4}
        page.set_tasks_page(10)
        query=bridge.take('daily_tasks');assert query['params']['offset']==10
    finally:page.close()

def test_today_old_date_tasks_do_not_leak_into_new_selected_day(app):
    bridge=ControlledBridge();page=TodayPage(bridge);page.set_date('2030-01-07');old=bridge.take('daily_tasks')
    try:
        page.set_date('2030-01-08')
        bridge.deliver('daily_tasks',{'date':'2030-01-08','items':[task('new','Current day')],'plan':None,'total':1})
        old['callback']({'date':'2030-01-07','items':[task('old','Wrong day')],'plan':None,'total':1})
        assert 'Current day' in texts(page.tasks_box) and 'Wrong day' not in texts(page.tasks_box)
        assert bridge.commands==[]
    finally:page.close()

def test_course_tasks_nodes_scores_and_notes_have_separate_meanings(app):
    bridge=ControlledBridge();page=WorkspacePage(bridge,on_create=lambda *args:None,on_edit=lambda *args:None)
    course={'id':'course','type':'course','title':'Synthetic course','data':{},'status':'active','version':1}
    page.set_types({'course':{'label':'课程'},'task':{'parent_types':['course']}})
    try:
        page.render({'entity':course,'children':[task(),{'id':'m','type':'milestone','title':'Submission deadline','data':{'due_date':'2030-01-12'},'status':'active'},{'id':'n','type':'note','title':'Source notes','data':{},'status':'active'}],'files':[],'summary':{},'course_info':{'assessments':[],'events':[]}})
        text=texts(page.content)
        assert '实际任务' in text and '交付与重要日期' in text and '课程笔记' in text
        assert '待办与节点' not in text and '评分规则说明课程如何计分，不是待完成任务' in text
        assert len([b for b in page.content.findChildren(QPushButton) if b.text()=='提前准备（一次）'])==1
        assert bridge.commands==[]
    finally:page.close()


def rule_entity():
    return {'id':'rule-a','version':3,'title':'课前练习','type':'recurring_task_rule','data':{'anchor_id':'event-a','content':'完成对应章节练习','completion_gate':'完成指定题目并核对','estimated_minutes':None,'days_before':2,'enabled':True,'effective_from':None,'effective_until':None}}


def test_recurring_form_requires_completion_gate_and_keeps_unknown_minutes(app):
    from management.gui_recurring import RecurringDialog
    bridge=ControlledBridge();dialog=RecurringDialog(bridge,{'id':'event-a','type':'event','title':'每周课程'})
    try:
        bridge.deliver('recurring_rules',{'items':[],'total':0,'next_offset':None})
        dialog.content.setPlainText('完成对应章节练习');dialog.save();assert bridge.commands==[]
        dialog.gate.setPlainText('完成指定题目并核对');dialog.days.setValue(2);dialog.save()
        payload=bridge.commands[-1]['payload']
        assert payload['estimated_minutes'] is None and payload['days_before']==2
        assert payload['anchor_id']=='event-a' and payload['completion_gate']=='完成指定题目并核对'
        assert not any(key in payload for key in ('plan_id','blocks','date'))
        assert bridge.commands[-1]['name']=='set_recurring_rule'
    finally:dialog.saving=False;dialog.allow_close=True;dialog.close()


def test_recurring_edit_disable_keeps_rule_version_and_does_not_delete_tasks(app):
    from management.gui_recurring import RecurringDialog
    bridge=ControlledBridge();dialog=RecurringDialog(bridge,{'id':'event-a','type':'event','title':'每周课程'})
    try:
        bridge.deliver('recurring_rules',{'items':[rule_entity()],'total':1,'next_offset':None})
        dialog.rules.setCurrentRow(0);assert dialog.rule['id']=='rule-a'
        dialog.disable_rule();payload=bridge.commands[-1]['payload']
        assert payload['id']=='rule-a' and payload['version']==3 and payload['enabled'] is False
        assert len(bridge.commands)==1 and bridge.commands[-1]['name']=='set_recurring_rule'
        assert '不会删除、覆盖' in texts(dialog)
    finally:dialog.saving=False;dialog.allow_close=True;dialog.close()


def test_anchor_rebind_is_explicit_and_failed_save_retains_draft(app):
    from management.gui_recurring import RecurringDialog
    bridge=ControlledBridge();dialog=RecurringDialog(bridge,{'id':'event-a','type':'event','title':'每周课程'})
    try:
        bridge.deliver('recurring_rules',{'items':[rule_entity()],'total':1,'next_offset':None});dialog.rules.setCurrentRow(0)
        dialog.content.setPlainText('修订后的明确任务内容');dialog.save()
        assert 'acknowledge_anchor_change' not in bridge.commands[-1]['payload']
        bridge.commands[-1]['error']({'code':'recurring_anchor_changed','message':'Series changed; confirm first'})
        assert dialog.content.toPlainText()=='修订后的明确任务内容' and not dialog.ack.isHidden() and not dialog.ack.isChecked()
        dialog.ack.setChecked(True);dialog.save()
        assert bridge.commands[-1]['payload']['acknowledge_anchor_change'] is True
    finally:dialog.saving=False;dialog.allow_close=True;dialog.close()


def test_recurring_old_list_response_does_not_overwrite_current_draft(app):
    from management.gui_recurring import RecurringDialog
    bridge=ControlledBridge();dialog=RecurringDialog(bridge,{'id':'event-a','type':'event','title':'每周课程'})
    try:
        old=bridge.take('recurring_rules');dialog.load_rules()
        bridge.deliver('recurring_rules',{'items':[rule_entity()],'total':1,'next_offset':None});dialog.rules.setCurrentRow(0)
        dialog.content.setPlainText('保留我的草稿')
        old['callback']({'items':[],'total':0,'next_offset':None})
        assert dialog.rules.count()==1 and dialog.content.toPlainText()=='保留我的草稿'
        dialog.load_rules();bridge.deliver('recurring_rules',{'items':[rule_entity()],'total':1,'next_offset':None})
        assert dialog.content.toPlainText()=='保留我的草稿'
    finally:dialog.allow_close=True;dialog.close()


def test_real_core_candidate_to_plan_to_review_never_pulls_undated_backlog(app,tmp_path):
    from management.core import Core
    core=Core(tmp_path/'task-day-chain');day='2030-01-07'
    due=cmd(core,'create',{'type':'task','title':'Due task','data':{'due_date':day,'completion_gate':'Explicit outcome'}})['result']['entity']
    other=cmd(core,'create',{'type':'task','title':'Undated backlog','data':{}})['result']['entity']
    bridge=QueuedCoreBridge(core);page=TodayPage(bridge);page.set_date(day)
    try:
        wait(app,lambda:bridge.pending==0)
        assert due['title'] in texts(page.tasks_box) and other['title'] not in texts(page.tasks_box)
        assert not core.query('daily_review',date=day)['has_plan']
        add=next(w for w in page.tasks_box.findChildren(QPushButton) if w.text()=='加入这天计划');add.click();wait(app,lambda:bridge.pending==0)
        review=core.query('daily_review',date=day)
        assert [x['target_id'] for x in review['items']]==[due['id']]
        assert review['summary']['unreported']==1 and review['summary']['done']==0
        plan=core.query('daily_tasks',date=day)['plan'];assert plan['mode']=='no_precise_time'
        assert core.query('list',type='feedback')['total']==0
        from PySide6.QtCore import QCoreApplication,QEvent
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        assert due['title'] not in texts(page.tasks_box)
    finally:page.close()


def test_real_task_detail_append_retains_original_block_and_duplicate_is_reused(app,tmp_path):
    from management.core import Core
    core=Core(tmp_path/'append-plan');day='2030-01-07'
    first=cmd(core,'create',{'type':'task','title':'Existing planned task','data':{'completion_gate':'Original gate'}})['result']['entity']
    second=cmd(core,'create',{'type':'task','title':'New task','data':{'completion_gate':'New gate'}})['result']['entity']
    initial=cmd(core,'create_plan',{'date':day,'mode':'no_precise_time','blocks':[{'target_id':first['id'],'minutes':25}]})['result']['entity']
    bridge=QueuedCoreBridge(core);dialog=TaskDetailDialog(bridge,second,business_date=day)
    try:
        wait(app,lambda:bridge.pending==0);dialog.join_plan();wait(app,lambda:bridge.pending==0)
        snapshot=core.query('daily_tasks',date=day)['plan'];current=core.query('get',id=snapshot['id'])['entity']
        assert current['data']['blocks'][0]==initial['data']['blocks'][0]
        assert current['data']['blocks'][1]['target_id']==second['id']
        assert current['data']['blocks'][1].get('minutes') is None
        count=core.query('list',type='plan')['total'];dialog.join_plan();wait(app,lambda:bridge.pending==0)
        assert core.query('list',type='plan')['total']==count
    finally:dialog.close()


def test_real_recurring_form_generates_candidate_without_plan_or_feedback(app,tmp_path):
    import datetime as dt
    from management.core import Core
    from management.gui_recurring import RecurringDialog
    core=Core(tmp_path/'recurring-form');day=QDate.currentDate().toString('yyyy-MM-dd');anchor_day=QDate.currentDate().addDays(2).toString('yyyy-MM-dd')
    course=cmd(core,'create',{'type':'course','title':'Synthetic course','data':{}})['result']['entity']
    event=cmd(core,'create',{'type':'event','title':'Every week class','data':{'owner_id':course['id'],'date':anchor_day,'start':'10:00','end':'11:00','recurrence':'weekly'}})['result']['entity']
    bridge=QueuedCoreBridge(core);dialog=RecurringDialog(bridge,event)
    try:
        wait(app,lambda:bridge.pending==0);dialog.content.setPlainText('完成本次课前练习');dialog.gate.setPlainText('完成三题并核对答案');dialog.days.setValue(2);dialog.save();wait(app,lambda:bridge.pending==0)
        assert dialog.rule,dialog.note.text()
        candidates=core.query('daily_tasks',date=day)['items']
        assert len(candidates)==1 and candidates[0]['data']['recurring_rule_id']==dialog.rule['id']
        assert candidates[0]['data']['scheduled_date']==day
        assert core.query('list',type='plan')['total']==0 and core.query('list',type='feedback')['total']==0
        dialog.disable_rule();wait(app,lambda:bridge.pending==0)
        rules=core.query('recurring_rules',anchor_id=event['id'])['items']
        assert rules[0]['data']['enabled'] is False and core.query('list',type='task')['total']==1
    finally:dialog.saving=False;dialog.allow_close=True;dialog.close()
