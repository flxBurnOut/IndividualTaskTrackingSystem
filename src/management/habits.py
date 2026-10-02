"""One read-only explanation of habits, warnings, reminders and task automation."""
from __future__ import annotations
import datetime as dt
import json
from zoneinfo import ZoneInfo
from .schemas import BusinessError
from . import recurring


def _now():return dt.datetime.now(dt.timezone.utc)


def rule_view(entity,day):
    d=entity['data'];kind=d.get('rule_kind')
    effect={'warning':'显示提前警戒，不创建待办','capacity':'保存计划时校验可用容量','protected_time':'保存计划时检查时段冲突'}.get(kind,'安排时供你参考；使用助手时也会提供，不会自行执行')
    state='已保存 · 安排时参考' if kind not in {'warning','capacity','protected_time'} else '已启用'
    enabled=not entity['archived'] and entity['status'] not in {'done','cancelled','draft'} and d.get('enabled',True) is not False
    if not enabled:state='已停用'
    elif d.get('effective_from') and day<d['effective_from']:state='尚未到生效日期';enabled=False
    elif d.get('effective_until') and day>d['effective_until']:state='已到期';enabled=False
    if kind=='warning':
        amounts=[]
        if d.get('calendar_months_before'):amounts.append(str(d['calendar_months_before'])+' 个日历月')
        if d.get('days_before'):amounts.append(str(d['days_before'])+' 天')
        explanation='提前 '+('加 '.join(amounts) or '0 天')+'在今天页显示警戒。'
    elif kind=='capacity':explanation='容量：'+(str(d['minutes'])+' 分钟' if d.get('minutes') is not None else '尚未明确')
    elif kind=='protected_time':explanation='保护时段：'+str(d.get('start') or '待明确')+'—'+str(d.get('end') or '待明确')
    else:explanation=str(d.get('policy') or d.get('notes') or '尚未填写习惯内容。')
    if enabled and (kind=='capacity' and d.get('minutes') is None or kind=='protected_time' and not (d.get('start') and d.get('end'))):state='缺少配置 · 待补充'
    scope_warning=None
    selected=d.get('target_types');selected=[selected] if isinstance(selected,str) else selected
    for field,expected,word in [('event_kind','event','日程'),('task_kind','task','任务')]:
        if d.get(field) and selected and any(t!=expected for t in selected):
            scope_warning='当前类型筛选实际只匹配'+word+'；列出的其他事项类型不会命中，需要核对范围。'
            if enabled:state='范围待核对'
    # Presentation aliases never replace the stored title, policy or scope.
    everyday_titles={
        '主动触发与低状态':'何时安排一天、累了怎么安排',
        '当前三周恢复边界':'这段时间的休息与学习安排',
        '睡眠、恢复与容量':'睡眠、休息与每天学习量',
        '按明确反馈更新':'只记录我确认过的进度',
        '课前要求与探索范围':'课前要准备什么',
        '重课日保护':'课多的日子少加任务',
    }
    display_title=everyday_titles.get(entity['title'],entity['title']) if kind in {'behavior','temporary'} else entity['title']
    return {**entity,'display_title':display_title,'effect':effect,'display_state':state,'effective':enabled,'explanation':explanation,'scope_warning':scope_warning}


def _reminder(core,c,key,prefs,instant):
    d=prefs[key];result={**d,'kind':key,'next_at':None,'next_label':'未启用','last_at':None,'last_state':None}
    identifier=d.get('schedule_id')
    if identifier:
        last=c.execute('SELECT * FROM schedule_runs WHERE schedule_id=? ORDER BY created_at DESC LIMIT 1',(identifier,)).fetchone()
        if last:result.update(last_at=last['created_at'],last_state='failed' if last['job_id'] else 'delivered')
    if not d['enabled']:return result
    zone=ZoneInfo(d.get('timezone') or prefs['timezone']);local=instant.astimezone(zone)
    day=local.date()
    if key=='weekly':day+=dt.timedelta(days=(d['weekday']-day.weekday())%7)
    for _ in range(2):
        occurrence=day.isoformat()+'T'+d['time']+'['+str(zone)+']'
        checked=identifier and c.execute('SELECT 1 FROM schedule_runs WHERE schedule_id=? AND occurrence=?',(identifier,occurrence)).fetchone()
        if not checked:break
        day+=dt.timedelta(days=7 if key=='weekly' else 1)
    due=dt.datetime.combine(day,dt.time.fromisoformat(d['time']),zone)
    result.update(next_at=due.isoformat(timespec='minutes'),next_label='下次后台检查补发今天的提醒' if due<=local else day.isoformat()+' '+d['time'])
    return result


def _anchors(core,c,day):
    end=dt.date.fromisoformat(day)+dt.timedelta(days=31);low=dt.date.fromisoformat(day)
    where="""archived=0 AND status NOT IN ('done','cancelled','draft') AND (
        (type='event' AND json_extract(data,'$.date') IS NOT NULL
          AND (coalesce(json_extract(data,'$.recurrence'),'none')!='none' OR json_extract(data,'$.date')>=?)
          AND coalesce(json_extract(data,'$.until'),'9999-12-31')>=?) OR
        (type IN ('task','milestone','assessment') AND json_extract(data,'$.due_date')>=?))"""
    total=c.execute('SELECT count(*) FROM entities WHERE '+where,(day,day,day)).fetchone()[0]
    from .planning import _Scope as Scope
    scope=Scope(core,c);warnings=[r for r in core._rules(c,day) if r['data'].get('rule_kind')=='warning']
    anchors=[]
    for row in c.execute("SELECT * FROM entities WHERE "+where+" ORDER BY CASE WHEN json_extract(data,'$.event_kind') IN ('tutorial','lab') THEN 0 ELSE 1 END,title,id LIMIT 200",(day,day,day)):
        e=core.store.entity(row);d=e['data']
        try:
            dates=[date for _,date in recurring._occurrences(e,low,end) if low<=date<=end and not d.get('exceptions',{}).get(date.isoformat(),{}).get('cancelled')]
        except (BusinessError,ValueError):continue
        if not dates:continue
        offsets=[r['data']['days_before'] for r in warnings if type(r['data'].get('days_before')) is int and not r['data'].get('calendar_months_before') and scope.matches(r,e)]
        anchors.append({'owner_id':recurring._scope(core,c,e),'suggested_days_before':max(offsets) if offsets else None,'id':e['id'],'title':e['title'],'type':e['type'],'version':e['version'],'next_date':min(dates).isoformat(),'event_kind':d.get('event_kind'),'recurrence':d.get('recurrence')})
    return sorted(anchors,key=lambda e:(e['next_date'],e['title'])),total<=200


