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


# Independent blind passes replace the anchored extract-then-verify flow, and
# status is now decided by program checks. The model's self-reported confidence
# is renamed to say plainly that it is a model claim rather than a measurement.
#
# The two anchored-flow columns are dropped rather than reinterpreted: their
# contents came from a verifier that had already seen the proposal, so carrying
# them forward under new names would misrepresent them. The raw model outputs
# they held remain in analysis_runs.output_json.
#
# Any thread previously marked verified was gated on a model-reported field, so
# it is marked stale and will be analyzed again instead of being trusted.
INDEPENDENT_PASSES = """
ALTER TABLE thread_analyses RENAME COLUMN factual_confidence
    TO model_reported_confidence;
ALTER TABLE thread_analyses DROP COLUMN extraction_json;
ALTER TABLE thread_analyses DROP COLUMN verification_json;
ALTER TABLE thread_analyses ADD COLUMN proposals_json TEXT;
ALTER TABLE thread_analyses ADD COLUMN agreement_score REAL;
ALTER TABLE thread_analyses ADD COLUMN gap_reasons_json TEXT;
ALTER TABLE thread_analyses ADD COLUMN segment_count INTEGER;

UPDATE thread_analyses
SET status = 'stale'
WHERE status IN ('verified', 'verified_with_gaps');

ALTER TABLE analysis_runs ADD COLUMN pass_label TEXT;
ALTER TABLE analysis_runs ADD COLUMN evidence_claims_checked INTEGER;
ALTER TABLE analysis_runs ADD COLUMN evidence_claims_valid INTEGER;
"""


# Claims and cases become first-class rows instead of staying inside a JSON
# blob, so they can be retrieved, filtered by status, and ranked individually.
# Everything here is a derived index: it is dropped and rebuilt from
# thread_analyses, never edited in place.
RETRIEVAL = """
CREATE TABLE IF NOT EXISTS claims (
    claim_uid TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    claim_type TEXT NOT NULL,
    text TEXT NOT NULL,
    message_ids_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    thread_status TEXT NOT NULL,
    importance_score INTEGER,
    occurred_at TEXT,
    indexed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claims_thread ON claims(thread_id);
CREATE INDEX IF NOT EXISTS idx_claims_type ON claims(claim_type);

CREATE TABLE IF NOT EXISTS cases (
    thread_id TEXT PRIMARY KEY,
    thread_status TEXT NOT NULL,
    category TEXT,
    importance_score INTEGER,
    subject TEXT NOT NULL,
    summary TEXT NOT NULL,
    situation TEXT NOT NULL,
    actions TEXT NOT NULL,
    outcome TEXT NOT NULL,
    outcome_state TEXT NOT NULL,
    participants_json TEXT NOT NULL,
    gap_reasons_json TEXT NOT NULL,
    agreement_score REAL,
    started_at TEXT,
    ended_at TEXT,
    indexed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(thread_status);

CREATE TABLE IF NOT EXISTS identifiers (
    value TEXT NOT NULL,
    kind TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    claim_uid TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (value, thread_id, claim_uid)
);

CREATE INDEX IF NOT EXISTS idx_identifiers_value ON identifiers(value);

CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(
    claim_uid UNINDEXED,
    thread_id UNINDEXED,
    body,
    tokenize = 'unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS cases_fts USING fts5(
    thread_id UNINDEXED,
    situation,
    actions,
    outcome,
    summary,
    tokenize = 'unicode61'
);
"""


# Feedback is user data, not a derived index, so it lives in its own table and
# is joined at query time. Rebuilding the retrieval index never discards it.
#
# Rows are append-only: a later verdict supersedes an earlier one through
# current_feedback rather than overwriting it, so a change of mind stays legible.
FEEDBACK = """
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feedback_target
    ON feedback(target_kind, target_id);

CREATE VIEW IF NOT EXISTS current_feedback AS
SELECT target_kind, target_id, verdict, note, created_at
FROM feedback AS outer_feedback
WHERE outer_feedback.id = (
    SELECT MAX(id)
    FROM feedback AS inner_feedback
    WHERE inner_feedback.target_kind = outer_feedback.target_kind
      AND inner_feedback.target_id = outer_feedback.target_id
);
"""


# A rule states what to do, not when it applies, so matching a description of
# the current situation against the rule's own wording finds nothing. Indexing
# each claim alongside the situation of the case it came from makes rules
# reachable by the circumstances they were formed under.
#
# A virtual table cannot be altered, so the index is dropped and recreated. It
# is derived, and 'index' rebuilds it.
CLAIM_CONTEXT = """
DROP TABLE IF EXISTS claims_fts;

CREATE VIRTUAL TABLE claims_fts USING fts5(
    claim_uid UNINDEXED,
    thread_id UNINDEXED,
    body,
    context,
    tokenize = 'unicode61'
);
"""


# Repeated cases must collapse into one rule with many instances rather than
# into many near-identical knowledge cards. A candidate rule therefore records
# which cases support it and which contradict it, and how many of those cases
# actually had a recorded outcome, so a pattern nobody ever confirmed cannot
# pass for a proven rule.
INDUCTION = """
CREATE TABLE IF NOT EXISTS rule_candidates (
    rule_id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    title TEXT NOT NULL,
    situation TEXT NOT NULL,
    trigger_text TEXT,
    actions_json TEXT NOT NULL,
    rationale TEXT,
    exceptions_json TEXT NOT NULL,
    failure_conditions_json TEXT NOT NULL,
    supporting_threads_json TEXT NOT NULL,
    contradicting_threads_json TEXT NOT NULL,
    supporting_cases INTEGER NOT NULL,
    cases_with_confirmed_outcome INTEGER NOT NULL,
    model TEXT,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rule_candidates_cluster
    ON rule_candidates(cluster_id);
"""


MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "baseline", BASELINE),
    (2, "independent_passes", INDEPENDENT_PASSES),
    (3, "retrieval", RETRIEVAL),
    (4, "feedback", FEEDBACK),
    (5, "claim_context", CLAIM_CONTEXT),
    (6, "induction", INDUCTION),
)

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
