"""Human-readable originals beside the database and immutable content store.

Paths and verification stamps are a rebuildable index, not business memories.
No query rewrites course/task facts or silently imports edits made in Explorer.
"""
from __future__ import annotations
import json
import os
import copy
from functools import wraps
from pathlib import Path, PurePosixPath
import posixpath
import re
import uuid
from .schemas import BusinessError
from .storage import now
from .resources import _plain_path, _digest, _publish_new, ResourceError, RESERVED

ROOT_NAME = '原文件'


def serialized(function):
    @wraps(function)
    def run(core, *args, **kwargs):
        with core.library_lock:
            return function(core, *args, **kwargs)
    return run


def subdirectory(value):
    if value == '': return ''
    value = relative(value)
    if any(part.startswith('.') for part in value.split('/')):
        raise BusinessError('library_path', '新归档目录不能使用隐藏或内部管理目录。')
    return value


def owner_id(c, entity):
    value = entity['data'].get('source_owner_id') or entity.get('parent_id')
    if value is None:
        row = c.execute("SELECT source_id FROM links WHERE target_id=? AND kind IN ('uses','contributes') ORDER BY id LIMIT 1", (entity['id'],)).fetchone()
        if row: value = row[0]
    return value


def _subdirs(core, c, folder):
    """Bounded choices from saved paths and existing folders, never from titles."""
    found = set(); truncated = False
    prefix = folder + '/'
    for row in c.execute("SELECT data FROM entities WHERE type='asset'"):
        data = json.loads(row[0]); path = data.get('library_relative_path')
        if not path and str(data.get('source_identity', '')).startswith('y2s1:') and data.get('source_relative_path'):
            path = 'Y2S1/' + data['source_relative_path']
        if not isinstance(path, str) or not path.casefold().startswith(prefix.casefold()): continue
        parts = path[len(prefix):].split('/')[:-1]
        for index in range(1, min(len(parts), 8) + 1):
            candidate = '/'.join(parts[:index])
            try: subdirectory(candidate)
            except BusinessError: continue
            found.add(candidate)
        if len(found) >= 500: truncated = True; break
    base = _plain_path(root(core) / relative(folder)); queue = [('', base, 0)]; scanned = 0
    while queue and len(found) < 500 and scanned < 5000:
        parent, path, depth = queue.pop(0)
        if not path.exists(): continue
        with os.scandir(path) as entries:
            for entry in entries:
                scanned += 1
                if scanned >= 5000: truncated = True; break
                if not entry.is_dir(follow_symlinks=False): continue
                candidate = (parent + '/' if parent else '') + entry.name
                try:
                    subdirectory(candidate); child = _plain_path(entry.path)
                except (BusinessError, ResourceError): continue
                found.add(candidate)
                if depth < 7: queue.append((candidate, child, depth + 1))
                else: truncated = True
                if len(found) >= 500: truncated = True; break
    if queue: truncated = True
    return sorted(found, key=str.casefold)[:500], truncated


def _selected_subdir(core, c, folder, value):
    if value is not None: return subdirectory(value)
    choices, truncated = _subdirs(core, c, folder)
    if choices or truncated:
        raise BusinessError('library_destination_required', '此归属已有分类目录，请先核对并选择归档文件夹；明确保存到根目录时传入空字符串。',
                            {'query': 'library_destinations', 'subdirs': choices[:20]})
    return ''


def location(core, c, entity):
    data = entity['data']; owner = owner_id(c, entity)
    folder = _folder(core, c, owner)
    cached = c.execute('SELECT * FROM library_files WHERE entity_id=?', (entity['id'],)).fetchone()
    rel = data.get('library_relative_path')
    if not rel and cached and cached['sha256'] == data.get('sha256'): rel = cached['relative_path']
    rel = relative(rel or _preferred(core, c, data, data.get('original_name') or entity['title'], owner))
    parent = posixpath.dirname(rel)
    subdir = '' if parent.casefold() == folder.casefold() else parent[len(folder)+1:] if parent.casefold().startswith(folder.casefold()+'/') else None
    path = _plain_path(root(core) / rel)
    return {'path': str(path), 'relative_path': rel, 'subdir': subdir, 'owner_id': owner,
            'exists': path.is_file(), 'sha256': data.get('sha256'), 'size': data.get('size'), 'read_only_copy': True}


