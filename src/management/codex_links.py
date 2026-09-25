"""Public OS links only; opening is not a claim of streaming or delivery."""
from __future__ import annotations
import os
import uuid
from pathlib import Path
from urllib.parse import urlencode


def thread_url(thread_id: str) -> str:
    if not isinstance(thread_id, str):
        raise ValueError('Codex 任务标识无效。')
    try:
        ident = str(uuid.UUID(thread_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError('Codex 任务标识无效。') from exc
    return 'codex://threads/' + ident + '?hostId=local'


def open_thread(thread_id: str) -> None:
    url = thread_url(thread_id)
    if os.name != 'nt':
        raise OSError('此入口需要 Windows 上已安装的 Codex 桌面应用。')
    os.startfile(url)


def open_workspace(workspace: str) -> None:
    path = Path(workspace)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('Codex 对话项目目录不存在，请在设置中重新连接。')
    if os.name != 'nt':
        raise OSError('此入口需要 Windows 上已安装的 Codex 桌面应用。')
    os.startfile('codex://new?' + urlencode({'path': str(path.resolve())}))
