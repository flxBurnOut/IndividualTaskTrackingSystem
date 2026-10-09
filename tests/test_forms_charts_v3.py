"""Type-specific forms, normalized weekly columns and native calendar semantics."""
from __future__ import annotations
import copy
import os
import time
import uuid
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import QDate, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCalendarWidget, QDateEdit, QDialog, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget
from management.core import Core
from management.gui_forms import EntityForm, FieldEditor, label_type
from management.gui_charts import CoverageBar, CoverageChart, CoverageRing, VerticalCoverageBar, WeekDaysChart
from management.gui_calendar import install_calendar
from management.gui_review import ReviewPage
from management.schemas import BusinessError


@pytest.fixture(scope='session')
def app():
    # Windows' offscreen platform needs fonts loaded explicitly for layout evidence.
    from pathlib import Path
    from PySide6.QtGui import QFontDatabase
    instance = QApplication.instance() or QApplication([])
    if instance.platformName() == 'offscreen' and os.name == 'nt':
        for name in ('msyh.ttc', 'msyhbd.ttc'):
            path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / name
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
    return QApplication.instance() or QApplication([])


def wait(app,predicate,timeout=5):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        app.processEvents()
        if predicate(): return
        time.sleep(.005)
    raise AssertionError('UI did not finish')


class Bridge:
    def __init__(self,core):
        self.core=core
        state=core.query('state')
        self.epoch,self.revision=state['epoch'],state['revision']
        self.pending=0
        self.commands=[]
    def later(self,operation,callback,error):
        self.pending+=1
        def run():
            try:
                result=operation()
                self.epoch,self.revision=result['epoch'],result['revision']
                if callback: callback(result)
            except BusinessError as exc:
                if error: error({'code':exc.code,'message':exc.message})
                else: raise
            finally:
                self.pending-=1
        QTimer.singleShot(0,run)
    def query(self,name,callback=None,error=None,**params):
        self.later(lambda:self.core.query(name,**params),callback,error)
    def command(self,name,payload,callback=None,error=None,**options):
        options.setdefault('epoch',self.epoch)
        options.setdefault('expected_revision',self.revision)
        options.setdefault('request_id',str(uuid.uuid4()))
        self.commands.append((name,copy.deepcopy(payload)))
        self.later(lambda:self.core.command(name,payload,**options),callback,error)


def cmd(core,name,payload):
    state=core.query('state')
    return core.command(name,payload,request_id=str(uuid.uuid4()),epoch=state['epoch'],expected_revision=state['revision'])['result']


def create(core,kind,title='Synthetic',**extra):
    return cmd(core,'create',{'type':kind,'title':title,**extra})['entity']


def form(core,kind,**kwargs):
    bridge=Bridge(core)
    return EntityForm(bridge,core.query('capabilities'),default_type=kind,**kwargs),bridge


@pytest.mark.parametrize('kind,title,fields',[
    ('course','新建课程',['code','notes']),
    ('project','新建项目',['purpose','acceptance','due_date']),
    ('activity','新建活动',['notes','due_date']),
])
def test_primary_objects_are_independent_with_distinct_fields_and_optional_classification(app,tmp_path,kind,title,fields):
    core=Core(tmp_path/kind)
    dialog,bridge=form(core,kind)
    try:
        dialog.show()
        app.processEvents()
        assert dialog.heading.text()==title and dialog.windowTitle()==title
        assert dialog.primary_field_names==fields
        assert dialog.parent_mode=='classification'
        assert dialog.parent_id is None
        assert not dialog.classification_enabled.isChecked()
        assert dialog.parent_picker.isHidden()
        assert dialog.parent_picker_types==['domain']
        assert '可选' in dialog.parent_label.text()
        assert not any('放在哪个项目或课程' in label.text() for label in dialog.findChildren(QLabel))
        dialog.title_edit.setText('Independent '+kind)
        dialog.save()
        wait(app,lambda:bridge.pending==0)
        assert dialog.result()==QDialog.DialogCode.Accepted,dialog.error_label.text()
        saved=core.query('list',type=kind)['items'][0]
        assert saved['parent_id'] is None
        assert 'due_date' not in saved['data']
        assert core.query('list',type='domain')['total']==0
    finally:
        dialog.close()


