"""Reviewable manual task batches, with one stable request for uncertain saves."""
from __future__ import annotations

import copy
import csv
import io
import uuid

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QTextBrowser, QTextEdit,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QScrollArea, QFrame, QSizePolicy, QLayout,
)

from .gui_forms import EntityPicker
from .gui_layout import ActionRow
from .schemas import TYPES


MAX_ITEMS = 50
FIELDS = ('title', 'completion_gate', 'estimated_minutes', 'due_date', 'scheduled_date')


def parse_task_rows(text):
    """Parse pasted lines/TSV as literal user content; never infer task facts."""
    rows = []
    for cells in csv.reader(io.StringIO(text), delimiter='\t'):
        if not cells or not any(cell.strip() for cell in cells):
            continue
        if len(cells) > len(FIELDS):
            raise ValueError('每行最多五列：标题、完成标准、分钟、截止日期、安排日期。')
        rows.append([cell.strip() for cell in cells] + [''] * (len(FIELDS) - len(cells)))
    if len(rows) > MAX_ITEMS:
        raise ValueError('每批最多 50 项，请分批录入。')
    return rows


def can_split_task(entity):
    completion = entity.get('completion_state') if entity else None
    completed = completion == 'done' if completion is not None else (entity or {}).get('status') in {'done', 'completed'}
    return bool(entity and entity.get('type') == 'task' and not entity.get('archived')
                and entity.get('status') not in {'cancelled', 'draft'} and not completed)


