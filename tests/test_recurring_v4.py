"""Synthetic end-to-end recurring tasks; no formal data or network access."""
import datetime as dt
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import uuid
import pytest
from management.core import Core
from management.recurring import tick
from management.scheduler import Background
from management.schemas import BusinessError


@pytest.fixture
def core(tmp_path):
    return Core(tmp_path / 'space')


def command(core, name, payload, *, request_id=None, state=None):
    state = state or core.query('state')
    return core.command(name, payload, request_id=request_id or str(uuid.uuid4()), epoch=state['epoch'], expected_revision=state['revision'])


def create(core, kind, title='Synthetic', parent=None, **data):
    return command(core, 'create', {'type': kind, 'title': title, 'parent_id': parent, 'data': data})['result']['entity']


def edit(core, entity, **data):
    return command(core, 'update', {'id': entity['id'], 'version': entity['version'], 'patch': {'data': data}})['result']['entity']


def rule(core, anchor, **kwargs):
    p = {'anchor_id': anchor['id'], 'title': 'Prepare this occurrence', 'content': 'Read the stated material and do two explicit exercises.', 'completion_gate': 'Two exercises checked and one question noted.', 'days_before': 1, 'estimated_minutes': None, 'enabled': True, 'materialize_date': '2001-01-01'}
    p.update(kwargs)
    return command(core, 'set_recurring_rule', p)['result']


def materialize(core, rule, start, end=None):
    return command(core, 'materialize_recurring', {'rule_id': rule['id'], 'start': start, 'end': end or start})['result']


def event(core, **kwargs):
    d = {'date': '2030-01-02', 'recurrence': 'weekly', 'time_kind': 'date_only'}
    d.update(kwargs)
    return create(core, 'event', **d)


def codes(result):
    return {i['code'] for i in result['issues']}


def test_weekly_instances_independent_and_never_become_plan_or_feedback(core):
    course = create(core, 'course')
    anchor = event(core, owner_id=course['id'])
    r = rule(core, anchor)['entity']
    made = materialize(core, r, '2030-01-01', '2030-01-15')['created']
    assert [t['data']['scheduled_date'] for t in made] == ['2030-01-01', '2030-01-08', '2030-01-15']
    assert len({t['id'] for t in made}) == 3
    assert all(t['parent_id'] == course['id'] and t['status'] == 'pending' for t in made)
    assert all(t['data']['estimated_minutes'] is None for t in made)
    assert all(t['data']['completion_gate'].startswith('Two exercises') for t in made)
    assert core.query('list', type='plan')['total'] == 0
    assert core.query('list', type='feedback')['total'] == 0
    command(core, 'record_feedback', {'target_id': made[0]['id'], 'business_date': '2030-01-01', 'dimensions': {'completion': 'done'}, 'source_text': 'Synthetic actual completion only this occurrence'})
    again = materialize(core, r, '2030-01-01', '2030-01-15')
    assert not again['created'] and len(again['existing']) == 3
    assert again['existing'][0]['protection_checked'] is False  # unchanged snapshots do not scan all plans
    assert core.query('get', id=made[1]['id'])['entity']['status'] == 'pending'


def test_initial_set_materializes_requested_day_only(core):
    result = rule(core, event(core), materialize_date='2030-01-01')
    assert result['materialization']['created_count'] == 1
    assert result['materialization']['coverage']['start'] == result['materialization']['coverage']['end'] == '2030-01-01'


