"""Managed course sources: immutable originals, bounded previews and AI evidence."""
from __future__ import annotations
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urljoin,urlsplit,urlunsplit

from .schemas import BusinessError
from .storage import now
from .resources import ResourceError,_plain_path,_digest

MAX_FILE=128*1024*1024
MAX_WEB=4*1024*1024
MAX_INPUT_TEXT=200000
MAX_CONTEXT_TEXT=28000
MAX_SOURCES=12
KINDS={'file','notice','email','image','web'}
OWNERS={'domain','project','course','activity','phase','task','goal','note','topic','timetable'}


def _owner(core,c,identifier):
    if identifier is None:return None
    entity=core.store.get(c,identifier)
    builtin=core.types(c).get(entity['type'],{}).get('module') in {None,'builtin'}
    if entity['archived'] or (entity['type'] not in OWNERS and builtin):raise BusinessError('source_owner','请选择仍在使用的课程、项目或事项。')
    core._ensure_mutable(c,entity)
    return entity


def fetch_web(url):
    """Pinned public IP connections; no cookies, credentials, scripts or assets."""
    if not isinstance(url,str) or len(url)>8192:raise BusinessError('web_url','网页链接无效或过长。')
    for _ in range(5):
        value=urlsplit(url)
        if value.scheme not in {'https','http'} or not value.hostname or value.username or value.password:
            raise BusinessError('web_url','请输入不含账号密码的 http 或 https 网页链接。')
        port=value.port or (443 if value.scheme=='https' else 80)
        if port not in {80,443}:raise BusinessError('web_url','网页快照仅支持普通 HTTP/HTTPS 端口。')
        addresses=list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(value.hostname,port,type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise BusinessError('web_private','这个地址不是公开网页。登录页面、内网通知请复制正文或截图保存。')
        # Pin the actual socket to the address that was validated above.
        connection=http.client.HTTPConnection(value.hostname,port,timeout=12)
        raw=socket.create_connection((addresses[0],port),timeout=12)
        connection.sock=ssl.create_default_context().wrap_socket(raw,server_hostname=value.hostname) if value.scheme=='https' else raw
        try:
            path=urlunsplit(('', '', value.path or '/',value.query,''))
            host=value.hostname if port==(443 if value.scheme=='https' else 80) else f'{value.hostname}:{port}'
            connection.request('GET',path,headers={'Host':host,'User-Agent':'PersonalManagement/0.3','Accept':'text/html,text/plain','Accept-Encoding':'identity'})
            response=connection.getresponse()
            if response.status in {301,302,303,307,308}:
                location=response.getheader('Location')
                if not location:raise BusinessError('web_fetch','网页跳转没有提供新地址。')
                url=urljoin(url,location);continue
            if response.status!=200:raise BusinessError('web_fetch',f'网页返回 {response.status}；可改为粘贴正文或截图。')
            media=response.getheader('Content-Type','').lower()
            if not (media.startswith('text/html') or media.startswith('text/plain')):
                raise BusinessError('web_format','此链接不是普通网页正文，请下载文件后添加。')
            body=response.read(MAX_WEB+1)
            if len(body)>MAX_WEB:raise BusinessError('web_limit','网页超过4 MiB快照预算，请复制需要的正文。')
            return {'bytes':body,'url':url,'media_type':media.split(';')[0]}
        finally:connection.close()
    raise BusinessError('web_redirect','网页跳转次数过多，请使用最终页面地址。')


def extract(core,metadata,name,directory,*,layout_mode=None):
    root=Path(directory)
    (root/'input.json').write_text(json.dumps({'source_path':str(core.resources._blob(metadata['sha256'])),'original_name':name,'layout_mode':layout_mode}),encoding='utf-8')
    args=[sys.executable]
    if getattr(sys,'frozen',False):
        sibling=Path(sys.executable).with_name('PersonalManagementService.exe')
        args=[str(sibling),'--source-worker',str(root)]
    else:args+=['-m','management.source_worker',str(root)]
    environment=dict(os.environ);environment['QT_QPA_PLATFORM']='offscreen';environment['PYTHONUTF8']='1'
    with core.resources._reservation((96 if layout_mode == 'timetable' else 8)*1024*1024):
        process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=environment,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        deadline=time.monotonic()+45
        import psutil
        try:
            while process.poll() is None:
                if time.monotonic()>deadline:raise BusinessError('source_timeout','资料已保存，自动读取超过45秒；请分割文档后重试。')
                try:
                    parent=psutil.Process(process.pid);members=[parent,*parent.children(recursive=True)]
                    memory=sum(getattr(p.memory_info(),'private',p.memory_info().rss) for p in members if p.is_running())
                    if memory>768*1024*1024:raise BusinessError('source_memory','资料已保存，自动读取达到768 MiB内存预算。')
                except psutil.NoSuchProcess:pass
                time.sleep(.04)
            result_file=root/'result.json'
            if not result_file.exists():raise BusinessError('source_parse','资料已保存，读取器没有返回结果。')
            if result_file.stat().st_size>65536:
                raise BusinessError('source_metadata','资料已保存，读取结果描述超出64 KiB预算；请分批提供。')
            result=json.loads(result_file.read_text('utf-8'))
        except (BusinessError,OSError,ValueError) as error:
            result={'status':'failed','coverage':{'complete':False},'warnings':[str(error)],'images':[]}
        finally:
            if process.poll() is None:
                try:
                    parent=psutil.Process(process.pid)
                    for child in parent.children(recursive=True):child.kill()
                    parent.kill();process.wait(timeout=5)
                except psutil.NoSuchProcess:pass
    entries=[];text_sha=None;images=[]
    textfile=root/'text.txt'
    if textfile.is_file() and textfile.stat().st_size:
        text_meta=core.resources.import_file(textfile,max_bytes=512000,media_type='text/plain')
        text_sha=text_meta['sha256'];entries.append({**text_meta,'path':'derived/extracted.txt'})
    for image in result.get('images',[])[:8]:
        relative=Path(image['path'])
        if relative.name!=str(relative):raise BusinessError('source_parse','读取器返回了无效的图片路径。')
        meta=core.resources.import_file(root/relative,max_bytes=12*1024*1024,media_type='image/png')
        images.append({'sha256':meta['sha256'],'label':image['label'],'size':meta['size']})
        entries.append({**meta,'path':'derived/'+relative.name})
    extraction={key:result.get(key) for key in ('status','coverage','warnings')}
    extraction['warnings']=[str(w)[:500] for w in (extraction.get('warnings') or [])[:12]]
    if len(json.dumps(extraction.get('coverage') or {},ensure_ascii=False).encode('utf-8'))>16384:
        extraction['coverage']={'complete':False,'metadata_truncated':True}
        extraction['warnings'].append('覆盖明细过多，已保留原件；请分批核对。')
        extraction['status']='partial'
    extraction['text_sha256']=text_sha;extraction['images']=images;extraction['characters']=len(textfile.read_text('utf-8')) if textfile.exists() else 0
    return extraction,entries


def prepare(core,p):
    if p.get('title') is not None and (not isinstance(p['title'],str) or len(p['title'])>300):
        raise BusinessError('source_title','资料名称应不超过300字符。')
    kind=p.get('kind','file')
    if kind not in KINDS:raise BusinessError('source_kind','请选择文件、通知、邮件、截图或网页。')
    with core.store.connect() as c: owner = _owner(core,c,p.get('owner_id'))
    source_url=None;source_path=None
    with tempfile.TemporaryDirectory(prefix='source-',dir=core.resources.root/'.staging') as work:
        root=Path(work)
        if p.get('path'):
            source=_plain_path(p['path'],file=True);source_path=str(source)
            name=source.name
        elif kind in {'notice','email'}:
            text=p.get('text')
            if not isinstance(text,str) or not text.strip():raise BusinessError('source_text','请粘贴需要保存的正文。')
            if len(text)>MAX_INPUT_TEXT:raise BusinessError('source_limit','正文过长，请分成多份资料。')
            name='邮件正文.txt' if kind=='email' else '通知.txt';source=root/name;source.write_text(text,encoding='utf-8')
        elif kind=='web':
            fetched=fetch_web(p.get('url',''));source_url=fetched['url']
            name='网页快照.html' if fetched['media_type']=='text/html' else '网页快照.txt'
            source=root/name;source.write_bytes(fetched['bytes'])
        else:raise BusinessError('source_input','请选择文件，或粘贴正文、截图与网页链接。')
        metadata=core.resources.import_file(source,max_bytes=MAX_FILE)
        if kind=='web':
            identity='web:'+str(source_url).split('#',1)[0]
        elif not p.get('path'):
            with core.store.connect() as c: capture_day=core.today(c)
            identity='pasted:'+capture_day+':'+str(p.get('title') or '')
        else:identity='file'
        if Path(name).suffix.lower()=='.eml':kind='email'
        elif Path(name).suffix.lower() in {'.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff','.gif'}:kind='image'
        # Identical bytes already registered for this owner reuse the same record
        # and derived data. The immutable original is already de-duplicated.
        with core.store.connect() as c:
            row=c.execute("SELECT * FROM entities WHERE type='asset' AND archived=0 AND json_extract(data,'$.source_kind')=? AND json_extract(data,'$.sha256')=? AND json_extract(data,'$.source_owner_id') IS ? AND json_extract(data,'$.source_identity')=? LIMIT 1",(kind,metadata['sha256'],p.get('owner_id'),identity)).fetchone()
            if row:
                existing=core.store.entity(row)
                from .library import materialize
                materialize(core,existing)
                return {'existing_id':existing['id'],'data':existing['data'],'title':existing['title']}
        try:
            extraction,derived=extract(core,metadata,name,root,layout_mode='timetable' if owner and owner['type']=='timetable' else None)
        except (ResourceError,BusinessError,OSError) as error:
            extraction={'status':'failed','coverage':{'complete':False},'warnings':['原件已保存，自动读取未完成：'+str(error)[:300]],'text_sha256':None,'images':[],'characters':0};derived=[]
        data={**metadata,'original_name':name,'source_kind':kind,'source_owner_id':p.get('owner_id'),'managed_copy':True,'source_identity':identity,'source_url':source_url,'original_path':source_path,'captured_at':now(),'extraction':extraction,'entries':[{'path':name,'sha256':metadata['sha256'],'size':metadata['size']},*derived]}
        from .library import prepare as prepare_original
        published=prepare_original(core,data,name,p.get('owner_id'))
        data['library_relative_path']=published['relative_path']
        return {'data':data,'library':published,'title':p.get('title') or (source_url[:300] if kind=='web' else name)}


def add(core,c,p,rid,prepared):
    owner=_owner(core,c,p.get('owner_id'));data=prepared['data']
    row=c.execute("SELECT * FROM entities WHERE type='asset' AND archived=0 AND json_extract(data,'$.source_kind')=? AND json_extract(data,'$.sha256')=? AND json_extract(data,'$.source_owner_id') IS ? AND json_extract(data,'$.source_identity')=? LIMIT 1",(data['source_kind'],data['sha256'],p.get('owner_id'),data['source_identity'])).fetchone()
    if row:return {'entity':core.store.entity(row),'reused':True,'extraction':data['extraction']}
    entity=core._create(c,{'type':'asset','title':prepared['title'],'data':data},rid)
    if owner:core._dispatch(c,'link',{'source_id':owner['id'],'target_id':entity['id'],'kind':'uses'},rid)
    if prepared.get('library'):
        from .library import register
        register(c,entity,prepared['library'])
    return {'entity':entity,'reused':False,'extraction':data['extraction']}


def list_sources(core,c,p):
    owner=p.get('owner_id');limit=max(1,min(100,int(p.get('limit',30))));offset=max(0,int(p.get('offset',0)))
    if owner is not None:core.store.get(c,owner)
    condition="e.type IN ('asset','artifact','file_reference') AND e.archived=0 AND "
    if owner is None:
        condition+="e.type='asset' AND json_extract(e.data,'$.source_kind') IS NOT NULL AND json_extract(e.data,'$.source_owner_id') IS NULL";args=[]
    else:
        condition+="(e.parent_id=? OR EXISTS(SELECT 1 FROM links l WHERE l.source_id=? AND l.target_id=e.id OR l.target_id=? AND l.source_id=e.id))";args=[owner,owner,owner]
    total=c.execute('SELECT count(*) FROM entities e WHERE '+condition,args).fetchone()[0]
    items=[core.store.entity(row) for row in c.execute('SELECT e.* FROM entities e WHERE '+condition+' ORDER BY e.created_at DESC,e.rowid DESC LIMIT ? OFFSET ?',[*args,limit,offset])]
    return {'items':items,'total':total,'next_offset':offset+limit if offset+limit<total else None}


def content(core,p):
    with core.store.connect() as c:entity=core.store.get(c,p['id'])
    extraction=entity['data'].get('extraction') or {'status':'unsupported','coverage':{'complete':False},'warnings':['这份旧记录没有可读取快照。请先保存到软件。']}
    sha=extraction.get('text_sha256');text=core.resources._blob(sha).read_text('utf-8') if sha else ''
    offset=max(0,int(p.get('offset',0)));limit=max(1,min(24000,int(p.get('limit',12000))))
    return {'entity_id':entity['id'],'text':text[offset:offset+limit],'extraction':extraction,'coverage':extraction.get('coverage',{}),'next_offset':offset+limit if offset+limit<len(text) else None,'characters':len(text)}


def prepare_context(core,p):
    scope=p.get('scope') or {};owner=scope.get('entity_id');selected=p.get('source_ids') or []
    if not isinstance(selected,list) or len(selected)>MAX_SOURCES or any(not isinstance(i,str) for i in selected):raise BusinessError('source_limit','一次讨论最多附带12份资料。')
    with core.store.connect() as c:
        from .conversations import query as query_conversation
        conversation=query_conversation(core,c,{'scope':scope})['conversation']
        previous=conversation['source_ids'] if conversation else []
        ids=list(dict.fromkeys(selected if 'source_ids' in p else previous))
        if 'source_ids' not in p and conversation is None and owner:
            available=list_sources(core,c,{'owner_id':owner,'limit':MAX_SOURCES+1})
            # Bulk collections require explicit attachments, but normal discussion remains available.
            ids=[x['id'] for x in available['items']] if available['total']<=MAX_SOURCES else []
        if len(ids)>MAX_SOURCES:raise BusinessError('source_limit','本次讨论累计资料超过12份，请分批另开资料范围。')
        entities=[core.store.get(c,i) for i in ids]
    materials=[];images=[];versions={};total=0
    for entity in entities:
        if entity['archived'] or entity['type'] not in {'asset','artifact'}:raise BusinessError('source_unavailable','资料尚未保存为软件副本，请先在所属课程中选择保存到软件。')
        data=entity['data'];extraction=data.get('extraction') or {};sha=extraction.get('text_sha256')
        text=core.resources._blob(sha).read_text('utf-8') if sha else ''
        total+=len(text)
        if total>MAX_CONTEXT_TEXT:raise BusinessError('source_context_limit','资料正文超过本次讨论预算，请选择较少资料或拆分课件；没有截断后假称已完整读取。')
        if scope.get('kind') == 'timetable' and Path(data.get('original_name') or '').suffix.lower() == '.pdf' and extraction.get('coverage', {}).get('layout_mode') != 'timetable':
            raise BusinessError('timetable_layout_missing', '这份PDF以前只提取了文字，请在每周课表里重新添加原文件，以核对课表行列。')
        current_images=extraction.get('images') or []
        if len(images)+len(current_images)>8:raise BusinessError('source_image_limit','本次最多读取8张图片或扫描页，请分批提供。')
        for item in current_images:
            blob=core.resources._blob(item['sha256'])
            directory=_plain_path(core.root/'exports'/'source-images');directory.mkdir(parents=True,exist_ok=True)
            imagepath=_plain_path(directory/(item['sha256']+'.png'))
            if not imagepath.exists():
                with core.resources._reservation(blob.stat().st_size):
                    import shutil
                    with blob.open('rb') as src,imagepath.open('xb') as dst:shutil.copyfileobj(src,dst,1024*1024)
            elif _digest(imagepath)[0]!=item['sha256']:raise BusinessError('source_integrity','资料图片缓存与保存版本不一致，请重新登记该图片。')
            images.append(str(imagepath))
        materials.append({'id':entity['id'],'title':entity['title'],'version':entity['version'],'kind':data.get('source_kind','file'),'text':text,'coverage':extraction.get('coverage',{'complete':False}),'warnings':extraction.get('warnings',['资料未提取正文，不能声称已读取。']),'sha256':data.get('sha256'),'visuals':[item['label'] for item in current_images]})
        versions[entity['id']]=entity['version']
    return {'source_context':materials,'local_images':images,'source_versions':versions}


def apply_source_action(core,c,job,action,rid):
    """Only deduplicate evidence-backed course creation; changes require updates."""
    value=json.loads(job['input']) if isinstance(job.get('input'),str) else job.get('input') or {}
    scope=value.get('conversation_scope') or {}
    payload=action['payload']
    if action['command']=='correct_recovery_scope' and scope.get('kind')=='course':
        from .catchup import _belongs
        if not _belongs(core,c,core.store.get(c,payload.get('id')),scope['entity_id']):
            raise BusinessError('course_scope','范围更正只能修改当前课程的补欠任务。')
    if scope.get('kind') == 'timetable':
        if action['command'] != 'apply_timetable' or payload.get('id') != scope['entity_id']:
            raise BusinessError('timetable_scope', '课表整理候选只能更新当前课表，请核对后重新生成。')
        if not payload.get('source_text'):
            raise BusinessError('source_evidence', '课表候选需要保留实际资料或用户补充作为依据。')
        payload = {**payload, 'source_versions': value.get('source_versions', {})}
        return core._dispatch(c, action['command'], payload, rid)
    if scope.get('kind')!='course'  or not value.get('source_versions'):
        return core._dispatch(c,action['command'],payload,rid)
    owner=scope['entity_id']
    owned={row[0] for row in c.execute('WITH RECURSIVE tree(id) AS (SELECT ? UNION SELECT e.id FROM entities e JOIN tree t ON e.parent_id=t.id) SELECT id FROM tree',(owner,))}
    def in_scope(identifier):
        if identifier in owned:return True
        row=c.execute("SELECT data FROM entities WHERE id=? AND type='event'",(identifier,)).fetchone()
        return bool(row and json.loads(row[0]).get('owner_id') in owned)
    if action['command'] in {'update','record_feedback'}:
        target=payload.get('id') if action['command']=='update' else payload.get('target_id')
        if not in_scope(target):raise BusinessError('course_scope','课程资料候选不能修改另一课程或范围外的事项。')
        if action['command']=='update':
            patch=payload.get('patch') or {};new_owner=(patch.get('data') or {}).get('owner_id')
            if 'owner_id' in (patch.get('data') or {}) and new_owner not in owned:raise BusinessError('course_scope','课程候选不能把日程改归其他课程。')
    if action['command']=='set_recovery_task':
        if payload.get('course_id') != owner or any(payload.get(key) and not in_scope(payload[key]) for key in ('id', 'original_task_id')):
            raise BusinessError('course_scope', '补课与补欠候选只能登记当前课程的任务。')
        if not payload.get('source_text'):
            raise BusinessError('source_evidence', '补欠登记需要用户明确陈述或实际未完成记录作为依据。')
    if action['command']=='record_recovery_progress' and not in_scope(payload.get('task_id')):
        raise BusinessError('course_scope', '补欠反馈只能更新当前课程的任务。')
    if action['command']=='set_recurring_rule':
        if not in_scope(payload.get('anchor_id')) or (payload.get('id') and not in_scope(payload['id'])):
            raise BusinessError('course_scope','准备规则只能绑定当前课程内的节点。')
        if not payload.get('source_text'):
            raise BusinessError('source_evidence','课程准备规则需要附上资料或用户明确要求作为依据。')
    if action['command']=='create_plan' and any(not in_scope(block.get('target_id')) for block in payload.get('blocks',[])):
        raise BusinessError('course_scope','当前课程的计划候选包含其他范围的事项，请重新核对。')
    if action['command']!='create':return core._dispatch(c,action['command'],payload,rid)
    kind=payload.get('type');data=dict(payload.get('data') or {})
    if kind not in {'assessment','milestone','event','task','topic','note'}:
        raise BusinessError('course_scope','课程资料整理只创建当前课程内的评分、节点、日程、任务或笔记。其他内容请在对应范围讨论。')
    actual_owner=data.get('owner_id') if kind=='event' else payload.get('parent_id')
    if actual_owner!=owner:
        raise BusinessError('course_scope','课程整理生成的事项需要归属当前课程，请让 Codex 修正候选。')
    if not data.get('source_text'):
        raise BusinessError('source_evidence','课程整理候选需要附上来源页码或原文，请让 Codex 补充后保存。')
    ignored={'source_text','source_ids','source_versions','captured_at'}
    expected={k:v for k,v in data.items() if k not in ignored and v is not None and v!=''}
    rows=c.execute("SELECT * FROM entities WHERE type=? AND title=? AND archived=0 AND " + ("json_extract(data,'$.owner_id')=?" if kind=='event' else 'parent_id=?'),(kind,payload['title'],owner))
    for row in rows:
        existing=core.store.entity(row)
        known={k:v for k,v in existing['data'].items() if k not in ignored and v is not None and v!=''}
        if known==expected:return {'entity':existing,'reused':True}
        # Separate dated occurrences are legitimate; other same-name course
        # definitions must be updated deliberately, never silently duplicated.
        if kind!='event' or known.get('date')==expected.get('date'):
            raise BusinessError('source_conflict','已有同名课程事项且内容不同，请核对后更新原记录，避免重复创建。',{'entity_id':existing['id']})
    data['source_versions']=value['source_versions']
    return core._dispatch(c,'create',{**payload,'data':data},rid)
