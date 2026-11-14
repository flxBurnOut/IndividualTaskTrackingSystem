"""Today is only the selected day's plan, fixed events and attention items."""
from __future__ import annotations
import copy
import uuid
from PySide6.QtCore import QDate, QEvent, Qt, Signal, QTimer
from PySide6.QtWidgets import QDateEdit, QDialog, QDialogButtonBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QScrollArea, QVBoxLayout, QWidget, QSizePolicy, QLayout
from .gui_workspace import CountProgress, clear_layout, make_button, plain_label
from .gui_layout import ActionRow, ReadingLabel

WARNING_CATEGORIES = [('all', '全部需要留意'), ('today', '今天到期'), ('past', '已过日期，结果待核对'), ('upcoming', '近期提醒'), ('unknown', '日期待确认')]
from .gui_write_state import UNKNOWN_WRITES, DAY_WRITE_REJECTIONS as REJECTED_WRITES, write_result_unknown


class DayTaskPicker(QDialog):
    """Small date-specific candidate picker; includes undated open tasks."""
    def __init__(self, bridge, day, parent=None):
        super().__init__(parent)
        self.bridge, self.day, self.selected = bridge, day, None
        self.closed = False
        self.finished.connect(lambda *_: setattr(self, 'closed', True))
        self.offset, self.generation = 0, 0
        self.setWindowTitle('选择加入 ' + day + ' 的任务')
        self.resize(580, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(ReadingLabel('选择已有未完成任务，包括没有填写日期的任务。加入安排不会修改任务的截止日期。', 'Body'))
        self.search = QLineEdit(); self.search.setPlaceholderText('搜索任务名称'); layout.addWidget(self.search)
        self.items = QListWidget(); self.items.setWordWrap(True); layout.addWidget(self.items, 1)
        self.note = ReadingLabel('', 'Quiet'); layout.addWidget(self.note)
        controls = ActionRow(); layout.addWidget(controls)
        self.previous = make_button('上一页', lambda: self.load(max(0, self.offset-10)))
        self.following = make_button('下一页', lambda: self.load(self.next_offset))
        controls.addWidget(self.previous); controls.addWidget(self.following)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('加入 ' + day)
        buttons.accepted.connect(self.choose); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.items.itemDoubleClicked.connect(lambda _: self.choose())
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(250)
        self.timer.timeout.connect(lambda: self.load(0)); self.search.textChanged.connect(lambda: self.timer.start())
        self.next_offset = None; self.load(0)

    def load(self, offset):
        if offset is None: return
        self.offset = offset; self.generation += 1; generation = self.generation
        self.note.setText('正在读取可选择的任务…')
        def loaded(result):
            if self.closed or generation != self.generation: return
            self.items.clear()
            for task in result.get('items', []):
                text = task.get('display_title') or task['title']
                text += '\n' + (task.get('owner_label') or '暂未分类') + (' · 已在当天安排中' if task.get('in_plan') else '')
                item = QListWidgetItem(text); item.setData(Qt.ItemDataRole.UserRole, task)
                if task.get('in_plan'): item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                self.items.addItem(item)
            self.next_offset = result.get('next_offset')
            self.previous.setEnabled(self.offset > 0); self.following.setEnabled(self.next_offset is not None)
            self.note.setText(f"找到 {result.get('total', 0)} 项 · 当前显示 {len(result.get('items', []))} 项。")
        self.bridge.query('task_pool', loaded, lambda error: self.note.setText(error.get('message', '读取失败')) if not self.closed and generation == self.generation else None,
                          date=self.day, group='open', search=self.search.text().strip(), offset=offset, limit=10)

    def choose(self):
        item = self.items.currentItem()
        if item is None or not (item.flags() & Qt.ItemFlag.ItemIsEnabled):
            self.note.setText('请先选择一项尚未加入当天安排的任务。'); return
        self.selected = item.data(Qt.ItemDataRole.UserRole); self.accept()


class TodayPage(QWidget):
    error = Signal(object)
    review_attention = Signal(bool)
    def __init__(self, bridge, parent=None, on_plan=None, on_manual=None, on_review=None, on_task=None, on_changed=None, on_timetable=None, on_habits=None):
        super().__init__(parent)
        self.dead = False
        self.destroyed.connect(lambda *_: setattr(self, "dead", True))
        self.review_saving = False
        self.review_uncertain = False
        self.review_submission = None
        self.quick_pending = self.quick_uncertain = self.join_pending = self.join_uncertain = False
        self.quick_submission = self.join_submission = self.join_target = None
        self.quick_day = None
        self.past_warnings = None
        self.past_warning_offset = 0
        self.past_expanded = True
        self.bridge, self.on_plan, self.on_manual, self.on_review, self.on_task = bridge, on_plan, on_manual, on_review, on_task
        self.on_changed = on_changed
        self.dialogs = []
        self.daily_tasks_result = None
        self.tasks_offset = 0
        self.pending_targets = set()
        self.generation = 0
        self.has_plan = False
        self.assistants_visible = False
        self.review_result = None
        self.today_result = None
        self.warning_result = None
        self.warning_offset = 0
        self.warning_date = None
        self.show_all_reminders = False
        self._rendered_plan = None
        self._rendered_tasks = None
        self._rendered_events = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(20)
        self.date_heading = plain_label("", "TodayDateHeading")
        font = self.date_heading.font(); font.setPointSize(22); font.setBold(True); self.date_heading.setFont(font)
        outer.addWidget(self.date_heading)
        self.compact_controls = ActionRow(self)
        row = self.compact_controls
        previous = make_button("‹", lambda: self.date.setDate(self.date.date().addDays(-1)))
        previous.setFixedWidth(35)
        row.addWidget(previous)
        self.date = QDateEdit(QDate.currentDate())
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy 年 M 月 d 日")
        self.date.dateChanged.connect(self.refresh)
        row.addWidget(self.date)
        following = make_button("›", lambda: self.date.setDate(self.date.date().addDays(1)))
        following.setFixedWidth(35)
        row.addWidget(following)
        today_button = make_button("回到今天", lambda: self.date.setDate(QDate.currentDate()))
        row.addWidget(today_button)
        self.timetable_button = make_button("每周课表", lambda: on_timetable(self.date_iso()) if on_timetable else None)
        row.addWidget(self.timetable_button)
        self.manual_button = make_button("详细安排…", lambda: self.on_manual(self.date_iso()) if self.on_manual else None)
        self.manual_button.setMinimumHeight(44)
        row.addWidget(self.manual_button)
        self.plan_button = make_button("请助手给建议", self.request_plan)
        self.plan_button.hide()
        row.addWidget(self.plan_button)
        self._date_controls = [previous, self.date, following, today_button, self.timetable_button]
        self._plan_controls = [self.manual_button, self.plan_button]
        outer.addWidget(self.compact_controls)
        quick_row = ActionRow(); self.quick_input = QLineEdit(); self.quick_input.setMinimumWidth(220)
        self.quick_input.setPlaceholderText('这一天要做什么？'); self.quick_input.returnPressed.connect(self.save_quick)
        self.quick_button = make_button('', self.save_quick, True)
        self.pick_button = make_button('从已有任务选择…', self.pick_task)
        quick_row.addWidget(self.quick_input); quick_row.addWidget(self.quick_button); quick_row.addWidget(self.pick_button)
        outer.addWidget(quick_row)
        self.quick_note = self._reading_label('在这里添加任务会同时加入所选日期的安排。', 'Quiet'); outer.addWidget(self.quick_note)
        quick_actions = ActionRow(); self.quick_actions = quick_actions; outer.addWidget(quick_actions)
        self.quick_retry = make_button('重试加入', self.retry_join); self.quick_retry.hide(); quick_actions.addWidget(self.quick_retry)
        self.quick_open = make_button('查看已记录任务', lambda: self.on_task(self.join_target['task']['id']) if self.on_task and self.join_target else None)
        self.quick_open.hide(); quick_actions.addWidget(self.quick_open)
        self.quick_keep = make_button('暂不安排，继续记录', self.keep_task_only); self.quick_keep.hide(); quick_actions.addWidget(self.quick_keep)
        quick_actions.hide()
        self.review_note = self._reading_label('', 'Quiet'); self.review_note.hide(); outer.addWidget(self.review_note)
        self.review_retry = make_button('核对上次保存', self.retry_review); self.review_retry.hide(); outer.addWidget(self.review_retry, alignment=Qt.AlignmentFlag.AlignLeft)
        self.habits_button=make_button('日常习惯 · 查看提醒、自动待办与安排偏好',lambda:on_habits() if on_habits else None)
        self.habits_button.setObjectName('HabitsSummary');self.habits_button.setMinimumHeight(44);outer.addWidget(self.habits_button)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        self.body = QVBoxLayout(body)
        self.body.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.body.setContentsMargins(0, 0, 10, 10)
        self.body.setSpacing(22)
        self.plan_box = QFrame()
        self.plan_box.setObjectName("PlanCard")
        self.plan_layout = QVBoxLayout(self.plan_box)
        self.plan_layout.setContentsMargins(24, 23, 24, 23)
        self.action_column = QWidget(); self.action_layout = QVBoxLayout(self.action_column)
        self.action_layout.setContentsMargins(0, 0, 0, 0); self.action_layout.setSpacing(22)
        self.action_layout.addWidget(self.plan_box)
        self.events_box = QWidget()
        self.events_layout = QVBoxLayout(self.events_box)
        self.events_layout.setContentsMargins(0, 0, 0, 0)
        self.events_layout.setSpacing(9)
        self.action_layout.addWidget(self.events_box)
        self.tasks_box = QWidget()
        self.tasks_layout = QVBoxLayout(self.tasks_box)
        self.tasks_layout.setContentsMargins(0, 0, 0, 0); self.tasks_layout.setSpacing(9)
        self.action_layout.addWidget(self.tasks_box)
        self.action_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.attention_box = QFrame(); self.attention_box.setObjectName('PlanCard')
        self.attention_layout = QVBoxLayout(self.attention_box)
        self.attention_layout.setContentsMargins(18, 18, 18, 18)
        self.attention_layout.setSpacing(9)
        self.columns = QGridLayout(); self.columns.setHorizontalSpacing(22); self.columns.setVerticalSpacing(22)
        self.columns.addWidget(self.attention_box, 0, 0, alignment=Qt.AlignmentFlag.AlignTop)
        self.columns.addWidget(self.action_column, 0, 1, alignment=Qt.AlignmentFlag.AlignTop)
        self.body.addLayout(self.columns)
        self.warning_category = QComboBox()
        self.warning_category.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        for key, caption in WARNING_CATEGORIES: self.warning_category.addItem(caption, key)
        self.warning_category.currentIndexChanged.connect(self.change_warning_category)
        self._wide_columns = None
        for section in (self.plan_box, self.events_box, self.tasks_box, self.attention_box):
            section.layout().setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.body.addStretch()
        self.scroll.setWidget(body)
        outer.addWidget(self.scroll, 1)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop)
        for section in (self.plan_box, self.events_box, self.tasks_box, self.attention_box):
            # Preferred sections grow to heightForWidth after text wraps; a
            # maximum policy would cap them at an unwrapped sizeHint.
            policy = section.sizePolicy()
            policy.setHeightForWidth(True)
            section.setSizePolicy(policy)
            section.layout().setAlignment(Qt.AlignmentFlag.AlignTop)
        self.update_date_heading()
        self.render_plan(None)
        self.render_attention()
        self._reflow_columns()

    def pending_writes(self):
        return any((self.quick_pending, self.quick_uncertain, self.join_pending, self.join_uncertain, self.review_saving, self.review_uncertain))

    def resizeEvent(self, event):
        super().resizeEvent(event); self._reflow_columns()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange): self._reflow_columns()

    def _reflow_columns(self):
        if not hasattr(self, 'columns'): return
        wide = self.width() >= max(1000, self.fontMetrics().height() * 54)
        if wide == self._wide_columns: return
        self._wide_columns = wide
        self.columns.removeWidget(self.attention_box); self.columns.removeWidget(self.action_column)
        self.columns.addWidget(self.attention_box, 0, 0, alignment=Qt.AlignmentFlag.AlignTop)
        self.columns.addWidget(self.action_column, 0 if wide else 1, 1 if wide else 0, alignment=Qt.AlignmentFlag.AlignTop)
        self.columns.setColumnStretch(0, 2 if wide else 1); self.columns.setColumnStretch(1, 3 if wide else 0)

    def apply_visual_style(self, selected=None):
        """Older callers may request a profile; the current layout stays intact."""

    def _compact_actions(self, target, widgets):
        row = ActionRow()
        for widget in widgets:
            row.addWidget(widget)
        target.addWidget(row)
        return row

    def _reading_label(self, text, name):
        label = ReadingLabel(text, name)
        policy = label.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
        label.setSizePolicy(policy)
        return label

    def update_date_heading(self):
        day = self.date.date()
        self.date_heading.setText(day.toString("yyyy 年 M 月 d 日") + "  ·  星期" + "一二三四五六日"[day.dayOfWeek()-1])
        if not self.quick_uncertain:
            self.quick_button.setText('添加到 ' + day.toString('M 月 d 日'))

    def date_iso(self):
        return self.date.date().toString("yyyy-MM-dd")

    def set_assistants_visible(self,visible):
        self.assistants_visible=bool(visible)
        self.plan_button.setVisible(self.assistants_visible)
        self.render_plan(self.review_result)

    def set_date(self, date):
        parsed = QDate.fromString(date, "yyyy-MM-dd") if isinstance(date, str) else date
        if parsed != self.date.date():
            self.date.setDate(parsed)
        else:
            self.refresh()

    def refresh(self, *_):
        if self.dead: return
        self.update_date_heading()
        self.generation += 1
        generation = self.generation
        day = self.date_iso()
        if self.warning_date != day:
            self.warning_date, self.warning_offset = day, 0
            self.tasks_offset = 0
            self.past_warning_offset = 0; self.past_warnings = None; self.past_expanded = True
            self.warning_result = None; self.today_result = None; self.review_result = None
            self.render_attention(); self.render_plan(None)
            self.daily_tasks_result = None
            clear_layout(self.tasks_layout)
        def failed(error):
            if not self.dead and generation == self.generation:
                self.error.emit(error)
        def review_loaded(result):
            if self.dead or generation != self.generation:
                return
            self.review_result = result
            self.has_plan = bool(result.get("has_plan"))
            needs_review = not result.get("can_review", self.has_plan) or (result.get("summary") or {}).get("pending_review", (result.get("summary") or {}).get("unreported", 0)) > 0
            self.review_attention.emit(needs_review and self.date.date() <= QDate.currentDate())
            self.manual_button.setText("详细安排…")
            self.plan_button.setText("请助手调整" if self.has_plan else "请助手给建议")
            self.plan_button.setVisible(self.assistants_visible)
            self.render_plan(result)
            if self.today_result:self.render_events(self.today_result.get("events",[]))
        def today_loaded(result):
            if self.dead or generation != self.generation:
                return
            self.today_result = result
            self.render_events(result.get("events", []))
            self.render_attention()
        def warnings_loaded(result):
            if self.dead or generation != self.generation:
                return
            if not result.get("items") and self.warning_offset and result.get("total", 0) <= self.warning_offset:
                self.warning_offset = max(0, self.warning_offset - 5); self.refresh(); return
            self.warning_result = result
            self.render_attention()
        def tasks_loaded(result):
            if self.dead or generation != self.generation: return
            self.daily_tasks_result = result
            if not result.get("items") and self.tasks_offset and result.get("total", 0) <= self.tasks_offset:
                self.tasks_offset = max(0, self.tasks_offset - 10); self.refresh(); return
            self.render_tasks(result)
        def habits_loaded(result):
            if not self.dead and generation==self.generation:
                self.habits_button.setText('日常习惯 · '+result['summary_text']+'  ›')
                self.habits_button.setToolTip(result.get('gap') or '查看实际作用、启用状态和下次触发时间。')
        self.bridge.query('habits_overview',habits_loaded,failed,compact=True)
        self.bridge.query("daily_tasks", tasks_loaded, failed, date=day, limit=10, offset=self.tasks_offset)
        self.bridge.query("daily_review", review_loaded, failed, date=day)
        self.bridge.query("today", today_loaded, failed, date=day)
        self.bridge.query("warnings", warnings_loaded, failed, date=day, limit=5, offset=self.warning_offset, group="all", category=self.warning_category.currentData())

    def request_plan(self):
        if self.on_plan:
            self.on_plan(self.date_iso(), self.has_plan)

    def _submission(self, name, payload, snapshot=None):
        snapshot = snapshot or {}
        return {'name': name, 'payload': copy.deepcopy(payload), 'request_id': str(uuid.uuid4()),
                'epoch': snapshot.get('epoch', self.bridge.epoch),
                'expected_revision': snapshot.get('revision', self.bridge.revision)}

    def _send(self, submission, saved, failed):
        self.bridge.command(submission['name'], copy.deepcopy(submission['payload']), saved, failed,
                            **{key: submission[key] for key in ('epoch', 'expected_revision', 'request_id')})

    @staticmethod
    def _unknown(error, previously_uncertain=False):
        return write_result_unknown(error, previously_uncertain)

    def _capture_controls(self):
        self.quick_input.setReadOnly(self.quick_pending or self.quick_uncertain)
        self.quick_button.setEnabled(not self.quick_pending and not self.join_target)
        self.pick_button.setEnabled(not self.quick_pending and not self.quick_uncertain and not self.join_target)
        self.quick_retry.setEnabled(not self.join_pending)
        self.quick_actions.setVisible(any(not button.isHidden() for button in (self.quick_retry, self.quick_open, self.quick_keep)))

    def save_quick(self):
        if self.dead or self.quick_pending or self.join_target: return
        if not self.quick_uncertain:
            title = self.quick_input.text().strip()
            if not title: self.quick_note.setText('先写下这一天要做的事。'); return
            self.quick_day = self.date_iso()
            self.quick_submission = self._submission('create', {'type': 'task', 'title': title, 'data': {}})
        submission, day = self.quick_submission, self.quick_day
        self.quick_pending = True; self._capture_controls()
        self.quick_note.setText('正在核对上次任务是否已经记下…' if self.quick_uncertain else '正在记录任务，然后加入 ' + day + '…')
        def saved(receipt):
            if self.dead: return
            self.quick_pending = self.quick_uncertain = False; self.quick_submission = None
            entity = (receipt.get('result') or {}).get('entity') or {}
            if not entity.get('id'):
                self.quick_pending = False; self.quick_uncertain = True; self.quick_submission = submission
                self.quick_note.setText('服务已返回结果，但任务编号尚待核对。请核对原请求。')
                self.quick_button.setText('核对上次记录'); self._capture_controls(); return
            if self.quick_input.text().strip() == submission['payload']['title']: self.quick_input.clear()
            self._capture_controls(); self.update_date_heading()
            self.add_task(entity, day, None)
            if self.on_changed: self.on_changed(receipt)
        def failed(error):
            if self.dead: return
            self.quick_pending = False; self.quick_uncertain = self._unknown(error, self.quick_uncertain)
            self.quick_note.setText(('记录结果尚待核对，输入已保留。' if self.quick_uncertain else '未能记下任务，输入已保留。') + error.get('message', ''))
            if not self.quick_uncertain: self.quick_submission = None
            self._capture_controls()
            if self.quick_uncertain: self.quick_button.setText('核对上次记录')
            self.error.emit(error)
        self._send(submission, saved, failed)

    def pick_task(self):
        if self.quick_pending or self.quick_uncertain or self.join_target: return
        day = self.date_iso(); dialog = DayTaskPicker(self.bridge, day, self)
        self.dialogs.append(dialog); dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        def finished(result):
            if dialog in self.dialogs: self.dialogs.remove(dialog)
            if result == QDialog.DialogCode.Accepted and dialog.selected: self.add_task(dialog.selected, day, None)
        dialog.finished.connect(finished); dialog.open()

    def keep_task_only(self):
        if self.join_pending or self.join_uncertain: return
        self.join_target = self.join_submission = None
        self.quick_retry.hide(); self.quick_open.hide(); self.quick_keep.hide()
        self.quick_note.setText('任务保留在任务库中，可以稍后安排。'); self._capture_controls()
        if self.daily_tasks_result: self.render_tasks(self.daily_tasks_result)

    def retry_join(self):
        if self.dead or self.join_pending or not self.join_target: return
        if self.join_uncertain:
            self.join_pending = True; self._send_join()
        else:
            self._read_join_plan()

    def _read_join_plan(self):
        target = self.join_target
        if not target: return
        day = target['day']; self.join_pending = True; self._capture_controls()
        self.quick_keep.hide(); self.quick_note.setText('任务已记下，正在读取 ' + day + ' 的安排…')
        def loaded(snapshot):
            if self.dead or target is not self.join_target: return
            plan = snapshot.get('plan') or {}
            if plan.get('mode') == 'rest':
                self._join_failed({'code': 'rest_plan', 'message': '这一天设为休息日。可打开详细安排核对；休息设置未改变。'}); return
            payload = {'date': day, 'target_id': target['task']['id'], 'plan_id': plan.get('id'), 'plan_version': plan.get('version')}
            self.join_submission = self._submission('add_to_plan', payload, snapshot)
            self._send_join()
        def failed(error):
            if not self.dead and target is self.join_target: self._join_failed(error, before_write=True)
        self.bridge.query('task_pool', loaded, failed, date=day, limit=1)

    def _send_join(self):
        submission = self.join_submission; day = submission['payload']['date']
        self._capture_controls(); self.quick_retry.setText('正在核对…' if self.join_uncertain else '正在加入…')
        self.quick_note.setText(('正在核对上次是否加入 ' if self.join_uncertain else '正在加入 ') + day + '…')
        def saved(receipt):
            if self.dead: return
            self.join_pending = self.join_uncertain = False
            self.pending_targets.discard((day, submission['payload']['target_id']))
            self.join_target = self.join_submission = None
            self.quick_retry.hide(); self.quick_keep.hide(); self.quick_open.hide()
            self.quick_note.setText(('已在 ' if (receipt.get('result') or {}).get('reused') else '已加入 ') + day + ' 的安排。')
            self._capture_controls(); self.refresh()
            if self.on_changed: self.on_changed(receipt)
        self._send(submission, saved, self._join_failed)

    def _join_failed(self, error, *, before_write=False):
        if self.dead: return
        self.join_pending = False
        self.join_uncertain = False if before_write else self._unknown(error, self.join_uncertain)
        if not self.join_uncertain:
            self.join_submission = None
            if self.join_target: self.pending_targets.discard((self.join_target['day'], self.join_target['task']['id']))
        day = self.join_target['day'] if self.join_target else self.date_iso()
        self.quick_note.setText(('任务已记下，加入 ' + day + ' 的结果尚待核对。' if self.join_uncertain else '任务已记下，尚未加入 ' + day + '。') + error.get('message', ''))
        self.quick_retry.setText('核对上次加入' if self.join_uncertain else '重试加入 ' + day)
        self.quick_retry.show(); self.quick_open.setVisible(self.on_task is not None); self.quick_keep.setVisible(not self.join_uncertain)
        self._capture_controls(); self.error.emit(error)

    def render_plan(self, result):
        self._rendered_plan = result
        clear_layout(self.plan_layout)
        empty = not result or not result.get('can_review', result.get('has_plan'))
        name = 'TodayPlanEmpty' if empty else 'PlanCard'
        if self.plan_box.objectName() != name:
            self.plan_box.setObjectName(name)
            self.plan_box.style().unpolish(self.plan_box)
            self.plan_box.style().polish(self.plan_box)
        if empty:
            self.plan_layout.setContentsMargins(0, 8, 0, 12)
        else:
            self.plan_layout.setContentsMargins(24, 23, 24, 23)
        if not result:
            self.plan_layout.addWidget(plain_label("今天的安排", "SectionHeading"))
            self.plan_layout.addWidget(plain_label("正在读取当天计划…", "Quiet"))
            return
        if not result.get("can_review", result.get("has_plan")):
            # Keep wrapped labels at the width used by the parent layout's
            # height-for-width calculation. AlignLeft on the *layout item*
            # shrinks the widget after height calculation and clips new lines;
            # the label's text is already left-aligned independently.
            self.plan_layout.addWidget(self._reading_label("这一天还没有安排任务", "CardTitle"))
            self.plan_layout.addWidget(self._reading_label("可以在上方添加一件事，也可以从已有任务中选择。\n无需填写钟点；需要排序或排时，再打开详细安排。", "Body"))
            self.plan_layout.addSpacing(5)
            manual = make_button("详细安排…", lambda:self.on_manual(self.date_iso()) if self.on_manual else None)
            actions = [manual]
            if self.assistants_visible: actions.append(make_button('请助手给建议', self.request_plan))
            self._compact_actions(self.plan_layout, actions)
            return
        plan = result.get("plan") or {}
        header = QHBoxLayout()
        heading = plain_label(plan.get("title") or "当天固定安排", "CardTitle")
        header.addWidget(heading, 1)
        mode = {"standard": "常规", "low_state": "低精力", "no_precise_time": "按先后", "rest": "休息"}.get(plan.get("mode"), "")
        if mode:
            header.addWidget(plain_label(mode, "StatusPill"))
        self.plan_layout.addLayout(header)
        items = result.get("items", [])
        if not items:
            self.plan_layout.addWidget(plain_label("今天以休息为主。" if plan.get("mode") == "rest" else "这份计划没有安排任务条目。", "Body"))
        for item in items:
            self.plan_layout.addWidget(self.plan_row(item))
        summary = result.get("summary", {})
        if items:
            self.progress = CountProgress(reveal_scope=self, reveal_key=('today', self.date_iso()))
            has_fixed = (summary.get('fixed_scheduled') or result.get('has_fixed_schedule')
                         or any(item.get('fixed_schedule') for item in items))
            self.progress.title.setText("执行反馈 · 出勤不代表学习完成" if has_fixed
                                        else "执行反馈 · 按任务完成标准记录")
            self.progress.set_counts(summary)
            self.plan_layout.addSpacing(7)
            self.plan_layout.addWidget(self.progress)
        self.plan_layout.addWidget(self._reading_label("直接标记即保存；每日复盘会同步显示同一份记录。", "Quiet"))
        self._compact_actions(self.plan_layout, [make_button("前往复盘", lambda: self.on_review(self.date_iso()) if self.on_review else None)])

    def plan_row(self, item):
        row = QFrame()
        row.setObjectName("TimelineRow")
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 16, 0, 16)
        layout.setSpacing(18)
        start, end = item.get("start"), item.get("end")
        clock = plain_label((str(start) + ("\n" + str(end) if end else "")) if start else "按先后", "TimeLabel")
        meta = ActionRow(); meta.addWidget(clock)
        dot = plain_label("●", "DoneDot" if item.get("result") == "done" else "WaitingDot")
        meta.addWidget(dot); layout.addWidget(meta)
        title = self._reading_label(item.get("display_title") or item.get("title", "计划事项"), 'SectionHeading')
        layout.addWidget(title)
        details = [item["owner_label"]] if item.get("owner_label") else []
        if item.get("target_archived"):
            details.append("已删除 · 保留原计划记录")
        if item.get("fixed_schedule") and not item.get("can_review") and not item.get("target_archived"): details.append("尚未开始，到时可记录实际情况")
        if item.get("catchup_task_id"):
            details.append("补课已完成" if (item.get("catchup_progress") or {}).get("completion_confirmed") else "已关联补课事项")
        if item.get("location"): details.append(item["location"])
        if item.get("completion_gate"):
            details.append(str(item["completion_gate"]))
        if item.get("planned_minutes") is not None:
            details.append(f"预计 {item['planned_minutes']} 分钟")
        if details:
            layout.addWidget(self._reading_label(" · ".join(details), "Quiet"))
        result = item.get("result")
        choices = ActionRow()
        if self.on_task: choices.addWidget(make_button('打开任务', lambda: self.on_task(item['target_id'])))
        for value, caption in item.get("choices", (("done", "已完成" if result == "done" else "完成"), ("incomplete", "未完成"))):
            choice = make_button(caption, lambda _, i=item, v=value: self.complete_plan_item(i, v))
            choice.setCheckable(True); choice.setChecked(result == value); choice.setObjectName("ReviewChoice")
            choice.setProperty("result", value)
            choice.setEnabled(not self.review_saving and not self.review_uncertain and not item.get("target_archived") and item.get("can_review", True) and result != value)
            choice.setAccessibleName((item.get("owner_label", "") + " " + item.get("title", "事项") + "：" + caption).strip())
            choices.addWidget(choice)
        layout.addWidget(choices)
        return row

    def complete_plan_item(self, item, result):
        if self.dead or self.review_saving or self.review_uncertain or not item.get("can_review", True) or item.get("target_archived"): return
        snapshot = self.review_result or {}; plan = snapshot.get("plan") or {}
        if not snapshot.get("can_review", bool(plan)) or item.get("result") == result: return
        day = self.date_iso(); self.review_saving = True; self.render_plan(snapshot)
        payload = {"date": day, "plan_id": plan.get("id"), "plan_version": plan.get("version"), "schedule_signature": snapshot.get("schedule_signature"), "answers": [{("item_id" if item.get("item_id") else "target_id"): item.get("item_id",item["target_id"]), "result": result}]}
        self.review_submission = self._submission('submit_daily_review', payload, snapshot)
        self._send_review()

    def retry_review(self):
        if self.review_uncertain and not self.review_saving:
            self.review_saving = True; self._send_review()

    def _send_review(self):
        submission = self.review_submission
        self.review_note.show()
        self.review_note.setText(('正在核对 ' if self.review_uncertain else '正在保存 ') + submission['payload']['date'] + ' 的实际结果…')
        self.review_retry.setEnabled(False)
        def saved(receipt):
            if self.dead: return
            self.review_saving = self.review_uncertain = False; self.review_submission = None
            self.review_note.setText('已保存 ' + submission['payload']['date'] + ' 的实际结果。'); self.review_retry.hide()
            self.refresh()
            if self.on_changed: self.on_changed(receipt)
        def failed(error):
            if self.dead: return
            self.review_saving = False; self.review_uncertain = self._unknown(error, self.review_uncertain)
            self.review_retry.setVisible(self.review_uncertain); self.review_retry.setEnabled(True)
            self.review_note.setText(('保存结果尚待核对，请核对原请求。' if self.review_uncertain else '实际结果未保存，可以重试。') + error.get('message', ''))
            if not self.review_uncertain: self.review_submission = None
            self.refresh(); self.error.emit(error)
        self._send(submission, saved, failed)

    def render_events(self, events):
        self._rendered_events = events
        clear_layout(self.events_layout)
        integrated=bool(self.review_result and self.review_result.get('has_fixed_schedule'))
        self.events_box.setVisible(not integrated)
        if integrated:return
        self.events_layout.addWidget(plain_label("固定安排", "SectionHeading"))
        if not events:
            self.events_layout.addWidget(plain_label("这一天没有已登记的固定安排。", "Quiet"))
            return
        for event in events:
            from .dashboard import projected_event_times
            data = event.get("effective") or event.get("data", event)
            row = QFrame()
            row.setObjectName("EventRow")
            layout = QVBoxLayout(row)
            layout.setContentsMargins(17, 13, 17, 13)
            start, end = projected_event_times(event)
            time = f"{start} – {end}" if start and end else "时间待确认"
            clock = plain_label(time, "TimeLabel")
            layout.addWidget(clock)
            layout.addWidget(self._reading_label(event.get("title", "固定安排"), 'SectionHeading'))
            if data.get("location"):
                layout.addWidget(plain_label(str(data["location"]), "Quiet"))
            if event.get("id"): self._compact_actions(layout, [make_button("自动生成准备待办", lambda _, item=event: self.open_recurring(item))])
            self.events_layout.addWidget(row)

    def render_tasks(self, result):
        self._rendered_tasks = result
        clear_layout(self.tasks_layout)
        self.tasks_layout.addWidget(plain_label("当天候选待办", "SectionHeading"))
        self.tasks_layout.addWidget(plain_label("这里只列已安排到这天、到期或逾期的未完成任务。是否今天处理由你决定；加入日计划后，才进入当天复盘。", "Quiet"))
        if not result.get("items"):
            self.tasks_layout.addWidget(self._reading_label("没有尚未排入当天安排的日期相关任务。上方“从已有任务选择…”也包含未填写日期的任务。", "Quiet"))
        day = result.get("date") or self.date_iso()
        snapshot = result.get("plan")
        group_label = None
        for task in result.get("items", []):
            owner = task.get("owner_label") or "暂未分类"
            if owner != group_label:
                group_label = owner; self.tasks_layout.addWidget(plain_label(owner, "SectionHeading"))
            row = QFrame(); row.setObjectName('TimelineRow')
            inner = QVBoxLayout(row); inner.setContentsMargins(0, 10, 0, 10)
            content = QWidget(); words = QVBoxLayout(content); words.setContentsMargins(0, 0, 0, 0); words.setSpacing(4)
            inner.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
            words.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
            policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred); policy.setHeightForWidth(True); content.setSizePolicy(policy)
            title = self._reading_label(task.get('display_title') or task['title'], 'SectionHeading')
            title.setToolTip(task.get('display_title') or task['title'])
            words.addWidget(title)
            words.addWidget(self._reading_label(' · '.join(x for x in (task.get('candidate_date'), task.get('reason') or '日期相关待办') if x), 'Quiet'))
            if task.get('data', {}).get('completion_gate'):
                words.addWidget(self._reading_label(str(task['data']['completion_gate']), 'Quiet'))
            add = make_button('加入这天计划', lambda _, item=task, d=day, p=snapshot: self.add_task(item, d, p))
            add.setEnabled((day, task['id']) not in self.pending_targets and not self.join_target and not self.quick_pending and not self.quick_uncertain)
            inner.addWidget(content)
            actions = []
            if self.on_task:
                actions.append(make_button('查看', lambda _, identifier=task['id']: self.on_task(identifier)))
            actions.append(add)
            self._compact_actions(inner, actions)
            self.tasks_layout.addWidget(row)
        if self.tasks_offset or result.get("next_offset") is not None:
            hint = self._reading_label(f"候选待办 {self.tasks_offset + 1}–{self.tasks_offset + len(result.get('items', []))} / {result.get('total', 0)} 项", 'Quiet')
            previous = make_button('上一页待办', lambda: self.set_tasks_page(max(0, self.tasks_offset - 10))); previous.setEnabled(self.tasks_offset > 0)
            following = make_button('更多待办', lambda: self.set_tasks_page(result.get('next_offset'))); following.setEnabled(result.get('next_offset') is not None)
            self._compact_actions(self.tasks_layout, [hint, previous, following])

    def set_tasks_page(self, offset):
        if offset is not None: self.tasks_offset = offset; self.refresh()

    def add_task(self, task, day, plan):
        key = (day, task["id"])
        if key in self.pending_targets or self.join_target: return
        self.pending_targets.add(key)
        self.join_target = {'task': copy.deepcopy(task), 'day': day}
        if self.daily_tasks_result: self.render_tasks(self.daily_tasks_result)
        self._read_join_plan()

    def open_recurring(self, anchor):
        from .gui_recurring import RecurringDialog
        dialog = RecurringDialog(self.bridge, anchor, self, lambda receipt: (self.refresh(), self.on_changed(receipt) if self.on_changed else None))
        self.dialogs.append(dialog); dialog.finished.connect(lambda _: self.dialogs.remove(dialog) if dialog in self.dialogs else None); dialog.open()

    def warning_card(self, item, past=False):
        card = QFrame(); card.setObjectName('TimelineRow')
        layout = QVBoxLayout(card); layout.setContentsMargins(0, 9, 0, 9); layout.setSpacing(4)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        title = item.get('display_title') or item.get('title') or item.get('message') or '需要核对'
        owner = item.get('owner_label')
        heading = self._reading_label((owner + ' · ' if owner and owner not in title else '') + str(title), 'AttentionTitle' if past else 'CurrentWarningTitle')
        actions = [heading]
        if self.on_task and item.get('id'):
            actions.append(make_button('查看', lambda _, identifier=item['id']: self.on_task(identifier)))
        self._compact_actions(layout, actions)
        detail = item.get('reason') or item.get('message') or item.get('description') or ''
        if detail and detail != title:
            layout.addWidget(self._reading_label(str(detail), 'AttentionText'))
        return card

    def render_attention(self):
        if self.dead: return
        self.attention_layout.removeWidget(self.warning_category)
        self.warning_category.hide(); self.warning_category.setParent(self)
        clear_layout(self.attention_layout)
        result = self.warning_result or {}; notices = result.get("items", [])
        reminders = (self.today_result or {}).get("notifications", [])
        unknowns = (self.today_result or {}).get("unknowns", [])
        self.attention_box.show()
        self._compact_actions(self.attention_layout, [
            self._reading_label('当前警戒 · 需要留意', 'CurrentWarningTitle'),
            make_button('跳到当天任务 ↓', lambda: self.scroll.ensureWidgetVisible(self.plan_box, 0, 20)),
        ])
        category_counts = result.get('category_counts')
        self.warning_category.blockSignals(True)
        for index, (key, caption) in enumerate(WARNING_CATEGORIES):
            count = sum(category_counts.values()) if key == 'all' and category_counts is not None else (category_counts or {}).get(key)
            self.warning_category.setItemText(index, caption + (f'（{count}）' if count is not None else ''))
        self.warning_category.blockSignals(False)
        self.attention_layout.addWidget(self._reading_label('查看类别', 'Quiet'))
        self.attention_layout.addWidget(self.warning_category); self.warning_category.show()
        if self.warning_result is None:
            self.attention_layout.addWidget(self._reading_label('正在读取需要留意的事项…', 'Quiet'))
        elif not notices:
            self.attention_layout.addWidget(self._reading_label('当前类别没有需要留意的事项。' if self.warning_category.currentData() != 'all' else '目前没有需要留意的警戒事项。', 'Quiet'))
        if result.get('scan_complete') is False or result.get('occurrences_complete') is False:
            self.attention_layout.addWidget(self._reading_label('部分周期范围尚未完整核查，以上数量不能代表全部事项。', 'Notice'))
        if notices:
            category = None
            labels = dict(WARNING_CATEGORIES)
            for item in notices:
                key = item.get('category') or ('past' if item.get('group') == 'past' else 'unknown' if not item.get('due_date') else 'today' if item['due_date'] == self.date_iso() else 'upcoming')
                if key != category:
                    category = key; self.attention_layout.addWidget(self._reading_label(labels.get(key, '需要留意'), 'SectionHeading'))
                self.attention_layout.addWidget(self.warning_card(item, key == 'past'))
            total = result.get("total", 0)
            if total > 5 or self.warning_offset:
                hint = self._reading_label(f"当前警戒 {self.warning_offset + 1}–{self.warning_offset + len(notices)} / {total} 条", 'Quiet')
                self.attention_layout.addWidget(hint)
                previous = make_button("上一页警戒", lambda: self.set_warning_page(max(0, self.warning_offset - 5))); previous.setEnabled(self.warning_offset > 0)
                following = make_button("更多警戒", lambda: self.set_warning_page(result.get("next_offset"))); following.setEnabled(result.get("next_offset") is not None)
                self._compact_actions(self.attention_layout, [previous, following])
        for item in unknowns[:2]:
            text = item if isinstance(item, str) else item.get("message", item.get("reason", "部分时间信息尚待确认"))
            if text: self.attention_layout.addWidget(plain_label("待确认 · " + str(text), "Quiet"))
        for reminder in (reminders if self.show_all_reminders else reminders[:3]):
            data = reminder.get('data', {})
            card = QFrame(); card.setObjectName('TimelineRow')
            layout = QVBoxLayout(card); layout.setContentsMargins(0, 9, 0, 9); layout.setSpacing(5)
            layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
            heading = reminder.get('title', '复盘提醒')
            if data.get('business_date') and data['business_date'] != self.date_iso(): heading += ' · ' + data['business_date']
            layout.addWidget(self._reading_label(heading, 'AttentionTitle'))
            if data.get('content'):
                layout.addWidget(self._reading_label(data['content'], 'AttentionText'))
            actions = []
            mode = data.get('review_mode')
            if mode in ('daily', 'weekly'):
                day = data.get('business_date') or self.date_iso()
                actions.append(make_button('查看每周回顾' if mode == 'weekly' else '前往每日复盘', lambda checked=False, d=day, m=mode: self.on_review(d, m) if self.on_review else None))
            actions.append(make_button('知道了', lambda checked=False, item=reminder: self.mark_reminder_seen(item)))
            self._compact_actions(layout, actions)
            self.attention_layout.addWidget(card)
        if len(reminders)>3 and not self.show_all_reminders:
            self.attention_layout.addWidget(make_button(f"展开其余 {len(reminders)-3} 条近期提醒", self.expand_reminders), alignment=Qt.AlignmentFlag.AlignLeft)

    def change_warning_category(self):
        self.warning_offset = 0; self.warning_result = None; self.refresh()

    def set_warning_page(self, offset):
        if offset is not None:
            self.warning_offset = offset
            self.refresh()


    def expand_reminders(self):
        self.show_all_reminders = True
        self.render_attention()

    def mark_reminder_seen(self, reminder):
        self.bridge.command("update", {"id": reminder["id"], "version": reminder["version"], "patch": {"data": {"seen": True}}}, lambda _: self.refresh(), self.error.emit)
