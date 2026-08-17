"""
MCP server exposing the knowledge base to agents.

Tools are named for the question an agent actually has, not for the retrieval
method behind them. A single `search_knowledge(query)` tool gets used as a
search box and the agent stops there; `check_if_i_tried_this_before(approach)`
prompts the comparison that makes the knowledge worth having.

Every result carries its status, known gaps, outcome state, evidence, and
advisories, so an agent cannot receive a bare assertion with no provenance.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .database import connect, initialize, quality_report
from .feedback import record_feedback
from .retrieval import AGENT_VISIBLE_STATUSES, index_stats
from .retrieval import check_prior_attempts as _check_prior_attempts
from .retrieval import find_similar_cases as _find_similar_cases
from .retrieval import get_applicable_rules as _get_applicable_rules
from .retrieval import get_case as _get_case
from .retrieval import lookup_identifier as _lookup_identifier

INSTRUCTIONS = """
This server answers questions about what the mailbox owner has done before,
using knowledge extracted from their own email and checked against the original
text.

Use it before proposing an approach, not after. A useful sequence is:

1. Describe the current situation to find_similar_cases.
2. Ask get_applicable_rules for rules that may bind here.
3. Ask check_if_i_tried_this_before about the approach you are considering.
4. Open promising results with get_case to read the underlying evidence.
5. Say how the current situation differs from the past one you are relying on.

Read the `advisories` field on every result. It states what the record does not
support. In particular, `outcome_state` of "unknown" means nobody wrote down
whether the approach worked, so it must not be presented as proven.

This knowledge comes only from email. Reasoning that happened in meetings, chat,
or someone's head is not here, and outcomes are frequently missing. Absence of a
case is not evidence that something never happened. Call knowledge_coverage to
see how much has been analyzed and how reliable it currently is.
""".strip()


def _open(database: Path) -> sqlite3.Connection:
    connection = connect(database)
    initialize(connection)
    return connection


def _query(database: Path, function: Any, *args: Any, **kwargs: Any) -> Any:
    connection = _open(database)
    try:
        return function(connection, *args, **kwargs)
    finally:
        connection.close()


def build_server(database: str | Path, *, allow_feedback: bool = False) -> Any:
    from mcp.server.mcpserver import MCPServer

    path = Path(database).expanduser().resolve()
    server = MCPServer(name="email-knowledge", instructions=INSTRUCTIONS)

    @server.tool()
    def find_similar_cases(situation: str, limit: int = 5) -> list[dict[str, Any]]:
        """
        Find past problems that resemble the situation you are facing now.

        Describe the situation itself: what is failing, what system is involved,
        what constraints apply. Matching is against the problem recorded in past
        cases, so a description of the problem works better than keywords.
        """
        return _query(path, _find_similar_cases, situation, limit=limit)

    @server.tool()
    def get_applicable_rules(context: str, limit: int = 5) -> list[dict[str, Any]]:
        """
        Get reusable rules, exceptions, and stated preferences that may apply.

        These are things the owner has done repeatedly or stated as a
        preference. Each one still carries the evidence it was drawn from;
        check whether the conditions it was formed under hold now.
        """
        return _query(path, _get_applicable_rules, context, limit=limit)

    @server.tool()
    def check_if_i_tried_this_before(
        approach: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """
        Check whether an approach was already attempted, and what came of it.

        Call this before proposing a course of action. An `outcome_state` of
        "unknown" means the attempt is recorded but the result is not, which is
        common and must not be read as success.
        """
        return _query(path, _check_prior_attempts, approach, limit=limit)

    @server.tool()
    def lookup_identifier(identifier: str, limit: int = 20) -> list[dict[str, Any]]:
        """
        Look up an exact CL, bug, ticket, build, or commit identifier.

        Use this instead of a text search when you have a precise identifier, so
        it is not outranked by prose that merely reads similarly.
        """
        return _query(path, _lookup_identifier, identifier, limit=limit)

    @server.tool()
    def get_case(thread_id: str) -> dict[str, Any] | None:
        """
        Open one case with every validated claim and the quotes behind it.

        Use this to check a result before relying on it. Each claim lists the
        source message and the exact quote that supports it.
        """
        return _query(path, _get_case, thread_id)

    @server.tool()
    def knowledge_coverage() -> dict[str, Any]:
        """
        Report how much has been analyzed and how reliable it currently is.

        Use this to calibrate. A small `cases` count means absence of a result
        says little, and a low `independent_pass_agreement` means extraction is
        still unstable.
        """
        connection = _open(path)
        try:
            return {
                "index": index_stats(connection),
                "quality": quality_report(connection),
                "visible_statuses": list(AGENT_VISIBLE_STATUSES),
                "source_limits": [
                    "Only email is indexed. Meetings, chat, and documents are not.",
                    "Attachment contents are not imported yet.",
                    (
                        "Evidence checks prove a quote exists, not that the "
                        "claim follows from it."
                    ),
                ],
            }
        finally:
            connection.close()

    if allow_feedback:

        @server.tool()
        def record_usefulness(
            target_id: str, was_useful: bool, note: str | None = None
        ) -> dict[str, Any]:
            """
            Record whether a retrieved case or claim actually helped.

            Call this after using a result, with the claim_uid or thread_id you
            relied on. This only moves things up or down in future rankings. It
            cannot change what a claim says, and it cannot mark anything wrong
            or outdated: those are the owner's judgements to make, not an
            agent's inference from one task going well or badly.
            """
            connection = _open(path)
            try:
                return record_feedback(
                    connection,
                    target_id=target_id,
                    verdict="useful" if was_useful else "not_useful",
                    note=note,
                )
            finally:
                connection.close()

    return server


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="email-kb-mcp",
        description="Serve the personal knowledge base over MCP",
    )
    parser.add_argument("--db", type=Path, default=Path("data") / "knowledge.db")
    parser.add_argument(
        "--allow-feedback",
        action="store_true",
        help="Let the agent record whether a result helped. Off by default: "
        "reading knowledge and writing to it are separate permissions.",
    )
    args = parser.parse_args(argv)
    build_server(args.db, allow_feedback=args.allow_feedback).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
