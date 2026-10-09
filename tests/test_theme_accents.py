"""Portable accent preferences and their live native rendering contracts."""
import os
import uuid

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton, QVBoxLayout

from management.appearance import ACCENT_LABELS, DEFAULT_APPEARANCE, normalize_appearance
from management.core import Core
from management.gui_materials import AppCanvas
from management.gui_theme import (DARK as CLASSIC_DARK, LIGHT as CLASSIC_LIGHT,
                                  apply_appearance, classic_palette_tokens, color,
                                  current_appearance, stylesheet)
from management.gui_theme_glass import DARK, LIGHT, palette_tokens
from management.gui_visual_profile import set_visual_style, visual_style
from management.schemas import BusinessError


def command(core, payload):
    state = core.query('state')
    return core.command('settings', {'settings': payload}, request_id=str(uuid.uuid4()),
                        epoch=state['epoch'], expected_revision=state['revision'])


@pytest.mark.parametrize('value', ['unknown', 'BLUE', None, True, [], {}])
def test_accent_rejects_invalid_values(value):
    with pytest.raises(BusinessError, match='界面颜色'):
        normalize_appearance({'accent': value})


def test_accent_persists_through_core_and_partial_updates(tmp_path):
    core = Core(tmp_path / 'synthetic-accents')
    command(core, {'appearance': {'accent': 'violet', 'font_size': 17}})
    command(core, {'appearance': {'theme': 'dark', 'workspace_sidebar_collapsed': True}})
    saved = Core(core.root).query('settings')['settings']
    assert saved['appearance'] == {
        **DEFAULT_APPEARANCE, 'accent': 'violet', 'font_size': 17,
        'theme': 'dark', 'workspace_sidebar_collapsed': True,
    }
    assert saved['ai']['enabled'] is False
    before = core.query('state')['revision']
    with pytest.raises(BusinessError):
        command(core, {'appearance': {'accent': 'not-a-palette'}})
    assert core.query('state')['revision'] == before
    assert core.query('settings')['settings']['appearance'] == saved['appearance']
    command(core, {'appearance': {'accent': 'default'}})
    restored = Core(core.root).query('settings')['settings']['appearance']
    assert restored == {**saved['appearance'], 'accent': 'default'}


def test_legacy_appearance_retains_default_profile_palette():
    legacy = normalize_appearance({'theme': 'dark', 'font_size': 16})
    assert legacy['accent'] == 'default'
    assert palette_tokens('light') == LIGHT
    assert palette_tokens('dark') == DARK
    assert classic_palette_tokens('light') == CLASSIC_LIGHT
    assert classic_palette_tokens('dark') == CLASSIC_DARK
    assert normalize_appearance({'font_size': 18}, current={**legacy, 'accent': 'rose'})['accent'] == 'rose'


def contrast(first, second):
    def luminance(value):
        components = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [part / 12.92 if part <= .04045 else ((part + .055) / 1.055) ** 2.4
                  for part in components]
        return sum(part * weight for part, weight in zip(linear, (.2126, .7152, .0722)))
    low, high = sorted((luminance(first), luminance(second)))
    return (high + .05) / (low + .05)


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('get_palette', [palette_tokens, classic_palette_tokens])
def test_accent_text_contrast_and_result_semantics(theme, get_palette):
    baseline = get_palette(theme)
    for accent in ACCENT_LABELS.keys() - {'default'}:
        tokens = get_palette(theme, accent)
        # These pairs are actually used by primary/calendar buttons, selected
        # fields/navigation and selected tabs/text links, including hover.
        for foreground, background in (
            ('primary_text', 'primary'), ('primary_text', 'primary_hover'),
            ('selection_text', 'selection'), ('selection_text', 'surface'),
            ('primary', 'surface'), ('primary', 'window'),
        ):
            assert contrast(tokens[foreground], tokens[background]) >= 4.5, (
                theme, accent, foreground, background)
        for role in ('done', 'incomplete', 'unreported', 'other_reported', 'danger',
                     'danger_bg', 'danger_border', 'warning_bg', 'warning_text',
                     'warning_border', 'chart_on_color', 'chart_on_unknown'):
            assert tokens[role] == baseline[role], (theme, accent, role)


@pytest.fixture
def accent_app():
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    old_style, old_appearance = visual_style(), current_appearance()
    yield app
    set_visual_style(old_style)
    apply_appearance(app, old_appearance)
    app.processEvents()


@pytest.mark.parametrize('profile', ['glass', 'classic'])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_accent_repaints_open_controls_without_losing_edits(accent_app, profile, theme):
    set_visual_style(profile)
    config = {**DEFAULT_APPEARANCE, 'theme': theme, 'accent': 'blue'}
    apply_appearance(accent_app, config)
    host = AppCanvas()
    host.resize(520, 300)
    layout = QVBoxLayout(host)
    field = QLineEdit('Unsaved synthetic text')
    button = QPushButton('Save')
    button.setObjectName('Primary')
    layout.addWidget(field)
    layout.addWidget(button)
    layout.addStretch()
    host.show()
    try:
        accent_app.processEvents()
        before = button.grab().toImage().pixelColor(10, button.height() // 2).name()
        old_backdrop = host.backdrop()
        old_image = old_backdrop.toImage()
        old_key = host._backdrop_key
        font_size = field.font().pixelSize()
        apply_appearance(accent_app, {**config, 'accent': 'violet'})
        accent_app.processEvents()
        after = button.grab().toImage().pixelColor(10, button.height() // 2).name()
        assert after == color('primary') and after != before
        assert field.text() == 'Unsaved synthetic text'
        assert field.font().pixelSize() == font_size
        assert accent_app.palette().color(QPalette.ColorRole.Highlight).name() == color('selection')
        assert host.backdrop().cacheKey() != old_backdrop.cacheKey()
        assert host._backdrop_key != old_key
        if profile == 'glass':
            assert host.backdrop().toImage() != old_image
        assert host.backdrop().cacheKey() == host.backdrop().cacheKey()
        assert color('primary') in stylesheet({**config, 'accent': 'violet'})
    finally:
        host.close()
        host.deleteLater()
        accent_app.processEvents()
