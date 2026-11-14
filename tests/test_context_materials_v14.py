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
            with core.store.connect() as c:
                if not contexts._operation(core,c,op)['job_id']:raise
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


def test_four_page_pdf_text_and_images_finish_without_worker_or_budget_reset(tmp_path):
    from reportlab.pdfgen import canvas
    path=tmp_path/'four-pages.pdf';pdf=canvas.Canvas(str(path))
    for number in range(4):
        pdf.drawString(40,780,f'PAGE {number+1} '+('FINAL RUBRIC' if number==3 else 'Instructions'))
        for line in range(16):pdf.drawString(40,750-line*20,'Text evidence. '*28)
        pdf.rect(40,100,300,200);pdf.showPage()
    pdf.save()
    core=Core(tmp_path/'data');asset=source(core,path)
    op=open_context(core,sources=[asset['id']])['operation_id'];kinds=[];texts=[]
    before=core.query('state')['revision']
    while True:
        step=core.query('next_context_step',operation_id=op)
        if step.get('all_steps_processed'):break
        kinds.append(step['content']['kind']);texts.append(step['content'].get('text',''))
        if step['content']['kind']=='image':
            assert materials.image_bytes(core,op,step['content']['image_ref']).startswith(b'\x89PNG')
        acknowledge(core,op,step)
    assert kinds.count('image')==4 and 'FINAL RUBRIC' in ''.join(texts)
    status=core.query('operation_status',operation_id=op)
    assert status['materials']['steps']=={'completed':len(kinds)}
    assert status['delivered_bytes']>contexts.STAGE_BYTES and status['stage']==0
    assert not status['checkpoint_required'] and core.query('state')['revision']==before


@pytest.mark.parametrize('reader',['read_material','next_context_step'])
def test_legacy_resume_does_not_deliver_changed_source_version(tmp_path,reader):
    core=Core(tmp_path/'data');path=tmp_path/'version.txt';path.write_text('Original')
    asset=source(core,path);op=open_context(core,sources=[asset['id']])['operation_id']
    core.query('next_context_step',operation_id=op)
    with core.store.connect() as c:
        c.execute("UPDATE context_operations SET phase='checkpointed',delivered_bytes=100000 WHERE id=?",(op,))
        c.execute('UPDATE entities SET version=version+1 WHERE id=?',(asset['id'],))
    with pytest.raises(Exception) as error:
        core.query(reader,operation_id=op,source_id=asset['id'],**({'chunk':0} if reader=='read_material' else {}))
    assert error.value.code=='context_changed'


def test_oversized_checkpoint_summary_preserves_prior_progress(tmp_path):
    core=Core(tmp_path/'data');path=tmp_path/'summary.txt';path.write_text('Evidence')
    asset=source(core,path);op=open_context(core,sources=[asset['id']])['operation_id']
    core.query('checkpoint_context',operation_id=op,summary='Prior summary')
    step=core.query('next_context_step',operation_id=op)
    oversized='摘要😀'*700
    with pytest.raises(Exception) as error:
        core.query('checkpoint_context',operation_id=op,step_key=step['step_key'],
            delivery_token=step['delivery_token'],summary=oversized,result={'notes':'New evidence'})
    assert error.value.code=='checkpoint_limit'
    assert error.value.details['field']=='summary' and error.value.details['max_bytes']==4000
    assert error.value.details['actual_bytes']==contexts.size(oversized)
    assert error.value.details['encoding']=='json_utf8' and error.value.details['progress_preserved']
    with core.store.connect() as c:
        assert contexts._operation(core,c,op)['checkpoint']['summary']=='Prior summary'
        assert c.execute('SELECT status FROM context_steps WHERE operation_id=?',(op,)).fetchone()[0]=='delivered'