def test_course_and_activity_do_not_inherit_an_illegal_project_context(app,tmp_path):
    core=Core(tmp_path/'data')
    project=create(core,'project')
    for kind in ('course','activity'):
        dialog,bridge=form(core,kind,parent_entity=project)
        try:
            assert dialog.parent_id is None
            assert dialog.parent_mode=='classification'
            assert dialog.parent_picker_types==['domain']
            dialog.title_edit.setText('Independent '+kind)
            dialog.save()
            wait(app,lambda:bridge.pending==0)
            assert dialog.result()==QDialog.DialogCode.Accepted,dialog.error_label.text()
            assert core.query('list',type=kind)['items'][0]['parent_id'] is None
        finally:
            dialog.close()


def test_context_subproject_keeps_parent_and_own_project_goal_fields(app,tmp_path):
    core=Core(tmp_path/'data')
    parent=create(core,'project',title='Parent project')
    dialog,bridge=form(core,'project',parent_entity=parent)
    try:
        assert dialog.heading.text()=='新建子项目'
        assert dialog.parent_mode=='context'
        assert dialog.parent_id==parent['id']
        assert dialog.parent_button.text()=='Parent project'
        assert not dialog.parent_button.isEnabled()
        dialog.clear_parent()
        assert dialog.parent_id==parent['id']
        dialog.title_edit.setText('Distinct child outcome')
        dialog.fields['purpose'].editor.setPlainText('Deliver this subproject outcome')
        dialog.save()
        wait(app,lambda:bridge.pending==0)
        assert dialog.result()==QDialog.DialogCode.Accepted,dialog.error_label.text()
        saved=core.query('get',id=parent['id'])['children'][0]
        assert saved['parent_id']==parent['id'] and saved['data']['purpose']=='Deliver this subproject outcome'
    finally:
        dialog.close()


def test_task_keeps_explicit_assignment_and_full_completion_gate(app,tmp_path):
    core=Core(tmp_path/'data')
    course=create(core,'course')
    dialog,bridge=form(core,'task',parent_entity=course)
    try:
        assert dialog.parent_mode=='assignment'
        assert dialog.parent_label.text()=='任务归属'
        assert dialog.parent_id==course['id']
        assert dialog.primary_field_names==['completion_gate','estimated_minutes','due_date']
        assert 'course' in dialog.parent_picker_types
        dialog.title_edit.setText('Practice task')
        dialog.fields['completion_gate'].editor.setPlainText('Solve and verify all three questions')
        dialog.save()
        wait(app,lambda:bridge.pending==0)
        saved=core.query('list',type='task')['items'][0]
        assert saved['parent_id']==course['id']
        assert saved['data']['completion_gate']=='Solve and verify all three questions'
        assert 'estimated_minutes' not in saved['data']
    finally:
        dialog.close()


def test_classification_picker_only_offers_domain_and_can_clear_without_changing_type(app,tmp_path,monkeypatch):
    core=Core(tmp_path/'data')
    category=create(core,'domain',title='学习')
    seen=[]
    class Picker:
        selected=category
        def __init__(self,bridge,parent=None,allowed_types=None):
            seen.append(allowed_types)
            self.heading=QLabel()
        def setWindowTitle(self,title): pass
        def exec(self): return QDialog.DialogCode.Accepted
    monkeypatch.setattr('management.gui_forms.EntityPicker',Picker)
    dialog,bridge=form(core,'course')
    try:
        dialog.classification_enabled.setChecked(True)
        dialog.choose_parent()
        assert seen==[['domain']]
        assert dialog.parent_id==category['id']
        assert dialog.parent_button.text()=='学习'
        dialog.classification_enabled.setChecked(False)
        assert dialog.parent_id is None
        dialog.title_edit.setText('Still a course')
        dialog.save()
        wait(app,lambda:bridge.pending==0)
        assert core.query('list',type='course')['items'][0]['parent_id'] is None
        assert core.query('get',id=category['id'])['entity']['type']=='domain'
        assert label_type('domain')=='分类'
    finally:
        dialog.close()


