"""Confirmed semester timetables projected through the existing event model.

No plans, course facts, attendance or completion are inferred. The caller owns
transactions, optimistic revision checks and receipts. Existing tables retain
calendar numbering; explicitly confirmed teaching numbering skips recess weeks.
"""
from __future__ import annotations
import copy
import datetime as dt
import hashlib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .schemas import BusinessError
from .storage import encode, now

MAX_ROWS = 100
MAX_WEEKS = 52
WEEK_RULE_FIELDS = ('week_numbering', 'recess_weeks')


def _day(value, label='日期'):
    try:
        if isinstance(value, dt.datetime):
            raise ValueError()
        if isinstance(value, dt.date):
            return value
        if not isinstance(value, str) or len(value)!=10:
            raise ValueError()
        result=dt.date.fromisoformat(value)
        if result.isoformat()!=value:
            raise ValueError()
        return result
    except (TypeError,ValueError) as exc:
        raise BusinessError('validation',label+'需要明确的 YYYY-MM-DD 日期。') from exc


def _text(value,label,maximum=300,optional=False):
    if optional and value in (None,''):
        return ''
    if not isinstance(value,str) or not value.strip() or len(value)>maximum:
        raise BusinessError('validation',label+'不能为空且不能超过 '+str(maximum)+' 字。')
    return value.strip()


def _clock(value):
    try:
        if not isinstance(value,str) or len(value)!=5:
            raise ValueError()
        parsed=dt.time.fromisoformat(value)
        if parsed.tzinfo or parsed.second or parsed.microsecond or parsed.strftime('%H:%M')!=value:
            raise ValueError()
        return parsed
    except (TypeError,ValueError) as exc:
        raise BusinessError('validation','课表时段需要明确的 HH:MM 时间。') from exc