def test_initial_set_defaults_to_business_today(core):
    today = dt.datetime.now(dt.timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date()
    result = rule(core, event(core, date=(today + dt.timedelta(days=1)).isoformat()), materialize_date=None)
    assert result['materialization']['created_count'] == 1
    assert result['materialization']['created'][0]['data']['scheduled_date'] == today.isoformat()


def test_monthly_skips_nonexistent_day_and_crosses_year_without_clipping(core):
    r = rule(core, event(core, date='2029-12-31', recurrence='monthly'), days_before=2)['entity']
    assert not materialize(core, r, '2030-02-01', '2030-02-28')['created']
    march = materialize(core, r, '2030-03-01', '2030-03-31')['created']
    assert [t['data']['scheduled_date'] for t in march] == ['2030-03-29']
    january = materialize(core, r, '2030-01-01', '2030-01-31')['created']
    assert [t['data']['anchor_occurrence'] for t in january] == ['2030-01-31']


def test_leap_year_monthly_and_offset_cross_month(core):
    r = rule(core, event(core, date='2032-01-29', recurrence='monthly'), days_before=30)['entity']
    made = materialize(core, r, '2032-01-30')['created']
    assert len(made) == 1 and made[0]['data']['due_date'] == '2032-02-29'


@pytest.mark.parametrize('kind', ['task', 'milestone', 'assessment'])
def test_single_deadline_anchor_and_parent_scope(core, kind):
    course = create(core, 'course')
    anchor = create(core, kind, parent=course['id'], due_date='2030-02-01')
    r = rule(core, anchor, days_before=3)['entity']
    made = materialize(core, r, '2030-01-29')['created']
    assert len(made) == 1 and made[0]['data']['anchor_occurrence'] == 'deadline'
    assert made[0]['parent_id'] == course['id']
    assert not materialize(core, r, '2030-02-28')['created']


def test_unknown_anchor_date_is_visible_and_never_guessed(core):
    r = rule(core, create(core, 'task'))['entity']
    result = materialize(core, r, '2030-01-01')
    assert not result['created'] and 'anchor_date_unknown' in codes(result)
    assert 'anchor_date_unknown' in {i['code'] for i in core.query('recurring_rules')['items'][0]['issues']}


def test_exact_event_timezone_sets_business_day_before_offset(core):
    anchor = event(core, date='2030-01-01', start='18:00', end='19:00', timezone='America/Los_Angeles', time_kind='exact')
    r = rule(core, anchor)['entity']
    made = materialize(core, r, '2030-01-01')['created']
    assert made[0]['data']['anchor_occurrence'] == '2030-01-01'
    assert made[0]['data']['due_date'] == '2030-01-02'
    assert made[0]['data']['scheduled_date'] == '2030-01-01'


def test_date_only_does_not_invent_midnight_timezone_conversion(core):
    anchor = event(core, date='2030-01-02', timezone='America/Los_Angeles', time_kind='date_only')
    r = rule(core, anchor)['entity']
    made = materialize(core, r, '2030-01-01')['created']
    assert len(made) == 1 and made[0]['data']['due_date'] == '2030-01-02'


@pytest.mark.parametrize('day,clock', [('2030-03-10','02:30'), ('2030-11-03','01:30')])
def test_dst_nonexistent_or_ambiguous_time_not_guessed(core, day, clock):
    r = rule(core, event(core, date=day, start=clock, end='04:00', timezone='America/New_York', time_kind='exact', recurrence='none'), days_before=0)['entity']
    result = materialize(core, r, day)
    assert not result['created'] and 'recurring_time_unknown' in codes(result)


def test_preview_is_readonly_and_does_not_materialize(core):
    r = rule(core, event(core))['entity']
    before = core.query('state')
    result = core.query('preview_recurring', rule_id=r['id'], start='2030-01-01', end='2030-01-08')
    assert len(result['candidates']) == 2
    assert core.query('state') == before
    assert core.query('list', type='task')['total'] == 0


def test_effective_dates_apply_to_preparation_day_not_event_day(core):
    r = rule(core, event(core), effective_from='2030-01-08', effective_until='2030-01-08')['entity']
    made = materialize(core, r, '2030-01-01', '2030-01-15')['created']
    assert len(made) == 1 and made[0]['data']['scheduled_date'] == '2030-01-08'


def test_cancel_exception_before_and_after_generation_preserves_task(core):
    anchor = event(core, exceptions={'2030-01-02': {'cancelled': True}})
    r = rule(core, anchor)['entity']
    result = materialize(core, r, '2030-01-01', '2030-01-08')
    assert len(result['created']) == 1 and result['created'][0]['data']['scheduled_date'] == '2030-01-08'
    task = result['created'][0]
    edit(core, anchor, exceptions={'2030-01-02': {'cancelled': True}, '2030-01-09': {'cancelled': True}})
    result = materialize(core, r, '2030-01-08')
    assert not result['created'] and 'generated_task_needs_review' in codes(result)
    assert core.query('get', id=task['id'])['entity'] == task


def test_until_shortening_marks_original_task_for_review(core):
    anchor = event(core)
    r = rule(core, anchor)['entity']
    task = materialize(core, r, '2030-01-08')['created'][0]
    edit(core, anchor, until='2030-01-02')
    result = materialize(core, r, '2030-01-08')
    assert 'generated_task_needs_review' in codes(result)
    assert core.query('get', id=task['id'])['entity'] == task


def test_series_shift_requires_explicit_rebind_and_preserves_planned_task(core):
    anchor = event(core)
    r = rule(core, anchor)['entity']
    task = materialize(core, r, '2030-01-01')['created'][0]
    command(core, 'create_plan', {'date':'2030-01-01', 'mode':'no_precise_time', 'blocks':[{'target_id': task['id'], 'target_version': task['version'], 'minutes':30}]})
    edit(core, anchor, date='2030-01-03')
    result = materialize(core, r, '2030-01-01', '2030-01-02')
    assert not result['created'] and 'anchor_changed' in codes(result)
    assert 'has_plan' in result['existing'][0]['protected_reasons']
    with pytest.raises(BusinessError, match='关联日程') as error:
        command(core, 'set_recurring_rule', {'id':r['id'],'version':r['version'],'materialize_date':'2030-01-02'})
    assert error.value.code == 'recurring_anchor_changed'
    result = command(core, 'set_recurring_rule', {'id':r['id'],'version':r['version'],'acknowledge_anchor_change':True,'materialize_date':'2030-01-02'})['result']
    assert result['materialization']['created_count'] == 1
    assert core.query('get', id=task['id'])['entity'] == task


def test_deadline_date_change_reuses_ledger_key_without_overwriting(core):
    anchor = create(core,'task',due_date='2030-01-02')
    r = rule(core,anchor)['entity']
    task = materialize(core,r,'2030-01-01')['created'][0]
    edit(core,anchor,due_date='2030-02-02')
    result = materialize(core,r,'2030-02-01')
    assert not result['created'] and 'generated_task_needs_review' in codes(result)
    assert result['issues'][0]['current_business_date'] == '2030-02-01'
    assert core.query('get',id=task['id'])['entity'] == task


def test_rule_edit_or_disable_keeps_execution_and_old_content(core):
    anchor = event(core)
    r = rule(core,anchor)['entity']
    task = materialize(core,r,'2030-01-01')['created'][0]
    command(core, 'record_feedback', {'target_id':task['id'],'business_date':'2030-01-01','dimensions':{'completion':'partial'},'source_text':'Half explicitly done'})
    changed = command(core,'set_recurring_rule', {'id':r['id'],'version':r['version'],'content':'A new preparation method','enabled':False,'materialize_date':'2030-01-01'})['result']
    assert changed['materialization']['created_count'] == 0
    assert 'generated_task_needs_review' in codes(changed['materialization'])
    assert 'has_feedback' in changed['materialization']['existing'][0]['protected_reasons']
    assert core.query('get',id=task['id'])['entity'] == task
    assert not materialize(core,changed['entity'],'2030-01-08')['created']


def test_disabling_after_series_change_is_allowed_but_not_implicit_ack(core):
    anchor = event(core)
    r = rule(core,anchor,materialize_date='2030-01-01')['entity']
    edit(core,anchor,date='2030-01-03')
    disabled = command(core,'set_recurring_rule', {'id':r['id'],'version':r['version'],'enabled':False,'materialize_date':'2030-01-02'})['result']['entity']
    with pytest.raises(BusinessError) as error:
        command(core,'set_recurring_rule', {'id':disabled['id'],'version':disabled['version'],'enabled':True,'materialize_date':'2030-01-02'})
    assert error.value.code == 'recurring_anchor_changed'


def test_explicit_past_generation_stays_pending_not_invented_complete(core):
    anchor = event(core,date='2000-01-02',recurrence='none')
    r = rule(core,anchor)['entity']
    task = materialize(core,r,'2000-01-01')['created'][0]
    assert task['status'] == 'pending' and task['data']['late_generation'] is True
    assert core.query('list',type='feedback')['total'] == 0


def test_phase_and_nested_task_anchor_inherits_project_without_becoming_child_task(core):
    project = create(core,'project')
    phase = create(core,'phase',parent=project['id'])
    anchor = create(core,'task',parent=phase['id'],due_date='2030-01-02')
    r = rule(core,anchor)['entity']
    task = materialize(core,r,'2030-01-01')['created'][0]
    assert r['parent_id'] == task['parent_id'] == project['id']


def test_query_rules_pagination_scope_and_anchor(core):
    project = create(core,'project')
    anchor = create(core,'task',parent=project['id'],due_date='2030-01-02')
    for i in range(3):
        rule(core,anchor,title=f'Preparation {i}')
    page = core.query('recurring_rules',scope_id=project['id'],limit=2)
    assert page['total'] == 3 and page['next_offset'] == 2
    tail = core.query('recurring_rules',anchor_id=anchor['id'],limit=2,offset=2)
    assert len(tail['items']) == 1 and tail['next_offset'] is None


def test_background_uses_business_day_and_idle_tick_does_not_write_revision(core):
    rule(core,event(core))
    bg = Background(core,threading.Event())
    before = core.query('state')['revision']
    # UTC Dec 31 16:30 is Shanghai Jan 1: generate Jan 1 preparation.
    instant = dt.datetime(2029,12,31,16,30,tzinfo=dt.timezone.utc)
    bg.tick(instant)
    assert core.query('state')['revision'] == before + 1
    assert core.query('list',type='task')['items'][0]['data']['scheduled_date'] == '2030-01-01'
    bg.tick(instant)
    assert core.query('state')['revision'] == before + 1


def test_cross_client_revision_conflict_and_exact_retry_after_restart(core):
    anchor = event(core)
    r = rule(core,anchor)['entity']
    state = core.query('state')
    payload = {'rule_id':r['id'],'start':'2030-01-01'}
    rid = str(uuid.uuid4())
    first = command(core,'materialize_recurring',payload,state=state,request_id=rid)
    second = Core(core.store.root)
    retry = command(second,'materialize_recurring',payload,state=state,request_id=rid)
    assert retry['result'] == first['result'] and retry['revision'] == first['revision']
    assert retry['replayed'] is True
    with pytest.raises(BusinessError) as error:
        command(second,'materialize_recurring',payload,state=state)
    assert error.value.code == 'revision_conflict'
    assert not materialize(second,r,'2030-01-01')['created']
    assert second.query('list',type='task')['total'] == 1


def test_two_background_instances_serialize_and_deduplicate(core):
    rule(core,event(core))
    second = Core(core.store.root)
    instant = dt.datetime(2030,1,1,12,tzinfo=dt.timezone.utc)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda c: tick(c,instant), [core,second]))
    assert sum(r['created_count'] for r in results) == 1
    assert core.query('list',type='task')['total'] == 1


