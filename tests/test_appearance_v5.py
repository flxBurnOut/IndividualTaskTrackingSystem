"""Appearance settings and real native-widget behavior over synthetic data only."""
import copy
import os
import time
import uuid
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')

import pytest
from PySide6.QtCore import QDate
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QComboBox, QDateEdit, QDialog, QLineEdit, QVBoxLayout

from management.appearance import DEFAULT_APPEARANCE, normalize_appearance
from management.core import Core
from management.gui import MainWindow
from management.gui_calendar import install_calendar
from management.gui_charts import CoverageBar
from management.gui_theme import apply_appearance, available_font_families, color, current_appearance
from management.gui_workflows import SettingsDialog
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
    instance=QApplication.instance() or QApplication([])
    instance.setStyle('Fusion')
    return instance


@pytest.fixture(autouse=True)
def reset_theme(app):
    apply_appearance(app,DEFAULT_APPEARANCE)
    yield
    apply_appearance(app,DEFAULT_APPEARANCE)
    app.processEvents()


def wait(app,predicate,timeout=5):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        app.processEvents()
        if predicate():return
        time.sleep(.005)
    raise AssertionError('Native view did not settle')


def cmd(core,name,p):
    state=core.query('state')
    return core.command(name,p,request_id=str(uuid.uuid4()),epoch=state['epoch'],expected_revision=state['revision'])


@pytest.mark.parametrize('bad',[{'theme':'invented'},{'theme':[]},{'font_size':True},{'font_size':10},{'font_size':21},{'font_family':'font\nscript'},{'workspace_sidebar_collapsed':'false'},{'show_assistants':'false'},{'unknown':1}])
def test_invalid_preferences_rejected(bad):
    with pytest.raises(BusinessError):normalize_appearance(bad)


def test_partial_settings_persist_and_keep_unrelated_fields(tmp_path):
    core=Core(tmp_path/'synthetic')
    cmd(core,'settings',{'settings':{'appearance':{'theme':'dark','font_size':17,'font_family':'Portable Missing Family'}}})
    cmd(core,'settings',{'settings':{'appearance':{'workspace_sidebar_collapsed':True}}})
    saved=Core(core.root).query('settings')['settings']['appearance']
    assert saved=={'theme':'dark','accent':'default','font_size':17,'font_family':'Portable Missing Family','workspace_sidebar_collapsed':True,'show_assistants':False}
    assert core.query('settings')['settings']['ai']['enabled'] is False


def test_legacy_assistant_visibility_survives_unrelated_settings(tmp_path):
    from management.appearance import assistants_visible
    core = Core(tmp_path)
    with core.store.connect() as connection:
        settings = core.store.meta(connection, 'settings')
        settings.pop('appearance', None)
        settings['ai']['enabled'] = True
        core.store.set_meta(connection, 'settings', settings)
        connection.commit()
    assert assistants_visible(core.query('settings')['settings'])
    cmd(core, 'settings', {'settings': {'timezone':'Asia/Shanghai'}})
    assert assistants_visible(core.query('settings')['settings'])
    cmd(core, 'settings', {'settings': {'appearance': {'show_assistants':False}}})
    assert not assistants_visible(core.query('settings')['settings'])
    assert core.query('settings')['settings']['ai']['enabled'] is True


def test_open_fields_theme_and_font_update_without_losing_edits(app):
    dialogs=[]
    try:
        for i in range(2):
            dialog=QDialog();layout=QVBoxLayout(dialog);field=QLineEdit('Unsaved synthetic draft');layout.addWidget(field);dialog.show();dialogs.append((dialog,field))
        selected=(available_font_families() or [app.font().family()])[0]
        apply_appearance(app,{'theme':'dark','font_family':selected,'font_size':20})
        app.processEvents()
        for dialog,field in dialogs:
            assert field.text()=='Unsaved synthetic draft'
            assert field.font().pixelSize()==20
            assert field.palette().color(QPalette.ColorRole.Text).name()==color('text')
        assert app.palette().color(QPalette.ColorRole.Window).name()==color('window')
    finally:
        for dialog,field in dialogs:dialog.close();dialog.deleteLater()


