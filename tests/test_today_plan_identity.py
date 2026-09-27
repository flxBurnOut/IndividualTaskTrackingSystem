"""The current plan is an explicit identity, independent of history ordering."""
import uuid

from management.core import Core


DAY = '2038-05-01'


def command(core, name, payload):
    state = core.query('state')
    return core.command(name, payload, request_id=str(uuid.uuid4()),
                        epoch=state['epoch'], expected_revision=state['revision'])['result']


def test_today_exposes_null_identity_when_no_current_plan(tmp_path):
    core = Core(tmp_path)
    result = core.query('today', date=DAY)
    assert result['current_plan_id'] is None
    assert result['current_plan_version'] is None
    assert result['plans'] == []


def test_restored_old_plan_does_not_replace_explicit_current_identity(tmp_path):
    core = Core(tmp_path)
    task = command(core, 'create', {'type':'task','title':'Synthetic task'})['entity']
    old = command(core, 'create_plan', {'date':DAY,'mode':'no_precise_time',
                 'blocks':[{'target_id':task['id']}]})['entity']
    latest = command(core, 'revise_plan', {'date':DAY,'mode':'no_precise_time',
                    'plan_id':old['id'],'plan_version':old['version'],
                    'blocks':[{'target_id':task['id']}],'title':'Revised plan'})['entity']
    archived = command(core, 'archive', {'id':old['id'],'version':old['version'],'archived':True})['entity']
    command(core, 'archive', {'id':old['id'],'version':archived['version'],'archived':False})
    result = core.query('today', date=DAY)
    assert result['current_plan_id'] == latest['id']
    assert result['current_plan_version'] == latest['version']
    assert {p['id'] for p in result['plans']} == {old['id'],latest['id']}
    assert core.query('daily_review',date=DAY)['plan']['id'] == result['current_plan_id']
    checkin = command(core,'create_checkin',{'date':DAY})['entity']
    assert checkin['data']['plan_id'] == result['current_plan_id']


def test_archiving_current_updates_identity_and_all_archived_means_none(tmp_path):
    core = Core(tmp_path)
    first = command(core,'create_plan',{'date':DAY,'mode':'rest','blocks':[]})['entity']
    second = command(core,'revise_plan',{'date':DAY,'mode':'rest','blocks':[],
                     'plan_id':first['id'],'plan_version':first['version']})['entity']
    command(core,'archive',{'id':second['id'],'version':second['version'],'archived':True})
    assert core.query('today',date=DAY)['current_plan_id'] == first['id']
    command(core,'archive',{'id':first['id'],'version':first['version'],'archived':True})
    result=core.query('today',date=DAY)
    assert result['current_plan_id'] is None and result['current_plan_version'] is None


def test_current_identity_remains_available_outside_recent_history_window(tmp_path):
    core=Core(tmp_path)
    history=[]
    for index in range(21):
        history.append(command(core,'create_plan',{'date':DAY,'mode':'rest','blocks':[],
                                                   'title':f'Plan {index}'})['entity'])
    latest=history[-1]
    # Public archive/restore commands can bring old versions to the front of
    # the 20-item history window without making them the current plan.
    for old in history[:-1]:
        archived=command(core,'archive',{'id':old['id'],'version':old['version'],'archived':True})['entity']
        command(core,'archive',{'id':old['id'],'version':archived['version'],'archived':False})
    result=core.query('today',date=DAY)
    assert len(result['plans'])==20
    assert latest['id'] not in {p['id'] for p in result['plans']}
    assert result['current_plan_id']==latest['id']
    assert result['current_plan_version']==latest['version']
