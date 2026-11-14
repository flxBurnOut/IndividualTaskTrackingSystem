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


@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('font_size',[13,20])
@pytest.mark.parametrize('raster_scale',[1.0,1.25,1.5])
def test_checkbox_focus_check_uncheck_keeps_indicator_and_outline_aligned(scene,theme,font_size,raster_scale):
    """Native layout and scaled rendering stay aligned through the full cycle."""
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QCheckBox,QStyle,QStyleOptionButton
    app,host,combo,draft,disclosure=scene
    apply_appearance(app,{'theme':theme,'font_size':font_size,'accent':'violet'})
    checkbox=QCheckBox('允许使用本机辅助处理');host.layout().insertWidget(0,checkbox)
    host.resize(650,320);draft.setFocus();QTest.qWait(25)

    def snapshot():
        app.processEvents()
        option=QStyleOptionButton();checkbox.initStyleOption(option)
        indicator=checkbox.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator,option,checkbox)
        contents=checkbox.style().subElementRect(QStyle.SubElement.SE_CheckBoxContents,option,checkbox)
        geometry=(checkbox.geometry().getRect(),(checkbox.sizeHint().width(),checkbox.sizeHint().height()),indicator.getRect(),contents.getRect())
        image=QImage(QSize(round(checkbox.width()*raster_scale),round(checkbox.height()*raster_scale)),QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(raster_scale);image.fill(Qt.GlobalColor.transparent);checkbox.render(image)
        # Sample the inside of the indicator; the outer keyboard focus ring is separate.
        inner=indicator.adjusted(3,3,-3,-3);primary=QColor(color('primary'))
        ink=sum(abs(pixel.red()-primary.red())+abs(pixel.green()-primary.green())+abs(pixel.blue()-primary.blue())<45
                for x in range(round(inner.left()*raster_scale),round(inner.right()*raster_scale))
                for y in range(round(inner.top()*raster_scale),round(inner.bottom()*raster_scale))
                for pixel in [image.pixelColor(x,y)])
        return geometry,ink

    baseline,empty_ink=snapshot()
    checkbox.setFocus();focused,_=snapshot()
    QTest.mouseClick(checkbox,Qt.MouseButton.LeftButton,pos=QPoint(baseline[2][0]+baseline[2][2]//2,baseline[2][1]+baseline[2][3]//2))
    checked,checked_ink=snapshot();assert checkbox.isChecked()
    QTest.keyClick(checkbox,Qt.Key.Key_Space)
    unchecked,unselected_ink=snapshot();assert not checkbox.isChecked()
    draft.setFocus();blurred,_=snapshot()
    checkbox.setFocus();refocused,_=snapshot()
    assert all(value==baseline for value in (focused,checked,unchecked,blurred,refocused))
    assert checked_ink>empty_ink+4 and unselected_ink==empty_ink
    assert draft.text()=='Unsaved input'
