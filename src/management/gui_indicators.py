"""Visible native-control indicators, without replacing controls or popup behavior.

Qt's stylesheet dropdown subcontrol can suppress the platform arrow. A tiny,
input-transparent child paints its indicator instead; the combo still owns its
native popup, focus, keyboard navigation, accessibility and current selection.
"""
from __future__ import annotations

import weakref

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QEvent, QObject, QPointF, QRect, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QComboBox, QPushButton, QStyle, QStyleOptionComboBox, QWidget
from shiboken6 import isValid

from .gui_materials import reduce_motion
from .gui_theme import bind_theme, color
from .gui_visual_profile import visual_style


class _Chevron(QWidget):
    def __init__(self, control, disclosure=False):
        super().__init__(control)
        self.setObjectName('ControlChevron')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.disclosure = disclosure
        self.angle = 0.0

    def paintEvent(self, event):
        if visual_style() != 'glass':
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink = QColor(color('muted' if self.parentWidget().isEnabled() else 'disabled_text'))
        painter.setPen(QPen(ink, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.translate(self.rect().center().x() + .5, self.rect().center().y() + .5)
        painter.rotate(self.angle)
        size = min(5.5, max(4.0, self.fontMetrics().height() * .24))
        path = QPainterPath()
        if self.disclosure:
            path.moveTo(-size / 2, -size);path.lineTo(size / 2, 0);path.lineTo(-size / 2, size)
        else:
            path.moveTo(-size, -size / 2);path.lineTo(0, size / 2);path.lineTo(size, -size / 2)
        painter.drawPath(path)
        painter.end()


class ControlIndicator(QObject):
    def __init__(self, control):
        super().__init__(control)
        self.control = weakref.ref(control)
        self.disclosure = isinstance(control, QPushButton)
        self.arrow = _Chevron(control, self.disclosure)
        self.open = bool(control.isChecked()) if self.disclosure else False
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(180)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.animation.valueChanged.connect(self._frame)
        self.popup = None
        if self.disclosure:
            control.toggled.connect(self.set_open)
        else:
            self._watch_popup(control)
        control.installEventFilter(self)
        bind_theme(self, self.refresh)

    def _watch_popup(self, control):
        popup = control.view().window()
        previous = self.popup
        if previous is popup:
            return
        if previous is not None and isValid(previous):
            previous.removeEventFilter(self)
        self.popup = popup
        popup.installEventFilter(self)

    def _target(self):
        return (90.0 if self.disclosure else 180.0) if self.open else 0.0

    def set_open(self, opened):
        self.open = bool(opened)
        self.animation.stop()
        control = self.control()
        if (control is not None and isValid(control) and control.isVisible()
                and visual_style() == 'glass' and not reduce_motion()):
            self.animation.setStartValue(self.arrow.angle)
            self.animation.setEndValue(self._target())
            self.animation.start()
        else:
            self.arrow.angle = self._target()
            self.arrow.update()

    def _frame(self, angle):
        if visual_style() != 'glass' or reduce_motion():
            self.animation.stop()
            angle = self._target()
        self.arrow.angle = float(angle)
        self.arrow.update()

    def refresh(self):
        control = self.control()
        if control is None or not isValid(control):
            return
        self.animation.stop()
        self.arrow.angle = self._target()
        self.place_arrow()

    def place_arrow(self):
        control = self.control()
        if control is None or not isValid(control):
            return
        if self.disclosure:
            rect = QRect(max(0, control.width() - 28), 0, 22, control.height())
        else:
            option = QStyleOptionComboBox()
            control.initStyleOption(option)
            rect = control.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option,
                                                  QStyle.SubControl.SC_ComboBoxArrow, control)
        self.arrow.setGeometry(rect)
        self.arrow.setVisible(visual_style() == 'glass' and control.isVisible())
        self.arrow.raise_()
        self.arrow.update()

    def eventFilter(self, watched, event):
        kind = event.type()
        popup = self.popup
        if watched is popup:
            if kind == QEvent.Type.Show:
                self.set_open(True)
            elif kind in (QEvent.Type.Hide, QEvent.Type.Close):
                self.set_open(False)
        elif kind == QEvent.Type.Hide:
            self.animation.stop()
            if not self.disclosure:self.open = False
            self.arrow.angle = self._target()
        elif kind == QEvent.Type.Resize:
            # Changing "Show" to "Hide" can resize a disclosure button. Move
            # the glyph with it without cancelling its opening/closing rotation.
            self.place_arrow()
        elif kind in (QEvent.Type.StyleChange, QEvent.Type.FontChange,
                      QEvent.Type.EnabledChange, QEvent.Type.Show):
            if not self.disclosure:
                self._watch_popup(watched)
            self.refresh()
        return False


class _IndicatorManager(QObject):
    def __init__(self, app):
        super().__init__(app)
        app.installEventFilter(self)
        for widget in app.allWidgets():
            self.ensure(widget)

    def ensure(self, widget):
        if not isinstance(widget, QComboBox) and not (
                isinstance(widget, QPushButton) and widget.property('disclosure') is True):
            return
        if getattr(widget, '_management_indicator', None) is None:
            # Store on the real control so one control owns exactly one helper.
            widget._management_indicator = False
            widget._management_indicator = ControlIndicator(widget)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Polish, QEvent.Type.Show, QEvent.Type.DynamicPropertyChange):
            self.ensure(watched)
        return False


def install_indicators(app):
    if getattr(app, '_management_indicators', None) is None:
        app._management_indicators = _IndicatorManager(app)
