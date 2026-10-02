"""Manual course records, paged relationships and source-backed creation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton
from management.core import Core
from management.gui_forms import EntityForm
from management.gui_workspace import WorkspacePage, TaskDependenciesDialog
from management.schemas import TYPES
from test_ux_workflows_v2 import ControlledBridge, QueuedCoreBridge, cmd, wait


@pytest.fixture(scope='session')
def app():
    return QApplication.instance() or QApplication([])


def create(core, kind, title, **fields):
    return cmd(core, 'create', {'type': kind, 'title': title, **fields})['result']['entity']


def dispose(app, widget):
    widget.close(); widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete); app.processEvents()


def test_course_assessment_event_and_note_pages_have_independent_complete_coverage(tmp_path):
    core = Core(tmp_path); owner = create(core, 'course', 'Synthetic course')
    assessments = {create(core, 'assessment', f'Assessment {i:03}', parent_id=owner['id'])['id'] for i in range(103)}
    events = {create(core, 'event', f'Event {i}', data={'owner_id': owner['id']})['id'] for i in range(4)}
    notes = {create(core, 'note', f'Note {i}', parent_id=owner['id'])['id'] for i in range(4)}
    found = set(); offset = 0
    while offset is not None:
        result = core.query('object_workspace', id=owner['id'], assessments_offset=offset, course_limit=17, limit=2)
        info = result['course_info']; found.update(item['id'] for item in info['assessments'])
        assert {item['id'] for item in info['events']} == events
        assert result['children_total'] == len(notes)
        assert {item['type'] for item in result['children']} == {'note'}
        offset = info['assessments_next_offset']
    assert found == assessments
    assert info['assessments_offset'] > 100 and not info['complete']


def test_dependency_pages_preserve_both_directions_without_truncation(tmp_path):
    core = Core(tmp_path); task = create(core, 'task', 'Main task')
    ids = set()
    for i in range(5):
        other = create(core, 'task', f'Linked task {i}')
        source, target = (task, other) if i % 2 else (other, task)
        ids.add(cmd(core, 'link', {'source_id': source['id'], 'target_id': target['id'], 'kind': 'depends_on'})['result']['id'])
    found = set(); offset = 0
    while offset is not None:
        result = core.query('object_workspace', id=task['id'], dependencies_offset=offset, dependencies_limit=2)['dependencies']
        found.update(link['id'] for link in result['items'])
        assert result['total'] == 5
        offset = result['next_offset']
    assert found == ids


def test_manual_dependency_editor_rejects_cycles_and_does_not_change_completion(app, tmp_path):
    core = Core(tmp_path); before = create(core, 'task', 'Read'); after = create(core, 'task', 'Practice')
    bridge = QueuedCoreBridge(core); dialog = TaskDependenciesDialog(bridge, after)
    try:
        wait(app, lambda: bridge.pending == 0)
        dialog.add(before, 'before'); wait(app, lambda: bridge.pending == 0)
        links = core.query('get', id=after['id'])['links']
        assert len(links) == 1 and links[0]['source_id'] == after['id'] and links[0]['target_id'] == before['id']
        dialog.add(before, 'after'); wait(app, lambda: bridge.pending == 0)
        assert '循环' in dialog.status.text()
        assert len(core.query('get', id=after['id'])['links']) == 1
        assert core.query('list', type='feedback')['total'] == 0
        dialog.remove(links[0]); wait(app, lambda: bridge.pending == 0)
        assert core.query('get', id=after['id'])['links'] == []
    finally: dispose(app, dialog)


def test_workspace_keeps_manual_actions_when_assistants_hidden(app, tmp_path):
    core = Core(tmp_path); course = create(core, 'course', 'Course')
    bridge = QueuedCoreBridge(core); creations = []
    page = WorkspacePage(bridge, on_create=lambda kind, owner: creations.append((kind, owner['id'])), on_codex=lambda *_args, **_kw: None)
    try:
        page.set_types(TYPES); page.set_assistants_visible(False); page.open_object(course)
        wait(app, lambda: bridge.pending == 0); page.show()
        for title, kind in [('＋ 新建笔记', 'note'), ('＋ 添加评分项目', 'assessment'), ('＋ 添加日程', 'event')]:
            button = next(button for button in page.findChildren(QPushButton) if button.text() == title)
            assert not button.isHidden(); button.click()
            assert creations[-1] == (kind, course['id'])
        assert page.assistant_buttons and all(button.isHidden() for button in page.assistant_buttons)
        page.set_assistants_visible(True)
        assert all(not button.isHidden() for button in page.assistant_buttons)
    finally:
        wait(app, lambda: bridge.pending == 0); dispose(app, page)


def test_workspace_paging_keeps_other_sections_position(app):
    bridge = ControlledBridge(); page = WorkspacePage(bridge)
    page.current_entity = {'id': 'course', 'type': 'course', 'title': 'Course'}
    try:
        page.files_offset = 50
        page.content_page('assessments', 50)
        request = bridge.queries[-1]['params']
        assert request['files_offset'] == 50 and request['assessments_offset'] == 50 and request['events_offset'] == 0
        page.content_page('children', 50)
        assert bridge.queries[-1]['params']['assessments_offset'] == 50
        page.content_page('assessments', None)
        assert bridge.queries[-1]['params']['offset'] == 50
        assert bridge.queries[-1]['params']['assessments_offset'] == 0
    finally: dispose(app, page)


@pytest.mark.parametrize('kind', ['task', 'event'])
def test_source_creation_saves_exact_source_and_correct_owner(app, tmp_path, kind):
    core = Core(tmp_path); owner = create(core, 'course', 'Course')
    source = cmd(core, 'add_source', {'kind': 'notice', 'title': 'Assignment notice', 'text': 'Synthetic source body', 'owner_id': owner['id']})['result']['entity']
    bridge = QueuedCoreBridge(core); page = WorkspacePage(bridge); page.type_map = TYPES; page.current_entity = owner
    try:
        page.create_from_source(source, kind)
        dialog = next(item for item in page.dialogs if isinstance(item, EntityForm))
        wait(app, lambda: bridge.pending == 0)
        assert 'source_text' in dialog.primary_field_names
        dialog.title_edit.setText('Manually checked item')
        dialog.save(); dialog.save()
        wait(app, lambda: bridge.pending == 0)
        created = core.query('list', type=kind)['items']
        assert len(created) == 1
        assert source['id'] in created[0]['data']['source_text']
        assert source['title'] in created[0]['data']['source_text']
        if kind == 'task': assert created[0]['parent_id'] == owner['id']
        else:
            assert created[0]['parent_id'] is None
            assert created[0]['data']['owner_id'] == owner['id']
            assert not created[0]['data'].get('date')
        assert core.query('list', type='feedback')['total'] == 0
    finally:
        wait(app, lambda: bridge.pending == 0); dispose(app, page)


def test_closed_dependency_editor_ignores_late_query(app):
    bridge = ControlledBridge(); dialog = TaskDependenciesDialog(bridge, {'id': 'task', 'type': 'task', 'title': 'Task'})
    request = bridge.queries[-1]
    dispose(app, dialog)
    request['callback']({'dependencies': {'items': [], 'total': 0}})
    assert bridge.commands == []
