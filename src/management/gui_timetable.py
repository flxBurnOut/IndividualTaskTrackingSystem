"""Confirmed weekly timetable, source import and review before business writes."""
from __future__ import annotations

import datetime as dt
import math
import copy
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PySide6.QtCore import QDate, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QFileDialog, QMenu, QToolButton, QTabWidget,
    QScrollArea, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from .gui_calendar import install_calendar
from .gui_sources import AddSourceDialog, SourcePickerDialog, SourceContentDialog, extraction_label, label, button
from .gui_theme import bind_theme, color

WEEKDAYS = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')


def monday(value):
    day = dt.date.fromisoformat(str(value))
    return (day - dt.timedelta(days=day.weekday())).isoformat()


def clock_text(minutes):
    return f'{int(minutes) // 60:02d}:{int(minutes) % 60:02d}'


def occurrence_title(event):
    course = event.get('course_title') or event.get('owner_title')
    title = event.get('title') or '课程时段'
    return f'{course} · {title}' if course and course != title else title


def occurrence_location(event):
    return (event.get('effective') or {}).get('location') or event.get('data', {}).get('location') or ''


def timed(event):
    start, end = event.get('start_minute'), event.get('end_minute')
    return (isinstance(start, (int, float)) and not isinstance(start, bool)
            and isinstance(end, (int, float)) and not isinstance(end, bool)
            and 0 <= start < end <= 1440)


def interval_lanes(events):
    """Each overlap cluster shares a denominator; adjacent blocks need no extra lane."""
    output, cluster, endings = [], [], []
    def flush():
        for event, lane in cluster:
            output.append((event, lane, len(endings)))
    for event in sorted(events, key=lambda e: (e['start_minute'], e['end_minute'], e.get('id', ''))):
        start = event['start_minute']
        if cluster and start >= max(endings):
            flush(); cluster, endings = [], []
        lane = next((i for i, end in enumerate(endings) if end <= start), len(endings))
        if lane == len(endings): endings.append(event['end_minute'])
        else: endings[lane] = event['end_minute']
        cluster.append((event, lane))
    flush()
    return output


