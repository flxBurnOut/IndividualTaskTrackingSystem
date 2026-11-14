"""Portable accent preferences and their live native rendering contracts."""
import copy
import os
import math
import uuid

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from management.appearance import ACCENT_LABELS, DEFAULT_APPEARANCE, normalize_appearance
from management.core import Core
from management.gui_charts import CoverageBar, CoverageRing, VerticalCoverageBar
from management.gui_progress import gradient_colors
from management.gui_materials import AppCanvas
from management.gui_theme import (apply_appearance, color,
                                  current_appearance, stylesheet)
from management.gui_theme_glass import DARK, LIGHT, palette_tokens
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


def test_legacy_appearance_retains_default_palette():
    legacy = normalize_appearance({'theme': 'dark', 'font_size': 16})
    assert legacy['accent'] == 'default'
    assert palette_tokens('light') == LIGHT
    assert palette_tokens('dark') == DARK
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
def test_accent_text_and_chart_contrast_with_distinct_result_shades(theme):
    baseline = palette_tokens(theme)
    for accent in ACCENT_LABELS:
        tokens = palette_tokens(theme, accent)
        # These pairs are actually used by primary/calendar buttons, selected
        # fields/navigation and selected tabs/text links, including hover.
        for foreground, background in (
            ('primary_text', 'primary'), ('primary_text', 'primary_hover'),
            ('selection_text', 'selection'), ('selection_text', 'surface'),
            ('primary', 'surface'), ('primary', 'window'),
            ('warning_text', 'warning_bg'),
        ):
            assert contrast(tokens[foreground], tokens[background]) >= 4.5, (
                theme, accent, foreground, background)
        assert tokens['warning_text'] == tokens['primary']
        assert tokens['warning_bg'] == tokens['selection']
        assert tokens['warning_border'] == tokens['focus_soft']
        assert tokens['done'] == tokens['primary']
        assert tokens['chart_on_color'] == tokens['primary_text']
        chart_roles = ('done', 'incomplete', 'other_reported', 'chart_absent')
        assert len({tokens[role] for role in chart_roles}) == len(chart_roles)
        hue = QColor(tokens['primary']).hsvHueF()
        for role in chart_roles:
            assert contrast(tokens['chart_on_color'], tokens[role]) >= 4.5, (theme, accent, role)
            difference = abs(QColor(tokens[role]).hsvHueF() - hue)
            assert min(difference, 1 - difference) < .025, (theme, accent, role)
        # Unknown coverage remains neutral. Destructive actions and errors
        # retain their separate color even though review selections use accent.
        for role in ('unreported', 'danger', 'danger_bg', 'danger_border', 'chart_on_unknown'):
            assert tokens[role] == baseline[role], (theme, accent, role)


@pytest.fixture
def accent_app():
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    old_appearance = current_appearance()
    yield app
    apply_appearance(app, old_appearance)
    app.processEvents()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_accent_repaints_open_controls_without_losing_edits(accent_app, theme):
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
        assert host.backdrop().toImage() != old_image
        assert host.backdrop().cacheKey() == host.backdrop().cacheKey()
        assert color('primary') in stylesheet({**config, 'accent': 'violet'})
    finally:
        host.close()
        host.deleteLater()
        accent_app.processEvents()


