from __future__ import annotations

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from email_kb.database import connect, initialize, save_thread_analysis
from email_kb.evaluation import (
    accepted_cards,
    card_coverage,
    dump_toml,
    load_experience_cards,
    load_replay_tasks,
    propose_replay_tasks,
)
from email_kb.feedback import record_feedback, resolve_target
from email_kb.induction import (
    cluster_cases,
    evidence_strength,
    list_rules,
    rule_stats,
    save_rule,
    validate_rules,
)
from email_kb.retrieval import index_knowledge


def claim(claim_id: str, claim_type: str, text: str, message_id: str) -> dict:
    return {
        "claim_id": claim_id,
        "type": claim_type,
        "text": text,
        "message_ids": [message_id],
        "evidence_quotes": [{"message_id": message_id, "quote": text[:20]}],
    }


class CaseFixture:
    """Shared setup. Not a TestCase, so its helpers are not run as tests."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.connection = connect(Path(self.temporary.name) / "knowledge.db")
        initialize(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def save(
        self,
        thread_id: str,
        *,
        claims: list[dict],
        subject: str = "Subject",
        sent: str = "2026-01-01T00:00:00Z",
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO messages (
                email_id, conversation_id, to_recipients_json, cc_recipients_json,
                bcc_recipients_json, reply_to_json, subject, body, clean_body,
                has_attachments, is_read, categories_json, body_sha256, raw_json,
                sent_at_utc
            )
            VALUES (?, ?, '[]', '[]', '[]', '[]', ?, 'b', 'b', 0, 1, '[]', 'x',
                    '{}', ?)
            """,
            (f"m-{thread_id}", thread_id, subject, sent),
        )
        save_thread_analysis(
            self.connection,
            thread_id=thread_id,
            source_fingerprint="fingerprint",
            status="verified",
            importance_score=70,
            model_reported_confidence=0.9,
            agreement_score=1.0,
            gap_reasons=[],
            segment_count=1,
            proposals=None,
            verified={
                "subject": subject,
                "summary": subject,
                "participants": [],
                "claims": claims,
            },
        )
        self.connection.commit()


class ClusterTests(CaseFixture, unittest.TestCase):
    def test_repeated_situations_become_one_cluster(self) -> None:
        for index in range(3):
            self.save(
                f"audit-{index}",
                subject="Automated licence audit findings",
                claims=[
                    claim(
                        "c1",
                        "problem",
                        "An automated licence audit flagged missing headers.",
                        f"m-audit-{index}",
                    ),
                    claim(
                        "c2",
                        "action",
                        "Checked one flagged file by hand.",
                        f"m-audit-{index}",
                    ),
                ],
            )
        index_knowledge(self.connection)

        clusters = cluster_cases(self.connection)

        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["thread_ids"], ["audit-0", "audit-1", "audit-2"])

    def test_cases_sharing_only_vocabulary_reach_the_model_together(self) -> None:
        # These three share a subject and most words but have different root
        # causes. Grouping them is correct: separating mechanism from wording
        # is the induction step's job, and a group it never sees cannot be
        # split.
        for thread_id, problem in (
            ("toolchain", "The nightly build failed on Windows with a link error."),
            ("disk", "The nightly build failed on Windows, the agent had no disk."),
            ("flake", "The nightly build failed on Windows under parallel runs."),
        ):
            self.save(
                thread_id,
                subject="Nightly build failure",
                claims=[claim("c1", "problem", problem, f"m-{thread_id}")],
            )
        index_knowledge(self.connection)

        clusters = cluster_cases(self.connection)

        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["thread_ids"], ["disk", "flake", "toolchain"])
        self.assertGreater(clusters[0]["similarity"]["min"], 0.2)

    def test_unrelated_cases_are_not_grouped(self) -> None:
        self.save(
            "build",
            subject="Nightly build failure",
            claims=[
                claim(
                    "c1",
                    "problem",
                    "The nightly build failed on Windows with a link error.",
                    "m-build",
                )
            ],
        )
        self.save(
            "travel",
            subject="Conference travel approval",
            claims=[
                claim(
                    "c1",
                    "problem",
                    "Travel approval for the conference was still pending.",
                    "m-travel",
                )
            ],
        )
        index_knowledge(self.connection)

        clusters = cluster_cases(self.connection)

        self.assertEqual(clusters, [])

    def test_a_lone_stated_rule_is_still_proposed(self) -> None:
        self.save(
            "lone",
            subject="How I handle flaky tests",
            claims=[
                claim(
                    "c1",
                    "problem",
                    "An integration test became flaky under parallel runs.",
                    "m-lone",
                ),
                claim(
                    "c2",
                    "reusable_rule",
                    "Quarantine a flaky test before investigating it.",
                    "m-lone",
                ),
            ],
        )
        index_knowledge(self.connection)

        clusters = cluster_cases(self.connection)

        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["reason"], "rule_stated_outright")

    def test_a_lone_case_without_a_rule_is_not_proposed(self) -> None:
        self.save(
            "lone",
            subject="One off question",
            claims=[claim("c1", "problem", "A one off question came up.", "m-lone")],
        )
        index_knowledge(self.connection)

        self.assertEqual(cluster_cases(self.connection), [])


