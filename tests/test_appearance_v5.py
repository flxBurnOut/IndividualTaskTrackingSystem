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
    assert saved=={'theme':'dark','font_size':17,'font_family':'Portable Missing Family','workspace_sidebar_collapsed':True,'show_assistants':False}
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


def test_font_picker_lists_local_families_without_free_text(settings):
    dialog,bridge=settings
    assert isinstance(dialog.font_picker,QComboBox) and not dialog.font_picker.isEditable()
    assert set(available_font_families())<={dialog.font_picker.itemData(i) for i in range(dialog.font_picker.count())}
    assert dialog.font_size.minimum()==11 and dialog.font_size.maximum()==20


def test_external_settings_refresh_does_not_overwrite_unsaved_appearance(settings):
    dialog,bridge=settings
    dialog.font_size.setValue(18)
    dialog.load()
    bridge.take('settings')['callback']({'settings':{'appearance':{'font_size':12,'theme':'dark'}},'epoch':'synthetic','revision':11})
    assert dialog.font_size.value()==18 and dialog.theme_picker.currentData()=='light'
    assert dialog.chart_revision==10 and dialog.display_dirty
    dialog.save_chart_style();saved=bridge.commands[-1]
    assert saved['options']['expected_revision']==10
    saved['error']({'code':'revision_conflict','message':'Synthetic concurrent change'})
    assert dialog.font_size.value()==18 and dialog.display_dirty


def test_display_save_applies_immediately_and_does_not_send_sidebar(settings,app):
    dialog,bridge=settings
    dialog.theme_picker.setCurrentIndex(dialog.theme_picker.findData('dark'))
    dialog.font_size.setValue(17)
    dialog.save_chart_style();sent=bridge.commands[-1]
    appearance=sent['payload']['settings']['appearance']
    assert 'workspace_sidebar_collapsed' not in appearance
    sent['callback']({'epoch':'synthetic','revision':11,'result':{'settings':{'appearance':normalize_appearance(appearance)}}})
    assert current_appearance()['theme']=='dark' and app.font().pixelSize()==17
    assert not dialog.display_dirty


def test_save_completion_preserves_newer_unsaved_edits(settings):
    dialog,bridge=settings
    dialog.font_size.setValue(15);dialog.save_chart_style();sent=bridge.commands[-1]
    dialog.font_size.setValue(19)
    sent['callback']({'epoch':'synthetic','revision':11,'result':{'settings':{'appearance':normalize_appearance({'font_size':15})}}})
    assert dialog.font_size.value()==19 and dialog.display_dirty
    assert current_appearance()['font_size']==15


def test_missing_portable_font_is_retained_and_explained(settings):
    dialog,bridge=settings
    dialog.set_display_preferences({'settings':{'appearance':{'font_family':'Synthetic unavailable font'}},'epoch':'synthetic','revision':10})
    assert dialog.font_picker.currentData()=='Synthetic unavailable font'
    assert '未安装' in dialog.appearance_note.text()
    dialog.save_chart_style()
    assert bridge.commands[-1]['payload']['settings']['appearance']['font_family']=='Synthetic unavailable font'


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
