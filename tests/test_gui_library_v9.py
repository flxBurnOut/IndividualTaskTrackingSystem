import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtWidgets import QApplication,QWidget,QPushButton,QLabel,QDialog
from PySide6.QtCore import QCoreApplication,QEvent
from management.gui_library import open_library
from management.gui_sources import LibraryDestination,RefileSourceDialog,AddSourceDialog
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


def test_destination_pagination_preserves_typed_choice_and_requires_explicit_root(app):
    bridge=ControlledBridge();widget=LibraryDestination(bridge,owner_id='course')
    try:
        bridge.deliver('library_destinations',{'path':'D:/Synthetic','items':[{'subdir':'课件','label':'课件'}],
            'total':2,'next_offset':1,'requires_choice':True})
        with pytest.raises(ValueError):widget.value()
        widget.combo.setEditText('课件/补充资料');widget.more.click()
        bridge.deliver('library_destinations',{'path':'D:/Synthetic','items':[{'subdir':'通知','label':'通知'}],
            'total':2,'next_offset':None,'requires_choice':True},offset=1)
        assert widget.value()=='课件/补充资料' and widget.combo.findData('通知')>=0
        widget.combo.setCurrentIndex(widget.combo.findData(''));assert widget.value()==''
    finally:widget.close()


def test_refiling_uses_current_version_and_retries_uncertain_write_unchanged(app):
    bridge=ControlledBridge();saved=[]
    entity={'id':'source-a','version':1,'type':'asset','title':'Synthetic lesson',
        'data':{'sha256':'a'*64,'library_relative_path':'课程/Synthetic/lesson.txt'}}
    dialog=RefileSourceDialog(bridge,entity,on_saved=saved.append)
    try:
        bridge.deliver('library_destinations',{'owner_id':'course','path':'D:/Synthetic','items':[{'subdir':'课件','label':'课件'}],
            'total':1,'next_offset':None,'requires_choice':True,
            'current':{'id':'source-a','version':3,'library_subdir':'','path':'D:/Synthetic/lesson.txt','relative_path':'课程/Synthetic/lesson.txt'}})
        dialog.destination.combo.setCurrentIndex(dialog.destination.combo.findData('课件'));dialog.save()
        first=bridge.commands[-1]
        assert first['name']=='refile_source' and first['payload']=={'id':'source-a','version':3,'library_subdir':'课件'}
        first['error']({'code':'connection_lost','message':'Result unknown'})
        assert not dialog.destination.isEnabled() and not dialog.completed
        dialog.destination.combo.setEditText('changed while disabled');dialog.save()
        assert bridge.commands[-1]['payload']==first['payload'] and bridge.commands[-1]['options']==first['options']
        bridge.commands[-1]['callback']({'result':{'entity':{**entity,'version':4},
            'library':{'path':'D:/Synthetic/课件/lesson.txt','relative_path':'课程/Synthetic/课件/lesson.txt','exists':True},
            'previous_relative_path':'课程/Synthetic/lesson.txt'},
            'archive_cleanup':{'status':'retained_modified','message':'旧位置原件已有修改，已保留供核对。'}})
        assert dialog.completed and len(saved)==1
        assert 'D:/Synthetic/课件/lesson.txt' in dialog.locations.toPlainText()
        assert '旧位置原件已有修改' in dialog.locations.toPlainText()
        dialog.save();assert dialog.result()==QDialog.DialogCode.Accepted
    finally:dialog.pending=False;dialog.uncertain=False;dialog.close()


def test_failed_refiling_keeps_dialog_editable_without_success_claim(app):
    bridge=ControlledBridge();dialog=RefileSourceDialog(bridge,{'id':'source-a','version':1,'title':'Synthetic','data':{}})
    try:
        bridge.deliver('library_destinations',{'path':'D:/Synthetic','items':[],'total':0,'next_offset':None,
            'current':{'id':'source-a','version':1,'library_subdir':'','path':'D:/Synthetic/source.txt','relative_path':'课程/Synthetic/source.txt'}})
        dialog.destination.combo.setEditText('课件');dialog.save()
        bridge.commands[-1]['error']({'code':'edited_copy','message':'原件已有修改，未移动。'})
        assert dialog.destination.isEnabled() and not dialog.completed and not dialog.locations.toPlainText()
        assert '未移动' in dialog.status.text()
    finally:dialog.close()


