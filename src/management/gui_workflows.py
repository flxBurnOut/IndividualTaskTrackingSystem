"""Daily planning, background assistance and settings forms."""
from __future__ import annotations

import datetime as dt
import csv
import io
import uuid
import html
import sys
import json
from pathlib import Path

from PySide6.QtCore import QTime, QDate, Qt, QTimer
from PySide6.QtGui import QShortcut, QKeySequence
from .appearance import normalize_appearance
from .chart_preferences import CHART_OPTIONS, normalize_chart_preferences
from .gui_theme import available_font_families, apply_appearance, resolved_font_family
from .gui_layout import ActionRow
from .gui_settings_controls import AccentPicker, AppearancePreview, SettingsGroup, SelectableText
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QScrollArea, QFrame, QAbstractSpinBox, QLayout, QAbstractItemView,
    QMessageBox, QPushButton, QTimeEdit, QSpinBox, QSplitter, QTabWidget, QTableWidget,
    QTextBrowser, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .gui_calendar import install_calendar
from .gui_forms import EntityForm, EntityPicker, FormDialog, FIELD_LABELS, label_status, readable


def codex_mcp_config(data_dir):
    """Compatibility entry point for the managed dialogue workspace configuration."""
    from .codex_workspace import mcp_config
    return mcp_config(data_dir)


def fit_input_fields(container):
    """Keep native editors readable when application fonts change."""
    for widget in container.findChildren(QWidget):
        if not isinstance(widget, (QComboBox, QLineEdit, QAbstractSpinBox, QPushButton)):
            continue
        if isinstance(widget, QLineEdit) and isinstance(widget.parentWidget(), (QComboBox, QAbstractSpinBox)):
            continue
        widget.ensurePolished()
        widget.setMinimumHeight(max(widget.sizeHint().height(), widget.fontMetrics().height() + 18))


