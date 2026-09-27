"""One bounded read/checkpoint contract for ordinary and managed MCP sessions."""
from __future__ import annotations
import json
from typing import Any
from mcp.types import ToolAnnotations, CallToolResult, TextContent, ImageContent
from mcp.server.mcpserver.exceptions import ToolError

TOOL_NAMES={'query_context','read_context_item','read_material','describe_action',
            'context_coverage','operation_status','next_context_step','checkpoint_context','validate_candidate','refresh_context'}
READ=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False)
STATE=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False)


def register_tools(server,query,*,bound=False):
    if not bound:
        @server.tool(annotations=READ,structured_output=True)
        def prepare_context(goal:str,scope:dict[str,Any]|None=None,source_ids:list[str]|None=None,operation_id:str|None=None)->dict[str,Any]:
            """Open a small goal/index envelope; resume with operation_id.
            Does not launch a model, copy the database or write business facts."""
            return query('prepare_context',goal=goal,scope=scope or {'kind':'general'},source_ids=source_ids or [],operation_id=operation_id)

    @server.tool(annotations=READ,structured_output=True)
    def query_context(operation_id:str,collection:str,filters:dict[str,Any]|None=None,cursor:str|None=None,limit:int=80)->dict[str,Any]:
        """Read a stable byte-bounded page. Follow next_cursor; filters support
        type/status/search/parent_id/date_from/date_to. Facts and history are also
        paged. To read past analysis, use collection=facts with filters={job_id:...};\n        it uses this current operation's budget and labels historical evidence. Index excerpts never mean whole records or materials were read."""
        return query('query_context',operation_id=operation_id,collection=collection,filters=filters or {},cursor=cursor,limit=limit)

    @server.tool(annotations=READ,structured_output=True)
    def read_context_item(operation_id:str,id:str,offset:int=0,version:Any=None)->dict[str,Any]:
        """Read canonical details, including @goal, @plan_request, @schedule and
        @daily_review. Entity dependencies point to dependencies:<id>, containing
        predecessors, dependents and dated completion evidence. If next_offset exists, concatenate json_fragment in order,
        passing the same version; do not infer from a partial JSON fragment."""
        return query('read_context_item',operation_id=operation_id,id=id,offset=offset,version=version)

    def material(operation_id,source_id=None,chunk=None,from_job=None):
        result=query('read_material' if chunk is not None or from_job else 'next_context_step',operation_id=operation_id,source_id=source_id,chunk=chunk,from_job=from_job)
        content=result.get('content') or {}
        blocks=[TextContent(type='text',text=json.dumps(result,ensure_ascii=False))]
        if content.get('image_ref'):
            image=query('material_image',operation_id=operation_id,image_ref=content['image_ref'],from_job=result.get('from_job'))
            blocks.append(ImageContent(type='image',mimeType=image['mime_type'],data=image['data']))
        # Codex prefers structuredContent over content when both are present;
        # keep only native content blocks here so image evidence reaches vision.
        return CallToolResult(content=blocks)

    @server.tool(annotations=READ,structured_output=False)
    def next_context_step(operation_id:str)->CallToolResult:
        """Automatically obtain the next unprocessed material chunk/image.
        Read it, save its facts with checkpoint_context, then call again until
        all_steps_processed. Includes actual images, not unread image paths."""
        return material(operation_id)

    @server.tool(annotations=READ,structured_output=False)
    def read_material(operation_id:str,source_id:str,chunk:int|None=None,from_job:str|None=None)->CallToolResult:
        """Read a selected original. Omit chunk for the next unprocessed unit,\n        or supply a returned chunk number to re-read exact evidence after compaction.
        Raw text, images and unsupported embedded units have distinct coverage."""
        return material(operation_id,source_id,chunk,from_job)

    @server.tool(annotations=STATE,structured_output=True)
    def checkpoint_context(operation_id:str,step_key:str|None=None,result:dict[str,Any]|None=None,summary:str='',yield_for_compaction:bool=False,delivery_token:str|None=None,expected_fingerprint:str|None=None)->dict[str,Any]:
        """Echo the exact delivery_token from the read. To explicitly correct an\n        existing checkpoint, re-read the raw chunk and provide its checkpoint_fingerprint\n        as expected_fingerprint. Old drafts are archived; re-merge before submitting.\n        Durably acknowledge analysis of a delivered chunk. result.facts uses
        {key,value,evidence}; keep key scoped to course/topic and retain conflicts.
        result.actions may hold a bounded command/payload_json/reason batch; use replace_actions with candidate row IDs to revise drafts. No business writes. On budget warning, save a short summary, set
        yield_for_compaction=true, then finish the turn. Software automatically
        compacts and continues the same operation; never create a new task."""
        return query('checkpoint_context',operation_id=operation_id,step_key=step_key,result=result or {},summary=summary,delivery_token=delivery_token,expected_fingerprint=expected_fingerprint,**{'yield':yield_for_compaction})

    @server.tool(annotations=READ,structured_output=True)
    def operation_status(operation_id:str,query_cursor:str|None=None)->dict[str,Any]:
        """Resume from durable cursors, material steps and checkpoint. This is
        not permission to apply a candidate; cancellation and epoch are checked."""
        return query('operation_status',operation_id=operation_id,query_cursor=query_cursor)

    @server.tool(annotations=READ,structured_output=True)
    def context_coverage(operation_id:str)->dict[str,Any]:
        """Report extracted, delivered, processed and remaining coverage."""
        return query('context_coverage',operation_id=operation_id)

    @server.tool(annotations=STATE,structured_output=True)
    def refresh_context(operation_id:str,include_new_sources:bool=False)->dict[str,Any]:
        """After context_changed, refresh evidence within this same operation.
        Keeps unchanged material checkpoints; archives superseded draft results.
        Set include_new_sources only when the original goal covers the entire
        course/project material collection. No business writes or new task."""
        return query('refresh_context',operation_id=operation_id,include_new_sources=include_new_sources)

    @server.tool(annotations=READ,structured_output=True)
    def describe_action(operation_id:str,command:str,type:str|None=None,offset:int=0)->dict[str,Any]:
        """Discover just the action/type needed, not the full type catalog."""
        return query('describe_action',operation_id=operation_id,command=command,type=type,offset=offset)

    @server.tool(annotations=READ,structured_output=True)
    def validate_candidate(operation_id:str,proposal:dict[str,Any])->dict[str,Any]:
        """Validate complete applicable constraints and current evidence locally.
        Rejections identify specific collections/records to re-read in this
        operation. Payload shape matches submit_candidate; nothing is adopted."""
        return query('validate_candidate',operation_id=operation_id,proposal=proposal)


