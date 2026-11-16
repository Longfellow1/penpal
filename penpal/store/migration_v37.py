"""Record user confirmation of shared memories."""
import sqlite3


def migrate_v37(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    if "confirmed_at" not in columns:
        conn.execute("ALTER TABLE memories ADD COLUMN confirmed_at TEXT")
        conn.execute("UPDATE memories SET confirmed_at = created_at WHERE source != 'host'")
