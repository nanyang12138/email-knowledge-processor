"""
Measuring whether the knowledge base changes what an agent does.

Every other metric in this project measures faithfulness: whether a quote
exists, whether a claim resolves to source text, whether two passes agreed.
All of them can read perfectly while the system remains useless, because a
claim can be exactly grounded and still be a restatement of nothing.

This module measures the only thing that corresponds to the product goal: the
difference between an agent working alone and the same agent with access to
this knowledge, on problems whose real outcome is already known.

Two inputs are needed, and only the owner can write them:

- Experience cards: rules the owner knows they hold, written by hand. They turn
  extraction from "summarize this thread" into "rebuild these from the raw
  email", which is a task with an answer.
- Replay tasks: past problems, described as they looked at the time, together
  with what actually happened. Knowledge dated after the cutoff is withheld, so
  the answer cannot leak back into the question.
"""

from __future__ import annotations

import json
import sqlite3
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .retrieval import (
    check_prior_attempts,
    find_similar_cases,
    get_applicable_rules,
    search_tokens,
)

CARD_REQUIRED = ("id", "title", "situation", "actions")
CARD_OPTIONAL = (
    "trigger",
    "rationale",
    "exceptions",
    "failure_conditions",
    "counterexamples",
)
TASK_REQUIRED = ("id", "situation", "asked_at", "expected_elements")
TASK_OPTIONAL = (
    "what_actually_happened",
    "what_worked",
    "notes",
    "source_thread_id",
)

ELEMENT_COVERAGE_THRESHOLD = 0.7


class EvaluationFileError(ValueError):
    """Raised when a hand-written evaluation file is malformed."""


def _load_table(path: str | Path, key: str) -> list[dict[str, Any]]:
    source = Path(path).expanduser()
    if not source.exists():
        raise EvaluationFileError(f"{source} does not exist")
    with source.open("rb") as handle:
        document = tomllib.load(handle)
    entries = document.get(key)
    if not isinstance(entries, list) or not entries:
        raise EvaluationFileError(f"{source} has no [[{key}]] entries")
    return [dict(entry) for entry in entries]


def _validate(
    entries: Sequence[Mapping[str, Any]],
    *,
    required: Sequence[str],
    optional: Sequence[str],
    label: str,
) -> list[dict[str, Any]]:
    known = set(required) | set(optional)
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        where = f"{label}[{index}]"
        for field in required:
            value = entry.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise EvaluationFileError(f"{where} is missing {field!r}")
            if isinstance(value, list) and not value:
                raise EvaluationFileError(f"{where} has an empty {field!r}")
        entry_id = str(entry["id"])
        if entry_id in seen:
            raise EvaluationFileError(f"{where} repeats id {entry_id!r}")
        seen.add(entry_id)
        unknown = set(entry) - known
        if unknown:
            raise EvaluationFileError(
                f"{where} has unknown fields: {', '.join(sorted(unknown))}"
            )
        validated.append(dict(entry))
    return validated


def load_experience_cards(path: str | Path) -> list[dict[str, Any]]:
    return _validate(
        _load_table(path, "card"),
        required=CARD_REQUIRED,
        optional=CARD_OPTIONAL,
        label="card",
    )


def load_replay_tasks(path: str | Path) -> list[dict[str, Any]]:
    return _validate(
        _load_table(path, "task"),
        required=TASK_REQUIRED,
        optional=TASK_OPTIONAL,
        label="task",
    )


_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _toml_basic(value: str) -> str:
    escaped = "".join(_TOML_ESCAPES.get(character, character) for character in value)
    return f'"{escaped}"'


def _toml_multiline(value: str) -> str:
    # Newlines stay literal so the file reads well; every quote is escaped so
    # neither an internal run of three nor a trailing one can close the string
    # early. TOML drops the newline right after the opening delimiter, so the
    # value round-trips unchanged.
    escaped = "".join(
        character if character == "\n" else _TOML_ESCAPES.get(character, character)
        for character in value
    )
    return f'"""\n{escaped}"""'


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        items = ",\n".join(f"  {_toml_basic(str(item))}" for item in value)
        return f"[\n{items},\n]" if items else "[]"
    text = str(value)
    return _toml_multiline(text) if "\n" in text else _toml_basic(text)


