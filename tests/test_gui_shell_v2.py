"""Real-service acceptance paths for the redesigned three-workspace UI."""
from __future__ import annotations
import os
import json
from pathlib import Path
import subprocess
import sys
import time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import QDate, QEvent, QMimeData, QPoint, QPointF, Qt, QUrl, QTimer
from PySide6.QtGui import QDropEvent, QDesktopServices, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QDialogButtonBox, QMessageBox, QScrollArea, QWidget, QLayout
from management.client import Client
from management.gui import MainWindow, STYLESHEET
from management.gui_workspace import CountProgress


def _text_layout_issues(host):
    """Check each label at its rendered width, including scrollable content.

    Container size hints alone miss Qt aligned-item height/width mismatches.
    Scroll viewports may clip their content intentionally; the label itself
    must still have enough room for every line, including offscreen lines.
    """
    from PySide6.QtCore import QRect
    issues = []
    for label in host.findChildren(QLabel):
        if not label.isVisibleTo(host) or not label.text().strip():
            continue
        if label.textFormat() == Qt.TextFormat.RichText:
            continue
        rect = label.contentsRect()
        flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        if label.wordWrap():
            flags |= Qt.TextFlag.TextWordWrap
        required = label.fontMetrics().boundingRect(
            QRect(0, 0, max(1, rect.width()), 100000), int(flags), label.text())
        if rect.height() < required.height() or rect.width() < required.width():
            issues.append({'name': label.objectName(), 'text': label.text()[:160],
                           'actual': rect.getRect(), 'required': (required.width(), required.height())})
    return issues

@pytest.fixture(scope="session")
def app_v2():
    app = QApplication.instance() or QApplication([])
    # The Windows offscreen plugin does not discover system fonts itself.
    # Load real Chinese glyphs so layout checks and retained screenshots are useful.
    if app.platformName() == 'offscreen' and os.name == 'nt':
        for name in ('msyh.ttc', 'msyhbd.ttc'):
            font_path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / name
            if font_path.is_file():
                QFontDatabase.addApplicationFont(str(font_path))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    return app

def wait(app, predicate, timeout=12):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("The native UI did not reach the expected state")

