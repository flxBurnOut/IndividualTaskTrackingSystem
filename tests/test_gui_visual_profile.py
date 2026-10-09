"""Visual switches are local preferences, never service or business writes."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from management import __main__, gui_visual_profile as profile
from management.data_space import BetaIsolationError, workspace_root


@pytest.fixture(autouse=True)
def isolated_visual_profile(monkeypatch):
    previous = profile.visual_style()
    monkeypatch.delenv(profile.ENVIRONMENT_KEY, raising=False)
    profile.set_visual_style('glass')
    yield
    profile.set_visual_style(previous)


def test_missing_preference_defaults_to_glass_without_creating_data(tmp_path):
    root = tmp_path / 'uncreated'
    profile.set_visual_style('classic')
    assert profile.configure_visual_profile(root) == 'glass'
    assert profile.visual_style() == 'glass'
    assert not root.exists()


@pytest.mark.parametrize('style', ['classic', 'glass'])
def test_explicit_choice_persists_and_reopens_in_its_own_data_space(tmp_path, style):
    root = tmp_path / 'one'
    assert profile.configure_visual_profile(root, style) == style
    path = root / 'ui-visual.json'
    assert json.loads(path.read_text(encoding='utf-8')) == {'version': 1, 'style': style}
    assert list(root.iterdir()) == [path]
    profile.set_visual_style('glass' if style == 'classic' else 'classic')
    assert profile.configure_visual_profile(root) == style
    assert profile.configure_visual_profile(tmp_path / 'two') == 'glass'
    assert not (tmp_path / 'two').exists()
    assert profile.configure_visual_profile(root) == style


def test_environment_override_is_temporary_and_explicit_choice_wins(tmp_path, monkeypatch):
    profile.configure_visual_profile(tmp_path, 'classic')
    path = tmp_path / 'ui-visual.json'
    saved = path.read_bytes()
    monkeypatch.setenv(profile.ENVIRONMENT_KEY, 'glass')
    assert profile.configure_visual_profile(tmp_path) == 'glass'
    assert path.read_bytes() == saved
    assert profile.configure_visual_profile(tmp_path, 'classic') == 'classic'
    monkeypatch.delenv(profile.ENVIRONMENT_KEY)
    assert profile.configure_visual_profile(tmp_path) == 'classic'


def test_live_switch_changes_only_the_process_not_the_saved_choice(tmp_path, monkeypatch):
    profile.configure_visual_profile(tmp_path, 'classic')
    path = tmp_path / 'ui-visual.json'
    saved = path.read_bytes()
    monkeypatch.setenv(profile.ENVIRONMENT_KEY, 'classic')
    assert profile.set_visual_style('glass') == 'glass'
    assert profile.visual_style() == 'glass'
    assert path.read_bytes() == saved
    assert profile.configure_visual_profile(tmp_path) == 'classic'


@pytest.mark.parametrize('content', [
    b'not json', b'\xff', b'[]', b'{}', b'null',
    b'{"version":2,"style":"glass"}',
    b'{"version":true,"style":"glass"}',
    b'{"version":1,"style":"unknown"}',
    b'{"version":1,"style":[]}', b' ' * 16385, b'[' * 2000 + b']' * 2000,
])
def test_damaged_or_future_local_file_falls_back_without_overwriting(tmp_path, content):
    path = tmp_path / 'ui-visual.json'
    path.write_bytes(content)
    assert profile.configure_visual_profile(tmp_path) == 'classic'
    assert profile.visual_style() == 'classic'
    assert path.read_bytes() == content
    assert not list(tmp_path.glob('.ui-visual-*.tmp'))


@pytest.mark.parametrize('invalid', ['', 'future', 'Glass', ' glass ', None, {}, []])
def test_invalid_live_style_leaves_current_state_unchanged(invalid):
    profile.set_visual_style('classic')
    with pytest.raises(ValueError):
        profile.set_visual_style(invalid)
    assert profile.visual_style() == 'classic'


def test_invalid_request_or_environment_preserves_file_and_process(tmp_path, monkeypatch):
    profile.configure_visual_profile(tmp_path, 'classic')
    path = tmp_path / 'ui-visual.json'
    saved = path.read_bytes()
    with pytest.raises(ValueError):
        profile.configure_visual_profile(tmp_path, 'future')
    monkeypatch.setenv(profile.ENVIRONMENT_KEY, 'future')
    with pytest.raises(ValueError):
        profile.configure_visual_profile(tmp_path)
    assert profile.visual_style() == 'classic'
    assert path.read_bytes() == saved
    # A valid explicit repair remains possible despite a stale environment.
    assert profile.configure_visual_profile(tmp_path, 'glass') == 'glass'


def test_atomic_replace_failure_preserves_previous_choice_and_cleans_temporary(tmp_path, monkeypatch):
    profile.configure_visual_profile(tmp_path, 'classic')
    path = tmp_path / 'ui-visual.json'
    saved = path.read_bytes()

    def unavailable(source, target):
        assert Path(target) == path
        assert Path(source).parent == tmp_path
        assert json.loads(Path(source).read_text(encoding='utf-8'))['style'] == 'glass'
        assert path.read_bytes() == saved
        raise OSError('simulated replacement failure')

    monkeypatch.setattr(profile.os, 'replace', unavailable)
    with pytest.raises(OSError, match='simulated replacement failure'):
        profile.configure_visual_profile(tmp_path, 'glass')
    assert path.read_bytes() == saved
    assert profile.visual_style() == 'classic'
    assert not list(tmp_path.glob('.ui-visual-*.tmp'))


def test_rejects_unowned_data_space_before_reading_or_writing(monkeypatch):
    monkeypatch.setattr(profile, '_read_style', lambda path: pytest.fail('must validate directory first'))
    monkeypatch.setattr(profile, '_save_style', lambda *args: pytest.fail('must validate directory first'))
    for requested in (None, 'classic'):
        with pytest.raises(BetaIsolationError):
            profile.configure_visual_profile(workspace_root() / 'not-a-beta-space', requested)


@pytest.mark.parametrize('style', ['classic', 'glass'])
def test_gui_entrypoint_configures_before_opening_and_forwards_switch(tmp_path, monkeypatch, style):
    opened = []

    def run(path, *, show_update=False, ui_style=None):
        assert profile.visual_style() == style
        assert json.loads((path / 'ui-visual.json').read_text(encoding='utf-8'))['style'] == style
        opened.append((path, show_update, ui_style))
        return 0

    monkeypatch.setitem(sys.modules, 'management.gui', SimpleNamespace(run=run))
    monkeypatch.setitem(sys.modules, 'management.installation_state',
                        SimpleNamespace(resume_after_update=lambda path: None))
    monkeypatch.setattr(sys, 'argv', ['management', '--data-dir', str(tmp_path),
                                     '--ui-style', style, '--show-update'])
    assert __main__.main() == 0
    assert opened == [(tmp_path, True, style)]


@pytest.mark.parametrize('mode', ['--service', '--mcp', '--mcp-discussion', '--bootstrap-service'])
def test_non_gui_entrypoints_never_configure_visual_preferences(tmp_path, monkeypatch, mode):
    called = []
    stub = lambda *args, **kwargs: called.append(args) or 0
    monkeypatch.setitem(sys.modules, 'management.service', SimpleNamespace(run_service=stub))
    monkeypatch.setitem(sys.modules, 'management.mcp_server', SimpleNamespace(run=stub))
    monkeypatch.setitem(sys.modules, 'management.discussion_mcp',
                        SimpleNamespace(create_server=lambda *args: SimpleNamespace(run=stub)))
    monkeypatch.setattr('management.runtime.bootstrap_service', stub)
    monkeypatch.setattr(profile, 'configure_visual_profile',
                        lambda *args: pytest.fail('non-GUI entrypoint touched visual preferences'))
    monkeypatch.setenv(profile.ENVIRONMENT_KEY, 'invalid-but-irrelevant-to-service')
    arguments = ['management', '--data-dir', str(tmp_path), '--ui-style', 'classic', mode]
    if mode == '--mcp-discussion':
        arguments.append('fictional-discussion')
    monkeypatch.setattr(sys, 'argv', arguments)
    assert __main__.main() == 0
    assert len(called) == 1
    assert not (tmp_path / 'ui-visual.json').exists()


def test_visual_option_is_hidden_and_rejects_unknown_values(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['management', '--help'])
    with pytest.raises(SystemExit) as help_exit:
        __main__.main()
    assert help_exit.value.code == 0
    assert '--ui-style' not in capsys.readouterr().out
    monkeypatch.setattr(sys, 'argv', ['management', '--data-dir', str(tmp_path), '--ui-style', 'future'])
    with pytest.raises(SystemExit) as invalid_exit:
        __main__.main()
    assert invalid_exit.value.code == 2
    assert not (tmp_path / 'ui-visual.json').exists()
