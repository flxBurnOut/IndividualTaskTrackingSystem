"""Focused review UI behavior with controlled asynchronous responses, no user data."""
import copy
import json
import os
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QScrollArea, QStyle, QStyleOptionButton, QTextEdit
from management.gui_review import ReviewPage
from management.gui_charts import CoverageChart, WeekDaysChart


@pytest.fixture(scope='session')
def app():
    application = QApplication.instance() or QApplication([])
    if application.platformName() == 'offscreen' and os.name == 'nt':
        for name in ('msyh.ttc', 'msyhbd.ttc'):
            path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / name
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
    return application


class ControlledBridge:
    def __init__(self):
        self.queries, self.commands = [], []
        self.epoch, self.revision = 'synthetic-epoch', 10

    def query(self, name, callback=None, error=None, **params):
        self.queries.append({'name': name, 'callback': callback, 'error': error, 'params': params, 'delivered': False})
        return len(self.queries)

    def command(self, name, payload, callback=None, error=None, **options):
        self.commands.append({'name': name, 'payload': copy.deepcopy(payload), 'callback': callback, 'error': error, 'options': options})
        return len(self.commands)

    def deliver(self, name, result, *, first=False):
        matches = [q for q in self.queries if q['name'] == name and not q['delivered']]
        request = matches[0] if first else matches[-1]
        request['delivered'] = True
        request['callback'](copy.deepcopy(result))
        return request


@pytest.fixture
def page(app):
    bridge = ControlledBridge()
    widget = ReviewPage(bridge)
    widget.resize(760, 780)
    widget.show()
    yield widget, bridge
    widget.close()
    widget.deleteLater()
    app.processEvents()


def daily(day='2030-01-07', *, has_plan=True, items=None, plan_version=1, revision=10):
    items = items if items is not None else [
        {'target_id': 'task-a', 'title': 'Synthetic first item', 'target_version': 1, 'completion_gate': 'Finish both checks', 'planned_minutes': 20, 'result': None},
        {'target_id': 'task-b', 'title': 'Synthetic second item', 'target_version': 1, 'completion_gate': 'Retain the full scope', 'planned_minutes': None, 'result': None},
    ]
    if not has_plan:
        items = []
    summary = {'total': len(items), 'done': sum(i.get('result') == 'done' for i in items),
               'incomplete': sum(i.get('result') == 'incomplete' for i in items),
               'unreported': sum(i.get('result') is None and not i.get('reported') for i in items),
               'other_reported': sum(i.get('result') is None and bool(i.get('reported')) for i in items)}
    return {'date': day, 'has_plan': has_plan, 'plan': {'id': 'plan-' + day, 'version': plan_version, 'title': 'Synthetic daily plan', 'mode': 'standard'} if has_plan else None,
            'items': items, 'summary': summary, 'needs_codex': not has_plan, 'epoch': 'synthetic-epoch', 'revision': revision}


def load(page, bridge, data=None):
    data = data or daily()
    page.set_date(data['date'])
    bridge.deliver('daily_review', data)


@pytest.mark.parametrize('font_size', [13, 20])
def test_long_review_owner_details_and_attendance_choices_fit_after_delivery(app, font_size):
    from PySide6.QtWidgets import QLabel
    from management.gui_theme import apply_appearance, current_appearance
    from management.gui_visual_profile import set_visual_style, visual_style
    from test_ux_workflows_v2 import wait
    previous, profile = current_appearance(), visual_style()
    set_visual_style('glass'); apply_appearance(app, {**previous, 'font_size': font_size})
    bridge = ControlledBridge(); widget = ReviewPage(bridge)
    widget.resize(1100, 900); widget.show(); app.processEvents()
    value = daily(items=[{'target_id': 'fixed-class', 'title': '【演示】每周课程与实践练习。' * 8,
        'owner_label': '【演示】设计研究课程与综合实践项目，保留完整课程名称。' * 5,
        'completion_gate': '按原始条件记录完成情况，未知内容仍保持未确认。' * 6,
        'fixed_schedule': True, 'can_review': True, 'planned_minutes': 95,
        'start': '09:00', 'end': '10:35', 'result': None,
        'choices': [('attended', '已参加'), ('absent', '未参加'), ('cancelled', '课程取消'), ('asynchronous', '异步替代')]}])
    try:
        load(widget, bridge, value)
        for width in (1100, 540, 1100):
            widget.resize(width, 900)
            labels = [label for label in widget.rows.findChildren(QLabel) if label.text()]
            wait(app, lambda: all(label.height() >= label.heightForWidth(label.width()) - 1
                                 for label in labels if label.wordWrap()))
            assert widget.scroll.horizontalScrollBar().maximum() == 0
            owner = next(label for label in labels if label.text() == value['items'][0]['owner_label'])
            assert owner.wordWrap()
            for label in labels:
                assert label.wordWrap() and label.height() >= label.heightForWidth(label.width()) - 1
            buttons = list(widget.item_buttons['fixed-class'].values())
            _assert_review_flow(buttons[0].parentWidget(), buttons)
        widget.item_buttons['fixed-class']['attended'].click()
        assert widget._choices[widget._date] == {'fixed-class': 'attended'}
        assert bridge.commands == []
    finally:
        widget.close(); widget.deleteLater(); app.processEvents()
        set_visual_style(profile); apply_appearance(app, previous)