class EventBlock(QPushButton):
    def __init__(self, event, conflict, parent=None):
        super().__init__(parent)
        self.event_data, self.conflict = event, conflict
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        title = occurrence_title(event)
        self.time_text = f"{clock_text(event['start_minute'])}–{clock_text(event['end_minute'])}"
        self.setAccessibleName(title + '，' + self.time_text + '，编辑日程')
        details = [title, self.time_text, occurrence_location(event), '有重叠安排，请核对' if conflict else '',
                   '点击编辑原日程（重复安排会作用于整个系列）']
        self.setToolTip('\n'.join(x for x in details if x))
        self.setText(title)

    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(color('warning_bg' if self.conflict else 'selection')))
        painter.setPen(QPen(QColor(color('focus' if self.hasFocus() else 'warning_text' if self.conflict else 'border')), 2 if self.hasFocus() else 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 7, 7)
        painter.setPen(QColor(color('warning_text' if self.conflict else 'selection_text')))
        font = self.font(); font.setBold(True); painter.setFont(font)
        fm = painter.fontMetrics(); height = fm.height()
        text_box = self.rect().adjusted(8, 6, -8, -6)
        rows = max(1, text_box.height() // height)
        title = occurrence_title(self.event_data)
        # Keep actual clock times visible even when a short lesson has little vertical space.
        lines = [title, self.time_text]
        if occurrence_location(self.event_data): lines.append(occurrence_location(self.event_data))
        if rows == 1: lines = [self.time_text + ' ' + title]
        for i, text in enumerate(lines[:rows]):
            if i: font.setBold(False); painter.setFont(font)
            painter.drawText(text_box.x(), text_box.y() + height * (i + 1) - fm.descent(), fm.elidedText(text, Qt.TextElideMode.ElideRight, text_box.width()))


class WeekGrid(QWidget):
    event_clicked = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.days = []; self.blocks = []; self.conflict_ids = set()
        self.start_hour, self.end_hour = 8, 20
        self.gutter, self.header, self.hour_height = 64, 64, 72
        bind_theme(self, self.relayout)

    def set_week(self, result):
        for widget in self.blocks: widget.hide(); widget.deleteLater()
        self.blocks = []; self.days = result.get('days', [])
        self.conflict_ids = {x.get('event_id') for x in result.get('conflicts', [])}
        self.conflict_ids.update(x.get('other_event_id') for x in result.get('conflicts', []))
        events = [event for day in self.days for event in day.get('events', []) if timed(event)]
        self.start_hour = min(8, min((int(e['start_minute'] // 60) for e in events), default=8))
        self.end_hour = max(20, max((math.ceil(e['end_minute'] / 60) for e in events), default=20))
        for day_index, day in enumerate(self.days[:7]):
            for event, lane, lanes in interval_lanes([e for e in day.get('events', []) if timed(e)]):
                widget = EventBlock(event, event.get('id') in self.conflict_ids, self)
                widget.layout_position = (day_index, lane, lanes)
                widget.clicked.connect(lambda checked=False, e=event: self.event_clicked.emit(e))
                widget.show(); self.blocks.append(widget)
        self.relayout()

    def relayout(self):
        fm = self.fontMetrics()
        self.gutter = max(64, fm.horizontalAdvance('23:00') + 20)
        self.header = fm.height() * 2 + 22
        self.hour_height = max(72, fm.height() * 3 + 12)
        self.setMinimumSize(self.gutter + 7 * max(142, fm.horizontalAdvance('星期三 09/23') + 28),
                            self.header + (self.end_hour - self.start_hour) * self.hour_height + 24)
        self.place_blocks(); self.update()

    def place_blocks(self):
        width = (self.width() - self.gutter) / 7
        for widget in self.blocks:
            day, lane, lanes = widget.layout_position
            y = self.header + (widget.event_data['start_minute'] / 60 - self.start_hour) * self.hour_height
            height = (widget.event_data['end_minute'] - widget.event_data['start_minute']) / 60 * self.hour_height
            x = self.gutter + day * width + lane * width / lanes
            widget.setGeometry(round(x + 3), round(y + 2), max(20, round(width / lanes - 6)), max(self.fontMetrics().height() + 14, round(height - 4)))

    def resizeEvent(self, event):
        super().resizeEvent(event); self.place_blocks()

    def paintEvent(self, event):
        painter = QPainter(self); painter.fillRect(self.rect(), QColor(color('surface')))
        painter.fillRect(0, 0, self.width(), self.header, QColor(color('calendar_header')))
        width = (self.width() - self.gutter) / 7
        fm = painter.fontMetrics()
        for index in range(7):
            x = self.gutter + index * width
            day = self.days[index].get('date', '') if index < len(self.days) else ''
            painter.setPen(QColor(color('text')))
            painter.drawText(QRectF(x, 9, width, fm.height()), Qt.AlignmentFlag.AlignCenter, WEEKDAYS[index])
            painter.setPen(QColor(color('muted')))
            painter.drawText(QRectF(x, 13 + fm.height(), width, fm.height()), Qt.AlignmentFlag.AlignCenter, day[5:].replace('-', '/') if day else '')
            painter.setPen(QColor(color('border'))); painter.drawLine(round(x), 0, round(x), self.height())
        for hour in range(self.start_hour, self.end_hour + 1):
            y = self.header + (hour - self.start_hour) * self.hour_height
            painter.setPen(QColor(color('chart_grid'))); painter.drawLine(self.gutter, y, self.width(), y)
            painter.setPen(QColor(color('muted')))
            painter.drawText(QRectF(1, y - fm.height() / 2, self.gutter - 10, fm.height()), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f'{hour:02d}:00')


class TimetableToolButton(QToolButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        bind_theme(self, self.refresh_theme)

    def refresh_theme(self):
        self.setStyleSheet('QToolButton { background: %s; color: %s; border: 1px solid %s; border-radius: 6px; padding: 8px 13px; } QToolButton:hover { background: %s; } QToolButton::menu-button { border: 0; border-left: 1px solid %s; width: 22px; }' % (color('surface'), color('text'), color('border'), color('surface_alt'), color('border')))


class TimetableDialog(QDialog):
    def __init__(self, bridge, parent=None, on_saved=None, business_date=None, on_open_settings=None):
        super().__init__(parent)
        self.bridge, self.on_saved = bridge, on_saved
        self.on_open_settings = on_open_settings or getattr(parent, 'open_settings', None)
        self.business_date = business_date or dt.date.today().isoformat()
        self.generation = 0; self.list_generation = 0; self.dialogs = []; self.last_result = {}; self.list_offset = None
        self.capabilities = None; self.epoch = bridge.epoch
        self.setWindowTitle('每周课表'); self.resize(1210, 820)
        layout = QVBoxLayout(self); layout.setContentsMargins(22, 18, 22, 18); layout.setSpacing(12)
        top = QHBoxLayout(); top.addWidget(label('每周课表', 'DialogHeading')); top.addStretch()
        self.manage_button = button('导入课表', self.import_timetable); self.manage_button.setObjectName('Primary'); top.addWidget(self.manage_button); layout.addLayout(top)
        controls = QHBoxLayout(); self.selector = QComboBox(); self.selector.setMinimumWidth(180); self.selector.addItem('全部课表', None); self.selector.currentIndexChanged.connect(self.refresh)
        controls.addWidget(self.selector); self.table_menu = TimetableToolButton(); self.table_menu.setText('⋯'); self.table_menu.setAccessibleName('课表选项'); self.table_menu.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.table_menu); menu.addAction('管理这份课表', self.manage); menu.addAction('手动添加课程时段', self.add_manual_row); self.more_tables = menu.addAction('加载更多课表', self.load_timetables); self.more_tables.setVisible(False); self.table_menu.setMenu(menu); controls.addWidget(self.table_menu); controls.addStretch()
        controls.addWidget(button('上一周', lambda: self.shift_week(-7))); self.week_date = QDateEdit(QDate.fromString(monday(self.business_date), 'yyyy-MM-dd')); self.week_date.setDisplayFormat('yyyy/MM/dd'); install_calendar(self.week_date); controls.addWidget(self.week_date)
        controls.addWidget(button('下一周', lambda: self.shift_week(7))); controls.addWidget(button('本周', lambda: self.set_week(self.business_date))); self.week_date.dateChanged.connect(self.date_changed); layout.addLayout(controls)
        self.caption = label('正在读取已确认的课表与固定安排…', 'Quiet'); layout.addWidget(self.caption)
        self.status = label('', 'Error'); self.status.hide(); layout.addWidget(self.status)
        self.attention = label('', 'Notice'); self.attention.hide(); layout.addWidget(self.attention)
        self.details_button = button('查看冲突与待确认项', self.toggle_details); self.details_button.hide(); layout.addWidget(self.details_button)
        self.details = QListWidget(); self.details.setMaximumHeight(160); self.details.hide(); self.details.itemActivated.connect(self.open_issue); layout.addWidget(self.details)
        self.grid = WeekGrid(); self.grid.event_clicked.connect(self.edit_occurrence); scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(self.grid); layout.addWidget(scroll, 1)
        layout.addWidget(label('点击课程格子可修改；课表只提供固定框架，不会自动生成每日计划。', 'Quiet'))
        self.bridge.query('capabilities', self.loaded_capabilities, self.error)
        self.load_timetables(); self.refresh()

    def loaded_capabilities(self, result): self.capabilities = result

    def error(self, error):
        if not isValid(self): return
        self.status.setText(error.get('message', str(error)) if isinstance(error, dict) else str(error)); self.status.show()

    def set_week(self, value): self.week_date.setDate(QDate.fromString(monday(value), 'yyyy-MM-dd'))
    def shift_week(self, days): self.week_date.setDate(self.week_date.date().addDays(days))

    def date_changed(self, value):
        normal = QDate.fromString(monday(value.toString('yyyy-MM-dd')), 'yyyy-MM-dd')
        if normal != value:
            self.week_date.blockSignals(True); self.week_date.setDate(normal); self.week_date.blockSignals(False)
        self.refresh()

    def load_timetables(self, *_):
        generation = self.list_generation; epoch = self.bridge.epoch; self.more_tables.setEnabled(False)
        def loaded(result):
            if not isValid(self) or generation != self.list_generation or epoch != self.bridge.epoch: return
            existing = {self.selector.itemData(i) for i in range(self.selector.count())}
            self.selector.blockSignals(True)
            for entity in result.get('items', []):
                if entity['id'] not in existing: self.selector.addItem(entity.get('title', '课表'), entity['id'])
                else:
                    index = self.selector.findData(entity['id']); self.selector.setItemText(index, entity.get('title', '课表'))
            self.selector.blockSignals(False); self.list_offset = result.get('next_offset'); self.more_tables.setVisible(self.list_offset is not None); self.more_tables.setEnabled(True)
        self.bridge.query('timetables', loaded, self.error, limit=50, offset=self.list_offset or 0)

    def refresh(self, *_):
        self.generation += 1; generation = self.generation; epoch = self.bridge.epoch
        self.caption.setText('正在读取这一周…'); self.grid.setEnabled(False); self.status.hide()
        params = {'week_start': self.week_date.date().toString('yyyy-MM-dd')}
        if self.selector.currentData(): params['timetable_id'] = self.selector.currentData()
        def loaded(result):
            if not isValid(self) or generation != self.generation or epoch != self.bridge.epoch: return
            self.last_result = result; self.grid.set_week(result); self.grid.setEnabled(True)
            self.caption.setText(f"{result.get('week_start', params['week_start'])} 至 {result.get('week_end', '')}　·　{result.get('timezone', '时区待确认')}　·　{len(result.get('events', []))} 个已确认时段")
            self.show_issues(result)
        def failed(error):
            if isValid(self) and generation == self.generation and epoch == self.bridge.epoch: self.grid.set_week({}); self.grid.setEnabled(True); self.error(error)
        self.bridge.query('timetable_week', loaded, failed, **params)

    def show_issues(self, result):
        conflicts, unknowns = result.get('conflicts', []), result.get('unknowns', [])
        self.details.clear()
        for issue in conflicts:
            item = QListWidgetItem(f"{issue.get('date', '')}　{issue.get('message') or (issue.get('title', '安排') + '与' + issue.get('other_title', '其他安排') + '时间重叠')}")
            item.setData(Qt.ItemDataRole.UserRole, issue.get('event_id')); self.details.addItem(item)
        for issue in unknowns:
            text = (str(issue.get('title', '')) + '：' if issue.get('title') else '') + (issue.get('message') or issue.get('reason') or '仍需核对') if isinstance(issue, dict) else str(issue)
            self.details.addItem(text)
        for event in result.get('events', []):
            if timed(event): continue
            item = QListWidgetItem(f"{event.get('business_date', '')}　{occurrence_title(event)}：时间待确认，点击编辑")
            item.setData(Qt.ItemDataRole.UserRole, event.get('id')); self.details.addItem(item)
        count = self.details.count()
        coverage = result.get('coverage') or {}
        conflict_total = coverage.get('conflicts_total', len(conflicts)); unknown_total = coverage.get('unknowns_total', len(unknowns))
        text = f'{conflict_total} 处时间冲突；{unknown_total} 项待确认。' if count else ''
        if coverage.get('conflicts_complete') is False or coverage.get('unknowns_complete') is False:
            text += ' 当前仅显示部分详情；请选择单张课表缩小范围后核对。'
        if not result.get('events'): text = '这一周尚无已确认的固定时段。可上传整张课表，核对后建立每周框架。' + (' ' + text if text else '')
        self.attention.setText(text); self.attention.setVisible(bool(text)); self.details_button.setVisible(bool(count))
        if not count: self.details.hide()

    def toggle_details(self): self.details.setVisible(not self.details.isVisible())
    def open_issue(self, item):
        if item.data(Qt.ItemDataRole.UserRole): self.edit_occurrence({'id': item.data(Qt.ItemDataRole.UserRole)})

    def edit_occurrence(self, occurrence):
        # Projection fields may describe an exception date; always edit the original entity.
        from .gui_forms import EntityForm
        epoch = self.bridge.epoch
        def loaded(result):
            if not isValid(self) or epoch != self.bridge.epoch: return
            entity = result.get('entity', result)
            if entity.get('type') != 'event': return
            if entity.get('data', {}).get('timetable_id'):
                def table_loaded(result):
                    if not isValid(self) or epoch != self.bridge.epoch: return
                    if not result.get('items'): self.error('找不到这门课所属的课表。'); return
                    dialog = TimetableRowDialog(self.bridge, result['items'][0], result.get('rows', []), self, event=entity, on_saved=self.saved)
                    show_child(self, dialog)
                self.bridge.query('timetables', table_loaded, self.error, id=entity['data']['timetable_id'])
                return
            def show(capabilities):
                dialog = EntityForm(self.bridge, capabilities, self, entity=entity, on_saved=self.saved)
                dialog.setWindowTitle('编辑日程 · 重复安排作用于整个系列'); show_child(self, dialog)
            if self.capabilities: show(self.capabilities)
            else: self.bridge.query('capabilities', show, self.error)
        self.bridge.query('get', loaded, self.error, id=occurrence['id'])

    def import_timetable(self):
        show_child(self, TimetableImportDialog(self.bridge, self, self.saved, business_date=self.business_date, on_open_settings=self.on_open_settings))

    def manage(self):
        table_id = self.selector.currentData()
        if not table_id and self.selector.count() == 2: table_id = self.selector.itemData(1)
        if not table_id:
            self.error('先在表名中选择一份课表，再管理其课程时段与资料。'); return
        show_child(self, TimetableManageDialog(self.bridge, self, self.saved, table_id, self.business_date, self.on_open_settings))

    def add_manual_row(self):
        table_id = self.selector.currentData()
        if not table_id and self.selector.count() == 2: table_id = self.selector.itemData(1)
        if not table_id:
            self.error('先在表名中选择一份课表，再添加课程时段。'); return
        epoch = self.bridge.epoch
        def loaded(result):
            if not isValid(self) or epoch != self.bridge.epoch: return
            if not result.get('items'): self.error('课表已不存在，请重新读取。'); return
            show_child(self, TimetableRowDialog(self.bridge, result['items'][0], result.get('rows', []), self, on_saved=self.saved))
        self.bridge.query('timetables', loaded, self.error, id=table_id)

    def saved(self, receipt):
        self.refresh(); self.list_generation += 1; self.list_offset = None; self.load_timetables()
        if self.on_saved: self.on_saved(receipt)


def row_payload(entity):
    data = entity.get('data', {})
    result = {'key': data['timetable_row_key'], 'event_id': entity['id'], 'version': entity['version'],
              'title': entity['title'], 'weekday': data.get('weekday', dt.date.fromisoformat(data['date']).weekday()),
              'start': data.get('start'), 'end': data.get('end'),
              'enabled': data.get('timetable_enabled', data.get('enabled', True)) and entity.get('status') != 'cancelled'}
    for key in ('teaching_weeks', 'owner_id', 'location', 'event_kind', 'exceptions', 'source_text'):
        if key in data: result[key] = data[key]
    return result


def timetable_payload(entity, rows, metadata=None):
    data = dict(entity.get('data', {})); data.update(metadata or {})
    return {'id': entity['id'], 'version': entity['version'], 'title': data.get('title') or entity['title'],
            'semester_start': data.get('semester_start'), 'semester_end': data.get('semester_end'),
            'timezone': data.get('timezone'), 'source_text': data.get('source_text') or '由用户在课表管理中确认。',
            'rows': [row_payload(row) for row in rows],
            **({'week_numbering':data['week_numbering']} if 'week_numbering' in data else {}),
            **({'recess_weeks':[day for day in data['recess_weeks'] if (not data.get('semester_start') or day >= data['semester_start']) and (not data.get('semester_end') or day <= data['semester_end'])]} if 'recess_weeks' in data else {})}


class RequiredDateEdit(QDateEdit):
    """Show a real current-month calendar without accepting a guessed date."""
    def __init__(self, value, placeholder, parent=None):
        self.chosen = False; self.placeholder = placeholder
        super().__init__(value, parent)
        self.setDisplayFormat('yyyy/MM/dd'); install_calendar(self)
        self.dateChanged.connect(self.confirm_date)
        self.calendarWidget().clicked.connect(self.confirm_date)
        self.calendarWidget().activated.connect(self.confirm_date)

    def textFromDateTime(self, value):
        return super().textFromDateTime(value) if self.chosen else self.placeholder

    def confirm_date(self, value=None):
        self.chosen = True
        self.lineEdit().setText(super().textFromDateTime(self.dateTime()))


class TimetableSourceCapture(AddSourceDialog):
    """Reuse the owned-source capture protocol without a second visible form."""
    def error(self, value):
        super().error(value)
        parent = self.parentWidget()
        if parent is not None and isValid(parent): parent.capture_failed(value)


class TimetableImportDialog(QDialog):
    def __init__(self, bridge, parent=None, on_saved=None, timetable_id=None, business_date=None, on_open_settings=None):
        super().__init__(parent)
        self.bridge, self.on_saved, self.on_open_settings = bridge, on_saved, on_open_settings
        self.business_date = business_date or dt.date.today().isoformat()
        self.epoch = bridge.epoch; self.entity = None; self.dialogs = []; self.selected_sources = {}
        self.settings = None; self.pending = False; self.dirty = False; self.uncertain_stage = None; self.metadata_command = None
        self.setWindowTitle('导入课表'); self.resize(590, 380)
        outer = QVBoxLayout(self); outer.setContentsMargins(26, 22, 26, 22); outer.setSpacing(16)
        outer.addWidget(label('导入课表', 'DialogHeading'))
        outer.addWidget(label('上传课表，命名并选择适用范围。整理后核对一次即可使用。', 'Quiet'))
        self.editor = QWidget(); form = QFormLayout(self.editor); form.setContentsMargins(0, 0, 0, 0); form.setSpacing(14)
        source_box = QWidget(); source_layout = QVBoxLayout(source_box); source_layout.setContentsMargins(0, 0, 0, 0)
        self.source_button = TimetableToolButton(); self.source_button.setText('选择课表图片或 PDF…'); self.source_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.source_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup); self.source_button.clicked.connect(self.choose_source)
        source_menu = QMenu(self.source_button); source_menu.addAction('粘贴剪贴板截图', self.paste_source); self.source_button.setMenu(source_menu)
        source_layout.addWidget(self.source_button); self.source_summary = label('也可以点击右侧箭头粘贴截图。', 'Quiet'); source_layout.addWidget(self.source_summary)
        form.addRow('课表', source_box)
        self.title = QLineEdit(); self.title.setPlaceholderText('例如：本学期课表'); form.addRow('名称', self.title)
        dates = QWidget(); row = QHBoxLayout(dates); row.setContentsMargins(0, 0, 0, 0)
        self.start = RequiredDateEdit(QDate.fromString(monday(self.business_date), 'yyyy-MM-dd'), '选择起始周')
        self.end = RequiredDateEdit(QDate.fromString(self.business_date, 'yyyy-MM-dd'), '选择结束日期')
        self.start.dateChanged.connect(self.start_changed); self.start.calendarWidget().clicked.connect(self.start_changed)
        row.addWidget(self.start, 1); row.addWidget(label('至')); row.addWidget(self.end, 1); form.addRow('适用范围', dates)
        outer.addWidget(self.editor)
        self.range_hint = label('起始周从周一开始。Recess 周和时区可在设置中调整。', 'Quiet'); outer.addWidget(self.range_hint)
        self.status = label('', 'Error'); self.status.hide(); outer.addWidget(self.status)
        bottom = QHBoxLayout(); bottom.addStretch(); bottom.addWidget(button('取消', self.close)); self.organize_button = button('整理课表', self.organize); self.organize_button.setObjectName('Primary'); bottom.addWidget(self.organize_button); outer.addLayout(bottom)
        self.capture = TimetableSourceCapture(bridge, parent=self, on_saved=self.source_saved)
        self.capture.accepted.connect(self.capture_finished)
        self.title.textChanged.connect(self.mark_dirty); self.start.dateChanged.connect(self.mark_dirty); self.end.dateChanged.connect(self.mark_dirty)
        self.bridge.query('settings', self.loaded_settings, self.error)

    def mark_dirty(self, *_): self.dirty = True

    def loaded_settings(self, result):
        if isValid(self) and self.epoch == self.bridge.epoch: self.settings = copy.deepcopy(result.get('settings', {}))

    def valid_epoch(self):
        if self.epoch != self.bridge.epoch:
            self.error('数据空间已经改变，请重新打开导入窗口。'); return False
        return True

    def start_changed(self, value):
        if not self.start.chosen: return
        normalized = QDate.fromString(monday(value.toString('yyyy-MM-dd')), 'yyyy-MM-dd')
        if normalized != self.start.date(): self.start.setDate(normalized)
        self.range_hint.setText('第 1 教学周从 ' + normalized.toString('yyyy/MM/dd') + '（周一）开始。Recess 周和时区使用设置。')

    def choose_source(self):
        if self.pending or self.uncertain_stage or not self.valid_epoch(): return
        path, _ = QFileDialog.getOpenFileName(self, '选择课表', '', '图片或 PDF (*.png *.jpg *.jpeg *.webp *.bmp *.pdf);;所有文件 (*)')
        if path: self.set_source_path(path)

    def set_source_path(self, path):
        if self.pending or self.uncertain_stage or not self.valid_epoch(): return
        self.capture.cleanup_temp(); self.capture.files.clear(); self.capture.set_files([str(path)])
        self.source_summary.setText(Path(path).name); self.status.hide(); self.dirty = True
        if not self.title.text().strip(): self.title.setText(Path(path).stem)

    def paste_source(self):
        if self.pending or self.uncertain_stage or not self.valid_epoch(): return
        self._capture_error = False; self.capture.paste_image()
        if self.capture.temp_path and not self._capture_error:
            self.capture.tabs.setCurrentIndex(2); self.source_summary.setText('已选择剪贴板截图'); self.status.hide(); self.dirty = True

    def metadata(self):
        title = self.title.text().strip()
        if not title: raise ValueError('请给这份课表起一个名字。')
        if not self.start.chosen or not self.end.chosen: raise ValueError('请选择课表的起始周和结束日期。')
        start = self.start.date().toString('yyyy-MM-dd'); end = self.end.date().toString('yyyy-MM-dd')
        if end < start: raise ValueError('结束日期不能早于起始周。')
        if self.settings is None: raise ValueError('正在读取设置，请稍后再试。')
        zone = self.settings.get('timezone')
        if not zone: raise ValueError('请先在设置中确认时区。')
        defaults = self.settings.get('timetable_defaults') or {}
        return {'type':'timetable', 'title':title, 'data':{
            'semester_start':start, 'semester_end':end, 'timezone':zone,
            'week_numbering':defaults.get('week_numbering', 'teaching'),
            'recess_weeks':[day for day in defaults.get('recess_weeks', []) if start <= day <= end]}}

    def organize(self):
        if self.pending or not self.valid_epoch(): return
        if self.uncertain_stage == 'source':
            self.pending = True; self.editor.setEnabled(False); self.organize_button.setEnabled(False); self.capture.save(); return
        if self.uncertain_stage == 'metadata':
            self.pending = True; self.editor.setEnabled(False); self.organize_button.setEnabled(False); self.submit_metadata(); return
        try:
            metadata = self.metadata()
            self.capture.build_payloads()
        except ValueError as exc: self.error(str(exc)); return
        self.pending = True; self.editor.setEnabled(False); self.organize_button.setEnabled(False)
        self.status.setText('正在保存课表资料…'); self.status.show()
        if self.entity and self.entity['title'] == metadata['title'] and all(self.entity.get('data', {}).get(key) == metadata['data'][key] for key in ('semester_start', 'semester_end')):
            # Retrying an upload must preserve a reused table's own timezone and
            # recess snapshot even if the import defaults have since changed.
            self.save_source()
        else:
            # Core resolves same-name/same-range containers transactionally.
            self.metadata_command = ('create', metadata); self.submit_metadata()

    def submit_metadata(self):
        name, payload = self.metadata_command
        def saved(receipt):
            if not isValid(self): return
            if self.epoch != self.bridge.epoch:
                self.error('数据空间已经改变，请重新打开导入窗口。'); return
            entity = receipt.get('result', receipt).get('entity')
            if not entity: self.error('已保存，但课表信息尚未返回，请重新打开窗口。'); return
            self.entity = entity; self.uncertain_stage = None
            if self.on_saved: self.on_saved(receipt)
            self.save_source()
        def failed(value):
            if isinstance(value, dict) and value.get('code') == 'connection_lost': self.uncertain_stage = 'metadata'
            self.error(value)
        self.bridge.command(name, payload, saved, failed, epoch=self.epoch)

    def save_source(self):
        if not self.valid_epoch(): return
        self.capture.owner_id = self.entity['id']; self.capture.save()

    def source_saved(self, receipt):
        if not isValid(self) or self.epoch != self.bridge.epoch: return
        entity = receipt.get('result', receipt).get('entity')
        if entity: self.selected_sources = {entity['id']:entity}
        if self.on_saved: self.on_saved(receipt)

    def capture_failed(self, value):
        self._capture_error = True
        self.uncertain_stage = 'source' if isinstance(value, dict) and value.get('code') == 'connection_lost' else None
        self.error(value)

    def capture_finished(self):
        if not isValid(self): return
        self.pending = False; self.uncertain_stage = None; self.dirty = False
        self.editor.setEnabled(True); self.organize_button.setEnabled(True)
        if not self.valid_epoch(): return
        if not self.selected_sources: self.error('课表资料尚未保存，请重试。'); return
        from .gui_assistant import AssistanceDialog
        prompt = ('请根据这份完整课表建立每周课程框架。采用已确认的名称和适用范围，以及课表保存的时区、Recess 周和教学周编号规则。'
                  '逐项核对课程名称、星期、起止时间、地点和教学周，标明资料依据。模糊或缺失内容先问我，不要猜测。'
                  '使用 apply_timetable 提供可审核候选；不要生成每日计划或推断到课、完成情况。')
        owner = self.parentWidget() if self.parentWidget() is not None and hasattr(self.parentWidget(), 'dialogs') else self
        dialog = AssistanceDialog(self.bridge, owner, prompt=prompt, on_saved=self.on_saved, context_entities=[self.entity],
            scope={'kind':'timetable','entity_id':self.entity['id']}, business_date=self.business_date,
            on_open_settings=self.on_open_settings, source_ids=list(self.selected_sources), auto_send=True)
        show_child(owner, dialog)
        if owner is not self: self.accept()
        else: self.hide(); dialog.finished.connect(self.accept)

    def error(self, value):
        if not isValid(self): return
        self.pending = False; self.editor.setEnabled(not self.uncertain_stage); self.organize_button.setEnabled(True)
        self.organize_button.setText('核对并重试' if self.uncertain_stage else '整理课表')
        self.status.setText(value.get('message', str(value)) if isinstance(value, dict) else str(value)); self.status.show()

    def closeEvent(self, event):
        if self.pending: event.ignore(); return
        self.capture.cleanup_temp(); event.accept()

    def reject(self): self.close()


class TimetableManageDialog(QDialog):
    def __init__(self, bridge, parent=None, on_saved=None, timetable_id=None, business_date=None, on_open_settings=None):
        super().__init__(parent)
        self.bridge, self.on_saved, self.on_open_settings = bridge, on_saved, on_open_settings
        self.business_date = business_date or dt.date.today().isoformat()
        self.entity = None; self.rows = []; self.dialogs = []; self.generation = 0; self.source_generation = 0
        self.dirty = False; self.pending = False; self.loading = False; self.selected_sources = {}; self.source_offset = None
        self.table_offset = None; self.tables_generation = 0; self.requested_id = timetable_id; self.epoch = bridge.epoch
        self.setWindowTitle('管理课表'); self.resize(710, 590)
        outer = QVBoxLayout(self); outer.setContentsMargins(22, 18, 22, 18)
        outer.addWidget(label('管理课表', 'DialogHeading'))
        # The selected table is inherited from the week view, never another selector.
        self.selector = QComboBox(self); self.selector.addItem('新课表', None); self.selector.hide()
        self.more_tables = button('更多', self.load_tables); self.more_tables.hide()
        self.pages = QTabWidget(); outer.addWidget(self.pages, 1)
        basic = QWidget(); basic_layout = QVBoxLayout(basic); self.pages.addTab(basic, '名称与范围')
        metadata = QFormLayout(); self.title = QLineEdit(); self.title.setPlaceholderText('课表名称'); metadata.addRow('名称', self.title)
        self.start_known = QCheckBox(self); self.start_known.hide()
        self.end_known = QCheckBox(self); self.end_known.hide()
        self.start = RequiredDateEdit(QDate.fromString(monday(self.business_date), 'yyyy-MM-dd'), '选择起始周')
        self.end = RequiredDateEdit(QDate.fromString(self.business_date, 'yyyy-MM-dd'), '选择结束日期')
        for text, checkbox, editor in [('起始周周一', self.start_known, self.start), ('结束日期', self.end_known, self.end)]:
            metadata.addRow(text, editor)
            editor.dateChanged.connect(lambda value, checked=checkbox: checked.setChecked(True))
            editor.calendarWidget().clicked.connect(lambda value, checked=checkbox: checked.setChecked(True))
        # This value remains part of the preserved table snapshot, not an editor.
        self.timezone = QComboBox(self); self.timezone.setEditable(True); self.timezone.hide()
        basic_layout.addLayout(metadata)
        self.meta_hint = label('时区与周号规则保留这份课表的设置；更改默认值不会修改已有课表。', 'Quiet'); basic_layout.addWidget(self.meta_hint)
        self.calendar_details = label('', 'Quiet'); basic_layout.addWidget(self.calendar_details)
        self.calendar_settings = button('课表设置…', self.open_calendar_settings); basic_layout.addWidget(self.calendar_settings)
        basic_layout.addStretch()
        row = QHBoxLayout(); row.addStretch(); self.save_button = button('保存修改', self.save_metadata); self.save_button.setObjectName('Primary'); row.addWidget(self.save_button)
        self.reload_button = button('重新读取', self.reload); self.reload_button.hide(); row.addWidget(self.reload_button); basic_layout.addLayout(row)
        sessions = QWidget(); sessions_layout = QVBoxLayout(sessions); self.pages.addTab(sessions, '课程时段')
        sessions_layout.addWidget(label('双击修改课程系列。已停用的课程仍保留在这里，可重新启用。', 'Quiet'))
        self.row_heading = label('已登记时段', 'SectionHeading'); sessions_layout.addWidget(self.row_heading)
        self.row_list = QListWidget(); self.row_list.itemDoubleClicked.connect(self.edit_row); sessions_layout.addWidget(self.row_list, 1)
        row = QHBoxLayout(); row.addStretch(); self.add_row_button = button('添加课程时段', self.add_row); row.addWidget(self.add_row_button); sessions_layout.addLayout(row)
        sources_page = QWidget(); sources_layout = QVBoxLayout(sources_page); self.pages.addTab(sources_page, '原始资料')
        self.sources = QListWidget(); self.sources.itemDoubleClicked.connect(self.preview_source); sources_layout.addWidget(self.sources, 1)
        self.source_summary = label('选中的原始资料会用于再次整理。', 'Quiet'); sources_layout.addWidget(self.source_summary)
        row = QHBoxLayout(); self.upload_button = button('添加资料', self.upload); row.addWidget(self.upload_button)
        self.source_more = button('加载更多', self.load_sources); self.source_more.hide(); row.addWidget(self.source_more)
        self.pick_button = button('选择资料', self.pick_sources); row.addWidget(self.pick_button); row.addStretch()
        self.organize_button = button('重新整理', self.organize); self.organize_button.setObjectName('Primary'); row.addWidget(self.organize_button); sources_layout.addLayout(row)
        self.status = label('', 'Error'); self.status.hide(); outer.addWidget(self.status)
        bottom = QHBoxLayout(); bottom.addStretch(); bottom.addWidget(button('关闭', self.close)); outer.addLayout(bottom)
        for signal in (self.title.textChanged, self.start_known.toggled, self.end_known.toggled, self.start.dateChanged, self.end.dateChanged): signal.connect(self.mark_dirty)
        self.bridge.query('settings', self.loaded_settings, self.error)
        if timetable_id: self.load_table(timetable_id)

    def open_calendar_settings(self):
        if self.on_open_settings:
            try: self.on_open_settings(page='课表', timetable_id=(self.entity or {}).get('id'))
            except TypeError: self.on_open_settings()
        else: self.error('请从主窗口的设置中打开“课表”。')

    def mark_dirty(self, *_):
        if not self.loading: self.dirty = True

    def loaded_settings(self, result):
        if not isValid(self) or self.entity or self.dirty: return
        value = result.get('settings', {}).get('timezone', '')
        self.loading = True; self.timezone.setCurrentText(value); self.loading = False

    def error(self, value):
        if not isValid(self): return
        self.pending = False; self.enable_controls(True)
        self.status.setText(value.get('message', str(value)) if isinstance(value, dict) else str(value)); self.status.show()
        self.reload_button.setVisible(bool(self.entity))

    def enable_controls(self, enabled):
        for widget in (self.title, self.selector, self.start_known, self.end_known, self.timezone, self.save_button, self.upload_button, self.pick_button, self.organize_button, self.add_row_button): widget.setEnabled(enabled)
        self.start.setEnabled(enabled); self.end.setEnabled(enabled)

    def load_tables(self, *_):
        generation = self.tables_generation
        def loaded(result):
            if not isValid(self) or generation != self.tables_generation: return
            self.selector.blockSignals(True)
            for entity in result.get('items', []):
                index = self.selector.findData(entity['id'])
                if index < 0: self.selector.addItem(entity['title'], entity['id'])
                else: self.selector.setItemText(index, entity['title'])
            chosen = (self.entity or {}).get('id') or self.requested_id
            if chosen and self.selector.findData(chosen) >= 0: self.selector.setCurrentIndex(self.selector.findData(chosen))
            self.selector.blockSignals(False); self.table_offset = result.get('next_offset'); self.more_tables.setVisible(self.table_offset is not None)
        self.bridge.query('timetables', loaded, self.error, limit=50, offset=self.table_offset or 0)

    def discard_confirmed(self):
        return not self.dirty or QMessageBox.question(self, '尚未保存', '课表信息尚未保存，放弃这次修改？', QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Discard

    def selection_changed(self, *_):
        selected = self.selector.currentData()
        if not self.discard_confirmed():
            self.selector.blockSignals(True); self.selector.setCurrentIndex(max(0, self.selector.findData((self.entity or {}).get('id')))); self.selector.blockSignals(False); return
        self.requested_id = selected; self.generation += 1; self.source_generation += 1; self.selected_sources = {}; self.sources.clear(); self.rows = []
        if selected: self.load_table(selected)
        else:
            self.entity = None; self.populate({}); self.source_summary.setText('尚未选择整理资料。保存副本不代表已经理解课表。'); self.source_more.hide()

    def reload(self):
        if self.entity and self.discard_confirmed(): self.load_table(self.entity['id'])

    def load_table(self, entity_id):
        self.generation += 1; generation = self.generation; epoch = self.bridge.epoch
        self.enable_controls(False)
        def loaded(result):
            if not isValid(self) or generation != self.generation or epoch != self.bridge.epoch: return
            entities = result.get('items', [])
            if not entities: self.error('该课表已不存在。'); return
            self.epoch = epoch; self.entity = entities[0]; self.rows = result.get('rows', []); self.populate(self.entity); self.enable_controls(True); self.refresh_sources()
        self.bridge.query('timetables', loaded, self.error, id=entity_id)

    def populate(self, entity):
        self.loading = True; data = entity.get('data', {})
        self.title.setText(entity.get('title', '')); self.start_known.setChecked(bool(data.get('semester_start'))); self.end_known.setChecked(bool(data.get('semester_end')))
        if data.get('semester_start'): self.start.setDate(QDate.fromString(data['semester_start'], 'yyyy-MM-dd')); self.start.confirm_date()
        else: self.start.chosen = False; self.start.lineEdit().setText(self.start.placeholder)
        if data.get('semester_end'): self.end.setDate(QDate.fromString(data['semester_end'], 'yyyy-MM-dd')); self.end.confirm_date()
        else: self.end.chosen = False; self.end.lineEdit().setText(self.end.placeholder)
        self.timezone.setCurrentText(data.get('timezone') or self.timezone.currentText())
        self.meta_hint.setText('更改范围后会同步这份课表已有时段，停用状态和临时例外会保留。')
        mode = 'Recess 周不计入教学周' if data.get('week_numbering') == 'teaching' else '连续日历周编号'
        self.calendar_details.setText('当前：' + (data.get('timezone') or '时区待确认') + ' · ' + mode + (' · Recess：' + '、'.join(data.get('recess_weeks', [])) if data.get('recess_weeks') else ''))
        self.loading = False; self.dirty = False; self.reload_button.hide(); self.status.hide(); self.refresh_rows()

    def metadata(self, require_dates=False):
        title = self.title.text().strip()
        if not title: title = '新学期课表'
        start = self.start.date().toString('yyyy-MM-dd') if self.start_known.isChecked() else None
        end = self.end.date().toString('yyyy-MM-dd') if self.end_known.isChecked() else None
        zone = self.timezone.currentText().strip() or None
        if start and dt.date.fromisoformat(start).weekday() != 0: raise ValueError('第一教学周的起点必须是周一。')
        if start and end and end < start: raise ValueError('学期结束日期不能早于第一教学周。')
        if zone:
            try: ZoneInfo(zone)
            except (ZoneInfoNotFoundError, ValueError): raise ValueError('请填写有效时区，例如 Asia/Shanghai 或 Asia/Singapore。')
        if require_dates and not (start and end and zone): raise ValueError('生成课程时段前，请先填写并确认学期首周周一、结束日期与时区。可以先上传课表，再通过讨论补充这些信息。')
        return {'title': title, 'semester_start': start, 'semester_end': end, 'timezone': zone}

    def save_metadata(self, checked=False, after=None):
        if self.pending: return
        try: metadata = self.metadata(bool(self.rows))
        except ValueError as exc: self.error(str(exc)); return
        if self.entity and not self.dirty:
            if after: after()
            return
        self.pending = True; self.enable_controls(False); self.status.hide()
        if self.entity and self.rows:
            name = 'apply_timetable'; payload = timetable_payload(self.entity, self.rows, metadata)
        elif self.entity:
            name = 'update'; payload = {'id': self.entity['id'], 'version': self.entity['version'], 'patch': {'title': metadata.pop('title'), 'data': metadata}}
        else:
            name = 'create'; payload = {'type': 'timetable', 'title': metadata.pop('title'), 'data': {key:value for key,value in metadata.items() if value is not None}}
        generation = self.generation
        def saved(receipt):
            if not isValid(self) or generation != self.generation: return
            result = receipt.get('result', receipt); entity = result.get('entity') or result.get('timetable')
            self.pending = False; self.enable_controls(True)
            if not entity: self.error('已保存，但未返回课表信息，请重新读取。'); return
            self.entity = entity; self.requested_id = entity['id']; self.dirty = False
            if 'rows' in result: self.rows = result['rows']; self.refresh_rows()
            index = self.selector.findData(entity['id']); self.selector.blockSignals(True)
            if index < 0: self.selector.addItem(entity['title'], entity['id']); index = self.selector.count() - 1
            self.selector.setItemText(index, entity['title']); self.selector.setCurrentIndex(index); self.selector.blockSignals(False)
            if self.on_saved: self.on_saved(receipt)
            if after: after()
        self.bridge.command(name, payload, saved, self.error, epoch=self.epoch)

    def ensure_saved(self, action):
        if self.epoch != self.bridge.epoch:
            self.error('数据空间已经改变，请重新打开课表管理后操作。'); return
        if self.entity and not self.dirty: action()
        else: self.save_metadata(after=action)

    def refresh_sources(self):
        self.source_generation += 1; self.source_offset = None; self.sources.clear(); self.load_sources()

    def load_sources(self, *_):
        if not self.entity: return
        generation = self.source_generation; owner_id = self.entity['id']; self.source_more.setEnabled(False)
        def loaded(result):
            if not isValid(self) or generation != self.source_generation: return
            known = {self.sources.item(i).data(Qt.ItemDataRole.UserRole)['id'] for i in range(self.sources.count())}
            for entity in result.get('items', []):
                if entity['id'] in known: continue
                item = QListWidgetItem(entity['title'] + '\n' + extraction_label(entity)); item.setData(Qt.ItemDataRole.UserRole, entity); self.sources.addItem(item)
            self.source_offset = result.get('next_offset'); self.source_more.setVisible(self.source_offset is not None); self.source_more.setEnabled(True)
        self.bridge.query('sources', loaded, self.error, owner_id=owner_id, offset=self.source_offset or 0, limit=30)

    def source_saved(self, receipt):
        entity = receipt.get('result', receipt).get('entity')
        if entity and len(self.selected_sources) < 12: self.selected_sources[entity['id']] = entity
        elif entity: self.error('资料已保存。一次讨论最多 12 份，请在“选择整理资料”中调整。')
        self.update_source_summary(); self.refresh_sources()
        if self.on_saved: self.on_saved(receipt)

    def upload(self):
        def action():
            dialog = AddSourceDialog(self.bridge, self.entity['id'], self, self.source_saved); show_child(self, dialog)
        self.ensure_saved(action)

    def preview_source(self, item):
        dialog = SourceContentDialog(self.bridge, item.data(Qt.ItemDataRole.UserRole), self); show_child(self, dialog)

    def update_source_summary(self):
        self.source_summary.setText('本次整理参考：' + '、'.join(x.get('title', '资料') for x in self.selected_sources.values()) if self.selected_sources else '尚未选择整理资料。可点击“选择整理资料”勾选；一次最多 12 份。')

    def pick_sources(self, after=None):
        def action():
            dialog = SourcePickerDialog(self.bridge, self.entity['id'], self, list(self.selected_sources.values()))
            def chosen():
                self.selected_sources = dict(dialog.selected); self.update_source_summary()
                if callable(after): after()
            dialog.accepted.connect(chosen); show_child(self, dialog)
        self.ensure_saved(action)

    def organize(self):
        def action():
            if not self.selected_sources: self.pick_sources(self.open_assistance)
            else: self.open_assistance()
        self.ensure_saved(action)

    def open_assistance(self):
        if self.epoch != self.bridge.epoch:
            self.error('数据空间已经改变，请重新打开课表管理。'); return
        if not self.selected_sources: self.error('先选择至少一份课表资料。'); return
        from .gui_assistant import AssistanceDialog
        prompt = ('请整理这份完整课表，允许跨多门课程。逐项核对课程名称、星期、起止时间、地点、教学周与临时例外，并标明资料依据和仍待确认的信息。'
                  '教学周按这份课表保存的编号规则与 Recess 周计算，勿猜测缺失日期或擅自改变规则。'
                  '检查与已登记固定安排的冲突；使用 apply_timetable 提供可审核的每周框架候选。不要生成每日计划或推断到课、完成情况。')
        dialog = AssistanceDialog(self.bridge, self, prompt=prompt, on_saved=self.assistance_saved, context_entities=[self.entity],
                                  scope={'kind':'timetable', 'entity_id':self.entity['id']}, business_date=self.business_date,
                                  on_open_settings=self.on_open_settings, source_ids=list(self.selected_sources), auto_send=True)
        show_child(self, dialog)

    def assistance_saved(self, receipt):
        if self.on_saved: self.on_saved(receipt)
        # Source refresh is safe while metadata is being edited; preserve that draft.
        if not self.dirty: self.load_table(self.entity['id'])
        else: self.refresh_sources(); self.reload_button.show()

    def refresh_rows(self):
        self.row_list.clear()
        for entity in self.rows:
            data = entity.get('data', {}); enabled = data.get('timetable_enabled', True) and entity.get('status') != 'cancelled'
            weekday = dt.date.fromisoformat(data['date']).weekday() if data.get('date') else 0
            weeks = data.get('teaching_weeks')
            detail = f"{WEEKDAYS[weekday]} {data.get('start', '?')}–{data.get('end', '?')} · " + ('每周' if weeks is None else '第 ' + ','.join(map(str, weeks)) + ' 周')
            item = QListWidgetItem(entity['title'] + (' · 已停用' if not enabled else '') + '\n' + detail)
            item.setData(Qt.ItemDataRole.UserRole, entity); self.row_list.addItem(item)
        self.row_list.setVisible(bool(self.rows)); self.row_heading.setVisible(bool(self.rows))

    def edit_row(self, item):
        entity_id = item.data(Qt.ItemDataRole.UserRole)['id']
        def action():
            current = next((row for row in self.rows if row['id'] == entity_id), None)
            if current:
                dialog = TimetableRowDialog(self.bridge, self.entity, self.rows, self, event=current, on_saved=self.assistance_saved)
                show_child(self, dialog)
        self.ensure_saved(action)

    def add_row(self):
        try: self.metadata(True)
        except ValueError as exc: self.error(str(exc)); return
        def action():
            dialog = TimetableRowDialog(self.bridge, self.entity, self.rows, self, on_saved=self.assistance_saved)
            show_child(self, dialog)
        self.ensure_saved(action)

    def closeEvent(self, event):
        if self.pending or not self.discard_confirmed(): event.ignore(); return
        self.generation += 1; self.source_generation += 1; event.accept()

    def reject(self): self.close()


def parse_weeks(text):
    text = text.strip().replace('，', ',').replace('、', ',').replace('–', '-').replace('—', '-')
    if not text: return None
    result = set()
    for token in text.split(','):
        token = token.strip()
        if not token: raise ValueError('教学周请写成 1-6,8,10-14，或留空表示学期内每周。')
        try:
            if '-' in token:
                parts = token.split('-')
                if len(parts) != 2: raise ValueError()
                start, end = map(int, parts)
                if not 1 <= start <= end <= 52: raise ValueError()
                result.update(range(start, end + 1))
            else:
                number = int(token)
                if not 1 <= number <= 52: raise ValueError()
                result.add(number)
        except ValueError: raise ValueError('教学周须在 1–52 之间，例如 1-6,8,10-14。')
    return sorted(result)


class TimetableRowDialog(QDialog):
    def __init__(self, bridge, timetable, rows, parent=None, event=None, on_saved=None):
        super().__init__(parent)
        import copy
        import uuid
        from PySide6.QtCore import QTime
        from PySide6.QtWidgets import QTimeEdit
        self.bridge, self.timetable, self.rows, self.event_data, self.on_saved = bridge, timetable, rows, event, on_saved
        self.pending = False; self.dirty = False; self.dialogs = []; self.epoch = bridge.epoch
        data = (event or {}).get('data', {}); self.owner_id = data.get('owner_id'); self.exceptions = copy.deepcopy(data.get('exceptions') or {})
        self.key = data.get('timetable_row_key') or 'manual-' + uuid.uuid4().hex[:16]
        self.setWindowTitle('编辑课程时段' if event else '添加课程时段'); self.resize(690, 710)
        outer = QVBoxLayout(self); outer.addWidget(label(self.windowTitle(), 'DialogHeading'))
        outer.addWidget(label('修改整个课程系列；单次停课或调课在下方“临时例外”中设置。保存后不会生成每日计划。', 'Quiet'))
        scroll = QScrollArea(); scroll.setWidgetResizable(True); body = QWidget(); layout = QVBoxLayout(body); scroll.setWidget(body); outer.addWidget(scroll, 1); self.editor_body = body
        form = QFormLayout(); self.title = QLineEdit((event or {}).get('title', '')); self.title.setPlaceholderText('例如：数据库 · Lecture'); form.addRow('课程时段名称', self.title)
        self.weekday = QComboBox(); self.weekday.addItems(WEEKDAYS)
        if data.get('date'): self.weekday.setCurrentIndex(dt.date.fromisoformat(data['date']).weekday())
        form.addRow('星期', self.weekday)
        self.event_kind = QComboBox()
        for title, value in [('讲座 / Lecture', 'lecture'), ('习题课 / Tutorial', 'tutorial'), ('实验 / Lab', 'lab'), ('其他课程安排', 'other')]: self.event_kind.addItem(title, value)
        original_kind = data.get('event_kind', 'lecture')
        if self.event_kind.findData(original_kind) < 0: self.event_kind.addItem('其他（保留原类别）', original_kind)
        self.event_kind.setCurrentIndex(self.event_kind.findData(original_kind)); form.addRow('课程类型', self.event_kind)
        times = QWidget(); time_row = QHBoxLayout(times); time_row.setContentsMargins(0, 0, 0, 0)
        self.start = QTimeEdit(QTime.fromString(data.get('start') or '09:00', 'HH:mm')); self.end = QTimeEdit(QTime.fromString(data.get('end') or '10:00', 'HH:mm'))
        self.start.setDisplayFormat('HH:mm'); self.end.setDisplayFormat('HH:mm'); time_row.addWidget(self.start); time_row.addWidget(label('至')); time_row.addWidget(self.end); form.addRow('起止时间', times)
        self.weeks = QLineEdit(','.join(str(x) for x in data.get('teaching_weeks', []) or [])); self.weeks.setPlaceholderText('留空为每周，例如 1-6,8,10-14'); form.addRow('教学周', self.weeks)
        self.location = QLineEdit(data.get('location', '')); form.addRow('地点（可留空）', self.location)
        owner = QWidget(); owner_row = QHBoxLayout(owner); owner_row.setContentsMargins(0, 0, 0, 0)
        self.owner_button = button('选择关联课程（可留空）', self.pick_owner); owner_row.addWidget(self.owner_button, 1); owner_row.addWidget(button('清除', self.clear_owner)); form.addRow('关联课程', owner)
        self.enabled = QCheckBox('启用这个系列'); self.enabled.setChecked(data.get('timetable_enabled', data.get('enabled', True)) and (event or {}).get('status') != 'cancelled'); form.addRow('', self.enabled); layout.addLayout(form)
        layout.addWidget(label('第 1 周从课表起始周开始。' + ('Recess 周不计入教学周。' if timetable.get('data', {}).get('week_numbering') == 'teaching' else '此表沿用连续日历周编号。') + '停用系列会保留历史记录。', 'Quiet'))
        self.exception_toggle = QCheckBox('临时例外：单次停课或改时间'); layout.addWidget(self.exception_toggle)
        self.exception_box = QWidget(); exceptions_layout = QVBoxLayout(self.exception_box); exceptions_layout.setContentsMargins(0, 0, 0, 0)
        self.exception_list = QListWidget(); self.exception_list.setMaximumHeight(115); exceptions_layout.addWidget(self.exception_list)
        exception_fields = QHBoxLayout(); self.exception_date = QDateEdit(QDate.fromString(timetable['data'].get('semester_start') or dt.date.today().isoformat(), 'yyyy-MM-dd')); self.exception_date.setDisplayFormat('yyyy/MM/dd'); install_calendar(self.exception_date); exception_fields.addWidget(self.exception_date)
        self.cancelled = QCheckBox('当次停课'); self.cancelled.setChecked(True); exception_fields.addWidget(self.cancelled)
        self.exception_start = QTimeEdit(self.start.time()); self.exception_end = QTimeEdit(self.end.time())
        for widget in (self.exception_start, self.exception_end): widget.setDisplayFormat('HH:mm'); widget.setEnabled(False); exception_fields.addWidget(widget)
        self.cancelled.toggled.connect(lambda checked: (self.exception_start.setEnabled(not checked), self.exception_end.setEnabled(not checked)))
        exceptions_layout.addLayout(exception_fields); buttons = QHBoxLayout(); buttons.addWidget(button('加入 / 更新这次例外', self.add_exception)); buttons.addWidget(button('移除选中例外', self.remove_exception)); buttons.addStretch(); exceptions_layout.addLayout(buttons)
        layout.addWidget(self.exception_box); self.exception_box.hide(); self.exception_toggle.toggled.connect(self.exception_box.setVisible); self.refresh_exceptions()
        self.status = label('', 'Error'); self.status.hide(); outer.addWidget(self.status)
        controls = QHBoxLayout(); controls.addStretch(); controls.addWidget(button('取消', self.close)); self.save_button = button('保存课程时段', self.save); self.save_button.setObjectName('Primary'); controls.addWidget(self.save_button); outer.addLayout(controls)
        for signal in (self.title.textChanged, self.weekday.currentIndexChanged, self.event_kind.currentIndexChanged, self.start.timeChanged, self.end.timeChanged, self.weeks.textChanged, self.location.textChanged, self.enabled.toggled): signal.connect(self.mark_dirty)
        if self.owner_id:
            def owner_loaded(result):
                if isValid(self) and self.owner_id == data.get('owner_id'): self.owner_button.setText(result.get('entity', {}).get('title', '已关联课程'))
            self.bridge.query('get', owner_loaded, lambda e: None, id=self.owner_id)

    def mark_dirty(self, *_): self.dirty = True
    def error(self, value):
        if not isValid(self): return
        self.pending = False; self.editor_body.setEnabled(True); self.save_button.setEnabled(True); self.status.setText(value.get('message', str(value)) if isinstance(value, dict) else str(value)); self.status.show()

    def clear_owner(self): self.owner_id = None; self.owner_button.setText('选择关联课程（可留空）'); self.dirty = True

    def pick_owner(self):
        from .gui_forms import EntityPicker
        dialog = EntityPicker(self.bridge, self, allowed_types=['course'])
        def chosen():
            if dialog.selected:
                self.owner_id = dialog.selected['id']; self.owner_button.setText(dialog.selected['title']); self.dirty = True
        dialog.accepted.connect(chosen); show_child(self, dialog)

    def refresh_exceptions(self):
        self.exception_list.clear()
        for date, value in sorted(self.exceptions.items()):
            item = QListWidgetItem(date + '　' + ('停课' if value.get('cancelled') else f"{value.get('start', '原时间')}–{value.get('end', '原时间')}")); item.setData(Qt.ItemDataRole.UserRole, date); self.exception_list.addItem(item)

    def add_exception(self):
        day = self.exception_date.date().toString('yyyy-MM-dd')
        start, end = self.timetable['data'].get('semester_start'), self.timetable['data'].get('semester_end')
        if start and end and not start <= day <= end: self.error('例外日期须在本学期范围内。'); return
        if dt.date.fromisoformat(day).weekday() != self.weekday.currentIndex(): self.error('例外日期须与这个系列的星期一致。若整门课改到其他星期，请修改系列星期。'); return
        value = {'cancelled': self.cancelled.isChecked()}
        if not value['cancelled']:
            value.update(start=self.exception_start.time().toString('HH:mm'), end=self.exception_end.time().toString('HH:mm'))
            if value['start'] == value['end']: self.error('调课开始与结束时间不能相同。'); return
        self.exceptions[day] = value; self.dirty = True; self.refresh_exceptions(); self.status.hide()

    def remove_exception(self):
        item = self.exception_list.currentItem()
        if item: self.exceptions.pop(item.data(Qt.ItemDataRole.UserRole), None); self.dirty = True; self.refresh_exceptions()

    def build_payload(self):
        title = self.title.text().strip()
        if not title: raise ValueError('请填写课程时段名称。')
        start, end = self.start.time().toString('HH:mm'), self.end.time().toString('HH:mm')
        if start == end: raise ValueError('开始与结束时间不能相同。')
        weeks = parse_weeks(self.weeks.text())
        row = row_payload(self.event_data) if self.event_data else {'key': self.key}
        row.update(title=title, weekday=self.weekday.currentIndex(), start=start, end=end, teaching_weeks=weeks,
                   event_kind=self.event_kind.currentData(), location=self.location.text().strip(), owner_id=self.owner_id, enabled=self.enabled.isChecked(), exceptions=self.exceptions)
        row['remove_exceptions'] = sorted(set((self.event_data or {}).get('data', {}).get('exceptions', {})) - set(self.exceptions))
        row['source_text'] = (self.event_data or {}).get('data', {}).get('source_text') or '用户在课表中手动确认的课程时段。'
        payload = timetable_payload(self.timetable, self.rows)
        payload['rows'] = [row if old['key'] == self.key else old for old in payload['rows']]
        if not any(old['key'] == self.key for old in payload['rows']): payload['rows'].append(row)
        return payload

    def save(self):
        if self.pending: return
        try: payload = self.build_payload()
        except ValueError as exc: self.error(str(exc)); return
        self.pending = True; self.editor_body.setEnabled(False); self.save_button.setEnabled(False); self.status.hide()
        def saved(receipt):
            if not isValid(self): return
            self.pending = False; self.dirty = False
            if self.on_saved: self.on_saved(receipt)
            self.accept()
        self.bridge.command('apply_timetable', payload, saved, self.error, epoch=self.epoch)

    def closeEvent(self, event):
        if self.pending: event.ignore(); return
        if self.dirty and QMessageBox.question(self, '尚未保存', '放弃这次课程时段修改？', QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Discard:
            event.ignore(); return
        event.accept()

    def reject(self): self.close()


def show_child(owner, dialog):
    """Release closed windows on the GUI thread, while keeping async callbacks valid."""
    owner.dialogs.append(dialog)
    def finished(_):
        if dialog in owner.dialogs: owner.dialogs.remove(dialog)
        dialog.deleteLater()
    dialog.finished.connect(finished)
    dialog.open()
    return dialog
