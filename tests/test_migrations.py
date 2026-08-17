from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from email_kb.database import connect, initialize
from email_kb.migrations import BASELINE, SCHEMA_VERSION, current_version


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fresh_database_reports_latest_version(self) -> None:
        connection = connect(self.root / "fresh.db")
        try:
            result = initialize(connection)
            self.assertEqual(result["previous_version"], 0)
            self.assertEqual(result["schema_version"], SCHEMA_VERSION)
            self.assertEqual(current_version(connection), SCHEMA_VERSION)
        finally:
            connection.close()

    def test_initialize_is_idempotent(self) -> None:
        connection = connect(self.root / "twice.db")
        try:
            initialize(connection)
            second = initialize(connection)
            self.assertEqual(second["applied_migrations"], [])
            rows = connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[
                0
            ]
            self.assertEqual(int(rows), SCHEMA_VERSION)
        finally:
            connection.close()

    def test_pre_versioning_database_is_adopted_not_replayed(self) -> None:
        path = self.root / "legacy.db"
        legacy = sqlite3.connect(path)
        try:
            legacy.executescript(BASELINE)
            legacy.execute(
                """
                INSERT INTO source_files (
                    path, sha256, kind, size_bytes, modified_ns, status
                )
                VALUES ('/legacy.json', 'abc', 'json', 1, 1, 'imported')
                """
            )
            legacy.commit()
        finally:
            legacy.close()

        connection = connect(path)
        try:
            result = initialize(connection)
            self.assertEqual(result["previous_version"], 1)
            self.assertEqual(current_version(connection), SCHEMA_VERSION)
            kept = connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0]
            self.assertEqual(int(kept), 1)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
