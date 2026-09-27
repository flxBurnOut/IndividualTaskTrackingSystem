"""Thread-scoped MCP: fresh context and durable candidate return only."""
from __future__ import annotations
import json
import tomllib
from typing import Any
from pathlib import Path
from .schemas import BusinessError

SERVER_NAME = 'personal_management_discussion'
CONTRACT = 'desktop_mcp_v3'


def configuration(workspace, conversation_id, epoch):
    config_path=Path(workspace)/'.codex/config.toml'
    try:
        raw=config_path.read_bytes()
        if len(raw)>2_097_152:raise ValueError()
        original=tomllib.loads(raw.decode('utf-8-sig'))['mcp_servers']['personal_management']
        command=original['command'];args=original.get('args',[])
        if not isinstance(command,str) or not isinstance(args,list) or not all(isinstance(a,str) for a in args):raise ValueError()
        # Reuse the configured, validated packaged runtime; no shell command.
        args=[a for a in args if a!='--mcp']
        args+=['--mcp-discussion',conversation_id,'--discussion-epoch',epoch]
        result={f'mcp_servers.{SERVER_NAME}.command':command,
                f'mcp_servers.{SERVER_NAME}.args':args,
                f'mcp_servers.{SERVER_NAME}.enabled':True}
        if original.get('env'):result[f'mcp_servers.{SERVER_NAME}.env']=original['env']
        return result
    except (OSError,KeyError,TypeError,ValueError) as exc:
        raise BusinessError('codex_project_incomplete','讨论业务接口尚未配置，请在设置中重新连接 Codex。') from exc


def create_server(data_dir, conversation_id, epoch, *, client=None):
    from .mcp_routing import RoutedMCPServer, DiscussionRouting
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
    from .client import Client
    client=client or Client(data_dir, entrance='mcp')
    server=RoutedMCPServer('personal-management-discussion',instructions=(
        '每轮先 begin_discussion 读取本事项的最新事实；提交候选后用自然中文回答。'
        '候选需用户在软件确认才会生效。不得通过脚本或其他工具改库。'),log_level='WARNING')
    routing = DiscussionRouting(server, client)

    def request(action,**params):
        try:
            routing.guard_fixed_binding(conversation_id, epoch)
            return client._request('discussion',action,{'conversation_id':conversation_id,'epoch':epoch,**params},timeout=100 if params.get('name') in {'next_context_step','read_material'} else 35)
        except Exception as exc:
            if hasattr(exc,'code'):raise ToolError(json.dumps({'code':exc.code,'message':str(exc),'details':getattr(exc,'details',{})},ensure_ascii=False)) from exc
            raise

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False),structured_output=True)
    def begin_discussion(text:str,request_id:str) -> dict[str,Any]:
        """Start this user turn with their exact message. Reuses a pending software
        request, or creates a native-desktop request in the same business conversation.
        Use a unique request_id for each user turn; reuse it on retries.
        Returns fresh context, job_id, generation and the allowed candidate commands.
        Never starts another model. Do not use previous chat as current facts."""
        return request('begin',text=text,request_id=request_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False),structured_output=True)
    def query_business(name:str, params:dict[str,Any]|None=None) -> dict[str,Any]:
        """Read current evidence, including paged course records and source content.
        A query is never a request to start another AI or apply any change."""
        routing.guard_fixed_binding(conversation_id, epoch)
        allowed={'state','get','list','source_content','sources','daily_review','weekly_review','plan_context',
                 'daily_tasks','recovery_summary','object_workspace','workspace_tasks','timetables','timetable_week','receipt'}
        if name not in allowed:raise ToolError('此会话不开放这个查询。')
        params=params or {}
        view=client.query('conversation',id=conversation_id)
        active=(view.get('conversation') or {}).get('active_job_id')
        if not active:raise ToolError('请先 begin_discussion。')
        job=client.query('job',id=active)['job']
        operation=job['input'].get('context_operation_id')
        if not operation:raise ToolError('本轮是旧版候选，请在原事项发起新消息。')
        if name=='get':return request('context',name='read_context_item',params={'operation_id':operation,'id':params['id']})
        return {'operation_id':operation,'reader':'query_context','collection':{'sources':'materials','list':'records','daily_tasks':'tasks','workspace_tasks':'tasks'}.get(name,'records'),
                'instruction':'使用统一分页工具读取；计划信息在 @plan_request、@schedule 和 @daily_review。'}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False),structured_output=True)
    def submit_candidate(job_id:str,generation:int,proposal:dict[str,Any]) -> dict[str,Any]:
        """Persist exactly one candidate using the handle from begin_discussion.
        proposal has summary, unknowns, sources and actions. Each action contains
        command, reason and payload_json (a JSON string). Nothing is applied here.
        Retries of the identical candidate are safe; conflicting candidates rejected."""
        return request('submit',job_id=job_id,generation=generation,proposal=proposal)
    from .context_mcp import register_tools
    register_tools(server,lambda name,**params:request('context',name=name,params=params),bound=True)
    return server