CLUSTER = {"cluster_id": "abc123", "thread_ids": ["t1", "t2"]}


class RuleValidationTests(unittest.TestCase):
    def test_rule_citing_a_thread_outside_its_cluster_is_dropped(self) -> None:
        result = {
            "rules": [
                {
                    "rule_id": "r1",
                    "title": "T",
                    "situation": "S",
                    "actions": ["A"],
                    "supporting_thread_ids": ["t1", "t9"],
                }
            ]
        }

        rules, errors = validate_rules(result, CLUSTER)

        self.assertEqual(rules, [])
        self.assertTrue(any("outside its cluster" in error for error in errors))

    def test_rule_without_support_is_dropped(self) -> None:
        result = {
            "rules": [
                {
                    "rule_id": "r1",
                    "title": "T",
                    "situation": "S",
                    "actions": ["A"],
                    "supporting_thread_ids": [],
                }
            ]
        }

        rules, errors = validate_rules(result, CLUSTER)

        self.assertEqual(rules, [])
        self.assertTrue(any("no supporting thread" in error for error in errors))

    def test_valid_rule_is_namespaced_by_cluster(self) -> None:
        result = {
            "rules": [
                {
                    "rule_id": "r1",
                    "title": "T",
                    "situation": "S",
                    "actions": ["A", " "],
                    "supporting_thread_ids": ["t1", "t1"],
                    "contradicting_thread_ids": ["t2"],
                }
            ]
        }

        rules, errors = validate_rules(result, CLUSTER)

        self.assertEqual(errors, [])
        self.assertEqual(rules[0]["rule_id"], "abc123:r1")
        self.assertEqual(rules[0]["actions"], ["A"])
        self.assertEqual(rules[0]["supporting_thread_ids"], ["t1"])

    def test_one_cluster_may_yield_several_mechanisms(self) -> None:
        result = {
            "rules": [
                {
                    "rule_id": "toolchain",
                    "title": "T1",
                    "situation": "S1",
                    "actions": ["A1"],
                    "supporting_thread_ids": ["t1"],
                },
                {
                    "rule_id": "disk",
                    "title": "T2",
                    "situation": "S2",
                    "actions": ["A2"],
                    "supporting_thread_ids": ["t2"],
                },
            ]
        }

        rules, errors = validate_rules(result, CLUSTER)

        self.assertEqual(errors, [])
        self.assertEqual(len(rules), 2)