@serialized
def destinations(core, p):
    limit, offset = p.get('limit', 100), p.get('offset', 0)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise BusinessError('validation', '归档目录每页需要 1 至 100 项。')
    with core.store.lock, core.store.connect() as c:
        current = None; owner = p.get('owner_id')
        if p.get('id'):
            entity = core.store.get(c, p['id'])
            if entity['type'] != 'asset' or not entity['data'].get('sha256'):
                raise BusinessError('not_file', '此记录不是已保存的原文件。')
            actual_owner = owner_id(c, entity)
            if owner is not None and owner != actual_owner: raise BusinessError('source_owner', '归属与资料不一致。')
            owner = actual_owner; current = {**location(core,c,entity), 'id':entity['id'], 'version':entity['version']}
            current['library_subdir'] = current['subdir']
        from .sources import _owner
        _owner(core,c,owner)
        folder = _folder(core,c,owner); choices, truncated = _subdirs(core,c,folder)
        rows = [{'subdir':'', 'label':'归属根目录'}] + [{'subdir':value, 'label':value} for value in choices]
    return {'owner_id':owner, 'path':str(root(core)/folder), 'root_relative_path':folder,
            'items':rows[offset:offset+limit], 'total':len(rows),
            'next_offset':offset+limit if offset+limit < len(rows) else None,
            'requires_choice':bool(choices) or truncated, 'truncated':truncated, 'current':current}


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


@serialized
def prepare(core,data,name,owner_id=None,*,library_subdir=None):
    """Publish a newly imported original outside the business transaction."""
    with core.store.lock,core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        from .sources import _owner
        _owner(core,c,owner_id)
        folder = _folder(core,c,owner_id)
        subdir = _selected_subdir(core,c,folder,library_subdir)
        rel = relative(folder + ('/'+subdir if subdir else '') + '/' + safe_name(Path(name).name))
        c.commit()
    for attempt in range(3):
        try:return {**_publish(core,data,rel), 'subdir':subdir, 'path':str(root(core)/rel), 'owner_id':owner_id}
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


@serialized
def materialize(core,entity):
    # Callers may hold an entity snapshot from before a completed refile.
    with core.store.connect() as c: entity=core.store.get(c,entity['id'])
    data=entity['data']
    if entity['type']!='asset' or not data.get('sha256'):
        raise BusinessError('not_file','此记录不是已保存的原文件。')
    with core.store.lock,core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        cached=c.execute('SELECT * FROM library_files WHERE entity_id=?',(entity['id'],)).fetchone()
        stamp=dict(cached) if cached and cached['sha256']==data['sha256'] else None
        owner=owner_id(c,entity)
        rel=data.get('library_relative_path') or (stamp['relative_path'] if stamp else _preferred(core,c,data,data.get('original_name') or entity['title'],owner))
        if stamp and stamp['relative_path'] != rel: stamp=None
        c.commit()
    published=_publish(core,data,rel,stamp)
    if not stamp or any(stamp.get(k)!=published[k] for k in ('relative_path','size','mtime_ns')):
        with core.store.lock,core.store.connect() as c:
            # This is a rebuildable file index, not a course/task edit or revision.
            register(c,entity,published)
    return {'entity_id':entity['id'],'path':str(root(core)/published['relative_path']),
        'exists':True,'reference_only':False,'read_only_copy':True,'library_original':True,**published}