def test_calendar_and_chart_repaint_and_preserve_semantics(app):
    editor=QDateEdit(QDate(2030,2,3));calendar=install_calendar(editor)
    bar=CoverageBar();bar.resize(200,22);bar.set_summary({'total':1,'done':1});bar.show()
    try:
        light=bar.grab().toImage().pixelColor(50,10).name()
        selected=calendar.selectedDate();minimum=calendar.minimumDate()
        apply_appearance(app,{'theme':'dark','font_size':20})
        app.processEvents()
        dark=bar.grab().toImage().pixelColor(50,10).name()
        assert dark==color('done') and dark!=light
        assert calendar.headerTextFormat().background().color().name()==color('calendar_header')
        assert calendar.selectedDate()==selected and calendar.minimumDate()==minimum
        assert calendar.minimumWidth()>336
    finally:
        editor.close();editor.deleteLater();bar.close();bar.deleteLater()


class Bridge:
    epoch='synthetic';revision=10
    def __init__(self):self.queries=[];self.commands=[]
    def query(self,name,callback=None,error=None,**params):self.queries.append({'name':name,'callback':callback,'error':error,'params':params,'delivered':False})
    def command(self,name,payload,callback=None,error=None,**options):self.commands.append({'name':name,'payload':copy.deepcopy(payload),'callback':callback,'error':error,'options':options})
    def take(self,name):
        q=next(q for q in self.queries if q['name']==name and not q['delivered']);q['delivered']=True;return q


@pytest.fixture
def settings(app,tmp_path):
    bridge=Bridge();dialog=SettingsDialog(bridge,{'types':[]},tmp_path/'synthetic')
    bridge.take('settings')['callback']({'settings':{'appearance':dict(DEFAULT_APPEARANCE)},'epoch':'synthetic','revision':10})
    yield dialog,bridge
    dialog.close();dialog.deleteLater();app.processEvents()


def display_read(bridge, settings=None, *, revision=10, epoch='synthetic'):
    bridge.take('settings')['callback']({'settings': settings or {'appearance': dict(DEFAULT_APPEARANCE)},
                                       'epoch': epoch, 'revision': revision})


def test_font_picker_lists_local_families_without_free_text(settings):
    dialog,bridge=settings
    assert isinstance(dialog.font_picker,QComboBox) and not dialog.font_picker.isEditable()
    assert set(available_font_families())<={dialog.font_picker.itemData(i) for i in range(dialog.font_picker.count())}
    assert dialog.font_size.minimum()==11 and dialog.font_size.maximum()==20


def test_accent_preview_is_local_and_save_merges_unrelated_display_changes(settings):
    dialog,bridge=settings
    before=current_appearance()
    dialog.accent_picker.buttons[dialog.accent_picker.findData('violet')].click()
    assert dialog.display_dirty and not bridge.commands
    assert current_appearance()==before
    assert '紫' in dialog.appearance_preview.caption.text()
    assert [dialog.theme_picker.itemText(index) for index in range(2)]==['浅色','深色']
    dialog.save_chart_style()
    display_read(bridge,{'appearance':{'theme':'dark','font_size':18}},revision=12)
    sent=bridge.commands[-1]
    assert sent['payload']=={'settings':{'appearance':{'accent':'violet'}}}
    # A new choice during the save remains an unsaved choice afterwards.
    dialog.accent_picker.setCurrentIndex(dialog.accent_picker.findData('teal'))
    sent['callback']({'epoch':'synthetic','revision':13,'result':{}})
    assert current_appearance()['accent']=='violet'
    assert current_appearance()['theme']=='dark'
    assert dialog.accent_picker.currentData()=='teal' and dialog.display_dirty
    assert dialog.font_size.value()==18