@pytest.mark.parametrize('first_error',['connection_lost','operation_pending','connection_error',
    'protocol_error','response_limit','internal_error','storage_error'])
def test_unknown_refile_keeps_original_request_after_auth_and_epoch_failures(app,first_error):
    bridge=ControlledBridge();dialog=RefileSourceDialog(bridge,{'id':'source-a','version':1,'title':'Synthetic','data':{}})
    dialog.show()
    try:
        bridge.deliver('library_destinations',{'path':'D:/Synthetic','items':[],'total':0,'next_offset':None,
            'current':{'id':'source-a','version':1,'library_subdir':'','path':'D:/Synthetic/source.txt','relative_path':'课程/Synthetic/source.txt'}})
        dialog.destination.combo.setEditText('课件');dialog.save();original=bridge.commands[-1]
        original['error']({'code':first_error,'message':'Result unknown'})
        assert dialog.uncertain and not dialog.close() and dialog.isVisible()
        dialog.reject();assert dialog.isVisible() and '不能关闭' in dialog.status.text()
        dialog.destination.current.update(id='other-source',version=99)
        dialog.destination.combo.setEditText('Other folder');dialog.epoch='changed-epoch';bridge.revision=99
        for code in ('unauthorized','revision_conflict','epoch_conflict','service_version_mismatch'):
            dialog.save();retry=bridge.commands[-1]
            assert retry['payload']==original['payload'] and retry['options']==original['options']
            retry['error']({'code':code,'message':'Later failure'})
            assert dialog.uncertain and not dialog.destination.isEnabled() and not dialog.close()
        dialog.save();bridge.commands[-1]['callback']({'result':{'entity':{'id':'source-a','version':2},
            'library':{'path':'D:/Synthetic/课件/source.txt','exists':True}},'archive_cleanup':{'status':'removed'}})
        assert dialog.completed and not dialog.uncertain
        dialog.save();assert dialog.result()==QDialog.DialogCode.Accepted
    finally:
        dialog.pending=False;dialog.uncertain=False;dialog.close()


def test_pending_cleanup_retries_same_request_and_notifies_saved_only_once(app):
    bridge=ControlledBridge();saved=[];entity={'id':'source-a','version':1,'title':'Synthetic','data':{}}
    dialog=RefileSourceDialog(bridge,entity,on_saved=saved.append);dialog.show()
    try:
        bridge.deliver('library_destinations',{'path':'D:/Synthetic','items':[],'total':0,'next_offset':None,
            'current':{'id':'source-a','version':1,'library_subdir':'','path':'D:/Synthetic/source.txt','relative_path':'课程/Synthetic/source.txt'}})
        dialog.destination.combo.setEditText('课件');dialog.save();original=bridge.commands[-1]
        receipt={'result':{'entity':{**entity,'version':2},'library':{'path':'D:/Synthetic/课件/source.txt','exists':True}},
            'archive_cleanup':{'status':'pending','message':'旧副本暂被占用，清理待重试。'}}
        original['callback'](receipt)
        assert dialog.completed and dialog.cleanup_pending and len(saved)==1
        assert dialog.save_button.text()=='重试旧副本清理' and not dialog.destination.isEnabled()
        dialog.destination.combo.setEditText('Another folder');dialog.destination.current['version']=88;bridge.revision=99
        for code in ('unauthorized','protocol_error','epoch_conflict'):
            dialog.save();retry=bridge.commands[-1]
            assert retry['payload']==original['payload'] and retry['options']==original['options']
            retry['error']({'code':code,'message':'Cleanup retry failed'})
            assert dialog.cleanup_pending and len(saved)==1 and not dialog.destination.isEnabled()
        assert dialog.uncertain and not dialog.close()
        dialog.save();bridge.commands[-1]['callback']({**receipt,'archive_cleanup':{'status':'removed'}})
        assert not dialog.cleanup_pending and not dialog.uncertain and len(saved)==1
        assert dialog.save_button.text()=='完成' and '清理待重试' not in dialog.locations.toPlainText()
        dialog.save();assert dialog.result()==QDialog.DialogCode.Accepted
    finally:
        dialog.pending=False;dialog.uncertain=False;dialog.close()


