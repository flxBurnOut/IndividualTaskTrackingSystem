"""User-facing workspaces, local file references and explicit review schedules."""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .schemas import BusinessError
from .storage import now

OWNER_TYPES = {'domain','project','course','activity','phase','task','goal'}


def review_preferences(core,c):
    value={'daily': {'enabled':False,'time':'21:30'}, 'weekly': {'enabled':False,'weekday':6,'time':'19:30'}, 'timezone':core.store.meta(c,'settings')['timezone']}
    extras=[]
    for key,workflow,frequency in [('daily','checkin','daily'),('weekly','weekly_review','weekly')]:
        rows=[core.store.entity(r) for r in c.execute("SELECT * FROM entities WHERE type='schedule' AND archived=0 AND parent_id IS NULL AND json_extract(data,'$.workflow')=? AND json_extract(data,'$.frequency')=? ORDER BY CASE WHEN json_extract(data,'$.review_setting')=? THEN 0 ELSE 1 END,created_at,rowid", (workflow,frequency,key))]
        if rows:
            selected=rows[0]
            data=selected['data']
            value[key].update(enabled=bool(data.get('enabled',False) and selected['status'] not in {'cancelled','draft'}),time=data.get('time') or value[key]['time'],schedule_id=selected['id'])
            if key=='weekly':
                value[key]['weekday']=data.get('weekday',6)
            if data.get('timezone'):
                value[key]['timezone']=data['timezone']
            extras.extend({'id':e['id'],'title':e['title'],'kind':key} for e in rows[1:] if e['data'].get('enabled'))
    value['duplicate_schedules']=extras
    return value


def set_review_preferences(core,c,p,rid):
    timezone=p.get('timezone') or core.store.meta(c,'settings')['timezone']
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError,TypeError):
        raise BusinessError('validation','请选择有效的时区。')
    for key in ('daily','weekly'):
        d=p.get(key)
        if not isinstance(d,dict) or type(d.get('enabled')) is not bool:
            raise BusinessError('validation','请明确每日复盘和每周回顾是否启用。')
        try:
            t=dt.time.fromisoformat(d['time'])
            if t.tzinfo or t.second or t.microsecond:
                raise ValueError()
        except (ValueError,TypeError,KeyError):
            raise BusinessError('validation','时间格式应为小时:分钟。')
        if key=='weekly' and (type(d.get('weekday')) is not int or not 0<=d['weekday']<=6):
            raise BusinessError('validation','请选择每周回顾的星期。')
    ids={}
    for key,workflow,frequency,label in [('daily','checkin','daily','每日复盘提醒'),('weekly','weekly_review','weekly','每周回顾提醒')]:
        rows=[core.store.entity(r) for r in c.execute("SELECT * FROM entities WHERE type='schedule' AND archived=0 AND parent_id IS NULL AND json_extract(data,'$.workflow')=? AND json_extract(data,'$.frequency')=? ORDER BY CASE WHEN json_extract(data,'$.review_setting')=? THEN 0 ELSE 1 END,created_at,rowid",(workflow,frequency,key))]
        values={'workflow':workflow,'frequency':frequency,'review_setting':key,'enabled':p[key]['enabled'],'time':p[key]['time'],'timezone':timezone}
        if key=='weekly':
            values['weekday']=p[key]['weekday']
        if rows:
            old=rows[0]
            new={**old,'title':label,'status':'active','data':{**old['data'],**values}}
            core._validate(c,new)
            entity=core._save(c,old,new,rid,'review_preferences') if new!=old else old
        else:
            entity=core._create(c,{'type':'schedule','title':label,'status':'active','data':values},rid)
        ids[key]=entity['id']
        # These are the legacy unscoped global reminders now represented by the
        # same settings control. Keep their records, disable duplicate delivery.
        for old in rows[1:]:
            if old['data'].get('enabled'):
                new={**old,'data':{**old['data'],'enabled':False,'superseded_by':entity['id']}}
                core._save(c,old,new,rid,'merge_review_reminder')
    return {'preferences':review_preferences(core,c),'schedule_ids':ids}


