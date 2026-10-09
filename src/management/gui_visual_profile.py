"""Local Beta visual preferences, independent of business data and Qt."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .data_space import require_beta_dir


STYLES = frozenset({'classic', 'glass'})
ENVIRONMENT_KEY = 'PERSONAL_MANAGEMENT_UI_STYLE'
_style = 'glass'


def _validated_style(style: str) -> str:
    if not isinstance(style, str) or style not in STYLES:
        raise ValueError('界面样式只能选择 classic 或 glass。')
    return style


def visual_style() -> str:
    """Return the configured process style; a fresh process starts with glass."""
    return _style


def set_visual_style(style: str) -> str:
    """Change this process only, including when an existing window is activated."""
    global _style
    _style = _validated_style(style)
    return _style


def _read_style(path: Path) -> str:
    try:
        with path.open('rb') as stream:
            content = stream.read(16385)
        if len(content) > 16384:
            raise ValueError('visual preferences are too large')
        value = json.loads(content.decode('utf-8'))
        if (not isinstance(value, dict) or type(value.get('version')) is not int
                or value['version'] != 1):
            raise ValueError('unsupported visual preferences version')
        return _validated_style(value.get('style'))
    except FileNotFoundError:
        return 'glass'
    except (OSError, ValueError, TypeError, RecursionError):
        # Preserve an unreadable or future-format file. A plain, readable UI is
        # the recovery path; only a later explicit choice replaces the file.
        return 'classic'


def _save_style(path: Path, style: str) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=path.parent, prefix='.ui-visual-',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'version': 1, 'style': style}, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def configure_visual_profile(data_dir, requested: str | None = None) -> str:
    """Configure the GUI after selecting its isolated Beta data directory.

    Explicit choices are persisted atomically. Environment overrides last only
    for this launch. Precedence is explicit choice, environment, local file,
    then glass for a missing file. Malformed local files fall back to classic.
    Invalid explicit/environment values raise without changing process state.
    """
    root = require_beta_dir(data_dir)
    path = root / 'ui-visual.json'
    if requested is not None:
        style = _validated_style(requested)
        _save_style(path, style)
    elif ENVIRONMENT_KEY in os.environ:
        style = _validated_style(os.environ[ENVIRONMENT_KEY])
    else:
        style = _read_style(path)
    return set_visual_style(style)
