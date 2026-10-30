"""Memory storage and full-text index."""
import sqlite3


def migrate_v35(conn: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY, type TEXT NOT NULL, content TEXT NOT NULL,
            scope TEXT NOT NULL, project TEXT, source_tool TEXT NOT NULL,
            evidence_ids TEXT NOT NULL, buckets TEXT NOT NULL, content_hash TEXT NOT NULL,
            valid_from TEXT NOT NULL, superseded_by INTEGER REFERENCES memories(id),
            status TEXT NOT NULL, created_at TEXT NOT NULL, confidence REAL NOT NULL,
            source TEXT NOT NULL, external_id TEXT, UNIQUE(source, external_id))""",
        "CREATE INDEX IF NOT EXISTS idx_memories_hash ON memories(content_hash)",
        "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, content='memories', content_rowid='id', tokenize='trigram')",
        """CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
           INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END""",
        """CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
           INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content); END""",
        """CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE OF content ON memories BEGIN
           INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
           INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END""",
    )
    for statement in statements:
        conn.execute(statement)
