"""One candidate command registry used by validation, dispatch and MCP."""
CANDIDATE_COMMANDS = frozenset({
    'apply_timetable','create','update','record_feedback','create_plan','revise_plan','add_to_plan',
    'set_task_completion','submit_daily_review','save_review','set_recurring_rule',
    'set_recovery_task','record_recovery_progress','correct_recovery_scope',
})
