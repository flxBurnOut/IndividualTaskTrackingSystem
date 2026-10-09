import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtWidgets import QApplication,QFrame,QPushButton,QLabel,QWidget,QStackedWidget
from PySide6.QtCore import QCoreApplication,QEvent,QObject
from management.core import Core
from management.gui_task_list import TaskListPanel
from management.gui_workspace import WorkspacePage,TaskDetailDialog
from management.schemas import TYPES
from test_ux_workflows_v2 import QueuedCoreBridge,ControlledBridge,wait
from test_plan_assistance_v8 import cmd,task


@pytest.fixture(scope='session')
def app():return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('assistants_visible', [True, False])
def test_workspace_refresh_never_shows_controls_as_top_level_windows(app, tmp_path, assistants_visible):
    """Observe Show events: an end-of-render snapshot misses the native flash."""
    core = Core(tmp_path)
    project = cmd(core, 'create', {'type': 'project', 'title': 'Synthetic project'})['entity']
    course = cmd(core, 'create', {'type': 'course', 'title': 'Synthetic course'})['entity']
    note = cmd(core, 'create', {'type': 'note', 'title': 'Synthetic note',
                              'parent_id': project['id'], 'data': {'content': 'Unsaved by browsing'}})['entity']
    before = core.query('state')
    bridge = QueuedCoreBridge(core)
    host = QStackedWidget()
    blank = QWidget()
    page = WorkspacePage(bridge, on_create=lambda *_: None, on_edit=lambda *_: None,
                         on_codex=lambda *_args, **_kwargs: None)
    page.set_types(TYPES)
    page.set_assistants_visible(assistants_visible)
    host.addWidget(blank); host.addWidget(page)
    host.resize(1100, 800); host.show()
    app.processEvents()

    class WindowShows(QObject):
        def __init__(self):
            super().__init__()
            self.unexpected = []
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Show and isinstance(watched, QWidget) and watched.isWindow():
                self.unexpected.append({
                    'class': type(watched).__name__,
                    'text': watched.text() if isinstance(watched, QPushButton) else watched.windowTitle(),
                    'in_top_levels': watched in QApplication.topLevelWidgets(),
                })
            return False

    observer = WindowShows()
    app.installEventFilter(observer)
    detail = None
    try:
        # Replay sidebar visits and each asynchronous subtree replacement.
        for entity in (project, course, project, course):
            host.setCurrentWidget(page)
            page.open_object(entity)
            wait(app, lambda: bridge.pending == 0)
            assert page.assistant_buttons
            assert all(button.isVisible() == assistants_visible for button in page.assistant_buttons)
            page.refresh()
            wait(app, lambda: bridge.pending == 0)
            page.set_assistants_visible(not assistants_visible)
            page.set_assistants_visible(assistants_visible)
            host.setCurrentWidget(blank)
        # A note dialog may be constructed while its host is visible. Its child
        # buttons must not become windows before the intentional dialog.open().
        detail = TaskDetailDialog(bridge, note, host, on_codex=lambda *_: None,
                                  assistants_visible=assistants_visible)
        assert detail.assistant_button.isVisibleTo(detail) == assistants_visible
        assert not detail.isVisible()
        assert not observer.unexpected, observer.unexpected
        assert not bridge.commands
        after = core.query('state')
        assert (after['epoch'], after['revision']) == (before['epoch'], before['revision'])
    finally:
        app.removeEventFilter(observer)
        if detail:
            detail.close(); detail.deleteLater()
        host.close(); host.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_completed_rows_are_lazy_collapsed_and_fully_pageable(app,tmp_path):
    core=Core(tmp_path);owner=cmd(core,'create',{'type':'course','title':'Synthetic'})['entity']
    task(core,'Still open',parent_id=owner['id'])
    for i in range(65):task(core,f'Done {i}',parent_id=owner['id'],status='done')
    state=core.query('state');bridge=QueuedCoreBridge(core);panel=TaskListPanel(bridge,owner);panel.show()
    try:
        wait(app,lambda:bridge.pending==0)
        assert panel.toggle.text()=='已完成（65） · 展开'
        assert not panel.done_body.isVisible() and not panel.findChildren(QFrame,'CompletedTaskRow')
        assert len(panel.ids['open'])==1
        panel.toggle.click();wait(app,lambda:bridge.pending==0)
        assert len(panel.findChildren(QFrame,'CompletedTaskRow'))==30
        panel.done_more.click();wait(app,lambda:bridge.pending==0)
        panel.done_more.click();wait(app,lambda:bridge.pending==0)
        assert len(panel.ids['done'])==65 and not panel.done_more.isVisible()
        panel.toggle.click();assert not panel.done_body.isVisible()
        panel.toggle.click();assert len(panel.ids['done'])==65
        assert core.query('state')['revision']==state['revision']
    finally:panel.close();panel.deleteLater();app.processEvents()


