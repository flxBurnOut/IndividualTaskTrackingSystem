"""One bounded read/checkpoint contract for ordinary and managed MCP sessions."""
from __future__ import annotations
import json
from typing import Any
from mcp.types import ToolAnnotations, CallToolResult, TextContent, ImageContent
from mcp.server.mcpserver.exceptions import ToolError

TOOL_NAMES={'query_context','read_context_item','read_material','describe_action',
            'context_coverage','operation_status','next_context_step','checkpoint_context','validate_candidate','refresh_context',
            'read_material_batch','checkpoint_context_batch','read_context_batch'}
READ=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False)
STATE=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False)


def register_tools(server,query,*,bound=False):
    if not bound:
        @server.tool(annotations=READ,structured_output=True)
        def prepare_context(goal:str,scope:dict[str,Any]|None=None,source_ids:list[str]|None=None,operation_id:str|None=None)->dict[str,Any]:
            """Open a small goal/index envelope; resume with operation_id.
            Follow the returned mode and next_action; never choose another mode
            or create a replacement operation to bypass a bound matter.
            Does not launch a model, copy the database or write business facts."""
            return query('prepare_context',goal=goal,scope=scope or {'kind':'general'},source_ids=source_ids or [],operation_id=operation_id)

    @server.tool(annotations=READ,structured_output=True)
    def query_context(operation_id:str,collection:str,filters:dict[str,Any]|None=None,cursor:str|None=None,limit:int=80,view:str="results")->dict[str,Any]:
        """Read a stable byte-bounded page. Follow next_cursor; filters support
        type/status/search/parent_id/date_from/date_to. Facts and history are also
        paged. To read past analysis, use collection=facts with filters={job_id:...};\n        it uses this current operation's budget and labels historical evidence. For collection=facts use view=facts to merge all saved facts/notes without empty acknowledgements or candidate actions. Index excerpts never mean whole records or materials were read."""
        return query('query_context',operation_id=operation_id,collection=collection,filters=filters or {},cursor=cursor,limit=limit,view=view)

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
    def checkpoint_context(operation_id:str,step_key:str|None=None,result:dict[str,Any]|None=None,summary:str|None=None,yield_for_compaction:bool=False,delivery_token:str|None=None,expected_fingerprint:str|None=None)->dict[str,Any]:
        """Echo the exact delivery_token from the read. To explicitly correct an\n        existing checkpoint, re-read the raw chunk and provide its checkpoint_fingerprint\n        as expected_fingerprint. Old drafts are archived; re-merge before submitting.\n        Durably acknowledge analysis of a delivered chunk. result.facts uses
        {key,value,evidence}; keep key scoped to course/topic and retain conflicts.
        result.actions may hold a bounded command/payload_json/reason batch; use
        replace_actions with candidate row IDs to revise drafts. No business writes.
        Keep summary within 4000 serialized JSON UTF-8 bytes, not characters;
        store detailed facts in bounded result batches. checkpoint_limit keeps
        earlier saved progress: shorten the rejected payload and retry.
        Follow the service's mode/next_action. For ordinary_mcp, yield only saves
        progress: keep reading the same operation; the Codex host manages its own
        context. For managed_job, request yield on a checkpoint requirement and
        end the turn only when end_turn_for_compaction is true. Software can
        resume that job after confirmed compaction; do not claim it has already
        resumed, fabricate compaction, or create another task/operation."""
        return query('checkpoint_context',operation_id=operation_id,step_key=step_key,result=result or {},delivery_token=delivery_token,expected_fingerprint=expected_fingerprint,**({'summary':summary} if summary is not None else {}),**{'yield':yield_for_compaction})

    @server.tool(annotations=READ,structured_output=False)
    def read_material_batch(operation_id:str,source_id:str|None=None,limit:int=20,byte_limit:int=49152,max_images:int=4)->CallToolResult:
        """Preferred material reader: up to 32 units, 64 KiB context budget and
        four native images. Every text/image/gap retains its step key, version,
        location and delivery token. Analyze ALL returned items then checkpoint
        them together. A read never marks analysis complete. On lost response,
        retry: unacknowledged items are returned first; completed ones are skipped.
        Continue until all_steps_processed, including extraction_pending batches.
        source_complete applies only to the optional selected source."""
        result=query('read_material_batch',operation_id=operation_id,source_id=source_id,limit=limit,byte_limit=byte_limit,max_images=max_images)
        blocks=[TextContent(type='text',text=json.dumps(result,ensure_ascii=False))]
        for item in result.get('items',[]):
            ref=item.get('content',{}).get('image_ref')
            if ref:
                picture=query('material_image',operation_id=operation_id,image_ref=ref)
                blocks.append(TextContent(type='text',text='Image for step '+item['step_key']))
                blocks.append(ImageContent(type='image',mimeType=picture['mime_type'],data=picture['data']))
        return CallToolResult(content=blocks)

    @server.tool(annotations=STATE,structured_output=True)
    def checkpoint_context_batch(operation_id:str,items:list[dict[str,Any]],summary:str|None=None,yield_for_compaction:bool=False)->dict[str,Any]:
        """Atomically save up to 32 analyzed units (64 KiB total input).
        Each item needs step_key, delivery_token and result; optionally include
        expected_fingerprint for an explicit correction. Every item passes the
        same version/evidence/conflict checks as checkpoint_context. One invalid
        item rejects the WHOLE batch. Retry the identical batch after uncertainty;
        already-saved results/actions are not applied twice. Omit summary to keep
        the previous one. Save concise source-linked facts, NOT copied raw text;
        result={} acknowledges an analyzed unit with no new facts. Keep all actual
        facts, conflicts, visual findings and unsupported-content gaps. A save is
        never permission for business writes. Follow returned mode/next_action."""
        return query('checkpoint_context_batch',operation_id=operation_id,items=items,
                     **({'summary':summary} if summary is not None else {}),**{'yield':yield_for_compaction})

    @server.tool(annotations=READ,structured_output=True)
    def read_context_batch(operation_id:str,requests:list[dict[str,Any]],start:int=0,byte_limit:int=49152)->dict[str,Any]:
        """Batch up to 32 controlled detail/collection reads within 64 KiB.
        Each request is {id,offset?,version?} OR {collection,filters?,cursor?,limit?,view?}.
        Results retain their original individual versions and next_offset/cursor;
        continue each unfinished resource. next_request_index resumes the SAME
        request list at start; requests_complete only means requests were served,
        never that all their pages were read. Preserves dependency/history scope,
        managed-stage limits and atomic read bookkeeping. No arbitrary commands."""
        return query('read_context_batch',operation_id=operation_id,requests=requests,start=start,byte_limit=byte_limit)

    @server.tool(annotations=READ,structured_output=True)
    def operation_status(operation_id:str,query_cursor:str|None=None)->dict[str,Any]:
        """Resume from durable cursors, material steps and checkpoint. This is
        not permission to apply a candidate; cancellation and epoch are checked.
        Follow mode/next_action. An old ordinary_mcp checkpoint can resume here
        without a background job, a new operation, or another upload."""
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
    def describe_action(operation_id:str,command:str,type:str|None=None,offset:int=0,version:str|None=None)->dict[str,Any]:
        """Discover only this action/type. Continue next_offset with the returned
        version; changed schemas reject splicing old and new fragments."""
        return query('describe_action',operation_id=operation_id,command=command,type=type,offset=offset,version=version)

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
materials prefer read_material_batch, analyze EVERY text/image/gap, then save
checkpoint_context_batch with the exact per-item tokens; repeat until all_steps_processed.
Use read_context_batch for independent details/collections and preserve each cursor.
Store concise facts with source locations, not copies of raw text; analyzed units
with no new facts can have result={}. Never mark unread content as analyzed.
Read all pages of query_context(collection=facts,view=facts) to merge stored facts/notes.
Empty acknowledgements are not content and need not be read back. Keep contradictory values and sources;
do not let the last summary overwrite earlier evidence. Unsupported content
remains explicitly unknown in the candidate.
The service determines mode and next_action; never switch mode yourself or create
a new operation to evade a boundary. In ordinary_mcp mode, cumulative usage is
advisory: follow next_cursor/next_offset or next_context_step in this operation.
checkpoint_context saves durable progress; yield_for_compaction does not schedule
another turn or stop reading. The Codex host manages its own context. After an
interruption use operation_status with the same operation_id, including for an
old checkpoint that was blocked. Never pretend compaction occurred.
In managed_job mode, stage budgets are enforced. On context_checkpoint_required,
checkpoint a short working summary, yield_for_compaction=true, and end the turn
only when end_turn_for_compaction is true. The software can continue the ORIGINAL
operation in the SAME task after confirmed context compaction. A saved checkpoint
is not evidence that continuation has begun. If compaction fails or is unconfirmed,
report the returned issue and recovery action; do not promise automatic progress.
No new authorization is implied. On an automatic continuation use operation_status,
not begin_discussion. Summary is limited to 4000 serialized JSON UTF-8 bytes;
put detailed facts in bounded result batches. On checkpoint_limit shorten only the
rejected payload; previous saved progress remains available.
Your memory is only a navigation aid; durable steps and current records govern.
For many business changes, stage bounded result.actions batches in checkpoint_context.
The final candidate seals ALL staged actions, not just its preview page. Return
only additional actions in submit_candidate. All staged actions remain subject
to user review and one atomic business adoption.
Do not submit a final candidate while selected material steps remain unfinished.
First validate_candidate; fix precise errors in the same operation, then submit.
"""
