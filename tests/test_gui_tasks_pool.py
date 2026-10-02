"""Manual capture and global task discovery through the real business service."""
import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication
from management.core import Core
from management.gui_tasks import GROUPS, TasksPage
from test_ux_workflows_v2 import ControlledBridge, QueuedCoreBridge, cmd, wait


@pytest.fixture(scope='session')
def app():
    return QApplication.instance() or QApplication([])


def create(core, title, **fields):
    return cmd(core, 'create', {'type': 'task', 'title': title, **fields})['result']['entity']


def dispose(app, widget):
    widget.close(); widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete); app.processEvents()


def snapshot(core):
    return core.query('state'), core.query('list', limit=100)


def test_undated_unowned_tasks_are_discoverable_across_pages_without_writes(tmp_path):
    core = Core(tmp_path)
    expected = {create(core, f'Unscheduled practice {index:02}')['id'] for index in range(37)}
    owner = cmd(core, 'create', {'type': 'project', 'title': 'Project'})['result']['entity']
    assigned = create(core, 'Assigned practice', parent_id=owner['id'])
    dated = create(core, 'Later appointment', data={'due_date': '2099-01-01'})
    before = snapshot(core)
    for group in ('undated', 'unowned'):
        found, offset = [], 0
        while offset is not None:
            page = core.query('task_pool', group=group, search='Unscheduled practice', limit=7, offset=offset)
            assert page['total'] == 37 and len(page['items']) <= 7
            found.extend(item['id'] for item in page['items'])
            offset = page['next_offset']
        assert set(found) == expected and len(found) == len(set(found))
    undated = {item['id'] for item in core.query('task_pool', group='undated', limit=100)['items']}
    unowned = {item['id'] for item in core.query('task_pool', group='unowned', limit=100)['items']}
    assert assigned['id'] in undated and assigned['id'] not in unowned
    assert dated['id'] not in undated and dated['id'] in unowned
    assert snapshot(core) == before


def test_search_treats_percent_and_underscore_as_literal_characters(tmp_path):
    core = Core(tmp_path)
    literal = create(core, 'Read 50%_notes')
    create(core, 'Read 50 other notes')
    result = core.query('task_pool', search='50%_')
    assert result['total'] == 1 and result['items'][0]['id'] == literal['id']


def test_group_names_and_keys_query_the_expected_service_group(app, tmp_path):
    core = Core(tmp_path)
    create(core, 'No date')
    create(core, 'Old deadline', data={'due_date': '2000-01-01'})
    create(core, 'Future deadline', data={'due_date': '2099-01-01'})
    create(core, 'Explicit done', status='done')
    bridge = QueuedCoreBridge(core); page = TasksPage(bridge); failures = []
    page.error.connect(failures.append)
    try:
        assert [(page.group_picker.itemData(i), page.group_picker.itemText(i)) for i in range(page.group_picker.count())] == GROUPS
        for index, (key, label) in enumerate(GROUPS):
            page.group_picker.setCurrentIndex(index); page.refresh()
            wait(app, lambda: bridge.pending == 0)
            assert page.result['group'] == key
            assert page.status.text().startswith(label + ' ·')
        assert failures == [] and bridge.commands == []
    finally: dispose(app, page)


def test_quick_capture_continues_after_each_saved_task_without_dates(app, tmp_path):
    core = Core(tmp_path); bridge = QueuedCoreBridge(core); page = TasksPage(bridge)
    try:
        for title in ('First thought', 'Second thought'):
            page.quick_input.setText(title)
            page.quick_input.returnPressed.emit()
            page.save_quick()
            wait(app, lambda: bridge.pending == 0)
            assert page.quick_input.text() == ''
            assert not page.quick_pending and page.quick_button.isEnabled() and not page.quick_input.isReadOnly()
        records = core.query('list', type='task')['items']
        assert {item['title'] for item in records} == {'First thought', 'Second thought'}
        assert len(bridge.commands) == 2
        assert all(not item['parent_id'] and not item['data'].get('due_date') and not item['data'].get('scheduled_date') for item in records)
        assert core.query('list', type='plan')['total'] == 0
    finally:
        wait(app, lambda: bridge.pending == 0); dispose(app, page)


def test_quick_capture_late_success_does_not_clear_newer_input(app):
    bridge = ControlledBridge(); page = TasksPage(bridge)
    try:
        page.quick_input.setText('First task'); page.save_quick(); page.save_quick()
        assert len(bridge.commands) == 1 and page.quick_pending
        # An input restore or another UI action can replace text while the save is pending.
        page.quick_input.setText('Next task already entered')
        bridge.commands[0]['callback']({'result': {'entity': {'id': 'first'}}, 'revision': 11})
        assert page.quick_input.text() == 'Next task already entered'
        assert not page.quick_pending and page.quick_button.isEnabled()
        page.save_quick()
        assert bridge.commands[-1]['payload']['title'] == 'Next task already entered'
    finally: dispose(app, page)


def test_quick_capture_failure_keeps_input_and_allows_explicit_retry(app):
    bridge = ControlledBridge(); page = TasksPage(bridge); errors = []
    page.error.connect(errors.append)
    try:
        page.quick_input.setText('Do not lose this'); page.save_quick()
        first = copy.deepcopy(bridge.commands[0]['payload'])
        bridge.commands[0]['error']({'code': 'conflict', 'message': 'Reload required'})
        assert page.quick_input.text() == 'Do not lose this'
        assert '输入已保留' in page.quick_note.text() and not page.quick_pending
        assert errors[-1]['code'] == 'conflict'
        page.save_quick()
        assert len(bridge.commands) == 2 and bridge.commands[-1]['payload'] == first
    finally: dispose(app, page)


