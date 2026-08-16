from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from email_kb.analysis import (
    _parse_agent_json,
    _validate_analysis,
    analyze_database,
    compare_passes,
    decide_status,
    segment_document,
    validate_claims,
)
from email_kb.database import connect, initialize, quality_report
from email_kb.ingest import ingest_sources

SOURCE_LOOKUP = {
    "m1": "Use option B.",
    "m2": "Option B succeeded.",
}

DOCUMENT = {
    "thread_id": "thread-1",
    "has_unavailable_attachments": False,
    "messages": [
        {"message_id": "m1", "body": "Use option B."},
        {"message_id": "m2", "body": "Option B succeeded."},
    ],
}


def claim(claim_id: str, message_id: str, quote: str, claim_type: str = "fact") -> dict:
    return {
        "claim_id": claim_id,
        "type": claim_type,
        "text": f"Claim from {message_id}.",
        "message_ids": [message_id],
        "evidence_quotes": [{"message_id": message_id, "quote": quote}],
        "confidence": 0.9,
    }


def analysis(
    *,
    category: str = "decision",
    importance: int = 92,
    claims: list[dict] | None = None,
    summary_claim_ids: list[str] | None = None,
    needs_more_context: bool = False,
) -> dict:
    claims = (
        claims
        if claims is not None
        else [
            claim("c1", "m1", "Use option B.", "decision"),
            claim("c2", "m2", "Option B succeeded.", "outcome"),
        ]
    )
    return {
        "schema_version": "2.0",
        "thread_id": "thread-1",
        "category": category,
        "importance_score": importance,
        "importance_reasons": ["A decision with a confirmed result."],
        "summary": "The owner selected option B and it succeeded.",
        "summary_claim_ids": (
            summary_claim_ids
            if summary_claim_ids is not None
            else [item["claim_id"] for item in claims]
        ),
        "claims": claims,
        "needs_more_context": needs_more_context,
        "missing_context": [],
        "factual_confidence": 0.98,
    }


class ClaimValidationTests(unittest.TestCase):
    def test_quote_absent_from_source_is_dropped(self) -> None:
        claims = [
            claim("c1", "m1", "Invented quote that is long enough."),
            claim("c2", "m2", "Option B succeeded."),
        ]

        valid, errors = validate_claims(claims, SOURCE_LOOKUP)

        self.assertEqual([item["claim_id"] for item in valid], ["c2"])
        self.assertTrue(any("quote not found" in error for error in errors))

    def test_valid_claim_carries_span_and_source_hash(self) -> None:
        valid, errors = validate_claims(
            [claim("c1", "m1", "Use option B.")], SOURCE_LOOKUP
        )

        self.assertEqual(errors, [])
        evidence = valid[0]["evidence_quotes"][0]
        self.assertEqual(evidence["normalized_span"], [0, 13])
        self.assertEqual(len(evidence["analysis_body_sha256"]), 64)

    def test_evidence_from_unknown_message_is_dropped(self) -> None:
        valid, errors = validate_claims(
            [claim("c1", "m9", "Use option B.")], SOURCE_LOOKUP
        )

        self.assertEqual(valid, [])
        self.assertTrue(any("unknown message" in error for error in errors))


class SummaryGroundingTests(unittest.TestCase):
    def test_summary_must_name_validated_claims(self) -> None:
        result = _validate_analysis(analysis(summary_claim_ids=[]), DOCUMENT)

        self.assertFalse(result["summary_grounded"])
        self.assertTrue(any("summary_claim_ids" in error for error in result["errors"]))

    def test_summary_referencing_dropped_claim_is_not_grounded(self) -> None:
        payload = analysis(
            claims=[claim("c1", "m1", "A quote that does not exist anywhere.")],
            summary_claim_ids=["c1"],
        )

        result = _validate_analysis(payload, DOCUMENT)

        self.assertFalse(result["summary_grounded"])
        self.assertEqual(result["claims"], [])


