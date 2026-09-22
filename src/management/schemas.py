"""Versioned declarative business types. Unknowns remain explicit."""
from __future__ import annotations

import copy
import re


class BusinessError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


def field(id, label, type="text", **extra):
    return {"id": id, "label": label, "type": type, **extra}


NOTE = field("notes", "说明", "multiline")
SOURCE = field("source_text", "信息来源 / 原话", "multiline")
DUE = field("due_date", "截止日期", "date")
STATUSES = {"pending": "待确认", "planned": "已安排", "active": "进行中", "done": "已完成", "cancelled": "已取消", "draft": "草稿", "blocked": "受阻", "failed": "失败"}
OWNERS = [None, "domain", "project", "course", "activity"]


def typ(id, label, section, parents=None, fields=(), statuses=None):
    return {"id": id, "label": label, "section": section, "module": "builtin", "version": 1,
            "parent_types": parents if parents is not None else OWNERS,
            "fields": list(fields), "statuses": statuses or STATUSES, "read_only": False}


TYPES = {t["id"]: t for t in [
    typ("domain", "分类", "projects", [None, "domain"], [NOTE]),
    typ("project", "项目", "projects", OWNERS, [DUE, field("purpose", "目标", "multiline"), field("acceptance", "验收条件", "multiline"), NOTE]),
    typ("course", "课程", "projects", [None, "domain"], [field("code", "课程代码"), field("teaching_week", "教学周", "integer"), NOTE]),
    typ("activity", "活动", "projects", [None, "domain"], [DUE, NOTE]),
    typ("task", "任务", "planning", [*OWNERS, "task"], [DUE, field("estimated_minutes", "预计分钟", "integer"), field("completion_gate", "完成条件", "multiline"), field("priority", "优先级", "choice", options=["normal", "high", "low"]), NOTE]),
    typ("checklist", "清单项", "planning", ["task"], [field("completion_gate", "完成条件", "multiline")]),
    typ("milestone", "里程碑", "projects", ["project", "course", "activity"], [DUE, field("acceptance", "验收条件", "multiline"), NOTE]),
    typ("goal", "目标", "projects", [None, "domain"], [DUE, field("success_criteria", "成功标准", "multiline"), NOTE]),
    typ("note", "笔记", "materials", [*OWNERS, "task", "note"], [field("content", "正文", "multiline"), SOURCE]),
    typ("inbox", "待整理信息", "today", [None], [field("content", "原始信息", "multiline"), SOURCE]),
    typ("event", "日程", "planning", [None], [field("date", "日期", "date"), field("start", "开始时间", "time"), field("end", "结束时间", "time"), field("hard", "固定约束", "boolean"), field("timezone", "时区"), field("time_kind", "时间精度", "choice", options=["exact", "date_only", "approximate", "unknown"]), field("recurrence", "重复", "choice", options=["none", "daily", "weekly", "monthly"]), field("until", "重复截止", "date"), NOTE]),
    typ("plan", "每日计划", "planning", [None], [field("date", "日期", "date"), field("mode", "计划方式", "choice", options=["standard", "low_state", "no_precise_time", "rest"]), NOTE]),
    typ("feedback", "执行反馈", "reviews", [None], [field("business_date", "归属日期", "date"), SOURCE]),
    typ("checkin", "完成情况询问", "reviews", [None], [field("date", "归属日期", "date"), NOTE]),
    typ("review", "周期回顾", "reviews", [None], [field("start", "开始日期", "date"), field("end", "结束日期", "date"), field("content", "回顾", "multiline")]),
    typ("rule", "使用规则", "settings", [None, "domain", "project", "course", "activity"], [field("rule_kind", "规则类型", "choice", options=["capacity", "protected_time", "warning", "behavior", "temporary"]), field("effective_from", "生效日期", "date"), field("effective_until", "失效日期", "date"), field("minutes", "容量分钟", "integer"), field("start", "保护时段开始", "time"), field("end", "保护时段结束", "time"), field("days_before", "提前天数", "integer"), field("policy", "策略", "multiline"), SOURCE]),
    typ("schedule", "定时任务", "settings", [None], [field("workflow", "工作流", "choice", options=["checkin", "warnings", "weekly_review"]), field("time", "执行时间", "time"), field("timezone", "时区"), field("frequency", "频率", "choice", options=["daily", "weekly"]), field("weekday", "星期 (0 为一)", "integer"), field("enabled", "启用", "boolean"), field("notify_unchanged", "没有变化也通知", "boolean")]),
    typ("notification", "通知", "today", [None], [field("content", "内容", "multiline"), field("business_date", "归属日期", "date"), field("seen", "已读", "boolean")]),
    typ("asset", "资料", "materials", [None], [field("description", "资料说明", "multiline"), SOURCE]),
    typ("bundle", "资料包", "materials", [None], [NOTE]),
    typ("artifact", "成果版本", "materials", [None], [field("purpose", "用途"), field("language", "语言"), NOTE]),
    typ("topic", "知识点", "projects", [None, "course", "project", "domain"], [field("description", "知识范围", "multiline"), field("mastery", "掌握证据", "multiline")]),
    typ("question", "题目", "materials", [None, "course", "project", "topic"], [field("prompt", "题干", "multiline"), field("answer_key", "参考答案", "multiline"), SOURCE]),
    typ("attempt", "作答 / 复测", "reviews", [None, "question", "topic", "course"], [field("answer", "作答", "multiline"), field("score", "分数", "number"), field("maximum", "满分", "number"), field("error", "错误分析", "multiline"), field("next_review", "复测日期", "date"), SOURCE]),
    typ("assessment", "考核组件", "projects", [None, "course"], [DUE, field("weight", "权重", "number"), field("aggregation", "多次成绩聚合", "choice", options=["latest", "best", "mean"]), field("score", "已确认成绩", "number"), field("maximum", "满分", "number"), SOURCE]),
    typ("experiment", "实验定义", "projects", [None, "project", "domain"], [field("hypothesis", "研究问题", "multiline"), field("parameters", "参数", "multiline"), NOTE]),
    typ("run", "实验 / 运行记录", "projects", [None, "project", "experiment"], [field("environment", "运行环境"), field("command", "执行指令", "multiline"), field("outcome", "结果证据", "multiline"), field("metric", "指标", "number"), field("unit", "指标单位"), SOURCE]),
    typ("environment", "环境说明", "materials", [None, "project"], [field("description", "环境与版本", "multiline"), field("connection", "连接名称"), NOTE]),
    typ("dataset", "数据集", "materials", [None, "project", "experiment"], [field("version_label", "数据版本"), field("description", "说明", "multiline"), SOURCE]),
    typ("batch", "采集批次", "projects", [None, "project"], [field("target_count", "计划对象数", "integer"), NOTE]),
    typ("case", "采集 Case", "projects", [None, "project", "batch"], [field("mapping", "映射", "multiline"), field("steps", "操作步骤", "multiline"), field("object_count", "对象数", "integer"), field("clip_count", "片段数", "integer"), field("valid_minutes", "有效分钟", "number"), field("actual_minutes", "实际工时分钟", "number"), field("quality", "质检状态", "choice", options=["unknown", "passed", "failed"]), field("upload", "上传状态", "choice", options=["unknown", "uploaded", "failed"]), field("acceptance", "验收状态", "choice", options=["unknown", "accepted", "rejected"]), SOURCE]),
    typ("external_record", "外部操作记录", "reviews", [None, "project", "course", "activity"], [field("provider", "外部系统"), field("external_id", "外部记录编号"), field("operation", "操作类型"), field("outcome", "确认结果", "choice", options=["unknown", "succeeded", "failed"]), field("receipt", "回执证据", "multiline"), SOURCE]),
]}

