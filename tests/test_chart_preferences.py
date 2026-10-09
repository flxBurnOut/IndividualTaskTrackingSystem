"""Per-location chart changes preserve legacy and unrelated preferences."""
import pytest

from management.chart_preferences import (
    CHART_DEFAULTS, normalize_chart_preferences, validate_chart_preferences,
)
from management.schemas import BusinessError
from test_service import service, stop_owned_service


def test_legacy_weekly_preference_gets_independent_defaults():
    old = {'weekly_style': 'rows'}
    result = normalize_chart_preferences(old)
    assert result['weekly_style'] == 'rows'
    assert all(result[key] == 'bar' for key in CHART_DEFAULTS if key != 'weekly_style')
    assert old == {'weekly_style': 'rows'}
    result['weekly_style'] = 'tiles'
    assert CHART_DEFAULTS['weekly_style'] == 'columns'


def test_partial_update_preserves_other_locations_without_aliasing():
    current = {'weekly_style': 'rows', 'review_daily_style': 'ring'}
    result = validate_chart_preferences({'dashboard_today_style': 'ring'}, current=current)
    assert result == {**CHART_DEFAULTS, **current, 'dashboard_today_style': 'ring'}
    assert current == {'weekly_style': 'rows', 'review_daily_style': 'ring'}


@pytest.mark.parametrize('invalid', [None, [], {'other': 'bar'}, {'weekly_style': 'line'},
                                    {'review_daily_style': True}, {'review_daily_style': ['ring']}])
def test_explicit_invalid_updates_are_rejected(invalid):
    with pytest.raises(BusinessError) as error:
        validate_chart_preferences(invalid)
    assert error.value.code == 'validation'


def test_invalid_old_values_can_be_read_without_crashing():
    result = normalize_chart_preferences({'weekly_style': ['bad'], 'review_daily_style': 'ring', 'old_key': 'ignored'})
    assert result == {**CHART_DEFAULTS, 'review_daily_style': 'ring'}


def test_core_chart_round_trip_partial_update_and_invalid_rollback(tmp_path):
    import uuid
    from management.core import Core
    core = Core(tmp_path)
    def save(charts):
        state = core.query('state')
        return core.command('settings', {'settings': {'charts': charts}}, request_id=str(uuid.uuid4()),
                            epoch=state['epoch'], expected_revision=state['revision'])
    before = core.query('settings')['settings']
    save({'weekly_style': 'tiles', 'review_daily_style': 'ring'})
    save({'dashboard_tasks_style': 'ring'})
    reopened = Core(tmp_path)
    stored = reopened.query('settings')['settings']
    assert stored['charts'] == {**CHART_DEFAULTS, 'weekly_style': 'tiles', 'review_daily_style': 'ring', 'dashboard_tasks_style': 'ring'}
    assert {k: v for k, v in stored.items() if k != 'charts'} == {k: v for k, v in before.items() if k != 'charts'}
    state = core.query('state')
    with pytest.raises(BusinessError):
        save({'dashboard_today_style': 'heatmap'})
    assert core.query('state') == state
    assert core.query('settings')['settings'] == stored


def test_all_five_chart_choices_persist_after_service_restart_and_partial_save(service):
    """Exercise the real HTTP command/receipt and a freshly loaded backend."""
    from management.client import Client
    before = service.query('settings')['settings']
    chosen = {'weekly_style': 'tiles', 'dashboard_today_style': 'ring',
              'dashboard_tasks_style': 'ring', 'review_daily_style': 'ring',
              'review_weekly_style': 'ring'}
    assert set(chosen) == set(CHART_DEFAULTS)
    receipt = service.command('settings', {'settings': {'charts': chosen}})
    assert receipt['result']['settings']['charts'] == chosen
    root = service.data_dir
    another_client = Client(root, autostart=False)
    assert another_client.query('settings')['settings']['charts'] == chosen

    # Reuse the normal synthetic-service fixture and its identity-checked
    # cleanup helper; no default Beta or installed data directory is opened.
    stop_owned_service(root)
    reopened = Client(root)
    stored = reopened.query('settings')['settings']
    assert stored['charts'] == chosen
    assert {key: value for key, value in stored.items() if key != 'charts'} == {
        key: value for key, value in before.items() if key != 'charts'}
    assert reopened.query('state')['counts'] == {}

    reopened.command('settings', {'settings': {'charts': {'review_daily_style': 'bar'}}})
    final = Client(root, autostart=False).query('settings')['settings']
    assert final['charts'] == {**chosen, 'review_daily_style': 'bar'}
    assert {key: value for key, value in final.items() if key != 'charts'} == {
        key: value for key, value in before.items() if key != 'charts'}
