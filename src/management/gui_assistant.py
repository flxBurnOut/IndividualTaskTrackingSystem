"""Persistent scoped conversations with one composer and reviewable proposals."""
from __future__ import annotations
import datetime as dt
import html
import re
from PySide6.QtCore import Qt,QTimer,QUrl
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QTextEdit,QTextBrowser,QPushButton,QScrollArea,QWidget,QFrame)
from .gui_forms import readable,label_type,FIELD_LABELS
from .gui_theme import bind_theme
from .gui_sources import SourcePickerDialog,label,button,EXTRACTION_LABELS

class Composer(QTextEdit):
    def __init__(self,on_send,parent=None):
        super().__init__(parent);self.on_send=on_send;self.preedit=False
    def inputMethodEvent(self,event):
        self.preedit=bool(event.preeditString());super().inputMethodEvent(event)
    def keyPressEvent(self,event):
        if event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier and not self.preedit:
            self.on_send();event.accept();return
        super().keyPressEvent(event)

class MessageText(QTextBrowser):
    def __init__(self,text,parent=None):
        super().__init__(parent);self.setFrameShape(QFrame.Shape.NoFrame);self.setOpenLinks(False);self.setOpenExternalLinks(False);self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff);self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff);self.setStyleSheet('QTextBrowser {background:transparent;border:0;padding:0;}');self.setPlainText(text);self.document().documentLayout().documentSizeChanged.connect(self.fit);bind_theme(self,self.refresh_theme)
    def refresh_theme(self):
        self.document().setDefaultFont(self.font());self.fit()
    def fit(self,*_):
        self.setFixedHeight(max(36,min(900,int(self.document().size().height())+8)))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if self.document().size().height()>890 else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    def resizeEvent(self,event):super().resizeEvent(event);self.fit()
    def loadResource(self,type,url):
        if url.scheme() in {'http','https','file'}:return None
        return super().loadResource(type,url)

