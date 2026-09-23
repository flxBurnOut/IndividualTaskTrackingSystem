"""Three focused native workspaces sharing one durable business service."""
from __future__ import annotations
from pathlib import Path
import sys
from PySide6.QtCore import QDate, Qt, QTimer, QThread
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QStackedWidget, QStatusBar, QVBoxLayout, QWidget,
)
from .gui_async import ServiceBridge
from .gui_gc import install_gui_gc
from .gui_forms import EntityForm
from .gui_workflows import AssistanceDialog, PlanDialog, SettingsDialog
from .gui_today import TodayPage
from .gui_workspace import BASE_TYPES, TREE_TYPES, WorkspacePage, TaskDetailDialog, make_button, plain_label
from .gui_review import ReviewPage
from .gui_calendar import install_calendar

NAVIGATION = [("today", "今天"), ("projects", "项目与课程"), ("reviews", "复盘")]
from .appearance import normalize_appearance
from .gui_theme import apply_appearance, current_appearance, stylesheet, bind_theme

# Kept as an import-compatible light default for embedders and tests.
STYLESHEET = stylesheet()


def configure_palette(app):
    apply_appearance(app, current_appearance())


class SearchDialog(QDialog):
    def __init__(self, bridge, parent, on_selected):
        super().__init__(parent)
        self.bridge, self.on_selected, self.generation = bridge, on_selected, 0
        self.visible_types = list(TREE_TYPES) + [key for key, definition in getattr(parent, "type_map", {}).items() if definition.get("module") not in (None, "builtin")]
        self.setWindowTitle("查找事项")
        self.resize(620, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(23, 20, 23, 20)
        layout.addWidget(plain_label("查找事项", "DialogHeading"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("输入任务、项目或课程名称")
        layout.addWidget(self.search)
        self.results = QListWidget()
        self.results.itemDoubleClicked.connect(self.choose)
        layout.addWidget(self.results, 1)
        self.hint = plain_label("输入名称后开始查找。", "Quiet")
        layout.addWidget(self.hint)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.load)
        self.search.textChanged.connect(self.search_changed)
        self.search.returnPressed.connect(self.choose)

    def search_changed(self, *_):
        self.generation += 1
        self.timer.start()

    def load(self):
        self.generation += 1
        generation = self.generation
        text = self.search.text().strip()
        if not text:
            self.results.clear()
            self.hint.setText("输入名称后开始查找。")
            return
        def loaded(result):
            if generation != self.generation:
                return
            self.results.clear()
            for entity in result.get("items", []):
                item = QListWidgetItem(entity["title"])
                item.setData(Qt.ItemDataRole.UserRole, entity)
                self.results.addItem(item)
            self.hint.setText(f"找到 {result.get('total', 0)} 项" + ("，请补充关键词缩小范围。" if result.get("next_offset") is not None else ""))
        self.bridge.query("list", loaded, lambda e: self.hint.setText(e.get("message", str(e))), types=self.visible_types, search=text, limit=30)

    def choose(self, *_):
        item = self.results.currentItem()
        if item:
            entity = item.data(Qt.ItemDataRole.UserRole)
            self.accept()
            self.on_selected(entity)


class MainWindow(QMainWindow):
    def __init__(self, data_dir, client_factory=None):
        install_gui_gc()
        super().__init__()
        configure_palette(QApplication.instance())
        self.data_dir = Path(data_dir)
        from . import __version__
        self.setWindowTitle(f"个人事务管理 · {__version__}")
        self.resize(1290, 850)
        self.setMinimumSize(1050, 720)
        self.bridge = ServiceBridge(data_dir, self, client_factory=client_factory)
        self.bridge.failed.connect(self.show_error)
        self.bridge.activity.connect(self.activity_changed)
        self.type_map, self.capabilities = {}, {"types": []}
        self.section = "today"
        self.change_cursor = 0
        self.poll_pending = self.closed = self.close_requested = False
        self.review_pending = False
        self.review_attention = False
        self.dialogs = set()
        self._display_generation = 0
        self._display_epoch = self._display_revision = None
        self._saved_appearance = normalize_appearance()
        self._sidebar_pending = False
        self._sidebar_desired = False
        self._build()
        if sys.platform == 'darwin':
            self._build_mac_menu()
        bind_theme(self, self.refresh_global_metrics)
        self.poll = QTimer(self)
        self.poll.setInterval(5000)
        self.poll.timeout.connect(self.poll_changes)
        self.load_capabilities()
        self.poll.start()

    def _build_mac_menu(self):
        menu = self.menuBar().addMenu('个人事务管理')
        for label, shortcut, callback, role in (
            ('设置…', 'Ctrl+,', self.open_settings, QAction.MenuRole.PreferencesRole),
            ('搜索…', QKeySequence.StandardKey.Find, self.search, QAction.MenuRole.NoRole),
            ('关闭窗口', QKeySequence.StandardKey.Close, self.close, QAction.MenuRole.NoRole),
            ('退出个人事务管理', QKeySequence.StandardKey.Quit, self.close, QAction.MenuRole.QuitRole),
        ):
            action = QAction(label, self)
            action.setMenuRole(role)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)
            menu.addAction(action)

    def _build(self):
        central = QWidget()
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = self.navigation_sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(182)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(15, 25, 15, 19)
        side.setSpacing(8)
        side.addWidget(plain_label("个人事务", "Brand"))
        side.addWidget(plain_label("安排与回顾", "BrandSub"))
        self.nav_buttons = {}
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for key, label in NAVIGATION:
            widget = make_button(label, lambda _, k=key: self.navigate(k))
            widget.setObjectName("Nav")
            widget.setCheckable(True)
            self.nav_group.addButton(widget)
            self.nav_buttons[key] = widget
            side.addWidget(widget)
        side.addStretch()
        self.new_button = make_button("＋ 新建", primary=True)
        self.create_menu = QMenu(self.new_button)
        self.new_button.setMenu(self.create_menu)
        side.addWidget(self.new_button)
        side.addWidget(plain_label("本地保存 · 同一份记录", "SidebarHint"))
        outer.addWidget(sidebar)
        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(31, 26, 30, 17)
        main_layout.setSpacing(20)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(4)
        self.page_title = plain_label("今天", "PageTitle")
        self.page_subtitle = plain_label("先看当天的安排，再开始行动。", "PageSubtitle")
        heading.addWidget(self.page_title)
        heading.addWidget(self.page_subtitle)
        header.addLayout(heading, 1)
        self.search_button = make_button("搜索", self.search)
        self.search_button.setObjectName("QuietButton")
        self.settings_button = make_button("设置", self.open_settings)
        self.settings_button.setObjectName("QuietButton")
        header.addWidget(self.search_button)
        header.addWidget(self.settings_button)
        main_layout.addLayout(header)
        self.notice = plain_label("", "Notice")
        self.notice.hide()
        main_layout.addWidget(self.notice)
        self.pages = QStackedWidget()
        self.today_page = TodayPage(self.bridge, self, on_plan=self.plan_with_codex, on_manual=self.manual_plan, on_review=self.open_review, on_task=self.open_task, on_changed=self.saved, on_timetable=self.open_timetable)
        install_calendar(self.today_page.date)
        self.today_page.error.connect(self.show_error)
        self.today_page.review_attention.connect(self.review_attention_changed)
        self.workspace_page = WorkspacePage(self.bridge, self, on_create=self.create_entity, on_edit=self.edit_entity, on_changed=self.workspace_changed, on_codex=self.workspace_assistance)
        self.workspace_page.error.connect(self.show_error)
        if hasattr(self.workspace_page, 'sidebar_collapsed_changed'):
            self.workspace_page.sidebar_collapsed_changed.connect(self.save_workspace_sidebar)
        self.review_page = ReviewPage(self.bridge, self, on_changed=self.saved, on_codex=self.review_handoff)
        self.review_page.has_pending.connect(self.pending_review_changed)
        self.pages.addWidget(self.today_page)
        self.pages.addWidget(self.workspace_page)
        self.pages.addWidget(self.review_page)
        main_layout.addWidget(self.pages, 1)
        outer.addWidget(main, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.connection_label = QLabel("正在连接本地记录…")
        self.activity_label = QLabel()
        self.statusBar().addWidget(self.connection_label, 1)
        self.statusBar().addPermanentWidget(self.activity_label)
        self.nav_buttons["today"].setChecked(True)

    def refresh_global_metrics(self):
        brand = self.navigation_sidebar.findChild(QLabel, 'Brand')
        widths = [max(182, round(182 * current_appearance()['font_size'] / 13)), *(button.fontMetrics().horizontalAdvance(button.text()) + 60 for button in self.nav_buttons.values())]
        if brand is not None:
            widths.append(brand.fontMetrics().horizontalAdvance(brand.text()) + 46)
        self.navigation_sidebar.setFixedWidth(max(widths))

    def activity_changed(self, busy):
        self.activity_label.setText("处理中…" if busy else "")
        if not busy and self.close_requested:
            QTimer.singleShot(0, self.close)

    def show_error(self, error):
        self.notice.setText(error.get("message", str(error)))
        self.notice.show()

    def load_capabilities(self):
        def loaded(result):
            self.capabilities = result
            values = result.get("types", [])
            self.type_map = {value["id"]: value for value in values} if isinstance(values, list) else values
            self.create_menu.clear()
            for kind in BASE_TYPES:
                definition = self.type_map.get(kind)
                if definition and not definition.get("read_only"):
                    self.create_menu.addAction(definition.get("label", kind), lambda checked=False, k=kind: self.create_entity(k))
            self.workspace_page.set_types(self.type_map)
            self.load_display_preferences()
            self.connection_label.setText("本地记录已连接")
            self.refresh()
        self.bridge.query("capabilities", loaded, self.show_error)

    def _apply_display_preferences(self, settings, epoch=None, revision=None):
        if epoch == self._display_epoch and revision is not None and self._display_revision is not None and revision < self._display_revision:
            return
        self._display_epoch, self._display_revision = epoch, revision
        self._saved_appearance = normalize_appearance(settings.get('appearance'))
        apply_appearance(QApplication.instance(), self._saved_appearance)
        self.review_page.set_weekly_style(settings.get('charts', {}).get('weekly_style', 'columns'))
        if not self._sidebar_pending:
            self._sidebar_desired = self._saved_appearance['workspace_sidebar_collapsed']
            if hasattr(self.workspace_page, 'set_sidebar_collapsed'):
                self.workspace_page.set_sidebar_collapsed(self._sidebar_desired)

    def load_display_preferences(self):
        self._display_generation += 1
        generation = self._display_generation
        def loaded(result):
            if self.closed or generation != self._display_generation:
                return
            self._apply_display_preferences(result.get('settings', {}), result.get('epoch'), result.get('revision'))
        self.bridge.query('settings', loaded, self.show_error)

    def save_workspace_sidebar(self, collapsed):
        self._sidebar_desired = bool(collapsed)
        if self._sidebar_pending:
            return
        wanted = self._sidebar_desired
        self._sidebar_pending = True
        def saved(receipt):
            self._display_generation += 1
            settings = receipt.get('result', {}).get('settings', {})
            self._apply_display_preferences(settings, receipt.get('epoch'), receipt.get('revision'))
            self._sidebar_pending = False
            if self._sidebar_desired != wanted:
                self.save_workspace_sidebar(self._sidebar_desired)
            elif hasattr(self.workspace_page, 'set_sidebar_collapsed'):
                self.workspace_page.set_sidebar_collapsed(wanted)
        def failed(error):
            self._sidebar_pending = False
            self._sidebar_desired = self._saved_appearance['workspace_sidebar_collapsed']
            if hasattr(self.workspace_page, 'set_sidebar_collapsed'):
                self.workspace_page.set_sidebar_collapsed(self._sidebar_desired)
            self.show_error(error)
        self.bridge.command('settings', {'settings': {'appearance': {'workspace_sidebar_collapsed': wanted}}}, saved, failed)

    def navigate(self, section):
        if section not in dict(NAVIGATION):
            return
        self.section = section
        self.nav_buttons[section].setChecked(True)
        self.pages.setCurrentIndex({"today": 0, "projects": 1, "reviews": 2}[section])
        titles = {
            "today": ("今天", "先看当天的安排，再开始行动。"),
            "projects": ("项目与课程", "沿着归属浏览，任务和文件都放在需要它们的地方。"),
            "reviews": ("复盘", "按实际情况确认完成，再看一周的变化。"),
        }
        self.page_title.setText(titles[section][0])
        self.page_subtitle.setText(titles[section][1])
        self.notice.hide()
        self.refresh()

    def refresh(self):
        if not self.type_map or self.closed:
            return
        if self.section == "today":
            self.today_page.refresh()
        elif self.section == "projects":
            self.workspace_page.refresh()
        else:
            self.review_page.refresh()

    def restricted_capabilities(self, kinds):
        return {**self.capabilities, "types": [self.type_map[kind] for kind in kinds if kind in self.type_map]}

    def create_entity(self, kind="task", parent_entity=None):
        if kind not in self.type_map:
            self.show_error({"message": "此功能尚未连接，请稍后再试。"})
            return
        initial = {"data": {"owner_id": parent_entity["id"]}} if kind == "event" and parent_entity else None
        EntityForm(self.bridge, self.restricted_capabilities([kind]), self, default_type=kind, parent_entity=None if kind == "event" else parent_entity, initial_payload=initial, on_saved=self.saved).exec()

    def edit_entity(self, entity):
        EntityForm(self.bridge, self.restricted_capabilities([entity["type"]]), self, entity=entity, on_saved=self.saved).exec()

    def saved(self, receipt=None):
        self.notice.hide()
        self.statusBar().showMessage("记录已更新。", 4500)
        summary = (receipt or {}).get("result", {}).get("summary")
        if self.section == "reviews" and summary:
            self.review_attention = summary.get("unreported", 0) > 0
            self.update_review_indicator()
        self.refresh()

    def workspace_changed(self, receipt=None):
        self.notice.hide()
        self.statusBar().showMessage("整理已保存。", 4500)

    def open_task(self, identifier):
        def loaded(result):
            dialog = TaskDetailDialog(self.bridge, result["entity"], self, self.edit_entity, on_saved=self.saved, business_date=self.today_page.date_iso() if self.section == "today" else None)
            self.dialogs.add(dialog)
            dialog.finished.connect(lambda _: self.dialogs.discard(dialog))
            dialog.open()
        self.bridge.query("get", loaded, self.show_error, id=identifier)

    def open_review(self, date, mode="daily"):
        self.section = "reviews"
        self.nav_buttons["reviews"].setChecked(True)
        self.pages.setCurrentIndex(2)
        self.page_title.setText("复盘")
        self.page_subtitle.setText("按实际情况确认完成，再看一周的变化。")
        self.review_page.set_date(date)
        self.review_page.tabs.setCurrentIndex(1 if mode == "weekly" else 0)

    def pending_review_changed(self, pending):
        self.review_pending = bool(pending)
        self.update_review_indicator()

    def review_attention_changed(self, pending):
        self.review_attention = bool(pending)
        self.update_review_indicator()

    def update_review_indicator(self):
        pending = self.review_pending or self.review_attention
        self.nav_buttons["reviews"].setText("复盘  ·" if pending else "复盘")
        self.nav_buttons["reviews"].setToolTip("有尚未确认保存的选择" if self.review_pending else "当天仍有待复盘事项或缺少计划" if self.review_attention else "查看每日复盘与每周回顾")

    def plan_with_codex(self, date, has_plan):
        verb = "调整" if has_plan else "生成"
        prompt = f"请为 {date} {verb}每日计划。先核对固定安排、可用时间和待确认事项；保留休息与缓冲，不把估计用时当成完成标准。需要补充的信息请明确列出。"
        self.open_assistance(prompt=prompt, date=date, scope={"kind":"daily_plan","date":date})

    def open_timetable(self, day=None):
        from .gui_timetable import TimetableDialog
        dialog = TimetableDialog(self.bridge, self, on_saved=self.saved, business_date=day)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def manual_plan(self, date):
        PlanDialog(self.bridge, self, self.saved, date=QDate.fromString(date, "yyyy-MM-dd")).exec()

    def review_handoff(self, prompt):
        import re
        match=re.search(r'\b\d{4}-\d{2}-\d{2}\b',prompt)
        day=match.group(0) if match else QDate.currentDate().toString('yyyy-MM-dd')
        kind='weekly_review' if self.review_page.tabs.currentIndex()==1 else 'daily_review'
        self.open_assistance(prompt=prompt, date=day, scope={'kind':kind,'date':day})

    def workspace_assistance(self, entity, organize=False, intent=None):
        scope={'kind':'course' if entity['type']=='course' else 'object','entity_id':entity['id']}
        if not organize and intent != 'course_notes':
            self.open_assistance(context_entities=[entity],scope=scope)
            return
        prompt=(f"请整理“{entity['title']}”的当前资料。提取原文已写明的课程介绍、评分组成及百分权重、里程碑、固定日程、每周安排。"
                "列出依据的资料和页码，已有相同信息不要重复创建；变更请更新原记录。评分权重不是成绩。"
                "周次转日期必须有明确学期锚点；日期、时间和规则缺失时向我提问，不要猜。每条候选保留原文来源，先给我核对。")
        if intent == 'course_notes':
            prompt = f"这是“{entity['title']}”的课件，请整理成可复习的课程笔记。说明来源和未读部分，先给我核对。"
        def loaded(result):
            if result.get('total',0)>12:
                self.open_assistance(prompt=prompt+' 当前资料较多，请先选择这次需要整理的附件。',context_entities=[entity],scope=scope,source_ids=[],intent=intent)
                return
            identifiers=[item['id'] for item in result.get('items',[]) if item['type'] in {'asset','artifact'}]
            if not identifiers:
                self.show_error({'message':'先在这个课程中添加课件、通知、邮件或截图，再整理课程信息。'})
                return
            self.open_assistance(prompt=prompt,context_entities=[entity],scope=scope,source_ids=identifiers,auto_send=True,intent=intent)
        self.bridge.query('sources',loaded,self.show_error,owner_id=entity['id'],limit=13)

    def open_assistance(self, prompt='', date=None, context_entities=None, intent=None, scope=None, source_ids=None, auto_send=False):
        dialog=AssistanceDialog(self.bridge,self,prompt=prompt,on_saved=self.saved,
            context_entities=context_entities or [],scope=scope or {'kind':'general'},
            business_date=date,source_ids=source_ids,auto_send=auto_send,intent=intent,
            on_open_settings=lambda:self.open_settings(page='Codex 协助'))
        dialog.exec()

    def open_settings(self, *_args, page=None, timetable_id=None):
        dialog=SettingsDialog(self.bridge,self.capabilities,self.data_dir,self,self.load_capabilities)
        if timetable_id:dialog.timetable_settings.select_timetable(timetable_id)
        if page:
            for index in range(dialog.tabs.count()):
                if dialog.tabs.tabText(index)==page:dialog.tabs.setCurrentIndex(index)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def search(self):
        def selected(entity):
            if entity["type"] in {"task", "note", "milestone", "topic"}:
                self.open_task(entity["id"])
            else:
                self.navigate("projects")
                self.workspace_page.open_object(entity)
        SearchDialog(self.bridge, self, selected).exec()

    def poll_changes(self):
        if self.closed or self.poll_pending or not self.type_map:
            return
        self.poll_pending = True
        def loaded(result):
            self.poll_pending = False
            self.change_cursor = result.get("cursor", self.change_cursor)
            if result.get("items") or result.get("reset_required"):
                if result.get("reset_required") or any(item.get("action")=="settings" for item in result.get("items",[])):
                    self.load_display_preferences()
                self.refresh()
        def failed(error):
            self.poll_pending = False
            self.show_error(error)
        self.bridge.query("changes", loaded, failed, after=self.change_cursor, epoch=self.bridge.epoch, limit=100)

    def closeEvent(self, event):
        if self.review_pending and not self.close_requested:
            choice = QMessageBox.question(self, "仍有未确认的复盘选择", "本次选择尚未保存。要关闭窗口并放弃这些选择吗？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if choice != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.review_pending = False
        self.closed = True
        self.poll.stop()
        if self.bridge.callbacks:
            self.close_requested = True
            self.notice.setText("等待本次读取或保存结束后自动关闭。")
            self.notice.show()
            event.ignore()
            return
        if not self.bridge.close(timeout_ms=0):
            self.close_requested = True
            self.notice.setText("正在等待后台连接退出，然后自动关闭。")
            self.notice.show()
            QTimer.singleShot(100, self.close)
            event.ignore()
            return
        event.accept()


def run(data_dir, argv=None):
    app = QApplication.instance() or QApplication(argv or [])
    manager = install_gui_gc(app)
    app.setApplicationName("个人事务管理")
    app.setOrganizationName("PersonalManagement")
    app.setStyle("Fusion")
    configure_palette(app)
    window = MainWindow(data_dir)
    window.show()
    result = app.exec()
    # Direct application.exit() can bypass Quit events. Keep Qt objects alive
    # and continue owner-thread collection until bounded client calls finish.
    while not manager.shutdown():
        app.processEvents()
        manager.poll()
        QThread.msleep(10)
    return result
