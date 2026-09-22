"""Evidence-sensitive domain projections; no commands, writes or model calls.

All counts come from scoped stable entity IDs. Display pagination never changes
aggregate coverage. Unknown scores, work time and execution evidence remain
unknown rather than becoming zero or inferred completion.
"""
from __future__ import annotations
from collections import Counter, defaultdict
import datetime as dt
import math

from .schemas import BusinessError


MAX_SCOPE = 50000
MAX_MEASUREMENTS = 200000
GAP_KINDS = {"not_released", "not_obtained", "unread", "access_blocked", "conflicting_evidence", "local_unprocessed", "external_wait", "user_confirmation"}
SCOPE_TYPES = {"course_id": {"course"}, "project_id": {"project"}, "topic_id": {"topic"}, "batch_id": {"batch"}}
TRACKS = {"main", "side", "unspecified"}


def _day(value, default=None):
    if value is None:
        return default
    try:
        return dt.date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise BusinessError("validation", "日期必须采用 YYYY-MM-DD 格式。")


def _scope(core, c, p, allowed, required=False):
    provided = [k for k in SCOPE_TYPES if p.get(k)]
    if len(provided) > 1 or provided and provided[0] not in allowed:
        raise BusinessError("scope_type", "此查询需要一个明确且兼容的范围。")
    if not provided:
        if required:
            raise BusinessError("scope_required", "请选择需要汇总的业务对象。")
        return None
    key = provided[0]
    root = core.store.get(c, p[key])
    if root["type"] not in SCOPE_TYPES[key]:
        raise BusinessError("scope_type", "查询范围与对象类型不一致。")
    return root


def _cte(root):
    if root:
        return ("WITH RECURSIVE scope(id) AS (SELECT id FROM entities WHERE id=? UNION SELECT e.id FROM entities e JOIN scope s ON e.parent_id=s.id) ", [root["id"]])
    return "", []


def _rows(core, c, types, root=None):
    prefix, args = _cte(root)
    sql = prefix + "SELECT e.*,e.rowid AS stable_order FROM entities e WHERE e.archived=0 AND e.type IN (" + ",".join("?" for _ in types) + ")"
    args += list(types)
    if root:
        sql += " AND e.id IN (SELECT id FROM scope)"
    sql += " ORDER BY e.created_at,e.rowid"
    count = 0
    for row in c.execute(sql, args):
        count += 1
        if count > MAX_SCOPE:
            raise BusinessError("scope_limit", "当前汇总范围过大，请选择较小的项目或课程。")
        yield core.store.entity(row)


def _page(p):
    try:
        limit, offset = int(p.get("limit", 100)), int(p.get("offset", 0))
    except (TypeError, ValueError):
        raise BusinessError("validation", "分页参数无效。")
    return min(500, max(1, limit)), max(0, offset)


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _source(data):
    values = [data.get("source_text"), data.get("evidence"), data.get("receipt")]
    return any(isinstance(value, str) and value.strip() for value in values)


def _performed(entity):
    data = entity["data"]
    return data.get("performed") is True and _source(data) and not data.get("generated_only", False)


def _brief(entity):
    return {"id": entity["id"], "type": entity["type"], "title": entity["title"], "status": entity["status"], "parent_id": entity["parent_id"], "version": entity["version"]}


def _collect(items, item, total_index, offset, limit):
    if offset <= total_index < offset + limit:
        items.append(item)


def learning_summary(core, c, p):
    root = _scope(core, c, p, {"course_id", "project_id", "topic_id"})
    as_of = _day(p.get("as_of"), core.today(c))
    limit, offset = _page(p)
    counts = Counter({"topics": 0, "questions": 0, "attempt_records": 0, "performed_attempts": 0,
                      "attempt_evidence_unknown": 0, "not_performed": 0, "open_errors": 0,
                      "mastery_verified_attempts": 0, "due_retests": 0})
    items, total = [], 0
    for entity in _rows(core, c, ("topic", "question", "attempt"), root):
        data = entity["data"]
        item = _brief(entity)
        if entity["type"] == "topic":
            counts["topics"] += 1
        elif entity["type"] == "question":
            counts["questions"] += 1
        else:
            counts["attempt_records"] += 1
            actual = _performed(entity)
            item["performed_with_source"] = actual
            item["has_source"] = _source(data)
            if actual:
                counts["performed_attempts"] += 1
                if data.get("error") and data.get("resolved") is not True:
                    counts["open_errors"] += 1
                    item["error_excerpt"] = str(data["error"])[:300]
                if data.get("mastery_verified") is True:
                    counts["mastery_verified_attempts"] += 1
                if data.get("next_review"):
                    next_day = _day(data["next_review"])
                    item["next_review"] = next_day
                    if next_day <= as_of and data.get("resolved") is not True:
                        counts["due_retests"] += 1
            elif data.get("performed") is False or data.get("generated_only") is True:
                counts["not_performed"] += 1
            else:
                counts["attempt_evidence_unknown"] += 1
        _collect(items, item, total, offset, limit)
        total += 1
    return {"scope_id": root["id"] if root else None, "as_of": as_of, "metrics": dict(counts), "items": items,
            "coverage": {"total": total, "returned": len(items), "metrics_complete": True},
            "next_offset": offset + limit if offset + limit < total else None,
            "unknowns": ["题目、参考答案或Notebook存在，不代表用户已经作答。", "仅明确performed且保留来源的尝试计入实际作答；分数不会自动转换为掌握。", "复测需要真实尝试及明确日期；本查询不会自动建立复习安排。"]}