def test_legacy_material_resume_keeps_acknowledged_steps_and_source_identity(tmp_path):
    core=Core(tmp_path/'data');path=tmp_path/'resume.txt';path.write_text('First and later evidence. '*300)
    asset=source(core,path);op=open_context(core,sources=[asset['id']])['operation_id']
    first=core.query('next_context_step',operation_id=op)
    result={'facts':[{'key':'already_read','value':'first','evidence':first['content']['locator']}]}
    acknowledge(core,op,first,result)
    core.query('checkpoint_context',operation_id=op,summary='Resume the next unread chunk.')
    with core.store.connect() as c:
        saved=dict(c.execute('SELECT * FROM context_steps WHERE operation_id=? AND step_key=?',(op,first['step_key'])).fetchone())
        bound=dict(c.execute('SELECT * FROM context_sources WHERE operation_id=?',(op,)).fetchone())
        c.execute("UPDATE context_operations SET phase='budget_stop',delivered_bytes=100000,tool_calls=150 WHERE id=?",(op,))
    before=core.query('state')['revision'];core=Core(core.root)
    resumed=core.query('next_context_step',operation_id=op)
    assert resumed['step_key']!=first['step_key'] and resumed['chunk']==first['chunk']+1
    assert resumed['source_id']==first['source_id'] and resumed['source_version']==first['source_version']
    status=core.query('operation_status',operation_id=op)
    assert status['checkpoint']['summary']=='Resume the next unread chunk.'
    assert status['stage']==0 and status['delivered_bytes']>100000 and status['tool_calls']>150
    with core.store.connect() as c:
        assert dict(c.execute('SELECT * FROM context_steps WHERE operation_id=? AND step_key=?',(op,first['step_key'])).fetchone())==saved
        assert dict(c.execute('SELECT * FROM context_sources WHERE operation_id=?',(op,)).fetchone())==bound
    assert core.query('state')['revision']==before


@pytest.mark.parametrize('reader',['read_material','next_context_step'])
def test_managed_image_read_still_requires_real_stage_reset(tmp_path,reader):
    from PIL import Image
    core=Core(tmp_path/'data');path=tmp_path/'managed.png';Image.new('RGB',(32,32),'red').save(path)
    asset=source(core,path);command(core,'settings',{'settings':{'ai':{'enabled':True}}})
    sent=command(core,'send_message',{'scope':{'kind':'general'},'text':'Read image','source_ids':[asset['id']]})
    op=sent['job']['input']['context_operation_id']
    with core.store.connect() as c:c.execute('UPDATE context_operations SET delivered_bytes=49000 WHERE id=?',(op,))
    params={'source_id':asset['id'],**({'chunk':0} if reader=='read_material' else {})}
    with pytest.raises(Exception) as error:core.query(reader,operation_id=op,**params)
    assert error.value.code=='context_checkpoint_required'
    status=core.query('operation_status',operation_id=op)
    assert status['phase']=='budget_stop' and status['next_action']=='save_checkpoint_and_yield'
    core.query('checkpoint_context',operation_id=op,summary='Image still unread',**{'yield':True})
    with pytest.raises(Exception) as error:core.query(reader,operation_id=op,**params)
    assert error.value.code=='context_checkpoint_required'
    contexts.compacted(core,op)  # Test models the confirmed provider event.
    image=core.query(reader,operation_id=op,**params)
    assert image['content']['kind']=='image'
    with core.store.connect() as c:assert contexts._operation(core,c,op)['stage']==1


@pytest.mark.parametrize('managed',[False,True])
def test_repeated_step_checkpoint_saves_new_summary_and_yield_without_reapplying_actions(tmp_path,managed):
    core=Core(tmp_path/'data');path=tmp_path/'repeat.txt';path.write_text('Original evidence')
    asset=source(core,path)
    if managed:
        command(core,'settings',{'settings':{'ai':{'enabled':True}}})
        sent=command(core,'send_message',{'scope':{'kind':'general'},'text':'Read evidence','source_ids':[asset['id']]})
        op=sent['job']['input']['context_operation_id']
    else:op=open_context(core,sources=[asset['id']])['operation_id']
    # A replacement row is intentionally no longer present on retry: the step
    # is already saved and must not execute its replace_actions a second time.
    with core.store.connect() as c:
        c.execute("INSERT INTO context_actions(operation_id,fingerprint,command,payload,reason) VALUES (?,?,'create','{}','old draft')",(op,'old'))
        old=c.execute('SELECT seq FROM context_actions WHERE operation_id=?',(op,)).fetchone()[0]
    step=core.query('next_context_step',operation_id=op)
    result={'notes':'Checked original','actions':[],'replace_actions':[old]}
    acknowledge(core,op,step,result)
    with core.store.connect() as c:
        before=dict(c.execute('SELECT * FROM context_steps WHERE operation_id=?',(op,)).fetchone())
    repeated=core.query('checkpoint_context',operation_id=op,step_key=step['step_key'],
        delivery_token=step['delivery_token'],result=result,summary='Continue after this acknowledged step.',**{'yield':True})
    assert repeated['reused'] and repeated['continue_in_same_operation']
    assert repeated['end_turn_for_compaction'] is managed
    assert repeated['next_action']==('await_managed_compaction' if managed else 'continue_reading')
    with core.store.connect() as c:
        assert contexts._operation(core,c,op)['checkpoint']['summary']=='Continue after this acknowledged step.'
        assert dict(c.execute('SELECT * FROM context_steps WHERE operation_id=?',(op,)).fetchone())==before


