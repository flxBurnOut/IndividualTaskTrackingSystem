"""Independent source boundaries; only synthetic files, fake sockets and model fixtures."""
from __future__ import annotations
import copy
from email.message import EmailMessage
import hashlib
import io
import json
from pathlib import Path
import socket
from types import SimpleNamespace
import uuid
import zipfile

import pytest
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from management import conversations, sources, source_worker
from management.core import Core
from management.schemas import BusinessError
from management.storage import encode


def cmd(core,name,payload):
    state=core.query('state')
    return core.command(name,payload,request_id=str(uuid.uuid4()),epoch=state['epoch'],expected_revision=state['revision'])['result']


def create(core,kind='course',**extra):
    return cmd(core,'create',{'type':kind,'title':extra.pop('title','Synthetic '+kind),**extra})['entity']


def make_pdf(path,*,text_pages=1,scan_pages=1,mixed=False):
    image=Image.new('RGB',(90,60),'white')
    for x in range(15,75):
        for y in range(20,40): image.putpixel((x,y),(40,80,120))
    writer=canvas.Canvas(str(path),pagesize=(300,300))
    for number in range(text_pages):
        writer.drawString(20,270,'Synthetic text page %d. Assessment weight is sixty percent.'%(number+1))
        if mixed: writer.drawImage(ImageReader(image),30,100,width=180,height=120)
        writer.showPage()
    for _ in range(scan_pages):
        writer.drawImage(ImageReader(image),30,100,width=180,height=120)
        writer.showPage()
    writer.save()


def source_fixture(core,tmp_path,text='Explicit synthetic text',owner=None):
    """Use actual notice ingestion; immutable source metadata is never patched."""
    return cmd(core,'add_source',{'kind':'notice','text':text,'owner_id':owner['id'] if owner else None})['entity']


def candidate(core,owner,source,actions):
    scope={'kind':'course','entity_id':owner['id']}
    sent=cmd(core,'send_message',{'scope':scope,'text':'Synthetic course extraction','source_ids':[source['id']]})
    id=sent['job']['id']
    from context_harness import ready
    ready(core,id,planning=any(a['command']=='create_plan' for a in actions))
    result={'summary':'Synthetic candidate, no actual model invoked','unknowns':[],'sources':[],
            'actions':actions,'provider':{'kind':'synthetic'}}
    with core.store.lock,core.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        job=core._job(c,id)
        c.execute("UPDATE jobs SET status='awaiting_review',result=? WHERE id=?",(encode(result),id))
        assert conversations.complete_job(core,c,job,result)
        c.commit()
    return id


def test_managed_pdf_original_text_and_scan_images_survive_delete_backup_restore(tmp_path):
    core=Core(tmp_path/'data');owner=create(core)
    original=tmp_path/'mixed-source.pdf'
    make_pdf(original)
    original_bytes=original.read_bytes()
    result=cmd(core,'add_source',{'kind':'file','owner_id':owner['id'],'path':str(original)})
    entity=result['entity'];extraction=entity['data']['extraction']
    assert entity['data']['managed_copy'] is True
    assert extraction['text_sha256'] and len(extraction['images'])==1
    assert extraction['images'][0]['label']=='PDF 第 2 页图像'
    original.unlink()
    opened=core.query('open_resource',id=entity['id'])
    assert Path(opened['path']).read_bytes()==original_bytes
    assert 'sixty percent' in core.query('source_content',id=entity['id'])['text']
    expected={entity['data']['sha256'],extraction['text_sha256'],extraction['images'][0]['sha256']}
    backup=cmd(core,'backup',{})
    assert expected <= {item['sha256'] for item in backup['manifest']['assets']}
    destination=tmp_path/'restored'
    cmd(core,'restore_backup',{'path':backup['path'],'target_dir':str(destination)})
    restored=Core(destination)
    assert restored.query('get',id=entity['id'])['entity']['data']['sha256']==hashlib.sha256(original_bytes).hexdigest()
    assert 'sixty percent' in restored.query('source_content',id=entity['id'])['text']
    context=restored.query('prepare_context',goal='Verify restored evidence',scope={'kind':'course','entity_id':owner['id']},source_ids=[entity['id']])
    from context_harness import read
    from management.materials import image_bytes
    op=context['operation_id'];image_ref=None
    while True:
        step=read(restored,'next_context_step',operation_id=op)
        if step.get('all_steps_processed'):break
        if step['content']['kind']=='image':
            image_ref=step['content']['image_ref']
            assert hashlib.sha256(image_bytes(restored,op,image_ref)).hexdigest()==extraction['images'][0]['sha256']
        restored.query('checkpoint_context',operation_id=op,step_key=step['step_key'],delivery_token=step['delivery_token'],result={'facts':[]})
    assert image_ref and not (destination/'runtime.json').exists()
    restored.resources._blob(extraction['images'][0]['sha256']).write_bytes(b'Tampered derived cache')
    with pytest.raises(BusinessError) as error:image_bytes(restored,op,image_ref)
    assert error.value.code=='source_integrity'