def dump_toml(entries: Sequence[Mapping[str, Any]], *, key: str, header: str) -> str:
    """
    Write entries in the hand-editable format the loaders read back.

    Generated files are meant to be opened and corrected, so they go through
    the same format as hand-written ones rather than a separate machine format.
    """
    lines = [line.rstrip() for line in header.strip().splitlines()]
    for entry in entries:
        lines.append("")
        lines.append(f"[[{key}]]")
        for field, value in entry.items():
            if value is None or value == [] or value == "":
                continue
            lines.append(f"{field} = {_toml_value(value)}")
    return "\n".join(lines).strip() + "\n"


def element_coverage(expected: Sequence[str], response: str) -> dict[str, Any]:
    """
    Deterministic scoring: did the answer contain what the owner said mattered?

    This is not a judgement of quality, and it is not a model's opinion. It is
    a check that survives changing models, which makes an A/B comparison
    meaningful over time. It shares the tokenizer used for retrieval, so it
    behaves the same for Chinese and Latin text.
    """
    found = set(search_tokens(response))
    covered: list[str] = []
    missing: list[str] = []
    for element in expected:
        tokens = set(search_tokens(element))
        if not tokens:
            continue
        ratio = len(tokens & found) / len(tokens)
        (covered if ratio >= ELEMENT_COVERAGE_THRESHOLD else missing).append(element)
    total = len(covered) + len(missing)
    return {
        "covered": covered,
        "missing": missing,
        "coverage": round(len(covered) / total, 4) if total else None,
    }


def build_context(
    connection: sqlite3.Connection, task: Mapping[str, Any], *, limit: int = 5
) -> dict[str, Any]:
    """
    Assemble what the agent would retrieve, as of the task's cutoff.

    Anything that concluded at or after `asked_at` is withheld. Without that
    the comparison would only prove the system can repeat an answer it was
    handed.
    """
    situation = str(task["situation"])
    cutoff = str(task["asked_at"])
    return {
        "as_of": cutoff,
        "similar_cases": find_similar_cases(
            connection, situation, limit=limit, before=cutoff
        ),
        "applicable_rules": get_applicable_rules(
            connection, situation, limit=limit, before=cutoff
        ),
        "prior_attempts": check_prior_attempts(
            connection, situation, limit=limit, before=cutoff
        ),
    }


def _baseline_prompt(task: Mapping[str, Any]) -> str:
    return f"""
You are advising on a problem. Propose what to do next.

Be concrete: name the checks to run, the changes to make, and the order to do
them in. State your uncertainty rather than hedging everything.

<situation>
{task["situation"]}
</situation>
""".strip()