def attach_local_file(core,c,p,rid,prepared):
    owner=core.store.get(c,p['owner_id'])
    if owner['archived'] or owner['type'] not in OWNER_TYPES:
        raise BusinessError('file_owner','请选择仍在使用的项目、课程或事项。')
    core._ensure_mutable(c,owner)
    existing=c.execute("SELECT * FROM entities WHERE type='file_reference' AND archived=0 AND parent_id=? AND json_extract(data,'$.path_key')=? LIMIT 1",(owner['id'],prepared['path_key'])).fetchone()
    if existing:
        return {'entity':core.store.entity(existing),'reused':True}
    entity=core._create(c,{'type':'file_reference','title':p.get('title') or Path(prepared['local_path']).name,'parent_id':owner['id'],'data':prepared},rid)
    return {'entity':entity,'reused':False}


def prepare_local_file(p):
    try:
        path=Path(p['path']).expanduser().resolve(strict=True)
    except (OSError,ValueError) as error:
        raise BusinessError('file_unavailable','文件不存在或无法读取，请重新选择本地文件。') from error
    if str(path).startswith('\\\\'):
        raise BusinessError('local_file_required','目前请选择本机文件；网络位置请交由 Codex 单独处理。')
    if not path.is_file():
        raise BusinessError('not_file','请选择已有的本地文件。')
    stat=path.stat()
    return {'local_path':str(path),'path_key':os.path.normcase(str(path)),'size':stat.st_size,'observed_mtime_ns':stat.st_mtime_ns,'reference_only':True,'snapshot':False,'registered_at':now()}


def object_workspace(core,c,p):
    from .presentation import Presenter
    presenter=Presenter(core,c)
    entity=presenter.entity(core.store.get(c,p['id']))
    offset=max(0,int(p.get('offset',0)))
    limit=max(1,min(100,int(p.get('limit',50))))
    separate=bool(p.get('separate_tasks',False))
    condition="parent_id=? AND archived=0 AND type!='file_reference'"+(" AND type!='task'" if separate else '')
    if entity['type']=='course': condition += " AND type!='assessment'"
    child_total=c.execute('SELECT count(*) FROM entities WHERE '+condition,(entity['id'],)).fetchone()[0]
    child_rows=c.execute('SELECT * FROM entities WHERE '+condition+' ORDER BY updated_at DESC,id LIMIT ? OFFSET ?',(entity['id'],limit,offset))
    children={'items':[presenter.entity(core.store.entity(r)) for r in child_rows],'total':child_total,'next_offset':offset+limit if offset+limit<child_total else None}
    from .task_views import summary as task_summary, state as task_state
    total,done,incomplete=task_summary(core,c,entity['id'])
    unknown=total-done-incomplete
    task_states={item['id']:task_state(core,c,item['id']) for item in [entity,*children['items']] if item['type']=='task'}
    files_offset=max(0,int(p.get('files_offset',0)))
    files_limit=max(1,min(100,int(p.get('files_limit',50))))
    file_sql="""SELECT e.* FROM entities e WHERE e.archived=0 AND
      ((e.type='file_reference' AND e.parent_id=?) OR
       (e.type IN ('asset','artifact','bundle') AND EXISTS
        (SELECT 1 FROM links l WHERE (l.source_id=? AND l.target_id=e.id) OR (l.target_id=? AND l.source_id=e.id))))
      ORDER BY e.title,e.id LIMIT ? OFFSET ?"""
    rows=list(c.execute(file_sql,(entity['id'],entity['id'],entity['id'],files_limit+1,files_offset)))
    files=[]
    for row in rows[:files_limit]:
        e=core.store.entity(row)
        e['reference_only']=e['type']=='file_reference'
        e['exists']=(Path(e['data'].get('local_path','')).is_file() if e['reference_only'] else
          True if e['type']=='bundle' else (core.root/'blobs'/e['data'].get('sha256','missing')).is_file())
        files.append(e)
    files_next_offset=files_offset+files_limit if len(rows)>files_limit else None
    course_info=None
    if entity['type']=='course':
        groups={}
        course_limit=max(1,min(100,int(p.get('course_limit',50))))
        for kind,key,where in [('assessment','assessments','parent_id=?'),('event','events',"json_extract(data,'$.owner_id')=?")]:
            group_offset=max(0,int(p.get(key+'_offset',0)))
            groups[key+'_total']=c.execute("SELECT count(*) FROM entities WHERE type=? AND archived=0 AND "+where,(kind,entity['id'])).fetchone()[0]
            groups[key]=[core.store.entity(row) for row in c.execute("SELECT * FROM entities WHERE type=? AND archived=0 AND "+where+" ORDER BY title,id LIMIT ? OFFSET ?",(kind,entity['id'],course_limit,group_offset))]
            groups[key+'_offset']=group_offset
            groups[key+'_next_offset']=group_offset+course_limit if group_offset+course_limit<groups[key+'_total'] else None
        groups['complete']=all(groups[key+'_offset']==0 and groups[key+'_next_offset'] is None for key in ('assessments','events'))
        course_info=groups
    dependencies=None
    if entity['type']=='task':
        dependency_offset=max(0,int(p.get('dependencies_offset',0)))
        dependency_limit=max(1,min(100,int(p.get('dependencies_limit',50))))
        where="l.kind='depends_on' AND (l.source_id=? OR l.target_id=?)"
        args=(entity['id'],entity['id'])
        dependency_total=c.execute('SELECT count(*) FROM links l WHERE '+where,args).fetchone()[0]
        dependency_rows=c.execute('SELECT l.*,a.title AS source_title,b.title AS target_title FROM links l JOIN entities a ON a.id=l.source_id JOIN entities b ON b.id=l.target_id WHERE '+where+' ORDER BY l.id LIMIT ? OFFSET ?',(*args,dependency_limit,dependency_offset))
        dependencies={'items':[dict(row) for row in dependency_rows],'total':dependency_total,'offset':dependency_offset,'next_offset':dependency_offset+dependency_limit if dependency_offset+dependency_limit<dependency_total else None}
    return {'entity':entity,'tasks_separated':separate,'course_info':course_info,'dependencies':dependencies,'children':[x for x in children['items'] if x['type']!='file_reference'],'children_total':children['total'],'offset':offset,'next_offset':children['next_offset'],'files':files,'files_offset':files_offset,'files_next_offset':files_next_offset,'files_has_more':files_next_offset is not None,'summary':{'total_tasks':total,'done_tasks':done,'incomplete_tasks':incomplete,'unknown_tasks':unknown,'active_tasks':total-done,'coverage_label':'任务条目完成情况'},'task_states':task_states}


