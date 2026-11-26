CREATE TABLE IF NOT EXISTS resource_requests(
    chat TEXT NOT NULL, request TEXT NOT NULL, action TEXT NOT NULL,
    fingerprint TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(chat, request)
);
CREATE TABLE IF NOT EXISTS project_files(
    id TEXT PRIMARY KEY, project TEXT NOT NULL, chat TEXT NOT NULL,
    name TEXT NOT NULL, version INTEGER NOT NULL, digest TEXT NOT NULL, data BLOB NOT NULL, created INTEGER NOT NULL,
    UNIQUE(project,name,version)
);
CREATE TABLE IF NOT EXISTS knowledge_docs(
    id TEXT PRIMARY KEY, project TEXT NOT NULL, chat TEXT NOT NULL, version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS knowledge_project ON knowledge_docs(project);
CREATE TABLE IF NOT EXISTS knowledge_versions(
    document TEXT NOT NULL REFERENCES knowledge_docs, version INTEGER NOT NULL, chat TEXT NOT NULL,
    title TEXT NOT NULL, text TEXT NOT NULL, updated INTEGER NOT NULL, PRIMARY KEY(document,version)
);
CREATE TABLE IF NOT EXISTS knowledge_state(
    document TEXT PRIMARY KEY REFERENCES knowledge_docs, revision INTEGER NOT NULL, enabled INTEGER NOT NULL,
    deleted INTEGER NOT NULL, tags TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_chunks(
    id TEXT PRIMARY KEY, document TEXT NOT NULL REFERENCES knowledge_docs, chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL, enabled INTEGER NOT NULL, deleted INTEGER NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS knowledge_chunk_document ON knowledge_chunks(document,deleted,chunk_index);
CREATE TABLE IF NOT EXISTS knowledge_history(
    document TEXT NOT NULL REFERENCES knowledge_docs, revision INTEGER NOT NULL, snapshot TEXT NOT NULL, PRIMARY KEY(document,revision)
);
CREATE TABLE IF NOT EXISTS knowledge_tag_definitions(
    id TEXT PRIMARY KEY, project TEXT NOT NULL, slot TEXT NOT NULL, type TEXT NOT NULL,
    created INTEGER NOT NULL, UNIQUE(project,slot)
);
CREATE TABLE IF NOT EXISTS project_tables(
    id TEXT PRIMARY KEY, project TEXT NOT NULL, chat TEXT NOT NULL,
    name TEXT NOT NULL, columns_json TEXT NOT NULL, updated INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tables_project ON project_tables(project);
CREATE TABLE IF NOT EXISTS project_records(
    id TEXT PRIMARY KEY, table_id TEXT NOT NULL REFERENCES project_tables, chat TEXT NOT NULL,
    values_json TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS records_table ON project_records(table_id);
CREATE TABLE IF NOT EXISTS graph_table_record_times(record TEXT PRIMARY KEY REFERENCES project_records ON DELETE CASCADE,updated INTEGER NOT NULL);
INSERT OR IGNORE INTO graph_table_record_times SELECT id,created FROM project_records;
CREATE TRIGGER IF NOT EXISTS graph_table_record_created AFTER INSERT ON project_records BEGIN
  INSERT INTO graph_table_record_times(record,updated) VALUES(NEW.id,NEW.created);
END;
CREATE TRIGGER IF NOT EXISTS graph_table_record_updated AFTER UPDATE OF values_json ON project_records BEGIN
  UPDATE graph_table_record_times SET updated=CAST(strftime('%s','now') AS INTEGER) WHERE record=NEW.id;
END;
CREATE TABLE IF NOT EXISTS resource_log(
    id INTEGER PRIMARY KEY, project TEXT NOT NULL, chat TEXT NOT NULL,
    at INTEGER NOT NULL, source TEXT NOT NULL, tool TEXT NOT NULL, host TEXT NOT NULL,
    status TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS resource_log_project ON resource_log(project,at);
CREATE TABLE IF NOT EXISTS typed_schemas(
    table_id TEXT PRIMARY KEY REFERENCES project_tables, revision INTEGER NOT NULL, schema_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS record_versions(
    record TEXT PRIMARY KEY REFERENCES project_records, version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS table_views(
    id TEXT PRIMARY KEY, project TEXT NOT NULL, table_id TEXT NOT NULL,
    name TEXT NOT NULL, version INTEGER NOT NULL, definition TEXT NOT NULL, updated INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS table_views_target ON table_views(project,table_id);
CREATE TABLE IF NOT EXISTS table_imports(
    id TEXT PRIMARY KEY, table_id TEXT NOT NULL REFERENCES project_tables, artifact TEXT REFERENCES project_files,
    digest TEXT NOT NULL, row_count INTEGER NOT NULL, revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_memory_messages(
 sequence INTEGER PRIMARY KEY, project TEXT NOT NULL,
 conversation TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
 content TEXT NOT NULL, created INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS graph_memory_conversation ON graph_memory_messages(project,conversation,sequence);

CREATE TABLE IF NOT EXISTS block_effects(
    run TEXT NOT NULL,node TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(run,node));
CREATE TABLE IF NOT EXISTS triggers(
    id TEXT PRIMARY KEY, workflow TEXT NOT NULL, version INTEGER NOT NULL, kind TEXT NOT NULL,
    config TEXT NOT NULL, resource TEXT, enabled INTEGER NOT NULL, next_due REAL, token_hash TEXT,
    last_error TEXT);
CREATE TABLE IF NOT EXISTS deliveries(
    id TEXT PRIMARY KEY,trigger_id TEXT NOT NULL REFERENCES triggers,workflow TEXT NOT NULL,version INTEGER NOT NULL,
    payload TEXT NOT NULL,lineage TEXT NOT NULL,state TEXT NOT NULL,detail TEXT,created REAL NOT NULL,run TEXT);
CREATE INDEX IF NOT EXISTS deliveries_ready ON deliveries(state,created);
CREATE TABLE IF NOT EXISTS callback_messages(
    id TEXT PRIMARY KEY,run TEXT NOT NULL,node TEXT NOT NULL,content TEXT NOT NULL,created REAL NOT NULL);

CREATE TABLE IF NOT EXISTS poll_seen(
    trigger_id TEXT NOT NULL REFERENCES triggers, item TEXT NOT NULL, PRIMARY KEY(trigger_id,item));
CREATE TABLE IF NOT EXISTS poll_state(
    trigger_id TEXT PRIMARY KEY REFERENCES triggers, initialized INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0);
