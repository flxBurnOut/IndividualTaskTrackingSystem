"""Native input, layout, lifetime and rendering contracts for optional materials."""
import os
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QAbstractAnimation, QEvent, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton, QScrollArea, QStackedWidget, QTabWidget, QVBoxLayout, QWidget

from management import gui_materials
from management.appearance import DEFAULT_APPEARANCE
from management.gui_materials import AnimatedTabWidget, AppCanvas, GlassPanel, NavigationButton, PageTransition, TabTransition
from management.gui_theme import apply_appearance, color, current_appearance
from management.gui_visual_profile import set_visual_style, visual_style


def wait_for_frame(predicate, timeout=1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        QTest.qWait(5)
    raise AssertionError('The material did not render the expected animation frame')


@pytest.fixture(scope='session')
def materials_app():
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    return app


@pytest.fixture
def material_scene(materials_app, monkeypatch):
    old_style, old_appearance = visual_style(), current_appearance()
    set_visual_style('glass')
    apply_appearance(materials_app, DEFAULT_APPEARANCE)
    monkeypatch.setenv('PERSONAL_MANAGEMENT_REDUCE_MOTION', '1')
    host = AppCanvas()
    host.setObjectName('AppCanvas')
    host.resize(800, 560)
    panel = GlassPanel(host, role='sidebar')
    panel.setObjectName('GlassSidebar')
    panel.setGeometry(12, 12, 200, 536)
    nav = NavigationButton('Tasks', panel)
    nav.setCheckable(True)
    nav.setGeometry(12, 25, 176, 48)
    stack = QStackedWidget(host)
    stack.setGeometry(226, 12, 554, 536)
    pages, editors, actions = [], [], []
    for index in range(2):
        page = QWidget()
        layout = QVBoxLayout(page)
        editor = QLineEdit('Unsaved draft ' + str(index))
        action = QPushButton('Save ' + str(index))
        layout.addWidget(editor)
        layout.addWidget(action)
        layout.addStretch()
        stack.addWidget(page)
        pages.append(page)
        editors.append(editor)
        actions.append(action)
    transition = PageTransition(stack)
    host.show()
    host.activateWindow()
    materials_app.processEvents()
    yield host, panel, nav, stack, transition, pages, editors, actions
    host.close()
    host.deleteLater()
    materials_app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    materials_app.processEvents()
    set_visual_style(old_style)
    apply_appearance(materials_app, old_appearance)


def test_reduce_motion_preserves_immediate_native_actions(material_scene, materials_app):
    host, panel, nav, stack, transition, pages, editors, actions = material_scene
    assert gui_materials.reduce_motion()
    nav.setFocus()
    QTest.keyClick(nav, Qt.Key.Key_Space)
    assert nav.isChecked()
    assert nav.animation.state() == QAbstractAnimation.State.Stopped
    stack.setCurrentIndex(1)
    assert not transition._veil.isVisible()
    assert transition.animation.state() == QAbstractAnimation.State.Stopped
    editors[1].setFocus()
    QTest.keyClicks(editors[1], ' retained')
    assert editors[1].text().endswith(' retained')


def test_real_page_fade_is_finite_and_does_not_capture_input(material_scene, materials_app, monkeypatch):
    host, panel, nav, stack, transition, pages, editors, actions = material_scene
    monkeypatch.setattr(gui_materials, 'reduce_motion', lambda: False)
    stack.setCurrentIndex(1)
    assert transition._veil.isVisible()
    assert transition._settle_timer.isActive()
    assert transition.is_active()
    wait_for_frame(lambda: transition.animation.state() == QAbstractAnimation.State.Running
                   and 0 < transition._veil.opacity < 1.0)
    assert transition.animation.state() == QAbstractAnimation.State.Running
    assert 0 < transition._veil.opacity < 1.0
    # Normal hit testing skips the veil and reaches the real underlying button.
    point = actions[1].mapTo(host, actions[1].rect().center())
    assert host.childAt(point) is actions[1]
    editors[1].setFocus()
    QTest.keyClicks(editors[1], ' live')
    assert editors[1].text().endswith(' live')
    assert pages[1].graphicsEffect() is None
    stack.setCurrentIndex(0)
    stack.setCurrentIndex(1)
    QTest.qWait(380)
    assert transition.animation.state() == QAbstractAnimation.State.Stopped
    assert not transition._veil.isVisible()
    assert transition._veil.snapshot.isNull()
    assert not transition.is_active()
    assert editors[0].text() == 'Unsaved draft 0'
    assert editors[1].text().endswith(' live')


@pytest.mark.parametrize('change', ['resize', 'hide', 'theme'])
def test_live_transition_cancels_on_geometry_or_presentation_change(material_scene, materials_app, monkeypatch, change):
    host, panel, nav, stack, transition, pages, editors, actions = material_scene
    monkeypatch.setattr(gui_materials, 'reduce_motion', lambda: False)
    stack.setCurrentIndex(1)
    assert transition._veil.isVisible()
    if change == 'resize':
        host.resize(host.width() - 20, host.height())
    elif change == 'hide':
        stack.hide()
    elif change == 'theme':
        apply_appearance(materials_app, {'theme': 'dark'})
    materials_app.processEvents()
    assert transition.animation.state() == QAbstractAnimation.State.Stopped
    assert not transition._veil.isVisible()
    assert all(editor.text() == 'Unsaved draft ' + str(index) for index, editor in enumerate(editors))


def test_navigation_renders_icon_and_keeps_keyboard_focus_and_checkable_state(material_scene, materials_app, monkeypatch):
    host, panel, nav, stack, transition, pages, editors, actions = material_scene
    monkeypatch.setattr(gui_materials, 'reduce_motion', lambda: False)
    icon_pixels = QPixmap(18, 18)
    icon_pixels.fill(QColor('#ff00ff'))
    nav.setIcon(QIcon(icon_pixels))
    nav.setIconSize(QSize(18, 18))
    editors[0].setFocus()
    nav.setFocus(Qt.FocusReason.MouseFocusReason)
    assert not nav._keyboard_focus
    QTest.keyClick(nav, Qt.Key.Key_Space)
    assert nav.hasFocus() and nav.isChecked()
    assert nav._keyboard_focus
    assert nav.animation.state() == QAbstractAnimation.State.Running
    QTest.qWait(330)
    assert nav.animation.state() == QAbstractAnimation.State.Stopped
    image = nav.grab().toImage()
    icon_columns = [x for y in range(image.height()) for x in range(image.width())
                    if image.pixelColor(x, y).name() == '#ff00ff']
    assert icon_columns and min(icon_columns) >= round(12 * nav.devicePixelRatioF())
    QTest.mouseClick(nav, Qt.MouseButton.LeftButton)
    assert not nav._keyboard_focus
    nav.setChecked(True)
    nav.setEnabled(False)
    QTest.keyClick(nav, Qt.Key.Key_Space)
    assert nav.isChecked()


def test_backdrop_cache_tracks_real_dpr_resize_theme_and_accent(material_scene, materials_app):
    host, panel, nav, stack, transition, pages, editors, actions = material_scene
    first = host.backdrop()
    assert first.cacheKey() == host.backdrop().cacheKey()
    ratio = host.devicePixelRatioF()
    assert first.devicePixelRatioF() == ratio
    assert first.width() == round(host.width() * ratio)
    assert first.height() == round(host.height() * ratio)
    host.resize(820, 580)
    larger = host.backdrop()
    assert larger.cacheKey() != first.cacheKey()
    assert larger.width() == round(820 * ratio)
    apply_appearance(materials_app, {'theme': 'dark'})
    dark = host.backdrop()
    assert dark.cacheKey() != larger.cacheKey()
    image = dark.toImage()
    # The dark canvas stays graphite; it must not return to a saturated navy.
    for x, y in ((.1, .2), (.5, .5), (.9, .8)):
        sample = image.pixelColor(round(image.width() * x), round(image.height() * y))
        assert max(sample.red(), sample.green(), sample.blue()) - min(sample.red(), sample.green(), sample.blue()) < 16
    apply_appearance(materials_app, {**current_appearance(), 'accent': 'violet'})
    accented = host.backdrop()
    assert accented.cacheKey() != dark.cacheKey()
    assert accented.toImage() != image
    assert editors[0].text() == 'Unsaved draft 0'


def test_sidebar_has_transparent_rounded_corners_and_interactive_children(material_scene, materials_app):
    host, panel, nav, stack, transition, pages, editors, actions = material_scene
    image = host.grab().toImage()
    canvas = host.backdrop().toImage()
    ratio = panel.devicePixelRatioF()
    # The outer corner shows the canvas; the frosted interior has its own fill.
    corner = image.pixelColor(round((panel.x() + 2) * ratio), round((panel.y() + 2) * ratio))
    source = canvas.pixelColor(round((panel.x() + 2) * ratio), round((panel.y() + 2) * ratio))
    assert abs(corner.red() - source.red()) <= 2
    assert abs(corner.green() - source.green()) <= 2
    assert abs(corner.blue() - source.blue()) <= 2
    center = image.pixelColor(round((panel.x() + 100) * ratio), round((panel.y() + 350) * ratio))
    assert center != canvas.pixelColor(round((panel.x() + 100) * ratio), round((panel.y() + 350) * ratio))
    assert host.childAt(nav.mapTo(host, nav.rect().center())) is nav


def test_page_layout_resize_does_not_erase_requested_transition(material_scene, materials_app, monkeypatch):
    host, panel, nav, stack, transition, pages, editors, actions = material_scene
    monkeypatch.setattr(gui_materials, 'reduce_motion', lambda: False)
    stack.setCurrentIndex(1)
    # A title/notice/content reflow after currentChanged is not a window resize.
    stack.resize(stack.width(), stack.height() - 22)
    materials_app.processEvents()
    assert transition._veil.isVisible()
    wait_for_frame(lambda: transition.animation.state() == QAbstractAnimation.State.Running
                   and .5 < transition._veil.opacity < 1.0)
    assert transition.animation.state() == QAbstractAnimation.State.Running
    assert .5 < transition._veil.opacity < 1
    stack.resize(stack.width(), stack.height() - 9)
    materials_app.processEvents()
    assert transition._veil.isVisible()
    assert transition._veil.geometry() == stack.contentsRect()
    assert transition._pending_page is None
    QTest.qWait(320)
    assert not transition._veil.isVisible()
    assert not transition._settle_timer.isActive()
    assert not transition.is_active()


def test_reduced_motion_or_hide_cancels_pending_layout_start(material_scene, materials_app, monkeypatch):
    host, panel, nav, stack, transition, pages, editors, actions = material_scene
    motion = {'reduced': False}
    monkeypatch.setattr(gui_materials, 'reduce_motion', lambda: motion['reduced'])
    stack.setCurrentIndex(1)
    assert transition._settle_timer.isActive()
    motion['reduced'] = True
    QTest.qWait(60)
    assert not transition._veil.isVisible()
    assert transition.animation.state() == QAbstractAnimation.State.Stopped
    motion['reduced'] = False
    stack.setCurrentIndex(0)
    stack.hide()
    QTest.qWait(60)
    assert not transition._veil.isVisible()
    assert not transition._settle_timer.isActive()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_sidebar_material_transmits_visible_backdrop_variation(material_scene, materials_app, theme):
    host, panel, nav, stack, transition, pages, editors, actions = material_scene
    apply_appearance(materials_app, {'theme': theme})
    image = host.grab().toImage()
    ratio = host.devicePixelRatioF()
    transmitted = [image.pixelColor(round((panel.x() + 95) * ratio), round((panel.y() + y) * ratio))
                   for y in (135, 290, 445)]
    # A solid card would flatten all three empty areas to the same surface.
    luminance = [(.2126 * c.red() + .7152 * c.green() + .0722 * c.blue()) for c in transmitted]
    assert max(luminance) - min(luminance) > 5


def test_tabs_animate_only_content_and_keep_keyboard_drafts_scroll_and_fast_selection(materials_app, monkeypatch):
    old_style, old_appearance = visual_style(), current_appearance()
    set_visual_style('glass')
    apply_appearance(materials_app, DEFAULT_APPEARANCE)
    monkeypatch.setattr(gui_materials, 'reduce_motion', lambda: False)
    host = AppCanvas()
    host.resize(680, 430)
    layout = QVBoxLayout(host)
    tabs = AnimatedTabWidget()
    layout.addWidget(tabs)
    editors, scrolls = [], []
    for index in range(3):
        body = QWidget()
        page_layout = QVBoxLayout(body)
        editor = QLineEdit('Draft ' + str(index))
        page_layout.addWidget(editor)
        page_layout.addSpacing(900)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        tabs.addTab(scroll, 'Page ' + str(index))
        editors.append(editor)
        scrolls.append(scroll)
    transition = TabTransition(tabs)
    changes = []
    tabs.currentChanged.connect(changes.append)
    try:
        host.show()
        materials_app.processEvents()
        scrolls[0].verticalScrollBar().setValue(120)
        first_scroll = scrolls[0].verticalScrollBar().value()
        assert first_scroll == 120
        tabs.tabBar().setFocus()
        QTest.keyClick(tabs.tabBar(), Qt.Key.Key_Right)
        assert tabs.currentIndex() == 1 and changes == [1]
        assert transition.is_active()
        assert tabs.tabBar().animation.state() == QAbstractAnimation.State.Running
        content_stack = transition._stack()
        assert transition._veil.parentWidget() is content_stack
        assert host.childAt(tabs.tabBar().mapTo(host, tabs.tabBar().tabRect(1).center())) is tabs.tabBar()
        point = editors[1].mapTo(host, editors[1].rect().center())
        assert host.childAt(point) is editors[1]
        editors[1].setFocus()
        QTest.keyClicks(editors[1], ' retained')
        assert editors[1].text() == 'Draft 1 retained'
        wait_for_frame(lambda: .2 < transition._veil.opacity < .8)
        bar = tabs.tabBar()
        assert bar.animation.state() == QAbstractAnimation.State.Running
        elapsed = bar.animation.currentTime()
        bar.resize(bar.width() + 2, bar.height())
        assert bar.animation.state() == QAbstractAnimation.State.Running
        assert bar.animation.currentTime() >= elapsed
        assert bar._before.size() == bar._after.size()
        for index in (2, 0, 1, 0):
            tabs.setCurrentIndex(index)
        wait_for_frame(lambda: not transition.is_active())
        assert changes == [1, 2, 0, 1, 0]
        assert scrolls[0].verticalScrollBar().value() == first_scroll
        assert editors[1].text() == 'Draft 1 retained'
        assert tabs.currentWidget() is scrolls[0]
        assert all(scroll.widget().graphicsEffect() is None for scroll in scrolls)
        assert transition._veil.snapshot.isNull()
        wait_for_frame(lambda: tabs.tabBar().animation.state() == QAbstractAnimation.State.Stopped)
        assert tabs.tabBar()._before.isNull() and tabs.tabBar()._after.isNull()
        tabs.setCurrentIndex(1)
        assert tabs.tabBar().animation.state() == QAbstractAnimation.State.Running
        host.resize(640, 420)
        materials_app.processEvents()
        assert tabs.tabBar().animation.state() == QAbstractAnimation.State.Stopped
        tabs.setCurrentIndex(2)
        assert tabs.tabBar().animation.state() == QAbstractAnimation.State.Running
        apply_appearance(materials_app, {'theme': 'dark'})
        assert tabs.tabBar().animation.state() == QAbstractAnimation.State.Stopped
        tabs.setCurrentIndex(0)
        tabs.hide()
        assert tabs.tabBar().animation.state() == QAbstractAnimation.State.Stopped
        tabs.show()
        apply_appearance(materials_app, DEFAULT_APPEARANCE)
        monkeypatch.setattr(gui_materials, 'reduce_motion', lambda: True)
        tabs.setCurrentIndex(2 if tabs.currentIndex() == 0 else 0)
        assert not transition.is_active()
        assert tabs.tabBar().animation.state() == QAbstractAnimation.State.Stopped
    finally:
        host.close()
        host.deleteLater()
        materials_app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        set_visual_style(old_style)
        apply_appearance(materials_app, old_appearance)
