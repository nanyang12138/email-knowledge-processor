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
from .ingest import ingest_sources

DEFAULT_DATABASE = Path("data") / "knowledge.db"


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
        help=f"SQLite database path (default: {DEFAULT_DATABASE})",
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

    subparsers.add_parser("models", help="List Cursor models available to the API key")
    return parser


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
        if args.command == "models":
            _print_json(_list_models())
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
                _print_json(database_stats(connection))
            elif args.command == "report":
                _print_json(quality_report(connection))
            elif args.command == "analyze":
                _print_json(
                    analyze_database(
                        connection,
                        owner_email=args.owner_email,
                        workspace=Path.cwd(),
                        model=args.model,
                        second_model=args.second_model,
                        reconciler_model=args.reconciler_model,
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
