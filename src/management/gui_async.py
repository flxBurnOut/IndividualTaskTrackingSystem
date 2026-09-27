"""An asynchronous bridge to the local service; Qt never opens SQLite."""
from __future__ import annotations

import traceback
import json
import uuid
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot
from .gui_gc import install_gui_gc


class _Worker(QObject):
    completed = Signal(int, object, object)

    def __init__(self, data_dir, client_factory=None):
        super().__init__()
        self.data_dir = data_dir
        self.client_factory = client_factory
        self.client = None

    @Slot(int, str, str, object)
    def execute(self, serial: int, method: str, name: str, arguments: dict):
        try:
            if self.client is None:
                factory = self.client_factory
                if factory is None:
                    from .client import Client
                    factory = Client
                self.client = factory(self.data_dir)
            if method == "maintenance":
                result = self.client.prepare_update()
            elif method == "command":
                options=dict(arguments.get("options", {}))
                if name=='send_message':
                    # A raw message requests fresh reasoning, not adoption of an
                    # edited business snapshot. Refresh its revision after native
                    # desktop turns while retaining the original epoch and ID.
                    state=self.client.query('state')
                    if options.get('epoch')==state.get('epoch'):
                        options['expected_revision']=state['revision']
                result = self.client.command(name, arguments["payload"], **options)
            else:
                result = self.client.query(name, **arguments)
            self.completed.emit(serial, result, None)
        except Exception as exc:
            if method == "command" and getattr(exc, "code", None) == "connection_lost":
                request_id = arguments.get("options", {}).get("request_id")
                if request_id:
                    try:
                        recovered = self.client.query("receipt", request_id=request_id)
                        if recovered.get("found"):
                            self.completed.emit(serial, recovered["receipt"], None)
                            return
                    except Exception:
                        pass
            self.completed.emit(serial, None, {
                "code": getattr(exc, "code", "connection_error"),
                "message": getattr(exc, "message", str(exc)),
                "details": getattr(exc, "details", None),
            })


class ServiceBridge(QObject):
    """Callbacks are delivered in the UI thread. Mutations are serialized.

    Separate read and write clients keep browsing responsive during large imports
    and backups. Writes carry the UI snapshot explicitly. Views use generations
    to reject late reads, and the bridge never lowers an observed revision.
    """
    requested = Signal(int, str, str, object)
    mutation_requested = Signal(int, str, str, object)
    activity = Signal(bool)
    failed = Signal(object)

    def __init__(self, data_dir, parent=None, client_factory=None):
        manager = install_gui_gc()
        super().__init__(parent)
        self._closing = False
        self.data_dir = data_dir
        self.thread = QThread(self)
        self.worker = _Worker(data_dir, client_factory)
        self.worker.moveToThread(self.thread)
        self.requested.connect(self.worker.execute)
        self.worker.completed.connect(self._complete)
        self.thread.finished.connect(self.worker.deleteLater)
        self.mutation_thread = QThread(self)
        self.mutation_worker = _Worker(data_dir, client_factory)
        self.mutation_worker.moveToThread(self.mutation_thread)
        self.mutation_requested.connect(self.mutation_worker.execute)
        self.mutation_worker.completed.connect(self._complete)
        self.mutation_thread.finished.connect(self.mutation_worker.deleteLater)
        self.callbacks: dict[int, tuple[Callable | None, Callable | None]] = {}
        self.command_contexts = {}
        self.uncertain_writes = {}
        self.serial = 0
        self.epoch = None
        self.revision = None
        manager.register_bridge(self)
        self.thread.start()
        self.mutation_thread.start()

    def query(self, name, callback=None, error=None, **params):
        if name == 'get':
            params.setdefault('display', True)
        return self._submit("query", name, params, callback, error)

    def prepare_update(self, callback=None, error=None):
        return self._submit('maintenance', 'prepare_update', {}, callback, error)

    def command(self, name, payload, callback=None, error=None, **options):
        options.setdefault("epoch", self.epoch)
        options.setdefault("expected_revision", self.revision)
        fingerprint = json.dumps([name, payload, options.get("epoch")], sort_keys=True, ensure_ascii=False)
        if fingerprint in self.uncertain_writes and "request_id" not in options:
            options = dict(self.uncertain_writes[fingerprint])
        options.setdefault("request_id", str(uuid.uuid4()))
        serial = self._submit("command", name, {"payload": payload, "options": options}, callback, error)
        if not self._closing:
            self.command_contexts[serial] = (fingerprint, dict(options))
        return serial

    def _submit(self, method, name, arguments, callback, error):
        self.serial += 1
        if self._closing:
            if error:
                error({"code": "bridge_closed", "message": "软件正在关闭，请等待当前操作结束。"})
            return self.serial
        self.callbacks[self.serial] = (callback, error)
        self.activity.emit(True)
        signal = self.mutation_requested if method in {"command", "maintenance"} else self.requested
        signal.emit(self.serial, method, name, arguments)
        return self.serial

    @Slot(int, object, object)
    def _complete(self, serial, result, error):
        callback, error_callback = self.callbacks.pop(serial, (None, None))
        context = self.command_contexts.pop(serial, None)
        if context:
            fingerprint, options = context
            if error is not None and error.get("code") == "connection_lost":
                self.uncertain_writes[fingerprint] = options
                error.setdefault("details", {})["request_id"] = options["request_id"]
            else:
                self.uncertain_writes.pop(fingerprint, None)
        if isinstance(result, dict):
            epoch, revision = result.get("epoch"), result.get("revision")
            if epoch is not None and revision is not None:
                if epoch != self.epoch:
                    self.epoch, self.revision = epoch, revision
                else:
                    self.revision = max(self.revision or 0, revision)
        try:
            if error is not None:
                (error_callback or self.failed.emit)(error)
            elif callback:
                callback(result)
        except RuntimeError:
            # Either a success or an error callback may refer to a dismissed
            # Qt view. Neither must prevent the accepted queue from draining.
            pass
        except Exception:
            traceback.print_exc()
            self.failed.emit({"code": "view_error", "message": "数据已返回，但此页面显示失败。请刷新页面。"})
        finally:
            try:
                self.activity.emit(bool(self.callbacks))
            finally:
                if self._closing and not self.callbacks:
                    self.thread.quit()
                    self.mutation_thread.quit()

    def workers_running(self):
        return self.thread.isRunning() or self.mutation_thread.isRunning()

    def request_stop(self):
        self._closing = True
        # Accepted operations and their receipts finish before either worker
        # event loop exits. Quitting immediately would drop queued mutations.
        if not self.callbacks:
            self.thread.quit()
            self.mutation_thread.quit()

    def close(self, timeout_ms=1000):
        # The owner must retain the bridge until both threads actually stop.
        self.request_stop()
        query_stopped = self.thread.wait(timeout_ms)
        mutation_stopped = self.mutation_thread.wait(timeout_ms)
        return query_stopped and mutation_stopped