@pytest.fixture
def shell(app_v2, tmp_path):
    root = tmp_path / "synthetic-v2-data"
    process = subprocess.Popen([sys.executable, "-m", "management", "--service", "--data-dir", str(root)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    window = None
    try:
        wait(app_v2, lambda: (root / "runtime.json").exists())
        window = MainWindow(root, client_factory=lambda p: Client(p, autostart=False))
        window.show()
        wait(app_v2, lambda: window.type_map and not window.bridge.callbacks)
        yield window
    finally:
        try:
            if window:
                window.review_pending = False
                # These inputs belong to this disposable fixture. A failed
                # assertion must not leave teardown waiting in a modal prompt.
                window.tasks_page.quick_input.clear()
                window.close()
                wait(app_v2, lambda: not window.isVisible() and not window.bridge.thread.isRunning() and not window.bridge.mutation_thread.isRunning())
                # Retire this fixture on the Qt owner thread before the next case;
                # don't leave closed windows and their signal cycles for later GC.
                window.deleteLater()
                app_v2.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                app_v2.processEvents()
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)

def create(client, kind, title, data=None, parent_id=None):
    client.state()
    return client.command("create", {"type": kind, "title": title, "parent_id": parent_id, "data": data or {}})["result"]["entity"]

def text_in(widget):
    return "\n".join(label.text() for label in widget.findChildren(QLabel)) + "\n" + "\n".join(button.text() for button in widget.findChildren(QPushButton))


def test_light_and_dark_appearance_share_data_and_live_editor(app_v2, shell, tmp_path):
    from management.gui_theme import apply_appearance, current_appearance
    from management.gui_workflows import HabitsDialog
    client = Client(shell.data_dir, autostart=False)
    day = QDate.currentDate().toString('yyyy-MM-dd')
    project = create(client, 'project', '周末阅读角', {'purpose': '虚构界面验收项目'})
    course = create(client, 'course', '视觉设计基础', {'code': 'DES101'})
    tasks = [create(client, 'task', title,
                    {'estimated_minutes': minutes, 'due_date': day, 'completion_gate': '仅作虚构界面验收'},
                    parent_id=owner)
             for title, minutes, owner in [
                 ('整理阅读笔记', 35, course['id']), ('准备活动清单', 25, project['id']),
                 ('完成色彩练习', 40, course['id']), ('确认展示布局', 30, project['id']),
                 ('整理参考资料', 20, course['id']), ('整理读者反馈', 25, project['id']),
             ]]
    client.command('create_plan', {'date': day, 'mode': 'no_precise_time',
                                  'blocks': [{'target_id': item['id'], 'minutes': item['data']['estimated_minutes']} for item in tasks[:3]]})
    create(client, 'event', '阅读角交流', {'date': day, 'start': '15:00', 'end': '16:00',
           'time_kind': 'exact', 'timezone': 'Asia/Shanghai'})
    create(client, 'rule', '虚构日期提醒', {'rule_kind':'warning','days_before':3})
    client.command('settings', {'settings': {'charts': {'dashboard_today_style': 'ring', 'dashboard_tasks_style': 'ring'}}})
    shell.load_display_preferences()
    wait(app_v2, lambda: not shell.bridge.callbacks)
    previous_appearance = current_appearance()
    polling = shell.poll.isActive()
    # These screenshots preview unsaved themes. A periodic service read would
    # correctly restore saved appearance halfway through the synthetic preview.
    shell.poll.stop()
    shell.onboarding.set_automatic(False)
    before = client.state()
    report = Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR', str(tmp_path)))
    habits = None
    try:
        for theme in ('light', 'dark'):
            apply_appearance(app_v2, {**previous_appearance, 'theme': theme, 'font_size': 13})
            shell.resize(1390, 900)
            for section in ('dashboard', 'today', 'tasks', 'projects', 'reviews'):
                shell.navigate(section)
                wait(app_v2, lambda: not shell.bridge.callbacks and not shell.dashboard_page._layout_pending)
                # Navigation can refresh saved display preferences. Apply the
                # preview theme after those asynchronous reads, not before them.
                apply_appearance(app_v2, {**previous_appearance, 'theme': theme, 'font_size': 13})
                wait(app_v2, lambda: not shell.dashboard_page._layout_pending)
                # Finite entrance animation must finish before a retained image.
                wait(app_v2, lambda: not shell.page_transition.is_active())
                app_v2.processEvents();shell.repaint()
                assert current_appearance()['theme']==theme
                assert shell.grab().save(str(report / f'visual-glass-{section}-{theme}.png'))
                if section=='dashboard':
                    owners=shell.dashboard_page.owner_section
                    wait(app_v2,lambda:owners.height()>=owners.minimumSizeHint().height())
                    assert shell.dashboard_page.owner_rows.count()==2
                    assert shell.dashboard_page.owner_open.isVisible()
                    assert shell.dashboard_page.owner_open.geometry().bottom()<owners.height()
                    shell.dashboard_page.verticalScrollBar().setValue(shell.dashboard_page.verticalScrollBar().maximum())
                    app_v2.processEvents()
                    assert shell.grab().save(str(report / f'visual-glass-dashboard-details-{theme}.png'))
                    shell.dashboard_page.verticalScrollBar().setValue(0)
                if section=='today':
                    shell.today_page.date.setDate(QDate.currentDate().addDays(1))
                    wait(app_v2,lambda:not shell.bridge.callbacks)
                    page=shell.today_page
                    # Switching from populated content must release its previous
                    # height, including a now-hidden warning or fixed schedule.
                    sections=[page.attention_box,page.plan_box,page.events_box,page.tasks_box]
                    def sections_ready():
                        for widget in sections:
                            if widget.isHidden():continue
                            layout=widget.layout()
                            for index in range(layout.count()):
                                child=layout.itemAt(index).widget()
                                if child is not None and not child.isVisible():return False
                            if widget.height()<layout.totalHeightForWidth(widget.width()):return False
                        return True
                    wait(app_v2,sections_ready)
                    geometry=[{'name':widget.objectName() or type(widget).__name__,
                               'hidden':widget.isHidden(),'y':widget.y(),'height':widget.height(),
                               'minimum':widget.minimumHeight(),'hint':widget.sizeHint().height()}
                              for widget in sections]
                    first=next(widget for widget in sections if not widget.isHidden())
                    assert first.y()<=page.body.contentsMargins().top()+4,geometry
                    assert page.plan_box.height()<=page.plan_box.sizeHint().height()+4,geometry
                    apply_appearance(app_v2, {**previous_appearance, 'theme': theme, 'font_size': 13})
                    wait(app_v2,sections_ready)
                    app_v2.processEvents();shell.repaint()
                    assert current_appearance()['theme']==theme
                    assert shell.grab().save(str(report / f'visual-glass-today-empty-{theme}.png'))
                    shell.today_page.date.setDate(QDate.currentDate())
            habits = HabitsDialog(shell.bridge, shell.capabilities, shell)
            habits.open()
            wait(app_v2, lambda: not shell.bridge.callbacks)
            apply_appearance(app_v2, {**previous_appearance, 'theme': theme, 'font_size': 13})
            app_v2.processEvents();habits.repaint()
            assert current_appearance()['theme']==theme
            assert habits.grab().save(str(report / f'visual-glass-habits-{theme}.png'))
            habits.close(); habits.deleteLater(); habits = None
        shell.navigate('tasks')
        quick_input = shell.tasks_page.quick_input
        quick_input.setText('Unsaved text survives appearance changes')
        for theme, size in (('light', 20), ('dark', 13)):
            apply_appearance(app_v2, {**previous_appearance, 'theme': theme, 'font_size': size})
            app_v2.processEvents()
            assert shell.tasks_page.quick_input is quick_input
            assert quick_input.text() == 'Unsaved text survives appearance changes'
        assert client.state()['revision'] == before['revision']
        assert client.state()['epoch'] == before['epoch']
    finally:
        if habits:
            habits.close(); habits.deleteLater()
        shell.tasks_page.quick_input.clear()
        apply_appearance(app_v2, previous_appearance)
        if polling:
            shell.poll.start()


def test_guides_cover_main_panels_without_changing_business_data(app_v2, shell, tmp_path):
    from management.gui_theme import apply_appearance
    from management.verify_ui import capture_onboarding
    from PySide6.QtWidgets import QStyle, QStyleOptionButton
    client = Client(shell.data_dir, autostart=False)
    before = client.state()
    manager = shell.onboarding
    assert shell.guide_button.text() == '使用指南'
    evidence = capture_onboarding(shell, tmp_path / 'packaged-gui.json')
    assert evidence['passed'] and evidence['progress_saved'] and evidence['steps'] == 9
    assert Path(evidence['screenshot']).is_file()
    for section, key in [('dashboard', 'dashboard'), ('today', 'today'),
                         ('tasks', 'tasks'), ('projects', 'projects'), ('reviews', 'review.daily')]:
        shell.navigate(section)
        wait(app_v2, lambda: not shell.bridge.callbacks)
        assert manager.show_current()
        assert manager.active_key == key
        overlay = manager.overlay
        # Walk every real target, including the empty-data fallback targets.
        for _ in overlay.steps:
            app_v2.processEvents()
            assert overlay.isVisible()
            QTest.mouseClick(overlay.next_button, Qt.MouseButton.LeftButton)
        assert manager.overlay is None
    shell.review_page.tabs.setCurrentIndex(1)
    assert manager.show_current()
    assert manager.active_key == 'review.weekly'
    manager.overlay.skip_button.click()
    assert client.state()['revision'] == before['revision']
    assert (shell.data_dir / 'ui-onboarding.json').exists()

    shell.navigate('dashboard')
    wait(app_v2, lambda: not shell.bridge.callbacks)
    report_dir = Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR', str(tmp_path)))
    try:
        for theme, size in [('light', 13), ('dark', 20)]:
            apply_appearance(app_v2, {'theme': theme, 'font_size': size})
            assert manager.show('dashboard')
            QTest.qWait(200)
            overlay = manager.overlay
            assert overlay.rect().contains(overlay.card.geometry())
            for button in (overlay.back_button, overlay.next_button, overlay.skip_button, overlay.disable_button):
                option = QStyleOptionButton()
                button.initStyleOption(option)
                contents = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
                text_bounds = button.fontMetrics().boundingRect(button.text())
                assert contents.height() >= text_bounds.height(), (theme, button.text(), contents, text_bounds)
                assert contents.width() >= text_bounds.width(), (theme, button.text(), contents, text_bounds)
            assert shell.grab().save(str(report_dir / f'onboarding-{theme}.png'))
            overlay.skip_button.click()
    finally:
        apply_appearance(app_v2, shell._saved_appearance)


def test_dialog_guides_preserve_unsaved_plan_and_settings(app_v2, shell):
    from management.gui_workflows import PlanDialog, SettingsDialog
    manager = shell.onboarding
    client = Client(shell.data_dir, autostart=False)
    before = client.state()['revision']
    plan = PlanDialog(shell.bridge, shell)
    try:
        plan.open()
        wait(app_v2, lambda: not shell.bridge.callbacks)
        plan.source.setPlainText('尚未保存的安排依据')
        assert manager.show_current()
        assert manager.active_key == 'plan'
        QTest.keyClick(manager.overlay.next_button, Qt.Key.Key_Escape)
        assert plan.isVisible()
        assert plan.source.toPlainText() == '尚未保存的安排依据'
        assert plan.dirty
    finally:
        plan.dirty = False
        plan.close()
        plan.deleteLater()
    settings = SettingsDialog(shell.bridge, shell.capabilities, shell.data_dir, shell)
    try:
        settings.open()
        wait(app_v2, lambda: not shell.bridge.callbacks)
        for index, key in enumerate(['display', 'timetable', 'codex', 'data']):
            settings.tabs.setCurrentIndex(index)
            app_v2.processEvents()
            assert manager.show_current()
            assert manager.active_key == 'settings.' + key
            if key == 'codex':
                target=manager.overlay.steps[-1].target
                assert (target() if callable(target) else target) is settings.ai_save
            manager.overlay.skip_button.click()
        settings.tabs.setCurrentIndex(settings._provider_tab)
        settings.ai_mode.setCurrentIndex(settings.ai_mode.findData('desktop_shared'))
        assert manager.show_current()
        target=manager.overlay.steps[-1].target
        assert (target() if callable(target) else target) is settings.codex_bridge_start
        manager.overlay.skip_button.click()
        assert settings.guide_button.isVisible()
        assert client.state()['revision'] == before
    finally:
        settings.close()
        settings.deleteLater()


@pytest.mark.parametrize('theme,size',[('light',13),('dark',20)])
def test_all_tutorial_steps_align_with_real_controls_and_keep_business_read_only(app_v2,shell,tmp_path,monkeypatch,theme,size):
    """Walk every actual tour against an independent global-coordinate oracle."""
    from management.gui_theme import apply_appearance,current_appearance
    from management.gui_workflows import HabitsDialog,PlanDialog,SettingsDialog
    from management.gui_forms import EntityForm
    from management.gui_assistant import AssistanceDialog
    from management.gui_timetable import TimetableDialog
    from management.verify_ui import onboarding_step_geometry
    monkeypatch.setenv('PERSONAL_MANAGEMENT_NO_ONBOARDING','1')
    client=Client(shell.data_dir,autostart=False);before=client.state()
    settings_before=client.query('settings')['settings'];jobs_before=client.query('jobs')['total']
    manager=shell.onboarding;old_appearance=current_appearance();records=[];dialogs=[]
    folder=Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR',str(tmp_path)))
    folder.mkdir(parents=True,exist_ok=True)
    report_path=folder/f'onboarding-geometry-{theme}.json'
    report={'theme':theme,'font_size':size,'coordinate_oracle':'independent mapToGlobal plus ancestor clipping',
            'steps':records,'business_unchanged':False,'passed':False}

    def activate(host):
        host.raise_();host.activateWindow()
        if app_v2.platformName()=='offscreen':app_v2.setActiveWindow(host)
        app_v2.processEvents()

    def inspect_step(key,host,phase):
        overlay=manager.overlay;last={}
        def valid():
            last.clear();last.update(onboarding_step_geometry(overlay))
            return last['passed'] and not getattr(overlay,'_geometry_pending',False)
        try:wait(app_v2,valid,timeout=2)
        except AssertionError:
            last.clear();last.update(onboarding_step_geometry(overlay))
            if getattr(overlay,'_geometry_pending',False):last['errors'].append('geometry_did_not_settle')
            last['passed']=False
        last.update(tour=key,phase=phase,host_size=[host.width(),host.height()])
        records.append(last)
        return last

    def walk(key,host,*,codex_mode=None,main=False):
        activate(host)
        assert manager.show(key),(key,host.windowTitle())
        overlay=manager.overlay;count=len(overlay.steps)
        assert count>0
        if codex_mode:assert count==4
        for index in range(count):
            assert manager.overlay is overlay and overlay.current_index==index
            base=inspect_step(key,host,'initial')
            base['variant']=codex_mode or 'default'
            if codex_mode:
                # Exercise the same live step after a nonzero window move and
                # a width/height change; no product button is invoked.
                host.move(shell.pos()+QPoint(37+index*11,49+index*7))
                host.resize(680 if index%2 else 740,530 if index%2 else 610)
                moved=inspect_step(key,host,'moved-and-resized')
                moved['variant']=codex_mode
                screenshot=folder/f'onboarding-codex-{codex_mode}-{theme}-step-{index+1}.png'
                assert host.grab().save(str(screenshot));moved['screenshot']=str(screenshot)
            elif main and index==0:
                screenshot=folder/f'onboarding-main-{key.replace(".","-")}-{theme}-first.png'
                assert host.grab().save(str(screenshot));base['screenshot']=str(screenshot)
            overlay.next_button.click()
            app_v2.processEvents()
        assert manager.overlay is None

    def open_dialog(dialog,width=740,height=610):
        dialogs.append(dialog);dialog.resize(width,height);dialog.move(shell.pos()+QPoint(53,61));dialog.open()
        wait(app_v2,lambda:not shell.bridge.callbacks)
        activate(dialog)
        return dialog

    def close_dialog(dialog):
        if manager.overlay is not None:manager.overlay.skip_button.click()
        if isinstance(dialog,PlanDialog):dialog.dirty=False
        dialog.close();app_v2.processEvents()
        wait(app_v2,lambda:not shell.bridge.callbacks)

    try:
        apply_appearance(app_v2,{'theme':theme,'font_size':size})
        shell.resize(1120 if size==13 else 1040,800 if size==13 else 760);shell.move(79,61)
        for section,key in [('dashboard','dashboard'),('today','today'),('tasks','tasks'),
                            ('projects','projects'),('reviews','review.daily')]:
            shell.navigate(section)
            if section=='reviews':shell.review_page.tabs.setCurrentIndex(0)
            wait(app_v2,lambda:not shell.bridge.callbacks)
            walk(key,shell,main=True)
        shell.review_page.tabs.setCurrentIndex(1);app_v2.processEvents()
        walk('review.weekly',shell,main=True)
        for key,factory in [
            ('habits',lambda:HabitsDialog(shell.bridge,shell.capabilities,shell)),
            ('plan',lambda:PlanDialog(shell.bridge,shell)),
            ('editor',lambda:EntityForm(shell.bridge,shell.capabilities,shell,default_type='task')),
            ('discussion',lambda:AssistanceDialog(shell.bridge,shell,prompt='虚构的未发送草稿',auto_send=False)),
            ('timetable',lambda:TimetableDialog(shell.bridge,shell)),
        ]:
            dialog=open_dialog(factory())
            if key=='plan':dialog.source.setPlainText('虚构的未保存计划依据')
            if key=='editor':dialog.title_edit.setText('虚构的未保存事项')
            walk(key,dialog)
            if key=='plan':assert dialog.source.toPlainText()=='虚构的未保存计划依据'
            if key=='editor':assert dialog.title_edit.text()=='虚构的未保存事项'
            if key=='discussion':assert dialog.prompt.toPlainText()=='虚构的未发送草稿'
            close_dialog(dialog)

        settings=open_dialog(SettingsDialog(shell.bridge,shell.capabilities,shell.data_dir,shell))
        for index,key in [(0,'display'),(1,'timetable'),(3,'data')]:
            settings.tabs.setCurrentIndex(index);app_v2.processEvents()
            walk('settings.'+key,settings)
        settings.tabs.setCurrentIndex(settings._provider_tab)
        for mode in ('background','desktop_shared'):
            settings.ai_mode.setCurrentIndex(settings.ai_mode.findData(mode))
            settings.resize(740,610);app_v2.processEvents()
            tab=settings.tabs.currentWidget()
            assert isinstance(tab,QScrollArea)
            tab.verticalScrollBar().setValue(tab.verticalScrollBar().maximum())
            scroll_before=tab.verticalScrollBar().value()
            walk('settings.codex',settings,codex_mode=mode)
            assert settings.ai_mode.currentData()==mode
            report.setdefault('codex_scrolls',[]).append({'mode':mode,'before':scroll_before,
                'after':tab.verticalScrollBar().value(),'range_after':tab.verticalScrollBar().maximum()})
        close_dialog(settings)
        wait(app_v2,lambda:not shell.bridge.callbacks)
        after=client.state()
        report['business_unchanged']=(after['epoch']==before['epoch'] and after['revision']==before['revision']
            and after['counts']==before['counts'] and client.query('settings')['settings']==settings_before
            and client.query('jobs')['total']==jobs_before)
        expected={'dashboard','today','tasks','projects','review.daily','review.weekly','habits','plan','editor',
                  'discussion','timetable','settings.display','settings.timetable','settings.data','settings.codex'}
        assert {record['tour'] for record in records}==expected
        report['tour_keys']=sorted(expected)
        report['initial_step_count']=sum(record['phase']=='initial' for record in records)
        report['geometry_check_count']=len(records)
        report['fully_revealed_small_targets']=sum(bool(record.get('scrolls')) and all(
            not item['fits_target'] or item['fully_revealed'] for item in record['scrolls']) for record in records)
        assert {record['variant'] for record in records if record['tour']=='settings.codex'}=={'background','desktop_shared'}
        report['passed']=report['business_unchanged'] and all(record['passed'] for record in records)
        assert report['business_unchanged']
        assert report['passed'],json.dumps([r for r in records if not r['passed']],ensure_ascii=False,indent=2)
    finally:
        if manager.overlay is not None:manager.overlay.skip_button.click()
        for dialog in reversed(dialogs):
            if isinstance(dialog,PlanDialog):dialog.dirty=False
            dialog.close();dialog.deleteLater()
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        app_v2.sendPostedEvents(None,QEvent.Type.DeferredDelete);app_v2.processEvents()
        apply_appearance(app_v2,old_appearance)


@pytest.mark.parametrize('theme,size',[('light',13),('dark',20)])
def test_populated_tutorial_targets_are_real_controls_and_tours_do_not_save(app_v2,shell,tmp_path,monkeypatch,theme,size):
    """Complement empty-space tours with live business views and UI-only chat fixtures."""
    from management.gui_theme import apply_appearance,current_appearance
    from management.gui_forms import EntityForm
    from management.gui_timetable import TimetableDialog
    from management.gui_assistant import AssistanceDialog
    from management.verify_ui import onboarding_step_geometry
    from test_ux_workflows_v2 import ControlledBridge,proposal
    monkeypatch.setenv('PERSONAL_MANAGEMENT_NO_ONBOARDING','1')
    client=Client(shell.data_dir,autostart=False);day=QDate.currentDate().toString('yyyy-MM-dd')
    project=create(client,'project','虚构项目：教程目标验收')
    course=create(client,'course','虚构课程：教程目标验收',{'code':'DEMO-GUIDE'})
    first=create(client,'task','虚构工作：检查目录',{'completion_gate':'写下检查结果'},project['id'])
    second=create(client,'task','虚构练习：完成两题',{'completion_gate':'独立完成并核对两题'},course['id'])
    client.command('create_plan',{'date':day,'mode':'no_precise_time','blocks':[
        {'target_id':first['id'],'minutes':20},{'target_id':second['id'],'minutes':25}],
        'source_text':'仅用于隔离测试的虚构安排。'})
    event=create(client,'event','虚构固定课程',{'date':day,'start':'09:00','end':'10:00',
        'timezone':'Asia/Shanghai','time_kind':'exact','hard':True,'owner_id':course['id'],
        'event_kind':'lecture','source_text':'仅用于教程几何测试的虚构日程。'})
    shell.refresh();wait(app_v2,lambda:not shell.bridge.callbacks)
    before=client.state();settings_before=client.query('settings')['settings'];jobs_before=client.query('jobs')['total']
    old_appearance=current_appearance();manager=shell.onboarding;records=[];dialogs=[]
    folder=Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR',str(tmp_path)));folder.mkdir(parents=True,exist_ok=True)
    report_path=folder/f'onboarding-geometry-{theme}.json'
    populated={'theme':theme,'font_size':size,'steps':records,'business_unchanged':False,'passed':False,
               'discussion_boundary':'ControlledBridge renders synthetic history/candidate cards only; no model or service assistant records.'}

    def focus(host):
        host.raise_();host.activateWindow()
        if app_v2.platformName()=='offscreen':app_v2.setActiveWindow(host)
        app_v2.processEvents()

    def walk(key,host,variant,expected=None):
        focus(host);assert manager.show(key),(key,variant)
        overlay=manager.overlay
        for index in range(len(overlay.steps)):
            last={}
            def aligned():
                last.clear();last.update(onboarding_step_geometry(overlay))
                return last['passed'] and not getattr(overlay,'_geometry_pending',False)
            try:wait(app_v2,aligned,timeout=2)
            except AssertionError:
                last.clear();last.update(onboarding_step_geometry(overlay));last['passed']=False
                if not last['errors']:last['errors'].append('geometry_did_not_settle')
            target=overlay.steps[index].target;target=target() if callable(target) else target
            if expected and index in expected and not expected[index](target):
                last['errors'].append('unexpected_fallback_in_populated_view');last['passed']=False
            last.update(tour=key,variant=variant,target_text=target.text() if isinstance(target,(QLabel,QPushButton)) else '')
            records.append(last)
            if index==2 and key in {'today','timetable','discussion'}:
                screenshot=folder/f'onboarding-populated-{key}-{variant}-{theme}.png'
                assert host.grab().save(str(screenshot));last['screenshot']=str(screenshot)
            overlay.next_button.click();app_v2.processEvents()
        assert manager.overlay is None

    def show_dialog(dialog):
        dialogs.append(dialog);dialog.resize(900 if size==13 else 980,680 if size==13 else 760)
        dialog.move(shell.pos()+QPoint(47,39));dialog.open();wait(app_v2,lambda:not shell.bridge.callbacks)
        focus(dialog);return dialog

    try:
        apply_appearance(app_v2,{'theme':theme,'font_size':size});shell.resize(1160,820);shell.move(61,47)
        shell.navigate('today');wait(app_v2,lambda:shell.today_page.has_plan and not shell.bridge.callbacks)
        walk('today',shell,'planned-tasks',{2:lambda target:target is not None and target.objectName()=='ReviewChoice'})
        shell.navigate('projects');shell.workspace_page.open_object(project)
        wait(app_v2,lambda:not shell.bridge.callbacks and (shell.workspace_page.current_entity or {}).get('id')==project['id'])
        walk('projects',shell,'project-detail',{1:lambda target:isinstance(target,QPushButton) and target.text()=='＋ 添加'})
        shell.open_review(day);wait(app_v2,lambda:first['id'] in shell.review_page.item_buttons and not shell.bridge.callbacks)
        walk('review.daily',shell,'planned-results',{
            1:lambda target:target is not None and target.objectName()=='ReviewChoice',
            3:lambda target:target is (shell.review_page.confirm_button if shell.review_page.confirm_button.isVisible() else shell.review_page.feedback_button)})
        shell.review_page.tabs.setCurrentIndex(1);app_v2.processEvents()
        for style in ('columns','rows','tiles'):
            shell.review_page.set_chart_preferences({'weekly_style':style});app_v2.processEvents()
            walk('review.weekly',shell,style,{1:lambda target:isinstance(target,QPushButton) and target.accessibleName().startswith('查看 ')})
        timetable=show_dialog(TimetableDialog(shell.bridge,shell,business_date=day))
        wait(app_v2,lambda:not shell.bridge.callbacks and any(block.event_data.get('id')==event['id'] for block in timetable.grid.blocks))
        walk('timetable',timetable,'actual-event-block',{2:lambda target:target in timetable.grid.blocks and target.event_data.get('id')==event['id']})
        timetable.close();app_v2.processEvents()
        for kind in ('course','project'):
            form=show_dialog(EntityForm(shell.bridge,shell.capabilities,shell,default_type=kind))
            form.title_edit.setText('虚构的未保存'+kind)
            walk('editor',form,kind,{1:lambda target,editor=form:target in [
                part for field in editor.fields.values()
                for part in (getattr(field,'enabled',None),getattr(field,'editor',None)) if part is not None]})
            assert form.title_edit.text()=='虚构的未保存'+kind
            form.close();app_v2.processEvents()

        bridge=ControlledBridge();chat=show_dialog(AssistanceDialog(bridge,shell,prompt='未发送的虚构草稿',auto_send=False))
        bridge.deliver('settings',{'settings':{'ai':{'enabled':False}}})
        bridge.deliver('conversation',{'conversation':None,'messages':[],'has_more':False});chat.timer.stop()
        chat.messages={'history':{'id':'history','seq':1,'role':'assistant','text':'虚构的已渲染历史，未访问真实模型。','job_id':None,'proposal_state':'none'}}
        chat.render_history();app_v2.processEvents()
        assert chat.cards['history'].author.isVisible() and not chat.cards['history'].proposal_heading.isVisible()
        walk('discussion',chat,'history-author',{2:lambda target:target is chat.cards['history'].author})
        chat.job_id='synthetic-candidate';chat.full_job=proposal('synthetic-candidate',title='虚构候选，仅验证教程控件')
        chat.messages['candidate']={'id':'candidate','seq':2,'role':'assistant','text':'这是一份尚未采用的虚构候选。',
            'job_id':chat.job_id,'proposal_state':'available'}
        chat.render_history();app_v2.processEvents();chat.timer.stop()
        assert chat.cards['candidate'].proposal_heading.isVisible() and chat.apply.isVisible()
        walk('discussion',chat,'candidate-apply',{2:lambda target:target is chat.apply})
        assert bridge.commands==[] and chat.prompt.toPlainText()=='未发送的虚构草稿'
        chat.close();app_v2.processEvents()
        wait(app_v2,lambda:not shell.bridge.callbacks)
        after=client.state()
        populated['business_unchanged']=(after['epoch']==before['epoch'] and after['revision']==before['revision']
            and after['counts']==before['counts'] and client.query('settings')['settings']==settings_before
            and client.query('jobs')['total']==jobs_before)
        populated['geometry_check_count']=len(records)
        populated['passed']=populated['business_unchanged'] and all(record['passed'] for record in records)
        assert populated['business_unchanged']
        assert populated['passed'],json.dumps([record for record in records if not record['passed']],ensure_ascii=False,indent=2)
    finally:
        if manager.overlay is not None:manager.overlay.skip_button.click()
        for dialog in reversed(dialogs):dialog.close();dialog.deleteLater()
        previous_report=json.loads(report_path.read_text('utf-8')) if report_path.exists() else {'theme':theme,'font_size':size}
        previous_report['populated']=populated
        report_path.write_text(json.dumps(previous_report,ensure_ascii=False,indent=2),encoding='utf-8')
        app_v2.sendPostedEvents(None,QEvent.Type.DeferredDelete);app_v2.processEvents();apply_appearance(app_v2,old_appearance)


def test_first_visits_offer_guides_after_loading_and_direct_review_entry(app_v2, shell, monkeypatch):
    monkeypatch.delenv('PERSONAL_MANAGEMENT_NO_ONBOARDING')
    shell.hide()
    app_v2.processEvents()
    shell.show()
    shell.activateWindow()
    if app_v2.platformName() == 'offscreen':
        # The Windows offscreen plugin can lose activation after hide/show.
        # Model the user focusing this synthetic window without relaxing the
        # production requirement that tutorials never steal another app's focus.
        app_v2.setActiveWindow(shell)
    manager = shell.onboarding
    wait(app_v2, lambda: manager.active_key == 'dashboard')
    manager.overlay.skip_button.click()
    shell.navigate('today')
    wait(app_v2, lambda: manager.active_key == 'today')
    manager.overlay.skip_button.click()
    shell.open_review(QDate.currentDate().toString('yyyy-MM-dd'))
    wait(app_v2, lambda: manager.active_key == 'review.daily')
    manager.overlay.skip_button.click()
    shell.navigate('today')
    QTest.qWait(500)
    assert manager.overlay is None

@pytest.mark.parametrize('theme,size', [('light', 13), ('dark', 20)])
def test_text_is_complete_across_main_pages_and_secondary_windows(app_v2, shell, tmp_path, theme, size):
    from management.gui import SearchDialog
    from management.gui_forms import EntityForm
    from management.gui_workspace import TaskDetailDialog
    from management.gui_update import UpdateDialog
    from management.gui_workflows import PlanDialog, JobsDialog, ArtifactDialog
    from management.gui_review import ReviewNotesDialog, ActualFeedbackDialog, ActualFeedbackHistoryDialog
    from management.gui_theme import apply_appearance, current_appearance
    from management.gui_visual_profile import visual_style
    old_style, old_appearance = visual_style(), current_appearance()
    polling = shell.poll.isActive()
    shell.poll.stop()
    client = Client(shell.data_dir, autostart=False)
    day = QDate.currentDate().toString('yyyy-MM-dd')
    project = create(client, 'project', '【演示】社区阅读角与信息展示原型：整理资料并核对各阶段的说明文字',
                     {'purpose': '这是虚构的界面验收内容，用于检查较长中文说明在窄窗口和较大字号下是否完整显示。' * 2})
    course = create(client, 'course', '【演示】视觉设计与信息表达课程：阅读、练习和阶段回顾', {'code': 'DEMO-LAYOUT'})
    task = create(client, 'task', '【演示】根据提纲绘制展示卡片并核对所有文字说明和截止日期',
                  {'due_date': day, 'completion_gate': '逐项核对标题、说明、日期和来源，并保留需要进一步确认的内容。' * 3}, project['id'])
    create(client, 'rule', '【演示】临近日程提示', {'rule_kind': 'warning', 'days_before': 3})
    shell.onboarding.set_automatic(False)
    shell.apply_visual_style('glass')
    shell.refresh(); wait(app_v2, lambda: not shell.bridge.callbacks)
    before = client.state()
    report = Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR', str(tmp_path)))
    records, dialogs = [], []

    def inspect(host, area, width, height):
        apply_appearance(app_v2, {**old_appearance, 'theme': theme, 'font_size': size})
        host.resize(width, height)
        # Process asynchronous polish, height-for-width and scrollbar changes.
        QTest.qWait(180); app_v2.processEvents()
        wait(app_v2, lambda: not shell.bridge.callbacks and not shell.dashboard_page._layout_pending)
        issues = _text_layout_issues(host)
        for scroll in host.findChildren(QScrollArea):
            if scroll.isVisibleTo(host) and scroll.horizontalScrollBar().maximum():
                issues.append({'horizontal_overflow': scroll.objectName(),
                               'width': scroll.width(), 'maximum': scroll.horizontalScrollBar().maximum(),
                               'wide_layouts': [(type(layout).__name__, layout.minimumSize().width(),
                                                 [(layout.itemAt(i).widget().text()[:70] if hasattr(layout.itemAt(i).widget(), 'text') else type(layout.itemAt(i).widget()).__name__) for i in range(layout.count())])
                                                for layout in scroll.widget().findChildren(QLayout)
                                                if layout.minimumSize().width() > scroll.viewport().width() - 40],
                               'wide_children': [(type(w).__name__, w.objectName(), w.minimumSizeHint().width(),
                                                  w.text()[:90] if hasattr(w, 'text') else '')
                                                 for w in scroll.widget().findChildren(QWidget)
                                                 if w.isVisibleTo(host) and w.minimumSizeHint().width() > scroll.viewport().width() - 40]})
        records.append({'area': area, 'width': host.width(), 'font_size': size, 'issues': issues})
        if issues or width == 1100:
            host.grab().save(str(report / f'text-layout-{area}-{theme}-{width}.png'))

    def dialog_check(dialog, area):
        dialogs.append(dialog)
        dialog.open(); wait(app_v2, lambda: not shell.bridge.callbacks)
        for width in (940, 620):
            inspect(dialog, area, width, 720)
        dialog.close(); app_v2.processEvents()

    try:
        for section in ('dashboard', 'today', 'tasks', 'projects', 'reviews'):
            shell.navigate(section)
            wait(app_v2, lambda: not shell.bridge.callbacks and not shell.page_transition.is_active())
            for width in (1700, 1100):
                inspect(shell, section, width, 950)
        for entity in (project, course):
            shell.navigate('projects'); shell.workspace_page.open_object(entity)
            wait(app_v2, lambda: not shell.bridge.callbacks)
            for width in (1700, 1100):
                inspect(shell, entity['type'] + '-content', width, 950)
        shell.open_review(day)
        wait(app_v2, lambda: not shell.bridge.callbacks)
        shell.review_page.tabs.setCurrentIndex(1)
        QTest.qWait(400)
        inspect(shell, 'weekly-review', 1100, 950)
        for kind in ('task', 'project', 'course', 'activity', 'domain', 'goal'):
            dialog_check(EntityForm(shell.bridge, shell.capabilities, shell, default_type=kind), 'new-' + kind)
        dialog_check(TaskDetailDialog(shell.bridge, task, shell), 'task-detail')
        dialog_check(SearchDialog(shell.bridge, shell, lambda *_: None, initial_text='演示'), 'search')
        dialog_check(UpdateDialog(shell), 'version')
        dialog_check(PlanDialog(shell.bridge, shell, date=QDate.fromString(day, 'yyyy-MM-dd')), 'plan')
        dialog_check(JobsDialog(shell.bridge, shell), 'jobs')
        dialog_check(ArtifactDialog(shell.bridge, shell), 'local-artifact')
        dialog_check(ReviewNotesDialog(shell.bridge, day, shell), 'review-notes')
        dialog_check(ActualFeedbackDialog(shell.bridge, task, day, shell), 'actual-feedback')
        dialog_check(ActualFeedbackHistoryDialog(shell.bridge, day, shell), 'feedback-history')
        shell.navigate('projects')
        wait(app_v2, lambda: not shell.bridge.callbacks)
        shell.raise_(); shell.activateWindow()
        if app_v2.platformName() == 'offscreen':
            app_v2.setActiveWindow(shell)
        assert shell.onboarding.show('projects')
        overlay = shell.onboarding.overlay
        for index in range(len(overlay.steps)):
            QTest.qWait(120)
            records.append({'area': 'guide-projects-' + str(index), 'font_size': size,
                            'issues': _text_layout_issues(overlay)})
            overlay.next_button.click(); app_v2.processEvents()
        after = client.state()
        assert (after['epoch'], after['revision']) == (before['epoch'], before['revision'])
        assert not [r for r in records if r['issues']], json.dumps([r for r in records if r['issues']], ensure_ascii=False, indent=2)
    finally:
        if shell.onboarding.overlay is not None:
            shell.onboarding.stop()
        for dialog in dialogs:
            dialog.close(); dialog.deleteLater()
        (report / f'text-layout-{theme}.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        app_v2.sendPostedEvents(None, QEvent.Type.DeferredDelete); app_v2.processEvents()
        shell.apply_visual_style(old_style)
        apply_appearance(app_v2, old_appearance)
        if polling:
            shell.poll.start()


def test_five_sections_six_creation_types_and_removed_toolbar(app_v2, shell):
    assert list(shell.nav_buttons) == ["dashboard", "today", "tasks", "projects", "reviews"]
    assert [widget.text().split("  ")[0] for widget in shell.nav_buttons.values()] == ["总览", "今天", "任务", "项目与课程", "复盘"]
    assert shell.section == "dashboard" and not shell.review_pending
    assert [action.text() for action in shell.create_menu.actions()] == ["任务", "项目", "课程", "活动", "分类", "目标"]
    buttons = {button.text() for button in shell.findChildren(QPushButton)}
    assert not {"快速记录", "后台结果", "通知", "收藏", "更多操作", "写下本期回顾"} & buttons
    assert not hasattr(shell, "tabs"), "The permanent generic four-tab inspector must be removed"

def test_today_ignores_unscheduled_backlog_and_page_reads_never_create_reviews(app_v2, shell):
    client = Client(shell.data_dir, autostart=False)
    task = create(client, "task", "合成：不属于每日计划的待办")
    shell.today_page.refresh()
    wait(app_v2, lambda: not shell.bridge.callbacks)
    assert task["title"] not in text_in(shell.today_page)
    assert not shell.today_page.has_plan
    before = client.query("state")["counts"]
    for section in ("reviews", "today", "tasks", "projects", "reviews"):
        shell.navigate(section)
        wait(app_v2, lambda: not shell.bridge.callbacks)
    after = client.query("state")["counts"]
    assert before == after
    assert after.get("checkin", 0) == 0
    assert after.get("review", 0) == 0


def test_manual_workspace_has_no_assistant_requirement_and_preserves_active_jobs(app_v2, shell, monkeypatch):
    from management.gui_theme import apply_appearance
    client = Client(shell.data_dir, autostart=False)
    shell.navigate('tasks')
    wait(app_v2, lambda: not shell.bridge.callbacks)
    assert shell.today_page.plan_button.isHidden()
    assert shell.codex_connection_panel.isHidden()
    assert shell.jobs_button.isHidden()
    shell.tasks_page.quick_input.setText('合成：先记录，再安排')
    shell.tasks_page.quick_button.click()
    wait(app_v2, lambda: not shell.bridge.callbacks)
    assert shell.tasks_page.items.topLevelItemCount() == 1
    shell.tasks_page.plan_button.click()
    wait(app_v2, lambda: not shell.bridge.callbacks)
    assert not shell.tasks_page.plan_button.isEnabled()
    assert shell.tasks_page.selected()['in_plan']
    assert client.query('jobs')['total'] == 0
    folder = Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR', str(shell.data_dir)))
    try:
        for theme, size in [('light', 13), ('dark', 20)]:
            apply_appearance(app_v2, {'theme': theme, 'font_size': size})
            app_v2.processEvents()
            assert shell.grab().save(str(folder / f'manual-tasks-{theme}.png'))
    finally:
        apply_appearance(app_v2, shell._saved_appearance)
    # Hide optional entrances without concealing queued/awaiting work.
    original_query = shell.bridge.query
    def query(name, callback=None, error=None, **params):
        if name == 'assistant_activity':
            callback({'active': 1, 'awaiting_review': 2})
        else:
            return original_query(name, callback, error, **params)
    monkeypatch.setattr(shell.bridge, 'query', query)
    shell.refresh_assistant_activity()
    wait(app_v2, lambda: not shell.bridge.callbacks)
    assert not shell.jobs_button.isHidden()
    assert '处理中' in shell.jobs_button.text()
    assert '2 项待核对' in shell.jobs_button.text()
    assert client.query('jobs')['total'] == 0


def test_close_during_quick_capture_failure_preserves_input(app_v2, shell, monkeypatch):
    pending = []
    def command(name, payload, callback=None, error=None, **options):
        pending.append((name, callback, error))
    monkeypatch.setattr(shell.bridge, 'command', command)
    shell.tasks_page.quick_input.setText('合成：保存失败也要留下')
    try:
        shell.tasks_page.save_quick()
        assert shell.tasks_page.quick_pending
        assert len(pending) == 1 and pending[0][0] == 'create'
        failed = pending[0][2]
        shell.close()
        assert not shell.closed and shell.isVisible()
        failed({'message': 'Synthetic failed save'})
        assert not shell.tasks_page.quick_pending
        app_v2.processEvents()
        assert shell.isVisible() and not shell.closed
        assert shell.tasks_page.quick_input.text() == '合成：保存失败也要留下'
    finally:
        # The command above is a captured synthetic callback, never a real
        # outstanding write. Restore its transient state even if an assertion
        # fails before the callback runs, then let the original failure surface.
        shell.tasks_page.quick_pending = False
        shell.tasks_page.quick_input.clear()


def test_today_plan_to_review_has_two_choices_and_no_duplicate_questionnaires(app_v2, shell):
    client = Client(shell.data_dir, autostart=False)
    day = QDate.currentDate().toString("yyyy-MM-dd")
    first = create(client, "task", "合成：完成一项明确练习", {"completion_gate": "完成两题并核对"})
    second = create(client, "task", "合成：整理一个要点", {"completion_gate": "写下一个可核对要点"})
    client.command("create_plan", {"date": day, "mode": "no_precise_time", "blocks": [{"target_id": first["id"], "minutes": 20}, {"target_id": second["id"], "minutes": 15}]})
    shell.navigate("today")
    wait(app_v2, lambda: shell.today_page.has_plan and not shell.bridge.callbacks)
    assert first["title"] in text_in(shell.today_page)
    assert {"完成", "未完成"} <= {b.text() for b in shell.today_page.findChildren(QPushButton)}
    shell.open_review(day)
    wait(app_v2, lambda: first["id"] in shell.review_page.item_buttons and not shell.bridge.callbacks)
    buttons = shell.review_page.item_buttons[first["id"]]
    assert set(buttons) == {"done", "incomplete"}
    QTest.mouseClick(buttons["done"], Qt.MouseButton.LeftButton)
    assert shell.review_page.confirm_button.isEnabled()
    QTest.mouseClick(shell.review_page.confirm_button, Qt.MouseButton.LeftButton)
    wait(app_v2, lambda: not shell.bridge.callbacks and not shell.review_page.confirm_button.isEnabled())
    review = client.query("daily_review", date=day)
    assert {key: review["summary"][key] for key in ("total", "done", "incomplete", "unreported")} == {"total": 2, "done": 1, "incomplete": 0, "unreported": 1}
    facts = client.query("list", type="feedback")["items"]
    assert len(facts) == 1 and facts[0]["data"]["dimensions"] == {"completion": "done"}
    for _ in range(3):
        shell.open_review(day)
        wait(app_v2, lambda: not shell.bridge.callbacks)
    assert client.query("list", type="feedback")["total"] == 1
    assert client.query("list", type="checkin")["total"] == 0

def test_hierarchy_browse_and_move_keep_identity_and_reject_cycle(app_v2, shell):
    client = Client(shell.data_dir, autostart=False)
    project = create(client, "project", "合成项目甲")
    child = create(client, "project", "合成子项目", parent_id=project["id"])
    destination = create(client, "project", "合成项目乙")
    shell.navigate("projects")
    wait(app_v2, lambda: not shell.bridge.callbacks)
    tree = shell.workspace_page.tree
    parent_item = next(item for item in tree.iter_items() if (item.data(0, Qt.ItemDataRole.UserRole) or {}).get("id") == project["id"])
    parent_item.setExpanded(True)
    wait(app_v2, lambda: any((item.data(0, Qt.ItemDataRole.UserRole) or {}).get("id") == child["id"] for item in tree.iter_items()) and not shell.bridge.callbacks)
    tree.request_move(project, child)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    assert client.query("get", id=project["id"])["entity"]["parent_id"] is None
    assert shell.notice.isVisible()
    tree.request_move(child, destination)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    moved = client.query("get", id=child["id"])["entity"]
    assert moved["id"] == child["id"]
    assert moved["parent_id"] == destination["id"]
    shell.workspace_page.open_object(destination)
    wait(app_v2, lambda: shell.workspace_page.current_entity["id"] == destination["id"] and not shell.bridge.callbacks)
    assert child["title"] in text_in(shell.workspace_page.content)

def test_file_drop_saves_copy_and_original_removal_does_not_break_open(app_v2, shell, tmp_path, monkeypatch):
    client = Client(shell.data_dir, autostart=False)
    course = create(client, "course", "合成课程")
    original = tmp_path / "synthetic-original.txt"
    original.write_text("only synthetic test material", encoding="utf-8")
    shell.navigate("projects")
    shell.workspace_page.open_object(course)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(original))])
    event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    shell.workspace_page.dropEvent(event)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    from management.gui_sources import AddSourceDialog
    captures = [dialog for dialog in shell.workspace_page.dialogs if isinstance(dialog, AddSourceDialog) and dialog.isVisible()]
    assert len(captures) == 1
    capture = captures[0]
    assert capture.destination.ready
    assert client.query("object_workspace", id=course["id"])["files"] == []
    capture.destination.combo.setEditText('课件')
    capture.save()
    wait(app_v2, lambda: capture.completed and not shell.bridge.callbacks)
    assert '课件' in capture.locations.toPlainText()
    capture.accept()
    result = client.query("object_workspace", id=course["id"])
    assert len(result["files"]) == 1
    file = result["files"][0]
    assert not file["reference_only"] and file["exists"]
    assert file["data"]["managed_copy"]
    assert original.read_text(encoding="utf-8") == "only synthetic test material"
    original.unlink()
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    shell.workspace_page.open_file(file["id"])
    wait(app_v2, lambda: bool(opened))
    assert Path(opened[0]) != original
    assert Path(opened[0]).read_text(encoding="utf-8") == "only synthetic test material"

