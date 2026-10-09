"""Local, interruptible first-visit tutorials; never write business records."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Callable
import weakref

from PySide6.QtCore import QEvent, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from .gui_onboarding_overlay import TourStep, TutorialOverlay


def _widget(reference):
    value = reference() if reference is not None else None
    return value if value is not None and isValid(value) else None


@dataclass
class _Watch:
    key: str
    trigger: weakref.ReferenceType
    host: weakref.ReferenceType
    steps: Callable[[], list[TourStep]]
    ready: Callable[[], bool] | None
    order: int
    pending_since: float | None = None


class OnboardingManager(QObject):
    """One overlay at a time, with progress scoped to the chosen data space.

    Automatic introductions are offered only at a fresh Show event. The offer
    waits through slow initialization and temporary loss of focus. Any input
    during that wait cancels this visit, so a delayed query cannot interrupt
    someone who has already started editing.
    """

    error = Signal(str)
    automatic_changed = Signal(bool)
    INITIAL_DELAY = 0.32

    def __init__(self, window: QWidget, data_dir):
        super().__init__(window)
        self._window = weakref.ref(window)
        self._path = Path(data_dir) / 'ui-onboarding.json'
        self._watches: dict[str, _Watch] = {}
        self._automatic = True
        self._seen: dict[str, str] = {}
        self._reported_errors: set[str] = set()
        self._write_allowed = True
        self._stopped = False
        self._overlay: TutorialOverlay | None = None
        self._active_key: str | None = None
        self._next_order = 0
        self._timer = QTimer(self)
        self._timer.setInterval(90)
        self._timer.timeout.connect(self._try_pending)
        self._load()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    @property
    def automatic(self) -> bool:
        return self._automatic

    @property
    def active_key(self) -> str | None:
        return self._active_key

    @property
    def overlay(self) -> TutorialOverlay | None:
        return self._overlay

    def _report(self, detail: str):
        if detail in self._reported_errors:
            return
        self._reported_errors.add(detail)
        # Connections are normally installed just after constructing the manager.
        QTimer.singleShot(0, self, lambda: self.error.emit(detail))

    def _load(self):
        try:
            if not self._path.exists():
                return
            if self._path.stat().st_size > 65536:
                raise ValueError('tutorial preferences are too large')
            value = json.loads(self._path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or not isinstance(value.get('automatic'), bool):
                raise ValueError('invalid tutorial preferences')
            # Honor an explicit opt-out even if a newer app wrote the remaining
            # progress schema. Unknown formats are never overwritten below.
            self._automatic = value['automatic']
            if value.get('version') != 1:
                self._write_allowed = False
                raise ValueError('unsupported tutorial preferences version')
            seen = value.get('seen', {})
            if not isinstance(seen, dict):
                raise ValueError('invalid tutorial progress')
            self._seen = {key: status for key, status in seen.items()
                          if isinstance(key, str) and len(key) <= 160
                          and status in ('completed', 'skipped')}
        except (OSError, ValueError, TypeError):
            self._report('无法读取使用指南进度，本次先使用默认设置；个人资料不受影响。')

    def _save(self):
        temporary = None
        try:
            if not self._write_allowed:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            value = {'version': 1, 'automatic': self._automatic, 'seen': self._seen}
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=self._path.parent,
                                             prefix='.ui-onboarding-', suffix='.tmp',
                                             delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        except OSError:
            self._report('使用指南进度暂时无法保存；本次仍会记住已查看的介绍，个人资料不受影响。')
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def watch(self, key: str, trigger: QWidget,
              steps: Callable[[], list[TourStep]], host: QWidget | None = None,
              ready: Callable[[], bool] | None = None):
        if self._stopped:
            return
        if not key or len(key) > 160:
            raise ValueError('A stable tutorial key of 1–160 characters is required.')
        host = host if host is not None else trigger.window()
        self._next_order += 1
        entry = _Watch(key, weakref.ref(trigger), weakref.ref(host), steps,
                       ready, self._next_order)
        old = self._watches.get(key)
        if old is not None and self._active_key == key:
            self._interrupt()
        self._watches[key] = entry
        # The identity check also allows a freshly reopened dialog to reuse a key.
        trigger.destroyed.connect(lambda _=None, record=entry: self._forget(record))
        if host is not trigger:
            host.destroyed.connect(lambda _=None, record=entry: self._forget(record))
        if trigger.isVisible():
            self._arm(entry)

    def _forget(self, entry: _Watch):
        if self._watches.get(entry.key) is not entry:
            return
        if self._active_key == entry.key:
            self._interrupt()
        self._watches.pop(entry.key, None)

    def _automatic_allowed(self):
        return self._automatic and os.environ.get('PERSONAL_MANAGEMENT_NO_ONBOARDING') != '1'

    def _arm(self, entry: _Watch):
        if self._automatic_allowed() and entry.key not in self._seen:
            entry.pending_since = time.monotonic()
            self._timer.start()

    def _visible(self, entry: _Watch):
        trigger, host = _widget(entry.trigger), _widget(entry.host)
        return bool(trigger is not None and host is not None
                    and trigger.isVisible() and host.isVisible()
                    and not host.isMinimized())

    def _active_host(self, entry: _Watch):
        host = _widget(entry.host)
        app = QApplication.instance()
        if host is None or app is None:
            return False
        top = host.window()
        modal = app.activeModalWidget()
        if modal is not None and modal.window() is not top:
            return False
        active = app.activeWindow()
        return active is top or (active is None and top.isActiveWindow())

    def _ready(self, entry: _Watch):
        try:
            return entry.ready is None or bool(entry.ready())
        except RuntimeError:
            # A dialog or its data loader may have been destroyed meanwhile.
            return False

    def _try_pending(self):
        if self._stopped or not self._automatic_allowed():
            self._timer.stop()
            return
        if self._overlay is not None:
            return
        now = time.monotonic()
        entries = sorted(self._watches.values(), key=lambda entry: entry.order)
        for entry in entries:
            pending = entry.pending_since
            if pending is None:
                continue
            if not self._visible(entry):
                entry.pending_since = None
                continue
            if (now - pending >= self.INITIAL_DELAY and self._active_host(entry)
                    and self._ready(entry)):
                if self._start(entry):
                    return
        if not any(entry.pending_since is not None for entry in self._watches.values()):
            self._timer.stop()

    def _start(self, entry: _Watch, *, manual: bool = False) -> bool:
        if self._stopped or not self._visible(entry) or not self._active_host(entry):
            return False
        if not manual and not self._ready(entry):
            return False
        try:
            steps = list(entry.steps())
        except RuntimeError:
            return False
        if not steps:
            entry.pending_since = None
            return False
        self._interrupt()
        host = _widget(entry.host)
        if host is None:
            return False
        overlay = TutorialOverlay(host, steps)
        self._overlay = overlay
        self._active_key = entry.key
        # Do not chain several introductions after dismissing the current one.
        for pending in self._watches.values():
            pending.pending_since = None
        self._timer.stop()
        overlay.finished.connect(lambda reason, current=overlay, key=entry.key:
                                 self._finished(current, key, reason))
        overlay.start()
        return self._overlay is overlay

    def _finished(self, overlay: TutorialOverlay, key: str, reason: str):
        if self._overlay is not overlay:
            return
        self._overlay = None
        self._active_key = None
        if reason in ('completed', 'skipped'):
            self._seen[key] = reason
            self._save()
        elif reason == 'disabled':
            self._automatic = False
            self._save()
            self.automatic_changed.emit(False)
        if isValid(overlay):
            overlay.deleteLater()

    def _interrupt(self):
        overlay = self._overlay
        if overlay is not None and isValid(overlay):
            overlay.stop()
        # Be robust if the host destroyed its children before delivering signals.
        self._overlay = None
        self._active_key = None

    def show(self, key: str) -> bool:
        """Replay a visible panel, even when automatic introductions are off."""
        entry = self._watches.get(key)
        return bool(entry is not None and self._start(entry, manual=True))

    def show_current(self) -> bool:
        entries = [entry for entry in self._watches.values()
                   if self._visible(entry) and self._active_host(entry)]

        def specificity(entry):
            depth, trigger = 0, _widget(entry.trigger)
            while trigger is not None:
                depth += 1
                trigger = trigger.parentWidget()
            return depth, entry.order

        for entry in sorted(entries, key=specificity, reverse=True):
            if self._start(entry, manual=True):
                return True
        return False

    def set_automatic(self, enabled: bool):
        enabled = bool(enabled)
        if self._automatic == enabled:
            return
        self._automatic = enabled
        self._save()
        self.automatic_changed.emit(enabled)
        if enabled:
            for entry in self._watches.values():
                if self._visible(entry):
                    self._arm(entry)
        else:
            self._timer.stop()
            for entry in self._watches.values():
                entry.pending_since = None
            self._interrupt()

    def stop(self):
        """Detach application-wide observation when the owning window closes."""
        self._stopped = True
        self._timer.stop()
        self._interrupt()
        self._watches.clear()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def eventFilter(self, watched, event):
        if self._stopped:
            return False
        kind = event.type()
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress,
                    QEvent.Type.Wheel, QEvent.Type.TouchBegin):
            if isinstance(watched, QWidget):
                window = watched.window()
                for entry in self._watches.values():
                    host = _widget(entry.host)
                    if host is not None and host.window() is window:
                        entry.pending_since = None
            return False
        if not isinstance(watched, QWidget):
            return False
        entries = tuple(self._watches.values())
        if kind == QEvent.Type.Show:
            if watched.isWindow() and self._overlay is not None:
                active = self._watches.get(self._active_key)
                host = _widget(active.host) if active else None
                if host is not None and watched is not host.window():
                    self._interrupt()
            for entry in entries:
                if watched is _widget(entry.trigger):
                    self._arm(entry)
        elif kind in (QEvent.Type.Hide, QEvent.Type.Close, QEvent.Type.WindowDeactivate):
            for entry in entries:
                if watched is _widget(entry.trigger) or watched is _widget(entry.host):
                    # Losing focus while startup queries finish is not a user
                    # dismissal. Keep an unstarted offer; never re-arm one that
                    # input, a skipped guide, or another guide already canceled.
                    if kind != QEvent.Type.WindowDeactivate:
                        entry.pending_since = None
                    if self._active_key == entry.key:
                        self._interrupt()
        return False
