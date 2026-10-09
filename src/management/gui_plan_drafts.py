"""Small, epoch-scoped UI drafts; never write business records or overwrite a newer draft."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from PySide6.QtCore import QLockFile, QSaveFile, QIODevice


class DraftConflict(RuntimeError):
    pass


class PlanDrafts:
    def __init__(self, root=None):
        self.root = Path(root) / 'ui-plan-drafts' if root is not None else None
        self.memory = {}

    def _path(self, epoch, day):
        key = hashlib.sha256(f'{epoch}:{day}'.encode()).hexdigest()
        return self.root / (key + '.json')

    def read(self, epoch, day):
        if self.root is None:
            return self.memory.get((epoch, day))
        path = self._path(epoch, day)
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('epoch') != epoch or value.get('date') != day:
            raise DraftConflict('草稿所属数据空间不一致，未载入。')
        return value

    def write(self, epoch, day, payload, expected_token):
        if not epoch:
            raise DraftConflict('数据空间尚未读取完成，暂不能保存草稿。')
        lock = None
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)
            lock = QLockFile(str(self._path(epoch, day)) + '.lock')
            if not lock.tryLock(0):
                raise DraftConflict('另一个窗口正在保存这一天的草稿，请稍后重试。')
        try:
            current = self.read(epoch, day)
            if (current or {}).get('token') != expected_token:
                raise DraftConflict('另一个窗口已修改这一天的草稿。当前输入仍在此窗口，未覆盖另一份草稿。')
            value = {'epoch': epoch, 'date': day, 'token': str(uuid.uuid4()), 'payload': payload}
            if self.root is None:
                self.memory[(epoch, day)] = value
            else:
                target = QSaveFile(str(self._path(epoch, day)))
                if not target.open(QIODevice.OpenModeFlag.WriteOnly):
                    raise OSError(target.errorString())
                data = json.dumps(value, ensure_ascii=False).encode('utf-8')
                if target.write(data) != len(data) or not target.commit():
                    raise OSError(target.errorString())
            return value['token']
        finally:
            if lock is not None:
                lock.unlock()

    def clear(self, epoch, day, expected_token):
        # A tombstone keeps the compare token: a stale window cannot recreate a
        # draft after another window has applied or deliberately discarded it.
        return self.write(epoch, day, None, expected_token)
