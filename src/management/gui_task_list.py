"""Open tasks first; completed history is collapsed and loaded on demand."""
from PySide6.QtCore import Signal,QDate,Qt
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QPushButton,QLabel,QFrame,QSizePolicy
from .gui_forms import label_status
from .gui_layout import ActionRow


class TaskListPanel(QWidget):
    expanded_changed=Signal(bool)
    def __init__(self,bridge,owner,parent=None,on_open=None,on_edit=None,initial=None,expanded=False,on_saved=None):
        super().__init__(parent)
        self.bridge,self.owner,self.on_open,self.on_edit=bridge,owner,on_open,on_edit
        self.on_saved=on_saved;self.pending=set()
        self.snapshot_epoch=bridge.epoch;self.snapshot_revision=bridge.revision
        self.dead=False;self.destroyed.connect(lambda *_:setattr(self,'dead',True))
        self.offsets={'open':0,'done':0};self.loading={'open':False,'done':False};self.loaded_done=False
        self.ids={'open':set(),'done':set()}
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        self.open_body=QWidget();self.open_layout=QVBoxLayout(self.open_body);self.open_layout.setContentsMargins(0,0,0,0);layout.addWidget(self.open_body)
        self.empty=QLabel('正在读取任务…');self.empty.setObjectName('Quiet');self.open_layout.addWidget(self.empty)
        self.open_more=QPushButton('加载更多待办');self.open_more.clicked.connect(lambda:self.load('open'));self.open_more.hide();layout.addWidget(self.open_more)
        self.toggle=QPushButton('已完成（0） · 展开');self.toggle.setObjectName('CompletedTasksToggle');self.toggle.setCheckable(True);self.toggle.setChecked(expanded);self.toggle.toggled.connect(self.set_expanded);self.toggle.hide();layout.addWidget(self.toggle)
        self.done_body=QWidget();self.done_layout=QVBoxLayout(self.done_body);self.done_layout.setContentsMargins(0,0,0,0);layout.addWidget(self.done_body);self.done_body.setVisible(expanded)
        self.done_more=QPushButton('加载更多已完成');self.done_more.clicked.connect(lambda:self.load('done'));self.done_more.hide();self.done_layout.addWidget(self.done_more)
        self.error=QLabel();self.error.setWordWrap(True);self.error.setObjectName('Error');self.error.hide();layout.addWidget(self.error)
        self.done_count=0
        if initial is not None:
            # Compatibility for a directly rendered snapshot (and old services).
            tasks=[x for x in initial.get('children',[]) if x.get('type')=='task']
            states=initial.get('task_states',{})
            for task in tasks:task={**task,'completion_state':states.get(task['id'],'done' if task.get('status')=='done' else None)};self.add_task(task)
            self.done_count=len(self.ids['done']);self.loaded_done=True;self.offsets={'open':None,'done':None};self.update_header()
        else:self.load('open')

    def update_header(self):
        self.toggle.setVisible(bool(self.done_count));self.toggle.setText(f"已完成（{self.done_count}） · "+('收起' if self.toggle.isChecked() else '展开'))
        self.empty.setVisible(not self.ids['open']);self.empty.setText('当前没有待办任务。' if self.done_count else '还没有实际任务。')

    def set_expanded(self,value):
        self.done_body.setVisible(value);self.update_header();self.expanded_changed.emit(value)
        if value and not self.loaded_done:self.load('done')

    def load(self,group):
        if self.dead or self.loading[group] or self.offsets[group] is None:return
        self.loading[group]=True
        button=self.open_more if group=='open' else self.done_more;button.setEnabled(False)
        def loaded(result):
            if self.dead:return
            self.loading[group]=False;self.error.hide()
            self.snapshot_epoch=result.get("epoch",self.bridge.epoch);self.snapshot_revision=result.get("revision",self.bridge.revision)
            for task in result.get('items',[]):self.add_task(task)
            self.offsets[group]=result.get('next_offset');button.setVisible(self.offsets[group] is not None);button.setEnabled(True)
            self.done_count=result.get('counts',{}).get('done',0)
            if group=='done':self.loaded_done=True
            self.update_header()
            if group=='open' and self.toggle.isChecked() and not self.loaded_done:self.load('done')
        def failed(error):
            if self.dead:return
            self.loading[group]=False;button.setEnabled(True);button.show();self.error.setText(error.get('message',str(error)));self.error.show()
        self.bridge.query('workspace_tasks',loaded,failed,id=self.owner['id'],group=group,offset=self.offsets[group],limit=30)

    def add_task(self,task):
        state=task.get('completion_state');group='done' if state=='done' else 'open'
        if task['id'] in self.ids[group]:return
        self.ids[group].add(task['id'])
        row=QFrame();row.setObjectName('CompletedTaskRow' if group=='done' else 'TaskRow');inner=QVBoxLayout(row);inner.setContentsMargins(15,12,15,12)
        title=QLabel(task.get('display_title') or task['title']);title.setTextFormat(Qt.TextFormat.PlainText);title.setWordWrap(True);title.setObjectName('SectionHeading');inner.addWidget(title)
        owner=QLabel(task.get('owner_label') or self.owner.get('data',{}).get('code') or self.owner['title']);owner.setTextFormat(Qt.TextFormat.PlainText);owner.setObjectName('Quiet');owner.setWordWrap(True);inner.addWidget(owner)
        actions=ActionRow();inner.addWidget(actions)
        mark=QPushButton('✓' if group=='done' else '○');mark.setObjectName('TaskMark');mark.setFixedWidth(36);mark.setToolTip('标为未完成' if group=='done' else '标为完成');mark.setAccessibleName(task['title']+'：'+mark.toolTip());mark.setEnabled(task['id'] not in self.pending);mark.clicked.connect(lambda _,e=task,b=mark:self.complete(e,'incomplete' if group=='done' else 'done',b));actions.addWidget(mark)
        open_button=QPushButton('查看');open_button.setAccessibleName('查看任务：'+task['title']);open_button.clicked.connect(lambda _,e=task:self.on_open(e) if self.on_open else None);actions.addWidget(open_button)
        if task.get('data',{}).get('catchup_enabled'):actions.addWidget(QLabel('补课 / 补欠'))
        if task.get('data',{}).get('due_date'):actions.addWidget(QLabel('截止 '+task['data']['due_date']))
        label={'done':'已完成','incomplete':'未完成','partial':'部分完成','not_started':'未开始','blocked':'受阻'}.get(state,label_status(task.get('status')))
        pill=QLabel(label);pill.setObjectName('StatusPill');actions.addWidget(pill)
        if self.on_edit:
            edit=QPushButton('编辑任务');edit.clicked.connect(lambda _,e=task:self.on_edit(e));actions.addWidget(edit)
        if group=='done':self.done_layout.insertWidget(max(0,self.done_layout.count()-1),row)
        else:self.open_layout.addWidget(row)

    def complete(self,task,result,button):
        if self.dead or task['id'] in self.pending:return
        self.pending.add(task['id']);button.setEnabled(False)
        payload={'target_id':task['id'],'target_version':task['version'],'business_date':QDate.currentDate().toString('yyyy-MM-dd'),'result':result}
        def saved(receipt):
            self.pending.discard(task['id'])
            if self.dead:return
            if self.on_saved:self.on_saved(receipt)
            else:self.reload()
        def failed(error):
            self.pending.discard(task['id'])
            if self.dead:return
            button.setEnabled(True);self.error.setText(error.get('message',str(error))+' 请重新打开课程读取最新状态后再试。');self.error.show()
        self.bridge.command('set_task_completion',payload,saved,failed,epoch=self.snapshot_epoch,expected_revision=self.snapshot_revision)

    def reload(self):
        from .gui_workspace import clear_layout
        clear_layout(self.open_layout)
        for index in range(self.done_layout.count()-2,-1,-1):
            item=self.done_layout.takeAt(index)
            if item.widget():item.widget().deleteLater()
        self.empty=QLabel('正在读取任务…');self.empty.setObjectName('Quiet');self.open_layout.addWidget(self.empty)
        self.ids={'open':set(),'done':set()};self.offsets={'open':0,'done':0};self.loaded_done=False;self.open_more.hide();self.done_more.hide();self.load('open')