def test_status_update_moves_row_into_fold_and_reopen_restores_it(app,tmp_path):
    core=Core(tmp_path);owner=cmd(core,'create',{'type':'course','title':'Synthetic'})['entity'];target=task(core,parent_id=owner['id'])
    bridge=QueuedCoreBridge(core);page=WorkspacePage(bridge,on_create=lambda *_:None,on_edit=lambda *_:None);page.set_types(TYPES);page.show();page.open_object(owner)
    try:
        wait(app,lambda:bridge.pending==0)
        assert target['id'] in page.task_panel.ids['open']
        cmd(core,'update',{'id':target['id'],'version':1,'patch':{'status':'done'}});page.refresh();wait(app,lambda:bridge.pending==0)
        assert not page.task_panel.ids['open'] and page.task_panel.done_count==1
        assert not page.task_panel.done_body.isVisible()
        cmd(core,'update',{'id':target['id'],'version':2,'patch':{'status':'active'}});page.refresh();wait(app,lambda:bridge.pending==0)
        assert target['id'] in page.task_panel.ids['open'] and page.task_panel.done_count==0
    finally:page.close();page.deleteLater();app.processEvents()


def test_late_page_response_does_not_touch_destroyed_panel(app):
    bridge=ControlledBridge();panel=TaskListPanel(bridge,{'id':'owner'})
    query=bridge.take('workspace_tasks');panel.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    assert panel.dead
    query['callback']({'items':[],'counts':{'done':0},'next_offset':None})


@pytest.mark.parametrize('font_size', [13, 20])
def test_long_task_titles_wrap_without_hiding_status_or_open_action(app, font_size):
    from PySide6.QtWidgets import QLabel
    from PySide6.QtTest import QTest
    from management.gui_theme import apply_appearance, current_appearance
    previous = current_appearance()
    apply_appearance(app, {'theme': 'dark', 'font_size': font_size})
    bridge = ControlledBridge(); opened = []
    owner = {'id': 'owner', 'title': '【演示】较长的课程与项目信息需要完整显示'}
    panel = TaskListPanel(bridge, owner, on_open=lambda item: opened.append(item['id']), on_edit=lambda *_: None)
    item = {'id': 'long-task', 'version': 1, 'status': 'active',
            'title': '【演示】根据提纲绘制展示卡片并逐项核对文字说明、日期以及尚未确认的内容',
            'data': {'due_date': '2030-01-09'}}
    try:
        panel.show()
        bridge.take('workspace_tasks')['callback']({'items': [item], 'counts': {'done': 0}, 'next_offset': None})
        for width in (900, 390, 700):
            panel.resize(width, 600); QTest.qWait(80)
            assert panel.width() == width
            labels = [label for label in panel.findChildren(QLabel) if label.isVisible() and label.text()]
            assert any(label.text() == item['title'] for label in labels)
            for label in labels:
                if label.wordWrap():
                    assert label.height() >= label.heightForWidth(label.width())
                assert label.parentWidget().rect().contains(label.geometry())
        button = next(button for button in panel.findChildren(QPushButton) if button.accessibleName() == '查看任务：' + item['title'])
        button.click()
        assert opened == [item['id']] and bridge.commands == []
    finally:
        panel.close(); panel.deleteLater(); app.processEvents()
        apply_appearance(app, previous)


def test_completed_recovery_does_not_remain_above_the_fold(app,tmp_path):
    from test_catchup_v5 import register,report
    from management.gui_recovery import RecoveryPanel
    core=Core(tmp_path);course=cmd(core,'create',{'type':'course','title':'Recovery course'})['entity']
    completed=register(core,course,title='Finished catchup')['entity'];report(core,completed,8,completion_confirmed=True)
    opened=register(core,course,title='Still catching up')['entity']
    bridge=QueuedCoreBridge(core);panel=RecoveryPanel(bridge,course);panel.show()
    try:
        wait(app,lambda:bridge.pending==0)
        wait(app, lambda: any(label.text() == opened['title'] and label.isVisible()
                              for label in panel.findChildren(QLabel)))
        titles=[label.text() for label in panel.findChildren(QLabel) if label.isVisible()]
        assert opened['title'] in titles and completed['title'] not in titles
        assert any(button.text() == '查看任务' and button.isVisible() for button in panel.findChildren(QPushButton))
        assert '已完成的 1 项' in panel.summary.text()
    finally:panel.close();panel.deleteLater();app.processEvents()
