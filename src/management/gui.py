"""Three focused native workspaces sharing one durable business service."""
from __future__ import annotations
from pathlib import Path
import datetime as dt
from zoneinfo import ZoneInfo
from PySide6.QtCore import QDate, QSize, Qt, QTimer, QThread
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QStackedWidget, QStatusBar, QVBoxLayout, QWidget,
)
from .gui_async import ServiceBridge
from .gui_gc import install_gui_gc
from .gui_codex_connection import CodexConnectionController
from .gui_forms import EntityForm
from .gui_workflows import AssistanceDialog, PlanDialog, SettingsDialog
from .gui_today import TodayPage
from .gui_tasks import TasksPage
from .gui_dashboard import DashboardPage
from .gui_workspace import BASE_TYPES, TREE_TYPES, WorkspacePage, TaskDetailDialog, make_button, plain_label
from .gui_review import ReviewPage
from .gui_calendar import install_calendar
from .branding import APP_NAME

NAVIGATION = [("dashboard", "总览"), ("today", "今天"), ("tasks", "任务"), ("projects", "项目与课程"), ("reviews", "复盘")]
from .appearance import normalize_appearance, assistants_visible
from .gui_theme import apply_appearance, current_appearance, stylesheet, bind_theme
from .gui_materials import AppCanvas, GlassPanel, NavigationButton, PageTransition
from .gui_icons import symbol_icon

# Kept as an import-compatible light default for embedders and tests.
STYLESHEET = stylesheet()


def clock_snapshot(timezone=None):
    instant=dt.datetime.now(dt.timezone.utc)
    local_day=instant.astimezone(ZoneInfo(timezone)).date() if timezone else instant.astimezone().date()
    return QDate(local_day.year,local_day.month,local_day.day),int(instant.timestamp())//60


def configure_palette(app):
    apply_appearance(app, current_appearance())


