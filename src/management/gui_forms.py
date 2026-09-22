"""Native, typed forms shared by the five application sections."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from .gui_calendar import install_calendar
from .schemas import RESERVED_TYPES

from PySide6.QtCore import QDate, QDateTime, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QScrollArea, QSpinBox, QTextEdit, QVBoxLayout,
    QWidget,
)

TYPE_LABELS = {
    "domain": "分类", "project": "项目", "course": "课程", "activity": "活动",
    "topic": "知识点", "phase": "项目阶段", "file_reference": "本地文件",
    "task": "任务", "checklist": "清单项", "milestone": "交付 / 检查点", "note": "笔记",
    "event": "日程", "plan": "每日计划", "feedback": "执行反馈", "review": "复盘",
    "asset": "资料", "artifact": "成果", "rule": "规则", "goal": "目标",
    "notification": "通知", "checkin": "完成度复核", "inbox": "收件箱",
    "research_run": "研究运行", "experiment": "实验", "collection_case": "采集 Case",
    "knowledge": "知识主题", "error": "错题", "workflow": "流程",
}
STATUS_LABELS = {
    "active": "进行中", "pending": "待处理", "todo": "待开始", "done": "已完成",
    "completed": "已完成", "cancelled": "已取消", "blocked": "受阻", "draft": "草稿",
    "open": "进行中", "paused": "暂停", "unknown": "待确认", "scheduled": "已安排",
    "queued": "等待中", "running": "执行中", "succeeded": "已完成", "failed": "失败",
    "normal": "普通", "high": "高", "low": "低", "standard": "常规", "low_state": "低精力",
    "no_precise_time": "只排先后", "rest": "休息", "exact": "准确时间", "date_only": "仅日期",
    "approximate": "大致时间", "none": "不重复", "daily": "每天", "weekly": "每周", "monthly": "每月",
    "capacity": "可用容量", "protected_time": "保护时段", "warning": "警戒", "behavior": "行为规则",
    "temporary": "临时规则", "checkin": "完成情况询问", "warnings": "警戒检查", "weekly_review": "每周回顾",
    "latest": "最新一次", "best": "最好一次", "mean": "平均", "passed": "通过", "uploaded": "已上传",
    "accepted": "已验收", "rejected": "未通过", "awaiting_review": "待核对采用",
    "ready": "待采用", "applied": "已采用", "needs_input": "待补充", "needs_review": "待核对",
}
FIELD_LABELS = {
    "description": "说明", "body": "正文", "text": "内容", "priority": "优先级",
    "due_date": "截止日期", "deadline": "截止", "estimated_minutes": "预计用时（分钟）",
    "completion_gate": "完成标准", "date": "日期", "start": "开始时间", "end": "结束时间",
    "start_at": "开始时间", "end_at": "结束时间", "timezone": "时区", "location": "地点",
    "hard": "固定安排", "url": "链接", "source": "来源", "source_text": "原始说明",
    "business_date": "归属日期", "mode": "计划模式", "actual_minutes": "实际用时（分钟）",
    "completion": "完成情况", "attendance": "到场情况", "viewing": "观看情况",
    "submission": "提交情况", "mastery": "掌握情况", "path": "位置", "sha256": "内容指纹",
    "mime_type": "文件类型", "size": "字节数", "content": "内容", "unit": "计量单位",
    "target": "目标值", "value": "记录值", "acceptance": "验收情况", "version": "版本",
    "effective_from": "生效日期", "effective_until": "失效日期", "scope": "适用范围",
    "metrics": "已确认统计", "items": "逐项明细", "unknowns": "仍待确认", "coverage": "覆盖范围",
    "total": "总数", "returned": "本页数量", "metrics_complete": "统计是否完整", "as_of": "截至日期",
    "notes": "说明", "facts": "明确反馈", "batches": "批次", "tracks": "工作轨道", "title": "名称",
    "scan_complete": "是否完成扫描", "returned_complete": "当前是否显示全部", "counts": "分类统计",
    "warnings": "警戒", "status": "状态", "reason": "依据", "level": "级别", "type": "类型",
    "owner_id": "关联项目或课程", "kind": "种类", "target_id": "关联对象", "minutes": "分钟", "enabled": "启用",
}


def label_type(value):
    return TYPE_LABELS.get(value, value or "事项")


def label_status(value):
    return STATUS_LABELS.get(value, value or "待确认")


def readable(value):
    if value is None:
        return "待确认"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        return "\n".join(readable(item) for item in value) if value else "无"
    if isinstance(value, dict):
        return "\n".join(f"{FIELD_LABELS.get(k, k)}：{readable(v)}" for k, v in value.items())
    return str(value)


class FormDialog(QDialog):
    def __init__(self, title, parent=None, width=620):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(width, 600)
        self.outer = QVBoxLayout(self)
        self.heading = QLabel(title)
        self.heading.setObjectName("DialogHeading")
        self.outer.addWidget(self.heading)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self.body)
        self.outer.addWidget(scroll, 1)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setObjectName("Error")
        self.error_label.hide()
        self.outer.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.rejected.connect(self.reject)
        self.outer.addWidget(self.buttons)

    def error(self, error):
        message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
        if isinstance(error, dict) and error.get("code") in ("conflict", "version_conflict", "entity_conflict", "stale_revision", "epoch_mismatch"):
            message = "这项内容已在另一端更新。你的输入仍保留，请关闭后重新读取当前内容再保存。\n" + message
        uncertain = isinstance(error, dict) and error.get("code") == "connection_lost"
        if uncertain:
            message += "\n保留当前输入；再次点击会使用原请求核对这次保存，避免重复创建。"
        self.body.setEnabled(not uncertain)
        save = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        save.setText("核对并重试" if uncertain else getattr(self, "_save_label", save.text()))
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))
        save.setEnabled(True)

    def busy(self):
        save = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        if not hasattr(self, "_save_label"):
            self._save_label = save.text()
        self.body.setEnabled(False)
        self.error_label.setText("正在保存…")
        self.error_label.show()
        save.setEnabled(False)


class DeleteTaskDialog(FormDialog):
    """A narrow confirmation with an immediate, version-fenced undo action."""
    def __init__(self, bridge, entity, parent=None, on_saved=None):
        super().__init__("删除任务", parent, 510)
        self.bridge, self.entity, self.on_saved = bridge, entity, on_saved
        self.epoch = bridge.epoch
        self.saving = False
        self.resize(510, 280)
        title = QLabel(entity["title"])
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        self.body_layout.addWidget(title)
        self.explanation = QLabel("这项任务将从任务列表和待办候选中移除。已有计划、反馈和关联记录保留；删除后可以在此窗口撤销。")
        self.explanation.setWordWrap(True)
        self.body_layout.addWidget(self.explanation)
        self.body_layout.addStretch()
        self.action_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.action_button.setText("删除任务")
        self.buttons.accepted.connect(self.apply)
        self._render()

    def _render(self):
        deleted = bool(self.entity.get("archived"))
        self.heading.setText("任务已删除" if deleted else "删除任务")
        self.action_button.setText("撤销删除" if deleted else "删除任务")
        self.action_button.setObjectName("QuietButton" if deleted else "DeleteTask")
        self._save_label = self.action_button.text()
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("关闭" if deleted else "取消")
        if deleted:
            self.explanation.setText("已从任务列表和待办候选中移除。原计划中的这一项会标记为已删除，已保存的反馈仍然保留。")
        self.body.setEnabled(True)
        self.error_label.hide()
        self.action_button.setEnabled(True)

    def apply(self):
        if self.saving:
            return
        self.saving = True
        restoring = bool(self.entity.get("archived"))
        self.busy()
        def saved(receipt):
            self.saving = False
            self.entity = receipt.get("result", receipt)["entity"]
            self._render()
            if self.on_saved:
                self.on_saved(receipt)
            if restoring:
                self.accept()
        def failed(error):
            self.saving = False
            self.error(error)
        self.bridge.command("restore_task" if restoring else "delete_task",
                            {"id": self.entity["id"], "version": self.entity["version"]},
                            saved, failed, epoch=self.epoch)

    def reject(self):
        if not self.saving:
            super().reject()

    def closeEvent(self, event):
        if self.saving:
            event.ignore()
        else:
            super().closeEvent(event)


class EntityPicker(FormDialog):
    """Paged search; choosing a parent does not load the entire object forest."""
    def __init__(self, bridge, parent=None, allowed_types=None, exclude=None):
        super().__init__("选择事项", parent, 590)
        self.bridge = bridge
        self.epoch = bridge.epoch
        self.allowed_types = allowed_types
        self.exclude = set(exclude or [])
        self.selected = None
        self.generation = 0
        self.offset = 0
        self.next_offset = None
        self.search = QLineEdit()
        self.search.setPlaceholderText("输入名称搜索…")
        self.body_layout.addWidget(self.search)
        self.items = QListWidget()
        self.items.setMinimumHeight(280)
        self.body_layout.addWidget(self.items)
        self.count = QLabel()
        self.body_layout.addWidget(self.count)
        self.more = QPushButton("载入下一页")
        self.more.clicked.connect(self.load_more)
        self.body_layout.addWidget(self.more)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.reload)
        self.search.textChanged.connect(lambda: self.timer.start())
        self.items.itemDoubleClicked.connect(lambda _: self.choose())
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("选择")
        self.buttons.accepted.connect(self.choose)
        self.reload()

    def reload(self):
        self.offset = 0
        self.items.clear()
        self.load()

    def load_more(self):
        if self.next_offset is not None:
            self.offset = self.next_offset
            self.load()

    def load(self):
        self.generation += 1
        generation = self.generation
        params = {"search": self.search.text().strip(), "limit": 50, "offset": self.offset}
        if self.allowed_types:
            params["types"] = self.allowed_types
        def loaded(result):
            if generation != self.generation:
                return
            for entity in result.get("items", []):
                if entity["id"] in self.exclude:
                    continue
                item = QListWidgetItem(f"{entity['title']}\n{label_type(entity['type'])} · {label_status(entity.get('status'))}")
                item.setData(Qt.ItemDataRole.UserRole, entity)
                self.items.addItem(item)
            self.next_offset = result.get("next_offset")
            self.more.setEnabled(self.next_offset is not None)
            self.count.setText(f"找到 {result.get('total', self.items.count())} 项 · 已显示 {self.items.count()} 项")
        self.bridge.query("list", loaded, self.error, **params)

    def choose(self):
        item = self.items.currentItem()
        if item is None:
            self.error("请先选择一项。")
            return
        self.selected = item.data(Qt.ItemDataRole.UserRole)
        self.accept()


class FieldEditor(QWidget):
    """Optional typed value. Unchecked means omitted, not false or zero."""
    def __init__(self, name, definition, value=None, parent=None):
        super().__init__(parent)
        self.name = name
        self.definition = definition if isinstance(definition, dict) else {"type": definition}
        self.kind = self.definition.get("type", "string")
        self.kind = {"int": "integer", "float": "number", "str": "string", "bool": "boolean", "multiline": "text"}.get(self.kind, self.kind)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.enabled = QCheckBox()
        self.enabled.setToolTip("勾选后填写；留空保持待确认")
        self.enabled.setChecked(value is not None or self.definition.get("required", False))
        layout.addWidget(self.enabled)
        options = self.definition.get("enum") or self.definition.get("choices") or self.definition.get("options")
        self.options = options
        if options:
            self.editor = QComboBox()
            for entry in options:
                if isinstance(entry, dict):
                    self.editor.addItem(entry.get("label", str(entry.get("value"))), entry.get("value"))
                else:
                    self.editor.addItem(label_status(str(entry)), entry)
            if value is not None:
                i = self.editor.findData(value)
                if i < 0:
                    self.editor.addItem(str(value), value)
                    i = self.editor.count() - 1
                self.editor.setCurrentIndex(i)
        elif self.kind == "selection":
            initial = "，".join(str(item) for item in value) if isinstance(value, list) else (str(value) if value is not None else "")
            self.editor = QLineEdit(initial)
            self.editor.setPlaceholderText("多个类别用逗号分隔")
        elif self.kind in ("integer", "number"):
            self.editor = QSpinBox() if self.kind == "integer" else QDoubleSpinBox()
            self.editor.setRange(self.definition.get("minimum", -100000000), self.definition.get("maximum", 100000000))
            if value is not None:
                self.editor.setValue(float(value) if self.kind == "number" else int(value))
        elif self.kind == "boolean":
            self.editor = QCheckBox("是")
            self.editor.setChecked(bool(value))
        elif self.kind == "date" or self.definition.get("format") == "date":
            self.editor = QDateEdit()
            install_calendar(self.editor)
            self.editor.setDisplayFormat("yyyy-MM-dd")
            if value:
                self.editor.setDate(QDate.fromString(str(value), "yyyy-MM-dd"))
            elif self.definition.get('required'):
                self.editor.setDate(QDate.currentDate())
            else:
                self.editor.setSpecialValueText('未填写')
                self.editor.setDate(self.editor.minimumDate())
                self.enabled.setToolTip('日期已明确后再勾选；未勾选时不保存日期。')
        elif self.kind in ("object", "array") or self.definition.get("type") == "multiline" or name in ("description", "body", "text", "completion_gate", "content", "source_text"):
            self.editor = QTextEdit()
            self.editor.setMaximumHeight(120)
            if self.kind in ("object", "array"):
                self.editor.setPlaceholderText("此扩展字段为结构化内容，按模块格式填写")
                self.editor.setPlainText(json.dumps(value, ensure_ascii=False, indent=2) if value is not None else "")
            else:
                self.editor.setPlainText(str(value) if value is not None else "")
        else:
            self.editor = QLineEdit(str(value) if value is not None else "")
            if self.definition.get("format") == "date-time" or self.kind == "datetime":
                self.editor.setPlaceholderText("2026-09-22T09:00:00+08:00")
            elif name in ("due_date", "date", "effective_from", "effective_until"):
                self.editor.setPlaceholderText("YYYY-MM-DD")
        self.editor.setEnabled(self.enabled.isChecked())
        self.enabled.toggled.connect(self.editor.setEnabled)
        if isinstance(self.editor, QDateEdit):
            self.editor.setSpecialValueText('未填写')
            self._date_draft = self.editor.date() if value is not None else None
            def date_enabled(active):
                if active:
                    self.editor.setDate(self._date_draft or QDate.currentDate())
                else:
                    if self.editor.date() != self.editor.minimumDate():
                        self._date_draft = self.editor.date()
                    self.editor.setDate(self.editor.minimumDate())
            self.enabled.toggled.connect(date_enabled)
        layout.addWidget(self.editor, 1)

    def value(self):
        if not self.enabled.isChecked():
            if self.definition.get("required"):
                raise ValueError(f"请填写{self.definition.get('label', FIELD_LABELS.get(self.name, self.name))}。")
            return None
        if isinstance(self.editor, QComboBox):
            return self.editor.currentData()
        if isinstance(self.editor, (QSpinBox, QDoubleSpinBox)):
            return self.editor.value()
        if isinstance(self.editor, QCheckBox):
            return self.editor.isChecked()
        if isinstance(self.editor, QDateEdit):
            return self.editor.date().toString("yyyy-MM-dd")
        text = self.editor.toPlainText() if isinstance(self.editor, QTextEdit) else self.editor.text()
        if self.kind == "selection":
            values = [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]
            values = list(dict.fromkeys(values))
            if len(values) > 30:
                raise ValueError("一次最多填写 30 个类别。")
            return values or None
        if self.kind in ("object", "array"):
            return json.loads(text)
        if self.definition.get("required") and not text.strip():
            raise ValueError(f"请填写{self.definition.get('label', FIELD_LABELS.get(self.name, self.name))}。")
        return text.strip()


FORM_PROFILES = {
    'course': {'name':'课程', 'title':'课程名称', 'placeholder':'例如：工程数学',
        'intro':'先建立课程本身，再在课程中整理任务、课次和资料。课程可以独立存在，不需要放进另一个项目。',
        'fields':['code','notes'], 'labels':{'code':'课程代码（可选）','notes':'课程说明','term':'学期名称','semester_start':'第一教学周的周一','semester_end':'学期结束日期'},
        'hints':{'code':'例如课程编号；没有编号可以留空。','notes':'记录课程范围、平台或其他需要保留的说明。','term':'例如 2030 第一学期；未确认可留空。','semester_start':'用明确的第一教学周周一作为周次换算锚点；不知道时保持未填写，不自动猜测。','semester_end':'只填写已经明确的学期结束日期。'}},
    'project': {'name':'项目', 'title':'项目名称', 'placeholder':'例如：完成课程展示作品',
        'intro':'项目围绕一个可验收的结果组织工作。先写目标，再逐步加入阶段、任务和资料。',
        'fields':['purpose','acceptance','due_date'], 'labels':{'purpose':'想达到什么结果','acceptance':'怎样算项目完成','due_date':'项目截止日期'},
        'hints':{'purpose':'说明这件事为什么要做、最终要得到什么。','acceptance':'列出需要交付或验证的结果；未知条件可暂时留空。'}},
    'activity': {'name':'活动', 'title':'活动名称', 'placeholder':'例如：比赛、分享会或参访',
        'intro':'建立一次活动，集中管理参与任务和资料。具体时段可在活动下另建日程，不必把活动归入其他项目。',
        'fields':['notes','due_date'], 'labels':{'notes':'活动说明','due_date':'报名或提交截止日'},
        'hints':{'notes':'可记录地点、参与方式、联系人或需要准备的内容。','due_date':'仅在确有报名或提交期限时填写；不是自动推定的活动时间。'}},
    'task': {'name':'任务', 'title':'要做什么', 'placeholder':'例如：独立完成两道练习并核对',
        'intro':'任务是一件可以直接行动的事。明确完成条件，必要时选择课程、项目或其他归属。',
        'fields':['completion_gate','estimated_minutes','due_date'], 'labels':{'completion_gate':'怎样算完成','estimated_minutes':'预计用时（分钟）','due_date':'截止日期'},
        'hints':{'completion_gate':'写清需要达到的结果；预计用时不会缩减完成条件。'}},
    'domain': {'name':'分类', 'title':'分类名称', 'placeholder':'例如：学习、健康、职业',
        'intro':'分类用于长期整理不同课程、项目和活动，例如学习、健康或职业。分类本身不表示一项待完成任务。',
        'fields':['notes'], 'labels':{'notes':'分类说明'}},
    'goal': {'name':'目标', 'title':'目标名称', 'placeholder':'例如：形成稳定的学习习惯',
        'intro':'目标描述希望达到的长期状态。目标可关联不同项目，不必把所有工作收进一条父子链。',
        'fields':['success_criteria','due_date'], 'labels':{'success_criteria':'如何判断目标达成','due_date':'希望达成的日期'}},
    'phase': {'name':'阶段', 'title':'阶段名称', 'placeholder':'例如：完成资料收集',
        'intro':'阶段属于项目，用来组织这一段工作的任务和里程碑。阶段完成条件与每个任务的结果分别保留。',
        'fields':['exit_gate','target_date'], 'labels':{'exit_gate':'进入下一阶段前要完成什么','target_date':'预计推进日期'}},
    'milestone': {'name':'交付 / 检查点', 'title':'要交付或检查什么', 'placeholder':'例如：提交实验报告、完成项目演示',
        'intro':'记录一项可检查的交付或阶段结果。长期要求可留在笔记，具体行动放在任务中；这里不会自动生成每日任务。',
        'fields':['acceptance','due_date','notes','source_text'], 'labels':{'acceptance':'需要交付或达到的结果','due_date':'明确截止日期','notes':'说明','source_text':'来源与依据'},
        'hints':{'due_date':'只填写已经确认的日期。未定日期的旧记录仍可保存，但不会显示提前准备入口。','source_text':'保留课程通知、资料页码或你的明确说明，便于核对这项记录的含义。'}},
    'topic': {'name':'知识点', 'title':'知识点名称', 'placeholder':'例如：向量的几何意义',
        'intro':'知识点整理要理解的内容。建立条目不代表已经学习或掌握。',
        'fields':['description'], 'labels':{'description':'知识范围'}},
    'note': {'name':'笔记', 'title':'笔记标题', 'placeholder':'给这份内容一个容易查找的名称',
        'intro':'保存内容与来源；写成笔记不会自动创建任务或改变完成状态。',
        'fields':['content'], 'labels':{'content':'正文'}},
    'event': {'name':'日程', 'title':'日程名称', 'placeholder':'例如：课程、面试或出行',
        'intro':'记录已知日期和时段。时间未确定时可留空，不会被当作空闲时间。',
        'fields':['date','start','end','recurrence','until','location','notes','source_text'], 'labels':{'date':'第一次发生日期','start':'开始时间','end':'结束时间','recurrence':'重复方式','until':'重复结束日期','location':'地点（可选）','notes':'日程说明','source_text':'来源与依据'},
        'hints':{'recurrence':'无重复表示只发生一次。每周等重复日程按各次发生日期安排准备。','until':'仅在重复日程有明确结束日期时填写。','source_text':'保留时间表、通知或你的确认依据。'}},
    'assessment': {'name':'评分项目','title':'评分项目名称','placeholder':'例如：期末笔试、课程项目',
        'intro':'登记课程如何计分。权重不是已得成绩，也不表示这部分学习或提交已经完成。',
        'fields':['weight','due_date','source_text'],'labels':{'weight':'占总评百分比（%）','due_date':'相关截止日期','source_text':'评分依据'},
        'hints':{'weight':'填写已经确认的总评权重，未知时保持未填写。'}},
}


class EntityForm(FormDialog):
    def __init__(self, bridge, capabilities, parent=None, entity=None, default_type="task", parent_entity=None, on_saved=None, initial_payload=None, workflow=None):
        super().__init__("编辑事项" if entity else "新建事项", parent)
        self.bridge = bridge
        self.epoch = bridge.epoch
        self.entity = entity
        self.initial_payload = initial_payload or {}
        self.workflow = workflow
        self.on_saved = on_saved
        types = capabilities.get("types", [])
        self.types = {t["id"]: t for t in types} if isinstance(types, list) else types
        if not entity:
            reserved = RESERVED_TYPES
            self.types = {k: v for k, v in self.types.items() if k not in reserved and not v.get("read_only")}
        self.context_parent = parent_entity
        chosen = entity['type'] if entity else default_type
        self.parent_id = entity.get('parent_id') if entity else (parent_entity or {}).get('id', self.initial_payload.get('parent_id'))
        self.ignored_context = False
        if not entity and parent_entity and parent_entity.get('type') not in self.types.get(chosen, {}).get('parent_types', []):
            self.parent_id, self.context_parent, self.ignored_context = None, None, True
        self.fields = {}
        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignTop)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.intro = QLabel()
        self.intro.setWordWrap(True)
        self.intro.setObjectName('Hint')
        self.body_layout.addWidget(self.intro)
        self.body_layout.addLayout(self.form)
        self.type_box = QComboBox()
        for key, definition in self.types.items():
            self.type_box.addItem("分类" if key == "domain" else definition.get("label", label_type(key)), key)
        chosen = entity["type"] if entity else default_type
        i = self.type_box.findData(chosen)
        if i >= 0:
            self.type_box.setCurrentIndex(i)
        self.type_box.setEnabled(entity is None)
        self.form.addRow("类型", self.type_box)
        self.form.setRowVisible(self.type_box, False)
        self.setWindowTitle(("编辑" if entity else "新建") + self.types.get(chosen, {}).get("label", label_type(chosen)))
        self.title_edit = QLineEdit(entity.get("title", "") if entity else self.initial_payload.get("title", ""))
        self.title_edit.setPlaceholderText("要做的事或需要保存的信息")
        self.title_label = QLabel("名称 *")
        self.form.addRow(self.title_label, self.title_edit)
        self.status_box = QComboBox()
        for status in ("pending", "planned", "active", "blocked", "done", "cancelled", "draft", "failed"):
            self.status_box.addItem(label_status(status), status)
        if entity:
            status = entity.get("status", "active")
            i = self.status_box.findData(status)
            if i < 0:
                self.status_box.addItem(label_status(status), status)
                i = self.status_box.count() - 1
            self.status_box.setCurrentIndex(i)
        self.form.addRow("状态", self.status_box)
        self.form.setRowVisible(self.status_box, False)
        self.parent_label = QLabel()
        self.parent_area = QWidget()
        parent_layout = QVBoxLayout(self.parent_area)
        parent_layout.setContentsMargins(0, 0, 0, 0)
        self.classification_enabled = QCheckBox('按长期分类整理（可选）')
        parent_layout.addWidget(self.classification_enabled)
        self.parent_picker = QWidget()
        parent_row = QHBoxLayout(self.parent_picker)
        parent_row.setContentsMargins(0, 0, 0, 0)
        self.parent_button = QPushButton((self.context_parent or {}).get('title', '选择归属…'))
        self.parent_button.clicked.connect(self.choose_parent)
        self.clear_parent_button = QPushButton('清除')
        self.clear_parent_button.clicked.connect(self.clear_parent)
        parent_row.addWidget(self.parent_button, 1)
        parent_row.addWidget(self.clear_parent_button)
        parent_layout.addWidget(self.parent_picker)
        self.parent_info = QLabel()
        self.parent_info.setWordWrap(True)
        self.parent_info.setObjectName('Hint')
        parent_layout.addWidget(self.parent_info)
        self.form.addRow(self.parent_label, self.parent_area)
        self.classification_enabled.toggled.connect(self._classification_changed)
        if self.parent_id and not self.context_parent:
            self.parent_button.setText('正在读取归属…')
            selected_parent = self.parent_id
            self.bridge.query('get', lambda result: self.parent_button.setText(result['entity']['title']) if self.parent_id == selected_parent else None,
                              lambda _: self.parent_button.setText('归属暂不可读取'), id=self.parent_id)
        self.event_owner_id = (entity or {}).get("data", {}).get("owner_id") if entity else self.initial_payload.get("data", {}).get("owner_id")
        self.owner_row = QWidget()
        owner_layout = QHBoxLayout(self.owner_row)
        owner_layout.setContentsMargins(0, 0, 0, 0)
        self.owner_button = QPushButton("未关联")
        self.owner_button.clicked.connect(self.choose_event_owner)
        self.clear_owner_button = QPushButton("清除")
        self.clear_owner_button.clicked.connect(self.clear_event_owner)
        owner_layout.addWidget(self.owner_button, 1)
        owner_layout.addWidget(self.clear_owner_button)
        self.form.addRow("关联项目或课程", self.owner_row)
        if self.event_owner_id:
            expected_owner = self.event_owner_id
            self.owner_button.setText("正在读取关联事项…")
            self.bridge.query("get", lambda result: self.owner_button.setText(result["entity"]["title"]) if self.event_owner_id == expected_owner else None, lambda _: self.owner_button.setText("关联事项暂不可读取"), id=expected_owner)
        self.field_area = QWidget()
        self.field_layout = QFormLayout(self.field_area)
        self.field_layout.setContentsMargins(0, 12, 0, 0)
        self.field_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.body_layout.addWidget(self.field_area)
        self.more_fields = QPushButton('更多信息')
        self.more_fields.setCheckable(True)
        self.more_fields.toggled.connect(self.toggle_more_fields)
        self.body_layout.addWidget(self.more_fields)
        self.form_hint = QLabel()
        self.form_hint.setWordWrap(True)
        self.form_hint.setObjectName('Hint')
        self.body_layout.addWidget(self.form_hint)
        self.body_layout.addStretch(1)
        self.type_box.currentIndexChanged.connect(self.rebuild_fields)
        self.rebuild_fields()
        self.buttons.accepted.connect(self.save)
        if entity and entity["type"] == "task":
            self.delete_button = QPushButton("恢复任务" if entity.get("archived") else "删除任务")
            self.delete_button.setObjectName("DeleteTask")
            self.delete_button.clicked.connect(self.delete_task)
            self.buttons.addButton(self.delete_button, QDialogButtonBox.ButtonRole.ActionRole)

    def delete_task(self):
        dialog = DeleteTaskDialog(self.bridge, self.entity, self, self.on_saved)
        try:
            dialog.exec()
            self.entity = dialog.entity
            if self.entity.get("archived"):
                self.accept()
        finally:
            dialog.deleteLater()

    def rebuild_fields(self):
        while self.field_layout.rowCount():
            self.field_layout.removeRow(0)
        self.fields.clear()
        definition = self.types.get(self.type_box.currentData(), {})
        is_event = self.type_box.currentData() == "event"
        self.form.setRowVisible(self.owner_row, is_event)
        self._configure_profile()
        self._configure_parent()
        raw_fields = definition.get("fields", {})
        if isinstance(raw_fields, list):
            raw_fields = {f.get("id", f.get("name")): f for f in raw_fields}
        raw_fields = dict(raw_fields)
        if self.type_box.currentData() in {"task", "milestone", "event"}:
            raw_fields.setdefault("source_text", {"id": "source_text", "label": "来源与依据", "type": "multiline"})
        if is_event:
            raw_fields.setdefault("location", {"id": "location", "label": "地点", "type": "text"})
        old_status = self.status_box.currentData()
        self.status_box.clear()
        for key, label in definition.get("statuses", {"pending": "待确认", "active": "进行中", "done": "已完成"}).items():
            self.status_box.addItem(label, key)
        index = self.status_box.findData(old_status)
        if index >= 0:
            self.status_box.setCurrentIndex(index)
        current = self.entity.get("data", {}) if self.entity else self.initial_payload.get("data", {})
        profile = FORM_PROFILES.get(self.type_box.currentData(), {})
        primary = profile.get('fields', list(raw_fields))
        self.primary_field_names = [key for key in primary if key in raw_fields]
        essentials = set(self.primary_field_names)
        raw_fields = {key: raw_fields[key] for key in [*self.primary_field_names, *(k for k in raw_fields if k not in essentials)]}
        self.extra_fields = []
        for key, spec in raw_fields.items():
            if self.type_box.currentData() == "task" and key.startswith("catchup_"):
                continue  # Edited through the dedicated catch-up workflow.
            if key in ("id", "title", "status", "parent_id", "version"):
                continue
            spec = spec if isinstance(spec, dict) else {"type": spec}
            editor = FieldEditor(key, spec, current.get(key), self)
            self.fields[key] = editor
            if spec.get("read_only"):
                editor.setEnabled(False)
                editor.setToolTip("此字段所属模块已停用，历史值保持不变")
            label = profile.get('labels', {}).get(key, spec.get('label', FIELD_LABELS.get(key, key)))
            self.field_layout.addRow(label, editor)
            hint = profile.get('hints', {}).get(key)
            if hint:
                editor.setToolTip(hint)
                if isinstance(editor.editor, (QTextEdit, QLineEdit)):
                    editor.editor.setPlaceholderText(hint)
            if key not in essentials:
                self.extra_fields.append(editor)
                self.field_layout.setRowVisible(editor, self.more_fields.isChecked())
            elif spec.get('type') in {'text','multiline'}:
                editor.enabled.setChecked(True)
                editor.enabled.hide()

        if self.entity is None:
            index = self.status_box.findData('active')
            if index >= 0:
                self.status_box.setCurrentIndex(index)
        self.more_fields.setVisible(bool(self.extra_fields) or self.entity is not None)
        self.more_fields.setText(('收起学期信息' if self.more_fields.isChecked() else '学期与更多信息') if self.type_box.currentData() == 'course' else ('收起更多信息' if self.more_fields.isChecked() else '更多信息'))
        if not raw_fields:
            editor = FieldEditor("description", {"type": "text", "label": "说明"}, current.get("description"), self)
            self.fields["description"] = editor
            self.field_layout.addRow("说明", editor)

    def _configure_profile(self):
        kind = self.type_box.currentData()
        profile = FORM_PROFILES.get(kind, {})
        name = profile.get('name', self.types.get(kind, {}).get('label', label_type(kind)))
        title = ('编辑' if self.entity else '新建') + name
        if not self.entity and kind == 'project' and self.context_parent and self.context_parent.get('type') == 'project':
            title = '新建子项目'
        self.setWindowTitle(title)
        self.heading.setText(title)
        self.title_label.setText(profile.get('title', '名称') + ' *')
        self.title_edit.setPlaceholderText(profile.get('placeholder', '填写容易识别的名称'))
        self.intro.setText(profile.get('intro', '按此类型保存内容。未提供的信息保持待确认；扩展字段与历史记录会保留。'))
        hints = {
            'course': '课程信息可以分次补充；资料、任务和学习情况在课程内分别记录。',
            'project': '先保存已知目标；日期与验收条件可以稍后补充。',
            'activity': '参加、提交与活动结果分别记录；建立活动不表示已报名或已出席。',
            'domain': '分类只改变整理方式，不会自动增加任务或改变完成情况。',
            'goal': '目标可以关联多个项目；建立目标不会自动安排每日任务。',
        }
        self.form_hint.setText(hints.get(kind, '未填写的用时和日期保持待确认；完成、提交和掌握分别记录。'))
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存修改' if self.entity else '建立' + name)

    def _configure_parent(self):
        kind = self.type_box.currentData()
        allowed = [x for x in self.types.get(kind, {}).get('parent_types', []) if x]
        self.parent_picker_types = allowed
        context_type = (self.context_parent or {}).get('type')
        anchored = bool(self.parent_id and kind == 'project' and context_type != 'domain' and not self.entity)
        classification = kind in {'course', 'project', 'activity', 'goal', 'domain'} and not anchored
        if self.entity:
            self.parent_mode = 'existing'
            self.parent_label.setText('当前归属')
            self.parent_info.setText('此处保留现有归属。需要移动时，请在项目与课程中调整。')
        elif anchored:
            self.parent_mode = 'context'
            self.parent_label.setText('所属' + label_type(context_type or 'project'))
            self.parent_info.setText('作为这里的子项目建立；它仍有自己的目标、任务和完成条件。')
        elif classification:
            self.parent_mode = 'classification'
            self.parent_picker_types = ['domain'] if 'domain' in allowed else []
            self.parent_label.setText('上级分类（可选）' if kind == 'domain' else '分类（可选）')
            self.parent_info.setText('例如学习、健康、职业。不选择分类也可以独立建立。')
        else:
            self.parent_mode = 'assignment'
            self.parent_label.setText('任务归属' if kind == 'task' else '归属')
            self.parent_info.setText('选择与这项工作直接相关的课程、项目或阶段；也可以保留为独立任务。' if kind == 'task' else '')
        self.classification_enabled.blockSignals(True)
        self.classification_enabled.setChecked(bool(self.parent_id))
        self.classification_enabled.blockSignals(False)
        self.classification_enabled.setVisible(self.parent_mode == 'classification')
        self.parent_picker.setVisible(self.parent_mode != 'classification' or bool(self.parent_id))
        editable = self.entity is None and self.parent_mode != 'context' and bool(self.parent_picker_types)
        self.parent_button.setEnabled(editable)
        self.clear_parent_button.setEnabled(editable)
        self.clear_parent_button.setVisible(self.parent_mode not in {'existing', 'context'})
        self.form.setRowVisible(self.parent_area, bool(allowed) or bool(self.parent_id))
        if not self.parent_id:
            self.parent_button.setText('选择分类…' if self.parent_mode == 'classification' else '独立任务（可选择归属）' if kind == 'task' else '选择归属…')
        elif self.context_parent:
            self.parent_button.setText(self.context_parent['title'])

    def _classification_changed(self, enabled):
        if getattr(self, 'parent_mode', '') != 'classification':
            return
        self.parent_picker.setVisible(enabled)
        if not enabled:
            self.parent_id = None
            self.parent_button.setText('选择分类…')

    def toggle_more_fields(self, visible):
        for editor in getattr(self, 'extra_fields', []):
            self.field_layout.setRowVisible(editor, visible)
        self.form.setRowVisible(self.status_box, bool(visible and self.entity))
        self.more_fields.setText(('收起学期信息' if visible else '学期与更多信息') if self.type_box.currentData() == 'course' else ('收起更多信息' if visible else '更多信息'))

    def set_event_owner(self, entity):
        self.event_owner_id = entity["id"]
        self.owner_button.setText(entity["title"])

    def choose_event_owner(self):
        picker = EntityPicker(self.bridge, self, allowed_types=["domain", "project", "course", "activity", "phase"])
        if picker.exec() == QDialog.DialogCode.Accepted:
            self.set_event_owner(picker.selected)

    def clear_event_owner(self):
        self.event_owner_id = None
        self.owner_button.setText("未关联")

    def choose_parent(self):
        if self.entity or self.parent_mode == 'context' or not self.parent_picker_types:
            return
        picker = EntityPicker(self.bridge, self, allowed_types=self.parent_picker_types)
        if self.parent_mode == 'classification':
            picker.setWindowTitle('选择分类')
            picker.heading.setText('选择分类')
        if picker.exec() == QDialog.DialogCode.Accepted:
            if picker.selected.get('type') not in self.parent_picker_types:
                self.error('这项内容不能作为当前类型的归属。')
                return
            self.parent_id = picker.selected['id']
            self.parent_button.setText(picker.selected['title'])

    def clear_parent(self):
        if self.entity or getattr(self, 'parent_mode', '') == 'context':
            return
        self.parent_id = None
        if getattr(self, 'parent_mode', '') == 'classification':
            self.classification_enabled.setChecked(False)
            self.parent_button.setText('选择分类…')
        else:
            self.parent_button.setText('独立任务（可选择归属）' if self.type_box.currentData() == 'task' else '选择归属…')

    def save(self):
        title = self.title_edit.text().strip()
        if not title:
            self.error('请填写' + FORM_PROFILES.get(self.type_box.currentData(), {}).get('title', '名称') + '。')
            return
        if self.parent_mode == 'classification' and self.classification_enabled.isChecked() and not self.parent_id:
            self.error('请选择一个分类，或取消分类选项以独立建立。')
            return
        try:
            data = {}
            for name, editor in self.fields.items():
                if editor.definition.get("read_only"):
                    continue
                value = editor.value()
                if value is not None:
                    prior = (self.entity or self.initial_payload).get('data', {})
                    if value == '' and prior.get(name) is None and not editor.definition.get('required'):
                        continue
                    data[name] = value
                elif self.entity and name in self.entity.get("data", {}):
                    data[name] = None
            if self.type_box.currentData() == "event":
                data["owner_id"] = self.event_owner_id
        except (ValueError, TypeError) as exc:
            self.error(str(exc))
            return
        self.busy()
        if self.entity:
            payload = {"id": self.entity["id"], "version": self.entity["version"], "patch": {"title": title, "status": self.status_box.currentData(), "data": data}}
            command = "update"
        else:
            payload = {"type": self.type_box.currentData(), "title": title, "status": self.status_box.currentData(), "parent_id": self.parent_id, "data": data}
            command = "create"
        def saved(result):
            if self.on_saved:
                self.on_saved(result)
            self.accept()
        if self.workflow:
            payload = {"module_id": self.workflow["module_id"], "workflow_id": self.workflow["id"], "input": payload}
            command = "run_workflow"
        self.bridge.command(command, payload, saved, self.error, epoch=self.epoch)


class FeedbackDialog(FormDialog):
    def __init__(self, bridge, target, parent=None, on_saved=None, workflow=None):
        super().__init__("记录执行反馈", parent)
        self.bridge, self.target, self.on_saved = bridge, target, on_saved
        self.workflow = workflow
        self.epoch = bridge.epoch
        self.body_layout.addWidget(QLabel(target["title"]))
        form = QFormLayout()
        self.body_layout.addLayout(form)
        self.date = QDateEdit(QDate.currentDate())
        install_calendar(self.date)
        self.date.setDisplayFormat("yyyy-MM-dd")
        form.addRow("反馈归属日期", self.date)
        self.editors = {}
        choices = {
            "completion": [("未完成", "incomplete"), ("未开始", "not_started"), ("部分完成", "partial"), ("已完成", "done"), ("受阻", "blocked")],
            "attendance": [("已到场", "attended"), ("未到场", "absent"), ("活动取消", "cancelled"), ("异步替代", "asynchronous"), ("在线替代", "online_replacement")],
            "viewing": [("未观看", "not_viewed"), ("部分观看", "partial"), ("已看完", "viewed")],
            "submission": [("未提交", "not_submitted"), ("已提交", "submitted"), ("已验收", "accepted"), ("被退回", "rejected")],
            "mastery": [("尚未测验", "not_tested"), ("需要复习", "needs_review"), ("已验证掌握", "verified")],
        }
        for key, options in choices.items():
            box = QComboBox()
            box.addItem("本次不更新", None)
            for label, value in options:
                box.addItem(label, value)
            self.editors[key] = box
            form.addRow(FIELD_LABELS[key], box)
        self.minutes = FieldEditor("actual_minutes", {"type": "integer", "minimum": 0, "maximum": 1440})
        form.addRow("实际用时（分钟）", self.minutes)
        self.source = QTextEdit()
        self.source.setPlaceholderText("写下本次实际发生的情况，作为这次更新的依据…")
        self.source.setMinimumHeight(120)
        form.addRow("原始反馈 *", self.source)
        note = QLabel("完成、到场、观看、提交和掌握分别记录。反馈不会自动把整日计划重新安排。")
        note.setWordWrap(True)
        note.setObjectName("Hint")
        self.body_layout.addWidget(note)
        self.buttons.accepted.connect(self.save)

    def save(self):
        dimensions = {key: box.currentData() for key, box in self.editors.items() if box.currentData() is not None}
        minutes = self.minutes.value()
        if minutes is not None:
            dimensions["actual_minutes"] = minutes
        source = self.source.toPlainText().strip()
        if not source or not dimensions:
            self.error("请填写原始反馈，并至少明确更新一项情况。")
            return
        self.busy()
        def saved(result):
            if self.on_saved:
                self.on_saved(result)
            self.accept()
        payload = {"target_id": self.target["id"], "business_date": self.date.date().toString("yyyy-MM-dd"), "dimensions": dimensions, "source_text": source}
        command = "record_feedback"
        if self.workflow:
            command = "run_workflow"
            payload = {"module_id": self.workflow["module_id"], "workflow_id": self.workflow["id"], "input": payload}
        self.bridge.command(command, payload, saved, self.error, epoch=self.epoch)