def test_nine_scanned_pdf_pages_continue_until_all_nine_are_read(tmp_path):
    core=Core(tmp_path/'data');owner=create(core);path=tmp_path/'nine.pdf'
    make_pdf(path,text_pages=0,scan_pages=9)
    result=cmd(core,'add_source',{'kind':'file','owner_id':owner['id'],'path':str(path)})
    assert len(result['extraction']['images'])<=4
    envelope=core.query('prepare_context',goal='Read all nine pages',source_ids=[result['entity']['id']])
    op=envelope['operation_id'];count=0
    from context_harness import read
    while True:
        step=read(core,'next_context_step',operation_id=op)
        if step.get('all_steps_processed'):break
        assert step['content']['kind']=='image'
        count+=1
        core.query('checkpoint_context',operation_id=op,step_key=step['step_key'],delivery_token=step['delivery_token'],result={'facts':[]})
    assert count==9


def test_pdf_page_limit_and_text_limit_cannot_report_full_coverage(tmp_path):
    path=tmp_path/'many.pdf';make_pdf(path,text_pages=81,scan_pages=0)
    result=source_worker.parse(path,path.name,tmp_path)
    assert result['coverage']['units_total']==81 and result['coverage']['units_read']==80
    assert result['coverage']['complete'] is False and result['status']=='partial'
    assert 'PDF第80页' in result['text'] and 'PDF第81页' not in result['text']
    path=tmp_path/'long.txt';path.write_text('X'*(source_worker.MAX_TEXT+20),encoding='utf-8')
    result=source_worker.parse(path,path.name,tmp_path)
    assert result['status']=='partial' and result['coverage']['complete'] is False
    assert len(result['text'])<=source_worker.MAX_TEXT


def test_mixed_text_and_graphic_pdf_marks_unread_graphic_scope(tmp_path):
    path=tmp_path/'diagram.pdf';make_pdf(path,text_pages=1,scan_pages=0,mixed=True)
    result=source_worker.parse(path,path.name,tmp_path)
    assert result['coverage']['text_complete'] is True
    assert result['coverage']['unread_graphic_pages']==[1]
    assert result['coverage']['complete'] is False and result['status']=='partial'


def test_email_unread_attachment_and_office_embedded_media_are_not_full_coverage(tmp_path):
    mail=EmailMessage();mail['Subject']='Synthetic notice';mail.set_content('The exam is Friday.')
    mail.add_attachment(b'Not parsed attachment',maintype='application',subtype='pdf',filename='assessment.pdf')
    path=tmp_path/'notice.eml';path.write_bytes(mail.as_bytes())
    result=source_worker.parse(path,path.name,tmp_path)
    assert result['coverage']['attachments']==['assessment.pdf']
    assert result['coverage']['complete'] is False and result['status']=='partial'
    office=tmp_path/'course.docx'
    with zipfile.ZipFile(office,'w') as archive:
        archive.writestr('word/document.xml','<w:document xmlns:w="w"><w:t>Known written fact</w:t></w:document>')
        archive.writestr('word/media/image1.png',b'Synthetic uninterpreted embedded bytes')
    result=source_worker.parse(office,office.name,tmp_path)
    assert 'Known written fact' in result['text']
    assert result['coverage']['unread_embedded_objects']==1
    assert result['coverage']['text_complete'] is True and result['coverage']['complete'] is False