def test_unknown_dates_create_single_durable_notice_not_each_tick(core):
    rule(core,create(core,'task'))
    instant = dt.datetime(2030,1,1,12,tzinfo=dt.timezone.utc)
    first = tick(core,instant)
    before = core.query('state')['revision']
    second = tick(core,instant)
    assert first['notification_count'] == 1 and second['notification_count'] == 0
    assert core.query('state')['revision'] == before


def test_bounds_fail_atomically_and_do_not_enumerate_old_history(core):
    anchor = event(core,date='1900-01-01',recurrence='daily')
    r = rule(core,anchor,days_before=0)['entity']
    before = core.query('state')['revision']
    with pytest.raises(BusinessError) as error:
        materialize(core,r,'2030-01-01','2030-02-01')
    assert error.value.code == 'recurring_window'
    assert core.query('state')['revision'] == before
    # A century-old series can directly preview just one current occurrence.
    result = core.query('preview_recurring',rule_id=r['id'],start='2030-01-01')
    assert len(result['candidates']) == 1
    assert core.query('list',type='task')['total'] == 1  # only explicit 2001 initial day


@pytest.mark.parametrize('patch', [{'days_before':True},{'days_before':367},{'estimated_minutes':-1},{'completion_gate':' '},{'effective_from':'2030-02-01','effective_until':'2030-01-01'}])
def test_rule_validation_rolls_back_all_changes(core,patch):
    anchor=event(core)
    before=core.query('state')['revision']
    with pytest.raises(BusinessError):
        rule(core,anchor,**patch)
    assert core.query('state')['revision']==before
    assert core.query('recurring_rules')['total']==0


