"""The GUI owner thread continuously reclaims cycles; workers never do so."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication
from management.gui_gc import GuiGarbageCollector


class FakeGC:
    def __init__(self, enabled=True, thresholds=(7, 3, 2)):
        self.enabled, self.thresholds = enabled, thresholds
        self.counts = (0, 0, 0)
        self.calls = []
        self.on_collect = None
    def isenabled(self): return self.enabled
    def enable(self): self.enabled = True
    def disable(self): self.enabled = False
    def get_threshold(self): return self.thresholds
    def set_threshold(self, *value): self.thresholds = value
    def get_count(self): return self.counts
    def collect(self, generation):
        self.calls.append(generation)
        self.counts = (0, 0, 0)
        if self.on_collect: self.on_collect()
        return 0


@pytest.mark.parametrize("original_enabled", [True, False])
def test_generations_periodic_full_and_exact_state_restore(original_enabled):
    app = QApplication.instance() or QApplication([])
    collector = FakeGC(original_enabled)
    now = [10.0]
    manager = GuiGarbageCollector(app, collector=collector, clock=lambda: now[0])
    try:
        assert collector.calls == [2] and not collector.enabled
        for counts, generation in [((7, 0, 0), 0), ((7, 3, 0), 1), ((7, 3, 2), 2)]:
            collector.counts = counts
            manager.poll()
            assert collector.calls[-1] == generation
        now[0] += 60
        manager.poll()
        assert collector.calls == [2, 0, 1, 2, 2]
        # Re-entrant event pumping from a finalizer must not nest collections.
        collector.on_collect = manager.poll
        now[0] += 60
        manager.poll()
        assert len(collector.calls) == 6
        assert manager.shutdown()
        assert collector.enabled == original_enabled
        assert collector.thresholds == (7, 3, 2)
        assert not manager.timer.isActive()
        assert manager.shutdown()
    finally:
        manager.shutdown()
        manager.deleteLater()


def test_zero_allocation_threshold_keeps_periodic_full_collection():
    app = QApplication.instance() or QApplication([])
    collector = FakeGC(False, (0, 5, 8))
    now = [0.0]
    manager = GuiGarbageCollector(app, collector=collector, clock=lambda: now[0])
    try:
        collector.counts = (100000, 50, 50)
        manager.poll()
        assert collector.calls == [2]
        now[0] = 61
        manager.poll()
        assert collector.calls == [2, 2]
        manager.shutdown()
        assert collector.thresholds == (0, 5, 8) and not collector.enabled
    finally:
        manager.shutdown()
        manager.deleteLater()


def isolated(tmp_path, code):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, "-X", "faulthandler", "-c", textwrap.dedent(code), str(tmp_path)],
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_real_worker_allocations_do_not_collect_and_timer_releases_widget_cycles(tmp_path):
    output = isolated(tmp_path, r"""
        import gc, threading, time, tracemalloc, weakref
        from PySide6.QtWidgets import QApplication, QWidget
        from management.gui_gc import install_gui_gc
        app = QApplication([])
        original = gc.isenabled(), gc.get_threshold()
        owner = threading.get_ident()
        manager = install_gui_gc(app)
        assert manager is install_gui_gc(app)
        manager.timer.setInterval(5)
        manager.full_interval_seconds = .04
        collected_on = []
        gc.callbacks.append(lambda phase, info: collected_on.append(threading.get_ident()) if phase == 'start' else None)
        finalized_on = []
        class Window(QWidget):
            def __init__(self):
                super().__init__()
                self.cycle = [self]
                self.payload = bytearray(65536)
            def __del__(self): finalized_on.append(threading.get_ident())
        def allocate():
            for index in range(20000):
                cycle = []; cycle.append(cycle)
        tracemalloc.start()
        retained = []
        for round_index in range(8):
            refs = []
            for index in range(20):
                window = Window(); window.close(); refs.append(weakref.ref(window)); del window
            before = len(collected_on)
            worker = threading.Thread(target=allocate)
            worker.start(); worker.join()
            assert len(collected_on) == before, 'Worker allocations triggered cyclic collection'
            until = time.monotonic() + 4
            while any(ref() is not None for ref in refs) and time.monotonic() < until:
                app.processEvents(); time.sleep(.005)
            assert all(ref() is None for ref in refs), 'Timer failed to reclaim closed widget cycles'
            retained.append(tracemalloc.get_traced_memory()[0])
        assert len(finalized_on) == 160 and set(finalized_on) == {owner}
        assert set(collected_on) == {owner}
        assert retained[-1] - retained[0] < 2 * 1024 * 1024, retained
        assert sum(manager.collection_counts) >= 8
        gc.callbacks.clear()
        assert manager.shutdown()
        assert (gc.isenabled(), gc.get_threshold()) == original
        manager2 = install_gui_gc(app)
        assert manager2 is not manager
        manager2.shutdown()
        print({'windows_reclaimed': len(finalized_on), 'retained_bytes': retained, 'collections': manager.collection_counts})
    """)
    assert "windows_reclaimed" in output


@pytest.mark.parametrize("original_enabled", [True, False])
def test_real_quit_waits_for_persisted_receipts_and_restores_state(tmp_path, original_enabled):
    output = isolated(tmp_path, r"""
        import gc, json, sys, time, uuid
        from pathlib import Path
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from management.core import Core
        from management.gui_async import ServiceBridge
        from management.gui_gc import install_gui_gc
        ORIGINAL_ENABLED
        original = gc.isenabled(), gc.get_threshold()
        app = QApplication([])
        app.setQuitOnLastWindowClosed(False)
        core = Core(Path(sys.argv[1])/'synthetic')
        received = []
        callback_gc = []
        class SlowClient:
            def __init__(self, data_dir): pass
            def command(self, name, payload, **options):
                time.sleep(.16)
                state = core.query('state')
                return core.command(name, payload, request_id=options['request_id'], epoch=state['epoch'], expected_revision=state['revision'])
        bridge = ServiceBridge(core.root, client_factory=SlowClient)
        manager = install_gui_gc(app)
        manager.timer.setInterval(10)
        manager.full_interval_seconds = .03
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        def accepted(result):
            received.append(result)
            callback_gc.append(gc.isenabled())
        for number, rid in enumerate(ids):
            bridge.command('create', {'type':'task','title':f'Synthetic task {number}','data':{}}, accepted, request_id=rid)
        QTimer.singleShot(20, app.quit)
        started = time.monotonic()
        app.exec()
        elapsed = time.monotonic()-started
        assert len(received) == 2 and callback_gc == [False, False]
        assert elapsed >= .30 and elapsed < 5
        assert not bridge.workers_running() and not manager.active
        assert (gc.isenabled(), gc.get_threshold()) == original
        assert all(core.query('receipt', request_id=rid)['found'] for rid in ids)
        print({'receipts':len(received), 'elapsed_seconds':elapsed, 'full_collections':manager.collection_counts[2]})
    """.replace("ORIGINAL_ENABLED", "gc.enable()" if original_enabled else "gc.disable()"))
    assert "'receipts': 2" in output


def test_reentrant_first_install_reuses_controller_before_initial_collection(tmp_path):
    isolated(tmp_path, r"""
        import gc
        from PySide6.QtWidgets import QApplication
        from management.gui_gc import install_gui_gc, GuiGarbageCollector
        app = QApplication([])
        gc.enable()
        original = gc.get_threshold()
        nested = []
        def during_collection(phase, info):
            if phase == 'start' and not nested:
                nested.append(install_gui_gc(app))
        gc.callbacks.append(during_collection)
        manager = install_gui_gc(app)
        gc.callbacks.remove(during_collection)
        assert nested == [manager]
        assert len(app.findChildren(GuiGarbageCollector)) == 1
        assert manager.original_enabled is True and manager.timer.isActive()
        manager.shutdown()
        assert gc.isenabled() and gc.get_threshold() == original
    """)


def test_quit_finishes_when_closed_view_error_callback_raises(tmp_path):
    isolated(tmp_path, r"""
        import gc, sys, time
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from management.gui_async import ServiceBridge
        from management.gui_gc import install_gui_gc
        app = QApplication([])
        app.setQuitOnLastWindowClosed(False)
        original = gc.isenabled()
        class SlowFailure:
            def __init__(self, *_): pass
            def query(self, *_args, **_kwargs):
                time.sleep(.12)
                raise ValueError('synthetic failed request')
        bridge = ServiceBridge(sys.argv[1], client_factory=SlowFailure)
        manager = install_gui_gc(app)
        manager.timer.setInterval(10)
        called = []
        def closed_view(error):
            called.append(error)
            raise RuntimeError('Internal C++ object already deleted')
        bridge.query('state', error=closed_view)
        QTimer.singleShot(10, app.quit)
        app.exec()
        assert called and not bridge.workers_running()
        assert not manager.active and gc.isenabled() == original
    """)