class SearchDialog(QDialog):
    def __init__(self, bridge, parent, on_selected, *, initial_text=''):
        super().__init__(parent)
        self.bridge, self.on_selected, self.generation = bridge, on_selected, 0
        self.offset, self.next_offset = 0, None
        self.page_history = []
        self.loading, self._closed = False, False
        self._loaded_text = None
        self.visible_types = list(TREE_TYPES) + [key for key, definition in getattr(parent, "type_map", {}).items() if definition.get("module") not in (None, "builtin")]
        self.setWindowTitle("查找事项")
        self.resize(620, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(23, 20, 23, 20)
        layout.addWidget(plain_label("查找事项", "DialogHeading"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("输入任务、项目或课程名称")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.results = QListWidget()
        self.results.itemActivated.connect(self.choose)
        layout.addWidget(self.results, 1)
        self.hint = plain_label("输入名称后开始查找。", "Quiet")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        actions = QHBoxLayout()
        self.previous = make_button('上一页', self.previous_page)
        self.next = make_button('下一页', lambda: self.set_page(self.next_offset))
        self.open_button = make_button('打开所选事项', self.choose, True)
        self.cancel_button = make_button('关闭', self.reject)
        for button in (self.previous, self.next, self.open_button, self.cancel_button):
            button.setAutoDefault(False)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.results.currentItemChanged.connect(self.update_actions)
        self.update_actions()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.load)
        self.search.textChanged.connect(self.search_changed)
        self.search.returnPressed.connect(self.search_or_open)
        if initial_text:
            self.search.setText(initial_text)
            self.load()

    def search_changed(self, *_):
        self.generation += 1
        self.offset, self.next_offset, self._loaded_text = 0, None, None
        self.page_history.clear()
        self.results.clear()
        self.hint.setText('正在查找…' if self.search.text().strip() else '输入名称后开始查找。')
        self.update_actions()
        self.timer.start()

    def update_actions(self, *_):
        available = not self.loading and not self._closed
        self.previous.setEnabled(available and bool(self.page_history))
        self.next.setEnabled(available and self.next_offset is not None)
        self.open_button.setEnabled(available and self.results.currentItem() is not None)

    def set_page(self, offset):
        if offset is not None and not self.loading:
            if offset > self.offset:
                self.page_history.append(self.offset)
            self.offset = offset
            self.load()

    def previous_page(self):
        if self.page_history and not self.loading:
            self.offset = self.page_history.pop()
            self.load()

    def search_or_open(self):
        if self.timer.isActive() or self._loaded_text != self.search.text().strip():
            self.load()
        elif not self.loading:
            self.choose()

    def load(self):
        if self._closed:
            return
        self.timer.stop()
        self.generation += 1
        generation = self.generation
        text = self.search.text().strip()
        self._loaded_text = None
        self.results.clear()
        self.next_offset = None
        self.loading = bool(text)
        self.update_actions()
        if not text:
            self.hint.setText("输入名称后开始查找。")
            return
        self.hint.setText('正在查找…')
        def loaded(result):
            if self._closed or generation != self.generation:
                return
            self.loading, self._loaded_text = False, text
            self.next_offset = result.get('next_offset')
            self.results.clear()
            for entity in result.get("items", []):
                item = QListWidgetItem(entity["title"])
                item.setData(Qt.ItemDataRole.UserRole, entity)
                self.results.addItem(item)
            if self.results.count():
                self.results.setCurrentRow(0)
                self.hint.setText(f"找到 {result.get('total', 0)} 项 · 当前第 {self.offset + 1}–{self.offset + self.results.count()} 项。选中后点击打开，或按 Enter。")
            else:
                self.hint.setText('没有找到匹配事项，请换一个关键词。')
            self.update_actions()
        def failed(error):
            if self._closed or generation != self.generation:
                return
            self.loading = False
            self.hint.setText(error.get('message', str(error)))
            self.update_actions()
        self.bridge.query("list", loaded, failed, types=self.visible_types, search=text, limit=30, offset=self.offset)

    def choose(self, *_):
        if self._closed or self.loading or self._loaded_text != self.search.text().strip():
            return
        item = self.results.currentItem()
        if item:
            entity = item.data(Qt.ItemDataRole.UserRole)
            self.accept()
            self.on_selected(entity)

    def done(self, result):
        self._closed = True
        self.generation += 1
        self.timer.stop()
        super().done(result)


class MainWindow(QMainWindow):
    def __init__(self, data_dir, client_factory=None, desktop_step=None):
        install_gui_gc()
        super().__init__()
        from .branding import configure_application
        configure_application(QApplication.instance())
        configure_palette(QApplication.instance())
        self.data_dir = Path(data_dir)
        from . import __version__
        self.setWindowTitle(f"{APP_NAME} · {__version__}")
        self.resize(1290, 850)
        self.setMinimumSize(1050, 720)
        self.bridge = ServiceBridge(data_dir, self, client_factory=client_factory)
        self.bridge.failed.connect(self.show_error)
        self.bridge.activity.connect(self.activity_changed)
        self.type_map, self.capabilities = {}, {"types": []}
        self.section = "dashboard"
        self.change_cursor = 0
        self.poll_pending = self.closed = self.close_requested = False
        self.review_pending = False
        self._business_timezone = None
        self._calendar_day, self._clock_minute = clock_snapshot()
        self.review_attention = False
        self.dialogs = set()
        self.search_dialog = None
        self._display_generation = 0
        self._onboarding_display_loaded = False
        self._display_epoch = self._display_revision = None
        self._saved_appearance = normalize_appearance()
        self._sidebar_pending = False
        self._sidebar_desired = False
        self._assistants_visible = False
        self._assistant_activity_pending = False
        self._assistant_work_pending = False
        self.codex_connection = CodexConnectionController(data_dir, self, bridge=self.bridge,
            step=desktop_step, automatic=client_factory is None)
        self._build()
        from .gui_onboarding import OnboardingManager
        from .gui_tutorials import install_main_tutorials
        self.onboarding = OnboardingManager(self, self.data_dir)
        self.onboarding.error.connect(lambda message: self.statusBar().showMessage(message, 10000))
        self.guide_current.triggered.connect(self.onboarding.show_current)
        self.guide_intro.triggered.connect(self.show_intro)
        self.guide_automatic.setChecked(self.onboarding.automatic)
        self.guide_automatic.toggled.connect(self.onboarding.set_automatic)
        self.onboarding.automatic_changed.connect(self.guide_automatic.setChecked)
        install_main_tutorials(self)
        self.codex_connection.changed.connect(self.show_codex_connection)
        bind_theme(self, self.refresh_global_metrics)
        self.poll = QTimer(self)
        self.poll.setInterval(5000)
        self.poll.timeout.connect(self.poll_changes)
        self.load_capabilities()
        self.poll.start()

    def _build(self):
        central = AppCanvas()
        central.setObjectName('AppCanvas')
        outer = self.shell_layout = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = self.navigation_sidebar = GlassPanel(role='sidebar')
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(182)
        side = self.sidebar_layout = QVBoxLayout(sidebar)
        side.setContentsMargins(15, 25, 15, 19)
        side.setSpacing(8)
        side.addWidget(plain_label("个人事务", "Brand"))
        side.addWidget(plain_label("Beta 测试版", "BrandSub"))
        self.nav_buttons = {}
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for key, label in NAVIGATION:
            widget = NavigationButton(label)
            widget.clicked.connect(lambda _, k=key: self.navigate(k))
            widget.setObjectName("Nav")
            widget.setCheckable(True)
            self.nav_group.addButton(widget)
            self.nav_buttons[key] = widget
            side.addWidget(widget)
        side.addStretch()
        from .gui_menu import MenuButton
        self.new_button = MenuButton("＋ 新建")
        self.new_button.setObjectName("Primary")
        self.create_menu = QMenu(self.new_button)
        self.new_button.setMenu(self.create_menu)
        side.addWidget(self.new_button)
        self.habits_button = make_button('日常习惯', self.open_habits)
        self.habits_button.setToolTip('设置安排习惯、计划时长、留空时段与日期提醒')
        side.addWidget(self.habits_button)
        self.assistant_button = make_button('Codex 协助', lambda: self.open_settings(page='Codex 协助'))
        self.assistant_button.setToolTip('查看助手设置与连接状态；打开此页不会启用助手')
        side.addWidget(self.assistant_button)
        self.guide_button = MenuButton("使用指南")
        self.guide_button.setToolTip("重新查看功能介绍，或设置自动介绍")
        self.guide_menu = QMenu(self.guide_button)
        self.guide_current = self.guide_menu.addAction("重看当前面板介绍")
        self.guide_intro = self.guide_menu.addAction("查看入门导览")
        self.guide_menu.addSeparator()
        self.guide_automatic = self.guide_menu.addAction("自动介绍新面板")
        self.guide_automatic.setCheckable(True)
        self.guide_button.setMenu(self.guide_menu)
        side.addWidget(self.guide_button)
        side.addWidget(plain_label("本地保存 · 同一份记录", "SidebarHint"))
        outer.addWidget(sidebar)
        main = QWidget()
        main_layout = self.main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(31, 26, 30, 17)
        main_layout.setSpacing(20)
        self.header_panel = GlassPanel(role='toolbar')
        header = self.header_layout = QHBoxLayout(self.header_panel)
        heading = QVBoxLayout()
        heading.setSpacing(4)
        self.page_title = plain_label("总览", "PageTitle")
        self.page_subtitle = plain_label("日期、本周安排与当前进展。", "PageSubtitle")
        heading.addWidget(self.page_title)
        heading.addWidget(self.page_subtitle)
        header.addLayout(heading, 1)
        self.search_input = QLineEdit()
        self.search_input.setObjectName('GlobalSearch')
        self.search_input.setPlaceholderText('搜索事项，按 Enter · Ctrl+K')
        self.search_input.setAccessibleName('搜索事项')
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumWidth(190)
        self.search_input.setMaximumWidth(320)
        self.search_input.returnPressed.connect(self.search)
        self.search_symbol = self.search_input.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        self.search_shortcut = QShortcut(QKeySequence('Ctrl+K'), self)
        self.search_shortcut.activated.connect(self.focus_search)
        self.settings_button = make_button("设置", self.open_settings)
        self.settings_button.setObjectName("QuietButton")
        header.addWidget(self.search_input)
        header.addWidget(self.settings_button)
        self.update_button = make_button('版本与更新', self.open_update)
        self.update_button.setObjectName('QuietButton')
        header.addWidget(self.update_button)
        self.jobs_button = make_button('助手处理记录', self.open_jobs)
        self.jobs_button.hide()
        header.addWidget(self.jobs_button)
        main_layout.addWidget(self.header_panel)
        self.notice = plain_label("", "Notice")
        self.notice.hide()
        main_layout.addWidget(self.notice)
        self.codex_connection_panel = QWidget()
        connection_row = QHBoxLayout(self.codex_connection_panel)
        connection_row.setContentsMargins(0, 0, 0, 0)
        connection_row.addWidget(plain_label('Codex 连接', 'SectionTitle'))
        self.codex_connection_note = plain_label('正在检查连接…', 'Hint')
        self.codex_connection_note.setWordWrap(True)
        self.codex_connection_retry = make_button('连接 Codex', self.codex_connection.request_connect)
        connection_row.addWidget(self.codex_connection_note, 1)
        connection_row.addWidget(self.codex_connection_retry)
        self.codex_connection_panel.hide()
        main_layout.addWidget(self.codex_connection_panel)
        self.pages = QStackedWidget()
        self.today_page = TodayPage(self.bridge, self, on_plan=self.plan_with_codex, on_manual=self.manual_plan, on_review=self.open_review, on_task=self.open_task, on_changed=self.saved, on_timetable=self.open_timetable, on_habits=self.open_habits)
        install_calendar(self.today_page.date)
        self.today_page.error.connect(self.show_error)
        self.today_page.review_attention.connect(self.review_attention_changed)
        self.tasks_page = TasksPage(self.bridge, self, on_task=self.open_task, on_edit=self.edit_entity, on_changed=self.saved, on_plan=self.manual_plan)
        self.tasks_page.error.connect(self.show_error)
        self.workspace_page = WorkspacePage(self.bridge, self, on_create=self.create_entity, on_edit=self.edit_entity, on_changed=self.workspace_changed, on_codex=self.workspace_assistance)
        self.workspace_page.error.connect(self.show_error)
        if hasattr(self.workspace_page, 'sidebar_collapsed_changed'):
            self.workspace_page.sidebar_collapsed_changed.connect(self.save_workspace_sidebar)
        self.review_page = ReviewPage(self.bridge, self, on_changed=self.saved, on_codex=self.review_handoff)
        self.review_page.has_pending.connect(self.pending_review_changed)
        self.dashboard_page = DashboardPage(self.bridge, self, on_today=self.open_today, on_timetable=self.open_timetable, on_projects=self.open_project, on_task=self.open_task)
        self.dashboard_page.error.connect(self.show_error)
        self.dashboard_page.review_attention.connect(self.review_attention_changed)
        for page in (self.dashboard_page, self.review_page):
            page.chart_settings_requested.connect(lambda key: self.open_settings(page='显示', chart_key=key))
        self.pages.addWidget(self.dashboard_page)
        self.pages.addWidget(self.today_page)
        self.pages.addWidget(self.tasks_page)
        self.pages.addWidget(self.workspace_page)
        self.pages.addWidget(self.review_page)
        self.page_transition = PageTransition(self.pages, self)
        main_layout.addWidget(self.pages, 1)
        outer.addWidget(main, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.connection_label = QLabel("正在连接本地记录…")
        self.activity_label = QLabel()
        self.statusBar().addWidget(self.connection_label, 1)
        self.statusBar().addPermanentWidget(self.activity_label)
        self.nav_buttons["dashboard"].setChecked(True)

    def refresh_global_metrics(self):
        self.navigation_sidebar.setObjectName('GlassSidebar')
        self.header_panel.setObjectName('GlassToolbar')
        self.shell_layout.setContentsMargins(12, 12, 12, 0)
        self.shell_layout.setSpacing(10)
        self.sidebar_layout.setContentsMargins(12, 20, 12, 16)
        self.sidebar_layout.setSpacing(5)
        self.main_layout.setContentsMargins(12, 0, 2, 12)
        self.main_layout.setSpacing(22)
        self.header_layout.setContentsMargins(18, 13, 18, 13)
        self.navigation_sidebar.style().unpolish(self.navigation_sidebar)
        self.navigation_sidebar.style().polish(self.navigation_sidebar)
        self.header_panel.style().unpolish(self.header_panel)
        self.header_panel.style().polish(self.header_panel)
        brand = self.navigation_sidebar.findChild(QLabel, 'Brand')
        base_width = 204
        widths = [max(base_width, round(base_width * current_appearance()['font_size'] / 13)), *(button.fontMetrics().horizontalAdvance(button.text()) + 78 for button in self.nav_buttons.values())]
        if brand is not None:
            widths.append(brand.fontMetrics().horizontalAdvance(brand.text()) + 46)
        self.navigation_sidebar.setFixedWidth(max(widths))
        self.refresh_navigation_icons()

    def refresh_navigation_icons(self):
        from .gui_theme import color
        size = max(18, round(19 * current_appearance()['font_size'] / 13))
        for key, button in self.nav_buttons.items():
            button.setIcon(symbol_icon(key, color('primary') if button.isChecked() else color('muted'), size))
            button.setIconSize(QSize(size, size))
        for button, name in ((self.habits_button, 'habits'), (self.assistant_button, 'assistant'),
                             (self.guide_button, 'help'), (self.settings_button, 'settings'),
                             (self.update_button, 'update')):
            button.setIcon(symbol_icon(name, color('muted'), size))
            button.setIconSize(QSize(size, size))
        self.new_button.setIcon(symbol_icon('plus', color('primary_text'), size))
        self.new_button.setText('新建')
        self.search_symbol.setVisible(True)
        self.search_symbol.setIcon(symbol_icon('search', color('muted'), 16))

    def apply_visual_style(self, selected=None):
        """Refresh presentation in place without replacing open editors."""
        apply_appearance(QApplication.instance(), current_appearance())
        for page in (self.today_page,self.review_page):
            if hasattr(page,'apply_visual_style'):
                page.apply_visual_style()
        if self.onboarding.overlay is not None:
            self.onboarding.overlay.update()

    def activity_changed(self, busy):
        self.activity_label.setText("处理中…" if busy else "")
        if not busy and self.close_requested:
            QTimer.singleShot(0, self.close)

    def show_error(self, error):
        self.notice.setText(error.get("message", str(error)))
        self.notice.show()

    def show_codex_connection(self, value):
        if self.closed:
            return
        self.codex_connection_panel.setVisible(value['required'] and self._assistants_visible)
        self.codex_connection_note.setText(value['message'])
        self.codex_connection_retry.setVisible(True)
        self.codex_connection_retry.setText('检查连接' if value.get('ready') else '连接 Codex')
        self.codex_connection_retry.setEnabled(not value.get('connect_pending'))

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

    def onboarding_ready(self):
        # Initial structure and display preferences determine tutorial targets.
        # Unrelated polls and later refreshes need not drain the entire bridge.
        if not self.type_map or not self._onboarding_display_loaded or self.closed:
            return False
        return self.section != 'dashboard' or bool(self.dashboard_page.result)

    def _apply_display_preferences(self, settings, epoch=None, revision=None):
        if epoch == self._display_epoch and revision is not None and self._display_revision is not None and revision < self._display_revision:
            return
        self._display_epoch, self._display_revision = epoch, revision
        self._assistants_visible = assistants_visible(settings)
        for page in (self.today_page, self.workspace_page, self.review_page):
            page.set_assistants_visible(self._assistants_visible)
        for dialog in self.dialogs:
            if hasattr(dialog, 'set_assistants_visible'):
                dialog.set_assistants_visible(self._assistants_visible)
        self.codex_connection.configure(settings)
        self.show_codex_connection(self.codex_connection.snapshot())
        self.jobs_button.setVisible(self._assistants_visible or self._assistant_work_pending)
        self.refresh_assistant_activity()
        self._business_timezone = settings.get("timezone")
        self._saved_appearance = normalize_appearance(settings.get('appearance'))
        apply_appearance(QApplication.instance(), self._saved_appearance)
        self.review_page.set_chart_preferences(settings.get('charts', {}))
        self.dashboard_page.set_chart_preferences(settings.get('charts', {}))
        if not self._sidebar_pending:
            self._sidebar_desired = self._saved_appearance['workspace_sidebar_collapsed']
            if hasattr(self.workspace_page, 'set_sidebar_collapsed'):
                self.workspace_page.set_sidebar_collapsed(self._sidebar_desired)
        self._onboarding_display_loaded = True

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
        self.refresh_navigation_icons()
        self.pages.setCurrentWidget({'dashboard': self.dashboard_page, 'today': self.today_page, 'tasks': self.tasks_page, 'projects': self.workspace_page, 'reviews': self.review_page}[section])
        titles = {
            "dashboard": ("总览", "日期、本周安排与当前进展。"),
            "today": ("今天", "先看当天的安排，再开始行动。"),
            "tasks": ("任务", "先记录，按需要整理与安排。"),
            "projects": ("项目与课程", "沿着归属浏览，任务和文件都放在需要它们的地方。"),
            "reviews": ("复盘", "按实际情况确认完成，再看一周的变化。"),
        }
        self.page_title.setText(titles[section][0])
        self.page_subtitle.setText(titles[section][1])
        self.notice.hide()
        self.refresh()

    def show_intro(self):
        self.navigate('dashboard')
        self.onboarding.show('dashboard')

    def refresh(self):
        if not self.type_map or self.closed:
            return
        if self.section == "dashboard":
            self.dashboard_page.refresh()
        elif self.section == "today":
            self.today_page.refresh()
        elif self.section == "tasks":
            self.tasks_page.refresh()
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

    def open_today(self, date):
        self.navigate('today')
        self.today_page.set_date(date)

    def open_project(self, identifier=None):
        self.navigate('projects')
        if identifier:
            self.bridge.query('get',lambda r:self.workspace_page.open_object(r['entity']),self.show_error,id=identifier)

    def open_task(self, identifier):
        def loaded(result):
            dialog = TaskDetailDialog(self.bridge, result["entity"], self, self.edit_entity, on_saved=self.saved, business_date=self.today_page.date_iso() if self.section == "today" else None, assistants_visible=self._assistants_visible)
            self.dialogs.add(dialog)
            dialog.finished.connect(lambda _: self.dialogs.discard(dialog))
            dialog.open()
        self.bridge.query("get", loaded, self.show_error, id=identifier)

    def open_review(self, date, mode="daily"):
        self.section = "reviews"
        self.nav_buttons["reviews"].setChecked(True)
        self.pages.setCurrentWidget(self.review_page)
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
        prompt = f"请为 {date} {verb}每日计划。请先使用已保存的任务和固定安排，直接给出可保存的计划。时间或精力不确定时先按先后顺序安排少量重点，不猜钟点；保留休息与缓冲。只有确实无法继续时才问一个必要问题，其他缺口作为提醒保留。"
        self.open_assistance(prompt=prompt, date=date, scope={"kind":"daily_plan","date":date}, intent="daily_plan")

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

    def open_settings(self, *_args, page=None, timetable_id=None, chart_key=None):
        if page in {'日常习惯', '复盘时间'}:
            return self.open_habits(reminders=page == '复盘时间')
        dialog=SettingsDialog(self.bridge,self.capabilities,self.data_dir,self,self.load_capabilities)
        if timetable_id:dialog.timetable_settings.select_timetable(timetable_id)
        if page:
            for index in range(dialog.tabs.count()):
                if dialog.tabs.tabText(index)==page:dialog.tabs.setCurrentIndex(index)
        if chart_key:
            dialog.focus_chart(chart_key)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def open_habits(self, *_args, reminders=False):
        from .gui_workflows import HabitsDialog
        dialog = HabitsDialog(self.bridge, self.capabilities, self, on_saved=lambda *_: self.load_capabilities())
        if reminders:
            dialog.habits.show_reminders()
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def open_update(self):
        from .gui_update import UpdateDialog
        dialog = UpdateDialog(self)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def open_jobs(self):
        from .gui_workflows import JobsDialog
        dialog = JobsDialog(self.bridge, self, on_changed=self.saved)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def refresh_assistant_activity(self):
        if self.closed or self._assistant_activity_pending:
            return
        self._assistant_activity_pending = True
        def loaded(result):
            self._assistant_activity_pending = False
            if self.closed:
                return
            active, awaiting = result['active'], result['awaiting_review']
            self._assistant_work_pending = bool(active or awaiting)
            self.jobs_button.setText(f'助手：{active} 项处理中 · {awaiting} 项待核对' if active or awaiting else '助手处理记录')
            self.jobs_button.setVisible(self._assistants_visible or self._assistant_work_pending)
        def failed(_error):
            self._assistant_activity_pending = False
            # An optional status read must not interrupt manual work.
        self.bridge.query('assistant_activity', loaded, failed)

    def activate_from_tray(self, command='show'):
        if self.closed:
            return
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        if command == 'update':
            self.open_update()
        elif command == 'exit':
            from .gui_shutdown import request_exit
            request_exit(self)

    def focus_search(self):
        self.search_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_input.selectAll()

    def search(self):
        if self.search_dialog is not None and self.search_dialog.isVisible():
            self.search_dialog.search.setText(self.search_input.text())
            self.search_dialog.load()
            self.search_dialog.raise_()
            self.search_dialog.activateWindow()
            return
        def selected(entity):
            if entity["type"] in {"task", "note", "milestone", "topic"}:
                self.open_task(entity["id"])
            else:
                self.navigate("projects")
                self.workspace_page.open_object(entity)
        dialog = SearchDialog(self.bridge, self, selected, initial_text=self.search_input.text())
        self.search_dialog = dialog
        self.dialogs.add(dialog)
        def finished(_result):
            self.search_input.setText(dialog.search.text())
            self.dialogs.discard(dialog)
            if self.search_dialog is dialog:
                self.search_dialog = None
            dialog.deleteLater()
        dialog.finished.connect(finished)
        dialog.open()

    def poll_changes(self):
        if self.closed or self.poll_pending or not self.type_map:
            return
        calendar_day, minute = clock_snapshot(self._business_timezone)
        clock_changed = minute != self._clock_minute
        self._clock_minute = minute
        if calendar_day != self._calendar_day:
            previous = self._calendar_day
            self._calendar_day = calendar_day
            if self.today_page.date.date() == previous:
                self.today_page.date.setDate(calendar_day)
            clock_changed = True
        self.poll_pending = True
        def loaded(result):
            self.poll_pending = False
            self.change_cursor = result.get("cursor", self.change_cursor)
            if result.get("items") or result.get("reset_required") or clock_changed and self.section in {"dashboard", "today", "tasks"}:
                if result.get("reset_required") or any(item.get("action")=="settings" for item in result.get("items",[])):
                    self.load_display_preferences()
                self.refresh()
                self.refresh_assistant_activity()
        def failed(error):
            self.poll_pending = False
            self.show_error(error)
        self.bridge.query("changes", loaded, failed, after=self.change_cursor, epoch=self.bridge.epoch, limit=100)

    def closeEvent(self, event):
        if self.tasks_page.quick_pending:
            self.notice.setText('正在保存任务，请等待结果后再关闭；保存失败时会保留输入。')
            self.notice.show()
            event.ignore()
            return
        if self.tasks_page.quick_input.text().strip() and not self.tasks_page.quick_pending and not self.close_requested:
            choice = QMessageBox.question(self, '任务尚未保存', '快速记录中仍有文字。要放弃这些输入并关闭窗口吗？', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if choice != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.tasks_page.quick_input.clear()
        if self.review_pending and not self.close_requested:
            choice = QMessageBox.question(self, "仍有未确认的复盘选择", "本次选择尚未保存。要关闭窗口并放弃这些选择吗？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if choice != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.review_pending = False
        self.closed = True
        self.onboarding.stop()
        self.poll.stop()
        self.codex_connection.request_stop()
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
        if not self.codex_connection.close(timeout_ms=0):
            self.close_requested = True
            self.notice.setText("正在结束连接检查，然后自动关闭。")
            self.notice.show()
            QTimer.singleShot(100, self.close)
            event.ignore()
            return
        event.accept()


def run(data_dir, argv=None, *, show_update=False):
    from .branding import set_windows_identity, configure_application
    from .data_space import require_beta_dir
    data_dir = require_beta_dir(data_dir)
    set_windows_identity()
    app = QApplication.instance() or QApplication(argv or [])
    configure_application(app)
    from .gui_instance import WindowInstance, notify_window
    instance = WindowInstance(data_dir, app)
    if not instance.acquire():
        command = 'update' if show_update else 'show'
        if not notify_window(data_dir, command):
            QMessageBox.information(None, APP_NAME, '已有 Beta 窗口正在启动或退出，请稍后重试。')
        return 0
    manager = install_gui_gc(app)
    app.setStyle("Fusion")
    configure_palette(app)
    window = MainWindow(data_dir)
    instance.requested.connect(window.activate_from_tray)
    window.show()
    if show_update:
        QTimer.singleShot(0, window.open_update)
    result = app.exec()
    # Direct application.exit() can bypass Quit events. Keep Qt objects alive
    # and continue owner-thread collection until bounded client calls finish.
    while not manager.shutdown():
        app.processEvents()
        manager.poll()
        QThread.msleep(10)
    instance.close()
    return result
