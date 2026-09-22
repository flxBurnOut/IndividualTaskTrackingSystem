"""Real-service acceptance paths for the redesigned three-workspace UI."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys
import time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import QDate, QMimeData, QPointF, Qt, QUrl, QTimer
from PySide6.QtGui import QDropEvent, QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QDialogButtonBox, QMessageBox
from management.client import Client
from management.gui import MainWindow, STYLESHEET
from management.gui_workspace import CountProgress

@pytest.fixture(scope="session")
def app_v2():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    return app

def wait(app, predicate, timeout=12):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("The native UI did not reach the expected state")

@pytest.fixture
def shell(app_v2, tmp_path):
    root = tmp_path / "synthetic-v2-data"
    process = subprocess.Popen([sys.executable, "-m", "management", "--service", "--data-dir", str(root)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    window = None
    try:
        wait(app_v2, lambda: (root / "runtime.json").exists())
        window = MainWindow(root, client_factory=lambda p: Client(p, autostart=False))
        window.show()
        wait(app_v2, lambda: window.type_map and not window.bridge.callbacks)
        yield window
    finally:
        if window:
            window.review_pending = False
            window.close()
            wait(app_v2, lambda: not window.bridge.thread.isRunning() and not window.bridge.mutation_thread.isRunning())
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)

def create(client, kind, title, data=None, parent_id=None):
    client.state()
    return client.command("create", {"type": kind, "title": title, "parent_id": parent_id, "data": data or {}})["result"]["entity"]

def text_in(widget):
    return "\n".join(label.text() for label in widget.findChildren(QLabel)) + "\n" + "\n".join(button.text() for button in widget.findChildren(QPushButton))

def test_three_sections_six_creation_types_and_removed_toolbar(app_v2, shell):
    assert list(shell.nav_buttons) == ["today", "projects", "reviews"]
    assert [widget.text().split("  ")[0] for widget in shell.nav_buttons.values()] == ["今天", "项目与课程", "复盘"]
    assert shell.review_attention and not shell.review_pending
    assert [action.text() for action in shell.create_menu.actions()] == ["任务", "项目", "课程", "活动", "分类", "目标"]
    buttons = {button.text() for button in shell.findChildren(QPushButton)}
    assert not {"快速记录", "后台结果", "通知", "收藏", "更多操作", "写下本期回顾"} & buttons
    assert not hasattr(shell, "tabs"), "The permanent generic four-tab inspector must be removed"

def test_today_ignores_unscheduled_backlog_and_page_reads_never_create_reviews(app_v2, shell):
    client = Client(shell.data_dir, autostart=False)
    task = create(client, "task", "合成：不属于每日计划的待办")
    shell.today_page.refresh()
    wait(app_v2, lambda: not shell.bridge.callbacks)
    assert task["title"] not in text_in(shell.today_page)
    assert not shell.today_page.has_plan
    before = client.query("state")["counts"]
    for section in ("reviews", "today", "projects", "reviews"):
        shell.navigate(section)
        wait(app_v2, lambda: not shell.bridge.callbacks)
    after = client.query("state")["counts"]
    assert before == after
    assert after.get("checkin", 0) == 0
    assert after.get("review", 0) == 0

def test_today_plan_to_review_has_two_choices_and_no_duplicate_questionnaires(app_v2, shell):
    client = Client(shell.data_dir, autostart=False)
    day = QDate.currentDate().toString("yyyy-MM-dd")
    first = create(client, "task", "合成：完成一项明确练习", {"completion_gate": "完成两题并核对"})
    second = create(client, "task", "合成：整理一个要点", {"completion_gate": "写下一个可核对要点"})
    client.command("create_plan", {"date": day, "mode": "no_precise_time", "blocks": [{"target_id": first["id"], "minutes": 20}, {"target_id": second["id"], "minutes": 15}]})
    shell.navigate("today")
    wait(app_v2, lambda: shell.today_page.has_plan and not shell.bridge.callbacks)
    assert first["title"] in text_in(shell.today_page)
    assert not {"完成", "未完成"} & {b.text() for b in shell.today_page.findChildren(QPushButton)}
    shell.open_review(day)
    wait(app_v2, lambda: first["id"] in shell.review_page.item_buttons and not shell.bridge.callbacks)
    buttons = shell.review_page.item_buttons[first["id"]]
    assert set(buttons) == {"done", "incomplete"}
    QTest.mouseClick(buttons["done"], Qt.MouseButton.LeftButton)
    assert shell.review_page.confirm_button.isEnabled()
    QTest.mouseClick(shell.review_page.confirm_button, Qt.MouseButton.LeftButton)
    wait(app_v2, lambda: not shell.bridge.callbacks and not shell.review_page.confirm_button.isEnabled())
    review = client.query("daily_review", date=day)
    assert {key: review["summary"][key] for key in ("total", "done", "incomplete", "unreported")} == {"total": 2, "done": 1, "incomplete": 0, "unreported": 1}
    facts = client.query("list", type="feedback")["items"]
    assert len(facts) == 1 and facts[0]["data"]["dimensions"] == {"completion": "done"}
    for _ in range(3):
        shell.open_review(day)
        wait(app_v2, lambda: not shell.bridge.callbacks)
    assert client.query("list", type="feedback")["total"] == 1
    assert client.query("list", type="checkin")["total"] == 0

def test_hierarchy_browse_and_move_keep_identity_and_reject_cycle(app_v2, shell):
    client = Client(shell.data_dir, autostart=False)
    project = create(client, "project", "合成项目甲")
    child = create(client, "project", "合成子项目", parent_id=project["id"])
    destination = create(client, "project", "合成项目乙")
    shell.navigate("projects")
    wait(app_v2, lambda: not shell.bridge.callbacks)
    tree = shell.workspace_page.tree
    parent_item = next(item for item in tree.iter_items() if (item.data(0, Qt.ItemDataRole.UserRole) or {}).get("id") == project["id"])
    parent_item.setExpanded(True)
    wait(app_v2, lambda: any((item.data(0, Qt.ItemDataRole.UserRole) or {}).get("id") == child["id"] for item in tree.iter_items()) and not shell.bridge.callbacks)
    tree.request_move(project, child)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    assert client.query("get", id=project["id"])["entity"]["parent_id"] is None
    assert shell.notice.isVisible()
    tree.request_move(child, destination)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    moved = client.query("get", id=child["id"])["entity"]
    assert moved["id"] == child["id"]
    assert moved["parent_id"] == destination["id"]
    shell.workspace_page.open_object(destination)
    wait(app_v2, lambda: shell.workspace_page.current_entity["id"] == destination["id"] and not shell.bridge.callbacks)
    assert child["title"] in text_in(shell.workspace_page.content)

def test_file_drop_saves_copy_and_original_removal_does_not_break_open(app_v2, shell, tmp_path, monkeypatch):
    client = Client(shell.data_dir, autostart=False)
    course = create(client, "course", "合成课程")
    original = tmp_path / "synthetic-original.txt"
    original.write_text("only synthetic test material", encoding="utf-8")
    shell.navigate("projects")
    shell.workspace_page.open_object(course)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(original))])
    event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    shell.workspace_page.dropEvent(event)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    result = client.query("object_workspace", id=course["id"])
    assert len(result["files"]) == 1
    file = result["files"][0]
    assert not file["reference_only"] and file["exists"]
    assert file["data"]["managed_copy"]
    assert original.read_text(encoding="utf-8") == "only synthetic test material"
    original.unlink()
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    shell.workspace_page.open_file(file["id"])
    wait(app_v2, lambda: bool(opened))
    assert Path(opened[0]) != original
    assert Path(opened[0]).read_text(encoding="utf-8") == "only synthetic test material"

def test_progress_uses_explicit_count_and_unknown_never_becomes_failed(app_v2):
    card = CountProgress()
    card.set_counts({"total_tasks": 6, "done_tasks": 2, "incomplete_tasks": 1, "unknown_tasks": 3})
    assert card.bar.maximum() == 6 and card.bar.value() == 2
    assert card.count.text() == "明确完成 2 / 6 项"
    assert "3 项尚未反馈" in card.explanation.text()
    assert "%" not in card.count.text()
    card.set_counts({"total_tasks": 0, "done_tasks": 0, "unknown_tasks": 0})
    assert card.bar.isHidden()
    card.set_counts({"total_tasks": None, "done_tasks": 0})
    assert card.bar.isHidden()
    assert "尚无可计算进度" in card.count.text()



def complete_modal_form(app, edit):
    from management.gui_forms import EntityForm
    failures = []
    def interact():
        dialog = QApplication.activeModalWidget()
        if not isinstance(dialog, EntityForm):
            failures.append("Expected a typed entity form")
            if dialog:
                dialog.reject()
            return
        try:
            edit(dialog)
            QTest.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Save), Qt.MouseButton.LeftButton)
        except Exception as error:
            failures.append(str(error))
            dialog.reject()
    QTimer.singleShot(50, interact)
    return failures

def test_course_context_creates_fixed_event_without_tree_parent(app_v2, shell):
    client = Client(shell.data_dir, autostart=False)
    course = create(client, "course", "合成：上下文课程")
    shell.bridge.query("state")
    wait(app_v2, lambda: not shell.bridge.callbacks)
    def edit(dialog):
        assert dialog.event_owner_id == course["id"]
        assert dialog.parent_id is None
        dialog.title_edit.setText("合成：课程固定安排")
        for key, value in (("start", "10:00"), ("end", "11:00")):
            dialog.fields[key].enabled.setChecked(True)
            dialog.fields[key].editor.setText(value)
        dialog.fields["date"].enabled.setChecked(True)
        dialog.fields["date"].editor.setDate(QDate.currentDate())
        dialog.fields["hard"].enabled.setChecked(True)
        dialog.fields["hard"].editor.setChecked(True)
    failures = complete_modal_form(app_v2, edit)
    shell.create_entity("event", course)
    assert not failures
    wait(app_v2, lambda: not shell.bridge.callbacks)
    event = client.query("list", type="event")["items"][0]
    assert event["parent_id"] is None and event["data"]["owner_id"] == course["id"]
    assert event["data"]["start"] == "10:00"
    assert event["data"]["hard"] is True
    assert len(shell.create_menu.actions()) == 6

def test_extension_appears_only_in_matching_context_and_can_be_created_edited(app_v2, shell):
    client = Client(shell.data_dir, autostart=False)
    project = create(client, "project", "合成：可扩展项目")
    module = {"id": "workspaceextra", "version": 1, "types": [{"id": "workspaceextra.observation", "label": "观察记录", "section": "projects", "parent_types": ["project"], "fields": [{"id": "sample_count", "label": "样本数量", "type": "integer"}]}]}
    client.command("install_module", {"manifest": module})
    shell.load_capabilities()
    wait(app_v2, lambda: "workspaceextra.observation" in shell.type_map and not shell.bridge.callbacks)
    shell.navigate("projects")
    shell.workspace_page.open_object(project)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    add = next(button for button in shell.workspace_page.content.findChildren(QPushButton) if button.text() == "＋ 添加")
    action = next(action for action in add.menu().actions() if action.text() == "观察记录")
    def fill(dialog):
        dialog.title_edit.setText("合成：新观察")
        dialog.fields["sample_count"].enabled.setChecked(True)
        dialog.fields["sample_count"].editor.setValue(3)
    failures = complete_modal_form(app_v2, fill)
    action.trigger()
    assert not failures
    wait(app_v2, lambda: not shell.bridge.callbacks)
    record = client.query("list", type="workspaceextra.observation")["items"][0]
    assert record["parent_id"] == project["id"] and record["data"]["sample_count"] == 3
    assert "观察记录" not in [action.text() for action in shell.create_menu.actions()]
    tree = shell.workspace_page.tree
    parent = next(item for item in tree.iter_items() if (item.data(0, Qt.ItemDataRole.UserRole) or {}).get("id") == project["id"])
    parent.setExpanded(True)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    assert any((item.data(0, Qt.ItemDataRole.UserRole) or {}).get("id") == record["id"] for item in tree.iter_items())
    shell.workspace_page.open_object(record)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    assert "样本数量" in text_in(shell.workspace_page.content)
    def change(dialog):
        dialog.fields["sample_count"].editor.setValue(4)
    failures = complete_modal_form(app_v2, change)
    shell.edit_entity(record)
    assert not failures
    assert client.query("get", id=record["id"])["entity"]["data"]["sample_count"] == 4

def test_files_paginate_inside_the_owner_and_do_not_displace_tasks(app_v2, shell, tmp_path):
    client = Client(shell.data_dir, autostart=False)
    project = create(client, "project", "合成：文件分页项目")
    task = create(client, "task", "合成：始终可见的待办", parent_id=project["id"])
    for index in range(55):
        path = tmp_path / f"synthetic-file-{index:03d}.txt"
        path.write_text("synthetic", encoding="utf-8")
        client.command("attach_local_file", {"path": str(path), "owner_id": project["id"]})
    shell.navigate("projects")
    shell.workspace_page.open_object(project)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    first = client.query("object_workspace", id=project["id"])
    assert first["children_total"] == 1 and first["children"][0]["id"] == task["id"]
    assert len(first["files"]) == 50 and first["files_next_offset"] == 50
    next_button = next(button for button in shell.workspace_page.content.findChildren(QPushButton) if button.text() == "下一页文件")
    QTest.mouseClick(next_button, Qt.MouseButton.LeftButton)
    wait(app_v2, lambda: shell.workspace_page.files_offset == 50 and not shell.bridge.callbacks)
    text = text_in(shell.workspace_page.content)
    assert "synthetic-file-054.txt" in text
    assert "第 51 至 55" in text

def test_inline_due_reminders_jump_to_daily_or_weekly_without_creating_questionnaires(app_v2, shell):
    client = Client(shell.data_dir, autostart=False)
    today = QDate.currentDate()
    client.command("set_review_preferences", {"timezone": "Asia/Shanghai", "daily": {"enabled": True, "time": "00:00"}, "weekly": {"enabled": True, "weekday": today.dayOfWeek() - 1, "time": "00:00"}})
    wait(app_v2, lambda: client.query("list", type="notification")["total"] >= 2, timeout=23)
    shell.navigate("today")
    wait(app_v2, lambda: not shell.bridge.callbacks)
    buttons = shell.today_page.findChildren(QPushButton)
    weekly = next(button for button in buttons if button.text() == "查看每周回顾")
    daily = next(button for button in buttons if button.text() == "前往每日复盘")
    QTest.mouseClick(weekly, Qt.MouseButton.LeftButton)
    wait(app_v2, lambda: shell.section == "reviews" and not shell.bridge.callbacks)
    assert shell.review_page.tabs.currentIndex() == 1
    shell.navigate("today")
    wait(app_v2, lambda: not shell.bridge.callbacks)
    daily = next(button for button in shell.today_page.findChildren(QPushButton) if button.text() == "前往每日复盘")
    QTest.mouseClick(daily, Qt.MouseButton.LeftButton)
    wait(app_v2, lambda: shell.section == "reviews" and not shell.bridge.callbacks)
    assert shell.review_page.tabs.currentIndex() == 0
    assert client.query("list", type="checkin")["total"] == 0
    assert client.query("list", type="feedback")["total"] == 0

def test_attention_marker_does_not_trigger_unsaved_close_confirmation(app_v2, shell, monkeypatch):
    assert shell.review_attention and not shell.review_pending
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: pytest.fail("No unsaved selection exists"))
    shell.close()
    wait(app_v2, lambda: not shell.bridge.thread.isRunning())



def test_today_warning_pagination_can_reach_all_returned_risks(app_v2):
    from management.gui_today import TodayPage
    class WarningBridge:
        def __init__(self):
            self.offsets = []
        def query(self, name, callback, error=None, **params):
            if name == "daily_review":
                callback({"has_plan": False, "summary": {"total": 0, "unreported": 0}})
            elif name == "today":
                callback({"events": [], "notifications": [], "unknowns": []})
            elif name == "warnings":
                offset = params.get("offset", 0)
                self.offsets.append(offset)
                end = min(8, offset + params["limit"])
                callback({"items": [{"title": f"合成警戒 {i}", "reason": "明确待核对事项"} for i in range(offset, end)], "total": 8, "next_offset": end if end < 8 else None})
    bridge = WarningBridge()
    page = TodayPage(bridge)
    page.show()
    page.refresh()
    assert len(page.warning_result["items"]) == 3
    button = next(button for button in page.findChildren(QPushButton) if button.text() == "更多警戒")
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert page.warning_offset == 3
    page.set_warning_page(page.warning_result["next_offset"])
    assert page.warning_offset == 6
    assert [item["title"] for item in page.warning_result["items"]] == ["合成警戒 6", "合成警戒 7"]
    assert bridge.offsets == [0, 3, 6]
    page.close()
