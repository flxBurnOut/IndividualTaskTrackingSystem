"""All mutations from GUI, scheduler and MCP cross this transaction boundary."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import threading
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .schemas import BusinessError, TYPES, RESERVED_TYPES, validate_dimensions, validate_manifest
from .storage import Store, encode, new_id, now


COMMANDS = ["delete_task", "restore_task", "apply_timetable", "set_recovery_task", "record_recovery_progress", "add_to_plan", "set_recurring_rule", "materialize_recurring", "add_source", "send_message", "submit_daily_review", "set_review_preferences", "attach_local_file", "create", "update", "move", "archive", "link", "unlink", "record_feedback", "create_plan",
            "create_checkin", "respond_checkin", "save_review", "settings", "install_module", "disable_module",
            "create_job", "cancel_job", "apply_proposal", "promote_checklist", "run_workflow", "undo",
            "import_asset", "create_notebook_from_pdf", "create_bundle", "create_artifact_job", "backup", "restore_backup", "export_asset", "adopt_artifact"]


def date_value(value, label="日期"):
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise BusinessError("validation", f"{label}需要有效日期，如 2026-09-22。")


def time_value(value):
    try:
        t = dt.time.fromisoformat(value)
        if t.tzinfo or t.second or t.microsecond:
            raise ValueError()
        return t.hour * 60 + t.minute
    except (TypeError, ValueError):
        raise BusinessError("validation", "时间格式应为小时:分钟，如 09:30。")


class Core:
    def __init__(self, data_dir):
        self.store = Store(data_dir)
        self.root = self.store.root
        self.cancel_events = {}
        self._resources = None
        from .extensions import ExtensionRegistry
        self.extensions = ExtensionRegistry()

    @property
    def resources(self):
        with self.store.connect() as c:
            reserve = self.store.meta(c, 'settings')['reserve_bytes']
        if self._resources is None:
            from .resources import ResourceManager
            self._resources = ResourceManager(self.root, reserve_bytes=reserve)
        else:
            self._resources.reserve_bytes = reserve
        return self._resources

    def types(self, c):
        definitions = copy.deepcopy(TYPES)
        for row in c.execute("SELECT * FROM modules ORDER BY id"):
            manifest = json.loads(row["manifest"])
            for t in manifest.get("types", []):
                definitions[t["id"]] = {"fields": [], "parent_types": [None], "statuses": TYPES["task"]["statuses"], **t, "module": row["id"], "read_only": not row["enabled"]}
            for f in manifest.get("fields", []):
                if f["target_type"] in definitions:
                    definitions[f["target_type"]]["fields"].append({**f, "read_only": not row["enabled"]})
        return definitions

    def query(self, name, **p):
        if name == 'codex_models':
            from .model_catalog import list_models
            with self.store.connect() as c:
                settings = copy.deepcopy(self.store.meta(c, 'settings'))
            if 'executable' in p:
                settings['ai']['executable'] = p['executable']
            result = list_models(settings)
            with self.store.connect() as c:
                return {**result, **self.store.state(c)}
        if name == 'source_content':
            from .sources import content
            result=content(self,p)
            with self.store.connect() as c:
                return {**result,**self.store.state(c)}
        if name == 'open_resource':
            from .workspace import open_resource
            result = open_resource(self, p)
            with self.store.connect() as c:
                return {**result, **self.store.state(c)}
        with self.store.connect() as c:
            c.execute("BEGIN")
            state = self.store.state(c)
            result = self._query(c, name, p)
            c.rollback()
            return {**result, **state}

    def _query(self, c, name, p):
        if name in {"timetables", "timetable_week"}:
            from .timetable import timetables, timetable_week
            return (timetables if name == "timetables" else timetable_week)(self, c, p)
        if name == "skills":
            from .skill_workflows import CATALOG, skill_context
            return {"items": [skill_context(k) for k in CATALOG]}
        if name == "recovery_summary":
            from .catchup import recovery_summary
            return recovery_summary(self, c, p)
        if name == 'daily_tasks':
            from .daily_flow import query_tasks
            return query_tasks(self, c, p)
        if name in {'recurring_rules', 'preview_recurring'}:
            from .recurring import query_rules, preview
            return (query_rules if name == 'recurring_rules' else preview)(self, c, p)
        if name == 'sources':
            from .sources import list_sources
            return list_sources(self,c,p)
        if name == 'conversation':
            from .conversations import query
            return query(self,c,p)
        if name in {'daily_review', 'weekly_review'}:
            from .reviews import query_daily, query_weekly
            return (query_daily if name == 'daily_review' else query_weekly)(self,c,p)
        if name == 'review_preferences':
            from .workspace import review_preferences
            return review_preferences(self,c)
        if name == 'object_workspace':
            from .workspace import object_workspace
            return object_workspace(self,c,p)
        if name in self.extensions.queries:
            definition = self.extensions.queries[name]
            self.extensions.validate(definition, p)
            return definition.handler(self, c, p)
        if name == "state":
            counts = {r[0]: r[1] for r in c.execute("SELECT type,count(*) FROM entities WHERE archived=0 GROUP BY type")}
            settings = self.store.meta(c, "settings")
            return {"schema_version": 1, "counts": counts, "provider": {"kind": "codex_app_server", "configured": bool(settings["ai"].get("enabled"))}, "data_dir": str(self.root), "sqlite_version": sqlite3.sqlite_version}
        if name == "capabilities":
            modules = [dict(r) for r in c.execute("SELECT * FROM modules")]
            for m in modules:
                m["manifest"] = json.loads(m["manifest"])
            return {"api_version": 1, "types": list(self.types(c).values()), "commands": COMMANDS + list(self.extensions.commands), "code_extensions": self.extensions.describe(),
                    "workflows": [dict(w, module_id=m["id"]) for m in modules if m["enabled"] for w in m["manifest"].get("workflows", [])], "modules": modules}
        if name == "list":
            limit = max(1, min(500, int(p.get("limit", 100))))
            offset = max(0, int(p.get("offset", 0)))
            clauses, args = ["archived=?"], [int(bool(p.get("archived", False)))]
            types = p.get("types") or ([p["type"]] if p.get("type") else [])
            if types:
                if len(types) > 50:
                    raise BusinessError("limit", "类型筛选过多。")
                clauses.append("type IN (" + ",".join("?" for _ in types) + ")")
                args.extend(types)
            if "parent_id" in p:
                clauses.append("parent_id IS ?")
                args.append(p["parent_id"])
            if p.get("status"):
                clauses.append("status=?")
                args.append(p["status"])
            if p.get("exclude_statuses"):
                excluded = p["exclude_statuses"][:20]
                clauses.append("status NOT IN (" + ",".join("?" for _ in excluded) + ")")
                args.extend(excluded)
            if p.get("search"):
                clauses.append("(title LIKE ? ESCAPE '\\' OR data LIKE ? ESCAPE '\\')")
                pattern = "%" + str(p["search"])[:200].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                args.extend([pattern, pattern])
            where = " AND ".join(clauses)
            total = c.execute("SELECT count(*) FROM entities WHERE " + where, args).fetchone()[0]
            rows = c.execute("SELECT * FROM entities WHERE " + where + " ORDER BY updated_at DESC,id LIMIT ? OFFSET ?", [*args, limit, offset])
            return {"items": [self.store.entity(r) for r in rows], "total": total, "next_offset": offset + limit if offset + limit < total else None}
        if name == "get":
            entity = self.store.get(c, p["id"])
            children = self._query(c, "list", {"parent_id": entity["id"], "limit": 100})
            history = [dict(r) for r in c.execute("SELECT seq,revision,action,request_id,created_at FROM changes WHERE entity_id=? ORDER BY seq DESC LIMIT 30", (p["id"],))]
            return {"entity": entity, "children": children["items"], "children_total": children["total"], "links": [dict(r) for r in c.execute("SELECT l.*,a.title AS source_title,b.title AS target_title,CASE WHEN l.source_id=? THEN b.title ELSE a.title END AS other_title FROM links l JOIN entities a ON a.id=l.source_id JOIN entities b ON b.id=l.target_id WHERE l.source_id=? OR l.target_id=? LIMIT 500", (p["id"], p["id"], p["id"]))], "history": history}
        if name == "changes":
            after = max(0, int(p.get("after", 0)))
            limit = max(1, min(500, int(p.get("limit", 100))))
            items = [dict(r) for r in c.execute("SELECT seq,revision,entity_id,action,created_at FROM changes WHERE seq>? ORDER BY seq LIMIT ?", (after, limit))]
            return {"items": items, "cursor": items[-1]["seq"] if items else after, "reset_required": bool(p.get("epoch") and p["epoch"] != self.store.meta(c, "epoch"))}
        if name == 'operation':
            row = c.execute('SELECT * FROM io_operations WHERE request_id=?', (p['request_id'],)).fetchone()
            if not row:
                return {'found': False}
            operation = dict(row)
            for key in ('payload', 'result', 'error'):
                operation[key] = json.loads(operation[key]) if operation[key] else None
            return {'found': True, 'operation': operation}
        if name == "receipt":
            r = c.execute("SELECT * FROM receipts WHERE request_id=?", (p["request_id"],)).fetchone()
            if not r:
                return {"found": False}
            return {"found": True, "receipt": {"request_id": r["request_id"], "result": json.loads(r["result"]), "revision": r["revision"], "epoch": r["epoch"]}}
        if name in {"today", "plan_context"}:
            day = p.get("date") or self.today(c)
            context = self.plan_context(c, day, p.get("mode", "standard"))
            if name == "plan_context":
                return context
            plans = [self.store.entity(r) for r in c.execute("SELECT * FROM entities WHERE type='plan' AND archived=0 AND json_extract(data,'$.date')=? ORDER BY updated_at DESC LIMIT 20", (day,))]
            notices = [self.store.entity(r) for r in c.execute("SELECT * FROM entities WHERE type='notification' AND archived=0 AND (json_extract(data,'$.seen') IS NULL OR json_extract(data,'$.seen')=0) ORDER BY created_at DESC LIMIT 30")]
            return {"date": day, "tasks": context["tasks"], "events": context["hard_events"], "plans": plans, "notifications": notices, "counts": context["coverage"], "unknowns": context["unknowns"]}
        if name == "review":
            return self.review(c, p["start"], p["end"])
        if name == "jobs":
            limit = max(1, min(100, int(p.get('limit', 30))))
            offset = max(0, int(p.get('offset', 0)))
            where, args = (' WHERE status=?', [p['status']]) if p.get('status') else ('', [])
            total = c.execute('SELECT count(*) FROM jobs' + where, args).fetchone()[0]
            sql = "SELECT id,kind,status,epoch,snapshot_revision,generation,created_at,updated_at,substr(json_extract(input,'$.prompt'),1,300) AS prompt,substr(json_extract(input,'$.title'),1,300) AS title,substr(json_extract(result,'$.summary'),1,500) AS summary,substr(json_extract(error,'$.message'),1,500) AS error_message FROM jobs"
            items = []
            for row in c.execute(sql + where + ' ORDER BY created_at DESC LIMIT ? OFFSET ?', [*args, limit, offset]):
                value = dict(row)
                value['input'] = {'prompt': value.pop('prompt') or '', 'title': value.pop('title') or ''}
                value['result'] = {'summary': value.pop('summary') or ''}
                value['error'] = {'message': value.pop('error_message')} if value.get('error_message') else None
                value['detail_required'] = True
                items.append(value)
            return {'items': items, 'total': total, 'next_offset': offset + limit if offset + limit < total else None}
        if name == "job":
            row = c.execute('SELECT * FROM jobs WHERE id=?', (p['id'],)).fetchone()
            if not row:
                raise BusinessError('not_found', '作业不存在。')
            value = dict(row)
            for key in ['input', 'result', 'error']:
                value[key] = json.loads(value[key]) if value[key] else None
            return {'job': value}
        if name == "settings":
            return {"settings": self.store.meta(c, "settings"), "modules": self._query(c, "capabilities", {})["modules"]}
        if name in {'learning_summary', 'assessment_summary', 'collection_summary', 'project_summary', 'coverage_gaps'}:
            from .domains import query_domain
            return query_domain(self, c, name, p)
        if name == "warnings":
            from .scheduler import warning_scan
            items = warning_scan(self, c, p.get('date') or self.today(c))
            offset = max(0, int(p.get('offset', 0)))
            limit = max(1, min(500, int(p.get('limit', 100))))
            return {'items': items[offset:offset+limit], 'total': len(items), 'scan_complete': True, 'occurrences_complete': all(r.get('occurrences_complete', True) for r in items), 'returned_complete': offset == 0 and len(items) <= limit, 'next_offset': offset+limit if offset+limit<len(items) else None}
        if name == "diagnostics":
            return {"database_bytes": self.store.path.stat().st_size, "wal_bytes": Path(str(self.store.path) + "-wal").stat().st_size if Path(str(self.store.path) + "-wal").exists() else 0,
                    "disk_free_bytes": shutil.disk_usage(self.root).free, "sqlite_version": sqlite3.sqlite_version,
                    "pending_jobs": c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0], "schema_version": 1}
        raise BusinessError("unknown_query", "未注册的查询。", {"name": name})

    def today(self, c):
        return dt.datetime.now(ZoneInfo(self.store.meta(c, "settings")["timezone"])).date().isoformat()

    def command(self, name, payload, *, request_id, epoch, expected_revision):
        if name not in COMMANDS and name not in self.extensions.commands or not isinstance(payload, dict):
            raise BusinessError("unknown_command", "未注册的业务操作。")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128:
            raise BusinessError("request_id", "操作需要稳定的请求编号，以避免重复保存。")
        try:
            encoded = encode({"command": name, "payload": payload})
        except (TypeError, ValueError) as error:
            raise BusinessError("validation", "输入包含不可保存的数值或字段格式；本次未写入。") from error
        if len(encoded.encode()) > 256 * 1024:
            raise BusinessError("request_limit", "请求过大，请将大内容存为资料附件。")
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()

        io_command = name in {"add_source", "import_asset", "backup", "restore_backup", "export_asset"}

        def check(c, *, check_revision=True):
            state = self.store.state(c)
            if epoch != state["epoch"]:
                raise BusinessError("epoch_conflict", "数据空间已经恢复或切换，请重新读取。", state)
            old = c.execute("SELECT * FROM receipts WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise BusinessError("idempotency_conflict", "同一请求编号用于不同内容；未保存。")
                return {"request_id": request_id, "result": json.loads(old["result"]), "epoch": old["epoch"], "revision": old["revision"], "replayed": True}
            if check_revision and (type(expected_revision) is not int or expected_revision != state["revision"]):
                raise BusinessError("revision_conflict", "其他入口已更新资料。请重新读取后保留你的编辑并核对，再保存。", state)
            return None

        prepared = None
        with self.store.lock, self.store.connect() as c:
            operation = c.execute('SELECT * FROM io_operations WHERE request_id=?', (request_id,)).fetchone() if io_command else None
            previous = check(c, check_revision=not (operation and operation['status'] == 'ready'))
            if previous:
                return previous
            if operation:
                if operation['fingerprint'] != fingerprint:
                    raise BusinessError('idempotency_conflict', '同一请求编号用于不同文件操作。')
                if operation['status'] == 'ready':
                    prepared = json.loads(operation['result'])
                else:
                    raise BusinessError('operation_pending', '此文件操作已有执行记录，请先查询回执和操作状态；不会自动重复处理。', {'request_id': request_id, 'status': operation['status']})
            elif io_command:
                stamp = now()
                c.execute('INSERT INTO io_operations VALUES (?,?,?,?,?,?,?,?,?)', (request_id, fingerprint, name, 'preparing', encode(payload), None, None, stamp, stamp))
        # Long I/O retains a durable intent, without blocking ordinary writes.
        if prepared is None:
            try:
                prepared = self._prepare(name, {**payload, '_operation_id': request_id})
                if io_command:
                    with self.store.connect() as c:
                        c.execute("UPDATE io_operations SET status='ready',result=?,updated_at=? WHERE request_id=?", (encode(prepared), now(), request_id))
            except Exception as error:
                if io_command:
                    with self.store.connect() as c:
                        c.execute("UPDATE io_operations SET status='needs_reconciliation',error=?,updated_at=? WHERE request_id=?", (encode({'message': str(error)[:500]}), now(), request_id))
                if isinstance(error, (KeyError, ValueError, TypeError)):
                    raise BusinessError('validation', '输入字段不完整或格式不正确。') from error
                raise
        with self.store.lock, self.store.connect() as c:
            try:
                c.execute("BEGIN IMMEDIATE")
                previous = check(c, check_revision=not io_command)
                if previous:
                    c.rollback()
                    return previous
                result = self._dispatch(c, name, payload, request_id, prepared)
                revision = self.store.meta(c, "revision") + 1
                self.store.set_meta(c, "revision", revision)
                c.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,?)", (request_id, fingerprint, epoch, name, encode(result), revision, now()))
                if io_command:
                    c.execute("UPDATE io_operations SET status='committed',updated_at=? WHERE request_id=?", (now(), request_id))
                c.commit()
            except BusinessError:
                c.rollback()
                raise
            except (KeyError, ValueError, TypeError) as error:
                c.rollback()
                raise BusinessError("validation", "输入字段不完整或格式不正确。", {"field": str(error)[:150]}) from error
            except sqlite3.Error as error:
                c.rollback()
                raise BusinessError("storage_error", "本次未确认保存成功。请检查磁盘或稍后按原请求编号查询回执。", {"reason": str(error)[:200]}) from error
            return {"request_id": request_id, "result": result, "epoch": epoch, "revision": revision, "replayed": False}

    def _prepare(self, name, p):
        if name == 'add_source':
            from .sources import prepare
            return prepare(self,p)
        if name == 'send_message':
            from .sources import prepare_context
            return prepare_context(self,p)
        if name == 'attach_local_file':
            from .workspace import prepare_local_file
            return prepare_local_file(p)
        # Filesystem and long I/O happen before BEGIN, with the application lock.
        if name == "import_asset":
            return self.resources.import_file(p["path"])
        if name == "backup":
            def select_assets(c):
                found = {}
                for row in c.execute("SELECT data FROM entities WHERE type IN ('asset','artifact','bundle')"):
                    d = json.loads(row[0])
                    for item in [d, *d.get("entries", [])]:
                        if item.get("sha256"):
                            found[item["sha256"]] = item
                return list(found.values())
            return self.resources.create_backup(self.store.path, select_assets, app_lock=self.store.lock, metadata={"app_version": "0.7.0", "operation_id": p.get("_operation_id")})
        if name == "restore_backup":
            target = Path(p["target_dir"]).resolve()
            if target == self.root or self.root in target.parents:
                raise BusinessError("validation", "恢复位置必须是当前数据空间之外的新目录。")
            result = self.resources.restore_backup(p["path"], target)
            restored = Store(target, allow_pending_restore=True)
            with restored.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                restored.set_meta(c, "epoch", new_id())
                c.execute("UPDATE jobs SET status='cancelled',generation=generation+1,error=? WHERE status IN ('queued','running','awaiting_review')", (encode({"message": "恢复后需重新创建或核对作业。"}),))
                from .conversations import reset_after_restore
                reset_after_restore(c)
                # Existing configured schedules must be deliberately re-enabled after restore.
                for row in c.execute("SELECT id,data FROM entities WHERE type IN ('schedule','recurring_rule')").fetchall():
                    restored_data = json.loads(row['data'])
                    restored_data['enabled'] = False
                    restored_data['restore_review_required'] = True
                    c.execute('UPDATE entities SET data=?,version=version+1 WHERE id=?', (encode(restored_data), row['id']))
                c.commit()
            marker = target / 'restore_pending.json'
            if marker.exists():
                marker.rename(target / 'restore_reconciled.json')
            return {"target_dir": str(target), "report": result, "message": "已恢复到新的数据空间；从启动器选择该目录后使用。"}
        if name == "export_asset":
            with self.store.connect() as c:
                e = self.store.get(c, p["id"])
            if e["type"] not in {"asset", "artifact", "bundle"}:
                raise BusinessError("validation", "此记录不是可导出的资料。")
            entries = e["data"].get("entries") or [{"path": e["data"].get("original_name", e["title"]), "sha256": e["data"]["sha256"]}]
            # Materialize into a fresh managed workspace before exporting.
            bundle = self.resources.materialize_bundle(entries)
            output = Path(p["target_dir"]).resolve()
            if output.exists():
                raise BusinessError("exists", "导出目录已存在，请指定新的目录，避免覆盖文件。")
            src = Path(bundle["path"] if isinstance(bundle, dict) else bundle)
            output.mkdir(parents=True, exist_ok=False)
            for entry in entries:
                member = Path(entry['path'])
                destination = output / member
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src / member, destination)
            return {"path": str(output)}
        return None

    def _dispatch(self, c, name, p, rid, prepared=None):
        if name in {"delete_task", "restore_task"}:
            from .tasks_delete import delete_task, restore_task
            return (delete_task if name == "delete_task" else restore_task)(self, c, p, rid)
        if name == "apply_timetable":
            from .timetable import apply_timetable
            return apply_timetable(self, c, p, rid)
        if name in {"set_recovery_task", "record_recovery_progress"}:
            from .catchup import set_recovery_task, record_recovery_progress
            return (set_recovery_task if name == "set_recovery_task" else record_recovery_progress)(self, c, p, rid)
        if name == 'add_to_plan':
            from .daily_flow import add_to_plan
            return add_to_plan(self, c, p, rid)
        if name in {'set_recurring_rule', 'materialize_recurring'}:
            from .recurring import set_rule, materialize
            return (set_rule if name == 'set_recurring_rule' else materialize)(self, c, p, rid)
        if name == 'add_source':
            from .sources import add
            return add(self,c,p,rid,prepared)
        if name == 'send_message':
            from .conversations import send
            return send(self,c,p,rid,prepared)
        if name == 'submit_daily_review':
            from .reviews import submit_daily
            return submit_daily(self,c,p,rid)
        if name == 'set_review_preferences':
            from .workspace import set_review_preferences
            return set_review_preferences(self,c,p,rid)
        if name == 'attach_local_file':
            from .workspace import attach_local_file
            return attach_local_file(self,c,p,rid,prepared)
        if name in self.extensions.commands:
            definition = self.extensions.commands[name]
            self.extensions.validate(definition, p)
            return definition.handler(self, c, p, rid)
        if name == "create":
            data = p.get("data") or {}
            if p.get("type") == "event" and any(k in data for k in ("timetable_id", "timetable_row_key", "semester_start", "semester_end", "teaching_weeks", "timetable_enabled", "week_numbering", "recess_weeks")):
                raise BusinessError("dedicated_command", "课表固定时段请通过每周课表登记，保证教学周与来源一致。")
            if p.get("type") == "task" and (data.get("task_kind") == "catchup" or any(k.startswith("catchup_") for k in data)):
                raise BusinessError("dedicated_command", "补课与补欠请通过专用登记流程创建，以保留依据和进度。")
            if p.get('type') == 'timetable' and p.get('parent_id') is None and p.get('status', 'active') == 'active':
                from .timetable import validate_timetable_metadata
                metadata = validate_timetable_metadata(data)
                title = p.get('title')
                if not isinstance(title, str) or not title.strip() or len(title) > 300:
                    raise BusinessError('validation', '请填写有效的课表名称。')
                if metadata.get('semester_start') and metadata.get('semester_end'):
                    matches = c.execute("SELECT * FROM entities WHERE type='timetable' AND archived=0 AND status='active' AND trim(title)=? COLLATE NOCASE AND json_extract(data,'$.semester_start')=? AND json_extract(data,'$.semester_end')=? ORDER BY created_at,id LIMIT 2", (title.strip(), metadata['semester_start'], metadata['semester_end'])).fetchall()
                    if len(matches) > 1:
                        raise BusinessError('timetable_duplicate_container', '存在多份同名同范围的课表，请从课表名称菜单选择原课表更新。')
                    if matches:
                        return {'entity': self.store.entity(matches[0]), 'reused': True}
            if p["type"] in RESERVED_TYPES:
                raise BusinessError("dedicated_command", "此类型需要使用对应业务流程创建。")
            return {"entity": self._create(c, p, rid)}
        if name == "update":
            old = self._versioned(c, p)
            if old["type"] in {"feedback", "asset", "artifact", "bundle", "review", "checkin", "plan", "recurring_rule"}:
                raise BusinessError("immutable", "该记录有证据或版本含义；请新增反馈或使用专用流程，原版本会保留。")
            patch = p["patch"]
            updates = patch.get("data") or {}
            if old["type"] == "timetable" and any(k in updates and updates[k] != old["data"].get(k) for k in ("semester_start", "semester_end", "timezone", "week_numbering", "recess_weeks")):
                if c.execute("SELECT 1 FROM entities WHERE type='event' AND json_extract(data,'$.timetable_id')=? LIMIT 1", (old['id'],)).fetchone():
                    raise BusinessError("dedicated_command", "已有课表时段；请通过每周课表同步调整学期范围，避免时段与课表不一致。")
            if old["type"] == "event" and old["data"].get("timetable_id") and any(k in updates and updates[k] != old["data"].get(k) for k in ("date", "recurrence", "until", "timezone", "hard", "time_kind")):
                raise BusinessError("dedicated_command", "课表的星期、学期范围和时区请在每周课表中修改，保证固定框架保持一致。")
            if old["type"] == "event" and any(k in updates and updates[k] != old["data"].get(k) for k in ("timetable_id", "timetable_row_key", "semester_start", "semester_end", "teaching_weeks", "timetable_enabled", "timetable_weekday", "week_numbering", "recess_weeks")):
                raise BusinessError("dedicated_command", "课表教学周和系列归属请通过每周课表调整。")
            if old["type"] == "task" and (any(k.startswith("catchup_") for k in updates) or updates.get("task_kind") == "catchup" and old["data"].get("task_kind") != "catchup"):
                raise BusinessError("dedicated_command", "补欠范围和计量设置请使用专用登记流程修改。")
            if old["type"] == "task" and old["data"].get("catchup_enabled") and "completion_gate" in updates and updates["completion_gate"] != old["data"].get("completion_gate"):
                raise BusinessError("completion_gate", "已登记的补欠完成条件会保留，请另建不同范围的任务，避免旧进度被误用。")
            if set(patch) - {"title", "status", "data"}:
                raise BusinessError("validation", "归属请通过移动操作修改。")
            new = copy.deepcopy(old)
            new.update({k: v for k, v in patch.items() if k != "data"})
            new["data"].update(patch.get("data", {}))
            for f in self.types(c)[old['type']].get('fields', []):
                if f.get('read_only') and new['data'].get(f['id']) != old['data'].get(f['id']):
                    raise BusinessError('read_only', '该字段所属模块未启用，原内容仍然保留。')
            self._validate(c, new)
            return {"entity": self._save(c, old, new, rid, "update")}
        if name == "move":
            old = self._versioned(c, p)
            if old["type"] == "recurring_rule":
                raise BusinessError("dedicated_command", "准备规则的归属跟随关联节点，请通过固定准备事项编辑。")
            new = {**old, "parent_id": p.get("parent_id")}
            self._validate(c, new)
            return {"entity": self._save(c, old, new, rid, "move")}
        if name == "archive":
            old = self._versioned(c, p)
            if p.get("archived") and c.execute("SELECT 1 FROM entities WHERE parent_id=? AND archived=0 LIMIT 1", (old["id"],)).fetchone():
                raise BusinessError("active_children", "请先处理仍可见的子项；归档不会自动隐藏它们。")
            if p.get('archived') and c.execute("SELECT 1 FROM entities WHERE type='event' AND archived=0 AND status NOT IN ('done','cancelled') AND json_extract(data,'$.owner_id')=? LIMIT 1", (old['id'],)).fetchone():
                raise BusinessError('active_events', '此事项还有未结束的关联日程，请先独立处理日程；不会随归档自动取消。')
            if p.get('archived') and old['type'] == 'timetable' and c.execute("SELECT 1 FROM entities WHERE type='event' AND archived=0 AND status NOT IN ('done','cancelled') AND json_extract(data,'$.timetable_id')=? AND json_extract(data,'$.timetable_enabled') IS NOT 0 LIMIT 1", (old['id'],)).fetchone():
                raise BusinessError('active_events', '课表仍有启用的固定时段，请先在课表中停用或结束这些时段，再归档。')
            new = {**old, "archived": bool(p["archived"])}
            if not new["archived"]:
                self._validate(c, new)
            return {"entity": self._save(c, old, new, rid, "archive")}
        if name == "promote_checklist":
            old = self._versioned(c, p)
            if old["type"] != "checklist":
                raise BusinessError("validation", "只能将清单项提升为任务。")
            new = {**old, "type": "task", "data": {**old["data"], "promoted_from": "checklist"}}
            return {"entity": self._save(c, old, new, rid, "promote")}
        if name == "link":
            a, b = self.store.get(c, p["source_id"]), self.store.get(c, p["target_id"])
            self._ensure_mutable(c, a)
            self._ensure_mutable(c, b)
            kind = p["kind"]
            if kind not in {"depends_on", "references", "evidence", "supports", "contributes", "supersedes", "uses"} or a["id"] == b["id"]:
                raise BusinessError("validation", "关联类型或目标无效。")
            if kind == "depends_on":
                if a["type"] in {"checklist", "milestone"} or b["type"] == "checklist":
                    raise BusinessError("validation", "清单项没有独立依赖；请先提升为任务。")
                cycle = c.execute("WITH RECURSIVE reach(id) AS (SELECT target_id FROM links WHERE source_id=? AND kind='depends_on' UNION SELECT l.target_id FROM links l JOIN reach r ON l.source_id=r.id WHERE l.kind='depends_on') SELECT 1 FROM reach WHERE id=?", (b["id"], a["id"])).fetchone()
                if cycle:
                    raise BusinessError("cycle", "依赖形成循环，未保存。")
            id = new_id()
            try:
                c.execute("INSERT INTO links VALUES (?,?,?,?,?)", (id, a["id"], b["id"], kind, now()))
            except sqlite3.IntegrityError:
                raise BusinessError("duplicate", "这项关联已经存在。")
            self.store.change(c, rid, "link", after={"id": a["id"], "link_id": id})
            return {"id": id}
        if name == "unlink":
            row = c.execute("SELECT * FROM links WHERE id=?", (p["id"],)).fetchone()
            if not row:
                raise BusinessError("not_found", "关联不存在。")
            c.execute("DELETE FROM links WHERE id=?", (p["id"],))
            self.store.change(c, rid, "unlink", before={"id": row["source_id"], "link_id": p["id"]})
            return {"id": p["id"]}
        if name == "record_feedback":
            return {"entity": self.feedback(c, p, rid)}
        if name == "create_plan":
            return {"entity": self.create_plan(c, p, rid)}
        if name == "create_checkin":
            from .reviews import legacy_checkin
            return legacy_checkin(self,c,p,rid)
        if name == "respond_checkin":
            checkin = self.store.get(c, p["id"])
            if checkin["type"] != "checkin":
                raise BusinessError("validation", "需要回答对应的询问记录。")
            questions = {q["id"]: q for q in checkin["data"]["questions"]}
            results = []
            seen = set()
            for a in p["answers"]:
                if a["question_id"] not in questions or a["question_id"] in seen:
                    raise BusinessError("validation", "回答编号不属于这一轮询问，或同一项重复出现。")
                seen.add(a["question_id"])
                results.append(self.feedback(c, {"target_id": questions[a["question_id"]]["target_id"], "business_date": checkin["data"]["date"], "dimensions": a["dimensions"], "source_text": p.get("source_text", ""), "checkin_id": checkin["id"], "question_id": a["question_id"]}, rid))
            return {"entities": results, "business_date": checkin["data"]["date"]}
        if name == "save_review":
            metrics = self.review(c, p["start"], p["end"])
            return {"entity": self._create(c, {"type": "review", "title": p.get("title") or f"{p['start']} 至 {p['end']} 回顾", "data": {"start": p["start"], "end": p["end"], "content": p.get("text", ""), "snapshot": metrics}}, rid)}
        if name == "settings":
            old = self.store.meta(c, "settings")
            value = copy.deepcopy(old)
            value.setdefault("favorites", [])
            from .appearance import DEFAULT_APPEARANCE, normalize_appearance
            value.setdefault("appearance", copy.deepcopy(DEFAULT_APPEARANCE))
            value.setdefault("charts", {"weekly_style":"columns"})
            from .timetable import normalize_timetable_defaults
            value.setdefault("timetable_defaults", normalize_timetable_defaults({}))
            updates = p["settings"]
            if set(updates) - set(value):
                raise BusinessError("validation", "未知的系统设置。")
            for k, v in updates.items():
                if k == "appearance":
                    value[k] = normalize_appearance(v, current=value[k])
                elif k == "timetable_defaults":
                    if not isinstance(v, dict):
                        raise BusinessError("validation", "课表设置格式无效。")
                    value[k] = normalize_timetable_defaults({**value[k], **v})
                elif k == "charts":
                    if not isinstance(v,dict) or set(v)-{"weekly_style"} or v.get("weekly_style") not in {"columns","rows"}:
                        raise BusinessError("validation","请选择支持的每周回顾图表样式。")
                    value[k].update(v)
                elif k == "ai":
                    if not isinstance(v, dict) or set(v) - set(value["ai"]):
                        raise BusinessError("validation", "AI 设置无效。")
                    value[k].update(v)
                else:
                    value[k] = v
            try:
                ZoneInfo(value["timezone"])
            except (ZoneInfoNotFoundError, TypeError):
                raise BusinessError("validation", "未知时区。")
            if not 10 <= int(value["ai"]["timeout_seconds"]) <= 900:
                raise BusinessError("validation", "AI 超时范围为 10 到 900 秒。")
            if not isinstance(value['favorites'], list) or len(value['favorites']) > 1000 or any(not isinstance(x, str) or len(x) > 128 for x in value['favorites']):
                raise BusinessError('validation', '收藏应为有限的记录编号列表。')
            if not 1 <= int(value['page_size']) <= 500 or not 4096 <= int(value['context_characters']) <= 48000:
                raise BusinessError('validation', '分页或上下文预算超出支持范围。')
            if int(value["reserve_bytes"]) < 1024 ** 3:
                raise BusinessError("validation", "磁盘保留空间不得低于 1 GiB。")
            self.store.set_meta(c, "settings", value)
            self.store.change(c, rid, "settings")
            return {"settings": value}
        if name == "install_module":
            m = validate_manifest(p["manifest"], self.types(c))
            old = c.execute("SELECT * FROM modules WHERE id=?", (m["id"],)).fetchone()
            if old and m["version"] <= old["version"]:
                raise BusinessError("version", "新模块版本必须高于已安装版本。")
            if old:
                prior = json.loads(old["manifest"])
                for field in prior.get('fields', []):
                    if field not in m.get('fields', []):
                        raise BusinessError('migration_required', '移除或改变已有扩展字段需要显式迁移。')
                old_types = {t["id"]: t for t in prior.get("types", [])}
                for key, t in old_types.items():
                    new_t = next((x for x in m.get("types", []) if x["id"] == key), None)
                    if new_t is None or any(f not in new_t.get("fields", []) for f in t.get("fields", [])):
                        raise BusinessError("migration_required", "删除或修改已有类型结构需要显式迁移；本次未改动数据。")
            c.execute("INSERT INTO modules VALUES (?,?,1,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,enabled=1,manifest=excluded.manifest", (m["id"], m["version"], encode(m)))
            self.store.change(c, rid, "install_module")
            return {"module": m}
        if name == "disable_module":
            if not c.execute("SELECT 1 FROM modules WHERE id=?", (p["id"],)).fetchone():
                raise BusinessError("not_found", "模块不存在。")
            c.execute("UPDATE modules SET enabled=0 WHERE id=?", (p["id"],))
            self.store.change(c, rid, "disable_module")
            return {"id": p["id"], "read_only": True}
        if name == "run_workflow":
            row = c.execute("SELECT manifest FROM modules WHERE id=? AND enabled=1", (p["module_id"],)).fetchone()
            if not row:
                raise BusinessError("not_found", "模块未启用。")
            w = next((w for w in json.loads(row[0]).get("workflows", []) if w["id"] == p["workflow_id"]), None)
            if not w:
                raise BusinessError("not_found", "工作流不存在。")
            payload = {**w.get("defaults", {}), **p.get("input", {})}
            if "data" in w.get("defaults", {}):
                payload["data"] = {**w["defaults"]["data"], **p.get("input", {}).get("data", {})}
            return self._dispatch(c, w["action"], payload, rid)
        if name == 'create_notebook_from_pdf':
            source = self.store.get(c, p['asset_id'])
            if source['type'] not in {'asset', 'artifact'}:
                raise BusinessError('validation', '请选择已导入的 PDF 资料。')
            pages = p['pages']
            if not isinstance(pages, list) or not 1 <= len(pages) <= 20 or any(type(n) is not int or n < 1 for n in pages):
                raise BusinessError('validation', '页码应为最多 20 个正整数。')
            value = {'kind': 'pdf_notebook', 'title': p.get('title') or source['title'] + ' 练习', 'relative_path': p.get('relative_path', '练习.ipynb'), 'content': '', 'pages': pages, 'source_sha256': source['data']['sha256'], 'source_versions': [{'id': source['id'], 'version': source['version'], 'sha256': source['data']['sha256']}]}
            return {'job': self.create_job(c, 'create_artifact_job', value, rid)}
        if name in {"create_job", "create_artifact_job"}:
            if name == 'create_job' and p.get('kind') == 'ai':
                internal={'conversation_id','conversation_scope','provider_thread_id','history','local_images','source_versions','source_ids'}
                if internal & set(p.get('input') or {}):
                    raise BusinessError('proposal_scope','会话与图片输入必须由正式讨论入口根据已保存资料建立。')
            return {"job": self.create_job(c, name, p, rid)}
        if name == "cancel_job":
            job = self._job(c, p["id"])
            if job["status"] not in {"queued", "running", "awaiting_review"}:
                raise BusinessError("job_state", "作业已结束，无法取消。")
            c.execute("UPDATE jobs SET status='cancelled',generation=generation+1,updated_at=? WHERE id=?", (now(), p["id"]))
            event = self.cancel_events.get(p["id"])
            if event:
                event.set()
            from .conversations import update_job
            update_job(self,c,job,"cancelled")
            self.store.change(c, rid, "cancel_job")
            return {"id": p["id"], "status": "cancelled"}
        if name == "apply_proposal":
            job = self._job(c, p["id"])
            from .conversations import validate_current_proposal,mark_applied
            validate_current_proposal(self,c,job)
            if job["status"] != "awaiting_review" or job["epoch"] != self.store.meta(c, "epoch"):
                raise BusinessError("job_state", "候选已失效、取消或处理。")
            if job["snapshot_revision"] != self.store.meta(c, "revision"):
                raise BusinessError("stale_proposal", "推理后业务资料已经变化，请重新生成候选。原候选已保留。")
            actions = json.loads(job["result"]).get("actions", [])
            if len(actions) > 30:
                raise BusinessError("limit", "候选操作数量过多。")
            results = []
            for action in actions:
                if action["command"] not in {"apply_timetable", "create", "update", "record_feedback", "create_plan", "save_review", "set_recurring_rule", "set_recovery_task", "record_recovery_progress"}:
                    raise BusinessError("proposal_scope", "候选含有未授权的操作。")
                from .sources import apply_source_action
                results.append(apply_source_action(self,c,job,action,rid))
            c.execute("UPDATE jobs SET status='applied',updated_at=? WHERE id=?", (now(), p["id"]))
            mark_applied(self,c,job)
            return {"results": results, "job_id": p["id"]}
        if name == "import_asset":
            data = {**prepared, "original_name": Path(p["path"]).name, "source_text": p.get("source_text", "")}
            entity = self._create(c, {"type": "asset", "title": p.get("title") or Path(p["path"]).name, "data": data}, rid)
            if p.get("owner_id"):
                self._dispatch(c, "link", {"source_id": p["owner_id"], "target_id": entity["id"], "kind": "uses"}, rid)
            return {"entity": entity}
        if name == "create_bundle":
            entries, paths = [], set()
            for item in p["entries"]:
                path = str(item["path"]).replace("\\", "/")
                if not path or path.startswith("/") or ":" in path or any(part in {"", ".", ".."} for part in path.split("/")) or path.casefold() in paths:
                    raise BusinessError("validation", "资料包包含无效或重复的相对路径。")
                paths.add(path.casefold())
                e = self.store.get(c, item["asset_id"])
                if e["type"] not in {"asset", "artifact"}:
                    raise BusinessError("validation", "包成员必须是已有资料版本。")
                entries.append({"path": path, "asset_id": e["id"], "sha256": e["data"]["sha256"], "size": e["data"]["size"]})
            return {"entity": self._create(c, {"type": "bundle", "title": p["title"], "data": {"entries": entries}}, rid)}
        if name in {"backup", "restore_backup", "export_asset"}:
            self.store.change(c, rid, name)
            return prepared
        if name == "adopt_artifact":
            old = self._versioned(c, p)
            if old["type"] != "artifact" or not old["data"].get("sha256"):
                raise BusinessError("validation", "只有已固化的成果版本可采纳。")
            return {"entity": self._save(c, old, {**old, "data": {**old["data"], "adopted_at": now()}}, rid, "adopt_artifact")}
        if name == "undo":
            return self.undo(c, p, rid)
        raise BusinessError("unknown_command", "此操作尚未注册。")

    def _versioned(self, c, p):
        old = self.store.get(c, p['id'])
        self._ensure_mutable(c, old)
        if p.get('version') != old['version']:
            raise BusinessError('entity_conflict', '这条记录已被修改，请重新读取并核对你的编辑。', {'current_version': old['version']})
        return old

    def _create(self, c, p, rid):
        timestamp = now()
        entity = {'id': new_id(), 'type': p['type'], 'title': p['title'], 'parent_id': p.get('parent_id'), 'status': p.get('status', 'active'), 'archived': False, 'data': copy.deepcopy(p.get('data', {})), 'version': 1, 'created_at': timestamp, 'updated_at': timestamp}
        self._validate(c, entity)
        c.execute('INSERT INTO entities VALUES (?,?,?,?,?,?,?,?,?,?)', (entity['id'], entity['type'], entity['title'], entity['parent_id'], entity['status'], 0, encode(entity['data']), 1, timestamp, timestamp))
        self.store.change(c, rid, 'create', after=entity)
        return entity

    def _save(self, c, old, new, rid, action):
        new = copy.deepcopy(new)
        new['version'], new['updated_at'] = old['version'] + 1, now()
        c.execute('UPDATE entities SET type=?,title=?,parent_id=?,status=?,archived=?,data=?,version=?,updated_at=? WHERE id=?', (new['type'], new['title'], new['parent_id'], new['status'], int(new['archived']), encode(new['data']), new['version'], new['updated_at'], old['id']))
        self.store.change(c, rid, action, old, new)
        return new

    def _validate(self, c, e):
        definition = self.types(c).get(e['type'])
        if not definition:
            raise BusinessError('unknown_type', '业务类型未注册；请先安装对应模块。')
        if definition.get('read_only'):
            raise BusinessError('read_only', '所属模块未启用；历史资料仍可读取。')
        if not isinstance(e['title'], str) or not e['title'].strip() or len(e['title']) > 300:
            raise BusinessError('validation', '标题不能为空，且应不超过 300 字符。')
        e['title'] = e['title'].strip()
        if e['status'] not in definition.get('statuses', TYPES['task']['statuses']):
            raise BusinessError('validation', '未知的状态。')
        if not isinstance(e['data'], dict) or len(encode(e['data']).encode()) > 128 * 1024:
            raise BusinessError('limit', '单条记录过大，请将大内容作为资料附件保存。')
        for f in definition.get('fields', []):
            value = e['data'].get(f['id'])
            if value is None or value == '':
                if f.get('required'):
                    raise BusinessError('validation', '请填写' + f['label'] + '。')
                continue
            kind, valid = f.get('type', 'text'), True
            if kind in {'text', 'multiline'}:
                valid = isinstance(value, str) and len(value) <= 60000
            elif kind == 'selection':
                valid = isinstance(value, str) and len(value) <= 200 or isinstance(value, list) and len(value) <= 30 and all(isinstance(x,str) and 0<len(x)<=100 for x in value)
            elif kind == 'integer':
                valid = type(value) is int and value >= 0
            elif kind == 'number':
                valid = type(value) in (float, int) and value >= 0
            elif kind == 'boolean':
                valid = type(value) is bool
            elif kind == 'choice':
                valid = value in f.get('options', [])
            elif kind == 'date':
                date_value(value, f['label'])
            elif kind == 'time':
                time_value(value)
            if not valid:
                raise BusinessError('validation', f['label'] + '的值不符合字段格式。')
        parent = self.store.get(c, e['parent_id']) if e.get('parent_id') else None
        if (parent['type'] if parent else None) not in definition['parent_types']:
            raise BusinessError('parent_type', '此对象不能放在选择的父级下。')
        if parent and parent['archived']:
            raise BusinessError('archived_parent', '父级已归档，请先恢复父级。')
        depth, cursor, seen = 0, parent, {e['id']}
        while cursor:
            if cursor['id'] in seen:
                raise BusinessError('cycle', '移动会形成父子循环。')
            seen.add(cursor['id'])
            depth += 1
            if depth >= 32:
                raise BusinessError('depth', '层级超过 32 层，请简化归属或使用关联。')
            cursor = self.store.get(c, cursor['parent_id']) if cursor['parent_id'] else None
        height = c.execute('WITH RECURSIVE d(id,n) AS (SELECT id,1 FROM entities WHERE parent_id=? UNION ALL SELECT e.id,d.n+1 FROM entities e JOIN d ON e.parent_id=d.id WHERE d.n<33) SELECT coalesce(max(n),0) FROM d', (e['id'],)).fetchone()[0]
        if depth + height >= 32:
            raise BusinessError('depth', '移动后的整个分支超过层级限制。')
        data = e['data']
        if e['type'] == 'timetable':
            from .timetable import validate_timetable_metadata
            validate_timetable_metadata(data)
        if e['type'] in {'event', 'schedule'} and data.get('timezone'):
            try:
                ZoneInfo(data['timezone'])
            except ZoneInfoNotFoundError:
                raise BusinessError('validation', '时区无效。')
        if e['type'] == 'event':
            if data.get('timetable_id') and data.get('start') and data.get('end') and data['start'] == data['end']:
                raise BusinessError('timetable_time', '课表开始和结束不能相同；请明确时段，不能视为全天。')
            if data.get('owner_id'):
                owner = self.store.get(c, data['owner_id'])
                if owner['type'] not in {'domain', 'project', 'course', 'activity', 'phase'} or owner['archived']:
                    raise BusinessError('event_owner', '请选择仍在使用的领域、项目、课程、活动或阶段作为事件归属。')
            if data.get('until') and data.get('date') and data['until'] < data['date']:
                raise BusinessError('validation', '重复结束日期不能早于开始日期。')
            if data.get('exceptions'):
                if not isinstance(data['exceptions'], dict) or len(data['exceptions']) > 366:
                    raise BusinessError('validation', '周期例外需要按日期记录。')
                for day, exception in data['exceptions'].items():
                    date_value(day)
                    if not isinstance(exception, dict) or set(exception) - {'cancelled', 'start', 'end', 'personal_choice', 'replacement_target_id'}:
                        raise BusinessError('validation', '周期例外格式无效。')
                    if 'cancelled' in exception and type(exception['cancelled']) is not bool:
                        raise BusinessError('validation', '单次停课需要明确的是或否。')
                    if data.get('timetable_id'):
                        occurrence = date_value(day)
                        if not data['semester_start'] <= day <= data['semester_end'] or occurrence.weekday() != date_value(data['date']).weekday():
                            raise BusinessError('timetable_exception', '单次例外必须在本课表行的星期和学期范围内。')
                        start, end = exception.get('start', data.get('start')), exception.get('end', data.get('end'))
                        if start and end and start == end:
                            raise BusinessError('timetable_time', '单次时段开始和结束不能相同。')
                    for key in ('start', 'end'):
                        if exception.get(key):
                            time_value(exception[key])
        if e['type'] == 'schedule' and data.get('enabled'):
            if not data.get('time') or not data.get('workflow') or not data.get('frequency'):
                raise BusinessError('validation', '启用定时任务前请设置时间、频率和工作流。')
            if data.get('frequency') == 'weekly' and data.get('weekday') not in range(7):
                raise BusinessError('validation', '星期范围为 0 到 6。')
        for row in c.execute('SELECT manifest FROM modules WHERE enabled=1'):
            for rule in json.loads(row[0]).get('rules', []):
                if rule.get('target_type') != e['type']:
                    continue
                value, op = data.get(rule['field']), rule['operator']
                ok = True
                if op == 'required':
                    ok = value is not None and value != ''
                elif value is not None:
                    try:
                        ok = {'min': lambda: value >= rule['value'], 'max': lambda: value <= rule['value'], 'one_of': lambda: value in rule['value']}[op]()
                    except (TypeError, KeyError):
                        ok = False
                if not ok:
                    raise BusinessError('rule_conflict', rule.get('message', '内容不符合已启用的业务规则。'))

    def feedback(self, c, p, rid):
        target = self.store.get(c, p['target_id'])
        if target['type'] == 'task' and target['archived']:
            raise BusinessError('task_deleted', '任务已删除，请先恢复后再更正反馈。')
        self._ensure_mutable(c, target)
        day = date_value(p['business_date']).isoformat()
        validate_dimensions(p['dimensions'])
        if not isinstance(p.get('source_text'), str) or not p['source_text'].strip():
            raise BusinessError('source_required', '反馈需要保留用户原话或明确填写的来源。')
        data = {'target_id': target['id'], 'target_version': target['version'], 'business_date': day, 'dimensions': p['dimensions'], 'source_text': p['source_text'], 'reported_at': now()}
        for key in ('checkin_id', 'question_id', 'supersedes_id'):
            if p.get(key):
                data[key] = p[key]
        if data.get('supersedes_id'):
            prior = self.store.get(c, data['supersedes_id'])
            if prior['type'] != 'feedback' or prior['data']['target_id'] != target['id'] or prior['data']['business_date'] != day:
                raise BusinessError('validation', '更正必须对应同一对象、同一业务日期的反馈。')
        return self._create(c, {'type': 'feedback', 'title': day + ' · ' + target['title'], 'data': data}, rid)

    def _events(self, c, day):
        target_date = date_value(day)
        zone = ZoneInfo(self.store.meta(c, 'settings')['timezone'])
        events, unknowns = [], []
        from .timetable import occurs_on
        from .event_time import local_datetime
        lo = dt.datetime.combine(target_date, dt.time(), zone)
        hi = lo + dt.timedelta(days=1)
        # Complete scan: paging candidate tasks must never omit hard constraints.
        for row in c.execute("SELECT * FROM entities WHERE type='event' AND archived=0 AND status!='cancelled'"):
            entity = self.store.entity(row)
            d = entity['data']
            if not d.get('hard', True):
                continue
            if not d.get('date'):
                unknowns.append({'id': entity['id'], 'title': entity['title'], 'reason': '日程日期未知'})
                continue
            origin = date_value(d['date'])
            source_zone = ZoneInfo(d.get('timezone') or str(zone))
            first = max(1, lo.astimezone(source_zone).date().toordinal() - 1)
            last = (hi - dt.timedelta(microseconds=1)).astimezone(source_zone).date().toordinal()
            for ordinal in sorted(set(range(first, last + 1)) | {target_date.toordinal()}):
                candidate = dt.date.fromordinal(ordinal)
                if candidate < origin or (d.get('until') and candidate > date_value(d['until'])):
                    continue
                repeat = d.get('recurrence', 'none')
                occurs = candidate == origin or repeat == 'daily' or repeat == 'weekly' and candidate.weekday() == origin.weekday() or repeat == 'monthly' and candidate.day == origin.day
                if not occurs or not occurs_on(d, candidate):
                    continue
                exception = d.get('exceptions', {}).get(candidate.isoformat(), {})
                if exception.get('cancelled'):
                    continue
                values = {**d, **exception}
                if not values.get('start') or not values.get('end') or values.get('time_kind') in {'date_only', 'approximate', 'unknown'}:
                    if candidate == target_date:
                        unknowns.append({'id': entity['id'], 'title': entity['title'], 'reason': '固定日程时段待确认'})
                        events.append({**entity, 'occurrence_date': candidate.isoformat(), 'start_minute': None, 'end_minute': None})
                    continue
                a, b = time_value(values['start']), time_value(values['end'])
                try:
                    start = local_datetime(candidate, a, source_zone).astimezone(zone)
                    end = local_datetime(candidate + dt.timedelta(days=int(b <= a)), b, source_zone).astimezone(zone)
                except ValueError:
                    approximate = dt.datetime.combine(candidate, dt.time(a // 60, a % 60), source_zone).astimezone(zone)
                    if approximate < hi and approximate + dt.timedelta(days=1) > lo:
                        unknowns.append({'id': entity['id'], 'title': entity['title'], 'reason': '夏令时转换导致日程时间不存在或有歧义，请确认具体时段'})
                        events.append({**entity, 'occurrence_date': candidate.isoformat(), 'start_minute': None, 'end_minute': None})
                    continue
                if start < hi and end > lo:
                    events.append({**entity, 'occurrence_date': candidate.isoformat(), 'start_minute': max(0, int((start - lo).total_seconds() // 60)), 'end_minute': min(1440, int((end - lo).total_seconds() // 60)), 'effective': values})
        return events, unknowns

    def _rules(self, c, day):
        result = []
        for row in c.execute("SELECT * FROM entities WHERE type='rule' AND archived=0 AND status NOT IN ('cancelled','draft')"):
            e = self.store.entity(row)
            d = e['data']
            if (d.get('effective_from') or day) <= day <= (d.get('effective_until') or day):
                result.append(e)
        return result

    def plan_context(self, c, day, mode):
        date_value(day)
        events, unknowns = self._events(c, day)
        tasks = self._query(c, 'list', {'type': 'task', 'limit': 100, 'exclude_statuses': ['done', 'cancelled']})
        active = [t for t in tasks['items'] if t['status'] not in {'done', 'cancelled'}]
        rules, protected, capacity = self._rules(c, day), [], None
        for rule in rules:
            d = rule['data']
            if rule['parent_id'] is not None or rule['status'] in {'done', 'cancelled', 'draft'} or rule['data'].get('enabled', True) is False or any(rule['data'].get(key) is not None and rule['data'].get(key) != '' for key in ('target_types','event_kind','task_kind')):
                continue
            if d.get('rule_kind') == 'capacity' and d.get('minutes') is not None:
                capacity = min(capacity, d['minutes']) if capacity is not None else d['minutes']
            if d.get('rule_kind') == 'protected_time' and d.get('start') and d.get('end'):
                a, b = time_value(d['start']), time_value(d['end'])
                spans = [(a, b)] if b > a else [(0, b), (a, 1440)]
                protected.extend({'id': rule['id'], 'title': rule['title'], 'start_minute': x, 'end_minute': y} for x, y in spans)
        occupied = sorted([(e['start_minute'], e['end_minute']) for e in events if e['start_minute'] is not None] + [(e['start_minute'], e['end_minute']) for e in protected])
        windows, cursor = [], 0
        for a, b in occupied:
            if cursor < a:
                windows.append({'start_minute': cursor, 'end_minute': a})
            cursor = max(cursor, b)
        if cursor < 1440:
            windows.append({'start_minute': cursor, 'end_minute': 1440})
        return {'date': day, 'mode': mode, 'tasks': active, 'hard_events': events, 'protected_times': protected, 'free_windows': windows, 'capacity_minutes': capacity, 'rules': rules, 'unknowns': unknowns, 'coverage': {'tasks_total': tasks['total'], 'tasks_returned': len(active), 'tasks_paged': tasks['next_offset'] is not None, 'hard_constraints_complete': True}, 'guidance': '计划时间为软估计，完成门保持不变。未知时段不可当作空闲。分页候选不代表完整任务集合。'}

    def create_plan(self, c, p, rid, *, _retained=()):
        day, mode = date_value(p['date']).isoformat(), p.get('mode', 'standard')
        if mode not in {'standard', 'low_state', 'no_precise_time', 'rest'}:
            raise BusinessError('validation', '未知的计划方式。')
        blocks = copy.deepcopy(p.get('blocks', []))
        if not isinstance(blocks, list) or len(blocks) > 100:
            raise BusinessError('limit', '单日时间块过多。')
        if mode == 'rest' and blocks:
            raise BusinessError('validation', '休整计划不安排工作块。')
        context = self.plan_context(c, day, mode)
        occupied, targets, minutes_total = [], set(), 0
        for i, block in enumerate(blocks):
            retained = i < len(_retained) and block == _retained[i]
            target = self.store.get(c, block['target_id'])
            if not retained and (target['archived'] or target['type'] not in {'task', 'event', 'milestone'} or target['status'] in {'done', 'cancelled'}):
                raise BusinessError('plan_target', '计划只能安排可执行的活动记录。')
            if target['id'] in targets:
                raise BusinessError('duplicate', '同一任务不要重复加入计划；请使用子任务区分工作。')
            targets.add(target['id'])
            if not retained:
                block['target_version'] = target['version']
                gate = target['data'].get('completion_gate') or target['data'].get('acceptance') or ''
                if block.get('completion_gate') and gate and block['completion_gate'] != gate:
                    raise BusinessError('completion_gate', '计划不能修改任务已有的完成条件。')
                block['completion_gate'] = gate or block.get('completion_gate', '')
            if block.get('start') or block.get('end'):
                if mode == 'no_precise_time':
                    raise BusinessError('validation', '无精确排时计划只保留顺序与估时。')
                if context['unknowns']:
                    raise BusinessError('uncertain_time', '固定事件时间待确认，请补充信息或使用无精确排时方式。', {'unknowns': context['unknowns']})
                a, z = time_value(block.get('start')), time_value(block.get('end'))
                if z <= a:
                    raise BusinessError('validation', '计划块结束应晚于开始，跨午夜请拆为两日。')
                for event in [*context['hard_events'], *context['protected_times']]:
                    if event['id'] != target['id'] and event['start_minute'] is not None and a < event['end_minute'] and z > event['start_minute']:
                        raise BusinessError('schedule_conflict', '与固定时段“' + event['title'] + '”冲突。')
                if target['type'] == 'event':
                    matches = [e for e in context['hard_events'] if e['id'] == target['id']]
                    if matches and not any(e['start_minute'] == a and e['end_minute'] == z for e in matches):
                        raise BusinessError('schedule_conflict', '固定事件应保持正式时段；改期需先更新日程来源。')
                if any(a < y and z > x for x, y in occupied):
                    raise BusinessError('schedule_conflict', '时间块互相重叠。')
                occupied.append((a, z))
                block['minutes'] = z - a
            if block.get('minutes') is not None:
                if type(block['minutes']) is not int or not 1 <= block['minutes'] <= 1440:
                    raise BusinessError('validation', '估时必须是 1 至 1440 分钟。')
                minutes_total += block['minutes']
            if retained:
                continue  # Preserve adopted work and its historical completion gate.
            for dep in c.execute("SELECT e.* FROM links l JOIN entities e ON l.target_id=e.id WHERE l.source_id=? AND l.kind='depends_on'", (target['id'],)):
                predecessor = self.store.entity(dep)
                from .daily_flow import completed_on_or_before
                completed = completed_on_or_before(self, c, predecessor, day)
                if not completed and predecessor['id'] not in {x['target_id'] for x in blocks[:i]}:
                    raise BusinessError('dependency', '前置任务“' + predecessor['title'] + '”尚未完成或安排在前。')
                prior_block = next((x for x in blocks[:i] if x['target_id'] == predecessor['id']), None)
                if not completed and prior_block and prior_block.get('end') and block.get('start') and time_value(prior_block['end']) > time_value(block['start']):
                    raise BusinessError('dependency', '依赖者的开始时间不能早于前置任务的结束。')
        if context['capacity_minutes'] is not None and minutes_total > context['capacity_minutes']:
            raise BusinessError('capacity', '总估时超过生效的容量规则；完成条件不会被缩减。')
        from .planning import validate_scoped_rules
        diagnostics = validate_scoped_rules(self, c, day, blocks, context)
        data = {'date': day, 'mode': mode, 'blocks': blocks, 'source_text': p.get('source_text', ''), 'context_revision': self.store.meta(c, 'revision'), 'soft_estimates': True, 'unknowns': context['unknowns'] + diagnostics['unknowns'], 'rule_diagnostics': diagnostics}
        if p.get('supersedes_id'):
            old = self.store.get(c, p['supersedes_id'])
            if old['type'] != 'plan' or old['data']['date'] != day:
                raise BusinessError('validation', '计划调整必须对应同一天的旧计划。')
            data['supersedes_id'] = old['id']
        return self._create(c, {'type': 'plan', 'title': p.get('title') or day + ' 的计划', 'status': 'planned', 'data': data}, rid)

    def create_checkin(self, c, p, rid):
        day = date_value(p['date']).isoformat()
        target_ids = p.get('target_ids')
        source_kind = 'explicit_targets' if target_ids is not None else 'plan'
        if target_ids is None:
            plan = c.execute("SELECT data FROM entities WHERE type='plan' AND archived=0 AND json_extract(data,'$.date')=? ORDER BY created_at DESC,id DESC LIMIT 1", (day,)).fetchone()
            target_ids = [b['target_id'] for b in json.loads(plan[0])['blocks']] if plan else []
            if plan is None:
                source_kind = 'explicit_dated_arrangements'
                target_ids = [r[0] for r in c.execute("SELECT id FROM entities WHERE type='task' AND archived=0 AND status NOT IN ('done','cancelled') AND json_extract(data,'$.scheduled_date')=? ORDER BY created_at LIMIT 101", (day,))]
                occurrences, _ = self._events(c, day)
                target_ids += [e['id'] for e in occurrences]
                target_ids = list(dict.fromkeys(target_ids))
        if len(target_ids) > 100:
            raise BusinessError('limit', '一次询问最多 100 个对象。')
        questions = []
        for id in dict.fromkeys(target_ids):
            target = self.store.get(c, id)
            gates = target['data'].get('gates') or [{'id': 'completion', 'label': target['data'].get('completion_gate') or '完成情况'}]
            for gate in gates:
                questions.append({'id': new_id(), 'target_id': id, 'target_version': target['version'], 'gate_id': gate['id'], 'title': target['title'], 'question': gate['label'], 'number': len(questions) + 1})
        if len(questions) > 200:
            raise BusinessError('limit', '完成门超过单次询问上限，请明确分批选择；未截断问题集。')
        return self._create(c, {'type': 'checkin', 'title': day + (' 完成情况' if questions else ' 暂无可复核安排'), 'status': 'active' if questions else 'draft', 'data': {'date': day, 'questions': questions, 'source_kind': source_kind, 'coverage': {'targets': len(set(target_ids)), 'questions': len(questions), 'complete': True}, 'delivery_state': 'not_delivered', 'source_revision': self.store.meta(c, 'revision'), 'no_reply_means': 'unknown'}}, rid)

    def review(self, c, start, end):
        a, b = date_value(start), date_value(end)
        if b < a or (b - a).days > 370:
            raise BusinessError('validation', '回顾日期范围为 0 至 370 天。')
        latest, facts, rows_count = {}, [], 0
        for row in c.execute("SELECT * FROM entities WHERE type='feedback' AND archived=0 AND json_extract(data,'$.business_date') BETWEEN ? AND ? ORDER BY created_at,rowid", (start, end)):
            entity = self.store.entity(row)
            rows_count += 1
            data = entity['data']
            for key, value in data['dimensions'].items():
                latest[(data['target_id'], data['business_date'], key)] = value
            if len(facts) < 200:
                facts.append(entity)
        metrics = {'completed_daily_targets': 0, 'actual_minutes': 0, 'attendance_reported': 0, 'submission_reported': 0, 'mastery_verified': 0}
        for (_, _, dim), value in latest.items():
            if dim == 'completion' and value == 'done':
                metrics['completed_daily_targets'] += 1
            if dim == 'actual_minutes':
                metrics['actual_minutes'] += value
            if dim == 'attendance' and value != 'unknown':
                metrics['attendance_reported'] += 1
            if dim == 'submission' and value != 'unknown':
                metrics['submission_reported'] += 1
            if dim == 'mastery' and value == 'verified':
                metrics['mastery_verified'] += 1
        return {'start': start, 'end': end, 'metrics': metrics, 'facts': facts, 'coverage': {'feedback_records': rows_count, 'facts_returned': len(facts), 'metrics_complete': True}, 'unknowns': ['没有反馈的对象和日期保持未知，不计为未完成或零工时。', '完成、出席、提交和掌握分别统计，不能相互推断。', '实际分钟按同一对象同一天最新明确反馈计入，不把更正重复相加。']}

    def create_job(self, c, command, p, rid):
        id, stamp = new_id(), now()
        kind = 'artifact' if command == 'create_artifact_job' else p.get('kind')
        if kind not in {'ai', 'artifact'}:
            raise BusinessError('executor_unavailable', '执行器尚未配置。可以先登记运行记录，软件不会假报执行成功。')
        value = copy.deepcopy(p if kind == 'artifact' else p.get('input', {}))
        if kind == 'ai':
            settings = self.store.meta(c, 'settings')
            if not settings['ai'].get('enabled'):
                raise BusinessError('ai_not_configured', '请先在设置中启用并配置本机 Codex；本地管理功能可以继续使用。')
            if not isinstance(value.get('prompt'), str) or not value['prompt'].strip():
                raise BusinessError('validation', '请输入需要分析或安排的内容。')
            day = value.get('date') or self.today(c)
            context = {'planning': self.plan_context(c, day, value.get('mode', 'standard')), **self.store.state(c)}
            context['recent_inbox'] = self._query(c, 'list', {'type': 'inbox', 'limit': 10})['items']
            context['execution_on_date'] = self.review(c, day, day)
            from .reviews import query_daily
            context['daily_review'] = query_daily(self,c,{'date':day})
            selected_ids = value.get('context_ids', [])
            if not isinstance(selected_ids, list) or len(selected_ids) > 12 or any(not isinstance(id, str) for id in selected_ids):
                raise BusinessError('context_limit', '一次协助最多附带 12 项明确记录。')
            context['selected_records'] = [self.store.get(c, id) for id in dict.fromkeys(selected_ids)]
            context['asset_content_coverage'] = '附件仅含版本与元数据，未读取正文；不能声称已经分析附件。'
            context['types'] = [{'id': t['id'], 'fields': t['fields'], 'parent_types': t['parent_types']} for t in self.types(c).values() if not t.get('read_only')]
            max_chars = settings.get('context_characters', 42000)
            while len(encode(context)) > max_chars and context['planning']['tasks']:
                context['planning']['tasks'].pop()
                context['planning']['coverage']['tasks_paged'] = True
            if len(encode(context)) > max_chars:
                raise BusinessError('context_limit', '必要上下文超过推理预算，请缩小日期或对象范围。')
            context['planning']['coverage']['tasks_returned'] = len(context['planning']['tasks'])
            value['context'] = context
            value['allowed_commands'] = ['apply_timetable', 'create', 'update', 'record_feedback', 'create_plan', 'save_review', 'set_recurring_rule', 'set_recovery_task', 'record_recovery_progress']
            value['context_characters'] = len(encode(context))
        else:
            if p.get('kind') not in {'text', 'markdown', 'csv', 'notebook', 'docx', 'pdf', 'pdf_notebook'}:
                raise BusinessError('executor_unavailable', '此成果格式尚未注册执行器。')
            if not p.get('relative_path') or 'content' not in p:
                raise BusinessError('validation', '成果需要文件名和内容。')
            if p.get('owner_id'):
                self.store.get(c, p['owner_id'])
        c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)', (id, kind, 'queued', encode(value), None, None, self.store.meta(c, 'epoch'), self.store.meta(c, 'revision') + 1, 1, stamp, stamp))
        self.store.change(c, rid, 'create_job')
        return {'id': id, 'kind': kind, 'status': 'queued', 'input': value, 'created_at': stamp}

    def _job(self, c, id):
        row = c.execute('SELECT * FROM jobs WHERE id=?', (id,)).fetchone()
        if not row:
            raise BusinessError('not_found', '作业不存在。')
        return dict(row)

    def undo(self, c, p, rid):
        changes = list(c.execute('SELECT * FROM changes WHERE request_id=? ORDER BY seq DESC', (p['request_id'],)))
        if not changes or any(r['action'] not in {'create', 'update', 'move', 'archive', 'promote', 'adopt_artifact'} for r in changes):
            raise BusinessError('undo_scope', '此操作不能整体撤销；请通过对应业务流程更正。')
        result = []
        for row in changes:
            after = json.loads(row['after_value'])
            current = self.store.get(c, row['entity_id'])
            if current['version'] != after['version']:
                raise BusinessError('undo_conflict', '撤销目标后来已被修改；请先核对。')
            if current['type'] in {'feedback', 'asset', 'artifact', 'bundle'}:
                raise BusinessError('immutable', '证据记录使用更正或归档，不能撤销后抹除。')
            if row['before_value']:
                restored = json.loads(row['before_value'])
                self._validate(c, restored)
            else:
                if c.execute('SELECT 1 FROM entities WHERE parent_id=? AND archived=0', (current['id'],)).fetchone():
                    raise BusinessError('active_children', '记录已有子项，不能撤销创建。')
                restored = {**current, 'archived': True}
            result.append(self._save(c, current, restored, rid, 'undo'))
        return {'entities': result}


    def _ensure_mutable(self, c, entity):
        definition = self.types(c).get(entity['type'])
        if definition is None or definition.get('read_only'):
            raise BusinessError('read_only', '此对象所属模块不可用，只能读取历史内容。')