def overview(core,c,p):
    from .workspace import review_preferences
    instant=_now();prefs=review_preferences(core,c);today=instant.astimezone(ZoneInfo(prefs['timezone'])).date();day=today.isoformat()
    counts={}
    for row in c.execute("SELECT json_extract(data,'$.rule_kind') AS kind,count(*) AS n FROM entities WHERE type='rule' AND archived=0 AND status NOT IN ('done','draft','cancelled') AND coalesce(json_extract(data,'$.enabled'),1)=1 AND coalesce(nullif(json_extract(data,'$.effective_from'),''),?)<=? AND coalesce(nullif(json_extract(data,'$.effective_until'),''),?)>=? GROUP BY kind",(day,day,day,day)):
        counts[row['kind']]=row['n']
    total_preparation=c.execute("SELECT count(*) FROM entities WHERE type='recurring_rule' AND archived=0").fetchone()[0]
    enabled_preparation=c.execute("SELECT count(*) FROM entities WHERE type='recurring_rule' AND archived=0 AND status NOT IN ('done','draft','cancelled') AND coalesce(json_extract(data,'$.enabled'),1)=1").fetchone()[0]
    reminders={key:_reminder(core,c,key,prefs,instant) for key in ('daily','weekly')}
    guidance=sum(n for kind,n in counts.items() if kind not in ('warning','capacity','protected_time'))
    summary={'warning_rules':counts.get('warning',0),'guidance_rules':guidance,'validation_rules':counts.get('capacity',0)+counts.get('protected_time',0),'preparation_total':total_preparation,'preparation_enabled':enabled_preparation}
    fragments=['每日 '+prefs['daily']['time']+' 复盘' if prefs['daily']['enabled'] else '每日复盘提醒未启用',str(enabled_preparation)+' 项自动准备配置' if enabled_preparation else '自动准备已停用' if total_preparation else '自动准备待办未设置',str(guidance)+' 条计划习惯']
    result={'date':day,'summary':summary,'reminders':reminders,'summary_text':' · '.join(fragments),'gap':'提前警戒已启用；自动准备待办尚未设置。' if counts.get('warning') and not total_preparation else '',
        'boundaries':['每周课表提供固定时段，不自动变成待办。','提醒在今天页出现，不会代填复盘结果。','周期准备生成待办；排入日计划后才逐项复盘。','日计划由你主动发起；缺课和漏报不会自动变成已完成或补课记录。','后台服务需要运行；电脑关机或睡眠时不执行。']}
    if p.get('compact'):return result
    offset=max(0,int(p.get('rules_offset',0)));limit=30
    total=c.execute("SELECT count(*) FROM entities WHERE type='rule' AND archived=0").fetchone()[0]
    rules=[rule_view(core.store.entity(r),day) for r in c.execute("SELECT * FROM entities WHERE type='rule' AND archived=0 ORDER BY json_extract(data,'$.rule_kind'),title,id LIMIT ? OFFSET ?",(limit,offset))]
    result['rules']={'items':rules,'total':total,'next_offset':offset+len(rules) if offset+len(rules)<total else None}
    preparations=recurring.query_rules(core,c,{'limit':20,'offset':max(0,int(p.get('preparation_offset',0)))})
    for rule in preparations['items']:
        d=rule['data'];rule['upcoming']=[];rule['next_label']='未来 7 天没有新的生成日期'
        if not d.get('enabled',True) or rule['status'] in {'done','cancelled','draft'}:rule['next_label']='已停用';continue
        if d.get('effective_until') and d['effective_until']<day:rule['next_label']='已到期（'+d['effective_until']+'）';continue
        if d.get('effective_from') and d['effective_from']>day:rule['next_label']='从 '+d['effective_from']+' 开始适用'
        try:
            preview=recurring.preview(core,c,{'rule_id':rule['id'],'start':day,'end':(today+dt.timedelta(days=6)).isoformat()})
            upcoming=[{'date':e['business_date'],'title':e['title'],'state':'will_generate'} for e in preview['candidates']]
            upcoming += [{'date':e['business_date'],'title':rule['title'],'state':'already_created'} for e in preview['existing']]
            rule['upcoming']=sorted(upcoming,key=lambda e:e['date'])[:3]
            rule['issues']=preview['issues'] or rule.get('issues',[])
            if rule['issues']:rule['next_label']='需要核对，查看具体规则'
            elif rule['upcoming']:
                first=rule['upcoming'][0];rule['next_label']=first['date']+(' · 待办已生成' if first['state']=='already_created' else ' · 将生成待办')
        except (BusinessError,KeyError,ValueError) as error:rule['next_label']='配置需要核对：'+str(error)[:150]
    result['preparations']=preparations
    result['anchors'],result['anchors_complete']=_anchors(core,c,day)
    return result
