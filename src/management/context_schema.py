"""Operational context state. Additive, independent of business revisions."""
from __future__ import annotations


def initialize(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS context_operations(
        id TEXT PRIMARY KEY, epoch TEXT NOT NULL, scope TEXT NOT NULL,
        goal TEXT NOT NULL, job_id TEXT, phase TEXT NOT NULL DEFAULT 'reading',
        stage INTEGER NOT NULL DEFAULT 0, delivered_bytes INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
        checkpoint TEXT NOT NULL DEFAULT '{}', validation TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS context_job ON context_operations(job_id) WHERE job_id IS NOT NULL;
    CREATE TABLE IF NOT EXISTS context_generations(key TEXT PRIMARY KEY, version INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS context_queries(
        id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES context_operations(id),
        collection TEXT NOT NULL, params TEXT NOT NULL, stamp TEXT NOT NULL,
        after_key TEXT NOT NULL DEFAULT '', total INTEGER NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
        complete INTEGER NOT NULL DEFAULT 0, last_cursor TEXT, last_page TEXT, signature TEXT, UNIQUE(operation_id,collection,params));
    CREATE TABLE IF NOT EXISTS context_parts(
        operation_id TEXT NOT NULL,item_id TEXT NOT NULL,version TEXT NOT NULL,
        read_until INTEGER NOT NULL DEFAULT 0,complete INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(operation_id,item_id));
    CREATE TABLE IF NOT EXISTS context_reads(
        operation_id TEXT NOT NULL REFERENCES context_operations(id),
        entity_id TEXT NOT NULL, version INTEGER NOT NULL, detail INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(operation_id,entity_id));
    CREATE TABLE IF NOT EXISTS context_selection(
        operation_id TEXT NOT NULL,entity_id TEXT NOT NULL,version INTEGER NOT NULL,
        PRIMARY KEY(operation_id,entity_id));
    CREATE TABLE IF NOT EXISTS context_sources(
        operation_id TEXT NOT NULL REFERENCES context_operations(id),
        entity_id TEXT NOT NULL, version INTEGER NOT NULL, material_key TEXT, indexed_chunks INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(operation_id,entity_id));
    CREATE TABLE IF NOT EXISTS context_step_history(
        seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL,step_key TEXT NOT NULL,
        snapshot TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS context_step_history_operation ON context_step_history(operation_id,seq);
    CREATE TABLE IF NOT EXISTS context_actions(
        operation_id TEXT NOT NULL,seq INTEGER PRIMARY KEY AUTOINCREMENT,
        fingerprint TEXT NOT NULL,command TEXT NOT NULL,payload TEXT NOT NULL,reason TEXT NOT NULL,
        UNIQUE(operation_id,fingerprint));
    CREATE INDEX IF NOT EXISTS context_actions_operation ON context_actions(operation_id,seq);
    CREATE TABLE IF NOT EXISTS context_steps(
        operation_id TEXT NOT NULL REFERENCES context_operations(id),
        step_key TEXT NOT NULL, source_id TEXT, source_version INTEGER,
        chunk_key TEXT, status TEXT NOT NULL DEFAULT 'pending',
        result TEXT, fingerprint TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY(operation_id,step_key));
    CREATE INDEX IF NOT EXISTS context_pending_steps ON context_steps(operation_id,status,step_key);
    CREATE TABLE IF NOT EXISTS material_catalog(
        key TEXT PRIMARY KEY, sha256 TEXT NOT NULL, name TEXT NOT NULL,
        layout TEXT, cursor TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
        units_done INTEGER NOT NULL DEFAULT 0, chunks INTEGER NOT NULL DEFAULT 0,
        coverage TEXT NOT NULL DEFAULT '{}', error TEXT, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS material_chunks(
        material_key TEXT NOT NULL REFERENCES material_catalog(key), seq INTEGER NOT NULL,
        kind TEXT NOT NULL, locator TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
        PRIMARY KEY(material_key,seq));
    ''')
    if 'indexed_chunks' not in {r[1] for r in c.execute('PRAGMA table_info(context_sources)')}:
        c.execute('ALTER TABLE context_sources ADD COLUMN indexed_chunks INTEGER NOT NULL DEFAULT 0')
    if 'tool_calls' not in {r[1] for r in c.execute('PRAGMA table_info(context_operations)')}:
        c.execute('ALTER TABLE context_operations ADD COLUMN tool_calls INTEGER NOT NULL DEFAULT 0')
    columns={r[1] for r in c.execute('PRAGMA table_info(context_queries)')}
    for column in ('last_cursor','last_page','signature'):
        if column not in columns:c.execute('ALTER TABLE context_queries ADD COLUMN '+column+' TEXT')
    if 'detail' not in {r[1] for r in c.execute('PRAGMA table_info(context_reads)')}:
        c.execute('ALTER TABLE context_reads ADD COLUMN detail INTEGER NOT NULL DEFAULT 0')
    # Cheap collection generations, including writes outside Core (migration).
    # They never store old payloads or copy a collection into each operation.
    for action, refs in [('INSERT', ('new',)), ('DELETE', ('old',)), ('UPDATE', ('old', 'new'))]:
        statements = []
        for ref in refs:
            for key in [f"'type:'||{ref}.type", "'entities'"]:
                statements.append(f'''INSERT INTO context_generations VALUES ({key},1)
                    ON CONFLICT(key) DO UPDATE SET version=version+1;''')
        c.execute(f'''CREATE TRIGGER IF NOT EXISTS context_entity_{action.lower()}
            AFTER {action} ON entities BEGIN {''.join(statements)} END''')
    for action in ('INSERT', 'UPDATE', 'DELETE'):
        c.execute(f'''CREATE TRIGGER IF NOT EXISTS context_link_{action.lower()}
            AFTER {action} ON links BEGIN
            INSERT INTO context_generations VALUES ('links',1)
            ON CONFLICT(key) DO UPDATE SET version=version+1; END''')