class AssistanceDialog(QDialog):
    def __init__(self,bridge,parent=None,prompt='',on_saved=None,context_entities=None,intent=None,business_date=None,scope=None,on_open_settings=None,source_ids=None,auto_send=False):
        super().__init__(parent)
        self.bridge,self.on_saved,self.on_open_settings=bridge,on_saved,on_open_settings;self.epoch=bridge.epoch
        context_entities=context_entities or [];self.context_entities=context_entities
        if scope is None:
            if context_entities:
                entity=context_entities[0];scope={'kind':'course' if entity.get('type')=='course' else 'object','entity_id':entity['id']}
            elif business_date or intent=='review_handoff':
                match=re.search(r'\b\d{4}-\d{2}-\d{2}\b',prompt);scope={'kind':'daily_review' if intent=='review_handoff' else 'daily_plan','date':business_date or (match.group(0) if match else dt.date.today().isoformat())}
            else:scope={'kind':'general'}
        self.intent=intent
        self.scope=dict(scope);self.owner_id=scope.get('entity_id');self.conversation=None;self.messages={};self.next_before=None;self.active_job_id=None;self.job_id=None;self.full_job={};self.pending=False;self.mutating=False;self.applying=False;self.generation=0;self.settings_ready=False;self.conversation_ready=False;self.ai_enabled=False;self.auto_send=auto_send;self.auto_sent=False;self.closed=False;self.loading_older=False;self.showing_older=False;self.last_render=None;self.proposal_loading=None;self.dialogs=[]
        self.attachment_dirty=source_ids is not None;self.selected_ids=list(dict.fromkeys(source_ids or []));self.source_meta={};self.attachment_version=0;self.initial_scroll=True;self.render_serial=0
        self.setWindowTitle('与 Codex 讨论');self.resize(840,780);self.setMinimumSize(590,520)
        layout=QVBoxLayout(self);layout.setContentsMargins(24,20,24,18);layout.setSpacing(12)
        header=QHBoxLayout();header.addWidget(label('与 Codex 讨论','DialogHeading'),1)
        scope_title=context_entities[0].get('title','') if context_entities else {'daily_plan':'每日计划','daily_review':'每日复盘','general':'日常讨论'}.get(scope.get('kind'),'当前事项')
        if scope.get('date'):scope_title+=' · '+scope['date']
        self.scope_label=label(scope_title,'Quiet');layout.addLayout(header);layout.addWidget(self.scope_label)
        self.history=QScrollArea();self.history.setWidgetResizable(True);self.history.setFrameShape(QFrame.Shape.NoFrame);self.history_body=QWidget();self.history_layout=QVBoxLayout(self.history_body);self.history_layout.setContentsMargins(4,4,10,8);self.history_layout.setSpacing(18);self.history.setWidget(self.history_body);layout.addWidget(self.history,1)
        self.status=label('正在读取这次讨论…','Quiet');layout.addWidget(self.status)
        self.connect_button=button('连接 Codex',self.open_settings);self.connect_button.setObjectName('Primary');self.connect_button.hide();layout.addWidget(self.connect_button,alignment=Qt.AlignmentFlag.AlignLeft)
        composer=QFrame();composer.setObjectName('ChatComposer');composer_layout=QVBoxLayout(composer);composer_layout.setContentsMargins(12,10,12,10)
        self.chips=QWidget();self.chip_layout=QHBoxLayout(self.chips);self.chip_layout.setContentsMargins(0,0,0,0);self.chip_scroll=QScrollArea();self.chip_scroll.setWidgetResizable(True);self.chip_scroll.setFrameShape(QFrame.Shape.NoFrame);self.chip_scroll.setWidget(self.chips);self.chip_scroll.setFixedHeight(51);composer_layout.addWidget(self.chip_scroll)
        self.prompt=Composer(self.start_job);self.prompt.setFrameShape(QFrame.Shape.NoFrame);self.prompt.setStyleSheet('QTextEdit {border:0;background:transparent;}');self.prompt.setFixedHeight(80);self.prompt.setPlaceholderText('说说你想安排什么，或补充新的实际情况…');self.prompt.setPlainText(prompt);composer_layout.addWidget(self.prompt)
        row=QHBoxLayout();self.attach=button('＋ 附件',self.choose_sources);row.addWidget(self.attach);row.addWidget(label('Enter 发送 · Shift + Enter 换行','Quiet'),1);self.send=button('发送',self.send_or_stop);self.send.setObjectName('Primary');self.send.setMinimumWidth(86);row.addWidget(self.send);composer_layout.addLayout(row);layout.addWidget(composer)
        self.apply=QPushButton('核对后保存这些变更');self.apply.setObjectName('Primary');self.apply.clicked.connect(self.apply_result);self.apply.hide()
        self.timer=QTimer(self);self.timer.setInterval(1200);self.timer.timeout.connect(self.poll);self.timer.start();self.finished.connect(self.finished_dialog);self.refresh_chips();self.load_settings();self.poll();self.load_source_titles()

    def finished_dialog(self, *_):
        self.closed=True;self.timer.stop()

    def load_source_titles(self):
        for identifier in self.selected_ids:
            if identifier in self.source_meta:continue
            def loaded(result, i=identifier):
                if self.closed:return
                entity=result.get('entity') or {}
                if entity.get('id')==i:self.source_meta[i]=entity;self.refresh_chips()
            self.bridge.query('get',loaded,lambda _:None,id=identifier)

    def load_settings(self):
        def loaded(result):
            self.settings_ready=True;self.ai_enabled=bool(result.get('settings',{}).get('ai',{}).get('enabled'));self.update_controls();self.maybe_auto_send()
        def failed(error):self.settings_ready=True;self.error(error);self.update_controls()
        self.bridge.query('settings',loaded,failed)

    def open_settings(self):
        if self.on_open_settings:
            result=self.on_open_settings()
            if hasattr(result,'finished'):result.finished.connect(lambda _:self.load_settings())
        QTimer.singleShot(300,self.load_settings)

    def maybe_auto_send(self):
        if self.auto_send and not self.auto_sent and self.settings_ready and self.conversation_ready and self.ai_enabled and not self.active_job_id:
            self.auto_sent=True;self.start_job()

    def refresh_chips(self):
        while self.chip_layout.count():
            item=self.chip_layout.takeAt(0)
            if item.widget():item.widget().deleteLater()
        for identifier in self.selected_ids:
            source=self.source_meta.get(identifier,{});title=source.get('title','资料 '+identifier[:8]);chip=button((title[:20]+'…' if len(title)>20 else title)+' ×',lambda _,i=identifier:self.remove_source(i));chip.setToolTip(title+'\n点击从这次讨论移除');self.chip_layout.addWidget(chip)
        self.chip_layout.addStretch();self.chip_scroll.setVisible(bool(self.selected_ids))

    def remove_source(self,identifier):
        if self.mutating:return
        self.selected_ids=[x for x in self.selected_ids if x!=identifier];self.attachment_dirty=True;self.attachment_version+=1;self.refresh_chips()

    def choose_sources(self):
        if self.mutating:return
        selected=[dict(self.source_meta.get(i,{}),id=i) for i in self.selected_ids]
        dialog=SourcePickerDialog(self.bridge,self.owner_id,self,selected);self.dialogs.append(dialog)
        def accepted():
            self.selected_ids=list(dialog.selected);self.source_meta.update(dialog.selected);self.attachment_dirty=True;self.attachment_version+=1;self.refresh_chips()
        dialog.accepted.connect(accepted);dialog.open()

    def update_controls(self):
        ready=self.settings_ready and self.conversation_ready
        self.send.setText('停止' if self.active_job_id else '发送');self.send.setEnabled(ready and not self.mutating and not self.applying and (bool(self.active_job_id) or self.ai_enabled))
        self.attach.setEnabled(not self.mutating);self.prompt.setReadOnly(self.mutating)
        self.connect_button.setVisible(self.settings_ready and not self.ai_enabled and not self.active_job_id)
        if self.settings_ready and not self.ai_enabled and not self.active_job_id:self.status.setText('连接 Codex 后，就可以在这里讨论。资料和历史记录仍保存在本机。')
        elif self.active_job_id:self.status.setText('Codex 正在处理。关闭窗口后仍会继续，下次打开可查看结果。')
        elif ready and not self.applying and not self.mutating:self.status.setText('讨论会自动保存；涉及实际记录的修改，需要核对后确认。')

    def send_or_stop(self):
        if self.active_job_id:self.cancel_job()
        else:self.start_job()

    def start_job(self):
        if self.mutating or self.applying or self.active_job_id or not self.settings_ready or not self.conversation_ready:return
        if not self.ai_enabled:self.update_controls();return
        text=self.prompt.toPlainText().strip()
        if not text:self.error({'message':'先写下需要讨论的内容。'});return
        if len(self.selected_ids)>12:self.error({'message':'最多附带 12 份资料，请通过附件选择移除一些后再发送。'});return
        payload={'scope':self.scope,'text':text}
        if self.intent=='course_notes':payload['skill_id']='course-notes'
        if self.attachment_dirty:payload['source_ids']=list(self.selected_ids)
        self.mutating=True;self.generation+=1;self.full_job={};self.proposal_loading=None;self.apply.setEnabled(False);self.update_controls();version=self.attachment_version
        def saved(receipt):
            if payload.get('skill_id') == 'course-notes':self.intent=None
            self.mutating=False;self.generation+=1;self.initial_scroll=True;self.showing_older=False;self.loading_older=False;self.messages={};result=receipt.get('result',receipt);self.conversation=result.get('conversation') or self.conversation
            self.active_job_id=(result.get('job') or {}).get('id') or (self.conversation or {}).get('active_job_id')
            if self.prompt.toPlainText().strip()==text:self.prompt.clear()
            if version==self.attachment_version:self.attachment_dirty=False
            message=result.get('user_message')
            if message:self.messages[message['id']]=message
            self.update_controls();self.render_history();self.poll(force=True)
        self.bridge.command('send_message',payload,saved,self.error,epoch=self.epoch)

    def return_latest(self):
        self.generation+=1;self.messages={};self.showing_older=False;self.loading_older=False;self.next_before=None;self.initial_scroll=True;self.poll(force=True)

    def poll(self,force=False,before=None):
        if self.closed or (self.pending and not force):return
        self.pending=True;generation=self.generation;params={'scope':self.scope,'limit':50}
        if before is not None:params['before']=before
        def loaded(result):
            self.pending=False
            if self.closed or generation!=self.generation:return
            self.conversation_ready=True
            # Bound the rendered window; older pages remain reachable without retaining every reply.
            if before is not None or not self.showing_older:
                for message in result.get('messages',[]):self.messages[message['id']]=message
                ordered=sorted(self.messages.values(),key=lambda m:m.get('seq',0))
                kept=ordered[:200] if before is not None else ordered[-200:]
                self.messages={m['id']:m for m in kept}
            if before is not None or not self.loading_older:self.next_before=(min((m.get('seq',0) for m in self.messages.values()),default=result.get('next_before')) if result.get('has_more') else None)
            if before is not None:self.loading_older=True;self.showing_older=True
            if before is None:
                self.conversation=result.get('conversation');conversation=self.conversation or {};self.active_job_id=conversation.get('active_job_id');current=conversation.get('current_proposal_job_id')
                for source in conversation.get('sources',[]):self.source_meta[source['id']]=source
                if not self.attachment_dirty:self.selected_ids=list(conversation.get('source_ids',[]));self.refresh_chips();self.load_source_titles()
                if current!=self.job_id:self.job_id=current;self.full_job={};self.proposal_loading=None
                if current and not self.full_job and self.proposal_loading!=current:self.load_proposal(current)
            self.update_controls();self.render_history();self.maybe_auto_send()
        def failed(error):
            self.pending=False
            if generation==self.generation:self.error(error)
        self.bridge.query('conversation',loaded,failed,**params)

    def load_proposal(self,identifier):
        self.proposal_loading=identifier;generation=self.generation
        def loaded(result):
            if self.closed or generation!=self.generation or identifier!=self.job_id:return
            self.proposal_loading=None;self.full_job=result.get('job',{});self.render_history(force=True)
        def failed(error):
            if identifier==self.job_id:self.proposal_loading=None;self.error(error)
        self.bridge.query('job',loaded,failed,id=identifier)

    def proposal_text(self):
        result=self.full_job.get('result') or {};lines=[]
        visible_reply='\n'.join(m.get('text','') for m in self.messages.values() if m.get('job_id')==self.job_id and m.get('role')=='assistant')
        unknowns=[str(x) for x in result.get('unknowns',[]) if str(x) not in visible_reply]
        if unknowns:lines+=['仍需确认']+['• '+x for x in unknowns]+['']
        for action in result.get('actions',[]):
            payload=action.get('payload') or {};command=action.get('command');title=payload.get('title') or ''
            names={'apply_timetable':'保存每周课表框架','create':'新建'+label_type(payload.get('type')),'update':'更新记录','record_feedback':'记录明确反馈','create_plan':'保存每日计划','save_review':'保存回顾','set_recurring_rule':'固定准备事项','set_recovery_task':'登记补课或补欠','record_recovery_progress':'记录补欠进度'}
            lines.append(names.get(command,'保存变更')+(' · '+title if title else ''))
            if action.get('reason'):lines.append(str(action['reason']))
            if command=='apply_timetable':
                lines.append('学期：'+str(payload.get('semester_start','待确认'))+' 至 '+str(payload.get('semester_end','待确认'))+' · '+str(payload.get('timezone','待确认')))
                if payload.get('week_numbering') == 'teaching':lines.append('教学周跳过 Recess week；假期不排课。')
                if payload.get('recess_weeks'):lines.append('Recess week 起始：'+', '.join(payload['recess_weeks']))
                for row in payload.get('rows',[]):
                    weekday = row.get('weekday')
                    name = '周'+'一二三四五六日'[weekday] if type(weekday) is int and 0 <= weekday < 7 else '星期待确认'
                    weeks = row.get('teaching_weeks')
                    lines.append(('停用 · ' if row.get('enabled') is False else '')+name+' '+str(row.get('start','?'))+'–'+str(row.get('end','?'))+' '+str(row.get('title',''))+' · '+('每周' if weeks is None else '第 '+', '.join(map(str,weeks))+' 周')+(' · '+str(row['location']) if row.get('location') else ''))
                lines.append('依据：'+str(payload.get('source_text') or '待补充'))
                lines.append('确认后成为固定时段；每日任务和补课需另外安排。')
            elif command=='create_plan':
                lines.append('日期：'+str(payload.get('date','待确认')))
                for block in payload.get('blocks',[]):lines.append(readable(block))
            elif command=='save_review':lines.append(str(payload.get('content','')))
            elif command=='set_recovery_task':
                total=payload.get('total_quantity');done=payload.get('completed_quantity')
                lines.append('完成条件：'+str(payload.get('completion_gate') or '待明确'))
                lines.append('计量单位：'+str(payload.get('unit') or '待明确'))
                lines.append('总量：'+('待确认' if total is None else str(total)))
                if 'completed_quantity' in payload:lines.append('累计已补：'+('待确认' if done is None else str(done)))
                if payload.get('original_task_id'):lines.append('复用原任务，保留原记录，不创建第二项任务。')
                lines.append('依据：'+str(payload.get('source_text') or '待补充'))
                lines.append('登记后仍需明确加入日计划，数量进度不代表已经掌握。')
            elif command=='record_recovery_progress':
                lines.append('反馈日期：'+str(payload.get('business_date') or '待确认'))
                if 'completed_quantity' in payload:lines.append('累计已补：'+('待确认' if payload['completed_quantity'] is None else str(payload['completed_quantity'])))
                if 'completion_confirmed' in payload:lines.append('整体完成：'+('明确完成全部条件' if payload['completion_confirmed'] is True else '明确尚未完成'))
                else:lines.append('仅记录本次明确反馈，不推断整体完成。')
                lines.append('实际情况：'+str(payload.get('source_text') or '待补充'))
                if payload.get('correction_reason'):lines.append('更正原因：'+str(payload['correction_reason']))
            elif command=='set_recurring_rule':
                editing=bool(payload.get('id'))
                def display(key, missing='待补充', format_value=str):
                    if key not in payload and editing:return '保持原设置'
                    value=payload.get(key)
                    return missing if value is None else format_value(value)
                if not title:lines.append('名称：'+('保持原名称' if editing else '待补充'))
                lines.append('提前出现：'+display('days_before','待确认',lambda v:'安排当天' if v==0 else str(v)+' 天'))
                lines.append('任务内容：'+display('content'))
                lines.append('完成条件：'+display('completion_gate'))
                lines.append('预计用时：'+display('estimated_minutes','未知',lambda v:str(v)+' 分钟'))
                lines.append('生效起始：'+display('effective_from','不额外限制'))
                lines.append('生效结束：'+display('effective_until','不额外限制'))
                enabled=display('enabled','启用' if not editing else '待确认',lambda v:'启用' if v is True else '停用' if v is False else '待确认')
                lines.append('启用状态：'+enabled)
                lines.append('来源：'+display('source_text','待补充'))
                if payload.get('acknowledge_anchor_change') is True:lines.append('重新绑定当前日期 / 频率；已生成的任务保留并待核对。')
                if payload.get('materialize_date'):lines.append('本次生成日期：'+str(payload['materialize_date']))
                lines.append('准备任务先进入对应日期的候选待办，加入日计划后才进入复盘。')
            elif payload.get('data') or payload.get('patch') or payload.get('dimensions'):lines.append(readable(payload.get('data') or payload.get('patch') or payload.get('dimensions')))
            else:lines.append(readable({k:v for k,v in payload.items() if k not in {'id','version','target_id'}}))
            lines.append('')
        return '\n'.join(lines)

    def render_history(self,force=False):
        messages=sorted(self.messages.values(),key=lambda x:x.get('seq',0));signature=repr((messages,self.active_job_id,self.job_id,self.full_job,self.next_before,self.showing_older))
        if not force and signature==self.last_render:return
        self.last_render=signature;self.render_serial+=1;serial=self.render_serial;bar=self.history.verticalScrollBar();old=bar.value();bottom=bar.maximum()-old<60
        self.apply.setParent(self);self.apply.hide();self.apply.setEnabled(False)
        while self.history_layout.count():
            item=self.history_layout.takeAt(0)
            if item.widget():item.widget().deleteLater()
        if self.showing_older:self.history_layout.addWidget(button('回到最新回复',self.return_latest))
        if self.next_before is not None:self.history_layout.addWidget(button('查看更早的讨论',lambda:self.poll(before=self.next_before)))
        if not messages:
            self.history_layout.addStretch();self.history_layout.addWidget(label('从一件具体的事开始','PageTitle'));self.history_layout.addWidget(label('补充你的想法、实际情况或课程资料。Codex 会保留这次讨论的上下文。','Quiet'));self.history_layout.addStretch()
        for message in messages:
            card=QFrame();card.setObjectName('ChatMessage');vertical=QVBoxLayout(card);vertical.setContentsMargins(16,12,16,14)
            if message.get('role')=='user':card.setProperty('userMessage',True)
            vertical.addWidget(label('你' if message.get('role')=='user' else 'Codex' if message.get('role')=='assistant' else '提示','Eyebrow'))
            vertical.addWidget(MessageText(message.get('text','')))
            ids=message.get('source_ids') or []
            if ids:vertical.addWidget(label('参考资料 · '+str(len(ids))+' 份','Quiet'))
            state=message.get('proposal_state')
            if state=='superseded':vertical.addWidget(label('已继续讨论；本轮旧建议不再可采用。','Quiet'))
            elif state=='applied':vertical.addWidget(label('✓ 这些变更已经保存','Quiet'))
            if message.get('job_id')==self.job_id and state=='available':
                valid=self.full_job.get('id')==self.job_id and self.full_job.get('status')=='awaiting_review'
                if valid:
                    vertical.addWidget(label('请核对将保存的内容','SectionHeading'));vertical.addWidget(MessageText(self.proposal_text()));vertical.addWidget(self.apply);self.apply.show();self.apply.setEnabled(bool((self.full_job.get('result') or {}).get('actions')) and not self.mutating and not self.applying and not self.active_job_id)
                else:vertical.addWidget(label('正在读取完整的待保存内容…','Quiet'))
            self.history_layout.addWidget(card)
        if self.active_job_id:self.history_layout.addWidget(label('Codex 正在处理…','Quiet'))
        if messages:self.history_layout.addStretch()
        def position_history():
            if self.closed or serial!=self.render_serial:return
            bar.setValue(bar.maximum() if bottom or self.initial_scroll else old);self.initial_scroll=False
        QTimer.singleShot(80,position_history)

    def apply_result(self):
        if not any(m.get('job_id')==self.job_id and m.get('proposal_state')=='available' for m in self.messages.values()) or self.applying or self.mutating or self.active_job_id or not self.job_id or self.full_job.get('id')!=self.job_id or self.full_job.get('status')!='awaiting_review' or not (self.full_job.get('result') or {}).get('actions'):return
        self.applying=True;self.generation+=1;self.apply.setEnabled(False);self.update_controls()
        def saved(receipt):
            self.applying=False;self.generation+=1;self.full_job={};self.status.setText('已保存这些变更。')
            if self.on_saved:self.on_saved(receipt)
            self.poll(force=True)
        self.bridge.command('apply_proposal',{'id':self.job_id},saved,self.error,epoch=self.epoch)

    def cancel_job(self):
        if not self.active_job_id or self.mutating:return
        self.mutating=True;self.update_controls()
        def saved(_):self.mutating=False;self.poll(force=True)
        self.bridge.command('cancel_job',{'id':self.active_job_id},saved,self.error,epoch=self.epoch)

    def error(self,error):
        self.mutating=False;self.applying=False;self.update_controls();self.status.setText(error.get('message',str(error)))

    def showEvent(self,event):
        super().showEvent(event);self.closed=False;self.timer.start()

    def closeEvent(self,event):
        self.closed=True;self.timer.stop();super().closeEvent(event)