def test_selected_classification_must_be_resolved_or_explicitly_omitted(app,tmp_path):
    core=Core(tmp_path/'data')
    dialog,bridge=form(core,'activity')
    try:
        dialog.title_edit.setText('Activity')
        dialog.classification_enabled.setChecked(True)
        dialog.save()
        assert bridge.commands==[]
        assert '请选择一个分类' in dialog.error_label.text()
    finally:
        dialog.close()


def test_edit_preserves_existing_parent_extensions_nulls_and_status(app,tmp_path):
    core=Core(tmp_path/'data')
    parent=create(core,'project',title='Existing parent')
    entity=create(core,'project',parent_id=parent['id'],status='blocked',data={
        'purpose':None,'acceptance':'Existing gate','notes':'Preserve note','custom_payload':{'count':3}})
    dialog,bridge=form(core,'project',entity=entity)
    try:
        wait(app,lambda:bridge.pending==0)
        assert dialog.parent_mode=='existing'
        dialog.clear_parent()
        dialog.title_edit.setText('Renamed')
        dialog.more_fields.click()
        dialog.more_fields.click()
        dialog.save()
        wait(app,lambda:bridge.pending==0)
        assert dialog.result()==QDialog.DialogCode.Accepted,dialog.error_label.text()
        actual=core.query('get',id=entity['id'])['entity']
        assert actual['parent_id']==parent['id'] and actual['status']=='blocked'
        assert actual['data']==entity['data']
    finally:
        dialog.close()



def test_course_semester_fields_are_optional_then_save_only_explicit_anchors(app,tmp_path):
    core=Core(tmp_path/'semester')
    dialog,bridge=form(core,'course')
    try:
        assert dialog.more_fields.text()=='学期与更多信息'
        for key in ('term','semester_start','semester_end'):
            assert key in dialog.fields
            assert not dialog.field_layout.isRowVisible(dialog.fields[key])
            assert dialog.fields[key].value() is None
        dialog.more_fields.click()
        assert dialog.field_layout.isRowVisible(dialog.fields['semester_start'])
        assert '周一' in dialog.fields['semester_start'].toolTip()
        dialog.fields['term'].enabled.setChecked(True)
        dialog.fields['term'].editor.setText('2030 第一学期')
        dialog.fields['semester_start'].enabled.setChecked(True)
        dialog.fields['semester_start'].editor.setDate(QDate(2030,1,7))
        dialog.title_edit.setText('Synthetic course with an explicit anchor')
        dialog.save()
        wait(app,lambda:bridge.pending==0)
        assert dialog.result()==QDialog.DialogCode.Accepted,dialog.error_label.text()
        entity=core.query('list',type='course')['items'][0]
        assert entity['data']['term']=='2030 第一学期'
        assert entity['data']['semester_start']=='2030-01-07'
        assert 'semester_end' not in entity['data']
    finally:
        dialog.close()


def days():
    return [
        {'date':'2030-01-07','has_plan':True,'summary':{'total':2,'done':1,'incomplete':1,'unreported':0}},
        {'date':'2030-01-08','has_plan':True,'summary':{'total':10,'done':5,'incomplete':0,'unreported':5}},
        {'date':'2030-01-09','has_plan':False,'summary':{}},
        {'date':'2030-01-10','has_plan':True,'summary':{'total':0}},
        {'date':'2030-01-11','has_plan':True,'summary':{'total':4,'done':0,'incomplete':0,'unreported':4}},
        {'date':'2030-01-12','has_plan':True,'summary':{'total':4,'done':1,'incomplete':1,'other_reported':1,'unreported':1}},
        {'date':'2030-01-13','has_plan':True,'summary':{'total':3,'done':3,'incomplete':0,'unreported':0}},
    ]


