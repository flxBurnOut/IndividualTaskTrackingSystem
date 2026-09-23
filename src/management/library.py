"""Human-readable originals beside the database and immutable content store.

Paths and verification stamps are a rebuildable index, not business memories.
No query rewrites course/task facts or silently imports edits made in Explorer.
"""
from __future__ import annotations
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import uuid
from .schemas import BusinessError
from .resources import _plain_path, _digest, _publish_new, ResourceError, RESERVED

ROOT_NAME = '原文件'


def initialize(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS library_folders(
        owner_id TEXT PRIMARY KEY REFERENCES entities(id), relative_path TEXT NOT NULL COLLATE NOCASE);
        CREATE TABLE IF NOT EXISTS library_files(
        entity_id TEXT PRIMARY KEY REFERENCES entities(id), sha256 TEXT NOT NULL,
        relative_path TEXT NOT NULL COLLATE NOCASE, size INTEGER, mtime_ns INTEGER);
        CREATE INDEX IF NOT EXISTS library_path ON library_files(relative_path);
    ''')


def relative(value):
    # Legacy .agents/.gitignore files are inert originals in this separate tree.
    if not isinstance(value,str) or not value or len(value)>1024 or '\\' in value:
        raise BusinessError('library_path','原文件目录需要有效的相对路径。')
    parts=value.split('/')
    if len(parts)>64 or any(not x or x in ('.','..') or x.endswith(('.', ' ')) or
        x.split('.')[0].upper() in RESERVED or any(ord(ch)<32 or ch in ':<>"|?*' for ch in x) for x in parts):
        raise BusinessError('library_path','原文件路径包含不安全的目录名称。')
    return '/'.join(parts)


def safe_name(value):
    name=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',str(value)).strip(' .')[:130].rstrip(' .') or '资料'
    if name.split('.')[0].upper() in RESERVED:name='_'+name
    return name


def root(core):
    path=_plain_path(core.root/ROOT_NAME);path.mkdir(exist_ok=True)
    return path


def _folder(core,c,owner_id,seen=()):
    if owner_id is None:return '未归类'
    if owner_id in seen or len(seen)>20:raise BusinessError('library_owner','资料归属层级需要核对。')
    cached=c.execute('SELECT relative_path FROM library_folders WHERE owner_id=?',(owner_id,)).fetchone()
    if cached:return relative(cached[0])
    owner=core.store.get(c,owner_id)
    candidates=[]
    for row in c.execute("""SELECT json_extract(data,'$.source_relative_path') FROM entities
        WHERE type='asset' AND json_extract(data,'$.source_owner_id')=?
        AND json_extract(data,'$.source_identity') LIKE 'y2s1:%'""",(owner_id,)):
        if row[0]:candidates.append(posixpath.dirname(relative(row[0])))
    common=posixpath.commonpath(candidates) if candidates else ''
    if candidates:folder='Y2S1'+('/'+common if common else '')
    else:
        category={'course':'课程','project':'项目','domain':'领域','activity':'活动','timetable':'课表'}.get(owner['type'],'事项')
        parent=_folder(core,c,owner['parent_id'],(*seen,owner_id)) if owner.get('parent_id') else category
        folder=parent+'/'+safe_name(owner['title'])
    existing=c.execute('SELECT owner_id FROM library_folders WHERE relative_path=?',(folder,)).fetchone()
    if not candidates and existing and existing[0]!=owner_id:folder+='_'+owner_id[:8]
    c.execute('INSERT INTO library_folders(owner_id,relative_path) VALUES (?,?)',(owner_id,relative(folder)))
    return folder


def folder_path(core,owner_id=None):
    if owner_id is None:return root(core)
    with core.store.lock,core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE');folder=_folder(core,c,owner_id);c.commit()
    path=_plain_path(root(core)/relative(folder));path.mkdir(parents=True,exist_ok=True)
    return path


def _preferred(core,c,data,name,owner_id):
    if data.get('library_relative_path'):return relative(data['library_relative_path'])
    if str(data.get('source_identity','')).startswith('y2s1:') and data.get('source_relative_path'):
        return 'Y2S1/'+relative(data['source_relative_path'])
    return _folder(core,c,owner_id)+'/'+safe_name(Path(name).name)


def _publish(core,data,rel,stamp=None):
    rel=relative(rel);path=_plain_path(root(core)/rel);path.parent.mkdir(parents=True,exist_ok=True)
    path=_plain_path(path)
    if path.exists():
        stat=path.stat()
        if stamp and stamp.get('size')==stat.st_size and stamp.get('mtime_ns')==stat.st_mtime_ns:
            return {'relative_path':rel,'size':stat.st_size,'mtime_ns':stat.st_mtime_ns,'created':False}
        if _digest(path)!=(data['sha256'],data['size']):
            raise BusinessError('edited_copy','目录中的原文件已被修改，已保留该文件。请通过“添加资料或通知”保存为新版本。',{'path':str(path)})
        return {'relative_path':rel,'size':stat.st_size,'mtime_ns':stat.st_mtime_ns,'created':False}
    source=core.resources._blob(data['sha256'],verify=False)
    # Stage near the data root: a long legacy parent plus a UUID temporary
    # name can exceed Win32 MAX_PATH even when the final filename is valid.
    partial=_plain_path(core.resources.root/'.staging'/('library-'+uuid.uuid4().hex+'.partial'))
    with core.resources._reservation(data['size']):
        try:
            core.resources._copy_verified(source,partial,data['sha256'],data['size'])
            _plain_path(path)
            _publish_new(partial,path)
        finally:
            if partial.exists():partial.unlink()
    # Independent bytes, never a writable hardlink into the immutable store.
    if data.get('source_mtime_ns'):
        os.utime(path,ns=(data['source_mtime_ns'],data['source_mtime_ns']))
    path.chmod(0o444)
    stat=path.stat()
    return {'relative_path':rel,'size':stat.st_size,'mtime_ns':stat.st_mtime_ns,'created':True}


def prepare(core,data,name,owner_id=None):
    """Publish a newly imported original outside the business transaction."""
    with core.store.lock,core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE');rel=_preferred(core,c,data,name,owner_id);c.commit()
    for attempt in range(3):
        try:return _publish(core,data,rel)
        except (BusinessError,ResourceError) as error:
            if error.code not in {'edited_copy','OUTPUT_EXISTS','ALREADY_EXISTS'}:raise
            # A different version/name collision never replaces an existing file.
            path=PurePosixPath(rel)
            suffix=data['sha256'][:12] if attempt==0 else uuid.uuid4().hex[:8]
            rel=str(path.with_name(path.stem+' ('+suffix+')'+path.suffix))
    raise BusinessError('library_conflict','同名原文件冲突，请更改文件名称后重试。')


def register(c,entity,published):
    c.execute('''INSERT INTO library_files(entity_id,sha256,relative_path,size,mtime_ns) VALUES (?,?,?,?,?)
        ON CONFLICT(entity_id) DO UPDATE SET sha256=excluded.sha256,relative_path=excluded.relative_path,
        size=excluded.size,mtime_ns=excluded.mtime_ns''',
        (entity['id'],entity['data']['sha256'],published['relative_path'],published['size'],published['mtime_ns']))


def materialize(core,entity):
    data=entity['data']
    if entity['type']!='asset' or not data.get('sha256'):
        raise BusinessError('not_file','此记录不是已保存的原文件。')
    with core.store.lock,core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        cached=c.execute('SELECT * FROM library_files WHERE entity_id=?',(entity['id'],)).fetchone()
        stamp=dict(cached) if cached and cached['sha256']==data['sha256'] else None
        owner_id=data.get('source_owner_id') or entity.get('parent_id')
        if owner_id is None:
            owner=c.execute("SELECT source_id FROM links WHERE target_id=? AND kind IN ('uses','contributes') LIMIT 1",(entity['id'],)).fetchone()
            if owner:owner_id=owner[0]
        rel=stamp['relative_path'] if stamp else _preferred(core,c,data,data.get('original_name') or entity['title'],owner_id)
        c.commit()
    published=_publish(core,data,rel,stamp)
    if not stamp or any(stamp.get(k)!=published[k] for k in ('relative_path','size','mtime_ns')):
        with core.store.lock,core.store.connect() as c:
            # This is a rebuildable file index, not a course/task edit or revision.
            register(c,entity,published)
    return {'entity_id':entity['id'],'path':str(root(core)/published['relative_path']),
        'exists':True,'reference_only':False,'read_only_copy':True,'library_original':True,**published}


def browse(core,p):
    owner_id=p.get('owner_id');limit=p.get('limit',20);offset=p.get('offset',0)
    if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or offset<0:
        raise BusinessError('validation','文件夹分页范围应为 1 到 100 项。')
    path=folder_path(core,owner_id)
    with core.store.connect() as c:
        where="e.type='asset' AND json_extract(e.data,'$.sha256') IS NOT NULL";args=[]
        if owner_id:
            scope='WITH RECURSIVE scope(id) AS (SELECT ? UNION SELECT e.id FROM entities e JOIN scope s ON e.parent_id=s.id) '
            where+=' AND (json_extract(e.data,\'$.source_owner_id\') IN (SELECT id FROM scope) OR e.parent_id IN (SELECT id FROM scope) OR EXISTS (SELECT 1 FROM links l WHERE l.target_id=e.id AND l.source_id IN (SELECT id FROM scope)))'
            args.append(owner_id)
        else:scope=''
        total=c.execute(scope+'SELECT count(*) FROM entities e WHERE '+where,args).fetchone()[0]
        items=[core.store.entity(r) for r in c.execute(scope+'SELECT e.* FROM entities e WHERE '+where+' ORDER BY e.id LIMIT ? OFFSET ?',[*args,limit,offset])]
        stamps={r['entity_id']:dict(r) for r in c.execute('SELECT * FROM library_files WHERE entity_id IN ('+','.join('?' for _ in items)+')',[e['id'] for e in items])} if items else {}
    created=0;issues=[];bytes_created=0
    for entity in items:
        try:
            stamp=stamps.get(entity['id'])
            if stamp and stamp['sha256']==entity['data']['sha256']:
                candidate=_plain_path(root(core)/relative(stamp['relative_path']))
                if candidate.is_file():
                    stat=candidate.stat()
                    if (stat.st_size,stat.st_mtime_ns)==(stamp['size'],stamp['mtime_ns']):continue
            result=materialize(core,entity);created+=int(result['created']);bytes_created+=result['size'] if result['created'] else 0
        except (BusinessError,ResourceError,OSError) as error:
            issues.append({'id':entity['id'],'name':entity['title'],'message':str(error)[:300]})
    return {'path':str(path),'exists':True,'total':total,'processed':len(items),'created':created,
        'bytes_created':bytes_created,'issues':issues,'next_offset':offset+len(items) if offset+len(items)<total else None}