def test_accent_conflict_and_uncertain_result_preserve_selection(settings):
    dialog,bridge=settings
    dialog.accent_picker.setCurrentIndex(dialog.accent_picker.findData('blue'))
    dialog.save_chart_style()
    display_read(bridge,{'appearance':{'accent':'rose'}},revision=12)
    assert not bridge.commands
    assert '强调色' in dialog.message.text() and dialog.accent_picker.currentData()=='blue'
    dialog.save_chart_style();display_read(bridge,revision=13)
    sent=bridge.commands[-1]
    sent['error']({'code':'connection_lost','message':'Synthetic lost receipt'})
    assert not dialog.accent_picker.isEnabled()
    dialog.save_chart_style()
    assert bridge.commands[-1]['options']==sent['options']
    assert bridge.commands[-1]['payload']==sent['payload']


@pytest.mark.parametrize('theme,font_size',[('light',13),('dark',20)])
def test_settings_sections_have_compact_controls_and_accessible_fixed_actions(settings,app,tmp_path,theme,font_size):
    from pathlib import Path
    from PySide6.QtWidgets import QPushButton,QScrollArea,QFrame
    from management.gui_settings_controls import SettingsGroup
    dialog,bridge=settings
    apply_appearance(app,{'theme':theme,'font_size':font_size,'accent':'violet'})
    dialog.resize(940,800);dialog.show();app.processEvents()
    report=Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR',str(tmp_path)))
    for index,label in ((0,'display'),(2,'codex'),(3,'data')):
        dialog.tabs.setCurrentIndex(index);app.processEvents()
        page=dialog.tabs.widget(index)
        assert page.horizontalScrollBar().maximum()==0
        groups=page.findChildren(SettingsGroup)
        assert len(groups)>=3
        for group in groups:
            for button in group.findChildren(QPushButton):
                if button.isVisible() and button is not dialog.appearance_preview.example:
                    assert button.width()<=max(button.minimumWidth(),button.sizeHint().width())+2,button.text()
        if index==0:
            assert dialog.theme_picker.width()<page.viewport().width()/2
            assert not dialog.chart_save.visibleRegion().isEmpty()
            assert dialog.appearance_preview.width()<page.viewport().width()*.85
        dialog.repaint()
        assert dialog.grab().save(str(report/f'settings-{label}-{theme}.png'))
    assert len(dialog.timetable_settings.findChildren(QScrollArea))==1
    # A narrow large-font window must scroll vertically, never crop fields.
    dialog.resize(460,650)
    dialog.tabs.setCurrentIndex(0);app.processEvents()
    page=dialog.tabs.currentWidget()
    assert page.horizontalScrollBar().maximum()==0
    assert not dialog.chart_save.visibleRegion().isEmpty()
    page.ensureWidgetVisible(dialog.accent_picker);app.processEvents()
    buttons=dialog.accent_picker.buttons
    assert all(button.geometry().right()<dialog.accent_picker.row.width() for button in buttons)
    assert len({button.y() for button in buttons})>1
    assert not bridge.commands


def test_external_settings_refresh_does_not_overwrite_unsaved_appearance(settings):
    dialog,bridge=settings
    dialog.font_size.setValue(18)
    dialog.load()
    bridge.take('settings')['callback']({'settings':{'appearance':{'font_size':12,'theme':'dark'}},'epoch':'synthetic','revision':11})
    assert dialog.font_size.value()==18 and dialog.theme_picker.currentData()=='light'
    assert dialog.chart_revision==10 and dialog.display_dirty
    dialog.save_chart_style()
    display_read(bridge, {'appearance': {'font_size': 12, 'theme': 'dark'}}, revision=11)
    assert not bridge.commands
    assert '字号' in dialog.message.text() and '未覆盖' in dialog.message.text()
    assert dialog.font_size.value()==18 and dialog.display_dirty


def test_display_save_applies_immediately_and_does_not_send_sidebar(settings,app):
    dialog,bridge=settings
    dialog.theme_picker.setCurrentIndex(dialog.theme_picker.findData('dark'))
    dialog.font_size.setValue(17)
    dialog.save_chart_style();display_read(bridge);sent=bridge.commands[-1]
    appearance=sent['payload']['settings']['appearance']
    assert 'workspace_sidebar_collapsed' not in appearance
    sent['callback']({'epoch':'synthetic','revision':11,'result':{'settings':{'appearance':normalize_appearance(appearance)}}})
    assert current_appearance()['theme']=='dark' and app.font().pixelSize()==17
    assert not dialog.display_dirty


