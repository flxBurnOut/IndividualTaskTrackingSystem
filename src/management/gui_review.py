"""Focused daily confirmation and structured weekly review via the shared service."""
from __future__ import annotations

import copy
import datetime as dt
import json

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QDateEdit, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)
from .gui_charts import CoverageChart, ORIGINAL_LABELS, WeekDaysChart
from .gui_calendar import install_calendar


class ReviewPage(QWidget):
    has_pending = Signal(bool)

    def __init__(self, bridge, parent=None, on_changed=None, on_codex=None):
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
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        title = QLabel('复盘')
        title.setObjectName('DialogHeading')
        top.addWidget(title, 1)
        self.date_editor = QDateEdit(QDate.fromString(self._date, 'yyyy-MM-dd'))
        install_calendar(self.date_editor)
        self.date_editor.setDisplayFormat('yyyy-MM-dd')
        self.date_editor.setAccessibleName('复盘日期')
        self.date_editor.dateChanged.connect(lambda date: self.set_date(date.toString('yyyy-MM-dd')))
        top.addWidget(self.date_editor)
        self.refresh_button = QPushButton('刷新')
        self.refresh_button.clicked.connect(lambda: self.refresh())
        top.addWidget(self.refresh_button)
        layout.addLayout(top)
        self.tabs = QTabWidget()
        self.daily_tab, self.weekly_tab = QWidget(), QWidget()
        self.tabs.addTab(self.daily_tab, '每日复盘')
        self.tabs.addTab(self.weekly_tab, '每周回顾')
        layout.addWidget(self.tabs, 1)

        day_layout = QVBoxLayout(self.daily_tab)
        self.daily_heading = QLabel('正在等待读取计划')
        self.daily_heading.setWordWrap(True)
        day_layout.addWidget(self.daily_heading)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setObjectName('Notice')
        day_layout.addWidget(self.notice)
        self.codex_button = QPushButton('到 Codex 按实际情况复盘')
        self.codex_button.clicked.connect(self._ask_codex)
        day_layout.addWidget(self.codex_button)
        self.daily_chart = CoverageChart()
        day_layout.addWidget(self.daily_chart)
        self.saved_hint = QLabel('上图是已保存的反馈。未选择的项目保持未反馈，不会自动算作未完成。')
        self.saved_hint.setWordWrap(True)
        self.saved_hint.setObjectName('Hint')
        day_layout.addWidget(self.saved_hint)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.rows = QWidget()
        self.rows_layout = QVBoxLayout(self.rows)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.addStretch()
        self.scroll.setWidget(self.rows)
        day_layout.addWidget(self.scroll, 1)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setObjectName('Error')
        day_layout.addWidget(self.message)
        self.pending_label = QLabel()
        self.pending_label.setWordWrap(True)
        self.pending_label.setObjectName('Hint')
        day_layout.addWidget(self.pending_label)
        actions = QHBoxLayout()
        self.reload_button = QPushButton('重新读取此日计划')
        self.reload_button.clicked.connect(lambda: self.reload_plan())
        actions.addWidget(self.reload_button)
        actions.addStretch()
        self.confirm_button = QPushButton('确认所选结果')
        self.confirm_button.setObjectName('Primary')
        self.confirm_button.clicked.connect(self.submit)
        actions.addWidget(self.confirm_button)
        day_layout.addLayout(actions)

        week_layout = QVBoxLayout(self.weekly_tab)
        self.week_heading = QLabel()
        self.week_heading.setObjectName('ReviewSummary')
        week_layout.addWidget(self.week_heading)
        self.week_notice = QLabel()
        self.week_notice.setWordWrap(True)
        self.week_notice.setObjectName('Hint')
        week_layout.addWidget(self.week_notice)
        self.week_chart = CoverageChart()
        week_layout.addWidget(self.week_chart)
        self.week_days = WeekDaysChart()
        self.week_days.date_selected.connect(self._open_day)
        week_scroll = QScrollArea()
        week_scroll.setWidgetResizable(True)
        week_scroll.setFrameShape(QFrame.Shape.NoFrame)
        week_scroll.setWidget(self.week_days)
        week_layout.addWidget(week_scroll, 1)
        self.week_error = QLabel()
        self.week_error.setWordWrap(True)
        self.week_error.setObjectName('Error')
        self.week_error.hide()
        week_layout.addWidget(self.week_error)
        self._render_daily()

    def set_weekly_style(self, style):
        self.week_days.set_style(style)

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
            'items': [{k: item.get(k) for k in ('target_id', 'target_version', 'completion_gate', 'result', 'original_completion', 'feedback_id', 'can_review', 'target_archived')}
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
        self.week_chart.set_summary(summary, denominator='本周有计划日期中的计划项目')
        days = result.get('days') or []
        self.week_days.set_days(days)
        with_plan = summary.get('days_with_plan', sum(bool(d.get('has_plan')) for d in days))
        without_plan = summary.get('days_without_plan', sum(not d.get('has_plan') for d in days))
        text = f'有计划 {with_plan} 天 · 缺计划 {without_plan} 天。缺计划不等于未完成；点击日期可查看当天并按实际情况复盘。'
        coverage = result.get('coverage') or {}
        if coverage.get('complete') is False or coverage.get('metrics_complete') is False:
            text += ' 当前统计覆盖不完整，请先核对缺口。'
        if summary.get('archived'):
            text += f" 其中 {summary['archived']} 项任务已删除，历史计划与原有反馈保留。"
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
        has_plan = bool(value and value.get('has_plan'))
        self.notice.setVisible(known and not has_plan)
        self.codex_button.setVisible(known and not has_plan)
        self.codex_button.setEnabled(self.on_codex is not None)
        self.confirm_button.setVisible(has_plan)
        self.daily_chart.setVisible(has_plan)
        self.saved_hint.setVisible(has_plan)
        if not known:
            self.daily_heading.setText(self._date + ' · 正在读取计划')
        elif not has_plan:
            self.daily_heading.setText(self._date + ' · 缺少每日计划')
            self.notice.setText('这一天没有每日计划，无法按计划逐项复核。请到 Codex 根据实际发生的事情复盘；这里不会自动生成问卷或把缺计划记为失败。')
        else:
            plan = value.get('plan') or {}
            self.daily_heading.setText(self._date + ' · ' + (plan.get('title') or '每日计划'))
            self.daily_chart.set_summary(value.get('summary') or {})
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
        target = item['target_id']
        card = QFrame()
        card.setObjectName('ReviewItem')
        layout = QVBoxLayout(card)
        layout.setSpacing(5)
        title = QLabel(item.get('title') or '未命名项目')
        title.setWordWrap(True)
        title.setTextFormat(Qt.TextFormat.PlainText)
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        if item.get('completion_gate'):
            gate = QLabel('完成条件：' + str(item['completion_gate']))
            gate.setWordWrap(True)
            gate.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(gate)
        detail = []
        if item.get('start') and item.get('end'):
            detail.append(str(item['start']) + '–' + str(item['end']))
        if item.get('planned_minutes') is not None:
            detail.append('计划估时 ' + str(item['planned_minutes']) + ' 分钟')
        if detail:
            label = QLabel(' · '.join(detail))
            label.setObjectName('Hint')
            layout.addWidget(label)
        status = QLabel()
        status.setWordWrap(True)
        status.setObjectName('Hint')
        layout.addWidget(status)
        choices = QHBoxLayout()
        buttons = {}
        for result, label in [('done', '完成'), ('incomplete', '未完成')]:
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
        layout.addLayout(choices)
        self.item_buttons[target], self.item_labels[target] = buttons, status
        self.rows_layout.addWidget(card)
        self._update_row(item)

    def _update_row(self, item):
        target = item['target_id']
        choices = self._choices.get(self._date, {})
        selected = choices.get(target, item.get('result'))
        if target in choices:
            label = '本次选择：' + ('完成' if selected == 'done' else '未完成') + ' · 待确认保存'
        elif item.get('result') in {'done', 'incomplete'}:
            label = '已保存：' + ('完成' if item['result'] == 'done' else '未完成')
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
        self.item_labels[target].setText(label)
        for result, button in self.item_buttons[target].items():
            button.setChecked(selected == result)
            button.setEnabled(self._submitting_date is None and self._date not in self._conflicts and item.get('can_review', True))

    def _choose(self, target, result):
        if self._submitting_date is not None or self._date in self._conflicts:
            return
        value = self._states.get(self._date) or {}
        item = next((i for i in value.get('items', []) if i['target_id'] == target), None)
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
        if self._submitting_date or not choices or day in self._conflicts or not value.get('has_plan'):
            return
        plan = value['plan']
        payload = {'date': day, 'plan_id': plan['id'], 'plan_version': plan['version'],
                   'answers': [{'target_id': item['target_id'], 'result': choices[item['target_id']]}
                               for item in value.get('items', []) if item['target_id'] in choices and item.get('can_review', True) and not item.get('target_archived')]}
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
