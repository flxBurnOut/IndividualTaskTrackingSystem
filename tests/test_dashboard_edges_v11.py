"""Dashboard date projections, current totals and asynchronous time refresh."""
import os,datetime as dt,time
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from types import SimpleNamespace
import pytest
from PySide6.QtCore import QDate,QCoreApplication,QEvent,QPoint,QRect
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QLabel,QDateEdit,QPushButton
from management.core import Core
from management.gui_dashboard import DashboardPage
from management.gui_today import TodayPage
from management.gui import MainWindow
from test_ux_workflows_v2 import ControlledBridge,cmd

@pytest.fixture(scope='session')
def app():
    application = QApplication.instance() or QApplication([])
    if application.platformName() == 'offscreen' and os.name == 'nt':
        for name in ('msyh.ttc', 'msyhbd.ttc'):
            path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / name
            if path.is_file(): QFontDatabase.addApplicationFont(str(path))
    application.setStyle('Fusion')
    return application

def create(core,kind,title,**p):return cmd(core,'create',{'type':kind,'title':title,**p})['result']['entity']


def settle_today_layout(app, condition):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        app.processEvents()
        if condition(): return
        QTest.qWait(10)


def test_dashboard_week_heading_matches_fixed_events_scope(app):
    opened=[];page=DashboardPage(ControlledBridge(),on_timetable=opened.append);page.day='2030-01-02'
    try:
        page.render({'date_label':'2030 年 1 月 2 日','weekday':'星期三','week_label':'教学周尚未设置',
            'week_start':'2029-12-31','week_end':'2030-01-06','periods':[],
            'plan':{'has_plan':False,'summary':{'total':0,'done':0,'unreported':0}},
            'tasks':{'total':0,'done':0,'open':0},'days':[],'owners':[],
            'warnings':{'counts':{'current':0,'past':0},'items':[]}})
        assert page.position.text()=='本周  ·  12-31 — 01-06'
        words=[label.text() for label in page.findChildren(QLabel)]
        assert '本周固定日程' in words and '本周安排' not in words
        next(button for button in page.findChildren(QPushButton) if button.text()=='查看每周日程').click()
        assert opened==['2030-01-02']
    finally:page.close();page.deleteLater();app.processEvents()


@pytest.mark.parametrize('font_size', [13, 20])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_dashboard_long_owner_warning_and_event_text_survives_resize(app, font_size, theme):
    from management.gui_theme import apply_appearance, current_appearance
    original = current_appearance()
    apply_appearance(app, {**original, 'font_size': font_size, 'theme': theme})
    bridge, opened = ControlledBridge(), []
    page = DashboardPage(bridge, on_projects=opened.append, on_task=opened.append)
    page.resize(1400, 900); page.show(); app.processEvents()
    title = '【演示】设计研究课程与综合实践项目，保留完整课程名称。' * 3
    reason = '截止日期：2030-01-09。请核对原始要求与已保存的完成标准。' * 4
    result = {'date': '2030-01-02', 'date_label': '2030 年 1 月 2 日', 'weekday': '星期三',
        'week_label': '', 'week_start': '2029-12-31', 'week_end': '2030-01-06', 'periods': [],
        'plan': {'has_plan': False, 'summary': {'total': 0, 'done': 0, 'unreported': 0}},
        'tasks': {'total': 2, 'done': 1, 'open': 1},
        'days': [{'weekday': '周三', 'date': '2030-01-02', 'is_today': True, 'event_count': 1,
                  'events': [{'start': '09:00', 'title': title, 'event_kind': 'other', 'owner_label': title}]}],
        'owners': [{'id': 'owner-long', 'label': title, 'done': 1, 'total': 2}],
        'warnings': {'counts': {'current': 1, 'past': 1}, 'items': [{'id': 'warning-long', 'title': title, 'reason': reason}]}}
    try:
        page.refresh(); bridge.deliver('dashboard', result)
        page.past_toggle.click()
        bridge.deliver('warnings', {'items': [{'id': 'past-long', 'title': title, 'reason': reason}], 'next_offset': None})
        for width in (1400, 650, 1400):
            page.resize(width, 900)
            labels = [label for label in page.findChildren(QLabel) if label.wordWrap() and label.text()]
            settle_today_layout(app, lambda: all(label.height() >= label.heightForWidth(label.width()) - 1
                                                  for label in labels if label.isVisible()))
            assert page.horizontalScrollBar().maximum() == 0
            for label in labels:
                if label.isVisible():
                    assert label.height() >= label.heightForWidth(label.width()) - 1, (width, label.text(), label.size(), label.heightForWidth(label.width()), label.parentWidget().objectName(), label.parentWidget().size(), label.parentWidget().minimumHeight(), label.parentWidget().layout().totalHeightForWidth(label.parentWidget().width()))
                    ancestor = label.parentWidget()
                    while ancestor is not None:
                        region = QRect(label.mapTo(ancestor, QPoint()), label.size())
                        assert ancestor.rect().contains(region), (label.text(), ancestor.objectName(), ancestor.rect(), region)
                        if ancestor is page.widget(): break
                        ancestor = ancestor.parentWidget()
            owner_labels = [label for label in page.owner_section.findChildren(QLabel) if label.text() == title]
            assert len(owner_labels) == 1 and owner_labels[0].wordWrap(), 'full owner name must be visible rather than clipped in a capped button'
        owner_action = page.owner_section.findChild(QPushButton, 'OwnerOpen')
        owner_action.click(); assert opened == ['owner-long']
        section_layout = page.attention_section.layout()
        expanded_height = section_layout.totalHeightForWidth(page.attention_section.width())
        page.past_toggle.click()
        settle_today_layout(app, lambda: section_layout.totalHeightForWidth(page.attention_section.width()) < expanded_height)
        assert page.past_box.isHidden() and section_layout.totalHeightForWidth(page.attention_section.width()) < expanded_height
        assert bridge.commands == []
    finally:
        page.close(); page.deleteLater(); app.processEvents()
        apply_appearance(app, original)


