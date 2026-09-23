"""Focused project/course workspace and a lazy, API-backed hierarchy tree."""
from __future__ import annotations
import html
import re
from pathlib import Path
from PySide6.QtCore import QDate, QSize, Qt, Signal, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea, QDateEdit,
    QProxyStyle, QStyle, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QTextBrowser, QSizePolicy,
)
from .gui_forms import label_type, label_status, DeleteTaskDialog
from .gui_calendar import install_calendar
from .gui_theme import color, bind_theme
from .gui_sources import AddSourceDialog, SourceContentDialog, extraction_label, source_origin, file_kind

BASE_TYPES = ("task", "project", "course", "activity", "domain", "goal")
TREE_TYPES = (*BASE_TYPES, "phase", "milestone", "topic", "note")
CONTAINERS = {"domain", "project", "course", "activity", "phase", "task", "note"}
TYPE_MARKS = {"domain": "域", "project": "项", "course": "课", "activity": "活", "goal": "目", "phase": "阶", "milestone": "里", "topic": "知", "task": "·", "note": "记"}


def make_button(text, callback=None, primary=False):
    result = QPushButton(text)
    if callback:
        result.clicked.connect(callback)
    if primary:
        result.setObjectName("Primary")
    return result


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget:
            widget.deleteLater()
        elif item.layout():
            clear_layout(item.layout())


def plain_label(text, name=None):
    result = QLabel(text)
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setWordWrap(True)
    if name:
        result.setObjectName(name)
    return result


