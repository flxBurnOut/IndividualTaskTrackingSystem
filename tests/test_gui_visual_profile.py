"""Retired visual switches cannot restore the old UI or write local data."""
import sys
from types import SimpleNamespace

import pytest

from management import __main__, gui_visual_profile as profile
from management.data_space import BetaIsolationError, workspace_root


def test_single_style_does_not_create_a_data_directory(tmp_path):
    root = tmp_path / 'uncreated'
    assert profile.set_visual_style('classic') == 'glass'
    assert profile.configure_visual_profile(root) == 'glass'
    assert profile.visual_style() == 'glass'
    assert not root.exists()


@pytest.mark.parametrize('content', [
    b'{"version":1,"style":"classic"}',
    b'{"version":1,"style":"glass"}',
    b'not json', b'\xff', b'[]', b'{}', b'null',
    b'{"version":2,"style":"future"}', b' ' * 16385,
])
@pytest.mark.parametrize('environment', ['classic', 'glass', 'invalid'])
def test_legacy_preferences_are_ignored_and_preserved(tmp_path, monkeypatch, content, environment):
    path = tmp_path / 'ui-visual.json'
    path.write_bytes(content)
    monkeypatch.setenv('PERSONAL_MANAGEMENT_UI_STYLE', environment)
    for requested in (None, 'classic', 'glass'):
        assert profile.configure_visual_profile(tmp_path, requested) == 'glass'
    assert profile.visual_style() == 'glass'
    assert path.read_bytes() == content
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('invalid', ['', 'future', 'Glass', ' glass ', None, {}, []])
def test_unknown_direct_style_requests_do_not_change_the_interface(invalid):
    with pytest.raises(ValueError):
        profile.set_visual_style(invalid)
    assert profile.visual_style() == 'glass'


def test_data_space_validation_is_retained():
    for requested in (None, 'classic'):
        with pytest.raises(BetaIsolationError):
            profile.configure_visual_profile(workspace_root() / 'not-a-beta-space', requested)


def test_gui_entrypoint_opens_new_style_without_touching_retired_preferences(tmp_path, monkeypatch):
    path = tmp_path / 'ui-visual.json'
    content = b'{"version":1,"style":"classic"}'
    path.write_bytes(content)
    monkeypatch.setenv('PERSONAL_MANAGEMENT_UI_STYLE', 'classic')
    opened = []

    def run(root, *, show_update=False):
        assert profile.visual_style() == 'glass'
        opened.append((root, show_update))
        return 0

    monkeypatch.setitem(sys.modules, 'management.gui', SimpleNamespace(run=run))
    monkeypatch.setitem(sys.modules, 'management.installation_state',
                        SimpleNamespace(resume_after_update=lambda root: None))
    monkeypatch.setattr(sys, 'argv', ['management', '--data-dir', str(tmp_path), '--show-update'])
    assert __main__.main() == 0
    assert opened == [(tmp_path, True)]
    assert path.read_bytes() == content
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('mode', ['--service', '--mcp', '--mcp-discussion', '--bootstrap-service'])
def test_non_gui_entrypoints_never_touch_visual_preferences(tmp_path, monkeypatch, mode):
    called = []
    stub = lambda *args, **kwargs: called.append(args) or 0
    monkeypatch.setitem(sys.modules, 'management.service', SimpleNamespace(run_service=stub))
    monkeypatch.setitem(sys.modules, 'management.mcp_server', SimpleNamespace(run=stub))
    monkeypatch.setitem(sys.modules, 'management.discussion_mcp',
                        SimpleNamespace(create_server=lambda *args: SimpleNamespace(run=stub)))
    monkeypatch.setattr('management.runtime.bootstrap_service', stub)
    monkeypatch.setattr(profile, 'configure_visual_profile',
                        lambda *args: pytest.fail('non-GUI entrypoint touched visual preferences'))
    monkeypatch.setenv('PERSONAL_MANAGEMENT_UI_STYLE', 'invalid-but-irrelevant-to-service')
    arguments = ['management', '--data-dir', str(tmp_path), mode]
    if mode == '--mcp-discussion':
        arguments.append('fictional-discussion')
    monkeypatch.setattr(sys, 'argv', arguments)
    assert __main__.main() == 0
    assert len(called) == 1
    assert not (tmp_path / 'ui-visual.json').exists()


@pytest.mark.parametrize('style', ['classic', 'glass', 'future'])
def test_retired_command_line_switch_is_rejected_before_opening_data(tmp_path, monkeypatch, capsys, style):
    monkeypatch.setattr(sys, 'argv', ['management', '--data-dir', str(tmp_path / 'uncreated'),
                                     '--ui-style', style])
    with pytest.raises(SystemExit) as invalid_exit:
        __main__.main()
    assert invalid_exit.value.code == 2
    assert 'unrecognized arguments: --ui-style' in capsys.readouterr().err
    assert not (tmp_path / 'uncreated').exists()