# Additional typed evidence fields; no actual personal values are seeded.
TYPES['course']['fields'] += [field('term','学期'),field('semester_start','第一教学周周一','date'),field('semester_end','学期结束日期','date')]
TYPES['attempt']['parent_types'].append('assessment')
TYPES['attempt']['fields'] += [field('performed', '已实际作答', 'boolean'), field('business_date', '作答日期', 'date'), field('mastery_verified', '已验证掌握', 'boolean'), field('resolved', '错误已解决', 'boolean')]
for f in TYPES['assessment']['fields']:
    if f['id'] == 'aggregation':
        f['options'].append('best_n')
TYPES['assessment']['fields'] += [field('best_n', '取最优次数', 'integer'), field('penalty_percentage_points', '总评扣分百分点', 'number'), field('penalty_confirmed', '扣分规则已确认', 'boolean'), field('penalty_source', '扣分来源', 'multiline')]
for key in ('project', 'task', 'run'):
    TYPES[key]['fields'].append(field('track', '所属方向', 'choice', options=['main', 'side', 'unspecified']))
TYPES['gap'] = typ('gap', '信息缺口', 'materials', OWNERS, [field('gap_kind', '缺口类型', 'choice', options=['not_released', 'not_obtained', 'unread', 'access_blocked', 'conflicting_evidence', 'local_unprocessed', 'external_wait', 'user_confirmation']), field('last_checked', '最近核对', 'date'), field('next_check', '下次核对', 'date'), SOURCE, NOTE])
TYPES['assertion'] = typ('assertion', '来源断言', 'materials', OWNERS, [field('field_name', '所描述的事实'), field('value', '明确内容', 'multiline'), field('observed_at', '观察日期', 'date'), field('certainty', '证据状态', 'choice', options=['reported', 'confirmed', 'conflicting', 'withdrawn']), SOURCE])
TYPES['phase'] = typ('phase', '项目阶段', 'projects', [None, 'project'], [field('target_date', '预计推进日期', 'date'), field('entry_gate', '进入条件', 'multiline'), field('exit_gate', '离开条件', 'multiline'), field('resume_point', '继续的位置', 'multiline'), NOTE])
TYPES['task']['parent_types'].append('phase')
TYPES['milestone']['parent_types'].append('phase')
TYPES['task']['fields'].append(field('scheduled_date', '明确安排日期', 'date'))
TYPES['task']['fields'] += [field('catchup_enabled','登记为待补事项','boolean'),
    field('catchup_unit','补课数量单位'),field('catchup_total_quantity','待补总量','number'),
    field('catchup_lesson_key','学习单元标识'),field('catchup_topic_ids','关联知识点','selection'),
    field('catchup_lesson_topics','具体学习内容','selection'),
    field('catchup_reason','待补依据','choice',options=['self_reported','confirmed_incomplete']),
    field('catchup_source_text','待补来源','multiline')]
