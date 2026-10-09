"""Silver and graphite surfaces for the optional native glass presentation.

Only presentation tokens live here. Translucent shell materials are painted by
their widgets; dialogs and business content keep opaque, readable surfaces.
"""
from __future__ import annotations


LIGHT = dict(
    window='#f3f4f6', surface='#ffffff', surface_alt='#f0f1f4', sidebar='#eceef2',
    text='#292a30', muted='#666a74', border='#d9dce2',
    primary='#b44845', primary_text='#ffffff', primary_hover='#9f3c3b',
    selection='#f4e6e4', selection_text='#793a39',
    warning_bg='#fff2f0', warning_text='#92524a', warning_border='#e6c3bd',
    disabled_bg='#eceef1', disabled_text='#8b8e96', focus='#bd605d', scrollbar='#c6c9d1',
    done='#607b71', incomplete='#986532', unreported='#d8dce3', other_reported='#7b6b96',
    chart_track='#eff0f3', chart_grid='#d4d7de', chart_label='#676b74',
    chart_on_color='#ffffff', chart_on_unknown='#444852',
    chat_user='#f4e8e7', calendar_header='#f3f3f6', calendar_today='#f2ddda',
    danger='#ac383f', danger_bg='#fbecef', danger_border='#dba9b0',
    hover='#f6f6f8', pressed='#e8e9ee',
    canvas_top='#fafafb', canvas_bottom='#ebeef3',
    glass_border='#d7dae2', glass_highlight='#ffffff',
    button_top='#ffffff', button_bottom='#f5f5f7',
    sidebar_top='rgba(255, 255, 255, 210)', sidebar_bottom='rgba(235, 237, 243, 192)',
    menu='#fcfcfd', focus_soft='#ead0cd',
)

DARK = dict(
    window='#202124', surface='#2b2c30', surface_alt='#33353a', sidebar='#242529',
    text='#f0eff2', muted='#b2b0ba', border='#48484f',
    primary='#dd9998', primary_text='#301c20', primary_hover='#ebaeab',
    selection='#4a353c', selection_text='#f7dcde',
    warning_bg='#3d3034', warning_text='#e0b0a7', warning_border='#775653',
    disabled_bg='#323337', disabled_text='#898890', focus='#e4b3b1', scrollbar='#626269',
    done='#a8bdb4', incomplete='#d9b18b', unreported='#505158', other_reported='#bdb0d5',
    chart_track='#36373d', chart_grid='#4e4e57', chart_label='#b9b6c1',
    chart_on_color='#251f24', chart_on_unknown='#edeaf1',
    chat_user='#44363e', calendar_header='#323237', calendar_today='#564049',
    danger='#f0a7ac', danger_bg='#472c35', danger_border='#75515c',
    hover='#3a3b41', pressed='#44454d',
    canvas_top='#2a2b30', canvas_bottom='#1e1f23',
    glass_border='#4b4c54', glass_highlight='#65656d',
    button_top='#3c3d43', button_bottom='#34353b',
    sidebar_top='rgba(52, 53, 59, 225)', sidebar_bottom='rgba(35, 36, 41, 218)',
    menu='#303137', focus_soft='#705159',
)


# These are interaction colors, not task/result colors. A palette choice must
# never change the meaning of success, warning, incomplete or destructive work.
_ACCENTS = {
    'light': {
        'coral': ('#b44845', '#9f3c3b', '#ffffff', '#f4e6e4', '#793a39', '#bd605d', '#ead0cd', '#f4e8e7', '#f2ddda'),
        'blue': ('#2d63a8', '#24538e', '#ffffff', '#e5edf8', '#274f80', '#447bbd', '#c8d9ef', '#e7eef8', '#d9e6f6'),
        'violet': ('#7850a4', '#65428e', '#ffffff', '#eee7f6', '#624081', '#8b66b3', '#dfcfee', '#eee8f5', '#e7dcef'),
        'rose': ('#a9436f', '#92395f', '#ffffff', '#f6e6ee', '#853556', '#bb5f88', '#ebcedc', '#f5e8ef', '#f0dce5'),
        'teal': ('#237368', '#1b6259', '#ffffff', '#e0f0ec', '#20594f', '#3a8b7f', '#bedfd6', '#e5f0ed', '#d3eae3'),
    },
    'dark': {
        'coral': ('#dd9998', '#ebaeab', '#301c20', '#4a353c', '#f7dcde', '#e4b3b1', '#705159', '#44363e', '#564049'),
        'blue': ('#95baf1', '#adcaf5', '#17263b', '#30415b', '#dfebff', '#b0ccf5', '#526b91', '#303e53', '#394e6d'),
        'violet': ('#c2a5e8', '#d1bbef', '#2b1e3d', '#443650', '#efe1ff', '#d4bdf1', '#715783', '#40364b', '#51405f'),
        'rose': ('#e4a0bf', '#edb5ce', '#371d2b', '#4d3543', '#ffdeed', '#ecc1d4', '#7c5367', '#46363f', '#5c4050'),
        'teal': ('#8ac9ba', '#a2d9cb', '#172e29', '#2b4741', '#d7f6ed', '#abdccc', '#4c776c', '#2e423d', '#36564d'),
    },
}
_ACCENT_ROLES = ('primary', 'primary_hover', 'primary_text', 'selection',
                 'selection_text', 'focus', 'focus_soft', 'chat_user', 'calendar_today')