@pytest.mark.parametrize('fixed',[False,True])
def test_today_only_explains_attendance_when_fixed_schedules_are_present(app,fixed):
    from test_review_ui_v2 import daily
    page=TodayPage(ControlledBridge());value=daily()
    value['has_fixed_schedule']=fixed
    try:
        page.render_plan(value)
        assert ('出勤不代表学习完成' in page.progress.title.text()) is fixed
        assert ('按任务完成标准记录' in page.progress.title.text()) is not fixed
    finally:page.close();page.deleteLater();app.processEvents()

def test_week_and_today_show_reschedule_and_timezone_projected_clock(app,tmp_path):
    core=Core(tmp_path)
    create(core,'event','Changed class',data={'date':'2030-01-02','start':'09:00','end':'10:00','timezone':'Asia/Shanghai','location':'New room','exceptions':{'2030-01-02':{'start':'14:00','end':'15:00'}}})
    create(core,'event','UTC meeting',data={'date':'2030-01-02','start':'09:00','end':'10:00','timezone':'UTC'})
    result=core.query('dashboard',date='2030-01-02');events=result['days'][2]['events']
    assert [(e['title'],e['start'],e['end']) for e in events]==[('Changed class','14:00','15:00'),('UTC meeting','17:00','18:00')]
    page=TodayPage(ControlledBridge())
    try:
        with core.store.connect() as c:projected=core._events(c,'2030-01-02')[0]
        page.render_events(projected)
        words='\n'.join(w.text() for w in page.events_box.findChildren(QLabel))
        assert '14:00 – 15:00' in words and '17:00 – 18:00' in words and 'New room' in words
        assert '09:00 – 10:00' not in words
    finally:page.close();page.deleteLater();app.processEvents()


def test_home_task_and_owner_totals_both_exclude_draft(tmp_path):
    core=Core(tmp_path);owner=create(core,'course','Course')
    create(core,'task','Draft',parent_id=owner['id'],status='draft')
    create(core,'task','Open',parent_id=owner['id'])
    result=core.query('dashboard',date='2030-01-02')
    assert result['tasks']=={'total':1,'done':0,'open':1}
    assert {k:result['owners'][0][k] for k in ('total','done','open')}==result['tasks']
    # Other course summaries keep their established all-status scope.
    from management.task_views import summary
    with core.store.connect() as c:assert summary(core,c,owner['id'])[0]==2


def test_past_warning_newest_request_wins_and_collapsing_invalidates(app):
    bridge=ControlledBridge();page=DashboardPage(bridge);page.day='2030-01-02';page.past_toggle.setChecked(True)
    try:
        page.load_past(offset=0);old=bridge.take('warnings')
        page.load_past(offset=5);new=bridge.take('warnings')
        new['callback']({'items':[{'id':'new','title':'Current page','reason':'current'}]})
        old['callback']({'items':[{'id':'old','title':'Stale page','reason':'old'}]})
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        assert [w.text() for w in page.past_box.findChildren(QLabel)]==['Current page','current']
        page.load_past(offset=10);pending=bridge.take('warnings');page.past_toggle.setChecked(False);page.load_past()
        page.past_toggle.setChecked(True);page.load_past(offset=0)
        pending['callback']({'items':[{'id':'late','title':'Wrong late page','reason':'old'}]})
        assert 'Wrong late page' not in [w.text() for w in page.past_box.findChildren(QLabel)]
    finally:page.close();page.deleteLater();app.processEvents()


