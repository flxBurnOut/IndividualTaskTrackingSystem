import datetime as dt
import threading
import uuid
from management.core import Core
from management.scheduler import Background


def test_failed_schedule_is_visible_once_without_false_success_or_retry_storm(tmp_path, monkeypatch):
    from management import reviews
    core = Core(tmp_path)
    state = core.query('state')
    core.command('create', {'type':'schedule','title':'Synthetic scheduled review','data':{'enabled':True,'workflow':'checkin','time':'08:00','timezone':'Asia/Shanghai','frequency':'daily'}}, request_id=str(uuid.uuid4()), epoch=state['epoch'], expected_revision=state['revision'])
    calls = []
    original = reviews.query_daily
    def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError('Synthetic validation failure')
    monkeypatch.setattr(reviews, 'query_daily', fail)
    worker = Background(core, threading.Event())
    instant = dt.datetime(2030, 1, 1, 1, tzinfo=dt.timezone.utc)
    worker.tick(instant)
    worker.tick(instant)
    reopened = Core(tmp_path)
    Background(reopened, threading.Event()).tick(instant)
    jobs = core.query('jobs')['items']
    assert len(jobs) == 1 and jobs[0]['status'] == 'failed'
    assert len(calls) == 1
    failed = reopened.query('job', id=jobs[0]['id'])['job']
    assert failed['kind'] == 'schedule' and failed['result'] is None
    assert failed['error'] == {'code':'schedule_error','message':'Synthetic validation failure'}
    assert core.query('list', type='notification')['total'] == 1
    assert core.query('list', type='checkin')['total'] == 0
    assert core.query('list', type='feedback')['total'] == 0
    assert core.query('list', type='review')['total'] == 0
    notice = core.query('list', type='notification')['items'][0]
    assert notice['data']['job_id'] == failed['id']
    with reopened.store.connect() as c:
        runs = c.execute('SELECT job_id FROM schedule_runs').fetchall()
    assert len(runs) == 1 and runs[0]['job_id'] == failed['id']
    monkeypatch.setattr(reviews, 'query_daily', original)
    Background(reopened, threading.Event()).tick(instant + dt.timedelta(days=1))
    assert reopened.query('list', type='notification')['total'] == 2
    assert len(reopened.query('jobs')['items']) == 1
    assert reopened.query('list', type='checkin')['total'] == 0
    assert reopened.query('list', type='review')['total'] == 0