def test_save_completion_preserves_newer_unsaved_edits(settings):
    dialog,bridge=settings
    dialog.font_size.setValue(15);dialog.save_chart_style();display_read(bridge);sent=bridge.commands[-1]
    dialog.font_size.setValue(19)
    sent['callback']({'epoch':'synthetic','revision':11,'result':{'settings':{'appearance':normalize_appearance({'font_size':15})}}})
    assert dialog.font_size.value()==19 and dialog.display_dirty
    assert current_appearance()['font_size']==15


def test_missing_portable_font_is_retained_and_explained(settings):
    dialog,bridge=settings
    dialog.set_display_preferences({'settings':{'appearance':{'font_family':'Synthetic unavailable font'}},'epoch':'synthetic','revision':10})
    assert dialog.font_picker.currentData()=='Synthetic unavailable font'
    assert '未安装' in dialog.appearance_note.text()
    dialog.font_size.setValue(16)
    dialog.save_chart_style()
    display_read(bridge, {'appearance': {'font_family': 'Synthetic unavailable font'}})
    assert bridge.commands[-1]['payload']['settings']['appearance'] == {'font_size': 16}
    bridge.commands[-1]['callback']({'epoch': 'synthetic', 'revision': 11, 'result': {}})
    assert dialog.font_picker.currentData() == 'Synthetic unavailable font'


class LocalClient:
    def __init__(self,core):self.core=core
    def query(self,name,**params):return self.core.query(name,**params)
    def command(self,name,payload,**options):return self.core.command(name,payload,**options)


def test_workspace_collapse_persists_and_external_theme_refreshes(app,tmp_path):
    core=Core(tmp_path/'synthetic-main')
    window=MainWindow(core.root,client_factory=lambda _:LocalClient(core));window.show()
    try:
        wait(app,lambda:bool(window.type_map) and not window.bridge.callbacks)
        window.navigate('projects');wait(app,lambda:not window.bridge.callbacks)
        window.workspace_page.toggle_sidebar(True)
        wait(app,lambda:not window.bridge.callbacks and not window._sidebar_pending)
        assert core.query('settings')['settings']['appearance']['workspace_sidebar_collapsed']
        assert window.workspace_page.expand_directory_button.isVisible()
        cmd(core,'settings',{'settings':{'appearance':{'theme':'dark','font_size':16}}})
        window.load_display_preferences();wait(app,lambda:not window.bridge.callbacks)
        assert current_appearance()['theme']=='dark'
        assert window.workspace_page.sidebar_collapsed
        # Reopen a new window over the same persisted synthetic settings.
        second=MainWindow(core.root,client_factory=lambda _:LocalClient(core));second.show()
        try:
            wait(app,lambda:bool(second.type_map) and not second.bridge.callbacks)
            assert second.workspace_page.sidebar_collapsed
            assert current_appearance()['font_size']==16
            second.workspace_page.toggle_sidebar(False)
            wait(app,lambda:not second.bridge.callbacks and not second._sidebar_pending)
            assert not core.query('settings')['settings']['appearance']['workspace_sidebar_collapsed']
        finally:
            second.poll.stop();wait(app,lambda:not second.bridge.callbacks);second.close()
    finally:
        window.poll.stop();wait(app,lambda:not window.bridge.callbacks);window.close()


def test_theme_registration_does_not_own_or_cycle_widgets(app):
    import weakref
    from PySide6.QtWidgets import QWidget
    from management.gui_theme import bind_theme
    class Probe(QWidget):
        def refresh_theme(self):pass
    widget=Probe()
    bind_theme(widget,widget.refresh_theme)
    reference=weakref.ref(widget)
    del widget
    # This fails with the old global-signal -> closure -> widget ownership chain.
    assert reference() is None
    apply_appearance(app,{'theme':'dark'})


