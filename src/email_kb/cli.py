from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from cursor_sdk import Cursor

from .analysis import analyze_database
from .database import connect, database_stats, initialize, quality_report
from .diagnostics import doctor, mcp_config
from .evaluation import (
    accepted_cards,
    card_coverage,
    dump_toml,
    load_experience_cards,
    load_replay_tasks,
    propose_replay_tasks,
    replay_prompts,
    run_replay,
)
from .feedback import VERDICTS, feedback_stats, record_feedback, review_queue
from .induction import (
    DEFAULT_MIN_SIMILARITY,
    induce_rules,
    list_rules,
    rule_stats,
)
from .ingest import ingest_sources, survey_sources
from .providers import build_provider
from .retrieval import (
    AGENT_VISIBLE_STATUSES,
    check_prior_attempts,
    find_similar_cases,
    get_applicable_rules,
    get_case,
    index_knowledge,
    index_messages,
    index_stats,
    lookup_identifier,
    read_thread,
    search_messages,
)

# The database lands at roughly three times the size of the export, so it
# often needs to live somewhere other than the default. Setting this once beats
# passing --db to every command.
DEFAULT_DATABASE = Path(os.environ.get("EMAIL_KB_DB") or Path("data") / "knowledge.db")


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="email-kb",
        description="Build an auditable personal email knowledge database.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"SQLite database path, or set EMAIL_KB_DB (default: {DEFAULT_DATABASE})",
    )
    parser.add_argument(
        "--provider",
        choices=("cursor", "openai-compatible"),
        default=os.environ.get("EMAIL_KB_PROVIDER", "cursor"),
        help="Which backend sees the mail. 'openai-compatible' points at any "
        "OpenAI chat-completions endpoint, including a local runtime "
        "(or set EMAIL_KB_PROVIDER)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("EMAIL_KB_BASE_URL"),
        help="Endpoint for --provider openai-compatible, for example "
        "http://localhost:11434/v1 (or set EMAIL_KB_BASE_URL)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize the SQLite database")

    ingest = subparsers.add_parser(
        "ingest", help="Import Graph RawJSON or exported CSV files"
    )
    ingest.add_argument("paths", nargs="+", type=Path)
    ingest.add_argument(
        "--force", action="store_true", help="Re-import unchanged files"
    )

    check = subparsers.add_parser(
        "check-source",
        help="Check an export is fully downloaded before importing it",
    )
    check.add_argument("paths", nargs="+", type=Path)

    subparsers.add_parser("stats", help="Show database counts")
    subparsers.add_parser("report", help="Show evidence and verification quality")

    analyze = subparsers.add_parser(
        "analyze",
        help="Extract thread knowledge with two blind passes and reconcile them",
    )
    analyze.add_argument(
        "--owner-email",
        default=os.environ.get("EMAIL_KB_OWNER", ""),
        help="Mailbox owner address (or set EMAIL_KB_OWNER)",
    )
    analyze.add_argument(
        "--model",
        default="auto",
        help="Model for the first blind extraction pass (default: auto)",
    )
    analyze.add_argument(
        "--second-model",
        help=(
            "Model for the second blind pass (default: same as --model). "
            "A different model makes the agreement score more informative."
        ),
    )
    analyze.add_argument(
        "--reconciler-model",
        help="Model that reconciles the two passes (default: same as --model)",
    )
    analyze.add_argument("--limit", type=int, help="Maximum threads to consider")
    analyze.add_argument(
        "--max-chars-per-request",
        type=int,
        default=80_000,
        help="Maximum source characters in one Cursor run; longer threads are "
        "split into ordered segments rather than dropped",
    )
    analyze.add_argument(
        "--min-agreement",
        type=float,
        default=0.5,
        help="Evidence overlap below which the two blind passes are treated as "
        "disagreeing (default: 0.5)",
    )
    analyze.add_argument(
        "--force", action="store_true", help="Reanalyze unchanged verified threads"
    )
    analyze.add_argument(
        "--dry-run",
        action="store_true",
        help="Estimate runs without calling Cursor or changing analysis state",
    )

    subparsers.add_parser(
        "index",
        help="Build the search index. The mail itself is indexed with no model "
        "involved; cases and claims are added once analyses exist",
    )

    search = subparsers.add_parser(
        "search", help="Query the index the way an agent would"
    )
    search.add_argument("query")
    search.add_argument(
        "--mode",
        choices=("email", "cases", "rules", "prior", "identifier"),
        default="email",
        help=(
            "email: the mail itself, needs no analysis; "
            "cases: past problems resembling this situation; "
            "rules: reusable rules that may apply; "
            "prior: whether this approach was already tried; "
            "identifier: exact CL/bug/ticket lookup"
        ),
    )
    search.add_argument("--limit", type=int, default=10)
    search.add_argument(
        "--sender", help="Only mail from this address, for --mode email"
    )
    search.add_argument("--since", help="ISO date lower bound, for --mode email")
    search.add_argument("--until", help="ISO date upper bound, for --mode email")

    thread = subparsers.add_parser("thread", help="Read one whole conversation")
    thread.add_argument("thread_id")
    search.add_argument(
        "--include-partial",
        action="store_true",
        help="Also return knowledge that failed validation (excluded by default)",
    )

    case = subparsers.add_parser(
        "case", help="Show one case with every validated claim and its evidence"
    )
    case.add_argument("thread_id")

    review = subparsers.add_parser(
        "review",
        help="Show knowledge you have not judged yet, most consequential first",
    )
    review.add_argument("--limit", type=int, default=20)

    mark = subparsers.add_parser(
        "mark", help="Record your judgement of one claim or case"
    )
    mark.add_argument("target_id", help="A claim uid (thread:claim) or a thread id")
    mark.add_argument("verdict", choices=VERDICTS)
    mark.add_argument("--note", help="Why, in your own words")

    induce = subparsers.add_parser(
        "induce",
        help="Group recurring cases and induce candidate rules from them",
    )
    induce.add_argument("--model", default="auto")
    induce.add_argument("--limit", type=int, help="Maximum clusters to process")
    induce.add_argument(
        "--max-cluster",
        type=int,
        default=8,
        help="Largest group of cases considered one recurring situation",
    )
    induce.add_argument(
        "--min-similarity",
        type=float,
        default=DEFAULT_MIN_SIMILARITY,
        help="How alike two situations must be to be grouped. Read the "
        "similarity figures in a dry run before changing this.",
    )
    induce.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the clusters and what would be sent, without calling a model",
    )

    rules = subparsers.add_parser(
        "rules", help="Review induced rules and decide which are yours"
    )
    rules.add_argument(
        "--status",
        choices=("unreviewed", "accepted", "rejected", "all"),
        default="unreviewed",
    )
    rules.add_argument("--limit", type=int, default=50)

    export = subparsers.add_parser(
        "export",
        help="Write accepted rules or generated replay tasks to an editable file",
    )
    export.add_argument("what", choices=("cards", "replay"))
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--limit", type=int, default=50)

    cards = subparsers.add_parser(
        "cards",
        help="Check what the pipeline finds on its own for each experience card",
    )
    cards.add_argument(
        "--source",
        choices=("file", "accepted"),
        default="file",
        help="file: cards you wrote or edited; accepted: induced rules you accepted",
    )
    cards.add_argument(
        "--path",
        type=Path,
        default=Path("evaluation") / "experience_cards.toml",
        help="TOML file of experience cards, when --source is file",
    )
    cards.add_argument("--limit", type=int, default=3)

    replay = subparsers.add_parser(
        "replay",
        help="Answer past problems with and without the knowledge base, and compare",
    )
    replay.add_argument(
        "--path",
        type=Path,
        default=Path("evaluation") / "decision_replay.toml",
        help="TOML file of decision replay tasks",
    )
    replay.add_argument("--model", default="auto")
    replay.add_argument("--limit", type=int, default=5)
    replay.add_argument(
        "--prompts-only",
        action="store_true",
        help="Show the two prompts and what was retrieved, without calling a model",
    )
    replay.add_argument(
        "--out",
        type=Path,
        help="Write the full report, including both responses, to this file",
    )

    subparsers.add_parser(
        "doctor", help="Check whether this is ready for an agent to use"
    )

    mcp = subparsers.add_parser(
        "mcp-config", help="Print an MCP entry for Claude Code or Cursor"
    )
    mcp.add_argument("--client", choices=("claude", "cursor"), required=True)
    mcp.add_argument(
        "--allow-feedback",
        action="store_true",
        help="Let the agent record whether a result helped, ranking only",
    )

    subparsers.add_parser("models", help="List Cursor models available to the API key")
    return parser


