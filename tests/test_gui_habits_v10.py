"""Discoverable habits, safe edits and real projections in the native UI."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import datetime as dt
import pytest
from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtCore import QCoreApplication,QEvent
from management.core import Core
from management import habits
from management.gui_habits import HabitsPanel,HabitEditor
from management.gui_today import TodayPage
from management.gui_workflows import SettingsDialog
from test_ux_workflows_v2 import QueuedCoreBridge,ControlledBridge,wait
from test_plan_assistance_v8 import cmd
from test_recurring_v4 import event

@pytest.fixture(scope='session')
def app():return QApplication.instance() or QApplication([])


def test_settings_opens_on_habits_and_shows_real_effects_without_writing(app,tmp_path,monkeypatch):
    core=Core(tmp_path);cmd(core,'create',{'type':'rule','title':'Only a preference','data':{'rule_kind':'behavior','policy':'Preserve sleep'}})
    before=core.query('state');bridge=QueuedCoreBridge(core);dialog=SettingsDialog(bridge,core.query('capabilities'),tmp_path);dialog.show()
    try:
        wait(app,lambda:bridge.pending==0)
        assert dialog.tabs.tabText(0)=='日常习惯'
        assert '未启用' in dialog.habits.reminder_summary.text() and '尚未设置' in dialog.habits.prep_summary.text()
        assert '参考' in dialog.habits.rule_info.text()
        assert core.query('state')==before
        dialog.habits.reminder_toggle.click();assert dialog.habits.reminder.isVisible() and not dialog.habits.prep_box.isVisible()
    finally:dialog.close();dialog.deleteLater();app.processEvents()


def test_today_has_a_visible_route_to_habits(app,tmp_path):
    core=Core(tmp_path);bridge=QueuedCoreBridge(core);opened=[];page=TodayPage(bridge,on_habits=lambda:opened.append(True));page.show();page.refresh()
    try:
        wait(app,lambda:bridge.pending==0)
        assert '日常习惯' in page.habits_button.text() and '自动准备待办未设置' in page.habits_button.text()
        page.habits_button.click();assert opened==[True]
    finally:page.close();page.deleteLater();app.processEvents()


def test_warning_lead_time_is_suggested_without_enabling_or_creating_tasks(app,tmp_path,monkeypatch):
    monkeypatch.setattr(habits,'_now',lambda:dt.datetime(2030,1,2,2,tzinfo=dt.timezone.utc))
    core=Core(tmp_path);cmd(core,'create',{'type':'rule','title':'D-1','data':{'rule_kind':'warning','days_before':1,'event_kind':['tutorial']}})
    anchor=event(core,date='2030-01-04',event_kind='tutorial');bridge=QueuedCoreBridge(core);panel=HabitsPanel(bridge,QWidget());panel.show()
    try:
        wait(app,lambda:bridge.pending==0);panel.anchor.setCurrentIndex(panel.anchor.findData(anchor['id']));panel.create_preparation.click();wait(app,lambda:bridge.pending==0)
        dialog=panel.dialogs[0];assert dialog.days.value()==1 and not dialog.dirty
        assert core.query('recurring_rules')['total']==0 and core.query('list',type='task')['total']==0
        dialog.close()
    finally:panel.close();panel.deleteLater();app.processEvents()


def test_edit_keeps_other_fields_and_pause_removes_rule_from_planning(app,tmp_path):
    core=Core(tmp_path);entity=cmd(core,'create',{'type':'rule','title':'Rest','data':{'rule_kind':'behavior','policy':'Preserve known rest','custom_field':'retain'}})['entity']
    bridge=QueuedCoreBridge(core);dialog=HabitEditor(bridge,entity=entity);dialog.show()
    try:
        dialog.enabled.setChecked(False);dialog.save();wait(app,lambda:bridge.pending==0)
        saved=core.query('get',id=entity['id'])['entity']
        assert saved['data']['enabled'] is False and saved['data']['custom_field']=='retain'
        assert core.query('plan_context',date='2030-01-02')['rules']==[]
    finally:dialog.dirty=False;dialog.close();dialog.deleteLater();app.processEvents()


def test_stale_editor_preserves_input_and_rejects_overwrite(app,tmp_path):
    core=Core(tmp_path);entity=cmd(core,'create',{'type':'rule','title':'Rest','data':{'rule_kind':'behavior','policy':'Original'}})['entity']
    bridge=QueuedCoreBridge(core);dialog=HabitEditor(bridge,entity=entity);dialog.show()
    try:
        dialog.policy.setPlainText('My unsaved change')
        cmd(core,'update',{'id':entity['id'],'version':entity['version'],'patch':{'data':{'policy':'Other entrance change'}}})
        dialog.save();wait(app,lambda:bridge.pending==0)
        assert dialog.policy.toPlainText()=='My unsaved change' and dialog.note.text()
        assert core.query('get',id=entity['id'])['entity']['data']['policy']=='Other entrance change'
    finally:dialog.dirty=False;dialog.close();dialog.deleteLater();app.processEvents()


def test_late_overview_does_not_touch_destroyed_panel(app):
    bridge=ControlledBridge();panel=HabitsPanel(bridge,QWidget());request=bridge.take('habits_overview')
    panel.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    request['callback']({})