def test_theme_close_then_worker_collection_keeps_qt_destruction_on_gui(app):
    import gc
    from concurrent.futures import ThreadPoolExecutor
    from PySide6.QtCore import QCoreApplication,QEvent
    from management.gui_assistant import MessageText
    for index in range(12):
        parent=QDialog();layout=QVBoxLayout(parent)
        date=QDateEdit(QDate.currentDate());layout.addWidget(date);install_calendar(date)
        message=MessageText('Synthetic chat text');layout.addWidget(message)
        apply_appearance(app,{'theme':'dark' if index%2 else 'light','font_size':11+index%10})
        parent.close();parent.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        del parent,layout,date,message
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(gc.collect).result(timeout=5)
    apply_appearance(app,DEFAULT_APPEARANCE)


def test_each_chart_choice_previews_without_write_and_is_saved_separately(settings):
    dialog, bridge = settings
    dialog.chart_pickers['review_daily_style'].setCurrentIndex(1)
    dialog.chart_pickers['weekly_style'].setCurrentIndex(2)
    assert not bridge.commands
    assert dialog.weekly_preview.layout_style == 'tiles'
    assert '虚构示例' in dialog.chart_preview_note.text()
    dialog.save_chart_style()
    display_read(bridge)
    sent = bridge.commands[-1]
    assert sent['payload']['settings'] == {'charts': {'weekly_style': 'tiles', 'review_daily_style': 'ring'}}
    sent['error']({'message': 'Synthetic conflict'})
    assert dialog.weekly_chart_style.currentData() == 'tiles'
    assert dialog.display_dirty


def test_chart_entry_keeps_its_preview_when_async_preferences_arrive(settings):
    dialog, bridge = settings
    dialog.focus_chart('dashboard_today_style')
    dialog.set_display_preferences({'settings': {'charts': {'weekly_style': 'tiles', 'dashboard_today_style': 'ring'}}, 'epoch': 'synthetic', 'revision': 11})
    assert dialog._chart_preview_key == 'dashboard_today_style'
    assert dialog.chart_preview.chart_style == 'ring'
    assert '今日计划' in dialog.chart_preview_note.text()


def test_codex_visibility_save_does_not_enable_ai_or_conflict_with_own_display_save(settings):
    dialog, bridge = settings
    dialog.show_assistants.setChecked(True)
    dialog.save_assistant_visibility()
    sent = bridge.commands[-1]
    assert sent['name'] == 'settings'
    assert sent['payload'] == {'settings': {'appearance': {'show_assistants': True}}}
    sent['callback']({'epoch': 'synthetic', 'revision': 11, 'result': {}})
    dialog.weekly_chart_style.setCurrentIndex(2)
    dialog.save_chart_style()
    display_read(bridge, {'appearance': {'show_assistants': True}}, revision=11)
    assert bridge.commands[-1]['options']['expected_revision'] == 11
    assert not dialog.ai_enabled.isChecked()


def test_visibility_save_retains_external_display_conflict(settings):
    dialog, bridge = settings
    dialog.font_size.setValue(19)
    dialog.show_assistants.setChecked(True)
    dialog.save_assistant_visibility()
    bridge.commands[-1]['callback']({'epoch': 'synthetic', 'revision': 13, 'result': {}})
    dialog.save_chart_style()
    display_read(bridge, {'appearance': {'font_size': 17, 'show_assistants': True}}, revision=13)
    assert len(bridge.commands) == 1
    assert '字号' in dialog.message.text()
    assert dialog.font_size.value() == 19


def test_chart_baseline_survives_unrelated_settings_refresh(settings):
    dialog, bridge = settings
    dialog.weekly_chart_style.setCurrentIndex(2)
    dialog.load()
    display_read(bridge, {'charts': {'weekly_style': 'rows'}}, revision=11)
    assert dialog.weekly_chart_style.currentData() == 'tiles'
    dialog.save_chart_style()
    display_read(bridge, {'charts': {'weekly_style': 'rows'}}, revision=11)
    assert not bridge.commands
    assert '每天的情况' in dialog.message.text()
    assert dialog.weekly_chart_style.currentData() == 'tiles' and dialog.display_dirty


