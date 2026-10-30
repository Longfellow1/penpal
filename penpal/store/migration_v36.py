"""Index both memory summaries and their optional details."""
import sqlite3


def migrate_v36(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    if "detail" not in columns:
        conn.execute("ALTER TABLE memories ADD COLUMN detail TEXT")
    for trigger in ("memories_fts_ai", "memories_fts_ad", "memories_fts_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("DROP TABLE IF EXISTS memories_fts")
    conn.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(content, detail, content='memories', content_rowid='id', tokenize='trigram')")
    conn.execute("""CREATE TRIGGER memories_fts_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content, detail) VALUES (new.id, new.content, new.detail); END""")
    conn.execute("""CREATE TRIGGER memories_fts_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, detail) VALUES ('delete', old.id, old.content, old.detail); END""")
    conn.execute("""CREATE TRIGGER memories_fts_au AFTER UPDATE OF content, detail ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, detail) VALUES ('delete', old.id, old.content, old.detail);
        INSERT INTO memories_fts(rowid, content, detail) VALUES (new.id, new.content, new.detail); END""")
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
