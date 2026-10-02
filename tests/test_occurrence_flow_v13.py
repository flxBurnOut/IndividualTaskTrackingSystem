"""End-to-end business tests over synthetic fixed schedules and durable feedback."""
import uuid
import pytest
from management.core import Core
from management.schemas import BusinessError

DAY='2020-01-06'

def cmd(core,name,p):
    state=core.query('state')
    return core.command(name,p,request_id=str(uuid.uuid4()),epoch=state['epoch'],expected_revision=state['revision'])['result']

@pytest.fixture
def core(tmp_path):return Core(tmp_path/'data')

def course_event(core,title='Tutorial 6',**extra):
    course=cmd(core,'create',{'type':'course','title':'Synthetic course','data':{'code':'XX0001'}})['entity']
    event=cmd(core,'create',{'type':'event','title':title,'data':{
        'owner_id':course['id'],'date':DAY,'start':'09:00','end':'10:00','hard':True,
        'recurrence':'weekly','event_kind':'tutorial',**extra}})['entity']
    return course,event

def daily(core,day=DAY):return core.query('daily_review',date=day)

def submit(core,view,result,item=None):
    item=item or view['items'][0];plan=view.get('plan') or {}
    return cmd(core,'submit_daily_review',{'date':view['date'],'plan_id':plan.get('id'),'plan_version':plan.get('version'),
        'schedule_signature':view['schedule_signature'],'answers':[{'item_id':item['item_id'],'result':result}]})

def test_fixed_only_day_is_reviewable_without_writing_tasks(core):
    course,event=course_event(core)
    view=daily(core)
    assert not view['has_plan'] and view['can_review'] and not view['needs_codex']
    assert len(view['items'])==1 and view['items'][0]['owner_label']=='XX0001'
    assert view['items'][0]['learning_unit_label']=='Tutorial 6'
    for _ in range(3):daily(core)
    assert core.query('list',type='task')['total']==core.query('list',type='feedback')['total']==0

def test_attendance_does_not_claim_completion_or_mastery(core):
    course_event(core)
    submit(core,daily(core),'attended')
    view=daily(core)
    assert view['summary']['attended']==1 and view['summary']['done']==0
    feedback=core.query('list',type='feedback')['items'][0]
    assert feedback['data']['dimensions']=={'attendance':'attended'}
    assert core.query('list',type='task')['total']==0
    assert daily(core,'2020-01-13')['items'][0]['reported'] is False

def test_missing_creates_one_recovery_and_later_progress_preserves_absence(core):
    course_event(core)
    submit(core,daily(core),'missed_needs_catchup')
    saved=daily(core);task_id=saved['items'][0]['catchup_task_id']
    assert task_id and saved['summary']['catchup_needed']==1
    assert submit(core,saved,'missed_needs_catchup')['changed_count']==0
    task=core.query('get',id=task_id)['entity']
    cmd(core,'record_recovery_progress',{'task_id':task_id,'version':task['version'],'business_date':DAY,
        'completed_quantity':1,'completion_confirmed':True,'source_text':'Explicitly completed synthetic lesson'})
    after=daily(core)
    assert after['items'][0]['result']=='missed_needs_catchup'
    assert after['summary']['catchup_needed']==0 and after['summary']['absent']==1
    assert core.query('list',type='task')['total']==1
    weekly=core.query('weekly_review',start=DAY,end='2020-01-12')
    assert weekly['summary']['absent']==1 and weekly['summary']['catchup_needed']==0

def test_unknown_lesson_never_infers_number_from_date(core):
    course_event(core,title='Weekly tutorial')
    view=daily(core)
    assert view['items'][0]['learning_unit_label']=='' and '课次待确认' in view['items'][0]['display_title']
    submit(core,view,'missed_needs_catchup')
    task=core.query('list',type='task')['items'][0]
    assert '课次待确认' in task['title']

def test_plan_event_and_projection_count_once_and_time_not_double_charged(core):
    _,event=course_event(core)
    before=core.query('plan_context',date=DAY)
    cmd(core,'create_plan',{'date':DAY,'mode':'no_precise_time','blocks':[{'target_id':event['id'],'minutes':60}]})
    view=daily(core)
    assert view['has_plan'] and len(view['items'])==1
    assert core.query('plan_context',date=DAY)['hard_events']==before['hard_events']
    assert view['items'][0]['planned_minutes'] is None

def test_cross_midnight_is_one_review_occurrence_but_both_days_remain_occupied(core):
    course_event(core,start='23:00',end='01:00')
    assert len(daily(core)['items'])==1
    assert not daily(core,'2020-01-07')['items']
    assert core.query('plan_context',date='2020-01-07')['hard_events']