INSTRUCTIONS="""
This discussion uses context/1. begin_discussion returns ONLY a goal and indexes,
never all database facts. Use the bounded context tools. If selected_skill is present, read @skill before doing that workflow. A search result or first
page is not a complete collection. Fetch all pages of deadlines/rules/events
before planning. Get exact IDs, details, dependencies and current plan with the
readers. Follow each selected item's dependencies reader, including every page;
predecessors must finish before dependents, and planned does not mean completed.
Time estimates never change facts or completion gates.
scope.analyze_materials distinguishes complete analysis from references. When it
is false (e.g. ordinary completion feedback), do not re-analyze the whole course;
look up only the specific material chunks needed. When true, for selected
materials call next_context_step, analyze each text/image/gap,
checkpoint facts with source locations, repeat until all_steps_processed. Read
all pages of the stored facts to merge. Keep contradictory values and sources;
do not let the last summary overwrite earlier evidence. Unsupported content
remains explicitly unknown in the candidate.
Each stage has a cumulative input budget. When context_checkpoint_required is
returned, checkpoint your short working summary, yield_for_compaction=true,
then end this turn. The software automatically continues the ORIGINAL operation
in the SAME task after confirmed context compaction. No new authorization is
implied. On an automatic continuation use operation_status, not begin_discussion.
Your memory is only a navigation aid; durable steps and current records govern.
For many business changes, stage bounded result.actions batches in checkpoint_context.
The final candidate seals ALL staged actions, not just its preview page. Return
only additional actions in submit_candidate. All staged actions remain subject
to user review and one atomic business adoption.
Do not submit a final candidate while selected material steps remain unfinished.
First validate_candidate; fix precise errors in the same operation, then submit.
"""
