"""Source capture, bounded attachment replacement and persistent composer checks."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage,QColor
from PySide6.QtWidgets import QApplication,QDialog
from management.gui_assistant import AssistanceDialog
from management.gui_sources import AddSourceDialog,SourcePickerDialog,SourceContentDialog,extraction_label
from test_ux_workflows_v2 import ControlledBridge,QueuedCoreBridge,Core,cmd,wait,ready_conversation,conversation_result

@pytest.fixture(scope='session')
def app():return QApplication.instance() or QApplication([])

def test_notice_email_and_url_payloads_keep_owner_and_original_text(app):
    bridge=ControlledBridge();dialog=AddSourceDialog(bridge,'course-a')
    try:
        dialog.tabs.setCurrentIndex(1);dialog.text_kind.setCurrentIndex(1);dialog.text.setPlainText('From: synthetic\nExam date pending');dialog.title.setText('Course mail')
        assert dialog.build_payloads()==[{'owner_id':'course-a','kind':'email','text':'From: synthetic\nExam date pending','title':'Course mail'}]
        dialog.tabs.setCurrentIndex(3);dialog.url.setText('file:///C:/private')
        with pytest.raises(ValueError):dialog.build_payloads()
        dialog.url.setText('https://example.org/course')
        assert dialog.build_payloads()[0]['kind']=='web'
    finally:dialog.close()

def test_clipboard_is_only_read_on_click_and_png_survives_uncertain_save(app):
    bridge=ControlledBridge();image=QImage(32,24,QImage.Format.Format_RGB32);image.fill(QColor('green'));old=app.clipboard().image();app.clipboard().setImage(image)
    dialog=AddSourceDialog(bridge)
    try:
        assert dialog.temp_path is None
        dialog.tabs.setCurrentIndex(2);dialog.paste_image();path=Path(dialog.temp_path);assert path.exists()
        dialog.save();assert bridge.commands[0]['payload']['kind']=='image'
        bridge.commands[0]['error']({'code':'connection_lost','message':'Checking receipt'})
        assert path.exists() and not dialog.tabs.isEnabled()
        dialog.save();assert bridge.commands[-1]['payload']==bridge.commands[0]['payload']
        bridge.commands[-1]['callback']({'result':{'entity':{'id':'image-source','title':'Screenshot'}}})
        assert not path.exists() and dialog.result()==QDialog.DialogCode.Accepted
    finally:
        app.clipboard().setImage(old);dialog.close()

def test_mixed_file_batch_infers_email_and_retains_success_on_later_failure(app,tmp_path):
    bridge=ControlledBridge();a=tmp_path/'one.txt';b=tmp_path/'two.eml';a.write_text('one');b.write_text('Subject: two')
    saved=[];dialog=AddSourceDialog(bridge,'owner',on_saved=saved.append,paths=[str(a),str(b)])
    try:
        dialog.save();assert bridge.commands[0]['payload']['kind']=='file'
        bridge.commands[0]['callback']({'result':{'entity':{'id':'one'}}})
        assert bridge.commands[-1]['payload']['kind']=='email'
        bridge.commands[-1]['error']({'code':'source_error','message':'Retry second file'})
        assert len(saved)==1 and dialog.files.count()==1
        dialog.save();assert bridge.commands[-1]['payload']['path']==str(b)
    finally:
        dialog.pending=False;dialog.close()

def test_attachment_omission_reuses_but_removal_explicitly_clears(app):
    bridge=ControlledBridge();dialog=AssistanceDialog(bridge,prompt='Continue')
    try:
        bridge.deliver('settings',{'settings':{'ai':{'enabled':True}}})
        result=conversation_result();result['conversation'].update(source_ids=['source-a'],sources=[{'id':'source-a','title':'Saved notice'}]);result['messages']=[]
        bridge.deliver('conversation',result)
        dialog.start_job();assert 'source_ids' not in bridge.commands[-1]['payload']
        bridge.commands[-1]['error']({'code':'input','message':'Synthetic controlled rejection'})
        dialog.remove_source('source-a');dialog.start_job();assert bridge.commands[-1]['payload']['source_ids']==[]
    finally:dialog.close()

def test_explicit_organize_sources_replace_history_and_send_once_after_ready(app):
    bridge=ControlledBridge();dialog=AssistanceDialog(bridge,prompt='Organize',source_ids=['new-a'],auto_send=True)
    try:
        bridge.deliver('settings',{'settings':{'ai':{'enabled':True}}});assert not bridge.commands
        result=conversation_result();result['conversation']['source_ids']=['old-a'];result['messages']=[]
        bridge.deliver('conversation',result)
        assert len(bridge.commands)==1 and bridge.commands[0]['payload']['source_ids']==['new-a']
        bridge.commands[0]['callback']({'result':{'job':{'id':'active'}}})
        bridge.deliver('conversation',conversation_result(active='active'))
        dialog.maybe_auto_send();assert len(bridge.commands)==1
        assert dialog.send.text()=='停止';dialog.send.click();assert bridge.commands[-1]['name']=='cancel_job'
    finally:dialog.close()

def test_too_many_explicit_sources_are_not_silently_truncated(app):
    bridge=ControlledBridge();dialog=AssistanceDialog(bridge,prompt='Organize',source_ids=[str(i) for i in range(13)])
    try:
        ready_conversation(dialog,bridge);dialog.start_job();assert bridge.commands
        assert len(dialog.selected_ids)==13
        assert len(bridge.commands[-1]['payload']['source_ids'])==13
    finally:dialog.close()

def test_picker_preserves_selection_over_source_pages(app):
    bridge=ControlledBridge();dialog=SourcePickerDialog(bridge,'course-a',selected=[{'id':'prior','title':'Prior source'}])
    try:
        bridge.deliver('sources',{'items':[{'id':'one','title':'One','data':{'extraction':{'status':'partial'}}}],'next_offset':30})
        dialog.items.item(0).setCheckState(Qt.CheckState.Checked);dialog.load()
        bridge.deliver('sources',{'items':[{'id':'two','title':'Two'}],'next_offset':None})
        assert set(dialog.selected)=={'prior','one'} and dialog.items.count()==2
    finally:dialog.close()

def test_real_saved_notice_content_and_source_kind_roundtrip(app,tmp_path):
    core=Core(tmp_path/'source-ui');course=cmd(core,'create',{'type':'course','title':'Synthetic course','data':{}})['result']['entity'];bridge=QueuedCoreBridge(core)
    dialog=AddSourceDialog(bridge,course['id']);viewer=None
    try:
        dialog.tabs.setCurrentIndex(1);dialog.text.setPlainText('Synthetic announcement: deadline remains unknown.');dialog.save();wait(app,lambda:bridge.pending==0)
        assert dialog.result()==QDialog.DialogCode.Accepted
        source=core.query('sources',owner_id=course['id'])['items'][0]
        assert source['data']['managed_copy'] and source['data']['source_kind']=='notice'
        assert '待核对' in extraction_label(source)
        viewer=SourceContentDialog(bridge,source);wait(app,lambda:bridge.pending==0)
        assert 'deadline remains unknown' in viewer.output.toPlainText()
    finally:
        dialog.close()
        if viewer:viewer.close()


def test_history_window_is_bounded_and_old_pages_remain_reachable(app):
    bridge=ControlledBridge();dialog=AssistanceDialog(bridge)
    try:
        ready_conversation(dialog,bridge)
        for batch in range(5):
            dialog.poll()
            messages=[{'id':str(i),'seq':i,'role':'user','text':'Synthetic '+str(i),'proposal_state':'none'} for i in range(batch*50+1,batch*50+51)]
            bridge.deliver('conversation',{'conversation':None,'messages':messages,'has_more':True,'next_before':messages[0]['seq']})
        assert len(dialog.messages)==200
        dialog.poll(before=51)
        old=[{'id':str(i),'seq':i,'role':'user','text':'Old '+str(i)} for i in range(1,51)]
        bridge.deliver('conversation',{'conversation':None,'messages':old,'has_more':False})
        assert len(dialog.messages)==200 and dialog.showing_older and '1' in dialog.messages
        dialog.poll();bridge.deliver('conversation',{'conversation':None,'messages':[{'id':'new','seq':251,'role':'user','text':'New latest'}],'has_more':True,'next_before':251})
        assert 'new' not in dialog.messages and '1' in dialog.messages
        dialog.return_latest();bridge.deliver('conversation',{'conversation':None,'messages':[{'id':'new','seq':251,'role':'user','text':'New latest'}],'has_more':True,'next_before':251})
        assert list(dialog.messages)==['new'] and not dialog.showing_older
    finally:dialog.close()


def test_candidate_does_not_repeat_unknowns_already_in_assistant_reply(app):
    bridge=ControlledBridge();dialog=AssistanceDialog(bridge)
    try:
        dialog.job_id='candidate';dialog.messages={'reply':{'id':'reply','role':'assistant','job_id':'candidate','text':'待确认：考试日期未公布。'}}
        dialog.full_job={'result':{'unknowns':['考试日期未公布。','地点待确认。'],'actions':[]}}
        text=dialog.proposal_text();assert '考试日期未公布' not in text and '地点待确认' in text
    finally:dialog.close()


def test_recurring_candidate_preview_uses_reviewable_chinese_fields(app):
    bridge=ControlledBridge();dialog=AssistanceDialog(bridge)
    try:
        payload={'anchor_id':'internal-anchor-id','title':'课前练习','content':'完成章节练习','completion_gate':'完成三题并核对','estimated_minutes':None,'days_before':2,'enabled':False,'effective_from':'2030-01-07','effective_until':None,'source_text':'课程通知第2页'}
        dialog.full_job={'result':{'actions':[{'command':'set_recurring_rule','payload':payload,'reason':'依据用户明确要求'}]}}
        text=dialog.proposal_text()
        for phrase in ('固定准备事项 · 课前练习','提前出现：2 天','任务内容：完成章节练习','完成条件：完成三题并核对','预计用时：未知','生效起始：2030-01-07','生效结束：不额外限制','启用状态：停用','来源：课程通知第2页'):
            assert phrase in text
        for raw in ('anchor_id','internal-anchor-id','days_before','completion_gate','estimated_minutes','effective_until','source_text'):
            assert raw not in text
        assert bridge.commands==[]
    finally:dialog.close()


def test_recurring_candidate_update_does_not_invent_omitted_existing_settings(app):
    bridge=ControlledBridge();dialog=AssistanceDialog(bridge)
    try:
        dialog.full_job={'result':{'actions':[{'command':'set_recurring_rule','payload':{'id':'existing','version':2,'anchor_id':'anchor','content':'修订任务内容','days_before':0,'estimated_minutes':0,'source_text':'确认消息'}}]}}
        text=dialog.proposal_text()
        assert '提前出现：安排当天' in text and '预计用时：0 分钟' in text
        assert '完成条件：保持原设置' in text and '启用状态：保持原设置' in text
        assert '生效起始：保持原设置' in text
    finally:dialog.close()


def test_notes_shortcut_does_not_lock_later_discussion_into_skill(app):
    bridge=ControlledBridge();dialog=AssistanceDialog(bridge,intent='course_notes')
    try:
        ready_conversation(dialog,bridge);dialog.prompt.setPlainText('这是课件，整理笔记');dialog.start_job()
        assert bridge.commands[-1]['payload']['skill_id']=='course-notes'
        bridge.commands[-1]['callback']({'result':{'job':{'id':'notes-job'}}})
        bridge.deliver('conversation',conversation_result())
        dialog.prompt.setPlainText('现在请登记我落下的课程');dialog.start_job()
        assert 'skill_id' not in bridge.commands[-1]['payload']
        assert bridge.commands[-1]['payload']['text']=='现在请登记我落下的课程'
    finally:dialog.close()
