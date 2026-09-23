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
from .appearance import normalize_appearance
from .gui_theme import available_font_families, apply_appearance, resolved_font_family
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QTimeEdit, QSpinBox, QSplitter, QTabWidget, QTableWidget,
    QTextBrowser, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .gui_calendar import install_calendar
from .gui_forms import EntityForm, EntityPicker, FormDialog, FIELD_LABELS, label_status, readable


def codex_mcp_config(data_dir):
    """Compatibility entry point for the managed dialogue workspace configuration."""
    from .codex_workspace import mcp_config
    return mcp_config(data_dir)


class PlanDialog(FormDialog):
    """A dated plan editor with visible candidates and preserved existing blocks."""
    def __init__(self, bridge, parent=None, on_saved=None, date=None):
        super().__init__("手动安排一天", parent, 940)
        self.bridge, self.on_saved = bridge, on_saved
        self.epoch = bridge.epoch
        self.context_generation = self.candidate_generation = 0
        self.context_day = None
        self.plan = None
        self.plan_loading = True
        self.context_ready = False
        self.closed = self.dirty = self.loading_fields = False
        self.finished.connect(lambda *_: setattr(self, "closed", True))
        self.resize(940, 790)
        top = QHBoxLayout()
        self.date = QDateEdit(date or QDate.currentDate())
        install_calendar(self.date)
        self.date.setDisplayFormat("yyyy-MM-dd dddd")
        self.mode = QComboBox()
        for label, key in [("常规安排", "standard"), ("低精力", "low_state"), ("只排先后，不定时", "no_precise_time"), ("休息", "rest")]:
            self.mode.addItem(label, key)
        top.addWidget(QLabel("安排哪一天"));top.addWidget(self.date)
        top.addWidget(QLabel("方式"));top.addWidget(self.mode);top.addStretch()
        self.body_layout.addLayout(top)
        self.context = QLabel("正在读取当天的固定安排…")
        self.context.setTextFormat(Qt.TextFormat.PlainText)
        self.context.setObjectName("ContextCard");self.context.setWordWrap(True)
        self.body_layout.addWidget(self.context)
        self.unknown_toggle = QPushButton("查看时间待确认的安排")
        self.unknown_toggle.setCheckable(True);self.unknown_toggle.hide()
        self.unknowns = QLabel();self.unknowns.setTextFormat(Qt.TextFormat.PlainText);self.unknowns.setWordWrap(True);self.unknowns.hide()
        self.unknown_toggle.toggled.connect(self.unknowns.setVisible)
        self.body_layout.addWidget(self.unknown_toggle);self.body_layout.addWidget(self.unknowns)
        self.body_layout.addWidget(QLabel("从当天候选中选任务 · 按课程 / 项目分类，较早的事项在前"))
        self.candidates = QTreeWidget();self.candidates.setHeaderLabels(["课程 / 项目与任务", "安排或截止日期"])
        self.candidates.setColumnWidth(0, 630);self.candidates.setMinimumHeight(150);self.candidates.setMaximumHeight(230)
        self.candidates.itemDoubleClicked.connect(self.add_candidate)
        self.body_layout.addWidget(self.candidates)
        row = QHBoxLayout()
        self.add_candidate_button = QPushButton("加入勾选任务");self.add_candidate_button.setObjectName("Primary");self.add_candidate_button.clicked.connect(self.add_checked)
        row.addWidget(self.add_candidate_button)
        self.candidate_previous = QPushButton("上一页");self.candidate_previous.clicked.connect(lambda: self.load_candidates(max(0,self.candidate_offset-100)));row.addWidget(self.candidate_previous)
        self.candidate_more = QPushButton("下一页");self.candidate_more.clicked.connect(lambda: self.load_candidates(self.candidate_next));row.addWidget(self.candidate_more)
        row.addStretch();self.candidate_count = QLabel("正在读取…");row.addWidget(self.candidate_count)
        self.body_layout.addLayout(row)
        self.plan_note = QLabel("下面是这一天的计划，可调整顺序和时间。")
        self.plan_note.setWordWrap(True);self.body_layout.addWidget(self.plan_note)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["任务与归属", "开始", "结束", "分钟", "本次完成标准"])
        self.table.horizontalHeader().setStretchLastSection(True)
        for column,width in [(0,290),(1,85),(2,85),(3,75)]:self.table.setColumnWidth(column,width)
        self.table.setMinimumHeight(180);self.body_layout.addWidget(self.table)
        actions = QHBoxLayout()
        add = QPushButton("搜索其他任务…");add.clicked.connect(self.add_block);actions.addWidget(add)
        remove = QPushButton("移除选中安排");remove.clicked.connect(self.remove_block);actions.addWidget(remove)
        for title,step in [("上移",-1),("下移",1)]:
            button=QPushButton(title);button.clicked.connect(lambda _,step=step:self.move_block(step));actions.addWidget(button)
        actions.addStretch();self.body_layout.addLayout(actions)
        self.source = QTextEdit();self.source.setPlaceholderText("安排依据或说明（可留空）");self.source.setMaximumHeight(65);self.source.textChanged.connect(self.mark_dirty);self.body_layout.addWidget(self.source)
        hint = QLabel("时间和分钟数都可留空，只安排先后。已经记录完成情况的原计划事项会保留，复盘仍可继续。")
        hint.setWordWrap(True);hint.setObjectName("Hint");self.body_layout.addWidget(hint)
        self.date.dateChanged.connect(self.load_context)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.buttons.accepted.connect(self.save)
        self.load_context()

    def mark_dirty(self,*_):
        if not self.loading_fields:self.dirty=True

    def mode_changed(self,*_):
        self.mark_dirty();self.load_context()

    def update_save_state(self):
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(self.context_ready and not self.plan_loading)
        self.mode.setEnabled(not self.plan_loading)

    def load_context(self,*_):
        day=self.date.date().toString("yyyy-MM-dd")
        new_day=day!=self.context_day
        if new_day and self.context_day and self.dirty:
            if QMessageBox.question(self,"尚未保存","切换日期会放弃当前尚未保存的安排。继续？",QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:
                self.date.blockSignals(True);self.date.setDate(QDate.fromString(self.context_day,"yyyy-MM-dd"));self.date.blockSignals(False);return
        self.context_generation+=1;generation=self.context_generation
        self.context_ready=False;self.error_label.hide()
        if new_day:
            self.context_day=day;self.plan=None;self.plan_loading=True;self.table.setRowCount(0);self.loading_fields=True;self.source.clear();self.loading_fields=False;self.dirty=False
            self.load_candidates(0)
            self.load_existing_plan(generation,day)
        self.update_save_state()
        def loaded(result):
            if self.closed or generation!=self.context_generation:return
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
            self.context_ready=True;self.update_save_state()
        self.bridge.query("plan_context",loaded,self.error,date=day,mode=self.mode.currentData())

    def load_candidates(self,offset=0):
        if offset is None:return
        self.candidate_generation+=1;generation=self.candidate_generation
        self.candidate_offset=offset;day=self.context_day or self.date.date().toString("yyyy-MM-dd")
        self.candidates.clear();self.add_candidate_button.setEnabled(False)
        def loaded(result):
            if self.closed or generation!=self.candidate_generation or day!=self.context_day:return
            groups=result.get("groups")
            if groups is None:
                grouped={}
                for e in result.get("items",[]):grouped.setdefault(e.get("owner_label") or "未归属课程 / 项目",[]).append(e)
                groups=[{"owner_label":label,"items":items} for label,items in grouped.items()]
            for group in groups:
                parent=QTreeWidgetItem([group.get("owner_label") or "未归属课程 / 项目",""]);self.candidates.addTopLevelItem(parent)
                for entity in group["items"]:
                    item=QTreeWidgetItem([entity.get("title", "任务"),str(entity.get("candidate_date") or entity.get("data",{}).get("scheduled_date") or entity.get("data",{}).get("due_date") or "日期待确认")])
                    item.setData(0,Qt.ItemDataRole.UserRole,entity);item.setCheckState(0,Qt.CheckState.Unchecked);parent.addChild(item)
                parent.setExpanded(True)
            self.candidate_next=result.get("next_offset")
            self.candidate_previous.setVisible(offset>0);self.candidate_more.setVisible(self.candidate_next is not None)
            self.candidate_count.setText(f"共 {result.get('total',len(result.get('items',[])))} 项候选" if result.get('items') else "这一天暂无候选；可搜索其他任务")
            self.add_candidate_button.setEnabled(bool(result.get("items")))
        self.bridge.query("daily_tasks",loaded,self.error,date=day,limit=100,offset=offset)

    def load_existing_plan(self,generation,day):
        def loaded(review):
            if self.closed or generation!=self.context_generation:return
            snapshot=review.get("plan")
            if not snapshot:
                self.plan_loading=False;self.plan_note.setText("尚无计划。勾选上面的任务，加入后保存即可。");self.update_save_state();return
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
                self.plan_note.setText('已载入这一天的现有计划。保存会生成修订版，原计划与反馈保留。');self.update_save_state()
            self.bridge.query('get',plan_loaded,self.error,id=snapshot['id'])
        self.bridge.query('daily_review',loaded,self.error,date=day)

    def add_candidate(self,item,*_):
        entity=item.data(0,Qt.ItemDataRole.UserRole)
        if entity:self.insert_block(entity);item.setCheckState(0,Qt.CheckState.Unchecked)

    def add_checked(self):
        for root in range(self.candidates.topLevelItemCount()):
            parent=self.candidates.topLevelItem(root)
            for index in range(parent.childCount()):
                item=parent.child(index)
                if item.checkState(0)==Qt.CheckState.Checked:self.add_candidate(item)

    def add_block(self):
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
        target=QLabel(title);target.setTextFormat(Qt.TextFormat.PlainText);target.setProperty('target_id',entity['id']);target.setProperty('original_block',dict(original or {}));target.setWordWrap(True);self.table.setCellWidget(row,0,target)
        for column,key in [(1,'start'),(2,'end')]:
            entry=QLineEdit((original or {}).get(key) or '');entry.setPlaceholderText('可留空');entry.textChanged.connect(self.mark_dirty);self.table.setCellWidget(row,column,entry)
        minutes=QSpinBox();minutes.setRange(0,1440);minutes.setSpecialValueText('待确认');minutes.setValue((original or {}).get('minutes') or entity.get('data',{}).get('estimated_minutes') or 0);minutes.valueChanged.connect(self.mark_dirty);self.table.setCellWidget(row,3,minutes)
        gate=QLineEdit((original or {}).get('completion_gate') or entity.get('data',{}).get('completion_gate') or '');gate.setPlaceholderText('做到什么才算完成');gate.textChanged.connect(self.mark_dirty);self.table.setCellWidget(row,4,gate)
        self.table.setRowHeight(row,60);self.mark_dirty()

    def remove_block(self):
        row=self.table.currentRow()
        if row>=0:self.table.removeRow(row);self.mark_dirty()

    def move_block(self,step):
        row=self.table.currentRow();other=row+step
        if row<0 or not 0<=other<self.table.rowCount():return
        def snapshot(index):
            target=self.table.cellWidget(index,0)
            return {'text':target.text(),'id':target.property('target_id'),'original':target.property('original_block'),'start':self.table.cellWidget(index,1).text(),'end':self.table.cellWidget(index,2).text(),'minutes':self.table.cellWidget(index,3).value(),'gate':self.table.cellWidget(index,4).text()}
        left,right=snapshot(row),snapshot(other)
        for index,value in [(row,right),(other,left)]:
            target=self.table.cellWidget(index,0);target.setText(value['text']);target.setProperty('target_id',value['id']);target.setProperty('original_block',value['original'])
            for column,key in [(1,'start'),(2,'end'),(4,'gate')]:self.table.cellWidget(index,column).setText(value[key])
            self.table.cellWidget(index,3).setValue(value['minutes'])
        self.table.setCurrentCell(other,0);self.mark_dirty()

    def save(self):
        if self.plan_loading or not self.context_ready:return
        blocks=[]
        for row in range(self.table.rowCount()):
            target=self.table.cellWidget(row,0);block=dict(target.property('original_block') or {});block['target_id']=target.property('target_id')
            for column,key in [(1,'start'),(2,'end'),(4,'completion_gate')]:
                value=self.table.cellWidget(row,column).text().strip()
                if value:block[key]=value
                else:block.pop(key,None)
            minutes=self.table.cellWidget(row,3).value()
            if minutes:block['minutes']=minutes
            else:block.pop('minutes',None)
            blocks.append(block)
        self.busy()
        def saved(result):
            self.dirty=False
            if self.on_saved:self.on_saved(result)
            self.accept()
        payload={'date':self.context_day,'mode':self.mode.currentData(),'blocks':blocks,'source_text':self.source.toPlainText().strip()}
        if self.plan:payload.update(plan_id=self.plan['id'],plan_version=self.plan['version'])
        self.bridge.command('revise_plan' if self.plan else 'create_plan',payload,saved,self.error,epoch=self.epoch,expected_revision=getattr(self,'context_revision',None))


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
        layout.addWidget(QLabel("这里保留处理进度、失败原因和未采用的候选。取消后，迟到结果不会被采用。"))
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
        actions = QHBoxLayout()
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
        layout.addLayout(actions)
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


class SettingsDialog(QDialog):
    def __init__(self, bridge, capabilities, data_dir, parent=None, on_changed=None):
        super().__init__(parent)
        self.bridge, self.capabilities, self.data_dir, self.on_changed = bridge, capabilities, Path(data_dir), on_changed
        self.current_settings = {}
        self.display_dirty = False
        self._display_loading = False
        self._display_edit_version = 0
        self._display_snapshot = normalize_appearance()
        self.preferences_epoch = self.preferences_revision = None
        self._models_generation = 0
        self._models_loading = self._models_pending_refresh = self._models_closed = False
        self._models_for_path = None
        self._model_items = []
        self._ai_settings_ready = False
        self._ai_configuring = False
        self.setWindowTitle("设置")
        self.resize(790, 650)
        outer = QVBoxLayout(self)
        heading = QLabel("设置")
        heading.setObjectName("DialogHeading")
        outer.addWidget(heading)
        tabs = QTabWidget()
        outer.addWidget(tabs, 1)
        self.tabs = tabs
        reminder = QWidget()
        reminder_layout = QVBoxLayout(reminder)
        intro = QLabel('直接设置你希望什么时候复盘。开启后，本机服务在约定时间提示；保存时间不会重复增加提醒。')
        intro.setWordWrap(True)
        reminder_layout.addWidget(intro)
        reminder_form = QFormLayout()
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
        weekly_row = QHBoxLayout()
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
        reminder_layout.addWidget(save_reminders)
        read_reminders = self.read_reminders = QPushButton('重新读取已保存的时间')
        read_reminders.clicked.connect(self.load_review_times)
        reminder_layout.addWidget(read_reminders)
        hint = QLabel('有每日计划：到复盘页逐项选择完成或未完成。没有每日计划：提示到 Codex 梳理实际情况。电脑关闭期间不会执行提醒。')
        hint.setWordWrap(True)
        hint.setObjectName('Hint')
        reminder_layout.addWidget(hint)
        reminder_layout.addStretch()
        from .gui_habits import HabitsPanel
        self.habits=HabitsPanel(bridge,reminder,self,on_changed=self.saved)
        tabs.addTab(self.habits, '日常习惯')
        display=QWidget()
        display_layout=QVBoxLayout(display)
        appearance_form=QFormLayout()
        self.theme_picker=QComboBox()
        self.theme_picker.addItem('浅色 · 当前风格','light')
        self.theme_picker.addItem('深色 · 灰黑与蓝色','dark')
        self.font_picker=QComboBox()
        self.font_picker.setEditable(False)
        self.font_picker.setMinimumContentsLength(24)
        self.font_picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.font_picker.addItem('系统推荐字体','')
        self._installed_font_families=available_font_families()
        for family in self._installed_font_families:
            self.font_picker.addItem(family,family)
        self.font_size=QSpinBox()
        self.font_size.setRange(11,20)
        self.font_size.setValue(13)
        appearance_form.addRow('界面主题',self.theme_picker)
        appearance_form.addRow('字体样式',self.font_picker)
        appearance_form.addRow('字号',self.font_size)
        display_layout.addLayout(appearance_form)
        self.font_preview=QLabel('字体预览：安排、记录与回顾 · Notes 123')
        self.font_preview.setObjectName('FontPreview')
        self.font_preview.setWordWrap(True)
        display_layout.addWidget(self.font_preview)
        self.appearance_note=QLabel('保存后，已经打开的窗口会立即应用。')
        self.appearance_note.setObjectName('Hint')
        self.appearance_note.setWordWrap(True)
        display_layout.addWidget(self.appearance_note)
        display_layout.addWidget(QLabel('每周回顾的图表形式'))
        self.weekly_chart_style=QComboBox()
        self.weekly_chart_style.addItem('纵向等高柱形图 · 比较每天内部占比','columns')
        self.weekly_chart_style.addItem('横向进度条 · 逐日阅读','rows')
        display_layout.addWidget(self.weekly_chart_style)
        chart_help=QLabel('颜色分别表示完成、未完成、未反馈及原有细分反馈。缺少计划的日期独立标示，不算作失败。')
        chart_help.setWordWrap(True);display_layout.addWidget(chart_help)
        self.chart_save=QPushButton('保存显示偏好')
        self.chart_save.setObjectName('Primary');self.chart_save.setEnabled(False)
        self.chart_save.clicked.connect(self.save_chart_style)
        display_layout.addWidget(self.chart_save)
        self.display_reload=QPushButton('放弃本页修改并重新读取')
        self.display_reload.clicked.connect(self.reload_display_preferences)
        display_layout.addWidget(self.display_reload)
        for combo in (self.theme_picker,self.font_picker,self.weekly_chart_style):
            combo.currentIndexChanged.connect(self.display_edited)
        self.font_size.valueChanged.connect(self.display_edited)
        display_layout.addStretch()
        tabs.addTab(display,'显示')
        from .gui_timetable_settings import TimetableSettingsPage
        self.timetable_settings = TimetableSettingsPage(bridge, self, on_changed=on_changed)
        tabs.addTab(self.timetable_settings, '课表')

        provider = QWidget()
        provider_layout = QVBoxLayout(provider)
        intro = QLabel("先在本机 Codex 完成登录。首次启用并保存时，软件会创建固定的‘Codex事务助手’项目并连接当前数据空间；以后可直接在该项目里新建对话，处理邮件、群消息与临时事项。")
        intro.setWordWrap(True)
        provider_layout.addWidget(intro)
        form = QFormLayout()
        self.ai_enabled = QCheckBox("允许软件使用本机 Codex 辅助处理")
        self.executable = QLineEdit()
        self.executable.setPlaceholderText("留空自动寻找本机 Codex")
        self.model = QComboBox()
        self.model.setEditable(False)
        self.model.addItem('跟随本机 Codex 设置（自动选择）', '')
        self.model.setMinimumContentsLength(24)
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
        form.addRow("Codex 程序位置", self.executable)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model, 1)
        model_row.addWidget(self.model_refresh)
        form.addRow("模型", model_row)
        form.addRow('', self.model_note)
        form.addRow("单次等待上限", self.timeout)
        provider_layout.addLayout(form)
        save = self.ai_save = QPushButton("保存并连接 Codex 项目")
        save.setEnabled(False)
        save.setObjectName("Primary")
        save.clicked.connect(self.save_ai)
        provider_layout.addWidget(save)
        self.codex_project_note = QLabel('尚未验证对话项目连接。启用并保存后自动创建项目、设置接口；无需手动复制配置。')
        self.codex_project_note.setTextFormat(Qt.TextFormat.PlainText)
        self.codex_project_note.setWordWrap(True)
        self.codex_project_note.setObjectName('Hint')
        self.codex_project_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        provider_layout.addWidget(self.codex_project_note)
        connection_help = QLabel('新对话会通过同一业务服务读取与更新数据，保存结果以业务回执为准。关闭上方开关只停止软件内的后台协助，已创建的 Codex 项目与接口仍然保留。')
        connection_help.setWordWrap(True)
        connection_help.setObjectName('Hint')
        provider_layout.addWidget(connection_help)
        advanced_connection = QPushButton('高级：查看接口配置')
        advanced_connection.setCheckable(True)
        provider_layout.addWidget(advanced_connection)
        copy_config = QPushButton('复制当前数据空间的 MCP 配置')
        copy_config.clicked.connect(self.copy_codex_config)
        copy_config.hide()
        advanced_connection.toggled.connect(copy_config.setVisible)
        provider_layout.addWidget(copy_config)
        provider_layout.addStretch()
        self._provider_tab = tabs.addTab(provider, "Codex 协助")
        tabs.currentChanged.connect(self._model_tab_changed)

        rules = QWidget()
        rules_layout = QVBoxLayout(rules)
        note = QLabel("规则决定容量、保护时间和警戒方式。定时任务由独立业务服务运行，关闭界面不影响服务中已配置的任务；计算机关闭期间不执行。")
        note.setWordWrap(True)
        rules_layout.addWidget(note)
        self.rules = QListWidget()
        rules_layout.addWidget(self.rules, 1)
        row = QHBoxLayout()
        for label, kind in (("新建规则", "rule"), ("新建定时任务", "schedule")):
            button = QPushButton(label)
            button.clicked.connect(lambda _, k=kind: self.edit_rule(kind=k))
            row.addWidget(button)
        edit = QPushButton("编辑选中项")
        edit.clicked.connect(lambda: self.edit_rule())
        row.addWidget(edit)
        rules_layout.addLayout(row)
        # Advanced maintenance stays available without becoming daily navigation.

        modules = QWidget()
        modules_layout = QVBoxLayout(modules)
        module_note = QLabel("扩展字段、规则、流程和业务类型在已有入口内呈现。停用后历史数据仍可读取。模块文件只能声明已支持的能力。")
        module_note.setWordWrap(True)
        modules_layout.addWidget(module_note)
        self.modules = QListWidget()
        modules_layout.addWidget(self.modules, 1)
        row = QHBoxLayout()
        install = QPushButton("从文件安装模块")
        install.clicked.connect(self.install_module)
        disable = QPushButton("停用选中模块")
        disable.clicked.connect(self.disable_module)
        row.addWidget(install)
        row.addWidget(disable)
        modules_layout.addLayout(row)
        # Module definitions are not a list of everyday user actions.

        data = QWidget()
        data_layout = QVBoxLayout(data)
        location = QLabel(f"当前数据位置\n{self.data_dir}")
        location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        location.setWordWrap(True)
        data_layout.addWidget(location)
        from .gui_library import open_library
        originals=QPushButton('打开原文件目录')
        originals.clicked.connect(lambda:open_library(self.bridge,self,originals,None,self.error))
        data_layout.addWidget(originals)
        data_note = QLabel("备份包含业务记录和已保存的资料版本。本地文件引用只保存路径，不会复制外部原件。恢复会写入新建的空目录，成功后从该目录启动应用；不会覆盖当前数据。")
        data_note.setWordWrap(True)
        data_layout.addWidget(data_note)
        backup = QPushButton("创建并核验备份")
        backup.clicked.connect(self.backup)
        restore = QPushButton("恢复备份到新目录")
        restore.clicked.connect(self.restore)
        data_layout.addWidget(backup)
        data_layout.addWidget(restore)
        self.data_result = QTextBrowser()
        data_layout.addWidget(self.data_result, 1)
        self.data_result.hide()
        self.data_result.setMaximumHeight(150)
        advanced = QPushButton('高级规则与扩展')
        advanced.setCheckable(True)
        data_layout.addWidget(advanced)
        advanced_tabs = QTabWidget()
        advanced_tabs.addTab(rules, '规则')
        advanced_tabs.addTab(modules, '扩展')
        advanced_tabs.setVisible(False)
        advanced.toggled.connect(advanced_tabs.setVisible)
        data_layout.addWidget(advanced_tabs)
        data_layout.addStretch()
        tabs.addTab(data, '数据与高级')

        self.message = QLabel()
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close.rejected.connect(self.reject)
        outer.addWidget(close)
        self.load()
        self.load_review_times()

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
            if generation != self.preferences_generation:
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
            self.preferences_epoch, self.preferences_revision = result.get("epoch"), result.get("revision")
            self.save_reminders.setEnabled(True)
            self.review_fields_enabled(True)
            self.preferences_saving = False
            self.read_reminders.setEnabled(True)
            self.preference_note.setText("复盘时间已保存。")
            self.saved(result)
        def failed(error):
            self.preferences_saving = False
            self.read_reminders.setEnabled(True)
            self.save_reminders.setEnabled(True)
            self.review_fields_enabled(True)
            self.error(error)
        self.bridge.command("set_review_preferences", payload, saved, failed, epoch=self.preferences_epoch, expected_revision=self.preferences_revision)

    def error(self, error):
        self.message.setObjectName("Error")
        self.message.setText(error.get("message", str(error)))

    def load(self):
        def loaded(result):
            self.current_settings = result.get("settings", {})
            if not self.display_dirty:
                self.set_display_preferences(result)
            config = self.current_settings.get("ai", {})
            self.show_codex_project(self.current_settings.get("codex_project"), enabled=config.get("enabled", False))
            self.ai_enabled.setChecked(config.get("enabled", False))
            self.executable.blockSignals(True)
            self.executable.setText(config.get("executable") or "")
            self.executable.blockSignals(False)
            self._replace_model_choices([], config.get("model") or "")
            self._ai_settings_ready = True
            self.ai_save.setEnabled(True)
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

    def set_display_preferences(self, result):
        settings=result.get('settings',{})
        value=normalize_appearance(settings.get('appearance'))
        self._display_loading=True
        self._display_snapshot=value
        self.chart_epoch,self.chart_revision=result.get('epoch'),result.get('revision')
        self.theme_picker.setCurrentIndex(max(0,self.theme_picker.findData(value['theme'])))
        index=self.font_picker.findData(value['font_family'])
        if index<0:
            self.font_picker.addItem(value['font_family']+'（本机未安装，保留选择）',value['font_family'])
            index=self.font_picker.count()-1
        self.font_picker.setCurrentIndex(index)
        self.font_size.setValue(value['font_size'])
        self.weekly_chart_style.setCurrentIndex(max(0,self.weekly_chart_style.findData(settings.get('charts',{}).get('weekly_style','columns'))))
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
            'font_family':self.font_picker.currentData() or '', 'font_size':self.font_size.value()})
        family=resolved_font_family(value)
        escaped=family.replace('\\','\\\\').replace('"','\\"')
        self.font_preview.setStyleSheet('QLabel#FontPreview {font-family: "'+escaped+'"; font-size: '+str(value['font_size'])+'px;}')
        missing=value['font_family'] and value['font_family'] not in self._installed_font_families
        note='本机未安装原字体，显示时使用系统字体；原选择仍会保留。' if missing else '保存后，已经打开的窗口会立即应用。'
        self.appearance_note.setText(note+(' 本页有尚未保存的修改。' if self.display_dirty else ''))

    def reload_display_preferences(self):
        def loaded(result):
            self.current_settings=result.get('settings',{})
            self.set_display_preferences(result)
        self.bridge.query('settings',loaded,self.error)

    def save_chart_style(self):
        if not self.chart_save.isEnabled():return
        self.chart_save.setEnabled(False)
        self.display_reload.setEnabled(False)
        edit_version=self._display_edit_version
        appearance={'theme':self.theme_picker.currentData(),'font_family':self.font_picker.currentData() or '', 'font_size':self.font_size.value()}
        payload={'settings':{'charts':{'weekly_style':self.weekly_chart_style.currentData()},'appearance':appearance}}
        def saved(result):
            self.chart_epoch,self.chart_revision=result.get('epoch'),result.get('revision')
            self.chart_save.setEnabled(True);self.display_reload.setEnabled(True)
            confirmed=result.get('result',{}).get('settings',{}).get('appearance') or normalize_appearance(appearance,self._display_snapshot)
            self._display_snapshot=normalize_appearance(confirmed)
            apply_appearance(QApplication.instance(),self._display_snapshot)
            self.display_dirty=edit_version!=self._display_edit_version
            self.display_edited(update_dirty=False)
            self.saved(result)
        def failed(error):
            self.chart_save.setEnabled(True);self.display_reload.setEnabled(True);self.error(error)
            self.appearance_note.setText('本页选择已保留，尚未保存。若其他窗口已修改设置，可重新读取后再核对。')
        self.bridge.command('settings',payload,saved,failed,epoch=self.chart_epoch,expected_revision=self.chart_revision)

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

    def show_codex_project(self, project, *, enabled):
        if project and project.get('status') == 'ready':
            path = project.get('workspace') or project.get('project_path') or ''
            self.codex_project_note.setText('上次已连接：' + project.get('name', 'Codex事务助手') + '\n' + path + '\n软件或数据位置改变后，再次保存可检查并修复连接。' + ('' if enabled else '\n软件后台协助已关闭；此对话项目仍可独立使用。'))
        elif enabled:
            self.codex_project_note.setText('尚未完成对话项目连接。请保存一次，自动创建项目并检查 MCP 接口。')
        else:
            self.codex_project_note.setText('软件后台协助已关闭。首次启用并保存时会自动创建固定对话项目与 MCP 连接，并打开空白对话，不会自动发送消息。')

    def _ai_fields_enabled(self, enabled):
        for widget in (self.ai_enabled, self.executable, self.model, self.timeout):
            widget.setEnabled(enabled)
        self.model_refresh.setEnabled(enabled and not self._models_loading)
        self.ai_save.setEnabled(enabled)

    def save_ai(self):
        if not self._ai_settings_ready or not self.ai_save.isEnabled() or self._models_closed:
            return
        config = {"enabled": self.ai_enabled.isChecked(), "timeout_seconds": self.timeout.value(),
                  "executable": self.executable.text().strip(), "model": self.model.currentData() or ''}
        self._ai_configuring = True
        self._models_generation += 1
        self._model_debounce.stop()
        self._ai_fields_enabled(False)
        self.message.setObjectName('Hint')
        self.message.setText('正在创建或检查 Codex 项目与业务接口…' if config['enabled'] else '正在保存协助设置…')
        def saved(result):
            if self._models_closed:
                return
            self._ai_configuring = False
            self._ai_fields_enabled(True)
            value = result.get('result', result)
            project = value.get('codex_project')
            if config['enabled'] and (not isinstance(project, dict) or project.get('status') != 'ready' or not project.get('mcp_verified')):
                failed({'message': '未收到对话项目和 MCP 接口的完成回执，请重新保存以检查连接。'})
                return
            self.current_settings['ai'] = dict(config)
            if project and project.get('status') == 'ready':
                self.current_settings['codex_project'] = project
            self.show_codex_project(self.current_settings.get('codex_project'), enabled=config['enabled'])
            self.saved(result)
            self.message.setObjectName('Hint')
            self.message.setText('设置已保存；事务助手已连接，已请求 Codex 打开项目的空白对话。' if config['enabled'] else '软件后台协助已关闭；已创建的对话项目和接口保留。')
        def failed(error):
            if self._models_closed:
                return
            self._ai_configuring = False
            self._ai_fields_enabled(True)
            if config['enabled']:
                self.codex_project_note.setText('本次对话项目连接未完成，设置尚未确认保存。当前填写内容已保留，可修复后再次保存。')
            self.error(error)
        self.bridge.command("configure_codex", {"ai": config}, saved, failed)

    def saved(self, result):
        self.message.setText("已保存。")
        self.habits.refresh()
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
