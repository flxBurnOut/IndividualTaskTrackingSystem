"""Everyday habit controls explain actual effects instead of exposing rule internals."""
from __future__ import annotations
import datetime as dt
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QLineEdit,QTextEdit,
    QCheckBox,QSpinBox,QPushButton,QListWidget,QListWidgetItem,QComboBox,QFrame,QWidget,QScrollArea,QMessageBox)
from shiboken6 import isValid
from .gui_recurring import RecurringDialog,text_label,action
from .gui_forms import label_type


class HabitEditor(QDialog):
    def __init__(self,bridge,parent=None,entity=None,on_saved=None):
        super().__init__(parent);self.bridge,self.entity,self.on_saved=bridge,entity,on_saved
        self.epoch,self.revision=bridge.epoch,bridge.revision;self.saving=False;self.dirty=False
        data=(entity or {}).get('data',{});self.kind=data.get('rule_kind','behavior')
        self.setWindowTitle('查看与修改偏好' if entity else '添加安排偏好');self.resize(620,530)
        layout=QVBoxLayout(self);layout.addWidget(text_label(self.windowTitle(),'DialogHeading'))
        effect=(entity or {}).get('effect') or ('提供给 Codex 在你请求安排时参考；不会因此创建定时任务或周期待办。' if self.kind not in {'capacity','warning','protected_time'} else '保存后按这条规则的范围执行。')
        layout.addWidget(text_label(effect))
        if entity and entity.get('scope_warning'):layout.addWidget(text_label(entity['scope_warning']))
        form=QFormLayout();layout.addLayout(form)
        self.title=QLineEdit((entity or {}).get('title',''));form.addRow('名称',self.title)
        self.fields={}
        if self.kind in {'behavior','temporary'} or self.kind not in {'warning','capacity','protected_time'}:
            self.policy=QTextEdit(data.get('policy') or data.get('notes') or '');self.policy.setAcceptRichText(False);self.policy.setMinimumHeight(120);form.addRow('安排时遵循的习惯',self.policy);self.fields['policy']=self.policy
        elif self.kind=='warning':
            for key,title,maximum in [('days_before','提前天数',366),('calendar_months_before','提前日历月数',24)]:
                editor=QSpinBox();editor.setRange(0,maximum);editor.setValue(data.get(key) or 0);form.addRow(title,editor);self.fields[key]=editor
        elif self.kind=='capacity':
            editor=QSpinBox();editor.setRange(-1,1440);editor.setSpecialValueText('尚未明确');editor.setValue(data.get('minutes') if data.get('minutes') is not None else -1);form.addRow('每天可安排的分钟数',editor);self.fields['minutes']=editor
        else:
            for key,title in [('start','保护时段开始'),('end','保护时段结束')]:
                editor=QLineEdit(data.get(key) or '');editor.setPlaceholderText('HH:mm');form.addRow(title,editor);self.fields[key]=editor
        self.enabled=QCheckBox('继续使用这项设置');self.enabled.setChecked(data.get('enabled',True) and (entity or {}).get('status') not in {'done','cancelled','draft'});form.addRow(self.enabled)
        self.first=QLineEdit(data.get('effective_from') or '');self.first.setPlaceholderText('留空表示不限；格式 YYYY-MM-DD')
        self.last=QLineEdit(data.get('effective_until') or '');self.last.setPlaceholderText('留空表示不限；格式 YYYY-MM-DD')
        form.addRow('从哪天开始适用',self.first);form.addRow('适用到哪天',self.last)
        self.note=text_label('');layout.addWidget(self.note);layout.addStretch()
        row=QHBoxLayout();row.addStretch();self.cancel=action('取消',self.reject);row.addWidget(self.cancel);self.save_button=action('保存设置',self.save);self.save_button.setObjectName('Primary');row.addWidget(self.save_button);layout.addLayout(row)
        for widget in [self.title,self.first,self.last,*self.fields.values()]:
            signal=widget.valueChanged if isinstance(widget,QSpinBox) else widget.textChanged
            signal.connect(self.changed)
        self.enabled.toggled.connect(self.changed)

    def changed(self,*_):self.dirty=True

    def save(self):
        if self.saving:return
        data=dict((self.entity or {}).get('data',{}));data.update(rule_kind=self.kind,enabled=self.enabled.isChecked(),effective_from=self.first.text().strip() or None,effective_until=self.last.text().strip() or None)
        for key,w in self.fields.items():
            if isinstance(w,QTextEdit):data[key]=w.toPlainText().strip()
            elif isinstance(w,QSpinBox):data[key]=None if w.value()<0 else w.value()
            else:data[key]=w.text().strip() or None
        try:
            for key in ('effective_from','effective_until'):
                if data[key]:dt.date.fromisoformat(data[key])
            if data['effective_from'] and data['effective_until'] and data['effective_from']>data['effective_until']:raise ValueError('起始日期晚于结束日期')
        except ValueError:
            self.note.setText('请使用 YYYY-MM-DD，且结束日期不得早于开始日期。');return
        if not self.title.text().strip() or self.kind in {'behavior','temporary'} and not data.get('policy'):
            self.note.setText('请填写名称和具体的安排习惯。');return
        data['source_text']='用户在“日常习惯”中明确设置。'+(' 原来源：'+str(data.get('source_text'))[:500] if data.get('source_text') else '')
        patch={'title':self.title.text().strip(),'data':data}
        if self.enabled.isChecked():patch['status']='active'
        name='update' if self.entity else 'create'
        payload={'id':self.entity['id'],'version':self.entity['version'],'patch':patch} if self.entity else {'type':'rule',**patch}
        self.saving=True;self.save_button.setEnabled(False);self.cancel.setEnabled(False)
        def saved(receipt):
            self.saving=False;self.dirty=False;self.accept()
            if self.on_saved:self.on_saved(receipt)
        def failed(error):
            self.saving=False;self.save_button.setEnabled(True);self.cancel.setEnabled(True);self.note.setText(error.get('message',str(error)))
        self.bridge.command(name,payload,saved,failed,epoch=self.epoch,expected_revision=self.revision)

    def reject(self):
        if self.saving:return
        if self.dirty and QMessageBox.question(self,'尚未保存','放弃这次规则修改？',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:return
        super().reject()


class HabitsPanel(QScrollArea):
    def __init__(self,bridge,reminder,parent=None,on_changed=None):
        super().__init__(parent);self.bridge,self.on_changed=bridge,on_changed;self.generation=0;self.result={};self.dialogs=[];self.dead=False
        self.destroyed.connect(lambda *_:setattr(self,'dead',True));self.setWidgetResizable(True);self.setFrameShape(QFrame.Shape.NoFrame)
        body=QWidget();self.content=QVBoxLayout(body);self.content.setSpacing(16);self.setWidget(body)
        self.content.addWidget(text_label('设置一次，之后按你的习惯安排。日常只需要看今天的计划和提醒。'))
        box,layout=self.card('什么时候提醒我复盘')
        self.reminder_summary=text_label('正在读取…');layout.addWidget(self.reminder_summary)
        self.reminder_toggle=QPushButton('调整提醒时间');self.reminder_toggle.setCheckable(True);layout.addWidget(self.reminder_toggle)
        self.reminder=reminder;reminder.setVisible(False);layout.addWidget(reminder);self.reminder_toggle.toggled.connect(self.show_reminders)
        self.prep_box,layout=self.card('课前与截止前准备')
        self.prep_summary=text_label('正在读取…');layout.addWidget(self.prep_summary)
        layout.addWidget(text_label('例如：每次 Tutorial 前一天提醒我完成对应题目。设置后会生成待办，再由你安排进每日计划。'))
        self.codex_preparation=action('设置课前准备',self.discuss_preparation);self.codex_preparation.setObjectName('Primary')
        self.prep_toggle=QPushButton('查看与手动设置');self.prep_toggle.setCheckable(True)
        controls=QHBoxLayout();controls.addWidget(self.codex_preparation,1);controls.addWidget(self.prep_toggle,1);layout.addLayout(controls)
        self.prep_details=QWidget();detail=QVBoxLayout(self.prep_details);detail.setContentsMargins(0,8,0,0);layout.addWidget(self.prep_details);self.prep_details.hide()
        self.prep_toggle.toggled.connect(lambda opened:self.toggle_details(self.prep_toggle,self.prep_details,opened,'查看与手动设置'))
        self.preparations=QListWidget();self.preparations.setMaximumHeight(115);self.preparations.itemDoubleClicked.connect(self.edit_preparation);detail.addWidget(self.preparations)
        self.prep_edit=action('修改这项准备',self.edit_preparation);detail.addWidget(self.prep_edit)
        self.prep_previous=action('上一页',lambda:self.previous_page('preparation'));detail.addWidget(self.prep_previous)
        self.prep_more=action('下一页',self.more_preparations);detail.addWidget(self.prep_more)
        detail.addWidget(text_label('也可以自己选择日程，填写提前天数和准备内容：'))
        row=QHBoxLayout();self.anchor=QComboBox();self.anchor.setMinimumContentsLength(20);self.anchor.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon);row.addWidget(self.anchor,1)
        self.create_preparation=action('填写准备内容',self.new_preparation);self.create_preparation.setEnabled(False);row.addWidget(self.create_preparation);detail.addLayout(row)
        self.anchor.currentIndexChanged.connect(lambda:self.create_preparation.setEnabled(bool(self.anchor.currentData())))
        self.rules_box,layout=self.card('我希望怎样安排一天')
        self.rule_summary=text_label('');layout.addWidget(self.rule_summary)
        self.preference_summary=text_label('');layout.addWidget(self.preference_summary)
        self.discuss_button=action('调整我的安排偏好',lambda:self.discuss(general=True));self.discuss_button.setObjectName('Primary')
        self.rules_toggle=QPushButton('查看已有设置');self.rules_toggle.setCheckable(True)
        controls=QHBoxLayout();controls.addWidget(self.discuss_button,1);controls.addWidget(self.rules_toggle,1);layout.addLayout(controls)
        self.rule_details=QWidget();detail=QVBoxLayout(self.rule_details);detail.setContentsMargins(0,8,0,0);layout.addWidget(self.rule_details);self.rule_details.hide()
        self.rules_toggle.toggled.connect(lambda opened:self.toggle_details(self.rules_toggle,self.rule_details,opened,'查看已有设置'))
        self.rules=QListWidget();self.rules.setMaximumHeight(165);self.rules.currentItemChanged.connect(lambda *_:self.describe_rule());self.rules.itemDoubleClicked.connect(self.edit_rule);detail.addWidget(self.rules)
        self.rule_info=text_label('安排计划时参考你的要求。');detail.addWidget(self.rule_info)
        row=QHBoxLayout();self.edit_button=action('查看原文 / 修改',self.edit_rule);row.addWidget(self.edit_button);row.addWidget(action('自己添加一条偏好',self.new_habit));detail.addLayout(row)
        self.rule_previous=action('上一页',lambda:self.previous_page('rules'));detail.addWidget(self.rule_previous)
        self.rule_more=action('下一页',self.more_rules);detail.addWidget(self.rule_more)
        self.boundary=text_label('');self.content.addWidget(self.boundary);self.note=text_label('');self.content.addWidget(self.note);self.content.addStretch()
        self.rules_offset=self.prep_offset=0;self.refresh()

    def card(self,title):
        box=QFrame();box.setObjectName('ProgressCard');layout=QVBoxLayout(box);layout.setContentsMargins(16,14,16,14);layout.addWidget(text_label(title,'SectionHeading'));self.content.addWidget(box);return box,layout

    def toggle_details(self,button,details,opened,title):
        details.setVisible(opened);button.setText('收起详细设置' if opened else title)

    def show_reminders(self,value=True):
        self.reminder_toggle.setChecked(bool(value));self.reminder.setVisible(bool(value));self.reminder_toggle.setText('收起提醒时间' if value else '调整提醒时间')
        self.prep_box.setVisible(not value);self.rules_box.setVisible(not value)

    def refresh(self,*_):
        self.generation+=1;generation=self.generation
        def loaded(value):
            if self.dead or generation!=self.generation:return
            self.result=value;self.render(value)
        def failed(error):
            if not self.dead and generation==self.generation:self.note.setText(error.get('message',str(error)))
        self.bridge.query('habits_overview',loaded,failed,rules_offset=self.rules_offset,preparation_offset=self.prep_offset)

    def render(self,value):
        self.note.clear();rows=[]
        for key,title in [('daily','每日复盘'),('weekly','每周回顾')]:
            d=value['reminders'][key];when=('每周'+'一二三四五六日'[d['weekday']]+' ' if key=='weekly' else '每天 ')+d['time']
            rows.append(title+'：'+('已启用 · '+when+'\n下一次：'+d['next_label'] if d['enabled'] else '未启用（已保存的时间：'+when+'）'))
            if d.get('last_state')=='failed':rows.append('上次触发失败，请查看今天页的提示。')
        self.reminder_summary.setText('\n'.join(rows))
        summary=value['summary'];enabled=summary['preparation_enabled'];total=summary['preparation_total']
        self.prep_summary.setText((str(total)+' 项配置，'+str(enabled)+' 项启用。下列预览覆盖未来 7 天，随实际课表和教学周变化。') if total else (value.get('gap') or '尚未设置自动准备待办。'))
        self.preparations.clear()
        for r in value['preparations']['items']:
            item=QListWidgetItem(r['title']+' · 提前 '+str(r['data'].get('days_before',0))+' 天\n'+r['next_label']);item.setData(Qt.ItemDataRole.UserRole,r);self.preparations.addItem(item)
        self.preparations.setVisible(bool(self.preparations.count()));self.prep_edit.setVisible(bool(self.preparations.count()))
        self.prep_previous.setVisible(self.prep_offset>0)
        self.prep_more.setVisible(value['preparations']['next_offset'] is not None)
        if self.preparations.count():self.preparations.setCurrentRow(0)
        chosen=self.anchor.currentData();self.anchor.blockSignals(True);self.anchor.clear();self.anchor.addItem('选择近期日程或节点…',None)
        for a in value['anchors']:self.anchor.addItem(a['next_date']+' · '+a['title'],a['id'])
        if chosen is not None:
            index=self.anchor.findData(chosen)
            if index>=0:self.anchor.setCurrentIndex(index)
        self.anchor.blockSignals(False);self.create_preparation.setEnabled(bool(self.anchor.currentData()));self.codex_preparation.setEnabled(True)
        self.rule_summary.setText(f"已保存 {summary['guidance_rules']} 条安排偏好、{summary['warning_rules']} 条提前提醒。")
        self.preference_summary.setText('安排计划时，Codex 会参考你的要求；提前提醒会在首页显示。它们不会自行生成计划或准备待办。')
        attention=sum(bool(r.get('scope_warning')) and r.get('effective',False) for r in value['rules']['items'])
        if attention:self.rule_summary.setText(self.rule_summary.text()+f' · 本页 {attention} 项范围待核对')
        selected=self.rules.currentItem().data(Qt.ItemDataRole.UserRole)['id'] if self.rules.currentItem() else None
        self.rules.clear()
        for e in value['rules']['items']:
            item=QListWidgetItem(e.get('display_title',e['title'])+' · '+e['display_state']);item.setData(Qt.ItemDataRole.UserRole,e);self.rules.addItem(item)
            if e['id']==selected:self.rules.setCurrentItem(item)
        if self.rules.count() and not self.rules.currentItem():self.rules.setCurrentRow(0)
        self.rule_previous.setVisible(self.rules_offset>0)
        self.edit_button.setEnabled(bool(self.rules.count()));self.rule_more.setVisible(value['rules']['next_offset'] is not None)
        self.boundary.setText('固定课表只提供时段；日计划仍由你主动发起。\n'+value['boundaries'][-1])

    def describe_rule(self):
        item=self.rules.currentItem()
        if not item:self.rule_info.clear();return
        e=item.data(Qt.ItemDataRole.UserRole);text=e['effect']+('\n'+e['scope_warning'] if e.get('scope_warning') else '')+'\n点击“查看原文 / 修改”可查看完整要求，修改或停用。'
        self.rule_info.setText(text[:260]+('…' if len(text)>260 else ''));self.rule_info.setToolTip(text)

    def keep_dialog(self,dialog):
        self.dialogs.append(dialog);dialog.finished.connect(lambda *_:self.dialogs.remove(dialog) if dialog in self.dialogs else None);dialog.open();return dialog

    def changed(self,receipt=None):
        if self.dead:return
        if self.on_changed:self.on_changed(receipt)
        else:self.refresh()

    def new_habit(self):self.keep_dialog(HabitEditor(self.bridge,self.window(),on_saved=self.changed))

    def edit_rule(self,*_):
        item=self.rules.currentItem()
        if item:self.keep_dialog(HabitEditor(self.bridge,self.window(),entity=item.data(Qt.ItemDataRole.UserRole),on_saved=self.changed))

    def new_preparation(self):
        if self.anchor.currentData():self.open_preparation(self.anchor.currentData())

    def edit_preparation(self,*_):
        item=self.preparations.currentItem()
        if item:
            rule=item.data(Qt.ItemDataRole.UserRole);self.open_preparation(rule['data']['anchor_id'],rule)

    def open_preparation(self,identifier,rule=None):
        def loaded(value):
            if self.dead:return
            dialog=RecurringDialog(self.bridge,value['entity'],self.window(),self.changed)
            if rule:dialog.populate(rule)
            else:
                candidate=next((a for a in self.result.get('anchors',[]) if a['id']==identifier),{})
                if candidate.get('suggested_days_before') is not None:
                    dialog.loading_fields=True;dialog.days.setValue(candidate['suggested_days_before']);dialog.loading_fields=False;dialog.dirty=False
            self.keep_dialog(dialog)
        self.bridge.query('get',loaded,lambda e:self.note.setText(e.get('message',str(e))) if not self.dead else None,id=identifier)

    def discuss_preparation(self):
        from .gui_assistant import AssistanceDialog
        # One conversational entrance can configure several courses at once.
        # Opening it does not enable a rule or send a message automatically.
        prompt='我想设置课前或截止前的准备待办。请结合我的课程、课表和已保存的要求，帮我一次整理哪些事项需要提前准备、提前几天、做到什么才算完成。不要把警戒规则直接当作已启用的自动准备；先列出具体设置供我核对，再保存。生成的待办由我之后排入每日计划。'
        self.keep_dialog(AssistanceDialog(self.bridge,self.window(),prompt=prompt,on_saved=self.changed,context_entities=[],scope={'kind':'general'},source_ids=[],on_open_settings=self.open_provider_settings))

    def open_provider_settings(self):
        from .gui_workflows import SettingsDialog
        from PySide6.QtWidgets import QApplication
        owner=self.window()
        if not hasattr(owner,'data_dir'):
            self.note.setText('请在软件设置的 Codex 协助页完成连接。');return None
        dialog=SettingsDialog(self.bridge,owner.capabilities,owner.data_dir,QApplication.activeModalWidget() or owner,self.changed)
        dialog.tabs.setCurrentIndex(dialog._provider_tab)
        return self.keep_dialog(dialog)

    def previous_page(self,kind):
        if kind=='rules':self.rules_offset=max(0,self.rules_offset-30)
        else:self.prep_offset=max(0,self.prep_offset-20)
        self.refresh()

    def discuss(self,general=False):
        from .gui_assistant import AssistanceDialog
        item=self.rules.currentItem();entity=item.data(Qt.ItemDataRole.UserRole) if item and not general else None
        prompt='我想调整'+('“'+entity['title']+'”这条习惯。' if entity else '日常安排习惯。')+'请根据我接下来说明的变化提出修改，说明实际影响，核对后再保存。'
        self.keep_dialog(AssistanceDialog(self.bridge,self.window(),prompt=prompt,on_saved=self.changed,context_entities=[entity] if entity else [],scope={'kind':'object','entity_id':entity['id']} if entity else {'kind':'general'},source_ids=[],on_open_settings=self.open_provider_settings))

    def more_rules(self):
        offset=(self.result.get('rules') or {}).get('next_offset')
        if offset is not None:self.rules_offset=offset;self.refresh()

    def more_preparations(self):
        offset=(self.result.get('preparations') or {}).get('next_offset')
        if offset is not None:self.prep_offset=offset;self.refresh()