TYPES['event']['fields'].append(field('event_kind', '事件类别', 'choice', options=['exam','lecture','tutorial','lab','meeting','interview','appointment','travel','other']))
TYPES['task']['fields'].append(field('task_kind', '任务类别', 'text'))
TYPES['rule']['fields'] += [field('calendar_months_before', '提前日历月数', 'integer'), field('event_kind', '只作用于事件类别', 'selection'), field('task_kind', '只作用于任务类别', 'selection'), field('target_types', '只作用于对象类型', 'selection')]


TYPES['file_reference'] = typ('file_reference', '本地文件', 'materials', [None, 'domain', 'project', 'course', 'activity', 'phase', 'task', 'goal'], [NOTE])

TYPES['timetable'] = typ('timetable','学期课表','planning',[None],[
    field('semester_start','第一教学周周一','date'),field('semester_end','学期结束日期','date'),
    field('timezone','课表时区'),field('week_numbering','教学周计数','choice',options=['calendar','teaching']),
    field('recess_weeks','休息周周一日期','selection'),SOURCE,NOTE])

TYPES['recurring_rule'] = typ('recurring_rule', '节点前准备规则', 'planning', [None, 'domain', 'project', 'course', 'activity'], [
    field('anchor_id', '关联日程或截止节点'), field('content', '每次要做什么', 'multiline'),
    field('completion_gate', '完成条件', 'multiline'), field('estimated_minutes', '预计分钟', 'integer'),
    field('days_before', '提前天数', 'integer'), field('enabled', '启用', 'boolean'),
    field('effective_from', '准备日开始日期', 'date'), field('effective_until', '准备日结束日期', 'date'), SOURCE])