def test_progress_uses_explicit_count_and_unknown_never_becomes_failed(app_v2):
    card = CountProgress()
    card.set_counts({"total_tasks": 6, "done_tasks": 2, "incomplete_tasks": 1, "unknown_tasks": 3})
    assert card.bar.maximum() == 6 and card.bar.value() == 2
    assert card.count.text() == "明确完成 2 / 6 项"
    assert "3 项尚未反馈" in card.explanation.text()
    assert "%" not in card.count.text()
    card.set_counts({"total_tasks": 0, "done_tasks": 0, "unknown_tasks": 0})
    assert card.bar.isHidden()
    card.set_counts({"total_tasks": None, "done_tasks": 0})
    assert card.bar.isHidden()
    assert "尚无可计算进度" in card.count.text()



def complete_modal_form(app, edit):
    from management.gui_forms import EntityForm
    failures = []
    def interact():
        dialog = QApplication.activeModalWidget()
        if not isinstance(dialog, EntityForm):
            failures.append("Expected a typed entity form")
            if dialog:
                dialog.reject()
            return
        try:
            edit(dialog)
            QTest.mouseClick(dialog.buttons.button(QDialogButtonBox.StandardButton.Save), Qt.MouseButton.LeftButton)
        except Exception as error:
            failures.append(str(error))
            dialog.reject()
    QTimer.singleShot(50, interact)
    return failures

