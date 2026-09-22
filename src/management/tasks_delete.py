"""Task removal is reversible; plans, evidence and linked records remain intact."""
from .schemas import BusinessError


def _task(core, c, p):
    if set(p) != {"id", "version"} or not isinstance(p.get("id"), str) or type(p.get("version")) is not int:
        raise BusinessError("validation", "删除或恢复任务需要明确的任务和版本。")
    entity = core._versioned(c, p)
    if entity["type"] != "task":
        raise BusinessError("validation", "此操作只用于任务；其他事项请使用各自的管理入口。")
    return entity


def delete_task(core, c, p, rid):
    entity = _task(core, c, p)
    if entity["archived"]:
        return {"entity": entity, "reused": True}
    # Use the existing archive checks and audit action, including active children
    # protection. No deletion cascades or plan/feedback rewrites are performed.
    result = core._dispatch(c, "archive", {**p, "archived": True}, rid)
    return {**result, "reused": False}


def restore_task(core, c, p, rid):
    entity = _task(core, c, p)
    if not entity["archived"]:
        return {"entity": entity, "reused": True}
    # Restoration revalidates parent and enabled-module rules at the current state.
    result = core._dispatch(c, "archive", {**p, "archived": False}, rid)
    return {**result, "reused": False}
