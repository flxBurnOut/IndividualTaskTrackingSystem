"""Dashboard date projections, current totals and asynchronous time refresh."""
import os,datetime as dt
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from types import SimpleNamespace
import pytest
from PySide6.QtCore import QDate,QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication,QLabel,QDateEdit
from management.core import Core
from management.gui_dashboard import DashboardPage
from management.gui_today import TodayPage
from management.gui import MainWindow
from test_ux_workflows_v2 import ControlledBridge,cmd

@pytest.fixture(scope='session')
def app():return QApplication.instance() or QApplication([])

def create(core,kind,title,**p):return cmd(core,'create',{'type':kind,'title':title,**p})['result']['entity']

def test_week_and_today_show_reschedule_and_timezone_projected_clock(app,tmp_path):
    core=Core(tmp_path)
    create(core,'event','Changed class',data={'date':'2030-01-02','start':'09:00','end':'10:00','timezone':'Asia/Shanghai','location':'New room','exceptions':{'2030-01-02':{'start':'14:00','end':'15:00'}}})
    create(core,'event','UTC meeting',data={'date':'2030-01-02','start':'09:00','end':'10:00','timezone':'UTC'})
    result=core.query('dashboard',date='2030-01-02');events=result['days'][2]['events']
    assert [(e['title'],e['start'],e['end']) for e in events]==[('Changed class','14:00','15:00'),('UTC meeting','17:00','18:00')]
    page=TodayPage(ControlledBridge())
    try:
        with core.store.connect() as c:projected=core._events(c,'2030-01-02')[0]
        page.render_events(projected)
        words='\n'.join(w.text() for w in page.events_box.findChildren(QLabel))
        assert '14:00 – 15:00' in words and '17:00 – 18:00' in words and 'New room' in words
        assert '09:00 – 10:00' not in words
    finally:page.close();page.deleteLater();app.processEvents()


def test_home_task_and_owner_totals_both_exclude_draft(tmp_path):
    core=Core(tmp_path);owner=create(core,'course','Course')
    create(core,'task','Draft',parent_id=owner['id'],status='draft')
    create(core,'task','Open',parent_id=owner['id'])
    result=core.query('dashboard',date='2030-01-02')
    assert result['tasks']=={'total':1,'done':0,'open':1}
    assert {k:result['owners'][0][k] for k in ('total','done','open')}==result['tasks']
    # Other course summaries keep their established all-status scope.
    from management.task_views import summary
    with core.store.connect() as c:assert summary(core,c,owner['id'])[0]==2


def test_past_warning_newest_request_wins_and_collapsing_invalidates(app):
    bridge=ControlledBridge();page=DashboardPage(bridge);page.day='2030-01-02';page.past_toggle.setChecked(True)
    try:
        page.load_past(offset=0);old=bridge.take('warnings')
        page.load_past(offset=5);new=bridge.take('warnings')
        new['callback']({'items':[{'id':'new','title':'Current page','reason':'current'}]})
        old['callback']({'items':[{'id':'old','title':'Stale page','reason':'old'}]})
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        assert [w.text() for w in page.past_box.findChildren(QLabel)]==['Current page','current']
        page.load_past(offset=10);pending=bridge.take('warnings');page.past_toggle.setChecked(False);page.load_past()
        page.past_toggle.setChecked(True);page.load_past(offset=0)
        pending['callback']({'items':[{'id':'late','title':'Wrong late page','reason':'old'}]})
        assert 'Wrong late page' not in [w.text() for w in page.past_box.findChildren(QLabel)]
    finally:page.close();page.deleteLater();app.processEvents()


def test_dashboard_refreshes_on_clock_minute_without_business_changes(app,monkeypatch):
    import management.gui as gui
    bridge=ControlledBridge();calls=[];date=QDate(2030,1,2);clock=[date,101]
    monkeypatch.setattr(gui,'clock_snapshot',lambda zone:tuple(clock))
    host=SimpleNamespace(closed=False,poll_pending=False,type_map={'task':{}},_business_timezone='Asia/Shanghai',_calendar_day=date,_clock_minute=100,
        today_page=SimpleNamespace(date=QDateEdit(date)),section='dashboard',bridge=bridge,change_cursor=0,refresh=lambda:calls.append('refresh'),load_display_preferences=lambda:None,show_error=lambda e:None)
    MainWindow.poll_changes(host);bridge.deliver('changes',{'items':[],'cursor':0});assert calls==['refresh']
    MainWindow.poll_changes(host);bridge.deliver('changes',{'items':[],'cursor':0});assert calls==['refresh']
    clock[0]=date.addDays(1);clock[1]=102
    MainWindow.poll_changes(host);bridge.deliver('changes',{'items':[],'cursor':0})
    assert host.today_page.date.date()==date.addDays(1) and calls==['refresh','refresh']
    host.today_page.date.deleteLater();app.processEvents()


def test_clock_business_day_uses_configured_zone(monkeypatch):
    import management.gui as gui
    class FixedDateTime(dt.datetime):
        @classmethod
        def now(cls,tz=None):return dt.datetime(2030,1,2,18,0,tzinfo=dt.timezone.utc).astimezone(tz)
    monkeypatch.setattr(gui,'dt',SimpleNamespace(datetime=FixedDateTime,timezone=dt.timezone))
    local_day,key=gui.clock_snapshot('Asia/Shanghai')
    assert local_day==QDate(2030,1,3)
    assert gui.clock_snapshot('America/Los_Angeles')[0]==QDate(2030,1,2)
