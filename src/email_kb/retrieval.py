"""
Case-based retrieval over validated knowledge.

An agent does not ask "which text is similar to this text". It asks "I am in
situation S, what did this person do before, and did it work". So cases are
indexed by their *situation* rather than by the whole thread, and every result
carries the status, the known gaps, the outcome state, and the evidence needed
to check it. Callers cannot get a bare snippet with no provenance.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .database import utc_now

# Situation-shaped claims describe the problem an agent would match against.
# Action-shaped claims describe what was done, outcome-shaped what happened.
SITUATION_TYPES = ("problem", "situation", "constraint", "goal")
ACTION_TYPES = ("action", "decision")
OUTCOME_TYPES = ("outcome",)
RULE_TYPES = ("reusable_rule", "exception", "preference")

AGENT_VISIBLE_STATUSES = ("verified", "verified_with_gaps")

_CJK = (
    "\u3040-\u30ff"  # kana
    "\u3400-\u4dbf"  # CJK extension A
    "\u4e00-\u9fff"  # CJK unified ideographs
    "\uf900-\ufaff"  # compatibility ideographs
)
_CJK_RUN = re.compile(f"[{_CJK}]+")
_LATIN_TOKEN = re.compile(r"[0-9A-Za-z_][0-9A-Za-z_.\-/#]*")

_IDENTIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cl", re.compile(r"\bcl[\s/#-]?(\d{4,})\b", re.IGNORECASE)),
    ("bug", re.compile(r"\b(?:bug|issue|ticket)[\s/#-]?(\d{3,})\b", re.IGNORECASE)),
    ("build", re.compile(r"\bbuild[\s/#-]?([0-9][0-9.\-]{2,})\b", re.IGNORECASE)),
    ("tracker", re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")),
    ("commit", re.compile(r"\b([0-9a-f]{7,40})\b")),
)


def search_tokens(value: str) -> list[str]:
    """
    Tokenize for FTS5 in a way that works for Chinese and Latin text alike.

    FTS5's unicode61 tokenizer treats a whole run of CJK characters as one
    token, so a Chinese query only matches when it repeats an entire run
    verbatim. Expanding CJK runs into overlapping bigrams restores partial
    matching without depending on an external tokenizer being installed.
    """
    tokens: list[str] = []
    position = 0
    for match in _CJK_RUN.finditer(value):
        tokens.extend(
            token.casefold()
            for token in _LATIN_TOKEN.findall(value[position : match.start()])
        )
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
        position = match.end()
    tokens.extend(token.casefold() for token in _LATIN_TOKEN.findall(value[position:]))
    return [token for token in tokens if token]


def index_text(value: str) -> str:
    return " ".join(search_tokens(value))


def _match_expression(query: str) -> str | None:
    """Build an FTS5 MATCH expression from untrusted text."""
    tokens = search_tokens(query)
    if not tokens:
        return None
    quoted = [
        '"{}"'.format(token.replace('"', '""')) for token in dict.fromkeys(tokens)
    ]
    return " OR ".join(quoted)


def extract_identifiers(value: str) -> list[dict[str, str]]:
    """
    Pull exact identifiers out of free text.

    These are matched exactly at query time so a CL or ticket number is never
    drowned out by semantically similar prose. The patterns are heuristic; a
    false positive costs a useless index row, not a wrong answer.
    """
    found: dict[tuple[str, str], dict[str, str]] = {}
    for kind, pattern in _IDENTIFIER_PATTERNS:
        for match in pattern.finditer(value):
            raw = match.group(1)
            if kind == "commit" and raw.isdigit():
                continue
            normalized = f"{kind}:{raw.casefold()}"
            found[(kind, normalized)] = {"kind": kind, "value": normalized, "raw": raw}
    return list(found.values())


def _claim_texts(claims: Iterable[Mapping[str, Any]], types: Sequence[str]) -> str:
    wanted = set(types)
    return "\n".join(
        str(claim.get("text") or "")
        for claim in claims
        if str(claim.get("type") or "") in wanted
    )


def _message_times(connection: sqlite3.Connection, thread_id: str) -> dict[str, str]:
    rows = connection.execute(
        """
        SELECT email_id, COALESCE(sent_at_utc, received_at_utc) AS at
        FROM messages
        WHERE conversation_id = ?
        """,
        (thread_id,),
    ).fetchall()
    return {str(row["email_id"]): str(row["at"] or "") for row in rows}


def clear_index(connection: sqlite3.Connection) -> None:
    for table in ("claims", "cases", "identifiers", "claims_fts", "cases_fts"):
        connection.execute(f"DELETE FROM {table}")


def index_knowledge(
    connection: sqlite3.Connection,
    *,
    statuses: Sequence[str] = AGENT_VISIBLE_STATUSES,
) -> dict[str, Any]:
    """
    Rebuild the retrieval index from validated analyses.

    Only statuses an agent is allowed to see are indexed, so `partial` and
    `rejected` knowledge cannot reach an agent's context by accident. The index
    is fully derived and is dropped and rebuilt every time.
    """
    placeholders = ", ".join("?" for _ in statuses)
    rows = connection.execute(
        f"""
        SELECT thread_id, status, importance_score, agreement_score,
               gap_reasons_json, verified_json
        FROM thread_analyses
        WHERE status IN ({placeholders})
        """,
        tuple(statuses),
    ).fetchall()

    now = utc_now()
    indexed_cases = 0
    indexed_claims = 0
    indexed_identifiers = 0

    with connection:
        clear_index(connection)
        for row in rows:
            thread_id = str(row["thread_id"])
            try:
                verified = json.loads(row["verified_json"] or "{}")
            except json.JSONDecodeError:
                continue
            claims = [
                claim
                for claim in verified.get("claims", [])
                if isinstance(claim, Mapping)
            ]
            if not claims:
                continue

            times = _message_times(connection, thread_id)
            ordered_times = sorted(value for value in times.values() if value)
            situation = _claim_texts(claims, SITUATION_TYPES)
            actions = _claim_texts(claims, ACTION_TYPES)
            outcome = _claim_texts(claims, OUTCOME_TYPES)
            summary = str(verified.get("summary") or "")
            subject = str(verified.get("subject") or "")

            connection.execute(
                """
                INSERT INTO cases (
                    thread_id, thread_status, category, importance_score, subject,
                    summary, situation, actions, outcome, outcome_state,
                    participants_json, gap_reasons_json, agreement_score,
                    started_at, ended_at, indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    str(row["status"]),
                    verified.get("category"),
                    row["importance_score"],
                    subject,
                    summary,
                    situation,
                    actions,
                    outcome,
                    "confirmed" if outcome.strip() else "unknown",
                    json.dumps(verified.get("participants") or [], ensure_ascii=False),
                    row["gap_reasons_json"] or "[]",
                    row["agreement_score"],
                    ordered_times[0] if ordered_times else None,
                    ordered_times[-1] if ordered_times else None,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO cases_fts (thread_id, situation, actions, outcome, summary)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    index_text(f"{situation}\n{subject}"),
                    index_text(actions),
                    index_text(outcome),
                    index_text(summary),
                ),
            )
            indexed_cases += 1

            identifier_rows: set[tuple[str, str, str, str]] = set()
            for identifier in extract_identifiers(f"{subject}\n{summary}"):
                identifier_rows.add(
                    (identifier["value"], identifier["kind"], thread_id, "")
                )

            for claim in claims:
                claim_id = str(claim.get("claim_id") or "")
                claim_uid = f"{thread_id}:{claim_id}"
                text = str(claim.get("text") or "")
                message_ids = [str(value) for value in claim.get("message_ids") or []]
                claim_times = sorted(
                    times.get(message_id, "") for message_id in message_ids
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO claims (
                        claim_uid, thread_id, claim_id, claim_type, text,
                        message_ids_json, evidence_json, thread_status,
                        importance_score, occurred_at, indexed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim_uid,
                        thread_id,
                        claim_id,
                        str(claim.get("type") or ""),
                        text,
                        json.dumps(message_ids, ensure_ascii=False),
                        json.dumps(
                            claim.get("evidence_quotes") or [], ensure_ascii=False
                        ),
                        str(row["status"]),
                        row["importance_score"],
                        next((value for value in claim_times if value), None),
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO claims_fts (claim_uid, thread_id, body) "
                    "VALUES (?, ?, ?)",
                    (claim_uid, thread_id, index_text(text)),
                )
                indexed_claims += 1
                for identifier in extract_identifiers(text):
                    identifier_rows.add(
                        (identifier["value"], identifier["kind"], thread_id, claim_uid)
                    )

            connection.executemany(
                """
                INSERT OR IGNORE INTO identifiers (value, kind, thread_id, claim_uid)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (value, kind, thread, uid)
                    for value, kind, thread, uid in identifier_rows
                ],
            )
            indexed_identifiers += len(identifier_rows)

    return {
        "cases": indexed_cases,
        "claims": indexed_claims,
        "identifiers": indexed_identifiers,
        "statuses": list(statuses),
    }


def _rank(
    relevance: float,
    *,
    status: str,
    outcome_state: str,
    importance: int | None,
    gap_reasons: Sequence[str],
    agreement: float | None,
) -> dict[str, Any]:
    """
    Explain every ranking factor instead of returning one opaque number.

    A case whose approach is known to have worked outranks one with the same
    text relevance and no recorded result. This is the signal a document
    retriever has no way to express.
    """
    components = {
        "text_relevance": round(relevance, 4),
        "outcome_known": 1.5 if outcome_state == "confirmed" else 0.0,
        "fully_verified": 1.0 if status == "verified" else 0.0,
        "importance": round((importance or 0) / 100, 4),
        "known_gaps": round(-0.25 * len(gap_reasons), 4),
        "pass_agreement": round((agreement if agreement is not None else 0.5) - 0.5, 4),
    }
    return {"score": round(sum(components.values()), 4), "components": components}


STALE_AFTER_DAYS = 730

_GAP_ADVISORIES = {
    "attachment_content_unavailable": (
        "Attachment contents were never imported, so part of this case is "
        "missing. Do not describe what an attachment said."
    ),
    "message_too_large_to_analyze": (
        "At least one message in this thread was too large to analyze and is "
        "not represented here."
    ),
    "model_reported_missing_context": (
        "The extractor reported that necessary context is missing from this thread."
    ),
    "independent_passes_disagreed_on_category": (
        "The two independent passes disagreed about what kind of thread this "
        "is. Check the evidence before relying on the framing."
    ),
    "independent_passes_disagreed_on_importance": (
        "The two independent passes disagreed about how important this is, so "
        "its rank here is unreliable."
    ),
    "independent_passes_cited_different_evidence": (
        "The two independent passes cited largely different evidence, which "
        "means the reading of this thread is unstable."
    ),
}


def _age_days(value: str | None) -> int | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - moment).days


def advisories(
    *,
    status: str,
    gap_reasons: Sequence[str],
    outcome_state: str,
    occurred_at: str | None,
) -> list[str]:
    """
    Limits an agent must not have to infer.

    The seven-step usage contract in the product plan is only a convention:
    nothing stops an agent from taking a snippet and running with it. Attaching
    these to every result puts the constraint in the payload instead.
    """
    notes: list[str] = []
    if outcome_state != "confirmed":
        notes.append(
            "No outcome was recorded for this case. Do not present the approach "
            "as known to have worked."
        )
    for reason in gap_reasons:
        advisory = _GAP_ADVISORIES.get(str(reason))
        if advisory:
            notes.append(advisory)
    if status != "verified":
        notes.append(
            f"This knowledge has status {status!r} rather than 'verified'; "
            "check the cited evidence before acting on it."
        )
    age = _age_days(occurred_at)
    if age is not None and age > STALE_AFTER_DAYS:
        notes.append(
            f"This is about {age // 365} years old. Confirm it still applies "
            "before relying on it."
        )
    return notes


def _case_payload(row: sqlite3.Row, ranking: Mapping[str, Any]) -> dict[str, Any]:
    gap_reasons = json.loads(row["gap_reasons_json"] or "[]")
    return {
        "thread_id": row["thread_id"],
        "subject": row["subject"],
        "summary": row["summary"],
        "situation": row["situation"],
        "actions": row["actions"],
        "outcome": row["outcome"],
        # An agent must be able to tell "this worked" from "nobody wrote down
        # what happened", so the outcome state is never omitted.
        "outcome_state": row["outcome_state"],
        "category": row["category"],
        "importance_score": row["importance_score"],
        "status": row["thread_status"],
        "gap_reasons": gap_reasons,
        "pass_agreement": row["agreement_score"],
        "occurred_between": [row["started_at"], row["ended_at"]],
        "advisories": advisories(
            status=str(row["thread_status"]),
            gap_reasons=gap_reasons,
            outcome_state=str(row["outcome_state"]),
            occurred_at=row["ended_at"],
        ),
        "ranking": ranking,
    }


def find_similar_cases(
    connection: sqlite3.Connection,
    situation: str,
    *,
    limit: int = 5,
    statuses: Sequence[str] = AGENT_VISIBLE_STATUSES,
) -> list[dict[str, Any]]:
    """Find past cases whose *problem* resembles the situation described."""
    expression = _match_expression(situation)
    if not expression:
        return []
    placeholders = ", ".join("?" for _ in statuses)
    rows = connection.execute(
        f"""
        SELECT cases.*, -bm25(cases_fts, 0.0, 10.0, 3.0, 3.0, 2.0) AS relevance
        FROM cases_fts
        JOIN cases ON cases.thread_id = cases_fts.thread_id
        WHERE cases_fts MATCH ?
          AND cases.thread_status IN ({placeholders})
        ORDER BY relevance DESC
        LIMIT ?
        """,
        (expression, *statuses, max(limit * 5, limit)),
    ).fetchall()

    results = [
        _case_payload(
            row,
            _rank(
                float(row["relevance"]),
                status=str(row["thread_status"]),
                outcome_state=str(row["outcome_state"]),
                importance=row["importance_score"],
                gap_reasons=json.loads(row["gap_reasons_json"] or "[]"),
                agreement=row["agreement_score"],
            ),
        )
        for row in rows
    ]
    results.sort(key=lambda item: item["ranking"]["score"], reverse=True)
    return results[:limit]


def _claim_rows(
    connection: sqlite3.Connection,
    query: str,
    types: Sequence[str],
    *,
    limit: int,
    statuses: Sequence[str],
) -> list[sqlite3.Row]:
    expression = _match_expression(query)
    if not expression:
        return []
    type_placeholders = ", ".join("?" for _ in types)
    status_placeholders = ", ".join("?" for _ in statuses)
    return connection.execute(
        f"""
        SELECT claims.*, cases.outcome_state, cases.gap_reasons_json,
               cases.agreement_score, cases.subject,
               -bm25(claims_fts) AS relevance
        FROM claims_fts
        JOIN claims ON claims.claim_uid = claims_fts.claim_uid
        LEFT JOIN cases ON cases.thread_id = claims.thread_id
        WHERE claims_fts MATCH ?
          AND claims.claim_type IN ({type_placeholders})
          AND claims.thread_status IN ({status_placeholders})
        ORDER BY relevance DESC
        LIMIT ?
        """,
        (expression, *types, *statuses, max(limit * 5, limit)),
    ).fetchall()


def _claim_payload(row: sqlite3.Row, ranking: Mapping[str, Any]) -> dict[str, Any]:
    gap_reasons = json.loads(row["gap_reasons_json"] or "[]")
    outcome_state = str(row["outcome_state"] or "unknown")
    return {
        "claim_uid": row["claim_uid"],
        "thread_id": row["thread_id"],
        "type": row["claim_type"],
        "text": row["text"],
        "subject": row["subject"],
        "occurred_at": row["occurred_at"],
        "status": row["thread_status"],
        "outcome_state": outcome_state,
        "gap_reasons": gap_reasons,
        "evidence": json.loads(row["evidence_json"] or "[]"),
        "advisories": advisories(
            status=str(row["thread_status"]),
            gap_reasons=gap_reasons,
            outcome_state=outcome_state,
            occurred_at=row["occurred_at"],
        ),
        "ranking": ranking,
    }


def _ranked_claims(rows: Sequence[sqlite3.Row], *, limit: int) -> list[dict[str, Any]]:
    results = [
        _claim_payload(
            row,
            _rank(
                float(row["relevance"]),
                status=str(row["thread_status"]),
                outcome_state=str(row["outcome_state"] or "unknown"),
                importance=row["importance_score"],
                gap_reasons=json.loads(row["gap_reasons_json"] or "[]"),
                agreement=row["agreement_score"],
            ),
        )
        for row in rows
    ]
    results.sort(key=lambda item: item["ranking"]["score"], reverse=True)
    return results[:limit]


def get_applicable_rules(
    connection: sqlite3.Connection,
    context: str,
    *,
    limit: int = 5,
    statuses: Sequence[str] = AGENT_VISIBLE_STATUSES,
) -> list[dict[str, Any]]:
    """Reusable rules, exceptions, and preferences that may apply right now."""
    rows = _claim_rows(connection, context, RULE_TYPES, limit=limit, statuses=statuses)
    return _ranked_claims(rows, limit=limit)


def check_prior_attempts(
    connection: sqlite3.Connection,
    approach: str,
    *,
    limit: int = 5,
    statuses: Sequence[str] = AGENT_VISIBLE_STATUSES,
) -> list[dict[str, Any]]:
    """Past actions and decisions resembling an approach under consideration."""
    rows = _claim_rows(
        connection, approach, ACTION_TYPES, limit=limit, statuses=statuses
    )
    return _ranked_claims(rows, limit=limit)


def lookup_identifier(
    connection: sqlite3.Connection, identifier: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Exact identifier lookup, kept separate so prose cannot outrank it."""
    wanted = extract_identifiers(identifier)
    values = [item["value"] for item in wanted] or [
        f"{kind}:{identifier.strip().casefold()}" for kind, _ in _IDENTIFIER_PATTERNS
    ]
    placeholders = ", ".join("?" for _ in values)
    rows = connection.execute(
        f"""
        SELECT DISTINCT identifiers.value, identifiers.kind, cases.*
        FROM identifiers
        JOIN cases ON cases.thread_id = identifiers.thread_id
        WHERE identifiers.value IN ({placeholders})
        ORDER BY cases.ended_at DESC
        LIMIT ?
        """,
        (*values, limit),
    ).fetchall()
    return [
        {
            "identifier": row["value"],
            "kind": row["kind"],
            **_case_payload(row, {"score": None, "components": {"exact_match": True}}),
        }
        for row in rows
    ]


def get_case(connection: sqlite3.Connection, thread_id: str) -> dict[str, Any] | None:
    """One case with every validated claim and its evidence."""
    row = connection.execute(
        "SELECT * FROM cases WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    if row is None:
        return None
    claims = connection.execute(
        "SELECT * FROM claims WHERE thread_id = ? ORDER BY occurred_at, claim_id",
        (thread_id,),
    ).fetchall()
    payload = _case_payload(row, {"score": None, "components": {}})
    payload["participants"] = json.loads(row["participants_json"] or "[]")
    payload["claims"] = [
        {
            "claim_uid": claim["claim_uid"],
            "type": claim["claim_type"],
            "text": claim["text"],
            "occurred_at": claim["occurred_at"],
            "message_ids": json.loads(claim["message_ids_json"] or "[]"),
            "evidence": json.loads(claim["evidence_json"] or "[]"),
        }
        for claim in claims
    ]
    return payload


def index_stats(connection: sqlite3.Connection) -> dict[str, Any]:
    def count(table: str) -> int:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    outcomes = connection.execute(
        "SELECT outcome_state, COUNT(*) AS total FROM cases GROUP BY outcome_state"
    ).fetchall()
    return {
        "cases": count("cases"),
        "claims": count("claims"),
        "identifiers": count("identifiers"),
        "cases_by_outcome_state": {
            str(row["outcome_state"]): int(row["total"]) for row in outcomes
        },
    }
