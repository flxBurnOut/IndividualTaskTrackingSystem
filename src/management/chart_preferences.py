"""Shared chart defaults and validation, without GUI or persistence side effects."""
from __future__ import annotations

from .schemas import BusinessError


CHART_OPTIONS = {
    'weekly_style': [('每日比例柱图', 'columns'), ('每日横条', 'rows'), ('每日圆环卡片', 'tiles')],
    'dashboard_today_style': [('横条', 'bar'), ('圆环', 'ring')],
    'dashboard_tasks_style': [('横条', 'bar'), ('圆环', 'ring')],
    'review_daily_style': [('横条', 'bar'), ('圆环', 'ring')],
    'review_weekly_style': [('横条', 'bar'), ('圆环', 'ring')],
}
CHART_DEFAULTS = {key: options[0][1] for key, options in CHART_OPTIONS.items()}


def normalize_chart_preferences(value=None, *, current=None):
    """Read old or partial preferences; invalid stored values use known defaults."""
    result = dict(CHART_DEFAULTS)
    for candidate in (current, value):
        if not isinstance(candidate, dict):
            continue
        for key, options in CHART_OPTIONS.items():
            if isinstance(candidate.get(key), str) and candidate[key] in {item[1] for item in options}:
                result[key] = candidate[key]
    return result


def validate_chart_preferences(value, *, current=None):
    """Validate an explicit partial update and return a complete independent dict."""
    if not isinstance(value, dict) or set(value) - set(CHART_OPTIONS):
        raise BusinessError('validation', '图表设置包含未知位置，请重新打开图表设置。')
    for key, selected in value.items():
        if not isinstance(selected, str) or selected not in {item[1] for item in CHART_OPTIONS[key]}:
            raise BusinessError('validation', '请选择支持的图表样式。')
    return normalize_chart_preferences(value, current=current)
