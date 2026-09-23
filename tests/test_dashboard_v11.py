import datetime as dt
from management.core import Core
from test_plan_assistance_v8 import cmd


def test_dashboard_date_teaching_week_and_recess_are_readonly(tmp_path):
    core=Core(tmp_path)
    cmd(core,'create',{'type':'timetable','title':'Semester','data':{'semester_start':'2026-08-10','semester_end':'2026-12-04','timezone':'Asia/Shanghai','week_numbering':'teaching','recess_weeks':['2026-09-28']}})
    state=core.query('state')
    current=core.query('dashboard',date='2026-09-23')
    assert current['weekday']=='星期三' and current['week_label']=='教学第 7 周'
    assert current['week_start']=='2026-09-21' and current['week_end']=='2026-09-27'
    assert len(current['days'])==7 and sum(d['is_today'] for d in current['days'])==1
    assert '休息周' in core.query('dashboard',date='2026-09-30')['week_label']
    assert core.query('dashboard',date='2026-10-05')['week_label']=='教学第 8 周'
    assert core.query('dashboard',date='2027-01-01')['week_label']=='教学周尚未设置'
    assert core.query('state')==state


def test_dashboard_does_not_show_past_warnings_as_current_and_links_owners(tmp_path):
    core=Core(tmp_path)
    course=cmd(core,'create',{'type':'course','title':'Science','data':{'code':'AB1234'}})['entity']
    cmd(core,'create',{'type':'rule','title':'Before deadline','data':{'rule_kind':'warning','days_before':3,'target_types':['task']}})
    for title,due,status in [('Old Tutorial 1','2026-09-10','pending'),('Tutorial 2','2026-09-24','pending'),('Closed','2026-09-24','done')]:
        cmd(core,'create',{'type':'task','title':title,'parent_id':course['id'],'status':status,'data':{'due_date':due}})
    result=core.query('dashboard',date='2026-09-23')
    assert result['warnings']['counts']=={'current':1,'past':1}
    assert [r['title'] for r in result['warnings']['items']]==['Tutorial 2']
    assert result['warnings']['items'][0]['owner_code']=='AB1234'
    assert result['tasks']=={'total':3,'done':1,'open':2}
    assert result['owners'][0]['open']==2
    assert not result['plan']['has_plan']


def test_week_preview_respects_actual_day_cancellation_and_dates(tmp_path):
    core=Core(tmp_path)
    cmd(core,'create',{'type':'event','title':'Wednesday tutorial','data':{'date':'2026-09-16','recurrence':'weekly','until':'2026-10-01','time_kind':'exact','start':'12:00','end':'13:00','timezone':'Asia/Shanghai','hard':True,'exceptions':{'2026-09-23':{'cancelled':True}}}})
    cmd(core,'create',{'type':'event','title':'Friday lab','data':{'date':'2026-09-25','time_kind':'exact','start':'14:00','end':'15:00','timezone':'Asia/Shanghai','hard':True}})
    result=core.query('dashboard',date='2026-09-23')
    assert result['days'][2]['event_count']==0
    assert result['days'][4]['event_count']==1
    assert sum(d['event_count'] for d in result['days'])==1