def test_columns_are_equal_height_and_normalized_by_each_days_denominator(app):
    chart=WeekDaysChart()
    chart.resize(560,390)
    chart.set_days(days())
    chart.show()
    app.processEvents()
    try:
        assert chart.layout_style=='columns'
        assert all(isinstance(bar,VerticalCoverageBar) for bar in chart.bars)
        assert len({bar.plot_rect().height() for bar in chart.bars})==1
        first,second=chart.bars[:2]
        a=next(rect for key,rect,ratio in first.segment_rects() if key=='done')
        b=next(rect for key,rect,ratio in second.segment_rects() if key=='done')
        assert a.height()==pytest.approx(b.height())
        assert first.total==2 and second.total==10
        for bar in chart.bars:
            if bar.kind=='data':
                assert sum(r.height() for _,r,_ in bar.segment_rects())==pytest.approx(bar.plot_rect().height())
                assert sum(ratio for _,_,ratio in bar.segment_rects())==pytest.approx(1)
        assert chart.bars[2].kind=='missing_plan' and chart.bars[2].segment_rects()==[]
        assert chart.bars[3].kind=='empty_plan' and chart.bars[3].segment_rects()==[]
        assert '0%' not in chart.bars[2].description() and '0%' not in chart.bars[3].description()
        assert chart.bars[4].values['incomplete']==0
        assert chart.bars[4].segment_rects()[0][0]=='unreported'
        assert chart.bars[5].values['other_reported']==1
    finally:
        chart.close()


def test_rows_columns_switch_preserves_day_data_and_navigation(app):
    chart=WeekDaysChart()
    original=days()
    chart.set_days(original)
    clicked=[]
    chart.date_selected.connect(clicked.append)
    try:
        chart.set_style('rows')
        assert chart.layout_style=='rows'
        assert all(isinstance(bar,CoverageBar) for bar in chart.bars)
        assert chart.days==original
        chart.set_style('columns')
        assert chart.days==original
        buttons=[b for b in chart.findChildren(QPushButton) if b.parentWidget().parentWidget() is not None and b.text()=='01-07']
        buttons[-1].click()
        assert clicked==['2030-01-07']
        with pytest.raises(ValueError): chart.set_style('pie')
        assert chart.layout_style=='columns'
    finally:
        chart.close()


def test_ring_keeps_denominator_unknown_and_attendance_separate(app):
    chart=CoverageChart()
    summary={'total':4,'done':1,'unreported':2,'other_reported':1,'fixed_scheduled':1,
             'breakdown':[{'key':'task:done','kind':'task','result':'done','count':1},
                          {'key':'course:attended','kind':'course_attendance','result':'attended','count':1},
                          {'key':'task:unreported','kind':'task','result':'unreported','count':2}]}
    try:
        chart.set_summary(summary,denominator='四个虚构事项')
        wording=[label.text() for label in (chart.heading,chart.legend,chart.denominator)]
        chart.set_style('ring')
        assert chart.summary==chart.ring.summary==summary
        assert chart.ring.values['incomplete']==0 and chart.ring.values['unreported']==2
        assert sum(angle for _,angle in chart.ring.segment_angles())==pytest.approx(360)
        assert next(angle for row,angle in chart.ring.segment_angles() if row['result']=='unreported')==180
        assert wording==[label.text() for label in (chart.heading,chart.legend,chart.denominator)]
        assert '课程 · 已参加' in chart.legend.text() and '已反馈 2 / 4' in chart.heading.text()
        chart.set_summary({})
        assert chart.ring.segment_angles()==[] and '不计算' in chart.ring.accessibleName()
        assert '不按 0% 完成处理' in chart.denominator.text()
        chart.resize(420,360);chart.show();app.processEvents()
        assert not chart.grab().isNull()
    finally:chart.close()


def test_weekly_circle_cards_keep_missing_empty_unreported_and_navigation(app):
    chart=WeekDaysChart();original=days();selected=[]
    chart.date_selected.connect(selected.append)
    try:
        chart.set_days(original);chart.set_style('tiles');chart.resize(460,900);chart.show();app.processEvents()
        assert chart.days==original and len(chart.cards)==7
        assert all(isinstance(bar,CoverageRing) for bar in chart.bars)
        assert chart.bars[2].empty_label=='无计划' and chart.bars[3].empty_label=='空计划'
        assert chart.bars[2].segment_angles()==chart.bars[3].segment_angles()==[]
        assert '缺计划' in chart.day_labels[2].text() and '空计划' in chart.day_labels[3].text()
        assert '未反馈 4' in chart.day_labels[4].text() and '未完成' not in chart.day_labels[4].text()
        assert chart.bars[6].values['done']==3
        chart.cards[0].findChild(QPushButton).click()
        assert selected==['2030-01-07']
        chart.set_style('columns');assert chart.days==original
    finally:chart.close()