@pytest.mark.parametrize('failure,code',[
    ('missing','source_missing'),('permission','source_permission'),('resource','NOT_A_FILE')])
def test_material_read_failure_keeps_specific_code_and_gap_evidence(tmp_path,monkeypatch,failure,code):
    from management.resources import ResourceError
    core=Core(tmp_path/'data');path=tmp_path/'unavailable.txt';path.write_text('Saved original')
    asset=source(core,path)
    # A new layout uses a separate extraction catalog while retaining the same
    # stored original. Only the simulated I/O failure differs between cases.
    def unavailable(*args,**kwargs):
        if failure=='missing':raise FileNotFoundError('synthetic missing file')
        if failure=='permission':raise PermissionError('synthetic access denied')
        raise ResourceError('NOT_A_FILE','Saved resource is unavailable',{'synthetic':True})
    monkeypatch.setattr(core.resources,'_blob',unavailable)
    op=open_context(core,scope={'kind':'timetable','date':'2026-09-25'},sources=[asset['id']])['operation_id']
    step=core.query('next_context_step',operation_id=op)
    assert step['content']['kind']=='gap' and step['content']['error']['code']==code
    assert step['content']['error']['message']==step['content']['text']
    if failure=='resource':assert step['content']['error']['details']=={'synthetic':True}
    status=core.query('operation_status',operation_id=op)
    assert not status['checkpoint_required'] and status['next_action']=='continue_reading'


def test_malformed_document_is_parse_failure_not_file_permission_or_budget(tmp_path):
    core=Core(tmp_path/'data');path=tmp_path/'broken.pdf';path.write_bytes(b'%PDF broken synthetic document')
    asset=source(core,path);op=open_context(core,sources=[asset['id']])['operation_id']
    step=core.query('next_context_step',operation_id=op)
    assert step['content']['kind']=='gap'
    assert step['content']['error']['code']=='source_parse'
    assert step['content']['error']['message']


@pytest.mark.parametrize('failure,code',[('missing','source_missing'),('permission','source_permission'),('parse','source_parse')])
def test_worker_failure_preserves_category_for_parent(tmp_path,monkeypatch,failure,code):
    from management.source_worker import worker
    from management import material_worker
    workspace=tmp_path/'worker';workspace.mkdir()
    request={'batch':True,'source_path':str(tmp_path/'unavailable.txt'),'original_name':'unavailable.txt'}
    (workspace/'input.json').write_text(json.dumps(request),encoding='utf-8')
    if failure!='missing':
        def fail(*args,**kwargs):
            if failure=='permission':raise PermissionError('Synthetic worker permission failure')
            raise ValueError('Synthetic invalid document')
        monkeypatch.setattr(material_worker,'parse_batch',fail)
    assert worker(workspace)==0
    result=json.loads((workspace/'result.json').read_text('utf-8'))
    assert not result['ok'] and result['error']['code']==code and result['warnings']
    error=materials._worker_failure(result)
    assert error.code==code and error.message==result['error']['message']


@pytest.mark.parametrize('code',[[],{},None,'unknown_reader_error'])
def test_malformed_worker_error_still_reports_parse_failure(code):
    error=materials._worker_failure({'ok':False,'error':{'code':code},'warnings':['Synthetic invalid reader response']})
    assert error.code=='source_parse' and error.message=='Synthetic invalid reader response'


# Batch transport: complete evidence, resumability and atomic acknowledgement.
def batch_ack(core, op, items, **extra):
    return core.query('checkpoint_context_batch', operation_id=op,
        items=[{'step_key': x['step_key'], 'delivery_token': x['delivery_token'], 'result': {}} for x in items], **extra)


def test_batch_materials_loss_restart_and_exact_text_with_fewer_calls(tmp_path,record_testsuite_property):
    core=Core(tmp_path/'data');path=tmp_path/'batch.txt'
    original=('Sequential evidence 0123456789. '*5000)+'终点😀\"\\final'
    path.write_text(original,encoding='utf-8');asset=source(core,path)
    op=open_context(core,sources=[asset['id']])['operation_id'];before=core.query('state')['revision']
    first=core.query('read_material_batch',operation_id=op)
    assert len(first['items'])>1 and not first['all_steps_processed']
    core=Core(core.root)  # Response was lost before the assistant acknowledged it.
    replay=core.query('read_material_batch',operation_id=op)
    assert replay['items']==first['items']
    parts=[];keys=[];calls=2;batch=replay
    while batch['items']:
        parts.extend(x['content'].get('text','') for x in batch['items'])
        keys.extend(x['step_key'] for x in batch['items'])
        receipt=batch_ack(core,op,batch['items']);calls+=1
        if len(parts)==len(first['items']):
            core=Core(core.root)  # Save committed but the receipt was lost.
            again=batch_ack(core,op,batch['items']);calls+=1
            assert again['reused_count']==len(batch['items']) and again['atomic']
        batch=core.query('read_material_batch',operation_id=op);calls+=1
    assert batch['all_steps_processed'] and not batch['extraction_pending']
    assert ''.join(parts)==original and len(keys)==len(set(keys))
    record_testsuite_property('batch_material_units',len(keys))
    record_testsuite_property('batch_calls_including_retries',calls)
    record_testsuite_property('single_unit_minimum_calls',len(keys)*2+1)
    assert calls < len(keys)  # At least a 2x improvement even including both retries.
    assert core.query('state')['revision']==before