class EvidenceStrengthTests(CaseFixture, unittest.TestCase):
    def test_pattern_without_outcomes_is_labelled_as_such(self) -> None:
        for index in range(2):
            self.save(
                f"t{index}",
                claims=[
                    claim("c1", "problem", "A problem occurred.", f"m-t{index}"),
                    claim("c2", "action", "Something was tried.", f"m-t{index}"),
                ],
            )
        index_knowledge(self.connection)

        strength = evidence_strength(
            self.connection,
            {"supporting_thread_ids": ["t0", "t1"], "contradicting_thread_ids": []},
        )

        self.assertEqual(strength["supporting_cases"], 2)
        self.assertEqual(strength["cases_with_confirmed_outcome"], 0)
        self.assertEqual(strength["basis"], "pattern_without_recorded_outcome")

    def test_confirmed_outcomes_are_counted_from_the_database(self) -> None:
        self.save(
            "t0",
            claims=[
                claim("c1", "problem", "A problem occurred.", "m-t0"),
                claim("c2", "action", "Something was tried.", "m-t0"),
                claim("c3", "outcome", "It worked.", "m-t0"),
            ],
        )
        index_knowledge(self.connection)

        strength = evidence_strength(
            self.connection,
            {"supporting_thread_ids": ["t0"], "contradicting_thread_ids": ["t1"]},
        )

        self.assertEqual(strength["cases_with_confirmed_outcome"], 1)
        self.assertEqual(strength["basis"], "confirmed_outcomes")
        self.assertTrue(strength["has_counterexample"])


class RuleReviewTests(CaseFixture, unittest.TestCase):
    def induce(self, rule_id: str = "abc:r1") -> None:
        self.save(
            "t0",
            claims=[
                claim("c1", "problem", "A build failed.", "m-t0"),
                claim("c2", "action", "Pinned the toolchain.", "m-t0"),
                claim("c3", "outcome", "It passed again.", "m-t0"),
            ],
        )
        index_knowledge(self.connection)
        rule = {
            "rule_id": rule_id,
            "cluster_id": "abc",
            "title": "Pin the toolchain first",
            "situation": "A build fails right after a change lands.",
            "trigger": "A build breaks near a change.",
            "actions": ["Pin the toolchain.", "Rerun."],
            "rationale": None,
            "exceptions": [],
            "failure_conditions": [],
            "supporting_thread_ids": ["t0"],
            "contradicting_thread_ids": [],
        }
        save_rule(
            self.connection,
            rule,
            evidence_strength(self.connection, rule),
            model="test-model",
        )

    def test_new_rules_start_unreviewed(self) -> None:
        self.induce()

        unreviewed = list_rules(self.connection, status="unreviewed")

        self.assertEqual(len(unreviewed), 1)
        self.assertIsNone(unreviewed[0]["verdict"])
        self.assertEqual(rule_stats(self.connection)["rules_accepted"], 0)

    def test_a_rule_id_resolves_to_a_rule_not_a_claim(self) -> None:
        self.induce()

        self.assertEqual(resolve_target(self.connection, "abc:r1"), "rule")
        self.assertEqual(resolve_target(self.connection, "t0:c1"), "claim")
        self.assertEqual(resolve_target(self.connection, "t0"), "case")

    def test_accepting_a_rule_makes_it_stated_experience(self) -> None:
        self.induce()

        result = record_feedback(self.connection, target_id="abc:r1", verdict="useful")

        self.assertEqual(result["target_kind"], "rule")
        self.assertEqual(result["effect"], "accepted_as_stated_experience")
        self.assertEqual(len(list_rules(self.connection, status="accepted")), 1)
        self.assertEqual(len(list_rules(self.connection, status="unreviewed")), 0)
        self.assertEqual(rule_stats(self.connection)["rules_accepted"], 1)

    def test_only_accepted_rules_become_cards(self) -> None:
        self.induce()
        self.assertEqual(accepted_cards(self.connection), [])

        record_feedback(self.connection, target_id="abc:r1", verdict="useful")

        cards = accepted_cards(self.connection)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "Pin the toolchain first")

    def test_unreviewed_cards_are_not_reported_as_a_baseline(self) -> None:
        self.induce()

        result = card_coverage(
            self.connection,
            [{"id": "x", "title": "T", "situation": "A build failed.", "actions": []}],
            reviewed=False,
        )

        self.assertFalse(result["baseline_valid"])
        self.assertIn("not a baseline", result["baseline_note"])


