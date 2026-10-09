"""A small home dashboard: today's position, this week and actionable warnings."""
from __future__ import annotations
from PySide6.QtCore import QEvent,Signal,Qt,QTimer
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QBoxLayout,QFrame,QScrollArea,QLabel,QLayout,QProgressBar,QPushButton,QGridLayout,QSizePolicy
from .gui_workspace import plain_label,make_button,clear_layout
from .gui_charts import CoverageChart
from .chart_preferences import normalize_chart_preferences
from .gui_layout import ActionRow, ReadingLabel


class DashboardPage(QScrollArea):
    error=Signal(object)
    review_attention=Signal(bool)
    chart_settings_requested=Signal(str)
    def __init__(self,bridge,parent=None,on_today=None,on_timetable=None,on_projects=None,on_task=None,on_chart_settings=None):
        super().__init__(parent);self.bridge=bridge;self.on_today=on_today;self.on_timetable=on_timetable;self.on_projects=on_projects;self.on_task=on_task
        self.generation=0;self.past_generation=0;self.dead=False;self.day=None;self.past_offset=0;self.result={};self.destroyed.connect(lambda *_:setattr(self,'dead',True))
        self.chart_preferences=normalize_chart_preferences()
        self._layout_pending=False;self._relayout_active=False
        self._layout_timer=QTimer(self);self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._apply_metric_layout)
        if on_chart_settings:self.chart_settings_requested.connect(on_chart_settings)
        self.setWidgetResizable(True);self.setFrameShape(QFrame.Shape.NoFrame)
        body=QWidget();self.body=QVBoxLayout(body);self.body.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize);self.body.setContentsMargins(0,0,10,12);self.body.setSpacing(18);self.setWidget(body)
        hero=QHBoxLayout();texts=QVBoxLayout();self.date=plain_label('正在读取今天…','DashboardDate');self.position=plain_label('','DashboardWeek');texts.addWidget(self.date);texts.addWidget(self.position);hero.addLayout(texts,1)
        hero.addWidget(make_button('查看今天',lambda:self.open_today(self.day),True));self.body.addLayout(hero)
        self.metric_panel=QWidget();self.metric_panel.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Minimum)
        self.metric_grid=QGridLayout(self.metric_panel);self.metric_grid.setContentsMargins(0,0,0,0);self.metric_grid.setSpacing(18)
        self.metric_grid.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.metric_cards=[];self.metrics={};self.charts={}
        for key,title in [('plan','今日计划'),('warnings','当前警戒'),('tasks','任务概况')]:
            box=QFrame();box.setObjectName('CurrentWarningCard' if key=='warnings' else 'PlanCard');layout=QVBoxLayout(box);layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize);layout.setContentsMargins(16,14,16,14)
            heading=QHBoxLayout();heading.addWidget(plain_label(title,'SectionHeading'),1)
            if key!='warnings':
                preference='dashboard_today_style' if key=='plan' else 'dashboard_tasks_style'
                button=make_button('图表样式…',lambda _,name=preference:self.chart_settings_requested.emit(name))
                button.setObjectName('QuietButton');button.setAccessibleName('设置'+title+'图表样式');heading.addWidget(button)
            layout.addLayout(heading);value=plain_label('—','DashboardMetric');layout.addWidget(value);detail=plain_label('','Quiet');layout.addWidget(detail)
            chart=None
            if key!='warnings':
                chart=CoverageChart();chart.heading.hide();layout.addWidget(chart);self.charts[key]=chart
                chart.geometry_changed.connect(self._relayout_metric_cards)
            layout.addStretch();self.metrics[key]=(value,detail,chart);self.metric_cards.append(box)
        self.body.addWidget(self.metric_panel)
        self.reflow_metrics()
        head=QHBoxLayout();head.addWidget(plain_label('本周固定日程','SectionHeading'),1);head.addWidget(make_button('查看每周日程',lambda:self.on_timetable(self.day) if self.on_timetable else None));self.body.addLayout(head)
        self.week=QGridLayout();self.week.setSpacing(8);self.week_cards=[];self.body.addLayout(self.week)
        self.details_panel=QWidget();self.details_grid=QGridLayout(self.details_panel);self.details_grid.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize);self.details_grid.setContentsMargins(0,10,0,0);self.details_grid.setHorizontalSpacing(36);self.details_grid.setVerticalSpacing(24);self.body.addWidget(self.details_panel)
        self.attention_section=QWidget();attention=QVBoxLayout(self.attention_section);attention.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize);attention.setContentsMargins(0,0,0,0);attention.setSpacing(12)
        self.alert_box=QFrame();self.alert_box.setObjectName('CurrentWarningCard');alerts=QVBoxLayout(self.alert_box);alerts.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize);alerts.setContentsMargins(16,14,16,14)
        self.alert_title=ReadingLabel('需要留意','CurrentWarningTitle');alerts.addWidget(self.alert_title);self.alerts=QVBoxLayout();alerts.addLayout(self.alerts)
        self.all_alerts=make_button('在今天页查看全部警戒',lambda:self.open_today(self.day));alerts.addWidget(self.all_alerts);attention.addWidget(self.alert_box)
        self.past_toggle=QPushButton('已过节点');self.past_toggle.setCheckable(True);self.past_toggle.clicked.connect(self.load_past);attention.addWidget(self.past_toggle)
        self.past_box=QWidget();self.past_layout=QVBoxLayout(self.past_box);self.past_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize);self.past_layout.setContentsMargins(12,0,12,0);self.past_box.hide();attention.addWidget(self.past_box);attention.addStretch()
        self.owner_section=QWidget();owners=QVBoxLayout(self.owner_section);owners.setContentsMargins(0,0,0,0);owners.setSpacing(16);owners.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.owner_heading=QVBoxLayout();self.owner_heading.addWidget(plain_label('课程与项目进度','SectionHeading'))
        self.owner_open=make_button('查看课程与项目',lambda:self.on_projects(None) if self.on_projects else None);self.owner_heading.addWidget(self.owner_open);owners.addLayout(self.owner_heading)
        self.owner_rows=QVBoxLayout();self.owner_rows.setSpacing(16);owners.addLayout(self.owner_rows);owners.addStretch();self.body.addStretch()
        from .gui_theme import bind_theme
        bind_theme(self,self.apply_visual_style)

    def open_today(self,day):
        if day and self.on_today:self.on_today(day)

    def set_chart_preferences(self,preferences):
        """Only repaint existing cards; filters, warnings and business state stay intact."""
        self.chart_preferences=normalize_chart_preferences(preferences,current=self.chart_preferences)
        self.charts['plan'].set_style(self.chart_preferences['dashboard_today_style'])
        self.charts['tasks'].set_style(self.chart_preferences['dashboard_tasks_style'])
        self._relayout_metric_cards()

    def _relayout_metric_cards(self):
        if not hasattr(self,'metric_cards') or self.dead:return
        self._layout_pending=True
        if not self._relayout_active and not self._layout_timer.isActive():self._layout_timer.start(0)

    def _apply_metric_layout(self):
        if self.dead:return
        # A chart can change height during the grid's resize pass. Re-entering
        # activate() there loses the new row height; settle it next event turn.
        self._layout_pending=False
        self._relayout_active=True
        try:
            for card in self.metric_cards:
                card.layout().invalidate();card.layout().activate();card.updateGeometry()
            # QGridLayout's height-for-width pass can otherwise propose a row
            # shorter than a resized card. Rebuild row bounds from live content
            # in both directions so switching back to bars can still shrink.
            for row in range(self.metric_grid.rowCount()):self.metric_grid.setRowMinimumHeight(row,0)
            for index in range(self.metric_grid.count()):
                card=self.metric_grid.itemAt(index).widget();row=self.metric_grid.getItemPosition(index)[0]
                minimum=max(card.minimumHeight(),card.minimumSizeHint().height(),card.layout().totalHeightForWidth(max(1,card.width())))
                self.metric_grid.setRowMinimumHeight(row,max(self.metric_grid.rowMinimumHeight(row),minimum))
            self.metric_grid.invalidate()
            required=max(self.metric_grid.minimumSize().height(),self.metric_grid.totalHeightForWidth(max(1,self.metric_panel.width())))
            self.metric_panel.setMinimumHeight(required);self.metric_panel.updateGeometry()
            self.body.invalidate();self.body.activate();self.metric_grid.activate()
            self.widget().updateGeometry()
        finally:self._relayout_active=False
        if self._layout_pending:self._layout_timer.start(0)

    def reflow_metrics(self):
        if not hasattr(self,'metric_cards'):return
        from .gui_theme import current_appearance
        minimum=max(round(270*current_appearance()['font_size']/13),self.fontMetrics().horizontalAdvance('今日计划  图表样式…')+80)
        minimum=max(minimum,round(340*current_appearance()['font_size']/13))
        maximum_columns=2
        columns=maximum_columns if self.viewport().width()>=minimum*maximum_columns else 1
        while self.metric_grid.count():self.metric_grid.takeAt(0)
        plan,warning,tasks=self.metric_cards
        self.metric_grid.addWidget(plan,0,0)
        self.metric_grid.addWidget(tasks,0 if columns==2 else 1,1 if columns==2 else 0)
        self.metric_grid.addWidget(warning,1 if columns==2 else 2,0,1,columns,Qt.AlignmentFlag.AlignLeft)
        warning.layout().setDirection(QBoxLayout.Direction.LeftToRight)
        warning.setObjectName('WarningSummary');warning.layout().setContentsMargins(0,2,0,2)
        warning.setSizePolicy(QSizePolicy.Policy.Maximum,QSizePolicy.Policy.Preferred)
        self.metrics['warnings'][0].setObjectName('SectionHeading')
        self.metrics['warnings'][1].setVisible(False)
        for index in range(3):self.metric_grid.setColumnStretch(index,1 if index<columns else 0)
        self._relayout_metric_cards()

    def apply_visual_style(self):
        if not hasattr(self,'owner_section'):return
        self.owner_heading.setDirection(QBoxLayout.Direction.TopToBottom)
        self.owner_heading.setStretch(0,0)
        self.alert_box.setObjectName('ContentSection')
        self.alert_box.layout().setContentsMargins(0,0,0,0)
        for widget,layout in ((self.all_alerts,self.alert_box.layout()),(self.past_toggle,self.attention_section.layout()),(self.owner_open,self.owner_heading)):
            widget.setSizePolicy(QSizePolicy.Policy.Maximum,QSizePolicy.Policy.Fixed)
            layout.setAlignment(widget,Qt.AlignmentFlag.AlignLeft)
            widget.setObjectName('QuietButton')
        self.reflow_details()
        self.reflow_metrics()
        for widget in (self.alert_box,self.metric_cards[1],self.metrics['warnings'][0]):
            widget.style().unpolish(widget);widget.style().polish(widget);widget.update()

    def reflow_details(self):
        if not hasattr(self,'owner_section'):return
        from .gui_theme import current_appearance
        columns=2 if self.viewport().width()>=round(920*current_appearance()['font_size']/13) else 1
        while self.details_grid.count():self.details_grid.takeAt(0)
        # Let the grid supply each section's full height-for-width geometry.
        # AlignTop clamps a nested widget to its unwrapped sizeHint; the
        # stretches inside the sections already keep their contents at the top.
        self.details_grid.addWidget(self.attention_section,0,0)
        self.details_grid.addWidget(self.owner_section,0 if columns==2 else 1,1 if columns==2 else 0)
        self.details_grid.setColumnStretch(0,3 if columns==2 else 1);self.details_grid.setColumnStretch(1,2 if columns==2 else 0)

    def refresh(self):
        self.generation+=1;generation=self.generation
        def loaded(result):
            if self.dead or generation!=self.generation:return
            self.result=result;self.day=result['date'];self.render(result)
            self.review_attention.emit(not result['plan'].get('can_review',result['plan']['has_plan']) or result['plan']['summary'].get('pending_review',0)>0)
            if self.past_toggle.isChecked():self.load_past()
        self.bridge.query('dashboard',loaded,lambda e:self.error.emit(e) if not self.dead and generation==self.generation else None)

    def render(self,r):
        self.result=r
        self.date.setText(r['date_label']+'  '+r['weekday']);self.position.setText((r['week_label'] if r['periods'] else '本周')+'  ·  '+r['week_start'][5:]+' — '+r['week_end'][5:])
        self.position.setToolTip('\n'.join(p['title']+'：'+p['label'] for p in r['periods']))
        daily=r['plan']['summary'];value,detail,chart=self.metrics['plan'];value.setText(f"{daily['done']} / {daily['total']}" if r['plan'].get('can_review',r['plan']['has_plan']) else '尚未安排');detail.setText('已完成 / 计划与固定安排' if r['plan'].get('can_review',r['plan']['has_plan']) else '进入今天，生成或手动安排');chart.set_summary(daily,denominator='今日计划与固定安排；任务完成和出勤分别统计')
        if daily.get('fixed_scheduled'):
            reported=daily['total']-daily['unreported'];value.setText(f"{reported} / {daily['total']}")
            detail.setText(f"已反馈 · 参加 {daily.get('attended',0)} 次 · 待补 {daily.get('catchup_needed',0)} 次")

        counts=r['warnings']['counts'];value,detail,_=self.metrics['warnings'];value.setText(str(counts['current'])+' 项');detail.setText('当前警戒与日期待核对事项')
        tasks=r['tasks'];value,detail,chart=self.metrics['tasks'];value.setText(str(tasks['open'])+' 项待办');detail.setText(str(tasks['done'])+' 项已完成，记录保留')
        chart.set_summary({'total':tasks['total'],'done':tasks['done'],'breakdown':[
            {'key':'done','kind':'task','result':'done','count':tasks['done']},
            {'key':'open','kind':'task','result':'unknown','count':tasks['open'],'label':'任务 · 待办（含待反馈）'},
        ]},denominator='全部活动任务；待办不等于失败')
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
        self._render_detail_rows(r)
        self._relayout_metric_cards()

    def _render_detail_rows(self,r):
        counts=r['warnings']['counts']
        clear_layout(self.alerts)
        for item in r['warnings']['items']:self.warning_row(self.alerts,item)
        if not r['warnings']['items']:self.alerts.addWidget(plain_label('当前没有进入已配置警戒范围的事项。','Quiet'))
        self.alert_title.setText('当前需要留意 · '+str(counts['current'])+' 项');self.all_alerts.setVisible(r['warnings'].get('next_offset') is not None)
        self.past_toggle.setText(('收起' if self.past_toggle.isChecked() else '展开')+'已过节点的警戒 · '+str(counts['past'])+' 项');self.past_toggle.setVisible(counts['past']>0)
        if not counts['past']:self.past_toggle.setChecked(False);self.past_box.hide()
        clear_layout(self.owner_rows)
        for owner in r['owners']:
            row=QVBoxLayout()
            button=make_button(owner['label'],lambda _,o=owner['id']:self.on_projects(o) if self.on_projects else None)
            summary=plain_label(str(owner['done'])+' / '+str(owner['total'])+' 已完成','Quiet')
            # Owner names are user text, not short action captions. Keeping
            # them in a capped single-line button silently cut off the name.
            row.addWidget(ReadingLabel(owner['label'],'SectionHeading'))
            button.setText('查看');button.setObjectName('OwnerOpen')
            button.setAccessibleName('查看课程或项目：'+owner['label'])
            actions=ActionRow();actions.addWidget(summary);actions.addWidget(button)
            row.addWidget(actions)
            bar=QProgressBar();bar.setRange(0,max(1,owner['total']));bar.setValue(owner['done']);bar.setTextVisible(False);bar.setMaximumHeight(9)
            bar.setObjectName('OwnerProgress');bar.setFixedHeight(6);bar.setMaximumWidth(280);row.addWidget(bar)
            self.owner_rows.addLayout(row)

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
        self.reflow_metrics()
        self.reflow_week()
        self.reflow_details()

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange,QEvent.Type.ApplicationFontChange,QEvent.Type.StyleChange}:
            self.reflow_metrics()
            self.reflow_week()
            self.reflow_details()

    def warning_row(self,layout,item):
        row=QVBoxLayout();row.setSpacing(4)
        title=item.get('display_title') or item['title']
        caption=ReadingLabel(title,'SectionHeading');row.addWidget(caption)
        if item.get('owner_label') and item['owner_label'] not in title:row.addWidget(ReadingLabel(item['owner_label'],'Quiet'))
        row.addWidget(ReadingLabel(item.get('reason',''),'Body'))
        if self.on_task:
            button=make_button('查看事项  ›',lambda _,i=item['id']:self.on_task(i));button.setObjectName('QuietButton')
            button.setSizePolicy(QSizePolicy.Policy.Maximum,QSizePolicy.Policy.Fixed);row.addWidget(button,0,Qt.AlignmentFlag.AlignLeft)
        row.addSpacing(10);layout.addLayout(row)

    def load_past(self,*_,offset=0):
        self.past_generation+=1;request=self.past_generation
        self.past_box.setVisible(self.past_toggle.isChecked())
        self._relayout_metric_cards()
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
            self._relayout_metric_cards()
        self.bridge.query('warnings',loaded,lambda e:self.error.emit(e) if not self.dead and generation==self.generation and request==self.past_generation and self.past_toggle.isChecked() else None,date=self.day,group='past',limit=5,offset=offset)