CARD_HEADER = """
# Experience cards accepted from induced rules.
#
# Edit these freely. A card you corrected is worth more than one you accepted
# as written, because the correction is the part only you can supply.
"""

REPLAY_HEADER = """
# Replay tasks generated from cases that recorded a result.
#
# Read each one before trusting the numbers it produces. Two things need your
# eye: whether expected_elements are what actually mattered rather than merely
# what got done, and whether the situation reads as it did at the time instead
# of with hindsight folded in.
#
# Delete the ones that are not worth measuring. A small set you believe in
# beats a large one you have not read.
"""


def _provider(args: Any) -> Any:
    return build_provider(
        provider=args.provider, workspace=Path.cwd(), base_url=args.base_url
    )


def _induce(connection: Any, args: Any) -> Any:
    return induce_rules(
        connection,
        workspace=Path.cwd(),
        provider=None if args.dry_run else _provider(args),
        model=args.model,
        limit=args.limit,
        max_cluster=args.max_cluster,
        min_similarity=args.min_similarity,
        dry_run=args.dry_run,
    ) | ({} if args.dry_run else rule_stats(connection))


def _export(connection: Any, args: Any) -> Any:
    if args.what == "cards":
        entries = accepted_cards(connection)
        key, header = "card", CARD_HEADER
        if not entries:
            raise RuntimeError(
                "No accepted rules yet. Run 'induce', then 'rules' to review "
                "them and 'mark <rule-id> useful' to accept."
            )
    else:
        entries = propose_replay_tasks(connection, limit=args.limit)
        key, header = "task", REPLAY_HEADER
        if not entries:
            raise RuntimeError(
                "No case has both a recorded action and a recorded outcome yet, "
                "so there is nothing to replay."
            )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(dump_toml(entries, key=key, header=header), encoding="utf-8")
    return {"written": str(args.out), key: len(entries)}