def test_semantic_duplicate_in_new_client_reuses_rule_task_and_source(core):
    anchor = event(core)
    first = rule(core,anchor,materialize_date='2030-01-01',source_text='Synthetic syllabus p. 2')
    other = Core(core.store.root)
    second = rule(other,anchor,materialize_date='2030-01-01',source_text='A later citation does not silently replace evidence')
    assert second['reused'] is True and second['entity']['id'] == first['entity']['id']
    assert second['entity']['data']['source_text'] == 'Synthetic syllabus p. 2'
    assert second['materialization']['created_count'] == 0
    assert core.query('recurring_rules')['total'] == 1
    assert core.query('list',type='task')['total'] == 1


@pytest.mark.parametrize('changed', [{'content':'Different explicit work'}, {'days_before':2}, {'completion_gate':'Different completion'}, {'enabled':False}])
def test_same_anchor_title_changed_semantics_requires_versioned_update(core,changed):
    anchor = event(core)
    original = rule(core,anchor)['entity']
    with pytest.raises(BusinessError) as error:
        rule(core,anchor,**changed)
    assert error.value.code == 'recurring_rule_exists'
    assert error.value.details['id'] == original['id']
    assert core.query('recurring_rules')['total'] == 1


def test_idle_existing_occurrence_skips_plan_and_feedback_scans(core,monkeypatch):
    r = rule(core,event(core),materialize_date='2030-01-01')['entity']
    import management.recurring as recurring
    def expensive_scan_forbidden(*args):
        raise AssertionError('unchanged occurrence must not scan plans/feedback')
    monkeypatch.setattr(recurring,'_protected',expensive_scan_forbidden)
    assert not materialize(core,r,'2030-01-01')['created']
    assert tick(core,dt.datetime(2030,1,1,12,tzinfo=dt.timezone.utc))['created_count'] == 0


