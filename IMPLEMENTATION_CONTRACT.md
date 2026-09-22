# First implementation contract

Source package: `src/management` (Python 3.12). No TypeScript or Node frontend. Production and synthetic-test data roots are separate. Never import D:/Y2S1 or .analysis.

## Shared API

`management.client.Client(data_dir: str|Path, autostart=True)` locates/starts the independent loopback service. Methods: `query(name, **params) -> dict`, `command(name, payload: dict, *, request_id=None, expected_revision=None, epoch=None) -> dict`, `state() -> dict`. Client caches latest epoch/revision from each response. ClientError has `.code`, `.message`, `.details`. Requests carry local service token, no network listener other than 127.0.0.1.

HTTP POST `/v1/query/{name}` with JSON params. HTTP POST `/v1/commands/{name}` with `{request_id, epoch, expected_revision, payload}`. All successes include `epoch`, `revision`. Command receipt includes `request_id`, `result`, `revision`, `epoch`, `replayed`. All failures have `{error:{code,message,details}}`. Exact request-id retries resolve committed receipts even when revision changed; different payload with same ID is an error. Fresh epoch required. No AI or file I/O inside write transactions.

## Queries

- `state`: schema_version, epoch, revision, counts by type, provider configuration summary.
- `capabilities`: types (id,label,fields,parent_types,section,module), commands and workflows. All standard entities share `id,type,title,parent_id,status,archived,data,version,created_at,updated_at`.
- `list`: optional type/types, parent_id, status, archived (default false), search, limit (default100,max500), offset. Returns `items,total,next_offset`.
- `get`: id. Returns `entity,children,links,history` (bounded).
- `changes`: after, limit. Returns items, cursor, reset_required.
- `today`: date (ISO). Returns tasks, events, plans, notifications, counts.
- `plan_context`: date, mode. Returns hard events, tasks, capacity/rules, unknowns, coverage.
- `review`: start,end. Returns metrics, unknowns, facts, coverage.
- `jobs`: optional status. Returns items.
- `settings`: returns settings (never secrets) and module config.

## Commands

- `create`: payload `{type,title,parent_id?,status?,data?}`. Core typed validation, immutable ID, version1.
- `update`: `{id,version,patch:{title?,status?,data?}}`; data merges fields.
- `move`: `{id,version,parent_id}`; cycle/parent/depth checks, no implicit propagation.
- `archive`: `{id,version,archived:bool}`; reject active children.
- `link`: `{source_id,target_id,kind}` / `unlink`: `{id}`. Dependency DAG.
- `record_feedback`: `{target_id,business_date,dimensions:{completion?,attendance?,viewing?,submission?,mastery?,actual_minutes?},source_text}`; explicit facts only, no inferred status/replan.
- `create_plan`: `{date,mode,title?,blocks:[{target_id,start?,end?,minutes?,completion_gate?}],source_text?}`; validate hard constraints, no truncated tasks or implicit actuals.
- `create_checkin`: `{date,target_ids?}` saves a dated question set; `respond_checkin`: `{id,answers:[{question_id,dimensions}],source_text}`; no-reply is not a fact.
- `save_review`: `{start,end,title?,text}` with metrics snapshot.
- `settings`: `{settings:{...}}`; runtime settings explicit.
- `install_module`: `{manifest}` / `disable_module`: `{id}`; versioned declarative type/field/rule/workflow extensions. No arbitrary Python eval.
- `create_job`: `{kind,input}`; durable job queue; `cancel_job`: `{id}`; `apply_proposal`: `{id}` validates saved AI proposal against captured versions. GUI and external MCP can query jobs; external MCP never recursively invokes AI unless specifically requested as a job (ordinary CRUD does not).

## Agent ownership

Root: storage.py, core.py, schemas.py, service.py, client.py, __main__.py, runtime.py, scheduler.py; tests/test_core.py and integration; project setup/package.
GUI agent: gui.py and optional gui_* modules, tests/test_gui.py. Native Qt five stable navigation entries 今天 / 安排 / 项目与领域 / 资料 / 复盘. Global capture/search/notifications/settings. No DB access. Use API Client. Refresh changes safely, preserve form edits. Usable forms, empty state, creation/edit/feedback/plan/review/AI job/config/backup actions. Extra server commands coordinate first.
Codex agent: mcp_server.py, ai.py, tests/test_adapters.py, adapter docs. `ai.generate(input:dict, settings:dict, cancel:threading.Event) -> dict` returns structured proposal only; no DB writes. Local Codex app-server adapter preferred, isolated subprocess, no user old-data context, bounded output/time, cancellation. MCP dispatches Client query/command, fresh context and receipts. No config auto-install into user global Codex config.
Resources agent: resources.py, tests/test_resources.py. `ResourceManager(data_dir)` handles streaming immutable asset import, bundle materialization, registered artifact workspace and freeze, backup/verify/restore helper. Coordinate signatures with root. No direct business commands/UI, no schema changes unless coordinated. Return metadata for root transaction. Default reserve1GiB; immutable content, no auto deletion of unique output. All synthetic tests isolated.

Data root: database.sqlite3, runtime.json (local discovery token; excluded from backups), blobs/<sha256>, jobs/<uuid>/, backups/, exports/. Runtime paths never part of portable business identity. Schema and generic config only seeded, business table starts empty.
