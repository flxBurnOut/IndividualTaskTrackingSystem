import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import psutil
from management.client import Client
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / 'release' / os.environ.get('PM_PACKAGE_NAME','PersonalManagement-1.0.1') / 'PersonalManagementService.exe'
GUI = EXE.with_name('PersonalManagement.exe')


async def verify_frozen_mcp_plan_revision(client, params):
    """Exercise packaged MCP dispatch against this smoke run's isolated service.

    All five business writes cross the frozen stdio entry and generic command
    tool. The source Client only inspects the same synthetic service. No model
    request, Codex session or production data root is involved.
    """
    day, page_limit = '2038-05-01', 16 * 1024
    encoded_size = lambda value: len(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                               separators=(',', ':')).encode('utf-8'))
    raw = client.query('capabilities')
    raw_bytes = encoded_size(raw)
    assert raw_bytes > page_limit and 'revise_plan' in raw['commands'], 'Fixture must exercise the oversized registry'
    assert not client.query('settings')['settings']['ai']['enabled'], 'Smoke must not enable models'
    jobs_before = client.query('jobs')['total']
    plans_before = client.query('list', type='plan')['total']
    evidence = {'transport': 'frozen_stdio_mcp', 'date': day, 'raw_capabilities_bytes': raw_bytes}

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(tool, args, error_code=None):
                response = await session.call_tool(tool, args)
                error_text = '\n'.join(block.text for block in response.content if hasattr(block, 'text'))
                if error_code is not None:
                    assert response.is_error, f'{tool} unexpectedly succeeded; expected {error_code}'
                    # The stdio server adds a human-readable tool-error prefix.
                    # Inspect its structured JSON detail without assuming that
                    # the transport text starts at the opening JSON brace.
                    start = error_text.find('{')
                    assert start >= 0, error_text
                    detail, _ = json.JSONDecoder().raw_decode(error_text[start:])
                    assert detail['code'] == error_code, error_text
                    return None
                assert not response.is_error, f'{tool}: {error_text}'
                assert isinstance(response.structured_content, dict), f'{tool} returned no structured result'
                return response.structured_content

            async def arguments(name, payload):
                state = await call('query_business', {'name': 'state'})
                return {'name': name, 'payload': payload, 'request_id': str(uuid.uuid4()),
                        'epoch': state['epoch'], 'expected_revision': state['revision']}

            async def execute(name, payload):
                return await call('execute_command', await arguments(name, payload))

            names, cursor, version, page_bytes = [], 0, None, []
            for _ in range(100):
                query = {'offset': cursor, 'limit': 20}
                if version is not None:
                    query['version'] = version
                page = await call('query_business', {'name': 'capabilities', 'params': query})
                page_bytes.append(encoded_size(page))
                assert page_bytes[-1] <= page_limit and 'context' not in page
                names.extend(page['commands'])
                cursor, version = page['next_offset'], page['version']
                if cursor is None:
                    break
            else:
                raise AssertionError('Capability index did not finish within the smoke page bound')
            assert names == sorted(raw['commands']) and 'revise_plan' in names
            evidence['public_capabilities_max_bytes'] = max(page_bytes)
            evidence['public_capabilities_complete'] = True

            predecessor = (await execute('create', {'type': 'task', 'title': 'Synthetic frozen prerequisite'}))['result']['entity']
            dependent = (await execute('create', {'type': 'task', 'title': 'Synthetic frozen dependent'}))['result']['entity']
            await execute('link', {'source_id': dependent['id'], 'target_id': predecessor['id'], 'kind': 'depends_on'})
            old = (await execute('create_plan', {'date': day, 'mode': 'no_precise_time',
                'title': 'Synthetic frozen original plan', 'blocks': [
                    {'target_id': predecessor['id'], 'minutes': 20},
                    {'target_id': dependent['id'], 'minutes': 20}]}))['result']['entity']
            original_blocks = old['data']['blocks']
            payload = {'date': day, 'plan_id': old['id'], 'plan_version': old['version'],
                       'mode': 'no_precise_time', 'blocks': original_blocks}

            for invalid_blocks in (list(reversed(original_blocks)), original_blocks[1:]):
                before_state = client.state()
                before_review = client.query('daily_review', date=day)
                args = await arguments('revise_plan', {**payload, 'blocks': invalid_blocks})
                await call('execute_command', args, error_code='dependency')
                assert client.state() == before_state
                assert client.query('daily_review', date=day) == before_review
                assert client.query('get', id=old['id'])['entity'] == old
                assert not (await call('recover_receipt', {'request_id': args['request_id']}))['found']
            evidence.update(dependency_reorder_rejected=True, prerequisite_removal_rejected=True,
                            rejected_revision_keeps_original=True)

            revised_title = 'Synthetic frozen revised dependency plan'
            valid_args = await arguments('revise_plan', {**payload, 'title': revised_title,
                'blocks': [{**original_blocks[0], 'minutes': 30}, original_blocks[1]]})
            receipt = await call('execute_command', valid_args)
            revised = receipt['result']['entity']
            assert revised['id'] != old['id'] and revised['title'] == revised_title
            assert revised['data']['supersedes_id'] == old['id']
            assert [block['target_id'] for block in revised['data']['blocks']] == [predecessor['id'], dependent['id']]
            assert revised['data']['blocks'][0]['minutes'] == 30
            assert client.query('daily_tasks', date=day)['plan']['id'] == revised['id']
            assert client.query('get', id=old['id'])['entity'] == old
            state_after = client.state()
            replay = await call('execute_command', valid_args)
            assert replay['replayed'] and replay['result'] == receipt['result']
            assert client.state() == state_after
            stale_args = await arguments('revise_plan', payload)
            await call('execute_command', stale_args, error_code='plan_conflict')
            assert client.state() == state_after
            assert not (await call('recover_receipt', {'request_id': stale_args['request_id']}))['found']
            assert client.query('list', type='plan')['total'] == plans_before + 2
            evidence.update(valid_revision_supersedes_original=True, old_plan_version_rejected=True,
                            identical_request_replayed_without_duplicate=True)

            context = await call('prepare_context', {'goal': 'Read synthetic frozen dependency direction',
                'scope': {'kind': 'daily_plan', 'date': day}})
            operation_id = context['operation_id']

            async def details(identifier):
                offset, item_version, fragments, reader = 0, None, [], None
                for _ in range(20):
                    args = {'operation_id': operation_id, 'id': identifier, 'offset': offset}
                    if item_version is not None:
                        args['version'] = item_version
                    page = await call('read_context_item', args)
                    assert encoded_size(page) <= page_limit
                    reader = page.get('dependencies', reader)
                    if 'item' in page:
                        assert page['complete'] and page['next_offset'] is None
                        return page['item'], reader
                    fragments.append(page['json_fragment'])
                    offset, item_version = page['next_offset'], page['version']
                    if offset is None:
                        assert page['complete']
                        return json.loads(''.join(fragments)), reader
                raise AssertionError('Dependency reader did not finish within the smoke page bound')

            body, reader = await details(dependent['id'])
            assert body['id'] == dependent['id'] and reader['reader'] == 'read_context_item'
            graph, _ = await details(reader['id'])
            assert graph['entity_id'] == dependent['id'] and graph['as_of_date'] == day
            assert [item['id'] for item in graph['predecessors']] == [predecessor['id']]
            assert not graph['predecessors'][0]['completed'] and not graph['dependents']
            _, reverse_reader = await details(predecessor['id'])
            reverse, _ = await details(reverse_reader['id'])
            assert not reverse['predecessors']
            assert [item['id'] for item in reverse['dependents']] == [dependent['id']]
            assert client.state() == state_after
            assert client.query('jobs')['total'] == jobs_before
            evidence.update(dependency_reader_both_directions=True, planning_is_not_completion=True,
                            business_revision_unchanged_by_reads=True, model_jobs_added=0, passed=True)
    return evidence


