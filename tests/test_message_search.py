from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from email_kb.database import connect, initialize
from email_kb.ingest import ingest_sources
from email_kb.retrieval import index_messages, read_thread, search_messages


def message(
    email_id: str,
    thread: str,
    subject: str,
    body: str,
    *,
    sender: str = "peer@example.test",
    sent: str = "2026-01-01T00:00:00Z",
) -> dict:
    return {
        "id": email_id,
        "conversationId": thread,
        "sentDateTime": sent,
        "from": {"emailAddress": {"name": "Peer", "address": sender}},
        "toRecipients": [
            {"emailAddress": {"name": "Owner", "address": "owner@example.test"}}
        ],
        "subject": subject,
        "body": {"contentType": "text", "content": body},
    }


class MessageSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = connect(self.root / "knowledge.db")
        initialize(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def load(self, records: list[dict]) -> None:
        source = self.root / "source.json"
        source.write_text(json.dumps({"value": records}), encoding="utf-8")
        ingest_sources(self.connection, [source], force=True)
        index_messages(self.connection)

    def test_mail_is_searchable_with_no_analysis_at_all(self) -> None:
        self.load(
            [
                message(
                    "m1",
                    "t1",
                    "Nightly build failed",
                    "The nightly build failed on Windows with a link error.",
                ),
                message("m2", "t2", "Lunch", "Anyone up for lunch on Friday?"),
            ]
        )

        found = search_messages(self.connection, "nightly build link error")

        analyzed = self.connection.execute(
            "SELECT COUNT(*) FROM thread_analyses"
        ).fetchone()[0]
        self.assertEqual(int(analyzed), 0)
        self.assertEqual([hit["message_id"] for hit in found], ["m1"])

    def test_chinese_mail_is_searchable(self) -> None:
        self.load(
            [
                message("m1", "t1", "夜间构建失败", "夜间构建在 Windows 上失败。"),
                message("m2", "t2", "午餐", "周五一起吃午饭吗？"),
            ]
        )

        found = search_messages(self.connection, "构建失败")

        self.assertEqual([hit["message_id"] for hit in found], ["m1"])

    def test_the_excerpt_is_taken_around_the_match(self) -> None:
        body = ("padding. " * 200) + "the toolchain was pinned here" + (" tail." * 50)
        self.load([message("m1", "t1", "Long", body)])

        excerpt = search_messages(self.connection, "toolchain pinned")[0]["excerpt"]

        self.assertIn("toolchain was pinned", excerpt)
        self.assertTrue(excerpt.startswith("…"))

    def test_search_can_be_narrowed_by_sender_and_date(self) -> None:
        self.load(
            [
                message(
                    "m1",
                    "t1",
                    "Build",
                    "build failed",
                    sender="alice@example.test",
                    sent="2024-01-01T00:00:00Z",
                ),
                message(
                    "m2",
                    "t2",
                    "Build",
                    "build failed",
                    sender="bob@example.test",
                    sent="2026-01-01T00:00:00Z",
                ),
            ]
        )

        by_sender = search_messages(self.connection, "build failed", sender="alice")
        by_date = search_messages(
            self.connection, "build failed", since="2025-01-01T00:00:00Z"
        )

        self.assertEqual([hit["message_id"] for hit in by_sender], ["m1"])
        self.assertEqual([hit["message_id"] for hit in by_date], ["m2"])

    def test_people_are_searchable_not_only_bodies(self) -> None:
        self.load(
            [message("m1", "t1", "Status", "all good", sender="carol@example.test")]
        )

        self.assertEqual(
            [hit["message_id"] for hit in search_messages(self.connection, "carol")],
            ["m1"],
        )

    def test_reindex_does_not_duplicate(self) -> None:
        self.load([message("m1", "t1", "Build", "build failed")])
        index_messages(self.connection)

        self.assertEqual(len(search_messages(self.connection, "build failed")), 1)

    def test_an_empty_query_returns_nothing_rather_than_everything(self) -> None:
        self.load([message("m1", "t1", "Build", "build failed")])

        self.assertEqual(search_messages(self.connection, "   "), [])


class ReadThreadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = connect(self.root / "knowledge.db")
        initialize(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def load(self, records: list[dict]) -> None:
        source = self.root / "source.json"
        source.write_text(json.dumps({"value": records}), encoding="utf-8")
        ingest_sources(self.connection, [source], force=True)

    def test_a_thread_reads_in_the_order_it_happened(self) -> None:
        self.load(
            [
                message("m2", "t1", "Re: Build", "second", sent="2026-01-02T00:00:00Z"),
                message("m1", "t1", "Build", "first", sent="2026-01-01T00:00:00Z"),
            ]
        )

        thread = read_thread(self.connection, "t1")

        self.assertEqual(
            [item["message_id"] for item in thread["messages"]], ["m1", "m2"]
        )
        self.assertEqual(thread["messages_omitted_for_length"], 0)

    def test_a_shortened_thread_says_so(self) -> None:
        self.load(
            [
                message("m1", "t1", "Build", "x" * 5_000),
                message(
                    "m2", "t1", "Re: Build", "y" * 5_000, sent="2026-01-02T00:00:00Z"
                ),
            ]
        )

        thread = read_thread(self.connection, "t1", max_chars=6_000)

        self.assertEqual(thread["message_count"], 2)
        self.assertEqual(len(thread["messages"]), 1)
        self.assertEqual(thread["messages_omitted_for_length"], 1)

    def test_an_unknown_thread_is_empty_not_an_error(self) -> None:
        thread = read_thread(self.connection, "missing")

        self.assertEqual(thread["message_count"], 0)
        self.assertEqual(thread["messages"], [])


if __name__ == "__main__":
    unittest.main()
