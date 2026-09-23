"""Visible effects reflect actual automation, without changing user configuration."""
import datetime as dt
import pytest
from management.core import Core
from management import habits
from management.recurring import tick
from test_plan_assistance_v8 import cmd
from test_recurring_v4 import event,rule,materialize

@pytest.fixture
def core(tmp_path,monkeypatch):
    monkeypatch.setattr(habits,'_now',lambda:dt.datetime(2030,1,2,2,tzinfo=dt.timezone.utc))
    return Core(tmp_path/'data')


def create_rule(core,title,**data):return cmd(core,'create',{'type':'rule','title':title,'data':data})['entity']


def test_habits_distinguish_guidance_warning_and_task_automation_and_do_not_write(core):
    create_rule(core,'Sleep and buffer',rule_kind='behavior',policy='Keep rest; unknown available hours stay unknown.')
    create_rule(core,'Prepare before class',rule_kind='warning',days_before=1,target_types=['event'],event_kind=['tutorial'])
    event(core,title='Example tutorial',date='2030-01-04',event_kind='tutorial')
    before=core.query('state');view=core.query('habits_overview')
    assert view['summary']=={'warning_rules':1,'guidance_rules':1,'validation_rules':0,'preparation_total':0,'preparation_enabled':0}
    assert view['preparations']['items']==[] and '尚未设置' in view['gap']
    assert view['anchors'][0]['suggested_days_before']==1
    assert '不创建待办' in next(e for e in view['rules']['items'] if e['title']=='Prepare before class')['effect']
    assert '参考' in next(e for e in view['rules']['items'] if e['title']=='Sleep and buffer')['effect']
    assert core.query('state')==before and core.query('list',type='task')['total']==0


def test_disabled_expired_and_completed_habits_are_not_given_to_codex(core):
    paused=create_rule(core,'Paused',rule_kind='behavior',policy='Do not apply this',enabled=False)
    expired=create_rule(core,'Expired',rule_kind='behavior',policy='Expired policy',effective_until='2029-12-31')
    future=create_rule(core,'Future',rule_kind='behavior',policy='Future policy',effective_from='2030-01-20')
    done=create_rule(core,'Closed',rule_kind='behavior',policy='Closed policy')
    cmd(core,'update',{'id':done['id'],'version':done['version'],'patch':{'status':'done'}})
    active=create_rule(core,'Active',rule_kind='behavior',policy='The only applicable preference')
    context=core.query('plan_context',date='2030-01-02');assert [r['id'] for r in context['rules']]==[active['id']]
    view=core.query('habits_overview');states={r['id']:r['display_state'] for r in view['rules']['items']}
    assert states[paused['id']]=='已停用' and states[done['id']]=='已停用'
    assert states[expired['id']]=='已到期' and states[future['id']]=='尚未到生效日期'
    assert view['summary']['guidance_rules']==1


def test_reminder_next_trigger_matches_delivery_ledger_and_disabled_is_explicit(core,monkeypatch):
    cmd(core,'set_review_preferences',{'daily':{'enabled':True,'time':'20:00'},'weekly':{'enabled':False,'weekday':6,'time':'19:30'},'timezone':'Asia/Shanghai'})
    view=core.query('habits_overview',compact=True)
    assert view['reminders']['daily']['next_label']=='2030-01-02 20:00'
    assert view['reminders']['weekly']['next_label']=='未启用'
    monkeypatch.setattr(habits,'_now',lambda:dt.datetime(2030,1,2,13,tzinfo=dt.timezone.utc))
    assert '补发' in core.query('habits_overview')['reminders']['daily']['next_label']
    with core.store.connect() as c:
        c.execute('INSERT INTO schedule_runs VALUES (?,?,?,?)',(view['reminders']['daily']['schedule_id'],'2030-01-02T20:00[Asia/Shanghai]',None,'2030-01-02T12:00:00+00:00'))

    assert core.query('habits_overview')['reminders']['daily']['next_label']=='2030-01-03 20:00'


def test_preview_shows_next_date_and_actual_created_state_without_generating_again(core):
    anchor=event(core,date='2030-01-04',event_kind='tutorial')
    r=rule(core,anchor)['entity'];before=core.query('state')
    view=core.query('habits_overview');assert view['preparations']['items'][0]['next_label']=='2030-01-03 · 将生成待办'
    assert core.query('state')==before and core.query('list',type='task')['total']==0
    materialize(core,r,'2030-01-03')
    view=core.query('habits_overview');assert view['preparations']['items'][0]['next_label']=='2030-01-03 · 待办已生成'
    assert core.query('list',type='task')['total']==1 and core.query('list',type='plan')['total']==0


def test_cancelled_occurrence_is_not_suggested_for_preparation(core):
    event(core,date='2030-01-04',event_kind='tutorial',exceptions={'2030-01-04':{'cancelled':True}})
    view=core.query('habits_overview')
    assert view['anchors'][0]['next_date']=='2030-01-11'


def test_rule_list_paging_does_not_change_total_counts(core):
    for i in range(35):create_rule(core,f'Habit {i:02}',rule_kind='behavior',policy='Keep known bounds')
    first=core.query('habits_overview');second=core.query('habits_overview',rules_offset=30)
    assert first['summary']['guidance_rules']==second['summary']['guidance_rules']==35
    assert first['rules']['next_offset']==30 and len(second['rules']['items'])==5


def test_pause_from_editor_cannot_change_task_feedback_or_old_plans(core):
    e=create_rule(core,'Pause me',rule_kind='behavior',policy='Original scope',custom_extension='Preserved')
    cmd(core,'update',{'id':e['id'],'version':e['version'],'patch':{'data':{**e['data'],'enabled':False}}})
    assert core.query('list',type='task')['total']==0 and core.query('list',type='feedback')['total']==0
    assert core.query('get',id=e['id'])['entity']['data']['custom_extension']=='Preserved'
    assert core.query('plan_context',date='2030-01-02')['rules']==[]


def test_mixed_type_filter_is_marked_for_review_instead_of_claiming_full_coverage(core):
    entity=create_rule(core,'Projects and assignments',rule_kind='warning',calendar_months_before=1,target_types=['project','milestone','task'],task_kind=['assignment'])
    before=core.query('state');view=core.query('habits_overview')['rules']['items'][0]
    assert view['display_state']=='范围待核对' and '只匹配任务' in view['scope_warning']
    assert core.query('state')==before and core.query('get',id=entity['id'])['entity']['data']['target_types']==['project','milestone','task']
