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
EXE = ROOT / 'release' / os.environ.get('PM_PACKAGE_NAME','PersonalManagement-0.7') / 'PersonalManagementService.exe'
GUI = EXE.with_name('PersonalManagement.exe')

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
        owner=client.command('create',{'type':'course','title':'Synthetic packaged course'})['result']['entity']
        source_path=root/'synthetic-source.txt';source_path.write_text('Final exam 60 percent. Coursework 40 percent.',encoding='utf-8')
        source=client.command('add_source',{'owner_id':owner['id'],'kind':'file','path':str(source_path)})['result']['entity']
        source_path.unlink()
        assert 'Final exam' in client.query('source_content',id=source['id'])['text']
        assert client.query('open_resource',id=source['id'])['exists']
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
        extraction=timetabledoc['data']['extraction'];assert extraction['coverage']['visual_complete'] and len(extraction['images'])==1
        report['packaged_timetable']['pdf_layout_worker']=True

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
