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


@pytest.mark.parametrize('font_size', [13, 20])
def test_task_actions_wrap_and_long_completion_text_stays_visible(app, monkeypatch, font_size):
    from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton
    from management.gui_theme import apply_appearance, current_appearance
    from management.gui_visual_profile import set_visual_style, visual_style
    previous, profile = current_appearance(), visual_style()
    set_visual_style('glass'); apply_appearance(app, {**previous, 'font_size': font_size})
    bridge = ControlledBridge(); page = TasksPage(bridge)
    page.resize(1300, 850); page.show(); page.open_library(); app.processEvents()
    page.quick_input.setText('保留尚未保存的新任务')
    owner_title = '【演示】设计研究课程与综合实践项目，保留完整课程名称。' * 5
    class SelectedOwner:
        def __init__(self, *args, **kwargs):
            self.selected = {'id': 'owner-long', 'title': owner_title}
        def exec(self): return True
        def deleteLater(self): pass
    monkeypatch.setattr('management.gui_tasks.EntityPicker', SelectedOwner)
    try:
        page.choose_owner()
        bridge.deliver('task_pool', {'group': 'open', 'items': [], 'total': 0, 'date': '2030-01-02', 'next_offset': None})
        assert owner_title in page.status.text() and page.owner_button.toolTip() == owner_title
        for width in (1300, 660, 1300):
            page.resize(width, 850)
            page.completion.setText('完成标准：' + '逐条核对原始要求，保留检验结论与尚未确认的内容。' * 8)
            wait(app, lambda: page.completion.height() >= page.completion.heightForWidth(page.completion.width()) - 1)
            assert page.status.height() >= page.status.heightForWidth(page.status.width()) - 1
            assert page.width() <= width, 'button row must not force the page beyond the available width'
            for button in page.findChildren(QPushButton):
                if not button.isVisible(): continue
                option = QStyleOptionButton(); button.initStyleOption(option)
                content = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
                text = button.fontMetrics().boundingRect(button.text())
                assert content.width() >= text.width() and content.height() >= text.height(), (button.text(), content, text)
                assert button.parentWidget().rect().contains(button.geometry())
            assert page.quick_input.text() == '保留尚未保存的新任务' and bridge.commands == []
    finally:
        dispose(app, page); set_visual_style(profile); apply_appearance(app, previous)


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
    page.open_library()
    try:
        assert [(page.group_picker.itemData(i), page.group_picker.itemText(i)) for i in range(page.group_picker.count())] == GROUPS
        for index, (key, label) in enumerate(GROUPS):
            page.group_picker.setCurrentIndex(index); page.refresh()
            wait(app, lambda: bridge.pending == 0)
            assert page.result['group'] == key
            assert page.status.text().startswith('正在查看：' + label)
        assert failures == [] and bridge.commands == []
    finally: dispose(app, page)


def test_quick_capture_continues_after_each_saved_task_without_dates(app, tmp_path):
    core = Core(tmp_path); bridge = QueuedCoreBridge(core); opened = []
    page = TasksPage(bridge, on_task=opened.append)
    try:
        for title in ('First thought', 'Second thought'):
            page.quick_input.setText(title)
            page.quick_input.returnPressed.emit()
            page.save_quick()
            wait(app, lambda: bridge.pending == 0)
            assert page.quick_input.text() == ''
            assert not page.quick_pending and page.quick_button.isEnabled() and not page.quick_input.isReadOnly()
            assert not page.quick_open_button.isHidden()
            page.quick_open_button.click()
            assert core.query('get', id=opened[-1])['entity']['title'] == title
        records = core.query('list', type='task')['items']
        assert {item['title'] for item in records} == {'First thought', 'Second thought'}
        assert len(bridge.commands) == 2
        assert len(opened) == 2 and opened[0] != opened[1]
        assert all(not item['parent_id'] and not item['data'].get('due_date') and not item['data'].get('scheduled_date') for item in records)
        assert core.query('list', type='plan')['total'] == 0
    finally:
        wait(app, lambda: bridge.pending == 0); dispose(app, page)


def test_quick_capture_late_success_does_not_clear_newer_input(app):
    bridge = ControlledBridge(); opened = []; page = TasksPage(bridge, on_task=opened.append)
    try:
        page.quick_input.setText('First task'); page.save_quick(); page.save_quick()
        assert len(bridge.commands) == 1 and page.quick_pending
        # An input restore or another UI action can replace text while the save is pending.
        page.quick_input.setText('Next task already entered')
        bridge.commands[0]['callback']({'result': {'entity': {'id': 'first'}}, 'revision': 11})
        assert page.quick_input.text() == 'Next task already entered'
        assert not page.quick_pending and page.quick_button.isEnabled()
        page.quick_open_button.click()
        assert opened == ['first'] and page.quick_input.text() == 'Next task already entered'
        page.save_quick()
        assert bridge.commands[-1]['payload']['title'] == 'Next task already entered'
        bridge.commands[-1]['error']({'code': 'conflict', 'message': 'Synthetic retry needed'})
        assert '上次成功记录' in page.quick_open_button.text()
        page.quick_open_button.click()
        assert opened == ['first', 'first'] and page.quick_input.text() == 'Next task already entered'
    finally: dispose(app, page)


