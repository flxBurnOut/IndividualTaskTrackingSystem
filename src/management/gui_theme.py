"""One palette and stylesheet for all native widgets, drawings and open dialogs."""
from __future__ import annotations
import re
import weakref
from PySide6.QtCore import QThread
from shiboken6 import isValid
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication
from .appearance import DEFAULT_APPEARANCE, normalize_appearance

LIGHT = dict(window='#f6f8f7',surface='#ffffff',surface_alt='#edf3ef',sidebar='#ecf1ee',
    text='#263b3c',muted='#7d8d84',border='#dce5df',primary='#326f57',primary_text='#ffffff',
    primary_hover='#255f48',selection='#eaf3ec',selection_text='#2d5e41',
    warning_bg='#fff5e8',warning_text='#9b6a39',warning_border='#f0e4ce',
    disabled_bg='#edf2ee',disabled_text='#8b9891',focus='#79a58a',scrollbar='#d4dfd6',
    done='#28786b',incomplete='#b67832',unreported='#d9dfe4',other_reported='#7286b2',
    chart_track='#eef1f4',chart_grid='#c7d2cb',chart_label='#7a8c80',chart_on_color='#ffffff',
    chart_on_unknown='#4c5f58',chat_user='#e9f0eb',calendar_header='#f1f6f2',calendar_today='#e5f0e7',danger='#a94040',danger_bg='#fff0f0',danger_border='#e8b0b0')
DARK = dict(window='#14161a',surface='#1d2026',surface_alt='#262a32',sidebar='#111318',
    text='#edf0f5',muted='#a4adba',border='#353b46',primary='#79a6ff',primary_text='#101929',
    primary_hover='#9abaff',selection='#273a5c',selection_text='#e3ebff',
    warning_bg='#342a1c',warning_text='#f0c58a',warning_border='#604b2e',
    disabled_bg='#272b33',disabled_text='#818a98',focus='#8db3ff',scrollbar='#4b5361',
    done='#77b8f6',incomplete='#dfaa69',unreported='#434954',other_reported='#aaa1e8',
    chart_track='#2c3039',chart_grid='#3c424f',chart_label='#aeb7c5',chart_on_color='#101929',
    chart_on_unknown='#e4e9f2',chat_user='#26354d',calendar_header='#252a34',calendar_today='#314b75',danger='#ff9f9f',danger_bg='#3a252b',danger_border='#72505a')