def test_course_context_creates_fixed_event_without_tree_parent(app_v2, shell):
    client = Client(shell.data_dir, autostart=False)
    course = create(client, "course", "合成：上下文课程")
    shell.bridge.query("state")
    wait(app_v2, lambda: not shell.bridge.callbacks)
    def edit(dialog):
        assert dialog.event_owner_id == course["id"]
        assert dialog.parent_id is None
        dialog.title_edit.setText("合成：课程固定安排")
        for key, value in (("start", "10:00"), ("end", "11:00")):
            dialog.fields[key].enabled.setChecked(True)
            dialog.fields[key].editor.setText(value)
        dialog.fields["date"].enabled.setChecked(True)
        dialog.fields["date"].editor.setDate(QDate.currentDate())
        dialog.fields["hard"].enabled.setChecked(True)
        dialog.fields["hard"].editor.setChecked(True)
    failures = complete_modal_form(app_v2, edit)
    shell.create_entity("event", course)
    assert not failures
    wait(app_v2, lambda: not shell.bridge.callbacks)
    event = client.query("list", type="event")["items"][0]
    assert event["parent_id"] is None and event["data"]["owner_id"] == course["id"]
    assert event["data"]["start"] == "10:00"
    assert event["data"]["hard"] is True
    assert len(shell.create_menu.actions()) == 6