class TaskBatchDialog(QDialog):
    def __init__(self, bridge, parent=None, *, source=None, parent_entity=None, on_saved=None, on_open=None):
        super().__init__(parent)
        self.bridge, self.source, self.parent_entity = bridge, source, parent_entity
        self.on_saved, self.on_open = on_saved, on_open
        self.mode = 'split' if source else 'create'
        self.closed = self.dirty = self.saving = self.uncertain = self.previewing = self.saved = False
        self.generation = self.source_generation = 0
        self.preview_result = self.submission = None
        self.request_id = None
        self.source_loading = bool(source)
        self.dialogs = []
        self.finished.connect(lambda *_: setattr(self, 'closed', True))
        self.destroyed.connect(lambda *_: setattr(self, 'closed', True))
        self.setWindowTitle('拆分任务' if source else '批量录入任务')
        self.resize(980, 790)
        layout = QVBoxLayout(self)
        heading = QLabel(self.windowTitle()); heading.setObjectName('DialogHeading'); layout.addWidget(heading)
        self.context = QLabel(); self.context.setWordWrap(True); self.context.setTextFormat(Qt.TextFormat.PlainText)
        self.context.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.context.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.context_scroll = QScrollArea(); self.context_scroll.setWidgetResizable(True); self.context_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.context_scroll.setWidget(self.context); self.context_scroll.setMinimumHeight(90 if source else 40); self.context_scroll.setMaximumHeight(100)
        layout.addWidget(self.context_scroll)
        self.editor = QWidget(); editor = QVBoxLayout(self.editor); editor.setContentsMargins(0, 0, 0, 0)
        editor.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        ownership = QHBoxLayout()
        self.owner_button = QPushButton('重新读取原任务' if source else '选择共同归属（可选）')
        self.owner_button.clicked.connect(self.reload_source if source else self.choose_parent)
        ownership.addWidget(self.owner_button)
        if not source:
            clear_owner = QPushButton('取消归属'); clear_owner.clicked.connect(self.clear_parent); ownership.addWidget(clear_owner)
        ownership.addStretch(); editor.addLayout(ownership)
        self.paste = QTextEdit(); self.paste.setMaximumHeight(85)
        self.paste.setPlaceholderText('每行一项任务；也可粘贴表格五列：标题、完成标准、分钟、截止日期、安排日期。日期填 YYYY-MM-DD，未知可留空。')
        self.paste.textChanged.connect(self.input_changed); editor.addWidget(self.paste)
        paste_actions = ActionRow()
        self.import_button = QPushButton('将文本加入下方表格'); self.import_button.clicked.connect(self.import_rows); paste_actions.addWidget(self.import_button)
        self.add_button = QPushButton('增加一行'); self.add_button.clicked.connect(self.add_row); paste_actions.addWidget(self.add_button)
        self.remove_button = QPushButton('删除选中行'); self.remove_button.clicked.connect(self.remove_rows); paste_actions.addWidget(self.remove_button)
        self.up_button = QPushButton('上移'); self.up_button.clicked.connect(lambda:self.move_row(-1)); paste_actions.addWidget(self.up_button)
        self.down_button = QPushButton('下移'); self.down_button.clicked.connect(lambda:self.move_row(1)); paste_actions.addWidget(self.down_button)
        self.count = QLabel('0 / 50 项'); paste_actions.addWidget(self.count); editor.addWidget(paste_actions)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['任务标题', '完成标准' + ('（必填）' if source else ''), '预计分钟', '截止日期', '安排日期'])
        for column in (2,3,4):self.table.horizontalHeaderItem(column).setToolTip('可留空；未知时不推定分钟或日期。日期格式 YYYY-MM-DD。')
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header=self.table.horizontalHeader()
        header.setStretchLastSection(True)
        metrics=header.fontMetrics()
        minimum=max(metrics.horizontalAdvance(self.table.horizontalHeaderItem(column).text()) for column in (2,3,4))+28
        header.setMinimumSectionSize(minimum)
        for column, width in enumerate([225, 290, 105, 140]): self.table.setColumnWidth(column, max(width,minimum))
        self.table.setMinimumHeight(200); self.table.itemChanged.connect(self.input_changed); editor.addWidget(self.table, 1)
        self.sequential = QCheckBox('依次完成：后一项以前一项为前置任务')
        self.sequential.toggled.connect(self.input_changed); editor.addWidget(self.sequential)
        help_text = ('原任务和完成标准保留；子任务继承原任务的前置依赖。拆分不会替换已有计划，也不会改变完成记录或自动完成原任务。'
                     if source else '每行保存为独立任务。填写安排日期只登记任务日期，不自动生成每日计划；不会新增完成记录。')
        help_label = QLabel(help_text); help_label.setWordWrap(True); editor.addWidget(help_label)
        self.editor_scroll=QScrollArea();self.editor_scroll.setWidgetResizable(True);self.editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.editor_scroll.setWidget(self.editor);layout.addWidget(self.editor_scroll,2)
        self.preview = QTextBrowser(); self.preview.setMinimumHeight(110); self.preview.setMaximumHeight(200)
        self.preview.setPlainText('完成表格后点击“预览”。核对任务、日期和依赖后，再一次保存。'); layout.addWidget(self.preview, 1)
        self.results = QTreeWidget(); self.results.setHeaderLabels(['已保存任务', '完成标准', '前置关系'])
        self.results.setColumnWidth(0, 260); self.results.setColumnWidth(1, 320)
        self.results.setRootIsDecorated(False); self.results.itemDoubleClicked.connect(self.open_result); self.results.hide(); layout.addWidget(self.results, 1)
        self.status = QLabel(); self.status.setWordWrap(True); self.status.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.preview_button = QPushButton('预览'); self.preview_button.clicked.connect(self.request_preview); actions.addWidget(self.preview_button)
        self.save_button = QPushButton('核对无误，一次保存'); self.save_button.setObjectName('Primary'); self.save_button.clicked.connect(self.save); actions.addWidget(self.save_button)
        self.open_button = QPushButton('打开选中任务'); self.open_button.clicked.connect(self.open_result); self.open_button.hide(); actions.addWidget(self.open_button)
        actions.addStretch(); self.close_button = QPushButton('关闭'); self.close_button.clicked.connect(self.reject); actions.addWidget(self.close_button); layout.addLayout(actions)
        self.render_context(); self.update_controls()
        if source: self.reload_source()

    def render_context(self, child_count=None):
        if self.source:
            standard = self.source.get('data', {}).get('completion_gate') or self.source.get('data', {}).get('acceptance') or '尚未填写'
            message = '原任务：' + self.source['title'] + '\n原完成标准：' + str(standard)
            if child_count is not None:
                message += f'\n已有 {child_count} 项子任务；本次会继续新增，不替换或覆盖已有子任务。'
            if not can_split_task(self.source): message += '\n该任务当前不可拆分，请重新读取并检查完成状态。'
        else:
            message = '共同归属：' + (self.parent_entity['title'] if self.parent_entity else '未归属，可以后分别调整')
        self.context.setText(message)

    def input_changed(self, *_):
        if self.saving or self.uncertain or self.saved: return
        self.dirty = True; self.generation += 1; self.previewing = False
        self.preview_result = self.submission = None; self.request_id = None
        self.preview.setPlainText('输入已改变，请重新预览后保存。')
        self.status.clear(); self.update_controls()

    def update_controls(self):
        editable = not (self.saving or self.uncertain or self.saved)
        self.editor.setEnabled(editable)
        self.count.setText(f'{self.table.rowCount()} / {MAX_ITEMS} 项')
        self.add_button.setEnabled(editable and self.table.rowCount() < MAX_ITEMS)
        allowed = not self.source or can_split_task(self.source)
        self.preview_button.setEnabled(editable and not self.previewing and not self.source_loading and allowed)
        self.save_button.setEnabled(not self.saving and not self.saved and (self.uncertain or self.preview_result is not None))
        self.save_button.setText('核对并重试原保存' if self.uncertain else '核对无误，一次保存')
        self.close_button.setEnabled(not self.saving)

    def add_row(self, *_ , values=None):
        if self.saving or self.uncertain or self.saved or self.table.rowCount() >= MAX_ITEMS: return
        self.table.blockSignals(True)
        row = self.table.rowCount(); self.table.insertRow(row)
        for column, value in enumerate(values or [''] * 5): self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.table.blockSignals(False); self.input_changed()

    def import_rows(self):
        if self.saving or self.uncertain or self.saved: return
        try:
            rows = parse_task_rows(self.paste.toPlainText())
            if not rows: raise ValueError('请先输入至少一行任务。')
            if self.table.rowCount() + len(rows) > MAX_ITEMS: raise ValueError('加入后超过 50 项，请先减少行数或分批录入。')
        except (ValueError, csv.Error) as exc:
            self.status.setText(str(exc)); return
        for row in rows: self.add_row(values=row)
        self.paste.clear(); self.input_changed()

    def remove_rows(self):
        if self.saving or self.uncertain or self.saved: return
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        for row in sorted(rows, reverse=True): self.table.removeRow(row)
        self.input_changed()

    def move_row(self, step):
        if self.saving or self.uncertain or self.saved: return
        rows = self.table.selectionModel().selectedRows()
        if len(rows)!=1:
            self.status.setText('请只选中一行，再上移或下移。');return
        source=rows[0].row();target=source+step
        if not 0<=target<self.table.rowCount():return
        self.table.blockSignals(True)
        for column in range(len(FIELDS)):
            left,right=self.table.takeItem(source,column),self.table.takeItem(target,column)
            self.table.setItem(source,column,right);self.table.setItem(target,column,left)
        self.table.blockSignals(False);self.table.setCurrentCell(target,0);self.table.selectRow(target)
        self.input_changed()

    def choose_parent(self):
        if self.saving or self.uncertain or self.saved: return
        picker = EntityPicker(self.bridge, self, allowed_types=[kind for kind in TYPES['task']['parent_types'] if kind is not None])
        try:
            if picker.exec() == QDialog.DialogCode.Accepted:
                self.parent_entity = picker.selected; self.render_context(); self.input_changed()
        finally: picker.deleteLater()

    def clear_parent(self):
        if self.saving or self.uncertain or self.saved: return
        self.parent_entity = None; self.render_context(); self.input_changed()

    def reload_source(self):
        if not self.source or self.saving or self.uncertain or self.saved: return
        was_dirty=self.dirty
        self.input_changed(); self.dirty=was_dirty
        self.source_generation += 1; generation = self.source_generation
        self.source_loading = True; self.update_controls()
        def loaded(result):
            if self.closed or generation != self.source_generation: return
            self.source = result['entity']; self.source_loading = False; self.render_context(); self.update_controls()
            def children(value):
                if not self.closed and generation == self.source_generation: self.render_context(value.get('total', 0))
            self.bridge.query('list', children, None, type='task', parent_id=self.source['id'], limit=1)
        def failed(error):
            if self.closed or generation != self.source_generation: return
            self.source_loading = True; self.status.setText('原任务读取失败，输入保留。' + error.get('message', str(error))); self.update_controls()
        self.bridge.query('get', loaded, failed, id=self.source['id'], display=True)

    def collect_items(self):
        if self.paste.toPlainText().strip(): raise ValueError('上方还有未加入表格的文本，请先加入表格或清空，避免遗漏。')
        if not 1 <= self.table.rowCount() <= MAX_ITEMS: raise ValueError('每批需有 1 至 50 项任务。')
        if self.source and self.table.rowCount() < 2: raise ValueError('拆分至少需要两项子任务。')
        items = []
        for row in range(self.table.rowCount()):
            cells = [(self.table.item(row, column).text().strip() if self.table.item(row, column) else '') for column in range(5)]
            if not cells[0]: raise ValueError(f'第 {row + 1} 行需要填写任务标题。')
            if self.source and not cells[1]: raise ValueError(f'第 {row + 1} 行需要独立的完成标准。')
            item = {'title': cells[0], 'completion_gate': cells[1]}
            if cells[2]:
                if not cells[2].isascii() or not cells[2].isdigit() or not 1 <= int(cells[2]) <= 1440:
                    raise ValueError(f'第 {row + 1} 行分钟数应为 1 至 1440 的整数；未知请留空。')
                item['estimated_minutes'] = int(cells[2])
            for column, key in [(3, 'due_date'), (4, 'scheduled_date')]:
                if cells[column]:
                    date = QDate.fromString(cells[column], 'yyyy-MM-dd')
                    if not date.isValid() or date.toString('yyyy-MM-dd') != cells[column]:
                        raise ValueError(f'第 {row + 1} 行日期应为 YYYY-MM-DD；未知请留空。')
                    item[key] = cells[column]
            items.append(item)
        return items

    def request_preview(self):
        if self.closed or self.saving or self.uncertain or self.saved or self.source_loading or self.previewing: return
        if self.source and not can_split_task(self.source): return
        try: items = self.collect_items()
        except ValueError as exc: self.status.setText(str(exc)); return
        self.generation += 1; generation = self.generation; self.preview_result = None
        self.previewing = True; self.status.setText('正在核对任务与依赖…'); self.update_controls()
        params = {'mode': self.mode, 'items': items, 'sequential': self.sequential.isChecked()}
        if self.source: params.update(source_id=self.source['id'], source_version=self.source['version'])
        elif self.parent_entity: params.update(parent_id=self.parent_entity['id'], parent_version=self.parent_entity['version'])
        def loaded(result):
            if self.closed or generation != self.generation: return
            self.previewing = False; self.preview_result = copy.deepcopy(result); self.request_id = str(uuid.uuid4())
            lines = [f'将创建 {len(result.get("items", items))} 项独立任务。']
            owner = result.get('source') if self.source else result.get('parent')
            if owner: lines.append('归属：' + owner.get('title', '所选事项'))
            dependencies=result.get('dependencies', [])
            if dependencies:
                lines.append('每项子任务继承的前置要求：'+'、'.join(dep['title']+('（已完成）' if dep.get('completed') else '（未完成）') for dep in dependencies))
            if self.source and owner: self.render_context(owner.get('children_count'))
            lines.append('相邻任务将按表格顺序建立前后依赖。' if self.sequential.isChecked() else '不新增这些任务之间的相互依赖。')
            for index, item in enumerate(result.get('items', items), 1):
                data = item.get('data', item)
                details = [f'{index}. {item.get("title", data.get("title", "任务"))}', '完成标准：' + str(data.get('completion_gate') or '尚未填写')]
                for key, label in [('estimated_minutes', '分钟'), ('due_date', '截止'), ('scheduled_date', '安排日期')]:
                    if data.get(key) is not None: details.append(f'{label}：{data[key]}')
                lines.append(' · '.join(details))
            lines.extend(str(w) for w in result.get('warnings', []))
            self.preview.setPlainText('\n'.join(lines)); self.status.setText('预览尚未写入。请核对后点击“一次保存”。'); self.update_controls()
        def failed(error):
            if self.closed or generation != self.generation: return
            self.previewing = False; self.status.setText('预览未通过，输入仍保留。' + error.get('message', str(error))); self.update_controls()
        self.bridge.query('preview_task_batch', loaded, failed, **params)

    def save(self):
        if self.closed or self.saving or self.saved: return
        if self.uncertain:
            if not self.submission: return
        else:
            if not self.preview_result: return
            value = self.preview_result
            self.submission = {'command': value['command'], 'payload': copy.deepcopy(value['payload']),
                               'epoch': value['epoch'], 'expected_revision': value['revision'], 'request_id': self.request_id}
        self.saving = True; self.status.setText('正在保存这一批任务…'); self.update_controls()
        submission = self.submission
        def failed(error):
            if self.closed: return
            self.saving = False
            code=error.get('code')
            transport_unknown=code in {'connection_lost', 'connection_error', 'timeout', 'request_timeout', 'protocol_error', 'response_limit', 'internal_error'}
            # Core checks the same-epoch receipt before these business failures.
            # A failed reconnect, upgrade, authentication or epoch check cannot
            # prove the earlier request did not commit: retain its exact identity.
            definite_rejection=code in {'revision_conflict','preview_conflict','entity_conflict','validation'}
            self.uncertain = transport_unknown or (self.uncertain and not definite_rejection)
            if self.uncertain:
                self.status.setText('暂时无法确认保存结果。输入已锁定；点击“核对并重试原保存”核对同一次请求，不会重新创建另一批。\n'+error.get('message',str(error)))
            else:
                self.preview_result = None; self.submission = None; self.request_id = None
                self.status.setText('保存未完成，输入仍保留。请重新预览。' + error.get('message', str(error)))
            self.update_controls()
        self.bridge.command(submission['command'], copy.deepcopy(submission['payload']), self.show_saved, failed,
                            epoch=submission['epoch'], expected_revision=submission['expected_revision'], request_id=submission['request_id'])

    def show_saved(self, receipt):
        if self.closed: return
        self.saving = self.uncertain = self.dirty = False; self.saved = True
        result = receipt.get('result', receipt)
        items = result.get('items', result.get('entities', []))
        names = {item['id']: item['title'] for item in items}
        names.update({dep['id']:dep['title'] for dep in (self.preview_result or {}).get('dependencies',[])})
        links = result.get('links', [])
        self.results.clear()
        for item in items:
            dependencies = [link.get('target_title') or names.get(link.get('target_id')) or '原任务的前置要求'
                            for link in links if link.get('kind') == 'depends_on' and link.get('source_id') == item['id']]
            row = QTreeWidgetItem([item['title'], str(item.get('data', {}).get('completion_gate') or '未填写'), '、'.join(dependencies) or '无新增前置关系'])
            row.setData(0, Qt.ItemDataRole.UserRole, item)
            for column in range(3): row.setToolTip(column, row.text(column))
            self.results.addTopLevelItem(row)
        if self.results.topLevelItemCount(): self.results.setCurrentItem(self.results.topLevelItem(0))
        self.results.show(); self.open_button.show(); self.open_button.setEnabled(bool(items))
        self.editor_scroll.hide(); self.preview.hide(); self.preview_button.hide(); self.save_button.hide()
        self.status.setText(f'已保存 {len(items)} 项独立任务。可双击打开查看。' + ('原任务、已有计划和完成记录保持原样。' if self.source else '尚未加入每日计划。'))
        self.update_controls()
        if self.on_saved: self.on_saved(receipt)

    def open_result(self, *_):
        item = self.results.currentItem()
        if not item: return
        entity = item.data(0, Qt.ItemDataRole.UserRole)
        if self.on_open: self.on_open(entity['id'])

    def reject(self):
        if self.saving or self.uncertain:
            self.status.setText('请先核对当前保存结果，再关闭；可重试同一次请求。'); return
        if self.dirty and not self.saved:
            answer = QMessageBox.question(self, '尚未保存', '关闭会放弃此窗口尚未保存的输入。确定关闭？', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes: return
        super().reject()

    def closeEvent(self, event):
        event.ignore(); self.reject()