def test_future_absence_is_not_an_actual_fact(core):
    course_event(core,date='2099-01-05')
    view=daily(core,'2099-01-05')
    assert view['items'] and not view['items'][0]['can_review']
    with pytest.raises(BusinessError,match='未来'):submit(core,view,'missed_needs_catchup')
    assert core.query('list',type='feedback')['total']==0

def test_stale_choices_are_rejected_and_history_survives_schedule_cancel(core):
    _,event=course_event(core)
    stale=daily(core);submit(core,stale,'attended')
    with pytest.raises(BusinessError) as error:submit(core,stale,'absent')
    assert error.value.code=='review_plan_conflict'
    cmd(core,'update',{'id':event['id'],'version':event['version'],'patch':{'status':'cancelled'}})
    assert daily(core)['items'][0]['result']=='attended'
    assert not daily(core,'2020-01-13')['items']

def test_recess_and_unselected_teaching_weeks_create_no_expected_debt(core):
    cmd(core,'apply_timetable',{'title':'Term','semester_start':'2020-01-06','semester_end':'2020-02-16',
        'timezone':'Asia/Shanghai','week_numbering':'teaching','recess_weeks':['2020-01-20'],
        'source_text':'Explicit synthetic schedule','rows':[{'key':'a','title':'Lecture','weekday':0,'start':'09:00','end':'10:00','teaching_weeks':[1,3]}]})
    assert daily(core)['items']
    assert not daily(core,'2020-01-20')['items']
    assert not daily(core,'2020-01-13')['items']
    assert daily(core,'2020-01-27')['items']
    assert core.query('list',type='task')['total']==0

def test_direct_codex_attendance_and_later_correction_are_visible(core):
    _,event=course_event(core)
    cmd(core,'record_feedback',{'target_id':event['id'],'business_date':DAY,'dimensions':{'attendance':'attended'},'source_text':'Explicit attendance via MCP'})
    assert daily(core)['items'][0]['result']=='attended'
    submit(core,daily(core),'missed_needs_catchup')
    cmd(core,'record_feedback',{'target_id':event['id'],'business_date':DAY,'dimensions':{'attendance':'attended'},'source_text':'Explicit correction via MCP'})
    result=daily(core)['items'][0]
    assert result['result']=='attended' and result['catchup_task_id']

def test_today_future_class_does_not_become_actual_absence(core,monkeypatch):
    import datetime as dt
    from types import SimpleNamespace
    from management import occurrences
    with core.store.connect() as c:today=core.today(c)
    class Clock(dt.datetime):
        @classmethod
        def now(cls,tz=None):return cls.fromisoformat(today+'T12:00:00').replace(tzinfo=tz)
    monkeypatch.setattr(occurrences,'dt',SimpleNamespace(date=dt.date,datetime=Clock))
    course_event(core,date=today,start='18:00',end='19:00')
    value=daily(core,today);assert not value['items'][0]['can_review']
    with pytest.raises(BusinessError):submit(core,value,'missed_needs_catchup')

def test_fixed_only_day_keeps_generate_plan_entry_visible():
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from management.gui_today import TodayPage
    app=QApplication.instance() or QApplication([])
    value={'has_plan':False,'has_fixed_schedule':True,'can_review':True,'plan':None,'items':[],
           'summary':{'total':0,'done':0,'unreported':0}}
    class Bridge:
        def query(self,name,callback,error,**params):
            if name=='daily_review':callback(value)
    page=TodayPage(Bridge())
    try:
        page.refresh()
        assert not page.manual_button.isHidden() and page.manual_button.text()=='安排这一天'
        assert page.plan_button.isHidden()
    finally:
        page.close();page.deleteLater();app.processEvents()

def test_daily_reminder_uses_fixed_schedule_without_inventing_feedback(core):
    import datetime as dt,threading
    from management.scheduler import Background
    course_event(core)
    cmd(core,'create',{'type':'schedule','title':'Review','data':{'workflow':'checkin','time':'00:01',
        'timezone':'Asia/Shanghai','frequency':'daily','enabled':True}})
    Background(core,threading.Event()).tick(dt.datetime(2020,1,6,12,tzinfo=dt.timezone.utc))
    notices=core.query('list',type='notification')['items']
    assert len(notices)==1 and notices[0]['data']['review_mode']=='daily'
    assert '固定安排' in notices[0]['data']['content'] and 'Codex' not in notices[0]['data']['content']
    assert core.query('list',type='feedback')['total']==core.query('list',type='checkin')['total']==0
