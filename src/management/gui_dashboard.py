"""A small home dashboard: today's position, this week and actionable warnings."""
from __future__ import annotations
from PySide6.QtCore import Signal,Qt
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QFrame,QScrollArea,QLabel,QProgressBar,QPushButton,QGridLayout
from .gui_workspace import plain_label,make_button,clear_layout


class DashboardPage(QScrollArea):
    error=Signal(object)
    review_attention=Signal(bool)
    def __init__(self,bridge,parent=None,on_today=None,on_timetable=None,on_projects=None,on_task=None):
        super().__init__(parent);self.bridge=bridge;self.on_today=on_today;self.on_timetable=on_timetable;self.on_projects=on_projects;self.on_task=on_task
        self.generation=0;self.past_generation=0;self.dead=False;self.day=None;self.past_offset=0;self.result={};self.destroyed.connect(lambda *_:setattr(self,'dead',True))
        self.setWidgetResizable(True);self.setFrameShape(QFrame.Shape.NoFrame)
        body=QWidget();self.body=QVBoxLayout(body);self.body.setContentsMargins(0,0,10,12);self.body.setSpacing(18);self.setWidget(body)
        hero=QHBoxLayout();texts=QVBoxLayout();self.date=plain_label('正在读取今天…','DashboardDate');self.position=plain_label('','DashboardWeek');texts.addWidget(self.date);texts.addWidget(self.position);hero.addLayout(texts,1)
        hero.addWidget(make_button('查看今天',lambda:self.open_today(self.day),True));self.body.addLayout(hero)
        cards=QHBoxLayout();self.metrics={}
        for key,title in [('plan','今日计划'),('warnings','当前警戒'),('tasks','任务概况')]:
            box=QFrame();box.setObjectName('CurrentWarningCard' if key=='warnings' else 'PlanCard');layout=QVBoxLayout(box);layout.setContentsMargins(16,14,16,14)
            layout.addWidget(plain_label(title,'SectionHeading'));value=plain_label('—','DashboardMetric');layout.addWidget(value);detail=plain_label('','Quiet');layout.addWidget(detail)
            progress=QProgressBar();progress.setTextVisible(False);progress.setMaximumHeight(8);layout.addWidget(progress);progress.setVisible(key!='warnings');self.metrics[key]=(value,detail,progress);cards.addWidget(box,1)
        self.body.addLayout(cards)
        head=QHBoxLayout();head.addWidget(plain_label('本周安排','SectionHeading'),1);head.addWidget(make_button('打开每周课表',lambda:self.on_timetable(self.day) if self.on_timetable else None));self.body.addLayout(head)
        self.week=QGridLayout();self.week.setSpacing(8);self.week_cards=[];self.body.addLayout(self.week)
        self.alert_box=QFrame();self.alert_box.setObjectName('CurrentWarningCard');alerts=QVBoxLayout(self.alert_box);alerts.setContentsMargins(16,14,16,14)
        self.alert_title=plain_label('需要留意','CurrentWarningTitle');alerts.addWidget(self.alert_title);self.alerts=QVBoxLayout();alerts.addLayout(self.alerts)
        self.all_alerts=make_button('在今天页查看全部警戒',lambda:self.open_today(self.day));alerts.addWidget(self.all_alerts);self.body.addWidget(self.alert_box)
        self.past_toggle=QPushButton('已过节点');self.past_toggle.setCheckable(True);self.past_toggle.clicked.connect(self.load_past);self.body.addWidget(self.past_toggle)
        self.past_box=QWidget();self.past_layout=QVBoxLayout(self.past_box);self.past_layout.setContentsMargins(12,0,12,0);self.past_box.hide();self.body.addWidget(self.past_box)
        head=QHBoxLayout();head.addWidget(plain_label('课程与项目进度','SectionHeading'),1);head.addWidget(make_button('查看课程与项目',lambda:self.on_projects(None) if self.on_projects else None));self.body.addLayout(head)
        self.owner_rows=QVBoxLayout();self.body.addLayout(self.owner_rows);self.body.addStretch()

    def open_today(self,day):
        if day and self.on_today:self.on_today(day)

    def refresh(self):
        self.generation+=1;generation=self.generation
        def loaded(result):
            if self.dead or generation!=self.generation:return
            self.result=result;self.day=result['date'];self.render(result)
            self.review_attention.emit(not result['plan'].get('can_review',result['plan']['has_plan']) or result['plan']['summary'].get('pending_review',0)>0)
            if self.past_toggle.isChecked():self.load_past()
        self.bridge.query('dashboard',loaded,lambda e:self.error.emit(e) if not self.dead and generation==self.generation else None)

    def render(self,r):
        self.date.setText(r['date_label']+'  '+r['weekday']);self.position.setText(r['week_label']+'  ·  '+r['week_start'][5:]+' — '+r['week_end'][5:])
        self.position.setToolTip('\n'.join(p['title']+'：'+p['label'] for p in r['periods']))
        daily=r['plan']['summary'];value,detail,progress=self.metrics['plan'];value.setText(f"{daily['done']} / {daily['total']}" if r['plan'].get('can_review',r['plan']['has_plan']) else '尚未安排');detail.setText('已完成 / 计划与固定安排' if r['plan'].get('can_review',r['plan']['has_plan']) else '进入今天，生成或手动安排');progress.setRange(0,max(1,daily['total']));progress.setValue(daily['done'])
        if daily.get('fixed_scheduled'):
            reported=daily['total']-daily['unreported'];value.setText(f"{reported} / {daily['total']}")
            detail.setText(f"已反馈 · 参加 {daily.get('attended',0)} 次 · 待补 {daily.get('catchup_needed',0)} 次");progress.setValue(reported)

        counts=r['warnings']['counts'];value,detail,_=self.metrics['warnings'];value.setText(str(counts['current'])+' 项');detail.setText('当前警戒与日期待核对事项')
        tasks=r['tasks'];value,detail,progress=self.metrics['tasks'];value.setText(str(tasks['open'])+' 项待办');detail.setText(str(tasks['done'])+' 项已完成，记录保留');progress.setRange(0,max(1,tasks['total']));progress.setValue(tasks['done'])
        clear_layout(self.week);self.week_cards=[]
        for day in r['days']:
            box=QFrame();box.setObjectName('DashboardToday' if day['is_today'] else 'PlanCard');column=QVBoxLayout(box);column.setContentsMargins(8,10,8,10)
            button=make_button(day['weekday']+' '+day['date'][5:].replace('-','/'),lambda _,d=day['date']:self.open_today(d));column.addWidget(button)
            for event in day['events'][:2]:
                label=(event.get('start') or '时间待核对')+' '+(event.get('owner_code') or event.get('owner_label') or '')
                kind={'lecture':'Lecture','tutorial':'Tutorial','lab':'Lab','exam':'考核'}.get(event.get('event_kind'),event['title'])
                text=plain_label(label+'\n'+kind,'Quiet');text.setMinimumWidth(0);column.addWidget(text)
            if not day['events']:column.addWidget(plain_label('无固定安排','Quiet'))
            if day['event_count']>2:column.addWidget(plain_label('另有 '+str(day['event_count']-2)+' 项','Quiet'))
            column.addStretch();self.week_cards.append(box)
        self.reflow_week()
        clear_layout(self.alerts)
        for item in r['warnings']['items']:self.warning_row(self.alerts,item)
        if not r['warnings']['items']:self.alerts.addWidget(plain_label('当前没有进入已配置警戒范围的事项。','Quiet'))
        self.alert_title.setText('当前需要留意 · '+str(counts['current'])+' 项');self.all_alerts.setVisible(r['warnings'].get('next_offset') is not None)
        self.past_toggle.setText(('收起' if self.past_toggle.isChecked() else '展开')+'已过节点的警戒 · '+str(counts['past'])+' 项');self.past_toggle.setVisible(counts['past']>0)
        if not counts['past']:self.past_toggle.setChecked(False);self.past_box.hide()
        clear_layout(self.owner_rows)
        for owner in r['owners']:
            row=QHBoxLayout();button=make_button(owner['label'],lambda _,o=owner['id']:self.on_projects(o) if self.on_projects else None);row.addWidget(button,2)
            bar=QProgressBar();bar.setRange(0,max(1,owner['total']));bar.setValue(owner['done']);bar.setTextVisible(False);bar.setMaximumHeight(9);row.addWidget(bar,3)
            row.addWidget(plain_label(str(owner['done'])+' / '+str(owner['total'])+' 已完成','Quiet'));self.owner_rows.addLayout(row)

    def reflow_week(self):
        if not hasattr(self,'week_cards') or not self.week_cards:return
        from .gui_theme import current_appearance
        minimum=140*current_appearance()['font_size']/13
        columns=7 if self.viewport().width()>=minimum*7 else 4 if self.viewport().width()>=minimum*4 else 3
        while self.week.count():self.week.takeAt(0)
        for index,card in enumerate(self.week_cards):self.week.addWidget(card,index//columns,index%columns)
        for index in range(7):self.week.setColumnStretch(index,1 if index<columns else 0)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.reflow_week()

    def warning_row(self,layout,item):
        row=QHBoxLayout();content=QVBoxLayout();title=item.get('display_title') or item['title'];owner=item.get('owner_label')
        content.addWidget(plain_label((owner+' · ' if owner and owner not in title else '')+title,'SectionHeading'));content.addWidget(plain_label(item.get('reason',''),'Body'));row.addLayout(content,1)
        if self.on_task:row.addWidget(make_button('查看',lambda _,i=item['id']:self.on_task(i)))
        layout.addLayout(row)

    def load_past(self,*_,offset=0):
        self.past_generation+=1;request=self.past_generation
        self.past_box.setVisible(self.past_toggle.isChecked())
        if not self.past_toggle.isChecked():return
        generation=self.generation
        def loaded(result):
            if self.dead or generation!=self.generation or request!=self.past_generation or not self.past_toggle.isChecked():return
            clear_layout(self.past_layout)
            for item in result['items']:self.warning_row(self.past_layout,item)
            controls=QHBoxLayout()
            if offset:controls.addWidget(make_button('上一页',lambda:self.load_past(offset=max(0,offset-5))))
            if result.get('next_offset') is not None:controls.addWidget(make_button('下一页',lambda:self.load_past(offset=result['next_offset'])))
            self.past_layout.addLayout(controls)
        self.bridge.query('warnings',loaded,lambda e:self.error.emit(e) if not self.dead and generation==self.generation and request==self.past_generation and self.past_toggle.isChecked() else None,date=self.day,group='past',limit=5,offset=offset)
