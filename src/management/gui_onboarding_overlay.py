"""Window-local, read-only spotlights for the application's guided tours."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import weakref

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from .gui_theme import bind_theme, color, current_appearance


@dataclass(frozen=True)
class TourStep:
    target: QWidget | Callable[[], QWidget | None] | None
    title: str
    body: str


class TutorialOverlay(QWidget):
    """Dim one host without letting a tutorial click perform a business action.

    The host's normal close/minimize lifecycle remains available. ``stop()``
    reports ``interrupted``; only explicit completion/skip/disable choices should
    be persisted by the owner. Each start emits at most one completion reason.
    """

    finished = Signal(str)

    def __init__(self, host: QWidget, steps: list[TourStep]):
        super().__init__(host)
        self.host = host
        self.steps = list(steps)
        self.current_index = 0
        self.spotlight_rect = QRect()
        self._active = False
        self._previous_focus = None
        self._focus_guard = False
        self._last_layout = None
        self._geometry_pending = False
        self._reveal_pending = False
        self._last_target = None
        self._scroll_positions = {}
        self.setObjectName('TutorialOverlay')
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName('使用指南')

        self.card = QFrame(self)
        self.card.setObjectName('TutorialCard')
        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(13)
        self.counter_label = QLabel(self.card)
        self.counter_label.setObjectName('TutorialCounter')
        layout.addWidget(self.counter_label)
        self.title_label = QLabel(self.card)
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setWordWrap(True)
        self.title_label.setObjectName('TutorialTitle')
        layout.addWidget(self.title_label)

        self.body_scroll = QScrollArea(self.card)
        self.body_scroll.setObjectName('TutorialBodyScroll')
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.body_scroll.setMinimumHeight(0)
        self.body_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)
        self.body_label = QLabel()
        self.body_label.setObjectName('TutorialBody')
        self.body_label.setTextFormat(Qt.TextFormat.PlainText)
        self.body_label.setWordWrap(True)
        self.body_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.body_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.body_scroll.setWidget(self.body_label)
        layout.addWidget(self.body_scroll, 1)

        self.buttons = QGridLayout()
        self.buttons.setHorizontalSpacing(10)
        self.buttons.setVerticalSpacing(7)
        self.back_button = QPushButton('上一步', self.card)
        self.next_button = QPushButton('下一步', self.card)
        self.next_button.setObjectName('TutorialNext')
        self.skip_button = QPushButton('跳过本页', self.card)
        self.disable_button = QPushButton('关闭自动介绍', self.card)
        self.skip_button.setObjectName('TutorialQuiet')
        self.disable_button.setObjectName('TutorialQuiet')
        self.back_button.clicked.connect(self._back)
        self.next_button.clicked.connect(self._next)
        self.skip_button.clicked.connect(self._skip)
        self.disable_button.clicked.connect(self._disable)
        layout.addLayout(self.buttons)
        self._button_columns = None
        self._place_buttons(2)
        for button in self._focus_buttons():
            button.setAutoDefault(False)
        self.setTabOrder(self.back_button, self.next_button)
        self.setTabOrder(self.next_button, self.skip_button)
        self.setTabOrder(self.skip_button, self.disable_button)

        # Geometry can change through scrolling, asynchronous content or splitter
        # movement without the host receiving a resize event.
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setInterval(100)
        self._geometry_timer.timeout.connect(self._refresh_geometry)
        bind_theme(self, self._apply_theme)
        self.hide()

    def _apply_theme(self):
        scale = current_appearance()['font_size'] / 13
        self.setStyleSheet('''
            QFrame#TutorialCard { background: %(surface)s; border: 1px solid %(border)s;
                border-radius: 16px; }
            QLabel#TutorialCounter { color: %(primary)s; font-size: %(small)dpx; font-weight: 600; }
            QLabel#TutorialTitle { color: %(text)s; font-size: %(title)dpx; font-weight: 700; }
            QLabel#TutorialBody { color: %(text)s; font-size: %(body)dpx; background: transparent; }
            QScrollArea#TutorialBodyScroll, QScrollArea#TutorialBodyScroll > QWidget > QWidget {
                background: transparent; border: 0; }
            QFrame#TutorialCard QPushButton { color: %(text)s; background: %(surface)s;
                border: 1px solid %(border)s; border-radius: 7px; padding: 9px 12px; }
            QFrame#TutorialCard QPushButton:hover { background: %(selection)s; }
            QFrame#TutorialCard QPushButton:focus { border: 2px solid %(focus)s; }
            QFrame#TutorialCard QPushButton:disabled { color: %(muted)s; background: %(surface_alt)s; }
            QFrame#TutorialCard QPushButton#TutorialNext { color: %(primary_text)s;
                background: %(primary)s; border-color: %(primary)s; font-weight: 600; }
            QFrame#TutorialCard QPushButton#TutorialNext:hover { background: %(primary_hover)s; }
            QFrame#TutorialCard QPushButton#TutorialQuiet { color: %(muted)s;
                border: 0; background: transparent; font-size: %(small)dpx; }
            QFrame#TutorialCard QPushButton#TutorialQuiet:focus { border: 2px solid %(focus)s; }
            QFrame#TutorialCard QPushButton#TutorialQuiet:hover { color: %(text)s; background: %(selection)s; }
        ''' % {**{key: color(key) for key in ('surface', 'surface_alt', 'text', 'muted', 'border',
                                              'primary', 'primary_text', 'primary_hover', 'focus', 'selection')},
               'small': round(12 * scale), 'body': round(14 * scale), 'title': round(20 * scale)})
        self._last_layout = None
        if self._active:
            self._refresh_geometry()
            self._schedule_geometry(reveal=True)

    def start(self):
        if self._active:
            return
        if not self.steps:
            self.finished.emit('completed')
            return
        app = QApplication.instance()
        focus = app.focusWidget()
        self._previous_focus = weakref.ref(focus) if focus is not None else None
        self.current_index = 0
        self._active = True
        self.setGeometry(self.host.rect())
        self.show()
        self.raise_()
        app.installEventFilter(self)
        app.focusChanged.connect(self._focus_changed)
        self._show_step()
        self._geometry_timer.start()
        self.next_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def stop(self):
        self._finish('interrupted')

    def _finish(self, reason):
        if not self._active:
            return
        self._active = False
        self._geometry_timer.stop()
        app = QApplication.instance()
        app.removeEventFilter(self)
        app.focusChanged.disconnect(self._focus_changed)
        self.hide()
        previous = self._previous_focus() if self._previous_focus else None
        self._previous_focus = None
        if previous is not None and isValid(previous) and previous.isVisible() and previous.isEnabled():
            previous.setFocus(Qt.FocusReason.OtherFocusReason)
        self._restore_scroll_positions()
        self.finished.emit(reason)

    def _show_step(self):
        step = self.steps[self.current_index]
        self.counter_label.setText(f'使用指南  ·  {self.current_index + 1} / {len(self.steps)}')
        self.title_label.setText(step.title)
        self.body_label.setText(step.body)
        self.back_button.setEnabled(self.current_index > 0)
        self.next_button.setText('完成介绍' if self.current_index == len(self.steps) - 1 else '下一步')
        self._reveal_target()
        self._last_layout = None
        self._refresh_geometry()
        self.body_scroll.verticalScrollBar().setValue(0)
        self.next_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _next(self):
        if self.current_index + 1 == len(self.steps):
            self._finish('completed')
        else:
            self.current_index += 1
            self._show_step()

    def _back(self):
        if self.current_index > 0:
            self.current_index -= 1
            self._show_step()

    def _skip(self):
        self._finish('skipped')

    def _disable(self):
        self._finish('disabled')

    def _resolve_target(self):
        target = self.steps[self.current_index].target
        try:
            target = target() if callable(target) else target
            if (not isinstance(target, QWidget) or not isValid(target)
                    or target.window() is not self.host.window()
                    or not target.isVisibleTo(self.host)
                    or target is not self.host and not self.host.isAncestorOf(target)):
                return None
            return target
        except (RuntimeError, ReferenceError):
            return None

    def _reveal_target(self):
        """Reveal a small control in existing scroll areas without navigating.

        Scrollbar objects are retained only for this tour and checked for C++
        validity before restoration. Keeping their wrappers avoids weak refs
        disappearing while Qt still owns the live scrollbar.
        """
        target = self._resolve_target()
        if target is None:
            return
        ancestor = target.parentWidget()
        while ancestor is not None and ancestor is not self.host:
            if isinstance(ancestor, QScrollArea):
                viewport = ancestor.viewport()
                content = ancestor.widget()
                fits = target.width() <= viewport.width() and target.height() <= viewport.height()
                belongs = content is not None and (content is target or content.isAncestorOf(target))
                visible = QRect(viewport.mapFromGlobal(target.mapToGlobal(QPoint())), target.size())
                if fits and belongs and not viewport.rect().contains(visible):
                    bars = (ancestor.horizontalScrollBar(), ancestor.verticalScrollBar())
                    before = [(bar, bar.value()) for bar in bars]
                    ancestor.ensureWidgetVisible(target, 12, 12)
                    for bar, value in before:
                        if bar.value() != value and bar not in self._scroll_positions:
                            self._scroll_positions[bar] = value
            ancestor = ancestor.parentWidget()

    def _restore_scroll_positions(self):
        positions, self._scroll_positions = self._scroll_positions, {}
        for bar, value in reversed(tuple(positions.items())):
            if isValid(bar):
                bar.setValue(value)

    def _resolve_target_rect(self):
        target = self._resolve_target()
        if target is None:
            return QRect()
        try:
            # The overlay is a sibling, not an ancestor of the target. Mapping
            # directly to that sibling can traverse an owned native dialog's
            # QObject parent and add its owner's window offset. Screen-space
            # mapping respects native window boundaries and Qt's DPI handling.
            rect = QRect(self.mapFromGlobal(target.mapToGlobal(QPoint())), target.size())
            ancestor = target.parentWidget()
            while ancestor is not None and ancestor is not self.host:
                clip = QRect(self.mapFromGlobal(ancestor.mapToGlobal(QPoint())), ancestor.size())
                rect = rect.intersected(clip)
                ancestor = ancestor.parentWidget()
            rect = rect.intersected(self.rect())
            if rect.isEmpty():
                return QRect()
            return rect.adjusted(-5, -5, 5, 5).intersected(self.rect().adjusted(3, 3, -3, -3))
        except (RuntimeError, ReferenceError):
            # A dynamic target may be deleted between two timer ticks.
            return QRect()

    def _refresh_geometry(self):
        if not self._active:
            return
        if self.geometry() != self.host.rect():
            self.setGeometry(self.host.rect())
        widget = self._resolve_target()
        previous = self._last_target() if self._last_target is not None else None
        if widget is not previous:
            self._last_target = weakref.ref(widget) if widget is not None else None
            self._reveal_target()
        target = self._resolve_target_rect()
        signature = (self.size().width(), self.size().height(), target.getRect(), self.current_index)
        if signature == self._last_layout:
            return
        self._last_layout = signature
        self.spotlight_rect = target
        self._layout_card(target)
        if self._avoid_covering_target(target):
            target = self._resolve_target_rect()
            self.spotlight_rect = target
            self._layout_card(target)
        self.raise_()
        self.update()

    def _avoid_covering_target(self, target_rect):
        """Use available scroll space when a large caption would hide its control."""
        if target_rect.isEmpty() or not self.card.geometry().intersects(target_rect):
            return False
        target = self._resolve_target()
        if target is None:
            return False
        def overlap(rect):
            covered = QRect(self._card_position(rect), self.card.size()).intersected(rect)
            return 0 if covered.isEmpty() else covered.width() * covered.height()
        best_score = overlap(target_rect)
        best = None
        ancestor = target.parentWidget()
        while ancestor is not None and ancestor is not self.host:
            if isinstance(ancestor, QScrollArea):
                viewport = ancestor.viewport()
                content = ancestor.widget()
                if (content is not None and (content is target or content.isAncestorOf(target))
                        and target.height() <= viewport.height()):
                    bar = ancestor.verticalScrollBar()
                    viewport_rect = QRect(self.mapFromGlobal(viewport.mapToGlobal(QPoint())), viewport.size())
                    current = bar.value()
                    # Move the highlighted control toward either edge, then put
                    # the caption in the freed area. Compare before scrolling.
                    for shift in (viewport_rect.top() + 12 - target_rect.top(),
                                  viewport_rect.bottom() - 12 - target_rect.bottom()):
                        value = max(bar.minimum(), min(bar.maximum(), current - shift))
                        predicted = target_rect.translated(0, current - value)
                        if not viewport_rect.adjusted(-5, -5, 5, 5).contains(predicted):
                            continue
                        score = overlap(predicted)
                        if score < best_score:
                            best_score, best = score, (bar, value)
            ancestor = ancestor.parentWidget()
        if best is None:
            return False
        bar, value = best
        if bar not in self._scroll_positions:
            self._scroll_positions[bar] = bar.value()
        bar.setValue(value)
        return True

    def _schedule_geometry(self, *, reveal=False):
        self._reveal_pending = self._reveal_pending or reveal
        if not self._geometry_pending:
            self._geometry_pending = True
            QTimer.singleShot(0, self._after_layout)

    def _after_layout(self):
        self._geometry_pending = False
        reveal, self._reveal_pending = self._reveal_pending, False
        if self._active and reveal:
            self._reveal_target()
        self._refresh_geometry()

    def _place_buttons(self, columns):
        if columns == self._button_columns:
            return
        self._button_columns = columns
        buttons = (self.back_button, self.next_button, self.skip_button, self.disable_button)
        for button in buttons:
            self.buttons.removeWidget(button)
        for i, button in enumerate(buttons):
            self.buttons.addWidget(button, i // columns, i % columns)

    def _layout_card(self, target, *, available_width=None):
        bounds = self.rect().adjusted(16, 16, -16, -16)
        if bounds.width() < 1 or bounds.height() < 1:
            return
        scale = current_appearance()['font_size'] / 13
        width = min(round(450 * max(1, scale * .9)), bounds.width())
        if available_width is not None:
            width = min(width, available_width)
        inner_width = max(1, width - 48)
        self.card.ensurePolished()
        for widget in (self.counter_label, self.title_label, self.body_label,
                       self.back_button, self.next_button, self.skip_button, self.disable_button):
            widget.ensurePolished()
        # Native font metrics and stylesheet padding must be known before the
        # fixed card height is computed. Otherwise the scroll area's minimum
        # size can squeeze the buttons and clip their text at the default font.
        for button in (self.back_button, self.next_button, self.skip_button, self.disable_button):
            button.setMinimumHeight(max(button.sizeHint().height(), button.fontMetrics().height() + 22))
        required_width = max(self.back_button.sizeHint().width() + self.next_button.sizeHint().width(),
                             self.skip_button.sizeHint().width() + self.disable_button.sizeHint().width()) + 10
        self._place_buttons(2 if required_width <= inner_width else 1)
        self.card.setFixedWidth(width)
        self.card.layout().invalidate()
        self.title_label.setFixedWidth(inner_width)
        title_height = max(self.title_label.fontMetrics().height(), self.title_label.heightForWidth(inner_width))
        self.title_label.setFixedHeight(title_height)
        body_height = max(self.body_label.fontMetrics().height(), self.body_label.heightForWidth(inner_width - 12))
        fixed_height = 22 + 18 + self.counter_label.sizeHint().height() + title_height + self.buttons.sizeHint().height() + 3 * 13
        height = min(bounds.height(), fixed_height + min(body_height + 8, round(225 * scale)))
        self.card.setFixedHeight(height)
        self.card.layout().activate()

        self.card.move(self._card_position(target))
        if available_width is None and not target.isEmpty() and self.card.geometry().intersects(target):
            # A caption need not span most of a dialog just because the font is
            # larger. Reflow it into a usable side column before scrolling the
            # underlying page; the body remains scrollable if needed.
            side_width = max(bounds.right() - target.right() - 18 + 1,
                             target.left() - bounds.left() - 18)
            if 320 <= side_width < width:
                self._layout_card(target, available_width=side_width)

    def _card_position(self, target):
        bounds = self.rect().adjusted(16, 16, -16, -16)
        width, height = self.card.width(), self.card.height()
        if target.isEmpty():
            position = QPoint(bounds.center().x() - width // 2, bounds.center().y() - height // 2)
        else:
            gap = 18
            candidates = [QPoint(target.right() + gap, target.center().y() - height // 2),
                          QPoint(target.left() - gap - width, target.center().y() - height // 2),
                          QPoint(target.center().x() - width // 2, target.bottom() + gap),
                          QPoint(target.center().x() - width // 2, target.top() - gap - height)]
            def bounded(point):
                return QPoint(max(bounds.left(), min(point.x(), bounds.right() - width + 1)),
                              max(bounds.top(), min(point.y(), bounds.bottom() - height + 1)))
            def score(point):
                box = QRect(point, self.card.size())
                overlap = box.intersected(target)
                return overlap.width() * overlap.height() if not overlap.isEmpty() else 0
            position = min((bounded(point) for point in candidates), key=score)
        return position

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shade = QPainterPath()
        shade.addRect(QRectF(self.rect()))
        if not self.spotlight_rect.isEmpty():
            opening = QPainterPath()
            opening.addRoundedRect(QRectF(self.spotlight_rect), 9, 9)
            shade = shade.subtracted(opening)
        painter.fillPath(shade, QColor(5, 10, 19, 192))
        if not self.spotlight_rect.isEmpty():
            accent = QColor(color('primary'))
            halo = QColor(accent)
            halo.setAlpha(65)
            painter.setPen(QPen(halo, 7))
            painter.drawRoundedRect(QRectF(self.spotlight_rect), 9, 9)
            painter.setPen(QPen(accent, 2))
            painter.drawRoundedRect(QRectF(self.spotlight_rect), 9, 9)

    def _belongs_to_host(self, obj):
        while obj is not None:
            if obj is self.host:
                return True
            if isinstance(obj, QWidget) and obj.window() is not self.host.window():
                # A newly opened modal dialog needs its own focus/close flow;
                # the owner will interrupt this tour before introducing it.
                return False
            obj = obj.parent()
        return False

    def _belongs_to_overlay(self, obj):
        while obj is not None:
            if obj is self:
                return True
            obj = obj.parent()
        return False

    def _focus_buttons(self):
        return [b for b in (self.back_button, self.next_button, self.skip_button, self.disable_button)
                if b.isEnabled()]

    def _focus_changed(self, old, new):
        if (self._active and not self._focus_guard and new is not None
                and self._belongs_to_host(new) and not self._belongs_to_overlay(new)):
            self._focus_guard = True
            self.next_button.setFocus(Qt.FocusReason.OtherFocusReason)
            self._focus_guard = False

    def eventFilter(self, watched, event):
        if not self._active:
            return False
        kind = event.type()
        if watched is self.host:
            if kind in (QEvent.Type.Hide, QEvent.Type.Close):
                self.stop()
                return False
            if kind == QEvent.Type.Resize:
                self._last_layout = None
                self._refresh_geometry()
                # Child layouts and scroll ranges settle after this event.
                self._schedule_geometry(reveal=True)
        if not self._belongs_to_host(watched):
            return False
        own = self._belongs_to_overlay(watched)
        if not own and kind in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show,
                                QEvent.Type.Hide, QEvent.Type.LayoutRequest, QEvent.Type.ParentChange):
            # Follow real layout/scroll changes on the next event cycle. The
            # timer remains a fallback for callable targets that change identity.
            # Reveal after an actual control reflow, not after an ordinary
            # scrollbar move. That keeps intentional scroll changes intact.
            reveal = kind in (QEvent.Type.Move, QEvent.Type.Resize) and watched is self._resolve_target()
            self._schedule_geometry(reveal=reveal)
        if kind == QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        if kind == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self._skip()
                return True
            if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                buttons = self._focus_buttons()
                focus = QApplication.focusWidget()
                index = buttons.index(focus) if focus in buttons else 0
                backward = event.key() == Qt.Key.Key_Backtab or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                buttons[(index + (-1 if backward else 1)) % len(buttons)].setFocus(Qt.FocusReason.TabFocusReason)
                return True
            if event.key() in (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                scrollbar = self.body_scroll.verticalScrollBar()
                direction = 1 if event.key() == Qt.Key.Key_PageDown else -1
                scrollbar.setValue(scrollbar.value() + direction * scrollbar.pageStep())
                return True
        if not own and kind in (
            QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick, QEvent.Type.Wheel, QEvent.Type.ContextMenu,
            QEvent.Type.KeyPress, QEvent.Type.KeyRelease, QEvent.Type.Shortcut,
            QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate, QEvent.Type.TouchEnd,
            QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop,
        ):
            event.accept()
            return True
        return False

    def closeEvent(self, event):
        self.stop()
        event.accept()

    def mousePressEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        event.accept()