def test_chinese_gb18030_is_not_misread_as_unmarked_utf16(tmp_path):
    text='课程成绩：考试占六成，作业占四成。'
    path=tmp_path/'course.txt';path.write_bytes(text.encode('gb18030'))
    assert source_worker.decode(path.read_bytes())==text
    assert text in source_worker.parse(path,path.name,tmp_path)['text']
    assert source_worker.decode(text.encode('utf-16'))==text


def test_document_zip_budget_rejects_before_extraction(tmp_path,monkeypatch):
    path=tmp_path/'expanded.docx'
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml','X'*2048)
    monkeypatch.setattr(source_worker,'MAX_ZIP',1024)
    with pytest.raises(ValueError,match='预算'):
        source_worker.parse(path,path.name,tmp_path)


class WebResponse:
    def __init__(self,status=200,body=b'Public plain text',headers=None):
        self.status,self.body,self.headers=status,body,headers or {'Content-Type':'text/plain'}
        self.read_sizes=[]
    def getheader(self,key,default=None): return self.headers.get(key,default)
    def read(self,amount):
        self.read_sizes.append(amount)
        return self.body[:amount]


def fake_web(monkeypatch,responses,address=None):
    records={'dns':[],'connections':[],'sockets':[],'requests':[]}
    def dns(host,port,**kwargs):
        records['dns'].append(host)
        addresses=(address(host) if address else ['8.8.8.8'])
        return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,port)) for ip in addresses]
    class Connection:
        def __init__(self,host,port,timeout):
            self.closed=False;self.host=host;self.sock=None
            records['connections'].append(self)
        def request(self,method,path,headers): records['requests'].append((method,path,headers))
        def getresponse(self): return responses.pop(0)
        def close(self): self.closed=True
    monkeypatch.setattr(sources.socket,'getaddrinfo',dns)
    monkeypatch.setattr(sources.socket,'create_connection',lambda endpoint,timeout:records['sockets'].append(endpoint) or object())
    monkeypatch.setattr(sources.http.client,'HTTPConnection',Connection)
    return records


def test_web_pins_validated_ip_and_sends_no_cookies_auth_or_fragment(monkeypatch):
    response=WebResponse(headers={'Content-Type':'text/plain; charset=utf-8'})
    records=fake_web(monkeypatch,[response])
    result=sources.fetch_web('http://public.example/path?x=1#fragment')
    assert result['bytes']==b'Public plain text'
    assert records['sockets']==[('8.8.8.8',80)]
    assert records['dns']==['public.example']
    method,path,headers=records['requests'][0]
    assert method=='GET' and path=='/path?x=1'
    assert not {'Cookie','Authorization','Proxy-Authorization'} & set(headers)
    assert headers['Accept-Encoding']=='identity'
    assert response.read_sizes==[sources.MAX_WEB+1]
    assert all(c.closed for c in records['connections'])


def test_web_redirect_to_private_host_is_refused_before_second_connection(monkeypatch):
    records=fake_web(monkeypatch,[WebResponse(status=302,headers={'Location':'http://127.0.0.1/private'})],
                     lambda host:['127.0.0.1'] if host=='127.0.0.1' else ['8.8.8.8'])
    with pytest.raises(BusinessError) as error: sources.fetch_web('http://public.example/start')
    assert error.value.code=='web_private'
    assert records['sockets']==[('8.8.8.8',80)]
    assert records['connections'][0].closed