@pytest.mark.parametrize('failure',['token','source_version','oversize','duplicate'])
def test_batch_checkpoint_late_failure_rolls_back_every_item(tmp_path,failure):
    core=Core(tmp_path/'data');path=tmp_path/'atomic.txt';path.write_text('Evidence. '*1200)
    asset=source(core,path);op=open_context(core,sources=[asset['id']])['operation_id']
    batch=core.query('read_material_batch',operation_id=op)
    assert len(batch['items'])>2
    core.query('checkpoint_context',operation_id=op,summary='Saved before this batch')
    entries=[{'step_key':x['step_key'],'delivery_token':x['delivery_token'],'result':{}} for x in batch['items']]
    if failure=='token':entries[-1]['delivery_token']='wrong-token'
    elif failure=='oversize':entries[-1]['result']={'notes':'大'*5000}
    elif failure=='duplicate':entries.append(entries[-1])
    else:
        with core.store.connect() as c:c.execute('UPDATE entities SET version=version+1 WHERE id=?',(asset['id'],))
    with pytest.raises(Exception):core.query('checkpoint_context_batch',operation_id=op,items=entries,summary='Must not be saved')
    with core.store.connect() as c:
        assert c.execute("SELECT count(*) FROM context_steps WHERE operation_id=? AND status='completed'",(op,)).fetchone()[0]==0
        assert contexts._operation(core,c,op)['checkpoint']['summary']=='Saved before this batch'


def test_batch_ack_retry_partial_subset_and_summary_preservation(tmp_path):
    core=Core(tmp_path/'data');path=tmp_path/'subset.txt';path.write_text('Stable evidence. '*1000)
    asset=source(core,path);op=open_context(core,sources=[asset['id']])['operation_id']
    first=core.query('read_material_batch',operation_id=op)
    batch_ack(core,op,first['items'][:2],summary='Keep this summary')
    resumed=core.query('read_material_batch',operation_id=op)
    assert not {x['step_key'] for x in first['items'][:2]} & {x['step_key'] for x in resumed['items']}
    receipt=batch_ack(core,op,first['items'])
    assert receipt['reused_count']==2
    assert core.query('operation_status',operation_id=op)['checkpoint']['summary']=='Keep this summary'


def test_batch_pdf_all_visuals_and_gap_survive_small_batches(tmp_path):
    from reportlab.pdfgen import canvas
    core=Core(tmp_path/'data');path=tmp_path/'visuals.pdf';pdf=canvas.Canvas(str(path))
    for page in range(9):
        pdf.drawString(40,780,f'PAGE {page+1} FINAL-PAGE' if page==8 else f'PAGE {page+1} synthetic evidence')
        pdf.rect(40,100,120,80);pdf.showPage()
    pdf.save();asset=source(core,path)
    gap=tmp_path/'unsupported.xyz';gap.write_bytes(b'unknown format');other=source(core,gap)
    op=open_context(core,sources=[asset['id'],other['id']])['operation_id'];units=[]
    for _ in range(40):
        batch=core.query('read_material_batch',operation_id=op,limit=7,max_images=2)
        assert sum(x['content']['kind']=='image' for x in batch['items'])<=2
        for item in batch['items']:
            if item['content']['kind']=='image':assert materials.image_bytes(core,op,item['content']['image_ref']).startswith(b'\x89PNG')
        units.extend(batch['items'])
        if batch['items']:batch_ack(core,op,batch['items'])
        if batch['all_steps_processed']:break
    else:pytest.fail('Batch reader did not finish extraction')
    kinds=[x['content']['kind'] for x in units]
    assert kinds.count('text')==9 and kinds.count('image')==9 and kinds.count('gap')==1
    assert len({x['step_key'] for x in units})==19
    assert any('FINAL-PAGE' in x['content'].get('text','') for x in units)
    merged=core.query('query_context',operation_id=op,collection='facts',view='facts')
    assert merged['complete'] and not merged['items']
    with core.store.connect() as c:
        candidate,_=contexts.validate(core,c,contexts._operation(core,c,op),{'actions':[],'unknowns':[]})
    assert candidate['material_coverage']['partial_sources']==1 and candidate['unknowns']


