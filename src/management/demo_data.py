"""Repeatable fictional Beta showcase, written through normal business commands.

The local journal preserves request identities across interruptions. It never
clears a database, rewrites an existing showcase, or copies a user's records.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo

from .data_space import require_beta_dir
from .runtime import OwnerLock


FORMAT = 'personal-management-beta-demo/1'
RECIPE_VERSION = 1
MARKER = 'beta-demo.json'
FICTION = '完全虚构的 Beta 演示资料，不代表任何真实个人、课程或执行记录。'


class DemoError(ValueError):
    pass


def _save(path, value):
    temporary = path.with_suffix('.json.new')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


class DemoBuilder:
    def __init__(self, client, root, journal):
        self.client, self.root, self.journal = client, root, journal
        self.path = root / MARKER
        self.day = dt.date.fromisoformat(journal['reference_date'])

    def date(self, offset=0):
        return (self.day + dt.timedelta(days=offset)).isoformat()

    def run(self, key, command, payload, *, inputs=None):
        inputs = payload if inputs is None else inputs
        previous = self.journal['steps'].get(key)
        if previous:
            if previous['command'] != command or previous['inputs'] != inputs:
                raise DemoError('演示配方与已保存步骤不一致；请使用新的 Beta 数据空间。')
            if previous.get('result') is not None:
                return previous['result']
            # A lost response may already have committed. Query before retry,
            # and still replay the original command to check its fingerprint.
            receipt = self.client.query('receipt', request_id=previous['request_id'])
            if not receipt['found']:
                operation = self.client.query('operation', request_id=previous['request_id'])
                if operation['found'] and operation['operation']['status'] != 'ready':
                    raise DemoError('演示资料导入尚待核对；保留原请求，不重新导入。')
                state = self.client.state()
                if state['revision'] != previous['expected_revision']:
                    raise DemoError('生成期间数据已被其他操作修改；已保留现有记录，请使用新空间。')
        else:
            state = self.client.state()
            if (state['epoch'] != self.journal['epoch']
                    or state['revision'] != self.journal['revision']):
                raise DemoError('演示空间或版本已经变化，未继续添加记录。')
            previous = {'command': command, 'inputs': inputs, 'payload': payload,
                        'request_id': 'beta-demo-' + uuid.uuid4().hex,
                        'epoch': state['epoch'], 'expected_revision': state['revision']}
            self.journal['steps'][key] = previous
            _save(self.path, self.journal)
        receipt = self.client.command(command, previous['payload'],
            request_id=previous['request_id'], epoch=previous['epoch'],
            expected_revision=previous['expected_revision'])
        previous['result'] = receipt['result']
        self.journal['revision'] = receipt['revision']
        _save(self.path, self.journal)
        return receipt['result']

    def entity(self, key, kind, title, parent=None, **data):
        return self.run(key, 'create', {'type': kind, 'title': '【演示】' + title,
            'parent_id': parent['id'] if parent else None, 'data': data})['entity']

    def batch(self, key, parameters):
        previous = self.journal['steps'].get(key)
        if previous:
            return self.run(key, previous['command'], previous['payload'], inputs=parameters)
        preview = self.client.query('preview_task_batch', **parameters)
        if preview['revision'] != self.journal['revision'] or preview['epoch'] != self.journal['epoch']:
            raise DemoError('预览时数据已变化，未保存批次。')
        return self.run(key, preview['command'], preview['payload'], inputs=parameters)

    def feedback(self, key, target, offset, **dimensions):
        return self.run(key, 'record_feedback', {'target_id': target['id'],
            'business_date': self.date(offset), 'dimensions': dimensions,
            'source_text': FICTION + '本条是为展示不同状态而构造的反馈。'})['entity']

    def file(self, name, content):
        folder = self.root / 'demo-inputs'
        folder.mkdir(exist_ok=True)
        path = folder / name
        temporary = folder / ('.' + name + '.pending')
        raw = content.encode('utf-8')
        if path.exists():
            if path.read_bytes() != raw:
                raise DemoError('演示原文件已有不同内容，未覆盖：' + name)
            if temporary.exists() and temporary.samefile(path):
                temporary.unlink()
        else:
            try:
                with temporary.open('wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Atomic create-if-absent: never overwrite a file created by an
                # editor between the existence check and publication.
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    if path.read_bytes() != raw:
                        raise DemoError('演示原文件已有不同内容，未覆盖：' + name)
            finally:
                temporary.unlink(missing_ok=True)
        return path


def _recipe(b):
    learn = b.entity('learn', 'domain', '学习实验室')
    work = b.entity('work', 'domain', '项目实验室')
    life = b.entity('life', 'domain', '日常与恢复')
    monday = b.day - dt.timedelta(days=b.day.weekday() + 14)
    course = b.entity('course-data', 'course', '数据思维入门', learn,
        code='DEMO101', term='虚构演示学期', semester_start=monday.isoformat(), semester_end=b.date(56))
    design = b.entity('course-design', 'course', '界面设计练习', learn,
        code='DEMO202', term='虚构演示学期', semester_start=monday.isoformat(), semester_end=b.date(56))
    project = b.entity('project', 'project', '社区阅读角原型', work,
        notes='虚构项目，演示层级、任务拆分、资料和成果。')
    subproject = b.entity('subproject', 'project', '信息展示原型', project)
    phase = b.entity('phase', 'phase', '调研与第一轮草图', subproject)
    activity = b.entity('activity', 'activity', '轻量恢复活动', life)
    guide = b.entity('guide', 'note', '先读这里：演示数据使用说明', project,
        content=(f'{FICTION}\n配方版本：{RECIPE_VERSION}；基准日期：{b.date()}。\n'
                 '建议顺序：总览→任务池与筛选→项目/课程→今天→复盘→资料→习惯。\n'
                 '包含未知估时、逾期、部分完成、已完成但未提交、依赖受阻、任务拆分、'
                 '课表、准备规则、补课进度、评分与未出分、资料搜索和分页。\n'
                 '所有任务都可手工操作。重复生成不会覆盖你的编辑；重新演示请选新的 Beta 目录。\n'
                 '外部助手未配置，账户授权、模型调用和真实回传不在本演示验收范围。'), source_text=FICTION)
    b.entity('course-note', 'note', '数据观察课学习笔记', course,
        content='虚构笔记：先记录观察，再检查样本与缺失值。未知结果保持待确认。', source_text=FICTION)
    b.entity('goal', 'goal', '两周内完成一次小型展示', work,
        due_date=b.date(14), success_criteria='完成原型、演示材料和一份复盘。')
    b.entity('milestone', 'milestone', '第一版原型可演示', project,
        due_date=b.date(10), acceptance='一页原型可以打开，三条使用场景可以讲清楚。')
    b.entity('inbox', 'inbox', '尚未整理的一个想法', None,
        content='也许可以增加阅读推荐区；范围和日期尚未确认。', source_text=FICTION)
    topic = b.entity('topic', 'topic', '样本与缺失值', course,
        description='识别样本范围，并区分零值与缺失值。', mastery='虚构错题尚待复测，不自动标为掌握。')
    question = b.entity('question', 'question', '缺失值能否直接记为零', topic,
        prompt='观察表中的一个值未记录，能否直接当作零？',
        answer_key='不能；应保留缺失状态，核对来源后再处理。', source_text=FICTION)
    b.entity('wrong-attempt', 'attempt', '待复测的错误示例', question,
        performed=True, business_date=b.date(-3), answer='直接记为零。', score=0, maximum=1,
        error='把未知与零混为一谈。', next_review=b.date(), source_text=FICTION, mastery_verified=False)
    b.entity('gap', 'gap', '报告详细要求尚未发布', course,
        gap_kind='not_released', last_checked=b.date(-1), next_check=b.date(2), source_text=FICTION)
    experiment = b.entity('experiment', 'experiment', '比较两种虚构展示布局', project,
        hypothesis='较短的说明是否更容易理解？', parameters='A：长说明；B：短说明。所有数值均为虚构。')
    b.entity('dataset', 'dataset', '虚构布局观察样本', experiment,
        version_label='demo-v1', description='只用于演示数据归属，不是实际实验数据。', source_text=FICTION)
    b.entity('run', 'run', '布局比较的虚构运行记录', experiment,
        environment='Beta 本地演示', outcome='仅展示记录方式，不代表真实试验结论。',
        metric=80, unit='虚构分', source_text=FICTION)
    assessment = b.entity('assessment', 'assessment', '阶段练习（取最好两次）', course,
        weight=40, aggregation='best_n', best_n=2, due_date=b.date(7))
    b.entity('assessment-pending', 'assessment', '课程报告（尚未出分）', course,
        weight=60, due_date=b.date(21))
    for index, score in enumerate((72, 85, 90)):
        b.entity(f'attempt-{index}', 'attempt', f'练习记录 {index + 1}', assessment,
            performed=True, business_date=b.date(-5 + index), score=score, maximum=100,
            source_text=FICTION, mastery_verified=False)

    tasks = {}
    definitions = [
        ('done', '已经整理的观察清单', course, {'due_date': b.date(-1), 'estimated_minutes': 25}),
        ('partial', '只完成一部分的设计草图', phase, {'due_date': b.date(1), 'estimated_minutes': 40}),
        ('overdue', '已逾期但没有完成反馈的练习', course, {'due_date': b.date(-2), 'estimated_minutes': 30}),
        ('unknown', '待澄清范围的灵感', None, {}),
        ('before', '先整理原型展示提纲', phase, {'due_date': b.date(2), 'estimated_minutes': 25}),
        ('after', '根据提纲绘制展示卡片', phase, {'due_date': b.date(3), 'estimated_minutes': 35}),
        ('blocked', '等待展示卡片后安排试讲', phase, {'due_date': b.date(4), 'estimated_minutes': 20}),
        ('submission', '内容完成但尚未提交的报告', design, {'due_date': b.date(2), 'estimated_minutes': 30}),
        ('split', '制作阅读角原型展示', project, {'due_date': b.date(10), 'estimated_minutes': 120}),
        ('tomorrow', '整理下一轮访谈问题', phase, {'due_date': b.date(5), 'estimated_minutes': 30}),
        ('rest', '整理桌面并散步', activity, {'estimated_minutes': 20}),
        ('deleted', '误录后删除的示例任务', project, {}),
    ]
    for key, title, parent, data in definitions:
        tasks[key] = b.entity('task-' + key, 'task', title, parent,
            completion_gate='完成标题所述动作，并留下可检查的简短记录。',
            source_text=FICTION, **data)
    for source, target in (('after', 'before'), ('blocked', 'after')):
        b.run('link-' + source, 'link', {'source_id': tasks[source]['id'],
            'target_id': tasks[target]['id'], 'kind': 'depends_on'})
    b.entity('checklist', 'checklist', '检查标题和图例', tasks['partial'],
        completion_gate='标题与图例均可读；勾选清单不自动完成父任务。')
    children = b.batch('split', {'mode': 'split', 'source_id': tasks['split']['id'],
        'source_version': tasks['split']['version'], 'sequential': True,
        'items': [{'title': '【演示】' + title, 'completion_gate': gate, 'estimated_minutes': 25}
                  for title, gate in [('整理展示材料', '写出三条展示要点'),
                                      ('制作展示页面', '完成一页可浏览的原型'),
                                      ('检查并记录问题', '留下三项检查结果')]]})['items']
    for group, owner in enumerate((course, design, phase)):
        b.batch(f'page-batch-{group}', {'mode': 'create', 'parent_id': owner['id'],
            'parent_version': owner['version'], 'sequential': False,
            'items': [{'title': f'【演示】分页样例 {group * 40 + index + 1:03d} · 资料整理',
                       'completion_gate': '核对一条虚构样例并写下整理结果。',
                       'estimated_minutes': None if index % 4 == 0 else 15,
                       'due_date': None if index % 3 == 0 else b.date(14 + index % 7)}
                      for index in range(40)]})

    b.run('yesterday-plan', 'create_plan', {'date': b.date(-1), 'mode': 'no_precise_time',
        'blocks': [{'target_id': tasks[key]['id'], 'minutes': minutes}
                   for key, minutes in [('done', 25), ('partial', 40)]], 'source_text': FICTION})
    b.feedback('feedback-done', tasks['done'], -1, completion='done', actual_minutes=25)
    b.feedback('feedback-partial', tasks['partial'], -1, completion='partial', actual_minutes=20)
    b.feedback('feedback-submission', tasks['submission'], -1,
               completion='done', submission='not_submitted', mastery='needs_review', actual_minutes=30)
    b.feedback('feedback-blocked', tasks['blocked'], 0, completion='blocked')
    b.run('today-plan', 'create_plan', {'date': b.date(), 'mode': 'no_precise_time',
        'blocks': [{'target_id': tasks[key]['id'], 'minutes': minutes}
                   for key, minutes in [('before', 25), ('after', 35), ('rest', 20)]], 'source_text': FICTION})
    b.run('tomorrow-plan', 'create_plan', {'date': b.date(1), 'mode': 'no_precise_time',
        'blocks': [{'target_id': tasks['tomorrow']['id'], 'minutes': 30}], 'source_text': FICTION})
    b.run('weekly-review', 'save_review', {'start': b.date(-7), 'end': b.date(-1),
        'title': '【演示】一周回顾', 'text': FICTION + '\n完成一项清单，草图仅部分完成；完成报告不代表已提交。'})
    recovery = b.run('recovery', 'set_recovery_task', {'course_id': course['id'],
        'title': '【演示】补齐基础单元', 'unit': '节', 'total_quantity': 8,
        'completion_gate': '看完八节并独立完成配套练习。',
        'source_text': FICTION + '虚构角色自述需要补齐基础单元。', 'reason': 'self_reported'})['entity']
    b.run('recovery-progress', 'record_recovery_progress', {'task_id': recovery['id'],
        'version': recovery['version'], 'business_date': b.date(-2), 'completed_quantity': 3,
        'actual_minutes': 45, 'source_text': FICTION + '累计完成三节，尚未完成练习。'})
    b.run('deleted-task', 'delete_task', {'id': tasks['deleted']['id'], 'version': tasks['deleted']['version']})

    b.run('timetable', 'apply_timetable', {'title': '【演示】虚构学期课表',
        'semester_start': monday.isoformat(), 'semester_end': b.date(56), 'timezone': 'Asia/Shanghai',
        'source_text': FICTION, 'rows': [
            {'key': 'demo-data', 'title': '【演示】数据思维讲座', 'weekday': b.day.weekday(),
             'start': '09:30', 'end': '10:30', 'owner_id': course['id'], 'event_kind': 'lecture', 'location': '虚构教室 A'},
            {'key': 'demo-design', 'title': '【演示】设计讨论', 'weekday': (b.day.weekday() + 2) % 7,
             'start': '14:00', 'end': '15:00', 'owner_id': design['id'], 'event_kind': 'tutorial', 'location': '虚构教室 B'}]})
    # A date-only anchor makes the disabled preparation-rule example independent
    # of the timetable result's generated row representation.
    anchor = b.entity('review-event', 'event', '原型展示日', None,
        owner_id=project['id'], date=b.date(10), time_kind='date_only', source_text=FICTION)
    b.run('preparation-rule', 'set_recurring_rule', {'anchor_id': anchor['id'],
        'title': '【演示】展示前准备（默认停用）', 'content': '整理虚构展示材料。',
        'completion_gate': '三份演示材料均已核对。', 'days_before': 2, 'estimated_minutes': 25,
        'enabled': False, 'materialize_date': b.date(), 'effective_until': b.date(14)})
    b.entity('schedule', 'schedule', '每日复盘提醒（默认停用）', None,
        workflow='checkin', time='20:00', timezone='Asia/Shanghai', frequency='daily',
        enabled=False, notify_unchanged=False)
    for key, title, data in [
        ('capacity', '每日演示容量', {'rule_kind': 'capacity', 'minutes': 180}),
        ('sleep', '保护休息时间', {'rule_kind': 'protected_time', 'start': '23:00', 'end': '07:00'}),
        ('warning', '提前七天提醒', {'rule_kind': 'warning', 'days_before': 7, 'target_types': ['task']}),
        ('behavior', '保留未知与缓冲', {'rule_kind': 'behavior', 'policy': '未知时长留空，安排少量重点并保留休息。'}),
    ]:
        b.entity('rule-' + key, 'rule', title, None, source_text=FICTION, **data)
    b.run('notice', 'add_source', {'owner_id': course['id'], 'kind': 'notice',
        'title': '【演示】评分与资料说明', 'text': FICTION + '\n阶段练习占40%，报告占60%。报告尚未出分。'})
    for key, name, content, owner in [
        ('handout', '演示讲义.md', '# 数据观察入门（虚构）\n\n' + FICTION + '\n\n## 三个步骤\n记录观察、核对缺失、说明限制。\n', course),
        ('csv', '演示观察数据.csv', 'sample,category,value\nA,synthetic,12\nB,synthetic,18\nC,synthetic,15\n', course),
        ('brief', '演示项目说明.txt', FICTION + '\n社区阅读角原型：一页界面、三条使用场景、一份问题清单。', project),
    ]:
        path = b.file(name, content)
        b.run('file-' + key, 'add_source', {'owner_id': owner['id'], 'kind': 'file',
            'title': '【演示】' + name, 'path': str(path)})
    b.run('settings', 'settings', {'settings': {'ai': {'enabled': False},
        'appearance': {'show_assistants': True}, 'favorites': [guide['id'], project['id'], course['id']]}})
    # Last business operation: the real local worker may now advance revision.
    # No model or external connector is required for this Markdown artifact.
    artifact = b.run('artifact', 'create_artifact_job', {'kind': 'markdown',
        'title': '【演示】本地生成的回顾草稿', 'relative_path': 'demo-review.md',
        'content': '# 虚构演示回顾\n\n' + FICTION + '\n\n此文件由本地后台生成，无模型调用。',
        'owner_id': project['id']})
    return {'guide_id': guide['id'], 'project_id': project['id'], 'course_id': course['id'],
            'task_ids': {key: value['id'] for key, value in tasks.items()},
            'split_child_ids': [item['id'] for item in children],
            'artifact_job_id': artifact['job']['id']}


def seed_demo(client, data_dir, *, reference_date=None):
    """Create or resume a showcase; an existing completed one is never rewritten."""
    root = require_beta_dir(data_dir)
    if Path(client.data_dir).resolve() != root:
        raise DemoError('演示生成器与业务连接的数据目录不一致。')
    reference = dt.date.fromisoformat(reference_date) if reference_date else None
    lock = OwnerLock(root / 'beta-demo.lock')
    if not lock.acquire():
        raise DemoError('另一处正在生成演示数据，请等待完成。')
    try:
        state = client.state()
        path = root / MARKER
        if path.exists():
            try:
                journal = json.loads(path.read_text('utf-8'))
                valid = (journal['format'] == FORMAT and journal['recipe_version'] == RECIPE_VERSION
                         and Path(journal['data_dir']).resolve() == root and journal['epoch'] == state['epoch']
                         and journal['status'] in {'building', 'complete'}
                         and isinstance(journal['steps'], dict) and type(journal['revision']) is int)
                day = dt.date.fromisoformat(journal['reference_date'])
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise DemoError('演示记录无法核验，未修改数据库。') from error
            if not valid or reference and reference != day:
                raise DemoError('演示版本、数据空间或基准日期不一致，请使用新的 Beta 空间。')
            if journal['status'] == 'complete':
                return {'reused': True, 'reference_date': day.isoformat(), 'data_dir': str(root),
                        'counts': state['counts'], 'scenarios': journal['scenarios']}
        else:
            if state['revision'] != 0 or state['counts']:
                raise DemoError('此 Beta 空间已有记录或设置，未覆盖；请选择新的空 Beta 目录生成演示。')
            if client.query('jobs', limit=1)['items']:
                raise DemoError('此空间已有作业，未生成演示数据。')
            unfinished = client.query('diagnostics').get('unfinished_file_operations')
            if unfinished is None:
                raise DemoError('当前后台尚不支持演示空库核验，请正常退出并重新启动 Beta 后再生成。')
            if unfinished:
                raise DemoError('此空间已有待核对的文件操作，未生成演示数据。')
            day = reference or dt.datetime.now(ZoneInfo('Asia/Shanghai')).date()
            journal = {'format': FORMAT, 'recipe_version': RECIPE_VERSION, 'fictional': True,
                       'data_dir': str(root), 'epoch': state['epoch'], 'revision': 0,
                       'reference_date': day.isoformat(), 'status': 'building', 'steps': {}}
            _save(path, journal)
        builder = DemoBuilder(client, root, journal)
        scenarios = _recipe(builder)
        journal.update(status='complete', scenarios=scenarios)
        _save(path, journal)
        return {'reused': False, 'reference_date': day.isoformat(), 'data_dir': str(root),
                'counts': client.state()['counts'], 'scenarios': scenarios}
    finally:
        lock.release()
