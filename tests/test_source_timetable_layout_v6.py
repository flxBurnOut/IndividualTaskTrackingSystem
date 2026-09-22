"""Visual timetable evidence must include text-rich PDF layouts, within budget."""
import json
import os
from pathlib import Path
import subprocess
import sys

from PIL import Image
from reportlab.pdfgen import canvas


def pdf(path, pages):
    output = canvas.Canvas(str(path), pagesize=(900, 600))
    for page in range(1, pages + 1):
        output.drawString(40, 550, f'Synthetic weekly timetable page {page}; week 1 starts 2030-01-07')
        output.drawString(200, 500, 'Monday')
        output.drawString(480, 500, 'Wednesday')
        output.drawString(50, 430, '09:00 - 10:00')
        output.drawString(200, 430, 'Synthetic lecture A')
        output.drawString(480, 430, 'Synthetic tutorial B')
        # A vector table has spatial meaning without any image XObject.
        output.rect(180, 410, 270, 70)
        output.rect(460, 410, 270, 70)
        output.showPage()
    output.save()


def extract(tmp_path, pages, layout_mode=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / 'synthetic-timetable.pdf'
    pdf(source, pages)
    request = {'source_path': str(source), 'original_name': source.name}
    if layout_mode is not None:
        request['layout_mode'] = layout_mode
    (tmp_path/'input.json').write_text(json.dumps(request), encoding='utf-8')
    environment = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]/'src')
    result = subprocess.run([sys.executable, '-m', 'management.source_worker', str(tmp_path)],
                            capture_output=True, text=True, env=environment, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((tmp_path/'result.json').read_text(encoding='utf-8'))


def test_text_rich_pdf_timetable_has_original_text_and_spatial_page_evidence(tmp_path):
    result = extract(tmp_path, 2, 'timetable')
    assert result['ok'] and result['coverage']['complete']
    assert result['coverage']['visual_complete']
    assert result['coverage']['visual_pages'] == [1, 2]
    assert result['coverage']['unread_visual_pages'] == []
    assert [image['label'] for image in result['images']] == ['PDF第1页', 'PDF第2页']
    text = (tmp_path/'text.txt').read_text(encoding='utf-8')
    assert '[PDF第1页]' in text and '[PDF第2页]' in text
    assert 'Monday' in text and 'Synthetic tutorial B' in text
    for image in result['images']:
        with Image.open(tmp_path/image['path']) as value:
            assert max(value.size) <= 1600
            assert value.width > 600
            assert value.mode == 'RGB', 'Page evidence must not depend on transparent-paper handling'
            assert value.getpixel((0,0)) == (255,255,255)
            histogram = value.convert('L').histogram()
            assert histogram[255] > value.width * value.height * .70
            assert sum(histogram[:60]) > 500, 'White paper must retain readable dark text and table boundaries'


def test_more_than_eight_pdf_pages_never_claims_complete_weekly_coverage(tmp_path):
    result = extract(tmp_path, 9, 'timetable')
    assert result['ok'] and result['status'] == 'partial'
    assert len(result['images']) == 8
    assert result['coverage']['visual_pages'] == list(range(1, 9))
    assert result['coverage']['unread_visual_pages'] == [9]
    assert result['coverage']['unread_visual_pages_total'] == 1
    assert result['coverage']['text_complete'] is True
    assert result['coverage']['visual_complete'] is False
    assert result['coverage']['complete'] is False
    assert any('不能声称课表完整' in warning for warning in result['warnings'])


def test_ordinary_course_pdf_does_not_gain_unrequested_rendered_pages(tmp_path):
    result = extract(tmp_path, 2)
    assert result['ok'] and result['images'] == []
    assert 'layout_mode' not in result['coverage']
    assert result['status'] == 'readable'


def test_unknown_layout_mode_is_structured_failure_not_silent_default(tmp_path):
    result = extract(tmp_path, 1, 'invented-mode')
    assert not result['ok']
    assert result['coverage']['complete'] is False
    assert result['images'] == []
    assert any('读取方式' in warning for warning in result['warnings'])


def test_oversized_page_coverage_metadata_is_bounded_and_counts_all_omitted_pages(tmp_path):
    result = extract(tmp_path, 83, 'timetable')
    coverage = result['coverage']
    assert result['ok'] and result['status'] == 'partial'
    assert coverage['units_total'] == 83 and coverage['units_read'] == 80
    assert len(result['images']) == 8
    assert coverage['unread_visual_pages_total'] == 75
    assert coverage['unread_visual_pages'] == list(range(9, 81))
    assert coverage['unread_visual_pages_truncated'] is True
    assert coverage['visual_complete'] is False and coverage['complete'] is False


def test_old_text_only_pdf_cannot_silently_become_complete_timetable_context(tmp_path):
    import uuid
    import pytest
    from management.core import Core
    from management.schemas import BusinessError
    from management.sources import prepare_context
    core = Core(tmp_path/'synthetic-context')
    def command(name, payload):
        state = core.query('state')
        return core.command(name, payload, request_id=str(uuid.uuid4()), epoch=state['epoch'], expected_revision=state['revision'])['result']
    course = command('create', {'type':'course', 'title':'Synthetic old course'})['entity']
    table = command('apply_timetable', {'title':'Synthetic new timetable', 'semester_start':'2030-01-07',
        'semester_end':'2030-02-03', 'timezone':'Asia/Shanghai', 'source_text':'Synthetic confirmed schedule',
        'rows':[{'key':'lecture', 'title':'Synthetic lecture', 'weekday':0, 'start':'09:00', 'end':'10:00'}]})['entity']
    source = tmp_path/'source.pdf'
    pdf(source, 1)
    old = command('add_source', {'kind':'file', 'path':str(source), 'owner_id':course['id']})['entity']
    assert old['data']['extraction']['images'] == []
    scope = {'kind':'timetable', 'entity_id':table['id']}
    with pytest.raises(BusinessError) as error:
        prepare_context(core, {'scope':scope, 'source_ids':[old['id']]})
    assert error.value.code == 'timetable_layout_missing'
    new = command('add_source', {'kind':'file', 'path':str(source), 'owner_id':table['id']})['entity']
    assert new['id'] != old['id']
    assert new['data']['sha256'] == old['data']['sha256']
    assert new['data']['extraction']['coverage']['layout_mode'] == 'timetable'
    prepared = prepare_context(core, {'scope':scope, 'source_ids':[new['id']]})
    assert len(prepared['local_images']) == 1
    assert prepared['source_context'][0]['visuals'] == ['PDF第1页']
    assert prepared['source_versions'] == {new['id']:new['version']}
