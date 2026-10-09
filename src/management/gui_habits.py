"""Everyday habit controls explain actual effects instead of exposing rule internals."""
from __future__ import annotations
import datetime as dt
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QGridLayout,QFormLayout,QLabel,QLineEdit,QTextEdit,
    QCheckBox,QSpinBox,QPushButton,QListWidget,QListWidgetItem,QComboBox,QFrame,QWidget,QScrollArea,QMessageBox)
from shiboken6 import isValid
from .gui_recurring import RecurringDialog,text_label,action
from .gui_forms import label_type
from .gui_layout import ActionRow
from .gui_theme import bind_theme


HABIT_CHOICES = {
    'behavior': {
        'title': '日常安排习惯',
        'description': '用文字说明平时希望怎样安排，供你和助手参考。例如“重要的事先做，下午留些空余”。',
        'example': '例如：重要的事先做',
        'policy_example': '例如：每天先处理最重要的一件事，下午留些空余。',
    },
    'capacity': {
        'title': '每天最多安排多久',
        'description': '设置日计划的时长上限，例如 180 分钟就是 3 小时。已填用时的合计超出上限时，需要调整后再保存计划。',
        'example': '例如：每天的计划不超过 3 小时',
    },
    'protected_time': {
        'title': '不安排任务的时段',
        'description': '为睡眠、吃饭等保留时间，例如 23:00–07:00。带具体钟点的计划占用这些时段时，需要调整后再保存。',
        'example': '例如：晚上睡觉时间',
    },
    'warning': {
        'title': '日程与截止日期提醒',
        'description': '设置提前多久在“今天”和“总览”显示日期提醒。例如提前 3 天看到临近日程或截止日期；这是软件内提示，不是定时闹钟。',
        'example': '例如：提前 3 天关注到期事项',
    },
    'temporary': {
        'title': '这段时间的特别安排',
        'description': '记录近期的特殊要求，供你和助手参考。例如“本周少排任务，多留休息”。填好适用日期；不填结束日期就不会自动到期。',
        'example': '例如：这周安排轻松一些',
        'policy_example': '例如：这周只安排少量重点，多留休息与缓冲。请在下面填写适用日期。',
    },
}