_LIGHT_STYLE = '\nQWidget { font-family: "Microsoft YaHei UI", "Microsoft YaHei"; font-size: 13px; color: #263b3c; }\nQMainWindow, QDialog { background: #f6f8f7; }\nQFrame#Sidebar { background: #ecf1ee; border-right: 1px solid #e0e7e2; }\nQLabel#Brand { font-size: 21px; font-weight: 700; color: #24453f; padding: 12px 8px 0; }\nQLabel#BrandSub { color: #7a8e84; font-size: 11px; padding: 0 8px 22px; }\nQPushButton#Nav { background: transparent; border: 0; border-radius: 8px; text-align: left; padding: 13px 15px; color: #587268; font-size: 14px; }\nQPushButton#Nav:checked { background: #ffffff; color: #215e4e; font-weight: 700; }\nQPushButton#Nav:hover { background: #f8fbf8; }\nQLabel#SidebarHint { color: #809187; font-size: 11px; padding: 8px; }\nQLabel#PageTitle { font-size: 27px; font-weight: 700; color: #233e35; }\nQLabel#PageSubtitle, QLabel#Quiet, QLabel#Hint { color: #7d8d84; font-size: 12px; }\nQLabel#Eyebrow { color: #759084; font-size: 11px; padding-bottom: 3px; }\nQLabel#DialogHeading { font-size: 22px; font-weight: 700; color: #233e35; padding-bottom: 12px; }\nQLabel#CardTitle { font-size: 20px; font-weight: 600; color: #233e35; }\nQLabel#SectionHeading { font-size: 15px; font-weight: 600; color: #344e43; }\nQLabel#Body { color: #556f61; font-size: 13px; padding: 3px 0; }\nQLabel#Error, QLabel#Notice { color: #9b6a39; background: #fff5e8; border: 1px solid #f0e4ce; border-radius: 7px; padding: 10px 14px; }\nQLabel#StatusPill { color: #658171; background: #edf3ef; border-radius: 5px; padding: 5px 8px; font-size: 11px; }\nQLabel#TimeLabel { color: #698270; font-size: 12px; }\nQLabel#DoneDot, QLabel#TaskMark { color: #39846c; font-size: 17px; }\nQLabel#WaitingDot { color: #c0cec5; font-size: 14px; }\nQLabel#FileBadge { background: #eef2f4; color: #758b94; border-radius: 5px; padding: 8px 5px; font-size: 10px; }\nQFrame#PlanCard { background: #ffffff; border: 1px solid #e5ebe6; border-radius: 12px; }\nQFrame#ProgressCard { background: #eef4ef; border: 0; border-radius: 9px; }\nQFrame#TimelineRow { border-bottom: 1px solid #edf1ee; }\nQFrame#TaskRow, QFrame#FileRow, QFrame#EventRow { background: #ffffff; border: 1px solid #e7ede8; border-radius: 8px; }\nQFrame#DirectoryPanel { background: #eff4f0; border: 0; border-radius: 10px; }\nQFrame#AttentionCard { background: #f9f2e7; border: 0; border-radius: 8px; }\nQLabel#AttentionTitle { color: #937241; font-weight: 600; }\nQLabel#AttentionText { color: #a18b67; font-size: 12px; }\nQPushButton { background: #ffffff; border: 1px solid #dce5df; border-radius: 6px; padding: 8px 13px; color: #4b6b58; }\nQPushButton:hover { background: #f0f6f0; border-color: #a9c2b2; }\nQPushButton:disabled { color: #a7b5ad; background: #edf2ee; border-color: #e2e8e3; }\nQPushButton#Primary { background: #326f57; color: #ffffff; border-color: #326f57; font-weight: 600; }\nQPushButton#Primary:disabled { background: #e4eae6; color: #8b9891; border-color: #dce3dd; }\nQPushButton#Primary:hover { background: #255f48; }\nQPushButton#QuietButton { border: 0; background: transparent; color: #7e9285; }\nQPushButton#TextLink { text-align: left; padding: 1px 0; border: 0; background: transparent; color: #354f40; font-size: 14px; }\nQPushButton#TextLink:hover { color: #1d7b50; }\nQPushButton#FolderCard { text-align: left; background: #ffffff; border: 1px solid #e0e8e1; border-radius: 9px; padding: 15px 18px; font-size: 14px; }\nQLineEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit { background: #ffffff; border: 1px solid #dce5df; border-radius: 5px; padding: 7px; selection-background-color: #b9d8c8; }\nQLineEdit:focus, QTextEdit:focus { border-color: #79a58a; }\nQTimeEdit:disabled, QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QDateEdit:disabled, QTextEdit:disabled { background: #edf1ee; color: #9bad9f; }\nQListWidget, QTextBrowser, QTableWidget { background: #ffffff; border: 1px solid #e3eae5; border-radius: 8px; padding: 7px; }\nQListWidget::item { padding: 11px 9px; border-bottom: 1px solid #eef2ef; border-radius: 5px; }\nQListWidget::item:selected { background: #eaf3ec; color: #2d5e41; }\nQTreeWidget#WorkspaceTree { background: transparent; border: 0; }\nQTreeWidget#WorkspaceTree::item { border: 0; padding: 5px; border-radius: 5px; }\nQTreeWidget#WorkspaceTree::item:selected { background: #dcecdf; color: #24593b; }\nQTreeWidget#WorkspaceTree::item:hover { background: #e8f0e9; }\nQProgressBar { background: #dce8df; border: 0; border-radius: 3px; }\nQProgressBar::chunk { background: #4b9271; border-radius: 3px; }\nQTabWidget::pane { border: 0; background: transparent; }\nQTabBar::tab { background: #eaf0eb; color: #7b907f; padding: 10px 19px; border: 0; }\nQTabBar::tab:selected { background: #ffffff; color: #326f4e; border-bottom: 2px solid #568b69; }\nQCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #a9beb0; border-radius: 3px; background: #ffffff; }\nQCheckBox::indicator:checked { background: #397954; border-color: #397954; }\nQScrollArea { border: 0; background: transparent; }\nQScrollBar:vertical { background: transparent; width: 9px; }\nQScrollBar::handle:vertical { background: #d4dfd6; border-radius: 4px; min-height: 30px; }\nQScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }\nQStatusBar { background: #edf2ee; color: #7b8e80; font-size: 11px; }\nQSplitter::handle { background: transparent; width: 15px; }\nQMenu { background: #ffffff; border: 1px solid #dce6df; padding: 6px; }\nQMenu::item { padding: 9px 22px; }\nQMenu::item:selected { background: #eaf3ec; }\n'

