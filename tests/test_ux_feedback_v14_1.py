
"""Regression coverage for explicit review meaning and responsive native controls."""
import os,time
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import QDate,QPoint,Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication,QMenu,QScrollArea,QLabel
from management.core import Core
from management.gui_theme import apply_appearance,color
from management.gui_review import ReviewPage
from management.gui_charts import CoverageChart,VerticalCoverageBar,display_segments
from management.gui_workflows import SettingsDialog
from management.gui_menu import MenuButton
from test_review_ui_v2 import ControlledBridge,load,daily as view
from test_occurrence_flow_v13 import cmd,course_event,daily,submit,DAY

@pytest.fixture(scope='session')
def app():
    app=QApplication.instance() or QApplication([])
    QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    return app

def settle(app,ms=50):
    end=time.monotonic()+ms/1000
    while time.monotonic()<end:app.processEvents();time.sleep(.005)

@pytest.mark.parametrize('theme',['light','dark'])
def test_attendance_selection_colors_saved_and_unsaved_choices(app,theme):
    apply_appearance(app,{'theme':theme,'font_size':16,'font_family':'Microsoft YaHei'})
    bridge=ControlledBridge();page=ReviewPage(bridge);page.resize(900,800);page.show()
    item={'target_id':'event','item_id':'occurrence','fixed_schedule':True,'review_dimension':'attendance','owner_type':'course',
          'title':'Tutorial 6','result':'attended','raw_result':'attended','reported':True,'can_review':True,
          'choices':[('attended','已参加'),('missed_needs_catchup','缺课需补'),('absent','未参加')]}
    try:
        load(page,bridge,view(items=[item]))
        for choice,role in [('attended','done'),('missed_needs_catchup','incomplete'),('absent','danger')]:
            button=page.item_buttons['occurrence'][choice]
            if choice!='attended':button.click()
            settle(app)
            assert button.isChecked()
            assert sum(x.isChecked() for x in page.item_buttons['occurrence'].values())==1
            picture=button.grab().toImage()
            assert picture.pixelColor(picture.width()//2,picture.height()-6).name()==color(role)
        assert page._choices[page._date]['occurrence']=='absent'
        assert bridge.commands==[], 'Choosing remains a draft until confirmation'
    finally:page.close()

def test_review_date_arrows_keep_each_day_draft(app):
    bridge=ControlledBridge();page=ReviewPage(bridge);page.show()
    try:
        load(page,bridge,view('2030-01-07'))
        page.item_buttons['task-a']['done'].click()
        page.next_date.click();bridge.deliver('daily_review',view('2030-01-08'))
        assert page._date=='2030-01-08' and not page.item_buttons['task-a']['done'].isChecked()
        page.previous_date.click();bridge.deliver('daily_review',view('2030-01-07'))
        assert page._date=='2030-01-07' and page.item_buttons['task-a']['done'].isChecked()
        assert bridge.commands==[]
    finally:page.close()

@pytest.mark.parametrize('attendance',['attended','absent','missed_needs_catchup'])
def test_mixed_week_names_course_project_and_actual_results(app,tmp_path,attendance):
    core=Core(tmp_path/'data');course,event=course_event(core)
    project=cmd(core,'create',{'type':'project','title':'Research project'})['entity']
    task=cmd(core,'create',{'type':'task','title':'Project preparation','parent_id':project['id']})['entity']
    course_task=cmd(core,'create',{'type':'task','title':'Course exercise','parent_id':course['id']})['entity']
    cmd(core,'create_plan',{'date':DAY,'mode':'no_precise_time','blocks':[{'target_id':task['id']},{'target_id':course_task['id']}]})
    for entity,result in [(task,'partial'),(course_task,'done')]:
        cmd(core,'record_feedback',{'target_id':entity['id'],'business_date':DAY,'dimensions':{'completion':result},'source_text':'Explicit synthetic feedback'})
    d=daily(core);occurrence=next(x for x in d['items'] if x.get('fixed_schedule'))
    submit(core,d,attendance,occurrence)
    before=core.query('state');weekly=core.query('weekly_review',start=DAY,end='2020-01-12')
    assert core.query('state')==before
    segments=display_segments(weekly['summary'])
    assert {x['key']:x['count'] for x in segments}=={
        'course_task:done':1,'project_task:partial':1,'course_attendance:'+attendance:1}
    labels=' '.join(x['label'] for x in segments)
    assert '其他' not in labels and '课程 · ' in labels and '项目任务 · 部分完成' in labels
    assert weekly['summary']['done']==1, 'Attendance never counts as completing coursework'
    bar=VerticalCoverageBar(weekly['days'][0]);bar.resize(150,230)
    assert sum(ratio for _,_,ratio in bar.segment_rects())==pytest.approx(1)
    assert '其他' not in bar.description()
    chart=CoverageChart();chart.set_summary(weekly['summary'])
    assert '其他' not in chart.legend.text()
    bar.close();chart.close()

def test_dashboard_rolls_subprojects_into_parent_after_move(tmp_path):
    core=Core(tmp_path/'data')
    course=cmd(core,'create',{'type':'course','title':'SC3060','data':{'code':'SC3060'}})['entity']
    project=cmd(core,'create',{'type':'project','title':'3D representative work'})['entity']
    child=cmd(core,'create',{'type':'project','title':'A nested phase','parent_id':project['id']})['entity']
    task=cmd(core,'create',{'type':'task','title':'Model','parent_id':child['id']})['entity']
    cmd(core,'create',{'type':'task','title':'Lecture task','parent_id':course['id'],'status':'done'})
    assert {x['id'] for x in core.query('dashboard')['owners']}=={course['id'],project['id']}
    moved=cmd(core,'move',{'id':project['id'],'version':project['version'],'parent_id':course['id']})['entity']
    before=core.query('state');after=core.query('dashboard')
    assert [x['id'] for x in after['owners']]==[course['id']]
    assert after['owners'][0]['total']==2 and after['owners'][0]['done']==1
    assert core.query('state')==before
    cmd(core,'move',{'id':moved['id'],'version':moved['version'],'parent_id':None})
    owners=core.query('dashboard')['owners']
    assert {x['id']:x['total'] for x in owners}=={course['id']:1,project['id']:1}

def test_large_font_settings_scrolls_and_keeps_controls_text_height(app,tmp_path):
    apply_appearance(app,{'theme':'dark','font_size':20,'font_family':'Microsoft YaHei'})
    bridge=ControlledBridge();dialog=SettingsDialog(bridge,{},tmp_path)
    try:
        dialog.resize(790,650);dialog.ai_mode.setCurrentIndex(0);dialog.tabs.setCurrentIndex(dialog._provider_tab)
        dialog.codex_project_note.setText('上次已连接：事务助手\n'+('本地已保存的项目路径 / '*8))
        dialog.show();settle(app,100)
        page=dialog.tabs.widget(dialog._provider_tab)
        assert isinstance(page,QScrollArea) and page.verticalScrollBar().maximum()>0
        for widget in (dialog.ai_mode,dialog.executable,dialog.model,dialog.timeout,dialog.model_refresh):
            assert widget.height()>=widget.fontMetrics().height()+18
        page.ensureWidgetVisible(dialog.codex_bridge_start)
        settle(app)
        assert not dialog.codex_bridge_start.visibleRegion().isEmpty()
        dialog.executable.setText('unsaved program path')
        apply_appearance(app,{'theme':'light','font_size':13,'font_family':'Microsoft YaHei'})
        settle(app)
        assert dialog.executable.text()=='unsaved program path'
    finally:dialog.close()

def test_new_menu_arrow_animates_open_and_returns_on_close(app):
    button=MenuButton('＋ 新建');button.setObjectName('Primary')
    menu=QMenu(button);menu.addAction('任务');button.setMenu(menu);button.show()
    try:
        assert button.arrow_angle==0 and not button.menu_open
        menu.popup(button.mapToGlobal(QPoint(0,button.height())));settle(app,200)
        assert menu.isVisible() and button.menu_open and button.arrow_angle==pytest.approx(180)
        menu.close();settle(app,200)
        assert not button.menu_open and button.arrow_angle==pytest.approx(0)
    finally:menu.close();button.close()


def test_week_columns_keep_common_baseline_with_different_detail_counts(app):
    from management.gui_charts import WeekDaysChart
    from test_forms_charts_v3 import days
    chart=WeekDaysChart();chart.resize(1250,650);chart.set_days(days());chart.show()
    try:
        settle(app)
        assert len({bar.mapTo(chart,QPoint(0,0)).y() for bar in chart.bars})==1
        assert len({bar.plot_rect().height() for bar in chart.bars})==1
    finally:chart.close()
