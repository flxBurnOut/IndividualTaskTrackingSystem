"""Read-only home overview, using the same dates, warnings and task evidence."""
from __future__ import annotations
import datetime as dt
from .schemas import BusinessError
from .reviews import query_daily
from .task_views import rows_sql, summary
from .timetable import validate_timetable_metadata, _week_position


def projected_event_times(event):
    """Displayed clock times use the already projected local-day occurrence."""
    if 'start_minute' not in event:
        data=event.get('effective') or event.get('data',event)
        return data.get('start'),data.get('end')
    def clock(value):
        return None if value is None else f'{int(value)//60:02d}:{int(value)%60:02d}'
    return clock(event.get('start_minute')),clock(event.get('end_minute'))


def overview(core,c,p):
    day=p.get('date') or core.today(c)
    try:today=dt.date.fromisoformat(day)
    except (ValueError,TypeError):raise BusinessError('validation','请选择有效日期。')
    monday=today-dt.timedelta(days=today.weekday())
    from .presentation import Presenter
    from .planning import warning_query
    presenter=Presenter(core,c)
    periods=[]
    for row in c.execute("SELECT * FROM entities WHERE type='timetable' AND archived=0 AND status NOT IN ('done','cancelled','draft') ORDER BY title,id LIMIT 101"):
        table=core.store.entity(row);d=table['data']
        if not d.get('enabled',True) or not d.get('semester_start') or not d.get('semester_end'):continue
        if not d['semester_start']<=day<=d['semester_end']:continue
        try:
            config=validate_timetable_metadata(d,require_complete=True)
            number,recess=_week_position(config,today)
        except BusinessError:continue
        label='Recess week · 休息周' if recess else ('教学第 ' if config['week_numbering']=='teaching' else '学期第 ')+str(number)+' 周'
        periods.append({'title':table['title'],'week_number':number,'is_recess':recess,'label':label})
    labels=list(dict.fromkeys(x['label'] for x in periods))
    week_label=labels[0] if len(labels)==1 else '多份课表的周次不同' if labels else '教学周尚未设置'
    days=[]
    for index in range(7):
        current=(monday+dt.timedelta(days=index)).isoformat();events,unknowns=core._events(c,current)
        events=sorted(events,key=lambda e:(e.get('start_minute') is None,e.get('start_minute') or 0,e['title']))
        days.append({'date':current,'weekday':'周'+'一二三四五六日'[index],'is_today':current==day,'event_count':len(events),
            'events':[dict(presenter.fields(e),id=e['id'],title=e['title'],start=projected_event_times(e)[0],end=projected_event_times(e)[1],event_kind=e['data'].get('event_kind')) for e in events[:3]]})
    sql=rows_sql("e.status NOT IN ('cancelled','draft')")
    totals=c.execute("SELECT count(*) AS total,coalesce(sum(completion_state='done'),0) AS done FROM ("+sql+")").fetchone()
    owner_rows=c.execute("""SELECT e.* FROM entities e WHERE e.type IN ('course','project')
        AND e.archived=0 AND e.status NOT IN ('done','cancelled','draft')
        AND NOT EXISTS (WITH RECURSIVE ancestors(id,parent_id,type,archived,status) AS (
            SELECT id,parent_id,type,archived,status FROM entities WHERE id=e.parent_id
            UNION SELECT p.id,p.parent_id,p.type,p.archived,p.status FROM entities p JOIN ancestors a ON p.id=a.parent_id)
            SELECT 1 FROM ancestors WHERE type IN ('course','project') AND archived=0
                AND status NOT IN ('done','cancelled','draft'))
        ORDER BY e.type,e.title,e.id LIMIT 9""").fetchall()
    owners=[]
    for row in owner_rows[:8]:
        entity=core.store.entity(row);total,done,incomplete=summary(core,c,entity['id'],exclude_draft=True)
        owners.append({'id':entity['id'],'type':entity['type'],'label':entity['data'].get('code') or entity['title'],'total':total,'done':done,'open':total-done})
    daily=query_daily(core,c,{'date':day})
    current=warning_query(core,c,{'date':day,'group':'current','limit':5})
    return {'date':day,'date_label':f'{today.year} 年 {today.month} 月 {today.day} 日','weekday':'星期'+'一二三四五六日'[today.weekday()],
        'week_label':week_label,'periods':periods,'week_start':monday.isoformat(),'week_end':(monday+dt.timedelta(days=6)).isoformat(),'days':days,
        'tasks':{'total':totals['total'],'done':totals['done'],'open':totals['total']-totals['done']},
        'owners':owners,'owners_more':len(owner_rows)>8,'plan':{'has_plan':daily['has_plan'],'summary':daily['summary']},'warnings':current}
