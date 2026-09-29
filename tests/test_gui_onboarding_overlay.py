"""Onboarding never executes a highlighted action or traps a closed window."""
import os
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QFontDatabase, QKeySequence, QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton, QScrollArea, QStyle, QStyleOptionButton, QWidget
from shiboken6 import isValid

from management.gui_onboarding_overlay import TourStep, TutorialOverlay
from management.gui_theme import apply_appearance, current_appearance


@pytest.fixture(scope='session')
def overlay_app():
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    if os.name == 'nt':
        fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
        for name in ('msyh.ttc', 'msyhbd.ttc'):
            path = fonts / name
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
    return app


@pytest.fixture
def scene(overlay_app):
    host = QWidget()
    host.resize(960, 650)
    action = QPushButton('保存实际记录', host)
    action.setGeometry(32, 34, 165, 42)
    editor = QLineEdit(host)
    editor.setGeometry(32, 140, 200, 38)
    host.show()
    host.activateWindow()
    editor.setFocus()
    overlay_app.processEvents()
    overlays = []

    def create(steps=None):
        overlay = TutorialOverlay(host, steps if steps is not None else [
            TourStep(action, '先认识这里', '这里可以保存记录。介绍期间不会执行保存。'),
            TourStep(editor, '填写后再保存', '输入内容后检查，再决定是否保存。'),
        ])
        overlays.append(overlay)
        return overlay

    yield host, action, editor, create
    for overlay in overlays:
        if isValid(overlay):
            overlay.stop()
    host.close()
    host.deleteLater()
    overlay_app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    overlay_app.processEvents()


