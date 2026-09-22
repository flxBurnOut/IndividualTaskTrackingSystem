import uuid
import pytest
from management.core import Core
from management.extensions import CommandDefinition, QueryDefinition
from management.schemas import BusinessError


def test_registered_algorithm_uses_same_transaction_versions_and_receipts(tmp_path):
    core = Core(tmp_path)
    def create_pair(core, c, payload, rid):
        first = core._create(c, {'type': 'task', 'title': payload['title']}, rid)
        second = core._create(c, {'type': 'task', 'title': payload['title'] + ' followup', 'parent_id': first['id']}, rid)
        return {'ids': [first['id'], second['id']]}
    schema = {'type': 'object', 'properties': {'title': {'type': 'string', 'minLength': 1}}, 'required': ['title'], 'additionalProperties': False}
    core.extensions.register(CommandDefinition('example.create_pair', '建立两步任务', create_pair, schema))
    core.extensions.register(QueryDefinition('example.count_tasks', '任务统计', lambda core,c,p: {'count': c.execute("SELECT count(*) FROM entities WHERE type='task'").fetchone()[0]}, {'type':'object','additionalProperties':False}))
    state = core.query('state')
    request = str(uuid.uuid4())
    result = core.command('example.create_pair', {'title':'Synthetic'}, request_id=request, epoch=state['epoch'], expected_revision=state['revision'])
    assert core.query('example.count_tasks')['count'] == 2
    again = core.command('example.create_pair', {'title':'Synthetic'}, request_id=request, epoch=state['epoch'], expected_revision=state['revision'])
    assert again['replayed'] and result['result'] == again['result']
    assert 'example.create_pair' in core.query('capabilities')['commands']
    with pytest.raises(BusinessError):
        core.command('example.create_pair', {'title':4}, request_id=str(uuid.uuid4()), epoch=state['epoch'], expected_revision=result['revision'])
    assert core.query('example.count_tasks')['count'] == 2


def test_failed_extension_rolls_back_all_its_changes(tmp_path):
    core = Core(tmp_path)
    def fail(core,c,p,rid):
        core._create(c, {'type':'task','title':'Must rollback'}, rid)
        raise BusinessError('failed','Synthetic failure')
    core.extensions.register(CommandDefinition('example.fail','Failure',fail,{'type':'object'}))
    state=core.query('state')
    with pytest.raises(BusinessError):
        core.command('example.fail',{},request_id=str(uuid.uuid4()),epoch=state['epoch'],expected_revision=0)
    assert core.query('list')['total'] == 0
    assert core.query('state')['revision'] == 0
