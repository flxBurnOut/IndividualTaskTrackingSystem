"""Synthetic scale probe. Fixture insertion is NOT CRUD write-performance evidence.

Run a short report:
  python tests/test_scale.py --soak-seconds 3 --report .build/scale-smoke.json
Start the optional 30-minute observation explicitly:
  python tests/test_scale.py --soak-seconds 1800 --report .build/scale-30min.json
All records live under TemporaryDirectory and contain synthetic text only.
"""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
import gc
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
import tracemalloc
import unittest
import uuid

from management.core import Core
from management.storage import encode


def seed(core, tasks=10000, events=1000, history=100000):
    stamp = "2030-01-01T00:00:00.000+00:00"
    with core.store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        c.executemany("INSERT INTO entities VALUES (?,?,?,?,?,?,?,?,?,?)", (
            (f"task-{i:05d}", "task", f"Synthetic task {i}", None, "active", 0,
             encode({"estimated_minutes": 30, "completion_gate": "Synthetic full gate"}), 1, stamp, stamp)
            for i in range(tasks)))
        c.executemany("INSERT INTO entities VALUES (?,?,?,?,?,?,?,?,?,?)", (
            (f"event-{i:05d}", "event", f"Synthetic fixed event {i}", None, "active", 0,
             encode({"date": "2030-01-10", "start": f"{i//60:02d}:{i%60:02d}", "end": f"{(i+1)//60:02d}:{(i+1)%60:02d}", "hard": True}),
             1, stamp, stamp) for i in range(events)))
        c.executemany("INSERT INTO changes(revision,entity_id,action,before_value,after_value,request_id,created_at) VALUES (?,?,?,?,?,?,?)", (
            (i+1, f"task-{i%tasks:05d}", "fixture_history", None, None, f"synthetic-{i}", stamp)
            for i in range(history)))
        core.store.set_meta(c, "revision", history)
        c.commit()


def process_memory():
    if os.name != "nt":
        return {"available": False, "reason": "Windows process counters only"}
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t)]
    value = Counters()
    value.cb = ctypes.sizeof(value)
    kernel, psapi = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
        return {"available": False, "error": ctypes.get_last_error()}
    return {"available": True, "private_commit_bytes": value.PrivateUsage, "working_set_bytes": value.WorkingSetSize}


def timing(values):
    ordered = sorted(values)
    return {"count": len(values), "median_ms": round(statistics.median(values), 3),
            "p95_ms": round(ordered[max(0, math.ceil(.95 * len(ordered))-1)], 3), "max_ms": round(ordered[-1], 3)}


def run_probe(seconds=3.0, minimum_iterations=100):
    with tempfile.TemporaryDirectory(prefix="management-scale-") as directory:
        core = Core(directory)
        start = time.perf_counter()
        seed(core)
        setup_seconds = time.perf_counter() - start
        gc.collect()
        baseline = process_memory()
        tracemalloc.start()
        samples, queries, saves = [], [], []
        start = time.perf_counter()
        iterations, failed = 0, 0
        while iterations < minimum_iterations or time.perf_counter() - start < seconds:
            qstart = time.perf_counter()
            page = core.query("list", type="task", limit=100, offset=(iterations % 100) * 100)
            context = core.query("plan_context", date="2030-01-10")
            history_page = core.query("changes", after=(iterations % 1000) * 100, limit=100)
            queries.append((time.perf_counter() - qstart) * 1000)
            assert len(page["items"]) == 100 and page["total"] == 10000
            assert len(context["hard_events"]) == 1000 and context["coverage"]["hard_constraints_complete"]
            assert len(history_page["items"]) <= 100
            del page, context, history_page
            if iterations < 100:
                state = core.query("state")
                cstart = time.perf_counter()
                core.command("record_feedback", {"target_id": f"task-{iterations:05d}", "business_date": "2030-01-10", "dimensions": {"completion": "partial"}, "source_text": "Synthetic benchmark feedback"}, request_id=str(uuid.uuid4()), epoch=state["epoch"], expected_revision=state["revision"])
                saves.append((time.perf_counter() - cstart) * 1000)
            if iterations % 100 == 0:
                current, peak = tracemalloc.get_traced_memory()
                samples.append({"elapsed_seconds": round(time.perf_counter()-start, 3), "python_current_bytes": current, "python_peak_bytes": peak, **process_memory()})
            iterations += 1
        gc.collect()
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        final = process_memory()
        return {"synthetic": True, "fixture_counts": {"tasks": 10000, "events": 1000, "history_rows": 100000},
                "fixture_creation_seconds": round(setup_seconds, 3), "fixture_insertion_is_crud_benchmark": False,
                "elapsed_seconds": round(time.perf_counter()-start, 3), "iterations": iterations, "failures": failed,
                "three_queries_per_iteration": timing(queries), "feedback_commits": timing(saves),
                "memory_before": baseline, "memory_after": final, "python_current_bytes": current, "python_peak_bytes": peak, "samples": samples,
                "limitations": ["Only this synthetic Core process, not GUI/service/Codex combined memory.", "Short run does not establish 30-minute or 8-hour stability.", "Query timing combines paged list, complete day constraints and change cursor.", "tracemalloc adds overhead and excludes non-Python allocations.", "No AI, external providers, old data or real attachments used."]}


class ScaleTests(unittest.TestCase):
    def test_scale_pagination_complete_constraints_and_short_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            core = Core(directory)
            seed(core)
            state = core.query("state")
            self.assertEqual(state["counts"], {"task": 10000, "event": 1000})
            page = core.query("list", type="task", limit=100, offset=9900)
            self.assertEqual(len(page["items"]), 100)
            self.assertIsNone(page["next_offset"])
            context = core.query("plan_context", date="2030-01-10")
            self.assertEqual(context["coverage"]["tasks_total"], 10000)
            self.assertTrue(context["coverage"]["tasks_paged"])
            self.assertEqual(len(context["hard_events"]), 1000)
            self.assertEqual(len(core.query("get", id="task-00001")["history"]), 10)
            receipt = core.command("record_feedback", {"target_id": "task-00001", "business_date": "2030-01-10", "dimensions": {"attendance": "attended"}, "source_text": "Synthetic known attendance"}, request_id=str(uuid.uuid4()), epoch=state["epoch"], expected_revision=state["revision"])
            self.assertEqual(receipt["revision"], 100001)
            self.assertEqual(core.query("review", start="2030-01-10", end="2030-01-10")["metrics"]["completed_daily_targets"], 0)

    def test_completed_recent_tasks_do_not_hide_all_active_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            core = Core(directory)
            seed(core, tasks=102, events=0, history=0)
            with core.store.connect() as c:
                c.execute("UPDATE entities SET status='done',updated_at='2040-01-01' WHERE id NOT IN ('task-00000','task-00001')")
            context = core.query("plan_context", date="2030-01-10")
            self.assertEqual({t["id"] for t in context["tasks"]}, {"task-00000", "task-00001"})


if __name__ == "__main__":
    if "--soak-seconds" in sys.argv:
        parser = argparse.ArgumentParser()
        parser.add_argument("--soak-seconds", type=float, default=3)
        parser.add_argument("--report", type=Path)
        args = parser.parse_args()
        if not 0 <= args.soak_seconds <= 8*3600:
            parser.error("soak duration must be within zero and eight hours")
        result = run_probe(args.soak_seconds)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        unittest.main()
