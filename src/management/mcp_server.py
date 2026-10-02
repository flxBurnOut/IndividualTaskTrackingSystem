"""Typed MCP tools backed exclusively by the independent business service."""
from __future__ import annotations
from . import __version__

import argparse
import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from .mcp_routing import DiscussionRouting, RoutedMCPServer, is_context_query
from .mcp_pages import PAGE_QUERIES, PageQuery, page as business_page, validate_request as validate_page_request


READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
INSTRUCTIONS = (
    "个人事务业务接口。每个新任务及每轮业务处理先读取最新上下文。所有写入经业务命令，"
    "必须携带读取所得 epoch、revision 和唯一 request_id；重试使用同一 ID 与同一内容。"
    "旧聊天不是当前事实。未确认保持未知，完成/出席/学习/提交/掌握分别记录。"
    "事实更新不自动重排计划。不要访问数据库、工作簿或生成写库脚本。普通操作由你推理，"
    "不会再次启动后台 AI。优先 prepare_context 并用统一分页工具读取完整约束；遇到冲突重新读取并处理，"
    "不可更换版本号盲重试。只有成功回执表示写入成功。"
    "如果当前会话关联软件事项，每轮先 begin_discussion，最后 submit_candidate；"
    "身份由 Codex 调用上下文提供，不能指定其他会话。受管事项只返回候选，由用户在软件确认。"
)

QueryName = Literal[
    "conversation_targets",
    "codex_connection", "timetables", "timetable_week",
    "skills", "habits_overview", "dashboard",
    "recovery_summary",
    "daily_tasks", "task_pool", "actual_feedback", "preview_task_batch",
    "recurring_rules", "preview_recurring", "codex_models",
    "sources", "source_content", "conversation", "library_folder",
    "daily_review", "weekly_review", "review_preferences", "object_workspace", "workspace_tasks", "open_resource", "operation",
    "state", "capabilities", "list", "get", "changes", "today", "plan_context",
    "review", "jobs", "job", "settings", "receipt", "diagnostics", "warnings",
    "learning_summary", "assessment_summary", "collection_summary", "project_summary", "coverage_gaps",
]
CommandName = Literal[
    "resume_context_operation",
    "attach_conversation",
    "set_task_completion", "revise_plan",
    "delete_task", "restore_task",
    "create_task_batch", "split_task",
    "apply_timetable",
    "set_recovery_task", "record_recovery_progress", "correct_recovery_scope",
    "add_to_plan", "set_recurring_rule", "materialize_recurring",
    "add_source", "send_message",
    "submit_daily_review", "set_review_preferences", "attach_local_file",
    "create", "update", "move", "archive", "link", "unlink", "record_feedback",
    "create_plan", "create_checkin", "respond_checkin", "save_review", "settings", "configure_codex",
    "install_module", "disable_module", "create_job", "cancel_job", "apply_proposal",
    "import_asset", "export_asset", "backup", "create_bundle", "restore_backup",
    "create_artifact_job", "adopt_artifact", "create_notebook_from_pdf", "run_workflow", "undo", "promote_checklist",
]


class FeedbackDimensions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completion: str | None = None
    attendance: str | None = None
    viewing: str | None = None
    submission: str | None = None
    mastery: str | None = None
    actual_minutes: float | None = Field(default=None, ge=0)


class PlanBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str
    start: str | None = None
    end: str | None = None
    minutes: Annotated[int, Field(strict=True, ge=1, le=1440)] | None = None
    completion_gate: str | None = None


class CheckinAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str
    dimensions: FeedbackDimensions


def _capability_error(code: str, message: str) -> ToolError:
    return ToolError(json.dumps({"code": code, "message": message}, ensure_ascii=False))