def test_display_merge_preserves_other_chart_locations_and_refreshes_untouched_controls(settings):
    dialog, bridge = settings
    dialog.weekly_chart_style.setCurrentIndex(2)
    dialog.save_chart_style()
    display_read(bridge, {'charts': {'review_daily_style': 'ring'},
                          'appearance': {'theme': 'dark', 'show_assistants': True}}, revision=14)
    sent = bridge.commands[-1]
    assert sent['payload'] == {'settings': {'charts': {'weekly_style': 'tiles'}}}
    assert sent['options']['expected_revision'] == 14
    sent['callback']({'epoch': 'synthetic', 'revision': 15, 'result': {}})
    assert dialog.theme_picker.currentData() == 'dark'
    assert dialog.chart_pickers['review_daily_style'].currentData() == 'ring'
    assert not dialog.display_dirty
    dialog.font_size.setValue(17)
    dialog.save_chart_style()
    display_read(bridge, {'charts': {'review_daily_style': 'ring', 'weekly_style': 'tiles'},
                          'appearance': {'theme': 'dark', 'show_assistants': True}}, revision=15)
    assert bridge.commands[-1]['payload'] == {'settings': {'appearance': {'font_size': 17}}}


def test_display_save_after_unrelated_business_write_uses_real_core_fence(app, tmp_path):
    core = Core(tmp_path / 'display-concurrency')
    bridge = Bridge()
    dialog = SettingsDialog(bridge, core.query('capabilities'), core.root)
    try:
        bridge.take('settings')['callback'](core.query('settings'))
        dialog.weekly_chart_style.setCurrentIndex(2)
        task = cmd(core, 'create', {'type': 'task', 'title': 'Synthetic unrelated task'})['result']['entity']
        cmd(core, 'settings', {'settings': {'appearance': {'workspace_sidebar_collapsed': True}}})
        before = core.query('state')
        dialog.save_chart_style()
        bridge.take('settings')['callback'](core.query('settings'))
        sent = bridge.commands[-1]
        assert sent['options']['expected_revision'] == before['revision']
        receipt = core.command(sent['name'], sent['payload'], **sent['options'])
        sent['callback'](receipt)
        actual = Core(core.root).query('settings')['settings']
        assert actual['charts']['weekly_style'] == 'tiles'
        assert actual['appearance']['workspace_sidebar_collapsed']
        assert core.query('get', id=task['id'])['entity'] == task
    finally:
        dialog.close(); dialog.deleteLater(); app.processEvents()


def test_display_save_keeps_core_race_rejection_then_checks_again(settings):
    dialog, bridge = settings
    dialog.font_size.setValue(16)
    dialog.save_chart_style(); display_read(bridge, revision=11)
    sent = bridge.commands[-1]
    assert sent['options']['expected_revision'] == 11
    sent['error']({'code': 'revision_conflict', 'message': 'Synthetic post-read write'})
    assert dialog.font_size.value() == 16 and dialog.display_dirty
    dialog.save_chart_style(); display_read(bridge, revision=12)
    retry = bridge.commands[-1]
    assert retry['options']['expected_revision'] == 12
    assert retry['options']['request_id'] != sent['options']['request_id']


def test_display_space_change_during_preflight_never_writes(settings):
    dialog, bridge = settings
    dialog.font_size.setValue(16)
    dialog.save_chart_style(); display_read(bridge, epoch='different-space')
    assert not bridge.commands
    assert '数据空间已经切换' in dialog.message.text()
    assert dialog.font_size.value() == 16 and dialog.display_dirty