@pytest.mark.parametrize('filter_kind', ['done', 'owner', 'search'])
def test_quick_capture_can_open_saved_record_hidden_by_unchanged_filters(app, tmp_path, filter_kind):
    core = Core(tmp_path); bridge = QueuedCoreBridge(core); opened = []
    page = TasksPage(bridge, on_task=opened.append)
    try:
        if filter_kind == 'done':
            page.group_picker.setCurrentIndex(page.group_picker.findData('done'))
        elif filter_kind == 'owner':
            page.owner = cmd(core, 'create', {'type': 'project', 'title': 'Synthetic filter owner'})['result']['entity']
            page.owner_button.setText(page.owner['title'])
        else:
            page.search_input.setText('Only match another task')
        page.filter_changed()
        wait(app, lambda: bridge.pending == 0)
        filters = (page.group_picker.currentData(), page.owner, page.search_input.text(), page.offset)
        page.quick_input.setText('Captured outside the active filter')
        page.save_quick()
        wait(app, lambda: bridge.pending == 0)
        assert page.items.topLevelItemCount() == 0
        assert '尚未加入每日安排' in page.quick_note.text() and not page.quick_open_button.isHidden()
        saved = core.query('get', id=page.last_quick_task_id)['entity']
        assert saved['title'] == 'Captured outside the active filter' and saved['parent_id'] is None
        page.quick_input.setText('Keep this unsaved next thought')
        page.quick_open_button.click()
        assert opened == [saved['id']]
        assert page.quick_input.text() == 'Keep this unsaved next thought'
        assert (page.group_picker.currentData(), page.owner, page.search_input.text(), page.offset) == filters
        assert len(bridge.commands) == 1
    finally:
        wait(app, lambda: bridge.pending == 0); dispose(app, page)


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
        page.open_library(); wait(app, lambda: bridge.pending == 0)
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
    page.open_library(); old = bridge.queries[-1]
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
        page.open_library(); wait(app, lambda: bridge.pending == 0)
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
        page.open_library(); deliver(7)
        page.next.click(); assert bridge.queries[-1]['params']['offset'] == 7; deliver(13)
        page.next.click(); assert bridge.queries[-1]['params']['offset'] == 13; deliver(19)
        page.previous.click(); assert bridge.queries[-1]['params']['offset'] == 7; deliver(13)
        page.previous.click(); assert bridge.queries[-1]['params']['offset'] == 0; deliver(7)
    finally: dispose(app, page)


def test_capture_hub_does_not_load_or_show_entire_library(app, tmp_path):
    core = Core(tmp_path)
    for index in range(8): create(core, f'Existing record {index}')
    bridge = QueuedCoreBridge(core); page = TasksPage(bridge)
    try:
        page.show(); page.refresh(); app.processEvents()
        assert page.stack.currentIndex() == 0 and not page.items.isVisible()
        assert page.result is None and not page.library_loaded and bridge.pending == 0
        for index in range(7):
            page.quick_input.setText(f'New thought {index}'); page.save_quick(); wait(app, lambda: bridge.pending == 0)
        assert len(page.recent_tasks) == 5 and page.recent_tasks[0]['title'] == 'New thought 6'
        assert page.result is None and core.query('list', type='plan')['total'] == 0
        page.open_library(); wait(app, lambda: bridge.pending == 0)
        assert page.items.isVisible() and page.result['total'] == 15
        assert page.result['sort'] == 'recent'
    finally: dispose(app, page)


def test_library_back_preserves_view_and_selection_without_reading_again(app, tmp_path):
    core = Core(tmp_path)
    for index in range(38): create(core, f'Library task {index:02}')
    bridge = QueuedCoreBridge(core); opened = []; page = TasksPage(bridge, on_task=opened.append)
    try:
        page.open_library(); wait(app, lambda: bridge.pending == 0)
        page.search_input.setText('Library task'); page.filter_changed(); wait(app, lambda: bridge.pending == 0)
        page.next.click(); wait(app, lambda: bridge.pending == 0)
        page.items.setCurrentItem(page.items.topLevelItem(4)); identifier = page.selected()['id']
        page.open_shortcut.activated.emit(); page.open_button.click(); page.items.itemDoubleClicked.emit(page.items.currentItem(), 0)
        assert opened == [identifier] * 3
        page.show_capture(); page.open_library()
        assert bridge.pending == 0 and page.offset == 30 and page.selected()['id'] == identifier
        assert page.search_input.text() == 'Library task' and page.page_note.text() == '第 31—38 项，共 38 项'
    finally: dispose(app, page)