def test_no_plan_is_explicit_and_reading_never_creates_question(page):
    widget, bridge = page
    prompts = []
    widget.on_codex = prompts.append
    load(widget, bridge, daily(has_plan=False))
    assert '没有每日计划' in widget.notice.text()
    assert not widget.notice.isHidden() and not widget.codex_button.isHidden()
    assert widget.confirm_button.isEnabled() is False
    widget.refresh()
    bridge.deliver('daily_review', daily(has_plan=False))
    assert bridge.commands == []
    widget.codex_button.click()
    assert len(prompts) == 1 and '2030-01-07' in prompts[0]
    assert '实际情况' in prompts[0] and bridge.commands == []


def test_manual_actual_feedback_uses_selected_date_without_creating_plan(page, app):
    from PySide6.QtWidgets import QDialogButtonBox
    widget, bridge = page
    load(widget, bridge, daily(has_plan=False))
    widget.set_assistants_visible(False)
    assert widget.codex_button.isHidden()
    assert not widget.feedback_button.isHidden() and not widget.notes_button.isHidden()
    widget.feedback_button.click()
    picker = widget.dialogs[0]
    entity = {'id': 'unplanned-task', 'title': 'Actual work', 'type': 'task', 'data': {}, 'status': 'todo'}
    bridge.deliver('list', {'items': [entity], 'total': 1, 'next_offset': None})
    assert bridge.queries[-1]['params']['types'] == ['task', 'event', 'milestone', 'checklist', 'assessment']
    # The date is captured at opening, even if the underlying page moves later.
    widget.set_date('2030-01-08')
    picker.items.setCurrentRow(0)
    picker.choose()
    editor = widget.dialogs[0]
    try:
        assert editor.date.date().toString('yyyy-MM-dd') == '2030-01-07'
        editor.editors['completion'].setCurrentIndex(editor.editors['completion'].findData('partial'))
        editor.source.setPlainText('Only the first exercise was completed.')
        editor.save()
        editor.save()
        assert len(bridge.commands) == 1
        call = bridge.commands[0]
        assert call['name'] == 'record_feedback'
        assert call['payload'] == {'target_id': entity['id'], 'business_date': '2030-01-07',
                                   'dimensions': {'completion': 'partial'}, 'source_text': 'Only the first exercise was completed.'}
        assert not editor.buttons.button(QDialogButtonBox.StandardButton.Cancel).isEnabled()
        editor.close()
        assert editor.isVisible()
        call['error']({'code': 'validation', 'message': 'Synthetic failure'})
        assert editor.source.toPlainText() == 'Only the first exercise was completed.'
        assert editor.buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled()
    finally:
        editor.saving = editor.dirty = False
        editor.close()
        editor.deleteLater()
        picker.deleteLater()
        app.processEvents()


def test_manual_notes_save_real_text_and_reopen_without_plan_or_feedback(app, tmp_path):
    from management.core import Core
    from management.gui_review import ReviewNotesDialog
    from test_ux_workflows_v2 import QueuedCoreBridge, wait
    core = Core(tmp_path)
    bridge = QueuedCoreBridge(core)
    notes = ReviewNotesDialog(bridge, '2030-01-07')
    notes.show()
    reopened = None
    try:
        wait(app, lambda: bridge.pending == 0)
        notes.text.setPlainText('I rested today; tomorrow I want to review the first exercise.')
        notes.save()
        notes.save()
        wait(app, lambda: bridge.pending == 0)
        saved = core.query('list', type='review')
        assert saved['total'] == 1
        assert saved['items'][0]['data']['start'] == saved['items'][0]['data']['end'] == '2030-01-07'
        assert saved['items'][0]['data']['content'] == notes.text.toPlainText()
        assert core.query('list', type='plan')['total'] == 0
        assert core.query('list', type='feedback')['total'] == 0
        review = core.query('daily_review', date='2030-01-07')
        assert review['has_plan'] is False and review['summary']['total'] == 0
        reopened = ReviewNotesDialog(bridge, '2030-01-07')
        reopened.show()
        wait(app, lambda: bridge.pending == 0)
        assert reopened.history.count() == 1
        assert reopened.saved_text.toPlainText() == notes.text.toPlainText()
        assert reopened.text.toPlainText() == ''
    finally:
        for dialog in (notes, reopened):
            if dialog:
                dialog.saving = dialog.dirty = False
                dialog.close()
                dialog.deleteLater()
        app.processEvents()


