from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from email_kb.analysis import (
    _parse_agent_json,
    _validate_corrected_thread,
    analyze_database,
)
from email_kb.database import connect, initialize, quality_report
from email_kb.ingest import ingest_sources


def analysis_result(*, audited: bool) -> dict:
    thread = {
        "thread_id": "thread-1",
        "category": "decision",
        "importance_score": 92,
        "importance_reasons": ["Owner made a decision with a confirmed result."],
        "summary": "The owner selected option B and it succeeded.",
        "claims": [
            {
                "claim_id": "thread-1:c1",
                "type": "decision",
                "text": "The owner selected option B.",
                "message_ids": ["m1"],
                "evidence_quotes": [{"message_id": "m1", "quote": "Use option B."}],
                "confidence": 0.99,
            },
            {
                "claim_id": "thread-1:c2",
                "type": "outcome",
                "text": "Option B succeeded.",
                "message_ids": ["m2"],
                "evidence_quotes": [
                    {"message_id": "m2", "quote": "Option B succeeded."}
                ],
                "confidence": 0.99,
            },
        ],
        "experience": {
            "situation": "A choice between implementation options.",
            "goal": "Choose a successful implementation.",
            "constraints": [],
            "actions": ["Selected option B."],
            "decision": "Use option B.",
            "outcome": "Option B succeeded.",
            "reusable_rule": None,
            "exceptions": [],
        },
        "needs_more_context": False,
        "missing_context": [],
    }
    if audited:
        thread["audit"] = {
            "summary_grounded": True,
            "factual_confidence": 0.98,
            "removed_unsupported_claims": [],
            "added_missed_claims": [],
            "notes": [],
        }
    return {"schema_version": "1.0", "threads": [thread]}


class AnalysisTests(unittest.TestCase):
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

    def test_parses_fenced_agent_json(self) -> None:
        parsed = _parse_agent_json('```json\n{"threads": []}\n```')
        self.assertEqual(parsed, {"threads": []})

    def test_rejects_evidence_not_present_in_source(self) -> None:
        result = analysis_result(audited=True)["threads"][0]
        result["claims"][0]["evidence_quotes"][0]["quote"] = "Invented quote."
        source = {
            "thread_id": "thread-1",
            "messages": [
                {"message_id": "m1", "body": "Use option B."},
                {"message_id": "m2", "body": "Option B succeeded."},
            ],
        }

        corrected, errors = _validate_corrected_thread(result, source)

        self.assertIsNotNone(corrected)
        self.assertTrue(any("quote not found" in error for error in errors))
        self.assertEqual(len(corrected["claims"]), 1)

    def test_dry_run_needs_no_api_key_and_changes_no_analysis_state(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            summary = analyze_database(
                self.connection,
                owner_email="owner@example.test",
                workspace=self.root,
                dry_run=True,
            )

        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["estimated_cursor_runs"], 2)
        count = self.connection.execute(
            "SELECT COUNT(*) FROM thread_analyses"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_full_pipeline_requires_verified_exact_evidence(self) -> None:
        responses = [
            analysis_result(audited=False),
            analysis_result(audited=True),
        ]

        def fake_prompt(*args, **kwargs):
            self.assertEqual(args[1].tools, [])
            output = responses.pop(0)
            return SimpleNamespace(
                status="finished",
                id=f"run-{len(responses)}",
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
            )

        self.assertEqual(summary["verified"], 1)
        row = self.connection.execute(
            """
            SELECT status, importance_score, factual_confidence, verified_json
            FROM thread_analyses
            WHERE thread_id = 'thread-1'
            """
        ).fetchone()
        self.assertEqual(row["status"], "verified")
        self.assertEqual(row["importance_score"], 92)
        self.assertAlmostEqual(row["factual_confidence"], 0.98)
        verified = json.loads(row["verified_json"])
        self.assertEqual(verified["validation"]["evidence_claims_valid"], 2)
        self.assertEqual(
            verified["experience"]["decisions"][0]["claim_id"], "thread-1:c1"
        )
        report = quality_report(self.connection)
        self.assertEqual(report["evidence_claims_checked"], 2)
        self.assertEqual(report["evidence_pass_rate"], 1.0)
        self.assertEqual(report["failed_agent_runs"], 0)

    def test_grounded_result_with_missing_context_is_verified_with_gaps(self) -> None:
        extraction = analysis_result(audited=False)
        verification = analysis_result(audited=True)
        verification["threads"][0]["needs_more_context"] = True
        verification["threads"][0]["missing_context"] = [
            "A later outcome is not present."
        ]
        responses = [extraction, verification]

        def fake_prompt(*args, **kwargs):
            output = responses.pop(0)
            return SimpleNamespace(
                status="finished",
                id=f"run-gap-{len(responses)}",
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
            )

        self.assertEqual(summary["verified_with_gaps"], 1)
        row = self.connection.execute(
            "SELECT status FROM thread_analyses WHERE thread_id = 'thread-1'"
        ).fetchone()
        self.assertEqual(row["status"], "verified_with_gaps")


if __name__ == "__main__":
    unittest.main()
