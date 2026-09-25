
"""Explicit review categories; attendance never becomes task completion."""
KIND_LABELS={
    'task':'任务','course_task':'课程任务','project_task':'项目任务',
    'activity_task':'活动任务','goal_task':'目标任务',
    'course_attendance':'课程','attendance':'出勤',
    'course_schedule':'课程安排','project_schedule':'项目安排',
    'activity_schedule':'活动安排','goal_schedule':'目标安排','schedule':'固定安排',
}
RESULT_LABELS={'done':'已完成','incomplete':'未完成','unreported':'未反馈',
    'attended':'已参加','absent':'未参加','missed_needs_catchup':'缺课需补',
    'partial':'部分完成','not_started':'未开始','blocked':'受阻','cancelled':'已取消',
    'unknown':'待确认','reported':'已反馈 · 状态待核对'}
RESULT_ORDER=('done','incomplete','attended','missed_needs_catchup','absent',
              'partial','not_started','blocked','cancelled','reported','unknown','unreported')


def category(item):
    owner=item.get('owner_type')
    attendance=item.get('review_dimension')=='attendance' or item.get('result') in {'attended','absent','missed_needs_catchup'}
    if attendance:return 'course_attendance' if owner=='course' else 'attendance'
    suffix='schedule' if item.get('fixed_schedule') else 'task'
    return owner+'_'+suffix if owner in {'course','project','activity','goal'} else suffix


def aggregate(items):
    counts={}
    for item in items:
        kind=category(item)
        result=(item.get('result') or item.get('raw_result') or 'reported') if item.get('reported') else 'unreported'
        key=kind+':'+str(result)
        row=counts.setdefault(key,{'key':key,'kind':kind,'result':result,'count':0})
        row['count']+=1
    return merge(counts.values())


def merge(rows):
    counts={}
    for row in rows:
        value=counts.setdefault(row['key'],dict(row,count=0))
        value['count']+=row['count']
    def order(row):
        return (list(KIND_LABELS).index(row['kind']) if row['kind'] in KIND_LABELS else 99,
                RESULT_ORDER.index(row['result']) if row['result'] in RESULT_ORDER else 99,row['key'])
    return sorted(counts.values(),key=order)


def label(row):
    return KIND_LABELS.get(row['kind'],'事项')+' · '+RESULT_LABELS.get(str(row['result']),str(row['result']))


def color_role(row):
    result=row['result']
    if result=='attended':return 'other_reported'
    if result=='absent':return 'danger'
    if result in {'incomplete','missed_needs_catchup','partial','not_started','blocked'}:return 'incomplete'
    if result=='done':return 'done'
    if result in {'unreported','unknown'}:return 'unreported'
    return 'other_reported'
