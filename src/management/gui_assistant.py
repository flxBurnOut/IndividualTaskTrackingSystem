"""Persistent scoped conversations with one composer and reviewable proposals."""
from __future__ import annotations
import datetime as dt
import html
import re
import time
import uuid
from PySide6.QtCore import Qt,QTimer,QUrl
from PySide6.QtGui import QTextCursor
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
    def update_text(self,text):
        """Append streamed plain text without discarding a user's selection."""
        old=self.toPlainText()
        if old==text:return
        cursor=self.textCursor();anchor,position=cursor.anchor(),cursor.position()
        if text.startswith(old):
            end=QTextCursor(self.document());end.movePosition(QTextCursor.MoveOperation.End);end.insertText(text[len(old):])
        else:self.setPlainText(text)
        cursor=QTextCursor(self.document());cursor.setPosition(min(anchor,len(text)));cursor.setPosition(min(position,len(text)),QTextCursor.MoveMode.KeepAnchor);self.setTextCursor(cursor);self.fit()

    def refresh_theme(self):
        self.document().setDefaultFont(self.font());self.fit()
    def fit(self,*_):
        self.setFixedHeight(max(36,min(900,int(self.document().size().height())+8)))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if self.document().size().height()>890 else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    def resizeEvent(self,event):super().resizeEvent(event);self.fit()
    def loadResource(self,type,url):
        if url.scheme() in {'http','https','file'}:return None
        return super().loadResource(type,url)

class ChatCard(QFrame):
    """A stable bubble: polling updates its content, never the whole transcript."""
    def __init__(self,parent=None):
        super().__init__(parent);self.setObjectName('ChatMessage')
        self.vertical=QVBoxLayout(self);self.vertical.setContentsMargins(16,12,16,14)
        self.author=label('','Eyebrow');self.body=MessageText('');self.note=label('','Quiet');self.sources=label('','Quiet')
        self.proposal_heading=label('请核对将保存的内容','SectionHeading');self.proposal=MessageText('')
        for widget in (self.author,self.body,self.sources,self.note,self.proposal_heading,self.proposal):self.vertical.addWidget(widget)
        self.proposal_heading.hide();self.proposal.hide();self.note.hide();self.sources.hide()

    def content(self,role,text,note='',source_count=0):
        user=role=='user'
        if self.property('userMessage')!=user:
            self.setProperty('userMessage',user);self.style().unpolish(self);self.style().polish(self)
        self.author.setText('你' if user else 'Codex' if role=='assistant' else '提示')
        self.body.update_text(text);self.body.setVisible(bool(text))
        self.note.setText(note);self.note.setVisible(bool(note))
        self.sources.setText('参考资料 · '+str(source_count)+' 份' if source_count else '');self.sources.setVisible(bool(source_count))


PROGRESS_LABELS={
    'queued':'已发送，等待处理','connecting':'正在连接 Codex','resuming':'正在恢复这次讨论',
    'creating_thread':'正在创建 Codex 任务','thread_ready':'Codex 任务已连接','sending':'正在将消息交给 Codex',
    'waiting_model':'Codex 已收到，正在等待回复','reasoning':'Codex 正在思考','receiving':'正在接收回复',
    'validating':'正在核对回复与待保存内容','provider_retry':'连接暂时中断，正在恢复',
    'compacting':'正在保存进度并整理上下文','checkpointed':'本批已保存，即将继续','reading':'正在分批读取资料',
    'running':'Codex 正在处理','failed':'本次处理失败','cancelled':'本次处理已停止','completed':'回复已完成',
}


