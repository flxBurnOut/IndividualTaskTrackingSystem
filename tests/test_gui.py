"""Native GUI checks against a real loopback service and synthetic data only."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import QDate, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLineEdit, QLabel, QSpinBox
from management.client import Client
from management.gui import MainWindow, STYLESHEET
from management.gui_async import ServiceBridge
from management.gui_forms import EntityForm, FeedbackDialog
from management.gui_workflows import PlanDialog, SettingsDialog

@pytest.fixture(scope="session")
def app():
    application = QApplication.instance() or QApplication([])
    application.setStyle("Fusion")
    application.setStyleSheet(STYLESHEET)
    return application

def wait(app, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("GUI condition did not become true before timeout")

@pytest.fixture
def window(app, tmp_path):
    root = tmp_path / "synthetic-data"
    process = subprocess.Popen([sys.executable, "-m", "management", "--service", "--data-dir", str(root)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        wait(app, lambda: (root / "runtime.json").exists())
        window = MainWindow(root, client_factory=lambda p: Client(p, autostart=False))
        window.show()
        wait(app, lambda: bool(window.type_map) and not window.bridge.callbacks)
        yield window
        window.close()
        wait(app, lambda: not window.bridge.thread.isRunning())
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)

def save_form(app, form):
    form.show()
    form.save()
    wait(app, lambda: not form.isVisible() or bool(form.error_label.text() and form.error_label.text() != "正在保存…"))

def create(client, title, kind="task", data=None, parent_id=None):
    client.state()
    return client.command("create", {"type": kind, "title": title, "data": data or {}, "parent_id": parent_id})["result"]["entity"]


def test_codex_connection_entry_stays_visible_across_all_enabled_states(app, window, monkeypatch):
    # Exercise the display setting separately from connection permission. Avoid
    # an unrelated automatic probe replacing the explicitly selected UI states.
    monkeypatch.setattr(window.codex_connection, 'automatic', False)
    settings = {'appearance': {'show_assistants': True},
                'ai': {'enabled': True, 'execution_mode': 'desktop_shared'}}
    window._apply_display_preferences(settings)
    for state in ('checking', 'desktop_closed', 'connecting', 'ready', 'disconnected', 'unsupported', 'error'):
        window.codex_connection._set_state(state)
        app.processEvents()
        assert window.codex_connection_panel.isVisible()
        assert window.codex_connection_retry.isVisible()
        assert window.codex_connection_note.text()
        assert '退出 Codex' not in window.codex_connection_note.text()
    calls = []
    monkeypatch.setattr(window.bridge, 'command', lambda name, *args, **kwargs: calls.append(name))
    connection_signature = window.codex_connection._signature
    window._apply_display_preferences({**settings, 'appearance': {'show_assistants': False}})
    app.processEvents()
    assert not window.codex_connection_panel.isVisible()
    assert window.codex_connection.required
    assert window.codex_connection._signature == connection_signature
    assert settings['ai'] == {'enabled': True, 'execution_mode': 'desktop_shared'}
    assert calls == []  # Hiding the entry does not disable the configured agent.
    window._apply_display_preferences(settings)
    assert window.codex_connection_panel.isVisible()
    window.codex_connection_retry.click()
    assert calls == ['connect_codex']

def test_empty_five_navigation_and_form_create(app, window):
    assert '个人事务管理 · Beta 测试版' in window.windowTitle()
    assert any(label.text() == 'Beta 测试版' for label in window.findChildren(QLabel))
    assert [b.text().split("  ")[0] for b in window.nav_buttons.values()] == ["总览", "今天", "任务", "项目与课程", "复盘"]
    assert not window.today_page.has_plan
    assert Client(window.data_dir, autostart=False).query("state")["counts"] == {}
    form = EntityForm(window.bridge, window.capabilities, window)
    form.title_edit.setText("合成测试：第一项任务")
    gate = form.fields["completion_gate"]
    gate.enabled.setChecked(True)
    gate.editor.setPlainText("独立完成两个核对步骤")
    save_form(app, form)
    assert form.result() == QDialog.DialogCode.Accepted, form.error_label.text()
    client = Client(window.data_dir, autostart=False)
    tasks = client.query("list", type="task")["items"]
    assert len(tasks) == 1
    assert tasks[0]["data"]["completion_gate"] == "独立完成两个核对步骤"
    assert "estimated_minutes" not in tasks[0]["data"]

def test_external_update_does_not_erase_form_and_stale_save_rejected(app, window):
    external = Client(window.data_dir, autostart=False)
    task = create(external, "原始标题")
    form = EntityForm(window.bridge, window.capabilities, window, entity=task)
    form.show()
    form.title_edit.setText("尚未保存的界面输入")
    external.command("update", {"id": task["id"], "version": task["version"], "patch": {"title": "另一入口的新标题"}})
    window.refresh()
    wait(app, lambda: not window.bridge.callbacks)
    assert form.title_edit.text() == "尚未保存的界面输入"
    form.save()
    wait(app, lambda: "修改" in form.error_label.text() or "更新" in form.error_label.text())
    assert form.isVisible()
    assert external.query("get", id=task["id"])["entity"]["title"] == "另一入口的新标题"
    form.reject()

def test_feedback_keeps_unreported_dimensions_unknown(app, window):
    client = Client(window.data_dir, autostart=False)
    task = create(client, "反馈边界合成任务")
    window.bridge.query("state")
    wait(app, lambda: not window.bridge.callbacks)
    form = FeedbackDialog(window.bridge, task, window)
    form.editors["completion"].setCurrentIndex(form.editors["completion"].findData("done"))
    form.source.setPlainText("我已完成本项；提交和掌握没有提供情况。")
    save_form(app, form)
    assert form.result() == QDialog.DialogCode.Accepted, form.error_label.text()
    feedback = client.query("list", type="feedback")["items"]
    assert len(feedback) == 1
    assert feedback[0]["data"]["dimensions"] == {"completion": "done"}

def test_tree_pagination_and_search_keep_large_workspaces_accessible(app, window):
    from management.gui import SearchDialog
    client = Client(window.data_dir, autostart=False)
    for i in range(105):
        create(client, f"分页合成任务 {i:03d}")
    window.navigate("projects")
    tree = window.workspace_page.tree
    wait(app, lambda: tree.topLevelItemCount() == 101 and not window.bridge.callbacks)
    more = tree.topLevelItem(100)
    assert more.data(0, Qt.ItemDataRole.UserRole).get("_page") == 100
    tree._clicked(more, 0)
    wait(app, lambda: tree.topLevelItemCount() == 105 and not window.bridge.callbacks)
    opened = []
    search = SearchDialog(window.bridge, window, opened.append)
    search.show()
    search.search.setText("分页合成任务")
    wait(app, lambda: search.results.count() == 30 and "105" in search.hint.text())
    first_page = {search.results.item(i).data(Qt.ItemDataRole.UserRole)['id'] for i in range(search.results.count())}
    search.next.click()
    wait(app, lambda: not search.loading and search.offset == 30 and search.results.count() == 30)
    assert not first_page & {search.results.item(i).data(Qt.ItemDataRole.UserRole)['id'] for i in range(search.results.count())}
    search.previous.click()
    wait(app, lambda: not search.loading and search.offset == 0 and search.results.count() == 30)
    search.search.setText("分页合成任务 003")
    wait(app, lambda: search.results.count() == 1 and "003" in search.results.item(0).text())
    assert search.offset == 0 and not search.next.isEnabled()
    item = search.results.item(0)
    QTest.mouseClick(search.results.viewport(), Qt.MouseButton.LeftButton, pos=search.results.visualItemRect(item).center())
    assert search.open_button.isEnabled() and not opened
    search.open_button.click()
    assert len(opened) == 1 and opened[0]['title'] == '分页合成任务 003'
    assert not search.isVisible()


def test_header_search_accepts_text_and_keyboard_without_retyping(app, window, monkeypatch):
    client = Client(window.data_dir, autostart=False)
    task = create(client, '合成：键盘可查找')
    opened = []
    monkeypatch.setattr(window, 'open_task', opened.append)
    window.activateWindow()
    app.setActiveWindow(window)
    app.processEvents()
    QTest.keyClick(window, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
    assert window.search_input.hasFocus()
    window.search_input.setText(task['title'])
    QTest.keyClick(window.search_input, Qt.Key.Key_Return)
    dialog = window.search_dialog
    assert dialog is not None and dialog.search.text() == task['title']
    try:
        wait(app, lambda: not dialog.loading and dialog.results.count() == 1)
        QTest.keyClick(dialog.search, Qt.Key.Key_Return)
        assert opened == [task['id']]
        assert window.search_dialog is None
    finally:
        if window.search_dialog is not None:
            window.search_dialog.reject()


def test_habits_and_assistant_have_independent_visible_entries(app, window, monkeypatch):
    from types import SimpleNamespace
    from management import gui_workflows
    client = Client(window.data_dir, autostart=False)
    before = client.state()['revision']
    calls = []
    class Habits:
        def __init__(self, bridge, capabilities, parent, on_saved):
            assert bridge is window.bridge and parent is window
            self.habits = SimpleNamespace(show_reminders=lambda: calls.append('reminders'))
        def exec(self): calls.append('habits')
        def deleteLater(self): pass
    monkeypatch.setattr(gui_workflows, 'HabitsDialog', Habits)
    assert not window.habits_button.isHidden() and not window.assistant_button.isHidden()
    window.habits_button.click()
    window.today_page.habits_button.click()
    window.open_settings(page='日常习惯')
    window.open_settings(page='复盘时间')
    assert calls == ['habits', 'habits', 'habits', 'reminders', 'habits']
    monkeypatch.setattr(window, 'open_settings', lambda *args, **kwargs: calls.append(kwargs.get('page')))
    window.assistant_button.click()
    assert calls[-1] == 'Codex 协助'
    assert client.state()['revision'] == before

def test_search_rejects_late_response_for_old_text(app):
    from management.gui import SearchDialog
    class DeferredBridge:
        def __init__(self):
            self.pending = []
            self.errors = []
        def query(self, name, callback, error=None, **params):
            self.pending.append(callback)
            self.errors.append(error)
    bridge = DeferredBridge()
    search = SearchDialog(bridge, None, lambda _: None)
    search.search.setText("旧条件")
    search.load()
    search.search.setText("新条件")
    search.load()
    bridge.pending[-1]({"items": [{"id": "new", "type": "task", "title": "新结果"}], "total": 1})
    bridge.pending[0]({"items": [{"id": "old", "type": "task", "title": "旧结果"}], "total": 1})
    bridge.errors[0]({'message': '过期查询失败'})
    assert search.results.item(0).text() == "新结果"
    assert '过期查询失败' not in search.hint.text()
    search.search.setText('另一个条件')
    assert search.results.count() == 0 and not search.open_button.isEnabled()
    search.timer.stop()
    search.reject()


def test_search_previous_page_uses_returned_offsets_and_new_text_resets_history(app):
    from management.gui import SearchDialog
    class Bridge:
        def __init__(self): self.queries = []
        def query(self, name, callback, error=None, **params):
            self.queries.append((params, callback))
    bridge = Bridge()
    search = SearchDialog(bridge, None, lambda _: None)
    def deliver(next_offset):
        params, callback = bridge.queries[-1]
        callback({'items': [{'id': str(params['offset']), 'type': 'task', 'title': 'Synthetic result'}],
                  'total': 80, 'next_offset': next_offset})
    try:
        search.search.setText('Synthetic')
        search.load(); deliver(7)
        search.next.click(); assert bridge.queries[-1][0]['offset'] == 7; deliver(13)
        search.next.click(); assert bridge.queries[-1][0]['offset'] == 13; deliver(26)
        search.previous.click(); assert bridge.queries[-1][0]['offset'] == 7; deliver(13)
        search.previous.click(); assert bridge.queries[-1][0]['offset'] == 0; deliver(7)
        assert not search.previous.isEnabled()
        search.next.click(); deliver(13)
        search.search.setText('A different condition')
        assert search.offset == 0 and search.page_history == [] and not search.previous.isEnabled()
        search.load()
        assert bridge.queries[-1][0]['offset'] == 0
    finally:
        search.reject()


def test_plan_conflict_keeps_draft_open(app, window):
    client = Client(window.data_dir, autostart=False)
    today = QDate.currentDate().toString("yyyy-MM-dd")
    task = create(client, "计划合成任务", data={"completion_gate": "完成完整核对"})
    create(client, "固定合成会议", kind="event", data={"date": today, "start": "10:00", "end": "11:00", "hard": True, "time_kind": "exact"})
    form = PlanDialog(window.bridge, window)
    form.show()
    wait(app, lambda: hasattr(form, "context_revision"))
    form.table.insertRow(0)
    label = QLabel(task["title"])
    label.setProperty("target_id", task["id"])
    form.table.setCellWidget(0, 0, label)
    for column, value in ((1, "10:30"), (2, "11:30"), (4, "完成完整核对")):
        form.table.setCellWidget(0, column, QLineEdit(value))
    minutes = QSpinBox()
    minutes.setValue(60)
    form.table.setCellWidget(0, 3, minutes)
    form.save()
    wait(app, lambda: form.error_label.text() not in ("", "正在保存…"))
    assert form.isVisible()
    assert client.query("list", type="plan")["total"] == 0
    form.reject()

def test_ten_extensions_keep_five_navigation_and_typed_fields(app, window):
    client = Client(window.data_dir, autostart=False)
    for index in range(10):
        module = f"synthetic{index}"
        client.command("install_module", {"manifest": {"id": module, "version": 1, "types": [{"id": module + ".record", "label": f"合成类型 {index}", "section": "projects", "parent_types": [None], "fields": [{"id": "amount", "label": "计量", "type": "number"}]}]}})
    window.load_capabilities()
    wait(app, lambda: "synthetic9.record" in window.type_map)
    assert [b.text().split("  ")[0] for b in window.nav_buttons.values()] == ["总览", "今天", "任务", "项目与课程", "复盘"]
    assert len(window.create_menu.actions()) == 6
    assert all("合成类型" not in action.text() for action in window.create_menu.actions())
    form = EntityForm(window.bridge, window.capabilities, window, default_type="synthetic9.record")
    assert "amount" in form.fields
    assert form.fields["amount"].kind == "number"
    form.reject()

def test_settings_allow_empty_optional_provider_values(app, window):
    dialog = SettingsDialog(window.bridge, window.capabilities, window.data_dir, window)
    dialog.show()
    wait(app, lambda: not window.bridge.callbacks)
    assert dialog.executable.text() == ""
    assert not dialog.ai_enabled.isChecked()
    dialog.reject()

def test_service_io_does_not_block_qt_event_loop(app, tmp_path):
    class SlowClient:
        def __init__(self, *_):
            pass
        def query(self, *_args, **_kwargs):
            time.sleep(.25)
            return {"epoch": "test", "revision": 0}
    bridge = ServiceBridge(tmp_path, client_factory=SlowClient)
    ticks = []
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    completed = []
    bridge.query("state", lambda _: completed.append(True))
    wait(app, lambda: bool(completed))
    timer.stop()
    assert len(ticks) >= 5
    assert bridge.close()



def test_artifact_forms_produce_csv_and_notebook_without_json_editor(app, window):
    from management.gui_workflows import ArtifactDialog
    client = Client(window.data_dir, autostart=False)
    for kind, content in (("csv", "名称,次数\n合成条目,2"), ("notebook", "合成题目：计算 1 + 1，并写下验证过程。")):
        window.bridge.query("state")
        wait(app, lambda: not window.bridge.callbacks)
        form = ArtifactDialog(window.bridge, window)
        form.kind.setCurrentIndex(form.kind.findData(kind))
        form.title.setText("合成成果 " + kind)
        form.content.setPlainText(content)
        save_form(app, form)
        assert form.result() == QDialog.DialogCode.Accepted, form.error_label.text()
    def completed():
        jobs = client.query("jobs")["items"]
        return len(jobs) == 2 and all(j["status"] in ("completed", "failed") for j in jobs)
    wait(app, completed, timeout=15)
    jobs = client.query("jobs")["items"]
    assert all(j["status"] == "completed" for j in jobs), jobs
    details = [client.query("job", id=j["id"])["job"] for j in jobs]
    notebook = next(j for j in details if j["input"]["kind"] == "notebook")
    assert notebook["input"]["content"]["nbformat"] == 4
    assert notebook["input"]["content"]["cells"][1]["cell_type"] == "code"

def test_extension_workflow_uses_typed_create_form(app, window):
    client = Client(window.data_dir, autostart=False)
    manifest = {"id": "typedworkflow", "version": 1, "workflows": [{"id": "new_task", "label": "合成流程", "action": "create", "defaults": {"type": "task", "title": "流程缺省标题", "data": {"completion_gate": "核对三个明确步骤"}}}]}
    client.command("install_module", {"manifest": manifest})
    window.load_capabilities()
    wait(app, lambda: bool(window.capabilities.get("workflows")) and not window.bridge.callbacks)
    workflow = window.capabilities["workflows"][0]
    form = EntityForm(window.bridge, window.capabilities, window, default_type="task", initial_payload=workflow["defaults"], workflow=workflow)
    assert form.title_edit.text() == "流程缺省标题"
    assert form.fields["completion_gate"].editor.toPlainText() == "核对三个明确步骤"
    form.title_edit.setText("通过原生表单运行的流程任务")
    save_form(app, form)
    assert form.result() == QDialog.DialogCode.Accepted, form.error_label.text()
    assert client.query("list", type="task")["items"][0]["title"] == "通过原生表单运行的流程任务"



def test_job_adoption_waits_for_full_selected_details(app):
    from management.gui_workflows import JobsDialog
    class DeferredBridge:
        def __init__(self):
            self.calls = []
        def query(self, name, callback, error=None, **params):
            self.calls.append((name, params))
            if name == "jobs":
                summary = {"id": "job-1", "kind": "ai", "status": "awaiting_review", "input": {"prompt": "合成请求"}, "result": {"summary": "候选摘要"}, "detail_required": True}
                QTimer.singleShot(0, lambda: callback({"items": [summary], "total": 1, "next_offset": None}))
            elif name == "job":
                detail = {"id": "job-1", "kind": "ai", "status": "awaiting_review", "input": {"prompt": "合成请求"}, "result": {"summary": "完整候选", "unknowns": [], "sources": [], "actions": [{"command": "create", "payload": {"type": "task", "title": "明确候选任务"}, "reason": "用户明确要求"}]}}
                QTimer.singleShot(150, lambda: callback({"job": detail}))
    bridge = DeferredBridge()
    dialog = JobsDialog(bridge)
    dialog.show()
    wait(app, lambda: any(name == "job" for name, _ in bridge.calls))
    assert not dialog.apply.isEnabled()
    wait(app, lambda: dialog.apply.isEnabled())
    assert "明确候选任务" in dialog.detail.toPlainText()
    assert [name for name, _ in bridge.calls] == ["jobs", "job"]
    dialog.accept()



@pytest.mark.parametrize('font_size', [13, 20])
def test_jobs_window_keeps_explanation_and_actions_readable_in_narrow_window(app, font_size):
    from management.gui_workflows import JobsDialog
    from management.gui_theme import apply_appearance, current_appearance
    from management.gui_visual_profile import set_visual_style, visual_style
    from test_ux_workflows_v2 import ControlledBridge
    from PySide6.QtWidgets import QPushButton
    previous, profile = current_appearance(), visual_style()
    bridge = ControlledBridge(); dialog = None
    try:
        set_visual_style('glass'); apply_appearance(app, {'theme': 'dark', 'font_size': font_size})
        dialog = JobsDialog(bridge); dialog.resize(620, 610); dialog.show(); dialog.timer.stop()
        for _ in range(4): app.processEvents()
        assert dialog.width() <= 620
        for label in dialog.findChildren(QLabel):
            if label.isVisibleTo(dialog) and label.text().strip():
                needed = label.heightForWidth(label.width()) if label.wordWrap() else label.sizeHint().height()
                assert label.height() + 1 >= needed, label.text()
        for button in dialog.findChildren(QPushButton):
            if button.isVisibleTo(dialog):
                assert button.width() >= button.minimumSizeHint().width(), button.text()
                assert button.height() >= button.minimumSizeHint().height(), button.text()
                assert not button.visibleRegion().isEmpty()
        report = os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR')
        if report: assert dialog.grab().save(str(Path(report) / f'text-jobs-{font_size}.png'))
        assert not bridge.commands
    finally:
        if dialog: dialog.close(); dialog.deleteLater()
        set_visual_style(profile); apply_appearance(app, previous); app.processEvents()


def test_long_write_allows_reads_and_revision_never_goes_backwards(app, tmp_path):
    class SplitClient:
        def __init__(self, *_):
            pass
        def query(self, name, **params):
            if name == "slow":
                time.sleep(.35)
                return {"epoch": "same", "revision": 1}
            return {"epoch": "same", "revision": 2}
        def command(self, name, payload, **options):
            time.sleep(.2)
            return {"epoch": "same", "revision": 3, "request_id": options["request_id"], "result": {}}
    bridge = ServiceBridge(tmp_path, client_factory=SplitClient)
    reads, writes = [], []
    bridge.command("backup", {}, lambda _: writes.append(True))
    bridge.query("state", lambda _: reads.append(True))
    wait(app, lambda: bool(reads))
    assert not writes, "A long write should not queue ordinary reads behind itself"
    bridge.query("slow")
    wait(app, lambda: bool(writes))
    wait(app, lambda: not bridge.callbacks)
    assert bridge.revision == 3
    assert bridge.close()

def test_uncertain_write_recovers_receipt_without_resending(app, tmp_path):
    from management.client import ClientError
    class ReceiptClient:
        calls = []
        def __init__(self, *_):
            pass
        def command(self, name, payload, **options):
            self.calls.append(options["request_id"])
            error = ClientError("connection_lost", "保存响应丢失")
            error.details["request_id"] = options["request_id"]
            raise error
        def query(self, name, **params):
            assert name == "receipt"
            return {"found": True, "receipt": {"epoch": "test", "revision": 1, "request_id": params["request_id"], "result": {"saved": True}}}
    bridge = ServiceBridge(tmp_path, client_factory=ReceiptClient)
    results, errors = [], []
    bridge.command("create", {"title": "合成"}, results.append, errors.append)
    wait(app, lambda: bool(results or errors))
    assert not errors
    assert results[0]["result"]["saved"]
    assert len(ReceiptClient.calls) == 1
    assert bridge.close()

def test_unconfirmed_retry_reuses_id_and_original_snapshot(app, tmp_path):
    from management.client import ClientError
    class RetryClient:
        calls = []
        def __init__(self, *_):
            pass
        def command(self, name, payload, **options):
            self.calls.append(dict(options))
            if len(self.calls) == 1:
                raise ClientError("connection_lost", "暂未取得回执")
            return {"epoch": "test", "revision": 3, "request_id": options["request_id"], "result": {}}
        def query(self, name, **params):
            return {"found": False, "epoch": "test", "revision": 2}
    bridge = ServiceBridge(tmp_path, client_factory=RetryClient)
    bridge.epoch, bridge.revision = "test", 2
    errors, results = [], []
    bridge.command("create", {"title": "同一次合成保存"}, results.append, errors.append)
    wait(app, lambda: bool(errors))
    bridge.revision = 9
    bridge.command("create", {"title": "同一次合成保存"}, results.append, errors.append)
    wait(app, lambda: bool(results))
    first, second = RetryClient.calls
    assert first["request_id"] == second["request_id"]
    assert first["expected_revision"] == second["expected_revision"] == 2
    assert bridge.close()



def test_assistance_sends_explicit_source_set_without_confusing_course_and_sources(app):
    from management.gui_assistant import AssistanceDialog
    class CaptureBridge:
        epoch = "synthetic"
        def __init__(self): self.commands = []
        def command(self, name, payload, callback=None, error=None, **options): self.commands.append((name,payload,options))
        def query(self,name,callback,error=None,**params):
            if name=="settings":callback({"settings":{"ai":{"enabled":True}}})
            elif name=="conversation":callback({"conversation":None,"messages":[]})
    bridge=CaptureBridge()
    dialog=AssistanceDialog(bridge,context_entities=[{"id":"course-1","type":"course","title":"合成课程"}],source_ids=["source-1","source-1"])
    dialog.prompt.setPlainText("请整理这份通知")
    dialog.start_job()
    assert bridge.commands[0][0]=="send_message"
    assert bridge.commands[0][1]=={"scope":{"kind":"course","entity_id":"course-1"},"text":"请整理这份通知","source_ids":["source-1"]}
    assert bridge.commands[0][2]["epoch"]=="synthetic"
    dialog.close()


def test_event_owner_creation_edit_and_clear_preserve_root(app, window):
    client = Client(window.data_dir, autostart=False)
    project = create(client, "合成日程所属项目", kind="project")
    course = create(client, "合成日程所属课程", kind="course")
    window.bridge.query("state")
    wait(app, lambda: not window.bridge.callbacks)
    form = EntityForm(window.bridge, window.capabilities, window, default_type="event")
    form.title_edit.setText("合成活动日程")
    form.set_event_owner(project)
    assert form.owner_button.text() == project["title"]
    assert form.parent_id is None
    assert not form.parent_button.isEnabled()
    save_form(app, form)
    assert form.result() == QDialog.DialogCode.Accepted, form.error_label.text()
    event = client.query("list", type="event")["items"][0]
    assert event["parent_id"] is None
    assert event["data"]["owner_id"] == project["id"]
    edit = EntityForm(window.bridge, window.capabilities, window, entity=event)
    wait(app, lambda: edit.owner_button.text() == project["title"])
    edit.set_event_owner(course)
    save_form(app, edit)
    assert edit.result() == QDialog.DialogCode.Accepted, edit.error_label.text()
    event = client.query("get", id=event["id"])["entity"]
    assert event["data"]["owner_id"] == course["id"]
    assert event["parent_id"] is None
    clear = EntityForm(window.bridge, window.capabilities, window, entity=event)
    clear.clear_event_owner()
    save_form(app, clear)
    assert clear.result() == QDialog.DialogCode.Accepted, clear.error_label.text()
    event = client.query("get", id=event["id"])["entity"]
    assert event["data"].get("owner_id") is None
    assert event["parent_id"] is None

def test_mcp_config_copy_uses_current_data_root_and_no_discovery_token(app, window):
    import tomllib
    from management.gui_workflows import codex_mcp_config
    config = codex_mcp_config(window.data_dir)
    parsed = tomllib.loads(config)["mcp_servers"]["personal_management"]
    assert Path(parsed["command"]).is_file()
    assert parsed["args"] == ["-m", "management", "--mcp", "--data-dir", str(window.data_dir.resolve())]
    assert Path(parsed["env"]["PYTHONPATH"]).is_dir()
    discovery = json.loads((window.data_dir / "runtime.json").read_text(encoding="utf-8"))
    assert discovery["token"] not in config
    dialog = SettingsDialog(window.bridge, window.capabilities, window.data_dir, window)
    dialog.copy_codex_config()
    assert QApplication.clipboard().text() == config
    wait(app, lambda: not window.bridge.callbacks)
    dialog.reject()

def test_frozen_mcp_config_uses_sibling_service_program(app, tmp_path, monkeypatch):
    import tomllib
    from management.gui_workflows import codex_mcp_config
    ui_executable = tmp_path / "PersonalManagement.exe"
    service_executable = tmp_path / "PersonalManagementService.exe"
    service_executable.write_bytes(b"synthetic fixture")
    monkeypatch.setattr(sys, "executable", str(ui_executable))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    data = tmp_path / "selected-data"
    config = tomllib.loads(codex_mcp_config(data))["mcp_servers"]["personal_management"]
    assert Path(config["command"]) == service_executable
    assert config["args"] == ["--mcp", "--data-dir", str(data.resolve())]
    assert "env" not in config



def test_rule_selection_field_roundtrip_uses_plain_comma_text(app):
    from management.gui_forms import FieldEditor
    editor = FieldEditor("task_kind", {"type": "selection", "label": "任务类别"}, ["exam", "project"])
    assert editor.editor.text() == "exam，project"
    assert editor.value() == ["exam", "project"]
    editor.editor.setText(" exam， project, reading ,, ")
    assert editor.value() == ["exam", "project", "reading"]
    editor.editor.setText("，,")
    assert editor.value() is None
    editor.editor.setText(",".join(f"kind{i}" for i in range(31)))
    with pytest.raises(ValueError, match="30"):
        editor.value()