def test_mixed_public_private_dns_answer_is_refused_without_connecting(monkeypatch):
    records=fake_web(monkeypatch,[],lambda _:['8.8.8.8','10.0.0.1'])
    with pytest.raises(BusinessError) as error: sources.fetch_web('http://mixed.example/')
    assert error.value.code=='web_private' and records['sockets']==[]


@pytest.mark.parametrize('url',['file:///synthetic.txt','http://name:secret@public.example','http://public.example:8080/'])
def test_unsafe_url_shape_is_rejected_before_any_dns(monkeypatch,url):
    records=fake_web(monkeypatch,[])
    with pytest.raises(BusinessError): sources.fetch_web(url)
    assert records['dns']==[] and records['sockets']==[]


@pytest.mark.parametrize('response,code',[
    (WebResponse(headers={'Content-Type':'application/pdf'}),'web_format'),
    (WebResponse(body=b'X'*(sources.MAX_WEB+1)),'web_limit'),
])
def test_web_media_and_body_budget_fail_closed(monkeypatch,response,code):
    records=fake_web(monkeypatch,[response])
    with pytest.raises(BusinessError) as error: sources.fetch_web('http://public.example/')
    assert error.value.code==code
    assert all(c.closed for c in records['connections'])


def test_html_snapshot_strips_active_subtrees_and_never_fetches_assets(tmp_path,monkeypatch):
    core=Core(tmp_path/'data')
    html=b'<h1>Known course notice</h1><script>SECRET_SCRIPT</script><iframe src="http://127.0.0.1">HIDDEN_FRAME</iframe><p>Deadline Friday.</p><img src="http://127.0.0.1/private">'
    records=fake_web(monkeypatch,[WebResponse(body=html,headers={'Content-Type':'text/html'})])
    entity=cmd(core,'add_source',{'kind':'web','url':'http://public.example/course'})['entity']
    text=core.query('source_content',id=entity['id'])['text']
    assert 'Deadline Friday' in text and 'SECRET_SCRIPT' not in text and 'HIDDEN_FRAME' not in text
    opened=core.query('open_resource',id=entity['id'])
    assert Path(opened['path']).suffix=='.txt'
    assert '<script' not in Path(opened['path']).read_text('utf-8')
    assert len(records['requests'])==1
    assert core.resources._blob(entity['data']['sha256']).read_bytes()==html


@pytest.mark.parametrize('budget',['time','memory'])
def test_parser_budget_kills_child_tree_and_retains_registered_original(tmp_path,monkeypatch,budget):
    core=Core(tmp_path/'data');original=tmp_path/'original.txt';original.write_text('Retain this original',encoding='utf-8')
    killed=[]
    process=SimpleNamespace(pid=12345,dead=False)
    process.poll=lambda:0 if process.dead else None
    process.wait=lambda timeout:0
    class Member:
        def __init__(self,name): self.name=name
        def is_running(self): return True
        def memory_info(self): return SimpleNamespace(private=800*1024*1024 if self.name=='parent' else 0,rss=0)
        def children(self,recursive=True): return [Member('child')]
        def kill(self):
            killed.append(self.name)
            if self.name=='parent': process.dead=True
    monkeypatch.setattr(sources.subprocess,'Popen',lambda *a,**kw:process)
    import psutil
    monkeypatch.setattr(psutil,'Process',lambda pid:Member('parent'))
    times=iter([0,46] if budget=='time' else [0,0])
    monkeypatch.setattr(sources.time,'monotonic',lambda:next(times))
    result=cmd(core,'add_source',{'kind':'file','path':str(original)})
    assert result['extraction']['coverage']['extraction_status'] in {'pending','paused'}
    assert result['extraction']['coverage']['complete'] is False
    assert killed==['child','parent']
    assert core.resources._blob(result['entity']['data']['sha256']).read_text('utf-8')=='Retain this original'
    assert core.query('sources')['total']==1