def test_chart_restyling_preserves_unsaved_review_choices_without_requests(app):
    from test_review_ui_v2 import ControlledBridge,daily,load
    bridge=ControlledBridge();page=ReviewPage(bridge);requested=[]
    page.chart_settings_requested.connect(requested.append)
    try:
        load(page,bridge,daily());button=page.item_buttons['task-a']['done'];button.click()
        before=copy.deepcopy(page._states);queries=len(bridge.queries)
        page.set_chart_preferences({'review_daily_style':'ring','review_weekly_style':'ring','weekly_style':'tiles'})
        assert page.item_buttons['task-a']['done'] is button and button.isChecked()
        assert page._states==before and page._pending
        assert len(bridge.queries)==queries and bridge.commands==[]
        assert page.daily_chart.chart_style==page.week_chart.chart_style=='ring' and page.week_days.layout_style=='tiles'
        next(b for b in page.findChildren(QPushButton) if b.accessibleName()=='设置每日复盘图表样式').click()
        assert requested==['review_daily_style']
    finally:page.close()


def test_dashboard_charts_restyle_locally_and_do_not_label_all_open_tasks_as_failure(app):
    from management.gui_dashboard import DashboardPage
    from test_review_ui_v2 import ControlledBridge
    bridge=ControlledBridge();page=DashboardPage(bridge);requested=[]
    page.chart_settings_requested.connect(requested.append)
    sample={'date_label':'2030 年 1 月 7 日','weekday':'星期一','date':'2030-01-07',
        'week_label':'演示周','week_start':'2030-01-07','week_end':'2030-01-13','periods':[],
        'plan':{'has_plan':True,'summary':{'total':2,'done':0,'unreported':2}},
        'warnings':{'counts':{'current':0,'past':0},'items':[]},
        'tasks':{'total':5,'done':1,'open':4},'days':[],'owners':[]}
    try:
        page.render(sample)
        page.set_chart_preferences({'dashboard_today_style':'ring','dashboard_tasks_style':'ring'})
        assert not bridge.queries and not bridge.commands
        assert page.charts['plan'].ring.values['unreported']==2
        assert page.charts['tasks'].ring.total==5 and page.charts['tasks'].ring.values['incomplete']==0
        assert '待办（含待反馈）' in page.charts['tasks'].legend.text()
        assert '待办不等于失败' in page.charts['tasks'].denominator.text()
        next(b for b in page.findChildren(QPushButton) if b.accessibleName()=='设置任务概况图表样式').click()
        assert requested==['dashboard_tasks_style']
    finally:page.close()


@pytest.mark.parametrize('theme',['light','dark'])
def test_circle_cards_fit_narrow_scroll_area_at_large_font(app,theme):
    from management.gui_theme import apply_appearance,current_appearance
    previous=current_appearance();scroll=QScrollArea();chart=WeekDaysChart()
    try:
        apply_appearance(app,{**previous,'theme':theme,'font_size':20})
        chart.set_style('tiles');chart.set_days(days());scroll.setWidgetResizable(True);scroll.setWidget(chart)
        scroll.resize(420,600);scroll.show();app.processEvents()
        assert scroll.horizontalScrollBar().maximum()==0
        assert chart.bars[2].minimumWidth()<=chart.cards[2].width()
        assert not scroll.grab().isNull()
    finally:
        scroll.close();scroll.deleteLater();app.processEvents();apply_appearance(app,previous)