def assessment_summary(core, c, p):
    root = _scope(core, c, p, {"course_id"})
    components = {}
    kept = {"weight", "aggregation", "best_n", "score", "maximum", "penalty_percentage_points", "penalty_confirmed"}
    for entity in _rows(core, c, ("assessment",), root):
        original = entity["data"]
        components[entity["id"]] = {**_brief(entity), "source_present": _source(original),
            "data": {**{key: original[key] for key in kept if key in original},
                     "penalty_source_present": isinstance(original.get("penalty_source"), str) and bool(original["penalty_source"].strip())}}
    attempts = defaultdict(list)
    unverified = Counter()
    for entity in _rows(core, c, ("attempt",), root):
        data = entity["data"]
        owner = data.get("assessment_id") or entity["parent_id"]
        if owner not in components:
            continue
        score, maximum = _number(data.get("score")), _number(data.get("maximum"))
        if not _performed(entity) or score is None or maximum is None or maximum <= 0 or score > maximum:
            unverified[owner] += 1
            continue
        day = _day(data.get("business_date"), entity["created_at"][:10])
        attempts[owner].append({"id": entity["id"], "ratio": score / maximum, "score": score, "maximum": maximum,
                                "business_date": day, "order": (day, entity["created_at"], entity["stable_order"])})
    items, known_points, known_weight, total_weight = [], 0.0, 0.0, 0.0
    incomplete = 0
    for id, entity in components.items():
        data = entity["data"]
        weight, mode = _number(data.get("weight")), data.get("aggregation", "latest")
        selected, ratio, provisional, problems = [], None, None, []
        choices = attempts[id]
        if mode not in {"latest", "best", "mean", "best_n"}:
            problems.append("不支持的计分聚合方式")
        elif choices:
            if mode == "latest":
                selected = [max(choices, key=lambda a: a["order"])]
            elif mode == "best":
                selected = [max(choices, key=lambda a: a["ratio"])]
            elif mode == "mean":
                selected = choices
            else:
                n = data.get("best_n")
                if type(n) is not int or n < 1:
                    problems.append("best_n需要明确的正整数次数")
                else:
                    selected = sorted(choices, key=lambda a: a["ratio"], reverse=True)[:n]
                    if len(selected) < n:
                        problems.append("已确认尝试不足以计算best_n正式结果")
            if selected:
                provisional = sum(a["ratio"] for a in selected) / len(selected)
                if not problems:
                    ratio = provisional
        elif entity["source_present"]:
            score, maximum = _number(data.get("score")), _number(data.get("maximum"))
            if score is not None and maximum and score <= maximum:
                ratio = score / maximum
                problems.append("使用组件中明确登记的成绩，未从尝试记录推断")
        if weight is None or weight > 100:
            problems.append("权重未知或不合法")
            weight = None
        else:
            total_weight += weight
        points = ratio * weight if ratio is not None and weight is not None else None
        penalty = _number(data.get("penalty_percentage_points"))
        if penalty is not None and penalty > 0:
            if data.get("penalty_confirmed") is True and data.get("penalty_source_present"):
                if points is not None:
                    points -= penalty
            else:
                problems.append("罚分尚未确认，未计入正式贡献")
                points = None
        if points is None:
            incomplete += 1
        else:
            known_points += points
            known_weight += weight
        items.append({**_brief(entity), "aggregation": mode, "weight": weight, "ratio": ratio,
                      "provisional_ratio": provisional if ratio is None else None,
                      "weighted_percentage_points": points, "confirmed_attempts": len(choices), "unverified_attempts": unverified[id],
                      "selected_attempt_ids": [a["id"] for a in selected[:100]], "selected_attempts_total": len(selected),
                      "issues": problems})
    limit, offset = _page(p)
    # Across courses, weights cannot be combined into one synthetic course grade.
    full = bool(items) and incomplete == 0 and abs(total_weight - 100) < 1e-8 and root is not None
    return {"scope_id": root["id"] if root else None, "items": items[offset:offset+limit],
            "metrics": {"known_weighted_percentage_points": known_points if root else None, "known_weight": known_weight if root else None,
                        "configured_weight_total": total_weight if root else None, "components_unknown": incomplete,
                        "overall_percentage": known_points if full else None},
            "coverage": {"total": len(items), "returned": len(items[offset:offset+limit]), "metrics_complete": True,
                         "overall_complete": full}, "next_offset": offset+limit if offset+limit < len(items) else None,
            "unknowns": ["未知分数或满分不当作零，部分成绩不重新归一化为总评。", "best_n次数不足时只显示暂算比例，不生成正式总评。", "罚分须明确确认、来源和课程总评百分点单位；没有配置时不推断惩罚。"]}


