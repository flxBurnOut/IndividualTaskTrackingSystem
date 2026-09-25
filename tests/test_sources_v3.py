import json
from pathlib import Path
import uuid
import pytest
from management.core import Core
from management.schemas import BusinessError
from management import sources
from management.source_worker import parse,html_text


def command(c,name,p):
    state=c.query('state')
    return c.command(name,p,request_id=str(uuid.uuid4()),epoch=state['epoch'],expected_revision=state['revision'])['result']


def course(c):return command(c,'create',{'type':'course','title':'Synthetic course'})['entity']


def test_original_can_move_and_delete_without_losing_saved_source(tmp_path):
    c=Core(tmp_path/'data');owner=course(c)
    path=tmp_path/'intro.txt';path.write_text('Assessment: exam 60%, coursework 40%.',encoding='utf-8')
    first=command(c,'add_source',{'owner_id':owner['id'],'kind':'file','path':str(path)})
    again=command(c,'add_source',{'owner_id':owner['id'],'kind':'file','path':str(path)})
    assert again['reused'] and again['entity']['id']==first['entity']['id']
    moved=tmp_path/'renamed.txt';path.rename(moved);moved.unlink()
    assert 'exam 60%' in c.query('source_content',id=first['entity']['id'])['text']
    opened=c.query('open_resource',id=first['entity']['id'])
    assert opened['exists'] and Path(opened['path']).read_text('utf-8').startswith('Assessment')
    assert c.query('sources',owner_id=owner['id'])['total']==1
    backup=command(c,'backup',{})
    assert first['entity']['data']['sha256']
    assert first['entity']['data']['extraction']['text_sha256']
    assert backup


def test_notice_email_and_image_become_owned_readable_sources(tmp_path):
    c=Core(tmp_path/'data');owner=course(c)
    notice=command(c,'add_source',{'owner_id':owner['id'],'kind':'notice','title':'Changed room','text':'Room changed to L2. No other changes.'})
    eml=tmp_path/'notice.eml';eml.write_bytes(b'Subject: Deadline\nFrom: staff@example.test\nTo: student@example.test\nContent-Type: text/plain; charset=utf-8\n\nSubmit on 2030-01-18.\n')
    email=command(c,'add_source',{'owner_id':owner['id'],'kind':'file','path':str(eml)})
    assert email['entity']['data']['source_kind']=='email'
    assert '2030-01-18' in c.query('source_content',id=email['entity']['id'])['text']
    from PIL import Image
    image=tmp_path/'screenshot.png';Image.new('RGB',(40,30),'white').save(image)
    saved=command(c,'add_source',{'owner_id':owner['id'],'kind':'image','path':str(image)})
    assert saved['extraction']['status']=='vision_ready'
    prepared=sources.prepare_context(c,{'scope':{'kind':'course','entity_id':owner['id']},'source_ids':[notice['entity']['id'],saved['entity']['id']]})
    assert prepared['local_images']==[] and prepared['source_context']==[]
    assert len(prepared['source_versions'])==2
    # Images are supplied as actual MCP image blocks during the bounded read,
    # rather than all being injected into the initial request.


def test_unowned_chat_attachment_and_explicit_empty_selection(tmp_path):
    c=Core(tmp_path/'data');owner=course(c)
    src=command(c,'add_source',{'kind':'notice','text':'Known context only'})['entity']
    assert c.query('sources',owner_id=None)['total']==1
    scope={'kind':'course','entity_id':owner['id']}
    assert sources.prepare_context(c,{'scope':scope,'source_ids':[]})['source_context']==[]
    prepared=sources.prepare_context(c,{'scope':scope,'source_ids':[src['id']]})
    assert prepared['source_versions']=={src['id']:src['version']}


