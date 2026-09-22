import json
from pathlib import Path
import threading
import time
import uuid
import pytest
from management.core import Core
from management.document_worker import produce
from management.resources import ResourceManager, ResourceError
from management.scheduler import Background


def test_docx_and_pdf_production_and_pdf_question_notebook(tmp_path):
    manager = ResourceManager(tmp_path / 'data')
    produced = {}
    for kind in ['docx', 'pdf']:
        id = str(uuid.uuid4())
        produced[kind] = produce(manager, id, {'kind': kind, 'title': '合成练习', 'relative_path': 'practice.' + kind, 'content': '题目 1：计算 2 + 3。\n保留作答区，不生成答案。'}, threading.Event())
        assert produced[kind]['validation']['format'] == 'verified'
        assert produced[kind]['validation']['layout'] == 'not_rendered'
        assert (tmp_path / 'data' / 'blobs' / produced[kind]['sha256']).exists()
    value = produce(manager, str(uuid.uuid4()), {'kind': 'pdf_notebook', 'title': '练习', 'relative_path': 'practice.ipynb', 'source_sha256': produced['pdf']['sha256'], 'pages': [1]}, threading.Event())
    notebook = json.loads((tmp_path / 'data' / 'blobs' / value['sha256']).read_text('utf-8'))
    assert notebook['metadata']['generated_answers'] is False
    assert any('2 + 3' in c['source'] for c in notebook['cells'] if c['cell_type'] == 'markdown')
    assert all(c['outputs'] == [] and c['execution_count'] is None for c in notebook['cells'] if c['cell_type'] == 'code')
    assert value['validation']['answers'] == 'not_provided'


def test_invalid_pdf_page_fails_without_success_metadata(tmp_path):
    manager = ResourceManager(tmp_path / 'data')
    pdf = produce(manager, str(uuid.uuid4()), {'kind': 'pdf', 'relative_path': 'sample.pdf', 'title': 'Synthetic', 'content': 'One page'}, threading.Event())
    id = str(uuid.uuid4())
    with pytest.raises(ResourceError) as raised:
        produce(manager, id, {'kind': 'pdf_notebook', 'relative_path': 'bad.ipynb', 'source_sha256': pdf['sha256'], 'pages': [99]}, threading.Event())
    assert raised.value.code == 'INVALID_PAGES'
    workspace = json.loads((tmp_path / 'data' / 'jobs' / id / '.workspace.json').read_text('utf-8'))
    assert workspace['files'][0]['state'] == 'registered'
    assert not (tmp_path / 'data' / 'jobs' / id / 'bad.ipynb').exists()


def test_durable_artifact_job_publishes_only_after_execution(tmp_path):
    core, stop = Core(tmp_path / 'data'), threading.Event()
    state = core.query('state')
    receipt = core.command('create_artifact_job', {'kind': 'docx', 'title': '合成文档', 'relative_path': 'review.docx', 'content': 'Synthetic evidence, not a real user record.'}, request_id=str(uuid.uuid4()), epoch=state['epoch'], expected_revision=state['revision'])
    job_id = receipt['result']['job']['id']
    assert core.query('list', type='artifact')['total'] == 0
    background = Background(core, stop)
    background.start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            job = core.query('job', id=job_id)['job']
            if job['status'] in {'completed', 'failed'}:
                break
            time.sleep(.05)
        assert job['status'] == 'completed', job.get('error')
        entity = core.query('list', type='artifact')['items'][0]
        assert entity['data']['adopted_at'] is None
        assert entity['data']['externally_submitted'] is False
        assert entity['data']['validation']['format'] == 'verified'
    finally:
        stop.set()
        for thread in background.threads:
            thread.join(3)