def ensure_same_destination(core, c, entity, requested):
    actual = location(core,c,entity)
    if requested is not None:
        requested = subdirectory(requested)
        if actual['subdir'] != requested:
            raise BusinessError('library_location_conflict', '这份资料已经保存于其他位置；未重复导入或自动移动，请用“移动原件”核对归档位置。',
                                {'entity_id':entity['id'], 'current_relative_path':actual['relative_path'], 'command':'refile_source'})
    return actual


@serialized
def prepare_refile(core, p):
    if set(p) - {'id','version','library_subdir','_operation_id'} or 'library_subdir' not in p:
        raise BusinessError('validation', '移动原件需要资料编号、版本和明确的归属内目录。')
    subdir = subdirectory(p['library_subdir'])
    if type(p.get('version')) is not int or p['version'] < 1:
        raise BusinessError('validation', '请重新读取资料版本后再移动。')
    with core.store.lock, core.store.connect() as c:
        entity = core._versioned(c,p)
        if entity['type'] != 'asset' or entity['archived'] or not entity['data'].get('sha256'):
            raise BusinessError('not_file', '只能移动仍在使用的已保存原文件。')
        old = location(core,c,entity); owner = old['owner_id']
        from .sources import _owner
        _owner(core,c,owner)
        folder = _folder(core,c,owner)
        filename = PurePosixPath(old['relative_path']).name
        target = relative(folder + ('/'+subdir if subdir else '') + '/' + filename)
        plan = {'id':entity['id'], 'version':entity['version'], 'owner_id':owner,
                'sha256':entity['data']['sha256'], 'size':entity['data']['size'],
                'old_relative_path':old['relative_path'], 'new_relative_path':target, 'subdir':subdir}
    path = _plain_path(root(core)/old['relative_path'])
    if path.exists() and _digest(path) != (plan['sha256'],plan['size']):
        raise BusinessError('edited_copy', '原位置的文件已被外部修改，已保留；请先作为新版本导入再整理。')
    try: published = _publish(core,entity['data'],target)
    except (BusinessError,ResourceError) as error:
        if error.code in {'edited_copy','OUTPUT_EXISTS','ALREADY_EXISTS'}:
            raise BusinessError('library_conflict', '目标位置已存在不同内容的同名文件，未覆盖；请先核对目标目录。') from error
        raise
    plan['published'] = published
    return plan


def apply_refile(core, c, p, rid, prepared):
    old = core._versioned(c,p)
    if old['type'] != 'asset' or old['archived']: raise BusinessError('not_file', '此资料当前不可移动。')
    actual = location(core,c,old)
    if (old['id'],old['version'],actual['owner_id'],old['data'].get('sha256'),old['data'].get('size'),actual['relative_path']) != (
            prepared['id'],prepared['version'],prepared['owner_id'],prepared['sha256'],prepared['size'],prepared['old_relative_path']):
        raise BusinessError('entity_conflict', '资料或归属已变化，原文件保留，请重新核对。')
    from .sources import _owner
    _owner(core,c,actual['owner_id'])
    target = _plain_path(root(core)/relative(prepared['new_relative_path']))
    if _digest(target) != (prepared['sha256'],prepared['size']):
        raise BusinessError('edited_copy', '准备好的目标副本发生变化，尚未更新资料位置。')
    new = copy.deepcopy(old)
    new['data'].update(library_relative_path=prepared['new_relative_path'], library_subdir=prepared['subdir'])
    entity = core._save(c,old,new,rid,'refile_source')
    register(c,entity,prepared['published'])
    return {'entity':entity, 'library':location(core,c,entity), 'previous_relative_path':prepared['old_relative_path']}


def _other_reference(core,c,identifier,rel):
    # Multiple asset records may intentionally share independent readable bytes.
    # Unindexed imports and legacy originals also need to retain their copies.
    for row in c.execute("SELECT * FROM entities WHERE type='asset' AND id!=?", (identifier,)):
        entity = core.store.entity(row)
        if not entity['data'].get('sha256'): continue
        try: other = location(core,c,entity)
        except (BusinessError,ResourceError):
            # An unreadable reference is not evidence that deletion is safe.
            return True
        if other['relative_path'].casefold() == rel.casefold(): return True
    return False