def test_extension_appears_only_in_matching_context_and_can_be_created_edited(app_v2, shell):
    client = Client(shell.data_dir, autostart=False)
    project = create(client, "project", "合成：可扩展项目")
    module = {"id": "workspaceextra", "version": 1, "types": [{"id": "workspaceextra.observation", "label": "观察记录", "section": "projects", "parent_types": ["project"], "fields": [{"id": "sample_count", "label": "样本数量", "type": "integer"}]}]}
    client.command("install_module", {"manifest": module})
    shell.load_capabilities()
    wait(app_v2, lambda: "workspaceextra.observation" in shell.type_map and not shell.bridge.callbacks)
    shell.navigate("projects")
    shell.workspace_page.open_object(project)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    add = next(button for button in shell.workspace_page.content.findChildren(QPushButton) if button.text() == "＋ 添加")
    action = next(action for action in add.menu().actions() if action.text() == "观察记录")
    def fill(dialog):
        dialog.title_edit.setText("合成：新观察")
        dialog.fields["sample_count"].enabled.setChecked(True)
        dialog.fields["sample_count"].editor.setValue(3)
    failures = complete_modal_form(app_v2, fill)
    action.trigger()
    assert not failures
    wait(app_v2, lambda: not shell.bridge.callbacks)
    record = client.query("list", type="workspaceextra.observation")["items"][0]
    assert record["parent_id"] == project["id"] and record["data"]["sample_count"] == 3
    assert "观察记录" not in [action.text() for action in shell.create_menu.actions()]
    tree = shell.workspace_page.tree
    parent = next(item for item in tree.iter_items() if (item.data(0, Qt.ItemDataRole.UserRole) or {}).get("id") == project["id"])
    parent.setExpanded(True)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    assert any((item.data(0, Qt.ItemDataRole.UserRole) or {}).get("id") == record["id"] for item in tree.iter_items())
    shell.workspace_page.open_object(record)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    assert "样本数量" in text_in(shell.workspace_page.content)
    def change(dialog):
        dialog.fields["sample_count"].editor.setValue(4)
    failures = complete_modal_form(app_v2, change)
    shell.edit_entity(record)
    assert not failures
    assert client.query("get", id=record["id"])["entity"]["data"]["sample_count"] == 4