def test_uncertain_display_save_keeps_original_request_through_reconnect_errors(settings):
    dialog, bridge = settings
    dialog.font_size.setValue(15)
    dialog.save_chart_style(); display_read(bridge, revision=12)
    sent = bridge.commands[-1]
    sent['error']({'code': 'connection_lost', 'message': 'Synthetic lost response'})
    assert '结果待确认' in dialog.appearance_note.text()
    assert not dialog.font_size.isEnabled() and not dialog.display_reload.isEnabled()
    query_count = len(bridge.queries)
    dialog.reload_display_preferences()
    dialog.save_chart_style()
    retry = bridge.commands[-1]
    assert len(bridge.queries) == query_count
    assert retry['payload'] == sent['payload'] and retry['options'] == sent['options']
    retry['error']({'code': 'service_version_mismatch', 'message': 'Synthetic old service'})
    dialog.save_chart_style()
    assert bridge.commands[-1]['options'] == sent['options']
    bridge.commands[-1]['callback']({'epoch': 'synthetic', 'revision': 13, 'result': {}})
    assert dialog.font_size.isEnabled() and dialog.display_reload.isEnabled()
    assert not dialog.display_dirty and current_appearance()['font_size'] == 15


def test_display_no_changes_or_already_saved_choice_never_adds_write(settings):
    dialog, bridge = settings
    dialog.save_chart_style(); display_read(bridge)
    assert not bridge.commands and not dialog.display_dirty
    dialog.weekly_chart_style.setCurrentIndex(2)
    dialog.save_chart_style(); display_read(bridge, {'charts': {'weekly_style': 'tiles'}}, revision=11)
    assert not bridge.commands and not dialog.display_dirty


@pytest.mark.parametrize('theme,font_size', [('light', 13), ('dark', 20)])
def test_chart_settings_preview_and_sticky_save_fit_large_fonts(settings, app, tmp_path, theme, font_size):
    from pathlib import Path
    dialog, bridge = settings
    apply_appearance(app, {'theme': theme, 'font_size': font_size})
    dialog.show()
    dialog.focus_chart('weekly_style')
    dialog.weekly_chart_style.setCurrentIndex(2)
    app.processEvents()
    page = dialog.tabs.currentWidget()
    assert page.horizontalScrollBar().maximum() == 0
    assert not dialog.chart_save.visibleRegion().isEmpty()
    report = Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR', str(tmp_path)))
    assert dialog.grab().save(str(report / ('chart-settings-' + theme + '.png')))
    page.ensureWidgetVisible(dialog.weekly_preview)
    app.processEvents()
    assert not dialog.chart_save.visibleRegion().isEmpty()
    assert dialog.weekly_preview.grab().save(str(report / ('chart-preview-' + theme + '.png')))


def test_enabling_codex_explains_when_panel_entries_are_still_hidden(settings):
    dialog, bridge = settings
    dialog.ai_enabled.setChecked(True)
    dialog.save_ai()
    sent = bridge.commands[-1]
    assert sent['name'] == 'configure_codex'
    sent['callback']({'epoch': 'synthetic', 'revision': 11, 'result': {}})
    assert '助手入口仍隐藏' in dialog.message.text()
    assert not dialog.show_assistants.isChecked()
    assert not any(call['name'] in {'connect_codex', 'create_ai_job'} for call in bridge.commands)


def test_visual_profile_switch_keeps_open_drafts_business_state_and_native_window(app, tmp_path):
    from PySide6.QtCore import Qt
    from management.gui_visual_profile import visual_style, set_visual_style
    from management.gui_theme import stylesheet, _classic_stylesheet
    previous = visual_style()
    core = Core(tmp_path / 'visual-profile-preservation')
    window = MainWindow(core.root, client_factory=lambda _: LocalClient(core))
    dialog = None
    try:
        window.show()
        wait(app, lambda: bool(window.type_map) and not window.bridge.callbacks)
        window.onboarding.stop()
        window.navigate('tasks')
        window.tasks_page.quick_input.setText('A draft that must survive both designs')
        dialog = SettingsDialog(window.bridge, window.capabilities, core.root, window)
        dialog.show()
        wait(app, lambda: not window.bridge.callbacks)
        dialog.font_size.setValue(18)
        before = core.query('state')
        flags = window.windowFlags()
        page = window.pages.currentWidget()
        for style in ('classic', 'glass', 'classic', 'glass'):
            window.apply_visual_style(style)
            app.processEvents()
            assert window.tasks_page.quick_input.text() == 'A draft that must survive both designs'
            assert dialog.font_size.value() == 18 and dialog.display_dirty
            assert window.pages.currentWidget() is page
            assert window.windowFlags() == flags
            assert not (window.windowFlags() & Qt.WindowType.FramelessWindowHint)
            assert core.query('state') == before
            assert visual_style() == style
            if style == 'classic':
                assert stylesheet(current_appearance()) == _classic_stylesheet(current_appearance())
                assert window.nav_buttons['tasks'].icon().isNull()
            else:
                assert not window.nav_buttons['tasks'].icon().isNull()
        assert not (core.root / 'ui-visual.json').exists()
    finally:
        if dialog:
            dialog.close(); dialog.deleteLater()
        window.tasks_page.quick_input.clear()
        window.review_pending = False
        window.close()
        wait(app, lambda: not window.bridge.workers_running())
        window.deleteLater(); app.processEvents()
        set_visual_style(previous)
        apply_appearance(app, DEFAULT_APPEARANCE)


