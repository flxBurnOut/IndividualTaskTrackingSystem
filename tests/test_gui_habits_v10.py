"""Discoverable habits, safe edits and real projections in the native UI."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import datetime as dt
import json
from pathlib import Path
import pytest
from PySide6.QtWidgets import QApplication,QWidget,QStyle,QStyleOptionButton
from PySide6.QtCore import QCoreApplication,QEvent,QPoint,QRect
from PySide6.QtGui import QFontDatabase
from management.core import Core
from management import habits
from management.gui_habits import HabitsPanel,HabitEditor
from management.gui_today import TodayPage
from management.gui_workflows import HabitsDialog, SettingsDialog
from test_ux_workflows_v2 import QueuedCoreBridge,ControlledBridge,wait
from test_plan_assistance_v8 import cmd
from test_recurring_v4 import event

@pytest.fixture(scope='session')
def app():
    application=QApplication.instance() or QApplication([])
    if application.platformName()=='offscreen' and os.name=='nt':
        for name in ('msyh.ttc','msyhbd.ttc'):
            path=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/name
            if path.is_file():QFontDatabase.addApplicationFont(str(path))
    return application


def test_independent_habits_shows_real_effects_without_writing(app,tmp_path,monkeypatch):
    core=Core(tmp_path);cmd(core,'create',{'type':'rule','title':'Only a preference','data':{'rule_kind':'behavior','policy':'Preserve sleep'}})
    before=core.query('state');bridge=QueuedCoreBridge(core);dialog=HabitsDialog(bridge,core.query('capabilities'));dialog.show()
    try:
        wait(app,lambda:bridge.pending==0)
        assert dialog.windowTitle()=='日常习惯'
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


def test_type_explanations_and_examples_are_read_only_until_explicit_save(app,tmp_path):
    core=Core(tmp_path);bridge=QueuedCoreBridge(core);panel=HabitsPanel(bridge,QWidget());panel.show()
    editor=None
    try:
        wait(app,lambda:bridge.pending==0);before=core.query('state')
        kinds={'behavior','capacity','protected_time','warning','temporary'}
        assert {panel.new_kind.itemData(index) for index in range(panel.new_kind.count())}==kinds
        explanations=[];examples=[]
        for index in range(panel.new_kind.count()):
            panel.new_kind.setCurrentIndex(index)
            description=panel.new_kind_help.text()
            assert description.strip() and panel.new_kind.accessibleDescription()==description
            explanations.append(description)
            panel.new_rule_button.click();editor=panel.dialogs[-1]
            examples.append(editor.title.placeholderText())
            assert editor.title.text()=='' and editor.title.placeholderText().strip()
            assert editor.first.text()==editor.last.text()==''
            if hasattr(editor,'policy'):
                assert editor.policy.toPlainText()=='' and editor.policy.placeholderText().strip()
            editor.save();wait(app,lambda:bridge.pending==0)
            assert editor.note.text() and not editor.saving
            assert not bridge.commands and core.query('state')==before
            editor.dirty=False;editor.close();editor.deleteLater();editor=None
            app.processEvents()
        assert len(set(explanations))==len(kinds)
        assert len(set(examples))==len(kinds)
    finally:
        if editor:editor.dirty=False;editor.close();editor.deleteLater()
        panel.close();panel.deleteLater();app.processEvents()


@pytest.mark.parametrize('kind', ['behavior', 'capacity', 'protected_time', 'warning', 'temporary'])
def test_manual_rule_type_creates_correct_structured_fields_without_assistant(app, tmp_path, kind):
    core=Core(tmp_path);bridge=QueuedCoreBridge(core);panel=HabitsPanel(bridge,QWidget());panel.show()
    editor=None
    try:
        wait(app,lambda:bridge.pending==0)
        panel.set_assistants_visible(False)
        assert panel.codex_preparation.isHidden() and panel.discuss_button.isHidden()
        assert panel.new_rule_button.isVisible() and panel.prep_toggle.isVisible()
        panel.new_kind.setCurrentIndex(panel.new_kind.findData(kind))
        panel.new_rule_button.click();editor=panel.dialogs[0]
        assert editor.kind==kind
        title_example=editor.title.placeholderText()
        policy_example=editor.policy.placeholderText() if hasattr(editor,'policy') else None
        assert editor.title.text()=='' and editor.first.text()==editor.last.text()==''
        if hasattr(editor,'policy'):
            assert editor.policy.toPlainText()==''
            # A visible example cannot satisfy the required actual policy.
            editor.title.setText('Manual '+kind);editor.save()
            assert not bridge.commands and core.query('list',type='rule')['total']==0
        editor.title.setText('Manual '+kind)
        if kind in {'behavior','temporary'}:editor.policy.setPlainText('Retain recovery time.')
        elif kind=='capacity':editor.fields['minutes'].setValue(40)
        elif kind=='protected_time':
            editor.fields['start'].setText('23:00');editor.fields['end'].setText('07:00')
        else:editor.fields['days_before'].setValue(3)
        editor.save();editor.save();wait(app,lambda:bridge.pending==0)
        result=core.query('list',type='rule')
        assert result['total']==1
        data=result['items'][0]['data']
        assert data['rule_kind']==kind
        assert result['items'][0]['title']=='Manual '+kind and result['items'][0]['title']!=title_example
        assert data['effective_from'] is None and data['effective_until'] is None
        assert not policy_example or data.get('policy')!=policy_example
        if kind=='capacity':assert core.query('plan_context',date='2030-01-02')['capacity_minutes']==40
        elif kind=='protected_time':
            spans=core.query('plan_context',date='2030-01-02')['protected_times']
            assert [(p['start_minute'],p['end_minute']) for p in spans]==[(0,420),(1380,1440)]
        elif kind=='warning':assert data['days_before']==3
        else:assert data['policy']=='Retain recovery time.'
        assert core.query('list',type='task')['total']==0
        assert all(name not in {'create_ai_job','send_message'} for name,*_ in bridge.commands)
    finally:
        if editor:
            editor.dirty=False;editor.close();editor.deleteLater()
        panel.close();panel.deleteLater();app.processEvents()


def test_new_capacity_requires_known_minutes_before_enabling_and_preserves_input(app):
    bridge=ControlledBridge();editor=HabitEditor(bridge,kind='capacity');editor.show()
    try:
        editor.title.setText('Still unknown');editor.save()
        assert bridge.commands==[] and '尚未明确' in editor.note.text()
        assert editor.title.text()=='Still unknown'
        editor.enabled.setChecked(False);editor.save()
        call=bridge.commands[-1]
        assert call['payload']['data']['minutes'] is None
        assert call['payload']['data']['enabled'] is False
        assert not editor.title.isEnabled()
        call['error']({'message':'Synthetic failure'})
        assert editor.title.isEnabled() and editor.title.text()=='Still unknown'
    finally:editor.saving=editor.dirty=False;editor.close();editor.deleteLater();app.processEvents()


def test_habits_is_no_longer_a_settings_tab(app, tmp_path):
    core = Core(tmp_path)
    bridge = QueuedCoreBridge(core)
    dialog = SettingsDialog(bridge, core.query('capabilities'), tmp_path)
    try:
        wait(app, lambda: bridge.pending == 0)
        assert '日常习惯' not in [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    finally:
        dialog.close(); dialog.deleteLater(); app.processEvents()


def test_independent_reminder_editors_follow_large_font(app, tmp_path):
    from management.gui_theme import apply_appearance
    from management.appearance import DEFAULT_APPEARANCE
    bridge = QueuedCoreBridge(Core(tmp_path))
    dialog = HabitsDialog(bridge, {})
    try:
        dialog.show()
        wait(app, lambda: bridge.pending == 0)
        dialog.habits.show_reminders()
        apply_appearance(app, {'theme': 'dark', 'font_size': 20})
        app.processEvents()
        for field in (dialog.daily_time, dialog.weekly_day, dialog.review_timezone):
            assert field.minimumHeight() >= field.fontMetrics().height() + 18
        assert dialog.habits.horizontalScrollBar().maximum() == 0
    finally:
        dialog.close(); dialog.deleteLater()
        apply_appearance(app, DEFAULT_APPEARANCE)
        app.processEvents()


@pytest.mark.parametrize('theme,font_size',[('light',13),('dark',20)])
def test_five_habit_editors_scroll_without_moving_or_covering_save_controls(app,tmp_path,theme,font_size):
    from management.gui_theme import apply_appearance,current_appearance
    previous=current_appearance();measurements=[];editor=None
    folder=Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR',str(tmp_path)));folder.mkdir(parents=True,exist_ok=True)
    def within(widget,parent):
        return QRect(widget.mapTo(parent,QPoint(0,0)),widget.size())
    try:
        apply_appearance(app,{'theme':theme,'font_size':font_size})
        for kind in ('behavior','capacity','protected_time','warning','temporary'):
            bridge=ControlledBridge();editor=HabitEditor(bridge,kind=kind);editor.resize(560,440);editor.show();app.processEvents()
            scroll=editor.editor_scroll
            assert scroll.horizontalScrollBar().maximum()==0,(kind,theme)
            save_before=within(editor.save_button,editor);cancel_before=within(editor.cancel,editor)
            for button in (editor.save_button,editor.cancel):
                assert editor.rect().contains(within(button,editor))
                assert not within(scroll,editor).intersects(within(button,editor))
                assert not button.visibleRegion().isEmpty()
                option=QStyleOptionButton();button.initStyleOption(option)
                content=button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents,option,button)
                text=button.fontMetrics().boundingRect(button.text())
                assert content.width()>=text.width() and content.height()>=text.height(),(kind,theme,content,text)
            scrollbar=scroll.verticalScrollBar();scrollbar.setValue(scrollbar.maximum());app.processEvents()
            assert within(editor.save_button,editor)==save_before and within(editor.cancel,editor)==cancel_before
            assert scroll.viewport().rect().contains(within(editor.last,scroll.viewport())),(kind,theme)
            assert not bridge.commands and not editor.dirty
            screenshot=folder/f'habit-editor-{kind}-{theme}.png'
            assert editor.grab().save(str(screenshot))
            measurements.append({'kind':kind,'theme':theme,'font_size':font_size,
                'window':[editor.width(),editor.height()],'save_bounds':list(save_before.getRect()),
                'vertical_scroll_range':scrollbar.maximum(),'horizontal_scroll_range':scroll.horizontalScrollBar().maximum(),
                'screenshot':str(screenshot),'business_commands':len(bridge.commands)})
            editor.close();editor.deleteLater();editor=None;app.processEvents()
        if font_size==20:assert all(row['vertical_scroll_range']>0 for row in measurements)
    finally:
        if editor:editor.dirty=False;editor.close();editor.deleteLater()
        (folder/f'habit-editor-geometry-{theme}.json').write_text(json.dumps(measurements,ensure_ascii=False,indent=2),encoding='utf-8')
        apply_appearance(app,previous);app.processEvents()


@pytest.mark.parametrize('theme,font_size',[('light',13),('dark',20)])
def test_habits_groups_reflow_and_compact_actions_preserve_unsaved_reminders(app,tmp_path,monkeypatch,theme,font_size):
    from PySide6.QtCore import QTime
    from management.gui_theme import apply_appearance,current_appearance
    from management.gui_visual_profile import set_visual_style,visual_style
    previous=current_appearance();profile=visual_style();dialog=None
    folder=Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR',str(tmp_path)));folder.mkdir(parents=True,exist_ok=True)
    core=Core(tmp_path);bridge=QueuedCoreBridge(core)
    try:
        set_visual_style('glass');apply_appearance(app,{'theme':theme,'font_size':font_size})
        dialog=HabitsDialog(bridge,core.query('capabilities'));dialog.resize(1440,820);dialog.show()
        wait(app,lambda:bridge.pending==0);app.processEvents()
        panel=dialog.habits;before=core.query('state')
        def unexpected_query(*args,**kwargs):raise AssertionError('A visual change must not reload saved preferences')
        monkeypatch.setattr(bridge,'query',unexpected_query)
        assert panel.reminder_box.objectName()==panel.prep_box.objectName()=='ContentSection'
        assert panel.section_grid.getItemPosition(panel.section_grid.indexOf(panel.prep_box))[:2]==(0,1)
        for button in (panel.reminder_toggle,panel.prep_toggle,panel.new_rule_button,panel.rules_toggle):
            assert button.width()<=button.sizeHint().width()+2
            assert button.width()<panel.viewport().width()/2
        assert panel.horizontalScrollBar().maximum()==0
        assert dialog.grab().save(str(folder/f'habits-compact-{theme}.png'))
        panel.new_kind.setCurrentIndex(panel.new_kind.findData('temporary'))
        panel.rules_toggle.setChecked(True)
        selected_kind=panel.new_kind.currentData()
        dialog.resize(560,610);app.processEvents();app.processEvents()
        assert panel.section_grid.getItemPosition(panel.section_grid.indexOf(panel.prep_box))[:2]==(1,0)
        assert panel.horizontalScrollBar().maximum()==0
        panel.show_reminders();dialog.daily_enabled.setChecked(True)
        dialog.daily_time.setTime(QTime(22,17));dialog.review_timezone.setText('Asia/Singapore')
        identity=(id(dialog.daily_time),id(panel.new_kind),id(panel.rules))
        for selected in ('classic','glass'):
            set_visual_style(selected);apply_appearance(app,{'theme':theme,'font_size':font_size});app.processEvents();app.processEvents()
            assert (id(dialog.daily_time),id(panel.new_kind),id(panel.rules))==identity
            assert dialog.daily_time.time()==QTime(22,17) and dialog.review_timezone.text()=='Asia/Singapore'
            assert panel.new_kind.currentData()==selected_kind and panel.rules_toggle.isChecked()
            assert panel.reminder.isVisible() and panel.prep_box.isHidden() and panel.rules_box.isHidden()
            assert panel.horizontalScrollBar().maximum()==0
        for field in (dialog.daily_time,dialog.weekly_time,dialog.weekly_day):
            assert field.width()<=max(round(120*max(1,dialog.fontMetrics().height()/18)),field.sizeHint().width())
        assert dialog.save_reminders.width()<=dialog.save_reminders.sizeHint().width()+2
        panel.verticalScrollBar().setValue(panel.verticalScrollBar().maximum());app.processEvents()
        assert dialog.grab().save(str(folder/f'habits-reminders-{theme}.png'))
        assert not bridge.commands and core.query('state')==before
    finally:
        if dialog:dialog.close();dialog.deleteLater()
        set_visual_style(profile);apply_appearance(app,previous);app.processEvents()


def test_habit_editor_narrow_fields_and_theme_change_keep_draft(app):
    from management.gui_theme import apply_appearance,current_appearance
    from management.gui_visual_profile import set_visual_style,visual_style
    previous=current_appearance();profile=visual_style();editor=None
    bridge=ControlledBridge()
    try:
        set_visual_style('glass');apply_appearance(app,{'theme':'light','font_size':13})
        editor=HabitEditor(bridge,kind='capacity');editor.resize(850,650);editor.show();app.processEvents()
        minutes=editor.fields['minutes'];editor.title.setText('Draft limit');minutes.setValue(180);editor.first.setText('2030-01-')
        identity=(id(minutes),id(editor.title),id(editor.first))
        assert minutes.width()<editor.width()/2 and editor.first.width()<editor.width()/2
        for selected,theme,font_size in [('classic','dark',20),('glass','dark',20),('glass','light',13)]:
            set_visual_style(selected);apply_appearance(app,{'theme':theme,'font_size':font_size});app.processEvents()
            assert (id(editor.fields['minutes']),id(editor.title),id(editor.first))==identity
            assert minutes.value()==180 and editor.title.text()=='Draft limit' and editor.first.text()=='2030-01-'
            assert editor.dirty and not bridge.queries and not bridge.commands
            assert editor.editor_scroll.horizontalScrollBar().maximum()==0
    finally:
        if editor:editor.dirty=False;editor.close();editor.deleteLater()
        set_visual_style(profile);apply_appearance(app,previous);app.processEvents()


@pytest.mark.parametrize('theme,font_size',[('light',13),('dark',20)])
def test_habits_all_sections_and_editor_explanations_are_not_vertically_clipped(app,tmp_path,theme,font_size):
    from PySide6.QtWidgets import QLabel
    from management.gui_habits import HABIT_CHOICES
    from management.gui_theme import apply_appearance,current_appearance
    from management.gui_visual_profile import set_visual_style,visual_style
    previous=current_appearance();profile=visual_style();dialog=editor=None;failures=[]
    core=Core(tmp_path);bridge=QueuedCoreBridge(core)
    def check_labels(host,context):
        for _ in range(4):app.processEvents()
        for label in host.findChildren(QLabel):
            if label.isVisibleTo(host) and label.text().strip():
                needed=label.heightForWidth(label.width()) if label.wordWrap() else label.sizeHint().height()
                if label.height()+1<needed:failures.append((context,label.text(),label.width(),label.height(),needed))
    try:
        set_visual_style('glass');apply_appearance(app,{'theme':theme,'font_size':font_size})
        dialog=HabitsDialog(bridge,core.query('capabilities'));dialog.show();wait(app,lambda:bridge.pending==0)
        for width in (940,560):
            dialog.resize(width,610)
            dialog.habits.prep_toggle.setChecked(True);dialog.habits.rules_toggle.setChecked(True)
            check_labels(dialog,('habits',width))
            dialog.habits.reminder_toggle.setChecked(True)
            check_labels(dialog,('reminders',width))
            dialog.habits.reminder_toggle.setChecked(False)
        for kind in HABIT_CHOICES:
            editor=HabitEditor(bridge,kind=kind);editor.resize(560,610);editor.show()
            check_labels(editor,kind)
            editor.close();editor.deleteLater();editor=None
        assert not failures,failures
        assert not bridge.commands
    finally:
        if editor:editor.dirty=False;editor.close();editor.deleteLater()
        if dialog:dialog.close();dialog.deleteLater()
        set_visual_style(profile);apply_appearance(app,previous);app.processEvents()
