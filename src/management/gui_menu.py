
"""Native menu button with an animated, truthful expanded indicator."""
from PySide6.QtCore import QVariantAnimation,QEasingCurve,QPointF
from PySide6.QtGui import QPainter,QPen,QPalette,QColor
from PySide6.QtWidgets import QPushButton


class MenuButton(QPushButton):
    def __init__(self,text,parent=None):
        super().__init__(text,parent)
        self.menu_open=False;self.arrow_angle=0.0
        self.setStyleSheet('QPushButton { padding-right: 30px; } QPushButton::menu-indicator { image: none; width: 0px; }')
        self.animation=QVariantAnimation(self)
        self.animation.setDuration(150);self.animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.animation.valueChanged.connect(self._angle_changed)

    def setMenu(self,menu):
        previous=self.menu()
        if previous:
            previous.aboutToShow.disconnect(self._expanded)
            previous.aboutToHide.disconnect(self._collapsed)
        super().setMenu(menu)
        if menu:
            menu.aboutToShow.connect(self._expanded)
            menu.aboutToHide.connect(self._collapsed)

    def _expanded(self):self._toggle(True)
    def _collapsed(self):self._toggle(False)

    def _toggle(self,expanded):
        self.menu_open=expanded
        self.setAccessibleDescription('菜单已展开' if expanded else '菜单已收起')
        self.animation.stop();self.animation.setStartValue(self.arrow_angle)
        self.animation.setEndValue(180.0 if expanded else 0.0);self.animation.start()

    def _angle_changed(self,angle):
        self.arrow_angle=float(angle);self.update()

    def paintEvent(self,event):
        super().paintEvent(event)
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        from .gui_theme import color
        painter.setPen(QPen(QColor(color('primary_text')) if self.objectName()=='Primary' else self.palette().color(QPalette.ColorRole.ButtonText),1.7))
        painter.translate(self.width()-16,self.height()/2);painter.rotate(self.arrow_angle)
        painter.drawPolyline([QPointF(-4,-2),QPointF(0,2),QPointF(4,-2)])
        painter.end()