@pytest.mark.parametrize('theme,font_size', [('light', 13), ('dark', 20)])
def test_settings_all_sections_keep_text_height_at_actual_width(settings, app, theme, font_size):
    """A scroll bar is acceptable; squeezing wrapped text into a short row is not."""
    from PySide6.QtWidgets import QLabel, QPushButton, QScrollArea
    from management.gui_settings_controls import SelectableText
    from management.gui_visual_profile import set_visual_style, visual_style
    dialog, bridge = settings
    previous = visual_style()
    failures = []
    try:
        set_visual_style('glass')
        apply_appearance(app, {'theme': theme, 'font_size': font_size})
        dialog.show_codex_project({'status': 'ready', 'name': '演示项目',
                                  'workspace': 'C:\\Users\\Example\\' + 'long_profile_directory_' * 4}, enabled=True)
        dialog.show()
        for width in (900, 560):
            dialog.resize(width, 610)
            for tab in range(dialog.tabs.count()):
                dialog.tabs.setCurrentIndex(tab)
                for button in dialog.findChildren(QPushButton):
                    if button.property('disclosure') and button.isVisibleTo(dialog):
                        button.setChecked(True)
                for advanced in range(dialog.advanced_tabs.count()):
                    dialog.advanced_tabs.setCurrentIndex(advanced)
                    for _ in range(4):
                        app.processEvents()
                    context = (theme, font_size, width, dialog.tabs.tabText(tab), advanced)
                    for label in dialog.findChildren(QLabel):
                        if not label.isVisibleTo(dialog) or not label.text().strip():
                            continue
                        needed = label.heightForWidth(label.width()) if label.wordWrap() else label.sizeHint().height()
                        if label.height() + 1 < needed:
                            failures.append((context, label.text(), label.width(), label.height(), needed))
                    for field in dialog.findChildren(SelectableText):
                        if not field.isVisibleTo(dialog):continue
                        assert field.document().size().height() <= field.viewport().height() + 1, context
                        block=field.document().begin()
                        while block.isValid():
                            text_layout=block.layout()
                            for line in range(text_layout.lineCount()):
                                assert text_layout.lineAt(line).naturalTextWidth() <= field.viewport().width() + 1, (context,field.text())
                            block=block.next()
                        original=field.text();field.selectAll()
                        assert field.textCursor().selectedText().replace('\u2029','\n')==original
                    page = dialog.tabs.currentWidget()
                    for scroll in ([page] if isinstance(page, QScrollArea) else page.findChildren(QScrollArea)):
                        if scroll.horizontalScrollBar().maximum():
                            failures.append((context, 'horizontal-scroll', scroll.horizontalScrollBar().maximum(),
                                [(type(w).__name__, w.objectName(), w.minimumSizeHint().width(),
                                  w.text() if isinstance(w, QLabel) else '')
                                 for w in scroll.widget().findChildren(QLabel)
                                 if w.minimumSizeHint().width() > scroll.viewport().width()]))
        assert not failures, failures
        assert not bridge.commands
    finally:
        set_visual_style(previous)