@pytest.mark.parametrize('theme,font_size',[('light',13),('dark',20)])
@pytest.mark.parametrize('width',[720,1120])
def test_dashboard_switching_chart_style_updates_parent_height_without_clipping(app,theme,font_size,width):
    from management.gui_dashboard import DashboardPage
    from management.gui_theme import apply_appearance,current_appearance
    from test_review_ui_v2 import ControlledBridge
    previous=current_appearance();page=DashboardPage(ControlledBridge())
    sample={'date_label':'2030 年 1 月 7 日','weekday':'星期一','date':'2030-01-07',
        'week_label':'演示周','week_start':'2030-01-07','week_end':'2030-01-13','periods':[],
        'plan':{'has_plan':False,'summary':{'total':0,'done':0,'unreported':0}},
        'warnings':{'counts':{'current':0,'past':0},'items':[]},
        'tasks':{'total':1,'done':0,'open':1},'days':[
            {'weekday':'周'+'一二三四五六日'[index],'date':f'2030-01-{7+index:02d}',
             'is_today':index==0,'events':[],'event_count':0} for index in range(7)],'owners':[]}
    def rect_in(widget,ancestor):
        return QRect(widget.mapTo(ancestor,QPoint(0,0)),widget.size())
    def settle_layout():
        app.processEvents()
        wait(app,lambda:not page._layout_pending and not page._relayout_active and not page._layout_timer.isActive())
    def assert_sections_do_not_overlap():
        body=page.widget()
        heading=next(label for label in page.findChildren(QLabel) if label.text()=='本周固定日程')
        cards=[rect_in(card,body) for card in page.metric_cards]
        assert all(page.metric_panel.rect().contains(rect_in(card,page.metric_panel)) for card in page.metric_cards), (page.metric_panel.rect(),page.metric_panel.minimumHeight(),[(rect_in(card,page.metric_panel),card.minimumHeight(),card.minimumSizeHint()) for card in page.metric_cards],page.metric_grid.minimumSize(),page.metric_grid.totalHeightForWidth(page.metric_panel.width()))
        assert all(card.bottom()<rect_in(heading,body).top() for card in cards)
        regions=cards+[rect_in(card,body) for card in page.week_cards]
        assert all(not first.intersects(second) for index,first in enumerate(regions) for second in regions[index+1:])
    try:
        apply_appearance(app,{**previous,'theme':theme,'font_size':font_size})
        page.resize(width,760);page.render(sample);page.show();settle_layout()
        page.set_chart_preferences({'dashboard_today_style':'bar','dashboard_tasks_style':'bar'})
        settle_layout()
        page.set_chart_preferences({'dashboard_today_style':'ring','dashboard_tasks_style':'ring'})
        settle_layout()
        ring_heights={key:chart.minimumHeight() for key,chart in page.charts.items()}
        for chart in page.charts.values():
            ring=rect_in(chart.ring,chart);card=chart.parentWidget()
            assert chart.rect().contains(ring)
            assert card.rect().contains(rect_in(chart.ring,card))
            for label in (chart.legend,chart.denominator):
                assert chart.rect().contains(rect_in(label,chart))
                assert not ring.intersects(rect_in(label,chart))
        assert_sections_do_not_overlap()
        page.refresh();page.bridge.deliver('dashboard',sample);settle_layout()
        assert_sections_do_not_overlap()
        apply_appearance(app,{**previous,'theme':theme,'font_size':20 if font_size==13 else 13});settle_layout()
        assert_sections_do_not_overlap()
        apply_appearance(app,{**previous,'theme':theme,'font_size':font_size});settle_layout()
        page.set_chart_preferences({'dashboard_today_style':'bar','dashboard_tasks_style':'bar'})
        settle_layout()
        assert_sections_do_not_overlap()
        for key,chart in page.charts.items():
            assert chart.ring.isHidden() and not chart.bar.isHidden()
            assert chart.minimumHeight()<ring_heights[key]
            assert chart.rect().contains(rect_in(chart.bar,chart))
    finally:
        page.close();page.deleteLater();app.processEvents();apply_appearance(app,previous)