class PaintCounter(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.paints = {}

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Paint:
            self.paints[id(watched)] = self.paints.get(id(watched), 0) + 1
        return False


def painted_pixel(widget, x, y):
    pixmap = widget.grab()
    scale = pixmap.devicePixelRatio()
    return pixmap.toImage().pixelColor(round(x * scale), round(y * scale)).name()


def painted_text_pixels(widget, foreground):
    """Read checked-state text from the paint, not the unqualified QPalette.

    The inset excludes rounded borders/focus outlines. Near-solid glyph pixels
    allow antialiasing while rejecting the contrasting button background.
    """
    pixmap = widget.grab()
    image = pixmap.toImage()
    scale = pixmap.devicePixelRatio()
    expected = QColor(foreground)
    target = (expected.red(), expected.green(), expected.blue())
    count = 0
    for y in range(round(6 * scale), image.height() - round(6 * scale)):
        for x in range(round(12 * scale), image.width() - round(12 * scale)):
            actual = image.pixelColor(x, y)
            if max(abs(channel - wanted) for channel, wanted in
                   zip((actual.red(), actual.green(), actual.blue()), target)) <= 24:
                count += 1
                if count >= 3:
                    return count
    return count


def assert_gradient_pixel(actual, role):
    """A real painted sample must fall inside the selected role's small gradient."""
    pixel = QColor(actual)
    endpoints = gradient_colors(role)
    for channel in ('red', 'green', 'blue'):
        low, high = sorted(getattr(endpoint, channel)() for endpoint in endpoints)
        assert low - 2 <= getattr(pixel, channel)() <= high + 2, (actual, role, low, high)


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_open_warning_review_choices_and_charts_follow_every_accent(accent_app, theme):
    config = {**DEFAULT_APPEARANCE, 'theme': theme, 'accent': 'blue'}
    apply_appearance(accent_app, config)
    host = AppCanvas()
    host.resize(760, 650)
    layout = QVBoxLayout(host)
    labels = []
    for name in ('AttentionTitle', 'CurrentWarningTitle', 'Notice'):
        label = QLabel('Upcoming synthetic item')
        label.setObjectName(name)
        layout.addWidget(label)
        labels.append(label)
    choices = QHBoxLayout()
    layout.addLayout(choices)
    buttons = []
    for result in ('done', 'incomplete', 'attended', 'missed_needs_catchup', 'absent'):
        button = QPushButton(result)
        button.setObjectName('ReviewChoice')
        button.setProperty('result', result)
        button.setCheckable(True)
        button.setChecked(True)
        choices.addWidget(button)
        buttons.append(button)
    summary = {
        'total': 5, 'done': 1, 'incomplete': 1, 'other_reported': 2, 'unreported': 1,
        'breakdown': [
            {'key': result, 'kind': kind, 'result': result, 'count': 1}
            for kind, result in (
                ('task', 'done'), ('course_attendance', 'missed_needs_catchup'),
                ('course_attendance', 'attended'), ('course_attendance', 'absent'),
                ('task', 'unreported'),
            )
        ],
    }
    bar = CoverageBar()
    bar.set_summary(summary)
    layout.addWidget(bar)
    ring = CoverageRing()
    ring.set_summary(summary)
    layout.addWidget(ring)
    column = VerticalCoverageBar({'date': '2026-10-08', 'can_review': True, 'summary': summary})
    layout.addWidget(column)
    charts = (bar, ring, column)
    descriptions = [chart.accessibleName() for chart in charts]
    segments = [copy.deepcopy(chart.segments) for chart in charts]
    counter = PaintCounter(host)
    for chart in charts:
        chart.installEventFilter(counter)
    host.show()
    try:
        accent_app.processEvents()
        for chart in charts:
            chart.reveal.animation.setCurrentTime(chart.reveal.animation.duration())
        # Start with a different accent so every iteration is a live change.
        for accent in ('violet', 'rose', 'teal', 'blue', 'coral', 'default'):
            previous_paints = dict(counter.paints)
            apply_appearance(accent_app, {**config, 'accent': accent})
            accent_app.processEvents()
            accent_app.processEvents()
            tokens = palette_tokens(theme, accent)
            for label in labels:
                assert label.palette().color(QPalette.ColorRole.WindowText).name() == tokens['primary']
            for button in buttons:
                assert button.isChecked(), (theme, accent, button.property('result'))
                assert painted_pixel(button, 10, button.height() / 2) == tokens['primary']
                assert painted_text_pixels(button, tokens['primary_text']) >= 3, (
                    theme, accent, button.property('result'))
            for index, chart in enumerate(charts):
                # A grab itself forces a repaint; check the normal update first.
                assert counter.paints[id(chart)] > previous_paints.get(id(chart), 0)
                assert chart.accessibleName() == descriptions[index]
                assert chart.segments == segments[index]
            roles = ('done', 'incomplete', 'other_reported', 'chart_absent', 'unreported')
            assert [row['color'] for row in bar.segments] == list(roles)
            for index, role in enumerate(roles):
                assert_gradient_pixel(painted_pixel(bar, bar.width() * (index + .5) / 5, bar.height() / 2), role)
                left = painted_pixel(bar, bar.width() * (index + .15) / 5, bar.height() / 2)
                right = painted_pixel(bar, bar.width() * (index + .85) / 5, bar.height() / 2)
                assert_gradient_pixel(left, role)
                assert_gradient_pixel(right, role)
                assert left != right, (theme, accent, role, 'gradient was not painted')
                rect = column.segment_rects()[index][1]
                assert_gradient_pixel(painted_pixel(column, rect.left() + 3, rect.center().y()), role)
                diameter = min(ring.width(), ring.height()) - 16
                radius = (diameter - max(10, diameter * .15)) / 2
                angle = math.radians(90 - 360 * (index + .5) / 5)
                assert_gradient_pixel(painted_pixel(ring, ring.width() / 2 + radius * math.cos(angle),
                                     ring.height() / 2 - radius * math.sin(angle)), role)
        assert ring.summary == summary
        assert column.day['summary'] == summary
    finally:
        host.close()
        host.deleteLater()
        accent_app.processEvents()