def test_damaged_rule_isolated_and_other_rule_plus_review_schedule_still_run(core):
    broken = rule(core,event(core),title='Broken synthetic rule')['entity']
    rule(core,event(core),title='Healthy rule')
    create(core,'schedule',workflow='checkin',time='08:00',timezone='Asia/Shanghai',frequency='daily',enabled=True)
    # A deliberately damaged fixture represents imported/legacy corruption, not a supported edit.
    with core.store.connect() as c:
        c.execute("UPDATE entities SET data=json_set(data,'$.anchor_id','missing-anchor') WHERE id=?",(broken['id'],))
    bg = Background(core,threading.Event())
    instant = dt.datetime(2030,1,1,12,tzinfo=dt.timezone.utc)
    bg.tick(instant)
    assert core.query('list',type='task')['total'] == 1
    notes = core.query('list',type='notification')['items']
    assert len(notes) == 2
    assert any(n['data'].get('recurring_rule_id') == broken['id'] for n in notes)
    assert any(n['data'].get('review_mode') == 'daily' for n in notes)
    before = core.query('state')['revision']
    bg.tick(instant)
    assert core.query('state')['revision'] == before
    rules = core.query('recurring_rules')['items']
    assert any(i['id']==broken['id'] and i['issues'][0]['code']=='rule_invalid' for i in rules)


def test_unexpected_recurring_tick_failure_does_not_skip_daily_review(core,monkeypatch):
    create(core,'schedule',workflow='checkin',time='08:00',timezone='Asia/Shanghai',frequency='daily',enabled=True)
    import management.recurring as recurring
    def broken(*args,**kwargs):
        raise RuntimeError('Synthetic internal error')
    monkeypatch.setattr(recurring,'tick',broken)
    Background(core,threading.Event()).tick(dt.datetime(2030,1,1,12,tzinfo=dt.timezone.utc))
    assert core.query('list',type='notification')['total'] == 1


def test_background_failure_after_partial_creation_rolls_back_rule_only(core,monkeypatch):
    broken = rule(core,event(core),title='Rule whose task insertion fails')['entity']
    rule(core,event(core),title='Independent successful rule')
    original = core._create
    def inject(c,p,rid):
        result=original(c,p,rid)
        if p.get('type')=='task' and p.get('data',{}).get('recurring_rule_id')==broken['id']:
            raise BusinessError('synthetic_storage_error','Fail after task insert')
        return result
    monkeypatch.setattr(core,'_create',inject)
    result=tick(core,dt.datetime(2030,1,1,12,tzinfo=dt.timezone.utc))
    assert result['created_count']==1 and result['notification_count']==1
    tasks=core.query('list',type='task')['items']
    assert len(tasks)==1 and tasks[0]['data']['recurring_rule_id']!=broken['id']
    with core.store.connect() as c:
        assert c.execute('SELECT count(*) FROM recurring_occurrences WHERE rule_id=?',(broken['id'],)).fetchone()[0]==0


