import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtWidgets import QApplication,QWidget,QPushButton
from PySide6.QtCore import QCoreApplication,QEvent
from management.gui_library import open_library
from management.core import Core
from test_ux_workflows_v2 import ControlledBridge,QueuedCoreBridge,wait
from test_plan_assistance_v8 import cmd
from management.gui_workspace import WorkspacePage
from management.schemas import TYPES

@pytest.fixture(scope='session')
def app():return QApplication.instance() or QApplication([])


def test_library_button_processes_pages_then_opens_one_folder(app,monkeypatch):
    from management import gui_library
    opened=[];monkeypatch.setattr(gui_library.QDesktopServices,'openUrl',lambda url:opened.append(url.toLocalFile()) or True)
    bridge=ControlledBridge();parent=QWidget();button=QPushButton('打开文件夹',parent);errors=[]
    operation=open_library(bridge,parent,button,'course',errors.append)
    assert not button.isEnabled()
    bridge.deliver('library_folder',{'processed':20,'total':25,'issues':[],'path':'D:/Synthetic','next_offset':20})
    app.processEvents()
    assert not opened and '20/25' in button.text()
    bridge.deliver('library_folder',{'processed':5,'total':25,'issues':[],'path':'D:/Synthetic','next_offset':None})
    assert opened==['D:/Synthetic'] and button.isEnabled() and button.text()=='打开文件夹' and not errors
    parent.deleteLater();app.processEvents()


def test_closed_workspace_does_not_receive_late_file_result(app):
    bridge=ControlledBridge();parent=QWidget();button=QPushButton('打开文件夹',parent)
    operation=open_library(bridge,parent,button,None,lambda e:None)
    query=bridge.take('library_folder');parent.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    query['callback']({'processed':0,'total':0,'issues':[],'path':'D:/Synthetic','next_offset':None})


def test_course_exposes_folder_entry_without_a_separate_files_page(app,tmp_path,monkeypatch):
    from management import gui_library
    opened=[];monkeypatch.setattr(gui_library.QDesktopServices,'openUrl',lambda url:opened.append(url.toLocalFile()) or True)
    core=Core(tmp_path);course=cmd(core,'create',{'type':'course','title':'Example course'})['entity'];bridge=QueuedCoreBridge(core)
    page=WorkspacePage(bridge,on_create=lambda *_:None,on_edit=lambda *_:None);page.set_types(TYPES);page.show();page.open_object(course)
    try:
        wait(app,lambda:bridge.pending==0)
        button=next(b for b in page.findChildren(QPushButton) if b.text()=='打开文件夹')
        button.click();wait(app,lambda:bool(opened))
        assert '原文件' in opened[0] and 'Example course' in opened[0]
    finally:page.close();page.deleteLater();app.processEvents()
