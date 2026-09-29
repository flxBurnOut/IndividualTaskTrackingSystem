"""Tutorial scheduling and local progress must never interrupt business editing."""
import json
import os
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton, QVBoxLayout, QWidget
import pytest

from management import gui_onboarding
from management.gui_onboarding import OnboardingManager
from management.gui_onboarding_overlay import TourStep


class FakeOverlay(QWidget):
    """Keep the scheduling contract independent of spotlight painting tests."""
    finished = Signal(str)

    def __init__(self, host, steps):
        super().__init__(host)
        self.steps = steps
        self.reason = None

    def start(self):
        self.show()

    def finish(self, reason):
        if self.reason is None:
            self.reason = reason
            self.hide()
            self.finished.emit(reason)

    def stop(self):
        self.finish('interrupted')


@pytest.fixture
def ui(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv('PERSONAL_MANAGEMENT_NO_ONBOARDING', '1')
    monkeypatch.setattr(gui_onboarding, 'TutorialOverlay', FakeOverlay)
    monkeypatch.setattr(OnboardingManager, 'INITIAL_DELAY', 0.03)
    windows = []

    def create(name='space'):
        window = QWidget()
        window.resize(700, 500)
        layout = QVBoxLayout(window)
        panel = QWidget(window)
        panel_layout = QVBoxLayout(panel)
        edit = QLineEdit(panel)
        button = QPushButton('功能', panel)
        panel_layout.addWidget(edit)
        panel_layout.addWidget(button)
        layout.addWidget(panel)
        manager = OnboardingManager(window, tmp_path / name)
        manager._timer.setInterval(10)
        window.manager = manager
        window.panel, window.edit, window.button = panel, edit, button
        window.show()
        window.activateWindow()
        app.processEvents()
        windows.append(window)
        return window, manager

    yield app, create, tmp_path
    for window in reversed(windows):
        window.manager.stop()
        window.close()
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def wait(app, predicate, timeout=1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate()


def steps(widget):
    return lambda: [TourStep(widget, '介绍', '这里介绍功能，不执行功能。')]


@pytest.mark.parametrize('reason', ['completed', 'skipped'])
def test_progress_persists_per_panel_and_per_data_space(ui, reason):
    app, create, root = ui
    window, manager = create()
    manager.watch('today', window.panel, steps(window.button))
    assert manager.show('today')
    manager.overlay.finish(reason)
    saved = json.loads((root / 'space' / 'ui-onboarding.json').read_text(encoding='utf-8'))
    assert saved == {'version': 1, 'automatic': True, 'seen': {'today': reason}}
    assert list((root / 'space').iterdir()) == [root / 'space' / 'ui-onboarding.json']
    reopened, loaded = create()
    loaded.watch('today', reopened.panel, steps(reopened.button))
    assert loaded._seen == {'today': reason}
    assert loaded.show('today'), 'Completed tutorials remain manually replayable.'
    _, other = create('other-data-space')
    assert other._seen == {}


def test_environment_disables_only_automatic_and_global_disable_is_saved(ui):
    app, create, root = ui
    window, manager = create()
    manager.watch('today', window.panel, steps(window.button))
    QTest.qWait(90)
    assert manager.overlay is None
    assert manager.show_current()
    manager.overlay.finish('disabled')
    assert not manager.automatic
    assert json.loads((root / 'space' / 'ui-onboarding.json').read_text(encoding='utf-8'))['automatic'] is False
    assert manager.show('today')
    manager.overlay.finish('completed')
    _, loaded = create()
    assert not loaded.automatic


def test_first_show_waits_for_data_but_user_typing_cancels_that_visit(ui, monkeypatch):
    app, create, _ = ui
    monkeypatch.delenv('PERSONAL_MANAGEMENT_NO_ONBOARDING')
    window, manager = create()
    ready = {'value': False}
    manager.watch('today', window.panel, steps(window.button), ready=lambda: ready['value'])
    QTest.qWait(60)
    assert manager.overlay is None
    window.edit.setFocus()
    QTest.keyClicks(window.edit, 'unfinished note')
    ready['value'] = True
    QTest.qWait(90)
    assert manager.overlay is None
    assert window.edit.text() == 'unfinished note'
    assert 'today' not in manager._seen
    window.panel.hide()
    window.panel.show()
    wait(app, lambda: manager.active_key == 'today')


def test_readiness_and_same_window_completion_never_chain_guides(ui, monkeypatch):
    app, create, _ = ui
    monkeypatch.delenv('PERSONAL_MANAGEMENT_NO_ONBOARDING')
    window, manager = create()
    ready = {'value': False}
    manager.watch('shell', window, steps(window.button), ready=lambda: ready['value'])
    manager.watch('today', window.panel, steps(window.button), ready=lambda: ready['value'])
    QTest.qWait(70)
    assert manager.overlay is None
    ready['value'] = True
    wait(app, lambda: manager.active_key == 'shell')
    manager.overlay.finish('completed')
    QTest.qWait(90)
    assert manager.overlay is None
    assert manager._seen == {'shell': 'completed'}


def test_automatic_guide_waits_for_real_window_focus(ui, monkeypatch):
    app, create, _ = ui
    monkeypatch.delenv('PERSONAL_MANAGEMENT_NO_ONBOARDING')
    window, manager = create()
    app.setActiveWindow(None)
    app.processEvents()
    manager.watch('today', window.panel, steps(window.button))
    QTest.qWait(90)
    assert manager.overlay is None
    assert not manager.show('today')
    app.setActiveWindow(window)
    wait(app, lambda: manager.active_key == 'today')


def test_manual_replay_chooses_the_current_specific_panel(ui):
    app, create, _ = ui
    window, manager = create()
    manager.watch('shell', window, steps(window.button))
    manager.watch('today', window.panel, steps(window.button))
    assert manager.show_current()
    assert manager.active_key == 'today'
    window.panel.hide()
    assert manager.overlay is None
    assert manager.show_current()
    assert manager.active_key == 'shell'


def test_explicit_replay_is_available_while_panel_data_is_refreshing(ui):
    _, create, _ = ui
    window, manager = create()
    manager.watch('today', window.panel, steps(window.button), ready=lambda: False)
    assert manager.show('today')
    manager.overlay.stop()
    assert manager.show_current()
    assert manager.active_key == 'today'


def test_other_window_and_modal_dialog_interrupt_without_marking_seen(ui):
    app, create, _ = ui
    window, manager = create()
    manager.watch('today', window.panel, steps(window.button))
    assert manager.show('today')
    original = manager.overlay
    dialog = QDialog(window)
    dialog.setModal(True)
    child = QPushButton('保存', dialog)
    manager.watch('settings', dialog, steps(child))
    dialog.show()
    dialog.activateWindow()
    app.processEvents()
    assert original.reason == 'interrupted'
    assert not manager.show('today')
    assert manager.show_current()
    assert manager.active_key == 'settings'
    dialog.close()
    app.processEvents()
    assert manager.overlay is None
    assert manager._seen == {}
    window.activateWindow()
    app.processEvents()
    assert manager.show('today')
    other, _ = create('another')
    other.activateWindow()
    app.processEvents()
    assert manager.overlay is None
    assert not manager.show('today')
    assert manager._seen == {}
    dialog.deleteLater()


def test_reopened_dialog_reuses_key_and_old_destruction_cannot_remove_it(ui):
    app, create, _ = ui
    window, manager = create()
    old = QWidget(window.panel)
    old.show()
    manager.watch('reusable-dialog', old, steps(window.button))
    current = QWidget(window.panel)
    current.show()
    manager.watch('reusable-dialog', current, steps(window.button))
    old.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    assert manager.show('reusable-dialog')
    current.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    assert manager.overlay is None
    assert not manager.show('reusable-dialog')
    assert 'reusable-dialog' not in manager._watches


def test_corrupt_preferences_report_nonfatal_error_and_can_be_replaced(ui):
    app, create, root = ui
    (root / 'space').mkdir()
    path = root / 'space' / 'ui-onboarding.json'
    path.write_text('{truncated', encoding='utf-8')
    window, manager = create()
    errors = []
    # create() processes events; capture a second load to inspect the user notice.
    manager.error.connect(errors.append)
    manager._reported_errors.clear()
    manager._load()
    wait(app, lambda: bool(errors))
    assert '个人资料不受影响' in errors[0]
    manager.watch('today', window.panel, steps(window.button))
    assert manager.show('today')
    manager.overlay.finish('completed')
    assert json.loads(path.read_text(encoding='utf-8'))['seen'] == {'today': 'completed'}


def test_save_failure_keeps_previous_file_and_remembers_progress_in_process(ui, monkeypatch):
    app, create, root = ui
    window, manager = create()
    manager.set_automatic(False)
    path = root / 'space' / 'ui-onboarding.json'
    before = path.read_bytes()
    errors = []
    manager.error.connect(errors.append)

    def denied(*args):
        raise PermissionError('read-only preferences')

    monkeypatch.setattr(gui_onboarding.os, 'replace', denied)
    manager.watch('today', window.panel, steps(window.button))
    assert manager.show('today')
    manager.overlay.finish('skipped')
    wait(app, lambda: bool(errors))
    assert manager._seen == {'today': 'skipped'}
    assert path.read_bytes() == before
    assert not list(path.parent.glob('.ui-onboarding-*.tmp'))


def test_future_preferences_are_not_downgraded_or_overwritten(ui):
    _, create, root = ui
    (root / 'space').mkdir()
    path = root / 'space' / 'ui-onboarding.json'
    before = '{"version": 2, "automatic": false, "future": "keep"}'
    path.write_text(before, encoding='utf-8')
    _, manager = create()
    manager.set_automatic(False)
    assert path.read_text(encoding='utf-8') == before


def test_stopping_detaches_pending_and_active_guides_without_business_writes(ui, monkeypatch):
    app, create, root = ui
    monkeypatch.delenv('PERSONAL_MANAGEMENT_NO_ONBOARDING')
    window, manager = create()
    manager.watch('today', window.panel, steps(window.button))
    assert manager.show('today')
    overlay = manager.overlay
    manager.stop()
    assert overlay.reason == 'interrupted'
    window.hide()
    window.show()
    QTest.qWait(90)
    assert manager.overlay is None
    assert not manager.show_current()
    assert not (root / 'space').exists()
