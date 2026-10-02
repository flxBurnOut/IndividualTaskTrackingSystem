"""Dated manual planning and progressive everyday preference controls."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import QDate,Qt
from PySide6.QtWidgets import QApplication,QLabel,QPushButton,QWidget
from management.core import Core
from management.gui_workflows import PlanDialog
from management.gui_habits import HabitsPanel,HabitEditor
from management import habits
from test_ux_workflows_v2 import QueuedCoreBridge,ControlledBridge,wait
from test_plan_assistance_v8 import cmd

@pytest.fixture(scope='session')
def app():return QApplication.instance() or QApplication([])

def create(core,kind,title,**kw):return cmd(core,'create',{'type':kind,'title':title,**kw})['entity']

def close(widget,app):
    widget.close();widget.deleteLater();app.processEvents()

def test_manual_day_shows_all_open_candidates_courses_and_no_machine_ids(app,tmp_path):
    core=Core(tmp_path)
    course=create(core,'course','SC3060 — Graphics')
    old=create(core,'task','Tutorial 4',parent_id=course['id'],data={'due_date':'2030-01-06'})
    today=create(core,'task','Tutorial 5',parent_id=course['id'],data={'due_date':'2030-01-07'})
    future=create(core,'task','Tutorial 6',parent_id=course['id'],data={'scheduled_date':'2030-01-09'})
    event=create(core,'event','Today lecture',data={'owner_id':course['id'],'date':'2030-01-07','start':'10:00','end':'11:00','hard':True,'time_kind':'exact'})
    create(core,'event','Tomorrow tutorial',data={'owner_id':course['id'],'date':'2030-01-08','start':'10:00','end':'11:00','hard':True,'time_kind':'exact'})
    unknown=create(core,'event','Time not announced',data={'owner_id':course['id'],'hard':True,'time_kind':'date_only'})
    bridge=QueuedCoreBridge(core);dialog=PlanDialog(bridge,date=QDate(2030,1,7));dialog.show()
    try:
        wait(app,lambda:bridge.pending==0)
        assert 'Today lecture' in dialog.context.text() and 'Tomorrow tutorial' not in dialog.context.text()
        assert 'Time not announced' not in dialog.context.text() and 'Time not announced' in dialog.unknowns.text()
        assert dialog.unknowns.isHidden()
        parent=dialog.candidates.topLevelItem(0)
        assert 'SC3060' in parent.text(0) and parent.childCount()==3
        assert {parent.child(i).data(0,Qt.ItemDataRole.UserRole)['id'] for i in range(3)}=={old['id'],today['id'],future['id']}
        for identifier in [old['id'],today['id'],event['id'],unknown['id']]:
            assert identifier not in '\n'.join(label.text() for label in dialog.findChildren(QLabel))
        selected=next(parent.child(i) for i in range(parent.childCount()) if parent.child(i).data(0,Qt.ItemDataRole.UserRole)['id']==old['id'])
        selected.setCheckState(0,Qt.CheckState.Checked);dialog.add_checked();dialog.insert_block(old)
        assert dialog.table.rowCount()==1 and 'SC3060' in dialog.table.cellWidget(0,0).text()
        dialog.save();wait(app,lambda:bridge.pending==0)
        assert core.query('daily_review',date='2030-01-07')['items'][0]['target_id']==old['id']
    finally:close(dialog,app)

def test_manual_plan_loads_existing_completed_blocks_and_revises_safely(app,tmp_path):
    core=Core(tmp_path);course=create(core,'course','IE2108')
    done=create(core,'task','Lecture 1',parent_id=course['id'],data={'completion_gate':'Review the notes'})
    new=create(core,'task','Lecture 2',parent_id=course['id'],data={'due_date':'2030-01-07'})
    original=cmd(core,'create_plan',{'date':'2030-01-07','mode':'no_precise_time','blocks':[{'target_id':done['id'],'completion_gate':'Review the notes','minutes':20}]})['entity']
    cmd(core,'record_feedback',{'target_id':done['id'],'business_date':'2030-01-07','dimensions':{'completion':'done'},'source_text':'Explicitly finished'})
    bridge=QueuedCoreBridge(core);dialog=PlanDialog(bridge,date=QDate(2030,1,7));dialog.show()
    try:
        wait(app,lambda:bridge.pending==0)
        assert dialog.table.rowCount()==1 and dialog.plan['id']==original['id']
        assert 'IE2108' in dialog.table.cellWidget(0,0).text()
        dialog.add_candidate(dialog.candidates.topLevelItem(0).child(0));dialog.save();wait(app,lambda:bridge.pending==0)
        assert bridge.commands[-1][0]=='revise_plan'
        review=core.query('daily_review',date='2030-01-07')
        assert review['summary']['done']==1 and len(review['items'])==2
        current=core.query('get',id=review['plan']['id'])['entity']
        assert current['data']['blocks'][0]==original['data']['blocks'][0]
    finally:close(dialog,app)

def test_old_date_responses_do_not_replace_new_candidate_context(app):
    bridge=ControlledBridge();dialog=PlanDialog(bridge,date=QDate(2030,1,7));old=bridge.take('task_pool');old_context=bridge.take('plan_context');old_review=bridge.take('daily_review')
    try:
        dialog.date.setDate(QDate(2030,1,8))
        bridge.deliver('task_pool',{'items':[{'id':'new','title':'Current day task','data':{},'owner_label':'Course'}],'total':1})
        bridge.deliver('daily_review',{'plan':None});bridge.deliver('plan_context',{'hard_events':[],'unknowns':[],'revision':20,'epoch':'synthetic-epoch'})
        old['callback']({'items':[{'id':'old','title':'Old task'}],'total':1})
        old_context['callback']({'hard_events':[{'title':'Old event'}]});old_review['callback']({'plan':{'id':'old-plan','version':1}})
        assert dialog.candidates.topLevelItem(0).child(0).text(0)=='Current day task'
        assert 'Old event' not in dialog.context.text() and dialog.plan is None
    finally:close(dialog,app)

def test_move_plan_blocks_preserves_identity_and_input(app):
    bridge=ControlledBridge();dialog=PlanDialog(bridge,date=QDate(2030,1,7))
    try:
        bridge.deliver('daily_review',{'plan':None});bridge.deliver('plan_context',{'hard_events':[],'epoch':bridge.epoch,'revision':10})
        dialog.insert_block({'id':'first','title':'First','data':{}})
        dialog.insert_block({'id':'second','title':'Second','data':{}})
        dialog.table.cellWidget(0,4).setText('First gate');dialog.table.setCurrentCell(0,0);dialog.move_block(1);app.processEvents()
        assert dialog.table.cellWidget(1,0).property('target_id')=='first'
        assert dialog.table.cellWidget(1,4).text()=='First gate'
        assert dialog.table.cellWidget(0,0).property('target_id')=='second'
    finally:close(dialog,app)

def test_habits_hide_raw_rules_and_secondary_controls_until_requested(app,tmp_path):
    core=Core(tmp_path);rule=create(core,'rule','主动触发与低状态',data={'rule_kind':'behavior','policy':'Only plan when requested. Use one required task on low-energy days.'})
    before=core.query('state');bridge=QueuedCoreBridge(core);panel=HabitsPanel(bridge,QWidget());panel.show()
    try:
        wait(app,lambda:bridge.pending==0)
        assert panel.prep_details.isHidden() and panel.rule_details.isHidden()
        assert panel.codex_preparation.isEnabled() and panel.codex_preparation.text()=='请助手建议准备规则'
        assert panel.prep_toggle.objectName()=='Primary' and panel.new_rule_button.objectName()=='Primary'
        visible=[b for b in panel.findChildren(QPushButton) if b.isVisible()]
        assert len(visible)<=6
        panel.rules_toggle.click()
        assert panel.rule_details.isVisible() and '何时安排一天、累了怎么安排' in panel.rules.item(0).text()
        assert 'Only plan' not in panel.rule_info.text()
        panel.edit_rule();editor=panel.dialogs[0]
        assert editor.policy.toPlainText()==rule['data']['policy'] and editor.title.text()==rule['title']
        editor.close();assert core.query('state')==before
    finally:close(panel,app)

def test_human_alias_is_projection_only(app,tmp_path):
    core=Core(tmp_path);rule=create(core,'rule','重课日保护',data={'rule_kind':'behavior','policy':'Original precise rule','custom':'kept'})
    viewed=habits.rule_view(rule,'2030-01-07')
    assert viewed['display_title']=='课多的日子少加任务'
    assert viewed['title']==rule['title'] and viewed['data']==rule['data']
    stored=core.query('get',id=rule['id'])['entity']
    assert stored['title']=='重课日保护' and stored['data']['custom']=='kept'
