"""Live chat shows acknowledged facts, with no real Codex/model calls."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import copy
import pytest
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication
from management.gui_assistant import AssistanceDialog
from test_ux_workflows_v2 import ControlledBridge,ready_conversation

THREAD='00000000-0000-4000-8000-000000000101'

@pytest.fixture(scope='session')
def app():return QApplication.instance() or QApplication([])

@pytest.fixture
def chat(app):
    bridge=ControlledBridge();dialog=AssistanceDialog(bridge,prompt='Synthetic update')
    dialog.show();dialog.timer.stop();ready_conversation(dialog,bridge)
    app.processEvents();dialog.timer.stop()
    yield dialog,bridge
    dialog.close();dialog.deleteLater();app.processEvents()


def message(identifier='user-a',text='Synthetic update',role='user',seq=1,job='job-a'):
    return {'id':identifier,'text':text,'role':role,'seq':seq,'job_id':job,'proposal_state':'none','source_ids':[]}


def receipt(msg=None):
    return {'result':{'conversation':{'id':'conversation-a','active_job_id':'job-a','provider_thread_id':THREAD},'job':{'id':'job-a'},'user_message':msg or message()}}


def snapshot(active='job-a',preview='',phase='waiting_model',sequence=1,messages=None,elapsed=7):
    return {'conversation':{'id':'conversation-a','active_job_id':active,'provider_thread_id':THREAD,'source_ids':[],'sources':[]},
            'messages':messages or [message()], 'has_more':False,
            'active_progress':{'job_id':active,'status':'running','phase':phase,'sequence':sequence,'elapsed_seconds':elapsed,'preview_text':preview,'provider_thread_id':THREAD} if active else None}


def deliver_poll(dialog,bridge,result):
    if not dialog.pending:dialog.poll()
    bridge.deliver('conversation',result);dialog.timer.stop()


def test_send_immediately_shows_one_bubble_and_receipt_adopts_it(chat):
    dialog,bridge=chat
    dialog.messages={'old':message('old','Earlier context','assistant',0,None)};dialog.render_history();old=dialog.cards['old']
    dialog.start_job();temporary=dialog.outgoing['id'];bubble=dialog.cards[temporary]
    assert bubble.body.toPlainText()=='Synthetic update'
    assert '正在发送' in bubble.note.text() and dialog.prompt.toPlainText()=='Synthetic update'
    dialog.start_job();assert len(bridge.commands)==1
    bridge.commands[-1]['callback'](receipt())
    assert dialog.cards['user-a'] is bubble and temporary not in dialog.cards
    assert dialog.cards['old'] is old and dialog.prompt.toPlainText()==''
    assert sum(card.property('userMessage') is True for card in dialog.cards.values())==1


def test_definitive_send_failure_preserves_text_and_explicit_retry_reuses_bubble(chat):
    dialog,bridge=chat;dialog.start_job();key=dialog.outgoing['id'];card=dialog.cards[key]
    bridge.commands[-1]['error']({'code':'validation','message':'Synthetic rejection'})
    assert '发送失败' in card.note.text() and dialog.prompt.toPlainText()=='Synthetic update'
    assert len(bridge.commands)==1
    dialog.start_job();assert dialog.cards[key] is card and len(bridge.commands)==2
    bridge.commands[-1]['callback'](receipt());assert dialog.cards['user-a'] is card


def test_uncertain_send_recovers_receipt_without_resending_or_duplicates(chat):
    dialog,bridge=chat;dialog.start_job();bubble=dialog.cards[dialog.outgoing['id']]
    bridge.commands[-1]['error']({'code':'connection_lost','message':'Connection interrupted','details':{'request_id':'request-a'}})
    assert '结果待确认' in bubble.note.text() and dialog.prompt.toPlainText()=='Synthetic update'
    deliver_poll(dialog,bridge,{'conversation':None,'messages':[],'has_more':False})
    bridge.deliver('receipt',{'found':True,'receipt':receipt()})
    assert len(bridge.commands)==1 and dialog.cards['user-a'] is bubble and dialog.outgoing is None
    assert dialog.prompt.toPlainText()==''


def test_stream_is_plain_text_and_keeps_old_bubbles_and_selections(chat):
    dialog,bridge=chat;dialog.start_job();bridge.commands[-1]['callback'](receipt())
    deliver_poll(dialog,bridge,snapshot(preview='<b>Partial',phase='receiving'))
    card=dialog.cards['live:job-a'];old=dialog.cards['user-a'];cursor=old.body.textCursor();cursor.setPosition(0);cursor.setPosition(9,QTextCursor.MoveMode.KeepAnchor);old.body.setTextCursor(cursor)
    deliver_poll(dialog,bridge,snapshot(preview='<b>Partial answer</b>',phase='receiving',sequence=2))
    assert dialog.cards['live:job-a'] is card and dialog.cards['user-a'] is old
    assert card.body.toPlainText()=='<b>Partial answer</b>'
    assert old.body.textCursor().selectedText()=='Synthetic'
    assert '尚未完成或保存' in card.note.text() and '接收回复' in dialog.status.text()


def test_final_message_replaces_partial_and_stops_active_poll_frequency(chat):
    dialog,bridge=chat;dialog.start_job();bridge.commands[-1]['callback'](receipt())
    deliver_poll(dialog,bridge,snapshot(preview='Partial reply',phase='receiving'))
    assert dialog.timer.interval()==500
    final=message('final','Complete reply','assistant',2)
    deliver_poll(dialog,bridge,snapshot(active=None,messages=[message(),final]))
    assert 'live:job-a' not in dialog.cards and dialog.cards['final'].body.toPlainText()=='Complete reply'
    assert dialog.timer.interval()==1500 and dialog.send.text()=='发送'


def test_failed_partial_is_explicitly_unfinished_and_never_a_proposal(chat):
    dialog,bridge=chat
    result=snapshot(active=None,messages=[message(),message('failure','处理失败','assistant',2)])
    result['latest_progress']={'job_id':'job-a','status':'failed','phase':'failed','preview_text':'Incomplete suggestion'}
    deliver_poll(dialog,bridge,result)
    card=dialog.cards['live:job-a']
    assert card.body.toPlainText()=='Incomplete suggestion' and '未完成' in card.note.text() and '未保存' in card.note.text()
    assert not dialog.apply.isEnabled() and dialog.active_job_id is None


def test_reopened_window_recovers_persisted_live_progress_and_no_hidden_ids(app):
    bridge=ControlledBridge();opened=[];dialog=AssistanceDialog(bridge,on_open_codex_thread=opened.append)
    try:
        dialog.show();bridge.deliver('settings',{'settings':{'ai':{'enabled':True}}});bridge.deliver('conversation',snapshot(preview='Persisted partial',phase='receiving',elapsed=72));dialog.timer.stop()
        assert 'Persisted partial'==dialog.cards['live:job-a'].body.toPlainText()
        assert '1 分 12 秒' in dialog.status.text()
        assert dialog.open_codex.isVisible() and not dialog.open_codex.isEnabled()
        dialog.open_codex_thread();assert opened==[]
        deliver_poll(dialog,bridge,snapshot(active=None));assert dialog.open_codex.isEnabled()
        dialog.open_codex.click();assert opened==[THREAD]
        assert THREAD not in dialog.open_codex.text() and THREAD not in dialog.status.text()
    finally:dialog.close()


def test_poll_requests_do_not_overlap_and_old_result_cannot_replace_new_send(chat):
    dialog,bridge=chat;dialog.poll();old=bridge.take('conversation')
    count=len(bridge.queries)
    dialog.poll(force=True);dialog.poll(force=True);dialog.poll();assert len(bridge.queries)==count
    dialog.start_job();bridge.commands[-1]['callback'](receipt())
    assert len(bridge.queries)==count
    old['callback'](snapshot(active=None,messages=[message('stale','stale','assistant',0,None)]))
    assert 'stale' not in dialog.messages and dialog.active_job_id=='job-a'
    assert len(bridge.queries)==count+1
    bridge.deliver('conversation',snapshot(preview='Newest response'));assert dialog.active_progress['preview_text']=='Newest response'


def test_epoch_change_keeps_draft_and_rejects_stale_send_receipt(chat):
    dialog,bridge=chat;dialog.start_job();bridge.epoch='new-epoch'
    bridge.commands[-1]['callback'](receipt())
    assert dialog.epoch_invalid and dialog.prompt.toPlainText()=='Synthetic update'
    assert not dialog.send.isEnabled() and '数据空间已切换' in dialog.status.text()
    assert dialog.active_job_id is None


def test_old_send_callback_cannot_replace_newer_send(chat):
    dialog,bridge=chat;dialog.start_job();first=bridge.commands[-1]
    first['error']({'code':'validation','message':'Rejected'})
    dialog.prompt.setPlainText('Changed message');dialog.start_job();second=bridge.commands[-1]
    first['callback'](receipt());assert dialog.active_job_id is None
    second['callback'](receipt(message(text='Changed message')))
    assert dialog.cards['user-a'].body.toPlainText()=='Changed message'


def test_polling_preserves_scrolled_history_and_selection(chat,app):
    dialog,bridge=chat
    history=[message(str(i),'Historic message '+str(i)+'\n'+'detail '*35,'assistant',i,None) for i in range(1,28)]
    deliver_poll(dialog,bridge,snapshot(messages=history,preview='Partial',phase='receiving'));app.processEvents();app.processEvents()
    bar=dialog.history.verticalScrollBar();assert bar.maximum()>300
    bar.setValue(300);selected=dialog.cards['4'];cursor=selected.body.textCursor();cursor.select(QTextCursor.SelectionType.LineUnderCursor);selected.body.setTextCursor(cursor);text=cursor.selectedText()
    for seq in range(2,6):
        deliver_poll(dialog,bridge,snapshot(messages=history,preview='Partial '+('result '*seq),phase='receiving',sequence=seq));app.processEvents();app.processEvents()
    assert dialog.cards['4'] is selected and selected.body.textCursor().selectedText()==text
    assert abs(bar.value()-300)<3


def test_lower_progress_sequence_cannot_roll_back_preview(chat):
    dialog,bridge=chat
    deliver_poll(dialog,bridge,snapshot(preview='Latest reply',sequence=5))
    deliver_poll(dialog,bridge,snapshot(preview='Stale reply',sequence=2))
    assert dialog.cards['live:job-a'].body.toPlainText()=='Latest reply'


def test_uncertain_receipt_after_persisted_message_never_adds_duplicate_user_bubble(chat):
    dialog,bridge=chat;dialog.start_job()
    bridge.commands[-1]['error']({'code':'connection_lost','message':'Interrupted','details':{'request_id':'request-a'}})
    deliver_poll(dialog,bridge,snapshot())
    assert sum(card.property('userMessage') is True for card in dialog.cards.values())==1
    assert dialog.outgoing and dialog.outgoing['delivery']=='uncertain'
    bridge.deliver('receipt',{'found':True,'receipt':receipt()})
    assert sum(card.property('userMessage') is True for card in dialog.cards.values())==1
    assert len(bridge.commands)==1 and dialog.outgoing is None

def shared_mode(dialog,bridge,opener):
    dialog.on_open_codex_thread=opener;dialog.load_settings()
    bridge.deliver('settings',{'settings':{'ai':{'enabled':True,'execution_mode':'desktop_shared'}}})
    bridge.deliver('codex_connection',{'ready':True,'state':'ready'})
    dialog.timer.stop()


def test_shared_mode_focuses_once_only_after_turn_ack_for_locally_sent_job(chat):
    dialog,bridge=chat;opened=[];shared_mode(dialog,bridge,opened.append)
    dialog.start_job();bridge.commands[-1]['callback'](receipt())
    deliver_poll(dialog,bridge,snapshot(phase='thread_ready'))
    assert opened==[] and dialog.open_codex.isEnabled()
    running=snapshot(preview='Progress in desktop',phase='receiving',sequence=2)
    running['active_progress']['provider_turn_id']='turn-a'
    deliver_poll(dialog,bridge,running)
    assert opened==[THREAD] and dialog.auto_focus_job is None
    deliver_poll(dialog,bridge,running);dialog.tick()
    assert opened==[THREAD] and dialog.open_codex.text()=='在 Codex 查看'


def test_shared_existing_or_reopened_running_thread_never_steals_focus(app):
    bridge=ControlledBridge();opened=[];dialog=AssistanceDialog(bridge,on_open_codex_thread=opened.append)
    try:
        dialog.show();bridge.deliver('settings',{'settings':{'ai':{'enabled':True,'execution_mode':'desktop_shared'}}})
        running=snapshot(preview='Already running');running['active_progress']['provider_turn_id']='turn-existing'
        bridge.deliver('conversation',running);dialog.timer.stop()
        assert opened==[] and dialog.open_codex.isEnabled()
        dialog.open_codex.click();assert opened==[THREAD]
    finally:dialog.close()


def test_shared_followup_same_thread_does_not_refocus(chat):
    dialog,bridge=chat;opened=[];shared_mode(dialog,bridge,opened.append)
    dialog.start_job();bridge.commands[-1]['callback'](receipt())
    first=snapshot();first['active_progress']['provider_turn_id']='turn-a';deliver_poll(dialog,bridge,first)
    assert opened==[THREAD]
    deliver_poll(dialog,bridge,snapshot(active=None))
    dialog.prompt.setPlainText('Follow-up update');dialog.start_job();bridge.commands[-1]['callback'](receipt(message(text='Follow-up update')))
    next_turn=snapshot();next_turn['active_progress']['provider_turn_id']='turn-b';deliver_poll(dialog,bridge,next_turn)
    assert opened==[THREAD] and len(bridge.commands)==2


def test_shared_navigation_failure_is_visible_without_failing_or_resending_job(chat):
    dialog,bridge=chat;attempts=[]
    def cannot_open(identifier):attempts.append(identifier);raise RuntimeError('Synthetic navigation rejection')
    shared_mode(dialog,bridge,cannot_open);dialog.start_job();bridge.commands[-1]['callback'](receipt())
    running=snapshot(phase='receiving');running['active_progress']['provider_turn_id']='turn-a';deliver_poll(dialog,bridge,running)
    assert dialog.active_job_id=='job-a' and '导航到 Codex 失败' in dialog.status.text()
    assert 'Synthetic navigation rejection' in dialog.status.text() and len(bridge.commands)==1
    deliver_poll(dialog,bridge,running)
    assert attempts==[THREAD] and '导航到 Codex 失败' in dialog.status.text()
    dialog.open_codex.click();assert attempts==[THREAD,THREAD] and len(bridge.commands)==1


def test_shared_backend_connection_error_is_shown_without_fabricated_running_state(chat):
    dialog,bridge=chat;opened=[];shared_mode(dialog,bridge,opened.append)
    dialog.start_job();bridge.commands[-1]['error']({'code':'desktop_bridge_unavailable','message':'Codex 桌面共享连接尚未开启，请完成配置并重启 Codex。'})
    assert opened==[] and dialog.active_job_id is None and '共享连接尚未开启' in dialog.status.text()
    assert dialog.prompt.toPlainText()=='Synthetic update'
