from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from email_kb.database import connect, database_stats, initialize, iter_thread_ids
from email_kb.ingest import (
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    ingest_sources,
    is_cloud_placeholder,
    survey_sources,
)


class IngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = connect(self.root / "knowledge.db")
        initialize(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_imports_graph_page_and_preserves_raw_body(self) -> None:
        source = self.root / "page.json"
        source.write_text(
            json.dumps(
                {
                    "@odata.context": "test",
                    "value": [
                        {
                            "id": "m1",
                            "internetMessageId": "<m1@example.test>",
                            "conversationId": "thread-1",
                            "sentDateTime": "2026-01-01T00:00:00Z",
                            "from": {
                                "emailAddress": {
                                    "name": "Owner",
                                    "address": "owner@example.test",
                                }
                            },
                            "toRecipients": [
                                {
                                    "emailAddress": {
                                        "name": "Peer",
                                        "address": "peer@example.test",
                                    }
                                }
                            ],
                            "subject": "Decision",
                            "body": {
                                "contentType": "html",
                                "content": (
                                    "<p>Use option B.</p>"
                                    "<script>alert('ignore')</script>"
                                ),
                            },
                            "bodyPreview": "Use option B.",
                            "hasAttachments": False,
                            "isRead": True,
                            "categories": ["Project"],
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
                            "subject": "Re: Decision",
                            "body": {
                                "contentType": "text",
                                "content": "Option B succeeded.",
                            },
                            "hasAttachments": True,
                            "isRead": True,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = ingest_sources(self.connection, [source])

        self.assertEqual(result["messages_imported"], 2)
        self.assertEqual(
            database_stats(self.connection),
            {
                "messages": 2,
                "threads": 1,
                "sources": 1,
                "verified_threads": 0,
            },
        )
        row = self.connection.execute(
            "SELECT body, clean_body, raw_json FROM messages WHERE email_id = 'm1'"
        ).fetchone()
        self.assertIn("<script>", row["body"])
        self.assertEqual(row["clean_body"], "Use option B.")
        self.assertIn('"contentType":"html"', row["raw_json"])

    def test_imports_csv_and_skips_unchanged_source(self) -> None:
        source = self.root / "page.csv"
        with source.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "email_id",
                    "conversation_id",
                    "sender_address",
                    "subject",
                    "body",
                    "body_type",
                    "has_attachments",
                    "is_read",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "email_id": "csv-1",
                    "conversation_id": "csv-thread",
                    "sender_address": "robot@example.test",
                    "subject": "Build result",
                    "body": "Build passed.",
                    "body_type": "text",
                    "has_attachments": "False",
                    "is_read": "True",
                }
            )

        first = ingest_sources(self.connection, [source])
        second = ingest_sources(self.connection, [source])

        self.assertEqual(first["messages_imported"], 1)
        self.assertEqual(second["files_skipped"], 1)
        row = self.connection.execute(
            "SELECT has_attachments, is_read FROM messages WHERE email_id = 'csv-1'"
        ).fetchone()
        self.assertEqual((row["has_attachments"], row["is_read"]), (0, 1))

    def test_changed_source_removes_messages_no_longer_present(self) -> None:
        source = self.root / "changing.json"
        source.write_text(
            json.dumps(
                {
                    "value": [
                        {
                            "id": "old-1",
                            "conversationId": "thread-1",
                            "body": {"contentType": "text", "content": "Keep."},
                        },
                        {
                            "id": "old-2",
                            "conversationId": "thread-2",
                            "body": {"contentType": "text", "content": "Remove."},
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        ingest_sources(self.connection, [source])
        source.write_text(
            json.dumps(
                {
                    "value": [
                        {
                            "id": "old-1",
                            "conversationId": "thread-1",
                            "body": {"contentType": "text", "content": "Keep."},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        ingest_sources(self.connection, [source])

        ids = {
            row["email_id"]
            for row in self.connection.execute("SELECT email_id FROM messages")
        }
        self.assertEqual(ids, {"old-1"})

    def test_csv_never_overwrites_richer_raw_json_record(self) -> None:
        raw_source = self.root / "raw.json"
        raw_source.write_text(
            json.dumps(
                {
                    "value": [
                        {
                            "id": "shared-1",
                            "conversationId": "thread-1",
                            "body": {
                                "contentType": "text",
                                "content": "Complete RawJSON body.",
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        csv_source = self.root / "browse.csv"
        with csv_source.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["email_id", "conversation_id", "body", "body_type"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "email_id": "shared-1",
                    "conversation_id": "thread-1",
                    "body": "CSV body must not replace RawJSON.",
                    "body_type": "text",
                }
            )

        ingest_sources(self.connection, [raw_source])
        ingest_sources(self.connection, [csv_source])

        body = self.connection.execute(
            "SELECT body FROM messages WHERE email_id = 'shared-1'"
        ).fetchone()["body"]
        self.assertEqual(body, "Complete RawJSON body.")

    def test_thread_order_prioritizes_owner_activity_then_conversation_depth(
        self,
    ) -> None:
        source = self.root / "priority.json"
        source.write_text(
            json.dumps(
                {
                    "value": [
                        {
                            "id": "auto",
                            "conversationId": "auto-thread",
                            "sentDateTime": "2026-03-03T00:00:00Z",
                            "from": {
                                "emailAddress": {
                                    "address": "robot@example.test",
                                }
                            },
                            "body": {"contentType": "text", "content": "Notice"},
                        },
                        {
                            "id": "multi-1",
                            "conversationId": "multi-thread",
                            "sentDateTime": "2026-03-01T00:00:00Z",
                            "from": {
                                "emailAddress": {
                                    "address": "peer@example.test",
                                }
                            },
                            "body": {"contentType": "text", "content": "Question"},
                        },
                        {
                            "id": "multi-2",
                            "conversationId": "multi-thread",
                            "sentDateTime": "2026-03-02T00:00:00Z",
                            "from": {
                                "emailAddress": {
                                    "address": "peer@example.test",
                                }
                            },
                            "body": {"contentType": "text", "content": "Follow-up"},
                        },
                        {
                            "id": "owner",
                            "conversationId": "owner-thread",
                            "sentDateTime": "2026-01-01T00:00:00Z",
                            "from": {
                                "emailAddress": {
                                    "address": "owner@example.test",
                                }
                            },
                            "body": {"contentType": "text", "content": "My decision"},
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        ingest_sources(self.connection, [source])

        ordered = list(
            iter_thread_ids(
                self.connection,
                owner_email="owner@example.test",
            )
        )

        self.assertEqual(
            ordered,
            ["owner-thread", "multi-thread", "auto-thread"],
        )


class SourceSurveyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, name: str, content: str = "{}") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_counts_only_importable_files(self) -> None:
        self.write("a.json")
        self.write("nested/b.csv", "subject\n")
        self.write("notes.txt", "ignore me")

        survey = survey_sources([self.root])

        self.assertEqual(survey["files_supported"], 2)
        self.assertEqual(survey["files_unsupported"], 1)
        self.assertTrue(survey["ready"])
        self.assertEqual(survey["advice"], [])

    def test_a_missing_path_is_reported_not_raised(self) -> None:
        survey = survey_sources([self.root / "absent"])

        self.assertFalse(survey["ready"])
        self.assertEqual(len(survey["missing_paths"]), 1)
        self.assertTrue(any("do not exist" in note for note in survey["advice"]))

    def test_an_empty_folder_says_nothing_was_found(self) -> None:
        survey = survey_sources([self.root])

        self.assertFalse(survey["ready"])
        self.assertTrue(any("No .json or .csv" in note for note in survey["advice"]))

    def test_cloud_placeholders_are_found_before_any_download(self) -> None:
        self.write("a.json")
        placeholder = self.write("b.json")

        with patch(
            "email_kb.ingest.is_cloud_placeholder",
            side_effect=lambda path: Path(path) == placeholder,
        ):
            survey = survey_sources([self.root])

        self.assertEqual(survey["files_not_downloaded"], 1)
        self.assertEqual(survey["not_downloaded_examples"], [str(placeholder)])
        self.assertFalse(survey["ready"])
        self.assertTrue(
            any("Always keep on this device" in note for note in survey["advice"])
        )

    def test_the_windows_placeholder_attribute_is_what_is_checked(self) -> None:
        path = self.write("a.json")

        with patch.object(
            Path,
            "stat",
            lambda _self, **_: SimpleNamespace(
                st_file_attributes=FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
            ),
        ):
            self.assertTrue(is_cloud_placeholder(path))

    def test_a_csv_export_without_a_body_column_is_caught_before_import(self) -> None:
        self.write("mail.csv", "id,subject,bodyPreview,receivedDateTime\n")

        survey = survey_sources([self.root])

        self.assertIn("bodypreview", survey["csv_columns"])
        self.assertTrue(any("no full body column" in note for note in survey["advice"]))

    def test_a_csv_export_with_a_body_column_is_accepted(self) -> None:
        self.write("mail.csv", "id,subject,body,receivedDateTime\n")

        survey = survey_sources([self.root])

        self.assertEqual(survey["advice"], [])
        self.assertTrue(survey["ready"])

    def test_both_columns_present_says_which_one_wins(self) -> None:
        self.write("mail.csv", "id,body,bodyPreview\n")

        survey = survey_sources([self.root])

        self.assertTrue(any("preview is ignored" in note for note in survey["advice"]))

    def test_raw_json_alongside_csv_silences_the_column_warning(self) -> None:
        self.write("mail.csv", "id,subject,bodyPreview\n")
        self.write("mail.json", "{}")

        survey = survey_sources([self.root])

        self.assertEqual(survey["advice"], [])

    def test_a_normal_file_is_not_mistaken_for_a_placeholder(self) -> None:
        self.assertFalse(is_cloud_placeholder(self.write("a.json")))

    def test_a_path_that_disappeared_is_not_a_placeholder(self) -> None:
        self.assertFalse(is_cloud_placeholder(self.root / "gone.json"))


if __name__ == "__main__":
    unittest.main()
