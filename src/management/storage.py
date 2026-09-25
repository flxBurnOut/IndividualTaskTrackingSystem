from __future__ import annotations

import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import threading
import uuid

from .schemas import BusinessError
from .appearance import DEFAULT_APPEARANCE


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def new_id():
    return str(uuid.uuid4())


DEFAULT_SETTINGS = {
    "appearance": dict(DEFAULT_APPEARANCE),
    "timezone": "Asia/Shanghai", "favorites": [], "ai": {"enabled": False, "executable": "", "model": "", "timeout_seconds": 180},
    "charts": {"weekly_style": "columns"},
    "timetable_defaults": {"week_numbering": "teaching", "recess_weeks": []},
    "reserve_bytes": 1073741824, "page_size": 100, "context_characters": 42000,
}


class Store:
    def __init__(self, data_dir, *, allow_pending_restore=False):
        self.root = Path(data_dir).resolve()
        if (self.root / 'restore_pending.json').exists() and not allow_pending_restore:
            raise BusinessError('restore_pending', '恢复协调尚未完成；此目录不能启动。请从原数据空间重新恢复到新的目录。')
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "database.sqlite3"
        self.lock = threading.RLock()
        v = sqlite3.sqlite_version_info
        if v < (3, 51, 3) and v[:2] not in {(3, 44), (3, 50)}:
            raise BusinessError("sqlite_version", "SQLite 版本缺少所需的 WAL 修复，请使用随软件提供的运行时。")
        if v[:2] == (3, 44) and v < (3, 44, 6) or v[:2] == (3, 50) and v < (3, 50, 7):
            raise BusinessError("sqlite_version", "SQLite 版本缺少 WAL 修复。")
        if self.path.exists() and self.path.stat().st_size:
            probe = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
            try:
                exists = probe.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
                if not exists:
                    raise BusinessError('unknown_database', '此文件不是软件创建的数据空间，未修改。')
                row = probe.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if not row or json.loads(row[0]) != 1:
                    raise BusinessError('schema_version', '此数据空间需要不同版本的软件；未改动业务数据。')
            finally:
                probe.close()
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entities(
                    id TEXT PRIMARY KEY,type TEXT NOT NULL,title TEXT NOT NULL,
                    parent_id TEXT REFERENCES entities(id),status TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,data TEXT NOT NULL DEFAULT '{}',
                    version INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS entities_parent ON entities(parent_id,archived,type);
                CREATE INDEX IF NOT EXISTS entities_type ON entities(type,archived,status);
                CREATE INDEX IF NOT EXISTS feedback_target_date ON entities(json_extract(data,'$.target_id'),json_extract(data,'$.business_date'),created_at) WHERE type='feedback';
                CREATE INDEX IF NOT EXISTS plan_date ON entities(json_extract(data,'$.date'),created_at) WHERE type='plan';
                CREATE INDEX IF NOT EXISTS source_owner_sha ON entities(json_extract(data,'$.source_owner_id'),json_extract(data,'$.sha256'),json_extract(data,'$.source_kind')) WHERE type='asset';
                CREATE INDEX IF NOT EXISTS daily_review_key ON entities(json_extract(data,'$.date'),json_extract(data,'$.plan_id')) WHERE type='review';

                CREATE INDEX IF NOT EXISTS task_scheduled_date ON entities(json_extract(data,'$.scheduled_date')) WHERE type='task' AND archived=0;
                CREATE INDEX IF NOT EXISTS task_due_date ON entities(json_extract(data,'$.due_date')) WHERE type='task' AND archived=0;

                CREATE TABLE IF NOT EXISTS links(
                    id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES entities(id),
                    target_id TEXT NOT NULL REFERENCES entities(id),kind TEXT NOT NULL,created_at TEXT NOT NULL,
                    UNIQUE(source_id,target_id,kind));
                CREATE INDEX IF NOT EXISTS links_target ON links(target_id,kind);
                CREATE TABLE IF NOT EXISTS receipts(
                    request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,epoch TEXT NOT NULL,
                    command TEXT NOT NULL,result TEXT NOT NULL,revision INTEGER NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS changes(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,revision INTEGER NOT NULL,
                    entity_id TEXT,action TEXT NOT NULL,before_value TEXT,after_value TEXT,
                    request_id TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS changes_entity ON changes(entity_id,seq);
                CREATE TABLE IF NOT EXISTS modules(id TEXT PRIMARY KEY,version INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,manifest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY,kind TEXT NOT NULL,status TEXT NOT NULL,input TEXT NOT NULL,
                    result TEXT,error TEXT,epoch TEXT NOT NULL,snapshot_revision INTEGER NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,created_at);
                CREATE TABLE IF NOT EXISTS io_operations(
                    request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,command TEXT NOT NULL,
                    status TEXT NOT NULL,payload TEXT NOT NULL,result TEXT,error TEXT,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS schedule_runs(
                    schedule_id TEXT NOT NULL,occurrence TEXT NOT NULL,job_id TEXT,
                    created_at TEXT NOT NULL,PRIMARY KEY(schedule_id,occurrence));
            """)
            from .library import initialize as initialize_library
            initialize_library(c)
            from .conversations import initialize
            initialize(c)
            from .occurrences import initialize as initialize_occurrences
            initialize_occurrences(c)
            from .context_schema import initialize as initialize_context
            initialize_context(c)
            from .recurring import initialize as initialize_recurring
            initialize_recurring(c)
            for k, value in {"epoch": new_id(), "revision": 0, "schema_version": 1, "settings": DEFAULT_SETTINGS}.items():
                c.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (k, encode(value)))
            if self.meta(c, "schema_version") != 1:
                raise BusinessError("schema_version", "此数据空间需要不同版本的软件；未改动业务数据。")

    @contextlib.contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA synchronous=FULL")
        try:
            yield c
        finally:
            c.close()

    @staticmethod
    def meta(c, key):
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def set_meta(c, key, value):
        c.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encode(value)))

    def state(self, c):
        return {"epoch": self.meta(c, "epoch"), "revision": self.meta(c, "revision")}

    @staticmethod
    def entity(row):
        if not row:
            raise BusinessError("not_found", "记录不存在，可能已切换数据空间。")
        v = dict(row)
        v["data"] = json.loads(v["data"])
        v["archived"] = bool(v["archived"])
        return v

    def get(self, c, id):
        return self.entity(c.execute("SELECT * FROM entities WHERE id=?", (id,)).fetchone())

    def change(self, c, request_id, action, before=None, after=None):
        entity = after or before or {}
        c.execute("INSERT INTO changes(revision,entity_id,action,before_value,after_value,request_id,created_at) VALUES (?,?,?,?,?,?,?)",
                  (self.meta(c, "revision") + 1, entity.get("id"), action,
                   encode(before) if before else None, encode(after) if after else None, request_id, now()))
