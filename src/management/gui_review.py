"""Focused daily confirmation and structured weekly review via the shared service."""
from __future__ import annotations

import copy
import datetime as dt
import json

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QDateEdit, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QTextEdit,
    QLayout, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)
from .gui_charts import CoverageChart, ORIGINAL_LABELS, WeekDaysChart
from .gui_calendar import install_calendar
from .gui_forms import EntityPicker, FeedbackDialog, FormDialog, FIELD_LABELS
from .chart_preferences import normalize_chart_preferences
from .gui_layout import ActionRow
from .gui_materials import AnimatedTabWidget, TabTransition
from .gui_theme import bind_theme


class _WeekHeading(QLabel):
    """Prefer an unbroken date range without imposing a minimum row width."""
    def sizeHint(self):
        hint = super().sizeHint()
        if self.wordWrap():
            margins = self.contentsMargins()
            text_width = max(self.fontMetrics().horizontalAdvance(self.text()),
                             self.fontMetrics().boundingRect(self.text()).width())
            width = max(hint.width(), text_width + margins.left() + margins.right()
                        + 2 * self.margin() + max(0, self.indent()) + 2)
            hint.setWidth(width)
            hint.setHeight(max(0, super().heightForWidth(width)))
        return hint


class ActualFeedbackDialog(FeedbackDialog):
    """The existing feedback command with a dated entry and draft protection."""
    def __init__(self, bridge, target, date, parent=None, on_saved=None):
        self.saving = self.dirty = False
        def saved(receipt):
            self.saving = self.dirty = False
            if on_saved:
                on_saved(receipt)
        super().__init__(bridge, target, parent, on_saved=saved)
        self.date.setDate(QDate.fromString(date, 'yyyy-MM-dd'))
        self.date.dateChanged.connect(self._changed)
        self.source.textChanged.connect(self._changed)
        for box in self.editors.values():
            box.currentIndexChanged.connect(self._changed)
        self.minutes.enabled.toggled.connect(self._changed)
        self.minutes.editor.valueChanged.connect(self._changed)

    def _changed(self, *_):
        self.dirty = True

    def save(self):
        if not self.saving:
            super().save()

    def busy(self):
        self.saving = True
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(False)
        super().busy()

    def error(self, error):
        self.saving = False
        super().error(error)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(True)

    def reject(self):
        if self.saving:
            return
        if self.dirty and QMessageBox.question(self, '尚未保存', '放弃这次尚未保存的执行反馈？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        super().reject()

    def closeEvent(self, event):
        if self.saving:
            event.ignore()
        else:
            super().closeEvent(event)


class ActualFeedbackHistoryDialog(QDialog):
    """Read back all dated statements, including superseded ones, without mutations."""
    VALUE_LABELS = {
        'done': '已完成', 'incomplete': '未完成', 'not_started': '未开始', 'partial': '部分完成',
        'blocked': '受阻', 'attended': '已到场', 'absent': '未到场', 'cancelled': '已取消',
        'asynchronous': '异步替代', 'online_replacement': '在线替代', 'not_viewed': '未观看',
        'viewed': '已看完', 'not_submitted': '未提交', 'submitted': '已提交', 'accepted': '已验收',
        'rejected': '被退回', 'not_tested': '尚未测验', 'needs_review': '需要复习',
        'verified': '已验证掌握', 'unknown': '待确认',
    }

    def __init__(self, bridge, date, parent=None):
        super().__init__(parent)
        self.bridge, self.date = bridge, date
        self.offset, self.next_offset, self.generation = 0, None, 0
        self.closed = False
        self.finished.connect(lambda *_: setattr(self, 'closed', True))
        self.destroyed.connect(lambda *_: setattr(self, 'closed', True))
        self.setWindowTitle('已保存实际记录 · ' + date)
        self.resize(660, 630)
        layout = QVBoxLayout(self)
        heading = QLabel(self.windowTitle())
        heading.setObjectName('DialogHeading')
        layout.addWidget(heading)
        hint = QLabel('按保存先后展示这一天的原始反馈，更正前的记录也保留。未计划事项不会加入原计划完成率；每条历史记录不一定代表当前状态。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.history = QListWidget()
        self.history.currentItemChanged.connect(self.show_record)
        layout.addWidget(self.history, 1)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 2)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.previous = QPushButton('上一页')
        self.previous.clicked.connect(lambda: self.load(max(0, self.offset - 30)))
        self.next = QPushButton('下一页')
        self.next.clicked.connect(lambda: self.load(self.next_offset))
        self.refresh = QPushButton('刷新')
        self.refresh.clicked.connect(lambda: self.load(0))
        for button in (self.previous, self.next, self.refresh):
            actions.addWidget(button)
        actions.addStretch()
        close = QPushButton('关闭')
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.load(0)

    @classmethod
    def dimension_text(cls, dimensions):
        return '\n'.join(FIELD_LABELS.get(key, key) + '：' +
                         (str(value) + ' 分钟' if key == 'actual_minutes' else
                          '部分观看' if key == 'viewing' and value == 'partial' else
                          cls.VALUE_LABELS.get(str(value), str(value)))
                         for key, value in dimensions.items())

    def show_record(self, item, previous=None):
        if item is None:
            self.details.clear()
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        note = '\n原事项已删除，历史反馈仍保留。' if row.get('target_archived') else ''
        if not row.get('target_available', True):
            note += '\n原事项暂不可用，以下为原始反馈。'
        if row.get('supersedes_id'):
            note += '\n这是一条更正记录，原反馈仍保留。'
        text = row['target_title'] + '\n归属日期：' + row['business_date'] + note
        text += '\n\n' + self.dimension_text(row['dimensions'])
        text += '\n\n原始反馈：\n' + row['source_text']
        self.details.setPlainText(text)

    def load(self, offset):
        if offset is None or self.closed:
            return
        self.generation += 1
        generation = self.generation
        self.previous.setEnabled(False)
        self.next.setEnabled(False)
        self.status.setText('正在读取已保存的实际记录…')
        def loaded(result):
            if self.closed or generation != self.generation or result.get('date') != self.date:
                return
            self.offset, self.next_offset = offset, result.get('next_offset')
            self.history.clear()
            for row in result.get('items', []):
                preview = self.dimension_text(row['dimensions']).replace('\n', ' · ')
                item = QListWidgetItem(row['target_title'] + '\n' + preview)
                item.setData(Qt.ItemDataRole.UserRole, row)
                self.history.addItem(item)
            self.previous.setEnabled(self.offset > 0)
            self.next.setEnabled(self.next_offset is not None)
            total = result.get('total', self.history.count())
            self.status.setText(f'共 {total} 条原始反馈 · 当前第 {offset + 1}–{offset + self.history.count()} 条' if total else '这一天尚未保存实际记录。可回到复盘页选择“记录实际情况”。')
            if self.history.count():
                self.history.setCurrentRow(0)
        def failed(error):
            if not self.closed and generation == self.generation:
                self.status.setText('读取失败，已有记录显示保留。请点击刷新重试。' + ReviewPage._error_text(error))
                self.previous.setEnabled(self.offset > 0)
                self.next.setEnabled(self.next_offset is not None)
        self.bridge.query('actual_feedback', loaded, failed, date=self.date, limit=30, offset=offset)


class ReviewNotesDialog(FormDialog):
    """Append-only dated notes; never invent a plan or completion measurements."""
    def __init__(self, bridge, date, parent=None, on_saved=None):
        super().__init__('文字小结 · ' + date, parent)
        self.bridge, self.date, self.on_saved = bridge, date, on_saved
        self.epoch = bridge.epoch
        self.saving = self.dirty = self.dead = False
        self.generation, self.offset, self.next_offset = 0, 0, None
        self.destroyed.connect(lambda *_: setattr(self, 'dead', True))
        hint = QLabel('写下当天发生的事情、感受或下一步想法。文字小结不会自动修改任务状态、补造计划或推算完成率。')
        hint.setWordWrap(True)
        self.body_layout.addWidget(hint)
        self.text = QTextEdit()
        self.text.setAcceptRichText(False)
        self.text.setPlaceholderText('今天实际发生了什么？有哪些需要以后调整的地方？')
        self.text.textChanged.connect(self._changed)
        self.body_layout.addWidget(self.text)
        self.body_layout.addWidget(QLabel('这一天已保存的小结（每次保存保留为独立记录）'))
        self.history = QListWidget()
        self.history.setMaximumHeight(120)
        self.history.currentItemChanged.connect(self._show_note)
        self.body_layout.addWidget(self.history)
        self.saved_text = QTextEdit()
        self.saved_text.setReadOnly(True)
        self.saved_text.setMaximumHeight(130)
        self.body_layout.addWidget(self.saved_text)
        self.history_hint = QLabel()
        self.history_hint.setWordWrap(True)
        self.body_layout.addWidget(self.history_hint)
        self.more = QPushButton('继续查找较早的小结')
        self.more.clicked.connect(self.load_more)
        self.body_layout.addWidget(self.more)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存这条小结')
        self.buttons.accepted.connect(self.save)
        self.load()

    def _changed(self):
        self.dirty = True

    def _show_note(self, item, previous=None):
        self.saved_text.setPlainText(item.data(Qt.ItemDataRole.UserRole) if item else '')

    def load_more(self):
        if self.next_offset is not None:
            self.offset = self.next_offset
            self.load()

    def load(self):
        self.generation += 1
        generation = self.generation
        self.more.setEnabled(False)
        def loaded(result):
            if self.dead or generation != self.generation:
                return
            for entity in result.get('items', []):
                data = entity.get('data') or {}
                if data.get('start') != self.date or data.get('end') != self.date or not data.get('content'):
                    continue
                preview = str(data['content']).splitlines()[0][:70]
                item = QListWidgetItem(entity['title'] + '\n' + preview)
                item.setData(Qt.ItemDataRole.UserRole, str(data['content']))
                self.history.addItem(item)
            self.next_offset = result.get('next_offset')
            self.more.setText('继续查找较早的小结')
            self.more.setVisible(self.next_offset is not None)
            self.more.setEnabled(self.next_offset is not None)
            self.history_hint.setText(f'已找到 {self.history.count()} 条当天小结。' + ('仍有较早记录可继续查找。' if self.next_offset is not None else ''))
            if self.history.count() and self.history.currentRow() < 0:
                self.history.setCurrentRow(0)
        def failed(error):
            if self.dead or generation != self.generation:
                return
            self.history_hint.setText('历史小结读取失败：' + ReviewPage._error_text(error))
            self.more.setText('重试读取历史小结')
            self.next_offset = self.offset
            self.more.setEnabled(True)
            self.more.show()
        self.bridge.query('list', loaded, failed, type='review', search=self.date, limit=30, offset=self.offset)

    def save(self):
        if self.saving:
            return
        text = self.text.toPlainText().strip()
        if not text:
            self.error('请先填写当天的小结。')
            return
        self.saving = True
        self.busy()
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(False)
        def saved(receipt):
            if self.dead:
                return
            self.saving = self.dirty = False
            self.accept()
            if self.on_saved:
                self.on_saved(receipt)
        def failed(error):
            if self.dead:
                return
            self.saving = False
            self.error(error)
            self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(True)
        self.bridge.command('save_review', {'start': self.date, 'end': self.date,
            'title': self.date + ' · 文字小结', 'text': text}, saved, failed, epoch=self.epoch)

    def reject(self):
        if self.saving:
            return
        if self.dirty and QMessageBox.question(self, '尚未保存', '放弃这条尚未保存的文字小结？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        super().reject()

    def closeEvent(self, event):
        if self.saving:
            event.ignore()
        else:
            super().closeEvent(event)


class ReviewPage(QWidget):
    has_pending = Signal(bool)
    chart_settings_requested = Signal(str)

    def __init__(self, bridge, parent=None, on_changed=None, on_codex=None, on_chart_settings=None):
        super().__init__(parent)
        self.bridge, self.on_changed, self.on_codex = bridge, on_changed, on_codex
        self._date = QDate.currentDate().toString('yyyy-MM-dd')
        self._states, self._choices, self._latest = {}, {}, {}
        self._conflicts = set()
        self._generation, self._week_generation = 0, 0
        self._submitting_date = None
        self._pending = False
        self.item_buttons, self.item_labels = {}, {}
        self.daily_data, self.weekly_data = None, None
        self.dialogs = []
        self._assistants_visible = True
        self.chart_preferences = normalize_chart_preferences()
        if on_chart_settings:
            self.chart_settings_requested.connect(on_chart_settings)
        self._build()
        self.tab_transition = TabTransition(self.tabs)
        bind_theme(self, self.apply_visual_style)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.header_actions = ActionRow()
        top = self.header_actions
        self.header_title = QLabel('复盘')
        self.header_title.setObjectName('DialogHeading')
        top.addWidget(self.header_title)
        self.date_editor = QDateEdit(QDate.fromString(self._date, 'yyyy-MM-dd'))
        install_calendar(self.date_editor)
        self.date_editor.setDisplayFormat('yyyy-MM-dd')
        self.date_editor.setAccessibleName('复盘日期')
        self.date_editor.dateChanged.connect(lambda date: self.set_date(date.toString('yyyy-MM-dd')))
        self.previous_date = QPushButton('‹')
        self.next_date = QPushButton('›')
        for button,step,title in ((self.previous_date,-1,'上一天'),(self.next_date,1,'下一天')):
            button.setAccessibleName(title);button.setToolTip(title)
            button.setMinimumWidth(36)
            button.setStyleSheet('QPushButton { padding: 8px 10px; }')
            button.clicked.connect(lambda checked=False,amount=step:self.date_editor.setDate(self.date_editor.date().addDays(amount)))
        self.date_navigation = QWidget()
        navigation = QHBoxLayout(self.date_navigation)
        navigation.setContentsMargins(0, 0, 0, 0)
        navigation.addWidget(self.previous_date)
        navigation.addWidget(self.date_editor)
        navigation.addWidget(self.next_date)
        top.addWidget(self.date_navigation)
        self.refresh_button = QPushButton('刷新')
        self.refresh_button.clicked.connect(lambda: self.refresh())
        top.addWidget(self.refresh_button)
        layout.addWidget(self.header_actions)
        self.tabs = AnimatedTabWidget()
        self.daily_tab, self.weekly_tab = QWidget(), QWidget()
        self.tabs.addTab(self.daily_tab, '每日复盘')
        self.tabs.addTab(self.weekly_tab, '每周回顾')
        layout.addWidget(self.tabs, 1)

        self.day_tab_layout = QVBoxLayout(self.daily_tab)
        self.day_tab_layout.setContentsMargins(0, 0, 0, 0)
        self.daily_body = QWidget()
        day_layout = self.day_layout = QVBoxLayout(self.daily_body)
        day_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.daily_heading = QLabel('正在等待读取计划')
        self.daily_heading.setWordWrap(True)
        day_layout.addWidget(self.daily_heading)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setObjectName('Notice')
        day_layout.addWidget(self.notice)
        self.daily_actions = ActionRow(self.daily_body)
        manual_actions = self.daily_actions
        self.feedback_button = QPushButton('记录实际情况')
        self.feedback_button.setObjectName('Primary')
        self.feedback_button.clicked.connect(self._record_actual)
        manual_actions.addWidget(self.feedback_button)
        self.notes_button = QPushButton('写小结 / 查看已保存小结')
        self.notes_button.clicked.connect(self._open_notes)
        manual_actions.addWidget(self.notes_button)
        self.feedback_history_button = QPushButton('查看已保存实际记录')
        self.feedback_history_button.clicked.connect(self._open_actual_history)
        manual_actions.addWidget(self.feedback_history_button)
        self.codex_button = QPushButton('请助手协助复盘')
        self.codex_button.clicked.connect(self._ask_codex)
        manual_actions.addWidget(self.codex_button)
        for button in (self.notes_button, self.feedback_history_button, self.codex_button):
            button.setObjectName('QuietButton')
        day_layout.addWidget(self.daily_actions)
        self.daily_chart = CoverageChart()
        self.daily_chart_button = self._chart_settings_button('review_daily_style')
        self.daily_chart_tools = ActionRow(self.daily_body)
        self.daily_chart_title = QLabel('已保存的实际结果')
        self.daily_chart_title.setObjectName('SectionHeading')
        self.daily_chart_tools.addWidget(self.daily_chart_title)
        self.daily_chart_tools.addWidget(self.daily_chart_button)
        day_layout.addWidget(self.daily_chart_tools)
        day_layout.addWidget(self.daily_chart)
        self.saved_hint = QLabel('上图是已保存的反馈。未选择的项目保持未反馈，不会自动算作未完成。')
        self.saved_hint.setWordWrap(True)
        self.saved_hint.setObjectName('Hint')
        day_layout.addWidget(self.saved_hint)
        self.rows = QWidget()
        self.rows_layout = QVBoxLayout(self.rows)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.addStretch()
        day_layout.addWidget(self.rows)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setObjectName('Error')
        day_layout.addWidget(self.message)
        self.pending_label = QLabel()
        self.pending_label.setWordWrap(True)
        self.pending_label.setObjectName('Hint')
        day_layout.addWidget(self.pending_label)
        self.confirm_actions = ActionRow(self.daily_body)
        self.reload_button = QPushButton('重新读取此日计划')
        self.reload_button.clicked.connect(lambda: self.reload_plan())
        self.confirm_button = QPushButton('确认所选结果')
        self.confirm_button.setObjectName('Primary')
        self.confirm_button.clicked.connect(self.submit)
        self.confirm_actions.addWidget(self.confirm_button)
        self.confirm_actions.addWidget(self.reload_button)
        day_layout.addWidget(self.confirm_actions)
        day_layout.addStretch()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidget(self.daily_body)
        self.day_tab_layout.addWidget(self.scroll)

        self.week_tab_layout = QVBoxLayout(self.weekly_tab)
        self.week_tab_layout.setContentsMargins(0, 0, 0, 0)
        self.weekly_body = QWidget()
        week_layout = self.week_layout = QVBoxLayout(self.weekly_body)
        week_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.week_heading = _WeekHeading()
        self.week_heading.setTextFormat(Qt.TextFormat.PlainText)
        self.week_heading.setWordWrap(True)
        self.week_heading.setObjectName('ReviewSummary')
        self.week_chart_button = self._chart_settings_button('review_weekly_style')
        self.week_header_tools = ActionRow(self.weekly_body)
        self.week_header_tools.addWidget(self.week_heading)
        self.week_header_tools.addWidget(self.week_chart_button)
        week_layout.addWidget(self.week_header_tools)
        self.week_notice = QLabel()
        self.week_notice.setWordWrap(True)
        self.week_notice.setObjectName('Hint')
        week_layout.addWidget(self.week_notice)
        self.week_chart = CoverageChart()
        week_layout.addWidget(self.week_chart)
        self.week_days = WeekDaysChart()
        self.week_days_button = self._chart_settings_button('weekly_style', '每日图表样式…')
        self.week_days_tools = ActionRow(self.weekly_body)
        self.week_days_title = QLabel('逐日回顾')
        self.week_days_title.setObjectName('SectionHeading')
        self.week_days_tools.addWidget(self.week_days_title)
        self.week_days_tools.addWidget(self.week_days_button)
        week_layout.addWidget(self.week_days_tools)
        self.week_days.date_selected.connect(self._open_day)
        week_layout.addWidget(self.week_days)
        self.week_error = QLabel()
        self.week_error.setWordWrap(True)
        self.week_error.setObjectName('Error')
        self.week_error.hide()
        week_layout.addWidget(self.week_error)
        week_layout.addStretch()
        self.week_scroll = QScrollArea()
        self.week_scroll.setWidgetResizable(True)
        self.week_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.week_scroll.setWidget(self.weekly_body)
        self.week_tab_layout.addWidget(self.week_scroll)
        self._render_daily()

    def apply_visual_style(self, selected=None):
        """Reflow the current UI for theme/font changes without moving controls."""
        for layout in (self.day_layout, self.week_layout):
            layout.invalidate()
        self.updateGeometry()

    def set_weekly_style(self, style):
        self.week_days.set_style(style)
        self.chart_preferences['weekly_style'] = style

    def _chart_settings_button(self, key, label='图表样式…'):
        button = QPushButton(label)
        button.setObjectName('QuietButton')
        button.setAccessibleName('设置' + {'review_daily_style': '每日复盘', 'review_weekly_style': '每周汇总', 'weekly_style': '每周每日分布'}[key] + '图表样式')
        button.clicked.connect(lambda checked=False: self.chart_settings_requested.emit(key))
        return button

    def set_chart_preferences(self, preferences):
        """Restyle saved summaries without changing pending feedback or querying data."""
        self.chart_preferences = normalize_chart_preferences(preferences, current=self.chart_preferences)
        self.daily_chart.set_style(self.chart_preferences['review_daily_style'])
        self.week_chart.set_style(self.chart_preferences['review_weekly_style'])
        self.week_days.set_style(self.chart_preferences['weekly_style'])

    def set_assistants_visible(self, visible):
        self._assistants_visible = bool(visible)
        self._render_daily()

    def _keep_dialog(self, dialog):
        self.dialogs.append(dialog)
        dialog.finished.connect(lambda *_: self.dialogs.remove(dialog) if dialog in self.dialogs else None)
        dialog.open()
        return dialog

    def _manual_saved(self, receipt):
        self.message.setText('已保存。可通过“查看已保存实际记录”回读反馈，或通过“写小结 / 查看已保存小结”回读小结。')
        self.message.show()
        self.refresh()
        if self.on_changed:
            self.on_changed(receipt)

    def _record_actual(self):
        day = self._date
        picker = EntityPicker(self.bridge, self, allowed_types=['task', 'event', 'milestone', 'checklist', 'assessment'])
        picker.setWindowTitle('选择实际执行的事项 · ' + day)
        picker.heading.setText('选择要记录实际情况的任务或日程')
        def chosen():
            if picker.selected:
                self._keep_dialog(ActualFeedbackDialog(self.bridge, picker.selected, day, self, self._manual_saved))
        picker.accepted.connect(chosen)
        self._keep_dialog(picker)

    def _open_notes(self):
        self._keep_dialog(ReviewNotesDialog(self.bridge, self._date, self, self._manual_saved))

    def _open_actual_history(self):
        self._keep_dialog(ActualFeedbackHistoryDialog(self.bridge, self._date, self))

    def set_date(self, dateISO):
        parsed = QDate.fromString(str(dateISO), 'yyyy-MM-dd')
        if not parsed.isValid() or parsed.toString('yyyy-MM-dd') != str(dateISO):
            raise ValueError('Review date must be YYYY-MM-DD')
        changed = self._date != str(dateISO)
        self._date = str(dateISO)
        self.date_editor.blockSignals(True)
        self.date_editor.setDate(parsed)
        self.date_editor.blockSignals(False)
        if changed:
            self.message.clear()
            self._render_daily()
        self.refresh()

    def _open_day(self, day):
        self.tabs.setCurrentWidget(self.daily_tab)
        self.set_date(day)

    @staticmethod
    def _week(day):
        selected = dt.date.fromisoformat(day)
        start = selected - dt.timedelta(days=selected.weekday())
        return start.isoformat(), (start + dt.timedelta(days=6)).isoformat()

    def refresh(self, date=None):
        if date is not None and str(date) != self._date:
            self.set_date(date)
            return
        day = self._date
        self._generation += 1
        generation = self._generation
        self.bridge.query('daily_review',
            lambda result: self._receive_daily(day, generation, result),
            lambda error: self._read_error(day, generation, error), date=day)
        start, end = self._week(day)
        self._week_generation += 1
        week_generation = self._week_generation
        self.week_heading.setText(f'{start} 至 {end}')
        self.bridge.query('weekly_review',
            lambda result: self._receive_week(start, end, week_generation, result),
            lambda error: self._week_error(week_generation, error), start=start, end=end)

    @staticmethod
    def _signature(value):
        plan = value.get('plan') or {}
        return json.dumps({'epoch': value.get('epoch'), 'has_plan': value.get('has_plan'),
            'plan': [plan.get('id'), plan.get('version')],
            'items': [{k: item.get(k) for k in ('item_id', 'occurrence_version', 'target_id', 'target_version', 'completion_gate', 'result', 'original_completion', 'feedback_id', 'can_review', 'target_archived')}
                      for item in value.get('items', [])]}, sort_keys=True, ensure_ascii=False)

    def _receive_daily(self, day, generation, result):
        if generation != self._generation or day != self._date or self._submitting_date == day:
            return
        if result.get('date') != day:
            return
        value = copy.deepcopy(result)
        previous = self._states.get(day)
        if self._choices.get(day) and previous and self._signature(previous) != self._signature(value):
            self._latest[day] = value
            self._conflicts.add(day)
            self.message.setText('计划或原有反馈已在另一端更新。你的选择仍保留，请重新读取此日计划后核对。')
        else:
            self._states[day] = value
            if not self._choices.get(day):
                self._conflicts.discard(day)
                self._latest.pop(day, None)
            # Keep drafts on other dates; discard only old clean cache entries.
            for cached in list(self._states):
                if len(self._states) <= 40:
                    break
                if cached != day and not self._choices.get(cached):
                    self._states.pop(cached)
        self._render_daily()

    def _read_error(self, day, generation, error):
        if generation == self._generation and day == self._date:
            self.message.setText(self._error_text(error))
            self._update_actions()

    def _receive_week(self, start, end, generation, result):
        if generation != self._week_generation or self._week(self._date) != (start, end):
            return
        if result.get('start') != start or result.get('end') != end:
            return
        self.weekly_data = copy.deepcopy(result)
        self.week_error.clear()
        self.week_error.hide()
        summary = result.get('summary') or {}
        self.week_chart.set_summary(summary, denominator='本周计划事项与按次固定安排；出勤单独记录')
        days = result.get('days') or []
        self.week_days.set_days(days)
        with_plan = summary.get('days_with_plan', sum(bool(d.get('has_plan')) for d in days))
        without_plan = summary.get('days_without_plan', sum(not d.get('has_plan') for d in days))
        text = f'有计划 {with_plan} 天 · 缺计划 {without_plan} 天。缺计划不等于未完成；点击日期可查看当天并按实际情况复盘。'
        if summary.get('fixed_scheduled'):
            fixed_only=sum(bool(d.get('has_fixed_schedule') and not d.get('has_plan')) for d in days)
            text=f'已有每日计划 {with_plan} 天 · 仅固定安排 {fixed_only} 天 · 无已知安排 {without_plan-fixed_only} 天。未反馈保持未知。'
            text += f" 固定安排 {summary['fixed_scheduled']} 次 · 仍需补课 {summary.get('catchup_needed',0)} 次；课程出勤与任务完成在下方分别标明。"
        coverage = result.get('coverage') or {}
        if coverage.get('complete') is False or coverage.get('metrics_complete') is False:
            text += ' 当前统计覆盖不完整，请先核对缺口。'
        if summary.get('archived'):
            text += f" 其中 {summary['archived']} 项任务已删除，历史计划与原有反馈保留。"
        if coverage.get('historical_import_days'):
            text += ' 旧计划按原时间块保留；未能明确对应条目的历史反馈保留原记录，不推算完成率。'
        self.week_notice.setText(text)

    def _week_error(self, generation, error):
        if generation == self._week_generation:
            self.week_error.setText(self._error_text(error))
            self.week_error.setVisible(bool(self.week_error.text()))

    @staticmethod
    def _error_text(error):
        return error.get('message', '暂时无法读取，请重试。') if isinstance(error, dict) else str(error)

    def _clear_rows(self):
        self.item_buttons, self.item_labels = {}, {}
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _render_daily(self):
        value = self._states.get(self._date)
        self.daily_data = value
        self._clear_rows()
        known = value is not None
        has_plan = bool(value and value.get('can_review',value.get('has_plan')))
        self.notice.setVisible(known and not has_plan)
        self.codex_button.setVisible(self._assistants_visible and known and not has_plan)
        self.codex_button.setEnabled(self.on_codex is not None)
        self.confirm_button.setVisible(has_plan)
        self.daily_chart.setVisible(has_plan)
        self.daily_chart_tools.setVisible(has_plan)
        self.saved_hint.setVisible(has_plan)
        if not known:
            self.daily_heading.setText(self._date + ' · 正在读取计划')
        elif not has_plan:
            self.daily_heading.setText(self._date + ' · 缺少每日计划')
            self.notice.setText('这一天没有每日计划，无法按计划逐项复核。仍可选择任务或日程记录实际情况，也可以保存文字小结；不会补造计划或把缺计划记为失败。')
        else:
            plan = value.get('plan') or {}
            self.daily_heading.setText(self._date + ' · ' + (plan.get('title') or '当天固定安排'))
            self.daily_chart.set_summary(value.get('summary') or {}, denominator='当日计划事项及固定安排；完成与出勤分别记录')
            items = value.get('items') or []
            if not items:
                empty = QLabel('这份计划没有需要逐项确认的项目。休整或空计划不等于未完成。')
                empty.setWordWrap(True)
                self.rows_layout.addWidget(empty)
            for item in items:
                self._add_row(item)
        self.rows_layout.addStretch()
        self._update_actions()

    def _add_row(self, item):
        target = item.get('item_id',item['target_id'])
        card = QFrame()
        card.setObjectName('ReviewItem')
        layout = QVBoxLayout(card)
        layout.setSpacing(5)
        title = QLabel(item.get('display_title') or item.get('title') or '未命名项目')
        title.setWordWrap(True)
        title.setTextFormat(Qt.TextFormat.PlainText)
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        if item.get('owner_label'):
            owner = QLabel(item['owner_label']); owner.setObjectName('StatusPill'); owner.setTextFormat(Qt.TextFormat.PlainText); owner.setWordWrap(True); layout.addWidget(owner)
        if item.get('completion_gate'):
            gate = QLabel(('记录说明：' if item.get('fixed_schedule') else '完成条件：') + str(item['completion_gate']))
            gate.setWordWrap(True)
            gate.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(gate)
        detail = []
        if item.get('fixed_schedule') and not item.get('can_review') and not item.get('target_archived'):detail.append('尚未开始，到时可记录实际情况')
        if item.get('start') and item.get('end'):
            detail.append(str(item['start']) + '–' + str(item['end']))
        if item.get('planned_minutes') is not None:
            detail.append('计划估时 ' + str(item['planned_minutes']) + ' 分钟')
        if detail:
            label = QLabel(' · '.join(detail))
            label.setObjectName('Hint')
            label.setWordWrap(True)
            layout.addWidget(label)
        status = QLabel()
        status.setWordWrap(True)
        status.setObjectName('Hint')
        layout.addWidget(status)
        choices = ActionRow()
        buttons = {}
        for result, label in item.get('choices', [('done', '完成'), ('incomplete', '未完成')]):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setObjectName('ReviewChoice')
            button.setProperty('result', result)
            button.setMinimumHeight(34)
            button.setToolTip('再次点击可撤回本次选择；已经保存的结果不会被清除。')
            button.setAccessibleName((item.get('title') or '项目') + '：' + label)
            button.clicked.connect(lambda checked=False, id=target, choice=result: self._choose(id, choice))
            choices.addWidget(button)
            buttons[result] = button
        choices.addStretch()
        layout.addWidget(choices)
        self.item_buttons[target], self.item_labels[target] = buttons, status
        self.rows_layout.addWidget(card)
        self._update_row(item)

    def _update_row(self, item):
        target = item.get('item_id',item['target_id'])
        choices = self._choices.get(self._date, {})
        selected = choices.get(target, item.get('result'))
        if target in choices:
            label = '本次选择：' + dict(item.get('choices',[('done','完成'),('incomplete','未完成')])).get(selected,selected) + ' · 待确认保存'
        elif item.get('result') in dict(item.get('choices',[('done','完成'),('incomplete','未完成')])):
            label = '已保存：' + dict(item.get('choices',[('done','完成'),('incomplete','未完成')])).get(item['result'],item['result'])
        elif item.get('original_completion') is not None or item.get('raw_result') is not None:
            raw = item.get('original_completion', item.get('raw_result'))
            label = '原记录：' + ORIGINAL_LABELS.get(str(raw), str(raw)) + '；保留原义，未替你改选。'
        elif item.get('reported'):
            label = '已有明确反馈；未归入完成或未完成。'
        else:
            label = '未反馈'
        if item.get('target_archived'):
            label = '已删除 · 保留原计划记录 · ' + label + '；恢复任务后可以更正。'
        elif not item.get('available', True):
            label = '原计划对象暂不可用 · ' + label
        if item.get('catchup_task_id'):
            label += ' · '+('补课已完成，原出勤不变' if (item.get('catchup_progress') or {}).get('completion_confirmed') else '已关联补课事项')
        self.item_labels[target].setText(label)
        for result, button in self.item_buttons[target].items():
            button.setChecked(selected == result)
            button.setEnabled(self._submitting_date is None and self._date not in self._conflicts and item.get('can_review', True))

    def _choose(self, target, result):
        if self._submitting_date is not None or self._date in self._conflicts:
            return
        value = self._states.get(self._date) or {}
        item = next((i for i in value.get('items', []) if i.get('item_id',i['target_id']) == target), None)
        if not item or not item.get('can_review', True) or item.get('target_archived'):
            return
        choices = self._choices.setdefault(self._date, {})
        if choices.get(target) == result or item.get('result') == result:
            choices.pop(target, None)
        else:
            choices[target] = result
        if not choices:
            self._choices.pop(self._date, None)
        self._update_row(item)
        self._update_actions()

    def _update_actions(self):
        self.message.setVisible(bool(self.message.text()))
        current = self._choices.get(self._date, {})
        other_days = sum(bool(v) for k, v in self._choices.items() if k != self._date)
        text = f'本日 {len(current)} 项待确认。' if current else ''
        if other_days:
            text += f' 另有 {other_days} 天的选择尚未提交，切回该日期可继续。'
        self.pending_label.setText(text)
        self.confirm_button.setText('正在确认…' if self._submitting_date else '确认所选结果')
        self.confirm_button.setEnabled(bool(current) and self._submitting_date is None and self._date not in self._conflicts)
        self.reload_button.setVisible(self._date in self._conflicts)
        self.reload_button.setEnabled(self._submitting_date is None)
        pending = bool(any(self._choices.values()) or self._submitting_date)
        if pending != self._pending:
            self._pending = pending
            self.has_pending.emit(pending)

    def reload_plan(self, *, discard=False):
        if self._submitting_date:
            return
        if self._choices.get(self._date) and not discard:
            answer = QMessageBox.question(self, '重新读取计划', '原计划或反馈已经改变。重新读取会放弃此日尚未提交的选择，之后请按新计划重新核对。',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._choices.pop(self._date, None)
        self._conflicts.discard(self._date)
        self._states.pop(self._date, None)
        self._latest.pop(self._date, None)
        self.message.clear()
        self._render_daily()
        self.refresh()

    def submit(self):
        day = self._date
        value = self._states.get(day) or {}
        choices = self._choices.get(day, {})
        if self._submitting_date or not choices or day in self._conflicts or not value.get('can_review',value.get('has_plan')):
            return
        plan = value.get('plan') or {}
        payload = {'date':day,'plan_id':plan.get('id'),'plan_version':plan.get('version'),
                   'schedule_signature':value.get('schedule_signature'),
                   'answers':[{('item_id' if i.get('item_id') else 'target_id'):i.get('item_id',i['target_id']),
                               'result':choices[i.get('item_id',i['target_id'])]}
                              for i in value.get('items',[]) if i.get('item_id',i['target_id']) in choices
                              and i.get('can_review',True) and not i.get('target_archived')]}
        if payload.get('schedule_signature') is None: payload.pop('schedule_signature',None)
        if not payload['answers']:
            return
        self._submitting_date = day
        self.message.clear()
        self._render_daily()
        options = {}
        for key in ('epoch', 'revision'):
            if value.get(key) is not None:
                options['expected_revision' if key == 'revision' else key] = value[key]
        self.bridge.command('submit_daily_review', payload,
            lambda result: self._submitted(day, result), lambda error: self._submit_error(day, error), **options)

    def _submitted(self, day, receipt):
        self._submitting_date = None
        self._choices.pop(day, None)
        self._conflicts.discard(day)
        self._latest.pop(day, None)
        if day == self._date:
            self.message.setText('已确认所选结果。没有选择的项目保留原状。')
        self._render_daily()
        self.refresh()
        if self.on_changed:
            self.on_changed(receipt)

    def _submit_error(self, day, error):
        self._submitting_date = None
        code = error.get('code', '') if isinstance(error, dict) else ''
        if 'conflict' in code or code in {'epoch_mismatch', 'review_no_plan'}:
            self._conflicts.add(day)
        if day == self._date:
            text = self._error_text(error) + ' 你的选择仍保留。'
            if day in self._conflicts:
                text += ' 请重新读取此日计划后核对。'
            self.message.setText(text)
        self._render_daily()

    def _ask_codex(self):
        if self.on_codex:
            self.on_codex('请帮我复盘 ' + self._date + '。软件中这一天没有每日计划，请根据我提供的实际情况逐项核对并保存明确反馈。没有提供的信息保持未知，不生成假的计划完成情况，也不要推断出席、提交、掌握或工时。')