def _validated_capabilities(value: Any) -> dict[str, Any]:
    """Validate the service registry, never a presentation page of that registry."""
    if not isinstance(value, dict) or value.get("next_offset") is not None:
        raise _capability_error("capabilities_invalid", "业务能力目录不完整，无法判断操作是否已注册。")
    commands, extensions = value.get("commands"), value.get("code_extensions")
    if (not isinstance(commands, list)
            or any(not isinstance(name, str) or not name.strip() for name in commands)
            or len(commands) != len(set(commands))
            or not isinstance(extensions, list)
            or any(not isinstance(entry, dict) or entry.get("kind") not in ("command", "query")
                   or not isinstance(entry.get("name"), str) or not entry["name"].strip()
                   for entry in extensions)):
        raise _capability_error("capabilities_invalid", "业务能力目录缺少有效的命令或扩展索引，请重新连接后读取。")
    return value


def _capabilities_page(value: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Bound public discovery independently from internal registration checks.

    The default page lists command/query names. A detail section without name
    lists identifiers; with name it returns a bounded JSON fragment. The catalog
    digest prevents joining pages across registry/module changes.
    """
    from .context_service import MAX_PAGE_BYTES, _fragment, size
    from .storage import encode

    value = _validated_capabilities(value)
    sections = {"types": "id", "code_extensions": "name", "workflows": "id", "modules": "id"}
    section, name = params.get("section", "index"), params.get("name")
    offset, limit = params.get("offset", 0), params.get("limit", 80)
    if (set(params) - {"section", "name", "offset", "limit", "version"}
            or not isinstance(section, str) or section not in {"index", *sections}
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 100
            or name is not None and (not isinstance(name, str) or not name or section == "index")):
        raise _capability_error("capabilities_parameters", "能力目录参数无效；使用 section、name、offset、limit 和 version。")
    version = hashlib.sha256(encode(value).encode("utf-8")).hexdigest()
    if params.get("version") is not None and params["version"] != version:
        raise _capability_error("capabilities_changed", "能力目录已经变化，请从首页重新读取，不能拼接不同版本。")
    header = {key: value[key] for key in ("epoch", "revision", "api_version") if key in value}
    header.update(section=section, version=version, offset=offset)
    if section == "index":
        lists = {"commands": sorted(value["commands"]),
                 "queries": sorted(set(get_args(QueryName)) | {
                     entry["name"] for entry in value["code_extensions"] if entry["kind"] == "query"})}
        header.update(
            detail_sections=list(sections),
            instructions="命令和查询名称按 next_offset 分页，并传回 version。用 query_business(name='capabilities', "
                         "params={section:'types'}) 等读取详情名称索引，再加 name 读取单项 JSON；"
                         "详情需按 next_offset 拼接 json_fragment。扩展 name 为 kind:name，工作流为 module_id:id。"
                         "具体业务操作先 prepare_context，再 describe_action。")
    else:
        entries = value.get(section)
        if (not isinstance(entries, list) or any(not isinstance(entry, dict)
                or not isinstance(entry.get(sections[section]), str) or not entry[sections[section]]
                or section == "workflows" and not isinstance(entry.get("module_id"), str)
                for entry in entries)):
            raise _capability_error("capabilities_invalid", "业务能力详情目录不完整。")
        def identifier(entry):
            if section == "code_extensions":
                return entry["kind"] + ":" + entry["name"]
            if section == "workflows":
                return entry["module_id"] + ":" + entry["id"]
            return entry[sections[section]]
        indexed = {identifier(entry): entry for entry in entries}
        if len(indexed) != len(entries):
            raise _capability_error("capabilities_invalid", "业务能力详情名称重复，无法确定条目。")
        if name is not None:
            if name not in indexed:
                raise _capability_error("capability_not_found", "能力目录中没有该详情名称。")
            header["name"] = name
            if size(header) > MAX_PAGE_BYTES // 2:
                raise _capability_error("capabilities_budget", "能力条目标识超过单页预算。")
            fragment, following = _fragment(encode(indexed[name]), offset, MAX_PAGE_BYTES - size(header) - 256)
            return {**header, "json_fragment": fragment, "next_offset": following}
        lists = {"names": sorted(indexed)}
    total = max(map(len, lists.values()), default=0)
    if offset > total:
        raise _capability_error("capabilities_parameters", "能力目录读取位置无效，请使用上一页返回的位置。")
    end = min(offset + limit, total)
    while True:
        result = {**header, **{key: names[offset:end] for key, names in lists.items()},
                  "totals": {key: len(names) for key, names in lists.items()},
                  "next_offset": end if end < total else None}
        if size(result) <= MAX_PAGE_BYTES:
            return result
        if end <= offset + 1:
            raise _capability_error("capabilities_budget", "能力条目标识超过单页预算。")
        end -= 1


def create_server(data_dir: str | Path, *, client: Any = None) -> MCPServer:
    if client is None:
        from .client import Client
        client = Client(data_dir, entrance='mcp')
    server = RoutedMCPServer("personal-management", title="个人事务管理", version=__version__, instructions=INSTRUCTIONS, log_level="WARNING")
    routing = DiscussionRouting(server, client)

    def raw_capabilities() -> dict[str, Any]:
        # No presentation paging or persistent context creation on this path.
        try:
            return _validated_capabilities(client.query("capabilities"))
        except Exception as exc:
            if hasattr(exc, "code"):
                raise ToolError(json.dumps({"code": exc.code, "message": str(exc),
                                           "details": getattr(exc, "details", {})}, ensure_ascii=False)) from exc
            raise

    def query(name: str, /, **params: Any) -> dict[str, Any]:
        try:
            if routing.is_managed():
                return routing.request('context' if is_context_query(name) else 'query', name=name, params=params)
            if name == "capabilities":
                return _capabilities_page(raw_capabilities(), params)
            value=client.query(name, **params)
            from .context_service import size,MAX_PAGE_BYTES,QUERY_NAMES
            if name in PAGE_QUERIES and size(value)>MAX_PAGE_BYTES:
                return business_page(name, params, value)
            if name not in QUERY_NAMES and name not in {'material_image','state','settings'} and size(value)>MAX_PAGE_BYTES:
                scope={'kind':'general'}
                if params.get('date'):scope['date']=params['date']
                if params.get('owner_id'):scope['entity_id']=params['owner_id']
                if name=='conversation' and value.get('conversation'):scope['conversation_id']=value['conversation']['id']
                context=client.query('prepare_context',goal='读取 '+name,scope=scope)
                operation=context['operation_id']
                if name=='job':return client.query('read_context_item',operation_id=operation,id='job:'+params['id'])
                result={'context':context,'requested_view':name,'instruction':'结果按统一分页接口读取，未将截断正文当作完整结果。'}
                if name=='today':
                    # The history body can require context readers without
                    # making the explicit current-plan identity disappear.
                    result.update({key:value[key] for key in ('current_plan_id','current_plan_version') if key in value})
                return result
            return value
        except Exception as exc:
            if hasattr(exc, "code"):
                raise ToolError(json.dumps({"code": exc.code, "message": str(exc), "details": getattr(exc, "details", {})}, ensure_ascii=False)) from exc
            raise

    def command(name: str, payload: dict, request_id: str, epoch: str, expected_revision: int) -> dict[str, Any]:
        routing.guard_write()
        if not request_id.strip() or len(request_id) > 128 or not epoch or expected_revision < 0:
            raise ToolError("写入必须使用唯一 request_id、最新上下文 epoch 和 revision。")
        try:
            return client.command(name, payload, request_id=request_id, epoch=epoch, expected_revision=expected_revision)
        except Exception as exc:
            if hasattr(exc, "code"):
                raise ToolError(json.dumps({"code": exc.code, "message": str(exc), "details": getattr(exc, "details", {})}, ensure_ascii=False)) from exc
            raise

    @server.tool(annotations=READ, structured_output=True)
    def begin_context(business_date: str | None = None) -> dict[str, Any]:
        """Start or resume any conversation using durable current state and capabilities.

        Optionally return today's data for an explicit ISO business date. Pending
        jobs and saved drafts are in the shared service, not this conversation.
        Read plan_context separately before planning. No hidden background AI.
        """
        if routing.is_managed():
            return routing.request('query', name='begin_context', params={'business_date': business_date})
        state=query('state')
        envelope=query('prepare_context',goal='读取当前个人事务',scope={'kind':'general',**({'date':business_date} if business_date else {})})
        return {**state,'context':envelope,'instructions':INSTRUCTIONS,
                'continuity':'先按需读取本次记录和未完成操作；软件与全部对话共用同一业务服务。'}

    @server.tool(annotations=READ, structured_output=True)
    def query_business(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read a supported business view. Discover types/fields with capabilities.

        capabilities returns a bounded command/query index. Follow next_offset
        with version; use params.section (types/code_extensions/workflows/modules)
        for detail names, then params.name for a paged JSON detail. Extension
        names use kind:name; workflow names use module_id:id. For action payloads
        use prepare_context then describe_action. list is also paged: follow
        next_offset; never claim the first page is complete. Large get/receipt/
        list/source_content responses use business-query-json/1: call the exact
        continuation tool/arguments, concatenate json_fragment from offset zero,
        then parse the original response. Only then use its business next_offset.
        receipt(request_id) reconciles an interrupted write without repeating it.
        settings excludes secrets. This does not execute SQL or arbitrary code.
        """
        if name not in get_args(QueryName):
            if routing.is_managed():
                return query(name, **(params or {}))
            capabilities = raw_capabilities()
            registered = {entry["name"] for entry in capabilities["code_extensions"]
                          if entry.get("kind") == "query"}
            if name not in registered:
                raise ToolError(json.dumps({"code": "unknown_query", "message": "Query is not registered."}))
        return query(name, **(params or {}))

    @server.tool(annotations=READ, structured_output=True)
    def read_business_page(name: PageQuery, params: dict[str, Any],
                           offset: Annotated[int, Field(strict=True, ge=0)] = 0,
                           version: str | None = None) -> dict[str, Any]:
        """Continue an ordinary large get/receipt/list/source_content response.

        Use the returned continuation arguments exactly, including explicit nulls
        and the original query's row/character offsets. Concatenate json_fragment
        in fragment_offset order from zero, then parse JSON once complete=true
        and continuation=null. Fragment offsets are not business next_offset.
        Changed results/queries/epochs reject continuation; restart the original
        read instead of joining versions. found confirms a receipt exists, while
        complete only marks the end of its serialized response, never a new write.
        Managed discussions must use their scoped context tools instead.
        """
        if routing.is_managed():
            raise _capability_error('discussion_query_forbidden',
                '此普通业务续读入口不适用于受管讨论；请使用当前事项的 read_context_item 或 read_material。')
        try:
            validate_page_request(name, params, offset, version)
            value = client.query(name, **params)
            return business_page(name, params, value, offset=offset, version=version)
        except Exception as exc:
            if hasattr(exc, 'code'):
                raise ToolError(json.dumps({'code': exc.code, 'message': str(exc),
                    'details': getattr(exc, 'details', {})}, ensure_ascii=False)) from exc
            raise

    @server.tool(annotations=READ, structured_output=True)
    def list_entities(type: str | None = None, parent_id: str | None = None, search: str | None = None,
                      status: str | None = None, archived: bool = False,
                      limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Find current objects by type, parent, status or text, with bounded paging."""
        values = {"type": type, "parent_id": parent_id, "search": search, "status": status,
                  "archived": archived, "limit": limit, "offset": offset}
        return query("list", **{k: v for k, v in values.items() if v is not None})

    @server.tool(annotations=READ, structured_output=True)
    def get_entity(id: str) -> dict[str, Any]:
        """Read an object, its version, children, relationships and bounded history.
        For a business-query-json/1 response, follow continuation and concatenate
        all json_fragment values before parsing the complete original view."""
        return query("get", id=id)

    @server.tool(annotations=READ, structured_output=True)
    def planning_context(date: str, mode: Literal["standard", "low_state", "no_precise_time", "rest"] = "standard") -> dict[str, Any]:
        """Read tasks, hard events, rules, capacity and unknowns before proposing a plan.

        Missing capacity is unknown. Preserve hard constraints even when candidate
        tasks are paginated; do not treat incomplete task coverage as exhaustive.
        """
        return query("prepare_context", goal="生成每日计划", scope={"kind":"daily_plan","date":date})

    @server.tool(annotations=READ, structured_output=True)
    def review_period(start: str, end: str) -> dict[str, Any]:
        """Read evidence-based metrics and unknown coverage for an ISO date interval."""
        return query("review", start=start, end=end)

    @server.tool(annotations=READ, structured_output=True)
    def recover_receipt(request_id: str) -> dict[str, Any]:
        """Recover a committed result after timeout or in another conversation.
        A large receipt retains found/request_id/receipt_metadata; follow its
        continuation to reconstruct the full receipt. Never repeat the write
        merely because the result requires more than one fragment."""
        return query("receipt", request_id=request_id)

    @server.tool(annotations=WRITE, structured_output=True)
    def create_entity(type: str, title: str, request_id: str, epoch: str, expected_revision: int,
                      parent_id: str | None = None, status: str | None = None,
                      data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create one task/project/domain/course/event/note or registered extension type.

        Discover legal type fields with capabilities first. A primary parent is
        ownership, not a cross-reference. Use the same request_id only on retry.
        """
        payload: dict[str, Any] = {"type": type, "title": title, "data": data or {}}
        if parent_id is not None:
            payload["parent_id"] = parent_id
        if status is not None:
            payload["status"] = status
        return command("create", payload, request_id, epoch, expected_revision)

    @server.tool(annotations=WRITE, structured_output=True)
    def update_entity(id: str, version: int, patch: dict[str, Any], request_id: str,
                      epoch: str, expected_revision: int) -> dict[str, Any]:
        """Update explicitly requested fields; use exact entity version from get_entity.

        Changes are optimistic, so another conversation or GUI edit cannot be
        silently overwritten. Use move/archive commands for parent/archive changes.
        """
        return command("update", {"id": id, "version": version, "patch": patch}, request_id, epoch, expected_revision)

    @server.tool(annotations=WRITE, structured_output=True)
    def record_feedback(target_id: str, business_date: str, dimensions: FeedbackDimensions,
                        source_text: str, request_id: str, epoch: str, expected_revision: int) -> dict[str, Any]:
        """Record only explicitly reported dimensions; unspecified values remain unknown.

        Actual minutes do not imply completion. Submission does not imply mastery.
        Preserve the original business date for replies after midnight. This does
        not automatically complete the task or rewrite the global plan.
        """
        payload = {"target_id": target_id, "business_date": business_date,
                   "dimensions": dimensions.model_dump(exclude_none=True), "source_text": source_text}
        return command("record_feedback", payload, request_id, epoch, expected_revision)

    @server.tool(annotations=WRITE, structured_output=True)
    def save_plan(date: str, mode: Literal["standard", "low_state", "no_precise_time", "rest"],
                  blocks: list[PlanBlock], request_id: str, epoch: str, expected_revision: int,
                  title: str | None = None, source_text: str = "") -> dict[str, Any]:
        """Save a plan after reading planning_context; deterministic rules validate it.

        Planned blocks never record actual performance. Hard events/rules changed
        since the context was read cause a conflict instead of a blind overwrite.
        """
        payload = {"date": date, "mode": mode, "blocks": [x.model_dump(exclude_none=True) for x in blocks], "source_text": source_text}
        if title:
            payload["title"] = title
        return command("create_plan", payload, request_id, epoch, expected_revision)

    @server.tool(annotations=WRITE, structured_output=True)
    def create_checkin(date: str, request_id: str, epoch: str, expected_revision: int,
                       target_ids: list[str] | None = None) -> dict[str, Any]:
        """Create a durable dated question set, shared with GUI and new conversations."""
        payload: dict[str, Any] = {"date": date}
        if target_ids is not None:
            payload["target_ids"] = target_ids
        return command("create_checkin", payload, request_id, epoch, expected_revision)

    @server.tool(annotations=WRITE, structured_output=True)
    def respond_checkin(id: str, answers: list[CheckinAnswer], source_text: str,
                         request_id: str, epoch: str, expected_revision: int) -> dict[str, Any]:
        """Answer exact stored questions. Silence/unanswered questions create no facts."""
        return command("respond_checkin", {"id": id, "answers": [a.model_dump(exclude_none=True) for a in answers], "source_text": source_text}, request_id, epoch, expected_revision)

    @server.tool(annotations=WRITE, structured_output=True)
    def save_review(start: str, end: str, text: str, request_id: str, epoch: str,
                    expected_revision: int, title: str | None = None) -> dict[str, Any]:
        """Save a review and its current metrics snapshot; unknown coverage stays unknown."""
        payload = {"start": start, "end": end, "text": text}
        if title:
            payload["title"] = title
        return command("save_review", payload, request_id, epoch, expected_revision)

    @server.tool(annotations=WRITE, structured_output=True)
    def execute_command(name: str, payload: dict[str, Any], request_id: str,
                        epoch: str, expected_revision: int) -> dict[str, Any]:
        """Execute a named business command advertised by capabilities, never SQL/scripts.

        Use specific tools for common workflows. Use this for move/archive/links,
        registered extension operations, assets, backup, scheduler and job control.
        A create_job with kind=ai explicitly starts background reasoning;
        ordinary operations never do. Configuration/restore actions require the
        user's specific intent. A proposal must be reviewed before apply_proposal.
        Unavailable commands return a typed error, not a fabricated success.
        """
        routing.guard_write()
        capabilities = raw_capabilities()
        if name not in capabilities["commands"]:
            raise ToolError(json.dumps({"code": "unknown_command", "message": "Command is not registered."}))
        return command(name, payload, request_id, epoch, expected_revision)

    @server.tool(annotations=WRITE, structured_output=True)
    def begin_discussion(text: str, request_id: str) -> dict[str, Any]:
        """Begin this exact user turn in the software matter bound to this Codex
        call. Identity comes from transport metadata, never a tool argument.
        Reuses a pending software request or starts a request in the same matter;
        never starts another model. Reuse request_id only for identical retries.
        Returns fresh context, job_id, generation and allowed candidate commands."""
        return routing.request('begin', text=text, request_id=request_id)

    @server.tool(annotations=WRITE, structured_output=True)
    def submit_candidate(job_id: str, generation: int, proposal: dict[str, Any]) -> dict[str, Any]:
        """Return a candidate for the current bound matter using begin_discussion's
        job handle. proposal contains summary, unknowns, sources and actions.
        Each action has command, reason and payload_json. Saves for review only;
        actual changes require the user's confirmation in the management app."""
        return routing.request('submit', job_id=job_id, generation=generation, proposal=proposal)

    from .context_mcp import register_tools
    register_tools(server,query)
    return server


def run(data_dir: str | Path) -> None:
    create_server(data_dir).run(transport="stdio")


def main() -> None:
    parser = argparse.ArgumentParser(description="个人事务管理 MCP 接口（标准输入输出）")
    parser.add_argument("--data-dir", required=True, help="与桌面客户端相同的数据目录")
    args = parser.parse_args()
    run(args.data_dir)


if __name__ == "__main__":
    main()