def open_resource(core,p):
    # Obtain identity/version in a short snapshot; filesystem work follows it.
    with core.store.connect() as c:
        entity=core.store.get(c,p['id'])
    d=entity['data']
    if entity['type']=='asset' and d.get('source_kind')!='web':
        from .library import materialize
        return materialize(core,entity)
    if d.get('source_kind')=='web':
        sha=d.get('extraction',{}).get('text_sha256')
        if not sha:
            raise BusinessError('source_unreadable','网页快照没有可读正文，请在资料预览查看原因。')
        d={**d,'sha256':sha,'original_name':'网页正文.txt'}
    if entity['type']=='file_reference':
        path=Path(d['local_path'])
        return {'path':str(path),'exists':path.is_file(),'reference_only':True,'entity_id':entity['id']}
    if entity['type'] not in {'asset','artifact'} or not d.get('sha256'):
        raise BusinessError('not_file','此记录不是可直接打开的文件。')
    from .resources import _relative,_plain_path,_digest
    name=_relative(d.get('original_name') or entity['title'])
    if '/' in name:
        name=Path(name).name
    source=core.resources._blob(d['sha256'])
    directory=_plain_path(core.root/'exports'/('view-'+entity['id']))
    directory.mkdir(parents=True,exist_ok=True)
    path=_plain_path(directory/name)
    if path.exists():
        if _digest(path)[0]!=d['sha256']:
            raise BusinessError('edited_copy','打开用的副本已被修改，已保留你的修改。请另存后通过 Codex 登记新版本。')
    else:
        with core.resources._reservation(source.stat().st_size):
            with source.open('rb') as src, path.open('xb') as dst:
                shutil.copyfileobj(src,dst,4*1024*1024)
            path.chmod(0o444)
    return {'path':str(path),'exists':True,'reference_only':False,'read_only_copy':True,'entity_id':entity['id']}
