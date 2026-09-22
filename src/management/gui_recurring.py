"""Anchored preparation rules: generate candidates without silently scheduling them."""
from __future__ import annotations
from PySide6.QtCore import QDate,Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QWidget,QScrollArea,QFrame,QLabel,QLineEdit,QTextEdit,QSpinBox,QCheckBox,QDateEdit,QPushButton,QListWidget,QListWidgetItem,QMessageBox)
from .gui_calendar import install_calendar


def text_label(text,style='Quiet'):
    label=QLabel(text);label.setTextFormat(Qt.TextFormat.PlainText);label.setWordWrap(True);label.setObjectName(style);return label

def action(text,callback):
    button=QPushButton(text);button.clicked.connect(callback);return button

class RecurringDialog(QDialog):
    def __init__(self,bridge,anchor,parent=None,on_saved=None):
        super().__init__(parent)
        self.bridge,self.anchor,self.on_saved=bridge,anchor,on_saved
        self.epoch=bridge.epoch;self.rule=None;self.dirty=False;self.loading_fields=False;self.saving=False;self.generation=0;self.closed=False;self.allow_close=False;self.offset=0;self.next_offset=None
        repeating=anchor.get('type')=='event' and anchor.get('data',{}).get('recurrence') in {'daily','weekly','monthly'}
        title='每次日程前准备' if repeating else '提前准备（一次）'
        self.setWindowTitle(title);self.resize(730,790);self.setMinimumSize(590,590)
        outer=QVBoxLayout(self);outer.setContentsMargins(24,20,24,18);outer.addWidget(text_label(title,'DialogHeading'));outer.addWidget(text_label('围绕「'+anchor.get('title','当前节点')+'」'+('，每次日程分别生成准备任务。' if repeating else '，只针对这次明确日期生成准备任务，不按周期重复。')))
        explanation='任务先进入相应日期的候选待办，加入日计划后才会出现在每日复盘。停用或修改规则不会删除、覆盖已经生成的任务。'
        outer.addWidget(text_label(explanation))
        history=QHBoxLayout();history.addWidget(text_label('已有准备事项','SectionHeading'),1);self.new_button=action('新建准备事项',self.new_rule);history.addWidget(self.new_button);outer.addLayout(history)
        self.rules=QListWidget();self.rules.setMaximumHeight(118);self.rules.currentItemChanged.connect(lambda item, previous: self.select_rule(item) if item is not None else None);outer.addWidget(self.rules)
        self.more=action('加载更多已有规则',self.more_rules);self.more.hide();outer.addWidget(self.more)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.Shape.NoFrame);self.form_body=QWidget();form=QFormLayout(self.form_body);form.setContentsMargins(2,8,8,8);form.setVerticalSpacing(12);scroll.setWidget(self.form_body);outer.addWidget(scroll,1)
        self.title=QLineEdit();self.title.setPlaceholderText('可留空，使用任务内容的第一行');form.addRow('名称（可选）',self.title)
        self.content=QTextEdit();self.content.setMaximumHeight(100);self.content.setPlaceholderText('例如：完成对应章节的课前练习并整理疑问');form.addRow('任务内容',self.content)
        self.gate=QTextEdit();self.gate.setMaximumHeight(85);self.gate.setPlaceholderText('例如：完成指定题目、核对答案并列出仍不理解的问题');form.addRow('完成条件',self.gate)
        self.days=QSpinBox();self.days.setRange(0,366);self.days.setSuffix(' 天');form.addRow('提前出现',self.days)
        form.addRow('',text_label('0 天表示每次日程当天出现；提前 2 天表示每次日程前两天成为候选待办。' if repeating else '0 天表示这次截止或日程当天出现；提前 2 天表示在这次日期前两天成为候选待办。'))
        self.minutes=QSpinBox();self.minutes.setRange(-1,1440);self.minutes.setValue(-1);self.minutes.setSpecialValueText('暂不确定');self.minutes.setSuffix(' 分钟');form.addRow('预计用时',self.minutes)
        self.enabled=QCheckBox('启用这个准备事项');self.enabled.setChecked(True);form.addRow('',self.enabled)
        self.start_enabled=QCheckBox('限制起始日期');self.start=QDateEdit(QDate.currentDate());self.start.setDisplayFormat('yyyy-MM-dd');self.start.setCalendarPopup(True);install_calendar(self.start);self.start.setEnabled(False)
        start_row=QHBoxLayout();start_row.addWidget(self.start_enabled);start_row.addWidget(self.start);form.addRow('生效范围',start_row)
        self.end_enabled=QCheckBox('限制结束日期');self.end=QDateEdit(QDate.currentDate().addMonths(4));self.end.setDisplayFormat('yyyy-MM-dd');self.end.setCalendarPopup(True);install_calendar(self.end);self.end.setEnabled(False)
        end_row=QHBoxLayout();end_row.addWidget(self.end_enabled);end_row.addWidget(self.end);form.addRow('',end_row)
        self.ack=QCheckBox('确认按当前日期 / 频率重新绑定；已生成任务保留并待核对');self.ack.setChecked(False);self.ack.hide();form.addRow('',self.ack)
        self.note=text_label('正在读取已登记的准备事项…');outer.addWidget(self.note)
        buttons=QHBoxLayout();self.disable=action('停用此规则',self.disable_rule);self.disable.hide();buttons.addWidget(self.disable);buttons.addStretch();self.close_button=action('关闭',self.reject);buttons.addWidget(self.close_button);self.save_button=action('保存准备事项',self.save);self.save_button.setObjectName('Primary');buttons.addWidget(self.save_button);outer.addLayout(buttons)
        for widget in (self.title,self.content,self.gate):widget.textChanged.connect(self.mark_dirty)
        self.days.valueChanged.connect(self.mark_dirty);self.minutes.valueChanged.connect(self.mark_dirty);self.enabled.toggled.connect(self.mark_dirty);self.start.dateChanged.connect(self.mark_dirty);self.end.dateChanged.connect(self.mark_dirty);self.ack.toggled.connect(self.mark_dirty)
        self.start_enabled.toggled.connect(lambda value:(self.start.setEnabled(value),self.mark_dirty()));self.end_enabled.toggled.connect(lambda value:(self.end.setEnabled(value),self.mark_dirty()))
        self.load_rules()

    def mark_dirty(self,*_):
        if not self.loading_fields:
            self.dirty=True
            if self.rule:self.disable.setText("保存并停用")

    def can_discard(self):
        if self.saving:return False
        if not self.dirty or self.allow_close:return True
        return QMessageBox.question(self,'尚未保存','这些修改尚未保存。要放弃这次输入吗？',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)==QMessageBox.StandardButton.Yes

    def load_rules(self,append=False):
        self.generation+=1;generation=self.generation
        def loaded(result):
            if self.closed or generation!=self.generation:return
            self.rules.blockSignals(True)
            if not append:self.rules.clear()
            for entity in result.get('items',[]):
                enabled=entity.get('data',{}).get('enabled',True);item=QListWidgetItem(entity.get('title','准备事项')+('  · 已停用' if not enabled else '  · 启用中')+('  · 待核对' if entity.get('issues') else ''));item.setData(Qt.ItemDataRole.UserRole,entity);self.rules.addItem(item)
                if self.rule and self.rule['id']==entity['id']:
                    self.rules.setCurrentItem(item)
                    if not self.dirty:self.populate(entity)
            self.rules.blockSignals(False);self.next_offset=result.get('next_offset');self.more.setVisible(self.next_offset is not None)
            if not self.rules.count():self.note.setText('还没有准备事项。填写下方内容即可新建。')
            elif not self.rule and not self.dirty:self.note.setText('可以选择一项编辑，也可以填写下方内容新建。')
        def failed(error):
            if not self.closed and generation==self.generation:self.note.setText(error.get('message',str(error)))
        self.bridge.query('recurring_rules',loaded,failed,anchor_id=self.anchor['id'],limit=30,offset=self.offset)

    def more_rules(self):
        if self.next_offset is not None:self.offset=self.next_offset;self.load_rules(append=True)

    def select_rule(self,item):
        entity=item.data(Qt.ItemDataRole.UserRole)
        if self.rule and entity['id']==self.rule['id']:return
        if not self.can_discard():
            self.rules.blockSignals(True)
            for index in range(self.rules.count()):
                candidate=self.rules.item(index)
                if self.rule and candidate.data(Qt.ItemDataRole.UserRole)['id']==self.rule['id']:self.rules.setCurrentItem(candidate)
            if not self.rule:self.rules.clearSelection()
            self.rules.blockSignals(False);return
        self.populate(entity)

    def new_rule(self):
        if not self.can_discard():return
        self.rules.clearSelection();self.populate(None)

    def populate(self,entity):
        self.loading_fields=True;self.rule=entity;data=(entity or {}).get('data',{})
        self.title.setText((entity or {}).get('title',''));self.content.setPlainText(data.get('content',''));self.gate.setPlainText(data.get('completion_gate',''));self.days.setValue(data.get('days_before',0));self.minutes.setValue(data.get('estimated_minutes') if data.get('estimated_minutes') is not None else -1);self.enabled.setChecked(data.get('enabled',True))
        for toggle,editor,key in ((self.start_enabled,self.start,'effective_from'),(self.end_enabled,self.end,'effective_until')):
            toggle.setChecked(bool(data.get(key)))
            if data.get(key):editor.setDate(QDate.fromString(data[key],'yyyy-MM-dd'))
        self.ack.hide();self.ack.setChecked(False);self.disable.setText('停用此规则');self.disable.setVisible(bool(entity and data.get('enabled',True)));self.save_button.setText('保存修改' if entity else '保存准备事项');self.loading_fields=False;self.dirty=False
        note='正在编辑已登记的准备事项。已生成任务会保留原内容。' if entity else '新建准备事项。'
        if entity and entity.get('materialized_count') is not None:note+=' 已生成 '+str(entity['materialized_count'])+' 项任务。'
        if entity and entity.get('issues'):note+=' 待核对：'+'；'.join(str(x.get('message',x)) if isinstance(x,dict) else str(x) for x in entity['issues'])
        self.note.setText(note)

    def payload(self):
        content=self.content.toPlainText().strip();gate=self.gate.toPlainText().strip()
        if not content or not gate:raise ValueError('请填写任务内容和明确的完成条件。')
        first=self.start.date().toString('yyyy-MM-dd') if self.start_enabled.isChecked() else None
        last=self.end.date().toString('yyyy-MM-dd') if self.end_enabled.isChecked() else None
        if first and last and first>last:raise ValueError('结束日期不能早于起始日期。')
        payload={'anchor_id':self.anchor['id'],'title':self.title.text().strip() or content.splitlines()[0][:100],'content':content,'completion_gate':gate,'estimated_minutes':self.minutes.value() if self.minutes.value()>=0 else None,'days_before':self.days.value(),'enabled':self.enabled.isChecked(),'effective_from':first,'effective_until':last}
        if self.rule:payload.update(id=self.rule['id'],version=self.rule['version'])
        if not self.ack.isHidden() and self.ack.isChecked():payload['acknowledge_anchor_change']=True
        return payload

    def set_busy(self,busy):
        self.saving=busy;self.form_body.setEnabled(not busy);self.rules.setEnabled(not busy);self.new_button.setEnabled(not busy);self.more.setEnabled(not busy);self.save_button.setEnabled(not busy);self.disable.setEnabled(not busy);self.close_button.setEnabled(not busy)

    def save(self):
        if self.saving:return
        try:payload=self.payload()
        except ValueError as error:self.note.setText(str(error));return
        self.set_busy(True);self.note.setText('正在保存准备事项…')
        def saved(receipt):
            self.set_busy(False);self.dirty=False;result=receipt.get('result',receipt)
            if result.get('entity'):self.populate(result['entity'])
            self.offset=0;self.load_rules();self.note.setText('已保存。生成的任务先进入候选待办；需要你安排到日计划后才进入复盘。')
            materialization=result.get('materialization') or {}
            if materialization.get('issues'):self.note.setText(self.note.text()+' 部分情况需要核对：'+'；'.join(str(x.get('message',x)) if isinstance(x,dict) else str(x) for x in materialization['issues'][:3]))
            if self.on_saved:self.on_saved(receipt)
        def failed(error):
            self.set_busy(False);self.note.setText(error.get('message',str(error)))
            if error.get('code')=='recurring_anchor_changed':self.ack.show();self.ack.setChecked(False)
        self.bridge.command('set_recurring_rule',payload,saved,failed,epoch=self.epoch)

    def disable_rule(self):
        if self.saving or not self.rule:return
        self.enabled.setChecked(False);self.save()

    def reject(self):
        if self.can_discard():self.closed=True;super().reject()

    def closeEvent(self,event):
        if not self.can_discard():event.ignore();return
        self.closed=True;super().closeEvent(event)