def test_prepare_context_replacement_limits_and_no_silent_text_truncation(tmp_path):
    core=Core(tmp_path/'data');owner=create(core)
    a=source_fixture(core,tmp_path,'Source A',owner);b=source_fixture(core,tmp_path,'Source B',owner)
    cmd(core,'settings',{'settings':{'ai':{'enabled':True}}})
    scope={'kind':'course','entity_id':owner['id']}
    first=cmd(core,'send_message',{'scope':scope,'text':'Synthetic A','source_ids':[a['id']]})
    cmd(core,'cancel_job',{'id':first['job']['id']})
    inherited=sources.prepare_context(core,{'scope':scope})
    assert list(inherited['source_versions'])==[a['id']]
    replaced=sources.prepare_context(core,{'scope':scope,'source_ids':[b['id'],b['id']]})
    assert list(replaced['source_versions'])==[b['id']]
    assert sources.prepare_context(core,{'scope':scope,'source_ids':[]})['source_context']==[]
    oversized=source_fixture(core,tmp_path,'X'*(sources.MAX_CONTEXT_TEXT+1),owner)
    before=core.query('jobs')['total']
    sent=cmd(core,'send_message',{'scope':scope,'text':'Read long material','source_ids':[oversized['id']]})
    assert core.query('jobs')['total']==before+1
    assert core.query('conversation',scope=scope)['conversation']['source_ids']==[oversized['id']]
    from context_harness import materials
    chunks=materials(core,sent['job'])
    assert ''.join(step['content'].get('text','') for step in chunks)=='X'*(sources.MAX_CONTEXT_TEXT+1)


def test_more_than_twelve_sources_requires_selection_instead_of_truncating(tmp_path):
    core=Core(tmp_path/'data');owner=create(core)
    items=[source_fixture(core,tmp_path,'Synthetic '+str(i),owner) for i in range(13)]
    scope={'kind':'course','entity_id':owner['id']}
    context=sources.prepare_context(core,{'scope':scope})
    assert context['source_context']==[] and context['source_versions']=={} and context['source_owner_scope']==owner['id']
    selected=sources.prepare_context(core,{'scope':scope,'source_ids':[item['id'] for item in items[:12]]})
    assert len(selected['source_versions'])==12
    assert len(sources.prepare_context(core,{'scope':scope,'source_ids':[item['id'] for item in items]})['source_versions'])==13


@pytest.mark.parametrize('operation',['update','feedback','plan','event_move','event_clear'])
def test_course_candidate_cannot_change_other_course_or_detach_its_event(tmp_path,operation):
    core=Core(tmp_path/'data');owner=create(core);other=create(core,title='Other course')
    target=create(core,'task',parent_id=other['id'],data={'notes':'Keep other course untouched'})
    event=create(core,'event',data={'date':'2030-01-08','owner_id':owner['id']})
    source=source_fixture(core,tmp_path,'Evidence for only the selected course',owner)
    cmd(core,'settings',{'settings':{'ai':{'enabled':True}}})
    if operation=='update':
        action={'command':'update','payload':{'id':target['id'],'version':target['version'],'patch':{'title':'Wrong overwritten title'}}}
    elif operation=='feedback':
        action={'command':'record_feedback','payload':{'target_id':target['id'],'business_date':'2030-01-07','dimensions':{'completion':'done'},'source_text':'Out of scope'}}
    elif operation=='plan':
        action={'command':'create_plan','payload':{'date':'2030-01-07','mode':'no_precise_time','blocks':[{'target_id':target['id'],'minutes':10}]}}
    else:
        action={'command':'update','payload':{'id':event['id'],'version':event['version'],'patch':{'data':{'owner_id':other['id'] if operation=='event_move' else None}}}}
    id=candidate(core,owner,source,[action])
    before=core.query('state')['revision']
    with pytest.raises(BusinessError) as error: cmd(core,'apply_proposal',{'id':id})
    assert error.value.code=='course_scope'
    assert core.query('state')['revision']==before
    assert core.query('get',id=target['id'])['entity']==target
    assert core.query('get',id=event['id'])['entity']==event
    assert core.query('list',type='feedback')['total']==0


