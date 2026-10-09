"""Settings snapshots and all-entrypoint protection; only synthetic business data."""
import copy
import datetime as dt
import os
import uuid
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')

import pytest
from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QApplication
from management.core import Core
from management.gui_timetable_settings import TimetableSettingsPage
from management.planning import _occurrences as warning_occurrences
from management.recurring import _occurrences as preparation_occurrences
from management.schemas import BusinessError
from test_ux_workflows_v2 import ControlledBridge, QueuedCoreBridge, cmd, wait


@pytest.fixture(scope='session')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def core(tmp_path):
    return Core(tmp_path/'synthetic-settings-space')


def old_table(core):
    return cmd(core,'apply_timetable',{
        'title':'Legacy synthetic semester','semester_start':'2030-01-07','semester_end':'2030-02-17',
        'timezone':'Asia/Shanghai','source_text':'Previously confirmed synthetic source',
        'rows':[{'key':'lecture','title':'Synthetic lecture','weekday':2,'start':'09:00','end':'10:00','teaching_weeks':[1,3,5]},
                {'key':'lab','title':'Synthetic lab','weekday':4,'start':'14:00','end':'15:00','teaching_weeks':[2,4]}]})['result']


def close(app,page):
    page.dirty=False; page.close();page.deleteLater();app.processEvents()


def opened(app,core):
    bridge=QueuedCoreBridge(core);page=TimetableSettingsPage(bridge);page.show()
    wait(app,lambda:page.ready and bridge.pending==0)
    return page,bridge


def test_settings_defaults_are_saved_normalized_without_changing_old_timetable(core):
    before=old_table(core); revision=core.query('state')['revision']
    initial=core.query('settings')['settings']['timetable_defaults']
    assert initial=={'week_numbering':'teaching','recess_weeks':[]}
    settings=cmd(core,'settings',{'settings':{'timetable_defaults':{'recess_weeks':['2031-01-20','2030-01-21']}}})['result']['settings']
    assert settings['timetable_defaults']=={'week_numbering':'teaching','recess_weeks':['2030-01-21','2031-01-20']}
    after=core.query('timetables',id=before['entity']['id'])
    assert after['entity']==before['entity'] and after['rows']==before['rows']
    assert core.query('state')['revision']==revision+1
    snapshot=core.query('state')
    core.query('settings');core.query('timetable_week',week_start='2030-01-21')
    assert core.query('state')==snapshot


def test_legacy_settings_without_defaults_read_only_and_first_explicit_save_adds_defaults(core):
    with core.store.connect() as connection:
        settings=core.store.meta(connection,'settings');settings.pop('timetable_defaults',None)
        core.store.set_meta(connection,'settings',settings)
    before=core.query('state')
    core.query('settings')
    assert core.query('state')==before
    saved=cmd(core,'settings',{'settings':{'timetable_defaults':{'recess_weeks':['2030-01-21']}}})['result']['settings']
    assert saved['timetable_defaults']=={'week_numbering':'teaching','recess_weeks':['2030-01-21']}


@pytest.mark.parametrize('invalid',[
    None,[],True,{'unknown':True},{'week_numbering':'unsupported'},{'week_numbering':False},
    {'recess_weeks':None},{'recess_weeks':['2030-01-22']},{'recess_weeks':['2030-01-21','2030-01-21']},
    {'recess_weeks':['not-a-date']},{'recess_weeks':[True]},
])
def test_invalid_global_rules_leave_settings_revision_and_tables_unchanged(core,invalid):
    table=old_table(core);before=core.query('settings');state=core.query('state')
    with pytest.raises(BusinessError):cmd(core,'settings',{'settings':{'timetable_defaults':invalid}})
    assert core.query('settings')==before and core.query('state')==state
    assert core.query('timetables',id=table['entity']['id'])['rows']==table['rows']


@pytest.mark.parametrize('timezone',['','../Asia/Shanghai','Not/AZone',None,True])
def test_invalid_global_timezone_is_atomic_with_recess_change(core,timezone):
    before=core.query('settings')
    with pytest.raises(BusinessError):
        cmd(core,'settings',{'settings':{'timezone':timezone,'timetable_defaults':{'recess_weeks':['2030-01-21']}}})
    assert core.query('settings')==before


