"""Course catch-up UI, using the same task and immutable feedback APIs as Codex."""
from __future__ import annotations
from PySide6.QtCore import QDate,Qt
from PySide6.QtWidgets import (QWidget,QDialog,QFrame,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QPushButton,QLineEdit,QTextEdit,QDoubleSpinBox,QSpinBox,QComboBox,QCheckBox,QDateEdit,QDialogButtonBox,QProgressBar,QMessageBox)
from .gui_forms import FormDialog,EntityPicker
from .gui_calendar import install_calendar
from .gui_layout import ActionRow


def label(text,name='Quiet'):
    w=QLabel(text);w.setTextFormat(Qt.TextFormat.PlainText);w.setWordWrap(True);w.setObjectName(name);return w

def button(text,fn):
    w=QPushButton(text);w.clicked.connect(fn);return w

def number(value):
    return '待确认' if value is None else f'{value:g}'

def quantity(initial=None):
    w=QDoubleSpinBox();w.setRange(-1,1000000);w.setDecimals(2);w.setSpecialValueText('待确认');w.setValue(-1 if initial is None else initial);return w

def quantity_value(w):return None if w.value()<0 else w.value()


def run_dialog(dialog):
    # These callers read no Qt fields after exec. Release the transient dialog
    # on its owner thread, instead of retaining every visit under MainWindow.
    try:
        return dialog.exec()
    finally:
        dialog.deleteLater()