def test_add_today_marks_existing_plan_and_never_duplicates(app, tmp_path):
    core = Core(tmp_path); target = create(core, 'Unscheduled task')
    bridge = QueuedCoreBridge(core); page = TasksPage(bridge)
    try:
        page.refresh(); wait(app, lambda: bridge.pending == 0)
        assert page.selected()['id'] == target['id'] and page.plan_button.isEnabled()
        page.plan_today(); page.plan_today(); wait(app, lambda: bridge.pending == 0)
        assert page.selected()['in_plan'] is True
        assert not page.plan_button.isEnabled() and page.plan_button.text() == '已在今天计划'
        assert '已在今天计划' in page.items.currentItem().text(2)
        page.plan_today(); wait(app, lambda: bridge.pending == 0)
        plans = core.query('list', type='plan')['items']
        assert len(plans) == 1
        assert [block['target_id'] for block in plans[0]['data']['blocks']] == [target['id']]
        assert len([name for name, _, _ in bridge.commands if name == 'add_to_plan']) == 1
        task = core.query('get', id=target['id'])['entity']
        assert not task['data'].get('scheduled_date')
        assert core.query('list', type='feedback')['total'] == 0
    finally:
        wait(app, lambda: bridge.pending == 0); dispose(app, page)


@pytest.mark.parametrize('completion_source', ['status', 'feedback'])
def test_completed_tasks_cannot_be_added_from_pool(app, tmp_path, completion_source):
    core = Core(tmp_path); task = create(core, 'Already completed', status='done' if completion_source == 'status' else 'active')
    if completion_source == 'feedback':
        day = core.query('task_pool')['date']
        cmd(core, 'record_feedback', {'target_id': task['id'], 'business_date': day, 'dimensions': {'completion': 'done'}, 'source_text': 'Explicit synthetic completion'})
    bridge = QueuedCoreBridge(core); page = TasksPage(bridge)
    try:
        page.group_picker.setCurrentIndex(page.group_picker.findData('done'))
        wait(app, lambda: bridge.pending == 0)
        assert page.selected()['completion_state'] == 'done'
        assert not page.plan_button.isEnabled()
        page.plan_today()
        assert bridge.commands == [] and core.query('list', type='plan')['total'] == 0
    finally: dispose(app, page)


def test_planned_prerequisite_remains_incomplete_until_actual_feedback(tmp_path):
    core = Core(tmp_path)
    before = create(core, 'Read notes'); after = create(core, 'Build project')
    cmd(core, 'link', {'source_id': after['id'], 'target_id': before['id'], 'kind': 'depends_on'})
    day = core.query('task_pool')['date']
    cmd(core, 'create_plan', {'date': day, 'mode': 'no_precise_time', 'blocks': [{'target_id': before['id']}, {'target_id': after['id']}]})
    state_before = core.query('state')
    result = core.query('task_pool', date=day, search='Build project')['items'][0]
    assert result['in_plan'] is True
    assert result['dependencies'] == [{'id': before['id'], 'title': 'Read notes', 'archived': False, 'completed': False}]
    assert core.query('state') == state_before
    cmd(core, 'record_feedback', {'target_id': before['id'], 'business_date': day, 'dimensions': {'completion': 'done'}, 'source_text': 'Explicit synthetic completion'})
    assert core.query('task_pool', date=day, search='Build project')['items'][0]['dependencies'][0]['completed'] is True


def test_pool_ignores_outdated_search_results_and_closed_page_callbacks(app):
    bridge = ControlledBridge(); page = TasksPage(bridge)
    page.refresh(); old = bridge.queries[-1]
    page.search_input.setText('New search'); page.filter_changed()
    old['callback']({'items': [{'id': 'unwanted'}]})
    assert page.items.topLevelItemCount() == 0
    pending = bridge.queries[-1]
    dispose(app, page)
    pending['callback']({'items': []})
    assert bridge.commands == []


def test_today_actions_refresh_business_date_after_midnight(app, tmp_path, monkeypatch):
    core = Core(tmp_path); create(core, 'Cross midnight task')
    monkeypatch.setattr(core, 'today', lambda _c: '2030-01-07')
    opened = []
    bridge = QueuedCoreBridge(core); page = TasksPage(bridge, on_plan=opened.append)
    try:
        page.refresh(); wait(app, lambda: bridge.pending == 0)
        assert page.result['date'] == '2030-01-07'
        monkeypatch.setattr(core, 'today', lambda _c: '2030-01-08')
        page.plan_today(); wait(app, lambda: bridge.pending == 0)
        assert core.query('list', type='plan')['items'][0]['data']['date'] == '2030-01-08'
        monkeypatch.setattr(core, 'today', lambda _c: '2030-01-09')
        page.edit_today(); wait(app, lambda: bridge.pending == 0)
        assert opened == ['2030-01-09']
    finally: dispose(app, page)


def test_previous_page_uses_actual_byte_limited_offsets(app):
    bridge = ControlledBridge(); page = TasksPage(bridge)
    row = {'id':'synthetic', 'title':'Large task', 'data':{}, 'owner_label':'未归属', 'in_plan':False, 'status':'active'}
    def deliver(next_offset):
        bridge.deliver('task_pool', {'date':'2030-01-07','group':'open','items':[row],'total':80,'next_offset':next_offset})
    try:
        page.refresh(); deliver(7)
        page.next.click(); assert bridge.queries[-1]['params']['offset'] == 7; deliver(13)
        page.next.click(); assert bridge.queries[-1]['params']['offset'] == 13; deliver(19)
        page.previous.click(); assert bridge.queries[-1]['params']['offset'] == 7; deliver(13)
        page.previous.click(); assert bridge.queries[-1]['params']['offset'] == 0; deliver(7)
    finally: dispose(app, page)
