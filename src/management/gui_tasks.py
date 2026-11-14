"""A lightweight capture hub and an explicitly opened, paged task library."""
import copy
import uuid
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QComboBox, QTreeWidget, QTreeWidgetItem, QStackedWidget, QScrollArea, QFrame
from .gui_workspace import make_button, clear_layout
from .gui_forms import EntityPicker, label_status
from .gui_layout import ActionRow, ReadingLabel as plain_label


GROUPS = [('open', '未完成任务'), ('undated', '任务未填日期'), ('due', '今天到期或日期已过'),
          ('future', '以后到期'), ('unowned', '未分类任务'), ('done', '已完成任务'), ('all', '全部有效任务')]
GROUP_HELP = {
    'open': '查看尚未记录完成的任务。查看条件只改变列表，不修改任务。',
    'undated': '任务没有填写处理日期和截止日期；它仍可能已加入某天的安排。',
    'due': '查看截止日期是今天或已经过去、尚未完成的任务。',
    'future': '查看截止日期晚于今天、尚未完成的任务。',
    'unowned': '查看尚未选择所属位置的未完成任务。',
    'done': '查看已明确记录完成的任务。',
    'all': '包含当前有效的未完成和已完成任务，不包含已删除、已取消及草稿。',
}
SORTS = [('recent', '最近记录优先'), ('due', '截止日期从近到远'), ('owner', '按所属位置排列')]
from .gui_write_state import UNKNOWN_WRITES as UNKNOWN, DAY_WRITE_REJECTIONS as REJECTED, write_result_unknown