class AgreementTests(unittest.TestCase):
    def test_identical_evidence_scores_full_agreement(self) -> None:
        left = _validate_analysis(analysis(), DOCUMENT)
        right = _validate_analysis(analysis(), DOCUMENT)

        result = compare_passes(left, right)

        self.assertEqual(result["evidence_agreement"], 1.0)
        self.assertTrue(result["category_agreement"])
        self.assertEqual(result["importance_delta"], 0)

    def test_disjoint_evidence_scores_no_agreement(self) -> None:
        left = _validate_analysis(
            analysis(claims=[claim("c1", "m1", "Use option B.")]), DOCUMENT
        )
        right = _validate_analysis(
            analysis(claims=[claim("c1", "m2", "Option B succeeded.")]), DOCUMENT
        )

        result = compare_passes(left, right)

        self.assertEqual(result["evidence_agreement"], 0.0)


AGREED = {
    "evidence_agreement": 1.0,
    "category_agreement": True,
    "importance_delta": 0,
}


class StatusTests(unittest.TestCase):
    def test_agreeing_passes_with_clean_evidence_are_verified(self) -> None:
        final = _validate_analysis(analysis(), DOCUMENT)

        status, gaps = decide_status(
            final, AGREED, DOCUMENT, unanalyzable=[], min_agreement=0.5
        )

        self.assertEqual(status, "verified")
        self.assertEqual(gaps, [])

    def test_model_self_report_cannot_produce_verified(self) -> None:
        payload = analysis(summary_claim_ids=[])
        payload["summary_grounded"] = True
        payload["audit"] = {"summary_grounded": True, "factual_confidence": 1.0}
        final = _validate_analysis(payload, DOCUMENT)

        status, _ = decide_status(
            final, AGREED, DOCUMENT, unanalyzable=[], min_agreement=0.5
        )

        self.assertEqual(status, "partial")

    def test_low_evidence_agreement_downgrades_to_gaps(self) -> None:
        final = _validate_analysis(analysis(), DOCUMENT)

        status, gaps = decide_status(
            final,
            {
                "evidence_agreement": 0.2,
                "category_agreement": True,
                "importance_delta": 0,
            },
            DOCUMENT,
            unanalyzable=[],
            min_agreement=0.5,
        )

        self.assertEqual(status, "verified_with_gaps")
        self.assertIn("independent_passes_cited_different_evidence", gaps)

    def test_importance_disagreement_downgrades_to_gaps(self) -> None:
        final = _validate_analysis(analysis(), DOCUMENT)

        status, gaps = decide_status(
            final,
            {
                "evidence_agreement": 1.0,
                "category_agreement": True,
                "importance_delta": 35,
            },
            DOCUMENT,
            unanalyzable=[],
            min_agreement=0.5,
        )

        self.assertEqual(status, "verified_with_gaps")
        self.assertIn("independent_passes_disagreed_on_importance", gaps)

    def test_unanalyzable_message_downgrades_to_gaps(self) -> None:
        final = _validate_analysis(analysis(), DOCUMENT)

        status, gaps = decide_status(
            final, AGREED, DOCUMENT, unanalyzable=["m3"], min_agreement=0.5
        )

        self.assertEqual(status, "verified_with_gaps")
        self.assertIn("message_too_large_to_analyze", gaps)

    def test_no_surviving_claim_is_rejected(self) -> None:
        final = _validate_analysis(
            analysis(claims=[claim("c1", "m1", "Nothing like this in the source.")]),
            DOCUMENT,
        )

        status, _ = decide_status(
            final, AGREED, DOCUMENT, unanalyzable=[], min_agreement=0.5
        )

        self.assertEqual(status, "rejected")