def test_files_paginate_inside_the_owner_and_do_not_displace_tasks(app_v2, shell, tmp_path):
    client = Client(shell.data_dir, autostart=False)
    project = create(client, "project", "合成：文件分页项目")
    task = create(client, "task", "合成：始终可见的待办", parent_id=project["id"])
    for index in range(55):
        path = tmp_path / f"synthetic-file-{index:03d}.txt"
        path.write_text("synthetic", encoding="utf-8")
        client.command("attach_local_file", {"path": str(path), "owner_id": project["id"]})
    shell.navigate("projects")
    shell.workspace_page.open_object(project)
    wait(app_v2, lambda: not shell.bridge.callbacks)
    first = client.query("object_workspace", id=project["id"])
    assert first["children_total"] == 1 and first["children"][0]["id"] == task["id"]
    assert len(first["files"]) == 50 and first["files_next_offset"] == 50
    next_button = next(button for button in shell.workspace_page.content.findChildren(QPushButton) if button.text() == "下一页文件")
    # Task groups load independently; network-idle can precede Qt layout.
    wait(app_v2, lambda: next_button.height() > 0)
    QTest.mouseClick(next_button, Qt.MouseButton.LeftButton)
    wait(app_v2, lambda: shell.workspace_page.files_offset == 50 and not shell.bridge.callbacks)
    text = text_in(shell.workspace_page.content)
    assert "synthetic-file-054.txt" in text
    assert "第 51 至 55" in text

def test_inline_due_reminders_jump_to_daily_or_weekly_without_creating_questionnaires(app_v2, shell):
    client = Client(shell.data_dir, autostart=False)
    today = QDate.currentDate()
    client.command("set_review_preferences", {"timezone": "Asia/Shanghai", "daily": {"enabled": True, "time": "00:00"}, "weekly": {"enabled": True, "weekday": today.dayOfWeek() - 1, "time": "00:00"}})
    wait(app_v2, lambda: client.query("list", type="notification")["total"] >= 2, timeout=23)
    shell.navigate("today")
    wait(app_v2, lambda: not shell.bridge.callbacks)
    buttons = shell.today_page.findChildren(QPushButton)
    weekly = next(button for button in buttons if button.text() == "查看每周回顾")
    daily = next(button for button in buttons if button.text() == "前往每日复盘")
    QTest.mouseClick(weekly, Qt.MouseButton.LeftButton)
    wait(app_v2, lambda: shell.section == "reviews" and not shell.bridge.callbacks)
    assert shell.review_page.tabs.currentIndex() == 1
    shell.navigate("today")
    wait(app_v2, lambda: not shell.bridge.callbacks)
    daily = next(button for button in shell.today_page.findChildren(QPushButton) if button.text() == "前往每日复盘")
    QTest.mouseClick(daily, Qt.MouseButton.LeftButton)
    wait(app_v2, lambda: shell.section == "reviews" and not shell.bridge.callbacks)
    assert shell.review_page.tabs.currentIndex() == 0
    assert client.query("list", type="checkin")["total"] == 0
    assert client.query("list", type="feedback")["total"] == 0

def test_attention_marker_does_not_trigger_unsaved_close_confirmation(app_v2, shell, monkeypatch):
    assert shell.review_attention and not shell.review_pending
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: pytest.fail("No unsaved selection exists"))
    shell.close()
    wait(app_v2, lambda: not shell.bridge.thread.isRunning())



