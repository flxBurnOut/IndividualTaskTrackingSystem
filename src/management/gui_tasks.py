"""Manual task capture and a paged pool backed by the shared service."""
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QComboBox, QTreeWidget, QTreeWidgetItem, QLabel
from .gui_workspace import make_button, plain_label
from .gui_forms import EntityPicker, label_status


GROUPS = [('open', '待办任务'), ('undated', '未填日期'), ('due', '到期与逾期'),
          ('future', '未来截止'), ('unowned', '未归属'), ('done', '已完成'), ('all', '全部任务')]


class TasksPage(QWidget):
    error = Signal(object)

    def __init__(self, bridge, parent=None, *, on_task=None, on_edit=None, on_changed=None, on_plan=None):
        super().__init__(parent)
        self.bridge, self.on_task, self.on_edit = bridge, on_task, on_edit
        self.on_changed, self.on_plan = on_changed, on_plan
        self.generation, self.offset = 0, 0
        self.page_history = []
        self.owner, self.result, self.dead = None, None, False
        self.quick_pending = self.plan_pending = False
        self.dialogs = []
        self.destroyed.connect(lambda *_: setattr(self, 'dead', True))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(plain_label('先记下来，稍后再安排', 'SectionHeading'))
        quick = QHBoxLayout()
        self.quick_input = QLineEdit()
        self.quick_input.setPlaceholderText('写下一件要做的事；日期、归属和完成标准可以稍后补充')
        self.quick_input.returnPressed.connect(self.save_quick)
        self.quick_button = make_button('记下任务', self.save_quick, True)
        quick.addWidget(self.quick_input, 1)
        quick.addWidget(self.quick_button)
        self.batch_button = make_button('批量录入…', self.open_batch)
        quick.addWidget(self.batch_button)
        layout.addLayout(quick)
        self.quick_note = plain_label('按 Enter 保存后可继续录入。记录任务不会自动安排日期。', 'Quiet')
        layout.addWidget(self.quick_note)
        filters = QHBoxLayout()
        self.group_picker = QComboBox()
        for value, label in GROUPS:
            self.group_picker.addItem(label, value)
        self.group_picker.currentIndexChanged.connect(self.filter_changed)
        filters.addWidget(self.group_picker)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText('搜索全部任务标题')
        self.search_input.setClearButtonEnabled(True)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self.filter_changed)
        self.search_input.textChanged.connect(lambda: self.search_timer.start())
        self.search_input.returnPressed.connect(self.filter_changed)
        filters.addWidget(self.search_input, 1)
        self.owner_button = make_button('筛选归属', self.choose_owner)
        filters.addWidget(self.owner_button)
        filters.addWidget(make_button('重置筛选', self.reset_filters))
        layout.addLayout(filters)
        self.status = plain_label('请选择要查看的任务。', 'Quiet')
        layout.addWidget(self.status)
        self.items = QTreeWidget()
        self.items.setHeaderLabels(['任务', '归属', '日期', '状态'])
        self.items.setRootIsDecorated(False)
        self.items.setColumnWidth(0, 310)
        self.items.setColumnWidth(1, 170)
        self.items.setColumnWidth(2, 220)
        self.items.itemDoubleClicked.connect(self.open_selected)
        self.items.itemSelectionChanged.connect(self.selection_changed)
        layout.addWidget(self.items, 1)
        self.completion = plain_label('选中任务可查看完成标准，或打开详情设置前后依赖。', 'Quiet')
        self.completion.setWordWrap(True)
        layout.addWidget(self.completion)
        actions = QHBoxLayout()
        self.open_button = make_button('任务详情', self.open_selected)
        self.edit_button = make_button('编辑任务', self.edit_selected)
        self.plan_button = make_button('加入今天', self.plan_today)
        self.editor_button = make_button('安排今天…', self.edit_today)
        self.split_button = make_button('拆分任务…', self.split_selected)
        for button in (self.open_button, self.edit_button, self.split_button, self.plan_button, self.editor_button):
            actions.addWidget(button)
        actions.addStretch()
        self.previous = make_button('上一页', self.previous_page)
        self.next = make_button('下一页', lambda: self.set_page((self.result or {}).get('next_offset')))
        actions.addWidget(self.previous)
        actions.addWidget(self.next)
        layout.addLayout(actions)
        self.selection_changed()

    def selected(self):
        item = self.items.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def filter_changed(self, *_):
        self.search_timer.stop()
        self.offset = 0
        self.page_history.clear()
        self.refresh()

    def choose_owner(self):
        picker = EntityPicker(self.bridge, self, allowed_types=['project', 'course', 'activity', 'domain', 'goal'])
        if picker.exec() and picker.selected:
            self.owner = picker.selected
            self.owner_button.setText(self.owner['title'])
            self.filter_changed()
        picker.deleteLater()

    def reset_filters(self):
        self.search_input.blockSignals(True)
        self.group_picker.blockSignals(True)
        self.search_input.clear()
        self.group_picker.setCurrentIndex(0)
        self.search_input.blockSignals(False)
        self.group_picker.blockSignals(False)
        self.owner = None
        self.owner_button.setText('筛选归属')
        self.filter_changed()

    def set_page(self, offset):
        if offset is not None:
            if offset > self.offset:
                self.page_history.append(self.offset)
            self.offset = offset
            self.refresh()

    def previous_page(self):
        self.offset = self.page_history.pop() if self.page_history else 0
        self.refresh()

    def refresh(self):
        if self.dead:
            return
        self.generation += 1
        generation = self.generation
        selected = (self.selected() or {}).get('id')
        def loaded(result):
            if self.dead or generation != self.generation:
                return
            self.result = result
            if self.offset and not result['items']:
                self.offset = self.page_history.pop() if self.page_history else 0
                self.refresh()
                return
            self.items.clear()
            for task in result['items']:
                data = task['data']
                dates = []
                if data.get('scheduled_date'):
                    dates.append('安排 ' + data['scheduled_date'])
                if data.get('due_date'):
                    dates.append('截止 ' + data['due_date'])
                if not dates:
                    dates.append('任务未填日期')
                if task['in_plan']:
                    dates.append('已在今天计划')
                state = task.get('completion_state') or task.get('status')
                item = QTreeWidgetItem([task.get('display_title') or task['title'], task['owner_label'], ' · '.join(dates), label_status(state)])
                item.setData(0, Qt.ItemDataRole.UserRole, task)
                for column in range(4):
                    item.setToolTip(column, item.text(column))
                self.items.addTopLevelItem(item)
                if task['id'] == selected:
                    self.items.setCurrentItem(item)
            if self.items.topLevelItemCount() and not self.items.currentItem():
                self.items.setCurrentItem(self.items.topLevelItem(0))
            self.status.setText(f"{dict(GROUPS)[result['group']]} · 共 {result['total']} 项 · 今天 {result['date']}" + (' · 没有符合筛选的任务' if not result['items'] else ''))
            self.previous.setEnabled(self.offset > 0)
            self.next.setEnabled(result['next_offset'] is not None)
            self.selection_changed()
        def failed(error):
            if not self.dead and generation == self.generation:
                self.status.setText('读取失败，可重新筛选或刷新；已有输入保留。')
                self.error.emit(error)
        self.bridge.query('task_pool', loaded, failed, group=self.group_picker.currentData(),
                          search=self.search_input.text().strip(), owner_id=self.owner['id'] if self.owner else None,
                          offset=self.offset, limit=30)

    def selection_changed(self):
        task = self.selected()
        self.open_button.setEnabled(bool(task))
        self.edit_button.setEnabled(bool(task))
        from .gui_task_batch import can_split_task
        self.split_button.setEnabled(can_split_task(task))
        self.plan_button.setEnabled(bool(task) and not self.plan_pending and not task.get('in_plan') and task.get('completion_state') != 'done')
        self.plan_button.setText('已在今天计划' if task and task.get('in_plan') else '加入今天')
        self.completion.setText(('完成标准：' + str(task['data'].get('completion_gate') or '尚未填写，可在编辑任务中补充。')) if task else '选中任务可查看完成标准，或打开详情设置前后依赖。')

    def open_selected(self, *_):
        if self.selected() and self.on_task:
            self.on_task(self.selected()['id'])

    def edit_selected(self, *_):
        if self.selected() and self.on_edit:
            self.on_edit(self.selected())

    def open_batch(self, *_):
        from .schemas import TYPES
        owner=self.owner if self.owner and self.owner.get('type') in TYPES['task']['parent_types'] else None
        self.open_batch_dialog(parent_entity=owner)

    def split_selected(self, *_):
        from .gui_task_batch import can_split_task
        if can_split_task(self.selected()): self.open_batch_dialog(source=self.selected())

    def open_batch_dialog(self, *, source=None, parent_entity=None):
        if self.dead: return
        from .gui_task_batch import TaskBatchDialog
        def saved(receipt):
            if self.dead: return
            self.refresh()
            if self.on_changed: self.on_changed(receipt)
        dialog = TaskBatchDialog(self.bridge, self, source=source, parent_entity=parent_entity, on_saved=saved, on_open=self.on_task)
        self.dialogs.append(dialog)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.finished.connect(lambda *_: self.dialogs.remove(dialog) if dialog in self.dialogs else None)
        dialog.open()

    def edit_today(self):
        if self.on_plan:
            self.bridge.query('task_pool', lambda result: self.on_plan(result['date']) if not self.dead else None,
                              self.error.emit, limit=1)

    def save_quick(self):
        title = self.quick_input.text().strip()
        if self.dead or self.quick_pending:
            return
        if not title:
            self.quick_note.setText('先写下要做的事。')
            return
        self.quick_pending = True
        self.quick_input.setReadOnly(True)
        self.quick_button.setEnabled(False)
        self.quick_note.setText('正在保存…')
        def finish():
            self.quick_pending = False
            self.quick_input.setReadOnly(False)
            self.quick_button.setEnabled(True)
        def saved(receipt):
            if self.dead:
                return
            finish()
            if self.quick_input.text().strip() == title:
                self.quick_input.clear()
            self.quick_note.setText('已记下：' + title + '。可以继续输入下一件事。')
            self.quick_input.setFocus()
            self.refresh()
            if self.on_changed:
                self.on_changed(receipt)
        def failed(error):
            if self.dead:
                return
            finish()
            self.quick_note.setText('保存未完成，输入已保留。' + error.get('message', ''))
            self.error.emit(error)
        self.bridge.command('create', {'type': 'task', 'title': title, 'data': {}}, saved, failed)

    def plan_today(self):
        task, result = self.selected(), self.result
        if not task or not result or not self.plan_button.isEnabled():
            return
        self.plan_pending = True
        self.selection_changed()
        day = result['date']
        def loaded(snapshot):
            nonlocal day
            if self.dead:
                return
            day = snapshot['date']
            plan = snapshot.get('plan') or {}
            self.bridge.command('add_to_plan', {'date': day, 'target_id': task['id'],
                'plan_id': plan.get('id'), 'plan_version': plan.get('version')}, saved, failed,
                epoch=snapshot.get('epoch', self.bridge.epoch), expected_revision=snapshot.get('revision', self.bridge.revision))
        def saved(receipt):
            if self.dead:
                return
            self.plan_pending = False
            self.quick_note.setText('已加入 ' + day + ' 的计划。')
            self.refresh()
            if self.on_changed:
                self.on_changed(receipt)
        def failed(error):
            if self.dead:
                return
            self.plan_pending = False
            self.selection_changed()
            self.error.emit(error)
        self.bridge.query('task_pool', loaded, failed, limit=1)
