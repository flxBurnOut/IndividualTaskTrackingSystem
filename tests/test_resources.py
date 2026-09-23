"""Synthetic resource failures and restore boundaries; never reads user data."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from management.resources import ResourceError, ResourceManager


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.manager = ResourceManager(self.base / "data", reserve_bytes=0, staging_limit=64 * 1024**2)

    def file(self, name="source.txt", content=b"synthetic evidence"):
        path = self.base / name
        path.write_bytes(content)
        return path

    def error(self, code, fn, *args, **kwargs):
        with self.assertRaises(ResourceError) as raised:
            fn(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def db(self, assets=()):
        path = self.manager.root / "database.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE assets (sha256 TEXT PRIMARY KEY, size INTEGER)")
        connection.execute("CREATE TABLE facts (id TEXT PRIMARY KEY, content TEXT)")
        connection.execute("INSERT INTO facts VALUES ('known', 'synthetic confirmed')")
        connection.executemany("INSERT INTO assets VALUES (?, ?)", [(a["sha256"], a["size"]) for a in assets])
        connection.commit()
        connection.close()
        return path

    @staticmethod
    def select_assets(conn):
        return [{"sha256": row[0], "size": row[1]} for row in conn.execute("SELECT sha256, size FROM assets")]

    def test_streaming_import_and_idempotent_blob(self):
        payload = b"0123456789abcdef" * 400000
        source = self.file(content=payload)
        first = self.manager.import_file(source)
        second = self.manager.import_file(source)
        self.assertEqual(first["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(len(list((self.manager.root / "blobs").iterdir())), 1)
        self.assertEqual(source.read_bytes(), payload)
        self.assertEqual(first["validation"]["content"], "not_checked")

    def test_concurrent_import_publishes_one_blob(self):
        source = self.file()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.manager.import_file(source), range(4)))
        self.assertEqual(len({r["sha256"] for r in results}), 1)
        self.assertEqual(len(list((self.manager.root / "blobs").iterdir())), 1)

    def test_wrong_expected_hash_does_not_publish(self):
        self.error("HASH_MISMATCH", self.manager.import_file, self.file(), expected_sha256="0" * 64)
        self.assertEqual(list((self.manager.root / "blobs").iterdir()), [])
        self.assertEqual(list((self.manager.root / ".staging").iterdir()), [])

    def test_corrupt_existing_blob_never_overwritten(self):
        source = self.file()
        item = self.manager.import_file(source)
        blob = self.manager.root / item["path"]
        blob.write_bytes(b"corrupt")
        self.error("HASH_MISMATCH", self.manager.import_file, source)
        self.assertEqual(blob.read_bytes(), b"corrupt")

    def test_low_space_and_quota_reject_without_losing_source(self):
        source = self.file()
        with patch("management.resources.shutil.disk_usage") as usage:
            usage.return_value.free = 1
            self.error("LOW_DISK_SPACE", self.manager.import_file, source)
        self.manager.staging_limit = 1
        self.error("RESOURCE_LIMIT", self.manager.import_file, source)
        self.assertTrue(source.is_file())
        self.assertEqual(self.manager._reserved, 0)

    def test_bundle_keeps_relative_dependencies_and_independent_bytes(self):
        item = self.manager.import_file(self.file())
        result = self.manager.materialize_bundle([
            {"path": "lab/data/example.csv", "sha256": item["sha256"]},
            {"path": "lab/question.txt", "sha256": item["sha256"]},
        ], job_id="bundle")
        work = Path(result["path"])
        self.assertTrue((work / "lab/data/example.csv").is_file())
        (work / "lab/question.txt").write_bytes(b"edited work copy")
        self.assertEqual((self.manager.root / item["path"]).read_bytes(), b"synthetic evidence")
        self.error("ALREADY_EXISTS", self.manager.materialize_bundle,
                   [{"path": "lab/question.txt", "sha256": item["sha256"]}], job_id="bundle")

    def test_bundle_traversal_absolute_ads_windows_devices_rejected(self):
        item = self.manager.import_file(self.file())
        for path in ("../escape", "/abs", "C:/file", "a\\b", "a/../../b", "NUL.txt", "a:stream", "a/./b", "a//b", "a.", ".workspace.json"):
            with self.subTest(path=path):
                self.error("UNSAFE_PATH", self.manager.materialize_bundle, [{"path": path, "sha256": item["sha256"]}])

    def test_bundle_case_and_parent_conflicts_preflight(self):
        item = self.manager.import_file(self.file())
        for paths in (("A.txt", "a.txt"), ("a", "a/b")):
            self.error("BUNDLE_CONFLICT", self.manager.materialize_bundle,
                       [{"path": p, "sha256": item["sha256"]} for p in paths])
        self.assertEqual(list((self.manager.root / "jobs").iterdir()), [])

    def test_linked_source_and_workspace_escape_are_rejected(self):
        source = self.file()
        link = self.base / "link.txt"
        try:
            link.symlink_to(source)
        except OSError:
            self.skipTest("OS does not grant symlink creation")
        self.error("UNSAFE_PATH", self.manager.import_file, link)
        work = self.manager.create_workspace("escape")
        (Path(work["path"]) / "outside").symlink_to(self.base, target_is_directory=True)
        self.error("UNSAFE_PATH", self.manager.generate_artifact, "escape", "outside/file.md", "markdown", "no")

    def test_generated_text_freeze_retains_versions_and_provenance(self):
        self.manager.create_workspace("report")
        self.manager.generate_artifact("report", "report-v1.md", "markdown", "# 已确认\n仅合成测试\n")
        result = self.manager.freeze_artifact("report", "report-v1.md", kind="markdown", source_versions=[{"id": "x", "version": 2}])
        self.assertEqual(result["state"], "frozen")
        self.assertEqual(result["source_versions"], [{"id": "x", "version": 2}])
        self.assertTrue(result["pinned"])
        self.error("ALREADY_EXISTS", self.manager.generate_artifact, "report", "report-v1.md", "markdown", "overwrite")
        self.manager.generate_artifact("report", "report-v2.md", "markdown", "# 新版本\n")
        self.assertTrue((self.manager.root / result["path"]).is_file())

    def test_notebook_does_not_claim_execution_and_rejects_fake_outputs(self):
        self.manager.create_workspace("notebook")
        value = {"nbformat": 4, "nbformat_minor": 4, "metadata": {}, "cells": [
            {"cell_type": "markdown", "source": "题目", "metadata": {}},
            {"cell_type": "code", "source": "", "metadata": {}, "outputs": [], "execution_count": None},
        ]}
        self.manager.generate_artifact("notebook", "questions.ipynb", "notebook", value)
        result = self.manager.freeze_artifact("notebook", "questions.ipynb", kind="notebook")
        self.assertEqual(result["validation"]["execution"], "not_run")
        value["cells"][1]["execution_count"] = 1
        self.error("INVALID_ARTIFACT", self.manager.generate_artifact, "notebook", "fake.ipynb", "notebook", value)

    def test_csv_and_unsupported_formats(self):
        self.manager.create_workspace("csv")
        result = self.manager.generate_artifact("csv", "rows.csv", "csv", [["id", "text"], [1, "a,b\n中文"]])
        with Path(result["path"]).open(encoding="utf-8", newline="") as stream:
            import csv
            self.assertEqual(list(csv.reader(stream)), [["id", "text"], ["1", "a,b\n中文"]])
        self.error("UNSUPPORTED_ARTIFACT", self.manager.generate_artifact, "csv", "report.pdf", "pdf", "text")

    def test_backup_uses_snapshot_manifest_excludes_unregistered_and_runtime(self):
        item = self.manager.import_file(self.file())
        orphan = self.manager.import_file(self.file("orphan", b"orphan unregistered"))
        db = self.db([item])
        (self.manager.root / "runtime.json").write_text('{"token":"secret"}', encoding="utf-8")
        result = self.manager.create_backup(db, self.select_assets, app_lock=threading.RLock())
        backup = Path(result["path"])
        self.assertFalse((backup / "runtime.json").exists())
        self.assertFalse((backup / "blobs" / orphan["sha256"]).exists())
        self.assertEqual(result["report"]["asset_count"], 1)
        self.assertTrue(self.manager.verify_backup(backup)["valid"])

    def test_wal_snapshot_contains_committed_wal_rows(self):
        db = self.db()
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("INSERT INTO facts VALUES ('wal', 'committed')")
        conn.commit()
        self.addCleanup(conn.close)
        result = self.manager.create_backup(db, self.select_assets, app_lock=threading.RLock())
        snap = sqlite3.connect(Path(result["path"]) / "database.sqlite3")
        self.addCleanup(snap.close)
        self.assertEqual(snap.execute("SELECT content FROM facts WHERE id='wal'").fetchone(), ("committed",))

    def test_restore_verifies_before_destination_change_and_requires_reconciliation(self):
        item = self.manager.import_file(self.file())
        backup = self.manager.create_backup(self.db([item]), self.select_assets, app_lock=threading.RLock())
        restored = self.manager.restore_backup(backup["path"], self.base / "restored",
            validator=lambda conn: conn.execute("SELECT content FROM facts").fetchone()[0] == "synthetic confirmed")
        target = Path(restored["path"])
        self.assertTrue(restored["requires_new_epoch"])
        self.assertTrue((target / "restore_pending.json").exists())
        self.assertFalse((target / "runtime.json").exists())
        self.assertEqual((target / item["path"]).read_bytes(), b"synthetic evidence")
        self.error("ALREADY_EXISTS", self.manager.restore_backup, backup["path"], target)

    def test_corrupt_or_missing_backup_asset_never_changes_restore_target(self):
        item = self.manager.import_file(self.file())
        backup = self.manager.create_backup(self.db([item]), self.select_assets, app_lock=threading.RLock())
        (Path(backup["path"]) / item["path"]).write_bytes(b"bad")
        target = self.base / "never-created"
        self.error("HASH_MISMATCH", self.manager.restore_backup, backup["path"], target)
        self.assertFalse(target.exists())

    def test_business_validator_rejection_precedes_destination_mutation(self):
        backup = self.manager.create_backup(self.db(), self.select_assets, app_lock=threading.RLock())
        target = self.base / "never-created"
        self.error("INVALID_DATABASE", self.manager.restore_backup, backup["path"], target, validator=lambda _: False)
        self.assertFalse(target.exists())

    def test_failed_backup_is_not_recoverable(self):
        item = {"sha256": "1" * 64, "size": 1}
        self.error("NOT_A_FILE", self.manager.create_backup, self.db([item]), self.select_assets, app_lock=threading.RLock())
        partial = next(p for p in (self.manager.root / "backups").iterdir() if p.name.endswith(".partial"))
        self.assertTrue((partial / "incomplete.json").is_file())
        self.error("INCOMPLETE_BACKUP", self.manager.verify_backup, partial)

    def test_secret_backup_metadata_is_rejected(self):
        self.error("UNSAFE_BACKUP_METADATA", self.manager.create_backup, self.db(), self.select_assets,
                   app_lock=threading.RLock(), metadata={"config": {"api_key": "not-real"}})


    def test_junction_guard_rechecks_mutable_staging_directory(self):
        source = self.file()
        actual = Path.is_junction
        def pretend_junction(path):
            return path.name == ".staging" or actual(path)
        with patch.object(Path, "is_junction", pretend_junction):
            self.error("UNSAFE_PATH", self.manager.import_file, source)
        self.assertEqual(list((self.manager.root / ".staging").iterdir()), [])

    def test_symlink_guard_without_os_link_privilege(self):
        source = self.file()
        actual = Path.is_symlink
        # macOS may expand the OS-owned /var alias before inspecting the file.
        canonical_source = source.resolve()
        def pretend_link(path):
            return path in {source, canonical_source} or actual(path)
        with patch.object(Path, "is_symlink", pretend_link):
            self.error("UNSAFE_PATH", self.manager.import_file, source)

    def test_manifest_traversal_hash_cannot_reach_outside_backup(self):
        backup = self.manager.create_backup(self.db(), self.select_assets, app_lock=threading.RLock())
        path = Path(backup["path"]) / "manifest.json"
        data = json.loads(path.read_text("utf-8"))
        data["assets"] = [{"sha256": "../outside", "size": 1}]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.error("INVALID_MANIFEST", self.manager.verify_backup, backup["path"])

    def test_snapshot_manifest_is_read_from_new_database_snapshot(self):
        old = self.manager.import_file(self.file("old", b"old"))
        new = self.manager.import_file(self.file("new", b"new"))
        db = self.db([old])
        with sqlite3.connect(db) as conn:
            conn.execute("DELETE FROM assets")
            conn.execute("INSERT INTO assets VALUES (?, ?)", (new["sha256"], new["size"]))
        conn.close()
        result = self.manager.create_backup(db, self.select_assets, app_lock=threading.RLock())
        self.assertEqual(result["manifest"]["assets"], [{"sha256": new["sha256"], "size": new["size"]}])
        self.assertFalse((Path(result["path"]) / old["path"]).exists())


    def test_backup_versions_reuse_private_content_without_linking_live_blob(self):
        item = self.manager.import_file(self.file())
        db = self.db([item])
        first = self.manager.create_backup(db, self.select_assets, app_lock=threading.RLock())
        with patch.object(self.manager, "_copy_verified", wraps=self.manager._copy_verified) as copy:
            second = self.manager.create_backup(db, self.select_assets, app_lock=threading.RLock())
            copy.assert_not_called()
        live = self.manager.root / item["path"]
        live.write_bytes(b"bad live data")
        self.assertTrue(self.manager.verify_backup(first["path"])["valid"])
        self.assertTrue(self.manager.verify_backup(second["path"])["valid"])


if __name__ == "__main__":
    unittest.main()