def test_event_nonrecurring_anchor_generates_once(core):
    r=rule(core,event(core,recurrence='none'))['entity']
    assert materialize(core,r,'2030-01-01','2030-01-15')['created_count']==1
    assert materialize(core,r,'2030-01-16','2030-01-31')['created_count']==0


def test_cancelled_anchor_and_rule_do_not_mark_task_complete(core):
    anchor=event(core)
    r=rule(core,anchor,materialize_date='2030-01-01')['entity']
    command(core,'update',{'id':anchor['id'],'version':anchor['version'],'patch':{'status':'cancelled'}})
    result=materialize(core,r,'2030-01-01','2030-01-08')
    assert result['created_count']==0 and 'anchor_closed' in codes(result)
    assert core.query('list',type='task')['items'][0]['status']=='pending'
    assert core.query('list',type='feedback')['total']==0


def test_user_edit_is_preserved_when_rule_content_changes(core):
    anchor=event(core)
    r=rule(core,anchor,materialize_date='2030-01-01')['entity']
    task=core.query('list',type='task')['items'][0]
    changed=edit(core,task,notes='My independent detailed edit')
    result=command(core,'set_recurring_rule',{'id':r['id'],'version':r['version'],'content':'New future content','materialize_date':'2030-01-01'})['result']
    issue=next(i for i in result['materialization']['issues'] if i.get('task_id')==task['id'])
    assert 'task_changed' in issue['protected_reasons']
    assert core.query('get',id=task['id'])['entity']==changed
    later=materialize(core,result['entity'],'2030-01-08')['created'][0]
    assert later['data']['notes']=='New future content'


def test_generated_task_archived_by_undo_is_not_recreated_from_same_occurrence(core):
    anchor=event(core)
    r=rule(core,anchor)['entity']
    made=command(core,'materialize_recurring',{'rule_id':r['id'],'start':'2030-01-01'})
    command(core,'undo',{'request_id':made['request_id']})
    result=materialize(core,r,'2030-01-01')
    assert not result['created']
    assert core.query('get',id=made['result']['created'][0]['id'])['entity']['archived'] is True


def test_scope_query_tracks_anchor_move_and_next_occurrence_inherits_current_project(core):
    old_project=create(core,'project',title='Old project')
    new_project=create(core,'project',title='New project')
    anchor=create(core,'task',parent=old_project['id'],due_date='2030-01-02')
    r=rule(core,anchor)['entity']
    command(core,'move',{'id':anchor['id'],'version':anchor['version'],'parent_id':new_project['id']})
    assert core.query('recurring_rules',scope_id=old_project['id'])['total']==0
    assert core.query('recurring_rules',scope_id=new_project['id'])['items'][0]['id']==r['id']
    assert materialize(core,r,'2030-01-01')['created'][0]['parent_id']==new_project['id']


def test_scope_query_includes_owned_events_but_not_unrelated_events(core):
    course=create(core,'course')
    expected=rule(core,event(core,owner_id=course['id']))['entity']
    rule(core,event(core),title='Unrelated rule')
    result=core.query('recurring_rules',scope_id=course['id'])
    assert result['total']==1 and result['items'][0]['id']==expected['id']


def test_out_of_calendar_offset_returns_explicit_gap_not_server_crash(core):
    r=rule(core,create(core,'task',due_date='0001-01-01'),days_before=366)['entity']
    result=materialize(core,r,'2030-01-01')
    assert not result['created'] and 'recurring_date_range' in codes(result)


def instant(day):
    return dt.datetime.combine(dt.date.fromisoformat(day),dt.time(12),tzinfo=dt.timezone.utc)


def cursor(core,rule_id):
    with core.store.connect() as c:
        row=c.execute('SELECT * FROM recurring_cursors WHERE rule_id=?',(rule_id,)).fetchone()
        return dict(row) if row else None