def collection_summary(core, c, p):
    root = _scope(core, c, p, {"batch_id", "project_id"})
    fields = ("object_count", "clip_count", "valid_minutes", "actual_minutes")
    known = {key: 0 for key in fields}
    missing = Counter({key: 0 for key in fields})
    states = {key: Counter() for key in ("quality", "upload", "acceptance")}
    items, total, batches, batch_count = [], 0, [], 0
    limit, offset = _page(p)
    for entity in _rows(core, c, ("case", "batch"), root):
        if entity["status"] == "cancelled":
            continue
        data = entity["data"]
        if entity["type"] == "batch":
            batch_count += 1
            if len(batches) < 100:
                batches.append({**_brief(entity), "target_count": _number(data.get("target_count"))})
            continue
        for key in fields:
            value = _number(data.get(key))
            if value is None:
                missing[key] += 1
            else:
                known[key] += value
        for key in states:
            states[key][data.get(key) or "unknown"] += 1
        _collect(items, {**_brief(entity), "measurements": {k: _number(data.get(k)) for k in fields},
                        "quality": data.get("quality", "unknown"), "upload": data.get("upload", "unknown"),
                        "acceptance": data.get("acceptance", "unknown")}, total, offset, limit)
        total += 1
    return {"scope_id": root["id"] if root else None, "items": items, "batches": batches,
            "batches_total": batch_count, "batches_truncated": batch_count > len(batches),
            "metrics": {"case_count": total, "known_totals": known, "unknown_counts": dict(missing),
                        "states": {k: dict(v) for k, v in states.items()}},
            "coverage": {"total": total, "returned": len(items), "metrics_complete": True},
            "next_offset": offset+limit if offset+limit < total else None,
            "unknowns": ["项、片段、有效拍摄分钟、实际工时分别统计，不能换算或混加。", "批次目标不加到逐Case实际数量；未知量独立显示，状态done不补造数量。", "拍摄、质检、上传和验收分别登记，不互相推断。"]}


