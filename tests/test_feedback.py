from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from email_kb.database import connect, initialize, save_thread_analysis
from email_kb.feedback import feedback_stats, record_feedback, review_queue
from email_kb.retrieval import (
    check_prior_attempts,
    find_similar_cases,
    get_case,
    index_knowledge,
)


def claim(claim_id: str, claim_type: str, text: str) -> dict:
    return {
        "claim_id": claim_id,
        "type": claim_type,
        "text": text,
        "message_ids": ["m1"],
        "evidence_quotes": [{"message_id": "m1", "quote": text[:20]}],
    }


class FeedbackTests(unittest.TestCase):
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
        for thread_id, importance in (("thread-1", 90), ("thread-2", 40)):
            save_thread_analysis(
                self.connection,
                thread_id=thread_id,
                source_fingerprint="fingerprint",
                status="verified",
                importance_score=importance,
                model_reported_confidence=0.9,
                agreement_score=1.0,
                gap_reasons=[],
                segment_count=1,
                proposals=None,
                verified={
                    "subject": f"Nightly build failure {thread_id}",
                    "summary": "The nightly build failed.",
                    "participants": [],
                    "claims": [
                        claim("c1", "problem", "The nightly build failed on Windows."),
                        claim("c2", "action", "Pinned the toolchain version."),
                        claim("c3", "outcome", "The nightly build passed again."),
                    ],
                },
            )
        index_knowledge(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_queue_shows_unreviewed_claims_with_their_evidence(self) -> None:
        queue = review_queue(self.connection, limit=3)

        self.assertEqual(len(queue), 3)
        self.assertTrue(all(item["thread_id"] == "thread-1" for item in queue))
        self.assertEqual(queue[0]["evidence"][0]["message_id"], "m1")

    def test_reviewed_claims_leave_the_queue(self) -> None:
        record_feedback(self.connection, target_id="thread-1:c1", verdict="useful")

        remaining = {item["target_id"] for item in review_queue(self.connection)}

        self.assertNotIn("thread-1:c1", remaining)
        self.assertIn("thread-1:c2", remaining)

    def test_unknown_target_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            record_feedback(self.connection, target_id="thread-9:c1", verdict="useful")

    def test_invalid_verdict_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            record_feedback(self.connection, target_id="thread-1:c1", verdict="maybe")

    def test_useful_only_changes_ranking(self) -> None:
        before = find_similar_cases(self.connection, "nightly build failed")

        record_feedback(self.connection, target_id="thread-2", verdict="useful")
        after = find_similar_cases(self.connection, "nightly build failed")

        self.assertEqual(len(before), len(after))
        self.assertEqual(after[0]["thread_id"], "thread-2")
        self.assertEqual(after[0]["ranking"]["components"]["owner_feedback"], 2.0)
        # The claim itself is untouched; only where it ranks has changed.
        self.assertEqual(
            {item["thread_id"] for item in before},
            {item["thread_id"] for item in after},
        )

    def test_wrong_hides_a_case_from_agents(self) -> None:
        record_feedback(
            self.connection,
            target_id="thread-1",
            verdict="wrong",
            note="This misreads the thread.",
        )

        found = find_similar_cases(self.connection, "nightly build failed")

        self.assertEqual([item["thread_id"] for item in found], ["thread-2"])

    def test_wrong_on_a_case_also_hides_its_claims(self) -> None:
        record_feedback(self.connection, target_id="thread-1", verdict="wrong")

        found = check_prior_attempts(self.connection, "pinned the toolchain version")

        self.assertTrue(all(item["thread_id"] == "thread-2" for item in found))

    def test_rejected_case_is_still_openable_by_id(self) -> None:
        record_feedback(self.connection, target_id="thread-1", verdict="wrong")

        case = get_case(self.connection, "thread-1")

        self.assertIsNotNone(case)
        self.assertEqual(case["owner_feedback"], "wrong")
        self.assertTrue(any("misreading" in note for note in case["advisories"]))

    def test_outdated_keeps_knowledge_but_flags_it(self) -> None:
        record_feedback(self.connection, target_id="thread-1", verdict="outdated")

        found = find_similar_cases(self.connection, "nightly build failed")
        thread_one = next(item for item in found if item["thread_id"] == "thread-1")

        self.assertTrue(
            any("no longer applicable" in note for note in thread_one["advisories"])
        )
        self.assertEqual(thread_one["ranking"]["components"]["owner_feedback"], -1.0)

    def test_latest_verdict_supersedes_earlier_ones(self) -> None:
        record_feedback(self.connection, target_id="thread-1", verdict="wrong")
        record_feedback(self.connection, target_id="thread-1", verdict="useful")

        found = {
            item["thread_id"]
            for item in find_similar_cases(self.connection, "nightly build failed")
        }

        self.assertIn("thread-1", found)
        history = self.connection.execute(
            "SELECT COUNT(*) FROM feedback WHERE target_id = 'thread-1'"
        ).fetchone()[0]
        self.assertEqual(int(history), 2)

    def test_feedback_survives_a_reindex(self) -> None:
        record_feedback(self.connection, target_id="thread-1", verdict="wrong")

        index_knowledge(self.connection)
        found = find_similar_cases(self.connection, "nightly build failed")

        self.assertEqual([item["thread_id"] for item in found], ["thread-2"])

    def test_stats_track_review_progress(self) -> None:
        record_feedback(self.connection, target_id="thread-1:c1", verdict="useful")

        stats = feedback_stats(self.connection)

        self.assertEqual(stats["claims_reviewed"], 1)
        self.assertEqual(stats["claims_unreviewed"], 5)
        self.assertEqual(stats["verdicts"]["claim.useful"], 1)


if __name__ == "__main__":
    unittest.main()