def test_restart_catches_missed_preparation_before_future_anchor(core):
    r=rule(core,event(core,date='2030-01-04'),days_before=2)['entity']
    revision=core.query('state')['revision']
    first=tick(core,instant('2030-01-01'))
    assert first['created_count']==0 and cursor(core,r['id'])['last_business_date']=='2030-01-01'
    assert core.query('state')['revision']==revision  # technical progress only
    restarted=Core(core.store.root)
    result=tick(restarted,instant('2030-01-03'))
    assert result['created_count']==1
    task=restarted.query('list',type='task')['items'][0]
    assert task['data']['scheduled_date']=='2030-01-02' and task['data']['due_date']=='2030-01-04'
    assert task['data']['late_generation'] is True and task['status']=='pending'
    assert restarted.query('list',type='plan')['total']==0
    before_cursor=cursor(restarted,r['id'])
    before_revision=restarted.query('state')['revision']
    assert tick(restarted,instant('2030-01-03'))['created_count']==0
    assert cursor(restarted,r['id'])==before_cursor and restarted.query('state')['revision']==before_revision


def test_first_tick_never_backfills_a_new_rule(core):
    r=rule(core,event(core,date='2030-01-04'),days_before=2)['entity']
    assert tick(core,instant('2030-01-03'))['created_count']==0
    assert cursor(core,r['id'])['last_business_date']=='2030-01-03'
    assert core.query('list',type='task')['total']==0


def test_offline_cross_month_keeps_missed_task_pending_even_after_anchor(core):
    rule(core,event(core,date='2030-02-02',recurrence='none'))
    tick(core,instant('2030-01-31'))
    result=tick(Core(core.store.root),instant('2030-02-03'))
    assert result['created_count']==1
    task=core.query('list',type='task')['items'][0]
    assert task['data']['scheduled_date']=='2030-02-01' and task['status']=='pending'
    assert core.query('list',type='feedback')['total']==0


def test_offline_catchup_bounded_to_31_days_and_older_gap_visible(core):
    r=rule(core,event(core,date='2030-01-01',recurrence='daily'),days_before=0)['entity']
    tick(core,instant('2030-01-01'))
    result=tick(core,instant('2030-03-15'))
    assert result['created_count']==31 and result['notification_count']==1
    note=core.query('list',type='notification')['items'][0]
    gap=next(i for i in note['data']['issues'] if i['code']=='catchup_limited')
    assert gap['unscanned_start']=='2030-01-02' and gap['unscanned_end']=='2030-02-12'
    assert cursor(core,r['id'])['last_business_date']=='2030-03-15'
    assert core.query('list',type='task')['total']==32


def test_failed_rule_does_not_advance_cursor_and_later_retry_recovers(core,monkeypatch):
    r=rule(core,event(core,date='2030-01-04'),days_before=2)['entity']
    tick(core,instant('2030-01-01'))
    original=core._create
    def fail(c,p,rid):
        if p.get('type')=='task':
            raise BusinessError('synthetic_failure','Retryable synthetic task insertion')
        return original(c,p,rid)
    monkeypatch.setattr(core,'_create',fail)
    tick(core,instant('2030-01-03'))
    assert cursor(core,r['id'])['last_business_date']=='2030-01-01'
    monkeypatch.setattr(core,'_create',original)
    assert tick(core,instant('2030-01-03'))['created_count']==1
    assert cursor(core,r['id'])['last_business_date']=='2030-01-03'


def test_resolved_unknown_date_alert_can_recur_with_new_visible_notice(core):
    anchor=create(core,'task')
    r=rule(core,anchor)['entity']
    assert tick(core,instant('2030-01-01'))['notification_count']==1
    assert cursor(core,r['id']) is None
    anchor=edit(core,anchor,due_date='2030-02-01')
    revision=core.query('state')['revision']
    assert tick(core,instant('2030-01-01'))['notification_count']==0
    assert core.query('state')['revision']==revision
    with core.store.connect() as c:
        assert not c.execute('SELECT 1 FROM recurring_alerts WHERE rule_id=?',(r['id'],)).fetchone()
    edit(core,anchor,due_date=None)
    assert tick(core,instant('2030-01-01'))['notification_count']==1
    assert core.query('list',type='notification')['total']==2


def test_disabled_then_reenabled_rule_does_not_backfill_disabled_period(core):
    r=rule(core,event(core,date='2030-01-04'),days_before=2)['entity']
    tick(core,instant('2030-01-01'))
    disabled=command(core,'set_recurring_rule',{'id':r['id'],'version':r['version'],'enabled':False,'materialize_date':'2030-01-01'})['result']['entity']
    assert cursor(core,r['id']) is None
    tick(core,instant('2030-01-02'))
    enabled=command(core,'set_recurring_rule',{'id':disabled['id'],'version':disabled['version'],'enabled':True,'materialize_date':'2030-01-03'})['result']['entity']
    assert cursor(core,enabled['id']) is None
    assert tick(core,instant('2030-01-03'))['created_count']==0
    assert core.query('list',type='task')['total']==0


