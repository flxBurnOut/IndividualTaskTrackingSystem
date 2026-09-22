"""Reversible task deletion across lists, existing plans, feedback and UI."""
import copy
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import uuid
import pytest
from management.core import Core
from management.schemas import BusinessError


def receipt(core, name, payload, request_id=None):
    state = core.query("state")
    return core.command(name, payload, request_id=request_id or str(uuid.uuid4()),
                        epoch=state["epoch"], expected_revision=state["revision"])


def command(core, name, payload):
    return receipt(core, name, payload)["result"]


def create(core, title="Synthetic test task", **values):
    return command(core, "create", {"type": "task", "title": title, **values})["entity"]


def remove(core, task):
    return command(core, "delete_task", {"id": task["id"], "version": task["version"]})["entity"]


@pytest.fixture
def core(tmp_path):
    return Core(tmp_path / "deletion")


def test_delete_hides_from_today_and_project_and_undo_restores(core):
    course = command(core, "create", {"type": "course", "title": "Synthetic course"})["entity"]
    task = create(core, parent_id=course["id"], data={"scheduled_date": "2030-01-07"})
    assert core.query("daily_tasks", date="2030-01-07")["total"] == 1
    saved = receipt(core, "delete_task", {"id": task["id"], "version": task["version"]})
    removed = saved["result"]["entity"]
    assert removed["archived"] and removed["status"] == task["status"] and removed["data"] == task["data"]
    assert core.query("daily_tasks", date="2030-01-07")["total"] == 0
    assert core.query("object_workspace", id=course["id"])["children"] == []
    assert core.query("list", type="task")["total"] == 0
    restored = command(core, "undo", {"request_id": saved["request_id"]})["entities"][0]
    assert not restored["archived"] and restored["version"] > removed["version"]
    assert core.query("daily_tasks", date="2030-01-07")["total"] == 1


def test_delete_preserves_plan_feedback_links_and_only_active_rows_can_be_reviewed(core):
    a = create(core)
    b = create(core, "Still active")
    link = command(core, "link", {"source_id": a["id"], "target_id": b["id"], "kind": "references"})
    plan = command(core, "create_plan", {"date": "2030-01-07", "mode": "no_precise_time", "blocks": [{"target_id": a["id"]}, {"target_id": b["id"]}]})["entity"]
    feedback = command(core, "record_feedback", {"target_id": a["id"], "business_date": "2030-01-07", "dimensions": {"completion": "done"}, "source_text": "Explicit synthetic completion."})["entity"]
    before = core.query("get", id=a["id"])
    removed = remove(core, a)
    assert core.query("get", id=plan["id"])["entity"] == plan
    assert core.query("get", id=feedback["id"])["entity"] == feedback
    assert core.query("get", id=a["id"])["links"] == before["links"]
    daily = core.query("daily_review", date="2030-01-07")
    item = next(i for i in daily["items"] if i["target_id"] == a["id"])
    assert item["target_archived"] and not item["can_review"] and item["result"] == "done"
    assert daily["summary"]["done"] == 1 and daily["summary"]["archived"] == 1 and daily["summary"]["pending_review"] == 1
    with pytest.raises(BusinessError) as error:
        command(core, "submit_daily_review", {"date": "2030-01-07", "plan_id": plan["id"], "plan_version": plan["version"], "answers": [{"target_id": a["id"], "result": "incomplete"}]})
    assert error.value.code == "task_deleted"
    saved = command(core, "submit_daily_review", {"date": "2030-01-07", "plan_id": plan["id"], "plan_version": plan["version"], "answers": [{"target_id": b["id"], "result": "incomplete"}]})
    assert saved["changed_count"] == 1
    week = core.query("weekly_review", start="2030-01-07", end="2030-01-13")
    assert week["summary"]["planned"] == 2 and week["summary"]["done"] == 1 and week["summary"]["archived"] == 1
    restored = command(core, "restore_task", {"id": a["id"], "version": removed["version"]})["entity"]
    assert not restored["archived"] and core.query("daily_review", date="2030-01-07")["items"][0]["can_review"]


def test_deleted_unreported_task_stays_unknown_but_no_longer_pending(core):
    task = create(core)
    command(core, "add_to_plan", {"date": "2030-01-07", "target_id": task["id"], "plan_id": None, "plan_version": None})
    remove(core, task)
    summary = core.query("daily_review", date="2030-01-07")["summary"]
    assert summary["unreported"] == 1 and summary["pending_review"] == 0 and summary["done"] == 0
    assert core.query("list", type="feedback")["total"] == 0