def _replay(connection: Any, args: Any) -> Any:
    tasks = load_replay_tasks(args.path)
    if args.prompts_only:
        return [replay_prompts(connection, task, limit=args.limit) for task in tasks]
    report = run_replay(
        connection,
        tasks,
        model=args.model,
        workspace=Path.cwd(),
        provider=_provider(args),
        limit=args.limit,
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {**report["summary"], "report": str(args.out)}
    return report


def _search(connection: Any, args: Any) -> Any:
    if args.mode == "email":
        return search_messages(
            connection,
            args.query,
            limit=args.limit,
            sender=args.sender,
            since=args.since,
            until=args.until,
        )
    statuses = list(AGENT_VISIBLE_STATUSES)
    if args.include_partial:
        statuses.append("partial")
    if args.mode == "cases":
        return find_similar_cases(
            connection, args.query, limit=args.limit, statuses=statuses
        )
    if args.mode == "rules":
        return get_applicable_rules(
            connection, args.query, limit=args.limit, statuses=statuses
        )
    if args.mode == "prior":
        return check_prior_attempts(
            connection, args.query, limit=args.limit, statuses=statuses
        )
    return lookup_identifier(connection, args.query, limit=args.limit)


def _list_models() -> list[dict[str, Any]]:
    if not os.environ.get("CURSOR_API_KEY"):
        raise RuntimeError("CURSOR_API_KEY is not set")
    models = Cursor.models.list(api_key=os.environ["CURSOR_API_KEY"])
    return [
        {
            "id": str(getattr(model, "id", "")),
            "name": str(getattr(model, "name", "")),
        }
        for model in models
    ]


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        # These answer questions about the outside world, so they must not
        # create a database as a side effect of being asked.
        if args.command == "models":
            _print_json(_list_models())
            return 0
        if args.command == "check-source":
            _print_json(survey_sources(args.paths))
            return 0
        if args.command == "mcp-config":
            _print_json(
                mcp_config(
                    args.db, client=args.client, allow_feedback=args.allow_feedback
                )
            )
            return 0

        connection = connect(args.db)
        try:
            schema = initialize(connection)
            if args.command == "init":
                _print_json(
                    {
                        "database": str(Path(args.db).resolve()),
                        "status": "ready",
                        **schema,
                    }
                )
            elif args.command == "ingest":
                _print_json(ingest_sources(connection, args.paths, force=args.force))
            elif args.command == "stats":
                _print_json(
                    database_stats(connection)
                    | index_stats(connection)
                    | feedback_stats(connection)
                )
            elif args.command == "doctor":
                _print_json(doctor(connection, args.db))
            elif args.command == "induce":
                _print_json(_induce(connection, args))
            elif args.command == "rules":
                _print_json(
                    list_rules(connection, status=args.status, limit=args.limit)
                )
            elif args.command == "export":
                _print_json(_export(connection, args))
            elif args.command == "cards":
                from_file = args.source == "file"
                _print_json(
                    card_coverage(
                        connection,
                        (
                            load_experience_cards(args.path)
                            if from_file
                            else accepted_cards(connection)
                        ),
                        limit=args.limit,
                        reviewed=True,
                    )
                )
            elif args.command == "replay":
                _print_json(_replay(connection, args))
            elif args.command == "review":
                _print_json(review_queue(connection, limit=args.limit))
            elif args.command == "mark":
                _print_json(
                    record_feedback(
                        connection,
                        target_id=args.target_id,
                        verdict=args.verdict,
                        note=args.note,
                    )
                )
            elif args.command == "report":
                _print_json(quality_report(connection))
            elif args.command == "index":
                _print_json(
                    index_messages(connection)
                    | index_knowledge(connection)
                    | index_stats(connection)
                )
            elif args.command == "search":
                _print_json(_search(connection, args))
            elif args.command == "thread":
                _print_json(read_thread(connection, args.thread_id))
            elif args.command == "case":
                found = get_case(connection, args.thread_id)
                if found is None:
                    raise RuntimeError(f"No indexed case for {args.thread_id}")
                _print_json(found)
            elif args.command == "analyze":
                _print_json(
                    analyze_database(
                        connection,
                        owner_email=args.owner_email,
                        workspace=Path.cwd(),
                        model=args.model,
                        second_model=args.second_model,
                        reconciler_model=args.reconciler_model,
                        provider=None if args.dry_run else _provider(args),
                        limit=args.limit,
                        max_chars_per_request=args.max_chars_per_request,
                        min_agreement=args.min_agreement,
                        force=args.force,
                        dry_run=args.dry_run,
                    )
                )
            else:
                raise RuntimeError(f"Unknown command: {args.command}")
        finally:
            connection.close()
        return 0
    # The CLI boundary converts all operational failures into a non-zero exit.
    except Exception as error:  # noqa: BLE001
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