@pytest.mark.parametrize('target',['table','event'])
@pytest.mark.parametrize('change',[{'week_numbering':'teaching'},{'recess_weeks':['2030-01-21']}])
def test_generic_updates_cannot_bypass_confirmed_table_rule_boundary(core,target,change):
    result=old_table(core);entity=result['entity'] if target=='table' else result['rows'][0]
    before=core.query('state')
    with pytest.raises(BusinessError):
        cmd(core,'update',{'id':entity['id'],'version':entity['version'],'patch':{'data':change}})
    assert core.query('state')==before and core.query('get',id=entity['id'])['entity']==entity


@pytest.mark.parametrize('field,value',[('week_numbering','teaching'),('recess_weeks',['2030-01-21'])])
def test_generic_event_creation_cannot_inject_reserved_timetable_rules(core,field,value):
    with pytest.raises(BusinessError):
        cmd(core,'create',{'type':'event','title':'Synthetic bypass','data':{'date':'2030-01-09',field:value}})
    assert core.query('list',type='event')['total']==0


def test_page_loads_only_when_shown_and_save_waits_for_loaded_settings(app):
    bridge=ControlledBridge();page=TimetableSettingsPage(bridge)
    try:
        assert bridge.queries==[] and not page.save_button.isEnabled()
        page.save();assert bridge.commands==[]
        page.show();assert len(bridge.queries)==2
        bridge.deliver('timetables',{'items':[],'next_offset':None})
        page.save();assert bridge.commands==[]
        bridge.deliver('settings',{'settings':{'timezone':'Asia/Shanghai','timetable_defaults':{'week_numbering':'teaching','recess_weeks':[]}},'epoch':bridge.epoch,'revision':12})
        assert page.save_button.isEnabled() and page.ready
        page.hide();page.show();assert len(bridge.queries)==2
    finally:close(app,page)


@pytest.mark.parametrize('theme,font_size', [('light',13),('dark',20)])
def test_settings_groups_keep_controls_compact_and_footer_visible_without_losing_edits(app,tmp_path,theme,font_size):
    from pathlib import Path
    from PySide6.QtWidgets import QScrollArea
    from management.gui_theme import apply_appearance,current_appearance
    from management.gui_visual_profile import visual_style,set_visual_style
    previous,previous_style=current_appearance(),visual_style()
    bridge=ControlledBridge();page=None
    try:
        set_visual_style('glass');apply_appearance(app,{'theme':theme,'font_size':font_size})
        page=TimetableSettingsPage(bridge);page.resize(790,650);page.show()
        bridge.deliver('timetables',{'items':[],'next_offset':None})
        bridge.deliver('settings',{'settings':{'timezone':'Asia/Shanghai','timetable_defaults':{'week_numbering':'teaching','recess_weeks':[]}},'epoch':bridge.epoch,'revision':12})
        page.timezone.setCurrentText('Asia/Singapore')
        page.recess_date.setDate(QDate(2030,1,23));page.add_week()
        controls=(page.scope,page.timezone,page.recess_date,page.add,page.remove,page.save_button,page.reload_button)
        originals=tuple(id(widget) for widget in controls)
        for _ in range(4):app.processEvents()
        assert len(page.findChildren(QScrollArea))==1
        for widget in controls:
            assert widget.width()<page.width()*0.65
        assert page.weeks.horizontalScrollBar().maximum()==0
        report=Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR',str(tmp_path)))
        assert page.grab().save(str(report/('settings-timetable-'+theme+'.png')))
        for style,width in (('glass',460),('classic',460),('glass',790)):
            set_visual_style(style);apply_appearance(app,{'theme':theme,'font_size':font_size})
            page.resize(width,650)
            for _ in range(4):app.processEvents()
            assert page.scroll.horizontalScrollBar().maximum()==0
            assert page.save_button.visibleRegion().boundingRect().contains(page.save_button.rect())
            assert not page.scroll.isAncestorOf(page.save_button)
            assert page.timezone.currentText()=='Asia/Singapore' and page.dirty
            assert page.weeks.count()==1 and page.weeks.item(0).data(Qt.ItemDataRole.UserRole)=='2030-01-21'
            assert tuple(id(widget) for widget in controls)==originals
            assert not bridge.commands and len(bridge.queries)==2
    finally:
        if page is not None:close(app,page)
        set_visual_style(previous_style);apply_appearance(app,previous)


