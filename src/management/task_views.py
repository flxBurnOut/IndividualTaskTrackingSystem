"""Paged current task groups, retaining all completed records and evidence."""
from .schemas import BusinessError
from .storage import encode

ENTITY_COLUMNS=('id','type','title','parent_id','status','archived','data','version','created_at','updated_at')


def rows_sql(where, as_of=None):
    # Compare explicit status changes with completion evidence by audit sequence.
    # Editing a note must not override feedback; explicitly completing/reopening
    # the task must not be hidden by older feedback. No facts are rewritten.
    if as_of is not None:
        from .core import date_value
        as_of = date_value(as_of).isoformat()
    dated = " AND json_extract(f.data,'$.business_date')<='"+as_of+"'" if as_of else ""
    return """WITH raw AS (
      SELECT e.*,
        (SELECT f.id FROM entities f WHERE f.type='feedback' AND f.archived=0
         AND json_extract(f.data,'$.target_id')=+e.id AND json_type(f.data,'$.dimensions.completion') IS NOT NULL"""+dated+"""
         ORDER BY json_extract(f.data,'$.business_date') DESC,f.created_at DESC,f.rowid DESC LIMIT 1) AS feedback_id,
        coalesce((SELECT ch.seq FROM changes ch WHERE ch.entity_id=e.id
          AND json_type(ch.after_value,'$.status') IS NOT NULL
          AND (ch.before_value IS NULL OR json_extract(ch.before_value,'$.status')!=json_extract(ch.after_value,'$.status'))
          ORDER BY ch.seq DESC LIMIT 1),0) AS status_seq
      FROM entities e WHERE e.type='task' AND e.archived=0 AND """+where+"""),
      task_states AS (SELECT raw.*,
        CASE WHEN feedback_id IS NULL OR status_seq > coalesce((SELECT seq FROM changes WHERE entity_id=feedback_id AND action='create' ORDER BY seq LIMIT 1),status_seq)
          THEN CASE status WHEN 'done' THEN 'done' WHEN 'blocked' THEN 'blocked' ELSE NULL END
          ELSE (SELECT json_extract(data,'$.dimensions.completion') FROM entities WHERE id=feedback_id)
        END AS completion_state FROM raw)
      SELECT * FROM task_states"""


def workspace_tasks(core,c,p):
    core.store.get(c,p['id'])
    group=p.get('group','open')
    if group not in {'open','done'}:raise BusinessError('validation','任务分组无效。')
    limit=max(1,min(100,int(p.get('limit',30))));offset=max(0,int(p.get('offset',0)))
    sql=rows_sql('e.parent_id=?')
    counts=c.execute("SELECT count(*) AS total,coalesce(sum(completion_state='done'),0) AS done FROM ("+sql+")",(p['id'],)).fetchone()
    selected="completion_state='done'" if group=='done' else "completion_state IS NULL OR completion_state!='done'"
    total=counts['done'] if group=='done' else counts['total']-counts['done']
    from .presentation import Presenter
    presenter=Presenter(core,c)
    items=[];size=0
    for row in c.execute('SELECT * FROM ('+sql+') WHERE ('+selected+') ORDER BY updated_at DESC,id LIMIT ? OFFSET ?',(p['id'],limit,offset)):
        entity=core.store.entity({k:row[k] for k in ENTITY_COLUMNS});entity['completion_state']=row['completion_state'];entity=presenter.entity(entity)
        cost=len(encode(entity).encode('utf-8'))
        if items and size+cost>512000:break
        items.append(entity);size+=cost
    return {'owner_id':p['id'],'group':group,'items':items,'total':total,
        'counts':{'open':counts['total']-counts['done'],'done':counts['done']},
        'next_offset':offset+len(items) if offset+len(items)<total else None}


def summary(core,c,owner_id,exclude_draft=False):
    scope="e.id IN (WITH RECURSIVE scope(id) AS (SELECT ? UNION SELECT child.id FROM entities child JOIN scope ON child.parent_id=scope.id WHERE child.archived=0) SELECT id FROM scope) AND e.status!='cancelled'"
    if exclude_draft:scope+=" AND e.status!='draft'"
    sql=rows_sql(scope)
    row=c.execute("SELECT count(*) AS total,coalesce(sum(completion_state='done'),0) AS done,coalesce(sum(completion_state IN ('incomplete','partial','not_started','blocked')),0) AS incomplete FROM ("+sql+")",(owner_id,)).fetchone()
    return row['total'],row['done'],row['incomplete']


def state(core,c,identifier,as_of=None):
    row=c.execute(rows_sql('e.id=?',as_of),(identifier,)).fetchone()
    return row['completion_state'] if row else None
