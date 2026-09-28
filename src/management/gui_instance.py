"""One interactive main window per data space, activated over a user-only pipe."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .runtime import OwnerLock

COMMANDS = {'show', 'update', 'exit'}


def pipe_name(data_dir):
    identity = os.path.normcase(str(Path(data_dir).resolve())) + '\0' + os.environ.get('USERNAME', os.environ.get('USER', ''))
    return 'PersonalManagement.Gui.' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]


def notify_window(data_dir, command='show', timeout_ms=700):
    if command not in COMMANDS:
        raise ValueError('Unsupported window request')
    socket = QLocalSocket()
    try:
        socket.connectToServer(pipe_name(data_dir))
        if not socket.waitForConnected(timeout_ms):
            return False
        socket.write((command + '\n').encode('ascii'))
        socket.flush()
        if not socket.bytesAvailable() and not socket.waitForReadyRead(timeout_ms):
            return False
        return bytes(socket.readAll()).strip() == b'accepted'
    finally:
        socket.abort()


def window_running(data_dir):
    lock = OwnerLock(Path(data_dir) / 'gui.lock')
    if not lock.acquire():
        return True
    lock.release()
    return False


class WindowInstance(QObject):
    requested = Signal(str)

    def __init__(self, data_dir, parent=None):
        super().__init__(parent)
        self.lock = OwnerLock(Path(data_dir) / 'gui.lock')
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.setMaxPendingConnections(4)
        self.name = pipe_name(data_dir)
        self.sockets = set()
        self.server.newConnection.connect(self._accept)

    def acquire(self):
        if not self.lock.acquire():
            return False
        # Only the lock owner can remove a stale server name.
        QLocalServer.removeServer(self.name)
        if not self.server.listen(self.name):
            self.lock.release()
            raise OSError('无法创建窗口连接，请稍后重新打开软件。')
        return True

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self.sockets.add(socket)
            buffer = bytearray()
            def read(socket=socket, buffer=buffer):
                buffer.extend(bytes(socket.readAll()))
                if len(buffer) > 32:
                    socket.abort()
                    return
                if b'\n' not in buffer:
                    return
                try:
                    command = bytes(buffer).decode('ascii').strip()
                except UnicodeError:
                    command = ''
                if command in COMMANDS:
                    socket.write(b'accepted\n')
                    socket.flush()
                    QTimer.singleShot(0, lambda command=command: self.requested.emit(command))
                socket.disconnectFromServer()
            def clean(socket=socket):
                self.sockets.discard(socket)
                socket.deleteLater()
            socket.readyRead.connect(read)
            socket.disconnected.connect(clean)
            timer = QTimer(socket)
            timer.setSingleShot(True)
            timer.timeout.connect(socket.abort)
            timer.start(1500)
            if socket.bytesAvailable():
                read()

    def close(self):
        self.server.close()
        for socket in tuple(self.sockets):
            socket.abort()
        self.lock.release()