_DARK_ROLES = {
    'window': 'f6f8f7', 'sidebar': 'ecf1ee eff4f0', 'surface': 'ffffff',
    'surface_alt': 'f8fbf8 edf3ef eef4ef edf2ee edf1ee eaf0eb f0f6f0 e8f0e9 eef2f4',
    'border': 'e0e7e2 e5ebe6 e7ede8 dce5df e2e8e3 dce3dd e0e8e1 e3eae5 eef2ef dce6df',
    'text': '263b3c 24453f 233e35 344e43 354f40 4b6b58 556f61 456751',
    'muted': '7a8e84 587268 809187 7d8d84 759084 658171 698270 758b94 7e9285 7b907f 7b8e80',
    'primary': '326f57 397954 397b56 4b9271 39846c 568b69 326f4e 1d7b50',
    'primary_hover': '255f48', 'selection': 'eaf3ec dcecdf b9d8c8',
    'selection_text': '215e4e 2d5e41 24593b', 'disabled_bg': 'e4eae6',
    'disabled_text': 'a7b5ad 8b9891 9bad9f c0cec5', 'focus': 'a9c2b2 79a58a a9beb0',
    'chart_track': 'dce8df', 'scrollbar': 'd4dfd6',
    'warning_bg': 'f9f2e7 fff5e8', 'warning_text': '9b6a39 937241 a18b67', 'warning_border': 'f0e4ce',
}


def current_appearance():
    app = QApplication.instance()
    return dict(getattr(app, '_management_appearance', DEFAULT_APPEARANCE)) if app else dict(DEFAULT_APPEARANCE)


def color(name):
    return (DARK if current_appearance()['theme'] == 'dark' else LIGHT)[name]


def available_font_families():
    return sorted(set(QFontDatabase.families()), key=str.casefold)


def resolved_font_family(config):
    family = config['font_family']
    families = available_font_families()
    if family in families:
        return family
    if not family and 'Microsoft YaHei UI' in families:
        return 'Microsoft YaHei UI'
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()


