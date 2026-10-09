"""Term-week configuration is maintained here, away from the import form."""
from __future__ import annotations
import datetime as dt
from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QComboBox,QFrame,QLayout,QSizePolicy,
 QCheckBox,QDateEdit,QListWidget,QListWidgetItem,QPushButton,QLabel,QMessageBox,QScrollArea)
from shiboken6 import isValid
from .gui_calendar import install_calendar
from .gui_layout import ActionRow
from .gui_theme import bind_theme


class TimetableSettingsPage(QWidget):
    def __init__(self, bridge, parent=None, on_changed=None):
        super().__init__(parent)
        self.bridge,self.on_changed=bridge,on_changed
        self.generation=0;self.pending=False;self.loading=False;self.dirty=False;self.ready=False
        self.entity=None;self.rows=[];self.selected_id=None;self.epoch=self.revision=None;self.table_offset=0
        outer=QVBoxLayout(self);outer.setContentsMargins(0,0,0,0)
        self.scroll=QScrollArea();self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame);self.scroll.setMinimumSize(0,0)
        body=QWidget();layout=QVBoxLayout(body);layout.setSpacing(22)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.scroll.setWidget(body);outer.addWidget(self.scroll,1)

        def section(title):
            group=QWidget();content=QVBoxLayout(group);content.setContentsMargins(0,0,0,0);content.setSpacing(9)
            heading=QLabel(title);heading.setObjectName('SectionHeading');content.addWidget(heading)
            layout.addWidget(group)
            return content

        scope_section=section('维护范围')
        self.scope=QComboBox();self.scope.addItem('新课表的默认设置',None)
        self.scope.setAccessibleName('要维护的课表')
        self.scope.setMinimumContentsLength(14)
        self.scope.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.more=QPushButton('更多课表');self.more.hide()
        scope_actions=ActionRow();scope_actions.addWidget(self.scope);scope_actions.addWidget(self.more)
        scope_section.addWidget(scope_actions)
        self.hint=QLabel();self.hint.setWordWrap(True);self.hint.setObjectName('Hint');scope_section.addWidget(self.hint)

        time_section=section('时间与教学周')
        self.timezone=QComboBox();self.timezone.setEditable(True);self.timezone.setAccessibleName('时区')
        self.timezone.setMinimumContentsLength(17)
        self.timezone.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.timezone.addItems(['Asia/Shanghai','Asia/Singapore','UTC','Asia/Tokyo','Europe/London','America/New_York'])
        timezone_row=ActionRow();timezone_row.addWidget(QLabel('时区'));timezone_row.addWidget(self.timezone)
        time_section.addWidget(timezone_row)
        self.skip_recess=QCheckBox('停课周不计入教学周');self.skip_recess.setChecked(True);time_section.addWidget(self.skip_recess)

        recess_section=section('停课周')
        text=QLabel('按校历添加停课周（Recess week）。该周不安排课程；启用上方规则时，假期后继续计算教学周。')
        text.setWordWrap(True);text.setObjectName('Hint');recess_section.addWidget(text)
        dates=ActionRow();self.recess_date=QDateEdit(QDate.currentDate());install_calendar(self.recess_date)
        self.recess_date.setDisplayFormat('yyyy/MM/dd');self.recess_date.setAccessibleName('停课周内任意一天')
        dates.addWidget(self.recess_date);self.add=QPushButton('加入这一周');dates.addWidget(self.add);recess_section.addWidget(dates)
        self.weeks=QListWidget();self.weeks.setAccessibleName('已设置的停课周')
        recess_section.addWidget(self.weeks)
        self.remove=QPushButton('移除选中周');remove_row=ActionRow();remove_row.addWidget(self.remove);recess_section.addWidget(remove_row)
        self.effect=QLabel();self.effect.setWordWrap(True);self.effect.setObjectName('Hint');recess_section.addWidget(self.effect)
        layout.addStretch()
        self.status=QLabel();self.status.setWordWrap(True);outer.addWidget(self.status)
        self.actions=ActionRow();self.save_button=QPushButton('保存课表设置');self.save_button.setObjectName('Primary');self.actions.addWidget(self.save_button)
        self.reload_button=QPushButton('重新读取');self.actions.addWidget(self.reload_button);outer.addWidget(self.actions)
        for control in (self.scope,self.timezone,self.recess_date):
            control.setSizePolicy(QSizePolicy.Policy.Maximum,QSizePolicy.Policy.Fixed)
        self.scope.currentIndexChanged.connect(self.scope_changed);self.timezone.currentTextChanged.connect(self.edited);self.skip_recess.toggled.connect(self.edited)
        self.add.clicked.connect(self.add_week);self.remove.clicked.connect(self.remove_week);self.save_button.clicked.connect(self.save);self.reload_button.clicked.connect(self.reload);self.more.clicked.connect(self.load_tables)
        self.started=False;self.enable(False)
        bind_theme(self,self.fit_fields);self.fit_fields()

    def fit_fields(self):
        # Keep fields legible after a live font change without stretching them
        # across the panel. ActionRow wraps their neighbouring buttons as needed.
        for control in (self.scope,self.timezone,self.recess_date):
            control.setMinimumWidth(0)
            control.setMinimumHeight(control.fontMetrics().height()+18)
            control.updateGeometry()
        row_width=self.weeks.fontMetrics().horizontalAdvance('2030-01-21 — 2030-01-27')+52
        self.weeks.setMinimumWidth(0);self.weeks.setMaximumWidth(row_width)
        self.weeks.setMaximumHeight(max(120,self.weeks.fontMetrics().height()*5+20))
        self.scroll.widget().updateGeometry();self.actions.updateGeometry()

    def showEvent(self,event):
        super().showEvent(event)
        if not self.started:
            self.started=True;self.load_tables();self.load()

    def select_timetable(self, identifier):
        self.selected_id=identifier
        index=self.scope.findData(identifier)
        if index<0:self.scope.addItem('当前课表',identifier);index=self.scope.count()-1
        self.scope.blockSignals(True);self.scope.setCurrentIndex(index);self.scope.blockSignals(False)
        if self.started:self.load()

    def edited(self,*_):
        if not self.loading:self.dirty=True

    def error(self,value):
        if not isValid(self):return
        self.pending=False;self.enable(True)
        self.status.setText(value.get('message',str(value)) if isinstance(value,dict) else str(value))

    def enable(self,enabled):
        for widget in (self.scope,self.timezone,self.skip_recess,self.recess_date,self.add,self.remove,self.weeks,self.reload_button):widget.setEnabled(enabled)
        self.save_button.setEnabled(enabled and self.ready)

    def discard(self):
        return not self.dirty or QMessageBox.question(self,'未保存的设置','放弃本页尚未保存的修改？',QMessageBox.StandardButton.Discard|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)==QMessageBox.StandardButton.Discard

    def scope_changed(self,*_):
        if not self.discard():
            self.scope.blockSignals(True);self.scope.setCurrentIndex(max(0,self.scope.findData(self.selected_id)));self.scope.blockSignals(False);return
        self.selected_id=self.scope.currentData();self.load()

    def reload(self):
        if not self.pending and self.discard():self.load()

    def load_tables(self):
        def loaded(result):
            if not isValid(self):return
            self.scope.blockSignals(True)
            for entity in result.get('items',[]):
                if self.scope.findData(entity['id'])<0:self.scope.addItem(entity['title'],entity['id'])
                else:self.scope.setItemText(self.scope.findData(entity['id']),entity['title'])
            self.scope.blockSignals(False);self.table_offset=result.get('next_offset');self.more.setVisible(self.table_offset is not None)
        self.bridge.query('timetables',loaded,self.error,limit=100,offset=self.table_offset or 0)

    def load(self):
        self.generation+=1;generation=self.generation;self.ready=False;self.enable(False);self.status.setText('正在读取…')
        selected=self.selected_id
        def loaded(result):
            if not isValid(self) or generation!=self.generation:return
            self.epoch,self.revision=result.get('epoch',self.bridge.epoch),result.get('revision',getattr(self.bridge,'revision',None))
            self.loading=True;self.entity=None;self.rows=[]
            if selected:
                self.entity=result.get('entity') or result['items'][0];self.rows=result.get('rows',[]);data=self.entity['data']
                config={'week_numbering':data.get('week_numbering','calendar'),'recess_weeks':data.get('recess_weeks',[])};zone=data.get('timezone','')
                self.hint.setText('当前课表：'+self.entity['title'])
                self.effect.setText('保存会同步这份课表的固定时段；已生成的准备任务和历史记录保留。旧课表不会仅因默认设置变化就改变周号。')
            else:
                settings=result['settings'];config=settings.get('timetable_defaults',{'week_numbering':'teaching','recess_weeks':[]});zone=settings['timezone']
                self.hint.setText('新课表使用这里的规则。时区也用于软件的每日安排。')
                self.effect.setText('假期可以提前配置多个学期，导入时仅使用所选范围内的日期。已有课表需要在上方选择后单独保存。')
            self.timezone.setCurrentText(zone);self.skip_recess.setChecked(config.get('week_numbering','teaching')=='teaching');self.weeks.clear()
            for day in config.get('recess_weeks',[]):self.insert_week(day)
            self.loading=False;self.dirty=False;self.ready=True;self.status.setText('');self.enable(True)
        if selected:self.bridge.query('timetables',loaded,self.error,id=selected)
        else:self.bridge.query('settings',loaded,self.error)

    def insert_week(self,day):
        start=dt.date.fromisoformat(day);item=QListWidgetItem(day+' — '+(start+dt.timedelta(days=6)).isoformat());item.setData(Qt.ItemDataRole.UserRole,day);self.weeks.addItem(item)

    def add_week(self):
        value=self.recess_date.date().toPython();value-=dt.timedelta(days=value.weekday());day=value.isoformat()
        if any(self.weeks.item(i).data(Qt.ItemDataRole.UserRole)==day for i in range(self.weeks.count())):return
        self.insert_week(day);self.weeks.sortItems();self.edited()

    def remove_week(self):
        index=self.weeks.currentRow()
        if index>=0:self.weeks.takeItem(index);self.edited()

    def save(self):
        if self.pending or not self.ready:return
        if self.epoch!=self.bridge.epoch:self.error('数据空间已经改变，请重新读取后再保存。');return
        from .timetable import normalize_timetable_defaults
        try:config=normalize_timetable_defaults({'week_numbering':'teaching' if self.skip_recess.isChecked() else 'calendar','recess_weeks':[self.weeks.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.weeks.count())]})
        except Exception as error:self.error(str(error));return
        if self.entity and self.rows:
            from .gui_timetable import timetable_payload
            payload=timetable_payload(self.entity,self.rows)
            payload.update(timezone=self.timezone.currentText().strip(),**config);name='apply_timetable'
        elif self.entity:
            name='update';payload={'id':self.entity['id'],'version':self.entity['version'],'patch':{'data':{'timezone':self.timezone.currentText().strip(),**config}}}
        else:
            name='settings';payload={'settings':{'timezone':self.timezone.currentText().strip(),'timetable_defaults':config}}
        self.pending=True;self.enable(False);generation=self.generation
        def saved(receipt):
            if not isValid(self) or generation!=self.generation:return
            self.pending=False;self.dirty=False
            if self.on_changed:self.on_changed()
            self.load();self.status.setText('已保存。')
        self.bridge.command(name,payload,saved,self.error,epoch=self.epoch,expected_revision=self.revision)