@pytest.mark.parametrize('section', ['dashboard', 'tasks'])
def test_dashboard_refreshes_on_clock_minute_without_business_changes(app,monkeypatch,section):
    import management.gui as gui
    bridge=ControlledBridge();calls=[];activity_calls=[];date=QDate(2030,1,2);clock=[date,101]
    monkeypatch.setattr(gui,'clock_snapshot',lambda zone:tuple(clock))
    host=SimpleNamespace(closed=False,poll_pending=False,type_map={'task':{}},_business_timezone='Asia/Shanghai',_calendar_day=date,_clock_minute=100,
        today_page=SimpleNamespace(date=QDateEdit(date)),section=section,bridge=bridge,change_cursor=0,refresh=lambda:calls.append('refresh'),
        refresh_assistant_activity=lambda:activity_calls.append('activity'),load_display_preferences=lambda:None,show_error=lambda e:None)
    MainWindow.poll_changes(host);bridge.deliver('changes',{'items':[],'cursor':0});assert calls==['refresh']
    MainWindow.poll_changes(host);bridge.deliver('changes',{'items':[],'cursor':0});assert calls==['refresh']
    clock[0]=date.addDays(1);clock[1]=102
    MainWindow.poll_changes(host);bridge.deliver('changes',{'items':[],'cursor':0})
    assert host.today_page.date.date()==date.addDays(1) and calls==['refresh','refresh']
    assert activity_calls==['activity','activity'] and bridge.commands==[]
    host.today_page.date.deleteLater();app.processEvents()


def test_clock_business_day_uses_configured_zone(monkeypatch):
    import management.gui as gui
    class FixedDateTime(dt.datetime):
        @classmethod
        def now(cls,tz=None):return dt.datetime(2030,1,2,18,0,tzinfo=dt.timezone.utc).astimezone(tz)
    monkeypatch.setattr(gui,'dt',SimpleNamespace(datetime=FixedDateTime,timezone=dt.timezone))
    local_day,key=gui.clock_snapshot('Asia/Shanghai')
    assert local_day==QDate(2030,1,3)
    assert gui.clock_snapshot('America/Los_Angeles')[0]==QDate(2030,1,2)


