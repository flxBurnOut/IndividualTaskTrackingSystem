"""Timetable UI remains explicit, bounded and safe across asynchronous reads."""
import datetime as dt
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QDate, QEvent, QTime
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid
from management.gui_timetable import (TimetableDialog, TimetableManageDialog, TimetableRowDialog,
                                      WeekGrid, interval_lanes, monday, parse_weeks)
from test_ux_workflows_v2 import ControlledBridge, QueuedCoreBridge, cmd, wait


@pytest.fixture(scope='session')
def app(): return QApplication.instance() or QApplication([])


@pytest.fixture
def cleanup(app):
    windows = []
    yield windows
    for window in reversed(windows):
        if not isValid(window): continue
        window.dirty = False; window.pending = False
        window.close(); window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def table(id='table-a', version=2):
    return {'id':id,'type':'timetable','title':'合成课程表','version':version,'status':'active',
            'data':{'semester_start':'2030-01-07','semester_end':'2030-04-14','timezone':'Asia/Shanghai','source_text':'合成手动信息'}}


def test_exception_controls_wrap_at_large_font_without_hiding_text(app, cleanup):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QScrollArea
    from management.gui_theme import apply_appearance, current_appearance

    previous = current_appearance()
    apply_appearance(app, {**previous, 'font_size': 20})
    bridge = ControlledBridge()
    dialog = TimetableRowDialog(bridge, table(), [])
    cleanup.append(dialog)
    try:
        dialog.title.setText('尚未保存的课程时段')
        dialog.exception_toggle.setChecked(True)
        dialog.show()
        for width in (920, 650, 920):
            dialog.resize(width, 680)
            QTest.qWait(60)
            scroll = dialog.findChild(QScrollArea)
            assert scroll.horizontalScrollBar().maximum() == 0
            controls = (dialog.exception_date, dialog.cancelled, dialog.exception_start, dialog.exception_end)
            for control in controls:
                assert control.width() >= control.sizeHint().width()
                assert control.parentWidget().rect().contains(control.geometry())
            if width == 650:
                assert len({control.y() for control in controls}) > 1
            assert dialog.title.text() == '尚未保存的课程时段'
        assert bridge.commands == []
    finally:
        dialog.dirty = False
        dialog.close()
        apply_appearance(app, previous)


def event(id='event-a', start=540, end=630, **extra):
    return dict({'id':id,'type':'event','title':'Lecture','course_title':'合成数据库','version':3,'status':'active',
            'start_minute':start,'end_minute':end,'business_date':'2030-01-07',
            'data':{'date':'2030-01-07','start':'09:00','end':'10:30','location':'TR-1','recurrence':'weekly',
                    'timetable_id':'table-a','timetable_row_key':id,'teaching_weeks':[1,2,4], 'enabled':True}},**extra)


def week(start='2030-01-07', events=None, conflicts=None, unknowns=None):
    events = events or []
    return {'week_start':start,'week_end':(dt.date.fromisoformat(start)+dt.timedelta(days=6)).isoformat(),
            'timezone':'Asia/Shanghai','days':[{'date':(dt.date.fromisoformat(start)+dt.timedelta(days=i)).isoformat(),
            'events':events if i==0 else []} for i in range(7)],'events':events,'conflicts':conflicts or [],'unknowns':unknowns or []}


def test_week_normalization_and_teaching_week_ranges():
    assert monday('2030-01-13') == '2030-01-07'
    assert parse_weeks('1-3，5, 7–8') == [1,2,3,5,7,8]
    assert parse_weeks(' ') is None
    for text in ('0','53','3-1','1,,2','1-10000000'):
        with pytest.raises(ValueError): parse_weeks(text)


def test_overlap_clusters_use_actual_interval_denominator():
    results = interval_lanes([event('a',540,630),event('b',570,660),event('c',660,720)])
    assert [(e['id'],lane,lanes) for e,lane,lanes in results] == [('a',0,2),('b',1,2),('c',0,1)]


def test_unknown_time_is_not_drawn_at_midnight(app,cleanup):
    grid = WeekGrid(); cleanup.append(grid)
    grid.set_week(week(events=[event(),event('unknown',None,None)]))
    assert len(grid.blocks) == 1
    assert grid.start_hour == 8
    assert '09:00' in grid.blocks[0].accessibleName()