def test_spotlight_blocks_clicks_and_underlying_shortcuts(scene, overlay_app):
    host, action, editor, create = scene
    saved = []
    action.clicked.connect(lambda: saved.append('saved'))
    shortcut = QShortcut(QKeySequence('F8'), host)
    shortcut.activated.connect(lambda: saved.append('shortcut'))
    QTest.keyClick(editor, Qt.Key.Key_F8)
    assert saved == ['shortcut']
    overlay = create()
    overlay.start()
    overlay_app.processEvents()
    assert overlay.spotlight_rect.contains(action.mapTo(overlay, action.rect().center()))
    assert not overlay.card.geometry().intersects(overlay.spotlight_rect)
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=overlay.spotlight_rect.center())
    # Directly delivered synthetic events must be protected as well as normal
    # hit-tested mouse events sent to the topmost overlay.
    QTest.mouseClick(action, Qt.MouseButton.LeftButton)
    QTest.keyClick(overlay.next_button, Qt.Key.Key_F8)
    QTest.keyClicks(editor, 'unwanted edit')
    assert saved == ['shortcut']
    assert editor.text() == ''
    QTest.mouseClick(overlay.skip_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(action, Qt.MouseButton.LeftButton)
    assert saved == ['shortcut', 'saved']


def test_navigation_is_local_and_focus_returns_after_completion(scene, overlay_app):
    host, action, editor, create = scene
    overlay = create()
    reasons = []
    overlay.finished.connect(reasons.append)
    overlay.start()
    overlay_app.processEvents()
    assert not overlay.back_button.isEnabled()
    for _ in range(13):
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Tab)
        assert overlay.isAncestorOf(QApplication.focusWidget())
    editor.setFocus()
    assert overlay.isAncestorOf(QApplication.focusWidget())
    QTest.mouseClick(overlay.next_button, Qt.MouseButton.LeftButton)
    assert overlay.current_index == 1
    assert overlay.back_button.isEnabled()
    assert overlay.next_button.text() == '完成介绍'
    QTest.mouseClick(overlay.back_button, Qt.MouseButton.LeftButton)
    assert overlay.current_index == 0
    QTest.mouseClick(overlay.next_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(overlay.next_button, Qt.MouseButton.LeftButton)
    assert reasons == ['completed']
    assert not overlay.isVisible()
    assert QApplication.focusWidget() is editor
    overlay.stop()
    assert reasons == ['completed']


@pytest.mark.parametrize('choice,expected', [('skip_button', 'skipped'), ('disable_button', 'disabled'),
                                           ('escape', 'skipped'), ('stop', 'interrupted')])
def test_explicit_finish_reasons_emit_once(scene, choice, expected):
    overlay = scene[3]()
    reasons = []
    overlay.finished.connect(reasons.append)
    overlay.start()
    if choice == 'escape':
        QTest.keyClick(overlay.next_button, Qt.Key.Key_Escape)
    elif choice == 'stop':
        overlay.stop()
    else:
        QTest.mouseClick(getattr(overlay, choice), Qt.MouseButton.LeftButton)
    overlay.stop()
    assert reasons == [expected]
    assert not overlay.isVisible()


def test_geometry_tracks_resize_move_hidden_and_destroyed_target(scene, overlay_app):
    host, action, editor, create = scene
    overlay = create([TourStep(lambda: action, '跟随控件', '改变布局后，高亮仍指向实际位置。')])
    overlay.start()
    original = overlay.spotlight_rect
    action.move(640, 500)
    QTest.qWait(120)
    assert overlay.spotlight_rect != original
    assert overlay.spotlight_rect.contains(action.mapTo(overlay, action.rect().center()))
    host.resize(800, 570)
    overlay_app.processEvents()
    assert overlay.geometry() == host.rect()
    assert overlay.rect().contains(overlay.card.geometry())
    action.hide()
    QTest.qWait(120)
    assert overlay.spotlight_rect.isEmpty()
    assert abs(overlay.card.geometry().center().x() - overlay.rect().center().x()) <= 1
    action.show()
    action.deleteLater()
    overlay_app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QTest.qWait(120)
    assert overlay.spotlight_rect.isEmpty()
    assert overlay.isVisible()


def test_scrolled_offscreen_target_falls_back_to_center(scene, overlay_app):
    host, action, editor, create = scene
    scroller = QScrollArea(host)
    scroller.setGeometry(350, 20, 280, 180)
    content = QWidget()
    content.resize(240, 750)
    target = QPushButton('较下方的控件', content)
    target.setGeometry(20, 520, 160, 40)
    scroller.setWidget(content)
    scroller.show()
    overlay_app.processEvents()
    overlay = create([TourStep(target, '列表入口', '看不到目标时仍能关闭介绍。')])
    overlay.start()
    assert scroller.verticalScrollBar().value() > 0
    assert not overlay.spotlight_rect.isEmpty()
    scroller.verticalScrollBar().setValue(0)
    QTest.qWait(120)
    assert overlay.spotlight_rect.isEmpty()
    scroller.verticalScrollBar().setValue(450)
    QTest.qWait(120)
    assert not overlay.spotlight_rect.isEmpty(), {
        'scroll': scroller.verticalScrollBar().value(), 'maximum': scroller.verticalScrollBar().maximum(),
        'target': target.mapTo(overlay, QPoint()), 'viewport': scroller.viewport().rect(),
        'visible': target.isVisibleTo(host), 'active': overlay._active,
        'fresh': overlay._resolve_target_rect(),
    }
    assert overlay.spotlight_rect.contains(target.mapTo(overlay, target.rect().center()))


@pytest.mark.parametrize('reason', ['completed', 'skipped', 'disabled', 'interrupted'])
def test_each_step_reveals_small_target_and_exit_restores_original_scroll(scene, overlay_app, reason):
    host, action, editor, create = scene
    scroller = QScrollArea(host)
    scroller.setGeometry(350, 20, 280, 210)
    content = QWidget()
    content.resize(240, 1300)
    first = QPushButton('第一处设置', content)
    first.setGeometry(20, 520, 160, 40)
    second = QPushButton('第二处设置', content)
    second.setGeometry(20, 1000, 160, 40)
    scroller.setWidget(content)
    scroller.show()
    overlay_app.processEvents()
    bar = scroller.verticalScrollBar()
    bar.setValue(90)
    overlay = create([TourStep(first, '第一步', '找到下方设置。'), TourStep(second, '第二步', '继续了解其他设置。')])
    overlay.start()
    first_scroll = bar.value()
    assert first_scroll > 90
    assert overlay.spotlight_rect.contains(first.mapTo(overlay, first.rect().center()))
    QTest.mouseClick(overlay.next_button, Qt.MouseButton.LeftButton)
    assert bar.value() > first_scroll
    assert overlay.spotlight_rect.contains(second.mapTo(overlay, second.rect().center()))
    if reason == 'completed':
        QTest.mouseClick(overlay.next_button, Qt.MouseButton.LeftButton)
    elif reason == 'skipped':
        QTest.mouseClick(overlay.skip_button, Qt.MouseButton.LeftButton)
    elif reason == 'disabled':
        QTest.mouseClick(overlay.disable_button, Qt.MouseButton.LeftButton)
    else:
        overlay.stop()
    assert bar.value() == 90


def test_large_or_hidden_target_does_not_scroll_and_deleted_scrollbar_is_safe(scene, overlay_app):
    host, action, editor, create = scene
    scroller = QScrollArea(host)
    scroller.setGeometry(350, 20, 280, 180)
    content = QWidget()
    content.resize(240, 950)
    large = QWidget(content)
    large.setGeometry(10, 300, 180, 280)
    hidden = QPushButton('隐藏控件', content)
    hidden.setGeometry(20, 620, 160, 40)
    small = QPushButton('可见控件', content)
    small.setGeometry(20, 740, 160, 40)
    scroller.setWidget(content)
    scroller.show()
    hidden.hide()
    overlay_app.processEvents()
    overlay = create([TourStep(large, '大型面板', '不改变此处位置。'),
                      TourStep(hidden, '隐藏控件', '不展开隐藏功能。'),
                      TourStep(small, '小控件', '只滚动到明确目标。')])
    overlay.start()
    assert scroller.verticalScrollBar().value() == 0
    QTest.mouseClick(overlay.next_button, Qt.MouseButton.LeftButton)
    assert scroller.verticalScrollBar().value() == 0
    QTest.mouseClick(overlay.next_button, Qt.MouseButton.LeftButton)
    assert scroller.verticalScrollBar().value() > 0
    scroller.deleteLater()
    overlay_app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    overlay.stop()


def test_host_close_interrupts_without_lingering_input_filter(scene, overlay_app):
    host, action, editor, create = scene
    overlay = create()
    reasons = []
    overlay.finished.connect(reasons.append)
    overlay.start()
    host.close()
    assert reasons == ['interrupted']
    assert not overlay._geometry_timer.isActive()
    host.show()
    editor.setFocus()
    QTest.keyClicks(editor, 'normal')
    assert editor.text() == 'normal'


def test_large_text_and_missing_target_keep_exit_buttons_visible(scene, overlay_app, monkeypatch):
    host, action, editor, create = scene
    monkeypatch.setattr(overlay_app, '_management_appearance', {
        'theme': 'dark', 'font_size': 20, 'font_family': '', 'workspace_sidebar_collapsed': False,
    }, raising=False)
    host.resize(660, 540)
    overlay = create([TourStep(None, '第一次进入这个面板', '这是较长的介绍。' * 60)])
    overlay.start()
    overlay_app.processEvents()
    assert overlay.rect().contains(overlay.card.geometry())
    for button in (overlay.skip_button, overlay.disable_button, overlay.next_button):
        assert button.isVisible()
        assert overlay.card.rect().contains(button.geometry())
    assert overlay.body_scroll.verticalScrollBar().maximum() > 0
    QTest.mouseClick(overlay.skip_button, Qt.MouseButton.LeftButton)
    assert not overlay.isVisible()


def test_missing_target_and_empty_tour_are_safe(scene):
    host, action, editor, create = scene
    overlay = create([])
    reasons = []
    overlay.finished.connect(reasons.append)
    overlay.start()
    assert reasons == ['completed']
    assert not overlay.isVisible()
    overlay = create([TourStep(lambda: None, '稍后再看', '<b>说明按纯文本显示</b>')])
    overlay.start()
    assert overlay.spotlight_rect.isEmpty()
    assert overlay.body_label.textFormat() == Qt.TextFormat.PlainText


@pytest.mark.parametrize('theme,font_size', [('light', 13), ('dark', 20)])
def test_real_application_font_keeps_button_text_and_padding_inside_card(scene, overlay_app, theme, font_size):
    host, action, editor, create = scene
    previous = current_appearance()
    try:
        apply_appearance(overlay_app, {**previous, 'theme': theme, 'font_size': font_size})
        overlay = create([TourStep(action, '欢迎使用个人事务管理',
                                  '总览汇总今天、本周安排和当前需要留意的事项。也可以随时跳过。')])
        overlay.start()
        overlay_app.processEvents()
        for button in (overlay.back_button, overlay.next_button, overlay.skip_button, overlay.disable_button):
            assert button.height() >= button.fontMetrics().height() + 22
            assert overlay.card.rect().contains(button.geometry())
            option = QStyleOptionButton()
            button.initStyleOption(option)
            content = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
            text_rect = button.fontMetrics().boundingRect(button.text())
            assert content.height() >= text_rect.height(), {
                'text': button.text(), 'content': content, 'text_bounds': text_rect,
                'button': button.rect(), 'font_height': button.fontMetrics().height(),
            }
    finally:
        apply_appearance(overlay_app, previous)