def test_manual_notes_keep_draft_on_failure_and_do_not_clear_other_dates(page, app, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    widget, bridge = page
    load(widget, bridge, daily(has_plan=False))
    widget.notes_button.click()
    notes = widget.dialogs[0]
    try:
        bridge.deliver('list', {'items': [], 'total': 0, 'next_offset': None})
        notes.text.setPlainText('Keep this unsaved observation')
        monkeypatch.setattr(QMessageBox, 'question', lambda *_: QMessageBox.StandardButton.No)
        notes.reject()
        assert notes.isVisible()
        widget.set_date('2030-01-08')
        notes.save()
        notes.save()
        assert len(bridge.commands) == 1
        assert bridge.commands[0]['payload']['start'] == '2030-01-07'
        notes.close()
        assert notes.isVisible()
        bridge.commands[0]['error']({'message': 'Synthetic save failure'})
        assert notes.text.toPlainText() == 'Keep this unsaved observation'
        assert notes.dirty and not notes.saving
        assert 'Synthetic save failure' in notes.error_label.text()
    finally:
        notes.saving = notes.dirty = False
        notes.close()
        notes.deleteLater()
        app.processEvents()


def test_manual_notes_paging_excludes_other_date_ranges_and_keeps_complete_text(app):
    from management.gui_review import ReviewNotesDialog
    bridge = ControlledBridge()
    notes = ReviewNotesDialog(bridge, '2030-01-07')
    try:
        bridge.deliver('list', {'items': [{'title': 'Weekly note', 'data': {'start': '2030-01-07', 'end': '2030-01-13', 'content': 'Other range'}}],
                                'next_offset': 30, 'total': 31})
        assert notes.history.count() == 0 and notes.more.isEnabled()
        notes.more.click()
        assert bridge.queries[-1]['params']['offset'] == 30
        complete_text = 'Long precise observation. ' * 200
        bridge.deliver('list', {'items': [{'title': 'Day note', 'data': {'start': '2030-01-07', 'end': '2030-01-07', 'content': complete_text}}],
                                'next_offset': None, 'total': 31})
        assert notes.history.count() == 1 and notes.saved_text.toPlainText() == complete_text
        assert notes.more.isHidden()
        assert bridge.commands == []
    finally:
        notes.close()
        notes.deleteLater()
        app.processEvents()


def test_unplanned_feedback_saved_in_ui_can_be_read_back_across_pages(app, tmp_path):
    from management.core import Core
    from management.gui_review import ActualFeedbackDialog
    from test_ux_workflows_v2 import QueuedCoreBridge, cmd, wait
    core = Core(tmp_path)
    target = cmd(core, 'create', {'type': 'task', 'title': 'Unplanned actual task'})['result']['entity']
    for number in range(30):
        cmd(core, 'record_feedback', {'target_id': target['id'], 'business_date': '2030-01-07',
            'dimensions': {'completion': 'partial'}, 'source_text': 'Earlier observation ' + str(number)})
    bridge = QueuedCoreBridge(core)
    page = ReviewPage(bridge)
    page.set_date('2030-01-07')
    page.set_assistants_visible(False)
    editor = ActualFeedbackDialog(bridge, target, '2030-01-07', on_saved=page._manual_saved)
    editor.show()
    history = None
    try:
        wait(app, lambda: bridge.pending == 0)
        editor.editors['completion'].setCurrentIndex(editor.editors['completion'].findData('done'))
        editor.source.setPlainText('Finished the exact task; no attendance or mastery claim.')
        editor.save()
        wait(app, lambda: bridge.pending == 0)
        assert '查看已保存实际记录' in page.message.text()
        before = core.query('state')
        page.feedback_history_button.click()
        history = page.dialogs[0]
        wait(app, lambda: bridge.pending == 0)
        assert history.history.count() == 30 and history.next.isEnabled()
        assert '已完成' in history.details.toPlainText()
        assert editor.source.toPlainText() in history.details.toPlainText()
        assert '掌握情况' not in history.details.toPlainText()
        history.next.click()
        wait(app, lambda: bridge.pending == 0)
        assert history.history.count() == 1 and not history.next.isEnabled()
        assert 'Earlier observation 0' in history.details.toPlainText()
        assert history.previous.isEnabled() and history.details.isReadOnly()
        assert core.query('state') == before
        assert core.query('daily_review', date='2030-01-07')['summary']['total'] == 0
        assert core.query('list', type='plan')['total'] == 0
    finally:
        for dialog in (editor, history, page):
            if dialog:
                if hasattr(dialog, 'dirty'):
                    dialog.dirty = dialog.saving = False
                dialog.close()
                dialog.deleteLater()
        app.processEvents()


def test_two_explicit_choices_submit_only_selected_and_fence_double_click(page):
    widget, bridge = page
    load(widget, bridge)
    assert set(widget.item_buttons['task-a']) == {'done', 'incomplete'}
    assert all(not button.isChecked() for buttons in widget.item_buttons.values() for button in buttons.values())
    widget.item_buttons['task-a']['done'].click()
    widget.confirm_button.click()
    widget.submit()
    widget.confirm_button.click()
    assert len(bridge.commands) == 1
    call = bridge.commands[0]
    assert call['name'] == 'submit_daily_review'
    assert call['payload'] == {'date': '2030-01-07', 'plan_id': 'plan-2030-01-07', 'plan_version': 1,
                               'answers': [{'target_id': 'task-a', 'result': 'done'}]}
    assert call['options'] == {'epoch': 'synthetic-epoch', 'expected_revision': 10}
    assert not widget.confirm_button.isEnabled()
    assert all(not b.isEnabled() for buttons in widget.item_buttons.values() for b in buttons.values())
    assert 'dimensions' not in call['payload']


def test_clear_new_choice_does_not_clear_existing_saved_fact(page):
    widget, bridge = page
    value = daily()
    value['items'][0]['result'] = 'done'
    load(widget, bridge, value)
    assert widget.item_buttons['task-a']['done'].isChecked()
    widget.item_buttons['task-a']['incomplete'].click()
    assert widget.item_buttons['task-a']['incomplete'].isChecked()
    widget.item_buttons['task-a']['incomplete'].click()
    assert widget.item_buttons['task-a']['done'].isChecked()
    assert not widget.confirm_button.isEnabled()
    widget.item_buttons['task-b']['done'].click()
    widget.item_buttons['task-b']['done'].click()
    assert all(not b.isChecked() for b in widget.item_buttons['task-b'].values())
    assert bridge.commands == []


def test_same_snapshot_refresh_preserves_draft_and_pending_signal(page):
    widget, bridge = page
    pending = []
    widget.has_pending.connect(pending.append)
    load(widget, bridge)
    widget.item_buttons['task-a']['incomplete'].click()
    widget.refresh()
    bridge.deliver('daily_review', daily(revision=12))
    assert widget.item_buttons['task-a']['incomplete'].isChecked()
    assert widget.confirm_button.isEnabled()
    assert pending == [True]
    widget.item_buttons['task-a']['incomplete'].click()
    assert pending == [True, False]


def test_changed_plan_preserves_choices_but_requires_explicit_reread(page):
    widget, bridge = page
    load(widget, bridge)
    widget.item_buttons['task-a']['done'].click()
    widget.refresh()
    bridge.deliver('daily_review', daily(plan_version=2, revision=11))
    assert widget.item_buttons['task-a']['done'].isChecked()
    assert widget.daily_data['plan']['version'] == 1
    assert '重新读取' in widget.message.text()
    assert not widget.confirm_button.isEnabled()
    assert not widget.reload_button.isHidden()
    widget.submit()
    assert bridge.commands == []
    widget.reload_plan(discard=True)
    bridge.deliver('daily_review', daily(plan_version=2, revision=11))
    assert widget.daily_data['plan']['version'] == 2
    assert not any(b.isChecked() for buttons in widget.item_buttons.values() for b in buttons.values())


def test_changed_feedback_on_same_plan_is_not_silently_overwritten(page):
    widget, bridge = page
    load(widget, bridge)
    widget.item_buttons['task-a']['done'].click()
    changed = daily(revision=11)
    changed['items'][0].update(result='incomplete', feedback_id='external-feedback')
    widget.refresh()
    bridge.deliver('daily_review', changed)
    assert widget.item_buttons['task-a']['done'].isChecked()
    assert not widget.confirm_button.isEnabled()
    assert '原有反馈' in widget.message.text()


def test_late_response_from_other_date_cannot_replace_current_day(page):
    widget, bridge = page
    widget.set_date('2030-01-07')
    widget.set_date('2030-01-08')
    bridge.deliver('daily_review', daily('2030-01-08'))
    bridge.deliver('daily_review', daily('2030-01-07'), first=True)
    assert widget.daily_data['date'] == '2030-01-08'
    assert '2030-01-08' in widget.daily_heading.text()


def test_date_switch_preserves_each_dates_unsaved_choices(page):
    widget, bridge = page
    pending = []
    widget.has_pending.connect(pending.append)
    load(widget, bridge)
    widget.item_buttons['task-a']['done'].click()
    widget.set_date('2030-01-08')
    bridge.deliver('daily_review', daily('2030-01-08'))
    assert not widget.item_buttons['task-a']['done'].isChecked()
    assert '另有 1 天' in widget.pending_label.text()
    widget.set_date('2030-01-07')
    bridge.deliver('daily_review', daily())
    assert widget.item_buttons['task-a']['done'].isChecked()
    assert pending == [True]


@pytest.mark.parametrize('raw,label', [('partial', '部分完成'), ('blocked', '受阻'), ('cancelled', '已取消'), ('not_started', '未开始')])
def test_historical_results_keep_original_meaning_and_are_not_preselected(page, raw, label):
    widget, bridge = page
    item = {'target_id': 'old', 'title': 'Historical feedback', 'target_version': 1, 'result': None,
            'original_completion': raw, 'reported': True, 'feedback_id': 'old-feedback'}
    value = daily(items=[item])
    value['summary']['original_results'] = {raw: 1}
    load(widget, bridge, value)
    assert label in widget.item_labels['old'].text()
    assert '未反馈' not in widget.item_labels['old'].text()
    assert all(not b.isChecked() for b in widget.item_buttons['old'].values())
    assert widget.daily_chart.bar.values == {'done': 0, 'incomplete': 0, 'unreported': 0, 'other_reported': 1}
    assert label in widget.daily_chart.originals.text()


def test_submit_failure_keeps_choices_and_exposes_conflict_reread(page):
    widget, bridge = page
    load(widget, bridge)
    widget.item_buttons['task-b']['incomplete'].click()
    widget.submit()
    bridge.commands[0]['error']({'code': 'review_plan_conflict', 'message': '计划已更新'})
    assert widget.item_buttons['task-b']['incomplete'].isChecked()
    assert not widget.confirm_button.isEnabled()
    assert '选择仍保留' in widget.message.text()


def test_success_clears_only_submitted_draft_and_notifies_shell(page):
    widget, bridge = page
    changed, pending = [], []
    widget.on_changed = changed.append
    widget.has_pending.connect(pending.append)
    load(widget, bridge)
    widget.item_buttons['task-b']['incomplete'].click()
    widget.submit()
    receipt = {'result': {'changed_count': 1, 'feedback_ids': ['f']}, 'epoch': 'synthetic-epoch', 'revision': 11}
    bridge.commands[0]['callback'](receipt)
    assert changed == [receipt]
    assert pending == [True, False]
    assert widget._choices == {}
    assert sum(q['name'] == 'daily_review' for q in bridge.queries) == 2
    assert '已确认' in widget.message.text()


def test_week_is_structured_and_missing_plans_are_not_failed_items(page, app):
    widget, bridge = page
    load(widget, bridge)
    result = {'start': '2030-01-07', 'end': '2030-01-13',
              'days': [{'date': '2030-01-07', 'has_plan': True, 'summary': {'total': 3, 'done': 1, 'incomplete': 0, 'unreported': 2}},
                       {'date': '2030-01-08', 'has_plan': False, 'summary': {'total': 0, 'done': 0, 'incomplete': 0, 'unreported': 0}}],
              'summary': {'planned': 3, 'done': 1, 'incomplete': 0, 'unreported': 2, 'days_with_plan': 1, 'days_without_plan': 1}, 'coverage': {}}
    bridge.deliver('weekly_review', result)
    assert '缺计划 1 天' in widget.week_notice.text()
    assert widget.week_chart.bar.total == 3
    assert widget.week_chart.bar.values['incomplete'] == 0
    assert widget.week_chart.bar.values['unreported'] == 2
    assert '分母' in widget.week_chart.denominator.text() and '不代表工作量' in widget.week_chart.denominator.text()
    assert '缺计划' in widget.week_days.day_labels[1].text()
    assert widget.findChildren(QTextEdit) == []
    widget.tabs.setCurrentWidget(widget.weekly_tab)
    app.processEvents()
    assert not widget.week_chart.grab().isNull()
    assert bridge.commands == []


def test_late_week_response_cannot_replace_new_week(page):
    widget, bridge = page
    widget.set_date('2030-01-07')
    widget.set_date('2030-01-14')
    latest = {'start': '2030-01-14', 'end': '2030-01-20', 'days': [], 'summary': {'planned': 0}}
    bridge.deliver('weekly_review', latest)
    bridge.deliver('weekly_review', {'start': '2030-01-07', 'end': '2030-01-13', 'days': [], 'summary': {'planned': 10}}, first=True)
    assert widget.weekly_data['start'] == '2030-01-14'
    assert '没有可统计' in widget.week_chart.heading.text()
    assert '0%' in widget.week_chart.denominator.text()


def test_empty_rest_plan_has_no_fake_choices_or_codex_missing_plan_warning(page):
    widget, bridge = page
    value = daily(items=[])
    value['plan']['mode'] = 'rest'
    load(widget, bridge, value)
    assert widget.notice.isHidden()
    assert widget.codex_button.isHidden()
    assert widget.item_buttons == {}
    assert not widget.confirm_button.isEnabled()
    assert bridge.commands == []



def test_real_http_submission_and_reopen_preserve_unique_review_and_unknowns(app, tmp_path):
    import subprocess
    import sys
    import time
    from management.client import Client
    from management.gui_async import ServiceBridge
    root = tmp_path / 'real-service-synthetic'
    process = subprocess.Popen([sys.executable, '-m', 'management', '--service', '--data-dir', str(root)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    bridge = None
    widgets = []
    def wait(predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return
            time.sleep(.01)
        raise AssertionError('Review/service state did not arrive')
    try:
        wait(lambda: (root / 'runtime.json').exists())
        client = Client(root, autostart=False)
        def cmd(name, payload):
            client.state()
            return client.command(name, payload)
        entities = [cmd('create', {'type': 'task', 'title': 'Synthetic task ' + str(i), 'data': {'completion_gate': 'Explicit completion gate'}})['result']['entity'] for i in range(3)]
        cmd('create_plan', {'date': '2030-01-07', 'mode': 'standard', 'blocks': [{'target_id': e['id'], 'minutes': 10} for e in entities]})
        cmd('record_feedback', {'target_id': entities[0]['id'], 'business_date': '2030-01-07',
                               'dimensions': {'completion': 'partial', 'viewing': 'viewed'}, 'source_text': 'Only partial completion and viewing were reported'})
        bridge = ServiceBridge(root, client_factory=lambda p: Client(p, autostart=False))
        widget = ReviewPage(bridge)
        widgets.append(widget)
        widget.resize(760, 800)
        widget.show()
        widget.set_date('2030-01-07')
        wait(lambda: widget.daily_data is not None and not bridge.callbacks)
        assert '部分完成' in widget.item_labels[entities[0]['id']].text()
        assert not any(b.isChecked() for b in widget.item_buttons[entities[0]['id']].values())
        assert client.query('list', type='checkin')['total'] == 0
        widget.item_buttons[entities[1]['id']]['done'].click()
        widget.item_buttons[entities[2]['id']]['incomplete'].click()
        widget.confirm_button.click()
        widget.confirm_button.click()
        wait(lambda: widget._submitting_date is None and not bridge.callbacks)
        assert '已确认' in widget.message.text(), widget.message.text()
        record = client.query('daily_review', date='2030-01-07')
        first_review_id = record['review_id']
        assert record['summary']['done'] == 1
        assert record['summary']['incomplete'] == 1
        assert record['summary']['other_reported'] == 1
        feedback = client.query('list', type='feedback')['items']
        assert len(feedback) == 3
        assert [f['data']['dimensions'] for f in feedback if f['data']['target_id'] == entities[0]['id']] == [{'completion': 'partial', 'viewing': 'viewed'}]
        for item in feedback:
            if item['data']['target_id'] != entities[0]['id']:
                assert set(item['data']['dimensions']) == {'completion'}
        widget.close()
        reopened = ReviewPage(bridge)
        widgets.append(reopened)
        reopened.show()
        reopened.set_date('2030-01-07')
        wait(lambda: reopened.daily_data is not None and not bridge.callbacks)
        assert reopened.daily_data['review_id'] == first_review_id
        assert reopened.item_buttons[entities[1]['id']]['done'].isChecked()
        assert reopened.item_buttons[entities[2]['id']]['incomplete'].isChecked()
        assert not reopened.confirm_button.isEnabled()
        reopened.refresh()
        wait(lambda: not bridge.callbacks)
        assert client.query('list', type='feedback')['total'] == 3
        assert client.query('list', type='review')['total'] == 1
        assert client.query('list', type='checkin')['total'] == 0
    finally:
        for widget in widgets:
            widget.close()
        if bridge:
            wait(lambda: not bridge.callbacks)
            assert bridge.close()
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)



@pytest.mark.parametrize('width', [540, 760])
def test_narrow_review_width_keeps_choice_buttons_inside_cards(page, app, width):
    widget, bridge = page
    widget.resize(width, 700)
    value = daily()
    value['items'][0]['title'] = '较长的项目名称，用来检查窄窗口中是否能够自动换行并保留两个明确选择。' * 2
    value['items'][0]['completion_gate'] = '按照实际提供的完整完成条件逐项核对，不应因窗口宽度变化隐藏文字。' * 3
    load(widget, bridge, value)
    app.processEvents()
    assert widget.scroll.horizontalScrollBar().maximum() == 0
    for buttons in widget.item_buttons.values():
        for button in buttons.values():
            assert button.geometry().right() <= button.parentWidget().width()
            assert button.width() >= button.minimumSizeHint().width()
    assert widget.confirm_button.geometry().right() <= widget.confirm_button.parentWidget().width()

def _review_rect(widget, ancestor):
    # Independent screen coordinates also cover widgets moved between parents.
    return QRect(ancestor.mapFromGlobal(widget.mapToGlobal(QPoint())), widget.size())


def _assert_review_flow(row, buttons):
    visible = [button for button in buttons if not button.isHidden()]
    regions = [_review_rect(button, row) for button in visible]
    assert all(row.rect().contains(region) for region in regions), (row.rect(), regions)
    assert all(not first.intersects(second) for index, first in enumerate(regions)
               for second in regions[index + 1:]), regions
    assert row.height() >= row.layout().heightForWidth(row.width())
    for button in visible:
        assert button.width() <= max(button.sizeHint().width(), button.minimumWidth()) + 1
        option = QStyleOptionButton()
        button.initStyleOption(option)
        content = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
        text = button.fontMetrics().boundingRect(button.text())
        assert content.width() >= text.width() and content.height() >= text.height(), (content, text)


def _assert_review_sections(body):
    regions = [_review_rect(item.widget(), body) for index in range(body.layout().count())
               if (item := body.layout().itemAt(index)).widget() is not None
               and not item.widget().isHidden()]
    assert all(body.rect().contains(region) for region in regions), (body.rect(), regions)
    assert all(not first.intersects(second) for index, first in enumerate(regions)
               for second in regions[index + 1:]), regions


@pytest.mark.parametrize('theme,font_size', [('light', 13), ('dark', 20)])
@pytest.mark.parametrize('width', [520, 1000])
def test_glass_review_actions_fit_content_wrap_and_remain_reachable(app, tmp_path, theme, font_size, width):
    from management.gui_theme import apply_appearance, current_appearance
    from management.gui_visual_profile import set_visual_style, visual_style
    from test_ux_workflows_v2 import wait
    previous, previous_style = current_appearance(), visual_style()
    bridge, widget, measurements = ControlledBridge(), None, []
    folder = Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR', str(tmp_path)))
    folder.mkdir(parents=True, exist_ok=True)
    try:
        set_visual_style('glass')
        apply_appearance(app, {**previous, 'theme': theme, 'font_size': 13})
        widget = ReviewPage(bridge, on_codex=lambda _: None)
        widget.resize(1000, 640)
        widget.show()
        # Reflow an existing page after both a font change and a width change.
        apply_appearance(app, {**previous, 'theme': theme, 'font_size': font_size})
        widget.resize(width, 640)

        def settle(scroll, body, rows):
            app.processEvents()
            wait(app, lambda: all(row.height() >= row.layout().heightForWidth(row.width())
                                 and all(row.rect().contains(_review_rect(row.layout().itemAt(i).widget(), row))
                                         for i in range(row.layout().count())
                                         if row.layout().itemAt(i).widget() is not None
                                         and not row.layout().itemAt(i).widget().isHidden())
                                 for row in rows if not row.isHidden())
                 and scroll.widget() is body
                 and body.width() <= scroll.viewport().width())
            assert scroll.horizontalScrollBar().maximum() == 0
            assert scroll.findChildren(QScrollArea) == [], 'one scrolling surface per tab'
            _assert_review_sections(body)
            for row in rows:
                if not row.isHidden():
                    _assert_review_sections(row)
            _assert_review_sections(widget.header_actions)
            assert widget.rect().contains(_review_rect(widget.header_actions, widget))

        def reachable(scroll, buttons):
            for button in buttons:
                if button.isHidden():
                    continue
                scroll.ensureWidgetVisible(button, 0, 0)
                app.processEvents()
                wait(app, lambda: scroll.viewport().rect().contains(_review_rect(button, scroll.viewport())))
                assert not button.visibleRegion().isEmpty()

        for has_plan in (False, True):
            widget.tabs.setCurrentWidget(widget.daily_tab)
            load(widget, bridge, daily(has_plan=has_plan))
            rows = (widget.daily_actions, widget.daily_chart_tools, widget.confirm_actions)
            settle(widget.scroll, widget.daily_body, rows)
            actions = (widget.feedback_button, widget.notes_button, widget.feedback_history_button, widget.codex_button)
            _assert_review_flow(widget.daily_actions, actions)
            if not has_plan and width == 520 and font_size == 20:
                assert len({button.y() for button in actions}) > 1
            if has_plan:
                _assert_review_flow(widget.daily_chart_tools, [widget.daily_chart_button])
                _assert_review_flow(widget.confirm_actions, [widget.confirm_button, widget.reload_button])
                assert widget.daily_chart_button.parentWidget() is widget.daily_chart_tools
            reachable(widget.scroll, [*actions, widget.daily_chart_button, widget.confirm_button]
                      if has_plan else actions)
            widget.scroll.verticalScrollBar().setValue(0)
            app.processEvents()
            name = f'review-actions-{theme}-{font_size}-{width}-' + ('daily' if has_plan else 'no-plan')
            assert widget.grab().save(str(folder / (name + '.png')))
            measurements.append({'panel': name, 'window': [widget.width(), widget.height()],
                                 'actions': [_review_rect(button, widget.daily_actions).getRect()
                                             for button in actions if not button.isHidden()],
                                 'scroll_range': widget.scroll.verticalScrollBar().maximum()})

        result = {'start': '2030-01-07', 'end': '2030-01-13',
                  'days': [{'date': f'2030-01-{7 + index:02}', 'has_plan': index != 2,
                            'summary': {'total': 2 if index != 2 else 0, 'done': 1 if index != 2 else 0,
                                        'unreported': 1 if index != 2 else 0}}
                           for index in range(7)],
                  'summary': {'planned': 12, 'done': 6, 'unreported': 6,
                              'days_with_plan': 6, 'days_without_plan': 1}, 'coverage': {}}
        bridge.deliver('weekly_review', result)
        widget.tabs.setCurrentWidget(widget.weekly_tab)
        settle(widget.week_scroll, widget.weekly_body, (widget.week_header_tools, widget.week_days_tools))
        _assert_review_flow(widget.week_header_tools, [widget.week_chart_button])
        _assert_review_flow(widget.week_days_tools, [widget.week_days_button])
        assert widget.week_heading.parentWidget() is widget.week_header_tools
        assert widget.week_days_button.parentWidget() is widget.week_days_tools
        heading = widget.week_heading
        single_line_height = heading.heightForWidth(16777215)
        assert heading.width() >= heading.fontMetrics().horizontalAdvance(heading.text())
        assert heading.height() == single_line_height, 'a complete date range fits on one line'
        assert heading.minimumWidth() == 0, 'preferred width must not prevent a narrow layout'
        if width == 520:
            date_range = heading.text()
            heading.setText(' · '.join([date_range] * 3))
            settle(widget.week_scroll, widget.weekly_body, (widget.week_header_tools, widget.week_days_tools))
            assert heading.width() < heading.sizeHint().width()
            assert heading.height() >= heading.heightForWidth(heading.width()) > single_line_height
            assert widget.week_scroll.horizontalScrollBar().maximum() == 0
            heading.setText(date_range)
            settle(widget.week_scroll, widget.weekly_body, (widget.week_header_tools, widget.week_days_tools))
        reachable(widget.week_scroll, [widget.week_chart_button, widget.week_days_button])
        widget.week_scroll.verticalScrollBar().setValue(0)
        app.processEvents()
        name = f'review-actions-{theme}-{font_size}-{width}-weekly'
        assert widget.grab().save(str(folder / (name + '.png')))
        measurements.append({'panel': name, 'window': [widget.width(), widget.height()],
                             'scroll_range': widget.week_scroll.verticalScrollBar().maximum()})
        assert bridge.commands == []
    finally:
        (folder / f'review-actions-geometry-{theme}-{font_size}-{width}.json').write_text(
            json.dumps(measurements, ensure_ascii=False, indent=2), encoding='utf-8')
        if widget is not None:
            widget.close()
            widget.deleteLater()
            app.processEvents()
        set_visual_style(previous_style)
        apply_appearance(app, previous)
        app.processEvents()


def test_review_visual_roundtrip_preserves_controls_choices_open_editor_and_callbacks(app):
    from management.gui_theme import apply_appearance, current_appearance
    from management.gui_visual_profile import set_visual_style, visual_style
    previous, previous_style = current_appearance(), visual_style()
    widget, editor = None, None
    try:
        set_visual_style('classic')
        apply_appearance(app, {**previous, 'font_size': 13})
        bridge = ControlledBridge()
        widget = ReviewPage(bridge)
        widget.resize(900, 760)
        widget.show()
        load(widget, bridge)
        app.processEvents()
        widget.item_buttons['task-a']['done'].click()
        original_choices = {key: dict(value) for key, value in widget.item_buttons.items()}
        references = (widget.feedback_button, widget.notes_button, widget.confirm_button,
                      widget.date_editor, widget.daily_tab, widget.weekly_tab, widget.rows)
        assert widget.scroll.widget() is widget.rows
        classic_button_width = widget.feedback_history_button.width()
        widget.notes_button.click()
        editor = widget.dialogs[-1]
        editor.text.setPlainText('Synthetic unsaved reflection; never submit during a visual switch.')
        queries = len(bridge.queries)
        requested = []
        widget.chart_settings_requested.connect(requested.append)
        for selected, font_size in (('glass', 20), ('classic', 13), ('glass', 13), ('classic', 13)):
            set_visual_style(selected)
            apply_appearance(app, {**previous, 'font_size': font_size})
            widget.apply_visual_style(selected)
            app.processEvents()
            assert references == (widget.feedback_button, widget.notes_button, widget.confirm_button,
                                  widget.date_editor, widget.daily_tab, widget.weekly_tab, widget.rows)
            assert widget.item_buttons == original_choices
            assert widget.item_buttons['task-a']['done'].isChecked() and widget.confirm_button.isEnabled()
            assert editor.isVisible() and editor.dirty and editor.text.toPlainText().startswith('Synthetic unsaved')
            assert len(bridge.queries) == queries and bridge.commands == []
            assert widget.scroll.isVisible() and widget.scroll.findChildren(QScrollArea) == []
            assert widget.scroll.widget() is (widget.daily_body if selected == 'glass' else widget.rows)
            if selected == 'classic':
                assert widget.feedback_history_button.width() == classic_button_width
            for button in (widget.daily_chart_button, widget.week_chart_button, widget.week_days_button):
                button.click()
            assert requested[-3:] == ['review_daily_style', 'review_weekly_style', 'weekly_style']
        assert len(requested) == 12
    finally:
        if editor is not None:
            editor.dirty = editor.saving = False
            editor.close()
            editor.deleteLater()
        if widget is not None:
            widget.close()
            widget.deleteLater()
        app.processEvents()
        set_visual_style(previous_style)
        apply_appearance(app, previous)
        app.processEvents()


def save_visual_previews(directory):
    """Run as a separate process per QT_SCALE_FACTOR; all content is synthetic."""
    from pathlib import Path
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    application = QApplication.instance() or QApplication([])
    application.setStyle('Fusion')
    from management.gui import STYLESHEET, configure_palette
    from PySide6.QtGui import QFont
    configure_palette(application)
    application.setFont(QFont('Microsoft YaHei UI', 10))
    application.setStyleSheet(STYLESHEET)
    bridge = ControlledBridge()
    widget = ReviewPage(bridge, on_codex=lambda _: None)
    widget.resize(980, 1080)
    widget.show()
    load(widget, bridge, daily(has_plan=False))
    application.processEvents()
    assert widget.grab().save(str(destination / 'no-plan.png'))
    items = [
        {'target_id':'a','target_version':1,'title':'完成讲义中的两道练习题','completion_gate':'独立作答并核对推导过程；保存无法确认的步骤。','planned_minutes':35,'result':'done','reported':True},
        {'target_id':'b','target_version':1,'title':'整理课程项目需求','completion_gate':'列出已确认要求与待确认问题。','planned_minutes':25,'result':'incomplete','reported':True},
        {'target_id':'c','target_version':1,'title':'阅读本周材料','completion_gate':'按实际阅读范围反馈，不由文件存在推断已经读过。','planned_minutes':20,'result':None},
        {'target_id':'d','target_version':1,'title':'复习上周的知识点','completion_gate':'保留原先的部分完成记录。','planned_minutes':15,'result':None,'original_completion':'partial','reported':True},
        {'target_id':'e','target_version':1,'title':'已取消的临时安排','completion_gate':'保留取消原义，不自动改为未完成。','planned_minutes':10,'result':None,'original_completion':'cancelled','reported':True},
    ]
    value = daily(items=items)
    value['plan']['title'] = '学习与项目安排（合成演示）'
    value['summary']['original_results'] = {'partial':1,'cancelled':1}
    widget.refresh()
    bridge.deliver('daily_review', value)
    application.processEvents()
    assert widget.grab().save(str(destination / 'mixed-daily.png'))
    days = [
        {'date':'2030-01-07','has_plan':True,'summary':value['summary']},
        {'date':'2030-01-08','has_plan':True,'summary':{'total':3,'done':2,'incomplete':0,'unreported':1}},
        {'date':'2030-01-09','has_plan':False,'summary':{}},
        {'date':'2030-01-10','has_plan':True,'summary':{'total':2,'done':0,'incomplete':1,'unreported':1}},
        {'date':'2030-01-11','has_plan':False,'summary':{}},
        {'date':'2030-01-12','has_plan':True,'summary':{'total':0}},
        {'date':'2030-01-13','has_plan':True,'summary':{'total':2,'done':1,'incomplete':0,'unreported':1}},
    ]
    bridge.deliver('weekly_review', {'start':'2030-01-07','end':'2030-01-13','days':days,
        'summary':{'planned':12,'done':4,'incomplete':2,'unreported':4,'other_reported':2,'original_results':{'partial':1,'cancelled':1},'days_with_plan':5,'days_without_plan':2},'coverage':{'complete':True}})
    widget.tabs.setCurrentWidget(widget.weekly_tab)
    application.processEvents()
    assert widget.grab().save(str(destination / 'weekly.png'))
    # Validate horizontal geometry in the actual scaled renderer; scrolling is intentional.
    for button in widget.week_days.findChildren(__import__('PySide6.QtWidgets', fromlist=['QPushButton']).QPushButton):
        assert button.geometry().right() <= button.parentWidget().width()
    metadata = {'scale': os.environ.get('QT_SCALE_FACTOR','1'), 'logical_size':[widget.width(),widget.height()],
                'device_pixel_ratio':widget.devicePixelRatioF(),'files':['no-plan.png','mixed-daily.png','weekly.png']}
    (destination/'geometry.json').write_text(__import__('json').dumps(metadata,indent=2),encoding='utf-8')
    widget.close()
    return metadata


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--snapshot-dir', required=True)
    args=parser.parse_args()
    print(save_visual_previews(args.snapshot_dir))