def stylesheet(config=None):
    config = normalize_appearance(config)
    tokens = DARK if config['theme'] == 'dark' else LIGHT
    text = _LIGHT_STYLE
    if config['theme'] == 'dark':
        mapping = {'#'+hex: tokens[role] for role, values in _DARK_ROLES.items() for hex in values.split()}
        text = re.sub(r'#[0-9a-fA-F]{6}', lambda m: mapping[m.group().lower()], text)
    family = config['font_family'].replace('\\', '\\\\').replace('"', '\\"')
    if not family:
        family = 'Microsoft YaHei UI'
    text = text.replace('"Microsoft YaHei UI", "Microsoft YaHei"', '"'+family+'"')
    text = re.sub(r'font-size: (\d+)px', lambda m: 'font-size: %dpx' % max(10, round(int(m[1])*config['font_size']/13)), text)
    text += """
QTextBrowser, QAbstractItemView { color: %(text)s; selection-color: %(selection_text)s; selection-background-color: %(selection)s; }
QComboBox QAbstractItemView { background: %(surface)s; color: %(text)s; selection-background-color: %(selection)s; selection-color: %(selection_text)s; }
QHeaderView::section { background: %(surface_alt)s; color: %(text)s; border: 1px solid %(border)s; padding: 7px; }
QToolTip { background: %(surface)s; color: %(text)s; border: 1px solid %(border)s; padding: 5px; }
QPushButton#Primary { color: %(primary_text)s; }
QPushButton#Prominent { background: %(surface)s; color: %(text)s; border: 2px solid %(primary)s; font-weight: 600; padding: 10px 18px; }
QPushButton#Prominent:hover { background: %(selection)s; }
QPushButton#HabitsSummary { text-align: left; background: %(surface_alt)s; color: %(text)s; border: 1px solid %(border)s; padding: 10px 14px; }
QPushButton#HabitsSummary:hover { border-color: %(primary)s; }
QFrame#CurrentWarningCard { background: %(warning_bg)s; border: 2px solid %(warning_border)s; border-left: 5px solid %(warning_text)s; border-radius: 10px; }
QLabel#CurrentWarningTitle { color: %(warning_text)s; font-weight: 700; }
QPushButton#TaskMark { padding: 4px; font-weight: 700; color: %(primary)s; }
QTreeView::indicator:unchecked { width: 16px; height: 16px; border: 1px solid %(muted)s; background: %(surface)s; border-radius: 3px; }
QTreeView::indicator:checked { width: 16px; height: 16px; border: 1px solid %(primary)s; background: %(primary)s; border-radius: 3px; }
QFrame#DashboardToday { background: %(selection)s; border: 2px solid %(primary)s; border-radius: 10px; }

QPushButton#Danger, QPushButton#DeleteTask { color: %(danger)s; border-color: %(danger_border)s; }
QPushButton#Danger:hover, QPushButton#DeleteTask:hover { background: %(danger_bg)s; }
QPushButton#Primary:disabled { background: %(disabled_bg)s; color: %(disabled_text)s; border-color: %(border)s; }
QFrame#ChatComposer { background: %(surface)s; border: 1px solid %(border)s; border-radius: 14px; }
QFrame#ChatMessage[userMessage="true"] { background: %(chat_user)s; border-radius: 12px; }
QFrame#ReviewItem { background: %(surface)s; border: 1px solid %(border)s; border-radius: 8px; }
QPushButton#ReviewChoice[result="done"]:checked { background: %(done)s; color: %(chart_on_color)s; border: 2px solid %(done)s; font-weight: 600; }
QPushButton#ReviewChoice[result="incomplete"]:checked { background: %(incomplete)s; color: %(chart_on_color)s; border: 2px solid %(incomplete)s; font-weight: 600; }
QPushButton#ReviewChoice[result="attended"]:checked { background: %(done)s; color: %(chart_on_color)s; border: 2px solid %(done)s; font-weight: 600; }
QPushButton#ReviewChoice[result="missed_needs_catchup"]:checked { background: %(incomplete)s; color: %(chart_on_color)s; border: 2px solid %(incomplete)s; font-weight: 600; }
QPushButton#ReviewChoice[result="absent"]:checked { background: %(danger)s; color: %(chart_on_color)s; border: 2px solid %(danger)s; font-weight: 600; }
QCalendarWidget { background: %(surface)s; border: 1px solid %(border)s; border-radius: 10px; }
QCalendarWidget QWidget#qt_calendar_navigationbar { background: %(calendar_header)s; padding: 6px; }
QCalendarWidget QToolButton { color: %(text)s; background: transparent; border: 0; border-radius: 5px; padding: 7px; font-weight: 600; }
QCalendarWidget QToolButton:hover { background: %(selection)s; }
QCalendarWidget QAbstractItemView { background: %(surface)s; color: %(text)s; selection-background-color: %(primary)s; selection-color: %(primary_text)s; outline: 0; border: 0; padding: 8px; }
QScrollBar:horizontal { background: transparent; height: 9px; }
QScrollBar::handle:horizontal { background: %(scrollbar)s; border-radius: 4px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
""" % tokens
    scale=config['font_size']/13
    text += f'\nQLabel#DashboardDate, QLabel#TodayDateHeading {{ font-size: {round(27*scale)}px; font-weight: 700; }}'
    text += f'\nQLabel#DashboardWeek {{ font-size: {round(16*scale)}px; color: {tokens["muted"]}; }}'
    text += f'\nQLabel#DashboardMetric {{ font-size: {round(24*scale)}px; font-weight: 700; }}'
    return text