def test_page_default_settings_save_keeps_all_semesters_and_existing_table_unchanged(app,core):
    table=old_table(core);page,bridge=opened(app,core)
    try:
        page.timezone.setCurrentText('Asia/Singapore')
        for day in ('2031-01-20','2030-01-23'):
            page.recess_date.setDate(QDate.fromString(day,'yyyy-MM-dd'));page.add_week()
        # Selecting any day explicitly identifies its week; Wed Jan 23 -> Mon Jan 21.
        page.recess_date.setDate(QDate(2030,1,21));page.add_week()
        assert page.weeks.count()==2
        page.save();page.save();assert len(bridge.commands)==1
        wait(app,lambda:bridge.pending==0 and page.ready)
        settings=core.query('settings')['settings']
        assert settings['timezone']=='Asia/Singapore'
        assert settings['timetable_defaults']['recess_weeks']==['2030-01-21','2031-01-20']
        current=core.query('timetables',id=table['entity']['id'])
        assert current['entity']==table['entity'] and current['rows']==table['rows']
    finally:close(app,page)


def test_page_existing_table_applies_all_rows_and_all_occurrence_readers_agree(app,core):
    table=old_table(core);page,bridge=opened(app,core)
    try:
        page.scope.setCurrentIndex(page.scope.findData(table['entity']['id']))
        wait(app,lambda:bridge.pending==0 and page.ready)
        assert not page.skip_recess.isChecked()
        page.skip_recess.setChecked(True);page.recess_date.setDate(QDate(2030,1,21));page.add_week();page.save()
        wait(app,lambda:bridge.pending==0 and page.ready)
        assert bridge.commands[-1][0]=='apply_timetable'
        payload=bridge.commands[-1][1]
        assert {r['event_id'] for r in payload['rows']}=={r['id'] for r in table['rows']}
        actual=core.query('timetables',id=table['entity']['id'])
        assert actual['entity']['version']==table['entity']['version']+1
        expected={'lecture':['2030-01-09','2030-01-30','2030-02-13'],'lab':['2030-01-18','2030-02-08']}
        for event in actual['rows']:
            assert event['data']['week_numbering']=='teaching' and event['data']['recess_weeks']==['2030-01-21']
            wanted=expected[event['data']['timetable_row_key']]
            assert [d.isoformat() for d in warning_occurrences(event['data'],dt.date(2030,2,17))]==wanted
            assert [d.isoformat() for _,d in preparation_occurrences(event,dt.date(2030,1,7),dt.date(2030,2,17))]==wanted
            for day in wanted:
                assert event['id'] in {e['id'] for e in core.query('plan_context',date=day)['hard_events']}
        assert core.query('plan_context',date='2030-01-23')['hard_events']==[]
        assert core.query('settings')['settings']['timetable_defaults']['recess_weeks']==[]
    finally:close(app,page)


def test_page_stale_epoch_never_sends_save_and_preserves_edit(app,core):
    page,bridge=opened(app,core)
    try:
        page.recess_date.setDate(QDate(2030,1,21));page.add_week();bridge.epoch='different-data-space'
        page.save()
        assert bridge.commands==[] and page.weeks.count()==1 and page.dirty
        assert '重新读取' in page.status.text()
    finally:close(app,page)


def test_page_stale_revision_rejects_save_without_replacing_user_edits(app,core):
    page,bridge=opened(app,core)
    try:
        page.recess_date.setDate(QDate(2030,1,21));page.add_week()
        cmd(core,'settings',{'settings':{'timetable_defaults':{'week_numbering':'calendar'}}})
        page.save();wait(app,lambda:bridge.pending==0)
        assert page.dirty and page.weeks.count()==1 and '重新读取' in page.status.text()
        assert core.query('settings')['settings']['timetable_defaults']=={'week_numbering':'calendar','recess_weeks':[]}
    finally:close(app,page)


def test_page_stale_row_version_rejects_entire_table_change(app,core):
    table=old_table(core);page,bridge=opened(app,core)
    try:
        page.scope.setCurrentIndex(page.scope.findData(table['entity']['id']));wait(app,lambda:bridge.pending==0 and page.ready)
        original=table['rows'][0]
        changed=cmd(core,'update',{'id':original['id'],'version':original['version'],'patch':{'data':{'location':'Explicit new room'}}})['result']['entity']
        # Revision can refresh elsewhere in a client while this editor still has old row versions.
        bridge.revision=page.revision=core.query('state')['revision']
        page.skip_recess.setChecked(True);page.recess_date.setDate(QDate(2030,1,21));page.add_week();page.save()
        wait(app,lambda:bridge.pending==0)
        assert page.dirty and page.weeks.count()==1
        current=core.query('timetables',id=table['entity']['id'])
        assert current['entity']==table['entity']
        assert next(e for e in current['rows'] if e['id']==changed['id'])==changed
        assert all('week_numbering' not in e['data'] for e in current['rows'])
    finally:close(app,page)