class PlanDialog(FormDialog):
    """A dated plan editor with visible candidates and preserved existing blocks."""
    def __init__(self, bridge, parent=None, on_saved=None, date=None):
        super().__init__("安排一天", parent, 1040)
        self.bridge, self.on_saved = bridge, on_saved
        self.epoch = bridge.epoch
        self.context_generation = self.candidate_generation = 0
        self.context_day = None
        self.plan = None
        self.plan_loading = True
        self.context_ready = False
        self.closed = self.dirty = self.loading_fields = False
        self.saving = False
        self.checked_candidates = {}
        self.candidate_history = []
        self.candidate_next = None
        self.candidate_loading = False
        self.dialogs = []
        self.draft_token = None
        self.draft_ready = False
        self.draft_blocked = False
        from .gui_plan_drafts import PlanDrafts
        if not hasattr(bridge, '_plan_drafts'):
            bridge._plan_drafts = PlanDrafts(getattr(bridge, 'data_dir', getattr(getattr(bridge, 'core', None), 'root', None)))
        self.drafts = bridge._plan_drafts
        self.draft_timer = QTimer(self)
        self.draft_timer.setSingleShot(True)
        self.draft_timer.setInterval(400)
        self.draft_timer.timeout.connect(self.persist_draft)
        self.finished.connect(lambda *_: setattr(self, "closed", True))
        self.resize(1040, 850)
        top = ActionRow()
        self.date = QDateEdit(date or QDate.currentDate())
        install_calendar(self.date)
        self.date.setDisplayFormat("yyyy-MM-dd dddd")
        self.mode = QComboBox()
        for label, key in [("常规安排", "standard"), ("低精力", "low_state"), ("只排先后，不定时", "no_precise_time"), ("休息", "rest")]:
            self.mode.addItem(label, key)
        for caption,field in (("安排哪一天",self.date),("方式",self.mode)):
            group=QWidget();group_layout=QHBoxLayout(group);group_layout.setContentsMargins(0,0,0,0)
            group_layout.addWidget(QLabel(caption));group_layout.addWidget(field);top.addWidget(group)
        self.body_layout.addWidget(top)
        self.context = QLabel("正在读取当天的固定安排…")
        self.context.setTextFormat(Qt.TextFormat.PlainText)
        self.context.setObjectName("ContextCard");self.context.setWordWrap(True)
        self.body_layout.addWidget(self.context)
        self.unknown_toggle = QPushButton("查看时间待确认的安排")
        self.unknown_toggle.setCheckable(True);self.unknown_toggle.hide()
        self.unknowns = QLabel();self.unknowns.setTextFormat(Qt.TextFormat.PlainText);self.unknowns.setWordWrap(True);self.unknowns.hide()
        self.unknown_toggle.toggled.connect(self.unknowns.setVisible)
        self.body_layout.addWidget(self.unknown_toggle);self.body_layout.addWidget(self.unknowns)
        candidate_hint=QLabel("从任务池勾选，可跨页选择；未定日期的任务也可以安排。")
        candidate_hint.setWordWrap(True);self.body_layout.addWidget(candidate_hint)
        filters = QHBoxLayout()
        self.candidate_search = QLineEdit();self.candidate_search.setPlaceholderText('搜索任务标题')
        self.candidate_group = QComboBox()
        for label, key in [('全部待办','open'),('未定日期','undated'),('到期与逾期','due'),('未来截止','future'),('未归属','unowned')]:
            self.candidate_group.addItem(label,key)
        filters.addWidget(self.candidate_search,1);filters.addWidget(self.candidate_group)
        self.new_task_button = QPushButton('新建任务');self.new_task_button.clicked.connect(self.new_task);filters.addWidget(self.new_task_button)
        self.body_layout.addLayout(filters)
        self.search_timer = QTimer(self);self.search_timer.setSingleShot(True);self.search_timer.setInterval(200)
        self.search_timer.timeout.connect(lambda:self.load_candidates(0))
        self.candidate_search.textChanged.connect(lambda:self.search_timer.start())
        self.candidate_group.currentIndexChanged.connect(lambda:self.load_candidates(0))
        self.candidates = QTreeWidget();self.candidates.setHeaderLabels(["课程 / 项目与任务", "日期与安排状态", "完成标准与前置任务"])
        self.candidates.setColumnWidth(0, 350);self.candidates.setColumnWidth(1,180);self.candidates.setMinimumHeight(150);self.candidates.setMaximumHeight(230)
        self.candidates.itemDoubleClicked.connect(self.add_candidate)
        self.candidates.itemChanged.connect(self.candidate_checked)
        self.body_layout.addWidget(self.candidates)
        row = ActionRow()
        self.add_candidate_button = QPushButton("加入勾选任务");self.add_candidate_button.setObjectName("Primary");self.add_candidate_button.clicked.connect(self.add_checked)
        row.addWidget(self.add_candidate_button)
        self.candidate_previous = QPushButton("上一页");self.candidate_previous.clicked.connect(self.previous_candidates);row.addWidget(self.candidate_previous)
        self.candidate_more = QPushButton("下一页");self.candidate_more.clicked.connect(self.next_candidates);row.addWidget(self.candidate_more)
        self.candidate_count = QLabel("正在读取…");self.candidate_count.setWordWrap(True);row.addWidget(self.candidate_count)
        self.body_layout.addWidget(row)
        self.plan_note = QLabel("下面是这一天的计划，可调整顺序和时间。")
        self.plan_note.setWordWrap(True);self.body_layout.addWidget(self.plan_note)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["任务与归属", "开始", "结束", "分钟", "任务完成标准（只读）"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column,width in [(0,290),(1,85),(2,85),(3,75)]:self.table.setColumnWidth(column,width)
        self.table.setMinimumHeight(180);self.body_layout.addWidget(self.table)
        actions = ActionRow()
        add = QPushButton("搜索其他任务…");add.clicked.connect(self.add_block);actions.addWidget(add)
        edit = QPushButton('编辑源任务');edit.clicked.connect(self.edit_task);actions.addWidget(edit)
        remove = QPushButton("移除选中安排");remove.clicked.connect(self.remove_block);actions.addWidget(remove)
        self.edit_actions=[add,edit,remove]
        for title,step in [("上移",-1),("下移",1)]:
            button=QPushButton(title);button.clicked.connect(lambda _,step=step:self.move_block(step));actions.addWidget(button)
            self.edit_actions.append(button)
        self.body_layout.addWidget(actions)
        for keys, step in [('Alt+Up',-1),('Alt+Down',1)]:
            shortcut = QShortcut(QKeySequence(keys), self.table)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda step=step:self.move_block(step))
        self.source = QTextEdit();self.source.setPlaceholderText("安排依据或说明（可留空）");self.source.setMaximumHeight(65);self.source.textChanged.connect(self.mark_dirty);self.body_layout.addWidget(self.source)
        hint = QLabel("时间和分钟数可留空，只排先后。选中一行后按 Alt+↑ / Alt+↓ 调序。完成标准请在源任务中编辑；已保存的原计划标准与反馈保留。前置任务须已完成或排在同日之前。")
        hint.setWordWrap(True);hint.setObjectName("Hint");self.body_layout.addWidget(hint)
        draft_row = QHBoxLayout()
        self.draft_note = QLabel('草稿仅保留编辑内容；点击“保存计划”后才成为正式计划。');self.draft_note.setWordWrap(True)
        draft_row.addWidget(self.draft_note,1)
        self.discard_draft_button = QPushButton('放弃草稿，重读计划');self.discard_draft_button.clicked.connect(self.discard_draft);draft_row.addWidget(self.discard_draft_button)
        self.body_layout.addLayout(draft_row)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存计划')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('保留草稿并关闭')
        self.date.dateChanged.connect(self.load_context)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.buttons.accepted.connect(self.save)
        self.load_context()
        from .gui_tutorials import install_dialog_tutorial
        install_dialog_tutorial(self, 'plan')

    def mark_dirty(self,*_):
        if not self.loading_fields and not self.closed:
            self.dirty=True
            self.draft_note.setText('编辑尚未保存为正式计划。正在保留草稿…')
            if self.draft_ready:self.draft_timer.start()

    def mode_changed(self,*_):
        self.mark_dirty();self.load_context()

    def update_save_state(self):
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(self.context_ready and not self.plan_loading and not self.saving and not self.draft_blocked)
        for widget in (self.mode,self.table,self.source,self.new_task_button,self.candidates,*self.edit_actions):widget.setEnabled(self.can_edit())
        self.add_candidate_button.setEnabled(self.can_edit() and bool(self.checked_candidates))

    def can_edit(self):
        return self.draft_ready and not self.plan_loading and not self.saving

    def load_context(self,*_):
        day=self.date.date().toString("yyyy-MM-dd")
        new_day=day!=self.context_day
        if new_day and self.context_day and self.dirty:
            if not self.persist_draft():
                self.date.blockSignals(True);self.date.setDate(QDate.fromString(self.context_day,"yyyy-MM-dd"));self.date.blockSignals(False);return
        self.context_generation+=1;generation=self.context_generation
        self.context_ready=False;self.error_label.hide()
        if new_day:
            self.context_day=day;self.plan=None;self.plan_loading=True;self.table.setRowCount(0);self.loading_fields=True;self.source.clear();self.loading_fields=False;self.dirty=False
            self.checked_candidates.clear();self.draft_token=None;self.draft_ready=False;self.draft_blocked=False
            self.mode.blockSignals(True);self.mode.setCurrentIndex(0);self.mode.blockSignals(False)
            self.load_candidates(0)
            self.load_existing_plan(generation,day)
        self.update_save_state()
        def loaded(result):
            if self.closed or generation!=self.context_generation:return
            if self.draft_ready and result.get('epoch',self.epoch)!=self.epoch:
                self.draft_blocked=True
                self.error('数据空间已经切换。当前草稿仍保留在原数据空间，请关闭后重新打开安排。')
                self.update_save_state();return
            self.context_revision=result.get("revision");self.epoch=result.get("epoch",self.epoch)
            lines=[]
            for event in sorted(result.get("hard_events",result.get("events",[])),key=lambda e:(e.get("start_minute") is None,e.get("start_minute") or 0,e.get("title",""))):
                data=event.get("data",event)
                a,b=event.get("start_minute"),event.get("end_minute")
                start=f"{a//60:02d}:{a%60:02d}" if a is not None else "时间待确认"
                end=f"{b//60:02d}:{b%60:02d}" if b is not None else "待确认"
                title=event.get("display_title") or event.get("title","固定安排")
                owner=event.get("owner_code") or event.get("owner_label")
                if owner and owner!="未归属" and owner not in title:title=owner+" · "+title
                lines.append(f"• {title}　{start}–{end}")
            text="当天固定安排\n"+("\n".join(lines) if lines else "这一天没有已记录的固定安排。")
            capacity=result.get("capacity_minutes")
            if capacity is not None:text+=f"\n已设置可安排时间：{capacity} 分钟"
            self.context.setText(text)
            unknowns=result.get("unknowns",[])
            self.unknowns.setText("这些安排的时间尚未确认，不代表它们就在这一天：\n"+"\n".join("• "+str(e.get("title") or "未命名安排")+"："+str(e.get("reason") or "时间待确认") for e in unknowns))
            self.unknown_toggle.setText(f"时间待确认的安排（{len(unknowns)}）")
            self.unknown_toggle.setVisible(bool(unknowns));self.unknowns.setVisible(bool(unknowns) and self.unknown_toggle.isChecked())
            self.context_ready=True;self.restore_draft();self.update_save_state()
        def failed(error):
            if not self.closed and generation==self.context_generation:self.error(error)
        self.bridge.query("plan_context",loaded,failed,date=day,mode=self.mode.currentData())

    def next_candidates(self):
        if self.candidate_loading or self.candidate_next is None:return
        self.candidate_history.append(self.candidate_offset)
        self.load_candidates(self.candidate_next)

    def previous_candidates(self):
        if self.candidate_loading or not self.candidate_history:return
        self.load_candidates(self.candidate_history.pop())

    def load_candidates(self,offset=0):
        if offset is None:return
        if offset==0:self.candidate_history.clear()
        self.candidate_loading=True;self.candidate_next=None
        self.candidate_previous.setEnabled(False);self.candidate_more.setEnabled(False)
        self.candidate_generation+=1;generation=self.candidate_generation
        self.candidate_offset=offset;day=self.context_day or self.date.date().toString("yyyy-MM-dd")
        self.candidates.clear();self.add_candidate_button.setEnabled(False)
        def loaded(result):
            if self.closed or generation!=self.candidate_generation or day!=self.context_day:return
            self.candidates.blockSignals(True)
            groups=result.get("groups")
            if groups is None:
                grouped={}
                for e in result.get("items",[]):grouped.setdefault(e.get("owner_id") or "unowned",[]).append(e)
                groups=[{"owner_label":items[0].get('owner_label') or '未归属课程 / 项目',"items":items} for items in grouped.values()]
            for group in groups:
                parent=QTreeWidgetItem([group.get("owner_label") or "未归属课程 / 项目",""]);self.candidates.addTopLevelItem(parent)
                for entity in group["items"]:
                    in_plan=any(self.table.cellWidget(row,0).property('target_id')==entity['id'] for row in range(self.table.rowCount()))
                    date_label=str(entity.get("candidate_date") or entity.get("data",{}).get("scheduled_date") or entity.get("data",{}).get("due_date") or "未定日期")
                    if in_plan:date_label+=' · 已在下方安排'
                    elif entity.get('in_plan'):date_label+=' · 已在当天正式计划'
                    detail=str(entity.get('data',{}).get('completion_gate') or entity.get('data',{}).get('acceptance') or '完成标准未填写')
                    predecessors=self.predecessor_text(entity)
                    if predecessors:detail+=' · '+predecessors
                    item=QTreeWidgetItem([entity.get("title", "任务"),date_label,detail])
                    item.setData(0,Qt.ItemDataRole.UserRole,entity)
                    item.setCheckState(0,Qt.CheckState.Checked if entity['id'] in self.checked_candidates else Qt.CheckState.Unchecked)
                    item.setToolTip(0,detail);item.setToolTip(2,detail)
                    if in_plan:item.setDisabled(True)
                    parent.addChild(item)
                parent.setExpanded(True)
            self.candidate_next=result.get("next_offset")
            self.candidate_loading=False
            self.candidate_previous.setEnabled(bool(self.candidate_history));self.candidate_more.setEnabled(self.candidate_next is not None)
            self.candidate_previous.setVisible(bool(self.candidate_history));self.candidate_more.setVisible(self.candidate_next is not None)
            self.candidate_total=result.get('total',len(result.get('items',[])))
            self.candidates.blockSignals(False);self.update_candidate_count()
        def failed(error):
            if not self.closed and generation==self.candidate_generation:
                self.candidate_loading=False
                self.candidate_previous.setEnabled(bool(self.candidate_history))
                self.candidate_previous.setVisible(bool(self.candidate_history))
                self.error(error)
        self.bridge.query("task_pool",loaded,failed,date=day,group=self.candidate_group.currentData(),search=self.candidate_search.text().strip(),limit=100,offset=offset)

    def load_existing_plan(self,generation,day):
        def loaded(review):
            if self.closed or generation!=self.context_generation:return
            snapshot=review.get("plan")
            if not snapshot:
                self.plan_loading=False;self.plan_note.setText("尚无计划。勾选上面的任务，加入后保存即可。");self.restore_draft();self.update_save_state();return
            def plan_loaded(value):
                if self.closed or generation!=self.context_generation:return
                plan=value['entity']
                if plan['version']!=snapshot['version']:
                    self.error({'code':'stale_revision','message':'当天计划已经更新，请重新打开手动安排。'});return
                self.plan=plan;self.loading_fields=True
                labels={e['target_id']:e for e in review.get('items',[])}
                for block in plan['data'].get('blocks',[]):
                    entity=labels.get(block.get('target_id'),{})
                    self.insert_block({'id':block['target_id'],'title':entity.get('title') or block.get('title') or '原计划事项','display_title':entity.get('display_title'),'owner_label':entity.get('owner_label'),'data':{}},block)
                index=self.mode.findData(plan['data'].get('mode','standard'))
                self.mode.blockSignals(True);self.mode.setCurrentIndex(max(0,index));self.mode.blockSignals(False)
                self.source.setPlainText(plan['data'].get('source_text') or '')
                self.loading_fields=False;self.dirty=False;self.plan_loading=False
                self.plan_note.setText('已载入这一天的现有计划。保存会生成修订版，原计划与反馈保留。');self.restore_draft();self.update_save_state()
                # The saved mode affects capacity and protected-time projections.
                self.load_context()
            def failed(error):
                if not self.closed and generation==self.context_generation:self.error(error)
            self.bridge.query('get',plan_loaded,failed,id=snapshot['id'])
        def failed(error):
            if not self.closed and generation==self.context_generation:self.error(error)
        self.bridge.query('daily_review',loaded,failed,date=day)

    def candidate_checked(self,item,*_):
        entity=item.data(0,Qt.ItemDataRole.UserRole)
        if not entity:return
        if item.checkState(0)==Qt.CheckState.Checked:self.checked_candidates[entity['id']]=entity
        else:self.checked_candidates.pop(entity['id'],None)
        self.update_candidate_count()

    def update_candidate_count(self):
        self.candidate_count.setText(f"共 {getattr(self,'candidate_total',0)} 项 · 已勾选 {len(self.checked_candidates)} 项")
        self.add_candidate_button.setEnabled(self.can_edit() and bool(self.checked_candidates))

    def add_candidate(self,item,*_):
        if not self.can_edit():return
        entity=item.data(0,Qt.ItemDataRole.UserRole)
        if entity:
            self.insert_block(entity);item.setCheckState(0,Qt.CheckState.Unchecked)
            self.load_candidates(self.candidate_offset)

    def add_checked(self):
        if not self.can_edit():return
        for entity in list(self.checked_candidates.values()):self.insert_block(entity)
        self.checked_candidates.clear();self.load_candidates(self.candidate_offset)

    def new_task(self):
        if not self.can_edit():return
        self.open_task_editor()

    def edit_task(self):
        if not self.can_edit():return
        row=self.table.currentRow()
        if row<0:self.error('请先选中要编辑的任务。');return
        identifier=self.table.cellWidget(row,0).property('target_id')
        self.bridge.query('get',lambda result:self.open_task_editor(result['entity']) if not self.closed else None,self.error,id=identifier)

    def open_task_editor(self,entity=None):
        if not self.can_edit():return
        day=self.context_day
        def ready(capabilities):
            if self.closed:return
            def saved(receipt):
                if self.closed:return
                updated=(receipt.get('result') or {}).get('entity')
                if updated and day==self.context_day:
                    if entity is None:self.insert_block(updated)
                    else:
                        for row in range(self.table.rowCount()):
                            target=self.table.cellWidget(row,0)
                            if target.property('target_id')!=updated['id']:continue
                            target.setProperty('entity',updated)
                            target.setText(updated.get('display_title') or updated['title'])
                            if not target.property('original_block'):
                                self.table.cellWidget(row,4).setText(updated.get('data',{}).get('completion_gate') or updated.get('data',{}).get('acceptance') or '')
                        self.mark_dirty()
                    self.load_candidates(0);self.load_context()
                if self.on_saved:self.on_saved(receipt)
            dialog=EntityForm(self.bridge,capabilities,self,entity=entity,default_type='task',on_saved=saved)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose,True)
            self.dialogs.append(dialog);dialog.finished.connect(lambda *_:self.dialogs.remove(dialog) if dialog in self.dialogs else None)
            dialog.show()
        self.bridge.query('capabilities',ready,self.error)

    def add_block(self):
        if not self.can_edit():return
        picker=EntityPicker(self.bridge,self,allowed_types=['task'])
        if picker.exec()!=QDialog.DialogCode.Accepted:return
        day=self.context_day
        def loaded(value):
            if not self.closed and day==self.context_day:self.insert_block(value['entity'])
        self.bridge.query('get',loaded,self.error,id=picker.selected['id'])

    def insert_block(self,entity,original=None):
        if any(self.table.cellWidget(row,0).property('target_id')==entity['id'] for row in range(self.table.rowCount())):return
        row=self.table.rowCount();self.table.insertRow(row)
        title=entity.get('display_title') or entity['title']
        if entity.get('owner_label') and entity['owner_label'] not in title:title=entity['owner_label']+' · '+title
        predecessors=self.predecessor_text(entity)
        if predecessors:title+='\n'+predecessors
        target=QLabel(title);target.setTextFormat(Qt.TextFormat.PlainText);target.setProperty('target_id',entity['id']);target.setProperty('entity',entity);target.setProperty('original_block',dict(original or {}));target.setWordWrap(True);self.table.setCellWidget(row,0,target)
        for column,key in [(1,'start'),(2,'end')]:
            entry=QLineEdit((original or {}).get(key) or '');entry.setPlaceholderText('可留空');entry.textChanged.connect(self.mark_dirty);self.table.setCellWidget(row,column,entry)
        planned_minutes=(original.get('minutes') or 0) if original is not None else (entity.get('data',{}).get('estimated_minutes') or 0)
        minutes=QSpinBox();minutes.setRange(0,1440);minutes.setSpecialValueText('待确认');minutes.setValue(planned_minutes);minutes.valueChanged.connect(self.mark_dirty);self.table.setCellWidget(row,3,minutes)
        standard=(original.get('completion_gate') or '') if original is not None else (entity.get('data',{}).get('completion_gate') or entity.get('data',{}).get('acceptance') or '')
        gate=QLineEdit(standard);gate.setReadOnly(True);gate.setPlaceholderText('请编辑源任务填写完成标准');self.table.setCellWidget(row,4,gate)
        self.table.setRowHeight(row,60);self.mark_dirty()

    @staticmethod
    def predecessor_text(entity):
        values=[str(dep.get('title') or '未命名任务')+('（已完成）' if dep.get('completed') else '（未完成）') for dep in entity.get('dependencies',[])]
        if entity.get('dependencies_truncated'):values.append('另有前置任务，请查看源任务')
        return '前置：'+'、'.join(values) if values else ''

    def remove_block(self):
        if not self.can_edit():return
        row=self.table.currentRow()
        if row>=0:self.table.removeRow(row);self.mark_dirty();self.load_candidates(self.candidate_offset)

    def move_block(self,step):
        if not self.can_edit():return
        row=self.table.currentRow();other=row+step
        if row<0 or not 0<=other<self.table.rowCount():return
        def snapshot(index):
            target=self.table.cellWidget(index,0)
            return {'text':target.text(),'id':target.property('target_id'),'entity':target.property('entity'),'original':target.property('original_block'),'start':self.table.cellWidget(index,1).text(),'end':self.table.cellWidget(index,2).text(),'minutes':self.table.cellWidget(index,3).value(),'gate':self.table.cellWidget(index,4).text()}
        left,right=snapshot(row),snapshot(other)
        for index,value in [(row,right),(other,left)]:
            target=self.table.cellWidget(index,0);target.setText(value['text']);target.setProperty('target_id',value['id']);target.setProperty('entity',value['entity']);target.setProperty('original_block',value['original'])
            for column,key in [(1,'start'),(2,'end'),(4,'gate')]:self.table.cellWidget(index,column).setText(value[key])
            self.table.cellWidget(index,3).setValue(value['minutes'])
        self.table.setCurrentCell(other,0);self.mark_dirty()

    def block_values(self):
        blocks=[]
        for row in range(self.table.rowCount()):
            target=self.table.cellWidget(row,0);block=dict(target.property('original_block') or {});block['target_id']=target.property('target_id')
            if not target.property('original_block') and (target.property('entity') or {}).get('version') is not None:
                block['target_version']=target.property('entity')['version']
            for column,key in [(1,'start'),(2,'end'),(4,'completion_gate')]:
                value=self.table.cellWidget(row,column).text().strip()
                if value:block[key]=value
                elif key!='completion_gate' or key not in (target.property('original_block') or {}):block.pop(key,None)
            minutes=self.table.cellWidget(row,3).value()
            if minutes:block['minutes']=minutes
            else:block.pop('minutes',None)
            blocks.append(block)
        return blocks

    def draft_payload(self):
        return {'plan':{'id':self.plan['id'],'version':self.plan['version']} if self.plan else None,
                'mode':self.mode.currentData(),'source_text':self.source.toPlainText(),
                'rows':[{'entity':self.table.cellWidget(row,0).property('entity'),
                         'original':self.table.cellWidget(row,0).property('original_block'),
                         'block':block} for row,block in enumerate(self.block_values())]}

    def persist_draft(self):
        self.draft_timer.stop()
        if not self.dirty:return True
        if not self.draft_ready:
            self.error('尚未完成读取，请稍等再切换日期或关闭。');return False
        try:
            self.draft_token=self.drafts.write(self.epoch,self.context_day,self.draft_payload(),self.draft_token)
        except (OSError,ValueError,RuntimeError) as exc:
            self.draft_note.setText('草稿未能保留：'+str(exc));return False
        self.draft_note.setText('草稿已保留；切换日期或关闭后可继续。尚未保存为正式计划。')
        return True

    def restore_draft(self):
        if self.draft_ready or self.plan_loading or not self.context_ready:return
        self.draft_ready=True
        try:
            stored=self.drafts.read(self.epoch,self.context_day)
        except (OSError,ValueError,RuntimeError) as exc:
            self.draft_note.setText('草稿读取失败：'+str(exc));self.draft_blocked=True;return
        self.draft_token=(stored or {}).get('token')
        draft=(stored or {}).get('payload')
        if not draft:
            self.draft_note.setText('编辑会保留为草稿；点击“保存计划”后才成为正式计划。');return
        current={'id':self.plan['id'],'version':self.plan['version']} if self.plan else None
        self.draft_blocked=draft.get('plan')!=current
        if self.draft_blocked:self.plan=draft.get('plan')
        self.loading_fields=True;self.table.setRowCount(0)
        for row in draft.get('rows',[]):
            self.insert_block(row['entity'],row['block'])
            self.table.cellWidget(self.table.rowCount()-1,0).setProperty('original_block',row.get('original') or {})
        self.mode.blockSignals(True);self.mode.setCurrentIndex(max(0,self.mode.findData(draft.get('mode'))));self.mode.blockSignals(False)
        self.source.setPlainText(draft.get('source_text') or '')
        self.loading_fields=False;self.dirty=True
        self.draft_note.setText('草稿对应的正式计划已在另一处更新。草稿仍保留，请核对后放弃草稿并重读当前计划。' if self.draft_blocked else '已恢复这一天尚未保存的草稿。点击“保存计划”后才生效。')
        # Re-read mode-specific constraints after restoring a mode from the draft.
        QTimer.singleShot(0,lambda:self.load_context() if not self.closed else None)

    def discard_draft(self):
        if self.saving:return
        if QMessageBox.question(self,'放弃当前草稿','放弃此窗口的编辑并重新读取？已保存的计划和反馈不变。',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:return
        try:
            current=self.drafts.read(self.epoch,self.context_day)
            if (current or {}).get('token')==self.draft_token:
                self.drafts.clear(self.epoch,self.context_day,self.draft_token)
        except (OSError,ValueError,RuntimeError) as exc:
            self.error(str(exc));return
        self.draft_timer.stop();self.dirty=False;self.context_day=None;self.load_context()

    def error(self,error):
        if self.closed:return
        if self.saving:
            self.error_label.setText(error.get('message',str(error)) if isinstance(error,dict) else str(error));self.error_label.show();return
        super().error(error)
        self.update_save_state()

    def reject(self):
        if self.saving:
            self.error_label.setText('正在保存计划，请等待结果后关闭。');self.error_label.show();return
        if self.persist_draft():super().reject()

    def closeEvent(self,event):
        if self.saving or not self.persist_draft():event.ignore();return
        super().closeEvent(event)

    def save(self):
        if self.plan_loading or not self.context_ready or self.saving or self.draft_blocked:return
        if not self.persist_draft():return
        blocks=self.block_values()
        self.saving=True
        self.busy()
        def saved(result):
            self.saving=False
            if self.closed:return
            self.dirty=False
            self.draft_timer.stop()
            try:self.drafts.clear(self.epoch,self.context_day,self.draft_token)
            except (OSError,ValueError,RuntimeError):pass  # Preserve any newer window's draft.
            if self.on_saved:self.on_saved(result)
            self.accept()
        payload={'date':self.context_day,'mode':self.mode.currentData(),'blocks':blocks,'source_text':self.source.toPlainText().strip()}
        if self.plan:payload.update(plan_id=self.plan['id'],plan_version=self.plan['version'])
        def failed(error):
            self.saving=False
            self.error(error)
        self.bridge.command('revise_plan' if self.plan else 'create_plan',payload,saved,failed,epoch=self.epoch,expected_revision=getattr(self,'context_revision',None))


from .gui_assistant import AssistanceDialog


class JobsDialog(QDialog):
    def __init__(self, bridge, parent=None, on_changed=None):
        super().__init__(parent)
        self.bridge, self.on_changed = bridge, on_changed
        self.detail_generation = 0
        self.full_job = {}
        self.offset, self.next_offset, self.loading = 0, None, False
        self.setWindowTitle("后台事项与结果")
        self.resize(970, 630)
        layout = QVBoxLayout(self)
        heading = QLabel("后台事项与结果")
        heading.setObjectName("DialogHeading")
        layout.addWidget(heading)
        explanation = QLabel("这里保留处理进度、失败原因和未采用的候选。取消后，迟到结果不会被采用。")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        split = QSplitter()
        self.list = QListWidget()
        self.list.setMinimumWidth(260)
        self.list.currentItemChanged.connect(self.show_job)
        split.addWidget(self.list)
        self.detail = QTextBrowser()
        split.addWidget(self.detail)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setObjectName("Error")
        layout.addWidget(self.message)
        actions = ActionRow()
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        self.cancel = QPushButton("取消此处理")
        self.cancel.clicked.connect(self.cancel_job)
        self.apply = QPushButton("采用候选变更")
        self.apply.setObjectName("Primary")
        self.apply.clicked.connect(self.apply_job)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        for widget in (refresh, self.cancel, self.apply):
            actions.addWidget(widget)
        actions.addStretch()
        self.previous = QPushButton("上一页")
        self.previous.clicked.connect(self.previous_page)
        self.next = QPushButton("下一页")
        self.next.clicked.connect(self.next_page)
        actions.addWidget(self.previous)
        actions.addWidget(self.next)
        actions.addWidget(close)
        layout.addWidget(actions)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self):
        if self.loading:
            return
        self.loading = True
        selected = self.current().get("id")
        def loaded(result):
            self.loading = False
            self.next_offset = result.get("next_offset")
            self.previous.setEnabled(self.offset > 0)
            self.next.setEnabled(self.next_offset is not None)
            self.list.blockSignals(True)
            self.list.clear()
            index = 0
            for i, job in enumerate(result.get("items", [])):
                description = job.get("input", {}).get("prompt") or job.get("title") or job.get("kind", "后台处理")
                item = QListWidgetItem(f"{str(description)[:55]}\n{label_status(job.get('status'))} · {job.get('created_at', '')[:16]}")
                item.setData(Qt.ItemDataRole.UserRole, job)
                self.list.addItem(item)
                if job.get("id") == selected:
                    index = i
            if self.list.count():
                self.list.setCurrentRow(index)
            self.list.blockSignals(False)
            if self.list.count():
                self.show_job()
            else:
                self.detail.setPlainText("还没有后台事项。\n\n从“让 Codex 协助”开始，结果会保留在这里。")
                self.apply.setEnabled(False)
                self.cancel.setEnabled(False)
        self.bridge.query("jobs", loaded, self.error, limit=50, offset=self.offset)

    def previous_page(self):
        self.offset = max(0, self.offset - 50)
        self.refresh()

    def next_page(self):
        if self.next_offset is not None:
            self.offset = self.next_offset
            self.refresh()

    def current(self):
        item = self.list.currentItem()
        summary = item.data(Qt.ItemDataRole.UserRole) if item else {}
        if self.full_job.get("id") == summary.get("id") and self.full_job.get("status") == summary.get("status"):
            return self.full_job
        return summary

    def show_job(self, *_):
        job = self.current()
        if not job:
            return
        if not job.get("detail_required"):
            if self.full_job.get("id") == job.get("id") and self.full_job.get("status") == job.get("status"):
                return
            self.full_job = job
            self.render_job(job)
            return
        self.detail_generation += 1
        generation = self.detail_generation
        self.apply.setEnabled(False)
        self.cancel.setEnabled(False)
        self.detail.setPlainText("正在读取完整处理结果…")
        def loaded(result):
            if generation != self.detail_generation:
                return
            detail = result["job"]
            selected = self.list.currentItem()
            if selected is None or selected.data(Qt.ItemDataRole.UserRole).get("id") != detail["id"]:
                return
            self.full_job = detail
            self.render_job(detail)
        self.bridge.query("job", loaded, self.error, id=job["id"])

    def render_job(self, job):
        result = job.get("result") or job.get("proposal") or {}
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                result = {"summary": result}
        if isinstance(result.get("proposal"), dict):
            result = result["proposal"]
        lines = [f"<h2>{html.escape(label_status(job.get('status')))}</h2>"]
        if job.get("input", {}).get("prompt"):
            lines.append(f"<p>{html.escape(job['input']['prompt'])}</p>")
        if job.get("error"):
            lines.append(f"<h3>需要处理</h3><p>{html.escape(readable(job['error']))}</p>")
        if result.get("metadata"):
            lines.append("<h3>已生成并核验</h3><p>" + html.escape(readable(result["metadata"])).replace("\n", "<br>") + "</p>")
            lines.append("<p>可在“资料”中查看成果版本，再选择采用或导出。</p>")
        if result.get("summary"):
            lines.append(f"<h3>处理结果</h3><p>{html.escape(result['summary'])}</p>")
        for index, action in enumerate(result.get("actions", []), 1):
            payload = action.get("payload") or {}
            if not payload and action.get("payload_json"):
                try:
                    payload = json.loads(action["payload_json"])
                except ValueError:
                    payload = {}
            verb = {"create": "创建", "update": "更新", "record_feedback": "记录反馈", "create_plan": "保存计划", "save_review": "保存复盘"}.get(action.get("command"), action.get("command"))
            lines.append(f"<h3>{index}. {html.escape(str(verb))} {html.escape(payload.get('title', ''))}</h3>")
            lines.append("<p>" + html.escape(action.get("reason", "")) + "</p>")
            lines.append("<p>" + html.escape(readable(payload)).replace("\n", "<br>") + "</p>")
        if result.get("unknowns"):
            lines.append("<h3>仍待确认</h3><p>" + html.escape(readable(result["unknowns"])).replace("\n", "<br>") + "</p>")
        if result.get("sources"):
            lines.append("<h3>依据</h3><p>" + html.escape(readable(result["sources"])).replace("\n", "<br>") + "</p>")
        self.detail.setHtml("".join(lines))
        status = job.get("status")
        self.cancel.setEnabled(status in ("queued", "running", "awaiting_review"))
        self.apply.setEnabled(status in ("succeeded", "ready", "needs_review", "awaiting_review") and bool(result.get("actions")) and not job.get("applied"))

    def error(self, error):
        self.loading = False
        self.message.setText(error.get("message", str(error)))

    def cancel_job(self):
        job = self.current()
        if job:
            self.bridge.command("cancel_job", {"id": job["id"]}, lambda _: self.changed(), self.error)

    def apply_job(self):
        job = self.current()
        if job:
            self.apply.setEnabled(False)
            self.bridge.command("apply_proposal", {"id": job["id"]}, lambda _: self.changed(), self.error)

    def changed(self):
        self.message.setText("")
        self.refresh()
        if self.on_changed:
            self.on_changed()

    def done(self, result):
        self.timer.stop()
        super().done(result)