class DraftDialog(FormDialog):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.dirty=False;self.saving=False;self.finished_ok=False;self.closed=False
        self.finished.connect(lambda *_:setattr(self,"closed",True));self.destroyed.connect(lambda *_:setattr(self,"closed",True))
    def mark_dirty(self,*_):self.dirty=True
    def reject(self):
        if self.saving:return
        if self.dirty and not self.finished_ok and QMessageBox.question(self,'尚未保存','输入尚未保存，要放弃这次修改吗？',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:return
        super().reject()
    def busy(self):
        self.saving=True;self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(False);super().busy()
    def error(self,error):
        self.saving=False;self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(True);super().error(error)


class RecoveryTaskDialog(DraftDialog):
    def __init__(self,bridge,course,parent=None,on_saved=None,entity=None):
        super().__init__('编辑补课与补欠' if entity else '登记补课与补欠',parent,670)
        self.bridge,self.course,self.on_saved,self.entity=bridge,course,on_saved,entity;self.epoch=bridge.epoch;self.original=None
        data=(entity or {}).get('data',{});self.form=QFormLayout();self.body_layout.addWidget(label('只登记你已确认落下的内容。缺少到课记录或尚无反馈，不会自动算作欠课。'));self.body_layout.addLayout(self.form)
        self.existing=button('选择本课程已有任务',self.choose_existing)
        if not entity:self.form.addRow('复用原任务',self.existing)
        self.selected=label('新建一项实际任务；也可选已有任务，保留原编号和记录。');self.form.addRow('',self.selected)
        self.title=QLineEdit((entity or {}).get('title',''));self.title.setPlaceholderText('例如：补第 2—4 讲，完成对应练习');self.form.addRow('需要补什么',self.title)
        self.gate=QTextEdit(data.get('completion_gate',''));self.gate.setMaximumHeight(110);self.gate.setPlaceholderText('补到什么程度才算完成，例如看完指定内容并做完配套练习');self.form.addRow('完成条件',self.gate)
        if entity and data.get('completion_gate'):self.gate.setReadOnly(True)
        self.unit=QLineEdit(data.get('catchup_unit','节课'));self.form.addRow('计量单位',self.unit)
        self.total=quantity(data.get('catchup_total_quantity'));self.form.addRow('总量（可未知）',self.total)
        self.definition_reason=QLineEdit();self.definition_reason.setPlaceholderText('更正原总量时说明依据，例如实际范围为9节而不是8节');self.form.addRow('范围更正原因',self.definition_reason)
        self.definition_reason.setVisible(bool(entity));self.form.labelForField(self.definition_reason).setVisible(bool(entity))
        self.completed=quantity();self.form.addRow('累计已补（可未知）',self.completed);self.completed.setVisible(not bool(entity))
        if entity:self.form.labelForField(self.completed).hide()
        self.minutes=QSpinBox();self.minutes.setRange(-1,1440);self.minutes.setSpecialValueText('待确认');self.minutes.setValue(data.get('estimated_minutes') if data.get('estimated_minutes') is not None else -1);self.minutes.setSuffix(' 分钟');self.form.addRow('预计用时',self.minutes)
        self.reason=QComboBox();self.reason.addItem('我明确确认需要补','self_reported');self.reason.addItem('原任务有明确未完成反馈','confirmed_incomplete');self.reason.setCurrentIndex(max(0,self.reason.findData(data.get('catchup_reason','self_reported'))));self.form.addRow('登记依据',self.reason)
        self.source=QTextEdit();self.source.setMaximumHeight(85);self.source.setPlaceholderText('例如：我确认第 2—4 讲还没补；目前已经补完第 2 讲。');self.form.addRow('本次说明',self.source)
        self.body_layout.addWidget(label('数量进度只表示已补数量；不自动等同于已经掌握、提交或完成全部条件。'))
        for w in (self.title,self.unit,self.definition_reason):w.textChanged.connect(self.mark_dirty)
        for w in (self.gate,self.source):w.textChanged.connect(self.mark_dirty)
        for w in (self.total,self.completed,self.minutes):w.valueChanged.connect(self.mark_dirty)
        self.reason.currentIndexChanged.connect(self.mark_dirty);self.buttons.accepted.connect(self.save)
    def choose_existing(self):
        picker=EntityPicker(self.bridge,self,allowed_types=['task'])
        if picker.exec()!=QDialog.DialogCode.Accepted:return
        candidate=picker.selected;d=candidate['data']
        if d.get('catchup_enabled') or d.get('task_kind')=='catchup':self.error('这项任务已登记补欠，请在补欠列表中修改范围或记录进度。');return
        self.original=candidate;self.title.setText(self.original['title']);self.gate.setPlainText(d.get('completion_gate',''));self.gate.setReadOnly(bool(d.get('completion_gate')))
        self.minutes.setValue(d.get('estimated_minutes') if d.get('estimated_minutes') is not None else -1)
        self.selected.setText('复用：'+self.original['title']+'。保留原归属与完成条件，不另建重复任务。');self.dirty=True
    def save(self):
        if self.saving:return
        if not self.title.text().strip() or not self.gate.toPlainText().strip() or not self.unit.text().strip() or not self.source.toPlainText().strip():self.error('请填写要补的内容、完成条件、计量单位和本次说明。');return
        total,completed=quantity_value(self.total),quantity_value(self.completed)
        if not self.entity and total is not None and completed is not None and completed>total:self.error('已补数量不能大于总量，请核对本次登记。');return
        p={'course_id':self.course['id'],'title':self.title.text().strip(),'completion_gate':self.gate.toPlainText().strip(),'unit':self.unit.text().strip(),'total_quantity':total,'estimated_minutes':None if self.minutes.value()<0 else self.minutes.value(),'source_text':self.source.toPlainText().strip(),'reason':self.reason.currentData()}
        if self.entity:
            if total != self.entity['data'].get('catchup_total_quantity') and not self.definition_reason.text().strip():self.error('更正原总量时请填写范围更正原因。');return
            p.update(id=self.entity['id'],version=self.entity['version'])
            if self.definition_reason.text().strip():p['correction_reason']=self.definition_reason.text().strip()
        else:p['completed_quantity']=completed
        if self.original:p.update(original_task_id=self.original['id'],version=self.original['version'])
        self.busy()
        def saved(result):
            self.finished_ok=True;self.saving=False;self.accept()
            if self.on_saved:self.on_saved(result)
        self.bridge.command('set_recovery_task',p,saved,self.error,epoch=self.epoch)


class RecoveryProgressDialog(DraftDialog):
    def __init__(self,bridge,task,parent=None,on_saved=None):
        super().__init__('记录补欠进度',parent,610);self.resize(610,440);self.body_layout.setAlignment(Qt.AlignmentFlag.AlignTop);self.body_layout.setSpacing(14);self.bridge,self.task,self.on_saved=bridge,task,on_saved;self.epoch=bridge.epoch;self.revision=bridge.revision;self.loaded=False;self.progress={};self.generation=0
        self.body_layout.addWidget(label(task.get('display_title') or task['title'],'SectionHeading'))
        if task.get('owner_label'):self.body_layout.addWidget(label(task['owner_label'],'StatusPill'))
        self.body_layout.addWidget(label('与实际任务共用同一份记录。填写累计数量，或直接确认整体完成，即可保存。'))
        form=QFormLayout();self.body_layout.addLayout(form)
        self.current=label('正在读取当前进度…');form.addRow('已有进度',self.current)
        quantity_row=QWidget();quantity_layout=QHBoxLayout(quantity_row);quantity_layout.setContentsMargins(0,0,0,0)
        self.completed=quantity();quantity_layout.addWidget(self.completed,1)
        self.add_one=button('＋1',self.increment);self.add_one.setToolTip('在已知累计数量上增加 1；保存后生效。');quantity_layout.addWidget(self.add_one);form.addRow('累计已补',quantity_row)
        self.completion=QComboBox();self.completion.addItem('只更新数量',None);self.completion.addItem('尚未完成',False);self.completion.addItem('确认全部完成',True);form.addRow('任务状态',self.completion)
        self.gate=label(task['data'].get('completion_gate','完成条件待确认'));form.addRow('完成条件',self.gate)
        self.body_layout.addWidget(label('数量达到总量时，不会自动认定全部完成；到课、提交、掌握情况保持原记录。'))
        self.more=button('补充说明 / 更正旧记录',lambda:self.advanced.setVisible(not self.advanced.isVisible()));self.more.setObjectName('TextLink');self.body_layout.addWidget(self.more)
        self.advanced=QWidget();advanced_form=QFormLayout(self.advanced);advanced_form.setContentsMargins(0,0,0,0);self.body_layout.addWidget(self.advanced);self.advanced.hide()
        self.day=QDateEdit(QDate.currentDate());self.day.setDisplayFormat('yyyy-MM-dd');self.day.setCalendarPopup(True);install_calendar(self.day);advanced_form.addRow('反馈日期',self.day)
        self.source=QTextEdit();self.source.setMaximumHeight(75);self.source.setPlaceholderText('可选：这次补了哪一个 Lecture、Tutorial 或练习。');advanced_form.addRow('说明（选填）',self.source)
        self.correction=QCheckBox('更正上一次记录');advanced_form.addRow('',self.correction)
        self.correction_reason=QLineEdit();self.correction_reason.setPlaceholderText('例如：上次将一讲重复计数');self.correction_reason.setEnabled(False);advanced_form.addRow('更正原因',self.correction_reason)
        self.correction.toggled.connect(self.correction_reason.setEnabled)
        self.completed.valueChanged.connect(self.mark_dirty);self.completion.currentIndexChanged.connect(self.mark_dirty);self.source.textChanged.connect(self.mark_dirty);self.correction.toggled.connect(self.mark_dirty);self.correction_reason.textChanged.connect(self.mark_dirty);self.day.dateChanged.connect(self.reload)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存进度');self.buttons.accepted.connect(self.save);self.reload()

    def increment(self):
        if not self.loaded:return
        if self.completed.value()<0:
            self.error('原累计数量未知，请先填写实际累计数量。');return
        self.completed.setValue(self.completed.value()+1)

    def reload(self,*_):
        self.generation+=1;generation=self.generation;self.loaded=False;self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
        def got(result):
            if self.closed or generation!=self.generation:return
            self.epoch=result.get("epoch",self.bridge.epoch);self.revision=result.get("revision",self.bridge.revision)
            items=result.get('items',[])
            if not items:self.error('这项补欠任务暂不可用，请重新读取。');return
            self.task=items[0];self.progress=self.task.get('progress',{});p=self.progress
            self.current.setText('累计 '+number(p.get('completed_quantity'))+' / '+number(p.get('total_quantity'))+' '+p.get('unit',''))
            if not self.dirty:self.completed.blockSignals(True);self.completed.setValue(-1 if p.get('completed_quantity') is None else p['completed_quantity']);self.completed.blockSignals(False)
            self.loaded=True;self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(True)
        def failed(error):
            if not self.closed and generation==self.generation:self.error(error);self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
        self.bridge.query('recovery_summary',got,failed,task_id=self.task['id'],as_of=self.day.date().toString('yyyy-MM-dd'))
    def save(self):
        if self.saving or not self.loaded:return
        value=quantity_value(self.completed);old=self.progress.get('completed_quantity')
        if old is not None and (value is None or value<old) and not self.correction.isChecked():self.advanced.show();self.error('累计数量减少时，请勾选更正并说明原因；不会覆盖原反馈。');return
        reverting=self.completion.currentData() is False and self.progress.get('completion')=='done'
        if reverting and not self.correction.isChecked():self.advanced.show();self.error('把已完成改为未完成时，请选择更正并填写原因。');return
        if self.correction.isChecked() and (not (self.progress.get('latest_feedback_id') or self.progress.get('completion_feedback_id')) or not self.correction_reason.text().strip()):self.error('更正需要已有反馈和明确原因。');return
        confirmed=self.completion.currentData()
        source=self.source.toPlainText().strip()
        if value==old and confirmed is None and not source and not self.correction.isChecked():self.error('先修改累计数量或选择完成情况，再保存。');return
        if not source:
            parts=['用户在补欠进度界面确认累计已补 '+number(value)+' '+self.progress.get('unit','')]
            if confirmed is not None:parts.append('整体完成情况：'+('已完成' if confirmed else '未完成'))
            source='；'.join(parts)+'。'
        p={'task_id':self.task['id'],'version':self.task['version'],'business_date':self.day.date().toString('yyyy-MM-dd'),'completed_quantity':value,'source_text':source}
        if confirmed is not None:p['completion_confirmed']=confirmed
        if self.correction.isChecked():
            p['correction_reason']=self.correction_reason.text().strip()
            if self.progress.get('latest_feedback_id'):p['correction_of']=self.progress['latest_feedback_id']
        self.busy()
        def saved(result):
            self.finished_ok=True;self.saving=False;self.accept()
            if self.on_saved:self.on_saved(result)
        self.bridge.command('record_recovery_progress',p,saved,lambda error:self.error(error) if not self.closed else None,epoch=self.epoch,expected_revision=self.revision)


class RecoveryPanel(QFrame):
    def __init__(self,bridge,course,parent=None,on_saved=None):
        super().__init__(parent);self.bridge,self.course,self.on_saved=bridge,course,on_saved;self.offset=0;self.next_offset=None;self.generation=0;self.dead=False;self.destroyed.connect(lambda *_:setattr(self,'dead',True))
        self.setObjectName('ProgressCard');self.root_layout=QVBoxLayout(self);self.root_layout.setContentsMargins(16,16,16,16)
        head=QHBoxLayout();head.addWidget(label('补课与补欠','SectionHeading'),1);head.addWidget(button('登记补课 / 补欠',self.register));self.root_layout.addLayout(head)
        self.summary=label('记录已确认落下的课或任务，查看已补进度，再按实际精力加入日计划。');self.root_layout.addWidget(self.summary)
        self.rows=QWidget();self.rows_layout=QVBoxLayout(self.rows);self.rows_layout.setContentsMargins(0,0,0,0);self.root_layout.addWidget(self.rows)
        pages=QHBoxLayout();self.previous=button('上一页',lambda:self.page(max(0,self.offset-10)));self.next=button('下一页',lambda:self.page(self.next_offset));pages.addWidget(self.previous);pages.addStretch();pages.addWidget(self.next);self.root_layout.addLayout(pages);self.load()
    def page(self,offset):
        if offset is not None:self.offset=offset;self.load()
    def saved(self,result):
        self.load()
        if self.on_saved:self.on_saved(result)
    def register(self):run_dialog(RecoveryTaskDialog(self.bridge,self.course,self.window(),on_saved=self.on_saved or self.saved))
    def edit(self,task):run_dialog(RecoveryTaskDialog(self.bridge,self.course,self.window(),on_saved=self.on_saved or self.saved,entity=task))
    def record(self,task):run_dialog(RecoveryProgressDialog(self.bridge,task,self.window(),on_saved=self.on_saved or self.saved))
    def open_task(self,task):
        from .gui_workspace import TaskDetailDialog
        parent,bridge,course,saved=self.window(),self.bridge,self.course,self.on_saved or self.saved
        def edit_current(entity):run_dialog(RecoveryTaskDialog(bridge,course,parent,on_saved=saved,entity=entity))
        run_dialog(TaskDetailDialog(bridge,task,parent,on_edit=edit_current,on_saved=saved))
    def load(self):
        self.generation+=1;generation=self.generation
        def got(result):
            if self.dead or generation!=self.generation:return
            from .gui_workspace import clear_layout
            clear_layout(self.rows_layout)
            total=result.get('total',len(result.get('items',[])))
            hidden=result.get('completed_hidden',0)
            message=('还有 '+str(total)+' 项补课或补欠任务。这里与上方实际任务是同一批事项，进度和完成记录同步。') if total else '暂无未完成的补欠任务。' if hidden else '还没有登记补欠。你可以记录落下的课程范围，或复用课程里已有的未完成任务。'
            if hidden:message+=' 已完成的 '+str(hidden)+' 项保留在所属事项的已完成记录中。'
            self.summary.setText(message)
            for task in result.get('items',[]):
                p=task.get('progress',{});row=QFrame();row.setObjectName('TaskRow');v=QVBoxLayout(row)
                # A task name is document content: allow the full name to wrap
                # independently of the controls instead of squeezing a button.
                v.addWidget(label(task.get('display_title') or task['title'],'SectionHeading'))
                top=ActionRow();top.addWidget(button('查看任务',lambda _,e=task:self.open_task(e)));top.addWidget(button('修改范围',lambda _,e=task:self.edit(e)));v.addWidget(top)
                if task.get('owner_label'):v.addWidget(label(task['owner_label'],'StatusPill'))
                if task.get('learning_unit_missing'):v.addWidget(label('课次编号待补充；尚无明确资料，未按日期或教学周推测。'))
                state='已明确完成' if p.get('completion_confirmed') else '原完成记录与当前数量需核对' if p.get('completion')=='done' else '整体尚未明确完成'
                v.addWidget(label('累计已补 '+number(p.get('completed_quantity'))+' / '+number(p.get('total_quantity'))+' '+p.get('unit','')+' · '+state))
                if p.get('issues'):v.addWidget(label('；'.join(p['issues'][:2])))
                if p.get('ratio') is not None:
                    bar=QProgressBar();bar.setRange(0,100);bar.setValue(round(max(0,min(1,p['ratio']))*100));bar.setTextVisible(True);bar.setFormat('%p%（数量进度）');v.addWidget(bar)
                else:v.addWidget(label('数量尚未明确，不显示推测的完成百分比。'))
                actions=ActionRow();actions.addWidget(button('更新进度',lambda _,e=task:self.record(e)));actions.addWidget(button('加入某天计划',lambda _,e=task:self.open_task(e)));v.addWidget(actions);self.rows_layout.addWidget(row)
            self.next_offset=result.get('next_offset');self.previous.setVisible(self.offset>0);self.next.setVisible(self.next_offset is not None)
        self.bridge.query('recovery_summary',got,lambda e:self.summary.setText(e.get('message',str(e))),course_id=self.course['id'],limit=10,offset=self.offset,open_only=True)
