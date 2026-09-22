"""Domain rules use explicit synthetic source statements, never existing records."""
from pathlib import Path
import tempfile
import unittest
import uuid

from management.core import Core
from management.domains import query_domain
from management.schemas import BusinessError


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = Core(Path(self.temp.name) / "space")

    def cmd(self, name, payload):
        state = self.core.query("state")
        return self.core.command(name, payload, request_id=str(uuid.uuid4()), epoch=state["epoch"], expected_revision=state["revision"])["result"]

    def create(self, type, title="Synthetic", parent=None, data=None, status="active"):
        return self.cmd("create", {"type": type, "title": title, "parent_id": parent["id"] if parent else None, "data": data or {}, "status": status})["entity"]

    def query(self, name, **params):
        before = self.core.query("state")["revision"]
        with self.core.store.connect() as c:
            c.execute("BEGIN")
            result = query_domain(self.core, c, name, params)
            c.rollback()
        self.assertEqual(self.core.query("state")["revision"], before)
        return result

    def attempt(self, owner, score, maximum=100, day="2030-01-01", **extra):
        return self.create("attempt", parent=owner, data={"performed": True, "source_text": "Synthetic explicit actual attempt", "business_date": day, "score": score, "maximum": maximum, **extra})

    def test_generated_question_and_answer_do_not_prove_attempt_or_mastery(self):
        course = self.create("course")
        topic = self.create("topic", parent=course)
        question = self.create("question", parent=topic, data={"prompt": "Question", "answer_key": "Generated reference"})
        self.create("attempt", parent=question, data={"answer": "Generated answer", "performed": False, "source_text": "Model draft"})
        result = self.query("learning_summary", course_id=course["id"])
        self.assertEqual(result["metrics"]["questions"], 1)
        self.assertEqual(result["metrics"]["performed_attempts"], 0)
        self.assertEqual(result["metrics"]["mastery_verified_attempts"], 0)
        self.assertEqual(result["metrics"]["not_performed"], 1)

    def test_learning_requires_source_and_keeps_errors_and_review_dates_separate(self):
        course = self.create("course")
        question = self.create("question", parent=course)
        self.create("attempt", parent=question, data={"performed": True, "answer": "No source"})
        self.attempt(question, 60, error="Synthetic wrong sign", next_review="2030-01-05", mastery_verified=False)
        self.attempt(question, 100, day="2030-01-02", mastery_verified=True, next_review="2030-02-01")
        result = self.query("learning_summary", course_id=course["id"], as_of="2030-01-10")
        self.assertEqual(result["metrics"]["attempt_evidence_unknown"], 1)
        self.assertEqual(result["metrics"]["performed_attempts"], 2)
        self.assertEqual(result["metrics"]["open_errors"], 1)
        self.assertEqual(result["metrics"]["due_retests"], 1)
        self.assertEqual(result["metrics"]["mastery_verified_attempts"], 1)

    def test_learning_pagination_does_not_change_metrics(self):
        course = self.create("course")
        for _ in range(3):
            self.create("question", parent=course)
        first = self.query("learning_summary", course_id=course["id"], limit=1)
        second = self.query("learning_summary", course_id=course["id"], limit=1, offset=1)
        self.assertEqual(first["metrics"], second["metrics"])
        self.assertEqual(first["coverage"]["total"], 3)
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])

    def test_partial_course_grade_does_not_turn_unknown_into_zero(self):
        course = self.create("course")
        known = self.create("assessment", parent=course, data={"weight": 50, "aggregation": "best"})
        self.create("assessment", parent=course, data={"weight": 50})
        self.attempt(known, 80)
        result = self.query("assessment_summary", course_id=course["id"])
        self.assertEqual(result["metrics"]["known_weighted_percentage_points"], 40)
        self.assertEqual(result["metrics"]["components_unknown"], 1)
        self.assertIsNone(result["metrics"]["overall_percentage"])
        self.assertFalse(result["coverage"]["overall_complete"])

    def test_best_latest_and_mean_are_explicit_weighted_policies(self):
        course = self.create("course")
        best = self.create("assessment", "Best", course, {"weight": 40, "aggregation": "best"})
        latest = self.create("assessment", "Latest", course, {"weight": 30, "aggregation": "latest"})
        mean = self.create("assessment", "Mean", course, {"weight": 30, "aggregation": "mean"})
        self.attempt(best, 5, 10)
        self.attempt(best, 90, 100, "2030-01-02")
        self.attempt(latest, 40, 100, "2030-01-02")
        self.attempt(latest, 90, 100, "2030-01-01")
        self.attempt(mean, 50)
        self.attempt(mean, 70, day="2030-01-02")
        result = self.query("assessment_summary", course_id=course["id"])
        self.assertAlmostEqual(result["metrics"]["overall_percentage"], 66)
        self.assertTrue(result["coverage"]["overall_complete"])

    def test_best_n_requires_enough_actual_evidenced_attempts(self):
        course = self.create("course")
        grade = self.create("assessment", parent=course, data={"weight": 100, "aggregation": "best_n", "best_n": 3})
        self.attempt(grade, 80)
        self.attempt(grade, 100, day="2030-01-02")
        result = self.query("assessment_summary", course_id=course["id"])
        self.assertIsNone(result["metrics"]["overall_percentage"])
        self.assertEqual(result["items"][0]["provisional_ratio"], .9)
        self.attempt(grade, 60, day="2030-01-03")
        self.assertEqual(self.query("assessment_summary", course_id=course["id"])["metrics"]["overall_percentage"], 80)

    def test_invalid_missing_or_generated_scores_not_counted(self):
        course = self.create("course")
        grade = self.create("assessment", parent=course, data={"weight": 100})
        self.attempt(grade, 80, performed=False)
        self.create("attempt", parent=grade, data={"performed": True, "score": 90, "source_text": "No maximum"})
        self.attempt(grade, 20, 10)
        result = self.query("assessment_summary", course_id=course["id"])
        self.assertEqual(result["items"][0]["confirmed_attempts"], 0)
        self.assertEqual(result["items"][0]["unverified_attempts"], 3)
        self.assertIsNone(result["metrics"]["overall_percentage"])

    def test_penalty_requires_confirmation_source_and_declared_units(self):
        course = self.create("course")
        grade = self.create("assessment", parent=course, data={"weight": 100, "penalty_percentage_points": 5, "penalty_confirmed": False})
        self.attempt(grade, 80)
        self.assertIsNone(self.query("assessment_summary", course_id=course["id"])["metrics"]["overall_percentage"])
        self.cmd("update", {"id": grade["id"], "version": 1, "patch": {"data": {"penalty_confirmed": True, "penalty_source": "Synthetic explicit 5 course percentage points"}}})
        self.assertEqual(self.query("assessment_summary", course_id=course["id"])["metrics"]["overall_percentage"], 75)

    def test_global_course_summaries_do_not_combine_unrelated_course_grades(self):
        for _ in range(2):
            course = self.create("course")
            self.create("assessment", parent=course, data={"weight": 50, "score": 80, "maximum": 100, "source_text": "Explicit grade"})
        metrics = self.query("assessment_summary")["metrics"]
        self.assertIsNone(metrics["overall_percentage"])
        self.assertIsNone(metrics["known_weighted_percentage_points"])

    def test_collection_units_unknowns_and_batch_estimates_remain_separate(self):
        project = self.create("project")
        batch = self.create("batch", parent=project, data={"target_count": 999})
        self.create("case", parent=batch, data={"object_count": 1, "clip_count": 11, "valid_minutes": 12, "actual_minutes": 60, "quality": "passed", "upload": "unknown"})
        self.create("case", parent=batch, status="done")
        result = self.query("collection_summary", batch_id=batch["id"], limit=1)
        self.assertEqual(result["metrics"]["case_count"], 2)
        self.assertEqual(result["metrics"]["known_totals"], {"object_count": 1, "clip_count": 11, "valid_minutes": 12, "actual_minutes": 60})
        self.assertEqual(result["metrics"]["unknown_counts"]["clip_count"], 1)
        self.assertEqual(result["batches"][0]["target_count"], 999)
        self.assertEqual(result["metrics"]["states"]["upload"]["unknown"], 2)

    def test_shared_references_do_not_duplicate_collection_counts(self):
        project = self.create("project")
        a, b = self.create("batch", parent=project), self.create("batch", parent=project)
        case = self.create("case", parent=a, data={"clip_count": 11})
        self.cmd("link", {"source_id": b["id"], "target_id": case["id"], "kind": "references"})
        self.assertEqual(self.query("collection_summary", project_id=project["id"])["metrics"]["case_count"], 1)
        self.assertEqual(self.query("collection_summary", batch_id=b["id"])["metrics"]["case_count"], 0)

    def feedback(self, entity, minutes):
        return self.cmd("record_feedback", {"target_id": entity["id"], "business_date": "2030-01-10", "dimensions": {"actual_minutes": minutes}, "source_text": "Synthetic actual minutes"})

    def test_project_tracks_hours_corrections_and_parent_totals_do_not_mix(self):
        project = self.create("project", data={"track": "main"})
        main = self.create("task", parent=project)
        side_project = self.create("project", parent=project, data={"track": "side"})
        side = self.create("task", parent=side_project)
        self.feedback(main, 30)
        self.feedback(main, 20)
        self.feedback(side, 10)
        self.feedback(project, 100)
        result = self.query("project_summary", project_id=project["id"])
        self.assertEqual(result["tracks"]["main"]["leaf_actual_minutes"], 20)
        self.assertEqual(result["tracks"]["side"]["leaf_actual_minutes"], 10)
        self.assertEqual(result["tracks"]["main"]["aggregate_reported_minutes"], 100)
        self.assertEqual(result["unknown_work_time_targets"], 0)

    def test_parent_done_does_not_automatically_finish_child(self):
        project = self.create("project", status="done")
        task = self.create("task", parent=project)
        result = self.query("project_summary", project_id=project["id"])
        self.assertEqual(result["closure_conflicts"][0]["active_child_ids"], [task["id"]])
        self.assertEqual(result["unknown_work_time_targets"], 1)
        self.assertEqual(result["tracks"]["unspecified"]["leaf_actual_minutes"], 0)

    def test_gap_types_are_explicit_and_cannot_infer_not_released_from_login_failure(self):
        course = self.create("course")
        self.create("gap", parent=course, data={"gap_kind": "access_blocked", "source_text": "Login unavailable", "last_checked": "2030-01-01", "next_check": "2030-01-20"})
        self.create("gap", parent=course, data={"gap_kind": "unread", "next_check": "2030-01-05"})
        result = self.query("coverage_gaps", course_id=course["id"], as_of="2030-01-10")
        self.assertEqual(result["counts"]["access_blocked"], 1)
        self.assertEqual(result["counts"]["not_released"], 0)
        self.assertEqual(sum(i["check_due"] for i in result["items"]), 1)

    def test_scope_type_mismatch_rejected(self):
        task = self.create("task")
        with self.assertRaises(BusinessError) as raised:
            self.query("learning_summary", course_id=task["id"])
        self.assertEqual(raised.exception.code, "scope_type")


if __name__ == "__main__":
    unittest.main()