RESERVED_TYPES = {"plan", "feedback", "checkin", "review", "asset", "artifact", "bundle", "notification", "file_reference", "recurring_rule"}
DIMENSIONS = {
    "completion": {"unknown", "not_started", "partial", "done", "blocked", "incomplete"},
    "attendance": {"unknown", "attended", "absent", "cancelled", "asynchronous", "online_replacement"},
    "viewing": {"unknown", "not_viewed", "partial", "viewed"},
    "submission": {"unknown", "not_submitted", "submitted", "accepted", "rejected"},
    "mastery": {"unknown", "not_tested", "needs_review", "verified"},
}


def validate_dimensions(value):
    if not isinstance(value, dict) or not value:
        raise BusinessError("validation", "请至少填写一项明确反馈。")
    if set(value) - (set(DIMENSIONS) | {"actual_minutes"}):
        raise BusinessError("validation", "反馈含有未知维度。")
    for key, v in value.items():
        if key == "actual_minutes":
            if type(v) not in (int, float) or not 0 <= v <= 24 * 60:
                raise BusinessError("validation", "实际时长必须是 0 到 1440 分钟；未知请留空。")
        elif v not in DIMENSIONS[key]:
            raise BusinessError("validation", f"反馈 {key} 的取值无效。")


def validate_manifest(manifest, existing_types):
    if not isinstance(manifest, dict) or not re.fullmatch(r"[a-z][a-z0-9_]{2,39}", str(manifest.get("id", ""))):
        raise BusinessError("validation", "模块需要稳定的英文标识，长度为 3 到 40 个字符。")
    if type(manifest.get("version")) is not int or manifest["version"] < 1:
        raise BusinessError("validation", "模块版本必须是正整数。")
    if manifest.get("api_version", 1) != 1:
        raise BusinessError("incompatible_module", "此模块需要不同版本的业务接口。")
    if len(manifest.get("types", [])) > 20 or len(manifest.get("fields", [])) > 100:
        raise BusinessError("limit", "单个模块定义过大。")
    declared = {t.get("id") for t in manifest.get("types", [])}
    for t in manifest.get("types", []):
        if not re.fullmatch(manifest["id"] + r"\.[a-z][a-z0-9_]{1,30}", str(t.get("id", ""))):
            raise BusinessError("validation", "新增类型必须使用模块标识作为前缀。")
        if not t.get("label") or t.get("section") not in {"today", "planning", "projects", "materials", "reviews"}:
            raise BusinessError("validation", "新增类型需要名称和已有的导航位置。")
        if any(p is not None and p not in existing_types and p not in declared for p in t.get("parent_types", [None])):
            raise BusinessError("validation", "新增类型引用了未知父类型。")
    for f in manifest.get("fields", []):
        if f.get("target_type") not in existing_types and f.get("target_type") not in declared:
            raise BusinessError("validation", "扩展字段引用了未知类型。")
    for definition in [f for t in manifest.get("types", []) for f in t.get("fields", [])] + manifest.get("fields", []):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", str(definition.get("id", ""))):
            raise BusinessError("validation", "字段标识无效。")
        if definition.get("type", "text") not in {"text", "multiline", "integer", "number", "boolean", "date", "time", "choice", "selection"}:
            raise BusinessError("validation", "不支持的字段类型。")
    for w in manifest.get("workflows", []):
        if w.get("action") not in {"create", "record_feedback", "save_review"}:
            raise BusinessError("validation", "配置工作流仅允许已注册的有限业务操作。")
    for r in manifest.get("rules", []):
        if r.get("operator") not in {"required", "min", "max", "one_of"}:
            raise BusinessError("validation", "规则必须使用已注册的校验操作符。")
    return copy.deepcopy(manifest)
