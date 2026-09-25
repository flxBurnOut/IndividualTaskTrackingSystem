import json
from pathlib import Path
import pytest
from management import context_service as contexts,materials
from management.core import Core
from test_context_service_v14 import command,open_context


def next_step(core,op,**p):
    while True:
        try:return core.query('next_context_step',operation_id=op,**p)
        except Exception as e:
            if getattr(e,'code',None)!='context_checkpoint_required':raise
            contexts.compacted(core,op)


def acknowledge(core,op,step,value=None):
    return core.query('checkpoint_context',operation_id=op,step_key=step['step_key'],delivery_token=step['delivery_token'],
        result=value or {'facts':[],'notes':'No actionable fact in this chunk'})


def source(core,path):
    return command(core,'add_source',{'kind':'file','path':str(path)})['entity']


def test_long_text_all_characters_survive_and_restart_reuses_checkpoint(tmp_path):
    core=Core(tmp_path/'data');path=tmp_path/'long.txt'
    text=('Chinese 中文😀 table\tcolumn\n'*13000)+'LAST_DEADLINE: 2026-10-06'
    path.write_bytes(text.encode('utf-8'));asset=source(core,path)
    op=open_context(core,sources=[asset['id']])['operation_id'];parts=[];keys=[]
    while True:
        step=next_step(core,op)
        if step.get('all_steps_processed'):break
        assert step.get('step_key'),step
        assert contexts.size(step)<=contexts.MAX_PAGE_BYTES
        parts.append(step['content'].get('text',''));keys.append(step['step_key'])
        acknowledge(core,op,step)
        if len(keys)==6:core=Core(core.root)
    assert ''.join(parts)==text
    assert len(keys)==len(set(keys))
    assert 'LAST_DEADLINE' in parts[-1]
    with core.store.connect() as c:
        before=c.execute('SELECT count(*) FROM material_chunks').fetchone()[0]
    reused=source(core,path)
    assert reused['id']==asset['id']
    with core.store.connect() as c:assert before==c.execute('SELECT count(*) FROM material_chunks').fetchone()[0]


def test_more_than_80_pdf_pages_and_last_page_fact(tmp_path):
    from reportlab.pdfgen import canvas
    path=tmp_path/'course.pdf';pdf=canvas.Canvas(str(path))
    for page in range(97):
        pdf.drawString(40,780,f'PAGE {page+1}: '+('FINAL EXAM October 12 weight 40 percent' if page==96 else 'Ordinary class text for source reading.'))
        pdf.showPage()
    pdf.save()
    core=Core(tmp_path/'data');asset=source(core,path)
    op=open_context(core,sources=[asset['id']])['operation_id'];locators=[];last=''
    while True:
        step=next_step(core,op)
        if step.get('all_steps_processed'):break
        locators.append(step['content']['locator']);last=step['content'].get('text','')
        acknowledge(core,op,step)
    assert len(locators)==97
    assert 'FINAL EXAM' in last and '97' in locators[-1]


def test_13_materials_9_images_and_conflicting_evidence(tmp_path):
    from PIL import Image
    core=Core(tmp_path/'data');ids=[]
    for number in range(4):
        path=tmp_path/f'notice-{number}.txt';path.write_text('Quiz due October '+str(3+number),encoding='utf-8')
        ids.append(source(core,path)['id'])
    for number in range(9):
        path=tmp_path/f'capture-{number}.png';Image.new('RGB',(32,32),(number*10,40,50)).save(path)
        ids.append(source(core,path)['id'])
    command(core,'settings',{'settings':{'ai':{'enabled':True}}})
    sent=command(core,'send_message',{'scope':{'kind':'general'},'text':'Read all supplied materials','source_ids':ids})
    assert len(sent['job']['input']['source_ids'])==13
    op=sent['job']['input']['context_operation_id'];images=0;count=0
    while True:
        step=next_step(core,op)
        if step.get('all_steps_processed'):break
        count+=1
        if step['content']['kind']=='image':
            images+=1
            raw=materials.image_bytes(core,op,step['content']['image_ref'])
            assert raw.startswith(b'\x89PNG')
        value={'facts':[{'key':'course.quiz.due','value':str(count),'evidence':step['content']['locator']}]}
        acknowledge(core,op,step,value)
        assert core.query('checkpoint_context',operation_id=op,step_key=step['step_key'],delivery_token=step['delivery_token'],result=value)['reused']
    assert (count,images)==(13,9)
    cursor=None
    while True:
        try:page=core.query('query_context',operation_id=op,collection='facts',cursor=cursor)
        except Exception as e:
            assert e.code=='context_checkpoint_required';contexts.compacted(core,op);continue
        cursor=page.get('next_cursor')
        if not cursor:break
    with core.store.connect() as c:
        candidate,proof=contexts.validate(core,c,contexts._operation(core,c,op),{'summary':'Review conflicts','actions':[],'unknowns':[],'sources':[]})
    assert candidate['material_coverage']['conflicts'][0]['variants']==13


def test_unsupported_original_is_explicit_gap_never_false_full_read(tmp_path):
    core=Core(tmp_path/'data');path=tmp_path/'opaque.xyz';path.write_bytes(b'opaque original')
    asset=source(core,path);op=open_context(core,sources=[asset['id']])['operation_id']
    with core.store.connect() as c:
        with pytest.raises(Exception) as e:contexts.validate(core,c,contexts._operation(core,c,op),{'actions':[]})
        assert e.value.code=='material_incomplete'
    step=next_step(core,op);assert step['content']['kind']=='gap'
    acknowledge(core,op,step)
    with core.store.connect() as c:
        candidate,_=contexts.validate(core,c,contexts._operation(core,c,op),{'actions':[],'unknowns':[]})
    assert candidate['material_coverage']['partial_sources']==1 and candidate['unknowns']


def test_unread_or_conflicting_checkpoint_cannot_be_acknowledged(tmp_path):
    core=Core(tmp_path/'data');path=tmp_path/'one.txt';path.write_text('source')
    asset=source(core,path);op=open_context(core,sources=[asset['id']])['operation_id']
    with pytest.raises(Exception):core.query('checkpoint_context',operation_id=op,step_key=asset['id']+':0',result={'facts':[]})
    step=next_step(core,op);acknowledge(core,op,step)
    with pytest.raises(Exception) as e:acknowledge(core,op,step,{'notes':'Different'})
    assert e.value.code=='checkpoint_conflict'