class ArtifactDialog(FormDialog):
    def __init__(self, bridge, parent=None, on_saved=None):
        super().__init__("创建成果文件", parent, 760)
        self.bridge, self.on_saved = bridge, on_saved
        self.epoch = bridge.epoch
        form = QFormLayout()
        self.body_layout.addLayout(form)
        self.title = QLineEdit()
        self.kind = QComboBox()
        for label, value in (("说明文档（Markdown）", "markdown"), ("纯文本", "text"), ("数据表（CSV）", "csv"), ("练习 Notebook", "notebook"), ("Word 文档", "docx"), ("PDF 文档", "pdf")):
            self.kind.addItem(label, value)
        self.path = QLineEdit("成果.md")
        form.addRow("成果名称", self.title)
        form.addRow("文件类型", self.kind)
        form.addRow("文件名", self.path)
        self.content = QTextEdit()
        self.content.setPlaceholderText("编写内容后创建独立成果版本。程序会先登记作业，再生成和核验文件。")
        self.body_layout.addWidget(self.content, 1)
        self.kind.currentIndexChanged.connect(lambda: self.path.setText({"markdown": "成果.md", "text": "成果.txt", "csv": "数据.csv", "notebook": "练习.ipynb", "docx": "文档.docx", "pdf": "文档.pdf"}[self.kind.currentData()]))
        self.buttons.accepted.connect(self.save)

    def save(self):
        if not self.title.text().strip() or not self.content.toPlainText().strip():
            self.error("请填写成果名称和内容。")
            return
        kind = self.kind.currentData()
        content = self.content.toPlainText()
        if kind == "csv":
            content = list(csv.reader(io.StringIO(content)))
        elif kind == "notebook":
            content = {"nbformat": 4, "nbformat_minor": 5, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}}, "cells": [
                {"id": uuid.uuid4().hex[:8], "cell_type": "markdown", "metadata": {}, "source": content.splitlines(keepends=True)},
                {"id": uuid.uuid4().hex[:8], "cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": []},
            ]}
        self.busy()
        def saved(result):
            if self.on_saved:
                self.on_saved(result)
            self.accept()
        self.bridge.command("create_artifact_job", {"title": self.title.text().strip(), "kind": kind, "relative_path": self.path.text().strip(), "content": content}, saved, self.error, epoch=self.epoch)