def test_course_duplicate_conflict_rolls_back_all_candidate_actions(tmp_path):
    core=Core(tmp_path/'data');owner=create(core)
    original=create(core,'assessment',title='Final exam',parent_id=owner['id'],data={'weight':60,'source_text':'Confirmed original'})
    source=source_fixture(core,tmp_path,'Different claim must be reviewed',owner)
    cmd(core,'settings',{'settings':{'ai':{'enabled':True}}})
    actions=[{'command':'create','payload':{'type':'topic','title':'Must roll back','parent_id':owner['id'],'data':{'source_text':'Page 1'}}},
             {'command':'create','payload':{'type':'assessment','title':'Final exam','parent_id':owner['id'],'data':{'weight':70,'source_text':'New unadopted claim'}}}]
    id=candidate(core,owner,source,actions)
    with pytest.raises(BusinessError) as error: cmd(core,'apply_proposal',{'id':id})
    assert error.value.code=='source_conflict'
    assert core.query('get',id=original['id'])['entity']==original
    assert core.query('list',type='topic')['total']==0
    assert core.query('list',type='assessment')['total']==1
    assert core.query('job',id=id)['job']['status']=='awaiting_review'


def test_exact_source_candidate_reuses_existing_finished_item_without_changing_status(tmp_path):
    core=Core(tmp_path/'data');owner=create(core)
    original=create(core,'task',title='Existing task',parent_id=owner['id'],status='done',data={'completion_gate':'Known scope','source_text':'Old source'})
    source=source_fixture(core,tmp_path,'Same scope, independently seen',owner)
    cmd(core,'settings',{'settings':{'ai':{'enabled':True}}})
    action={'command':'create','payload':{'type':'task','title':'Existing task','parent_id':owner['id'],'status':'active','data':{'completion_gate':'Known scope','source_text':'New source'}}}
    id=candidate(core,owner,source,[action])
    result=cmd(core,'apply_proposal',{'id':id})
    assert result['results'][0]['reused'] is True
    assert core.query('get',id=original['id'])['entity']==original
    assert core.query('list',type='task')['total']==1



@pytest.mark.parametrize('suffix,main,secondary',[
    ('.docx','word/document.xml','word/header1.xml'),
    ('.pptx','ppt/slides/slide1.xml','ppt/notesSlides/notesSlide1.xml'),
])
def test_secondary_office_text_is_extracted_or_marked_outside_coverage(tmp_path,suffix,main,secondary):
    path=tmp_path/('secondary'+suffix)
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr(main,'<r xmlns:a="a"><a:t>Visible main content</a:t></r>')
        archive.writestr(secondary,'<r xmlns:a="a"><a:t>Critical notice in secondary text</a:t></r>')
    value=source_worker.parse(path,path.name,tmp_path)
    assert 'Critical notice in secondary text' in value['text'] or value['coverage']['complete'] is False


def test_large_email_attachment_metadata_cannot_prevent_original_registration(tmp_path):
    core=Core(tmp_path/'data')
    message=EmailMessage()
    message['Subject']='Bounded metadata fixture'
    message.set_content('Known short body')
    message.add_attachment(b'X',maintype='application',subtype='octet-stream',filename='x'*70000+'.bin')
    path=tmp_path/'large-header.eml'
    path.write_bytes(message.as_bytes())
    expected=hashlib.sha256(path.read_bytes()).hexdigest()
    result=cmd(core,'add_source',{'kind':'email','path':str(path)})
    assert result['entity']['data']['sha256']==expected
    assert result['entity']['data']['managed_copy'] is True
    assert len(encode(result['entity']['data']).encode('utf-8'))<=128*1024
    assert result['extraction']['coverage']['complete'] is False
    assert core.resources._blob(expected).is_file()