def test_today_warning_pagination_can_reach_all_returned_risks(app_v2):
    from management.gui_today import TodayPage
    class WarningBridge:
        def __init__(self):
            self.offsets = []
        def query(self, name, callback, error=None, **params):
            if name == "daily_review":
                callback({"has_plan": False, "summary": {"total": 0, "unreported": 0}})
            elif name == "today":
                callback({"events": [], "notifications": [], "unknowns": []})
            elif name == "warnings":
                offset = params.get("offset", 0)
                self.offsets.append(offset)
                end = min(8, offset + params["limit"])
                callback({"items": [{"title": f"合成警戒 {i}", "reason": "明确待核对事项"} for i in range(offset, end)], "total": 8, "next_offset": end if end < 8 else None})
    bridge = WarningBridge()
    page = TodayPage(bridge)
    page.show()
    page.refresh()
    assert len(page.warning_result["items"]) == 3
    button = next(button for button in page.findChildren(QPushButton) if button.text() == "更多警戒")
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert page.warning_offset == 3
    page.set_warning_page(page.warning_result["next_offset"])
    assert page.warning_offset == 6
    assert [item["title"] for item in page.warning_result["items"]] == ["合成警戒 6", "合成警戒 7"]
    assert bridge.offsets == [0, 3, 6]
    page.close()


def test_habits_guide_and_chart_preferences_apply_through_real_service(app_v2, shell, tmp_path):
    from PySide6.QtCore import QPoint
    from management.gui_workflows import HabitsDialog, SettingsDialog
    from management.gui_theme import apply_appearance
    from management.appearance import DEFAULT_APPEARANCE
    client = Client(shell.data_dir, autostart=False)
    create(client, 'task', '虚构工作任务：整理本周事项')
    habits = HabitsDialog(shell.bridge, shell.capabilities, shell)
    settings = None
    try:
        habits.open()
        wait(app_v2, lambda: not shell.bridge.callbacks)
        assert shell.onboarding.show_current()
        assert shell.onboarding.active_key == 'habits'
        shell.onboarding.overlay.skip_button.click()
        habits.close()
        settings = SettingsDialog(shell.bridge, shell.capabilities, shell.data_dir, shell, shell.load_display_preferences)
        settings.open()
        wait(app_v2, lambda: not shell.bridge.callbacks)
        settings.chart_pickers['dashboard_today_style'].setCurrentIndex(1)
        settings.chart_pickers['dashboard_tasks_style'].setCurrentIndex(1)
        settings.chart_pickers['review_daily_style'].setCurrentIndex(1)
        settings.chart_pickers['review_weekly_style'].setCurrentIndex(1)
        settings.weekly_chart_style.setCurrentIndex(2)
        settings.save_chart_style()
        wait(app_v2, lambda: not shell.bridge.callbacks)
        assert shell.dashboard_page.charts['plan'].chart_style == 'ring'
        assert shell.dashboard_page.charts['tasks'].chart_style == 'ring'
        assert shell.review_page.daily_chart.chart_style == 'ring'
        assert shell.review_page.week_days.layout_style == 'tiles'
        assert client.query('settings')['settings']['charts']['weekly_style'] == 'tiles'
        settings.close()
        shell.navigate('dashboard')
        wait(app_v2, lambda: not shell.bridge.callbacks)
        folder = Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR', str(tmp_path)))
        for theme, size in [('light', 13), ('dark', 20)]:
            apply_appearance(app_v2, {'theme': theme, 'font_size': size})
            shell.resize(1290, 850)
            app_v2.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app_v2.processEvents()
            page = shell.dashboard_page
            def chart_layout_ready():
                if page._layout_pending or page._relayout_active or page._layout_timer.isActive():
                    return False
                body = page.widget()
                heading = next(label for label in page.findChildren(QLabel) if label.text() == '本周固定日程')
                return max(card.mapTo(body, QPoint(0, 0)).y() + card.height() for card in page.metric_cards) <= heading.mapTo(body, QPoint(0, 0)).y()
            wait(app_v2, chart_layout_ready)
            assert page.horizontalScrollBar().maximum() == 0
            assert not shell.habits_button.visibleRegion().isEmpty()
            assert not shell.search_input.visibleRegion().isEmpty()
            assert shell.grab().save(str(folder / ('custom-charts-' + theme + '.png')))
            settings.open()
            settings.tabs.setCurrentIndex(settings._provider_tab)
            app_v2.processEvents()
            assert settings.grab().save(str(folder / ('assistant-settings-' + theme + '.png')))
            settings.close()
    finally:
        shell.onboarding.stop()
        if settings:
            settings.close(); settings.deleteLater()
        habits.close(); habits.deleteLater()
        apply_appearance(app_v2, DEFAULT_APPEARANCE)
        app_v2.processEvents()


def test_accent_settings_save_via_service_and_reopen(app_v2,shell,tmp_path):
    from management.gui_workflows import SettingsDialog
    from management.gui_theme import current_appearance,apply_appearance,color
    from management.gui_theme_glass import palette_tokens
    previous=current_appearance()
    client=Client(shell.data_dir,autostart=False)
    before=client.state()
    shell.onboarding.set_automatic(False)
    settings=SettingsDialog(shell.bridge,shell.capabilities,shell.data_dir,shell,shell.load_display_preferences)
    try:
        settings.open();wait(app_v2,lambda:not shell.bridge.callbacks)
        settings.theme_picker.setCurrentIndex(settings.theme_picker.findData('dark'))
        settings.accent_picker.buttons[settings.accent_picker.findData('violet')].click()
        assert client.state()['revision']==before['revision']
        settings.chart_save.click()
        wait(app_v2,lambda:not shell.bridge.callbacks and not settings._display_saving)
        saved=Client(shell.data_dir,autostart=False).query('settings')['settings']['appearance']
        assert saved['theme']=='dark' and saved['accent']=='violet'
        assert current_appearance()['accent']=='violet'
        assert color('primary')==palette_tokens('dark','violet')['primary']
        settings.close();settings.deleteLater();app_v2.processEvents()
        settings=SettingsDialog(shell.bridge,shell.capabilities,shell.data_dir,shell)
        settings.open();wait(app_v2,lambda:not shell.bridge.callbacks)
        assert settings.accent_picker.currentData()=='violet'
        assert settings.theme_picker.currentData()=='dark'
        assert not settings.display_dirty
        report=Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR',str(tmp_path)))
        settings.repaint()
        assert settings.grab().save(str(report/'settings-saved-violet.png'))
    finally:
        settings.close();settings.deleteLater();app_v2.processEvents()
        apply_appearance(app_v2,previous)


def test_real_navigation_renders_distinct_entrance_frames_after_layout_and_settings_refresh(app_v2, shell, tmp_path, monkeypatch):
    """Exercise the actual nav/notice/layout/service path, not a bare stack timer."""
    from PySide6.QtCore import QAbstractAnimation
    from management import gui_materials
    from management.gui_theme import apply_appearance, current_appearance
    from management.gui_visual_profile import visual_style

    previous_style, previous_appearance = visual_style(), current_appearance()
    automatic = shell.onboarding.automatic
    monkeypatch.setattr(gui_materials, 'reduce_motion', lambda: False)
    client = Client(shell.data_dir, autostart=False)
    before = client.state()
    report = Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR', str(tmp_path)))

    def capture_frame():
        # A child grab omits the AppCanvas ancestor, producing transparent/black
        # final pixels. Crop the real composed window instead, in device pixels.
        composed = shell.grab().toImage()
        origin = shell.pages.mapTo(shell, QPoint())
        ratio = composed.devicePixelRatio()
        left, top = round(origin.x() * ratio), round(origin.y() * ratio)
        right = round((origin.x() + shell.pages.width()) * ratio)
        bottom = round((origin.y() + shell.pages.height()) * ratio)
        return composed.copy(left, top, right - left, bottom - top)

    try:
        shell.onboarding.set_automatic(False)
        shell.apply_visual_style('glass')
        apply_appearance(app_v2, shell._saved_appearance)
        shell.navigate('dashboard')
        wait(app_v2, lambda: not shell.bridge.callbacks and not shell.page_transition.is_active())
        shell.tasks_page.quick_input.setText('Unsaved text remains while the page appears')
        # navigate() hides this real banner after changing the stack page. This
        # produces the delayed layout resize that used to cancel the animation.
        shell.notice.setText('Temporary visual acceptance notice')
        shell.notice.show()
        app_v2.processEvents()
        QTest.mouseClick(shell.nav_buttons['tasks'], Qt.MouseButton.LeftButton)
        wait(app_v2, lambda: shell.page_transition.animation.state() == QAbstractAnimation.State.Running
             and .90 < shell.page_transition._veil.opacity < 1.0)
        assert shell.section == 'tasks' and shell.page_transition._veil.isVisible()
        shell.load_display_preferences()
        early = capture_frame()
        frames = [early]
        wait(app_v2, lambda: .75 < shell.page_transition._veil.opacity < .90)
        frames.append(capture_frame())
        wait(app_v2, lambda: .40 < shell.page_transition._veil.opacity < .65)
        middle = capture_frame()
        frames.append(middle)
        assert shell.page_transition._veil.isVisible()
        for _ in range(3):
            QTest.qWait(40)
            frames.append(capture_frame())
        wait(app_v2, lambda: not shell.page_transition.is_active() and not shell.bridge.callbacks)
        final = capture_frame()
        frames.append(final)

        def frame_difference(first, second):
            first, second = first.scaled(120, 90), second.scaled(120, 90)
            changed, total = 0, 0.0
            for y in range(first.height()):
                for x in range(first.width()):
                    a, b = first.pixelColor(x, y), second.pixelColor(x, y)
                    delta = sum(abs(left - right) for left, right in zip(a.getRgb()[:3], b.getRgb()[:3])) / 3
                    changed += delta > 10
                    total += delta
            pixels = first.width() * first.height()
            return changed / pixels, total / pixels

        early_change, early_mean = frame_difference(early, final)
        middle_change, middle_mean = frame_difference(middle, final)
        assert early_change > .04 and early_mean > 1.0, (early_change, early_mean)
        assert middle_change > .02 and middle_mean > .5, (middle_change, middle_mean)
        assert early != middle != final
        for name, frame in (('early', early), ('middle', middle), ('final', final)):
            assert frame.save(str(report / f'navigation-entrance-{name}.png'))
        try:
            from PIL import Image
        except ImportError:
            pass
        else:
            from PySide6.QtGui import QImage
            images = []
            for frame in frames:
                rgba = frame.convertToFormat(QImage.Format.Format_RGBA8888)
                images.append(Image.frombytes('RGBA', (rgba.width(), rgba.height()), bytes(rgba.constBits())).convert('RGB'))
            images[0].save(report / 'navigation-entrance.gif', save_all=True,
                           append_images=images[1:], duration=[45, 55, 40, 40, 40, 40, 850], loop=0, disposal=2)
        assert shell.tasks_page.quick_input.text() == 'Unsaved text remains while the page appears'
        assert shell.page_transition.animation.state() == QAbstractAnimation.State.Stopped
        assert not shell.page_transition._settle_timer.isActive()
        after = client.state()
        assert (after['epoch'], after['revision']) == (before['epoch'], before['revision'])
    finally:
        shell.tasks_page.quick_input.clear()
        shell.notice.hide()
        shell.page_transition.stop()
        shell.apply_visual_style(previous_style)
        apply_appearance(app_v2, previous_appearance)
        shell.onboarding.set_automatic(automatic)


