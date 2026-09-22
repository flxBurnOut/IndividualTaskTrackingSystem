"""Observable business behavior with isolated synthetic data only."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from management.core import Core
from management.schemas import BusinessError


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.core = Core(self.root / "space")

    def cmd(self, name, payload, *, core=None, request_id=None, state=None):
        core = core or self.core
        state = state or core.query("state")
        return core.command(name, payload, request_id=request_id or str(uuid.uuid4()), epoch=state["epoch"], expected_revision=state["revision"])

    def create(self, type="task", title="Synthetic task", **kwargs):
        return self.cmd("create", {"type": type, "title": title, **kwargs})["result"]["entity"]

    def assert_command_error(self, code, name, payload, **kwargs):
        before = self.core.query("state")["revision"]
        with self.assertRaises(BusinessError) as raised:
            self.cmd(name, payload, **kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.core.query("state")["revision"], before)

    def plan(self, blocks, **kwargs):
        return self.cmd("create_plan", {"date": "2030-01-10", "mode": "standard", "blocks": blocks, **kwargs})["result"]["entity"]

    def feedback(self, target, dimensions, **kwargs):
        return self.cmd("record_feedback", {"target_id": target["id"], "business_date": "2030-01-09", "dimensions": dimensions, "source_text": "Synthetic explicit response", **kwargs})["result"]["entity"]

    def test_business_space_starts_empty_without_user_records(self):
        state = self.core.query("state")
        self.assertEqual(state["counts"], {})
        self.assertEqual(state["revision"], 0)
        self.assertEqual(self.core.query("list")["total"], 0)
        self.assertFalse(self.core.query("settings")["settings"]["ai"]["enabled"])
        self.assertEqual(self.core.query("jobs")["items"], [])

    def test_persistent_second_client_and_stale_revision(self):
        stale = self.core.query("state")
        task = self.create()
        another = Core(self.root / "space")
        self.assertEqual(another.query("get", id=task["id"])["entity"]["title"], task["title"])
        self.assert_command_error("revision_conflict", "create", {"type": "task", "title": "Stale"}, core=another, state=stale)
        self.assertEqual(self.core.query("list")["total"], 1)

    def test_idempotency_retry_survives_later_commits_but_payload_change_rejected(self):
        state, rid = self.core.query("state"), str(uuid.uuid4())
        payload = {"type": "task", "title": "Once"}
        first = self.cmd("create", payload, request_id=rid, state=state)
        self.create(title="Later")
        retry = self.cmd("create", payload, request_id=rid, state=state)
        self.assertTrue(retry["replayed"])
        self.assertEqual(retry["result"], first["result"])
        self.assert_command_error("idempotency_conflict", "create", {**payload, "title": "Different"}, request_id=rid)
        self.assertEqual(self.core.query("list")["total"], 2)

    def test_same_core_concurrent_requests_have_one_revision_winner(self):
        state = self.core.query("state")
        def call(i):
            try:
                return self.cmd("create", {"type": "task", "title": str(i)}, state=state)
            except BusinessError as e:
                return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(call, [1, 2]))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertIn("revision_conflict", results)

    def test_entity_version_rejects_old_editor_even_with_fresh_global_state(self):
        task = self.create()
        self.cmd("update", {"id": task["id"], "version": 1, "patch": {"title": "New"}})
        self.assert_command_error("entity_conflict", "update", {"id": task["id"], "version": 1, "patch": {"title": "Old editor"}})

    def test_parent_cycle_wrong_parent_and_archive_children(self):
        a = self.create("domain", "A")
        b = self.create("domain", "B", parent_id=a["id"])
        self.assert_command_error("cycle", "move", {"id": a["id"], "version": 1, "parent_id": b["id"]})
        self.assert_command_error("active_children", "archive", {"id": a["id"], "version": 1, "archived": True})
        task = self.create()
        self.assert_command_error("parent_type", "move", {"id": a["id"], "version": 1, "parent_id": task["id"]})

    def test_depth_checks_entire_moved_subtree(self):
        chain = [self.create("domain", "Root")]
        for i in range(31):
            chain.append(self.create("domain", str(i), parent_id=chain[-1]["id"]))
        self.assert_command_error("depth", "create", {"type": "domain", "title": "Too deep", "parent_id": chain[-1]["id"]})
        new_parent = self.create("domain", "New root")
        self.assert_command_error("depth", "move", {"id": chain[0]["id"], "version": 1, "parent_id": new_parent["id"]})

    def test_dependency_dag_and_checklist_promotion(self):
        a, b = self.create(title="A"), self.create(title="B")
        self.cmd("link", {"source_id": a["id"], "target_id": b["id"], "kind": "depends_on"})
        self.assert_command_error("cycle", "link", {"source_id": b["id"], "target_id": a["id"], "kind": "depends_on"})
        check = self.create("checklist", "Small step", parent_id=a["id"])
        self.assert_command_error("validation", "link", {"source_id": check["id"], "target_id": b["id"], "kind": "depends_on"})
        promoted = self.cmd("promote_checklist", {"id": check["id"], "version": 1})["result"]["entity"]
        self.assertEqual(promoted["id"], check["id"])
        self.assertEqual(promoted["type"], "task")

    def test_feedback_dimensions_are_independent_and_unknown_minutes_omitted(self):
        task = self.create()
        feedback = self.feedback(task, {"attendance": "attended", "submission": "not_submitted"})
        self.assertEqual(feedback["data"]["dimensions"], {"attendance": "attended", "submission": "not_submitted"})
        self.assertNotIn("actual_minutes", feedback["data"]["dimensions"])
        review = self.core.query("review", start="2030-01-09", end="2030-01-10")
        self.assertEqual(review["metrics"]["completed_daily_targets"], 0)
        self.assertEqual(review["metrics"]["mastery_verified"], 0)

    def test_cross_midnight_checkin_answer_uses_question_business_date(self):
        task = self.create()
        check = self.cmd("create_checkin", {"date": "2030-01-09", "target_ids": [task["id"]]})["result"]["entity"]
        self.assertEqual(self.core.query("list", type="feedback")["total"], 0)
        answered = self.cmd("respond_checkin", {"id": check["id"], "answers": [{"question_id": check["data"]["questions"][0]["id"], "dimensions": {"completion": "partial"}}], "source_text": "Reply after midnight"})["result"]
        self.assertEqual(answered["business_date"], "2030-01-09")
        self.assertEqual(answered["entities"][0]["data"]["business_date"], "2030-01-09")
        self.assert_command_error("validation", "respond_checkin", {"id": check["id"], "answers": [{"question_id": "wrong", "dimensions": {"completion": "done"}}], "source_text": "Not this question"})

    def test_feedback_correction_preserves_prior_and_counts_latest_minutes_once(self):
        task = self.create()
        first = self.feedback(task, {"actual_minutes": 40, "viewing": "viewed"})
        corrected = self.feedback(task, {"actual_minutes": 25}, supersedes_id=first["id"])
        self.assertEqual(self.core.query("list", type="feedback")["total"], 2)
        self.assertEqual(corrected["data"]["supersedes_id"], first["id"])
        self.assertEqual(self.core.query("review", start="2030-01-09", end="2030-01-09")["metrics"]["actual_minutes"], 25)
        other = self.create()
        self.assert_command_error("validation", "record_feedback", {"target_id": other["id"], "business_date": "2030-01-09", "dimensions": {"completion": "done"}, "source_text": "Correction", "supersedes_id": first["id"]})

    def test_feedback_invalid_dimensions_and_missing_source_are_atomic(self):
        task = self.create()
        self.assert_command_error("validation", "record_feedback", {"target_id": task["id"], "business_date": "2030-01-09", "dimensions": {"actual_minutes": -1}, "source_text": "No"})
        self.assert_command_error("source_required", "record_feedback", {"target_id": task["id"], "business_date": "2030-01-09", "dimensions": {"completion": "done"}, "source_text": ""})
        self.assertEqual(self.core.query("list", type="feedback")["total"], 0)

    def test_hard_event_overlap_and_unknown_event_time(self):
        task = self.create()
        self.create("event", "Class", data={"date": "2030-01-10", "start": "10:00", "end": "11:00", "hard": True})
        self.assert_command_error("schedule_conflict", "create_plan", {"date": "2030-01-10", "blocks": [{"target_id": task["id"], "start": "10:30", "end": "11:30"}]})
        self.create("event", "Unknown event", data={"date": "2030-01-10", "time_kind": "unknown"})
        self.assert_command_error("uncertain_time", "create_plan", {"date": "2030-01-10", "blocks": [{"target_id": task["id"], "start": "12:00", "end": "13:00"}]})
        plan = self.plan([{"target_id": task["id"], "minutes": 30}], mode="no_precise_time")
        self.assertTrue(plan["data"]["unknowns"])

    def test_recurrence_exception_only_removes_one_occurrence(self):
        self.create("event", "Weekly", data={"date": "2030-01-03", "start": "10:00", "end": "11:00", "recurrence": "weekly", "exceptions": {"2030-01-10": {"cancelled": True}}})
        self.assertEqual(len(self.core.query("plan_context", date="2030-01-10")["hard_events"]), 0)
        self.assertEqual(len(self.core.query("plan_context", date="2030-01-17")["hard_events"]), 1)

    def test_overnight_event_blocks_following_day(self):
        self.create("event", "Night", data={"date": "2030-01-09", "start": "23:00", "end": "01:00", "timezone": "Asia/Shanghai"})
        events = self.core.query("plan_context", date="2030-01-10")["hard_events"]
        self.assertEqual([(e["start_minute"], e["end_minute"]) for e in events], [(0, 60)])

    def test_completion_gate_and_capacity_not_silently_reduced(self):
        task = self.create(data={"completion_gate": "All three questions"})
        self.create("rule", "Capacity", data={"rule_kind": "capacity", "minutes": 30})
        self.assert_command_error("completion_gate", "create_plan", {"date": "2030-01-10", "blocks": [{"target_id": task["id"], "minutes": 20, "completion_gate": "One question"}]})
        self.assert_command_error("capacity", "create_plan", {"date": "2030-01-10", "blocks": [{"target_id": task["id"], "minutes": 60}]})
        plan = self.plan([{"target_id": task["id"], "minutes": 20}])
        self.assertEqual(plan["data"]["blocks"][0]["completion_gate"], "All three questions")
        self.assertTrue(plan["data"]["soft_estimates"])

    def test_dependency_requires_predecessor_before_dependent(self):
        before, after = self.create(title="Before"), self.create(title="After")
        self.cmd("link", {"source_id": after["id"], "target_id": before["id"], "kind": "depends_on"})
        self.assert_command_error("dependency", "create_plan", {"date": "2030-01-10", "blocks": [{"target_id": after["id"], "minutes": 20}]})
        plan = self.plan([{"target_id": before["id"], "minutes": 20}, {"target_id": after["id"], "minutes": 20}])
        self.assertEqual(len(plan["data"]["blocks"]), 2)

    def test_dependency_chronology_cannot_be_bypassed_by_array_order(self):
        before, after = self.create(title="Before"), self.create(title="After")
        self.cmd("link", {"source_id": after["id"], "target_id": before["id"], "kind": "depends_on"})
        self.assert_command_error("dependency", "create_plan", {"date": "2030-01-10", "blocks": [{"target_id": before["id"], "start": "12:00", "end": "13:00"}, {"target_id": after["id"], "start": "10:00", "end": "11:00"}]})

    def test_plan_cannot_move_fixed_event_itself_to_another_time(self):
        event = self.create("event", "Fixed", data={"date": "2030-01-10", "start": "10:00", "end": "11:00", "hard": True})
        self.assert_command_error("schedule_conflict", "create_plan", {"date": "2030-01-10", "blocks": [{"target_id": event["id"], "start": "14:00", "end": "15:00"}]})

    def test_no_precise_time_rest_and_old_plan_are_preserved(self):
        task = self.create()
        old = self.plan([{"target_id": task["id"], "minutes": 30}], mode="no_precise_time")
        new = self.plan([], mode="rest", supersedes_id=old["id"])
        self.assertEqual(new["data"]["supersedes_id"], old["id"])
        self.assertEqual(len(self.core.query("get", id=old["id"])["entity"]["data"]["blocks"]), 1)
        self.assert_command_error("validation", "create_plan", {"date": "2030-01-10", "mode": "no_precise_time", "blocks": [{"target_id": task["id"], "start": "10:00", "end": "11:00"}]})

    @staticmethod
    def module(version=1):
        return {"id": "research", "version": version, "api_version": 1,
                "fields": [{"target_type": "task", "id": "effort", "label": "Effort", "type": "integer"}],
                "types": [{"id": "research.sample", "label": "Sample", "section": "projects", "parent_types": [None, "project"], "fields": [{"id": "count", "label": "Count", "type": "integer"}]}],
                "rules": [{"target_type": "research.sample", "field": "count", "operator": "min", "value": 1}],
                "workflows": [{"id": "new_sample", "label": "New sample", "action": "create", "defaults": {"type": "research.sample", "title": "New sample", "data": {"count": 1}}}]}

    def test_four_extension_surfaces_share_capabilities_and_business_validation(self):
        self.cmd("install_module", {"manifest": self.module()})
        caps = self.core.query("capabilities")
        self.assertIn("research.sample", {t["id"] for t in caps["types"]})
        self.assert_command_error("rule_conflict", "create", {"type": "research.sample", "title": "Invalid", "data": {"count": 0}})
        sample = self.cmd("run_workflow", {"module_id": "research", "workflow_id": "new_sample", "input": {"title": "Actual sample"}})["result"]["entity"]
        self.assertEqual(sample["title"], "Actual sample")
        self.assertEqual(sample["data"]["count"], 1)
        self.cmd("disable_module", {"id": "research"})
        self.assertEqual(self.core.query("get", id=sample["id"])["entity"]["data"]["count"], 1)
        self.assert_command_error("read_only", "update", {"id": sample["id"], "version": 1, "patch": {"title": "Disabled edit"}})

    def test_disabled_extension_field_cannot_be_mutated_through_builtin_type(self):
        self.cmd("install_module", {"manifest": self.module()})
        task = self.create(data={"effort": 2})
        self.cmd("disable_module", {"id": "research"})
        self.assert_command_error("read_only", "update", {"id": task["id"], "version": 1, "patch": {"data": {"effort": 9}}})

    def test_breaking_extension_field_change_requires_migration(self):
        self.cmd("install_module", {"manifest": self.module()})
        self.create(data={"effort": 2})
        changed = self.module(version=2)
        changed["fields"] = []
        self.assert_command_error("migration_required", "install_module", {"manifest": changed})

    def test_undo_checks_later_entity_changes(self):
        receipt = self.cmd("create", {"type": "task", "title": "Original"})
        task = receipt["result"]["entity"]
        changed = self.cmd("update", {"id": task["id"], "version": 1, "patch": {"title": "Modified"}})
        self.assert_command_error("undo_conflict", "undo", {"request_id": receipt["request_id"]})
        restored = self.cmd("undo", {"request_id": changed["request_id"]})["result"]["entities"][0]
        self.assertEqual(restored["title"], "Original")
        self.assertEqual(restored["version"], 3)

    def test_backup_restore_changes_epoch_and_cancels_pending_jobs(self):
        task = self.create()
        source = self.root / "synthetic.txt"
        source.write_text("Synthetic retained evidence", encoding="utf-8")
        asset = self.cmd("import_asset", {"path": str(source), "source_text": "Synthetic fixture"})["result"]["entity"]
        self.cmd("create_artifact_job", {"kind": "markdown", "relative_path": "report.md", "content": "Synthetic"})
        backup = self.cmd("backup", {})["result"]
        target = self.root / "restored"
        self.cmd("restore_backup", {"path": backup["path"], "target_dir": str(target)})
        restored = Core(target)
        self.assertNotEqual(restored.query("state")["epoch"], self.core.query("state")["epoch"])
        self.assertEqual(restored.query("get", id=task["id"])["entity"]["title"], task["title"])
        self.assertTrue((target / asset["data"]["path"]).is_file())
        self.assertTrue(all(j["status"] == "cancelled" for j in restored.query("jobs")["items"]))
        self.assertFalse((target / "runtime.json").exists())

    def test_export_exposes_only_user_bundle_members(self):
        source = self.root / "synthetic.txt"
        source.write_text("Evidence", encoding="utf-8")
        asset = self.cmd("import_asset", {"path": str(source)})["result"]["entity"]
        destination = self.root / "exported"
        self.cmd("export_asset", {"id": asset["id"], "target_dir": str(destination)})
        self.assertTrue((destination / "synthetic.txt").exists())
        self.assertFalse((destination / ".workspace.json").exists())


if __name__ == "__main__":
    unittest.main()
