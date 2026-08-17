from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from email_kb.database import connect, initialize, save_thread_analysis
from email_kb.evaluation import (
    EvaluationFileError,
    build_context,
    card_coverage,
    element_coverage,
    load_experience_cards,
    load_replay_tasks,
    replay_prompts,
    score_replay,
    summarize_replay,
)
from email_kb.retrieval import index_knowledge

EXAMPLES = Path(__file__).resolve().parent.parent / "evaluation"


def claim(claim_id: str, claim_type: str, text: str) -> dict:
    return {
        "claim_id": claim_id,
        "type": claim_type,
        "text": text,
        "message_ids": ["m1"],
        "evidence_quotes": [{"message_id": "m1", "quote": text[:20]}],
    }


class FileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_shipped_examples_are_valid(self) -> None:
        cards = load_experience_cards(EXAMPLES / "experience_cards.example.toml")
        tasks = load_replay_tasks(EXAMPLES / "decision_replay.example.toml")

        self.assertEqual(len(cards), 2)
        self.assertEqual(len(tasks), 1)
        self.assertIn("expected_elements", tasks[0])

    def test_missing_required_field_is_reported_with_position(self) -> None:
        path = self.root / "cards.toml"
        path.write_text(
            '[[card]]\nid = "a"\ntitle = "t"\nsituation = "s"\n', encoding="utf-8"
        )

        with self.assertRaises(EvaluationFileError) as raised:
            load_experience_cards(path)

        self.assertIn("card[0]", str(raised.exception))
        self.assertIn("actions", str(raised.exception))

    def test_duplicate_ids_are_rejected(self) -> None:
        path = self.root / "tasks.toml"
        entry = (
            '[[task]]\nid = "a"\nsituation = "s"\nasked_at = "2026-01-01"\n'
            'expected_elements = ["x"]\n'
        )
        path.write_text(entry * 2, encoding="utf-8")

        with self.assertRaises(EvaluationFileError):
            load_replay_tasks(path)

    def test_unknown_field_is_rejected_rather_than_ignored(self) -> None:
        path = self.root / "tasks.toml"
        path.write_text(
            '[[task]]\nid = "a"\nsituation = "s"\nasked_at = "2026-01-01"\n'
            'expected_elements = ["x"]\nanswer = "leaked"\n',
            encoding="utf-8",
        )

        with self.assertRaises(EvaluationFileError) as raised:
            load_replay_tasks(path)

        self.assertIn("answer", str(raised.exception))

    def test_missing_file_is_reported_clearly(self) -> None:
        with self.assertRaises(EvaluationFileError):
            load_replay_tasks(self.root / "absent.toml")


class ScoringTests(unittest.TestCase):
    def test_coverage_counts_only_elements_present_in_the_answer(self) -> None:
        result = element_coverage(
            ["pin the toolchain", "compare the machine images"],
            "You should pin the toolchain to the last passing version.",
        )

        self.assertEqual(result["covered"], ["pin the toolchain"])
        self.assertEqual(result["missing"], ["compare the machine images"])
        self.assertEqual(result["coverage"], 0.5)

    def test_chinese_elements_are_scored(self) -> None:
        result = element_coverage(
            ["锁定工具链版本"], "建议先锁定工具链版本再排查代码改动。"
        )

        self.assertEqual(result["coverage"], 1.0)

    def test_delta_is_the_difference_the_knowledge_made(self) -> None:
        task = {
            "id": "t1",
            "expected_elements": ["pin the toolchain", "compare machine images"],
        }

        score = score_replay(
            task,
            without="Bisect the recent change.",
            with_knowledge="Pin the toolchain, then compare machine images.",
        )

        self.assertEqual(score["without_knowledge"]["coverage"], 0.0)
        self.assertEqual(score["with_knowledge"]["coverage"], 1.0)
        self.assertEqual(score["coverage_delta"], 1.0)

    def test_summary_counts_improved_and_regressed_tasks(self) -> None:
        summary = summarize_replay(
            [
                {
                    "coverage_delta": 0.5,
                    "without_knowledge": {"coverage": 0.5},
                    "with_knowledge": {"coverage": 1.0},
                },
                {
                    "coverage_delta": -0.25,
                    "without_knowledge": {"coverage": 0.5},
                    "with_knowledge": {"coverage": 0.25},
                },
            ]
        )

        self.assertEqual(summary["tasks"], 2)
        self.assertEqual(summary["tasks_improved"], 1)
        self.assertEqual(summary["tasks_regressed"], 1)
        self.assertEqual(summary["mean_coverage_delta"], 0.125)


class ReplayContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.connection = connect(Path(self.temporary.name) / "knowledge.db")
        initialize(self.connection)
        for thread_id, sent in (
            ("thread-early", "2024-01-01T00:00:00Z"),
            ("thread-late", "2024-06-01T00:00:00Z"),
        ):
            self.connection.execute(
                """
                INSERT INTO messages (
                    email_id, conversation_id, to_recipients_json,
                    cc_recipients_json, bcc_recipients_json, reply_to_json,
                    subject, body, clean_body, has_attachments, is_read,
                    categories_json, body_sha256, raw_json, sent_at_utc
                )
                VALUES (?, ?, '[]', '[]', '[]', '[]', 'Build failure', 'b', 'b',
                        0, 1, '[]', 'x', '{}', ?)
                """,
                (f"m-{thread_id}", thread_id, sent),
            )
            save_thread_analysis(
                self.connection,
                thread_id=thread_id,
                source_fingerprint="fingerprint",
                status="verified",
                importance_score=80,
                model_reported_confidence=0.9,
                agreement_score=1.0,
                gap_reasons=[],
                segment_count=1,
                proposals=None,
                verified={
                    "subject": "Nightly build failure",
                    "summary": "The nightly build failed on Windows.",
                    "participants": [],
                    "claims": [
                        {
                            "claim_id": "c1",
                            "type": "problem",
                            "text": "The nightly build failed on Windows.",
                            "message_ids": [f"m-{thread_id}"],
                            "evidence_quotes": [
                                {
                                    "message_id": f"m-{thread_id}",
                                    "quote": "nightly build failed",
                                }
                            ],
                        },
                        {
                            "claim_id": "c2",
                            "type": "reusable_rule",
                            "text": "Pin the toolchain before bisecting.",
                            "message_ids": [f"m-{thread_id}"],
                            "evidence_quotes": [
                                {
                                    "message_id": f"m-{thread_id}",
                                    "quote": "pin the toolchain",
                                }
                            ],
                        },
                    ],
                },
            )
        self.connection.commit()
        index_knowledge(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_knowledge_after_the_cutoff_is_withheld(self) -> None:
        task = {
            "id": "t1",
            "situation": "The nightly build failed on Windows.",
            "asked_at": "2024-03-01T00:00:00Z",
            "expected_elements": ["pin the toolchain"],
        }

        context = build_context(self.connection, task)

        self.assertEqual(
            [item["thread_id"] for item in context["similar_cases"]], ["thread-early"]
        )
        self.assertTrue(
            all(
                item["thread_id"] == "thread-early"
                for item in context["applicable_rules"]
            )
        )

    def test_without_a_cutoff_everything_is_visible(self) -> None:
        task = {
            "id": "t1",
            "situation": "The nightly build failed on Windows.",
            "asked_at": "2030-01-01T00:00:00Z",
            "expected_elements": ["pin the toolchain"],
        }

        context = build_context(self.connection, task)

        self.assertEqual(len(context["similar_cases"]), 2)

    def test_only_the_informed_prompt_carries_knowledge(self) -> None:
        task = {
            "id": "t1",
            "situation": "The nightly build failed on Windows.",
            "asked_at": "2030-01-01T00:00:00Z",
            "expected_elements": ["pin the toolchain"],
        }

        prompts = replay_prompts(self.connection, task)

        self.assertNotIn("prior_experience", prompts["without_knowledge"])
        self.assertIn("<prior_experience>", prompts["with_knowledge"])
        self.assertIn("outcome_state", prompts["with_knowledge"])
        self.assertEqual(prompts["retrieved_counts"]["similar_cases"], 2)

    def test_card_coverage_reports_candidates_without_judging(self) -> None:
        cards = [
            {
                "id": "pin-toolchain",
                "title": "Pin the toolchain first",
                "situation": "A nightly build fails on Windows after a change.",
                "actions": ["Pin the toolchain before bisecting."],
            },
            {
                "id": "unrelated",
                "title": "Something never emailed",
                "situation": "Deciding whether to attend a conference.",
                "actions": ["Weigh the travel cost."],
            },
        ]

        result = card_coverage(self.connection, cards)

        self.assertEqual(result["cards"], 2)
        found, missing = result["results"]
        self.assertFalse(found["found_nothing"])
        self.assertEqual(found["candidate_rules"][0]["action_overlap"]["coverage"], 1.0)
        self.assertTrue(missing["found_nothing"])
        self.assertEqual(result["cards_with_no_candidate"], 1)


if __name__ == "__main__":
    unittest.main()