@pytest.mark.parametrize('cleanup',[None,{'status':'unrecognized'}])
def test_recovered_receipt_without_known_cleanup_keeps_original_retry(app,cleanup):
    bridge=ControlledBridge();saved=[];entity={'id':'source-a','version':1,'title':'Synthetic','data':{}}
    dialog=RefileSourceDialog(bridge,entity,on_saved=saved.append)
    try:
        bridge.deliver('library_destinations',{'path':'D:/Synthetic','items':[],'total':0,'next_offset':None,
            'current':{'id':'source-a','version':1,'library_subdir':'','path':'D:/Synthetic/source.txt','relative_path':'课程/Synthetic/source.txt'}})
        dialog.destination.combo.setEditText('课件');dialog.save();original=bridge.commands[-1]
        # Mirrors the bridge's automatic receipt recovery after losing the
        # command response: the durable result lacks post-commit cleanup data.
        recovered={'request_id':original['options']['request_id'],'replayed':True,
            'result':{'entity':{**entity,'version':2},'library':{'path':'D:/Synthetic/课件/source.txt','exists':True}}}
        if cleanup is not None:recovered['archive_cleanup']=cleanup
        original['callback'](recovered)
        assert dialog.completed and dialog.cleanup_pending and len(saved)==1
        assert dialog.save_button.text()=='重试旧副本清理' and '尚未确认完成' in dialog.status.text()
        dialog.destination.combo.setEditText('Another folder');bridge.revision=99
        dialog.save();retry=bridge.commands[-1]
        assert retry['payload']==original['payload'] and retry['options']==original['options']
        retry['callback']({**recovered,'archive_cleanup':{'status':'removed'}})
        assert dialog.completed and not dialog.cleanup_pending and len(saved)==1
        assert dialog.save_button.text()=='完成'
    finally:
        dialog.pending=False;dialog.uncertain=False;dialog.close()


def test_course_displays_archive_path_and_dragged_files_use_destination_dialog(app,tmp_path):
    core=Core(tmp_path/'data');course=cmd(core,'create',{'type':'course','title':'Synthetic course'})['entity']
    source=tmp_path/'lesson.txt';source.write_text('Synthetic lesson')
    asset=cmd(core,'add_source',{'owner_id':course['id'],'kind':'file','path':str(source),'library_subdir':''})['entity']
    bridge=QueuedCoreBridge(core);page=WorkspacePage(bridge,on_create=lambda *_:None,on_edit=lambda *_:None)
    page.set_types(TYPES);page.show();page.open_object(course)
    try:
        wait(app,lambda:bridge.pending==0)
        assert any(asset['data']['library_relative_path'] in label.text() for label in page.findChildren(QLabel))
        move=next(button for button in page.findChildren(QPushButton) if button.text()=='移动原件…')
        move.click();wait(app,lambda:bridge.pending==0)
        dialog=next(dialog for dialog in page.dialogs if isinstance(dialog,RefileSourceDialog))
        assert dialog.destination.current['id']==asset['id'];dialog.close()
        before=len(bridge.commands);page.attach_paths([str(source)]);wait(app,lambda:bridge.pending==0)
        added=next(dialog for dialog in page.dialogs if isinstance(dialog,AddSourceDialog))
        assert added.files.count()==1 and len(bridge.commands)==before
        added.close()
    finally:
        for dialog in list(page.dialogs):dialog.close()
        page.close();page.deleteLater();app.processEvents()
