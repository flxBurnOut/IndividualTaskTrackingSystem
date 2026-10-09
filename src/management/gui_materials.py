"""Client-area materials and finite animations; never sample the desktop.

The canvas owns a cached, synthetic backdrop. Glass panels sample that backdrop,
not their children or other windows. Every animated control remains a standard
Qt widget and the page veil is explicitly transparent to input.
"""
from __future__ import annotations

import os
import time
import weakref

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QEvent, QObject, QPoint, QPointF, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QFrame, QPushButton, QStackedWidget, QStyle, QStyleOptionButton, QStyleOptionTab, QStyleOptionTabBarBase, QTabBar, QTabWidget, QWidget
from shiboken6 import isValid

from .gui_theme import bind_theme, color, current_appearance


def reduce_motion() -> bool:
    """Respect the process override and the Windows client-area animation flag."""
    if os.environ.get('PERSONAL_MANAGEMENT_REDUCE_MOTION', '').strip().lower() in {'1', 'true', 'yes', 'on'}:
        return True
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            enabled = wintypes.BOOL()
            query = ctypes.windll.user32.SystemParametersInfoW
            query.argtypes = (wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT)
            query.restype = wintypes.BOOL
            # SPI_GETCLIENTAREAANIMATION is a read-only query (no broadcast).
            if query(0x1042, 0, ctypes.byref(enabled), 0):
                return not bool(enabled.value)
        except (AttributeError, ImportError, OSError):
            pass
    return False


def _alpha(value, opacity):
    result = QColor(value)
    result.setAlphaF(max(0.0, min(1.0, opacity)))
    return result


def _blend(first, second, amount):
    return QColor.fromRgbF(*(
        left + (right - left) * amount
        for left, right in zip(first.getRgbF(), second.getRgbF())
    ))


def _canvas(widget):
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, AppCanvas):
            return parent
        parent = parent.parentWidget()
    return None


def _paint_backdrop(painter, widget, *, refract=False):
    canvas = _canvas(widget)
    if canvas is None:
        painter.fillRect(widget.rect(), QColor(color('window')))
        return
    origin = widget.mapTo(canvas, QPoint())
    painter.save()
    painter.translate(-origin.x(), -origin.y())
    if refract:
        # Only the broad ambient light is displaced, never text or child widgets.
        painter.translate(1.25, -0.65)
        painter.scale(1.008, 1.008)
    painter.drawPixmap(QPointF(), canvas.backdrop())
    painter.restore()