@pytest.mark.parametrize('font_size', [13, 20])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_today_actions_keep_content_width_and_wrap_without_wide_cards(app, font_size, theme):
    from PySide6.QtWidgets import QFrame
    from management.gui_layout import ActionRow
    from management.gui_theme import apply_appearance, current_appearance
    old_appearance = current_appearance()
    apply_appearance(app, {'font_size': font_size, 'theme': theme})
    bridge = ControlledBridge(); opened = []
    page = TodayPage(bridge, on_task=opened.append)
    page.resize(1400, 900)
    page.set_assistants_visible(True)
    page.review_result = {'has_plan': False, 'can_review': False}
    page.render_plan(page.review_result)
    page.warning_result = {'items': [{'id': 'warning-one', 'title': 'Due soon', 'reason': 'Synthetic deadline'}], 'total': 1}
    page.render_attention()
    page.render_tasks({'date': page.date_iso(), 'plan': None, 'items': [
        {'id': 'candidate-one', 'title': 'Short candidate', 'owner_label': 'Synthetic project',
         'reason': 'Due today', 'data': {}}], 'total': 1, 'next_offset': None})
    try:
        page.show(); app.processEvents()
        actions = page.plan_box.findChildren(QPushButton)
        assert len(actions) == 2
        assert len({button.y() for button in actions}) == 1
        assert all(button.width() <= button.sizeHint().width() + 2 for button in actions)
        assert all(button.width() < page.plan_box.width() / 2 for button in actions)
        assert page.plan_box.objectName() != 'PlanCard'
        assert not page.tasks_box.findChildren(QFrame, 'TaskRow')
        assert not page.attention_box.findChildren(QFrame, 'CurrentWarningCard')

        warning_row = page.attention_box.findChild(QFrame, 'TimelineRow')
        warning_actions = warning_row.findChild(ActionRow)
        heading = warning_actions.layout().itemAt(0).widget()
        view = warning_actions.layout().itemAt(1).widget()
        assert 0 <= view.x() - heading.geometry().right() <= warning_actions.layout().spacing() + 2
        assert view.width() <= view.sizeHint().width() + 2
        view.click(); assert opened == ['warning-one']

        task_row = page.tasks_box.findChild(QFrame, 'TimelineRow')
        task_actions = task_row.findChild(ActionRow)
        content = task_row.layout().itemAt(0).widget()
        task_view = task_actions.layout().itemAt(0).widget()
        add = task_actions.layout().itemAt(1).widget()
        assert 0 <= task_actions.y() - content.geometry().bottom() <= task_row.layout().spacing() + 2
        assert 0 <= add.x() - task_view.geometry().right() <= task_actions.layout().spacing() + 2
        assert add.width() <= add.sizeHint().width() + 2
        task_view.click(); assert opened[-1] == 'candidate-one'

        controls = [widget for widget in page._date_controls + page._plan_controls if widget.isVisible()]
        natural_width = page.compact_controls.layout().sizeHint().width()
        narrow_width = max(page.minimumSizeHint().width(), natural_width - max(32, controls[-1].sizeHint().width() // 2))
        assert narrow_width < natural_width
        page.resize(narrow_width, 760)
        wrapped = lambda: max(widget.y() for widget in controls) - min(widget.y() for widget in controls) >= min(widget.height() for widget in controls)
        settle_today_layout(app, wrapped)
        assert page.compact_controls.width() < natural_width
        assert max(widget.y() for widget in controls) - min(widget.y() for widget in controls) >= min(widget.height() for widget in controls)
        assert all(widget.geometry().right() < page.compact_controls.width() for widget in controls)
        assert all(button.width() <= max(button.sizeHint().width(), button.minimumWidth()) + 2
                   for button in controls if isinstance(button, QPushButton))
        title = content.layout().itemAt(0).widget()
        title.setText('较长的事项说明仍须保留完整内容并能在窄窗口换行。' * 8)
        settle_today_layout(app, lambda: title.height() >= title.heightForWidth(title.width()) - 2)
        assert title.wordWrap() and title.height() >= title.heightForWidth(title.width()) - 2
        assert content.geometry().right() < task_row.width()

        # Removing attention items must leave the empty plan at the viewport's
        # top, even when more vertical space is available. No hidden section or
        # flow row may turn the first half of the page into a blank spacer.
        page.warning_result = {'items': [], 'total': 0}
        page.today_result = {'notifications': [], 'unknowns': []}
        page.render_attention()
        page.render_tasks({'items': [], 'total': 0, 'next_offset': None})
        page.render_events([])
        page.resize(page.width(), 1200)
        page.scroll.verticalScrollBar().setValue(0)
        heading = page.plan_layout.itemAt(0).widget()
        top_limit = page.body.contentsMargins().top() + page.plan_layout.contentsMargins().top() + page.body.spacing()
        settled_top = lambda: heading.mapTo(page.scroll.widget(), QPoint()).y() <= top_limit
        settle_today_layout(app, settled_top)
        assert heading.mapTo(page.scroll.widget(), QPoint()).y() <= top_limit
        natural_height = max(page.plan_layout.sizeHint().height(), page.plan_layout.totalHeightForWidth(page.plan_box.width()))
        assert page.plan_box.height() <= natural_height + 6
        assert bridge.commands == []
    finally:
        page.close(); page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete); app.processEvents()
        apply_appearance(app, old_appearance)


def test_today_legacy_profile_request_preserves_current_controls_pending_state_and_input(app):
    from PySide6.QtWidgets import QFrame, QLineEdit
    from management.gui_theme import apply_appearance, current_appearance
    from management.gui_visual_profile import visual_style, set_visual_style
    old_appearance = current_appearance()
    bridge = ControlledBridge(); page = TodayPage(bridge)
    draft = QLineEdit('Unsaved synthetic draft', page)
    controls = page.date, page.manual_button, page.plan_button, page.habits_button
    day = page.date_iso()
    page.set_assistants_visible(True)
    page.review_result = {'has_plan': False, 'can_review': False}
    page.render_plan(page.review_result)
    page.warning_result = {'items': [{'id': 'warning-one', 'title': 'Warning', 'reason': 'Evidence'}], 'total': 1}
    page.render_attention()
    page.render_tasks({'date': day, 'plan': None, 'items': [
        {'id': 'candidate-one', 'title': 'Candidate', 'owner_label': 'Project', 'data': {}}], 'total': 1})
    page.pending_targets.add((day, 'candidate-one'))
    try:
        for theme, font_size in (('light', 13), ('dark', 20), ('light', 13)):
            set_visual_style('classic'); page.apply_visual_style('classic')
            apply_appearance(app, {**old_appearance, 'theme': theme, 'font_size': font_size})
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete); app.processEvents()
            assert visual_style() == 'glass'
            assert (page.date, page.manual_button, page.plan_button, page.habits_button) == controls
            assert page.date_iso() == day and draft.text() == 'Unsaved synthetic draft'
            assert page.pending_targets == {(day, 'candidate-one')} and page.assistants_visible
            assert not page.tasks_box.findChildren(QFrame, 'TaskRow')
            assert page.plan_box.objectName() == 'TodayPlanEmpty'
            assert not hasattr(page, 'classic_controls')
            assert not page.compact_controls.isHidden()
        assert bridge.commands == [] and bridge.queries == []
    finally:
        page.close(); page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete); app.processEvents()
        apply_appearance(app, old_appearance)
