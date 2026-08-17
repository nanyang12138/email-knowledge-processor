from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path

from email_kb.database import connect, initialize, save_thread_analysis
from email_kb.retrieval import advisories, index_knowledge

HAS_MCP = importlib.util.find_spec("mcp.server.mcpserver") is not None


class AdvisoryTests(unittest.TestCase):
    def test_missing_outcome_is_stated_not_implied(self) -> None:
        notes = advisories(
            status="verified",
            gap_reasons=[],
            outcome_state="unknown",
            occurred_at="2026-01-01T00:00:00Z",
        )

        self.assertEqual(len(notes), 1)
        self.assertIn("No outcome was recorded", notes[0])

    def test_clean_recent_case_has_nothing_to_warn_about(self) -> None:
        notes = advisories(
            status="verified",
            gap_reasons=[],
            outcome_state="confirmed",
            occurred_at="2026-06-01T00:00:00Z",
        )

        self.assertEqual(notes, [])

    def test_each_gap_reason_becomes_a_readable_limit(self) -> None:
        notes = advisories(
            status="verified_with_gaps",
            gap_reasons=[
                "attachment_content_unavailable",
                "independent_passes_cited_different_evidence",
            ],
            outcome_state="confirmed",
            occurred_at="2026-06-01T00:00:00Z",
        )

        self.assertTrue(any("Attachment contents" in note for note in notes))
        self.assertTrue(any("unstable" in note for note in notes))
        self.assertTrue(any("verified_with_gaps" in note for note in notes))

    def test_old_knowledge_is_flagged_as_needing_confirmation(self) -> None:
        notes = advisories(
            status="verified",
            gap_reasons=[],
            outcome_state="confirmed",
            occurred_at="2015-01-01T00:00:00Z",
        )

        self.assertTrue(any("years old" in note for note in notes))


@unittest.skipUnless(HAS_MCP, "mcp extra is not installed")
class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "knowledge.db"
        connection = connect(self.database)
        initialize(connection)
        connection.execute(
            """
            INSERT INTO messages (
                email_id, conversation_id, to_recipients_json, cc_recipients_json,
                bcc_recipients_json, reply_to_json, subject, body, clean_body,
                has_attachments, is_read, categories_json, body_sha256, raw_json,
                sent_at_utc
            )
            VALUES ('m1', 'thread-1', '[]', '[]', '[]', '[]', 'Build failure',
                    'b', 'b', 0, 1, '[]', 'x', '{}', '2026-01-01T00:00:00Z')
            """
        )
        connection.commit()
        save_thread_analysis(
            connection,
            thread_id="thread-1",
            source_fingerprint="fingerprint",
            status="verified_with_gaps",
            importance_score=80,
            model_reported_confidence=0.9,
            agreement_score=0.9,
            gap_reasons=["attachment_content_unavailable"],
            segment_count=1,
            proposals=None,
            verified={
                "subject": "Nightly build failure",
                "summary": "The nightly build failed and was investigated.",
                "participants": [],
                "claims": [
                    {
                        "claim_id": "c1",
                        "type": "problem",
                        "text": "The nightly build failed on Windows.",
                        "message_ids": ["m1"],
                        "evidence_quotes": [
                            {"message_id": "m1", "quote": "nightly build failed"}
                        ],
                    },
                    {
                        "claim_id": "c2",
                        "type": "action",
                        "text": "Pinned the toolchain version.",
                        "message_ids": ["m1"],
                        "evidence_quotes": [
                            {"message_id": "m1", "quote": "pinned the toolchain"}
                        ],
                    },
                ],
            },
        )
        index_knowledge(connection)
        connection.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def call(self, name: str, arguments: dict):
        from email_kb.mcp_server import build_server

        server = build_server(self.database)
        result = asyncio.run(server.call_tool(name, arguments))
        return result.structured_content["result"]

    def test_tools_are_named_for_the_question_not_the_method(self) -> None:
        from email_kb.mcp_server import build_server

        tools = asyncio.run(build_server(self.database).list_tools())

        self.assertEqual(
            {tool.name for tool in tools},
            {
                "search_email",
                "read_email_thread",
                "find_similar_cases",
                "get_applicable_rules",
                "check_if_i_tried_this_before",
                "lookup_identifier",
                "get_case",
                "knowledge_coverage",
            },
        )

    def test_results_cannot_arrive_without_provenance(self) -> None:
        results = self.call("find_similar_cases", {"situation": "nightly build failed"})

        self.assertEqual(len(results), 1)
        found = results[0]
        for field in ("status", "gap_reasons", "outcome_state", "advisories"):
            self.assertIn(field, found)
        self.assertTrue(any("Attachment" in note for note in found["advisories"]))
        self.assertTrue(
            any("No outcome was recorded" in note for note in found["advisories"])
        )

    def test_prior_attempt_without_outcome_is_not_presented_as_proven(self) -> None:
        results = self.call(
            "check_if_i_tried_this_before", {"approach": "pin the toolchain version"}
        )

        self.assertEqual(results[0]["outcome_state"], "unknown")
        self.assertTrue(
            any("No outcome was recorded" in note for note in results[0]["advisories"])
        )

    def test_coverage_reports_what_the_source_cannot_contain(self) -> None:
        from email_kb.mcp_server import build_server

        server = build_server(self.database)
        result = asyncio.run(server.call_tool("knowledge_coverage", {}))
        coverage = result.structured_content

        self.assertEqual(coverage["index"]["cases"], 1)
        self.assertTrue(
            any("Only email" in limit for limit in coverage["source_limits"])
        )


if __name__ == "__main__":
    unittest.main()
