"""The primary timetable flow is one source, a name, and an explicit range."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')

import pytest
from PySide6.QtCore import QDate
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QDialog, QComboBox, QCheckBox, QTabWidget, QWidget
from management.gui_timetable import TimetableImportDialog, TimetableManageDialog, timetable_payload
from test_gui_timetable import app, cleanup, table, event
from test_ux_workflows_v2 import ControlledBridge, QueuedCoreBridge, wait


def settings():
    return {'settings':{'timezone':'Asia/Singapore', 'timetable_defaults':{
        'week_numbering':'teaching', 'recess_weeks':['2029-12-31','2030-02-18','2030-04-22']}}}


def fill(dialog):
    dialog.title.setText('新的学期课表')
    dialog.start.setDate(QDate(2030,1,8))
    dialog.end.setDate(QDate(2030,4,14))
    dialog.set_source_path('D:/synthetic/timetable.png')


def test_import_has_only_source_name_and_range_visible(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableImportDialog(bridge,business_date='2030-01-07'); cleanup.append(dialog)
    dialog.show(); app.processEvents()
    assert dialog.start.text()=='选择起始周' and dialog.end.text()=='选择结束日期'
    assert not any(w.isVisible() for cls in (QComboBox,QCheckBox,QTabWidget) for w in dialog.findChildren(cls))
    assert dialog.organize_button.text()=='整理课表'
    dialog.title.setText('课表'); dialog.organize()
    assert '请选择课表的起始周' in dialog.status.text() and not bridge.commands


def test_import_snapshot_uses_settings_and_filters_recess_to_range(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableImportDialog(bridge,business_date='2030-01-07'); cleanup.append(dialog)
    bridge.deliver('settings',settings()); fill(dialog)
    assert dialog.start.date()==QDate(2030,1,7)
    assert '2030/01/07' in dialog.range_hint.text()
    dialog.organize(); write=bridge.commands[-1]
    assert write['name']=='create'
    assert write['payload']['data']=={'semester_start':'2030-01-07','semester_end':'2030-04-14',
        'timezone':'Asia/Singapore','week_numbering':'teaching','recess_weeks':['2030-02-18']}
    assert write['options']['epoch']=='synthetic-epoch'


def test_single_action_owns_source_then_opens_scoped_review(app,cleanup,monkeypatch):
    from management import gui_assistant
    captured=[]
    class Review(QDialog):
        def __init__(self,bridge,parent=None,**kwargs):
            super().__init__(parent); captured.append(kwargs)
    monkeypatch.setattr(gui_assistant,'AssistanceDialog',Review)
    parent=QDialog(); parent.dialogs=[]; cleanup.append(parent)
    bridge=ControlledBridge(); dialog=TimetableImportDialog(bridge,parent,business_date='2030-01-07'); cleanup.append(dialog)
    bridge.deliver('settings',settings()); fill(dialog); dialog.organize()
    metadata=copy.deepcopy(bridge.commands[-1]['payload']); entity=dict(table(),title=metadata['title'],data=metadata['data'])
    bridge.commands[-1]['callback']({'result':{'entity':entity}})
    upload=bridge.commands[-1]
    assert upload['name']=='add_source' and upload['payload']['owner_id']=='table-a'
    assert len(bridge.commands)==2 and not dialog.editor.isEnabled()
    upload['callback']({'result':{'entity':{'id':'source-a','title':'课表截图'}}})
    assert len(captured)==1
    assert captured[0]['scope']=={'kind':'timetable','entity_id':'table-a'}
    assert captured[0]['source_ids']==['source-a'] and captured[0]['auto_send'] is True
    assert 'Recess' in captured[0]['prompt']
    cleanup.extend(parent.dialogs)


def test_uncertain_source_retry_does_not_duplicate_container(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableImportDialog(bridge,business_date='2030-01-07'); cleanup.append(dialog)
    bridge.deliver('settings',settings()); fill(dialog); dialog.organize()
    create=bridge.commands[-1]; create['callback']({'result':{'entity':dict(table(),data=create['payload']['data'])}})
    original=bridge.commands[-1]['payload']; bridge.commands[-1]['error']({'code':'connection_lost','message':'连接中断'})
    assert dialog.uncertain_stage=='source' and not dialog.editor.isEnabled()
    dialog.organize()
    assert [w['name'] for w in bridge.commands]==['create','add_source','add_source']
    assert bridge.commands[-1]['payload']==original
    dialog.capture.pending=False; dialog.capture.uncertain=False


def test_failed_upload_keeps_container_for_retry(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableImportDialog(bridge,business_date='2030-01-07'); cleanup.append(dialog)
    bridge.deliver('settings',settings()); fill(dialog); dialog.organize()
    create=bridge.commands[-1]; create['callback']({'result':{'entity':dict(table(),title=create['payload']['title'],data=create['payload']['data'])}})
    bridge.commands[-1]['error']({'code':'not_found','message':'找不到原文件'})
    assert dialog.entity['id']=='table-a' and not dialog.pending
    dialog.set_source_path('D:/synthetic/replacement.png'); dialog.organize()
    assert [w['name'] for w in bridge.commands]==['create','add_source','add_source']
    assert bridge.commands[-1]['payload']['path'].endswith('replacement.png')
    dialog.capture.pending=False


def test_epoch_change_stops_multistep_upload_after_container_receipt(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableImportDialog(bridge,business_date='2030-01-07'); cleanup.append(dialog)
    bridge.deliver('settings',settings()); fill(dialog); dialog.organize(); bridge.epoch='restored'
    bridge.commands[-1]['callback']({'result':{'entity':table()}})
    assert len(bridge.commands)==1 and not dialog.pending
    assert '数据空间已经改变' in dialog.status.text()


def test_existing_calendar_snapshot_is_preserved_when_editing_row(app,cleanup):
    original=table(); original['data'].update(week_numbering='teaching',recess_weeks=['2030-02-18'])
    payload=timetable_payload(original,[event()],{'title':'改名'})
    assert payload['week_numbering']=='teaching' and payload['recess_weeks']==['2030-02-18']
    legacy=timetable_payload(table(),[event()])
    assert 'week_numbering' not in legacy and 'recess_weeks' not in legacy


def test_real_import_owned_image_and_handoff_without_plan(app,cleanup,tmp_path,monkeypatch):
    from management.core import Core
    from management import gui_assistant
    handoffs=[]
    class Review(QDialog):
        def __init__(self,bridge,parent=None,**kwargs): super().__init__(parent); handoffs.append(kwargs)
    monkeypatch.setattr(gui_assistant,'AssistanceDialog',Review)
    core=Core(tmp_path/'data'); bridge=QueuedCoreBridge(core)
    parent=QDialog(); parent.dialogs=[]; cleanup.append(parent)
    dialog=TimetableImportDialog(bridge,parent,business_date='2030-01-07'); cleanup.append(dialog)
    wait(app,lambda:bridge.pending==0)
    image=QImage(100,80,QImage.Format.Format_RGB32); image.fill(0xffeeeeee); path=tmp_path/'schedule.png'; assert image.save(str(path))
    fill(dialog); dialog.set_source_path(path); dialog.organize(); wait(app,lambda:bridge.pending==0)
    assert len(handoffs)==1 and not dialog.pending
    table_id=handoffs[0]['scope']['entity_id']; source_id=handoffs[0]['source_ids'][0]
    assert core.query('timetables',id=table_id)['items'][0]['data']['week_numbering']=='teaching'
    source=core.query('get',id=source_id)['entity']; assert source['data']['source_owner_id']==table_id
    assert core.query('list',type='plan')['total']==0
    assert [w[0] for w in bridge.commands]==['create','add_source']
    cleanup.extend(parent.dialogs)

def test_reused_container_preserves_own_snapshot_even_after_upload_failure(app,cleanup):
    bridge=ControlledBridge(); dialog=TimetableImportDialog(bridge,business_date='2030-01-07'); cleanup.append(dialog)
    bridge.deliver('settings',settings()); fill(dialog); dialog.organize()
    original=table(); original['title']=dialog.title.text(); original['data'].update(week_numbering='calendar',recess_weeks=[])
    bridge.commands[-1]['callback']({'result':{'entity':original,'reused':True}})
    bridge.commands[-1]['error']({'code':'not_found','message':'原文件已移走'})
    dialog.set_source_path('D:/synthetic/retry.png'); dialog.organize()
    assert [write['name'] for write in bridge.commands]==['create','add_source','add_source']
    assert dialog.entity['data']['timezone']=='Asia/Shanghai'
    assert dialog.entity['data']['week_numbering']=='calendar'
    dialog.capture.pending=False


def test_range_edit_filters_recess_and_settings_targets_current_table(app,cleanup):
    bridge=ControlledBridge(); selections=[]
    original=table(); original['data'].update(week_numbering='teaching',recess_weeks=['2030-01-14','2030-02-18'])
    dialog=TimetableManageDialog(bridge,timetable_id='table-a',on_open_settings=lambda **kwargs:selections.append(kwargs)); cleanup.append(dialog)
    bridge.deliver('timetables',{'items':[original],'rows':[event()]},id='table-a')
    dialog.open_calendar_settings()
    assert selections==[{'page':'课表','timetable_id':'table-a'}]
    dialog.start.setDate(QDate(2030,2,4)); dialog.save_metadata()
    assert bridge.commands[-1]['payload']['recess_weeks']==['2030-02-18']
    assert bridge.commands[-1]['payload']['week_numbering']=='teaching'