def test_dashboard_font_change_reflows_cards_without_resizing_window(app):
    from management.gui_dashboard import DashboardPage
    from management.gui_theme import apply_appearance,current_appearance
    from test_review_ui_v2 import ControlledBridge
    previous=current_appearance();page=DashboardPage(ControlledBridge())
    try:
        apply_appearance(app,{**previous,'font_size':13})
        page.resize(850,700);page.show();app.processEvents()
        assert page.metric_grid.getItemPosition(1)[:2]==(0,1)
        apply_appearance(app,{**previous,'font_size':20});app.processEvents()
        assert page.metric_grid.getItemPosition(1)[:2]==(1,0)
    finally:
        page.close();page.deleteLater();app.processEvents();apply_appearance(app,previous)


def test_dashboard_details_have_local_actions_and_reflow_without_business_changes(app):
    from management.gui_dashboard import DashboardPage
    from management.gui_theme import apply_appearance, current_appearance
    from management.gui_visual_profile import set_visual_style, visual_style
    from test_review_ui_v2 import ControlledBridge
    from PySide6.QtWidgets import QPushButton, QProgressBar
    previous, old_style=current_appearance(),visual_style()
    set_visual_style('glass');opened=[];bridge=ControlledBridge()
    page=DashboardPage(bridge,on_task=opened.append,on_projects=opened.append)
    sample={'date':'2030-01-07','date_label':'2030 年 1 月 7 日','weekday':'星期一',
        'week_label':'演示周','week_start':'2030-01-07','week_end':'2030-01-13','periods':[],
        'plan':{'has_plan':False,'summary':{'total':0,'done':0,'unreported':0}},
        'warnings':{'counts':{'current':52,'past':0},'items':[
            {'id':'task-sample','title':'检查虚构活动的准备材料','reason':'明天到期','owner_label':'虚构项目'}], 'next_offset':5},
        'tasks':{'total':8,'done':2,'open':6},'days':[],
        'owners':[{'id':'course-sample','label':'虚构课程','total':8,'done':2,'open':6}]}
    try:
        apply_appearance(app,{**previous,'font_size':13})
        page.resize(1200,850);page.show();page.render(sample);app.processEvents()
        wait(app,lambda:not page._layout_pending)
        assert page.metric_cards[1].width()<page.metric_panel.width()*.6
        assert page.details_grid.getItemPosition(1)[:2]==(0,1)
        assert page.attention_section.width()<page.viewport().width()*.7
        assert page.all_alerts.width()<=page.all_alerts.sizeHint().width()+2
        assert page.owner_open.width()<=page.owner_open.sizeHint().width()+2
        assert all(bar.width()<=280 for bar in page.owner_section.findChildren(QProgressBar))
        assert page.owner_open.isVisible() and page.owner_open.height()>=page.owner_open.sizeHint().height()
        for bar in page.owner_section.findChildren(QProgressBar):
            assert bar.isVisible() and bar.geometry().bottom()<page.owner_section.height()
            assert bar.height()==6
        assert page.owner_rows.count()==1
        next(button for button in page.alert_box.findChildren(QPushButton) if button.text().startswith('查看事项')).click()
        assert opened==['task-sample']
        apply_appearance(app,{**previous,'font_size':20});page.resize(760,850);app.processEvents()
        assert page.details_grid.getItemPosition(1)[:2]==(1,0)
        assert page.horizontalScrollBar().maximum()==0
        assert not getattr(bridge,'commands',[])
        set_visual_style('classic');apply_appearance(app,previous);app.processEvents()
        assert page.alert_box.objectName()=='CurrentWarningCard'
        assert page.details_grid.getItemPosition(1)[:2]==(1,0)
        assert sample['warnings']['counts']['current']==52
    finally:
        page.close();page.deleteLater();app.processEvents()
        set_visual_style(old_style);apply_appearance(app,previous)


def test_calendar_installation_is_idempotent_preserves_date_bounds_and_optional_unknown(app):
    editor=QDateEdit(QDate(2030,1,7))
    editor.setDateRange(QDate(2029,1,1),QDate(2031,12,31))
    calendar=install_calendar(editor)
    assert install_calendar(editor) is calendar
    assert editor.date()==QDate(2030,1,7)
    assert calendar.selectedDate()==editor.date()
    assert calendar.minimumDate()==QDate(2029,1,1)
    assert calendar.maximumDate()==QDate(2031,12,31)
    assert calendar.firstDayOfWeek()==Qt.DayOfWeek.Monday
    assert calendar.verticalHeaderFormat()==QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
    field=FieldEditor('due_date',{'type':'date'})
    assert field.value() is None
    assert getattr(field.editor,'_management_calendar',None) is not None
    assert not field.enabled.isChecked()
    assert field.editor.text()=='未填写'
    field.enabled.setChecked(True)
    field.editor.setDate(QDate(2030,2,4))
    field.enabled.setChecked(False)
    assert field.value() is None and field.editor.text()=='未填写'
    field.enabled.setChecked(True)
    assert field.value()=='2030-02-04'
    editor.close()
    field.close()