def test_batch_managed_image_budget_and_real_compaction(tmp_path):
    from PIL import Image
    core=Core(tmp_path/'data');path=tmp_path/'managed-batch.png';Image.new('RGB',(32,32),'red').save(path)
    asset=source(core,path);command(core,'settings',{'settings':{'ai':{'enabled':True}}})
    sent=command(core,'send_message',{'scope':{'kind':'general'},'text':'Read all images','source_ids':[asset['id']]})
    op=sent['job']['input']['context_operation_id']
    with core.store.connect() as c:c.execute('UPDATE context_operations SET delivered_bytes=49000 WHERE id=?',(op,))
    with pytest.raises(Exception) as error:core.query('read_material_batch',operation_id=op)
    assert error.value.code=='context_checkpoint_required'
    core.query('checkpoint_context_batch',operation_id=op,items=[],summary='Image still unread',**{'yield':True})
    with pytest.raises(Exception) as error:core.query('read_material_batch',operation_id=op)
    assert error.value.code=='context_checkpoint_required'
    contexts.compacted(core,op)
    batch=core.query('read_material_batch',operation_id=op)
    assert len(batch['items'])==1 and batch['items'][0]['content']['kind']=='image'
    with core.store.connect() as c:c.execute("UPDATE jobs SET status='cancelled' WHERE id=?",(sent['job']['id'],))
    with pytest.raises(Exception):batch_ack(core,op,batch['items'])
    with core.store.connect() as c:
        assert c.execute("SELECT count(*) FROM context_steps WHERE operation_id=? AND status='completed'",(op,)).fetchone()[0]==0


def test_batch_compact_facts_preserves_conflicts_notes_and_invalidates_on_correction(tmp_path):
    core=Core(tmp_path/'data');path=tmp_path/'facts.txt';path.write_text('Synthetic fact block. '*1600)
    asset=source(core,path);op=open_context(core,sources=[asset['id']])['operation_id'];all_items=[]
    while True:
        batch=core.query('read_material_batch',operation_id=op)
        if batch['all_steps_processed']:break
        entries=[]
        for item in batch['items']:
            index=len(all_items);all_items.append(item)
            result={'facts':[{'key':'date','value':str(index),'evidence':item['content']['locator']}]} if index<2 else {'notes':'Unresolved visual gap'} if index==2 else {}
            entries.append({'step_key':item['step_key'],'delivery_token':item['delivery_token'],'result':result})
        if entries:core.query('checkpoint_context_batch',operation_id=op,items=entries)
    compact=core.query('query_context',operation_id=op,collection='facts',view='facts')
    assert compact['complete'] and len(compact['items'])==3 and compact['total']==len(all_items)
    with core.store.connect() as c:
        candidate,_=contexts.validate(core,c,contexts._operation(core,c,op),{'actions':[],'unknowns':[]})
    assert candidate['material_coverage']['conflicts'][0]['variants']==2
    exact=core.query('read_material',operation_id=op,source_id=asset['id'],chunk=all_items[0]['chunk'])
    core.query('checkpoint_context_batch',operation_id=op,items=[{'step_key':exact['step_key'],'delivery_token':exact['delivery_token'],
        'expected_fingerprint':exact['checkpoint_fingerprint'],'result':{'facts':[{'key':'date','value':'corrected','evidence':exact['content']['locator']}]}}])
    with core.store.connect() as c:
        with pytest.raises(Exception) as error:contexts.validate(core,c,contexts._operation(core,c,op),{'actions':[]})
    assert error.value.code=='material_merge_required'
    reread=core.query('query_context',operation_id=op,collection='facts',view='facts')
    assert any(f['value']=='corrected' for x in reread['items'] for f in x['result'].get('facts',[]))


@pytest.mark.parametrize('params',[{'limit':0},{'limit':True},{'max_images':0},{'max_images':5},{'byte_limit':65537}])
def test_batch_invalid_limits_reject_without_acknowledging(tmp_path,params):
    core=Core(tmp_path/'data');op=open_context(core)['operation_id']
    with pytest.raises(Exception) as error:core.query('read_material_batch',operation_id=op,**params)
    assert error.value.code=='context_batch_parameters'