def test_old_week_response_cannot_replace_new_selection(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableDialog(bridge,business_date='2030-01-07'); cleanup.append(dialog)
    old=bridge.take('timetable_week'); dialog.set_week('2030-01-21'); current=bridge.take('timetable_week')
    current['callback'](week('2030-01-21', [event('current')]))
    old['callback'](week(events=[event('old')]))
    assert dialog.last_result['week_start']=='2030-01-21'
    assert [b.event_data['id'] for b in dialog.grid.blocks]==['current']
    assert bridge.commands==[]


def test_unknown_reason_remains_visible(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableDialog(bridge,business_date='2030-01-07'); cleanup.append(dialog)
    bridge.deliver('timetable_week',week(unknowns=[{'title':'合成体育','reason':'结束时间未提供'}]))
    assert '结束时间未提供' in dialog.details.item(0).text()


def test_timetable_pages_are_not_silently_truncated(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableDialog(bridge,business_date='2030-01-07'); cleanup.append(dialog)
    bridge.deliver('timetables',{'items':[table()],'next_offset':50,'total':51})
    assert dialog.more_tables.isVisible()
    dialog.load_timetables(); assert bridge.take('timetables')['params']['offset']==50


def test_click_refetches_original_event_and_table_snapshot(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableDialog(bridge,business_date='2030-01-21'); cleanup.append(dialog)
    projection=event(); projection['data']=dict(projection['data'],date='2030-01-21')
    dialog.edit_occurrence(projection)
    bridge.deliver('get',{'entity':event()})
    bridge.deliver('timetables',{'items':[table()],'rows':[event()]},id='table-a')
    editor=dialog.dialogs[-1]; cleanup.append(editor)
    assert isinstance(editor,TimetableRowDialog)
    assert editor.event_data['data']['date']=='2030-01-07'
    assert editor.timetable['version']==2
    assert bridge.commands==[]


def test_unknown_metadata_can_create_source_container_before_upload(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableManageDialog(bridge,business_date='2030-01-07'); cleanup.append(dialog)
    dialog.title.setText('整张课表'); dialog.upload()
    write=bridge.commands[-1]
    assert write['name']=='create'
    assert write['payload']=={'type':'timetable','title':'整张课表','data':{}}
    write['callback']({'result':{'entity':dict(table(),title='整张课表',data={})}})
    assert dialog.dialogs[-1].owner_id=='table-a'
    assert bridge.commands[-1]['name']=='create'


def test_source_refresh_keeps_unsaved_semester_metadata(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableManageDialog(bridge,timetable_id='table-a'); cleanup.append(dialog)
    bridge.deliver('timetables',{'items':[table()],'rows':[]},id='table-a')
    dialog.title.setText('尚未保存的修改'); dialog.start.setDate(QDate(2030,1,14)); dialog.refresh_sources()
    bridge.deliver('sources',{'items':[],'next_offset':None})
    assert dialog.title.text()=='尚未保存的修改'
    assert dialog.start.date()==QDate(2030,1,14)
    assert dialog.dirty


def test_existing_rows_metadata_change_uses_versioned_apply(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableManageDialog(bridge,timetable_id='table-a'); cleanup.append(dialog)
    bridge.deliver('timetables',{'items':[table()],'rows':[event()]},id='table-a')
    dialog.start.setDate(QDate(2030,1,14)); dialog.save_metadata()
    write=bridge.commands[-1]
    assert write['name']=='apply_timetable'
    assert write['payload']['version']==2
    assert write['payload']['semester_start']=='2030-01-14'
    assert write['payload']['rows'][0]['event_id']=='event-a'
    assert write['payload']['rows'][0]['version']==3
    assert write['payload']['rows'][0]['teaching_weeks']==[1,2,4]


def test_manual_row_requires_real_semester_dates(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableManageDialog(bridge); cleanup.append(dialog)
    dialog.add_row()
    assert bridge.commands==[]
    assert '先填写并确认' in dialog.status.text()


def test_manual_row_payload_preserves_other_rows_and_accepts_unknown_course(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableRowDialog(bridge,table(),[event()]); cleanup.append(dialog)
    dialog.title.setText('新课程'); dialog.weeks.setText('1-3,5'); dialog.location.setText('LT2'); dialog.save()
    write=bridge.commands[-1]; assert write['name']=='apply_timetable'
    first,new=write['payload']['rows']
    assert first['event_id']=='event-a' and first['version']==3
    assert new['teaching_weeks']==[1,2,3,5] and new['owner_id'] is None
    assert new['location']=='LT2' and new['enabled'] is True
    assert all(command['name']!='create_plan' for command in bridge.commands)


def test_edit_row_pause_and_single_occurrence_exception(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableRowDialog(bridge,table(),[event()],event=event()); cleanup.append(dialog)
    dialog.enabled.setChecked(False); dialog.exception_date.setDate(QDate(2030,1,14)); dialog.add_exception(); payload=dialog.build_payload()
    row=payload['rows'][0]
    assert row['event_id']=='event-a' and row['version']==3
    assert row['enabled'] is False
    assert row['exceptions']=={'2030-01-14':{'cancelled':True}}
    dialog.exception_date.setDate(QDate(2030,1,15)); dialog.add_exception()
    assert len(dialog.exceptions)==1 and '星期一致' in dialog.status.text()


def test_organize_uses_timetable_scope_and_explicit_selected_sources(app,cleanup,monkeypatch):
    from management import gui_assistant
    captured=[]
    class DummyDialog:
        def __init__(self,*args,**kwargs): captured.append(kwargs)
    monkeypatch.setattr(gui_assistant,'AssistanceDialog',DummyDialog)
    monkeypatch.setattr('management.gui_timetable.show_child',lambda owner,dialog:dialog)
    bridge=ControlledBridge(); dialog=TimetableManageDialog(bridge,timetable_id='table-a'); cleanup.append(dialog)
    bridge.deliver('timetables',{'items':[table()],'rows':[]},id='table-a')
    dialog.selected_sources={'source-a':{'id':'source-a','title':'原课表'}}; dialog.open_assistance()
    assert captured[0]['scope']=={'kind':'timetable','entity_id':'table-a'}
    assert captured[0]['source_ids']==['source-a'] and captured[0]['auto_send'] is True
    assert '不要生成每日计划' in captured[0]['prompt']
    assert bridge.commands==[]

def test_disabled_row_stays_disabled_during_metadata_update(app,cleanup):
    paused=event(); paused['data']['timetable_enabled']=False; paused['status']='cancelled'
    bridge=ControlledBridge(); dialog=TimetableManageDialog(bridge,timetable_id='table-a'); cleanup.append(dialog)
    bridge.deliver('timetables',{'items':[table()],'rows':[paused]},id='table-a')
    assert '已停用' in dialog.row_list.item(0).text()
    dialog.title.setText('修改课表名称'); dialog.save_metadata()
    assert bridge.commands[-1]['payload']['rows'][0]['enabled'] is False


def test_exception_removal_is_explicit_and_epoch_is_pinned(app,cleanup):
    original=event(); original['data']['exceptions']={'2030-01-14':{'cancelled':True}}
    bridge=ControlledBridge(); dialog=TimetableRowDialog(bridge,table(),[original],event=original); cleanup.append(dialog)
    dialog.exception_list.setCurrentRow(0); dialog.remove_exception(); bridge.epoch='replacement-space'; dialog.save()
    write=bridge.commands[-1]
    assert write['payload']['rows'][0]['remove_exceptions']==['2030-01-14']
    assert write['options']['epoch']=='synthetic-epoch'


def test_real_ui_upload_manual_edit_and_week_stay_separate_from_plans(app,cleanup,tmp_path):
    from management.core import Core
    core=Core(tmp_path/'isolated'); bridge=QueuedCoreBridge(core)
    manager=TimetableManageDialog(bridge,business_date='2030-01-07'); cleanup.append(manager)
    wait(app,lambda:bridge.pending==0)
    manager.title.setText('真实接口合成课表'); manager.start_known.setChecked(True); manager.end_known.setChecked(True)
    manager.start.setDate(QDate(2030,1,7)); manager.end.setDate(QDate(2030,4,14)); manager.timezone.setCurrentText('Asia/Shanghai')
    manager.save_metadata(); wait(app,lambda:bridge.pending==0)
    assert manager.entity and not manager.pending
    source=cmd(core,'add_source',{'owner_id':manager.entity['id'],'kind':'notice','title':'原始课表','text':'合成课程，每周一 09:00–10:30。'})['result']['entity']
    manager.refresh_sources(); wait(app,lambda:bridge.pending==0)
    assert manager.sources.count()==1
    manager.add_row(); editor=manager.dialogs[-1]; cleanup.append(editor)
    editor.title.setText('合成实验'); editor.event_kind.setCurrentIndex(editor.event_kind.findData('lab')); editor.weeks.setText('1-3,5'); editor.save()
    wait(app,lambda:bridge.pending==0)
    result=core.query('timetables',id=manager.entity['id']); assert len(result['rows'])==1
    saved_event=result['rows'][0]; assert saved_event['data']['event_kind']=='lab'
    assert core.query('list',type='plan')['total']==0
    weekly=TimetableDialog(bridge,business_date='2030-01-07'); cleanup.append(weekly)
    wait(app,lambda:bridge.pending==0)
    assert len(weekly.grid.blocks)==1
    assert weekly.grid.blocks[0].event_data['id']==saved_event['id']
    # Non-teaching weeks stay empty; changing the grid does not materialize a plan.
    weekly.set_week('2030-01-28'); wait(app,lambda:bridge.pending==0)
    assert weekly.grid.blocks==[]
    assert core.query('list',type='plan')['total']==0
    # Series pause remains accessible through the manager, rather than vanishing forever.
    manager.load_table(manager.entity['id']); wait(app,lambda:bridge.pending==0)
    paused=TimetableRowDialog(bridge,manager.entity,manager.rows,event=manager.rows[0]); cleanup.append(paused)
    paused.enabled.setChecked(False); paused.save(); wait(app,lambda:bridge.pending==0)
    latest=core.query('timetables',id=manager.entity['id'])['rows'][0]
    assert latest['data']['timetable_enabled'] is False
    assert core.query('timetable_week',week_start='2030-01-07')['events']==[]

def test_stale_manager_cannot_attach_old_owner_to_replaced_database(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableManageDialog(bridge,timetable_id='table-a'); cleanup.append(dialog)
    bridge.deliver('timetables',{'items':[table()],'rows':[]},id='table-a')
    bridge.epoch='restored-space'; dialog.upload()
    assert dialog.dialogs==[] and bridge.commands==[]
    assert '数据空间已经改变' in dialog.status.text()


def test_conflict_coverage_is_not_presented_as_complete(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableDialog(bridge,business_date='2030-01-07'); cleanup.append(dialog)
    result=week(conflicts=[{'date':'2030-01-07','message':'合成重叠'}]); result['coverage']={'conflicts_total':201,'conflicts_complete':False}
    bridge.deliver('timetable_week',result)
    assert '201' in dialog.attention.text() and '仅显示部分' in dialog.attention.text()


def test_real_ui_remove_exception_restores_exact_occurrence(app,cleanup,tmp_path):
    from management.core import Core
    core=Core(tmp_path/'isolated'); bridge=QueuedCoreBridge(core)
    original=cmd(core,'apply_timetable',{'title':'例外合成课表','semester_start':'2030-01-07','semester_end':'2030-02-03',
        'timezone':'Asia/Shanghai','source_text':'合成手动确认','rows':[{'key':'lesson','title':'课程','weekday':0,'start':'09:00','end':'10:00',
        'exceptions':{'2030-01-14':{'cancelled':True}}}]})['result']
    assert core.query('timetable_week',week_start='2030-01-14')['events']==[]
    bridge=QueuedCoreBridge(core)
    editor=TimetableRowDialog(bridge,original['entity'],original['rows'],event=original['rows'][0]); cleanup.append(editor)
    editor.exception_list.setCurrentRow(0); editor.remove_exception(); editor.save(); wait(app,lambda:bridge.pending==0)
    assert len(core.query('timetable_week',week_start='2030-01-14')['events'])==1
    assert core.query('list',type='plan')['total']==0