def test_library_condition_reset_does_not_change_sort_or_task_data(app, tmp_path):
    core = Core(tmp_path); target = create(core, 'Find me'); other = create(core, 'Other')
    bridge = QueuedCoreBridge(core); page = TasksPage(bridge)
    try:
        page.show_batch_tasks([target['id']]); wait(app, lambda: bridge.pending == 0)
        assert page.result['total'] == 1 and '本次记录 1 项' in page.status.text()
        page.sort_picker.setCurrentIndex(page.sort_picker.findData('due')); wait(app, lambda: bridge.pending == 0)
        page.search_input.setText('Find'); page.filter_changed(); wait(app, lambda: bridge.pending == 0)
        before = core.query('state'); page.reset_filters(); wait(app, lambda: bridge.pending == 0)
        assert page.result['total'] == 2 and page.result['sort'] == 'due'
        assert page.task_ids is None and not page.search_input.text()
        assert core.query('state') == before and bridge.commands == []
    finally: dispose(app, page)


def test_capture_unknown_result_reuses_original_request_and_locks_input(app):
    bridge = ControlledBridge(); page = TasksPage(bridge)
    try:
        page.quick_input.setText('Record once'); page.save_quick(); original = bridge.commands[-1]
        original['error']({'code':'connection_lost','message':'No response'})
        assert page.quick_uncertain and page.quick_input.isReadOnly() and page.pending_writes()
        page.save_quick(); retried = bridge.commands[-1]
        assert retried['payload'] == original['payload'] and retried['options'] == original['options']
        retried['error']({'code':'authentication','message':'Reconnect'})
        assert page.quick_uncertain and page.quick_submission is not None
        page.save_quick(); bridge.commands[-1]['callback']({'result':{'entity':{'id':'created-once','title':'Record once','data':{}}}})
        assert not page.pending_writes() and len(page.recent_tasks) == 1 and not page.quick_input.text()
    finally: dispose(app, page)


def test_recent_capture_join_failure_retries_only_plan_not_create(app, tmp_path, monkeypatch):
    core = Core(tmp_path); bridge = QueuedCoreBridge(core); page = TasksPage(bridge)
    try:
        page.quick_input.setText('Record then arrange'); page.save_quick(); wait(app, lambda: bridge.pending == 0)
        target = page.last_quick_task_id
        day = core.query('task_pool')['date']; cmd(core, 'create_plan', {'date':day,'mode':'rest','blocks':[]})
        page.recent_plan_buttons[target].click(); wait(app, lambda: bridge.pending == 0)
        assert '任务已记下，尚未加入今天' in page.recent_notes[target].text()
        assert core.query('list', type='task')['total'] == 1 and not page.plan_pending
        assert [name for name, _, _ in bridge.commands] == ['create','add_to_plan']
        assert page.recent_plan_buttons[target].isEnabled()
    finally: dispose(app, page)


def test_recent_join_uncertainty_has_visible_retry_and_preserves_original_date(app):
    bridge = ControlledBridge(); page = TasksPage(bridge)
    try:
        page.quick_input.setText('Join once'); page.save_quick()
        bridge.commands[-1]['callback']({'result':{'entity':{'id':'target','title':'Join once','data':{}}}})
        page.recent_plan_buttons['target'].click()
        bridge.deliver('task_pool', {'date':'2030-01-02','plan':None,'epoch':'e','revision':9})
        original = bridge.commands[-1]; original['error']({'code':'timeout','message':'Unknown'})
        assert page.plan_uncertain and not page.hub_plan_retry.isHidden() and not page.recent_plan_buttons['target'].isEnabled()
        page.hub_plan_retry.click(); retry = bridge.commands[-1]
        assert retry['payload'] == original['payload'] and retry['options'] == original['options']
        assert retry['payload']['date'] == '2030-01-02'
        retry['callback']({'result':{'entity':{'id':'plan'}}})
        assert not page.pending_writes() and page.recent_tasks[0]['in_plan']
        assert not page.recent_plan_buttons['target'].isEnabled()
    finally: dispose(app, page)