def accent_tokens(theme='light', accent='default'):
    """Return only portable accent overrides; default keeps the profile's palette."""
    if accent == 'default':
        return {}
    return dict(zip(_ACCENT_ROLES, _ACCENTS[theme][accent]))


def palette_tokens(theme='light', accent='default'):
    return {**(DARK if theme == 'dark' else LIGHT), **accent_tokens(theme, accent)}


def stylesheet(config):
    tokens = palette_tokens(config['theme'], config.get('accent', 'default'))
    scale = config['font_size'] / 13
    family = config['font_family'] or 'Microsoft YaHei UI'
    tokens['font_family'] = family.replace('\\', '\\\\').replace('"', '\\"')
    tokens.update({name: max(11, round(size * scale)) for name, size in {
        'small': 11, 'hint_size': 12, 'body_size': 13, 'nav_size': 14,
        'section_size': 15, 'week_size': 16, 'mark_size': 17, 'card_size': 20,
        'brand_size': 21, 'dialog_size': 22, 'metric_size': 25, 'title_size': 27,
    }.items()})
    tokens['indicator_size'] = max(15, round(15 * scale))
    tokens['radio_radius'] = max(7, round(tokens['indicator_size'] / 2))
    return _STYLE % tokens


_STYLE = r'''
QWidget {
    font-family: "%(font_family)s"; font-size: %(body_size)dpx;
    color: %(text)s; background: transparent;
}
QMainWindow { background: %(window)s; }
QDialog, QMessageBox, QProgressDialog { background: %(surface)s; }
QWidget#AppCanvas, QWidget#GlassSidebar, QWidget#GlassToolbar {
    background: transparent; border: 0;
}
QFrame#Sidebar {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 %(sidebar_top)s, stop:1 %(sidebar_bottom)s);
    border: 1px solid %(glass_border)s; border-radius: 18px;
}
QLabel { background: transparent; border: 0; }
QLabel#Brand { color: %(text)s; font-size: %(brand_size)dpx; font-weight: 700; padding: 10px 8px 0; }
QLabel#BrandSub { color: %(muted)s; font-size: %(small)dpx; padding: 2px 8px 18px; }
QLabel#SidebarHint { color: %(muted)s; font-size: %(small)dpx; padding: 8px; }
QLabel#PageTitle, QLabel#DashboardDate, QLabel#TodayDateHeading {
    color: %(text)s; font-size: %(title_size)dpx; font-weight: 700;
}
QLabel#PageSubtitle, QLabel#Quiet, QLabel#Hint, QLabel#AttentionText {
    color: %(muted)s; font-size: %(hint_size)dpx;
}
QLabel#Eyebrow { color: %(muted)s; font-size: %(small)dpx; padding-bottom: 3px; }
QLabel#DialogHeading { color: %(text)s; font-size: %(dialog_size)dpx; font-weight: 700; padding-bottom: 10px; }
QLabel#CardTitle { color: %(text)s; font-size: %(card_size)dpx; font-weight: 600; }
QLabel#SectionHeading, QLabel#SectionTitle, QLabel#ReviewSummary {
    color: %(text)s; font-size: %(section_size)dpx; font-weight: 600;
}
QLabel#DashboardWeek { color: %(muted)s; font-size: %(week_size)dpx; }
QLabel#DashboardMetric { color: %(text)s; font-size: %(metric_size)dpx; font-weight: 700; }
QLabel#Body { color: %(text)s; font-size: %(body_size)dpx; padding: 3px 0; }
QLabel#Error { color: %(danger)s; background: %(danger_bg)s; border: 1px solid %(danger_border)s; border-radius: 10px; padding: 10px 13px; }
QLabel#Notice {
    color: %(warning_text)s; background: %(warning_bg)s;
    border: 1px solid %(warning_border)s; border-radius: 10px; padding: 10px 13px;
}
QLabel#ContextCard { color: %(text)s; background: %(surface_alt)s; border: 1px solid %(border)s; border-radius: 10px; padding: 10px 13px; }
QLabel#StatusPill, QLabel#FileBadge {
    color: %(muted)s; background: %(surface_alt)s;
    border-radius: 6px; padding: 5px 8px; font-size: %(small)dpx;
}
QLabel#TimeLabel { color: %(muted)s; font-size: %(hint_size)dpx; }
QLabel#DoneDot { color: %(done)s; font-size: %(mark_size)dpx; }
QLabel#TaskMark { color: %(primary)s; font-size: %(mark_size)dpx; }
QLabel#WaitingDot { color: %(disabled_text)s; font-size: %(nav_size)dpx; }
QLabel#AttentionTitle, QLabel#CurrentWarningTitle { color: %(warning_text)s; font-weight: 700; }
QLabel#FontPreview { background: %(surface_alt)s; border: 1px solid %(border)s; border-radius: 12px; padding: 12px; }
QFrame#PlanCard, QFrame#ReviewItem {
    background: %(surface)s; border: 1px solid %(border)s; border-radius: 15px;
}
QFrame#ProgressCard, QFrame#DirectoryPanel {
    background: %(surface_alt)s; border: 1px solid %(border)s; border-radius: 12px;
}
QFrame#TaskRow, QFrame#FileRow, QFrame#EventRow {
    background: %(surface)s; border: 1px solid %(border)s; border-radius: 11px;
}
QFrame#TimelineRow { background: transparent; border: 0; border-bottom: 1px solid %(border)s; }
QFrame#ContentSection, QFrame#WarningSummary { background: transparent; border: 0; }
QFrame#AttentionCard { background: %(surface)s; border: 1px solid %(warning_border)s; border-radius: 11px; }
QFrame#CurrentWarningCard {
    background: %(surface)s; border: 1px solid %(warning_border)s;
    border-left: 3px solid %(warning_text)s; border-radius: 13px;
}
QFrame#DashboardToday { background: %(selection)s; border: 2px solid %(primary)s; border-radius: 13px; }
QFrame#ChatComposer { background: %(surface)s; border: 1px solid %(border)s; border-radius: 15px; }
QFrame#ChatMessage[userMessage="true"] { background: %(chat_user)s; border-radius: 14px; }
QPushButton, QToolButton {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 %(button_top)s, stop:1 %(button_bottom)s);
    color: %(text)s; border: 1px solid %(border)s; border-radius: 9px; padding: 8px 13px;
}
QPushButton:hover, QToolButton:hover { background: %(hover)s; border-color: %(scrollbar)s; }
QPushButton:pressed, QToolButton:pressed { background: %(pressed)s; }
QPushButton:checked, QToolButton:checked { background: %(selection)s; color: %(selection_text)s; border-color: %(focus)s; }
QPushButton#Primary, QPushButton#TutorialNext {
    background: %(primary)s; color: %(primary_text)s; border-color: %(primary)s; font-weight: 600;
}
QPushButton#Primary:hover, QPushButton#TutorialNext:hover { background: %(primary_hover)s; border-color: %(primary_hover)s; }
QPushButton#Primary:pressed, QPushButton#TutorialNext:pressed { background: %(primary_hover)s; border-color: %(focus)s; }
QPushButton#Prominent { background: %(surface)s; color: %(text)s; border: 1px solid %(primary)s; padding: 10px 16px; font-weight: 600; }
QPushButton#Prominent:hover { background: %(selection)s; }
QPushButton#Nav {
    color: %(text)s; background: transparent; border: 1px solid transparent;
    border-radius: 10px; text-align: left; padding: 11px 14px; font-size: %(nav_size)dpx;
}
QPushButton#Nav:hover { background: %(hover)s; }
QPushButton#Nav:checked { background: %(selection)s; color: %(selection_text)s; border-color: %(focus_soft)s; font-weight: 600; }
QPushButton#Nav:pressed { background: %(pressed)s; }
QPushButton#QuietButton, QPushButton#CompletedTasksToggle, QPushButton#TutorialQuiet {
    background: transparent; color: %(muted)s; border: 1px solid transparent;
}
QPushButton#QuietButton:hover, QPushButton#CompletedTasksToggle:hover, QPushButton#TutorialQuiet:hover {
    background: %(hover)s; color: %(text)s; border-color: %(border)s;
}
QPushButton#TextLink {
    text-align: left; padding: 2px 3px; background: transparent;
    color: %(text)s; border: 1px solid transparent; font-size: %(nav_size)dpx;
}
QPushButton#TextLink:hover { color: %(primary)s; background: %(hover)s; }
QPushButton#TaskMark { padding: 4px; color: %(primary)s; font-weight: 700; }
QPushButton#FolderCard {
    background: %(surface)s; color: %(text)s; border: 1px solid %(border)s;
    border-radius: 13px; padding: 14px 17px; text-align: left; font-size: %(nav_size)dpx;
}
QPushButton#FolderCard:hover, QPushButton#HabitsSummary:hover { background: %(hover)s; border-color: %(focus)s; }
QPushButton#HabitsSummary { background: %(surface_alt)s; color: %(text)s; border: 1px solid %(border)s; border-radius: 12px; padding: 10px 14px; text-align: left; }
QPushButton#Danger, QPushButton#DeleteTask { color: %(danger)s; border-color: %(danger_border)s; }
QPushButton#Danger:hover, QPushButton#DeleteTask:hover { background: %(danger_bg)s; }
QPushButton#ReviewChoice[result="done"]:checked, QPushButton#ReviewChoice[result="attended"]:checked {
    background: %(done)s; color: %(chart_on_color)s; border: 2px solid %(done)s; font-weight: 600;
}
QPushButton#ReviewChoice[result="incomplete"]:checked, QPushButton#ReviewChoice[result="missed_needs_catchup"]:checked {
    background: %(incomplete)s; color: %(chart_on_color)s; border: 2px solid %(incomplete)s; font-weight: 600;
}
QPushButton#ReviewChoice[result="absent"]:checked { background: %(danger)s; color: %(chart_on_color)s; border: 2px solid %(danger)s; font-weight: 600; }
QPushButton:focus, QToolButton:focus, QPushButton#Primary:focus, QPushButton#Prominent:focus,
QPushButton#Nav:focus, QPushButton#QuietButton:focus, QPushButton#CompletedTasksToggle:focus,
QPushButton#TextLink:focus, QPushButton#TaskMark:focus, QPushButton#FolderCard:focus,
QPushButton#HabitsSummary:focus, QPushButton#Danger:focus, QPushButton#DeleteTask:focus,
QPushButton#ReviewChoice:focus, QPushButton#ReviewChoice:checked:focus,
QPushButton#TutorialNext:focus, QPushButton#TutorialQuiet:focus {
    border: 2px solid %(focus)s;
}
QPushButton:disabled, QToolButton:disabled, QPushButton#Primary:disabled, QPushButton#Prominent:disabled,
QPushButton#Nav:disabled, QPushButton#QuietButton:disabled, QPushButton#TextLink:disabled,
QPushButton#TaskMark:disabled, QPushButton#FolderCard:disabled, QPushButton#HabitsSummary:disabled,
QPushButton#Danger:disabled, QPushButton#DeleteTask:disabled, QPushButton#CompletedTasksToggle:disabled,
QPushButton#TutorialNext:disabled, QPushButton#TutorialQuiet:disabled {
    background: %(disabled_bg)s; color: %(disabled_text)s; border: 1px solid %(border)s;
}
QPushButton#ReviewChoice:disabled { color: %(disabled_text)s; border-color: %(border)s; }
QPushButton#ReviewChoice:checked:disabled { background: %(surface_alt)s; color: %(disabled_text)s; border: 2px solid %(disabled_text)s; }
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit, QDateTimeEdit {
    background: %(surface)s; color: %(text)s; border: 1px solid %(border)s;
    border-radius: 8px; padding: 7px; selection-background-color: %(selection)s; selection-color: %(selection_text)s;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus, QTimeEdit:focus, QDateTimeEdit:focus { border: 2px solid %(focus)s; }
QLineEdit:read-only, QTextEdit:read-only, QPlainTextEdit:read-only { background: %(surface_alt)s; }
QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled,
QSpinBox:disabled, QDoubleSpinBox:disabled, QDateEdit:disabled, QTimeEdit:disabled, QDateTimeEdit:disabled {
    background: %(disabled_bg)s; color: %(disabled_text)s; border-color: %(border)s;
}
QComboBox { padding-right: 26px; }
QComboBox::drop-down { width: 24px; border: 0; }
QComboBox::down-arrow { image: none; width: 0; height: 0; }
QPushButton[disclosure="true"] { padding-right: 32px; text-align: left; }
QWidget#ControlChevron { background: transparent; border: 0; }
QComboBox QAbstractItemView { background: %(surface)s; color: %(text)s; border: 1px solid %(border)s; selection-background-color: %(selection)s; selection-color: %(selection_text)s; }
QAbstractItemView, QListWidget, QTreeWidget, QTableWidget, QTextBrowser {
    background: %(surface)s; alternate-background-color: %(surface_alt)s; color: %(text)s;
    border: 1px solid %(border)s; border-radius: 10px; padding: 5px;
    selection-background-color: %(selection)s; selection-color: %(selection_text)s;
}
QAbstractItemView:focus { border-color: %(focus)s; }
QListWidget::item { padding: 10px 9px; border-bottom: 1px solid %(border)s; border-radius: 6px; }
QTreeView::item { padding: 7px 9px; }
QListWidget::item:hover, QTreeView::item:hover, QTableView::item:hover { background: %(hover)s; }
QListWidget::item:selected, QTreeView::item:selected, QTableView::item:selected {
    background: %(selection)s; color: %(selection_text)s;
}
QAbstractItemView:disabled, QTextBrowser:disabled { background: %(disabled_bg)s; color: %(disabled_text)s; }
QTreeWidget#WorkspaceTree { background: transparent; border: 0; padding: 2px; }
QTreeWidget#WorkspaceTree::item { padding: 6px; border: 1px solid transparent; border-radius: 7px; }
QTreeWidget#WorkspaceTree::item:selected { background: %(selection)s; color: %(selection_text)s; border-color: %(focus_soft)s; }
QTreeWidget#WorkspaceTree::item:hover { background: %(hover)s; }
QTreeWidget#WorkspaceTree::item:selected:hover { background: %(selection)s; color: %(selection_text)s; }
QTreeWidget#WorkspaceTree:focus { border: 1px solid %(focus)s; }
QTableView { gridline-color: %(border)s; }
QHeaderView { background: %(surface_alt)s; }
QHeaderView::section { background: %(surface_alt)s; color: %(text)s; border: 0; border-bottom: 1px solid %(border)s; padding: 8px; }
QHeaderView::section:checked { background: %(selection)s; color: %(selection_text)s; }
QTableCornerButton::section { background: %(surface_alt)s; border: 1px solid %(border)s; }
QProgressBar { background: %(chart_track)s; color: %(text)s; border: 0; border-radius: 5px; text-align: center; }
QProgressBar::chunk { background: %(primary)s; border-radius: 5px; }
QProgressBar#OwnerProgress, QProgressBar#OwnerProgress::chunk { border-radius: 3px; }
QTabWidget::pane { background: transparent; border: 0; }
QTabBar::tab { background: %(surface_alt)s; color: %(muted)s; border: 1px solid transparent; border-radius: 8px; padding: 9px 17px; margin: 2px; }
QTabBar::tab:hover { background: %(hover)s; color: %(text)s; }
QTabBar::tab:selected { background: %(surface)s; color: %(selection_text)s; border-color: %(border)s; border-bottom: 2px solid %(primary)s; font-weight: 600; }
QTabBar::tab:disabled { color: %(disabled_text)s; background: %(disabled_bg)s; }
QTabBar:focus { border: 1px solid %(focus)s; border-radius: 9px; }
QCheckBox, QRadioButton { spacing: 7px; }
QCheckBox:focus, QRadioButton:focus { border: 1px solid %(focus)s; border-radius: 5px; }
QCheckBox:disabled, QRadioButton:disabled { color: %(disabled_text)s; }
QCheckBox::indicator, QTreeView::indicator { width: %(indicator_size)dpx; height: %(indicator_size)dpx; background: %(surface)s; border: 1px solid %(scrollbar)s; border-radius: 4px; }
QCheckBox::indicator:hover, QTreeView::indicator:hover { border-color: %(focus)s; }
QCheckBox::indicator:checked, QTreeView::indicator:checked { background: %(primary)s; border-color: %(primary)s; }
QCheckBox::indicator:indeterminate, QTreeView::indicator:indeterminate { background: %(focus_soft)s; border: 2px solid %(primary)s; }
QCheckBox::indicator:disabled, QTreeView::indicator:disabled { background: %(disabled_bg)s; border-color: %(border)s; }
QCheckBox::indicator:checked:disabled, QTreeView::indicator:checked:disabled { background: %(disabled_text)s; }
QRadioButton::indicator { width: %(indicator_size)dpx; height: %(indicator_size)dpx; background: %(surface)s; border: 1px solid %(scrollbar)s; border-radius: %(radio_radius)dpx; }
QRadioButton::indicator:checked { background: %(primary)s; border: 3px solid %(focus_soft)s; }
QRadioButton::indicator:disabled { background: %(disabled_bg)s; border-color: %(border)s; }
QRadioButton::indicator:checked:disabled { background: %(disabled_text)s; }
QGroupBox { background: transparent; border: 1px solid %(border)s; border-radius: 11px; margin-top: 14px; padding-top: 12px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 12px; padding: 0 5px; color: %(text)s; }
QSlider::groove:horizontal { height: 5px; background: %(border)s; border-radius: 2px; }
QSlider::sub-page:horizontal { background: %(primary)s; border-radius: 2px; }
QSlider::handle:horizontal { background: %(surface)s; border: 1px solid %(scrollbar)s; width: 17px; margin: -6px 0; border-radius: 8px; }
QSlider::handle:horizontal:focus { border: 2px solid %(focus)s; }
QSlider::handle:horizontal:disabled { background: %(disabled_bg)s; }
QScrollArea, QStackedWidget { background: transparent; border: 0; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px 1px; }
QScrollBar::handle:vertical { background: %(scrollbar)s; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover { background: %(muted)s; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; border: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 1px 2px; }
QScrollBar::handle:horizontal { background: %(scrollbar)s; border-radius: 4px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; border: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
QStatusBar { background: %(surface_alt)s; color: %(muted)s; font-size: %(small)dpx; border-top: 1px solid %(border)s; }
QStatusBar::item { border: 0; }
QSplitter::handle { background: transparent; width: 12px; }
QSplitter::handle:hover { background: %(focus_soft)s; }
QMenuBar { background: %(surface)s; color: %(text)s; }
QMenuBar::item:selected { background: %(selection)s; color: %(selection_text)s; }
QMenu { background: %(menu)s; color: %(text)s; border: 1px solid %(border)s; border-radius: 10px; padding: 6px; }
QMenu::item { padding: 9px 24px; border-radius: 6px; }
QMenu::item:selected { background: %(selection)s; color: %(selection_text)s; }
QMenu::item:disabled { color: %(disabled_text)s; }
QMenu::separator { height: 1px; background: %(border)s; margin: 5px 8px; }
QToolTip { background: %(surface)s; color: %(text)s; border: 1px solid %(border)s; border-radius: 6px; padding: 6px 9px; }
QCalendarWidget { background: %(surface)s; border: 1px solid %(border)s; border-radius: 12px; }
QCalendarWidget QWidget#qt_calendar_navigationbar { background: %(calendar_header)s; padding: 6px; }
QCalendarWidget QToolButton { color: %(text)s; background: transparent; border: 1px solid transparent; border-radius: 7px; padding: 7px; font-weight: 600; }
QCalendarWidget QToolButton:hover { background: %(selection)s; }
QCalendarWidget QToolButton:focus { border-color: %(focus)s; }
QCalendarWidget QAbstractItemView { background: %(surface)s; color: %(text)s; selection-background-color: %(primary)s; selection-color: %(primary_text)s; border: 0; padding: 8px; }
QCalendarWidget QAbstractItemView:disabled { color: %(disabled_text)s; }
QWidget#TutorialOverlay, QScrollArea#TutorialBodyScroll { background: transparent; border: 0; }
QFrame#TutorialCard { background: %(surface)s; border: 1px solid %(border)s; border-radius: 15px; }
QLabel#TutorialTitle { color: %(text)s; font-size: %(card_size)dpx; font-weight: 700; }
QLabel#TutorialBody { color: %(text)s; font-size: %(nav_size)dpx; }
QLabel#TutorialCounter { color: %(muted)s; font-size: %(small)dpx; }
'''
