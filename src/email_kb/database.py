from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .migrations import SCHEMA_VERSION, apply_migrations, current_version

MESSAGE_COLUMNS = (
    "email_id",
    "internet_message_id",
    "conversation_id",
    "parent_folder_id",
    "received_at_utc",
    "sent_at_utc",
    "sender_name",
    "sender_address",
    "to_recipients_json",
    "cc_recipients_json",
    "bcc_recipients_json",
    "reply_to_json",
    "subject",
    "body",
    "clean_body",
    "body_type",
    "body_preview",
    "importance",
    "has_attachments",
    "is_read",
    "categories_json",
    "web_link",
    "body_sha256",
    "raw_json",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    database_path = Path(path).expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.executescript("PRAGMA journal_mode = WAL;")
    before = current_version(connection)
    applied = apply_migrations(connection)
    return {
        "schema_version": SCHEMA_VERSION,
        "previous_version": before,
        "applied_migrations": applied,
    }


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_is_current(
    connection: sqlite3.Connection, path: str | Path, digest: str
) -> bool:
    row = connection.execute(
        "SELECT sha256, status FROM source_files WHERE path = ?",
        (str(Path(path).resolve()),),
    ).fetchone()
    return bool(row and row["sha256"] == digest and row["status"] == "imported")


def begin_source(
    connection: sqlite3.Connection,
    path: str | Path,
    digest: str,
    kind: str,
) -> int:
    source_path = Path(path).resolve()
    stat = source_path.stat()
    connection.execute(
        """
        INSERT INTO source_files (
            path, sha256, kind, size_bytes, modified_ns, status,
            message_count, error, imported_at
        )
        VALUES (?, ?, ?, ?, ?, 'importing', 0, NULL, NULL)
        ON CONFLICT(path) DO UPDATE SET
            sha256 = excluded.sha256,
            kind = excluded.kind,
            size_bytes = excluded.size_bytes,
            modified_ns = excluded.modified_ns,
            status = 'importing',
            message_count = 0,
            error = NULL,
            imported_at = NULL
        """,
        (
            str(source_path),
            digest,
            kind,
            stat.st_size,
            stat.st_mtime_ns,
        ),
    )
    row = connection.execute(
        "SELECT id FROM source_files WHERE path = ?", (str(source_path),)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Unable to register source file: {source_path}")
    source_id = int(row["id"])
    connection.commit()
    return source_id


def finish_source(
    connection: sqlite3.Connection,
    source_id: int,
    *,
    message_count: int,
    error: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE source_files
        SET status = ?, message_count = ?, error = ?, imported_at = ?
        WHERE id = ?
        """,
        (
            "failed" if error else "imported",
            message_count,
            error,
            utc_now(),
            source_id,
        ),
    )
    connection.commit()


def upsert_messages(
    connection: sqlite3.Connection,
    source_id: int,
    messages: Iterable[Mapping[str, Any]],
) -> int:
    message_list = list(messages)
    placeholders = ", ".join("?" for _ in MESSAGE_COLUMNS)
    updates = ", ".join(
        f"{column} = excluded.{column}"
        for column in MESSAGE_COLUMNS
        if column != "email_id"
    )
    insert_sql = f"""
        INSERT INTO messages ({", ".join(MESSAGE_COLUMNS)})
        VALUES ({placeholders})
        ON CONFLICT(email_id) DO UPDATE SET {updates}
    """
    insert_without_update_sql = f"""
        INSERT INTO messages ({", ".join(MESSAGE_COLUMNS)})
        VALUES ({placeholders})
        ON CONFLICT(email_id) DO NOTHING
    """

    with connection:
        source_row = connection.execute(
            "SELECT kind FROM source_files WHERE id = ?", (source_id,)
        ).fetchone()
        if source_row is None:
            raise RuntimeError(f"Unknown source id: {source_id}")
        incoming_kind = str(source_row["kind"])
        existing_ids = {
            str(row["email_id"])
            for row in connection.execute(
                "SELECT email_id FROM message_sources WHERE source_file_id = ?",
                (source_id,),
            )
        }
        current_ids = {str(message["email_id"]) for message in message_list}
        stale_ids = existing_ids - current_ids

        for source_index, message in enumerate(message_list):
            values = [message[column] for column in MESSAGE_COLUMNS]
            existing_json_source = connection.execute(
                """
                SELECT 1
                FROM message_sources
                JOIN source_files
                  ON source_files.id = message_sources.source_file_id
                WHERE message_sources.email_id = ?
                  AND source_files.kind = 'json'
                LIMIT 1
                """,
                (message["email_id"],),
            ).fetchone()
            statement = (
                insert_without_update_sql
                if incoming_kind == "csv" and existing_json_source
                else insert_sql
            )
            connection.execute(statement, values)
            connection.execute(
                """
                INSERT INTO message_sources (email_id, source_file_id, source_index)
                VALUES (?, ?, ?)
                ON CONFLICT(email_id, source_file_id) DO UPDATE SET
                    source_index = excluded.source_index
                """,
                (message["email_id"], source_id, source_index),
            )
        connection.executemany(
            """
            DELETE FROM message_sources
            WHERE email_id = ? AND source_file_id = ?
            """,
            ((email_id, source_id) for email_id in stale_ids),
        )
        connection.execute(
            """
            DELETE FROM messages
            WHERE NOT EXISTS (
                SELECT 1
                FROM message_sources
                WHERE message_sources.email_id = messages.email_id
            )
            """
        )
    return len(message_list)


def database_stats(connection: sqlite3.Connection) -> dict[str, int]:
    messages = int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    threads = int(
        connection.execute(
            "SELECT COUNT(DISTINCT conversation_id) FROM messages"
        ).fetchone()[0]
    )
    sources = int(
        connection.execute(
            "SELECT COUNT(*) FROM source_files WHERE status = 'imported'"
        ).fetchone()[0]
    )
    analyses = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM thread_analyses
            WHERE status IN ('verified', 'verified_with_gaps')
            """
        ).fetchone()[0]
    )
    return {
        "messages": messages,
        "threads": threads,
        "sources": sources,
        "verified_threads": analyses,
    }


def quality_report(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT status, importance_score, factual_confidence, verified_json
        FROM thread_analyses
        """
    ).fetchall()
    statuses: dict[str, int] = {}
    checked_claims = 0
    valid_claims = 0
    confidences: list[float] = []
    high_importance = 0

    for row in rows:
        status = str(row["status"])
        statuses[status] = statuses.get(status, 0) + 1
        if row["factual_confidence"] is not None:
            confidences.append(float(row["factual_confidence"]))
        if row["importance_score"] is not None and int(row["importance_score"]) >= 80:
            high_importance += 1
        try:
            verified = json.loads(row["verified_json"] or "{}")
        except json.JSONDecodeError:
            verified = {}
        validation = verified.get("validation", {})
        if isinstance(validation, Mapping):
            checked_claims += int(validation.get("evidence_claims_checked") or 0)
            valid_claims += int(validation.get("evidence_claims_valid") or 0)

    failed_runs = int(
        connection.execute(
            "SELECT COUNT(*) FROM analysis_runs WHERE status = 'failed'"
        ).fetchone()[0]
    )
    return {
        "analyzed_threads": len(rows),
        "status_counts": statuses,
        "high_importance_threads": high_importance,
        "evidence_claims_checked": checked_claims,
        "evidence_claims_valid": valid_claims,
        "evidence_pass_rate": (
            round(valid_claims / checked_claims, 4) if checked_claims else None
        ),
        "average_verifier_confidence": (
            round(sum(confidences) / len(confidences), 4) if confidences else None
        ),
        "failed_agent_runs": failed_runs,
    }


def iter_thread_ids(
    connection: sqlite3.Connection,
    *,
    owner_email: str | None = None,
    limit: int | None = None,
) -> Iterator[str]:
    owner_priority = ""
    parameters: list[Any] = []
    if owner_email:
        owner_priority = """
          MAX(
            CASE
              WHEN LOWER(COALESCE(m.sender_address, '')) = LOWER(?) THEN 1
              ELSE 0
            END
          ) DESC,
          MAX(
            CASE
              WHEN INSTR(LOWER(m.to_recipients_json), LOWER(?)) > 0 THEN 1
              ELSE 0
            END
          ) DESC,
        """
        parameters.extend([owner_email, owner_email])

    sql = """
        SELECT m.conversation_id
        FROM messages AS m
        LEFT JOIN thread_analyses AS a
          ON a.thread_id = m.conversation_id
        GROUP BY m.conversation_id
        ORDER BY
          CASE WHEN a.thread_id IS NULL THEN 0 ELSE 1 END,
    """
    sql += owner_priority
    sql += """
          COUNT(*) DESC,
          MAX(m.has_attachments) DESC,
          MAX(COALESCE(m.received_at_utc, m.sent_at_utc, '')) DESC
    """
    if limit is not None:
        sql += " LIMIT ?"
        parameters.append(limit)
    for row in connection.execute(sql, parameters):
        yield str(row["conversation_id"])


def load_thread(connection: sqlite3.Connection, thread_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT *
        FROM messages
        WHERE conversation_id = ?
        ORDER BY
          COALESCE(sent_at_utc, received_at_utc, ''),
          email_id
        """,
        (thread_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_thread_analysis(
    connection: sqlite3.Connection, thread_id: str
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM thread_analyses WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    return dict(row) if row else None


def record_run(
    connection: sqlite3.Connection,
    *,
    batch_id: str,
    phase: str,
    status: str,
    thread_ids: list[str],
    input_sha256: str,
    agent_id: str | None = None,
    run_id: str | None = None,
    model: str | None = None,
    output: Any = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO analysis_runs (
            batch_id, phase, status, thread_ids_json, input_sha256,
            agent_id, run_id, model, output_json, error, duration_ms, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            phase,
            status,
            json.dumps(thread_ids, ensure_ascii=False),
            input_sha256,
            agent_id,
            run_id,
            model,
            (json.dumps(output, ensure_ascii=False) if output is not None else None),
            error,
            duration_ms,
            utc_now(),
        ),
    )
    connection.commit()


def save_thread_analysis(
    connection: sqlite3.Connection,
    *,
    thread_id: str,
    source_fingerprint: str,
    status: str,
    importance_score: int | None,
    factual_confidence: float | None,
    extraction: Any,
    verification: Any,
    verified: Any,
) -> None:
    connection.execute(
        """
        INSERT INTO thread_analyses (
            thread_id, source_fingerprint, status, importance_score,
            factual_confidence, extraction_json, verification_json,
            verified_json, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            source_fingerprint = excluded.source_fingerprint,
            status = excluded.status,
            importance_score = excluded.importance_score,
            factual_confidence = excluded.factual_confidence,
            extraction_json = excluded.extraction_json,
            verification_json = excluded.verification_json,
            verified_json = excluded.verified_json,
            updated_at = excluded.updated_at
        """,
        (
            thread_id,
            source_fingerprint,
            status,
            importance_score,
            factual_confidence,
            json.dumps(extraction, ensure_ascii=False) if extraction else None,
            json.dumps(verification, ensure_ascii=False) if verification else None,
            json.dumps(verified, ensure_ascii=False) if verified else None,
            utc_now(),
        ),
    )
    connection.commit()
