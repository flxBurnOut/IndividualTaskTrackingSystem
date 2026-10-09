"""The Beta interface uses one visual design.

Legacy environment settings and ui-visual.json are neither read nor rewritten.
Appearance and accent preferences still belong to normal settings.
"""
from __future__ import annotations

from .data_space import require_beta_dir


def visual_style() -> str:
    return 'glass'


def set_visual_style(style: str) -> str:
    """Compatibility accessor: an old caller cannot restore the retired UI."""
    if not isinstance(style, str) or style not in {'classic', 'glass'}:
        raise ValueError('未知界面样式；Beta 现已统一使用新版界面。')
    return visual_style()


def configure_visual_profile(data_dir, requested: str | None = None) -> str:
    """Validate the selected space without creating or changing local files."""
    require_beta_dir(data_dir)
    return set_visual_style(requested) if requested is not None else visual_style()