async def main():
    root = ROOT / '.test-output' / ('packaged-' + uuid.uuid4().hex[:10])
    root.mkdir(parents=True)
    data = root / 'data'
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.pop('VIRTUAL_ENV', None)
    report = {'synthetic_only': True, 'package': str(EXE.parent), 'data_dir': str(data)}
    try:
        result = subprocess.run([str(EXE), '--diagnose', '--data-dir', str(data)], capture_output=True, encoding='utf-8', errors='replace', env=environment, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        client = Client(data, autostart=False)
        shim = EXE.with_name('PersonalManagementCodex.exe')
        assert shim.is_file(), 'Missing desktop connection adapter'
        connection = client.query('codex_connection')
        assert connection['ready'] is False and 'token' not in connection
        report['desktop_connection_adapter'] = {'legacy_adapter_packaged': True, 'disabled_until_enabled': True, 'no_secrets_in_status': True, 'default_transport': 'native_ipc_v1'}
        report['fresh_empty'] = client.state()['counts'] == {}
        report['diagnostic'] = json.loads(result.stdout)
        runtime = json.loads((data/'runtime.json').read_text('utf-8'))
        service = psutil.Process(runtime['pid'])
        report['frozen_service'] = Path(service.exe()).name == EXE.name
        verify_report = root / 'frozen-gui.json'
        gui = subprocess.Popen([str(GUI), '--data-dir', str(data), '--verify-ui', str(verify_report)], env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(1)
        gui_process = psutil.Process(gui.pid)
        report['service_private_bytes_snapshot'] = getattr(service.memory_info(),'private',service.memory_info().rss)
        report['gui_private_bytes_snapshot'] = getattr(gui_process.memory_info(),'private',gui_process.memory_info().rss)
        gui.wait(timeout=30)
        report['gui'] = json.loads(verify_report.read_text('utf-8'))
        assert report['gui']['loaded'] and report['gui']['visible']
        report['fresh_empty_after_gui'] = client.state()['counts'] == {}
        skills=client.query('skills')['items'];assert any(x['id']=='course-notes' and x['instructions'] for x in skills)
        report['packaged_notes_skill']=True
        params = StdioServerParameters(command=str(EXE), args=['--mcp','--data-dir',str(data)], env=environment)
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                tools = await session.list_tools()
                report['mcp_tools'] = [t.name for t in tools.tools]
                response = await session.call_tool('query_business', {'name':'state','params':{}})
                assert not response.is_error, response
        assert client.state()['counts'] == {}
        habit_state=client.state()
        habits=client.query('habits_overview')
        assert habits['summary']['preparation_total']==0 and not habits['reminders']['daily']['enabled']
        assert habits['rules']['total']==0 and client.state()==habit_state
        report['habits_readonly_empty']=True
        home=client.query('dashboard',date='2030-01-01')
        assert home['weekday']=='星期二' and len(home['days'])==7 and home['tasks']['total']==0
        assert client.state()==habit_state
        report['dashboard_readonly']=True
        state = client.state()
        assert client.query('daily_review',date='2030-01-01')['needs_codex']
        target=client.command('create',{'type':'task','title':'Synthetic packaged task'})['result']['entity']
        plan=client.command('create_plan',{'date':'2030-01-01','mode':'no_precise_time','blocks':[{'target_id':target['id'],'minutes':20}]})['result']['entity']
        payload={'date':'2030-01-01','plan_id':plan['id'],'plan_version':plan['version'],'answers':[{'target_id':target['id'],'result':'incomplete'}]}
        client.command('submit_daily_review',payload)
        first=client.query('daily_review',date='2030-01-01')
        client.command('submit_daily_review',payload)
        again=client.query('daily_review',date='2030-01-01')
        assert first['review_id']==again['review_id'] and again['summary']['incomplete']==1
        assert client.query('list',type='feedback')['total']==1
        assert client.query('list',type='checkin')['total']==0
        prefs={'daily':{'enabled':False,'time':'22:15'},'weekly':{'enabled':False,'weekday':5,'time':'19:45'},'timezone':'Asia/Shanghai'}
        client.command('set_review_preferences',prefs)
        client.command('set_review_preferences',prefs)
        assert client.query('list',type='schedule')['total']==2
        report['structured_review_and_settings']={'explicit_incomplete':True,'duplicate_feedback':False,'duplicate_reminders':False,'no_generated_checkins':True}
        quick=client.command('set_task_completion',{'target_id':target['id'],'target_version':target['version'],'business_date':'2030-01-01','result':'done'})
        assert client.query('daily_review',date='2030-01-01')['summary']['done']==1
        current=client.query('get',id=target['id'])['entity']
        client.command('set_task_completion',{'target_id':target['id'],'target_version':current['version'],'business_date':'2030-01-01','result':'incomplete'})
        assert client.query('daily_review',date='2030-01-01')['summary']['incomplete']==1
        report['quick_completion_review_link']=True
        owner=client.command('create',{'type':'course','title':'Synthetic packaged course'})['result']['entity']
        source_path=root/'synthetic-source.txt';source_path.write_text('Final exam 60 percent. Coursework 40 percent.',encoding='utf-8')
        source=client.command('add_source',{'owner_id':owner['id'],'kind':'file','path':str(source_path)})['result']['entity']
        source_path.unlink()
        assert 'Final exam' in client.query('source_content',id=source['id'])['text']
        readable=client.query('open_resource',id=source['id'])
        assert readable['exists'] and '原文件' in Path(readable['path']).parts
        folder=client.query('library_folder',owner_id=owner['id'])
        assert not folder['issues'] and Path(readable['path']).parent==Path(folder['path'])
        report['readable_originals']={'managed_original_exists':True,'owner_folder_exists':Path(folder['path']).is_dir(),'external_source_required':False}
        from PIL import Image
        scan_path=root/'synthetic-scan.pdf';Image.new('RGB',(200,280),'white').save(scan_path,'PDF')
        scan=client.command('add_source',{'owner_id':owner['id'],'kind':'file','path':str(scan_path)})['result']['entity']
        assert scan['data']['extraction']['images'],scan['data']['extraction']
        assert client.query('conversation',scope={'kind':'course','entity_id':owner['id']})['conversation'] is None
        client.command('settings',{'settings':{'charts':{'weekly_style':'rows'}}})
        assert client.query('settings')['settings']['charts']['weekly_style']=='rows'
        report['managed_sources_and_conversations']={'deleted_original_still_readable':True,'frozen_scan_pdf_renderer':True,'empty_conversation_read_only':True,'chart_preferences_persisted':True}
        anchor=client.command('create',{'type':'event','title':'Synthetic repeating lab','data':{'owner_id':owner['id'],'date':'2030-01-09','recurrence':'weekly','time_kind':'date_only'}})['result']['entity']
        rule=client.command('set_recurring_rule',{'anchor_id':anchor['id'],'title':'Prepare before lab','content':'Complete synthetic exercises','completion_gate':'Check all three exercises','days_before':2,'estimated_minutes':37,'effective_from':'2030-01-01','effective_until':'2030-02-01'})['result']['entity']
        for _ in range(2):client.command('materialize_recurring',{'rule_id':rule['id'],'start':'2030-01-07','end':'2030-01-07'})
        tasks=client.query('daily_tasks',date='2030-01-07');assert tasks['total']==1 and tasks['plan'] is None
        target=tasks['items'][0]
        plan=client.command('add_to_plan',{'date':'2030-01-07','target_id':target['id'],'plan_id':None,'plan_version':None})['result']['entity']
        assert client.query('daily_tasks',date='2030-01-07')['total']==0
        assert client.query('daily_review',date='2030-01-07')['summary']['unreported']==1
        assert client.query('recurring_rules',anchor_id=anchor['id'])['total']==1
        report['recurring_daily_flow']={'one_task_per_occurrence':True,'no_implicit_plan':True,'explicit_plan_then_review':True}
        habit_state=client.state();habits=client.query('habits_overview')
        assert habits['summary']['preparation_total']==1 and habits['preparations']['items'][0]['id']==rule['id']
        assert not habits['reminders']['daily']['enabled'] and client.state()==habit_state
        report['habits_readonly_configured']=True
        table=client.command('apply_timetable',{'title':'Synthetic packaged timetable','semester_start':'2030-01-07','semester_end':'2030-02-10','timezone':'Asia/Shanghai','week_numbering':'teaching','recess_weeks':['2030-01-21'],'source_text':'Synthetic user-confirmed course schedule','rows':[{'key':'lab','title':'Synthetic even-week lab','weekday':2,'start':'14:00','end':'16:00','teaching_weeks':[2,4],'owner_id':owner['id']}]})['result']
        event_id=table['rows'][0]['id']
        assert not any(e['id']==event_id for e in client.query('plan_context',date='2030-01-09')['hard_events'])
        assert any(e['id']==event_id for e in client.query('plan_context',date='2030-01-16')['hard_events'])
        assert len(client.query('timetable_week',week_start='2030-01-14',timetable_id=table['entity']['id'])['events'])==1
        assert client.query('timetables',id=table['entity']['id'])['rows'][0]['id']==event_id
        report['packaged_timetable']={'same_fixed_constraints':True,'even_weeks_only':True,'readable_week_view':True}
        assert not any(e['id']==event_id for e in client.query('plan_context',date='2030-01-23')['hard_events'])
        assert not any(e['id']==event_id for e in client.query('plan_context',date='2030-01-30')['hard_events'])
        assert any(e['id']==event_id for e in client.query('plan_context',date='2030-02-06')['hard_events'])
        assert client.query('timetable_week',week_start='2030-01-28',timetable_id=table['entity']['id'])['week_number']==3
        report['packaged_timetable']['recess_excludes_teaching_week']=True
        duplicate=client.command('create',{'type':'timetable','title':table['entity']['title'],'data':{'semester_start':'2030-01-07','semester_end':'2030-02-10','timezone':'UTC'}})['result']
        assert duplicate['reused'] and duplicate['entity']['id']==table['entity']['id'] and duplicate['entity']['data']['timezone']=='Asia/Shanghai'
        report['packaged_timetable']['same_name_range_reuses_snapshot']=True
        trash=client.command('create',{'type':'task','title':'Synthetic disposable task'})['result']['entity']
        removed=client.command('delete_task',{'id':trash['id'],'version':trash['version']})['result']['entity'];assert removed['archived']
        restored=client.command('restore_task',{'id':removed['id'],'version':removed['version']})['result']['entity'];assert not restored['archived']
        report['task_delete_restore']=True

        from reportlab.pdfgen import canvas
        timetable_pdf=root/'synthetic-timetable.pdf';document=canvas.Canvas(str(timetable_pdf),pagesize=(600,400))
        document.drawString(40,350,'Synthetic timetable Monday Wednesday')
        document.drawString(40,300,'09:00 - 10:00 Math Physics');document.rect(35,280,450,60);document.showPage();document.save()
        timetabledoc=client.command('add_source',{'owner_id':table['entity']['id'],'kind':'file','path':str(timetable_pdf)})['result']['entity']
        extraction=timetabledoc['data']['extraction'];assert extraction['coverage']['extraction_status']=='complete' and not extraction['coverage']['analysis_complete'] and len(extraction['images'])==1
        report['packaged_timetable']['pdf_layout_worker']=True
        from openpyxl import Workbook
        book=Workbook();sheet=book.active;sheet.append(['Week','Class']);sheet.append([1,'Synthetic tutorial'])
        xlsx=root/'synthetic-schedule.xlsx';book.save(xlsx);book.close()
        spreadsheet=client.command('add_source',{'owner_id':table['entity']['id'],'kind':'file','path':str(xlsx)})['result']['entity']
        assert spreadsheet['data']['extraction']['coverage']['extraction_status']=='complete'
        content=client.query('source_content',id=spreadsheet['id'])
        assert 'Synthetic tutorial' in content['text']
        report['packaged_xlsx_reader']=True

        recovery=client.command('set_recovery_task',{'course_id':owner['id'],'title':'Synthetic missed lessons','completion_gate':'Complete lessons and exercises','unit':'lessons','total_quantity':8,'completed_quantity':3,'source_text':'Explicit synthetic 3 of 8','reason':'self_reported'})['result']['entity']
        progress=client.query('recovery_summary',course_id=owner['id'])['items'][0]['progress']
        assert progress['ratio']==3/8 and not progress['completion_confirmed']
        client.command('settings',{'settings':{'appearance':{'theme':'dark','font_size':18}}})
        settings=client.query('settings')['settings'];assert settings['appearance']['theme']=='dark' and settings['appearance']['font_size']==18
        dark_report=root/'frozen-dark-gui.json'
        dark_gui=subprocess.Popen([str(GUI),'--data-dir',str(data),'--verify-ui',str(dark_report)],env=environment,creationflags=subprocess.CREATE_NO_WINDOW)
        dark_gui.wait(timeout=30);assert dark_gui.returncode==0
        report['dark_gui']=json.loads(dark_report.read_text('utf-8'));assert report['dark_gui']['loaded'] and report['dark_gui']['visible'] and report['dark_gui']['appearance']['theme']=='dark' and report['dark_gui']['font_pixel_size']==18
        report['recovery_and_appearance']={'three_of_eight':True,'not_auto_completed':True,'dark_large_font_saved':True}
        receipt = client.command('create_artifact_job', {'kind':'docx','relative_path':'synthetic.docx','title':'合成文档','content':'仅用于发布包测试。'})
        job_id = receipt['result']['job']['id']
        deadline = time.monotonic()+25
        while time.monotonic()<deadline:
            job = client.query('job',id=job_id)['job']
            if job['status'] in ('completed','failed'):
                break
            time.sleep(.1)
        assert job['status']=='completed', job
        report['frozen_document_worker'] = job['result']['metadata']['validation']
        report['mcp_generic_plan_revision'] = await verify_frozen_mcp_plan_revision(client, params)
        report['service_survives_both_entrances'] = service.is_running()
        report['passed'] = True
    except Exception as error:
        report['passed'] = False
        report['error'] = repr(error)
        raise
    finally:
        report_path = ROOT / '.build' / 'packaged-smoke.json'
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        if (data/'runtime.json').exists():
            runtime=json.loads((data/'runtime.json').read_text('utf-8'))
            try:
                p=psutil.Process(runtime['pid'])
                if '--service' in p.cmdline() and str(data) in p.cmdline():
                    p.terminate()
                    p.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
        print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':
    asyncio.run(main())