def test_web_snapshot_strips_active_markup_and_opens_plain_text(tmp_path,monkeypatch):
    c=Core(tmp_path/'data');owner=course(c)
    monkeypatch.setattr(sources,'fetch_web',lambda url:{'bytes':b'<h1>Course update</h1><script>bad()</script><p>Friday 10:00</p>','url':url,'media_type':'text/html'})
    source=command(c,'add_source',{'owner_id':owner['id'],'kind':'web','url':'https://example.com/course'})['entity']
    text=c.query('source_content',id=source['id'])['text']
    assert 'Friday 10:00' in text and 'bad()' not in text
    opened=c.query('open_resource',id=source['id'])
    assert Path(opened['path']).suffix=='.txt'
    assert '<script>' not in Path(opened['path']).read_text('utf-8')


@pytest.mark.parametrize('url',['file:///C:/Windows/win.ini','https://user:secret@example.com','http://127.0.0.1','http://[::1]'])
def test_web_rejects_nonpublic_or_credential_urls(url):
    with pytest.raises(BusinessError):sources.fetch_web(url)


def test_pptx_slide_text_and_document_zip_limits(tmp_path):
    import zipfile
    source=tmp_path/'intro.pptx'
    with zipfile.ZipFile(source,'w') as z:
        z.writestr('ppt/slides/slide1.xml','<p:sld xmlns:p="p" xmlns:a="a"><a:t>Exam 60%</a:t><a:t>Coursework 40%</a:t></p:sld>')
        z.writestr('ppt/slides/slide2.xml','<p:sld xmlns:p="p" xmlns:a="a"><a:t>Monday 09:00</a:t></p:sld>')
    result=parse(source,source.name,tmp_path)
    assert 'Exam 60%' in result['text'] and '第2页' in result['text']
    assert result['coverage']['units_total']==2


def test_unsupported_file_preserved_and_no_false_read_claim(tmp_path):
    c=Core(tmp_path/'data');owner=course(c)
    source=tmp_path/'mail.msg';source.write_bytes(b'synthetic unsupported format')
    result=command(c,'add_source',{'owner_id':owner['id'],'kind':'file','path':str(source)})
    assert result['extraction']['status']=='unsupported'
    assert c.query('open_resource',id=result['entity']['id'])['exists']
    assert not c.query('source_content',id=result['entity']['id'])['text']


def test_chart_settings_validate_and_persist(tmp_path):
    c=Core(tmp_path)
    command(c,'settings',{'settings':{'charts':{'weekly_style':'rows'}}})
    assert Core(tmp_path).query('settings')['settings']['charts']['weekly_style']=='rows'
    with pytest.raises(BusinessError):command(c,'settings',{'settings':{'charts':{'weekly_style':'invented'}}})


def test_public_legacy_ai_job_rejects_forged_image_and_conversation(tmp_path):
    c=Core(tmp_path)
    for key,value in [('local_images',['C:/secret.png']),('provider_thread_id','another-thread'),('history',{'messages':[]})]:
        with pytest.raises(BusinessError) as error:command(c,'create_job',{'kind':'ai','input':{'prompt':'hello',key:value}})
        assert error.value.code=='proposal_scope'


def test_course_source_creation_reuses_same_facts_and_rejects_changed_duplicate(tmp_path):
    c=Core(tmp_path);owner=course(c)
    job={'input':json.dumps({'conversation_scope':{'kind':'course','entity_id':owner['id']},'source_versions':{'source-test':1}})}
    action={'command':'create','payload':{'type':'assessment','title':'Final exam','parent_id':owner['id'],'data':{'weight':60,'source_text':'Slide 1: exam60%'}}}
    with c.store.connect() as db:
        first=sources.apply_source_action(c,db,job,action,'synthetic-a')['entity']
        again=sources.apply_source_action(c,db,job,action,'synthetic-b')
        assert again['reused'] and again['entity']['id']==first['id']
        action['payload']['data']['weight']=70
        with pytest.raises(BusinessError) as error:sources.apply_source_action(c,db,job,action,'synthetic-c')
        assert error.value.code=='source_conflict'
    details=c.query('object_workspace',id=owner['id'])['course_info']
    assert details['assessments_total']==1 and details['assessments'][0]['data']['weight']==60