def test_version_receipt_replay_and_semantic_reuse_are_bounded(core):
    task = create(core)
    payload = {"id": task["id"], "version": task["version"]}
    rid = str(uuid.uuid4())
    first = receipt(core, "delete_task", payload, rid)
    replay = receipt(core, "delete_task", payload, rid)
    assert replay["replayed"] and replay["result"] == first["result"]
    removed = first["result"]["entity"]
    with pytest.raises(BusinessError) as error:
        command(core, "restore_task", payload)
    assert error.value.code == "entity_conflict"
    reused = command(core, "delete_task", {"id": task["id"], "version": removed["version"]})
    assert reused["reused"] and reused["entity"]["version"] == removed["version"]
    restored = command(core, "restore_task", {"id": task["id"], "version": removed["version"]})["entity"]
    assert command(core, "restore_task", {"id": task["id"], "version": restored["version"]})["reused"]


def test_active_children_protected_and_archived_parent_blocks_restore(core):
    parent = create(core)
    child = command(core, "create", {"type": "checklist", "title": "Child evidence", "parent_id": parent["id"]})["entity"]
    with pytest.raises(BusinessError) as error:
        remove(core, parent)
    assert error.value.code == "active_children"
    assert not core.query("get", id=parent["id"])["entity"]["archived"]
    assert not core.query("get", id=child["id"])["entity"]["archived"]
    project = command(core, "create", {"type": "project", "title": "Synthetic project"})["entity"]
    task = create(core, parent_id=project["id"])
    removed = remove(core, task)
    command(core, "archive", {"id": project["id"], "version": project["version"], "archived": True})
    with pytest.raises(BusinessError) as error:
        command(core, "restore_task", {"id": removed["id"], "version": removed["version"]})
    assert error.value.code == "archived_parent"


def test_non_task_and_untyped_versions_rejected(core):
    project = command(core, "create", {"type": "project", "title": "Project"})["entity"]
    for payload in ({"id": project["id"], "version": project["version"]}, {"id": project["id"], "version": True}):
        with pytest.raises(BusinessError) as error:
            command(core, "delete_task", payload)
        assert error.value.code == "validation"


@pytest.fixture(scope="session")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def gui_task(**fields):
    return {"id": "task-a", "type": "task", "title": "Synthetic task", "version": 1, "status": "active", "data": {}, **fields}


def test_delete_dialog_is_explicit_double_click_safe_and_restores_latest_version(app):
    from management.gui_forms import DeleteTaskDialog
    from test_ux_workflows_v2 import ControlledBridge
    bridge = ControlledBridge()
    saved = []
    dialog = DeleteTaskDialog(bridge, gui_task(), on_saved=saved.append)
    try:
        assert bridge.commands == []
        dialog.action_button.click(); dialog.action_button.click()
        assert len(bridge.commands) == 1 and bridge.commands[-1]["name"] == "delete_task"
        assert bridge.commands[-1]["payload"] == {"id": "task-a", "version": 1}
        removed = gui_task(archived=True, version=2)
        bridge.commands[-1]["callback"]({"result": {"entity": removed}})
        assert dialog.action_button.text() == "撤销删除" and "原计划" in dialog.explanation.text()
        dialog.action_button.click()
        assert bridge.commands[-1]["name"] == "restore_task" and bridge.commands[-1]["payload"]["version"] == 2
        bridge.commands[-1]["callback"]({"result": {"entity": gui_task(archived=False, version=3)}})
        assert len(saved) == 2 and not dialog.entity["archived"]
    finally:
        dialog.saving = False; dialog.close(); dialog.deleteLater()


def test_task_detail_and_edit_form_have_clear_delete_entry(app):
    from management.gui_workspace import TaskDetailDialog
    from management.gui_forms import EntityForm
    from management.schemas import TYPES
    from test_ux_workflows_v2 import ControlledBridge
    bridge = ControlledBridge()
    detail = TaskDetailDialog(bridge, gui_task())
    form = EntityForm(bridge, {"types": TYPES}, entity=gui_task())
    try:
        assert detail.delete_button.text() == "删除任务"
        assert form.delete_button.text() == "删除任务"
        assert bridge.commands == []
    finally:
        detail.close(); detail.deleteLater(); form.close(); form.deleteLater()


def test_deleted_review_rows_read_only_and_no_hidden_feedback_submission(app):
    from management.gui_review import ReviewPage
    from test_review_ui_v2 import ControlledBridge, daily, load
    bridge = ControlledBridge(); page = ReviewPage(bridge)
    value = daily(items=[{"target_id": "task-a", "title": "Removed task", "target_version": 2, "result": None,
                         "target_archived": True, "can_review": False, "available": True},
                        {"target_id": "task-b", "title": "Active task", "target_version": 1, "result": None}])
    try:
        load(page, bridge, value)
        assert "已删除" in page.item_labels["task-a"].text()
        assert not any(button.isEnabled() for button in page.item_buttons["task-a"].values())
        page._choose("task-a", "done")
        assert page._choices == {}
        page._choose("task-b", "incomplete"); page.submit()
        assert bridge.commands[-1]["payload"]["answers"] == [{"target_id": "task-b", "result": "incomplete"}]
    finally:
        page._submitting_date = None; page.close(); page.deleteLater()


