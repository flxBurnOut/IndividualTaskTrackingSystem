"""Conservative receipt handling for task, day-plan and actual-result writes.

Core checks same-epoch receipts before the business validators in daily_flow,
reviews, planning and create_plan. Those explicit rejections prove that the
original request did not commit. Transport, authentication, service and epoch
failures never prove that an earlier write was absent.
"""
UNKNOWN_WRITES = frozenset({
    'connection_lost', 'connection_error', 'timeout', 'request_timeout',
    'protocol_error', 'response_limit', 'internal_error',
})
DAY_WRITE_REJECTIONS = frozenset({
    'conflict', 'version_conflict', 'stale_revision', 'revision_conflict',
    'entity_conflict', 'validation', 'plan_conflict', 'rest_plan', 'plan_target',
    'completion_target', 'review_plan_conflict', 'review_target', 'review_no_plan',
    'task_deleted', 'occurrence_feedback', 'plan_history', 'context_changed',
    'completion_gate', 'uncertain_time', 'schedule_conflict', 'capacity',
    'dependency', 'duplicate', 'limit', 'rule_conflict', 'cycle', 'not_found',
})


def write_result_unknown(error, was_unknown):
    code = error.get('code')
    return code in UNKNOWN_WRITES or (was_unknown and code not in DAY_WRITE_REJECTIONS)