def test_changed_recess_rule_stops_preparation_until_explicit_rebind_preserving_tasks(app,core):
    table=old_table(core);anchor=next(event for event in table['rows'] if event['data']['timetable_row_key']=='lecture')
    rule=cmd(core,'set_recurring_rule',{'anchor_id':anchor['id'],'title':'Synthetic preparation','content':'Read explicit chapter',
        'completion_gate':'Read and answer the stated question','days_before':1,'enabled':True,'materialize_date':'2001-01-01'})['result']['entity']
    created=cmd(core,'materialize_recurring',{'rule_id':rule['id'],'start':'2030-01-08'})['result']['created']
    assert len(created)==1
    page,bridge=opened(app,core)
    try:
        page.scope.setCurrentIndex(page.scope.findData(table['entity']['id']));wait(app,lambda:bridge.pending==0 and page.ready)
        page.skip_recess.setChecked(True);page.recess_date.setDate(QDate(2030,1,21));page.add_week();page.save()
        wait(app,lambda:bridge.pending==0 and page.ready)
        result=cmd(core,'materialize_recurring',{'rule_id':rule['id'],'start':'2030-01-22','end':'2030-01-29'})['result']
        assert result['created']==[] and 'anchor_changed' in {issue['code'] for issue in result['issues']}
        assert core.query('get',id=created[0]['id'])['entity']==created[0]
    finally:close(app,page)


def test_upload_container_create_reuses_populated_table_without_overwriting_snapshot(core):
    first=old_table(core);table=first['entity']
    payload={'type':'timetable','title':'  '+table['title']+'  ',
             'data':{'semester_start':table['data']['semester_start'],'semester_end':table['data']['semester_end'],
                     'timezone':'Asia/Singapore','week_numbering':'teaching','recess_weeks':['2030-01-21']}}
    created=cmd(core,'create',payload)['result']
    assert created['reused'] and created['entity']==table
    assert core.query('timetables',id=table['id'])['rows']==first['rows']
    assert core.query('list',type='timetable')['total']==1


def test_upload_container_create_reuses_empty_source_container_and_allows_distinct_range(core):
    payload={'type':'timetable','title':'Synthetic empty upload','data':{'semester_start':'2030-01-07',
             'semester_end':'2030-02-17','timezone':'Asia/Shanghai','week_numbering':'teaching','recess_weeks':['2030-01-21']}}
    first=cmd(core,'create',payload)['result']['entity']
    repeated=cmd(core,'create',payload)['result']
    assert repeated['reused'] and repeated['entity']==first
    changed=copy.deepcopy(payload);changed['data']['semester_end']='2030-02-24'
    second=cmd(core,'create',changed)['result']['entity']
    assert second['id']!=first['id'] and core.query('list',type='timetable')['total']==2


def test_upload_container_create_stale_state_cannot_race_a_new_container(core):
    payload={'type':'timetable','title':'Concurrent synthetic upload','data':{'semester_start':'2030-01-07',
             'semester_end':'2030-02-17','timezone':'Asia/Shanghai'}}
    before=core.query('state');first=cmd(core,'create',payload)['result']['entity']
    with pytest.raises(BusinessError) as error:
        core.command('create',payload,request_id=str(uuid.uuid4()),epoch=before['epoch'],expected_revision=before['revision'])
    assert error.value.code=='revision_conflict'
    assert core.query('list',type='timetable')['total']==1
    assert core.query('get',id=first['id'])['entity']==first


def test_ambiguous_same_name_same_range_containers_reject_new_upload_instead_of_arbitrary_reuse(core):
    payload={'type':'timetable','title':'Synthetic original','data':{'semester_start':'2030-01-07',
             'semester_end':'2030-02-17','timezone':'Asia/Shanghai'}}
    original=cmd(core,'create',payload)['result']['entity']
    other=cmd(core,'create',{**payload,'title':'Synthetic other'})['result']['entity']
    cmd(core,'update',{'id':other['id'],'version':other['version'],'patch':{'title':original['title']}})
    before=core.query('state')
    with pytest.raises(BusinessError) as error:cmd(core,'create',payload)
    assert error.value.code=='timetable_duplicate_container' and core.query('state')==before
    assert core.query('list',type='timetable')['total']==2