def _remove_old(path, digest, size):
    path = _plain_path(path,file=True)
    if _digest(path) != (digest,size):
        raise BusinessError('edited_copy', '旧位置已有外部修改，保留该副本。')
    path = _plain_path(path,file=True)
    path.chmod(0o600)
    path.unlink()


@serialized
def finish_refile(core, request_id):
    """Post-commit cleanup never changes the stable business receipt or record."""
    with core.store.connect() as c:
        row = c.execute("SELECT * FROM io_operations WHERE request_id=? AND command='refile_source'", (request_id,)).fetchone()
        if not row or row['status'] != 'committed':
            return {'status':'pending','message':'归档尚未确认提交，旧位置保留。'}
        plan = json.loads(row['result'])
    cleanup = plan.get('cleanup') or {}
    if cleanup.get('status') in {'removed','not_needed','retained_shared','retained_modified'}: return cleanup
    oldrel = plan['old_relative_path']; newrel = plan['new_relative_path']
    try:
        with core.store.lock, core.store.connect() as c:
            current = core.store.get(c,plan['id']); actual = location(core,c,current)
            if actual['relative_path'].casefold() == oldrel.casefold() or oldrel.casefold() == newrel.casefold():
                cleanup = {'status':'not_needed','message':'当前资料仍使用该位置，无需清理。'}
            elif _other_reference(core,c,plan['id'],oldrel):
                cleanup = {'status':'retained_shared','message':'归档已更新；旧位置仍被其他资料引用，已保留共用副本。'}
            else:
                current_path = _plain_path(actual['path'],file=True)
                if _digest(current_path) != (plan['sha256'],plan['size']):
                    raise BusinessError('library_target_changed', '当前归档文件已变化，暂不清理旧副本。')
                path = _plain_path(root(core)/relative(oldrel))
                if path.exists(): _remove_old(path,plan['sha256'],plan['size'])
                cleanup = {'status':'removed','message':'新位置已保存并核验，旧位置已清理。'}
    except (BusinessError,ResourceError,OSError) as error:
        modified = getattr(error,'code',None) == 'edited_copy'
        cleanup = {'status':'retained_modified' if modified else 'pending',
                   'message':'新位置已保存；旧副本'+('发生变化，已保留。' if modified else '暂未清理，请按原请求编号重试。'),
                   'reason':str(error)[:300]}
    plan['cleanup'] = cleanup
    try:
        with core.store.connect() as c:
            c.execute('UPDATE io_operations SET result=?,updated_at=? WHERE request_id=?',
                      (json.dumps(plan,ensure_ascii=False,sort_keys=True),now(),request_id))
    except Exception:
        # The asset and original business receipt already committed. A replay
        # rechecks the filesystem and completes this bookkeeping safely.
        return {**cleanup,'status':'pending','message':'资料位置已保存；清理状态暂未记录，请按原请求编号重试核对。'}
    return cleanup


@serialized
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
            canonical=entity['data'].get('library_relative_path')
            if stamp and stamp['sha256']==entity['data']['sha256'] and (not canonical or canonical==stamp['relative_path']):
                candidate=_plain_path(root(core)/relative(stamp['relative_path']))
                if candidate.is_file():
                    stat=candidate.stat()
                    if (stat.st_size,stat.st_mtime_ns)==(stamp['size'],stamp['mtime_ns']):continue
            result=materialize(core,entity);created+=int(result['created']);bytes_created+=result['size'] if result['created'] else 0
        except (BusinessError,ResourceError,OSError) as error:
            issues.append({'id':entity['id'],'name':entity['title'],'message':str(error)[:300]})
    return {'path':str(path),'exists':True,'total':total,'processed':len(items),'created':created,
        'bytes_created':bytes_created,'issues':issues,'next_offset':offset+len(items) if offset+len(items)<total else None}
