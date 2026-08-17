"""
User feedback on extracted knowledge.

Importance ranking and personal relevance cannot be calibrated from the email
alone; they need signal only the owner can give. Collecting it has to start
early, because the value of labelled data comes from how long it has been
accumulating.

Feedback is deliberately asymmetric. `useful` and `not_useful` are opinions
about ranking and never change what a claim says. `wrong` and `outdated` are
explicit statements from the owner, so they change what an agent is allowed to
see — but even then the claim and its evidence are left intact, because the
record of what an email said is not the owner's to rewrite.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .database import utc_now

RANKING_VERDICTS = ("useful", "not_useful")
VISIBILITY_VERDICTS = ("wrong", "outdated")
VERDICTS = RANKING_VERDICTS + VISIBILITY_VERDICTS

VERDICT_HELP = {
    "useful": "Worth surfacing again. Affects ranking only.",
    "not_useful": "Not worth surfacing. Affects ranking only.",
    "wrong": "The claim misreads the source. Hidden from agents.",
    "outdated": "It was true once but no longer applies. Kept, but flagged.",
}


# Checked in order, so an id is resolved by where it exists rather than by a
# guess at its shape. Rule ids and claim uids both contain a colon.
TARGET_TABLES = (
    ("rule", "rule_candidates", "rule_id"),
    ("claim", "claims", "claim_uid"),
    ("case", "cases", "thread_id"),
)


def resolve_target(connection: sqlite3.Connection, target_id: str) -> str:
    for kind, table, column in TARGET_TABLES:
        found = connection.execute(
            f"SELECT 1 FROM {table} WHERE {column} = ?", (target_id,)
        ).fetchone()
        if found is not None:
            return kind
    raise ValueError(
        f"No rule, claim, or case with id {target_id!r}. Run 'index' or "
        "'induce' first, or check the id."
    )


def record_feedback(
    connection: sqlite3.Connection,
    *,
    target_id: str,
    verdict: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Append one verdict for a rule, a claim, or a case."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of: {', '.join(VERDICTS)}")
    target_kind = resolve_target(connection, target_id)

    connection.execute(
        """
        INSERT INTO feedback (target_kind, target_id, verdict, note, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (target_kind, target_id, verdict, note, utc_now()),
    )
    connection.commit()
    if target_kind == "rule":
        effect = (
            "accepted_as_stated_experience"
            if verdict == "useful"
            else "not_stated_experience"
        )
    elif verdict in RANKING_VERDICTS:
        effect = "ranking_only"
    elif verdict == "wrong":
        effect = "hidden_from_agents"
    else:
        effect = "flagged_as_outdated"
    return {
        "target_kind": target_kind,
        "target_id": target_id,
        "verdict": verdict,
        "effect": effect,
    }


def review_queue(
    connection: sqlite3.Connection, *, limit: int = 20
) -> list[dict[str, Any]]:
    """
    Knowledge that has never been reviewed, most consequential first.

    Each item carries the source quote, because deciding whether a claim is
    wrong means comparing it against what the email actually said.
    """
    rows = connection.execute(
        """
        SELECT claims.claim_uid, claims.thread_id, claims.claim_type, claims.text,
               claims.evidence_json, claims.occurred_at, claims.importance_score,
               claims.thread_status, cases.subject, cases.outcome_state
        FROM claims
        LEFT JOIN cases ON cases.thread_id = claims.thread_id
        LEFT JOIN current_feedback
          ON current_feedback.target_kind = 'claim'
         AND current_feedback.target_id = claims.claim_uid
        WHERE current_feedback.verdict IS NULL
        ORDER BY claims.importance_score DESC, claims.occurred_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "target_id": row["claim_uid"],
            "thread_id": row["thread_id"],
            "subject": row["subject"],
            "type": row["claim_type"],
            "text": row["text"],
            "evidence": json.loads(row["evidence_json"] or "[]"),
            "occurred_at": row["occurred_at"],
            "importance_score": row["importance_score"],
            "status": row["thread_status"],
            "outcome_state": row["outcome_state"],
            "verdicts": VERDICT_HELP,
        }
        for row in rows
    ]


def feedback_stats(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT target_kind, verdict, COUNT(*) AS total
        FROM current_feedback
        GROUP BY target_kind, verdict
        """
    ).fetchall()
    reviewed = int(
        connection.execute(
            "SELECT COUNT(*) FROM current_feedback WHERE target_kind = 'claim'"
        ).fetchone()[0]
    )
    total_claims = int(connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0])
    return {
        "claims_reviewed": reviewed,
        "claims_unreviewed": max(total_claims - reviewed, 0),
        "verdicts": {
            f"{row['target_kind']}.{row['verdict']}": int(row["total"]) for row in rows
        },
    }