class AssistanceDialog(QDialog):
    def __init__(self,bridge,parent=None,prompt='',on_saved=None,context_entities=None,intent=None,business_date=None,scope=None,on_open_settings=None,source_ids=None,auto_send=False,on_open_codex_thread=None):
        super().__init__(parent)
        self.bridge,self.on_saved,self.on_open_settings=bridge,on_saved,on_open_settings;self.epoch=bridge.epoch;self.on_open_codex_thread=on_open_codex_thread
        self.codex_connection=getattr(parent,'codex_connection',None)
        self._owns_codex_connection=self.codex_connection is None
        if self._owns_codex_connection:
            from .gui_codex_connection import CodexConnectionController
            self.codex_connection=CodexConnectionController(parent=self,bridge=bridge)
        self._pending_connection_send=None
        context_entities=context_entities or [];self.context_entities=context_entities
        if scope is None:
            if context_entities:
                entity=context_entities[0];scope={'kind':'course' if entity.get('type')=='course' else 'object','entity_id':entity['id']}
            elif business_date or intent=='review_handoff':
                match=re.search(r'\b\d{4}-\d{2}-\d{2}\b',prompt);scope={'kind':'daily_review' if intent=='review_handoff' else 'daily_plan','date':business_date or (match.group(0) if match else dt.date.today().isoformat())}
            else:scope={'kind':'general'}
        self.intent=intent
        self.initial_planning_prompt=prompt.strip() if intent=='daily_plan' else None
        self.scope=dict(scope);self.owner_id=scope.get('entity_id');self.conversation=None;self.messages={};self.next_before=None;self.active_job_id=None;self.job_id=None;self.full_job={};self.pending=False;self.mutating=False;self.applying=False;self.generation=0;self.settings_ready=False;self.conversation_ready=False;self.ai_enabled=False;self.auto_send=auto_send;self.auto_sent=False;self.closed=False;self.loading_older=False;self.showing_older=False;self.last_render=None;self.proposal_loading=None;self.dialogs=[]
        self.execution_mode='background';self.auto_focus_job=None;self.focused_thread_ids=set();self.navigation_error='';self.cards={};self.card_order=[];self.outgoing=None;self.active_progress={};self.latest_progress={};self.progress_seen_at=time.monotonic();self.poll_again=False;self.poll_before=None;self.receipt_pending=False;self.operation_serial=0;self.epoch_invalid=False
        self.attachment_dirty=source_ids is not None;self.selected_ids=list(dict.fromkeys(source_ids or []));self.source_meta={};self.attachment_version=0;self.initial_scroll=True;self.render_serial=0
        self._auto_send_text=prompt
        self.setWindowTitle('与 Codex 讨论');self.resize(840,780);self.setMinimumSize(590,520)
        layout=QVBoxLayout(self);layout.setContentsMargins(24,20,24,18);layout.setSpacing(12)
        header=QHBoxLayout();header.addWidget(label('与 Codex 讨论','DialogHeading'),1);self.open_codex=button('在 Codex 查看',self.open_codex_thread);self.open_codex.hide();header.addWidget(self.open_codex)
        scope_title=context_entities[0].get('title','') if context_entities else {'daily_plan':'每日计划','daily_review':'每日复盘','general':'日常讨论'}.get(scope.get('kind'),'当前事项')
        if scope.get('date'):scope_title+=' · '+scope['date']
        self.scope_label=label(scope_title,'Quiet');layout.addLayout(header);layout.addWidget(self.scope_label)
        self.history=QScrollArea();self.history.setWidgetResizable(True);self.history.setFrameShape(QFrame.Shape.NoFrame);self.history_body=QWidget();self.history_layout=QVBoxLayout(self.history_body);self.history_layout.setContentsMargins(4,4,10,8);self.history_layout.setSpacing(18);self.history.setWidget(self.history_body);layout.addWidget(self.history,1)
        self.latest_button=button('回到最新回复',self.return_latest);self.older_button=button('查看更早的讨论',lambda:self.poll(before=self.next_before));self.empty_history=QWidget();empty_layout=QVBoxLayout(self.empty_history);empty_layout.addWidget(label('从一件具体的事开始','PageTitle'));empty_layout.addWidget(label('补充你的想法、实际情况或课程资料。Codex 会保留这次讨论的上下文。','Quiet'));self.history_layout.addWidget(self.latest_button);self.history_layout.addWidget(self.older_button);self.history_layout.addWidget(self.empty_history);self.history_layout.addStretch();self.latest_button.hide();self.older_button.hide()
        self.status=label('正在读取这次讨论…','Quiet');layout.addWidget(self.status)
        self.desktop_connection_note=label('','Quiet');self.desktop_connection_note.hide();layout.addWidget(self.desktop_connection_note)
        self.desktop_connection_retry=button('连接 Codex',self.retry_desktop_connection);self.desktop_connection_retry.hide();layout.addWidget(self.desktop_connection_retry,alignment=Qt.AlignmentFlag.AlignLeft)
        self.connect_button=button('连接 Codex',self.open_settings);self.connect_button.setObjectName('Primary');self.connect_button.hide();layout.addWidget(self.connect_button,alignment=Qt.AlignmentFlag.AlignLeft)
        self.repair_button=button('恢复原会话关联',self.repair_conversation);self.repair_button.hide();layout.addWidget(self.repair_button,alignment=Qt.AlignmentFlag.AlignLeft)
        self.resume_button=button('从已保存进度继续',self.resume_operation);self.resume_button.hide();layout.addWidget(self.resume_button,alignment=Qt.AlignmentFlag.AlignLeft)
        composer=QFrame();composer.setObjectName('ChatComposer');composer_layout=QVBoxLayout(composer);composer_layout.setContentsMargins(12,10,12,10)
        self.chips=QWidget();self.chip_layout=QHBoxLayout(self.chips);self.chip_layout.setContentsMargins(0,0,0,0);self.chip_scroll=QScrollArea();self.chip_scroll.setWidgetResizable(True);self.chip_scroll.setFrameShape(QFrame.Shape.NoFrame);self.chip_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff);self.chip_scroll.setWidget(self.chips);composer_layout.addWidget(self.chip_scroll)
        self.prompt=Composer(self.start_job);self.prompt.setFrameShape(QFrame.Shape.NoFrame);self.prompt.setStyleSheet('QTextEdit {border:0;background:transparent;}');self.prompt.setFixedHeight(80);self.prompt.setPlaceholderText('说说你想安排什么，或补充新的实际情况…');self.prompt.setPlainText(prompt);composer_layout.addWidget(self.prompt)
        self.prompt.textChanged.connect(self.cancel_pending_connection_send)
        row=QHBoxLayout();self.attach=button('＋ 附件',self.choose_sources);row.addWidget(self.attach);row.addWidget(label('Enter 发送 · Shift + Enter 换行','Quiet'),1);self.send=button('发送',self.send_or_stop);self.send.setObjectName('Primary');self.send.setMinimumWidth(86);row.addWidget(self.send);composer_layout.addLayout(row);layout.addWidget(composer)
        self.all_changes=button('查看完整变更清单',self.show_all_changes);self.all_changes.hide();layout.addWidget(self.all_changes,alignment=Qt.AlignmentFlag.AlignLeft)
        self.apply=QPushButton('核对后保存这些变更');self.apply.setObjectName('Primary');self.apply.clicked.connect(self.apply_result);self.apply.hide()
        self.connection_send_timer=QTimer(self);self.connection_send_timer.setSingleShot(True);self.connection_send_timer.setInterval(30000);self.connection_send_timer.timeout.connect(self.cancel_pending_connection_send)
        self.timer=QTimer(self);self.timer.setInterval(1500);self.timer.timeout.connect(self.tick);self.timer.start();self.finished.connect(self.finished_dialog);self.refresh_chips();self.load_settings();self.poll();self.load_source_titles()
        from .gui_tutorials import install_dialog_tutorial
        install_dialog_tutorial(self, 'discussion')
        if self.codex_connection is not None:
            self.codex_connection.changed.connect(self.desktop_connection_changed)
            self.desktop_connection_changed(self.codex_connection.snapshot())
        bind_theme(self,self.fit_attachment_height)

    def desktop_connection_changed(self,value):
        if self.closed:return
        visible=self.execution_mode=='desktop_shared' and value.get('required',False)
        self.desktop_connection_note.setVisible(visible)
        self.desktop_connection_note.setText('Codex 连接：'+value.get('message',''))
        self.desktop_connection_retry.setVisible(visible)
        self.desktop_connection_retry.setText('检查连接' if value.get('ready') else '连接 Codex')
        self.desktop_connection_retry.setEnabled(not value.get('connect_pending'))
        pending=self._pending_connection_send
        if pending and value.get('ready'):
            matches=self._connection_draft_matches(pending)
            self._pending_connection_send=None;self.connection_send_timer.stop()
            if matches and self.current_epoch() and not self.active_job_id and not self.mutating and not self.applying:
                self.start_job(connect_if_needed=False)
                return
        elif pending and value.get('state') in {'disabled','desktop_closed','disconnected','unsupported','error'}:
            self._pending_connection_send=None;self.connection_send_timer.stop()
        self.update_controls()

    def retry_desktop_connection(self):
        if self.codex_connection is not None:self.codex_connection.request_connect()

    def desktop_connection_blocked(self):
        return self.execution_mode=='desktop_shared' and self.codex_connection is not None and self.codex_connection.required and not self.codex_connection.ready

    def _connection_draft_matches(self,pending):
        return (not self.closed and pending['text']==self.prompt.toPlainText()
                and pending['attachment_version']==self.attachment_version
                and pending['source_ids']==tuple(self.selected_ids) and pending['epoch']==self.epoch)

    def cancel_pending_connection_send(self,*_):
        if self._pending_connection_send is None:return
        self._pending_connection_send=None;self.connection_send_timer.stop()
        if not self.closed:
            self.update_controls();self.status.setText('已取消本次等待发送，草稿保留；需要发送时再次点击。')

    def connect_and_send(self):
        if self._pending_connection_send is not None:return
        self._pending_connection_send={'text':self.prompt.toPlainText(),'attachment_version':self.attachment_version,
            'source_ids':tuple(self.selected_ids),'epoch':self.epoch}
        self.connection_send_timer.start()
        self.update_controls()
        self.codex_connection.request_connect()


    def show_all_changes(self):
        result=(self.full_job or {}).get('result') or {};reader=result.get('action_reader') or {}
        if not reader.get('operation_id'):return
        dialog=QDialog(self);dialog.setWindowTitle('待确认变更');dialog.resize(700,600)
        layout=QVBoxLayout(dialog);layout.addWidget(label(f"共 {result.get('actions_total',len(result.get('actions',[])))} 项 · 确认后统一保存",'DialogHeading'))
        view=QTextBrowser();layout.addWidget(view,1);more=button('下一页',lambda:load());layout.addWidget(more)
        state={'after':0,'offset':0,'fragment':''}
        def load():
            more.setEnabled(False)
            def received(page):
                if 'json_fragment' in page:
                    import json
                    state['fragment']+=page['json_fragment']
                    state['offset']=page.get('next_offset') or 0
                    if page.get('next_offset') is not None:more.setEnabled(True);load();return
                    items=[json.loads(state['fragment'])];state['fragment']=''
                else:items=page.get('items',[])
                lines=[]
                for item in items:
                    payload=item.get('payload') or {}
                    lines.append((payload.get('title') or {'update':'更新事项','create':'新建事项','create_plan':'保存每日计划'}.get(item['command'],'保存变更'))+'\n'+item.get('reason',''))
                    def visible(value):
                        if isinstance(value,dict):return {FIELD_LABELS.get(k,k):visible(v) for k,v in value.items() if k not in {'id','version','target_version','source_versions'} and not k.endswith(('_id','_ids'))}
                        if isinstance(value,list):return [visible(v) for v in value]
                        return value
                    lines.append(readable(visible(payload)))
                view.setPlainText('\n\n'.join(lines))
                state['after']=page.get('next_after') or 0;more.setEnabled(True);more.setVisible(page.get('next_after') is not None)
            self.bridge.query('candidate_actions',received,lambda e:(more.setEnabled(True),self.error(e)),operation_id=reader['operation_id'],after=state['after'],offset=state['offset'])
        self.dialogs.append(dialog);dialog.open();load()

    def resume_operation(self):
        if self.closed or not self.current_epoch() or self.mutating or self.active_job_id:return
        if self.desktop_connection_blocked():
            self.update_controls();return
        identifier=(self.latest_progress or {}).get('job_id')
        if not identifier:return
        self.resume_button.setEnabled(False)
        def saved(_):
            self.resume_button.setEnabled(True);self.poll()
        def failed(error):
            self.resume_button.setEnabled(True);self.error(error)
        self.bridge.command('resume_context_operation',{'id':identifier},saved,failed,epoch=self.epoch)

    def repair_conversation(self):
        if self.closed or not self.conversation or self.active_job_id or self.mutating:return
        from PySide6.QtWidgets import QInputDialog
        import datetime as dt
        conversation=dict(self.conversation);generation=self.generation
        self.repair_button.setEnabled(False)
        def loaded(result):
            if self.closed or generation!=self.generation:return
            self.repair_button.setEnabled(True)
            items=result.get('items',[])
            if not items:
                self.status.setText('没有找到可关联任务。请先在 Codex 打开原任务，再回来核对。');return
            options=[]
            for index,item in enumerate(items):
                stamp=item.get('created_at')
                try:date=dt.datetime.fromtimestamp(stamp).strftime('%m-%d %H:%M')
                except (TypeError,ValueError,OSError):date='日期未知'
                options.append(str(index+1)+' · '+str(item['title'])[:100]+' · 创建于 '+date)
            selected,ok=QInputDialog.getItem(self,'恢复原会话关联','选择对应的原 Codex 任务（会保留所有历史讨论）：',options,0,False)
            if not ok:return
            target=items[options.index(selected)]
            self.mutating=True;self.update_controls()
            def saved(receipt):
                if self.closed:return
                self.mutating=False;self.status.setText('已关联原任务，没有新建会话。可以继续发送。');self.poll()
            def failed(error):
                if self.closed:return
                self.mutating=False;self.repair_button.setEnabled(True);self.error(error)
            self.bridge.command('attach_conversation',{'conversation_id':conversation['id'],'version':conversation['version'],
                'provider_thread_id':target['id']},saved,failed)
        def failed(error):
            if not self.closed:self.repair_button.setEnabled(True);self.error(error)
        self.bridge.query('conversation_targets',loaded,failed,conversation_id=conversation['id'])

    def finished_dialog(self, *_):
        self.closed=True;self.timer.stop();self._pending_connection_send=None;self.connection_send_timer.stop()
        if self._owns_codex_connection:self.codex_connection.request_stop()

    def load_source_titles(self):
        for identifier in self.selected_ids[:20]:
            if identifier in self.source_meta:continue
            def loaded(result, i=identifier):
                if self.closed:return
                entity=result.get('entity') or {}
                if entity.get('id')==i:self.source_meta[i]=entity;self.refresh_chips()
            self.bridge.query('get',loaded,lambda _:None,id=identifier)

    def load_settings(self):
        if self.closed:return
        def loaded(result):
            if self.closed or not self.current_epoch(result):return
            self.settings_ready=True;ai=result.get('settings',{}).get('ai',{});self.ai_enabled=bool(ai.get('enabled'));self.execution_mode=ai.get('execution_mode','background')
            if self.codex_connection is not None:
                self.codex_connection.configure(result.get('settings',{}));self.desktop_connection_changed(self.codex_connection.snapshot())
            self.update_controls();self.maybe_open_live_thread();self.maybe_auto_send()
        def failed(error):
            if self.closed or not self.current_epoch():return
            self.settings_ready=True;self.error(error);self.update_controls()
        self.bridge.query('settings',loaded,failed)

    def open_settings(self):
        if self.on_open_settings:
            result=self.on_open_settings()
            if hasattr(result,'finished'):result.finished.connect(lambda _:self.load_settings())
        QTimer.singleShot(300,self.load_settings)

    def maybe_auto_send(self):
        if self.auto_send and not self.auto_sent and self.settings_ready and self.conversation_ready and self.ai_enabled and not self.active_job_id:
            self.auto_sent=True
            if (not self.desktop_connection_blocked() and self.prompt.toPlainText()==self._auto_send_text
                    and self.attachment_version==0):self.start_job(connect_if_needed=False)

    def fit_attachment_height(self):
        # Reserve the horizontal scrollbar as well as the current button height.
        # A fixed 51px strip cut off large-font chips when the bar appeared.
        for index in range(self.chip_layout.count()):
            widget=self.chip_layout.itemAt(index).widget()
            if widget:widget.ensurePolished()
        self.chip_layout.invalidate()
        content_height=self.chip_layout.sizeHint().height()
        scrollbar_height=self.chip_scroll.horizontalScrollBar().sizeHint().height()
        self.chip_scroll.setFixedHeight(max(51,content_height+scrollbar_height+2*self.chip_scroll.frameWidth()))

    def refresh_chips(self):
        if self._pending_connection_send and not self._connection_draft_matches(self._pending_connection_send):
            self.cancel_pending_connection_send()
        while self.chip_layout.count():
            item=self.chip_layout.takeAt(0)
            if item.widget():item.widget().deleteLater()
        for identifier in self.selected_ids:
            source=self.source_meta.get(identifier,{});title=source.get('title','资料 '+identifier[:8]);chip=button((title[:20]+'…' if len(title)>20 else title)+' ×',lambda _,i=identifier:self.remove_source(i));chip.setToolTip(title+'\n点击从这次讨论移除');self.chip_layout.addWidget(chip)
        automatic=bool((self.conversation or {}).get('source_scope_owner') and not self.attachment_dirty)
        if automatic:
            self.chip_layout.addWidget(label('课程资料 · 按需读取','Quiet'))
        if len(self.selected_ids)>20:self.chip_layout.addWidget(label(f'共 {len(self.selected_ids)} 份资料','Quiet'))
        self.chip_layout.addStretch();self.chip_scroll.setVisible(bool(self.selected_ids) or automatic);self.fit_attachment_height()

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

    def current_epoch(self,result=None):
        epoch=(result or {}).get('epoch')
        if (epoch is not None and self.epoch is not None and epoch!=self.epoch) or (self.epoch is not None and self.bridge.epoch is not None and self.bridge.epoch!=self.epoch):
            self._pending_connection_send=None;self.connection_send_timer.stop()
            self.epoch_invalid=True;self.mutating=False;self.applying=False;self.timer.stop()
            self.send.setEnabled(False);self.apply.setEnabled(False);self.status.setText('数据空间已切换，请关闭并重新打开讨论。未发送文字仍保留在输入框。')
            return False
        return not self.epoch_invalid

    def thread_id(self):
        return (self.conversation or {}).get('provider_thread_id') or self.active_progress.get('provider_thread_id')

    def open_codex_thread(self):
        identifier=self.thread_id()
        if not identifier or (self.active_job_id and self.execution_mode!='desktop_shared') or self.mutating or not self.current_epoch():return
        try:
            if self.on_open_codex_thread:result=self.on_open_codex_thread(identifier)
            else:
                from .codex_links import open_thread
                result=open_thread(identifier)
            self.focused_thread_ids.add(identifier)
            if result is False:self.navigation_error='导航到 Codex 失败，任务仍按当前状态继续；可再次点击“在 Codex 查看”。'
            else:self.navigation_error=''
        except Exception as exc:self.navigation_error='导航到 Codex 失败：'+str(exc)+'。任务不会重发。'
        self.update_controls()

    def maybe_open_live_thread(self):
        if self.execution_mode!='desktop_shared' or not self.auto_focus_job or self.closed:return
        progress=self.active_progress if self.active_progress.get('job_id')==self.auto_focus_job else self.latest_progress
        if progress.get('job_id')!=self.auto_focus_job or not progress.get('provider_turn_id') or not progress.get('provider_thread_id'):return
        identifier=progress['provider_thread_id'];self.auto_focus_job=None
        if identifier in self.focused_thread_ids:return
        self.focused_thread_ids.add(identifier)
        self.open_codex_thread()

    def progress_text(self):
        progress=self.active_progress;phase=progress.get('phase') or progress.get('status') or 'running'
        seconds=progress.get('elapsed_seconds',0)
        try:seconds=max(0,int(float(seconds)+time.monotonic()-self.progress_seen_at))
        except (TypeError,ValueError,OverflowError):seconds=0
        duration=(str(seconds//60)+' 分 '+str(seconds%60)+' 秒') if seconds>=60 else str(seconds)+' 秒'
        text=PROGRESS_LABELS.get(phase,'Codex 正在处理')+' · 已等待 '+duration
        context=progress.get('context_progress') or {}
        if context:
            text+=f" · 第 {context.get('stage',1)} 阶段"
            if context.get('extracted'):
                text+=f" · 已处理 {context.get('processed',0)} 批"
                text+=('，其余资料仍在读取' if context.get('more_extracting') else f" / 共 {context['extracted']} 批")
        return text

    def tick(self):
        if self.closed or not self.current_epoch():return
        if self.active_job_id:self.update_controls();self.render_history()
        self.poll()

    def update_controls(self):
        if not self.current_epoch():return
        ready=self.settings_ready and self.conversation_ready
        candidate=(self.full_job or {}).get('result') or {}
        total=candidate.get('actions_total',len(candidate.get('actions',[])))
        self.all_changes.setVisible(bool(candidate.get('action_reader') and (total>len(candidate.get('actions',[])) or any(a.get('large_candidate') for a in candidate.get('actions',[])))))
        self.all_changes.setText(f'查看完整的 {total} 项变更')
        waiting=self._pending_connection_send is not None
        self.send.setText('停止' if self.active_job_id else '取消等待发送' if waiting else '连接并发送' if self.desktop_connection_blocked() else '发送');self.send.setEnabled(ready and not self.mutating and not self.applying and (bool(self.active_job_id) or self.ai_enabled))
        self.attach.setEnabled(not self.mutating);self.prompt.setReadOnly(self.mutating)
        can_open=not self.mutating and (not self.active_job_id or self.execution_mode=='desktop_shared')
        self.open_codex.setVisible(bool(self.thread_id()));self.open_codex.setEnabled(can_open);self.open_codex.setToolTip('在 Codex 查看消息与处理进度' if can_open and self.execution_mode=='desktop_shared' else '处理结束后可在 Codex 查看' if not can_open else '在 Codex 查看这次讨论')
        self.timer.setInterval(500 if self.active_job_id or self.mutating else 1500)
        self.connect_button.setVisible(self.settings_ready and not self.ai_enabled and not self.active_job_id)
        if self.settings_ready and not self.ai_enabled and not self.active_job_id:self.status.setText('连接 Codex 后，就可以在这里讨论。资料和历史记录仍保存在本机。')
        elif self.mutating and self.outgoing and self.outgoing.get('delivery')=='sending':self.status.setText('正在发送消息…')
        elif self.active_job_id:self.status.setText(self.progress_text()+'。关闭窗口后仍会继续。')
        elif self.outgoing and self.outgoing.get('delivery')=='uncertain':self.status.setText('发送结果待确认，正在查询回执；不会自动重发。输入框保留原文。')
        elif self.outgoing and self.outgoing.get('delivery')=='failed':self.status.setText(self.outgoing.get('error') or '发送失败，原文保留在输入框。')
        elif waiting:self.status.setText('正在连接；成功后只发送本次草稿一次。修改草稿或点击“取消等待发送”可取消。')
        elif self.desktop_connection_blocked():self.status.setText('Codex 尚未连接，草稿已保留。点击“连接并发送”即可继续。')
        elif ready and not self.applying and not self.mutating:self.status.setText('讨论会自动保存；涉及实际记录的修改，需要核对后确认。')
        if self.navigation_error:self.status.setText(self.status.text()+' '+self.navigation_error)

    def send_or_stop(self):
        if self.active_job_id:self.cancel_job()
        elif self._pending_connection_send is not None:self.cancel_pending_connection_send()
        else:self.start_job()

    def start_job(self,*,connect_if_needed=True):
        if self.closed or not self.current_epoch() or self.mutating or self.applying or self.active_job_id or not self.settings_ready or not self.conversation_ready:return
        if not self.ai_enabled:self.update_controls();return
        if self._pending_connection_send is not None:return
        text=self.prompt.toPlainText().strip()
        if not text:self.error({'message':'先写下需要讨论的内容。'});return
        if self.outgoing and self.outgoing.get('delivery')=='uncertain':
            self.update_controls();return
        if self.desktop_connection_blocked():
            if connect_if_needed:self.connect_and_send()
            else:self.update_controls()
            return
        payload={'scope':self.scope,'text':text}
        if self.intent=='daily_plan' and text==self.initial_planning_prompt:payload['request_plan']=True
        if self.intent=='course_notes':payload['skill_id']='course-notes'
        if self.attachment_dirty:payload['source_ids']=list(self.selected_ids)
        self.navigation_error='';self.mutating=True;self.generation+=1;self.full_job={};self.proposal_loading=None;self.apply.setEnabled(False);self.operation_serial+=1;operation=self.operation_serial;version=self.attachment_version
        key=(self.outgoing or {}).get('id') or 'outgoing:'+str(uuid.uuid4())
        self.outgoing={'id':key,'role':'user','text':text,'source_ids':list(self.selected_ids),'delivery':'sending','operation':operation,'payload':payload,'attachment_version':version,'baseline_seq':max((m.get('seq',0) for m in self.messages.values()),default=0)}
        self.update_controls();self.render_history()
        def saved(receipt):self.accept_send(receipt,operation)
        def failed(error):
            if self.closed or operation!=self.operation_serial or not self.current_epoch():return
            self.mutating=False;self.generation+=1
            uncertain=error.get('code') in {'connection_lost','connection_error','timeout'}
            self.outgoing.update(delivery='uncertain' if uncertain else 'failed',error=error.get('message','发送失败'),request_id=(error.get('details') or {}).get('request_id'))
            self.update_controls();self.render_history();self.poll(force=True)
        self.bridge.command('send_message',payload,saved,failed,epoch=self.epoch)

    def accept_send(self,receipt,operation):
        if self.closed or operation!=self.operation_serial or not self.current_epoch(receipt) or not self.outgoing:return
        pending=self.outgoing;payload=pending['payload'];text=pending['text']
        if payload.get('skill_id')=='course-notes' or self.intent=='daily_plan':self.intent=None
        self.mutating=False;self.generation+=1;self.showing_older=False;self.loading_older=False;result=receipt.get('result',receipt);self.conversation=result.get('conversation') or self.conversation
        self.active_job_id=(result.get('job') or {}).get('id') or (self.conversation or {}).get('active_job_id')
        self.auto_focus_job=self.active_job_id if self.execution_mode=='desktop_shared' else None
        self.active_progress={'job_id':self.active_job_id,'status':'queued','phase':'queued','elapsed_seconds':0};self.progress_seen_at=time.monotonic()
        if self.prompt.toPlainText().strip()==text:self.prompt.clear()
        if pending['attachment_version']==self.attachment_version:self.attachment_dirty=False
        message=result.get('user_message')
        if message:self.adopt_outgoing(message)
        else:self.outgoing.update(delivery='sent',job_id=self.active_job_id)
        self.update_controls();self.render_history();self.poll(force=True)

    def adopt_outgoing(self,message):
        if self.outgoing:
            old=self.cards.pop(self.outgoing['id'],None)
            if old:
                existing=self.cards.get(message['id'])
                if existing is not None and existing is not old:
                    self.history_layout.removeWidget(existing);existing.hide();existing.setParent(None);existing.deleteLater();self.card_order=[]
                self.cards[message['id']]=old
                self.card_order=list(dict.fromkeys(message['id'] if key==self.outgoing['id'] else key for key in self.card_order))
        self.messages[message['id']]=message;self.outgoing=None

    def recover_outgoing(self):
        outgoing=self.outgoing
        if self.receipt_pending or not outgoing or outgoing.get('delivery')!='uncertain' or not outgoing.get('request_id'):return
        self.receipt_pending=True;operation=outgoing['operation'];request_id=outgoing['request_id']
        def loaded(result):
            self.receipt_pending=False
            if self.closed or operation!=self.operation_serial or not self.current_epoch(result):return
            if result.get('found'):self.accept_send(result['receipt'],operation)
        def failed(_):self.receipt_pending=False
        self.bridge.query('receipt',loaded,failed,request_id=request_id)

    def return_latest(self):
        self.generation+=1;self.messages={};self.showing_older=False;self.loading_older=False;self.next_before=None;self.initial_scroll=True;self.poll(force=True)

    def poll(self,force=False,before=None):
        if self.closed or not self.current_epoch():return
        if self.pending or self.mutating:
            if force or before is not None:self.poll_again=True;self.poll_before=before
            return
        self.pending=True;generation=self.generation;params={'scope':self.scope,'limit':50}
        if before is not None:params['before']=before
        def settled():
            self.pending=False
            if self.poll_again and not self.closed:
                queued_before=self.poll_before;self.poll_again=False;self.poll_before=None;self.poll(before=queued_before)
        def loaded(result):
            if self.closed or generation!=self.generation or not self.current_epoch(result):settled();return
            self.conversation_ready=True
            # Keep a bounded transcript; old pages are not displaced by live polling.
            if before is not None or not self.showing_older:
                for message in result.get('messages',[]):
                    if self.outgoing and self.outgoing.get('job_id') and message.get('role')=='user' and message.get('job_id')==self.outgoing.get('job_id'):self.adopt_outgoing(message)
                    else:self.messages[message['id']]=message
                ordered=sorted(self.messages.values(),key=lambda m:m.get('seq',0));kept=ordered[:200] if before is not None else ordered[-200:];self.messages={m['id']:m for m in kept}
            if before is not None or not self.loading_older:self.next_before=(min((m.get('seq',0) for m in self.messages.values()),default=result.get('next_before')) if result.get('has_more') else None)
            if before is not None:self.loading_older=True;self.showing_older=True
            if before is None:
                prior_source_scope=(self.conversation or {}).get('source_scope_owner')
                self.conversation=result.get('conversation');conversation=self.conversation or {};self.active_job_id=conversation.get('active_job_id');current=conversation.get('current_proposal_job_id')
                if prior_source_scope!=conversation.get('source_scope_owner'):self.refresh_chips()
                progress=result.get('active_progress') or conversation.get('active_progress') or {}
                if progress.get('job_id')==self.active_job_id:
                    previous=self.active_progress
                    if previous.get('job_id')!=progress.get('job_id') or progress.get('sequence',0)>=previous.get('sequence',0):
                        changed=previous.get('job_id')!=progress.get('job_id') or previous.get('elapsed_seconds')!=progress.get('elapsed_seconds')
                        self.active_progress=progress
                        if changed:self.progress_seen_at=time.monotonic()
                elif self.active_progress.get('job_id')!=self.active_job_id:self.active_progress={}
                self.latest_progress=result.get('latest_progress') or conversation.get('latest_progress') or {}
                self.repair_button.setVisible(bool(self.conversation and not self.active_job_id and self.latest_progress.get('status')=='failed'))
                self.resume_button.setVisible(bool(not self.active_job_id and self.latest_progress.get('resumable_context') and self.latest_progress.get('status') in {'failed','cancelled'}))
                if not self.auto_focus_job and not self.outgoing and conversation.get('provider_thread_id'):self.focused_thread_ids.add(conversation['provider_thread_id'])
                for source in conversation.get('sources',[]):self.source_meta[source['id']]=source
                if not self.attachment_dirty:
                    selected=list(conversation.get('source_ids',[]))
                    if selected!=self.selected_ids:self.selected_ids=selected;self.refresh_chips();self.load_source_titles()
                if current!=self.job_id:self.job_id=current;self.full_job={};self.proposal_loading=None
                if current and not self.full_job and self.proposal_loading!=current:self.load_proposal(current)
            self.update_controls();self.render_history();self.maybe_open_live_thread();self.recover_outgoing();settled();self.maybe_auto_send()
        def failed(error):
            if not self.closed and generation==self.generation and self.current_epoch():self.status.setText('暂时无法刷新讨论：'+error.get('message',str(error)))
            settled()
        self.bridge.query('conversation',loaded,failed,**params)

    def load_proposal(self,identifier):
        self.proposal_loading=identifier;generation=self.generation
        def loaded(result):
            if self.closed or generation!=self.generation or identifier!=self.job_id or not self.current_epoch(result):return
            self.proposal_loading=None;self.full_job=result.get('job',{})
            preview=(self.full_job.get('result') or {}).get('plan_preview')
            self.apply.setText('保存到 '+preview['date']+' 的每日计划' if preview else '核对后保存这些变更')
            self.render_history(force=True)
        def failed(error):
            if not self.closed and generation==self.generation and identifier==self.job_id and self.current_epoch():self.proposal_loading=None;self.error(error)
        self.bridge.query('job',loaded,failed,id=identifier)

    def proposal_text(self):
        result=self.full_job.get('result') or {};lines=[]
        visible_reply='\n'.join(m.get('text','') for m in self.messages.values() if m.get('job_id')==self.job_id and m.get('role')=='assistant')
        unknowns=[str(x) for x in result.get('unknowns',[]) if str(x) not in visible_reply]
        if unknowns:lines+=['仍需确认']+['• '+x for x in unknowns]+['']
        for action in result.get('actions',[]):
            payload=action.get('payload') or {};command=action.get('command');title=payload.get('title') or ''
            names={'revise_plan':'调整每日计划','add_to_plan':'加入每日计划','set_task_completion':'记录任务完成情况','submit_daily_review':'记录逐项复盘','apply_timetable':'保存每周课表框架','create':'新建'+label_type(payload.get('type')),'update':'更新记录','record_feedback':'记录明确反馈','create_plan':'保存每日计划','save_review':'保存回顾','set_recurring_rule':'固定准备事项','set_recovery_task':'登记补课或补欠','record_recovery_progress':'记录补欠进度'}
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
                lines.append('确认后自动进入当天安排和复盘；准备任务按规则生成，明确缺课需补才建立补课事项。')
            elif command=='create_plan':
                lines.append('日期：'+str(payload.get('date','待确认')))
                preview=result.get('plan_preview') or {}
                if payload.get('mode')=='no_precise_time':lines.append('按先后顺序推进；具体钟点暂不假定。')
                for i,block in enumerate(preview.get('items',payload.get('blocks',[])),1):
                    title=block.get('title') or next((e.get('title') for e in self.context_entities if e.get('id')==block.get('target_id')),None) or '已登记事项'
                    timing=(str(block['start'])+'–'+str(block['end'])+' ') if block.get('start') and block.get('end') else ''
                    lines.append(str(i)+'. '+timing+title)
                lines.append('点击下方保存后，计划才会出现在当天页面。')
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
        if self.closed:return
        bar=self.history.verticalScrollBar();old=bar.value();bottom=bar.maximum()-old<60
        anchor=next((self.cards[key] for key in self.card_order if key in self.cards and self.cards[key].geometry().bottom()>=old),None)
        offset=anchor.y()-old if anchor is not None else 0
        entries=[]
        for message in sorted(self.messages.values(),key=lambda x:x.get('seq',0)):entries.append((message['id'],message))
        if self.outgoing:
            # An uncertain receipt may arrive after the persisted message is visible.
            # Suppress only the redundant display; identity still waits for its receipt.
            shadowed=self.outgoing.get('delivery')=='uncertain' and any(m.get('role')=='user' and m.get('seq',0)>self.outgoing.get('baseline_seq',0) and m.get('text')==self.outgoing['text'] and sorted(m.get('source_ids') or [])==sorted(self.outgoing.get('source_ids') or []) for m in self.messages.values())
            if not shadowed:entries.append((self.outgoing['id'],self.outgoing))
        progress=self.active_progress if self.active_job_id else self.latest_progress
        if self.active_job_id:
            entries.append(('live:'+self.active_job_id,{'role':'assistant','text':str(progress.get('preview_text') or ''),'live':True,'note':self.progress_text()+(' · 回复生成中，尚未完成或保存' if progress.get('preview_text') else '')}))
        elif progress.get('job_id') and progress.get('status') in {'failed','cancelled'} and progress.get('preview_text'):
            entries.append(('live:'+progress['job_id'],{'role':'assistant','text':str(progress['preview_text']),'live':True,'note':PROGRESS_LABELS[progress['status']]+' · 以下为未完成的部分回复，未保存为业务记录'}))
        keys=[key for key,_ in entries];changed=keys!=self.card_order
        self.latest_button.setVisible(self.showing_older);self.older_button.setVisible(self.next_before is not None);self.empty_history.setVisible(not entries)
        self.apply.setEnabled(False);apply_card=None
        for key,message in entries:
            card=self.cards.get(key)
            if card is None:card=ChatCard(self.history_body);self.cards[key]=card
            state=message.get('proposal_state');delivery=message.get('delivery');note=message.get('note','')
            if delivery:note={'sending':'正在发送…','sent':'已发送','uncertain':'发送结果待确认 · 不会自动重发','failed':'发送失败 · 原文仍在输入框'}[delivery]
            elif state=='superseded':note='已继续讨论；本轮旧建议不再可采用。'
            elif state=='applied':note='✓ 这些变更已经保存'
            card.content(message.get('role'),message.get('text',''),note,len(message.get('source_ids') or []))
            valid=message.get('job_id')==self.job_id and state=='available' and self.full_job.get('id')==self.job_id and self.full_job.get('status')=='awaiting_review'
            card.proposal_heading.setVisible(valid);card.proposal.setVisible(valid)
            if valid:
                card.proposal.update_text(self.proposal_text());apply_card=card
            elif message.get('job_id')==self.job_id and state=='available':card.note.setText('正在读取完整的待保存内容…');card.note.show()
        if apply_card:
            if self.apply.parent() is not apply_card:apply_card.vertical.addWidget(self.apply)
            self.apply.show();self.apply.setEnabled(bool((self.full_job.get('result') or {}).get('actions')) and not self.mutating and not self.applying and not self.active_job_id and not self.epoch_invalid)
        else:self.apply.hide();self.apply.setParent(self)
        for key in list(self.cards):
            if key not in keys:
                card=self.cards.pop(key);self.history_layout.removeWidget(card);card.hide();card.setParent(None);card.deleteLater()
        if changed:
            for index,key in enumerate(keys,3):
                widget=self.cards[key]
                if self.history_layout.indexOf(widget)!=index:self.history_layout.insertWidget(index,widget)
                widget.show()
            self.card_order=keys
        self.history_layout.activate();self.render_serial+=1;serial=self.render_serial
        def position_history():
            if self.closed or serial!=self.render_serial:return
            if bottom or self.initial_scroll:bar.setValue(bar.maximum())
            elif anchor is not None and anchor in self.cards.values():bar.setValue(anchor.y()-offset)
            else:bar.setValue(old)
            self.initial_scroll=False
        QTimer.singleShot(0,position_history)

    def apply_result(self):
        if not any(m.get('job_id')==self.job_id and m.get('proposal_state')=='available' for m in self.messages.values()) or self.applying or self.mutating or self.active_job_id or not self.job_id or self.full_job.get('id')!=self.job_id or self.full_job.get('status')!='awaiting_review' or not (self.full_job.get('result') or {}).get('actions'):return
        self.applying=True;self.generation+=1;generation=self.generation;self.apply.setEnabled(False);self.update_controls()
        def saved(receipt):
            if self.closed or generation!=self.generation or not self.current_epoch(receipt):return
            self.applying=False;self.generation+=1;self.full_job={};self.status.setText('已保存这些变更。')
            if self.on_saved:self.on_saved(receipt)
            self.poll(force=True)
        def failed(error):
            if not self.closed and generation==self.generation and self.current_epoch():self.error(error)
        self.bridge.command('apply_proposal',{'id':self.job_id},saved,failed,epoch=self.epoch)

    def cancel_job(self):
        if not self.active_job_id or self.mutating:return
        self.mutating=True;self.generation+=1;generation=self.generation;self.update_controls()
        def saved(receipt):
            if self.closed or generation!=self.generation or not self.current_epoch(receipt):return
            self.mutating=False;self.poll(force=True)
        def failed(error):
            if not self.closed and generation==self.generation and self.current_epoch():self.error(error)
        self.bridge.command('cancel_job',{'id':self.active_job_id},saved,failed,epoch=self.epoch)

    def error(self,error):
        if self.closed or not self.current_epoch():return
        self.mutating=False;self.applying=False;self.update_controls();self.status.setText(error.get('message',str(error)))

    def showEvent(self,event):
        super().showEvent(event);self.closed=False;self.timer.start()

    def closeEvent(self,event):
        self.closed=True;self.timer.stop();self._pending_connection_send=None;self.connection_send_timer.stop()
        if self._owns_codex_connection:self.codex_connection.request_stop()
        super().closeEvent(event)