class HabitsDialog(QDialog):
    """Daily preferences have their own entrance, separate from application settings."""
    def __init__(self, bridge, capabilities, parent=None, on_saved=None):
        super().__init__(parent)
        self.bridge, self.capabilities, self.on_saved = bridge, capabilities, on_saved
        self.data_dir = getattr(parent, 'data_dir', getattr(bridge, 'data_dir', getattr(getattr(bridge, 'core', None), 'root', None)))
        self.codex_connection = getattr(parent, 'codex_connection', None)
        self.preferences_epoch = self.preferences_revision = None
        self._closed = False
        self.finished.connect(self._finished)
        self.destroyed.connect(lambda *_: setattr(self, '_closed', True))
        self.setWindowTitle('日常习惯')
        self.resize(940, 710)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 18)
        outer.setSpacing(14)
        heading = QLabel('日常习惯')
        heading.setObjectName('DialogHeading')
        outer.addWidget(heading)
        reminder = QWidget()
        reminder_layout = QVBoxLayout(reminder)
        reminder_layout.setContentsMargins(0, 12, 0, 0)
        reminder_layout.setSpacing(14)
        intro = QLabel('直接设置你希望什么时候复盘。开启后，本机服务在约定时间提示；保存时间不会重复增加提醒。')
        intro.setWordWrap(True)
        reminder_layout.addWidget(intro)
        reminder_form = self.reminder_form = QFormLayout()
        reminder_form.setVerticalSpacing(14)
        self.daily_enabled = QCheckBox('每天提醒我复盘')
        self.daily_time = QTimeEdit(QTime(21,30))
        self.daily_time.setDisplayFormat('HH:mm')
        self.weekly_enabled = QCheckBox('每周提醒我回顾')
        self.weekly_day = QComboBox()
        for day in ['周一','周二','周三','周四','周五','周六','周日']:
            self.weekly_day.addItem(day)
        self.weekly_time = QTimeEdit(QTime(19,30))
        self.weekly_time.setDisplayFormat('HH:mm')
        self.review_timezone = QLineEdit('Asia/Shanghai')
        reminder_form.addRow(self.daily_enabled)
        reminder_form.addRow('每日复盘时间', self.daily_time)
        reminder_form.addRow(self.weekly_enabled)
        weekly_row = ActionRow()
        weekly_row.addWidget(self.weekly_day)
        weekly_row.addWidget(self.weekly_time)
        reminder_form.addRow('每周回顾时间', weekly_row)
        reminder_form.addRow('时区', self.review_timezone)
        reminder_layout.addLayout(reminder_form)
        self.daily_enabled.toggled.connect(self.daily_time.setEnabled)
        self.weekly_enabled.toggled.connect(self.weekly_time.setEnabled)
        self.weekly_enabled.toggled.connect(self.weekly_day.setEnabled)
        self.preference_note = QLabel('正在读取已设置的时间…')
        self.preference_note.setWordWrap(True)
        reminder_layout.addWidget(self.preference_note)
        self.preferences_ready = False
        self.preferences_saving = False
        self.preferences_generation = 0
        save_reminders = self.save_reminders = QPushButton('保存复盘时间')
        save_reminders.setEnabled(False)
        save_reminders.setObjectName('Primary')
        save_reminders.clicked.connect(self.save_review_times)
        read_reminders = self.read_reminders = QPushButton('重新读取已保存的时间')
        read_reminders.clicked.connect(self.load_review_times)
        reminder_actions = ActionRow()
        reminder_actions.addWidget(save_reminders)
        reminder_actions.addWidget(read_reminders)
        reminder_layout.addWidget(reminder_actions)
        hint = QLabel('到复盘页记录实际结果；没有每日计划也可选择事项反馈或写下小结。电脑关闭期间不会执行提醒。')
        hint.setWordWrap(True)
        hint.setObjectName('Hint')
        reminder_layout.addWidget(hint)
        reminder_layout.addStretch()
        from .gui_habits import HabitsPanel
        self.habits=HabitsPanel(bridge,reminder,self,on_changed=self.saved)
        outer.addWidget(self.habits, 1)
        self.message = QLabel()
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('关闭')
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        reminder_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        reminder_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        from .gui_theme import bind_theme
        bind_theme(self, self.fit_fields)
        self.habits.set_assistants_visible(False)
        def loaded(result):
            if self._closed:
                return
            from .appearance import assistants_visible
            self.habits.set_assistants_visible(assistants_visible(result.get('settings', {})))
        self.bridge.query('settings', loaded, self.error)
        self.load_review_times()
        from .gui_tutorials import install_dialog_tutorial
        install_dialog_tutorial(self, 'habits')

    def fit_fields(self):
        fit_input_fields(self)
        scale = max(1, self.fontMetrics().height() / 18)
        for field in (self.daily_time, self.weekly_time, self.weekly_day):
            field.setMaximumWidth(max(round(120 * scale), field.sizeHint().width()))
        self.review_timezone.setMaximumWidth(round(280 * scale))
        self.reminder_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.habits.widget().updateGeometry()

    def _finished(self, *_):
        self._closed = True
        self.preferences_generation += 1

    def error(self, error):
        if self._closed:
            return
        self.message.setObjectName('Error')
        self.message.setText(error.get('message', str(error)))

    def saved(self, result):
        if self._closed:
            return
        if isinstance(result, dict) and isinstance(self.preferences_revision, int) and result.get('epoch') == self.preferences_epoch and result.get('revision') == self.preferences_revision + 1:
            self.preferences_revision = result['revision']
        self.message.setText('已保存。')
        self.habits.refresh()
        if self.on_saved:
            self.on_saved(result)

    def review_fields_enabled(self, enabled):
        for widget in (self.daily_enabled, self.weekly_enabled, self.review_timezone):
            widget.setEnabled(enabled)
        self.daily_time.setEnabled(enabled and self.daily_enabled.isChecked())
        self.weekly_time.setEnabled(enabled and self.weekly_enabled.isChecked())
        self.weekly_day.setEnabled(enabled and self.weekly_enabled.isChecked())

    def load_review_times(self):
        if self.preferences_saving:
            return
        self.review_fields_enabled(False)
        self.preferences_ready = False
        self.save_reminders.setEnabled(False)
        self.preferences_generation += 1
        generation = self.preferences_generation
        def loaded(result):
            if self._closed or generation != self.preferences_generation:
                return
            self.preferences_ready = True
            self.save_reminders.setEnabled(True)
            self.preferences_epoch, self.preferences_revision = result.get("epoch"), result.get("revision")
            self.daily_enabled.setChecked(result["daily"]["enabled"])
            self.daily_time.setTime(QTime.fromString(result["daily"]["time"], "HH:mm"))
            self.daily_time.setEnabled(result["daily"]["enabled"])
            self.weekly_enabled.setChecked(result["weekly"]["enabled"])
            self.weekly_time.setTime(QTime.fromString(result["weekly"]["time"], "HH:mm"))
            self.weekly_day.setCurrentIndex(result["weekly"]["weekday"])
            self.weekly_time.setEnabled(result["weekly"]["enabled"])
            self.weekly_day.setEnabled(result["weekly"]["enabled"])
            self.review_timezone.setText(result["timezone"])
            self.review_fields_enabled(True)
            self.preference_note.setText("检测到多条旧的全局复盘提醒。保存后会统一到这里的时间，旧记录保留。" if result.get("duplicate_schedules") else "未启用的提醒不会运行。")
        self.bridge.query("review_preferences", loaded, self.error)

    def save_review_times(self):
        if not self.preferences_ready or not self.save_reminders.isEnabled():
            return
        self.preferences_saving = True
        self.preferences_generation += 1
        self.read_reminders.setEnabled(False)
        self.save_reminders.setEnabled(False)
        self.review_fields_enabled(False)
        payload = {"daily": {"enabled": self.daily_enabled.isChecked(), "time": self.daily_time.time().toString("HH:mm")}, "weekly": {"enabled": self.weekly_enabled.isChecked(), "weekday": self.weekly_day.currentIndex(), "time": self.weekly_time.time().toString("HH:mm")}, "timezone": self.review_timezone.text().strip()}
        def saved(result):
            if self._closed:
                return
            self.preferences_epoch, self.preferences_revision = result.get("epoch"), result.get("revision")
            self.save_reminders.setEnabled(True)
            self.review_fields_enabled(True)
            self.preferences_saving = False
            self.read_reminders.setEnabled(True)
            self.preference_note.setText("复盘时间已保存。")
            self.saved(result)
        def failed(error):
            if self._closed:
                return
            self.preferences_saving = False
            self.read_reminders.setEnabled(True)
            self.save_reminders.setEnabled(True)
            self.review_fields_enabled(True)
            self.error(error)
        self.bridge.command("set_review_preferences", payload, saved, failed, epoch=self.preferences_epoch, expected_revision=self.preferences_revision)