@pytest.mark.parametrize('area,theme', [('settings', 'light'), ('reviews', 'dark')])
def test_real_tab_changes_render_entrance_frames_without_losing_unsaved_work(app_v2, shell, tmp_path, monkeypatch, area, theme):
    from PySide6.QtCore import QAbstractAnimation
    from management import gui_materials
    from management.gui_theme import apply_appearance, current_appearance
    from management.gui_visual_profile import visual_style
    from management.gui_workflows import SettingsDialog

    previous_style, previous_appearance = visual_style(), current_appearance()
    automatic = shell.onboarding.automatic
    monkeypatch.setattr(gui_materials, 'reduce_motion', lambda: False)
    client = Client(shell.data_dir, autostart=False)
    settings = None
    report = Path(os.environ.get('PERSONAL_MANAGEMENT_CHECK_REPORT_DIR', str(tmp_path)))

    def capture_widget(host, widget):
        image = host.grab().toImage()
        return crop_widget(image, host, widget)

    def crop_widget(image, host, widget):
        origin = widget.mapTo(host, QPoint())
        ratio = image.devicePixelRatio()
        x, y = round(origin.x() * ratio), round(origin.y() * ratio)
        return image.copy(x, y, round(widget.width() * ratio), round(widget.height() * ratio))

    def visible_difference(first, second):
        first, second = first.scaled(120, 90), second.scaled(120, 90)
        changed, total = 0, 0.0
        for y in range(first.height()):
            for x in range(first.width()):
                left, right = first.pixelColor(x, y), second.pixelColor(x, y)
                delta = sum(abs(a - b) for a, b in zip(left.getRgb()[:3], right.getRgb()[:3])) / 3
                changed += delta > 10
                total += delta
        pixels = first.width() * first.height()
        return changed / pixels, total / pixels

    def switch_and_capture(tabs, transition, host, index, name):
        bar = tabs.tabBar()
        QTest.mouseClick(tabs.tabBar(), Qt.MouseButton.LeftButton, pos=tabs.tabBar().tabRect(index).center())
        assert bar.animation.state() == QAbstractAnimation.State.Running
        bar_early = capture_widget(host, bar)
        frames = {}
        frame_states = []

        def collect_frames():
            # The label starts immediately, while content first waits 24–96ms
            # for layout. Observe both clocks independently; a content midpoint
            # is not necessarily a midpoint of the label's earlier animation.
            requests = []
            label_running = bar.animation.state() == QAbstractAnimation.State.Running
            content_running = transition.animation.state() == QAbstractAnimation.State.Running
            frame_states.append((label_running, round(bar._progress, 3),
                                 content_running, round(transition._veil.opacity, 3)))
            if 'bar_middle' not in frames and label_running and .30 < bar._progress < .65:
                requests.append(('bar_middle', bar))
            if 'early' not in frames and content_running and .90 < transition._veil.opacity < 1.0:
                requests.append(('early', transition._stack()))
            if 'middle' not in frames and content_running and .30 < transition._veil.opacity < .65:
                requests.append(('middle', transition._stack()))
            if requests:
                composed = host.grab().toImage()
                for frame_name, widget in requests:
                    frames[frame_name] = crop_widget(composed, host, widget)
            if not label_running and 'bar_middle' not in frames:
                raise AssertionError((name, 'label stopped before its own midpoint was rendered', frame_states))
            return all(key in frames for key in ('bar_middle', 'early', 'middle'))

        wait(app_v2, collect_frames)
        assert tabs.currentIndex() == index
        early, middle, bar_middle = frames['early'], frames['middle'], frames['bar_middle']
        wait(app_v2, lambda: not transition.is_active() and not shell.bridge.callbacks
             and bar.animation.state() == QAbstractAnimation.State.Stopped)
        final = capture_widget(host, transition._stack())
        bar_final = capture_widget(host, bar)
        early_changed, early_mean = visible_difference(early, final)
        middle_changed, middle_mean = visible_difference(middle, final)
        assert early_changed > .01 and early_mean > .5, (name, early_changed, early_mean)
        assert middle_changed > .005 and middle_mean > .2, (name, middle_changed, middle_mean)
        assert early != middle != final
        for moment, image in (('early', early), ('middle', middle), ('final', final)):
            assert image.save(str(report / f'tab-entrance-{name}-{moment}.png'))
        _, bar_early_mean = visible_difference(bar_early, bar_final)
        _, bar_middle_mean = visible_difference(bar_middle, bar_final)
        assert bar_early_mean > .5 and bar_middle_mean > .1, (name, bar_early_mean, bar_middle_mean)
        assert bar_early != bar_middle != bar_final
        for moment, image in (('early', bar_early), ('middle', bar_middle), ('final', bar_final)):
            assert image.save(str(report / f'tab-selection-{name}-{moment}.png'))
        assert transition._veil.snapshot.isNull()

    try:
        shell.onboarding.set_automatic(False)
        shell.apply_visual_style('glass')
        apply_appearance(app_v2, {**shell._saved_appearance, 'theme': theme})
        if area == 'settings':
            settings = SettingsDialog(shell.bridge, shell.capabilities, shell.data_dir, shell)
            settings.open()
            wait(app_v2, lambda: not shell.bridge.callbacks)
            settings.font_size.setValue(17)
            assert settings.display_dirty
            field = settings.font_size
            scroll = settings.tabs.widget(0).verticalScrollBar()
            scroll.setValue(min(90, scroll.maximum()))
            position = scroll.value()
            assert position > 0
            before = client.state()
            for index, name in ((1, 'settings-timetable'), (2, 'settings-assistance'),
                                (3, 'settings-data'), (0, 'settings-display')):
                switch_and_capture(settings.tabs, settings.tab_transition, settings, index, name)
            assert settings.font_size is field and field.value() == 17
            assert settings.display_dirty and scroll.value() == position
            settings.tabs.setCurrentIndex(3)
            wait(app_v2, lambda: not settings.tab_transition.is_active())
            advanced = next(button for button in settings.findChildren(QPushButton)
                            if button.text() == '高级规则与扩展')
            advanced.click()
            app_v2.processEvents()
            settings.tabs.widget(3).ensureWidgetVisible(settings.advanced_tabs)
            switch_and_capture(settings.advanced_tabs, settings.advanced_tab_transition,
                               settings, 1, 'settings-extensions')
        else:
            day = QDate.currentDate().toString('yyyy-MM-dd')
            item = create(client, 'task', '虚构动画验收事项', {'estimated_minutes': 20, 'due_date': day})
            client.command('create_plan', {'date': day, 'mode': 'no_precise_time',
                                          'blocks': [{'target_id': item['id'], 'minutes': 20}]})
            shell.navigate('reviews')
            page = shell.review_page
            wait(app_v2, lambda: not shell.bridge.callbacks and not shell.page_transition.is_active()
                 and item['id'] in page.item_buttons)
            page.item_buttons[item['id']]['done'].click()
            choice_button = page.item_buttons[item['id']]['done']
            scroll = page.scroll.verticalScrollBar()
            scroll.setValue(min(90, scroll.maximum()))
            position = scroll.value()
            before = client.state()
            switch_and_capture(page.tabs, page.tab_transition, shell, 1, 'review-weekly')
            switch_and_capture(page.tabs, page.tab_transition, shell, 0, 'review-daily')
            assert page.item_buttons[item['id']]['done'] is choice_button
            assert choice_button.isChecked() and page._pending
            assert page._choices[day][item['id']] == 'done'
            assert scroll.value() == position
        after = client.state()
        assert (after['epoch'], after['revision']) == (before['epoch'], before['revision'])
    finally:
        if settings is not None:
            settings.close()
            settings.deleteLater()
        shell.review_page._choices.clear()
        shell.review_page._update_actions()
        shell.review_pending = False
        shell.review_page.tab_transition.stop()
        shell.apply_visual_style(previous_style)
        apply_appearance(app_v2, previous_appearance)
        shell.onboarding.set_automatic(automatic)