def test_library_completion_uses_current_business_day_and_only_completion(app, tmp_path, monkeypatch):
    core = Core(tmp_path); target = create(core, 'Finish task', data={'completion_gate':'Check all items'})
    monkeypatch.setattr(core, 'today', lambda _c: '2030-01-02')
    cmd(core, 'record_feedback', {'target_id':target['id'],'business_date':'2030-01-02','dimensions':{'attendance':'absent'},'source_text':'Explicit example'})
    bridge = QueuedCoreBridge(core); page = TasksPage(bridge)
    try:
        page.open_library(); wait(app, lambda: bridge.pending == 0)
        monkeypatch.setattr(core, 'today', lambda _c: '2030-01-03')
        page.complete_selected(); page.complete_selected(); wait(app, lambda: bridge.pending == 0)
        assert len(bridge.commands) == 1 and bridge.commands[0][0] == 'set_task_completion'
        assert bridge.commands[0][1]['business_date'] == '2030-01-03'
        assert core.query('task_pool', group='done')['total'] == 1
        records = core.query('list', type='feedback')['items']
        assert len(records) == 2
        assert {tuple(r['data']['dimensions'].items()) for r in records} == {(('completion','done'),), (('attendance','absent'),)}
        assert core.query('list', type='plan')['total'] == 0
    finally: dispose(app, page)


def test_library_completion_uncertain_cannot_be_resent_as_new_fact(app):
    bridge = ControlledBridge(); page = TasksPage(bridge)
    row = {'id':'task','title':'Task','type':'task','data':{},'version':2,'owner_label':'未分类','status':'active','in_plan':False}
    try:
        page.open_library(); bridge.deliver('task_pool', {'items':[row],'group':'open','total':1,'date':'2030-01-02','next_offset':None})
        page.complete_selected(); bridge.deliver('task_pool', {'date':'2030-01-02','epoch':'e','revision':8})
        original = bridge.commands[-1]; original['error']({'code':'connection_lost','message':'Unknown'})
        assert page.completion_uncertain and not page.complete_button.isEnabled()
        page.completion_retry.click(); retry = bridge.commands[-1]
        assert retry['options'] == original['options'] and retry['payload'] == original['payload']
    finally: dispose(app, page)


def test_location_picker_and_selected_parent_show_typed_ancestry(app, tmp_path):
    from management.gui_forms import EntityPicker, EntityForm
    from management.gui_task_batch import TaskBatchDialog
    core = Core(tmp_path)
    one = cmd(core, 'create', {'type':'domain','title':'Work'})['result']['entity']
    two = cmd(core, 'create', {'type':'domain','title':'Study'})['result']['entity']
    project = cmd(core, 'create', {'type':'project','title':'Same name','parent_id':one['id']})['result']['entity']
    course = cmd(core, 'create', {'type':'course','title':'Same name','parent_id':two['id']})['result']['entity']
    bridge = QueuedCoreBridge(core); picker = EntityPicker(bridge, allowed_types=['project','course'])
    form = batch = None
    before = core.query('state')
    try:
        picker.search.setText('Same name'); picker.reload(); wait(app, lambda: bridge.pending == 0)
        rows = {picker.items.item(i).data(Qt.ItemDataRole.UserRole)['id']:picker.items.item(i) for i in range(picker.items.count())}
        assert '分类 · Work › 项目 · Same name' in rows[project['id']].text()
        assert '分类 · Study › 课程 · Same name' in rows[course['id']].text()
        picker.items.setCurrentItem(rows[course['id']]); picker.choose()
        assert picker.selected['id'] == course['id'] and picker.selected['location_path'][0]['id'] == two['id']
        form = EntityForm(bridge, core.query('capabilities'), default_type='task', parent_entity=picker.selected)
        wait(app, lambda: bridge.pending == 0)
        assert '分类 · Study › 课程 · Same name' in form.parent_path.text()
        assert form.parent_button.toolTip() == form.parent_path.text()
        expected = next(t['parent_types'] for t in core.query('capabilities')['types'] if t['id']=='task')
        assert set(form.parent_picker_types) == {kind for kind in expected if kind is not None}
        batch = TaskBatchDialog(bridge, parent_entity=picker.selected)
        assert '分类 · Study › 课程 · Same name' in batch.context.text()
        form.clear_parent(); batch.clear_parent()
        assert form.parent_id is None and not form.parent_path.text()
        assert batch.parent_entity is None and '暂不分类' in batch.context.text()
        assert core.query('state') == before
    finally:
        if form: form._discard_close = True; dispose(app, form)
        if batch: batch.dirty = False; dispose(app, batch)
        dispose(app, picker)


def test_location_picker_ignores_closed_and_outdated_path_callbacks(app):
    from management.gui_forms import EntityPicker
    bridge = ControlledBridge(); picker = EntityPicker(bridge, allowed_types=['domain','task','goal'])
    initial = bridge.queries[-1]
    assert initial['params']['types'] == ['domain','task','goal'] and initial['params']['include_path'] is True
    picker.search.setText('New name'); picker.reload(); current = bridge.queries[-1]
    initial['callback']({'items':[{'id':'old'}]})
    assert picker.items.count() == 0
    dispose(app, picker)
    current['callback']({'items':[{'id':'late'}]})
    current['error']({'message':'late failure'})
    assert bridge.commands == []
