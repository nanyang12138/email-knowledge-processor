"""Ordered, versioned schema migrations.

A long-lived knowledge database must be able to state which schema it is on
before anything tries to read it. Every schema change is appended here as a new
numbered migration; nothing mutates an earlier one.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

BASELINE = """
CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    modified_ns INTEGER NOT NULL,
    status TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    imported_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    email_id TEXT PRIMARY KEY,
    internet_message_id TEXT,
    conversation_id TEXT NOT NULL,
    parent_folder_id TEXT,
    received_at_utc TEXT,
    sent_at_utc TEXT,
    sender_name TEXT,
    sender_address TEXT,
    to_recipients_json TEXT NOT NULL,
    cc_recipients_json TEXT NOT NULL,
    bcc_recipients_json TEXT NOT NULL,
    reply_to_json TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    clean_body TEXT NOT NULL,
    body_type TEXT,
    body_preview TEXT,
    importance TEXT,
    has_attachments INTEGER NOT NULL,
    is_read INTEGER NOT NULL,
    categories_json TEXT NOT NULL,
    web_link TEXT,
    body_sha256 TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_received
    ON messages(received_at_utc);
CREATE INDEX IF NOT EXISTS idx_messages_sender
    ON messages(sender_address);

CREATE TABLE IF NOT EXISTS message_sources (
    email_id TEXT NOT NULL REFERENCES messages(email_id) ON DELETE CASCADE,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    source_index INTEGER NOT NULL,
    PRIMARY KEY (email_id, source_file_id)
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY,
    batch_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    thread_ids_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    agent_id TEXT,
    run_id TEXT,
    model TEXT,
    output_json TEXT,
    error TEXT,
    duration_ms INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analysis_runs_batch
    ON analysis_runs(batch_id, phase);

CREATE TABLE IF NOT EXISTS thread_analyses (
    thread_id TEXT PRIMARY KEY,
    source_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    importance_score INTEGER,
    factual_confidence REAL,
    extraction_json TEXT,
    verification_json TEXT,
    verified_json TEXT,
    updated_at TEXT NOT NULL
);
"""


MIGRATIONS: tuple[tuple[int, str, str], ...] = ((1, "baseline", BASELINE),)

SCHEMA_VERSION = MIGRATIONS[-1][0]


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def current_version(connection: sqlite3.Connection) -> int:
    if not _table_exists(connection, "schema_version"):
        # Databases created before versioning existed already carry the
        # baseline tables; adopt them instead of replaying migration 1.
        return 1 if _table_exists(connection, "messages") else 0
    row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM schema_version"
    ).fetchone()
    return int(row["version"] if isinstance(row, sqlite3.Row) else row[0])


def apply_migrations(connection: sqlite3.Connection) -> list[int]:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        """
    )
    version = current_version(connection)
    applied: list[int] = []
    for number, name, script in MIGRATIONS:
        if number <= version:
            continue
        connection.executescript(script)
        connection.execute(
            "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
            (number, name, datetime.now(UTC).isoformat()),
        )
        connection.commit()
        applied.append(number)

    if not applied and version >= 1:
        connection.execute(
            """
            INSERT OR IGNORE INTO schema_version (version, name, applied_at)
            VALUES (?, ?, ?)
            """,
            (version, "adopted", datetime.now(UTC).isoformat()),
        )
        connection.commit()
    return applied