class SettingsDialog(QDialog):
    def __init__(self, bridge, capabilities, data_dir, parent=None, on_changed=None):
        super().__init__(parent)
        self.bridge, self.capabilities, self.data_dir, self.on_changed = bridge, capabilities, Path(data_dir), on_changed
        self.codex_connection = getattr(parent, 'codex_connection', None)
        self._owns_codex_connection = self.codex_connection is None
        if self._owns_codex_connection:
            from .gui_codex_connection import CodexConnectionController
            self.codex_connection = CodexConnectionController(data_dir, self, bridge=bridge)
        self.current_settings = {}
        self.display_dirty = False
        self._display_loading = False
        self._display_edit_version = 0
        self._display_snapshot = normalize_appearance()
        self._chart_snapshot = normalize_chart_preferences()
        self._display_saving = False
        self._display_request = None
        self.preferences_epoch = self.preferences_revision = None
        self._models_generation = 0
        self._models_loading = self._models_pending_refresh = self._models_closed = False
        self._models_for_path = None
        self._model_items = []
        self._ai_settings_ready = False
        self._ai_configuring = False
        self._visibility_dirty = False
        self.setWindowTitle("设置")
        self.resize(900, 760)
        outer = QVBoxLayout(self)
        heading = QLabel("设置")
        heading.setObjectName("DialogHeading")
        outer.addWidget(heading)
        from .gui_materials import AnimatedTabWidget, TabTransition
        tabs = AnimatedTabWidget()
        outer.addWidget(tabs, 1)
        self.tabs = tabs
        def add_page(widget,title):
            if isinstance(widget,QScrollArea):
                return tabs.addTab(widget,title)
            scroll=QScrollArea();scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setMinimumSize(0,0)
            if widget.layout():widget.layout().setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
            scroll.setWidget(widget)
            return tabs.addTab(scroll,title)
        display=QWidget()
        display_layout=QVBoxLayout(display)
        display_layout.setSpacing(8)
        appearance_group=SettingsGroup('外观','明暗与强调色可以分别选择。下方预览不改变已保存的设置。')
        display_layout.addWidget(appearance_group)
        appearance_form=QFormLayout()
        self.theme_picker=QComboBox()
        self.theme_picker.addItem('浅色','light')
        self.theme_picker.addItem('深色','dark')
        appearance_form.addRow('明暗模式',self.theme_picker)
        appearance_group.layout().addLayout(appearance_form)
        accent_title=QLabel('强调色');accent_title.setObjectName('SectionHeading')
        appearance_group.layout().addWidget(accent_title)
        self.accent_picker=AccentPicker()
        appearance_group.layout().addWidget(self.accent_picker)
        self.appearance_preview=AppearancePreview()
        appearance_group.layout().addWidget(self.appearance_preview)
        text_group=SettingsGroup('文字','调整字体和大小；未安装的字体会自动使用本机字体显示。')
        display_layout.addWidget(text_group)
        font_form=QFormLayout()
        self.font_picker=QComboBox()
        self.font_picker.setEditable(False)
        self.font_picker.setMinimumContentsLength(16)
        self.font_picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.font_picker.addItem('系统推荐字体','')
        self._installed_font_families=available_font_families()
        for family in self._installed_font_families:
            self.font_picker.addItem(family,family)
        self.font_size=QSpinBox()
        self.font_size.setRange(11,20)
        self.font_size.setValue(13)
        font_form.addRow('字体样式',self.font_picker)
        font_form.addRow('字号',self.font_size)
        text_group.layout().addLayout(font_form)
        self.font_preview=QLabel('字体预览：安排、记录与回顾 · Notes 123')
        self.font_preview.setObjectName('FontPreview')
        self.font_preview.setWordWrap(True)
        text_group.layout().addWidget(self.font_preview)
        self.appearance_note=QLabel('保存后，已经打开的窗口会立即应用。')
        self.appearance_note.setObjectName('Hint')
        self.appearance_note.setWordWrap(True)
        display_layout.addWidget(self.appearance_note)
        self.chart_options_box = SettingsGroup('图表样式','按使用位置选择合适的图表。保存后应用到对应页面。')
        chart_layout = self.chart_options_box.layout()
        chart_form = QFormLayout()
        chart_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        chart_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.chart_pickers = {}
        self.chart_labels = {
            'dashboard_today_style': '总览 · 今日计划',
            'dashboard_tasks_style': '总览 · 任务概况',
            'review_daily_style': '复盘 · 当日反馈',
            'review_weekly_style': '复盘 · 本周汇总',
            'weekly_style': '复盘 · 每天的情况',
        }
        for key, title in self.chart_labels.items():
            picker = QComboBox()
            picker.setAccessibleName(title + '图表样式')
            for label, value in CHART_OPTIONS[key]:
                picker.addItem(label, value)
            self.chart_pickers[key] = picker
            chart_form.addRow(title, picker)
            picker.currentIndexChanged.connect(lambda _, k=key: self.preview_chart(k))
            picker.currentIndexChanged.connect(self.display_edited)
        self.weekly_chart_style = self.chart_pickers['weekly_style']
        chart_layout.addLayout(chart_form)
        self.chart_preview_button = QPushButton('查看当前样式预览')
        self.chart_preview_button.clicked.connect(self.show_chart_preview)
        chart_actions=ActionRow();chart_actions.addWidget(self.chart_preview_button);chart_layout.addWidget(chart_actions)
        chart_help=QLabel('每处可以单独选择。只改变外观，不改变计划、反馈或统计口径；未反馈不算作失败。')
        chart_help.setWordWrap(True)
        chart_layout.addWidget(chart_help)
        self.chart_preview_note = QLabel()
        self.chart_preview_note.setWordWrap(True)
        chart_layout.addWidget(self.chart_preview_note)
        from .gui_charts import CoverageChart, WeekDaysChart
        self.chart_preview = CoverageChart()
        self.weekly_preview = WeekDaysChart()
        chart_layout.addWidget(self.chart_preview)
        chart_layout.addWidget(self.weekly_preview)
        self.preview_chart('dashboard_today_style')
        display_layout.addWidget(self.chart_options_box)
        self.chart_save=QPushButton('保存显示与图表')
        self.chart_save.setObjectName('Primary');self.chart_save.setEnabled(False)
        self.chart_save.clicked.connect(self.save_chart_style)
        self.display_reload=QPushButton('放弃本页修改并重新读取')
        self.display_reload.clicked.connect(self.reload_display_preferences)
        self.display_actions = ActionRow()
        self.display_actions.addWidget(self.chart_save)
        self.display_actions.addWidget(self.display_reload)
        outer.addWidget(self.display_actions)
        for combo in (self.theme_picker,self.accent_picker,self.font_picker):
            combo.currentIndexChanged.connect(self.display_edited)
        self.font_size.valueChanged.connect(self.display_edited)
        display_layout.addStretch()
        add_page(display,'显示')
        tabs.currentChanged.connect(lambda index: self.display_actions.setVisible(tabs.tabText(index) == '显示'))
        from .gui_timetable_settings import TimetableSettingsPage
        self.timetable_settings = TimetableSettingsPage(bridge, self, on_changed=on_changed)
        tabs.addTab(self.timetable_settings, '课表')

        provider = QWidget()
        provider_root = QVBoxLayout(provider)
        visibility_group=SettingsGroup('助手入口','选择是否在今天、项目和复盘中显示助手操作。手动记录与安排始终可用。')
        provider_root.addWidget(visibility_group)
        provider_layout=visibility_group.layout()
        self.show_assistants = QCheckBox('在各面板显示助手操作入口')
        self.show_assistants.setToolTip('只控制入口显示；不会启用或调用 Codex，也不会取消已有工作。')
        self.show_assistants.toggled.connect(lambda _: setattr(self, '_visibility_dirty', True))
        provider_layout.addWidget(self.show_assistants)
        self.assistant_visibility_save = QPushButton('保存入口显示')
        self.assistant_visibility_save.setEnabled(False)
        self.assistant_visibility_save.clicked.connect(self.save_assistant_visibility)
        visibility_actions=ActionRow();visibility_actions.addWidget(self.assistant_visibility_save);provider_layout.addWidget(visibility_actions)
        visibility_help = QLabel('入口显示与使用授权分别保存。即使隐藏面板中的助手操作，左下角仍可打开本页；已有处理记录仍可查看。')
        visibility_help.setWordWrap(True)
        visibility_help.setObjectName('Hint')
        provider_layout.addWidget(visibility_help)
        authorization_group=SettingsGroup('使用授权','先在本机 Codex 登录，再允许协助并保存。')
        provider_root.addWidget(authorization_group)
        provider_layout=authorization_group.layout()
        form = QFormLayout()
        self.ai_enabled = QCheckBox("允许软件使用本机 Codex 辅助处理")
        self.executable = QLineEdit()
        self.executable.setPlaceholderText("留空自动寻找本机 Codex")
        self.model = QComboBox()
        self.model.setEditable(False)
        self.model.addItem('跟随本机 Codex 设置（自动选择）', '')
        self.model.setMinimumContentsLength(16)
        self.model_refresh = QPushButton('刷新可选模型')
        self.model_refresh.clicked.connect(self.refresh_models)
        self.model_refresh.setEnabled(False)
        self.model_note = QLabel('正在读取已保存的选择…')
        self.model_note.setWordWrap(True)
        self.model_note.setObjectName('Hint')
        self._model_debounce = QTimer(self)
        self._model_debounce.setSingleShot(True)
        self._model_debounce.setInterval(600)
        self._model_debounce.timeout.connect(self.refresh_models)
        self.executable.textChanged.connect(self._model_path_changed)
        self.finished.connect(self._model_dialog_finished)
        self.timeout = QSpinBox()
        self.timeout.setRange(10, 900)
        self.timeout.setValue(180)
        self.timeout.setSuffix(" 秒")
        form.addRow(self.ai_enabled)
        self.ai_mode = QComboBox()
        self.ai_mode.addItem('在 Codex 桌面同步显示', 'desktop_shared')
        self.ai_mode.addItem('仅在管理软件中处理', 'background')
        self.ai_mode.setCurrentIndex(1)
        form.addRow('处理方式', self.ai_mode)
        model_row = ActionRow()
        model_row.addWidget(self.model)
        model_row.addWidget(self.model_refresh)
        form.addRow("模型", model_row)
        form.addRow('', self.model_note)
        provider_layout.addLayout(form)
        self.ai_advanced_toggle = QPushButton('高级连接选项')
        self.ai_advanced_toggle.setCheckable(True)
        self.ai_advanced_toggle.setProperty('disclosure', True)
        advanced_actions=ActionRow();advanced_actions.addWidget(self.ai_advanced_toggle);provider_layout.addWidget(advanced_actions)
        self.ai_advanced = QWidget()
        advanced_form = QFormLayout(self.ai_advanced)
        advanced_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        advanced_form.addRow('Codex 程序位置', self.executable)
        advanced_form.addRow('单次等待上限', self.timeout)
        self.ai_advanced.hide()
        self.ai_advanced_toggle.toggled.connect(self.ai_advanced.setVisible)
        provider_layout.addWidget(self.ai_advanced)
        save = self.ai_save = QPushButton("保存 Codex 协助设置")
        save.setEnabled(False)
        save.setObjectName("Primary")
        save.clicked.connect(self.save_ai)
        authorization_actions=ActionRow();authorization_actions.addWidget(save);provider_layout.addWidget(authorization_actions)
        connection_group=SettingsGroup('连接与对话','查看连接状态，或打开已准备好的对话项目。')
        provider_root.addWidget(connection_group)
        provider_layout=connection_group.layout()
        self.codex_project_note = SelectableText('首次连接或发送时会准备对话项目与业务接口；当前连接状态见下方。')
        self.codex_project_note.setObjectName('Hint')
        provider_layout.addWidget(self.codex_project_note)
        self.codex_open_project = QPushButton('打开 Codex 事务助手')
        self.codex_open_project.setEnabled(False)
        self.codex_open_project.clicked.connect(self.open_codex_project)
        connection_actions=ActionRow();connection_actions.addWidget(self.codex_open_project);provider_layout.addWidget(connection_actions)
        connection_help = QLabel('新对话会通过同一业务服务读取与更新数据，保存结果以业务回执为准。关闭上方开关只停止软件内的后台协助，已创建的 Codex 项目与接口仍然保留。')
        connection_help.setWordWrap(True)
        connection_help.setObjectName('Hint')
        provider_layout.addWidget(connection_help)
        advanced_connection = QPushButton('高级：查看接口配置')
        advanced_connection.setCheckable(True)
        advanced_connection.setProperty('disclosure', True)
        advanced_connection_actions=ActionRow();advanced_connection_actions.addWidget(advanced_connection);provider_layout.addWidget(advanced_connection_actions)
        copy_config = QPushButton('复制当前数据空间的 MCP 配置')
        copy_config.clicked.connect(self.copy_codex_config)
        copy_config.hide()
        advanced_connection.toggled.connect(copy_config.setVisible)
        config_actions=ActionRow();config_actions.addWidget(copy_config);provider_layout.addWidget(config_actions)
        provider_root.addStretch()
        self._provider_tab = add_page(provider, "Codex 协助")
        tabs.currentChanged.connect(self._model_tab_changed)

        rules = QWidget()
        rules_layout = QVBoxLayout(rules)
        note = QLabel("规则决定容量、保护时间和警戒方式。定时任务由独立业务服务运行，关闭界面不影响服务中已配置的任务；计算机关闭期间不执行。")
        note.setWordWrap(True)
        rules_layout.addWidget(note)
        self.rules = QListWidget()
        rules_layout.addWidget(self.rules, 1)
        row = ActionRow()
        for label, kind in (("新建规则", "rule"), ("新建定时任务", "schedule")):
            button = QPushButton(label)
            button.clicked.connect(lambda _, k=kind: self.edit_rule(kind=k))
            row.addWidget(button)
        edit = QPushButton("编辑选中项")
        edit.clicked.connect(lambda: self.edit_rule())
        row.addWidget(edit)
        rules_layout.addWidget(row)
        # Advanced maintenance stays available without becoming daily navigation.

        modules = QWidget()
        modules_layout = QVBoxLayout(modules)
        module_note = QLabel("扩展字段、规则、流程和业务类型在已有入口内呈现。停用后历史数据仍可读取。模块文件只能声明已支持的能力。")
        module_note.setWordWrap(True)
        modules_layout.addWidget(module_note)
        self.modules = QListWidget()
        modules_layout.addWidget(self.modules, 1)
        row = ActionRow()
        install = QPushButton("从文件安装模块")
        install.clicked.connect(self.install_module)
        disable = QPushButton("停用选中模块")
        disable.clicked.connect(self.disable_module)
        row.addWidget(install)
        row.addWidget(disable)
        modules_layout.addWidget(row)
        # Module definitions are not a list of everyday user actions.

        data = QWidget()
        data_root = QVBoxLayout(data)
        location_group=SettingsGroup('资料位置')
        data_root.addWidget(location_group)
        data_layout=location_group.layout()
        location = SelectableText(f"当前数据位置\n{self.data_dir}")
        data_layout.addWidget(location)
        from .gui_library import open_library
        originals=QPushButton('打开原文件目录')
        originals.clicked.connect(lambda:open_library(self.bridge,self,originals,None,self.error))
        original_actions=ActionRow();original_actions.addWidget(originals);data_layout.addWidget(original_actions)
        backup_group=SettingsGroup('备份与恢复')
        data_root.addWidget(backup_group)
        data_layout=backup_group.layout()
        data_note = QLabel("备份包含业务记录和已保存的资料版本。本地文件引用只保存路径，不会复制外部原件。恢复会写入新建的空目录，成功后从该目录启动应用；不会覆盖当前数据。")
        data_note.setWordWrap(True)
        data_layout.addWidget(data_note)
        backup = QPushButton("创建并核验备份")
        backup.clicked.connect(self.backup)
        restore = QPushButton("恢复备份到新目录")
        restore.clicked.connect(self.restore)
        backup_actions=ActionRow();backup_actions.addWidget(backup);backup_actions.addWidget(restore);data_layout.addWidget(backup_actions)
        self.data_result = QTextBrowser()
        data_layout.addWidget(self.data_result, 1)
        self.data_result.hide()
        self.data_result.setMaximumHeight(150)
        maintenance_group=SettingsGroup('高级维护','规则和扩展适合需要进一步定制时使用。')
        data_root.addWidget(maintenance_group)
        data_layout=maintenance_group.layout()
        advanced = QPushButton('高级规则与扩展')
        advanced.setCheckable(True)
        advanced.setProperty('disclosure', True)
        maintenance_actions=ActionRow();maintenance_actions.addWidget(advanced);data_layout.addWidget(maintenance_actions)
        advanced_tabs = AnimatedTabWidget()
        advanced_tabs.addTab(rules, '规则')
        advanced_tabs.addTab(modules, '扩展')
        self.advanced_tabs = advanced_tabs
        advanced_tabs.setVisible(False)
        advanced.toggled.connect(advanced_tabs.setVisible)
        data_layout.addWidget(advanced_tabs)
        data_root.addStretch()
        add_page(data, '数据与高级')
        self.tab_transition = TabTransition(tabs)
        self.advanced_tab_transition = TabTransition(advanced_tabs)

        # Connection progress and the next action must remain visible even when
        # large fonts make the provider settings taller than the scroll page.
        # Keep this separate from save errors in the other settings tabs.
        self.codex_bridge_note = QLabel('Codex 连接：启用并保存设置后可查看状态或连接。')
        self.codex_bridge_note.setTextFormat(Qt.TextFormat.PlainText)
        self.codex_bridge_note.setWordWrap(True)
        self.codex_bridge_note.setObjectName('Notice')
        self.codex_bridge_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.codex_bridge_note)
        self.message = QLabel()
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        actions = QHBoxLayout()
        self.codex_bridge_start = QPushButton('连接 Codex')
        self.codex_bridge_start.clicked.connect(self.launch_codex_desktop)
        actions.addWidget(self.codex_bridge_start)
        actions.addStretch()
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close.rejected.connect(self.reject)
        actions.addWidget(close)
        outer.addLayout(actions)
        self.ai_mode.currentIndexChanged.connect(self.show_codex_mode)
        tabs.currentChanged.connect(self.show_codex_mode)
        self.show_codex_mode()
        if self.codex_connection is not None:
            self.codex_connection.changed.connect(self.show_connection_state)
            self.show_connection_state(self.codex_connection.snapshot())
        for editor_form in (appearance_form,font_form,chart_form,form,advanced_form):
            editor_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            editor_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        from .gui_theme import bind_theme
        bind_theme(self,self.fit_settings_fields)
        self.fit_settings_fields()
        self.load()

        from .gui_tutorials import install_dialog_tutorial
        install_dialog_tutorial(self, 'settings')

    def fit_settings_fields(self):
        # Scroll pages keep the content's natural height; font changes must
        # never squeeze edit fields below their text and padding.
        fit_input_fields(self)
        scale=max(1,self.fontMetrics().height()/18)
        self.appearance_preview.setMaximumWidth(round(440*scale))
        self.font_preview.setMaximumWidth(round(540*scale))
        self.font_picker.setMaximumWidth(round(320*scale))
        self.model.setMaximumWidth(round(360*scale))
        self.executable.setMinimumWidth(0)
        self.executable.setMaximumWidth(round(420*scale))
        if hasattr(self,'accent_picker'):
            self.display_edited(update_dirty=False)
        for index in range(self.tabs.count()):
            page=self.tabs.widget(index)
            if isinstance(page,QScrollArea) and page.widget():
                page.widget().updateGeometry()

    def error(self, error):
        self.message.setObjectName("Error")
        self.message.setText(error.get("message", str(error)))

    def load(self):
        def loaded(result):
            if self._models_closed:
                return
            self.current_settings = result.get("settings", {})
            if not self.display_dirty and not self._display_saving and self._display_request is None:
                self.set_display_preferences(result)
            if not self._visibility_dirty:
                from .appearance import assistants_visible
                self.show_assistants.blockSignals(True)
                self.show_assistants.setChecked(assistants_visible(self.current_settings))
                self.show_assistants.blockSignals(False)
            config = self.current_settings.get("ai", {})
            if self.codex_connection is not None:
                self.codex_connection.configure(self.current_settings)
            self.show_codex_project(self.current_settings.get("codex_project"), enabled=config.get("enabled", False))
            self.ai_enabled.setChecked(config.get("enabled", False))
            self.ai_mode.setCurrentIndex(max(0, self.ai_mode.findData(config.get("execution_mode", "background"))))
            self.executable.blockSignals(True)
            self.executable.setText(config.get("executable") or "")
            self.executable.blockSignals(False)
            self._replace_model_choices([], config.get("model") or "")
            self._ai_settings_ready = True
            self.ai_save.setEnabled(True)
            self.assistant_visibility_save.setEnabled(True)
            self.model_refresh.setEnabled(True)
            self.model_note.setText('进入 Codex 协助后读取可选模型；原选择会保留。')
            self._model_tab_changed(self.tabs.currentIndex())
            self.timeout.setValue(config.get("timeout_seconds", 180))
            self.modules.clear()
            modules = result.get("modules", [])
            if isinstance(modules, dict):
                modules = list(modules.values())
            for module in modules:
                manifest = module.get("manifest", module)
                item = QListWidgetItem(f"{manifest.get('label', manifest.get('name', manifest.get('id', '扩展')))} · 版本 {manifest.get('version', 1)}\n{'已启用' if module.get('enabled', True) else '已停用'}")
                item.setData(Qt.ItemDataRole.UserRole, module)
                self.modules.addItem(item)
        self.bridge.query("settings", loaded, self.error)
        self.load_rules()

    def load_rules(self):
        def loaded(result):
            self.rules.clear()
            for entity in result.get("items", []):
                item = QListWidgetItem(f"{entity['title']}\n{'定时任务' if entity['type'] == 'schedule' else '规则'} · {label_status(entity.get('status'))}")
                item.setData(Qt.ItemDataRole.UserRole, entity)
                self.rules.addItem(item)
            if result.get("next_offset") is not None:
                self.message.setText("当前展示前 100 条规则；更多内容可通过全局搜索定位。")
        self.bridge.query("list", loaded, self.error, types=["rule", "schedule"], limit=100)

    def copy_codex_config(self):
        try:
            text = codex_mcp_config(self.data_dir)
        except (OSError, ValueError) as exc:
            self.error({"message": str(exc)})
            return
        QApplication.clipboard().setText(text)
        self.message.setText("当前数据空间的接口配置已复制，不包含连接令牌。添加后，新对话先调用 begin_context。")

    def focus_chart(self, key):
        picker = self.chart_pickers.get(key, self.weekly_chart_style)
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index) == '显示':
                self.tabs.setCurrentIndex(index)
                page = self.tabs.widget(index)
                QTimer.singleShot(0, lambda: page.ensureWidgetVisible(picker, 0, 60))
                break
        self.preview_chart(key if key in self.chart_pickers else 'weekly_style')
        picker.setFocus()

    def preview_chart(self, key):
        if not hasattr(self, 'chart_preview'):
            return
        self._chart_preview_key = key
        style = self.chart_pickers[key].currentData()
        self.chart_preview_note.setText(self.chart_labels[key] + ' · 虚构示例预览（保存后应用）')
        weekly = key == 'weekly_style'
        self.chart_preview.setVisible(not weekly)
        self.weekly_preview.setVisible(weekly)
        if weekly:
            self.weekly_preview.set_style(style)
            self.weekly_preview.set_days([
                {'date': '2030-01-07', 'has_plan': True, 'summary': {'total': 4, 'done': 2, 'incomplete': 1, 'unreported': 1}},
                {'date': '2030-01-08', 'has_plan': True, 'summary': {'total': 3, 'done': 1, 'unreported': 2}},
                {'date': '2030-01-09', 'has_plan': False, 'summary': {}},
            ])
        else:
            self.chart_preview.set_style(style)
            self.chart_preview.set_summary({'total': 8, 'done': 4, 'incomplete': 1, 'unreported': 3}, denominator='示例事项')

    def show_chart_preview(self):
        page = self.tabs.currentWidget()
        if isinstance(page, QScrollArea):
            page.ensureWidgetVisible(self.chart_preview_note, 0, 20)

    def save_assistant_visibility(self):
        if not self.assistant_visibility_save.isEnabled():
            return
        selected = self.show_assistants.isChecked()
        self.assistant_visibility_save.setEnabled(False)
        self.show_assistants.setEnabled(False)
        def saved(result):
            if self._models_closed:
                return
            self.assistant_visibility_save.setEnabled(True)
            self.show_assistants.setEnabled(True)
            self.current_settings.setdefault('appearance', {})['show_assistants'] = selected
            self._display_snapshot['show_assistants'] = selected
            self._visibility_dirty = False
            # An unrelated preference saved by this window can safely advance
            # its snapshot only if no other revision intervened.
            if isinstance(self.chart_revision, int) and result.get('epoch') == self.chart_epoch and result.get('revision') == self.chart_revision + 1:
                self.chart_revision = result['revision']
            self.saved(result)
            self.message.setText('助手入口显示已保存。使用授权请在下方单独设置。')
        def failed(error):
            if self._models_closed:
                return
            self.assistant_visibility_save.setEnabled(True)
            self.show_assistants.setEnabled(True)
            self.error(error)
        self.bridge.command('settings', {'settings': {'appearance': {'show_assistants': selected}}}, saved, failed)

    def set_display_preferences(self, result):
        settings=result.get('settings',{})
        value=normalize_appearance(settings.get('appearance'))
        self._display_loading=True
        self._display_snapshot=value
        self.chart_epoch,self.chart_revision=result.get('epoch'),result.get('revision')
        self.theme_picker.setCurrentIndex(max(0,self.theme_picker.findData(value['theme'])))
        self.accent_picker.setCurrentIndex(max(0,self.accent_picker.findData(value['accent'])))
        index=self.font_picker.findData(value['font_family'])
        if index<0:
            self.font_picker.addItem(value['font_family']+'（本机未安装，保留选择）',value['font_family'])
            index=self.font_picker.count()-1
        self.font_picker.setCurrentIndex(index)
        self.font_size.setValue(value['font_size'])
        charts = normalize_chart_preferences(settings.get('charts'))
        self._chart_snapshot = dict(charts)
        for key, picker in self.chart_pickers.items():
            picker.blockSignals(True)
            picker.setCurrentIndex(picker.findData(charts[key]))
            picker.blockSignals(False)
        self.preview_chart(self._chart_preview_key)
        self.display_dirty=False
        self._display_loading=False
        self._display_edit_version+=1
        self.chart_save.setEnabled(True)
        self.display_edited(update_dirty=False)

    def display_edited(self, *_args, update_dirty=True):
        if self._display_loading:
            return
        if update_dirty:
            self.display_dirty=True
            self._display_edit_version+=1
        value=normalize_appearance({'theme':self.theme_picker.currentData() or 'light',
            'accent':self.accent_picker.currentData(),
            'font_family':self.font_picker.currentData() or '', 'font_size':self.font_size.value()})
        self.accent_picker.set_theme(value['theme'])
        self.appearance_preview.set_appearance(value)
        family=resolved_font_family(value)
        escaped=family.replace('\\','\\\\').replace('"','\\"')
        self.font_preview.setStyleSheet('QLabel#FontPreview {font-family: "'+escaped+'"; font-size: '+str(value['font_size'])+'px;}')
        missing=value['font_family'] and value['font_family'] not in self._installed_font_families
        note='本机未安装原字体，显示时使用系统字体；原选择仍会保留。' if missing else '保存后，已经打开的窗口会立即应用。'
        self.appearance_note.setText(note+(' 本页有尚未保存的修改。' if self.display_dirty else ''))

    def reload_display_preferences(self):
        if self._display_saving or self._display_request is not None:
            return
        def loaded(result):
            if self._models_closed:
                return
            self.current_settings=result.get('settings',{})
            self.set_display_preferences(result)
        self.bridge.query('settings',loaded,self.error)

    def _display_selection(self):
        return {'appearance': {'theme': self.theme_picker.currentData(),
                               'accent': self.accent_picker.currentData(),
                               'font_family': self.font_picker.currentData() or '',
                               'font_size': self.font_size.value()},
                'charts': {key: picker.currentData() for key, picker in self.chart_pickers.items()}}

    def _display_save_controls(self, busy, *, uncertain=False):
        self._display_saving = busy
        self.chart_save.setEnabled(not busy)
        self.chart_save.setText('核对并重试原保存' if uncertain else '保存显示与图表')
        self.display_reload.setEnabled(not busy and not uncertain)
        for widget in (self.theme_picker, self.accent_picker, self.font_picker, self.font_size, *self.chart_pickers.values()):
            widget.setEnabled(not uncertain)

    def _display_saved(self, result, request):
        if self._models_closed:
            return
        # Preserve edits made while the read/save was in flight, but refresh
        # untouched controls so a later save cannot undo another window's work.
        current = self._display_selection()
        edited = {group: {key: value for key, value in values.items()
                          if value != request['selection'][group][key]}
                  for group, values in current.items()}
        confirmed = result.get('result', {}).get('settings')
        if confirmed is None:
            confirmed = {group: {**values, **request['payload']['settings'].get(group, {})}
                         for group, values in request['latest'].items()}
        self._display_request = None
        self._display_save_controls(False)
        self.set_display_preferences({'settings': confirmed, 'epoch': result['epoch'], 'revision': result['revision']})
        self._display_loading = True
        for key, value in edited['appearance'].items():
            widget = {'theme': self.theme_picker, 'accent': self.accent_picker, 'font_family': self.font_picker, 'font_size': self.font_size}[key]
            if key == 'font_size':
                widget.setValue(value)
            else:
                widget.setCurrentIndex(widget.findData(value))
        for key, value in edited['charts'].items():
            self.chart_pickers[key].setCurrentIndex(self.chart_pickers[key].findData(value))
        self._display_loading = False
        selection = self._display_selection()
        self.display_dirty = (any(value != self._display_snapshot[key] for key, value in selection['appearance'].items())
                              or selection['charts'] != self._chart_snapshot)
        self.display_edited(update_dirty=False)
        apply_appearance(QApplication.instance(), self._display_snapshot)
        self.saved(result)

    def _send_display_request(self):
        request = self._display_request
        self._display_save_controls(True, uncertain=request.get('uncertain', False))
        def failed(error):
            if self._models_closed:
                return
            uncertain = request.get('uncertain', False) or error.get('code') in {
                'connection_lost', 'protocol_error', 'response_limit', 'storage_error'}
            if uncertain:
                request['uncertain'] = True
            else:
                self._display_request = None
            self._display_save_controls(False, uncertain=uncertain)
            self.error(error)
            self.appearance_note.setText(
                '保存结果待确认。原选择和请求已保留；请点击“核对并重试原保存”，确认前不能修改或重新读取。'
                if uncertain else '本次未保存，选择已保留。再次保存会重新核对最新设置；同一字段有冲突时不会覆盖。')
        self.bridge.command('settings', request['payload'],
                            lambda result: self._display_saved(result, request), failed,
                            request_id=request['request_id'], epoch=request['epoch'],
                            expected_revision=request['revision'])

    def save_chart_style(self):
        if self._models_closed or self._display_saving or not self.chart_save.isEnabled():
            return
        if self._display_request is not None:
            self._send_display_request()
            return
        selection = self._display_selection()
        baseline = {'appearance': dict(self._display_snapshot), 'charts': dict(self._chart_snapshot)}
        expected_epoch = self.chart_epoch
        changed = {group: {key: value for key, value in values.items() if value != baseline[group][key]}
                   for group, values in selection.items()}
        self._display_save_controls(True)
        self.message.setText('正在核对最新显示设置…')
        def failed(error):
            if self._models_closed:
                return
            self._display_save_controls(False)
            self.error(error)
            self.appearance_note.setText('本页选择已保留；尚未发送保存请求。')
        def loaded(result):
            if self._models_closed:
                return
            if result.get('epoch') != expected_epoch:
                failed({'message': '数据空间已经切换，未保存显示设置。请重新打开设置后核对；当前选择保留。'})
                return
            settings = result.get('settings', {})
            latest = {'appearance': normalize_appearance(settings.get('appearance')),
                      'charts': normalize_chart_preferences(settings.get('charts'))}
            labels = {'theme': '明暗模式', 'accent': '强调色', 'font_family': '字体样式', 'font_size': '字号', **self.chart_labels}
            conflicts = [labels[key] for group, values in changed.items() for key, value in values.items()
                         if latest[group][key] not in (baseline[group][key], value)]
            if conflicts:
                failed({'message': '这些显示设置已在其他入口修改：' + '、'.join(conflicts)
                                   + '。你的选择已保留，未覆盖；请核对后决定是否放弃本页修改并重新读取。'})
                return
            patch = {group: {key: value for key, value in values.items() if value != latest[group][key]}
                     for group, values in changed.items()}
            request = {'selection': selection, 'latest': latest,
                       'payload': {'settings': {group: values for group, values in patch.items() if values}},
                       'epoch': expected_epoch, 'revision': result['revision'], 'request_id': str(uuid.uuid4())}
            if not request['payload']['settings']:
                self._display_saved({'epoch': expected_epoch, 'revision': result['revision'],
                                     'result': {'settings': settings}}, request)
                self.message.setText('本次无需重复写入；读取期间的新选择仍未保存。' if self.display_dirty
                                     else '所选显示设置已经保存，无需重复写入。')
                return
            self._display_request = request
            self._send_display_request()
        self.bridge.query('settings', loaded, failed)

    def _replace_model_choices(self, models, selected):
        self._model_items = list(models)
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItem('跟随本机 Codex 设置（自动选择）', '')
        for item in models:
            label = item['display_name'] + (' · 目录推荐' if item.get('is_default') else '')
            self.model.addItem(label, item['model'])
            self.model.setItemData(self.model.count() - 1,
                item['model'] + ('\n' + item['description'] if item.get('description') else ''), Qt.ItemDataRole.ToolTipRole)
        index = self.model.findData(selected)
        if selected and index < 0:
            self.model.addItem(selected + '（原选择，未验证）', selected)
            index = self.model.count() - 1
        self.model.setCurrentIndex(max(0, index))
        self.model.blockSignals(False)

    def _model_dialog_finished(self, *_):
        if self._owns_codex_connection:
            self.codex_connection.request_stop()
        self._models_closed = True
        self._models_generation += 1
        self._model_debounce.stop()

    def _model_tab_changed(self, index):
        if index == self._provider_tab and self._ai_settings_ready and self._models_for_path != self.executable.text().strip():
            self.refresh_models()

    def _model_path_changed(self, *_):
        self._models_generation += 1
        self._models_for_path = None
        selected = self.model.currentData() or ''
        self._replace_model_choices([], selected)
        self.model_note.setText('Codex 程序位置已改变，需重新读取模型；当前选择保留。')
        if self._ai_settings_ready and self.tabs.currentIndex() == self._provider_tab:
            self._model_debounce.start()

    def refresh_models(self):
        if not self._ai_settings_ready or self._models_closed or self._ai_configuring:
            return
        self._model_debounce.stop()
        if self._models_loading:
            self._models_pending_refresh = True
            return
        self._models_generation += 1
        generation = self._models_generation
        path = self.executable.text().strip()
        self._models_loading = True
        self._models_pending_refresh = False
        self.model_refresh.setEnabled(False)
        self.model_note.setText('正在从本机 Codex 读取可选模型…原选择保留，不会自动切换。')

        def current_response():
            self._models_loading = False
            if self._models_closed or self._ai_configuring:
                return False
            self.model_refresh.setEnabled(True)
            stale = generation != self._models_generation or path != self.executable.text().strip()
            pending = self._models_pending_refresh
            self._models_pending_refresh = False
            if stale:
                # One request per dialog: finish the old process before reading
                # the new path, and never render the old path's late response.
                if pending or self._ai_settings_ready:
                    self.refresh_models()
                return False
            return True

        def loaded(result):
            if not current_response():
                return
            selected = self.model.currentData() or ''
            models = result.get('models', [])
            self._replace_model_choices(models, selected)
            self._models_for_path = path
            missing = selected and not any(item['model'] == selected for item in models)
            if missing:
                self.model_note.setText('已读取模型列表；原选择不在当前列表中，仍然保留。请自行选择是否更换。')
            elif not models:
                self.model_note.setText('Codex 当前没有返回可选模型；原选择保留，可稍后刷新。')
            else:
                self.model_note.setText('已读取 %d 个可选模型。自动选择跟随本机 Codex 设置；目录推荐不会覆盖你的选择。' % len(models))

        def failed(error):
            if not current_response():
                return
            self.model_note.setText('暂时无法读取模型，原选择保留，未切换模型。' + error.get('message', '请检查本机 Codex 后刷新。'))

        self.bridge.query('codex_models', loaded, failed, executable=path)

    def show_codex_mode(self):
        desktop = (self.tabs.currentIndex() == self._provider_tab
                   and self.ai_mode.currentData() == 'desktop_shared')
        self.codex_bridge_note.setVisible(desktop)
        self.codex_bridge_start.setVisible(desktop)

    def launch_codex_desktop(self):
        config = self.current_settings.get('ai', {})
        if config.get('execution_mode') != 'desktop_shared' or not config.get('enabled'):
            self.codex_bridge_note.setText('请先启用 Codex 协助并保存设置，再点击“连接 Codex”。')
            return
        if self.codex_connection is not None:
            self.codex_connection.request_connect()
        else:
            self.codex_bridge_note.setText('请从管理软件主窗口连接 Codex。')

    def show_connection_state(self, value):
        if self._models_closed:
            return
        from .gui_codex_connection import MESSAGES
        state = value.get('state') or ('ready' if value.get('ready') else 'checking')
        message = value.get('message') or MESSAGES.get(state, MESSAGES['error'])
        self.codex_bridge_note.setText('Codex 连接：' + message)
        self.codex_bridge_start.setText('检查连接' if value.get('ready') else '连接 Codex')
        self.codex_bridge_start.setEnabled(not value.get('connect_pending') and state != 'disabled')

    def refresh_codex_connection(self, attempts=0):
        if self._models_closed:
            return
        if self.codex_connection is not None:
            self.show_connection_state(self.codex_connection.snapshot())
            return
        def loaded(value):
            if self._models_closed:
                return
            self.show_connection_state(value)
            if not value.get('ready') and attempts:
                QTimer.singleShot(1500, lambda: self.refresh_codex_connection(attempts-1))
        self.bridge.query('codex_connection', loaded, lambda error: self.show_connection_state({'state': 'error', 'message': error.get('message', '连接状态读取失败。')}))

    def open_codex_project(self):
        project = self.current_settings.get('codex_project') or {}
        try:
            from .codex_links import open_workspace
            open_workspace(project.get('workspace') or project.get('project_path') or '')
        except (OSError, ValueError) as exc:
            self.error({'message': str(exc)})

    def show_codex_project(self, project, *, enabled):
        self.codex_open_project.setEnabled(bool(project and project.get('status') == 'ready'))
        if project and project.get('status') == 'ready':
            path = project.get('workspace') or project.get('project_path') or ''
            self.codex_project_note.setText('已保存的项目：' + project.get('name', 'Codex事务助手') + '\n' + path + '\n连接时会检查项目与业务接口；当前 Codex 状态见下方。' + ('' if enabled else '\nCodex 协助已关闭；此对话项目仍可独立使用。'))
        elif enabled:
            self.codex_project_note.setText('首次连接或发送时会准备固定项目与业务接口；无需反复保存设置。')
        else:
            self.codex_project_note.setText('Codex 协助已关闭。启用并保存设置后，可点击“连接 Codex”或在讨论中发送，准备固定项目与业务接口。')

    def _ai_fields_enabled(self, enabled):
        for widget in (self.ai_enabled, self.executable, self.model, self.timeout, self.ai_mode):
            widget.setEnabled(enabled)
        self.model_refresh.setEnabled(enabled and not self._models_loading)
        self.ai_save.setEnabled(enabled)

    def save_ai(self):
        if not self._ai_settings_ready or not self.ai_save.isEnabled() or self._models_closed:
            return
        config = {"enabled": self.ai_enabled.isChecked(), "timeout_seconds": self.timeout.value(),
                  "executable": self.executable.text().strip(), "model": self.model.currentData() or ''}
        if self.ai_mode.currentData() == 'desktop_shared' or 'execution_mode' in self.current_settings.get('ai', {}):
            config['execution_mode'] = self.ai_mode.currentData()
        self._ai_configuring = True
        self._models_generation += 1
        self._model_debounce.stop()
        self._ai_fields_enabled(False)
        self.message.setObjectName('Hint')
        self.message.setText('正在保存 Codex 协助设置…')
        def saved(result):
            if self._models_closed:
                return
            self._ai_configuring = False
            self._ai_fields_enabled(True)
            if isinstance(self.chart_revision, int) and result.get('epoch') == self.chart_epoch and result.get('revision') == self.chart_revision + 1:
                self.chart_revision = result['revision']
            value = result.get('result', result)
            project = value.get('codex_project')
            self.current_settings['ai'] = dict(config)
            if isinstance(project, dict) and project.get('status') != 'disabled':
                self.current_settings['codex_project'] = project
            self.show_codex_project(self.current_settings.get('codex_project'), enabled=config['enabled'])
            self.saved(result)
            self.message.setObjectName('Hint')
            from .appearance import assistants_visible
            message = '设置已保存。连接状态见下方；可直接连接或发送。' if config['enabled'] else 'Codex 协助已关闭；已创建的对话项目和接口保留。'
            if config['enabled'] and not assistants_visible(self.current_settings):
                message = '设置已保存。各面板的助手入口仍隐藏；勾选上方“在各面板显示助手操作入口”并保存入口显示，即可从今天、项目或复盘发起讨论。'
            self.message.setText(message)
            if self.codex_connection is not None:
                self.codex_connection.configure(self.current_settings)
                self.show_connection_state(self.codex_connection.snapshot())
            elif config.get('execution_mode') == 'desktop_shared' and config['enabled']:
                self.show_connection_state({'state': 'unknown'})
        def failed(error):
            if self._models_closed:
                return
            self._ai_configuring = False
            self._ai_fields_enabled(True)
            if config['enabled']:
                self.codex_project_note.setText('本次设置尚未确认保存。当前填写内容已保留，请检查提示后重试。')
            self.error(error)
        self.bridge.command("configure_codex", {"ai": config}, saved, failed)

    def saved(self, result):
        self.message.setText("已保存。")
        if self.on_changed:
            self.on_changed()

    def edit_rule(self, kind=None):
        entity = None
        if kind is None:
            item = self.rules.currentItem()
            if item is None:
                self.message.setText("请先选择一条规则或定时任务。")
                return
            entity = item.data(Qt.ItemDataRole.UserRole)
            kind = entity["type"]
        dialog = EntityForm(self.bridge, self.capabilities, self, entity=entity, default_type=kind, on_saved=lambda _: self.load_rules())
        dialog.exec()

    def install_module(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择扩展模块定义", "", "模块定义 (*.json)")
        if not path:
            return
        try:
            if Path(path).stat().st_size > 262144:
                raise ValueError("模块定义超过 256 KB，请先检查文件。")
            manifest = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            summary = f"{manifest.get('label', manifest.get('id', '模块'))} · 版本 {manifest.get('version', '?')}\n新增类型 {len(manifest.get('types', []))}，字段 {len(manifest.get('fields', []))}，规则 {len(manifest.get('rules', []))}，流程 {len(manifest.get('workflows', []))}"
            if QMessageBox.question(self, "安装模块", summary + "\n\n安装此模块？") != QMessageBox.StandardButton.Yes:
                return
            self.bridge.command("install_module", {"manifest": manifest}, lambda _: self.module_changed(), self.error)
        except (OSError, ValueError, TypeError) as exc:
            self.error({"message": str(exc)})

    def disable_module(self):
        item = self.modules.currentItem()
        if item is None:
            self.message.setText("请先选择一个模块。")
            return
        module = item.data(Qt.ItemDataRole.UserRole)
        identifier = module.get("id") or module.get("manifest", {}).get("id")
        self.bridge.command("disable_module", {"id": identifier}, lambda _: self.module_changed(), self.error)

    def module_changed(self):
        self.load()
        self.saved({})

    def backup(self):
        self.data_result.show()
        self.data_result.setPlainText("正在建立一致快照并核验资料…")
        self.bridge.command("backup", {}, lambda r: self.data_result.setPlainText("备份结果\n" + readable(r.get("result", r))), self.error)

    def restore(self):
        path = QFileDialog.getExistingDirectory(self, "选择已有备份目录")
        if not path:
            return
        target = QFileDialog.getExistingDirectory(self, "选择存放恢复数据的上级目录")
        if not target:
            return
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "恢复到新目录", "新数据目录名称", text="恢复的数据")
        if not ok or not name.strip():
            return
        if Path(name).name != name or name in (".", ".."):
            self.error({"message": "请只填写新目录名称。"})
            return
        target_dir = str(Path(target) / name)
        self.data_result.show()
        self.data_result.setPlainText("正在核验备份并恢复到新目录…")
        self.bridge.command("restore_backup", {"path": path, "target_dir": target_dir}, lambda r: self.data_result.setPlainText("恢复结果\n" + readable(r.get("result", r)) + f"\n\n恢复的数据位置：{target_dir}\n关闭当前窗口后，可用启动器选择这个数据位置。"), self.error)