def project_summary(core, c, p):
    root = _scope(core, c, p, {"project_id"}, required=True)
    prefix, args = _cte(root)
    nodes = {}
    for row in c.execute(prefix + "SELECT e.id,e.type,e.title,e.parent_id,e.status,e.version,json_extract(e.data,'$.track') AS track FROM entities e WHERE e.archived=0 AND e.id IN (SELECT id FROM scope)", args):
        if len(nodes) >= MAX_SCOPE:
            raise BusinessError("scope_limit", "项目分支过大，请分层汇总。")
        nodes[row["id"]] = dict(row)
    children = Counter(e["parent_id"] for e in nodes.values() if e["parent_id"] in nodes)
    def track(id):
        seen = set()
        while id in nodes and id not in seen:
            seen.add(id)
            value = nodes[id].get("track")
            if value in {"main", "side"}:
                return value
            id = nodes[id]["parent_id"]
        return "unspecified"
    latest = {}
    for row in c.execute(prefix + "SELECT e.data FROM entities e WHERE e.type='feedback' AND e.archived=0 AND json_extract(e.data,'$.target_id') IN (SELECT id FROM scope) ORDER BY e.created_at,e.rowid", args):
        import json
        data = json.loads(row[0])
        value = _number(data.get("dimensions", {}).get("actual_minutes"))
        if value is not None:
            key = (data["target_id"], data["business_date"])
            latest[key] = value
            if len(latest) > MAX_MEASUREMENTS:
                raise BusinessError("scope_limit", "工时范围过大，请缩小项目或日期范围。")
    tracks = {key: {"entity_count": 0, "statuses": Counter(), "leaf_actual_minutes": 0,
                    "aggregate_reported_minutes": 0, "known_leaf_target_days": 0} for key in TRACKS}
    for id, entity in nodes.items():
        t = tracks[track(id)]
        t["entity_count"] += 1
        t["statuses"][entity["status"]] += 1
    for (id, _), value in latest.items():
        t = tracks[track(id)]
        if children[id] or nodes.get(id, {}).get("type") in {"project", "batch", "domain"}:
            t["aggregate_reported_minutes"] += value
        else:
            t["leaf_actual_minutes"] += value
            t["known_leaf_target_days"] += 1
    pending_children = Counter()
    pending_examples = defaultdict(list)
    for entity in nodes.values():
        parent = entity["parent_id"]
        if parent in nodes and entity["status"] not in {"done", "cancelled"}:
            pending_children[parent] += 1
            if len(pending_examples[parent]) < 100:
                pending_examples[parent].append(entity["id"])
    closure = [{"id": id, "active_child_ids": pending_examples[id], "active_children_total": pending_children[id]}
               for id, entity in nodes.items() if entity["status"] == "done" and pending_children[id]]
    for value in tracks.values():
        value["statuses"] = dict(value["statuses"])
    limit, offset = _page(p)
    ordered = sorted(nodes.values(), key=lambda x: (x["type"], x["title"], x["id"]))
    measured_targets = {key[0] for key in latest}
    missing = sum(e["type"] in {"task", "run", "case"} and e["id"] not in measured_targets for e in nodes.values())
    return {"project_id": root["id"], "items": ordered[offset:offset+limit], "tracks": tracks,
            "closure_conflicts": closure[:100], "closure_conflicts_total": len(closure),
            "unknown_work_time_targets": missing,
            "coverage": {"total": len(nodes), "returned": len(ordered[offset:offset+limit]), "metrics_complete": True},
            "next_offset": offset+limit if offset+limit < len(nodes) else None,
            "unknowns": ["父项目关闭不代表子项完成，旁线成果不提升主线进度。", "工时按对象和业务日期最新明确反馈去重；父级汇总单列，不能与子项时间相加。", "uses/supports等关联不复制归属与工时；未明确main/side的记录保留unspecified。"]}


def coverage_gaps(core, c, p):
    root = _scope(core, c, p, {"course_id", "project_id"})
    as_of = _day(p.get("as_of"), core.today(c))
    limit, offset = _page(p)
    counts, items, total, resolved = Counter({key: 0 for key in GAP_KINDS}), [], 0, 0
    for entity in _rows(core, c, ("gap", "inbox"), root):
        data = entity["data"]
        if entity["type"] == "inbox" and not data.get("gap_kind"):
            continue
        if entity["status"] in {"done", "cancelled"}:
            resolved += 1
            continue
        kind = data.get("gap_kind") if data.get("gap_kind") in GAP_KINDS else "unclassified"
        counts[kind] += 1
        next_check = _day(data.get("next_check"))
        _collect(items, {**_brief(entity), "gap_kind": kind, "last_checked": _day(data.get("last_checked")),
                        "next_check": next_check, "check_due": next_check is not None and next_check <= as_of,
                        "has_source": _source(data), "source_excerpt": str(data.get("source_text") or "")[:240]}, total, offset, limit)
        total += 1
    return {"scope_id": root["id"] if root else None, "as_of": as_of, "items": items, "counts": dict(counts),
            "resolved_or_cancelled": resolved, "coverage": {"total": total, "returned": len(items), "metrics_complete": True},
            "next_offset": offset+limit if offset+limit < total else None,
            "unknowns": ["访问受阻或未检出不等于平台尚未发布；这里只汇总明确登记的缺口。", "未到核对时间不自动关闭，资料存在不证明已阅读，历史快照不证明当前平台状态。"]}


QUERIES = {"learning_summary": learning_summary, "assessment_summary": assessment_summary,
           "collection_summary": collection_summary, "project_summary": project_summary,
           "coverage_gaps": coverage_gaps}


def query_domain(core, c, name, p):
    if name not in QUERIES:
        raise BusinessError("unknown_query", "未注册的领域查询。")
    return QUERIES[name](core, c, p)