def normalize_timetable_defaults(value):
    """Validate reusable settings; dates may span multiple semesters."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(WEEK_RULE_FIELDS):
        raise BusinessError('validation', '课表默认规则仅包含教学周计数方式和休息周。')
    numbering = value.get('week_numbering', 'teaching')
    if numbering not in ('calendar', 'teaching'):
        raise BusinessError('timetable_week_numbering', '教学周计数方式无效。')
    recess = value.get('recess_weeks', [])
    if not isinstance(recess, list) or len(recess) > MAX_WEEKS:
        raise BusinessError('timetable_recess', '请提供最多 52 个休息周的周一日期。')
    normalized = []
    for raw in recess:
        day = _day(raw, '休息周周一')
        if day.weekday() != 0 or not 2 <= day.year <= 9997:
            raise BusinessError('timetable_recess', '每个休息周必须以明确的周一日期表示。')
        normalized.append(day.isoformat())
    if len(set(normalized)) != len(normalized):
        raise BusinessError('timetable_recess', '休息周日期不能重复。')
    return {'week_numbering': numbering, 'recess_weeks': sorted(normalized)}


def _week_position(config, day):
    """Return the chosen numbered week; recess always has no occurrence."""
    start = _day(config['semester_start']); end = _day(config['semester_end'])
    if not start <= day <= end:
        return None, False
    monday = day - dt.timedelta(days=day.weekday())
    recess = config.get('recess_weeks', [])
    if monday.isoformat() in recess:
        return None, True
    number = (day - start).days // 7 + 1
    if config.get('week_numbering', 'calendar') == 'teaching':
        number -= sum(value < monday.isoformat() for value in recess)
    return number, False


def validate_timetable_metadata(data, require_complete=False):
    if not isinstance(data,dict):
        raise BusinessError('validation','课表元数据需要对象格式。')
    values={}
    for key,label in (('semester_start','第一教学周周一'),('semester_end','学期结束日期')):
        raw=data.get(key)
        if raw in (None,''):
            if require_complete:
                raise BusinessError('timetable_incomplete','请明确填写'+label+'；不能从课表截图猜测学期日期。')
            values[key]=None
        else:
            values[key]=_day(raw,label)
    start,end=values['semester_start'],values['semester_end']
    if start and (start.weekday()!=0 or start.year<2 or start.year>9997):
        raise BusinessError('timetable_start','第一教学周日期必须是明确的周一，且处于支持范围。')
    if start and end and not 0 <= (end-start).days < MAX_WEEKS*7:
        raise BusinessError('timetable_range','学期范围应为连续 1 到 52 个日历周；结束日包含在内。')
    zone=data.get('timezone')
    if zone in (None,''):
        if require_complete:
            raise BusinessError('timetable_incomplete','请明确课表时区，未使用系统时区替你猜测。')
        zone=None
    else:
        if not isinstance(zone,str) or len(zone)>100:
            raise BusinessError('validation','课表时区无效。')
        try:ZoneInfo(zone)
        except (ValueError,ZoneInfoNotFoundError) as exc:
            raise BusinessError('validation','课表时区无效。') from exc
    rules = normalize_timetable_defaults({
        'week_numbering': data.get('week_numbering', 'calendar'),
        'recess_weeks': data.get('recess_weeks', []),
    })
    for raw in rules['recess_weeks']:
        day = _day(raw)
        if start and day <= start:
            raise BusinessError('timetable_recess', '休息周必须位于第一教学周之后；学期第一周不能同时是休息周。')
        if end and day > end:
            raise BusinessError('timetable_recess', '休息周必须位于当前课表的日期范围内。')
    return {'semester_start':start.isoformat() if start else None,'semester_end':end.isoformat() if end else None,'timezone':zone,**rules}


def occurs_on(data, occurrence_date):
    """Shared extra filter; Core retains recurrence, timezone and time projection."""
    if not data.get('timetable_id'):
        return True
    if data.get('timetable_enabled',True) is False:
        return False
    config=validate_timetable_metadata(data,require_complete=True)
    day,start,end=_day(occurrence_date),_day(config['semester_start']),_day(config['semester_end'])
    if not start<=day<=end:
        return False
    number, is_recess = _week_position(config, day)
    if is_recess:
        return False
    origin=_day(data.get('date'),'课表首次星期锚点')
    if day.weekday()!=origin.weekday():
        return False
    weeks=data.get('teaching_weeks')
    if weeks is not None:
        if not isinstance(weeks,list) or not weeks or any(type(w) is not int or not 1<=w<=MAX_WEEKS for w in weeks):
            raise BusinessError('timetable_weeks','教学周清单无效；未知不能作为每周处理。')
        if number not in weeks:
            return False
    exception=data.get('exceptions',{}).get(day.isoformat(),{})
    return exception.get('cancelled') is not True


def _container(core,c,id):
    entity=core.store.get(c,id)
    if entity['type']!='timetable':
        raise BusinessError('timetable_type','请选择课表容器。')
    return entity


def _rows(core,c,table_id):
    rows=c.execute("SELECT * FROM entities WHERE type='event' AND json_extract(data,'$.timetable_id')=? ORDER BY created_at,id LIMIT ?",(table_id,MAX_ROWS+1)).fetchall()
    if len(rows)>MAX_ROWS:
        raise BusinessError('timetable_limit','单张课表超过 100 行，请拆分后管理。')
    return [core.store.entity(r) for r in rows]


def timetables(core,c,p):
    limit,offset=p.get('limit',50),p.get('offset',0)
    if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or offset<0:
        raise BusinessError('validation','课表分页范围无效。')
    if p.get('id'):
        table=_container(core,c,p['id'])
        return {'items':[table],'entity':table,'rows':_rows(core,c,table['id']),'total':1,'next_offset':None}
    total=c.execute("SELECT count(*) FROM entities WHERE type='timetable' AND archived=0").fetchone()[0]
    items=[]
    for row in c.execute("SELECT * FROM entities WHERE type='timetable' AND archived=0 ORDER BY created_at,id LIMIT ? OFFSET ?",(limit,offset)):
        table=core.store.entity(row)
        table['row_count']=c.execute("SELECT count(*) FROM entities WHERE type='event' AND json_extract(data,'$.timetable_id')=?",(table['id'],)).fetchone()[0]
        table['configured']=bool(table['data'].get('semester_start') and table['data'].get('semester_end') and table['data'].get('timezone'))
        items.append(table)
    return {'items':items,'total':total,'next_offset':offset+len(items) if offset+len(items)<total else None}


def _sources(core,c,value):
    if isinstance(value,dict):
        value=[{'id':id,'version':version} for id,version in value.items()]
    if not isinstance(value,list) or len(value)>40:
        raise BusinessError('validation','课表来源版本最多 40 项。')
    result={};seen=set()
    for reference in value:
        if not isinstance(reference,dict) or not isinstance(reference.get('id'),str) or type(reference.get('version')) is not int:
            raise BusinessError('validation','来源引用需要明确的稳定标识和版本。')
        entity=core.store.get(c,reference['id'])
        if entity['type'] not in {'asset','file_reference','artifact','note'} or entity['version']!=reference['version']:
            raise BusinessError('source_changed','课表来源已经变化或类型不适用，请重新读取。',{'id':entity['id'],'current_version':entity['version']})
        if entity['id'] in seen:
            raise BusinessError('validation','课表来源版本不能重复。')
        seen.add(entity['id'])
        result[entity['id']]=entity['version']
    return result


def _validate_local_time(day,value,zone):
    naive=dt.datetime.combine(day,_clock(value))
    localized=naive.replace(tzinfo=zone)
    if localized.astimezone(dt.timezone.utc).astimezone(zone).replace(tzinfo=None)!=naive or localized.utcoffset()!=naive.replace(tzinfo=zone,fold=1).utcoffset():
        raise BusinessError('timetable_dst','该次时段处于夏令时不存在或有歧义的本地时间，请明确单次时段后再确认。',{'date':day.isoformat(),'time':value})


def _inherit_source(data, source):
    # The full confirmation remains on the versioned container. Repeating a
    # 12000-character source 100 times would exceed the transport response cap.
    excerpt = len(source or '') > 600
    data['source_text'] = source[:600] + '…（完整来源保留在所属课表及来源版本记录。）' if excerpt else source
    data['timetable_source_excerpt'] = excerpt


def _row(core,c,row,metadata,table_id,old=None,table_source=None):
    if not isinstance(row,dict):
        raise BusinessError('validation','课表每行需要对象格式。')
    original=old['data'] if old else {}
    key=_text(row.get('key'),'稳定课表行标识',160)
    title=_text(row.get('title',old['title'] if old else None),'日程名称')
    previous_weekday=_day(original['date']).weekday() if original.get('date') else None
    weekday=row.get('weekday',original.get('timetable_weekday',previous_weekday))
    if type(weekday) is not int or not 0<=weekday<=6:
        raise BusinessError('validation','星期必须为 0 到 6 的整数（0 为周一）。')
    start=row.get('start',original.get('start'));end=row.get('end',original.get('end'))
    a,b=_clock(start),_clock(end)
    if a==b:
        raise BusinessError('timetable_time','课表开始和结束不能相同；未猜测零时长或全天课程。')
    semester_start=_day(metadata['semester_start']);semester_end=_day(metadata['semester_end'])
    origin=semester_start+dt.timedelta(days=weekday)
    if origin>semester_end:
        raise BusinessError('timetable_no_occurrence','该星期不在所填学期范围内。')
    if row.get('weeks_unknown') is True:
        raise BusinessError('timetable_weeks_unknown','这行教学周尚未确认，未把未知周扩展为每周。')
    weeks=row.get('teaching_weeks',original.get('teaching_weeks'))
    if weeks is not None:
        if not isinstance(weeks,list) or not weeks or len(weeks)>MAX_WEEKS or any(type(w) is not int or not 1<=w<=MAX_WEEKS for w in weeks) or len(set(weeks))!=len(weeks):
            raise BusinessError('timetable_weeks','请明确每周或不重复的 1 到 52 教学周清单，空清单不能代替未知。')
        weeks=sorted(weeks)
        if metadata.get('week_numbering', 'calendar') == 'teaching':
            valid_weeks = {_week_position(metadata, origin + dt.timedelta(weeks=w))[0]
                           for w in range(MAX_WEEKS) if origin + dt.timedelta(weeks=w) <= semester_end}
            if any(w not in valid_weeks for w in weeks):
                raise BusinessError('timetable_weeks', '跳过休息周后，指定教学周的该次课超出学期结束日期。')
        elif any(origin+dt.timedelta(weeks=w-1)>semester_end for w in weeks):
            raise BusinessError('timetable_weeks','指定教学周的该次课超出学期结束日期。')
    enabled=row.get('enabled',original.get('timetable_enabled',True) and (not old or old['status']!='cancelled'))
    if type(enabled) is not bool:
        raise BusinessError('validation','课表行启用状态必须明确。')
    owner_id=row.get('owner_id',original.get('owner_id')) or None
    if owner_id:
        owner=core.store.get(c,owner_id)
        if owner['type']!='course' or owner['archived']:
            raise BusinessError('timetable_owner','课表归属只能选择仍在使用的已有课程；不会擅自新建课程。')
    event_kind=row.get('event_kind',original.get('event_kind','lecture'))
    if event_kind not in {'exam','lecture','tutorial','lab','meeting','interview','appointment','travel','other'}:
        raise BusinessError('validation','事件类别无效。')
    location=_text(row.get('location',original.get('location')),'地点',500,optional=True)
    exceptions=copy.deepcopy(original.get('exceptions',{}))
    removals=row.get('remove_exceptions',[])
    if not isinstance(removals,list) or len(removals)>52:
        raise BusinessError('validation','单次例外移除列表无效。')
    for date in removals:
        exceptions.pop(_day(date).isoformat(),None)
    patches=row.get('exceptions',{})
    if not isinstance(patches,dict) or len(patches)>52:
        raise BusinessError('validation','单行课表最多 52 个单次例外。')
    for date,exception in patches.items():
        day=_day(date)
        if not isinstance(exception,dict) or set(exception)-{'cancelled','start','end','personal_choice','replacement_target_id'}:
            raise BusinessError('validation','单次例外包含不支持的字段。')
        merged={**exceptions.get(day.isoformat(),{}),**exception}
        if 'cancelled' in merged and type(merged['cancelled']) is not bool:
            raise BusinessError('validation','单次取消必须是明确的启用或取消状态。')
        for field in ('start','end'):
            if field in merged:_clock(merged[field])
        exceptions[day.isoformat()]=merged
    if len(exceptions)>52:
        raise BusinessError('timetable_limit','单行课表的单次例外超过 52 项。')
    for date,exception in exceptions.items():
        day=_day(date)
        if not semester_start<=day<=semester_end or day.weekday()!=weekday:
            raise BusinessError('timetable_exception','单次例外必须位于本行对应的星期和学期范围内；改学期时请明确移除不适用例外。')
        if exception.get('start',start)==exception.get('end',end):
            raise BusinessError('timetable_time','单次时段开始和结束不能相同。')
    data={**copy.deepcopy(original),**metadata,'timetable_id':table_id,'timetable_row_key':key,'timetable_weekday':weekday,
          'date':origin.isoformat(),'start':start,'end':end,'timezone':metadata['timezone'],'recurrence':'weekly',
          'until':metadata['semester_end'],'hard':True,'time_kind':'exact','teaching_weeks':weeks,
          'timetable_enabled':enabled,'owner_id':owner_id,'event_kind':event_kind,'location':location,'exceptions':exceptions}
    if 'source_text' in row:
        data['source_text']=_text(row['source_text'],'本行来源',12000)
        data['timetable_source_excerpt']=bool(original.get('timetable_source_excerpt') and data['source_text']==original.get('source_text'))
    elif not data.get('source_text'):_inherit_source(data,table_source)
    zone=ZoneInfo(metadata['timezone'])
    for week in range(1,MAX_WEEKS+1):
        occurrence=origin+dt.timedelta(weeks=week-1)
        if occurrence>semester_end:break
        if not occurs_on(data,occurrence):continue
        effective={**data,**exceptions.get(occurrence.isoformat(),{})}
        _validate_local_time(occurrence,effective['start'],zone)
        ending=occurrence+dt.timedelta(days=int(_clock(effective['end'])<_clock(effective['start'])))
        _validate_local_time(ending,effective['end'],zone)
    return {'type':'event','title':title,'parent_id':None,'status':'active' if enabled else 'cancelled','data':data}


def apply_timetable(core,c,p,rid):
    rows=p.get('rows')
    if not isinstance(rows,list) or not 1<=len(rows)<=MAX_ROWS:
        raise BusinessError('timetable_limit','每次确认需要 1 到 100 行课表；空容器可先保存资料而不生成日程。')
    source=_text(p.get('source_text'),'课表确认来源',12000)
    old=_container(core,c,p['id']) if p.get('id') else None
    if old and old['archived']:
        raise BusinessError('archived_parent','课表已归档，请先明确恢复。')
    prior=old['data'] if old else {}
    title=_text(p.get('title',old['title'] if old else None),'课表名称')
    def confirmed_metadata(previous):
        fields = ('semester_start', 'semester_end', 'timezone', *WEEK_RULE_FIELDS)
        raw = {key: p.get(key, previous.get(key)) for key in fields if key in p or key in previous}
        result = validate_timetable_metadata(raw, require_complete=True)
        # A repeated 0.6 import is a true no-op, without silently writing a new
        # rule snapshot. New UI imports explicitly supply the settings snapshot.
        for key in WEEK_RULE_FIELDS:
            if key not in raw:
                result.pop(key, None)
        return result
    metadata=confirmed_metadata(prior)
    identity=hashlib.sha256((' '.join(title.split()).casefold()+'\n'+metadata['semester_start']).encode('utf-8')).hexdigest()
    if not old:
        duplicate=c.execute("SELECT * FROM entities WHERE type='timetable' AND archived=0 AND (json_extract(data,'$.timetable_key')=? OR (trim(title)=? COLLATE NOCASE AND json_extract(data,'$.semester_start')=?)) ORDER BY created_at,id LIMIT 1",(identity,title,metadata['semester_start'])).fetchone()
        if duplicate:
            old=core.store.entity(duplicate);prior=old['data']
            metadata=confirmed_metadata(prior)
    if old and p.get('id') and p.get('version')!=old['version']:
        raise BusinessError('entity_conflict','课表已变化，请重新读取后确认。',{'current_version':old['version']})
    versions=_sources(core,c,p['source_versions']) if 'source_versions' in p else copy.deepcopy(prior.get('source_versions',{}))
    if isinstance(versions,list):versions={entry['id']:entry['version'] for entry in versions}
    previous_rows=_rows(core,c,old['id']) if old else []
    old_by_key={}
    for event in previous_rows:
        key=event['data'].get('timetable_row_key')
        if not key or key in old_by_key:
            raise BusinessError('timetable_corrupt','课表行标识缺失或重复，需要先核对现有日程。')
        old_by_key[key]=event
    supplied_keys=[]
    for row in rows:
        if not isinstance(row,dict):raise BusinessError('validation','课表行格式无效。')
        supplied_keys.append(_text(row.get('key'),'稳定课表行标识',160))
    if len(set(supplied_keys))!=len(supplied_keys):
        raise BusinessError('timetable_duplicate_row','一次上传中稳定行标识不能重复。')
    if len(set(old_by_key)|set(supplied_keys))>MAX_ROWS:
        raise BusinessError('timetable_limit','保留原有日程后课表将超过 100 行，请拆分课表。')
    boundary_changed=bool(old and any(prior.get(k)!=v for k,v in metadata.items()) and previous_rows)
    if boundary_changed and set(old_by_key)-set(supplied_keys):
        raise BusinessError('timetable_rows_required','修改学期日期、时区或休息周规则需要明确提交全部已有行及版本，不能悄悄移动未列出的日程。',{'missing_keys':sorted(set(old_by_key)-set(supplied_keys))})
    # Allocate the container first only inside this caller-owned atomic transaction.
    table_data={**copy.deepcopy(prior),**metadata,'timetable_key':identity,'source_text':source,'source_versions':versions}
    created_table=False
    if old:
        table=old
    else:
        table=core._create(c,{'type':'timetable','title':title,'data':table_data},rid)
        created_table=True
    prepared=[]
    for row in rows:
        key=row['key'].strip();existing=old_by_key.get(key)
        if row.get('event_id') and (not existing or row['event_id']!=existing['id']):
            raise BusinessError('timetable_row_identity','行标识和日程编号不匹配，不能覆盖另一条日程。')
        if existing and existing['archived']:
            raise BusinessError('timetable_row_archived','该行原日程已归档；不会通过再次上传偷偷恢复。',{'id':existing['id']})
        desired=_row(core,c,row,metadata,table['id'],existing,source)
        # Preserve independent extra fields and references unless this confirmed
        # row actually changes; repeated uploads remain semantic no-ops.
        dirty=not existing or any(existing.get(k)!=desired[k] for k in ('title','parent_id','status','data'))
        if existing and row.get('version') is not None and row['version']!=existing['version']:
            raise BusinessError('entity_conflict','这条课表日程已被修改，请重新读取该行。',{'id':existing['id'],'current_version':existing['version']})
        if existing and dirty and (row.get('event_id')!=existing['id'] or row.get('version')!=existing['version']):
            raise BusinessError('timetable_row_version','修改已有课表行需要明确日程编号和当前版本。',{'id':existing['id'],'version':existing['version'],'key':key})
        if existing and dirty and 'source_text' not in row:
            _inherit_source(desired['data'],source)
        prepared.append((existing,desired,dirty))
    final_rows={key:event for key,event in old_by_key.items()}
    for row,(_,desired,_) in zip(rows,prepared):final_rows[row['key'].strip()]=desired
    meanings={}
    for key,event in final_rows.items():
        d=event['data']
        identity_fields={k:d.get(k) for k in ('date','start','end','timezone','teaching_weeks','week_numbering','recess_weeks','owner_id','event_kind','location','exceptions')}
        semantic=encode({'title':' '.join(event['title'].split()).casefold(),**identity_fields})
        if semantic in meanings:
            duplicate_key=meanings[semantic]
            prior_event=old_by_key.get(duplicate_key) or old_by_key.get(key)
            raise BusinessError('timetable_duplicate_semantics','课表中已有相同名称、时段、教学周和归属的行；请复用原行标识，若确为不同班请明确区分名称或地点。',{'key':key,'existing_key':duplicate_key,'event_id':prior_event['id'] if prior_event else None})
        meanings[semantic]=key
    metadata_dirty=bool(old and (old['title']!=title or any(prior.get(k)!=v for k,v in metadata.items())))
    explicit_source_edit=bool(old and p.get('id') and (prior.get('source_text')!=source or prior.get('source_versions',{})!=versions))
    dirty=metadata_dirty or explicit_source_edit or any(item[2] for item in prepared)
    if old and dirty:
        if p.get('id')!=old['id'] or p.get('version')!=old['version']:
            raise BusinessError('timetable_version','已有课表需要编号和当前版本才能追加或修改；重复内容可直接复用。',{'id':old['id'],'version':old['version']})
        table_data['last_applied_at']=now()
        target={**old,'title':title,'data':table_data}
        core._validate(c,target)
        table=core._save(c,old,target,rid,'apply_timetable')
    created_ids=[];updated_ids=[];reused_ids=[]
    for existing,desired,changed in prepared:
        if not changed:
            reused_ids.append(existing['id']);continue
        desired['data']['timetable_source_version']=table['version']
        desired['data']['source_versions']=versions
        if existing:
            target={**existing,**desired}
            core._validate(c,target)
            event=core._save(c,existing,target,rid,'apply_timetable_row')
            updated_ids.append(event['id'])
        else:
            event=core._create(c,desired,rid);created_ids.append(event['id'])
    return {'entity':table,'rows':_rows(core,c,table['id']),'created_ids':created_ids,'updated_ids':updated_ids,'reused_ids':reused_ids,
            'reused':not created_table and not dirty,'preserved_row_ids':[e['id'] for k,e in old_by_key.items() if k not in supplied_keys],
            'guidance':'未列出的旧行保留；所有行仍是同一份日程，不创建每日计划、出勤或完成反馈。'}


def _event_projection(core,c,event,day,cache):
    data=event['data'];owner_id=data.get('owner_id');owner=None
    if owner_id:
        if owner_id not in cache:
            row=c.execute('SELECT * FROM entities WHERE id=?',(owner_id,)).fetchone()
            cache[owner_id]=core.store.entity(row) if row else None
        owner=cache[owner_id]
    keep=('date','start','end','timezone','hard','time_kind','recurrence','until','owner_id','event_kind','location','timetable_id','timetable_row_key','semester_start','semester_end','teaching_weeks','timetable_enabled','week_numbering','recess_weeks')
    effective=event.get('effective') or data
    return {k:event[k] for k in ('id','type','title','parent_id','status','version','occurrence_date','start_minute','end_minute') if k in event}|{
        'business_date':day,'data':{k:data[k] for k in keep if k in data},
        'effective':{k:effective[k] for k in ('start','end','timezone','time_kind','location') if k in effective},
        'owner_title':owner['title'] if owner else None,'course_title':owner['title'] if owner and owner['type']=='course' else None}


def timetable_week(core,c,p):
    start=_day(p.get('week_start'),'查看周的周一')
    if start.weekday()!=0 or start.year<2 or start.year>9997:
        raise BusinessError('timetable_week','周视图需要明确的周一日期。')
    selected=_container(core,c,p['timetable_id']) if p.get('timetable_id') else None
    tables=[selected] if selected else [core.store.entity(r) for r in c.execute("SELECT * FROM entities WHERE type='timetable' AND archived=0 ORDER BY id LIMIT 101")]
    if len(tables)>100:
        raise BusinessError('timetable_limit','课表过多，请指定一张课表查看。')
    table_names={t['id']:t['title'] for t in tables}
    days=[];events=[];conflicts=[];unknowns=[];conflict_total=0;unknown_total=0;owner_cache={}
    for table in tables:
        if not all(table['data'].get(k) for k in ('semester_start','semester_end','timezone')):
            unknowns.append({'id':table['id'],'title':table['title'],'code':'timetable_incomplete','message':'学期日期或时区尚未明确，未生成或猜测每周安排。'});unknown_total+=1
    for index in range(7):
        day=(start+dt.timedelta(days=index)).isoformat()
        hard,missing=core._events(c,day)
        matched=[e for e in hard if e['data'].get('timetable_id')==selected['id']] if selected else hard
        projected=[]
        for event in matched:
            item=_event_projection(core,c,event,day,owner_cache)
            item['timetable_title']=table_names.get(event['data'].get('timetable_id'))
            projected.append(item);events.append(item)
        if len(events)>1000:
            raise BusinessError('timetable_limit','该周日程过多，请指定一张课表查看；未截断成完整视图。')
        selected_ids={e['id'] for e in matched};day_conflicts=[];day_unknowns=[]
        for a in matched:
            if a['start_minute'] is None:continue
            for b in hard:
                if a['id']==b['id'] or b['start_minute'] is None:continue
                if b['id'] in selected_ids and a['id']>b['id']:continue
                left=max(a['start_minute'],b['start_minute']);right=min(a['end_minute'],b['end_minute'])
                if left<right:
                    conflict_total+=1
                    issue={'date':day,'event_id':a['id'],'other_event_id':b['id'],'title':a['title'],'other_title':b['title'],
                           'message':a['title']+' 与 '+b['title']+' 的已确认时段重叠。','start_minute':left,'end_minute':right}
                    if len(conflicts)<200:conflicts.append(issue);day_conflicts.append(issue)
        for item in missing:
            unknown_total+=1
            issue={'date':day,'id':item.get('id'),'title':item.get('title','待核对事项'),'code':'hard_event_unknown','message':item.get('reason') or item.get('message') or '固定安排待核对。'}
            if len(unknowns)<200:unknowns.append(issue);day_unknowns.append(issue)
        days.append({'date':day,'events':projected,'conflicts':day_conflicts,'unknowns':day_unknowns})
    week_number = None; is_recess = False; week_numbering = None
    if selected and all(selected['data'].get(k) for k in ('semester_start','semester_end','timezone')):
        config = validate_timetable_metadata(selected['data'], require_complete=True)
        week_number, is_recess = _week_position(config, start)
        week_numbering = config['week_numbering']
    return {'week_start':start.isoformat(),'week_end':(start+dt.timedelta(days=6)).isoformat(),'timezone':core.store.meta(c,'settings')['timezone'],
            'week_number':week_number,'is_recess':is_recess,'week_numbering':week_numbering,
            'timetable_id':selected['id'] if selected else None,'days':days,'events':events,'conflicts':conflicts,'unknowns':unknowns,
            'coverage':{'events_complete':True,'events_total':len(events),'conflicts_total':conflict_total,'conflicts_complete':len(conflicts)==conflict_total,
                        'unknowns_total':unknown_total,'unknowns_complete':len(unknowns)==unknown_total},
            'guidance':'空白只是没有已确认课表块，不代表可全部使用；每日计划还需遵守其他硬日程、休息、容量与未知信息。'}