@pytest.mark.parametrize('width',[500,760])
def test_type_specific_form_fits_narrow_width_without_horizontal_scroll(app,tmp_path,width):
    core=Core(tmp_path/'narrow')
    dialog,_=form(core,'project')
    try:
        dialog.resize(width,700)
        dialog.show()
        app.processEvents()
        scroll=dialog.findChild(QScrollArea)
        assert scroll.horizontalScrollBar().maximum()==0
        assert dialog.title_edit.geometry().right()<=dialog.title_edit.parentWidget().width()
        assert dialog.parent_area.geometry().right()<=dialog.parent_area.parentWidget().width()
    finally:
        dialog.close()


def save_previews(directory):
    import json
    import tempfile
    from pathlib import Path
    from management.gui import STYLESHEET,configure_palette
    out=Path(directory)
    out.mkdir(parents=True,exist_ok=True)
    app=QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    configure_palette(app)
    app.setFont(QFont('Microsoft YaHei UI',10))
    app.setStyleSheet(STYLESHEET)
    measurements={}
    with tempfile.TemporaryDirectory(dir=out,prefix='synthetic-') as temp:
        core=Core(Path(temp)/'data')
        for kind in ('course','project','activity','task'):
            dialog,_=form(core,kind)
            dialog.resize(530,720)
            dialog.show()
            app.processEvents()
            assert dialog.findChild(QScrollArea).horizontalScrollBar().maximum()==0
            assert dialog.grab().save(str(out/(kind+'.png')))
            measurements[kind]={'logical_size':[dialog.width(),dialog.height()],'dpr':dialog.devicePixelRatioF()}
            if kind=='course':
                dialog.more_fields.click()
                app.processEvents()
                assert dialog.findChild(QScrollArea).horizontalScrollBar().maximum()==0
                assert dialog.grab().save(str(out/'course-semester.png'))
            if kind=='project':
                date=dialog.fields['due_date']
                date.enabled.setChecked(True)
                date.editor.setDate(QDate(2030,1,7))
                dialog.findChild(QScrollArea).ensureWidgetVisible(date.editor)
                app.processEvents()
                QTest.mouseClick(date.editor,Qt.MouseButton.LeftButton,pos=QPoint(date.editor.width()-9,date.editor.height()//2))
                app.processEvents()
                calendar=date.editor.calendarWidget()
                assert calendar.isVisible()
                assert calendar.grab().save(str(out/'calendar.png'))
                measurements['calendar']={'logical_size':[calendar.width(),calendar.height()],'dpr':calendar.devicePixelRatioF()}
                QTest.keyClick(calendar,Qt.Key.Key_Escape)
            dialog.close()
        chart=WeekDaysChart()
        chart.resize(540,410)
        chart.set_days(days())
        chart.show()
        app.processEvents()
        assert chart.grab().save(str(out/'weekly-columns.png'))
        measurements['weekly']={'logical_size':[chart.width(),chart.height()],'dpr':chart.devicePixelRatioF(),
            'heights':[bar.plot_rect().height() for bar in chart.bars],'kinds':[bar.kind for bar in chart.bars]}
        chart.set_style('rows')
        chart.resize(640,580)
        app.processEvents()
        assert chart.grab().save(str(out/'weekly-rows.png'))
        chart.close()
    (out/'geometry.json').write_text(json.dumps(measurements,ensure_ascii=False,indent=2),encoding='utf-8')
    return measurements


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--snapshot-dir',required=True)
    print(save_previews(parser.parse_args().snapshot_dir))
