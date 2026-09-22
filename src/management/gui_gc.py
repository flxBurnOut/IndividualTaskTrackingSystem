"""Keep cyclic Python collection on the Qt owner thread in GUI processes.

Reference counting remains active. Only automatic *cyclic* collection is replaced
by a timer, because it can otherwise destroy a Python-owned QWidget on whichever
HTTP worker happens to cross the allocation threshold. This module is never
imported by the business service. Explicit gc.collect() in third-party worker
code is outside this policy.
"""
from __future__ import annotations

import gc
import time
import weakref

from PySide6.QtCore import QEvent, QObject, QThread, QTimer, Slot
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid


class GuiGarbageCollector(QObject):
    def __init__(self, app, *, collector=gc, clock=time.monotonic,
                 interval_ms=250, full_interval_seconds=60, start_immediately=True):
        if QThread.currentThread() != app.thread():
            raise RuntimeError("GUI collection must be installed on the application thread")
        super().__init__(app)
        self.collector = collector
        self.clock = clock
        self.original_enabled = collector.isenabled()
        self.original_thresholds = collector.get_threshold()
        self.full_interval_seconds = full_interval_seconds
        self.last_full = clock()
        self.active = True
        self._collecting = False
        self._shutting_down = False
        self.quit_pending = False
        self.shutdown_pending = False
        self._bridges = []
        self.collection_counts = [0, 0, 0]
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.poll)
        # Disable before allocating more GUI state; collection continues below.
        collector.disable()
        app.installEventFilter(self)
        app.aboutToQuit.connect(self.shutdown)
        self._started = False
        if start_immediately:
            self.start()

    def start(self):
        self._assert_owner()
        if self._started or not self.active:
            return
        self._started = True
        self._collect(2)
        self.timer.start()

    def _assert_owner(self):
        if QThread.currentThread() != self.thread():
            raise RuntimeError("GUI collection must run on the application thread")

    def _collect(self, generation):
        self._assert_owner()
        if self._collecting:
            return
        self._collecting = True
        try:
            self.collector.collect(generation)
            self.collection_counts[generation] += 1
            if generation == 2:
                self.last_full = self.clock()
        finally:
            self._collecting = False

    def register_bridge(self, bridge):
        self._assert_owner()
        if not any(ref() is bridge for ref in self._bridges):
            self._bridges.append(weakref.ref(bridge))

    def _live_bridges(self):
        live = [ref() for ref in self._bridges]
        live = [bridge for bridge in live if bridge is not None and isValid(bridge)]
        self._bridges = [weakref.ref(bridge) for bridge in live]
        return live

    def _workers_running(self):
        return any(bridge.workers_running() for bridge in self._live_bridges())

    def _request_stop(self):
        for bridge in self._live_bridges():
            bridge.request_stop()

    def eventFilter(self, watched, event):
        if self.active and event.type() == QEvent.Type.Quit and self._workers_running():
            # A forced app.quit must not destroy a live QThread or re-enable
            # automatic collection while its Python callback is still running.
            self.quit_pending = True
            self._request_stop()
            return True
        return super().eventFilter(watched, event)

    @Slot()
    def poll(self):
        self._assert_owner()
        if not self.active or self._collecting or self._shutting_down:
            return
        if self.clock() - self.last_full >= self.full_interval_seconds:
            self._collect(2)
        else:
            counts = self.collector.get_count()
            thresholds = self.original_thresholds
            if thresholds[0] > 0 and counts[0] >= thresholds[0]:
                generation = 0
                if counts[1] >= thresholds[1]:
                    generation = 1
                    if counts[2] >= thresholds[2]:
                        generation = 2
                self._collect(generation)
        if not self._workers_running():
            if self.quit_pending:
                self.quit_pending = False
                QApplication.instance().quit()
            elif self.shutdown_pending:
                self.shutdown()

    @Slot()
    def shutdown(self):
        self._assert_owner()
        if not self.active:
            return True
        if self._workers_running():
            # QCoreApplication.exit may bypass Quit filtering. Do not restore
            # unsafe automatic GC. The host must finish pumping events until
            # drain completes; the normal app.quit path is deferred above.
            self.shutdown_pending = True
            self._request_stop()
            return False
        if self._collecting or self._shutting_down:
            self.shutdown_pending = True
            return False
        self._shutting_down = True
        try:
            self.shutdown_pending = False
            self.timer.stop()
            self._collect(2)
            self.collector.set_threshold(*self.original_thresholds)
            if self.original_enabled:
                self.collector.enable()
            else:
                self.collector.disable()
            self.active = False
            app = self.parent()
            app.removeEventFilter(self)
            app.aboutToQuit.disconnect(self.shutdown)
            return True
        finally:
            self._shutting_down = False


def install_gui_gc(app=None):
    """Idempotently install the GUI-only policy before starting any workers."""
    app = app or QApplication.instance()
    if not isinstance(app, QApplication):
        raise RuntimeError("A QApplication is required for GUI collection")
    if QThread.currentThread() != app.thread():
        raise RuntimeError("GUI collection must be installed on the application thread")
    existing = getattr(app, "_management_gui_gc", None)
    if existing is not None and isValid(existing) and existing.active:
        return existing
    manager = GuiGarbageCollector(app, start_immediately=False)
    # A Python finalizer/gc callback can re-enter installation during collect.
    # Publish the unique controller before its first collection, not afterwards.
    app._management_gui_gc = manager
    manager.start()
    return manager