class CountProgress(QFrame):
    """A count of confirmed task outcomes; never a time or mastery percentage."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ProgressCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        top = QHBoxLayout()
        self.title = plain_label("任务进展", "SectionHeading")
        self.count = plain_label("尚无可计算进度", "Quiet")
        top.addWidget(self.title, 1)
        top.addWidget(self.count)
        layout.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setFixedHeight(7)
        self.bar.setTextVisible(False)
        layout.addWidget(self.bar)
        self.explanation = plain_label("", "Quiet")
        layout.addWidget(self.explanation)
        self.set_counts(None)

    def set_counts(self, summary):
        summary = summary or {}
        total = summary.get("total_tasks", summary.get("total"))
        done = summary.get("done_tasks", summary.get("done"))
        incomplete = summary.get("incomplete_tasks", summary.get("incomplete"))
        unknown = summary.get("unknown_tasks", summary.get("unreported", summary.get("unknown")))
        self.total, self.done, self.unknown = total, done, unknown
        if type(total) is not int or total < 0 or type(done) is not int or done < 0 or done > total:
            self.bar.hide()
            self.count.setText("尚无可计算进度")
            self.explanation.setText("有明确任务范围和完成记录后，在这里显示进展。")
            return
        if total == 0:
            self.bar.hide()
            self.count.setText("还没有任务")
            self.explanation.setText("添加第一项任务后，这里会显示任务条目的完成情况。")
            return
        self.count.setText(f"明确完成 {done} / {total} 项")
        self.bar.setRange(0, total)
        self.bar.setValue(done)
        self.bar.show()
        parts = []
        if type(incomplete) is int and incomplete:
            parts.append(f"{incomplete} 项明确未完成")
        if type(unknown) is int and unknown:
            parts.append(f"{unknown} 项尚未反馈")
        elif unknown is None:
            parts.append("其余任务的反馈覆盖情况待确认")
        self.explanation.setText(" · ".join(parts) or "全部任务均有明确完成记录")


class HierarchyBranchStyle(QProxyStyle):
    def __init__(self):
        super().__init__("Fusion")

    def drawPrimitive(self, element, option, painter, widget=None):
        if element == QStyle.PrimitiveElement.PE_IndicatorBranch:
            if option.state & QStyle.StateFlag.State_Children:
                center = option.rect.center()
                x, y = center.x(), center.y()
                path = QPainterPath()
                if option.state & QStyle.StateFlag.State_Open:
                    path.moveTo(x - 4, y - 2)
                    path.lineTo(x + 4, y - 2)
                    path.lineTo(x, y + 3)
                else:
                    path.moveTo(x - 2, y - 4)
                    path.lineTo(x + 3, y)
                    path.lineTo(x - 2, y + 4)
                path.closeSubpath()
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(color("muted")))
                painter.drawPath(path)
                painter.restore()
            return
        super().drawPrimitive(element, option, painter, widget)


class HierarchyTree(QTreeWidget):
    object_selected = Signal(object)
    changed = Signal(object)
    error = Signal(object)

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.type_map = {}
        self.generation = 0
        self._loading = set()
        self._expanded = set()
        self._selected_id = None
        self.setObjectName("WorkspaceTree")
        self.branch_style = HierarchyBranchStyle()
        self.branch_style.setParent(self)
        self.setStyle(self.branch_style)
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setIndentation(18)
        self.setAnimated(True)
        self.setUniformRowHeights(True)
        self.setMinimumWidth(228)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.itemExpanded.connect(self._expanded_item)
        self.itemClicked.connect(self._clicked)
        bind_theme(self, self.resize_rows)

    def resize_rows(self):
        for item in self.iter_items(): item.setSizeHint(0, QSize(210, max(39, self.fontMetrics().height() + 20)))
        self.doItemsLayout()

    def set_types(self, type_map):
        self.type_map = type_map
        self.refresh()

    def refresh(self):
        self._expanded = {item.data(0, Qt.ItemDataRole.UserRole)["id"] for item in self.iter_items() if item.isExpanded() and item.data(0, Qt.ItemDataRole.UserRole) and "id" in item.data(0, Qt.ItemDataRole.UserRole)}
        current = self.currentItem()
        if current and current.data(0, Qt.ItemDataRole.UserRole):
            self._selected_id = current.data(0, Qt.ItemDataRole.UserRole).get("id")
        self.generation += 1
        self._loading.clear()
        self.clear()
        self.load_children(None)

    def iter_items(self, parent=None):
        count = parent.childCount() if parent else self.topLevelItemCount()
        for index in range(count):
            item = parent.child(index) if parent else self.topLevelItem(index)
            yield item
            yield from self.iter_items(item)

    def load_children(self, parent_item, offset=0):
        parent_entity = parent_item.data(0, Qt.ItemDataRole.UserRole) if parent_item else None
        identifier = parent_entity.get("id") if parent_entity else None
        key = (identifier, offset)
        if key in self._loading:
            return
        self._loading.add(key)
        generation = self.generation
        def loaded(result):
            self._loading.discard(key)
            if generation != self.generation:
                return
            if parent_item and offset == 0:
                parent_item.takeChildren()
                parent_item.setData(0, Qt.ItemDataRole.UserRole + 1, True)
            for entity in result.get("items", []):
                item = QTreeWidgetItem()
                definition = self.type_map.get(entity["type"], {})
                item.setText(0, f"{TYPE_MARKS.get(entity['type'], '·')}  {entity['title']}")
                item.setToolTip(0, definition.get("label", label_type(entity["type"])) + " · " + entity["title"])
                item.setData(0, Qt.ItemDataRole.UserRole, entity)
                item.setSizeHint(0, QSize(210, max(39, self.fontMetrics().height() + 20)))
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
                if entity["type"] in CONTAINERS or any(entity["type"] in other.get("parent_types", []) for other in self.type_map.values()):
                    flags |= Qt.ItemFlag.ItemIsDropEnabled
                    item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                if definition.get("read_only"):
                    flags &= ~Qt.ItemFlag.ItemIsDragEnabled
                item.setFlags(flags)
                if parent_item:
                    parent_item.addChild(item)
                else:
                    self.addTopLevelItem(item)
                if entity["id"] == self._selected_id:
                    self.setCurrentItem(item)
                if entity["id"] in self._expanded:
                    item.setExpanded(True)
            next_offset = result.get("next_offset")
            if next_offset is not None:
                more = QTreeWidgetItem(["载入更多…"])
                more.setData(0, Qt.ItemDataRole.UserRole, {"_page": next_offset, "_parent_id": identifier})
                more.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                more.setSizeHint(0, QSize(210, max(37, self.fontMetrics().height() + 20)))
                (parent_item.addChild if parent_item else self.addTopLevelItem)(more)
            if parent_item and parent_item.childCount() == 0:
                parent_item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator)
        def failed(error):
            self._loading.discard(key)
            if generation == self.generation:
                self.error.emit(error)
        types = [kind for kind, definition in self.type_map.items() if kind in TREE_TYPES or definition.get("module") not in (None, "builtin")]
        self.bridge.query("list", loaded, failed, parent_id=identifier, types=types, limit=100, offset=offset)

    def _expanded_item(self, item):
        if not item.data(0, Qt.ItemDataRole.UserRole + 1):
            self.load_children(item)

    def _clicked(self, item, column):
        entity = item.data(0, Qt.ItemDataRole.UserRole)
        if not entity:
            return
        if "_page" in entity:
            parent = item.parent()
            index = parent.indexOfChild(item) if parent else self.indexOfTopLevelItem(item)
            if parent:
                parent.takeChild(index)
            else:
                self.takeTopLevelItem(index)
            self.load_children(parent, entity["_page"])
            return
        self._selected_id = entity["id"]
        self.object_selected.emit(entity)

    def breadcrumb(self, identifier):
        item = next((item for item in self.iter_items() if (item.data(0, Qt.ItemDataRole.UserRole) or {}).get("id") == identifier), None)
        names = []
        while item:
            entity = item.data(0, Qt.ItemDataRole.UserRole) or {}
            if entity.get("title"):
                names.append(entity["title"])
            item = item.parent()
        return list(reversed(names))

    def request_move(self, source, target=None):
        """Move ownership through the core; Qt never mutates the tree optimistically."""
        if not source or "_page" in source:
            return False
        parent_id = target.get("id") if target else None
        if source.get("id") == parent_id:
            self.error.emit({"message": "不能把事项放入它自己。"})
            return False
        if source.get("parent_id") == parent_id:
            return False
        allowed = self.type_map.get(source["type"], {}).get("parent_types", [])
        target_type = target.get("type") if target else None
        if target_type not in allowed:
            self.error.emit({"message": "这种事项不能放在所选目录中，请选择项目、课程或其他合适的归属。"})
            return False
        def saved(receipt):
            self.refresh()
            self.changed.emit(receipt)
        self.bridge.command("move", {"id": source["id"], "version": source["version"], "parent_id": parent_id}, saved, self.error.emit)
        return True

    def dropEvent(self, event):
        source_item = self.currentItem()
        target_item = self.itemAt(event.position().toPoint())
        source = source_item.data(0, Qt.ItemDataRole.UserRole) if source_item else None
        if target_item and self.dropIndicatorPosition() in (QAbstractItemView.DropIndicatorPosition.AboveItem, QAbstractItemView.DropIndicatorPosition.BelowItem):
            target_item = target_item.parent()
        cursor = target_item
        while cursor:
            if cursor is source_item:
                self.error.emit({"message": "不能把事项放入自己或自己的下级中。"})
                event.ignore()
                return
            cursor = cursor.parent()
        target = target_item.data(0, Qt.ItemDataRole.UserRole) if target_item else None
        if target and "_page" in target:
            event.ignore()
            return
        self.request_move(source, target)
        event.ignore()


def has_preparation_date(entity):
    data = entity.get("data", {})
    value = data.get("date" if entity.get("type") == "event" else "due_date")
    return bool(isinstance(value, str) and QDate.fromString(value, "yyyy-MM-dd").isValid() and not entity.get("archived") and entity.get("status") not in {"done", "cancelled"})


def preparation_label(entity):
    if entity.get("type") == "event" and entity.get("data", {}).get("recurrence") in {"daily", "weekly", "monthly"}:
        return "每次日程前准备"
    return "提前准备（一次）"


class NoteReader(QTextBrowser):
    def loadResource(self, resource_type, url):
        return None


class TaskDetailDialog(QDialog):
    def __init__(self, bridge, entity, parent=None, on_edit=None, on_open=None, on_saved=None, business_date=None, on_codex=None):
        super().__init__(parent)
        self.bridge, self.entity, self.on_saved = bridge, entity, on_saved
        self.completion_saving = False
        self.completion_epoch, self.completion_revision = bridge.epoch, bridge.revision
        self.completion_date = business_date or QDate.currentDate().toString("yyyy-MM-dd")
        self.destroyed.connect(lambda *_: setattr(self, "closed", True))
        self.plan_snapshot = None
        self.plan_generation = 0
        self.plan_ready = False
        self.saving_plan = False
        self.closed = False
        self.dialogs = []
        self.finished.connect(lambda _: setattr(self, "closed", True))
        self.setWindowTitle(entity.get("title", "事项"))
        self.resize(760, 640) if entity["type"] == "note" else self.resize(610, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(15)
        layout.addWidget(plain_label("交付 / 检查点" if entity["type"] == "milestone" else label_type(entity["type"]), "Eyebrow"))
        layout.addWidget(plain_label(entity.get("display_title") or entity["title"], "DialogHeading"))
        if entity.get("owner_label"): layout.addWidget(plain_label(entity["owner_label"], "StatusPill"))
        data = entity.get("data", {})
        hints = [] if entity.get("type") in {"task", "note", "topic"} else [label_status(entity.get("status"))]
        if data.get("due_date"):
            hints.append("截止 " + data["due_date"])
        if data.get("estimated_minutes") is not None:
            hints.append(f"预计 {data['estimated_minutes']} 分钟")
        if hints:
            layout.addWidget(plain_label(" · ".join(hints), "Quiet"))
        details = QWidget(); details_layout = QVBoxLayout(details); details_layout.setContentsMargins(0, 0, 6, 0); details_layout.setSpacing(12)
        detail_scroll = QScrollArea(); detail_scroll.setWidgetResizable(True); detail_scroll.setFrameShape(QFrame.Shape.NoFrame); detail_scroll.setWidget(details); layout.addWidget(detail_scroll, 1)
        for key, heading in [("completion_gate", "完成标准"), ("notes", "说明"), ("description", "说明"), ("content", "内容"), ("acceptance", "需要交付或达到的结果"), ("source_text", "来源与依据")]:
            if data.get(key):
                details_layout.addWidget(plain_label(heading, "SectionHeading"))
                if entity["type"] == "note" and key == "content":
                    content = NoteReader(); content.setOpenExternalLinks(False); content.setOpenLinks(False); content.setMinimumHeight(260)
                    text = str(data[key])
                    if data.get("content_format") == "markdown" or re.search(r"(?m)^#{1,6}\s|^[-*]\s", text): content.setMarkdown(text)
                    else: content.setPlainText(text)
                else:
                    content = plain_label(str(data[key])); content.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                details_layout.addWidget(content)
        details_layout.addStretch()
        if entity["type"] == "task" and not entity.get("archived"):
            quick = QHBoxLayout()
            self.complete_button = make_button("标记未完成" if (entity.get("completion_state") == "done" if entity.get("completion_state") is not None else entity.get("status") == "done") else "标记完成", self.mark_complete, True)
            quick.addWidget(self.complete_button); quick.addStretch(); layout.addLayout(quick)
            self.completion_note = plain_label("完成记录与课程任务、补欠和当天复盘同步；不会自动确认到课、提交或掌握。", "Quiet")
            layout.addWidget(self.completion_note)
            layout.addWidget(plain_label("加入日计划后，也可在今天页直接记录完成情况。", "Quiet"))
            entry = QHBoxLayout()
            self.plan_date = QDateEdit(QDate.fromString(business_date, "yyyy-MM-dd") if business_date else QDate.currentDate())
            self.plan_date.setDisplayFormat("yyyy-MM-dd"); self.plan_date.setCalendarPopup(True); install_calendar(self.plan_date)
            entry.addWidget(self.plan_date)
            self.join_plan_button = make_button("加入这天计划", self.join_plan, True)
            self.join_plan_button.setEnabled(False); entry.addWidget(self.join_plan_button); entry.addStretch(); layout.addLayout(entry)
            self.plan_note = plain_label("正在读取当天安排…", "Quiet"); layout.addWidget(self.plan_note)
            self.plan_date.dateChanged.connect(self.load_plan); self.load_plan()
        elif entity["type"] == "milestone":
            layout.addWidget(plain_label("这里记录交付或检查点，不直接算作每日任务。长期要求和学习建议应先核对来源，再决定怎样安排。", "Quiet"))
            if has_preparation_date(entity):
                layout.addWidget(make_button(preparation_label(entity), self.open_recurring), alignment=Qt.AlignmentFlag.AlignLeft)
                layout.addWidget(plain_label("提前准备只针对这个明确截止日期生成一次任务。", "Quiet"))
            else:
                layout.addWidget(plain_label("尚无可用于提前准备的明确日期。可先编辑说明与日期；原记录会保留。", "Quiet"))
        layout.addStretch()
        row = QHBoxLayout()
        self.edit_button = make_button("编辑笔记" if entity["type"] == "note" else "编辑说明与日期" if entity["type"] == "milestone" else "编辑", lambda: (self.accept(), on_edit(self.entity)) if on_edit else None, True)
        self.edit_button.setEnabled(on_edit is not None and not entity.get("archived"))
        row.addWidget(self.edit_button)
        if entity["type"] == "note" and on_codex: row.addWidget(make_button("与 Codex 整理", lambda: (self.accept(), on_codex(entity))))
        if entity["type"] == "task":
            self.delete_button = make_button("恢复任务" if entity.get("archived") else "删除任务", self.delete_task)
            self.delete_button.setObjectName("DeleteTask")
            row.addWidget(self.delete_button)
        row.addStretch()
        row.addWidget(make_button("关闭", self.accept))
        layout.addLayout(row)

    def mark_complete(self):
        if self.closed or self.completion_saving: return
        self.completion_saving = True; self.complete_button.setEnabled(False)
        current = (self.entity.get("completion_state") == "done" if self.entity.get("completion_state") is not None else self.entity.get("status") == "done")
        payload = {"target_id": self.entity["id"], "target_version": self.entity["version"], "business_date": self.completion_date, "result": "incomplete" if current else "done"}
        def saved(receipt):
            self.completion_saving = False
            if self.on_saved: self.on_saved(receipt)
            if not self.closed: self.accept()
        def failed(error):
            self.completion_saving = False
            if self.closed: return
            self.complete_button.setEnabled(True); self.completion_note.setText(error.get("message", str(error)) + " 请关闭后重新读取这项任务。")
        self.bridge.command("set_task_completion", payload, saved, failed, epoch=self.completion_epoch, expected_revision=self.completion_revision)

    def delete_task(self):
        if self.saving_plan or self.completion_saving:
            return
        dialog = DeleteTaskDialog(self.bridge, self.entity, self, self.on_saved)
        try:
            dialog.exec()
            previous_version = self.entity["version"]
            self.entity = dialog.entity
            if self.entity.get("archived") or self.entity["version"] != previous_version:
                self.accept()
        finally:
            dialog.deleteLater()

    def load_plan(self, *_):
        self.plan_generation += 1; generation = self.plan_generation
        self.plan_ready = False; self.join_plan_button.setEnabled(False); self.join_plan_button.setText("加入这天计划")
        day = self.plan_date.date().toString("yyyy-MM-dd")
        def loaded(result):
            if self.closed or generation != self.plan_generation: return
            self.plan_snapshot = result.get("plan"); self.plan_ready = True
            self.join_plan_button.setEnabled(not self.saving_plan)
            self.plan_note.setText("加入后保留已有安排；重复加入不会新增一项。" if self.plan_snapshot else "这一天还没有计划。加入时会建立按先后顺序的计划，不推定具体时间。")
        def failed(error):
            if not self.closed and generation == self.plan_generation:
                self.plan_note.setText(error.get("message", str(error))); self.join_plan_button.setEnabled(True); self.join_plan_button.setText("重新读取安排")
        self.bridge.query("daily_tasks", loaded, failed, date=day, limit=1)

    def join_plan(self):
        if self.saving_plan: return
        if not self.plan_ready: self.load_plan(); return
        day = self.plan_date.date().toString("yyyy-MM-dd")
        payload = {"date": day, "target_id": self.entity["id"], "plan_id": None, "plan_version": None}
        if self.plan_snapshot: payload.update(plan_id=self.plan_snapshot["id"], plan_version=self.plan_snapshot["version"])
        self.saving_plan = True; self.join_plan_button.setEnabled(False); self.plan_date.setEnabled(False)
        def saved(receipt):
            self.saving_plan = False
            if self.on_saved: self.on_saved(receipt)
            if self.closed: return
            self.plan_date.setEnabled(True); self.load_plan(); self.plan_note.setText("已加入 " + day + " 的计划，可在当天复盘中确认完成情况。")
        def failed(error):
            self.saving_plan = False
            if self.closed: return
            self.plan_date.setEnabled(True); self.plan_ready = False; self.join_plan_button.setEnabled(True); self.join_plan_button.setText("重新读取安排")
            self.plan_note.setText(error.get("message", str(error)) + " 输入仍保留，请读取最新计划后再加入。")
        self.bridge.command("add_to_plan", payload, saved, failed)

    def open_recurring(self):
        if not has_preparation_date(self.entity): return
        from .gui_recurring import RecurringDialog
        dialog = RecurringDialog(self.bridge, self.entity, self, self.on_saved)
        self.dialogs.append(dialog); dialog.open()


class WorkspacePage(QWidget):
    error = Signal(object)
    sidebar_collapsed_changed = Signal(bool)

    def __init__(self, bridge, parent=None, on_create=None, on_edit=None, on_changed=None, on_codex=None):
        super().__init__(parent)
        self.bridge, self.on_create, self.on_edit, self.on_changed, self.on_codex = bridge, on_create, on_edit, on_changed, on_codex
        self.type_map = {}
        self.setAcceptDrops(True)
        self.dialogs = set()
        self.current_entity = None
        self.files_offset, self.files_history = 0, []
        self.generation = 0
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.sidebar_collapsed = False
        self.expand_directory_button = make_button("展开目录", lambda: self.toggle_sidebar(False))
        self.expand_directory_button.hide(); layout.addWidget(self.expand_directory_button, alignment=Qt.AlignmentFlag.AlignTop)
        split = QSplitter()
        self.directory_splitter = split
        directory = QFrame()
        self.directory_panel = directory
        directory.setObjectName("DirectoryPanel")
        directory_layout = QVBoxLayout(directory)
        directory_layout.setContentsMargins(15, 17, 15, 14)
        top = QHBoxLayout()
        top.addWidget(plain_label("项目目录", "SectionHeading"), 1)
        refresh = make_button("↻", self.refresh)
        refresh.setToolTip("刷新目录")
        refresh.setFixedWidth(36)
        top.addWidget(refresh)
        collapse = make_button("收起", lambda: self.toggle_sidebar(True)); collapse.setToolTip("收起项目目录，保留当前选择"); top.addWidget(collapse)
        directory_layout.addLayout(top)
        self.tree = HierarchyTree(bridge)
        self.tree.object_selected.connect(self.open_object)
        self.tree.changed.connect(self.saved)
        self.tree.error.connect(self.show_error)
        directory_layout.addWidget(self.tree, 1)
        directory_layout.addWidget(plain_label("展开查看下级；拖动可调整归属。", "Quiet"))
        split.addWidget(directory)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(24, 8, 10, 20)
        self.content_layout.setSpacing(22)
        scroll.setWidget(self.content)
        split.addWidget(scroll)
        split.setSizes([270, 820])
        split.setStretchFactor(1, 1)
        layout.addWidget(split)
        self.empty()

    def set_sidebar_collapsed(self, collapsed):
        self.sidebar_collapsed = bool(collapsed)
        self.directory_panel.setVisible(not self.sidebar_collapsed)
        self.expand_directory_button.setVisible(self.sidebar_collapsed)

    def toggle_sidebar(self, collapsed):
        self.set_sidebar_collapsed(collapsed)
        self.sidebar_collapsed_changed.emit(self.sidebar_collapsed)

    def set_types(self, types):
        self.type_map = types
        self.tree.set_types(types)

    def show_error(self, error):
        message = error.get("message", str(error))
        self.error.emit({"message": message})

    def empty(self):
        clear_layout(self.content_layout)
        self.content_layout.addSpacing(60)
        self.content_layout.addWidget(plain_label("让每件事各有所属", "PageTitle"))
        self.content_layout.addWidget(plain_label("从左侧选择一个项目或课程，查看任务和资料。\n也可以先建立一个项目，之后再逐步整理。", "Quiet"))
        row = QHBoxLayout()
        row.addWidget(make_button("新建项目", lambda: self.on_create("project", None), True))
        row.addWidget(make_button("新建课程", lambda: self.on_create("course", None)))
        row.addStretch()
        self.content_layout.addLayout(row)
        self.content_layout.addStretch()

    def refresh(self):
        self.tree.refresh()
        if self.current_entity:
            self.load_workspace(self.current_entity["id"])

    def open_object(self, entity):
        if entity.get("type") in {"task", "note", "milestone", "topic"}:
            def loaded(result):
                dialog = TaskDetailDialog(self.bridge, result["entity"], self, self.on_edit, on_saved=self.saved, on_codex=self.discuss_note if self.on_codex else None)
                self.dialogs.add(dialog)
                dialog.finished.connect(lambda _: self.dialogs.discard(dialog))
                dialog.open()
            self.bridge.query("get", loaded, self.show_error, id=entity["id"])
            return
        if not self.current_entity or self.current_entity["id"] != entity["id"]:
            self.files_offset, self.files_history = 0, []
            self.completed_tasks_expanded=False
        self.current_entity = entity
        self.load_workspace(entity["id"])

    def load_workspace(self, identifier):
        self.generation += 1
        generation = self.generation
        self.bridge.query("object_workspace", lambda result: self.render(result) if generation == self.generation else None, self.show_error, id=identifier, files_offset=self.files_offset, separate_tasks=True)

    def section_header(self, title, action=None, label=None):
        row = QHBoxLayout()
        row.addWidget(plain_label(title, "SectionHeading"), 1)
        if action and label:
            row.addWidget(make_button(label, action))
        self.content_layout.addLayout(row)

    def render(self, result):
        entity = result["entity"]
        self.current_entity = entity
        clear_layout(self.content_layout)
        names = self.tree.breadcrumb(entity["id"])
        self.content_layout.addWidget(plain_label(" / ".join(names[:-1]) or "项目与课程", "Eyebrow"))
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.addWidget(plain_label(entity["title"], "PageTitle"))
        heading.addWidget(plain_label(self.type_map.get(entity["type"], {}).get("label", label_type(entity["type"])), "Quiet"))
        header.addLayout(heading, 1)
        edit_button = make_button("编辑", lambda: self.on_edit(entity))
        edit_button.setEnabled(not self.type_map.get(entity["type"], {}).get("read_only", False))
        header.addWidget(edit_button)
        if entity['type'] in {'domain','project','course','activity','phase','task','goal'}:
            from .gui_library import open_library
            folder=make_button('打开文件夹')
            folder.clicked.connect(lambda _,b=folder,i=entity['id']:open_library(self.bridge,self,b,i,self.show_error))
            header.addWidget(folder)
        if entity["type"] == "event" and has_preparation_date(entity): header.addWidget(make_button(preparation_label(entity), lambda: self.open_recurring(entity)))
        add = make_button("＋ 添加")
        menu = QMenu(add)
        kinds = ["task", "project", "activity", "goal", "note"]
        if entity["type"] in {"project", "phase"}:
            kinds += ["phase", "milestone"]
        if entity["type"] == "course":
            kinds += ["topic", "milestone"]
        if entity["type"] == "domain":
            kinds += ["domain", "course"]
        for kind in dict.fromkeys(kinds):
            definition = self.type_map.get(kind, {})
            if entity["type"] in definition.get("parent_types", []):
                menu.addAction("新建交付 / 检查点" if kind == "milestone" else definition.get("label", label_type(kind)), lambda checked=False, k=kind: self.on_create(k, entity))
        if entity["type"] in {"domain", "project", "course", "activity", "phase"} and "event" in self.type_map:
            menu.addAction("固定日程", lambda: self.on_create("event", entity))
        extensions = [(kind, definition) for kind, definition in self.type_map.items() if definition.get("module") not in (None, "builtin") and not definition.get("read_only") and entity["type"] in definition.get("parent_types", [])]
        if extensions:
            menu.addSeparator()
            for kind, definition in extensions:
                menu.addAction(definition.get("label", kind), lambda checked=False, k=kind: self.on_create(k, entity))
        if entity["type"] in {"domain", "project", "course", "activity", "phase", "task", "goal"}:
            menu.addSeparator()
            menu.addAction("添加资料或通知…", self.attach_file)
        add.setMenu(menu)
        header.addWidget(add)
        self.content_layout.addLayout(header)
        description = entity.get("data", {}).get("purpose") or entity.get("data", {}).get("notes") or entity.get("data", {}).get("description")
        if description:
            self.content_layout.addWidget(plain_label(str(description), "Body"))
        if self.type_map.get(entity["type"], {}).get("module") not in (None, "builtin"):
            self.section_header("记录内容")
            for field in self.type_map[entity["type"]].get("fields", []):
                value = entity.get("data", {}).get(field["id"])
                if value is not None:
                    row = QHBoxLayout()
                    row.addWidget(plain_label(field.get("label", field["id"]), "Quiet"))
                    row.addWidget(plain_label("、".join(str(item) for item in value) if isinstance(value, list) else str(value)), 1)
                    self.content_layout.addLayout(row)
        if result.get("summary", {}).get("total_tasks", 0) or entity["type"] in CONTAINERS:
            progress = CountProgress()
            progress.set_counts(result.get("summary", {}))
            self.progress = progress
            self.content_layout.addWidget(progress)
        children = result.get("children", [])
        folders = [child for child in children if child.get("type") in {"domain", "project", "course", "activity", "phase", "goal"} or self.type_map.get(child.get("type"), {}).get("module") not in (None, "builtin")]
        tasks = [child for child in children if child.get("type") == "task"]
        milestones = [child for child in children if child.get("type") == "milestone"]
        records = [child for child in children if child.get("type") in {"topic", "note"}]
        if folders:
            self.section_header("组成")
            grid = QGridLayout()
            grid.setSpacing(12)
            for index, child in enumerate(folders):
                card = QPushButton(f"{self.type_map.get(child['type'], {}).get('label', label_type(child['type']))}\n{child['title']}")
                card.setObjectName("FolderCard")
                card.setMinimumHeight(86)
                card.clicked.connect(lambda _, item=child: self.open_object(item))
                grid.addWidget(card, index // 2, index % 2)
            self.content_layout.addLayout(grid)
        if entity["type"] == "course":
            from .gui_recovery import RecoveryPanel
            self.recovery_panel = RecoveryPanel(self.bridge, entity, self, on_saved=self.saved)
            self.content_layout.addWidget(self.recovery_panel)
        can_add_task = entity["type"] in self.type_map.get("task", {}).get("parent_types", [])
        self.section_header("实际任务", (lambda: self.on_create("task", entity)) if can_add_task else None, "＋ 添加任务")
        self.content_layout.addWidget(plain_label("点击任务左侧圆圈即可标记完成；补课与补欠使用同一项任务，进度和完成记录会同步。", "Quiet"))
        from .gui_task_list import TaskListPanel
        self.task_panel=TaskListPanel(self.bridge,entity,self,on_open=self.open_object,on_edit=self.on_edit,
            initial=None if result.get('tasks_separated') else result,expanded=getattr(self,'completed_tasks_expanded',False),on_saved=self.saved)
        self.task_panel.expanded_changed.connect(lambda value:setattr(self,'completed_tasks_expanded',value))
        self.content_layout.addWidget(self.task_panel)
        if milestones or entity["type"] in {"course", "project"}:
            allowed_node = entity["type"] in self.type_map.get("milestone", {}).get("parent_types", [])
            self.section_header("交付与重要日期", (lambda: self.on_create("milestone", entity)) if allowed_node else None, "＋ 新建交付 / 检查点")
            self.content_layout.addWidget(plain_label("记录报告提交、考试或阶段检查点。未定日期的旧记录先保留并核对来源，不会自动生成准备任务。", "Quiet"))
            if not milestones: self.content_layout.addWidget(plain_label("尚未登记交付或检查点。长期要求可放在笔记，实际行动请建立任务。", "Quiet"))
            for milestone in milestones:
                row = QFrame(); row.setObjectName("FileRow"); inner = QHBoxLayout(row)
                words = QVBoxLayout(); title = make_button(milestone["title"], lambda _, item=milestone: self.open_object(item)); title.setObjectName("TextLink"); words.addWidget(title)
                data = milestone.get("data", {}); dated = has_preparation_date(milestone)
                words.addWidget(plain_label("截止 " + data["due_date"] if data.get("due_date") else "尚未定日期 · 请核对这项记录是交付要求还是一般说明", "Quiet"))
                source = str(data.get("source_text") or data.get("source") or "来源尚待核对")
                source_label = plain_label("来源：" + source[:240] + ("…" if len(source)>240 else ""), "Quiet"); source_label.setToolTip(source); words.addWidget(source_label)
                inner.addLayout(words, 1)
                actions = QVBoxLayout()
                if self.on_edit: actions.addWidget(make_button("编辑说明与日期", lambda _, item=milestone: self.on_edit(item)))
                if dated: actions.addWidget(make_button(preparation_label(milestone), lambda _, item=milestone: self.open_recurring(item)))
                inner.addLayout(actions); self.content_layout.addWidget(row)
        topics = [record for record in records if record["type"] == "topic"]
        if topics:
            self.section_header("知识点")
            for record in topics:self.content_layout.addWidget(make_button(record["title"], lambda _, item=record: self.open_object(item)))
        notes = [record for record in records if record["type"] == "note"]
        if notes or entity["type"] == "course":
            self.section_header("课程笔记" if entity["type"] == "course" else "笔记", (lambda: self.on_codex(entity, intent="course_notes")) if self.on_codex else None, "整理课件笔记")
            if not notes:self.content_layout.addWidget(plain_label("添加课件后，可在课程对话中说“这是课件，整理笔记”。整理结果核对后再保存。", "Quiet"))
            for note in notes:
                card = QFrame(); card.setObjectName("FileRow"); inner = QHBoxLayout(card); words = QVBoxLayout()
                title = make_button(note["title"], lambda _, item=note: self.open_object(item)); title.setObjectName("TextLink"); words.addWidget(title)
                excerpt_source = str(note.get("data", {}).get("summary") or note.get("data", {}).get("content") or "尚无正文")
                excerpt = " ".join(re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", excerpt_source).split())
                words.addWidget(plain_label(excerpt[:150] + ("…" if len(excerpt)>150 else ""), "Quiet")); inner.addLayout(words, 1)
                if self.on_edit:inner.addWidget(make_button("编辑笔记", lambda _, item=note: self.on_edit(item)))
                self.content_layout.addWidget(card)
        children_total = result.get("children_total")
        if result.get("next_offset") is not None:
            self.content_layout.addWidget(plain_label(f"本页展示 {len(children)} 项下级内容；其余内容可在左侧展开目录分页查看。", "Quiet"))
        if entity["type"] == "course":
            self.course_information(result.get("course_info", {}))
        can_attach = entity["type"] in {"domain", "project", "course", "activity", "phase", "task", "goal"}
        self.section_header("资料与通知", self.attach_file if can_attach else None, "＋ 添加资料或通知")
        files = result.get("files", [])
        if not files:
            self.content_layout.addWidget(plain_label("拖入课件，或添加通知、邮件、截图和网页。软件会保存副本，提取内容单独核对。", "Quiet"))
        for file in files:
            row = QFrame()
            row.setObjectName("FileRow")
            inner = QHBoxLayout(row)
            inner.setContentsMargins(14, 12, 14, 12)
            suffix = Path(file.get("title", "")).suffix.lstrip(".").upper() or "文件"
            badge = plain_label(suffix[:7], "FileBadge")
            badge.setFixedWidth(64)
            inner.addWidget(badge)
            name = QVBoxLayout()
            name.addWidget(plain_label(file.get("title", "资料")))
            data = file.get("data", {})
            missing = file.get("missing", False) or file.get("exists") is False
            is_reference = file.get("reference_only") or file.get("type") == "file_reference"
            name.addWidget(plain_label(("原文件位置需要核对" if missing else "旧本地引用 · 原文件移动后可能无法打开") if is_reference else source_origin(file) + " · " + extraction_label(file), "Quiet"))
            inner.addLayout(name, 1)
            if is_reference and data.get("local_path") and not missing:
                inner.addWidget(make_button("保存到软件", lambda _, path=data["local_path"]: self.attach_paths([path])))
            if data.get("source_kind"):
                inner.addWidget(make_button("查看提取", lambda _, item=file: self.view_source(item)))
            inner.addWidget(make_button("打开" if is_reference else "打开原文件", lambda _, identifier=file["id"]: self.open_file(identifier)))
            self.content_layout.addWidget(row)
        file_next = result.get("files_next_offset")
        if self.files_offset or file_next is not None:
            pages = QHBoxLayout()
            pages.addWidget(plain_label(f"当前显示第 {self.files_offset + 1} 至 {self.files_offset + len(files)} 个文件", "Quiet"), 1)
            previous = make_button("上一页文件", self.previous_files)
            previous.setEnabled(bool(self.files_history))
            pages.addWidget(previous)
            following = make_button("下一页文件", lambda: self.next_files(file_next))
            following.setEnabled(file_next is not None)
            pages.addWidget(following)
            self.content_layout.addLayout(pages)
        elif result.get("files_has_more"):
            self.content_layout.addWidget(plain_label("还有文件未显示，请稍后刷新以继续读取。", "Quiet"))
        self.content_layout.addSpacing(4)
        footer = QHBoxLayout()
        if self.on_codex:
            if entity["type"] == "course":
                footer.addWidget(make_button("整理课程信息", lambda: self.on_codex(entity, organize=True), True))
            footer.addWidget(make_button("与 Codex 讨论", lambda: self.on_codex(entity)))
        archive = make_button("归档这个" + self.type_map.get(entity["type"], {}).get("label", label_type(entity["type"])), lambda: self.archive(entity))
        archive.setObjectName("QuietButton")
        footer.addStretch()
        footer.addWidget(archive)
        self.content_layout.addLayout(footer)
        self.content_layout.addStretch()

    def next_files(self, offset):
        if offset is not None:
            self.files_history.append(self.files_offset)
            self.files_offset = offset
            self.load_workspace(self.current_entity["id"])

    def previous_files(self):
        if self.files_history:
            self.files_offset = self.files_history.pop()
            self.load_workspace(self.current_entity["id"])

    def course_information(self, info):
        for key, title in [("assessments", "评分规则"), ("events", "固定与每周安排")]:
            self.section_header(title)
            if key == "assessments": self.content_layout.addWidget(plain_label("评分规则说明课程如何计分，不是待完成任务。实际备考或项目准备请放在上方「实际任务」。", "Quiet"))
            rows = info.get(key, [])
            if not rows:
                self.content_layout.addWidget(plain_label("尚未登记。添加课程资料后，可点击「整理课程信息」核对提取建议。", "Quiet"))
            for item in rows:
                data = item.get("data", {})
                row = QFrame(); row.setObjectName("FileRow"); inner = QHBoxLayout(row)
                words = QVBoxLayout(); words.addWidget(plain_label(item.get("title", title), "CardTitle"))
                if key == "assessments":
                    details = ["权重 " + str(data["weight"]) + "%" if data.get("weight") is not None else "权重待确认"]
                    if data.get("due_date"): details.append("截止 " + data["due_date"])
                else:
                    details = [data.get("date") or "日期待确认"]
                    if data.get("start"): details.append(data["start"] + (" — " + data["end"] if data.get("end") else ""))
                    if data.get("recurrence") not in (None, "none"): details.append(label_status(data["recurrence"]))
                    if data.get("until"): details.append("至 " + data["until"])
                words.addWidget(plain_label(" · ".join(details), "Quiet"))
                source = data.get("source_text") or data.get("source")
                words.addWidget(plain_label("来源：" + str(source) if source else "来源待确认", "Quiet"))
                inner.addLayout(words, 1)
                actions = QVBoxLayout()
                if self.on_edit: actions.addWidget(make_button("编辑日程" if key == "events" else "编辑评分", lambda _, e=item: self.on_edit(e)))
                if key == "events":
                    if has_preparation_date(item): actions.addWidget(make_button(preparation_label(item), lambda _, e=item: self.open_recurring(e)))
                    else: words.addWidget(plain_label("先确认日期，再设置提前准备。", "Quiet"))
                inner.addLayout(actions)
                self.content_layout.addWidget(row)
            total = info.get(key + "_total", len(rows))
            if total > len(rows): self.content_layout.addWidget(plain_label(f"已显示 {len(rows)} / {total} 项，其余可通过课程讨论按需读取。", "Quiet"))

    def discuss_note(self, note):
        if not self.on_codex: return
        visited = {note["id"]}
        def owner(record):
            if record.get("type") in {"course", "project", "activity", "domain"} or not record.get("parent_id"):
                self.on_codex(record, intent="course_notes"); return
            parent_id = record["parent_id"]
            if parent_id in visited or len(visited) >= 20:
                self.show_error({"message": "笔记归属需要核对，请从相应课程打开整理入口。"}); return
            visited.add(parent_id)
            self.bridge.query("get", lambda result: owner(result["entity"]), self.show_error, id=parent_id)
        owner(note)

    def open_recurring(self, anchor):
        if not has_preparation_date(anchor):
            self.show_error({"message": "请先编辑这项记录，确认明确日期后再设置提前准备。"}); return
        from .gui_recurring import RecurringDialog
        dialog = RecurringDialog(self.bridge, anchor, self, self.saved)
        self.dialogs.add(dialog); dialog.finished.connect(lambda _: self.dialogs.discard(dialog)); dialog.open()

    def view_source(self, entity):
        dialog = SourceContentDialog(self.bridge, entity, self)
        self.dialogs.add(dialog); dialog.finished.connect(lambda _: self.dialogs.discard(dialog)); dialog.open()

    def attach_file(self):
        if not self.current_entity:
            return
        dialog = AddSourceDialog(self.bridge, self.current_entity["id"], self, self.saved)
        self.dialogs.add(dialog); dialog.finished.connect(lambda _: self.dialogs.discard(dialog)); dialog.open()

    def dragEnterEvent(self, event):
        if self.current_entity and event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not self.current_entity or not event.mimeData().hasUrls():
            event.ignore()
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.attach_paths(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    def attach_paths(self, paths):
        if not self.current_entity:
            return
        owner_id = self.current_entity["id"]
        paths = list(dict.fromkeys(str(Path(path).resolve()) for path in paths))
        if len(paths) > 50:
            self.show_error({"message": "一次最多加入 50 个文件，请分批选择。"})
            return
        receipts, failures = [], []
        def next_file(index=0):
            if index >= len(paths):
                self.saved(receipts[-1] if receipts else None)
                if failures:
                    self.show_error({"message": "部分文件没有加入：" + "；".join(failures[:3])})
                return
            path = paths[index]
            def saved(receipt):
                receipts.append(receipt)
                next_file(index + 1)
            def failed(error):
                failures.append(Path(path).name + "：" + error.get("message", str(error)))
                next_file(index + 1)
            self.bridge.command("add_source", {"kind": file_kind(path), "path": path, "owner_id": owner_id, "title": Path(path).name}, saved, failed)
        next_file()

    def open_file(self, identifier):
        def loaded(result):
            if not result.get("exists", False):
                self.show_error({"message": "原文件目前不在已记录的位置。请先确认文件是否移动，或重新选择文件。"})
                return
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(result["path"])):
                self.show_error({"message": "系统暂时无法打开这个文件，请检查是否安装了相应程序。"})
        self.bridge.query("open_resource", loaded, self.show_error, id=identifier)

    def archive(self, entity):
        self.bridge.command("archive", {"id": entity["id"], "version": entity["version"], "archived": True}, self.saved, self.show_error)

    def saved(self, receipt=None):
        self.refresh()
        if self.on_changed:
            self.on_changed(receipt)