def _informed_prompt(task: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    return f"""
You are advising on a problem. Propose what to do next.

<prior_experience> holds what this person has done before, drawn from their own
email and checked against the original text. Use it as follows:

- Read the advisories on each item. They state what the record does not support.
- An outcome_state of "unknown" means nobody recorded whether it worked. Never
  present such an approach as proven.
- Say where the current situation differs from the past one you are relying on.
- If nothing here applies, say so and advise from first principles. The record
  covers email only, so absence is not evidence that something never happened.

Be concrete: name the checks to run, the changes to make, and the order to do
them in. State your uncertainty rather than hedging everything.

<situation>
{task["situation"]}
</situation>

<prior_experience>
{json.dumps(context, ensure_ascii=False)}
</prior_experience>
""".strip()


def replay_prompts(
    connection: sqlite3.Connection, task: Mapping[str, Any], *, limit: int = 5
) -> dict[str, Any]:
    """The two prompts an A/B run compares, plus the context that differs."""
    context = build_context(connection, task, limit=limit)
    return {
        "task_id": task["id"],
        "context": context,
        "retrieved_counts": {
            key: len(value) for key, value in context.items() if isinstance(value, list)
        },
        "without_knowledge": _baseline_prompt(task),
        "with_knowledge": _informed_prompt(task, context),
    }


def score_replay(
    task: Mapping[str, Any], *, without: str, with_knowledge: str
) -> dict[str, Any]:
    expected = [str(item) for item in task["expected_elements"]]
    baseline = element_coverage(expected, without)
    informed = element_coverage(expected, with_knowledge)
    delta = (
        round((informed["coverage"] or 0) - (baseline["coverage"] or 0), 4)
        if baseline["coverage"] is not None and informed["coverage"] is not None
        else None
    )
    return {
        "task_id": task["id"],
        "without_knowledge": baseline,
        "with_knowledge": informed,
        "coverage_delta": delta,
    }


def summarize_replay(scores: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    deltas = [
        float(score["coverage_delta"])
        for score in scores
        if score.get("coverage_delta") is not None
    ]

    def mean(key: str) -> float | None:
        values = [
            float(score[key]["coverage"])
            for score in scores
            if score.get(key, {}).get("coverage") is not None
        ]
        return round(sum(values) / len(values), 4) if values else None

    return {
        "tasks": len(scores),
        "mean_coverage_without_knowledge": mean("without_knowledge"),
        "mean_coverage_with_knowledge": mean("with_knowledge"),
        "mean_coverage_delta": round(sum(deltas) / len(deltas), 4) if deltas else None,
        "tasks_improved": sum(1 for value in deltas if value > 0),
        "tasks_unchanged": sum(1 for value in deltas if value == 0),
        "tasks_regressed": sum(1 for value in deltas if value < 0),
    }


def run_replay(
    connection: sqlite3.Connection,
    tasks: Sequence[Mapping[str, Any]],
    *,
    model: str,
    workspace: str | Path,
    provider: Any = None,
    limit: int = 5,
) -> dict[str, Any]:
    """
    Answer every replay task twice, with and without the knowledge base.

    The same model answers both, so the difference is attributable to the
    knowledge rather than to the model. Responses are kept alongside the
    scores: a coverage number is a summary, and disagreements with it have to
    be checkable by reading what was actually said.
    """
    from .providers import build_provider

    backend = provider or build_provider(workspace=workspace)
    scores: list[dict[str, Any]] = []
    transcripts: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for task in tasks:
        prompts = replay_prompts(connection, task, limit=limit)
        answers: dict[str, str] = {}
        try:
            for condition in ("without_knowledge", "with_knowledge"):
                result = backend.complete(
                    prompts[condition],
                    model=model,
                    idempotency_key=f"replay:{task['id']}:{condition}:{model}",
                )
                answers[condition] = result.text
        # One failing task must not discard the rest of the comparison.
        except Exception as error:  # noqa: BLE001
            failures.append({"task_id": task["id"], "error": str(error)})
            continue

        scores.append(
            score_replay(
                task,
                without=answers["without_knowledge"],
                with_knowledge=answers["with_knowledge"],
            )
        )
        transcripts.append(
            {
                "task_id": task["id"],
                "retrieved_counts": prompts["retrieved_counts"],
                **answers,
            }
        )

    return {
        "model": model,
        "provider": backend.name,
        "summary": summarize_replay(scores),
        "scores": scores,
        "transcripts": transcripts,
        "failures": failures,
    }


def propose_replay_tasks(
    connection: sqlite3.Connection, *, limit: int = 50
) -> list[dict[str, Any]]:
    """
    Derive replay tasks from cases that recorded a problem, an action, and a
    result.

    Writing these by hand is the slowest part of setting up an evaluation, and
    almost all of it is mechanical: the situation, the moment before the
    resolution, and what actually happened are already in the validated claims.

    Unlike experience cards, generating these does not make the evaluation
    circular. The answer comes from the part of the thread that follows the
    cutoff, and the cutoff excludes that whole thread from retrieval, so
    neither condition can be handed its own answer.

    What still needs a person is `expected_elements`. These are taken from what
    the thread says was done, which is not always the same as what mattered.
    """
    rows = connection.execute(
        """
        SELECT thread_id, subject, situation, actions, outcome, summary
        FROM cases
        WHERE outcome_state = 'confirmed'
          AND situation <> ''
          AND actions <> ''
        ORDER BY importance_score DESC, ended_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    tasks: list[dict[str, Any]] = []
    for row in rows:
        thread_id = str(row["thread_id"])
        # The cutoff is the first moment an outcome was recorded. Retrieval
        # drops any thread still running then, which removes this one.
        resolution = connection.execute(
            """
            SELECT MIN(occurred_at) AS at FROM claims
            WHERE thread_id = ? AND claim_type = 'outcome' AND occurred_at IS NOT NULL
            """,
            (thread_id,),
        ).fetchone()
        asked_at = str(resolution["at"] or "") if resolution else ""
        if not asked_at:
            continue
        expected = [
            line.strip()
            for line in str(row["actions"] or "").splitlines()
            if line.strip()
        ]
        if not expected:
            continue
        tasks.append(
            {
                "id": f"auto-{thread_id[:32]}",
                "asked_at": asked_at,
                "source_thread_id": thread_id,
                "situation": f"{row['subject']}\n\n{row['situation']}".strip(),
                "expected_elements": expected,
                "what_actually_happened": str(row["outcome"] or "").strip(),
                "notes": (
                    "Generated from a case with a recorded outcome. Check that "
                    "expected_elements are what actually mattered, and that the "
                    "situation reads as it did at the time rather than in "
                    "hindsight."
                ),
            }
        )
    return tasks


def accepted_cards(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Induced rules the owner has accepted, in experience-card form."""
    from .induction import list_rules

    return [
        {
            "id": rule["rule_id"],
            "title": rule["title"],
            "situation": rule["situation"],
            "trigger": rule["trigger"],
            "actions": rule["actions"],
            "rationale": rule["rationale"],
            "exceptions": rule["exceptions"],
            "failure_conditions": rule["failure_conditions"],
        }
        for rule in list_rules(connection, status="accepted", limit=1000)
    ]


def card_coverage(
    connection: sqlite3.Connection,
    cards: Sequence[Mapping[str, Any]],
    *,
    limit: int = 3,
    reviewed: bool = True,
) -> dict[str, Any]:
    """
    For each card, show what the pipeline found on its own.

    This deliberately reports candidates rather than passing judgement. Whether
    a retrieved rule is really the same rule is a call only the owner can make,
    and an automatic verdict here would manufacture a score that means nothing.

    `reviewed` records whether a person stood behind these cards. Measuring the
    pipeline against cards the pipeline itself wrote and nobody confirmed is
    marking your own exam, so the result says so rather than being reported as
    a score.
    """
    results = []
    for card in cards:
        situation = str(card["situation"])
        rules = get_applicable_rules(connection, situation, limit=limit)
        cases = find_similar_cases(connection, situation, limit=limit)
        actions = [str(item) for item in card.get("actions") or []]
        results.append(
            {
                "card_id": card["id"],
                "title": card["title"],
                "candidate_rules": [
                    {
                        "claim_uid": rule["claim_uid"],
                        "text": rule["text"],
                        "action_overlap": element_coverage(actions, rule["text"]),
                    }
                    for rule in rules
                ],
                "candidate_cases": [
                    {
                        "thread_id": case["thread_id"],
                        "summary": case["summary"],
                        "outcome_state": case["outcome_state"],
                    }
                    for case in cases
                ],
                "found_nothing": not rules and not cases,
            }
        )
    return {
        "cards": len(cards),
        "cards_with_no_candidate": sum(1 for item in results if item["found_nothing"]),
        "baseline_valid": reviewed,
        "baseline_note": (
            "These cards were written or accepted by a person, so they are a "
            "usable baseline."
            if reviewed
            else "These cards have not been reviewed. They came from the same "
            "pipeline being measured, so this is not a baseline; use it to see "
            "what was induced, not as a score."
        ),
        "results": results,
    }