def test_alert_text_identifies_course_and_anchor_not_just_generic_rule(core):
    course=create(core,'course',title='Synthetic Course A')
    anchor=create(core,'task',title='Unknown-date Lab A',parent=course['id'])
    rule(core,anchor,title='Common preparation')
    tick(core,instant('2030-01-01'))
    text=core.query('list',type='notification')['items'][0]['data']['content']
    assert 'Synthetic Course A' in text and 'Unknown-date Lab A' in text


def test_timezone_change_does_not_reinterpret_old_cursor_dates_as_known_history(core):
    r=rule(core,event(core,date='2030-01-04'),days_before=2)['entity']
    tick(core,instant('2030-01-01'))
    command(core,'settings',{'settings':{'timezone':'America/Los_Angeles'}})
    result=tick(core,instant('2030-01-03'))
    assert result['created_count']==0 and result['notification_count']==1
    assert cursor(core,r['id'])['business_timezone']=='America/Los_Angeles'
    note=core.query('list',type='notification')['items'][0]
    assert note['data']['issues'][0]['code']=='business_timezone_changed'


@pytest.mark.parametrize('kind',['task','milestone','assessment'])
def test_nonrecurring_anchor_explicit_completion_prevents_preparation_even_if_pending(core,kind):
    course=create(core,'course')
    anchor=create(core,kind,parent=course['id'],due_date='2030-01-04')
    r=rule(core,anchor,days_before=2)['entity']
    command(core,'record_feedback',{'target_id':anchor['id'],'business_date':'2000-01-01','dimensions':{'completion':'done'},'source_text':'Synthetic earlier explicit completion'})
    result=materialize(core,r,'2030-01-02')
    assert not result['created'] and 'anchor_completed' in codes(result)
    assert core.query('get',id=anchor['id'])['entity']['status']=='active'


def test_future_dated_completion_does_not_suppress_tasks_now(core):
    anchor=create(core,'task',due_date='2030-01-04')
    r=rule(core,anchor,days_before=2)['entity']
    command(core,'record_feedback',{'target_id':anchor['id'],'business_date':'2099-01-01','dimensions':{'completion':'done'},'source_text':'Future synthetic feedback must not affect current decisions'})
    assert materialize(core,r,'2030-01-02')['created_count']==1


def test_completion_correction_reopens_anchor_and_attendance_does_not_close_it(core):
    anchor=create(core,'task',due_date='2030-01-04')
    r=rule(core,anchor,days_before=2)['entity']
    for dim in ({'completion':'done'},{'completion':'partial'},{'attendance':'attended'}):
        command(core,'record_feedback',{'target_id':anchor['id'],'business_date':'2000-01-01','dimensions':dim,'source_text':'Synthetic narrow factual correction'})
    assert materialize(core,r,'2030-01-02')['created_count']==1


def test_event_completion_suppresses_only_that_occurrence_not_series(core):
    anchor=event(core,date='2030-01-02')
    r=rule(core,anchor)['entity']
    command(core,'record_feedback',{'target_id':anchor['id'],'business_date':'2030-01-02','dimensions':{'completion':'done'},'source_text':'Only this occurrence explicitly done'})
    # First tick Jan 1 establishes the cursor before the gap. Injected time is
    # earlier than the future feedback, so no future fact is used on Jan 1.
    tick(core,instant('2029-12-31'))
    first=tick(core,instant('2030-01-03'))
    assert first['created_count']==0
    assert tick(core,instant('2030-01-08'))['created_count']==1
    task=core.query('list',type='task')['items'][0]
    assert task['data']['anchor_occurrence']=='2030-01-09'


def test_exact_31_missed_days_have_no_false_older_gap_notice(core):
    r=rule(core,event(core,date='2030-01-01',recurrence='daily'),days_before=0)['entity']
    tick(core,instant('2030-01-01'))
    result=tick(core,instant('2030-02-01'))
    assert result['created_count']==31 and result['notification_count']==0
    assert core.query('list',type='task')['total']==32
    assert cursor(core,r['id'])['last_business_date']=='2030-02-01'