def launch_workflow(bridge, capabilities, workflow, parent, on_saved):
    """Render each supported workflow action with its normal typed user form."""
    from .gui_forms import FeedbackDialog
    defaults = workflow.get("defaults", {})
    action = workflow["action"]
    if action == "create":
        dialog = EntityForm(bridge, capabilities, parent, default_type=defaults.get("type", "task"), initial_payload=defaults, workflow=workflow, on_saved=on_saved)
        dialog.heading.setText(workflow.get("label", workflow["id"]))
        dialog.exec()
    elif action == "record_feedback":
        picker = EntityPicker(bridge, parent)
        if picker.exec() == QDialog.DialogCode.Accepted:
            FeedbackDialog(bridge, picker.selected, parent, on_saved, workflow=workflow).exec()
    elif action == "save_review":
        dialog = FormDialog(workflow.get("label", "扩展回顾"), parent)
        form = QFormLayout()
        start, end = QDateEdit(QDate.currentDate().addDays(-6)), QDateEdit(QDate.currentDate())
        for date in (start, end):
            date.setCalendarPopup(True)
            date.setDisplayFormat("yyyy-MM-dd")
        form.addRow("开始日期", start)
        form.addRow("结束日期", end)
        dialog.body_layout.addLayout(form)
        content = QTextEdit(defaults.get("text", ""))
        dialog.body_layout.addWidget(content, 1)
        epoch = bridge.epoch
        def save():
            if not content.toPlainText().strip():
                dialog.error("请先填写回顾内容。")
                return
            dialog.busy()
            payload = {"module_id": workflow["module_id"], "workflow_id": workflow["id"], "input": {"start": start.date().toString("yyyy-MM-dd"), "end": end.date().toString("yyyy-MM-dd"), "text": content.toPlainText().strip()}}
            bridge.command("run_workflow", payload, lambda r: (dialog.accept(), on_saved(r)), dialog.error, epoch=epoch)
        dialog.buttons.accepted.connect(save)
        dialog.exec()



