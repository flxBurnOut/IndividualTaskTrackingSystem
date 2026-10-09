"""Real rendered arrows and unchanged native popup/button interactions."""
import os
import time
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')

import pytest
from PySide6.QtCore import QAbstractAnimation,QEvent,QPoint,Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QComboBox,QLineEdit,QPushButton,QVBoxLayout,QWidget
from management.appearance import DEFAULT_APPEARANCE
from management.gui_theme import apply_appearance,color,current_appearance
from management import gui_indicators


def wait_for(predicate,timeout=2):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():return
        QTest.qWait(5)
    raise AssertionError('The native control did not reach the expected state')


@pytest.fixture
def scene(monkeypatch):
    app=QApplication.instance() or QApplication([]);app.setStyle('Fusion')
    old_config=current_appearance()
    apply_appearance(app,DEFAULT_APPEARANCE)
    monkeypatch.setattr(gui_indicators,'reduce_motion',lambda:False)
    host=QWidget();layout=QVBoxLayout(host)
    combo=QComboBox();combo.addItems(['First','Second','Third'])
    draft=QLineEdit('Unsaved input')
    disclosure=QPushButton('Advanced options');disclosure.setCheckable(True);disclosure.setProperty('disclosure',True)
    for widget in (combo,draft,disclosure):layout.addWidget(widget)
    host.resize(340,200);host.show();app.processEvents()
    yield app,host,combo,draft,disclosure
    combo.hidePopup();host.close();host.deleteLater();app.sendPostedEvents(None,QEvent.Type.DeferredDelete);app.processEvents()
    apply_appearance(app,old_config)


def ink_pixels(indicator,role):
    image=indicator.arrow.grab().toImage()
    target=QColor(color(role))
    return sum(abs(pixel.red()-target.red())+abs(pixel.green()-target.green())+abs(pixel.blue()-target.blue())<45
               and pixel.alpha()>180
               for x in range(image.width()) for y in range(image.height())
               for pixel in [image.pixelColor(x,y)])


@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('size',[13,20])
def test_dropdown_arrow_is_visible_and_rotates_with_real_popup(scene,theme,size):
    app,host,combo,draft,disclosure=scene
    apply_appearance(app,{'theme':theme,'font_size':size,'accent':'violet'});app.processEvents()
    indicator=combo._management_indicator
    assert indicator.arrow.isVisible() and ink_pixels(indicator,'muted')>=8
    assert indicator.arrow.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert combo.childAt(indicator.arrow.geometry().center()) is not indicator.arrow
    before=indicator.arrow.grab().toImage()
    QTest.mouseClick(combo,Qt.MouseButton.LeftButton,pos=indicator.arrow.geometry().center())
    wait_for(lambda:indicator.open and 0<indicator.arrow.angle<180)
    wait_for(lambda:indicator.animation.state()==QAbstractAnimation.State.Stopped)
    assert indicator.arrow.angle==180 and combo.view().window().isVisible()
    assert indicator.arrow.grab().toImage()!=before
    QTest.keyClick(combo.view(),Qt.Key.Key_Down);QTest.keyClick(combo.view(),Qt.Key.Key_Return)
    wait_for(lambda:not indicator.open and indicator.arrow.angle==0)
    assert combo.currentIndex()==1 and draft.text()=='Unsaved input'
    combo.setEnabled(False);app.processEvents()
    assert ink_pixels(indicator,'disabled_text')>=8


def test_disclosure_arrow_animates_without_intercepting_native_click_or_keyboard(scene):
    app,host,combo,draft,button=scene
    indicator=button._management_indicator
    host.layout().setAlignment(button,Qt.AlignmentFlag.AlignLeft)
    button.toggled.connect(lambda opened:button.setText('Hide' if opened else 'Advanced options'))
    app.processEvents()
    original_width=button.width()
    changes=[];button.toggled.connect(changes.append)
    QTest.mouseClick(button,Qt.MouseButton.LeftButton,pos=indicator.arrow.geometry().center())
    wait_for(lambda:0<indicator.arrow.angle<90)
    assert button.width()<original_width
    wait_for(lambda:indicator.arrow.angle==90)
    assert button.isChecked() and changes==[True]
    button.setFocus();QTest.keyClick(button,Qt.Key.Key_Space)
    wait_for(lambda:indicator.arrow.angle==0)
    assert not button.isChecked() and changes==[True,False]
    assert draft.text()=='Unsaved input'


def test_indicator_respects_reduce_motion_and_dynamic_controls(scene,monkeypatch):
    app,host,combo,draft,button=scene
    indicator=combo._management_indicator
    monkeypatch.setattr(gui_indicators,'reduce_motion',lambda:True)
    combo.showPopup();app.processEvents()
    assert indicator.open and indicator.arrow.angle==180
    assert indicator.animation.state()==QAbstractAnimation.State.Stopped
    combo.hidePopup();app.processEvents()
    assert indicator.arrow.angle==0
    late=QComboBox();late.addItems(['Late','Other']);host.layout().addWidget(late);app.processEvents()
    assert late._management_indicator.arrow.isVisible()
    assert combo._management_indicator is indicator
    assert draft.text()=='Unsaved input'