class SegmentationTests(unittest.TestCase):
    def build(self, count: int, body_chars: int) -> dict:
        return {
            "thread_id": "thread-long",
            "owner_email": "owner@example.test",
            "subject": "Long thread",
            "message_count": count,
            "has_unavailable_attachments": False,
            "participants": [],
            "messages": [
                {
                    "message_id": f"m{index}",
                    "sent_at_utc": f"2026-01-{index + 1:02d}T00:00:00Z",
                    "sender": {"name": "Owner", "address": "owner@example.test"},
                    "subject": "Long thread",
                    "has_attachments": False,
                    "body": "x" * body_chars,
                }
                for index in range(count)
            ],
        }

    def test_short_thread_is_one_segment(self) -> None:
        segments, unanalyzable = segment_document(self.build(3, 100), max_chars=80_000)

        self.assertEqual(len(segments), 1)
        self.assertEqual(unanalyzable, [])
        self.assertIsNone(segments[0]["other_messages_in_thread"])

    def test_long_thread_is_split_and_keeps_every_message(self) -> None:
        document = self.build(20, 3_000)

        segments, unanalyzable = segment_document(document, max_chars=10_000)

        self.assertGreater(len(segments), 1)
        self.assertEqual(unanalyzable, [])
        carried = [
            message["message_id"]
            for segment in segments
            for message in segment["messages"]
        ]
        self.assertEqual(carried, [f"m{index}" for index in range(20)])
        for segment in segments:
            self.assertLessEqual(len(json.dumps(segment, ensure_ascii=False)), 10_000)
            self.assertIsNotNone(segment["other_messages_in_thread"])

    def test_single_message_larger_than_a_request_is_reported_not_truncated(
        self,
    ) -> None:
        document = self.build(3, 100)
        document["messages"][1]["body"] = "y" * 50_000

        segments, unanalyzable = segment_document(document, max_chars=10_000)

        self.assertEqual(unanalyzable, ["m1"])
        carried = [
            message["message_id"]
            for segment in segments
            for message in segment["messages"]
        ]
        self.assertEqual(carried, ["m0", "m2"])
        for segment in segments:
            for message in segment["messages"]:
                self.assertNotIn("y" * 100, message["body"])


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = connect(self.root / "knowledge.db")
        initialize(self.connection)
        source = self.root / "source.json"
        source.write_text(
            json.dumps(
                {
                    "value": [
                        {
                            "id": "m1",
                            "conversationId": "thread-1",
                            "sentDateTime": "2026-01-01T00:00:00Z",
                            "from": {
                                "emailAddress": {
                                    "name": "Owner",
                                    "address": "owner@example.test",
                                }
                            },
                            "subject": "Choose option",
                            "body": {
                                "contentType": "text",
                                "content": "Use option B.",
                            },
                        },
                        {
                            "id": "m2",
                            "conversationId": "thread-1",
                            "sentDateTime": "2026-01-02T00:00:00Z",
                            "from": {
                                "emailAddress": {
                                    "name": "Peer",
                                    "address": "peer@example.test",
                                }
                            },
                            "subject": "Re: Choose option",
                            "body": {
                                "contentType": "text",
                                "content": "Option B succeeded.",
                            },
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        ingest_sources(self.connection, [source])

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def run_pipeline(self, responses: list[dict], **kwargs):
        prompts: list[str] = []
        remaining = list(responses)

        def fake_prompt(*args, **_):
            prompts.append(args[0])
            self.assertEqual(args[1].tools, [])
            output = remaining.pop(0)
            return SimpleNamespace(
                status="finished",
                id=f"run-{len(remaining)}",
                agent_id="agent-test",
                result=json.dumps(output),
                model="test-model",
                duration_ms=1,
            )

        with (
            patch.dict("os.environ", {"CURSOR_API_KEY": "test-key"}),
            patch("email_kb.analysis.Agent.prompt", side_effect=fake_prompt),
        ):
            summary = analyze_database(
                self.connection,
                owner_email="owner@example.test",
                workspace=self.root,
                model="test-model",
                **kwargs,
            )
        return summary, prompts

    def test_parses_fenced_agent_json(self) -> None:
        self.assertEqual(_parse_agent_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_dry_run_needs_no_api_key_and_changes_no_state(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            summary = analyze_database(
                self.connection,
                owner_email="owner@example.test",
                workspace=self.root,
                dry_run=True,
            )

        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["segments_total"], 1)
        self.assertEqual(summary["estimated_cursor_runs"], 3)
        count = self.connection.execute(
            "SELECT COUNT(*) FROM thread_analyses"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_extraction_passes_never_see_each_other(self) -> None:
        _, prompts = self.run_pipeline([analysis(), analysis(), analysis()])

        self.assertEqual(len(prompts), 3)
        pass_a, pass_b, reconcile = prompts
        self.assertNotIn("pass_a", pass_a)
        self.assertNotIn("proposed_analysis", pass_a)
        self.assertEqual(pass_a, pass_b)
        self.assertIn("<pass_a>", reconcile)
        self.assertIn("<pass_b>", reconcile)

    def test_agreeing_passes_produce_verified_thread(self) -> None:
        summary, _ = self.run_pipeline([analysis(), analysis(), analysis()])

        self.assertEqual(summary["verified"], 1)
        row = self.connection.execute(
            """
            SELECT status, importance_score, model_reported_confidence,
                   agreement_score, segment_count, verified_json
            FROM thread_analyses WHERE thread_id = 'thread-1'
            """
        ).fetchone()
        self.assertEqual(row["status"], "verified")
        self.assertEqual(row["importance_score"], 92)
        self.assertAlmostEqual(row["model_reported_confidence"], 0.98)
        self.assertEqual(row["agreement_score"], 1.0)
        self.assertEqual(row["segment_count"], 1)
        verified = json.loads(row["verified_json"])
        self.assertEqual(verified["experience"]["decisions"][0]["claim_id"], "c1")

    def test_disagreeing_passes_are_recorded_as_gaps(self) -> None:
        summary, _ = self.run_pipeline(
            [
                analysis(claims=[claim("c1", "m1", "Use option B.", "decision")]),
                analysis(
                    category="project_update",
                    importance=30,
                    claims=[claim("c1", "m2", "Option B succeeded.", "outcome")],
                ),
                analysis(),
            ]
        )

        self.assertEqual(summary["verified_with_gaps"], 1)
        row = self.connection.execute(
            "SELECT gap_reasons_json FROM thread_analyses WHERE thread_id = 'thread-1'"
        ).fetchone()
        reasons = json.loads(row["gap_reasons_json"])
        self.assertIn("independent_passes_disagreed_on_category", reasons)
        self.assertIn("independent_passes_cited_different_evidence", reasons)
        self.assertIn("independent_passes_disagreed_on_importance", reasons)

    def test_report_separates_extraction_from_reconciliation(self) -> None:
        hallucinated = analysis(
            claims=[
                claim("c1", "m1", "Use option B.", "decision"),
                claim("c2", "m2", "A sentence that is not in any message."),
            ]
        )
        self.run_pipeline([hallucinated, hallucinated, analysis()])

        report = quality_report(self.connection)
        stages = report["evidence_pass_rate_by_stage"]
        self.assertEqual(stages["extraction"]["claims_checked"], 4)
        self.assertEqual(stages["extraction"]["claims_valid"], 2)
        self.assertEqual(stages["extraction"]["evidence_pass_rate"], 0.5)
        self.assertEqual(stages["reconcile"]["evidence_pass_rate"], 1.0)
        self.assertEqual(report["independent_pass_agreement"]["mean"], 1.0)

    def test_verified_thread_is_not_reanalyzed(self) -> None:
        self.run_pipeline([analysis(), analysis(), analysis()])
        summary, prompts = self.run_pipeline([])

        self.assertEqual(summary["threads_considered"], 0)
        self.assertEqual(prompts, [])


if __name__ == "__main__":
    unittest.main()