class HabitEditor(QDialog):
    def __init__(self,bridge,parent=None,entity=None,on_saved=None,kind='behavior'):
        super().__init__(parent);self.bridge,self.entity,self.on_saved=bridge,entity,on_saved
        self.epoch,self.revision=bridge.epoch,bridge.revision;self.saving=False;self.dirty=False
        data=(entity or {}).get('data',{});self.kind=data.get('rule_kind',kind)
        choice=HABIT_CHOICES.get(self.kind,{})
        kind_title=choice.get('title','安排偏好')
        self.setWindowTitle(('查看与修改 · ' if entity else '添加 · ')+kind_title);self.resize(680,610)
        outer=QVBoxLayout(self);outer.setContentsMargins(24,20,24,18);outer.setSpacing(14);outer.addWidget(text_label(self.windowTitle(),'DialogHeading'))
        self.editor_scroll=QScrollArea();self.editor_scroll.setWidgetResizable(True);self.editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        body=QWidget();layout=QVBoxLayout(body);self.editor_scroll.setWidget(body);outer.addWidget(self.editor_scroll,1)
        effect=choice.get('description') or (entity or {}).get('effect') or '供你和助手安排时参考。'
        layout.addWidget(text_label(effect))
        if entity and entity.get('scope_warning'):layout.addWidget(text_label(entity['scope_warning']))
        form=QFormLayout();form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows);form.setVerticalSpacing(14);layout.addLayout(form)
        self.title=QLineEdit((entity or {}).get('title',''));self.title.setPlaceholderText(choice.get('example','为这项设置起个名字'));form.addRow('这项设置的名称',self.title)
        self.fields={}
        if self.kind in {'behavior','temporary'} or self.kind not in {'warning','capacity','protected_time'}:
            self.policy=QTextEdit(data.get('policy') or data.get('notes') or '');self.policy.setAcceptRichText(False);self.policy.setMinimumHeight(120);self.policy.setPlaceholderText(choice.get('policy_example','写下你希望怎样安排。'));form.addRow('这段时间希望怎样安排' if self.kind=='temporary' else '平时希望怎样安排',self.policy);self.fields['policy']=self.policy
        elif self.kind=='warning':
            for key,title,maximum,suffix in [('days_before','提前多少天',366,' 天'),('calendar_months_before','按月提前（可选）',24,' 个月')]:
                editor=QSpinBox();editor.setRange(0,maximum);editor.setValue(data.get(key) or 0);editor.setSuffix(suffix);form.addRow(title,editor);self.fields[key]=editor
            form.addRow('',text_label('一般只填天数，月数保留 0。两项都填时，取更早的提醒日期，不相加；都填 0 表示当天开始提醒。','Hint'))
        elif self.kind=='capacity':
            editor=QSpinBox();editor.setRange(-1,1440);editor.setSpecialValueText('尚未明确');editor.setSuffix(' 分钟');editor.setValue(data.get('minutes') if data.get('minutes') is not None else -1);form.addRow('每天的计划时长上限',editor);self.fields['minutes']=editor
            form.addRow('',text_label('例如：180 分钟 = 3 小时。只合计已加入日计划的项目；没有加入计划的课表、日程不会自动扣除，未填用时的项目仍待核对。','Hint'))
        else:
            for key,title in [('start','从几点开始留空'),('end','到几点结束')]:
                editor=QLineEdit(data.get(key) or '');editor.setPlaceholderText('HH:mm');form.addRow(title,editor);self.fields[key]=editor
            form.addRow('',text_label('例如 12:00–13:00；23:00–07:00 表示夜里到次日早上。开始和结束相同表示全天留空。只排先后、不填钟点的计划仍需另行核对。','Hint'))
        self.enabled=QCheckBox('启用这项设置');self.enabled.setChecked(data.get('enabled',True) and (entity or {}).get('status') not in {'done','cancelled','draft'});form.addRow(self.enabled)
        self.first=QLineEdit(data.get('effective_from') or '');self.first.setPlaceholderText('留空不限；YYYY-MM-DD')
        self.last=QLineEdit(data.get('effective_until') or '');self.last.setPlaceholderText('结束日期：YYYY-MM-DD' if self.kind=='temporary' else '留空不限；YYYY-MM-DD')
        form.addRow('从哪天开始适用',self.first);form.addRow('适用到哪天',self.last)
        if self.kind=='temporary':form.addRow('',text_label('结束日期当天仍适用，第二天起不再采用这项要求；原记录会保留。','Hint'))
        layout.addStretch();self.note=text_label('');outer.addWidget(self.note)
        row=QHBoxLayout();row.addStretch();self.cancel=action('取消',self.reject);row.addWidget(self.cancel);self.save_button=action('保存设置',self.save);self.save_button.setObjectName('Primary');row.addWidget(self.save_button);outer.addLayout(row)
        self.edit_widgets=[self.title,self.first,self.last,*self.fields.values()]
        for widget in self.edit_widgets:
            signal=widget.valueChanged if isinstance(widget,QSpinBox) else widget.textChanged
            signal.connect(self.changed)
        self.enabled.toggled.connect(self.changed)
        bind_theme(self,self.fit_fields)

    def fit_fields(self):
        from .gui_workflows import fit_input_fields
        fit_input_fields(self)
        scale=max(1,self.fontMetrics().height()/18)
        self.title.setMaximumWidth(round(460*scale))
        for field in (self.first,self.last):
            field.setMaximumWidth(max(round(220*scale),field.fontMetrics().horizontalAdvance(field.placeholderText())+32))
        for field in self.fields.values():
            if isinstance(field,QSpinBox):
                field.setMaximumWidth(max(round(170*scale),field.sizeHint().width()))
            elif isinstance(field,QLineEdit):
                field.setMaximumWidth(round(140*scale))
        self.editor_scroll.widget().updateGeometry()

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
        if not self.title.text().strip():
            self.note.setText('请给这项设置填写一个名称，方便以后查看或修改。');return
        if self.kind in {'behavior','temporary'} and not data.get('policy'):
            self.note.setText('请写下具体的安排要求；示例文字不会自动保存。');return
        if self.kind=='capacity' and self.enabled.isChecked() and data.get('minutes') is None:
            self.note.setText('启用时长上限前，请填写每天最多安排多少分钟；尚未明确时可先取消启用。');return
        if self.kind=='protected_time' and self.enabled.isChecked():
            try:
                start,end=(dt.time.fromisoformat(data.get(key) or '') for key in ('start','end'))
                if any(len(data.get(key) or '')!=5 for key in ('start','end')):raise ValueError()
            except ValueError:
                self.note.setText('请填写有效的 HH:mm 开始和结束时间，例如 23:00 与 07:00。');return
        data['source_text']='用户在“日常习惯”中明确设置。'+(' 原来源：'+str(data.get('source_text'))[:500] if data.get('source_text') else '')
        patch={'title':self.title.text().strip(),'data':data}
        if self.enabled.isChecked():patch['status']='active'
        name='update' if self.entity else 'create'
        payload={'id':self.entity['id'],'version':self.entity['version'],'patch':patch} if self.entity else {'type':'rule',**patch}
        self.saving=True;self.save_button.setEnabled(False);self.cancel.setEnabled(False)
        for widget in [*self.edit_widgets,self.enabled]:widget.setEnabled(False)
        def saved(receipt):
            self.saving=False;self.dirty=False;self.accept()
            if self.on_saved:self.on_saved(receipt)
        def failed(error):
            self.saving=False;self.save_button.setEnabled(True);self.cancel.setEnabled(True);self.note.setText(error.get('message',str(error)) if isinstance(error,dict) else str(error))
            for widget in [*self.edit_widgets,self.enabled]:widget.setEnabled(True)
        self.bridge.command(name,payload,saved,failed,epoch=self.epoch,expected_revision=self.revision)

    def reject(self):
        if self.saving:return
        if self.dirty and QMessageBox.question(self,'尚未保存','放弃这次规则修改？',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:return
        super().reject()


class HabitsPanel(QScrollArea):
    def __init__(self,bridge,reminder,parent=None,on_changed=None):
        super().__init__(parent);self.bridge,self.on_changed=bridge,on_changed;self.generation=0;self.result={};self.dialogs=[];self.dead=False
        self.destroyed.connect(lambda *_:setattr(self,'dead',True));self.setWidgetResizable(True);self.setFrameShape(QFrame.Shape.NoFrame)
        body=QWidget();self.content=QVBoxLayout(body);self.content.setContentsMargins(2,0,10,0);self.content.setSpacing(18);self.setWidget(body)
        self.content.addWidget(text_label('在这里设置提醒、计划时长和休息时间，也可以写下自己的安排习惯。只填写你需要的项目。'))
        self.sections=[];self.section_grid=QGridLayout();self.section_grid.setHorizontalSpacing(30);self.section_grid.setVerticalSpacing(22);self.content.addLayout(self.section_grid)
        self.reminder_box,layout=self.card('什么时候提醒我复盘')
        self.reminder_summary=text_label('正在读取…');layout.addWidget(self.reminder_summary)
        self.reminder_toggle=QPushButton('调整提醒时间');self.reminder_toggle.setCheckable(True);self.reminder_toggle.setProperty('disclosure',True);self.actions(layout,self.reminder_toggle)
        self.reminder=reminder;reminder.setVisible(False);layout.addWidget(reminder);self.reminder_toggle.toggled.connect(self.show_reminders)
        self.prep_box,layout=self.card('课前与截止前准备')
        self.prep_summary=text_label('正在读取…');layout.addWidget(self.prep_summary)
        layout.addWidget(text_label('例如：每次 Tutorial 前一天提醒我完成对应题目。设置后会生成待办，再由你安排进每日计划。'))
        self._assistants_visible=True
        self.codex_preparation=action('请助手建议准备规则',self.discuss_preparation)
        self.prep_toggle=QPushButton('查看与设置准备规则');self.prep_toggle.setCheckable(True);self.prep_toggle.setProperty('disclosure',True)
        self.actions(layout,self.prep_toggle,self.codex_preparation)
        self.prep_details=QWidget();detail=QVBoxLayout(self.prep_details);detail.setContentsMargins(0,8,0,0);layout.addWidget(self.prep_details);self.prep_details.hide()
        self.prep_toggle.toggled.connect(lambda opened:self.toggle_details(self.prep_toggle,self.prep_details,opened,'查看与设置准备规则'))
        self.preparations=QListWidget();self.preparations.setMaximumHeight(115);self.preparations.itemDoubleClicked.connect(self.edit_preparation);detail.addWidget(self.preparations)
        self.prep_edit=action('修改这项准备',self.edit_preparation)
        self.prep_previous=action('上一页',lambda:self.previous_page('preparation'))
        self.prep_more=action('下一页',self.more_preparations);self.actions(detail,self.prep_edit,self.prep_previous,self.prep_more)
        detail.addWidget(text_label('选择日程，填写提前天数和准备内容：'))
        self.anchor=QComboBox();self.anchor.setMinimumContentsLength(12);self.anchor.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.create_preparation=action('填写准备内容',self.new_preparation);self.create_preparation.setEnabled(False);self.actions(detail,self.anchor,self.create_preparation)
        self.anchor.currentIndexChanged.connect(lambda:self.create_preparation.setEnabled(bool(self.anchor.currentData())))
        self.rules_box,layout=self.card('我希望怎样安排一天')
        self.rule_summary=text_label('');layout.addWidget(self.rule_summary)
        self.preference_summary=text_label('');layout.addWidget(self.preference_summary)
        layout.addWidget(text_label('你想设置什么？','SectionHeading'))
        self.discuss_button=action('请助手建议安排偏好',lambda:self.discuss(general=True))
        self.new_kind=QComboBox();self.new_kind.setAccessibleName('新增安排设置类型')
        for kind,choice in HABIT_CHOICES.items():self.new_kind.addItem(choice['title'],kind)
        self.new_rule_button=action('填写这项设置',self.new_habit);self.new_rule_button.setObjectName('Primary');self.actions(layout,self.new_kind,self.new_rule_button)
        self.new_kind_help=text_label('');layout.addWidget(self.new_kind_help)
        self.new_kind.currentIndexChanged.connect(self.describe_new_kind);self.describe_new_kind()
        self.rules_toggle=QPushButton('查看已有设置');self.rules_toggle.setCheckable(True);self.rules_toggle.setProperty('disclosure',True)
        self.actions(layout,self.rules_toggle,self.discuss_button)
        self.rule_details=QWidget();detail=QVBoxLayout(self.rule_details);detail.setContentsMargins(0,8,0,0);layout.addWidget(self.rule_details);self.rule_details.hide()
        self.rules_toggle.toggled.connect(lambda opened:self.toggle_details(self.rules_toggle,self.rule_details,opened,'查看已有设置'))
        self.rules=QListWidget();self.rules.setMaximumHeight(165);self.rules.currentItemChanged.connect(lambda *_:self.describe_rule());self.rules.itemDoubleClicked.connect(self.edit_rule);detail.addWidget(self.rules)
        self.rule_info=text_label('安排计划时参考你的要求。');detail.addWidget(self.rule_info)
        self.edit_button=action('查看原文 / 修改',self.edit_rule)
        self.rule_previous=action('上一页',lambda:self.previous_page('rules'))
        self.rule_more=action('下一页',self.more_rules);self.actions(detail,self.edit_button,self.rule_previous,self.rule_more)
        self.boundary=text_label('');self.content.addWidget(self.boundary);self.note=text_label('');self.content.addWidget(self.note);self.content.addStretch()
        self.rules_offset=self.prep_offset=0;bind_theme(self,self.apply_visual_style);self.refresh()

    def card(self,title):
        box=QFrame();layout=QVBoxLayout(box);layout.setSpacing(12);layout.addWidget(text_label(title,'SectionHeading'));self.sections.append(box);return box,layout

    @staticmethod
    def actions(layout,*widgets):
        row=ActionRow()
        for widget in widgets:row.addWidget(widget)
        layout.addWidget(row)
        return row

    def apply_visual_style(self):
        for section in self.sections:
            section.setObjectName('ContentSection')
            section.layout().setContentsMargins(2,12,2,18)
            section.style().unpolish(section);section.style().polish(section)
        self.reflow_sections()

    def reflow_sections(self):
        if not hasattr(self,'rules_box'):return
        wide=self.viewport().width()>=round(820*max(1,self.fontMetrics().height()/18))
        key=(wide,self.reminder_toggle.isChecked())
        if getattr(self,'_section_layout',None)==key:return
        self._section_layout=key
        for section in self.sections:self.section_grid.removeWidget(section)
        self.section_grid.setColumnStretch(0,1);self.section_grid.setColumnStretch(1,1 if wide else 0)
        if wide and not self.reminder_toggle.isChecked():
            self.section_grid.addWidget(self.reminder_box,0,0,alignment=Qt.AlignmentFlag.AlignTop)
            self.section_grid.addWidget(self.prep_box,0,1,alignment=Qt.AlignmentFlag.AlignTop)
            self.section_grid.addWidget(self.rules_box,1,0,1,2)
        else:
            for index,section in enumerate(self.sections):self.section_grid.addWidget(section,index,0,1,2 if wide else 1)
        self.widget().updateGeometry()

    def resizeEvent(self,event):
        super().resizeEvent(event);self.reflow_sections()

    def describe_new_kind(self,*_):
        description=HABIT_CHOICES[self.new_kind.currentData()]['description']
        self.new_kind_help.setText(description)
        self.new_kind.setAccessibleDescription(description)

    def set_assistants_visible(self,visible):
        self._assistants_visible=bool(visible)
        self.codex_preparation.setVisible(self._assistants_visible)
        self.discuss_button.setVisible(self._assistants_visible)

    def toggle_details(self,button,details,opened,title):
        details.setVisible(opened);button.setText('收起详细设置' if opened else title)

    def show_reminders(self,value=True):
        self.reminder_toggle.setChecked(bool(value));self.reminder.setVisible(bool(value));self.reminder_toggle.setText('收起提醒时间' if value else '调整提醒时间')
        self.prep_box.setVisible(not value);self.rules_box.setVisible(not value)
        self.reflow_sections()

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
        self.rule_summary.setText(f"当前适用：{summary['guidance_rules']} 条安排要求、{summary.get('validation_rules',0)} 条时长或时段限制、{summary['warning_rules']} 条日期提醒。")
        self.preference_summary.setText('下面的设置用于参考、检查计划或显示提醒。计划由你发起；需要自动生成准备待办时，使用上方“课前与截止前准备”。')
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
        e=item.data(Qt.ItemDataRole.UserRole);choice=HABIT_CHOICES.get(e['data'].get('rule_kind'),{})
        text=choice.get('title','安排设置')+'：'+choice.get('description',e['effect'])+('\n'+e['scope_warning'] if e.get('scope_warning') else '')+'\n点击“查看原文 / 修改”可查看完整要求，修改或停用。'
        self.rule_info.setText(text[:260]+('…' if len(text)>260 else ''));self.rule_info.setToolTip(text)

    def keep_dialog(self,dialog):
        self.dialogs.append(dialog);dialog.finished.connect(lambda *_:self.dialogs.remove(dialog) if dialog in self.dialogs else None);dialog.open();return dialog

    def changed(self,receipt=None):
        if self.dead:return
        if self.on_changed:self.on_changed(receipt)
        else:self.refresh()

    def new_habit(self):self.keep_dialog(HabitEditor(self.bridge,self.window(),on_saved=self.changed,kind=self.new_kind.currentData()))

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
        if not getattr(owner,'data_dir',None):
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
