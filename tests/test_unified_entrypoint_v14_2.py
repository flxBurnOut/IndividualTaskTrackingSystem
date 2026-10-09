from pathlib import Path
import sys

import pytest

from management import __main__, gui


@pytest.mark.parametrize('legacy', [False, True])
def test_normal_and_legacy_shortcuts_open_same_management_window(tmp_path, monkeypatch, legacy):
    calls = []
    monkeypatch.setattr(gui, 'run', lambda path, *, show_update=False, ui_style=None: calls.append((path, show_update, ui_style)) or 0)
    monkeypatch.setattr(sys, 'argv', ['personal-management', '--data-dir', str(tmp_path)] +
                        (['--launch-codex'] if legacy else []))
    assert __main__.main() == 0
    assert calls == [(Path(tmp_path), False, None)]
