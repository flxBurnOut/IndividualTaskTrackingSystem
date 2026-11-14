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
    QLineEdit, QFormLayout, QStackedWidget, QComboBox,
)

from .gui_forms import EntityPicker, label_type, location_label
from .gui_layout import ActionRow, ReadingLabel
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


class SpreadsheetDraftDialog(QDialog):
    def reject(self):
        if self.input.toPlainText().strip() and QMessageBox.question(
                self,'尚未加入清单','返回会放弃此处尚未加入清单的输入。确定返回？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:return
        super().reject()

    def closeEvent(self,event):
        event.ignore();self.reject()


class TaskEntryCard(QFrame):
    """One task with visible identity and optional, explicitly empty details."""
    def __init__(self, dialog, row, *, split):
        super().__init__()
        self.dialog, self.row, self.split = dialog, row, split
        self.setObjectName('Card')
        layout=QVBoxLayout(self)
        self.title=QLineEdit();self.title.setPlaceholderText('要做什么？' if split else '任务名称')
        self.title.setAccessibleName(f'第 {row+1} 项任务名称')
        layout.addWidget(ReadingLabel(f'子任务 {row+1}' if split else f'任务 {row+1}', 'Muted'))
        layout.addWidget(self.title)
        self.gate=QLineEdit();self.gate.setPlaceholderText('例如：独立完成练习并纠正错题')
        self.gate.setAccessibleName(f'第 {row+1} 项完成条件')
        if split:
            layout.addWidget(ReadingLabel('做到什么算完成？（必填）','Muted'));layout.addWidget(self.gate)
        self.summary=ReadingLabel('', 'Muted');layout.addWidget(self.summary)
        actions=ActionRow()
        self.details_button=QPushButton('补充信息…');self.details_button.setCheckable(True)
        actions.addWidget(self.details_button)
        remove=QPushButton('移除这个子任务' if split else '从本次清单移除')
        remove.clicked.connect(self.remove);actions.addWidget(remove)
        if split:
            up=QPushButton('上移');down=QPushButton('下移')
            up.setEnabled(row>0);down.setEnabled(row<dialog.table.rowCount()-1)
            up.clicked.connect(lambda:self.move(-1));down.clicked.connect(lambda:self.move(1))
            actions.addWidget(up);actions.addWidget(down)
        layout.addWidget(actions)
        self.details=QWidget();form=QFormLayout(self.details)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.minutes=QLineEdit();self.minutes.setPlaceholderText('未估计，可留空；1–1440 分钟')
        self.due=QLineEdit();self.due.setPlaceholderText('未设置，可留空；YYYY-MM-DD')
        self.scheduled=QLineEdit();self.scheduled.setPlaceholderText('未设置，可留空；YYYY-MM-DD')
        if not split:form.addRow('怎样算完成（可选）',self.gate)
        form.addRow('预计用时（分钟）',self.minutes);form.addRow('截止日期',self.due)
        form.addRow('希望处理的日期',self.scheduled)
        form.addRow(ReadingLabel('希望处理的日期只登记候选日期，不会加入当天安排。清空输入即不设置该项。','Muted'))
        self.details.hide();layout.addWidget(self.details)
        self.details_button.toggled.connect(self.details.setVisible)
        self.fields=[self.title,self.gate,self.minutes,self.due,self.scheduled]
        for column,field in enumerate(self.fields):
            field.textChanged.connect(lambda value,c=column:self.dialog.table.item(self.row,c).setText(value))
        self.sync()

    def sync(self):
        if self.row>=self.dialog.table.rowCount():return
        values=[self.dialog.table.item(self.row,c).text() for c in range(5)]
        for field,value in zip(self.fields,values):
            if field.text()!=value:field.blockSignals(True);field.setText(value);field.blockSignals(False)
        entries=[]
        for column,label in ((1,'完成条件'),(2,'预计分钟'),(3,'截止'),(4,'希望处理')):
            if values[column].strip() and not (self.split and column==1):entries.append(f'{label}：{values[column]}')
        self.summary.setText(' · '.join(entries) or '未填写其他信息，可以留空。')

    def remove(self):
        self.dialog.table.selectRow(self.row);self.dialog.remove_rows()

    def move(self,step):
        self.dialog.table.selectRow(self.row);self.dialog.move_row(step)


class TaskBatchDialog(QDialog):
    """Two purpose-built editors sharing only atomic preview/commit machinery.

    ``table`` is an invisible row model retained for literal TSV compatibility;
    it is never presented as the ordinary task-entry interface.
    """
    def __init__(self, bridge, parent=None, *, source=None, parent_entity=None,
                 on_saved=None, on_open=None, on_plan=None, on_saved_ids=None):
        super().__init__(parent)
        self.bridge, self.source, self.parent_entity = bridge, source, parent_entity
        self.on_saved, self.on_open = on_saved, on_open
        self.on_plan, self.on_saved_ids = on_plan, on_saved_ids
        self.mode = 'split' if source else 'create'
        self.closed = self.dirty = self.saving = self.uncertain = self.previewing = self.saved = False
        self.generation = self.source_generation = 0
        self.preview_result = self.submission = None
        self.request_id = None
        self.source_loading = bool(source)
        self.dialogs, self.cards, self.saved_ids = [], [], []
        self.step = 'review' if source else 'input'
        self._rebuilding = False
        self.finished.connect(lambda *_: setattr(self, 'closed', True))
        self.destroyed.connect(lambda *_: setattr(self, 'closed', True))
        self.setWindowTitle('拆成几个小任务' if source else '一次添加多个任务')
        self.resize(820, 760)
        layout = QVBoxLayout(self)
        heading_row = ActionRow()
        self.heading = ReadingLabel(self.windowTitle(), 'DialogHeading')
        heading_row.addWidget(self.heading)
        help_button = QPushButton('如何拆分？' if source else '如何一次添加？')
        help_button.clicked.connect(self.show_help); heading_row.addWidget(help_button)
        layout.addWidget(heading_row)
        self.step_label = ReadingLabel('', 'Muted'); layout.addWidget(self.step_label)
        self.context = ReadingLabel('', 'Muted')
        self.context.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.context.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.context_scroll = QScrollArea(); self.context_scroll.setWidgetResizable(True)
        self.context_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.context_scroll.setWidget(self.context)
        self.context_scroll.setMinimumHeight(90 if source else 42)
        self.context_scroll.setMaximumHeight(130 if source else 70)
        layout.addWidget(self.context_scroll)
        self.table = QTableWidget(0, 5, self); self.table.hide()
        self.table.setHorizontalHeaderLabels(['任务名称', '完成条件', '预计分钟', '截止日期', '希望处理的日期'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.itemChanged.connect(self.input_changed)
        # The relation model is deliberately not a checkbox in the new UI.
        self.sequential = QCheckBox(self); self.sequential.hide()
        self.sequential.toggled.connect(self.input_changed)
        self.pages = QStackedWidget(); layout.addWidget(self.pages, 1)
        self.input_page = QWidget(); input_layout = QVBoxLayout(self.input_page)
        self.paste = QTextEdit()
        self.paste.setPlaceholderText('在这里输入任务，每行一项')
        input_layout.addWidget(ReadingLabel('每行写一件要做的事，一次最多 50 项。保存后可以再选择是否加入某天的安排。', 'Muted'))
        input_layout.addWidget(ReadingLabel('输入示例：\n整理阅读笔记\n购买活动材料\n联系场地负责人', 'Muted'))
        input_layout.addWidget(self.paste, 1)
        self.paste.textChanged.connect(self.input_changed)
        self.advanced_button = QPushButton('从表格粘贴…')
        self.advanced_button.clicked.connect(self.paste_spreadsheet)
        input_actions = ActionRow(); input_actions.addWidget(self.advanced_button)
        input_layout.addWidget(input_actions)
        self.pages.addWidget(self.input_page)
        self.editor = QWidget(); self.editor_layout = QVBoxLayout(self.editor)
        self.editor_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.editor_scroll = QScrollArea(); self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setFrameShape(QFrame.Shape.NoFrame); self.editor_scroll.setWidget(self.editor)
        self.pages.addWidget(self.editor_scroll)
        self.owner_button = QPushButton('重新读取原任务' if source else '选择位置…')
        self.owner_button.clicked.connect(self.reload_source if source else self.choose_parent)
        self.count = ReadingLabel('', 'Muted')
        self.card_host = QWidget(); self.card_layout = QVBoxLayout(self.card_host)
        self.card_layout.setContentsMargins(0, 0, 0, 0)
        self.card_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.add_button = QPushButton('添加子任务' if source else '再添加一项任务')
        self.add_button.clicked.connect(self.add_row)
        self.relation = QComboBox()
        self.relation.addItems(['这些小任务可以分别进行' if source else '这些任务可以分别进行', '需要按顺序进行'])
        self.relation.currentIndexChanged.connect(lambda index:self.sequential.setChecked(index == 1))
        self.sequence_hint = ReadingLabel('', 'Muted')
        self.preview = QTextBrowser(); self.preview.setMinimumHeight(70); self.preview.setMaximumHeight(145)
        self.preview.setPlainText('核对名称和保存位置；这些内容尚未保存。')
        if source:
            self._build_split_editor()
        else:
            self._build_batch_editor()
        self.editor_layout.addWidget(self.preview)
        self.editor_layout.addStretch()
        self.results = QTreeWidget(); self.results.setHeaderLabels(['已保存任务', '完成条件', '前置关系'])
        self.results.setColumnWidth(0, 260); self.results.setColumnWidth(1, 230)
        self.results.setRootIsDecorated(False); self.results.itemDoubleClicked.connect(self.open_result)
        self.pages.addWidget(self.results)
        self.status = ReadingLabel('', 'Muted'); layout.addWidget(self.status)
        actions = ActionRow()
        self.back_button = QPushButton('继续添加任务'); self.back_button.clicked.connect(self.back_to_input); actions.addWidget(self.back_button)
        self.preview_button = QPushButton('下一步：核对任务' if not source else '核对子任务')
        self.preview_button.clicked.connect(self.next_step); actions.addWidget(self.preview_button)
        self.save_button = QPushButton(); self.save_button.setObjectName('Primary')
        self.save_button.clicked.connect(self.save); actions.addWidget(self.save_button)
        self.open_button = QPushButton('打开选中任务'); self.open_button.clicked.connect(self.open_result); actions.addWidget(self.open_button)
        self.view_batch_button = QPushButton(('查看这批子任务' if on_saved_ids else '返回原任务查看子任务') if source else '查看这批任务')
        self.view_batch_button.clicked.connect(self.view_saved); actions.addWidget(self.view_batch_button)
        self.plan_button = QPushButton('安排这些任务…'); self.plan_button.clicked.connect(self.plan_saved); actions.addWidget(self.plan_button)
        self.close_button = QPushButton('关闭'); self.close_button.clicked.connect(self.reject); actions.addWidget(self.close_button)
        layout.addWidget(actions)
        # Retain model operations for import clients; no unexplained global row toolbar.
        self.import_button = QPushButton(self); self.import_button.hide(); self.import_button.clicked.connect(self.import_rows)
        self.remove_button = QPushButton(self); self.remove_button.hide(); self.remove_button.clicked.connect(self.remove_rows)
        self.up_button = QPushButton(self); self.up_button.hide(); self.up_button.clicked.connect(lambda:self.move_row(-1))
        self.down_button = QPushButton(self); self.down_button.hide(); self.down_button.clicked.connect(lambda:self.move_row(1))
        if source:
            self.add_row(); self.add_row(); self.dirty = False
        self.render_context(); self.update_controls()
        if source: self.reload_source()

    def _build_batch_editor(self):
        self.editor_layout.addWidget(ReadingLabel('这批任务放在哪里？', 'SectionTitle'))
        owner_actions = ActionRow(); owner_actions.addWidget(self.owner_button)
        self.clear_owner_button = QPushButton('暂不分类'); self.clear_owner_button.clicked.connect(self.clear_parent)
        owner_actions.addWidget(self.clear_owner_button); self.editor_layout.addWidget(owner_actions)
        self.editor_layout.addWidget(ReadingLabel('本批使用同一位置；不同位置可以分批记录。所选位置可以是项目、课程、分类、活动、项目阶段或另一任务。', 'Muted'))
        self.editor_layout.addWidget(self.count); self.editor_layout.addWidget(self.card_host)
        add_actions = ActionRow(); add_actions.addWidget(self.add_button)
        self.editor_layout.addWidget(add_actions)
        self.relation.setParent(self.editor); self.relation.hide()
        self.sequence_hint.setParent(self.editor); self.sequence_hint.hide()
        self.editor_layout.addWidget(ReadingLabel('这里仅记录任务，不会自动加入每日安排。截止日期和希望处理的日期都可以留空。', 'Muted'))

    def _build_split_editor(self):
        actions = ActionRow(); actions.addWidget(self.owner_button); self.editor_layout.addWidget(actions)
        self.editor_layout.addWidget(ReadingLabel('原任务会保留。本次只新增子任务，不覆盖已有子任务、计划或实际结果。', 'Muted'))
        self.editor_layout.addWidget(self.count); self.editor_layout.addWidget(self.card_host)
        add_actions = ActionRow(); add_actions.addWidget(self.add_button); self.editor_layout.addWidget(add_actions)
        self.editor_layout.addWidget(ReadingLabel('这些子任务之间有什么关系？', 'SectionTitle'))
        relation_actions = ActionRow(); relation_actions.addWidget(self.relation); self.editor_layout.addWidget(relation_actions)
        self.editor_layout.addWidget(self.sequence_hint)
        self.editor_layout.addWidget(ReadingLabel('子任务可以分别安排和记录完成。全部子任务完成后，原任务仍需单独确认完成。', 'Muted'))

    def _rebuild_cards(self):
        if self._rebuilding:return
        while self.card_layout.count():
            item = self.card_layout.takeAt(0)
            if item.widget():item.widget().hide();item.widget().deleteLater()
        self.cards = []
        for row in range(self.table.rowCount()):
            card = TaskEntryCard(self, row, split=bool(self.source))
            self.cards.append(card);self.card_layout.addWidget(card)
        self._sync_cards()

    def _sync_cards(self):
        for card in self.cards:card.sync()
        names = [self.table.item(row,0).text().strip() or f'子任务 {row+1}' for row in range(self.table.rowCount())]
        self.sequence_hint.setText((' → '.join(names) + '\n这是前后依赖：后一项以前一项为前置要求，不只是显示排序。')
                                   if self.sequential.isChecked() else '各项互不新增前后依赖，可以分别安排。原任务已有的前置要求仍会继承。')

    def next_step(self):
        if self.step == 'input':
            if self.paste.toPlainText().strip():
                before = self.table.rowCount(); self.import_rows()
                if self.paste.toPlainText().strip() or self.table.rowCount() == before:return
            if not self.table.rowCount():self.status.setText('请先写下一项任务，每行一件事。');return
            self.step = 'review';self.update_controls()
        self.request_preview()

    def back_to_input(self):
        if self.saving or self.uncertain or self.saved or self.source:return
        # Keep reviewed rows and all optional facts while returning to add input.
        self.step = 'input';self.update_controls()
        self.status.setText(f'已保留清单中的 {self.table.rowCount()} 项及补充信息。可以继续输入，也可直接回到核对清单。')

    def paste_spreadsheet(self):
        dialog = SpreadsheetDraftDialog(self); dialog.setWindowTitle('从表格粘贴任务'); dialog.resize(740,520)
        body = QVBoxLayout(dialog)
        body.addWidget(ReadingLabel('按五列粘贴，不包含表头：任务名称、完成条件、预计分钟、截止日期、希望处理的日期。日期使用 YYYY-MM-DD；未知留空，每批最多 50 项。\n例如：整理资料 [Tab] 能找到三份原始文件 [Tab] 30 [Tab]  [Tab]\n希望处理的日期只登记候选日期，不会加入每日安排。', 'Muted'))
        text = QTextEdit(); dialog.input = text; body.addWidget(text,1)
        error = ReadingLabel('', 'Muted');body.addWidget(error)
        actions = ActionRow(); confirm = QPushButton('下一步：核对这些任务'); cancel = QPushButton('返回')
        actions.addWidget(confirm);actions.addWidget(cancel);body.addWidget(actions)
        def accept():
            try:
                rows = parse_task_rows(text.toPlainText())
                if not rows:raise ValueError('请先粘贴至少一项任务。')
                if self.table.rowCount()+len(rows)>MAX_ITEMS:raise ValueError('合计超过 50 项，请分批记录。')
            except (ValueError,csv.Error) as exc:error.setText(str(exc));return
            # Keep a draft in the ordinary input intact until explicitly reviewed.
            if self.paste.toPlainText().strip():error.setText('前一页还有任务草稿，请先返回核对；不会覆盖原输入。');return
            self._rebuilding=True
            for values in rows:self.add_row(values=values)
            self._rebuilding=False;self._rebuild_cards();dialog.accept()
            self.step='review';self.update_controls();self.request_preview()
        confirm.clicked.connect(accept)
        cancel.clicked.connect(dialog.reject)
        dialog.exec()

    def show_help(self):
        title = '怎样拆分任务' if self.source else '怎样一次添加多个任务'
        content = ("例如：把‘准备阅读角活动’拆成‘确认场地’（取得确认）、‘准备材料’（清单齐全）、‘布置现场’（现场检查通过）。\n\n子任务是可以独立安排日期、记录完成和打开详情的任务。清单项只是任务内部的核对要点，不会自动变成独立任务。\n\n每项都要填写‘做到什么算完成’。子任务保存在原任务下面；已有子任务不被替换。需要前后依赖时选择‘需要按顺序进行’，核对箭头方向。原任务的前置要求也会继承。\n\n保存不会改动原有计划或实际结果，也不会自动完成原任务。可以从保存结果返回原任务查看子任务。"
                   if self.source else "例如输入三行：整理阅读笔记、购买活动材料、联系场地负责人。每行会创建一项独立任务。\n\n点击下一步，在清单里修改名称或补充信息，选择这批任务的共同位置；暂不分类也可以。可选择项目、课程、分类、活动或另一任务。\n\n核对后点击保存。任务保存在任务库和所选位置下；它们尚未加入每日安排。保存后使用‘安排这些任务…’选择日期。\n\n日期和预计用时未知时留空。希望处理的日期只让任务成为当天候选，不等于加入当天计划。高级表格粘贴按五列导入，每批最多 50 项。")
        dialog=QDialog(self);dialog.setWindowTitle(title);dialog.resize(660,560)
        layout=QVBoxLayout(dialog);body=QTextBrowser();body.setPlainText(content);layout.addWidget(body)
        button=QPushButton('知道了');button.clicked.connect(dialog.accept);layout.addWidget(button)
        dialog.exec()

    def view_saved(self):
        if not self.saved:return
        if self.on_saved_ids:
            identifiers, callback = list(self.saved_ids), self.on_saved_ids
            self.accept(); callback(identifiers)
        elif self.source and self.on_open:self.on_open(self.source['id'])
        else:self.results.setFocus()

    def plan_saved(self):
        if self.saved and self.on_plan:self.on_plan(list(self.saved_ids))

    def render_context(self, child_count=None):
        if self.source:
            standard = self.source.get('data', {}).get('completion_gate') or self.source.get('data', {}).get('acceptance') or '尚未填写'
            message = '原任务：' + self.source['title'] + '\n原完成标准：' + str(standard)
            if child_count is not None:
                message += f'\n已有 {child_count} 项子任务；本次会继续新增，不替换或覆盖已有子任务。'
            if not can_split_task(self.source): message += '\n该任务当前不可拆分，请重新读取并检查完成状态。'
        else:
            message = ('保存位置：' + location_label(self.parent_entity)
                       if self.parent_entity else '保存位置：任务库 · 暂不分类')
        self.context.setText(message)

    def input_changed(self, *_):
        if self.saving or self.uncertain or self.saved: return
        self.dirty = True; self.generation += 1; self.previewing = False
        self.preview_result = self.submission = None; self.request_id = None
        self.preview.setPlainText('内容已修改，尚未保存。请核对修改后的任务。')
        self._sync_cards()
        self.status.clear(); self.update_controls()

    def update_controls(self):
        editable = not (self.saving or self.uncertain or self.saved)
        self.editor.setEnabled(editable);self.input_page.setEnabled(editable)
        count=self.table.rowCount()
        self.count.setText(f'将要保存的子任务：{count} 项（最多 {MAX_ITEMS} 项）' if self.source else f'将要保存的任务：{count} 项（最多 {MAX_ITEMS} 项）')
        self.add_button.setEnabled(editable and count < MAX_ITEMS)
        allowed = not self.source or can_split_task(self.source)
        self.preview_button.setEnabled(editable and not self.previewing and not self.source_loading and allowed)
        self.preview_button.setText(('下一步：核对任务' if self.step=='input' else '核对修改') if not self.source else '核对子任务')
        self.save_button.setEnabled(not self.saving and not self.saved and (self.uncertain or self.preview_result is not None))
        self.save_button.setText('核对上次保存结果' if self.uncertain else f'保存这 {count} 项' + ('子任务' if self.source else '任务'))
        self.close_button.setEnabled(not self.saving)
        self.pages.setCurrentWidget(self.results if self.saved else self.input_page if self.step=='input' else self.editor_scroll)
        self.step_label.setText('已保存 · 可以打开任务或继续安排' if self.saved else
                               ('填写子任务 → 核对关系 → 保存' if self.source else '1 · 写下任务' if self.step=='input' else '2 · 核对任务与保存位置（尚未保存）'))
        self.context_scroll.setVisible(bool(self.source) or self.step!='input')
        self.back_button.setVisible(not self.source and self.step=='review' and not self.saved)
        self.back_button.setEnabled(editable)
        self.preview_button.setVisible(not self.saved)
        self.save_button.setVisible(self.step=='review' and not self.saved)
        self.open_button.setVisible(self.saved and bool(self.on_open))
        self.view_batch_button.setVisible(self.saved)
        self.plan_button.setVisible(self.saved and bool(self.on_plan))

    def add_row(self, *_ , values=None):
        if self.saving or self.uncertain or self.saved or self.table.rowCount() >= MAX_ITEMS: return
        self.table.blockSignals(True)
        row = self.table.rowCount(); self.table.insertRow(row)
        for column, value in enumerate(values or [''] * 5): self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.table.blockSignals(False); self._rebuild_cards(); self.input_changed()

    def import_rows(self):
        if self.saving or self.uncertain or self.saved: return
        try:
            rows = parse_task_rows(self.paste.toPlainText())
            if not rows: raise ValueError('请先输入至少一行任务。')
            empty_seed = self.source and self.table.rowCount()==2 and not any(self.table.item(r,c).text().strip() for r in range(2) for c in range(5))
            if (0 if empty_seed else self.table.rowCount()) + len(rows) > MAX_ITEMS: raise ValueError('合计超过 50 项，请减少任务数或分批记录。')
        except (ValueError, csv.Error) as exc:
            self.status.setText(str(exc)); return
        if empty_seed:self.table.setRowCount(0)
        self._rebuilding=True
        for row in rows: self.add_row(values=row)
        self._rebuilding=False;self._rebuild_cards()
        self.paste.clear(); self.input_changed()

    def remove_rows(self):
        if self.saving or self.uncertain or self.saved: return
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        for row in sorted(rows, reverse=True): self.table.removeRow(row)
        self._rebuild_cards();self.input_changed()

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
        self._rebuild_cards();self.input_changed()

    def choose_parent(self):
        if self.saving or self.uncertain or self.saved: return
        picker = EntityPicker(self.bridge, self, allowed_types=[kind for kind in TYPES['task']['parent_types'] if kind is not None])
        picker.setWindowTitle('选择这批任务的保存位置')
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
        if self.paste.toPlainText().strip(): raise ValueError('输入页还有尚未核对的任务，请先点击下一步，避免遗漏。')
        if not 1 <= self.table.rowCount() <= MAX_ITEMS: raise ValueError('每批需有 1 至 50 项任务。')
        if self.source and self.table.rowCount() < 2: raise ValueError('拆分至少需要两项子任务。')
        items = []
        for row in range(self.table.rowCount()):
            cells = [(self.table.item(row, column).text().strip() if self.table.item(row, column) else '') for column in range(5)]
            if not cells[0]: raise ValueError(f'第 {row + 1} 项需要填写任务名称。')
            if self.source and not cells[1]: raise ValueError(f'第 {row + 1} 项需要独立的完成标准：请填写做到什么算完成。')
            item = {'title': cells[0], 'completion_gate': cells[1]}
            if cells[2]:
                if not cells[2].isascii() or not cells[2].isdigit() or not 1 <= int(cells[2]) <= 1440:
                    raise ValueError(f'第 {row + 1} 项分钟数应为 1 至 1440 的整数；未知请留空。')
                item['estimated_minutes'] = int(cells[2])
            for column, key in [(3, 'due_date'), (4, 'scheduled_date')]:
                if cells[column]:
                    date = QDate.fromString(cells[column], 'yyyy-MM-dd')
                    if not date.isValid() or date.toString('yyyy-MM-dd') != cells[column]:
                        raise ValueError(f'第 {row + 1} 项日期应为 YYYY-MM-DD；未知请留空。')
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
            self.step='review'
            lines = [f'已核对 {len(result.get("items", items))} 项' + ('子任务。' if self.source else '任务。')]
            owner = result.get('source') if self.source else result.get('parent')
            if owner: lines.append('保存位置：' + owner.get('title', '所选位置'))
            dependencies=result.get('dependencies', [])
            if dependencies:
                lines.append('每项子任务继承的前置要求：'+'、'.join(dep['title']+('（已完成）' if dep.get('completed') else '（未完成）') for dep in dependencies))
            if self.source and owner: self.render_context(owner.get('children_count'))
            if self.sequential.isChecked():lines.append('将按清单顺序建立前后依赖，请核对箭头方向。')
            lines.extend(str(w) for w in result.get('warnings', []))
            self.preview.setPlainText('\n'.join(lines))
            self.status.setText('这些任务尚未保存。确认清单和位置后，点击保存。'); self.update_controls()
        def failed(error):
            if self.closed or generation != self.generation: return
            self.previewing = False; self.status.setText('核对未通过，输入仍保留。' + error.get('message', str(error))); self.update_controls()
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
                self.status.setText('暂时无法确认保存结果。输入已锁定；点击“核对上次保存结果”核对同一次请求，不会重新创建另一批。\n'+error.get('message',str(error)))
            else:
                self.preview_result = None; self.submission = None; self.request_id = None
                self.status.setText('保存未完成，输入仍保留。请重新核对。' + error.get('message', str(error)))
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
        self.saved_ids=[item['id'] for item in items]
        self.open_button.setEnabled(bool(items))
        self.status.setText(f'已记下 {len(items)} 项' + ('子任务，保存在原任务下面。原任务仍需单独确认完成。' if self.source else '任务，尚未加入每日计划。可在下一步选择安排日期。'))
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