def _legacy_instructions(base):
    return base.replace('You have no tools.','Use only the personal_management_discussion MCP tools.').replace(
        'Return exactly the supplied proposal schema. This is a proposal, never a receipt.',
        'Submit the supplied proposal schema through submit_candidate, then answer in natural Chinese. A proposal is not a business receipt.'
    )+"""

Every user turn, including messages typed directly in Codex desktop, must begin
with begin_discussion(text=<the exact current user message>,request_id=<unique per user turn; reuse on retry>). Its fresh context,
allowed_commands and job handle override older discussion. query_business reads
additional current evidence. Do not recursively send a message or start a model.
Call submit_candidate(job_id,generation,proposal) exactly once, then give a concise
natural Chinese answer. Never print raw JSON or machine identifiers to the user.
Only these three MCP tools are allowed. Use tool_search to discover them when deferred. Do not use legacy dynamic candidate tools.
A successful candidate acknowledgement means it is saved for review; only a
business receipt after user confirmation means the actual changes were applied.
"""


def run(rpc,input,workspace,images,allowed,cancel,report,read_candidate):
    from .ai import AIError
    from .ai_shared_turn import _normal_text
    from .ai_stream import matching_event
    rpc.allowed_mcp_server=SERVER_NAME
    report('sending')
    text=input['prompt']
    if input.get('context_continuation'):
        text='[软件自动接续原请求，无新增用户授权] 原操作 '+input['context_operation_id']+'。请用 operation_status 从检查点继续，勿重新 begin_discussion。'
    turn=rpc.request('turn/start',{'threadId':rpc.thread_id,'input':[{'type':'text','text':text},*images],
        'cwd':workspace,'environments':[],'approvalPolicy':'never',
        'sandboxPolicy':{'type':'readOnly','networkAccess':False},'summary':'none'})
    rpc.turn_id=turn['turn']['id']
    if not isinstance(rpc.turn_id,str) or not rpc.turn_id:raise AIError('AI_PROTOCOL_ERROR','Codex 未返回本轮标识。')
    report('waiting_model',provider_turn_id=rpc.turn_id)
    texts={};size=0;forced_checkpoint=False
    while True:
        if cancel.is_set():raise AIError('AI_CANCELLED','本次处理已取消。')
        event=rpc.next_event();body=event.get('params') or {}
        if not matching_event(body,rpc.thread_id,rpc.turn_id):continue
        method=event.get('method')
        if method=='item/agentMessage/delta':
            key,delta=body.get('itemId'),body.get('delta')
            if not isinstance(key,str) or not isinstance(delta,str):raise AIError('AI_PROTOCOL_ERROR','Codex 回复分片无效。')
            size+=len(delta.encode('utf-8'))
            if size>512000 or len(texts)>64:raise AIError('AI_OUTPUT_LIMIT','回复超过本轮容量。')
            texts[key]=texts.get(key,'')+delta
            report('receiving',preview_text=_normal_text(texts[key])[:32000])
        elif method in {'item/reasoning/textDelta','item/reasoning/summaryTextDelta'}:report('reasoning')
        elif method=='item/completed' and (body.get('item') or {}).get('type')=='mcpToolCall':
            publisher=getattr(read_candidate,'__self__',None)
            status=publisher.context_status() if hasattr(publisher,'context_status') else None
            if status and (status['phase']=='budget_stop' or status.get('tool_calls',0)>=128) and not read_candidate():
                forced_checkpoint=True
                rpc.request('turn/interrupt',{'threadId':rpc.thread_id,'turnId':rpc.turn_id})
        elif method=='turn/completed':
            if body['turn'].get('status')!='completed' and not (forced_checkpoint and body['turn'].get('status')=='interrupted'):raise AIError('AI_GENERATION_FAILED','Codex 本轮未正常结束，请核对已返回候选。')
            candidate=read_candidate()
            if not candidate:
                publisher=getattr(read_candidate,'__self__',None)
                status=publisher.context_status() if hasattr(publisher,'context_status') else None
                if status and (status['phase']=='checkpointed' or forced_checkpoint):
                    report('checkpointed')
                    return {'context_continuation':True,'actions':[],'summary':'当前批已保存，自动接续原操作。'}
                raise AIError('AI_EMPTY_OUTPUT','Codex 未通过业务接口返回候选，本轮没有可保存的变更。')
            return candidate


def instructions(base):
    from .context_mcp import INSTRUCTIONS
    return _legacy_instructions(base).replace('Only these three MCP tools are allowed.', 'Use only the controlled personal_management_discussion MCP tools, including the context/1 readers and checkpoints listed below.')+INSTRUCTIONS
