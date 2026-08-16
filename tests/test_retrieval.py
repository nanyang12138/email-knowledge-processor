from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from email_kb.database import connect, initialize, save_thread_analysis, utc_now
from email_kb.retrieval import (
    check_prior_attempts,
    extract_identifiers,
    find_similar_cases,
    get_applicable_rules,
    get_case,
    index_knowledge,
    lookup_identifier,
    search_tokens,
)


def claim(claim_id: str, claim_type: str, text: str, message_id: str) -> dict:
    return {
        "claim_id": claim_id,
        "type": claim_type,
        "text": text,
        "message_ids": [message_id],
        "evidence_quotes": [
            {
                "message_id": message_id,
                "quote": text[:20],
                "normalized_span": [0, 20],
                "analysis_body_sha256": "0" * 64,
            }
        ],
    }


class TokenizerTests(unittest.TestCase):
    def test_latin_text_keeps_words(self) -> None:
        self.assertEqual(
            search_tokens("Build failed on CL/12345"),
            ["build", "failed", "on", "cl/12345"],
        )

    def test_chinese_run_becomes_overlapping_bigrams(self) -> None:
        self.assertEqual(search_tokens("构建失败"), ["构建", "建失", "失败"])

    def test_mixed_text_keeps_both(self) -> None:
        self.assertEqual(
            search_tokens("构建失败 build 12345"),
            ["构建", "建失", "失败", "build", "12345"],
        )


class IdentifierTests(unittest.TestCase):
    def test_extracts_common_identifier_shapes(self) -> None:
        found = {
            item["value"] for item in extract_identifiers("See CL 12345 and PROJ-42")
        }

        self.assertIn("cl:12345", found)
        self.assertIn("tracker:proj-42", found)

    def test_ignores_plain_numbers_as_commits(self) -> None:
        found = {item["kind"] for item in extract_identifiers("12345678")}

        self.assertNotIn("commit", found)


class IndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.connection = connect(Path(self.temporary.name) / "knowledge.db")
        initialize(self.connection)
        self.connection.execute(
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
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def save(
        self,
        thread_id: str,
        *,
        status: str = "verified",
        claims: list[dict],
        gap_reasons: list[str] | None = None,
        importance: int = 90,
        subject: str = "Build failure on CL 12345",
    ) -> None:
        save_thread_analysis(
            self.connection,
            thread_id=thread_id,
            source_fingerprint="fingerprint",
            status=status,
            importance_score=importance,
            model_reported_confidence=0.9,
            agreement_score=1.0,
            gap_reasons=gap_reasons or [],
            segment_count=1,
            proposals=None,
            verified={
                "subject": subject,
                "summary": "A build failed and was fixed.",
                "participants": [],
                "claims": claims,
                "updated_at": utc_now(),
            },
        )

    def test_partial_knowledge_is_not_indexed_for_agents(self) -> None:
        self.save(
            "thread-1",
            status="partial",
            claims=[claim("c1", "problem", "The nightly build failed.", "m1")],
        )

        result = index_knowledge(self.connection)

        self.assertEqual(result["cases"], 0)
        self.assertEqual(find_similar_cases(self.connection, "build failed"), [])

    def test_case_with_known_outcome_outranks_one_without(self) -> None:
        self.save(
            "thread-solved",
            claims=[
                claim("c1", "problem", "The nightly build failed on Windows.", "m1"),
                claim("c2", "action", "Pinned the toolchain version.", "m1"),
                claim("c3", "outcome", "The nightly build passed again.", "m1"),
            ],
        )
        self.save(
            "thread-open",
            claims=[
                claim("c1", "problem", "The nightly build failed on Windows.", "m1"),
                claim("c2", "action", "Asked the owning team to look.", "m1"),
            ],
        )
        index_knowledge(self.connection)

        results = find_similar_cases(self.connection, "nightly build failed")

        self.assertEqual(
            [item["thread_id"] for item in results], ["thread-solved", "thread-open"]
        )
        self.assertEqual(results[0]["outcome_state"], "confirmed")
        self.assertEqual(results[1]["outcome_state"], "unknown")

    def test_text_relevance_separates_unrelated_cases(self) -> None:
        topics = {
            "t-build": "The nightly build failed on Windows with a toolchain error.",
            "t-cert": "The TLS certificate for the staging gateway expired.",
            "t-disk": "The build agent ran out of disk space during upload.",
        }
        for thread_id, problem in topics.items():
            self.save(
                thread_id,
                subject=thread_id,
                claims=[
                    claim("c1", "problem", problem, "m1"),
                    claim("c2", "outcome", "It was resolved.", "m1"),
                ],
            )
        index_knowledge(self.connection)

        results = find_similar_cases(self.connection, "tls certificate expired")

        self.assertEqual(results[0]["thread_id"], "t-cert")
        self.assertGreater(results[0]["ranking"]["components"]["text_relevance"], 1.0)

    def test_every_result_carries_status_gaps_and_evidence(self) -> None:
        self.save(
            "thread-1",
            status="verified_with_gaps",
            gap_reasons=["attachment_content_unavailable"],
            claims=[
                claim("c1", "problem", "The nightly build failed.", "m1"),
                claim("c2", "reusable_rule", "Always pin the toolchain first.", "m1"),
            ],
        )
        index_knowledge(self.connection)

        case = find_similar_cases(self.connection, "nightly build failed")[0]
        rule = get_applicable_rules(self.connection, "pin the toolchain")[0]

        self.assertEqual(case["status"], "verified_with_gaps")
        self.assertEqual(case["gap_reasons"], ["attachment_content_unavailable"])
        self.assertEqual(rule["gap_reasons"], ["attachment_content_unavailable"])
        self.assertEqual(rule["evidence"][0]["message_id"], "m1")
        self.assertIn("text_relevance", case["ranking"]["components"])

    def test_chinese_situation_is_retrievable(self) -> None:
        self.save(
            "thread-cn",
            subject="夜间构建失败",
            claims=[
                claim("c1", "problem", "夜间构建在 Windows 上失败。", "m1"),
                claim("c2", "outcome", "锁定工具链版本后构建恢复。", "m1"),
            ],
        )
        index_knowledge(self.connection)

        results = find_similar_cases(self.connection, "构建失败")

        self.assertEqual([item["thread_id"] for item in results], ["thread-cn"])

    def test_prior_attempts_only_returns_actions_and_decisions(self) -> None:
        self.save(
            "thread-1",
            claims=[
                claim("c1", "problem", "Pinned versions caused a conflict.", "m1"),
                claim("c2", "action", "Pinned the toolchain version.", "m1"),
            ],
        )
        index_knowledge(self.connection)

        results = check_prior_attempts(self.connection, "pin the toolchain version")

        self.assertEqual([item["type"] for item in results], ["action"])

    def test_identifier_lookup_is_exact(self) -> None:
        self.save(
            "thread-1",
            subject="Build failure on CL 12345",
            claims=[claim("c1", "problem", "CL 12345 broke the build.", "m1")],
        )
        self.save(
            "thread-2",
            subject="Unrelated build failure",
            claims=[claim("c1", "problem", "Something else broke.", "m1")],
        )
        index_knowledge(self.connection)

        results = lookup_identifier(self.connection, "CL 12345")

        self.assertEqual([item["thread_id"] for item in results], ["thread-1"])
        self.assertEqual(results[0]["identifier"], "cl:12345")

    def test_get_case_returns_claims_with_evidence(self) -> None:
        self.save(
            "thread-1",
            claims=[claim("c1", "problem", "The nightly build failed.", "m1")],
        )
        index_knowledge(self.connection)

        case = get_case(self.connection, "thread-1")

        self.assertEqual(case["thread_id"], "thread-1")
        self.assertEqual(len(case["claims"]), 1)
        self.assertEqual(case["claims"][0]["evidence"][0]["message_id"], "m1")
        self.assertEqual(case["occurred_between"][0], "2026-01-01T00:00:00Z")

    def test_reindex_replaces_previous_content(self) -> None:
        self.save(
            "thread-1",
            claims=[claim("c1", "problem", "The nightly build failed.", "m1")],
        )
        index_knowledge(self.connection)
        index_knowledge(self.connection)

        rows = self.connection.execute("SELECT COUNT(*) FROM claims_fts").fetchone()[0]
        self.assertEqual(int(rows), 1)


if __name__ == "__main__":
    unittest.main()
