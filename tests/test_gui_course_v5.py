"""Course record editing, dated preparation gates, note cards and directory state."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import QDate,Qt,QUrl
from PySide6.QtWidgets import QApplication,QPushButton,QLabel,QTreeWidgetItem,QFrame
from management.gui_workspace import WorkspacePage,TaskDetailDialog,NoteReader,has_preparation_date,preparation_label
from management.gui_forms import EntityForm
from management.gui_recurring import RecurringDialog
from management.gui_theme import apply_appearance
from management.schemas import TYPES
from management.core import Core
from test_ux_workflows_v2 import ControlledBridge,QueuedCoreBridge,cmd,wait

@pytest.fixture(scope='session')
def app():return QApplication.instance() or QApplication([])

def entity(kind,id,title,data=None,parent_id=None):return {'type':kind,'id':id,'title':title,'data':data or {},'parent_id':parent_id,'version':1,'status':'active','archived':False}
def words(widget):return '\n'.join(w.text() for w in widget.findChildren(QLabel))+ '\n'+'\n'.join(w.text() for w in widget.findChildren(QPushButton))
def render_course(page,children,events=None,assessments=None):
    course=entity('course','course','Synthetic course')
    page.set_types(TYPES);page.render({'entity':course,'children':children,'task_states':{},'summary':{},'files':[],'course_info':{'events':events or [],'assessments':assessments or []}});return course


def test_undated_existing_node_keeps_source_and_has_edit_not_preparation(app):
    bridge=ControlledBridge();edits=[];page=WorkspacePage(bridge,on_edit=edits.append,on_create=lambda *_:None)
    old=entity('milestone','old','First week preparation requirement',{'source_text':'Synthetic lecture page 1','notes':'An existing requirement; date unknown'},'course')
    try:
        render_course(page,[old])
        assert '交付与重要日期' in words(page) and '尚未定日期' in words(page) and 'Synthetic lecture page 1' in words(page)
        assert not [b for b in page.findChildren(QPushButton) if b.text()=='提前准备（一次）']
        edit=next(b for b in page.findChildren(QPushButton) if b.text()=='编辑说明与日期');edit.click()
        assert edits==[old] and bridge.commands==[]
        page.open_recurring(old);assert bridge.commands==[] and not page.dialogs
    finally:page.close()


def test_dated_node_and_recurring_event_have_distinct_preparation_semantics(app):
    dated=entity('milestone','m','Submit report',{'due_date':'2030-01-07'})
    recurring=entity('event','e','Weekly course',{'date':'2030-01-09','recurrence':'weekly'})
    once=entity('event','single','One meeting',{'date':'2030-01-09','recurrence':'none'})
    assert has_preparation_date(dated) and preparation_label(dated)=='提前准备（一次）'
    assert preparation_label(recurring)=='每次日程前准备' and preparation_label(once)=='提前准备（一次）'
    assert not has_preparation_date(entity('milestone','bad','Bad date',{'due_date':'unknown'}))
    for anchor,word in ((dated,'只针对这次明确日期'),(recurring,'每次日程分别生成')):
        dialog=RecurringDialog(ControlledBridge(),anchor)
        try:assert word in words(dialog)
        finally:dialog.close()


def test_course_rows_expose_specific_edit_and_context_create_actions(app):
    bridge=ControlledBridge();edits=[];creates=[];page=WorkspacePage(bridge,on_edit=edits.append,on_create=lambda *v:creates.append(v))
    records=[entity('task','t','Do practice'),entity('milestone','m','Report',{'due_date':'2030-01-12'}),entity('note','n','Notes',{'content':'A compact retained note'})]
    event=entity('event','e','Class',{'date':'2030-01-09','recurrence':'weekly','owner_id':'course'})
    assessment=entity('assessment','a','Exam weighting',{'weight':40})
    try:
        course=render_course(page,records,[event],[assessment])
        for caption in ('编辑任务','编辑日程','编辑评分','编辑笔记','编辑说明与日期'):
            next(b for b in page.findChildren(QPushButton) if b.text()==caption).click()
        assert {item['id'] for item in edits}=={'t','e','a','n','m'}
        next(b for b in page.findChildren(QPushButton) if b.text()=='＋ 新建交付 / 检查点').click()
        assert creates==[('milestone',course)] and bridge.commands==[]
    finally:page.close()


def test_note_cards_route_to_course_skill_without_writing_notes(app):
    bridge=ControlledBridge();calls=[];page=WorkspacePage(bridge,on_edit=lambda *_:None,on_create=lambda *_:None,on_codex=lambda *args,**kwargs:calls.append((args,kwargs)))
    note=entity('note','n','Course notes',{'content':'Original content stays unchanged. '*30},'course')
    try:
        course=render_course(page,[note])
        assert 'Original content stays unchanged.' in words(page)
        next(b for b in page.findChildren(QPushButton) if b.text()=='请助手整理课件笔记').click()
        assert calls==[((course,),{'intent':'course_notes'})] and bridge.commands==[]
        page.discuss_note(note);bridge.deliver('get',{'entity':course})
        assert calls[-1]==((course,),{'intent':'course_notes'})
    finally:page.close()


def test_note_reader_blocks_resource_fetch_and_does_not_introduce_rich_storage(app):
    bridge=ControlledBridge();note=entity('note','n','Read only',{'content':'# Heading\n\n- retained item\n\n![remote](https://example.com/a.png)'})
    dialog=TaskDetailDialog(bridge,note,on_edit=lambda _:None)
    try:
        reader=dialog.findChild(NoteReader);assert reader
        assert 'Heading' in reader.toPlainText() and 'retained item' in reader.toPlainText()
        assert reader.loadResource(2,QUrl('https://example.com/a.png')) is None
        assert reader.loadResource(2,QUrl.fromLocalFile('C:/private.png')) is None
        assert bridge.commands==[] and set(note['data'])=={'content'}
    finally:dialog.close()


def test_directory_collapse_preserves_selection_and_setter_never_emits(app):
    bridge=ControlledBridge();page=WorkspacePage(bridge);events=[];page.sidebar_collapsed_changed.connect(events.append)
    item=QTreeWidgetItem(['Synthetic course']);item.setData(0,Qt.ItemDataRole.UserRole,entity('course','course','Synthetic course'));page.tree.addTopLevelItem(item);page.tree.setCurrentItem(item)
    try:
        page.set_sidebar_collapsed(True);assert events==[] and page.tree.currentItem() is item and not page.expand_directory_button.isHidden()
        page.toggle_sidebar(False);assert events==[False] and page.tree.currentItem() is item
        apply_appearance(app,{'theme':'dark','font_size':20});app.processEvents()
        assert item.sizeHint(0).height()>=page.tree.fontMetrics().height()+20
    finally:page.close();apply_appearance(app,{'theme':'light','font_size':13})


def test_real_course_schedule_edit_preserves_owner_exceptions_and_source(app,tmp_path):
    core=Core(tmp_path/'event-edit');course=cmd(core,'create',{'type':'course','title':'Course','data':{}})['result']['entity']
    event=cmd(core,'create',{'type':'event','title':'Weekly class','data':{'owner_id':course['id'],'date':'2030-01-09','start':'10:00','end':'11:00','recurrence':'weekly','until':'2030-03-01','location':'Room A','source_text':'Initial notice','exceptions':{'2030-01-16':{'cancelled':True}}}})['result']['entity']
    bridge=QueuedCoreBridge(core);form=EntityForm(bridge,core.query('capabilities'),entity=event)
    try:
        for name in ('recurrence','until','location','notes','source_text'):assert form.field_layout.isRowVisible(form.fields[name])
        form.fields['start'].editor.setText('11:00');form.fields['end'].editor.setText('12:00');form.fields['location'].editor.setText('Room B');form.save();wait(app,lambda:bridge.pending==0)
        updated=core.query('get',id=event['id'])['entity'];assert updated['data']['start']=='11:00' and updated['data']['location']=='Room B'
        assert updated['data']['owner_id']==course['id'] and updated['data']['exceptions']==event['data']['exceptions']
        assert updated['data']['source_text']=='Initial notice' and updated['parent_id'] is None
    finally:form.close()


def test_real_assessment_and_node_edit_keep_unshown_confirmed_fields(app,tmp_path):
    core=Core(tmp_path/'record-edit');course=cmd(core,'create',{'type':'course','title':'Course','data':{}})['result']['entity']
    assessment=cmd(core,'create',{'type':'assessment','title':'Project','parent_id':course['id'],'data':{'weight':40,'score':17,'maximum':20,'source_text':'Official notice'}})['result']['entity']
    node=cmd(core,'create',{'type':'milestone','title':'First week preparation','parent_id':course['id'],'data':{'notes':'Original requirement','source_text':'Page 1','custom_fact':'Retain'}})['result']['entity']
    for original in (assessment,node):
        bridge=QueuedCoreBridge(core);form=EntityForm(bridge,core.query('capabilities'),entity=original)
        try:
            form.title_edit.setText(original['title']+' clarified')
            if original['type']=='assessment':form.fields['weight'].editor.setValue(35)
            else:
                form.fields['source_text'].editor.setPlainText('Page 1, confirmed as one delivery')
                form.fields['due_date'].enabled.setChecked(True);form.fields['due_date'].editor.setDate(QDate(2030,1,12))
            form.save();wait(app,lambda:bridge.pending==0)
            updated=core.query('get',id=original['id'])['entity'];assert updated['type']==original['type'] and updated['parent_id']==course['id']
            if original['type']=='assessment':assert updated['data']['score']==17 and updated['data']['maximum']==20 and updated['data']['weight']==35
            else:assert updated['data']['custom_fact']=='Retain' and updated['data']['due_date']=='2030-01-12'
        finally:form.close()


def test_nested_note_discussion_follows_its_real_course_not_current_selection(app):
    bridge=ControlledBridge();calls=[];page=WorkspacePage(bridge,on_codex=lambda *args,**kwargs:calls.append((args,kwargs)))
    note=entity('note','nested','Nested note',{'content':'Retained'},'task-owner')
    try:
        page.current_entity=entity('course','unrelated','Other course')
        page.discuss_note(note)
        bridge.deliver('get',{'entity':entity('task','task-owner','Own task',parent_id='real-course')},id='task-owner')
        bridge.deliver('get',{'entity':entity('course','real-course','Correct course')},id='real-course')
        assert calls[0][0][0]['id']=='real-course' and calls[0][1]=={'intent':'course_notes'}
        assert bridge.commands==[]
    finally:page.close()