def test_direct_feedback_and_old_checkin_cannot_write_to_deleted_task(core):
    a = create(core)
    b = create(core, "Remaining task")
    plan = command(core, "create_plan", {"date": "2030-01-07", "mode": "no_precise_time", "blocks": [{"target_id": a["id"]}, {"target_id": b["id"]}]})["entity"]
    checkin = command(core, "create_checkin", {"date": "2030-01-07"})["entity"]
    remove(core, a)
    with pytest.raises(BusinessError) as error:
        command(core, "record_feedback", {"target_id": a["id"], "business_date": "2030-01-07", "dimensions": {"completion": "done"}, "source_text": "Synthetic explicit report"})
    assert error.value.code == "task_deleted"
    question = next(q for q in checkin["data"]["questions"] if q["target_id"] == a["id"])
    with pytest.raises(BusinessError) as error:
        command(core, "respond_checkin", {"id": checkin["id"], "answers": [{"question_id": question["id"], "dimensions": {"completion": "done"}}], "source_text": "Synthetic test"})
    assert error.value.code == "task_deleted"
    assert core.query("list", type="feedback")["total"] == 0
    assert core.query("get", id=checkin["id"])["entity"] == checkin
    next_checkin = command(core, "create_checkin", {"date": "2030-01-07"})["entity"]
    assert [q["target_id"] for q in next_checkin["data"]["questions"]] == [b["id"]]
    with pytest.raises(BusinessError) as error:
        command(core, "create_checkin", {"date": "2030-01-07", "target_ids": [a["id"]]})
    assert error.value.code == "task_deleted"


def test_deleted_recurring_occurrence_is_not_regenerated(core):
    event = command(core, "create", {"type": "event", "title": "Synthetic weekly class", "data": {"date": "2030-01-08", "recurrence": "weekly", "time_kind": "date_only"}})["entity"]
    rule = command(core, "set_recurring_rule", {"anchor_id": event["id"], "title": "Prepare", "content": "Explicit preparation", "completion_gate": "Check one exercise", "days_before": 1, "estimated_minutes": None, "enabled": True, "materialize_date": "2030-01-07"})
    task = rule["materialization"]["created"][0]
    removed = remove(core, task)
    repeated = command(core, "materialize_recurring", {"rule_id": rule["entity"]["id"], "start": "2030-01-07", "end": "2030-01-07"})
    assert repeated["created_count"] == 0 and len(repeated["existing"]) == 1
    assert core.query("get", id=task["id"])["entity"] == removed
    assert core.query("daily_tasks", date="2030-01-07")["total"] == 0
    # Deleting one instance leaves future occurrences and the rule intact.
    future = command(core, "materialize_recurring", {"rule_id": rule["entity"]["id"], "start": "2030-01-14", "end": "2030-01-14"})
    assert future["created_count"] == 1


def test_delete_restore_error_preserves_entity_and_explains_version_conflict(app):
    from management.gui_forms import DeleteTaskDialog
    from test_ux_workflows_v2 import ControlledBridge
    bridge = ControlledBridge()
    dialog = DeleteTaskDialog(bridge, gui_task())
    try:
        dialog.action_button.click()
        bridge.commands[-1]["error"]({"code": "entity_conflict", "message": "这条记录已修改"})
        assert dialog.entity["version"] == 1 and dialog.action_button.isEnabled()
        assert "重新读取" in dialog.error_label.text()
        assert "已修改" in dialog.error_label.text()
        assert not dialog.entity.get("archived")
    finally:
        dialog.close(); dialog.deleteLater()


def test_deleted_review_change_detects_stale_selected_answer(app):
    from management.gui_review import ReviewPage
    from test_review_ui_v2 import ControlledBridge, daily, load
    bridge = ControlledBridge(); page = ReviewPage(bridge)
    try:
        value = daily(); load(page, bridge, value); page._choose("task-a", "done")
        deleted = copy.deepcopy(value)
        deleted["items"][0].update(target_archived=True, can_review=False, target_version=2)
        page.refresh(); bridge.deliver("daily_review", deleted)
        assert not page.confirm_button.isEnabled() and page._date in page._conflicts
        assert bridge.commands == []
    finally:
        page.close(); page.deleteLater()


def test_archived_task_detail_only_offers_restore_and_no_plan_entry(app):
    from management.gui_workspace import TaskDetailDialog
    from test_ux_workflows_v2 import ControlledBridge
    bridge = ControlledBridge()
    detail = TaskDetailDialog(bridge, gui_task(archived=True, version=2), on_edit=lambda value: None)
    try:
        assert detail.delete_button.text() == "恢复任务"
        assert not detail.edit_button.isEnabled() and not hasattr(detail, "join_plan_button")
        assert bridge.queries == [] and bridge.commands == []
    finally:
        detail.close(); detail.deleteLater()
