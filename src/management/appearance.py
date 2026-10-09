"""Portable appearance preferences; no GUI/font discovery in the business service."""
from .schemas import BusinessError

ACCENT_LABELS = {'default': '默认配色', 'coral': '珊瑚', 'blue': '海蓝',
                 'violet': '鸢尾紫', 'rose': '玫瑰', 'teal': '青碧'}

DEFAULT_APPEARANCE = {'theme': 'light', 'accent': 'default', 'font_family': 'Microsoft YaHei UI', 'font_size': 13,
                      'workspace_sidebar_collapsed': False, 'show_assistants': False}


def normalize_appearance(value=None, current=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(DEFAULT_APPEARANCE) or current is not None and not isinstance(current, dict):
        raise BusinessError('validation', '显示设置包含未知项目。')
    result = {**DEFAULT_APPEARANCE, **(current or {}), **value}
    if not isinstance(result['theme'], str) or result['theme'] not in {'light', 'dark'}:
        raise BusinessError('validation', '请选择浅色或深色主题。')
    if not isinstance(result['accent'], str) or result['accent'] not in ACCENT_LABELS:
        raise BusinessError('validation', '请选择列表中的界面颜色。')
    if type(result['font_size']) is not int or not 11 <= result['font_size'] <= 20:
        raise BusinessError('validation', '字号应为 11 到 20 的整数。')
    family = result['font_family']
    if not isinstance(family, str) or len(family) > 200 or any(ord(char) < 32 for char in family):
        raise BusinessError('validation', '字体名称无效，请从本机字体列表选择。')
    if type(result['workspace_sidebar_collapsed']) is not bool:
        raise BusinessError('validation', '目录收起设置需要明确的开关值。')
    if type(result['show_assistants']) is not bool:
        raise BusinessError('validation', '助手入口需要明确的开关值。')
    return result


def assistants_visible(settings):
    """Keep an existing assistant user's entries; new installs start manually."""
    return bool(settings.get('appearance', {}).get('show_assistants', settings.get('ai', {}).get('enabled', False)))