class ReplayGenerationTests(CaseFixture, unittest.TestCase):
    def test_tasks_come_only_from_cases_with_a_recorded_result(self) -> None:
        self.save(
            "solved",
            subject="Nightly build failure",
            claims=[
                claim("c1", "problem", "The nightly build failed.", "m-solved"),
                claim("c2", "action", "Pinned the toolchain.", "m-solved"),
                claim("c3", "outcome", "The build passed again.", "m-solved"),
            ],
        )
        self.save(
            "open",
            subject="Unresolved failure",
            claims=[
                claim("c1", "problem", "Something failed.", "m-open"),
                claim("c2", "action", "Asked the owning team.", "m-open"),
            ],
        )
        index_knowledge(self.connection)

        tasks = propose_replay_tasks(self.connection)

        self.assertEqual([task["source_thread_id"] for task in tasks], ["solved"])
        self.assertEqual(tasks[0]["expected_elements"], ["Pinned the toolchain."])
        self.assertEqual(tasks[0]["asked_at"], "2026-01-01T00:00:00Z")

    def test_generated_tasks_pass_the_loader(self) -> None:
        self.save(
            "solved",
            claims=[
                claim("c1", "problem", "The nightly build failed.", "m-solved"),
                claim("c2", "action", "Pinned the toolchain.", "m-solved"),
                claim("c3", "outcome", "The build passed again.", "m-solved"),
            ],
        )
        index_knowledge(self.connection)
        path = Path(self.temporary.name) / "replay.toml"

        path.write_text(
            dump_toml(
                propose_replay_tasks(self.connection), key="task", header="# generated"
            ),
            encoding="utf-8",
        )

        self.assertEqual(len(load_replay_tasks(path)), 1)


class TomlWriterTests(unittest.TestCase):
    def test_awkward_text_survives_a_round_trip(self) -> None:
        entries = [
            {
                "id": "quotes",
                "title": 'He said "pin it" and left',
                "situation": 'A path like C:\\builds\\"nightly" broke.\nLine two.',
                "actions": ['Run "make -j8"', "Check C:\\temp"],
            },
            {
                "id": "delimiters",
                "title": 'Ends with a quote"',
                "situation": 'Contains """ a closing delimiter run.',
                "actions": ["x"],
            },
            {
                "id": "chinese",
                "title": "构建失败先锁定工具链",
                "situation": "夜间构建在 Windows 上失败，\n且改动刚落地。",
                "actions": ["锁定工具链版本", "只改 pin 重跑一次"],
            },
            {
                "id": "control",
                "title": "Tabs\tand returns\r",
                "situation": "Trailing backslash \\",
                "actions": ["y"],
            },
        ]

        rendered = dump_toml(entries, key="card", header="# header")
        parsed = tomllib.loads(rendered)["card"]

        self.assertEqual(len(parsed), len(entries))
        for original, restored in zip(entries, parsed, strict=True):
            self.assertEqual(restored, original, msg=json.dumps(original))

    def test_empty_fields_are_omitted_rather_than_written_null(self) -> None:
        rendered = dump_toml(
            [
                {
                    "id": "a",
                    "title": "T",
                    "situation": "S",
                    "actions": ["x"],
                    "rationale": None,
                    "exceptions": [],
                    "trigger": "",
                }
            ],
            key="card",
            header="# header",
        )

        self.assertNotIn("rationale", rendered)
        self.assertNotIn("exceptions", rendered)
        self.assertNotIn("trigger", rendered)
        self.assertEqual(len(load_experience_cards_from_text(rendered)), 1)


def load_experience_cards_from_text(text: str) -> list[dict]:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".toml", encoding="utf-8", delete=False
    ) as handle:
        handle.write(text)
        path = Path(handle.name)
    try:
        return load_experience_cards(path)
    finally:
        path.unlink()


if __name__ == "__main__":
    unittest.main()