class AppCanvas(QWidget):
    """Pearl canvas with quiet, static cool/warm light and a DPR-aware cache."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._backdrop = None
        self._backdrop_key = None
        self.setAutoFillBackground(True)
        bind_theme(self, self.refresh_material)

    def refresh_material(self):
        self._backdrop = None
        self._backdrop_key = None
        self.update()

    def backdrop(self):
        appearance = current_appearance()
        dark = appearance['theme'] == 'dark'
        accent = appearance.get('accent', 'default')
        ratio = self.devicePixelRatioF()
        key = (self.width(), self.height(), ratio, dark, accent, color('window'),
               color('primary'))
        if self._backdrop is not None and self._backdrop_key == key:
            return self._backdrop
        width, height = max(1, self.width()), max(1, self.height())
        pixmap = QPixmap(max(1, round(width * ratio)), max(1, round(height * ratio)))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(QColor(color('window')))
        painter = QPainter(pixmap)
        base = QLinearGradient(0, 0, width * .8, height)
        top, bottom = QColor(color('canvas_top')), QColor(color('canvas_bottom'))
        base.setColorAt(0, top)
        base.setColorAt(.55, _blend(top, bottom, .55))
        base.setColorAt(1, bottom)
        painter.fillRect(QRectF(0, 0, width, height), base)
        for x, y, radius, tint, opacity in (
            (.10, .18, .70, '#9a9290' if dark else '#d9dce7', .06 if dark else .28),
            (.90, .74, .67, (color('primary') if accent != 'default' else
                             '#947d88' if dark else '#e9dbd4'), .07 if dark else .25),
            (.58, -.10, .57, '#b1aba7' if dark else '#ffffff', .03 if dark else .50),
        ):
            glow = QRadialGradient(QPointF(width * x, height * y), max(width, height) * radius)
            glow.setColorAt(0, _alpha(tint, opacity))
            glow.setColorAt(1, _alpha(tint, 0))
            painter.fillRect(QRectF(0, 0, width, height), glow)
        # A broad silver/graphite pool behind the floating sidebar provides
        # something visible for its translucent material to transmit.
        pool = QRadialGradient(QPointF(width * .055, height * .67), max(height * .53, width * .23))
        pool.setColorAt(0, _alpha('#b4a6a6' if dark else '#aeb6c4', .20 if dark else .48))
        pool.setColorAt(1, _alpha('#b4a6a6' if dark else '#aeb6c4', 0))
        painter.fillRect(QRectF(0, 0, width, height), pool)
        painter.end()
        self._backdrop, self._backdrop_key = pixmap, key
        return pixmap

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(QPointF(), self.backdrop())
        painter.end()


class GlassPanel(QFrame):
    """A translucent sidebar or toolbar, without an input-capturing overlay."""

    def __init__(self, parent=None, *, role='sidebar'):
        super().__init__(parent)
        if role not in {'sidebar', 'toolbar'}:
            raise ValueError('Unknown glass panel role: ' + str(role))
        self.role = role
        self.setAutoFillBackground(False)
        bind_theme(self, self.refresh_material)

    def refresh_material(self):
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        radius = 19.0 if self.role == 'sidebar' else 16.0
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.setClipPath(path)
        _paint_backdrop(painter, self, refract=True)
        dark = current_appearance()['theme'] == 'dark'
        frost = QLinearGradient(rect.topLeft(), rect.bottomRight())
        frost.setColorAt(0, _alpha(color('surface'), .44 if dark else .62))
        frost.setColorAt(.48, _alpha(color('surface'), .16 if dark else .15))
        frost.setColorAt(1, _alpha(color('surface'), .32 if dark else .30))
        painter.fillPath(path, frost)
        # Soft vertical reflection bands and a complete inner rim distinguish
        # the material from an ordinary opaque card. Text is never sampled.
        reflection = QLinearGradient(rect.topLeft(), rect.topRight())
        reflection.setColorAt(0, _alpha('#ffffff', .13 if dark else .32))
        reflection.setColorAt(.09, _alpha('#ffffff', .035 if dark else .08))
        reflection.setColorAt(.23, _alpha('#ffffff', 0))
        reflection.setColorAt(.84, _alpha('#ffffff', 0))
        reflection.setColorAt(.97, _alpha('#ffffff', .075 if dark else .19))
        reflection.setColorAt(1, _alpha('#ffffff', .025 if dark else .06))
        painter.fillPath(path, reflection)
        rim = QLinearGradient(rect.topLeft(), rect.bottomRight())
        rim.setColorAt(0, _alpha('#ffffff', .36 if dark else .97))
        rim.setColorAt(.45, _alpha('#ffffff', .13 if dark else .52))
        rim.setColorAt(1, _alpha(color('border'), .86))
        painter.setPen(QPen(rim, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)
        painter.setPen(QPen(_alpha('#ffffff', .065 if dark else .28), .75))
        painter.drawRoundedRect(rect.adjusted(1.2, 1.2, -1.2, -1.2), radius - 1.2, radius - 1.2)
        painter.end()


class NavigationButton(QPushButton):
    """Standard button semantics with a short painted hover/selection blend."""

    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self.setObjectName('Nav')
        self._fill = QColor(Qt.GlobalColor.transparent)
        self._start_fill = QColor(self._fill)
        self._end_fill = QColor(self._fill)
        self._state = None
        self._keyboard_focus = False
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(260)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._frame)
        self.toggled.connect(self._sync_state)
        bind_theme(self, self.refresh_material)

    def _target_fill(self):
        if not self.isEnabled():
            return _alpha(color('disabled_bg'), .4)
        if self.isDown():
            return _alpha(color('selection'), .98)
        if self.isChecked():
            return _alpha(color('selection'), .86)
        if self.underMouse():
            return _alpha(color('surface'), .67)
        return _alpha(color('surface'), 0)

    def refresh_material(self):
        self._state = None
        self._sync_state(animate=False)

    def _sync_state(self, *_args, animate=True):
        state = (self.isEnabled(), self.isDown(), self.isChecked(), self.underMouse())
        if state == self._state:
            self.update()
            return
        self._state = state
        self.animation.stop()
        target = self._target_fill()
        if animate and not reduce_motion() and self.isVisible():
            self._start_fill = QColor(self._fill)
            self._end_fill = target
            self.animation.setStartValue(0.0)
            self.animation.setEndValue(1.0)
            self.animation.start()
        else:
            self._fill = target
            self.update()

    def _frame(self, value):
        if reduce_motion():
            self.animation.stop()
            self._fill = self._target_fill()
            self.update()
            return
        self._fill = _blend(self._start_fill, self._end_fill, float(value))
        self.update()

    def event(self, event):
        result = super().event(event)
        if not hasattr(self, 'animation'):
            return result
        if event.type() == QEvent.Type.FocusIn:
            self._keyboard_focus = event.reason() in {
                Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                Qt.FocusReason.ShortcutFocusReason,
            }
        elif event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.FocusOut):
            self._keyboard_focus = False
        elif event.type() == QEvent.Type.KeyPress and self.hasFocus():
            self._keyboard_focus = True
        if event.type() == QEvent.Type.Hide:
            self.animation.stop()
            self._fill = self._target_fill()
        elif event.type() in {
            QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease, QEvent.Type.KeyPress, QEvent.Type.KeyRelease,
            QEvent.Type.EnabledChange, QEvent.Type.FocusIn, QEvent.Type.FocusOut,
        }:
            self._sync_state()
        return result

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._fill)
        painter.drawRoundedRect(rect, 11, 11)
        if self.isChecked():
            shine = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            shine.setColorAt(0, _alpha('#ffffff', .23 if current_appearance()['theme'] == 'dark' else .91))
            shine.setColorAt(1, _alpha(color('border'), .20))
            painter.setPen(QPen(shine, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, 11, 11)
        if self.hasFocus() and self._keyboard_focus:
            painter.setPen(QPen(QColor(color('focus')), 1.6))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, 11, 11)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        # CE_PushButtonLabel alone does not apply the outer button's QSS padding.
        option.rect = option.rect.adjusted(14, 0, -14, 0)
        text_role = 'disabled_text' if not self.isEnabled() else ('selection_text' if self.isChecked() else 'text')
        option.palette.setColor(QPalette.ColorRole.ButtonText, QColor(color(text_role)))
        # Delegating the label keeps setIcon(), mnemonics and native text metrics.
        self.style().drawControl(QStyle.ControlElement.CE_PushButtonLabel, option, painter, self)
        painter.end()


class _TransitionVeil(QWidget):
    def __init__(self, stack):
        super().__init__(stack)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.opacity = 0.0
        self.snapshot = QPixmap()
        self.hide()

    def capture_backdrop(self):
        ratio = self.devicePixelRatioF()
        self.snapshot = QPixmap(max(1, round(self.width() * ratio)), max(1, round(self.height() * ratio)))
        self.snapshot.setDevicePixelRatio(ratio)
        self.snapshot.fill(QColor(color('window')))
        painter = QPainter(self.snapshot)
        _paint_backdrop(painter, self)
        painter.end()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setOpacity(self.opacity)
        painter.drawPixmap(QPointF(), self.snapshot)
        painter.end()


class PageTransition(QObject):
    """Fade in an existing stack page; never move or recreate its controls."""

    def __init__(self, stack, parent=None):
        super().__init__(parent or stack)
        self._stack = weakref.ref(stack)
        self._host = None
        self._pending_page = None
        self._pending_since = 0.0
        self._veil = _TransitionVeil(stack)
        self.destroyed.connect(self._veil.deleteLater)
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.timeout.connect(self._start_after_layout)
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(260)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.animation.valueChanged.connect(self._frame)
        self.animation.finished.connect(self.stop)
        stack.currentChanged.connect(self._changed)
        stack.installEventFilter(self)
        bind_theme(self, self.stop)

    def is_active(self):
        """Include layout settling so captures cannot race the pending entrance."""
        return bool(self._pending_page is not None or self._settle_timer.isActive()
                    or self.animation.state() != QAbstractAnimation.State.Stopped
                    or isValid(self._veil) and self._veil.isVisible())

    def stop(self):
        self._settle_timer.stop()
        self._pending_page = None
        self.animation.stop()
        if isValid(self._veil):
            self._veil.hide()
            self._veil.snapshot = QPixmap()

    def _changed(self, _index):
        self.stop()
        stack = self._stack()
        if (stack is None or not isValid(stack) or not stack.isVisible()
                or stack.currentWidget() is None or reduce_motion()):
            return
        host = stack.window()
        previous = self._host() if self._host is not None else None
        if previous is not host:
            if previous is not None and isValid(previous):
                previous.removeEventFilter(self)
            self._host = weakref.ref(host)
            host.installEventFilter(self)
        self._pending_page = weakref.ref(stack.currentWidget())
        self._pending_since = time.monotonic()
        self._prepare_veil(stack)
        self._queue_settle()

    def _prepare_veil(self, stack):
        self._veil.setGeometry(stack.contentsRect())
        self._veil.capture_backdrop()
        if self._pending_page is not None:
            self._veil.opacity = 1.0
        self._veil.show()
        self._veil.raise_()

    def _queue_settle(self):
        # A one-shot debounce allows navigate()'s title/notice/content changes to
        # finish. Bound the delay even when asynchronous content keeps relaying out.
        elapsed = (time.monotonic() - self._pending_since) * 1000
        self._settle_timer.start(0 if elapsed >= 96 else 24)

    def _start_after_layout(self):
        if self._pending_page is None:
            return
        stack, page = self._stack(), self._pending_page()
        if (stack is None or not isValid(stack) or page is None or not isValid(page)
                or stack.currentWidget() is not page or not stack.isVisible()
                or reduce_motion()):
            self.stop()
            return
        self._prepare_veil(stack)
        self._pending_page = None
        self._settle_timer.stop()
        self.animation.setStartValue(1.0)
        self.animation.setEndValue(0.0)
        self.animation.start()

    def _frame(self, value):
        if reduce_motion():
            self.stop()
            return
        if isValid(self._veil):
            self._veil.opacity = float(value)
            self._veil.update()

    def eventFilter(self, watched, event):
        kind = event.type()
        host = self._host() if self._host is not None else None
        if kind in (QEvent.Type.Hide, QEvent.Type.Close, QEvent.Type.DevicePixelRatioChange):
            self.stop()
        elif kind == QEvent.Type.Resize:
            if watched is host:
                # Real window resizing cancels the effect; a page's own layout
                # resize preserves it and re-samples the backdrop at its new size.
                self.stop()
            elif self._veil.isVisible():
                stack = self._stack()
                if stack is not None and isValid(stack):
                    self._prepare_veil(stack)
                    if self._pending_page is not None:
                        self._queue_settle()
        elif kind == QEvent.Type.LayoutRequest and self._pending_page is not None:
            self._queue_settle()
        return super().eventFilter(watched, event)


class AnimatedTabBar(QTabBar):
    """Cross-fade native styled tab states without replacing tab semantics."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected = -1
        self._before = QPixmap()
        self._after = QPixmap()
        self._progress = 1.0
        self._host = None
        self._start_weights = {}
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(260)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.animation.valueChanged.connect(self._frame)
        self.animation.finished.connect(self.stop)
        self.currentChanged.connect(self._selection_changed)
        bind_theme(self, self.stop)

    def stop(self):
        self.animation.stop()
        self._selected = self.currentIndex()
        self._before = QPixmap()
        self._after = QPixmap()
        self._start_weights = {}
        self._progress = 1.0
        self.update()

    def _native_state(self, selected):
        ratio = self.devicePixelRatioF()
        image = QPixmap(max(1, round(self.width() * ratio)), max(1, round(self.height() * ratio)))
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        if self.drawBase():
            base = QStyleOptionTabBarBase()
            base.initFrom(self)
            base.shape = self.shape()
            base.tabBarRect = self.rect()
            base.selectedTabRect = self.tabRect(selected)
            base.documentMode = self.documentMode()
            self.style().drawPrimitive(QStyle.PrimitiveElement.PE_FrameTabBarBase, base, painter, self)
        # Native Qt draws the selected tab last, preserving overlapping styles.
        indices = [index for index in range(self.count()) if index != selected] + [selected]
        for index in indices:
            if index < 0 or not self.isTabVisible(index):
                continue
            option = QStyleOptionTab()
            self.initStyleOption(option, index)
            if index == selected:
                option.state |= QStyle.StateFlag.State_Selected
            else:
                option.state &= ~QStyle.StateFlag.State_Selected
            self.style().drawControl(QStyle.ControlElement.CE_TabBarTab, option, painter, self)
        painter.end()
        return image

    def _composed_state(self):
        image = QPixmap(self._before.size())
        image.setDevicePixelRatio(self._before.devicePixelRatioF())
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setOpacity(1.0 - self._progress)
        painter.drawPixmap(QPointF(), self._before)
        # Add premultiplied channels so an old opaque selection actually fades
        # away when the new state has transparent corners or borders.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        painter.setOpacity(self._progress)
        painter.drawPixmap(QPointF(), self._after)
        painter.end()
        return image

    def _current_weights(self):
        weights = {index: weight * (1 - self._progress) for index, weight in self._start_weights.items()}
        weights[self._selected] = weights.get(self._selected, 0) + self._progress
        return {index: weight for index, weight in weights.items() if weight > .001}

    def _native_mixture(self, weights):
        image = self._native_state(self.currentIndex())
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        for index, weight in weights.items():
            painter.setOpacity(weight)
            painter.drawPixmap(QPointF(), self._native_state(index))
        painter.end()
        return image

    def _selection_changed(self, index):
        previous = self._selected
        if (reduce_motion() or not self.isVisible()
                or previous < 0 or index < 0 or previous == index):
            self.stop()
            return
        host = self.window()
        previous_host = self._host() if self._host is not None else None
        if previous_host is not host:
            if previous_host is not None and isValid(previous_host):
                previous_host.removeEventFilter(self)
            self._host = weakref.ref(host)
            host.installEventFilter(self)
        active = not self._before.isNull()
        weights = self._current_weights() if active else {previous: 1.0}
        before = self._composed_state() if active else self._native_state(previous)
        self.animation.stop()
        self._before, self._after = before, self._native_state(index)
        self._start_weights = weights
        self._selected = index
        self._progress = 0.0
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.start()
        self.update()

    def _frame(self, value):
        if reduce_motion():
            self.stop()
            return
        self._progress = float(value)
        self.update()

    def paintEvent(self, event):
        if reduce_motion() or self._before.isNull():
            if not self._before.isNull():
                self.stop()
            return super().paintEvent(event)
        painter = QPainter(self)
        painter.drawPixmap(QPointF(), self._composed_state())
        painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # A selected tab's native size hint may change the bar's own geometry.
        # Keep its finite clock and re-render at the new geometry; only the
        # separate host-window Resize filter cancels a real window resize.
        if not self._before.isNull():
            self._before = self._native_mixture(self._start_weights)
            self._after = self._native_state(self.currentIndex())
            self.update()

    def hideEvent(self, event):
        self.stop()
        super().hideEvent(event)

    def changeEvent(self, event):
        if event.type() in (QEvent.Type.EnabledChange, QEvent.Type.FontChange,
                            QEvent.Type.StyleChange, QEvent.Type.PaletteChange,
                            QEvent.Type.DevicePixelRatioChange):
            self.stop()
        super().changeEvent(event)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Hide, QEvent.Type.Close,
                            QEvent.Type.DevicePixelRatioChange):
            self.stop()
        return super().eventFilter(watched, event)


class AnimatedTabWidget(QTabWidget):
    """Install the native-semantic animated bar before any pages are added."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTabBar(AnimatedTabBar(self))


class TabTransition(PageTransition):
    """Use the same finite entrance for a tab's content, keeping its bar live.

    QTabWidget owns a direct-child QStackedWidget. Attaching to that content
    stack leaves the tab bar, keyboard navigation, scroll positions and every
    existing editor intact. Nested tabs each get their own content transition.
    """

    def __init__(self, tabs):
        stack = tabs.findChild(QStackedWidget, options=Qt.FindChildOption.FindDirectChildrenOnly)
        if stack is None:
            raise ValueError('A tab content stack is required for its transition')
        super().__init__(stack, tabs)