def _bindings(app):
    result = []
    for owner, method in getattr(app, '_management_theme_bindings', []):
        widget = owner()
        if widget is not None and isValid(widget) and method() is not None:
            result.append((owner, method))
    app._management_theme_bindings = result
    return result


def bind_theme(widget, callback):
    """Keep no owning reference to a widget, signal slot, or anonymous closure.

    A bound method is required so its weak reference cannot retain a QWidget and
    later cause Python's cyclic collector to delete it on an I/O worker thread.
    """
    app = QApplication.instance()
    if app is None:
        return
    if QThread.currentThread() != app.thread():
        raise RuntimeError('Theme binding requires the GUI thread')
    method = weakref.WeakMethod(callback)
    _bindings(app).append((weakref.ref(widget), method))
    callback()


def apply_appearance(app, config=None):
    from .gui_gc import install_gui_gc
    install_gui_gc(app)
    if QThread.currentThread() != app.thread():
        raise RuntimeError('Applying appearance requires the GUI thread')
    config = normalize_appearance(config)
    old = getattr(app, '_management_appearance', None)
    # Sidebar persistence is not a palette change; avoid rebuilding all widgets.
    if old and all(old.get(k) == config[k] for k in ('theme','font_family','font_size')):
        app._management_appearance = dict(config)
        return config
    app._management_appearance = dict(config)
    palette = QPalette()
    for role, name in {
        QPalette.ColorRole.Window: 'window', QPalette.ColorRole.WindowText: 'text',
        QPalette.ColorRole.Base: 'surface', QPalette.ColorRole.AlternateBase: 'surface_alt',
        QPalette.ColorRole.ToolTipBase: 'surface', QPalette.ColorRole.ToolTipText: 'text',
        QPalette.ColorRole.Text: 'text', QPalette.ColorRole.Button: 'surface',
        QPalette.ColorRole.ButtonText: 'text', QPalette.ColorRole.Highlight: 'selection',
        QPalette.ColorRole.HighlightedText: 'selection_text', QPalette.ColorRole.PlaceholderText: 'muted',
        QPalette.ColorRole.Link: 'primary', QPalette.ColorRole.LinkVisited: 'other_reported',
    }.items():
        palette.setColor(role, QColor(color(name)))
    for role in (QPalette.ColorRole.Text,QPalette.ColorRole.WindowText,QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(color('disabled_text')))
    app.setPalette(palette)
    font = QFont(resolved_font_family(config))
    font.setPixelSize(config['font_size'])
    app.setFont(font)
    app.setStyleSheet(stylesheet({**config, 'font_family': font.family()}))
    for owner, method in list(_bindings(app)):
        widget, callback = owner(), method()
        if widget is not None and isValid(widget) and callback is not None:
            callback()
    for widget in app.allWidgets():
        widget.updateGeometry()
        widget.update()
    return config