class TasksPage(QWidget):
    error = Signal(object)

    def __init__(self, bridge, parent=None, *, on_task=None, on_edit=None, on_changed=None, on_plan=None):
        super().__init__(parent)
        self.bridge, self.on_task, self.on_edit = bridge, on_task, on_edit
        self.on_changed, self.on_plan = on_changed, on_plan
        self.generation, self.offset = 0, 0
        self.page_history = []
        self.owner, self.result, self.dead = None, None, False
        self.task_ids = None
        self.library_loaded = False
        self.quick_pending = self.plan_pending = self.completion_pending = False
        self.quick_uncertain = self.plan_uncertain = self.completion_uncertain = False
        self.quick_submission = self.plan_submission = self.completion_submission = None
        self.last_quick_task_id = None
        self.recent_tasks, self.recent_plan_buttons, self.recent_notes = [], {}, {}
        self.recent_generation = 0
        self.dialogs = []
        self.destroyed.connect(lambda *_: setattr(self, 'dead', True))
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget(); layout.addWidget(self.stack)
        self.hub_scroll = QScrollArea(); self.hub_scroll.setWidgetResizable(True); self.hub_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.hub = QWidget(); self.hub_scroll.setWidget(self.hub); self.stack.addWidget(self.hub_scroll)
        home = QVBoxLayout(self.hub); home.setContentsMargins(0, 0, 0, 0)
        home.addWidget(plain_label('先记下一件事', 'SectionHeading'))
        home.addWidget(plain_label('记下任务后，再决定什么时候做。日期、所属项目和完成条件都可以稍后补充。', 'Quiet'))
        quick = QHBoxLayout()
        self.quick_input = QLineEdit(); self.quick_input.setPlaceholderText('写下一件要做的事…')
        self.quick_input.setAccessibleName('新任务名称')
        self.quick_input.returnPressed.connect(self.save_quick)
        self.quick_button = make_button('记下任务', self.save_quick, True)
        quick.addWidget(self.quick_input, 1); quick.addWidget(self.quick_button); home.addLayout(quick)
        self.quick_note = plain_label('按 Enter 保存后可继续记录。记录任务不会自动加入每日安排。', 'Quiet')
        home.addWidget(self.quick_note)
        self.quick_open_button = make_button('查看刚记录任务', self.open_recent_quick)
        self.quick_open_button.hide(); captured = ActionRow(); captured.addWidget(self.quick_open_button); home.addWidget(captured)
        self.hub_plan_retry = make_button('核对上次加入结果', self.retry_plan); self.hub_plan_retry.hide(); captured.addWidget(self.hub_plan_retry)
        self.rest_plan_button = make_button('查看当天安排', self.open_blocked_plan); self.rest_plan_button.hide(); captured.addWidget(self.rest_plan_button)
        self.blocked_plan_day = None
        routes = ActionRow()
        self.batch_button = make_button('一次添加多个任务…', self.open_batch)
        self.library_button = make_button('查看任务库', self.open_library)
        routes.addWidget(self.batch_button); routes.addWidget(self.library_button); home.addWidget(routes)
        home.addWidget(plain_label('任务库用于查找、整理和编辑已有任务。', 'Quiet'))
        self.recent_box = QWidget(); self.recent_layout = QVBoxLayout(self.recent_box); self.recent_layout.setContentsMargins(0, 0, 0, 0)
        home.addWidget(self.recent_box); home.addStretch()
        self.library_scroll = QScrollArea(); self.library_scroll.setWidgetResizable(True); self.library_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.library = QWidget(); self.library_scroll.setWidget(self.library); self.stack.addWidget(self.library_scroll)
        library = QVBoxLayout(self.library); library.setContentsMargins(0, 0, 0, 0)
        breadcrumb = ActionRow()
        self.back_button = make_button('‹ 返回记录任务', self.show_capture)
        breadcrumb.addWidget(self.back_button); breadcrumb.addWidget(plain_label('任务 › 任务库', 'SectionHeading')); library.addWidget(breadcrumb)
        filters = ActionRow()
        filters.addWidget(plain_label('显示哪些任务', 'Quiet'))
        self.group_picker = QComboBox()
        self.group_picker.setAccessibleName('显示哪些任务')
        for value, label in GROUPS: self.group_picker.addItem(label, value)
        self.group_picker.currentIndexChanged.connect(self.filter_changed); filters.addWidget(self.group_picker)
        filters.addWidget(plain_label('排列顺序', 'Quiet'))
        self.sort_picker = QComboBox()
        self.sort_picker.setAccessibleName('排列顺序')
        for value, label in SORTS: self.sort_picker.addItem(label, value)
        self.sort_picker.currentIndexChanged.connect(self.filter_changed); filters.addWidget(self.sort_picker)
        library.addWidget(filters)
        search = QHBoxLayout(); search.addWidget(plain_label('任务名称', 'Quiet'))
        self.search_input = QLineEdit(); self.search_input.setPlaceholderText('搜索任务名称'); self.search_input.setClearButtonEnabled(True)
        self.search_input.setAccessibleName('搜索任务名称')
        self.search_timer = QTimer(self); self.search_timer.setSingleShot(True); self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self.filter_changed); self.search_input.textChanged.connect(lambda: self.search_timer.start())
        self.search_input.returnPressed.connect(self.filter_changed); search.addWidget(self.search_input, 1); library.addLayout(search)
        conditions = ActionRow()
        self.owner_button = make_button('所属位置：全部', self.choose_owner)
        self.clear_owner_button = make_button('清除位置条件', self.clear_owner); self.clear_owner_button.hide()
        self.clear_batch_button = make_button('查看全部批次', self.clear_batch); self.clear_batch_button.hide()
        self.reset_button = make_button('清除查看条件', self.reset_filters)
        self.refresh_button = make_button('刷新列表', lambda: self.refresh(force=True))
        for button in (self.owner_button, self.clear_owner_button, self.clear_batch_button, self.reset_button, self.refresh_button): conditions.addWidget(button)
        library.addWidget(conditions)
        self.range_note = plain_label(GROUP_HELP['open'], 'Quiet'); library.addWidget(self.range_note)
        self.status = plain_label('选择查看范围后查找任务。', 'Quiet'); library.addWidget(self.status)
        self.items = QTreeWidget(); self.items.setHeaderLabels(['任务', '所属位置', '日期', '状态'])
        self.items.setMinimumHeight(180)
        self.items.setRootIsDecorated(False); self.items.setColumnWidth(0, 310); self.items.setColumnWidth(1, 170); self.items.setColumnWidth(2, 220)
        self.items.itemDoubleClicked.connect(self.open_selected); self.items.itemSelectionChanged.connect(self.selection_changed)
        self.open_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Return), self.items)
        self.open_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut); self.open_shortcut.activated.connect(self.open_selected)
        self.enter_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Enter), self.items)
        self.enter_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut); self.enter_shortcut.activated.connect(self.open_selected)
        library.addWidget(self.items, 1)
        self.completion = plain_label('选中任务可查看完成条件。双击或按 Enter 打开任务。', 'Quiet'); library.addWidget(self.completion)
        actions = ActionRow()
        self.open_button = make_button('打开任务', self.open_selected)
        self.edit_button = make_button('编辑…', self.edit_selected)
        self.split_button = make_button('拆成几个小任务…', self.split_selected)
        for button in (self.open_button, self.edit_button, self.split_button): actions.addWidget(button)
        library.addWidget(actions)
        outcomes = ActionRow()
        self.plan_button = make_button('加入今天', self.plan_today)
        self.complete_button = make_button('标记完成', self.complete_selected)
        self.complete_button.setToolTip('为所选任务记录今天的完成结果；不改变出勤、提交和掌握记录。')
        self.plan_retry = make_button('核对上次加入结果', self.retry_plan); self.plan_retry.hide()
        self.completion_retry = make_button('核对上次完成结果', self.retry_completion); self.completion_retry.hide()
        self.library_plan_button = make_button('查看当天安排', self.open_blocked_plan); self.library_plan_button.hide()
        for button in (self.plan_button, self.complete_button, self.plan_retry, self.completion_retry, self.library_plan_button): outcomes.addWidget(button)
        library.addWidget(outcomes)
        self.operation_note = plain_label('', 'Quiet'); library.addWidget(self.operation_note)
        paging = ActionRow(); self.page_note = plain_label('', 'Quiet'); paging.addWidget(self.page_note)
        self.previous = make_button('上一页', self.previous_page)
        self.next = make_button('下一页', lambda: self.set_page((self.result or {}).get('next_offset')))
        paging.addWidget(self.previous); paging.addWidget(self.next); library.addWidget(paging)
        # Compatibility for callers of edit_today; the full-day editor belongs to Today.
        self.editor_button = make_button('详细安排…', self.edit_today, parent=self); self.editor_button.hide()
        self.stack.setCurrentIndex(0); self.selection_changed()

    def pending_writes(self):
        return any((self.quick_pending, self.quick_uncertain, self.plan_pending, self.plan_uncertain, self.completion_pending, self.completion_uncertain))

    def show_capture(self):
        self.stack.setCurrentIndex(0)

    def open_library(self, *_):
        self.stack.setCurrentIndex(1)
        if not self.library_loaded: self.refresh(force=True)

    def selected(self):
        item = self.items.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def filter_changed(self, *_):
        self.search_timer.stop(); self.offset = 0; self.page_history.clear(); self.refresh(force=True)

    def choose_owner(self):
        from .schemas import TYPES
        picker = EntityPicker(self.bridge, self, allowed_types=TYPES['task']['parent_types'])
        if picker.exec() and picker.selected:
            self.owner = picker.selected; self.owner_button.setText('更换所属位置…'); self.owner_button.setToolTip(self.owner['title'])
            self.clear_owner_button.show(); self.filter_changed()
        picker.deleteLater()

    def clear_owner(self):
        self.owner = None; self.owner_button.setText('所属位置：全部'); self.owner_button.setToolTip(''); self.clear_owner_button.hide(); self.filter_changed()

    def clear_batch(self):
        self.task_ids = None; self.clear_batch_button.hide(); self.filter_changed()

    def reset_filters(self):
        for widget in (self.search_input, self.group_picker): widget.blockSignals(True)
        self.search_input.clear(); self.group_picker.setCurrentIndex(0)
        for widget in (self.search_input, self.group_picker): widget.blockSignals(False)
        self.owner = None; self.task_ids = None; self.owner_button.setText('所属位置：全部'); self.owner_button.setToolTip('')
        self.clear_owner_button.hide(); self.clear_batch_button.hide(); self.filter_changed()

    def set_page(self, offset):
        if offset is not None:
            if offset > self.offset: self.page_history.append(self.offset)
            self.offset = offset; self.refresh(force=True)

    def previous_page(self):
        self.offset = self.page_history.pop() if self.page_history else 0; self.refresh(force=True)

    def refresh(self, *, force=False):
        if self.dead: return
        self.refresh_recent()
        if not force and not self.library_loaded and self.stack.currentIndex() == 0: return
        self.generation += 1; generation = self.generation
        selected = (self.selected() or {}).get('id'); scroll = self.items.verticalScrollBar().value()
        group, search, owner, task_ids = self.group_picker.currentData(), self.search_input.text().strip(), copy.deepcopy(self.owner), self.task_ids
        self.range_note.setText(GROUP_HELP[group])
        def loaded(result):
            if self.dead or generation != self.generation: return
            self.result = result; self.library_loaded = True
            if self.offset and not result['items']:
                self.offset = self.page_history.pop() if self.page_history else 0; self.refresh(force=True); return
            self.items.clear()
            for task in result['items']:
                data = task['data']; dates = []
                if data.get('scheduled_date'): dates.append('希望处理 ' + data['scheduled_date'])
                if data.get('due_date'): dates.append('截止 ' + data['due_date'])
                if not dates: dates.append('任务未填日期')
                if task['in_plan']: dates.append('已在今天计划')
                state = task.get('completion_state') or task.get('status')
                item = QTreeWidgetItem([task.get('display_title') or task['title'], task['owner_label'], ' · '.join(dates), label_status(state)])
                item.setData(0, Qt.ItemDataRole.UserRole, task)
                for column in range(4): item.setToolTip(column, item.text(column))
                self.items.addTopLevelItem(item)
                if task['id'] == selected: self.items.setCurrentItem(item)
            if self.items.topLevelItemCount() and not self.items.currentItem(): self.items.setCurrentItem(self.items.topLevelItem(0))
            self.items.verticalScrollBar().setValue(scroll)
            QTimer.singleShot(0, self, lambda: self.items.verticalScrollBar().setValue(scroll)
                              if not self.dead and generation == self.generation else None)
            conditions = [dict(GROUPS)[group]]
            if owner: conditions.append(owner['title'] + '及其下属任务')
            if search: conditions.append('名称包含“' + search + '”')
            if task_ids is not None: conditions.append(f'本次记录 {len(task_ids)} 项')
            self.status.setText('正在查看：' + ' · '.join(conditions) + (f"\n找到 {result['total']} 项。" if result['items'] else '\n没有符合条件的任务；可清除查看条件重新查找。'))
            total, count = result['total'], len(result['items'])
            self.page_note.setText(f'第 {self.offset + 1}—{self.offset + count} 项，共 {total} 项' if count else f'共 {total} 项')
            self.previous.setEnabled(self.offset > 0); self.next.setEnabled(result['next_offset'] is not None); self.selection_changed()
        def failed(error):
            if not self.dead and generation == self.generation:
                self.status.setText('读取失败，可重新选择查看条件；已有输入保留。'); self.error.emit(error)
        params = dict(group=group, sort=self.sort_picker.currentData(), search=search, owner_id=owner['id'] if owner else None, offset=self.offset, limit=30)
        if task_ids is not None: params['task_ids'] = task_ids
        self.bridge.query('task_pool', loaded, failed, **params)

    def selection_changed(self):
        task = self.selected(); locked = self.plan_pending or self.plan_uncertain or self.completion_pending or self.completion_uncertain
        self.open_button.setEnabled(bool(task)); self.edit_button.setEnabled(bool(task) and not locked)
        from .gui_task_batch import can_split_task
        self.split_button.setEnabled(can_split_task(task) and not locked)
        self.plan_button.setEnabled(bool(task) and not locked and not task.get('in_plan') and task.get('completion_state') != 'done')
        self.plan_button.setText('已在今天计划' if task and task.get('in_plan') else '加入今天')
        self.complete_button.setEnabled(bool(task) and not locked and task.get('completion_state') != 'done')
        self.complete_button.setText('已完成' if task and task.get('completion_state') == 'done' else '标记完成')
        self.completion.setText(('所选任务：' + task['title'] + '\n完成条件：' + str(task['data'].get('completion_gate') or '尚未填写，可在编辑中补充。')) if task else '选中任务可查看完成条件。双击或按 Enter 打开任务。')
        for identifier, button in self.recent_plan_buttons.items():
            task = next((t for t in self.recent_tasks if t['id'] == identifier), {})
            button.setEnabled(not locked and not task.get('in_plan') and task.get('completion_state') != 'done' and not task.get('unavailable'))

    def refresh_recent(self):
        """Only the five session captures; a date change can re-enable today's action."""
        if not self.recent_tasks or self.pending_writes(): return
        self.recent_generation += 1; generation = self.recent_generation
        identifiers = [task['id'] for task in self.recent_tasks]
        def loaded(result):
            if self.dead or generation != self.recent_generation or self.pending_writes(): return
            rows = {task['id']: task for task in result.get('items', [])}
            for task in self.recent_tasks:
                current = rows.get(task['id'])
                if current:
                    task.update(in_plan=current.get('in_plan', False), completion_state=current.get('completion_state'), unavailable=False)
                else: task['unavailable'] = True
                button = self.recent_plan_buttons.get(task['id'])
                if button:
                    button.setText('任务已不可安排' if task.get('unavailable') else '已完成' if task.get('completion_state') == 'done' else '已在今天安排中' if task.get('in_plan') else '加入今天')
            self.selection_changed()
        self.bridge.query('task_pool', loaded, lambda _error: None, group='all', task_ids=identifiers, limit=5)

    def open_selected(self, *_):
        if self.selected() and self.on_task: self.on_task(self.selected()['id'])

    def edit_selected(self, *_):
        if self.selected() and self.on_edit: self.on_edit(self.selected())

    def open_batch(self, *_):
        # Capture is independent of hidden library filters.
        self.open_batch_dialog()

    def split_selected(self, *_):
        from .gui_task_batch import can_split_task
        if can_split_task(self.selected()): self.open_batch_dialog(source=self.selected())

    def show_batch_tasks(self, ids):
        self.reset_filters(); self.task_ids = list(ids); self.clear_batch_button.show()
        self.group_picker.blockSignals(True); self.group_picker.setCurrentIndex(self.group_picker.findData('all')); self.group_picker.blockSignals(False)
        self.items.clear(); self.result = None; self.selection_changed()
        self.stack.setCurrentIndex(1); self.filter_changed()

    def plan_batch(self, ids):
        if self.on_plan:
            self.bridge.query('task_pool', lambda result: self.on_plan(result['date'], task_ids=list(ids)) if not self.dead else None, self.error.emit, limit=1)

    def open_batch_dialog(self, *, source=None, parent_entity=None):
        if self.dead: return
        from .gui_task_batch import TaskBatchDialog
        def saved(receipt):
            if self.dead: return
            self.refresh()
            if self.on_changed: self.on_changed(receipt)
        dialog = TaskBatchDialog(self.bridge, self, source=source, parent_entity=parent_entity, on_saved=saved, on_open=self.on_task,
                                 on_saved_ids=self.show_batch_tasks, on_plan=self.plan_batch)
        self.dialogs.append(dialog); dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.finished.connect(lambda *_: self.dialogs.remove(dialog) if dialog in self.dialogs else None); dialog.open()

    def edit_today(self):
        if self.on_plan: self.bridge.query('task_pool', lambda result: self.on_plan(result['date']) if not self.dead else None, self.error.emit, limit=1)

    def open_blocked_plan(self):
        if self.on_plan and self.blocked_plan_day: self.on_plan(self.blocked_plan_day)

    def _submission(self, name, payload, snapshot=None):
        snapshot = snapshot or {}
        return dict(name=name, payload=copy.deepcopy(payload), epoch=snapshot.get('epoch', self.bridge.epoch),
                    expected_revision=snapshot.get('revision', self.bridge.revision), request_id=str(uuid.uuid4()))

    def _send(self, submission, saved, failed):
        self.bridge.command(submission['name'], copy.deepcopy(submission['payload']), saved, failed,
                            **{key: submission[key] for key in ('epoch', 'expected_revision', 'request_id')})

    def _unknown(self, error, previous):
        return write_result_unknown(error, previous)

    def save_quick(self):
        if self.dead or self.quick_pending: return
        title = self.quick_input.text().strip()
        if not self.quick_uncertain:
            if not title: self.quick_note.setText('先写下要做的事。'); return
            self.quick_submission = self._submission('create', {'type': 'task', 'title': title, 'data': {}})
        title = self.quick_submission['payload']['title']
        self.quick_pending = True; self.quick_input.setReadOnly(True); self.quick_button.setEnabled(False); self.quick_note.setText('正在核对上次记录…' if self.quick_uncertain else '正在保存…')
        if self.last_quick_task_id: self.quick_open_button.setText('查看上次成功记录')
        def saved(receipt):
            if self.dead: return
            self.quick_pending = self.quick_uncertain = False; self.quick_submission = None
            self.quick_input.setReadOnly(False); self.quick_button.setEnabled(True); self.quick_button.setText('记下任务')
            if self.quick_input.text().strip() == title: self.quick_input.clear()
            entity = (receipt.get('result') or {}).get('entity') or {}; identifier = entity.get('id')
            self.last_quick_task_id = identifier if isinstance(identifier, str) and identifier else None
            self.quick_open_button.setVisible(bool(self.last_quick_task_id) and self.on_task is not None)
            self.quick_open_button.setText('查看刚记录任务'); self.quick_open_button.setToolTip('打开最近一次成功记录的任务：' + str(entity.get('title') or title))
            self.quick_note.setText('已记下：' + title + '，尚未加入每日安排。')
            if self.last_quick_task_id:
                self.recent_tasks.insert(0, {**entity, 'title': entity.get('title') or title}); self.recent_tasks = self.recent_tasks[:5]; self.render_recent()
            self.quick_input.setFocus(); self.refresh()
            if self.on_changed: self.on_changed(receipt)
        def failed(error):
            if self.dead: return
            self.quick_pending = False; self.quick_uncertain = self._unknown(error, self.quick_uncertain)
            self.quick_input.setReadOnly(self.quick_uncertain); self.quick_button.setEnabled(True)
            self.quick_button.setText('核对上次记录' if self.quick_uncertain else '记下任务')
            self.quick_note.setText(('暂时无法确认保存结果，输入已保留。核对同一次请求，不会重复创建。' if self.quick_uncertain else '保存未完成，输入已保留。') + error.get('message', ''))
            if not self.quick_uncertain: self.quick_submission = None
            self.error.emit(error)
        self._send(self.quick_submission, saved, failed)

    def render_recent(self):
        clear_layout(self.recent_layout); self.recent_plan_buttons = {}; self.recent_notes = {}
        self.recent_layout.addWidget(plain_label('本次刚记录', 'SectionHeading'))
        for task in self.recent_tasks:
            row = QFrame(); row.setObjectName('TaskRow'); inner = QVBoxLayout(row)
            inner.addWidget(plain_label(task['title'], 'SectionHeading'))
            note = plain_label(task.get('capture_note') or '已记下，尚未加入每日安排。', 'Quiet'); inner.addWidget(note)
            actions = ActionRow(); inner.addWidget(actions)
            button = make_button('已在当天安排中' if task.get('in_plan') else '加入今天', lambda _=False, t=task: self.add_today(t))
            self.recent_plan_buttons[task['id']] = button; self.recent_notes[task['id']] = note; actions.addWidget(button)
            if self.on_task: actions.addWidget(make_button('打开任务', lambda _=False, i=task['id']: self.on_task(i)))
            self.recent_layout.addWidget(row)
        self.selection_changed()

    def open_recent_quick(self):
        if not self.dead and self.last_quick_task_id and self.on_task: self.on_task(self.last_quick_task_id)

    def plan_today(self):
        if self.selected() and self.plan_button.isEnabled(): self.add_today(self.selected())

    def _plan_note(self, task_id, text):
        self.operation_note.setText(text); self.quick_note.setText(text)
        for task in self.recent_tasks:
            if task['id'] == task_id: task['capture_note'] = text
        if task_id in self.recent_notes: self.recent_notes[task_id].setText(text)

    def add_today(self, task):
        if self.dead or self.plan_pending or self.plan_uncertain: return
        self.recent_generation += 1
        self.plan_pending = True; self.selection_changed(); self._plan_note(task['id'], '正在读取今天的安排…')
        def loaded(snapshot):
            if self.dead: return
            plan = snapshot.get('plan') or {}
            self.plan_submission = self._submission('add_to_plan', {'date': snapshot['date'], 'target_id': task['id'], 'plan_id': plan.get('id'), 'plan_version': plan.get('version')}, snapshot)
            self._send_plan()
        def failed(error):
            if self.dead: return
            self.plan_pending = False; self.selection_changed(); self._plan_note(task['id'], '任务已记下，尚未加入今天；可以重试加入。' + error.get('message', '')); self.error.emit(error)
        self.bridge.query('task_pool', loaded, failed, limit=1)

    def retry_plan(self):
        if not self.dead and self.plan_uncertain and not self.plan_pending: self.plan_pending = True; self._send_plan()

    def _send_plan(self):
        submission = self.plan_submission; identifier = submission['payload']['target_id']; day = submission['payload']['date']
        self.rest_plan_button.hide(); self.library_plan_button.hide()
        self.selection_changed(); self.plan_retry.setEnabled(False); self.hub_plan_retry.setEnabled(False); self._plan_note(identifier, '正在核对加入结果…' if self.plan_uncertain else '正在加入 ' + day + '…')
        def saved(receipt):
            if self.dead: return
            self.plan_pending = self.plan_uncertain = False; self.plan_submission = None; self.plan_retry.hide(); self.hub_plan_retry.hide()
            self._plan_note(identifier, '已加入 ' + day + ' 的安排。')
            for task in self.recent_tasks:
                if task['id'] == identifier: task['in_plan'] = True
            if identifier in self.recent_plan_buttons: self.recent_plan_buttons[identifier].setText('已在当天安排中')
            self.selection_changed(); self.refresh()
            if self.on_changed: self.on_changed(receipt)
        def failed(error):
            if self.dead: return
            self.plan_pending = False; self.plan_uncertain = self._unknown(error, self.plan_uncertain)
            self.plan_retry.setVisible(self.plan_uncertain); self.plan_retry.setEnabled(True)
            self.hub_plan_retry.setVisible(self.plan_uncertain); self.hub_plan_retry.setEnabled(True)
            if not self.plan_uncertain: self.plan_submission = None
            if error.get('code') == 'rest_plan' and self.on_plan:
                self.blocked_plan_day = day; self.rest_plan_button.show(); self.library_plan_button.show()
            self._plan_note(identifier, ('任务已记下，加入结果尚待核对；请核对原请求。' if self.plan_uncertain else '任务已记下，尚未加入今天；可以重试加入。') + error.get('message', ''))
            self.selection_changed(); self.error.emit(error)
        self._send(submission, saved, failed)

    def complete_selected(self):
        task = self.selected()
        if self.dead or not task or not self.complete_button.isEnabled(): return
        self.completion_pending = True; self.selection_changed(); self.operation_note.setText('正在核对今天的日期…')
        def loaded(snapshot):
            if self.dead: return
            self.completion_submission = self._submission('set_task_completion', {'target_id': task['id'], 'target_version': task['version'], 'business_date': snapshot['date'], 'result': 'done'}, snapshot)
            self._send_completion()
        def failed(error):
            if self.dead: return
            self.completion_pending = False; self.selection_changed(); self.operation_note.setText('尚未记录完成。' + error.get('message', '')); self.error.emit(error)
        self.bridge.query('task_pool', loaded, failed, limit=1)

    def retry_completion(self):
        if not self.dead and self.completion_uncertain and not self.completion_pending:
            self.completion_pending = True; self._send_completion()

    def _send_completion(self):
        submission = self.completion_submission
        self.selection_changed(); self.completion_retry.setEnabled(False); self.operation_note.setText('正在保存完成结果…')
        def saved(receipt):
            if self.dead: return
            self.completion_pending = self.completion_uncertain = False; self.completion_submission = None; self.completion_retry.hide()
            self.operation_note.setText('已保存 ' + submission['payload']['business_date'] + ' 的完成结果。'); self.refresh(); self.selection_changed()
            if self.on_changed: self.on_changed(receipt)
        def failed(error):
            if self.dead: return
            self.completion_pending = False; self.completion_uncertain = self._unknown(error, self.completion_uncertain)
            self.completion_retry.setVisible(self.completion_uncertain); self.completion_retry.setEnabled(True)
            if not self.completion_uncertain: self.completion_submission = None
            self.operation_note.setText(('完成结果尚待核对，请核对原请求。' if self.completion_uncertain else '完成结果未保存，请刷新后再试。') + error.get('message', ''))
            self.selection_changed(); self.error.emit(error)
        self._send(submission, saved, failed)