class ReportPanel(QWidget):
    """Paged domain result, with scope and unknown coverage kept visible."""
    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.generation, self.offset, self.next_offset = 0, 0, None
        self.name, self.params = None, {}
        layout = QVBoxLayout(self)
        self.text = QTextBrowser()
        layout.addWidget(self.text, 1)
        row = QHBoxLayout()
        self.count = QLabel()
        row.addWidget(self.count, 1)
        self.previous = QPushButton("上一页")
        self.previous.clicked.connect(self.previous_page)
        self.next = QPushButton("下一页")
        self.next.clicked.connect(self.next_page)
        row.addWidget(self.previous)
        row.addWidget(self.next)
        layout.addLayout(row)

    def set_query(self, name, **params):
        self.name, self.params, self.offset = name, params, 0
        self.load()

    def load(self):
        if not self.name:
            return
        self.generation += 1
        generation = self.generation
        def loaded(result):
            if generation != self.generation:
                return
            visible = {k: v for k, v in result.items() if k not in ("epoch", "revision", "scope_id", "project_id", "next_offset")}
            self.text.setPlainText(readable(visible))
            self.next_offset = result.get("next_offset")
            self.previous.setEnabled(self.offset > 0)
            self.next.setEnabled(self.next_offset is not None)
            coverage = result.get("coverage", {})
            total = result.get("total", coverage.get("total", len(result.get("items", []))))
            self.count.setText(f"共 {total} 项 · 第 {self.offset // 50 + 1} 页")
        self.bridge.query(self.name, loaded, lambda e: self.text.setPlainText(e.get("message", str(e))), **self.params, offset=self.offset, limit=50)

    def previous_page(self):
        self.offset = max(0, self.offset - 50)
        self.load()

    def next_page(self):
        if self.next_offset is not None:
            self.offset = self.next_offset
            self.load()
