from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

from email_kb.database import connect, initialize, save_thread_analysis
from email_kb.diagnostics import doctor, exposure_check, mcp_config
from email_kb.migrations import SCHEMA_VERSION

HAS_MCP = importlib.util.find_spec("mcp.server.mcpserver") is not None


class McpConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_paths_are_absolute_so_the_client_can_launch_it_anywhere(self) -> None:
        result = mcp_config(self.root / "kb.db", client="cursor", python="/py")

        args = result["config"]["mcpServers"]["email-knowledge"]["args"]
        self.assertEqual(
            result["config"]["mcpServers"]["email-knowledge"]["command"], "/py"
        )
        self.assertTrue(Path(args[-1]).is_absolute())
        self.assertEqual(args[:3], ["-m", "email_kb.mcp_server", "--db"])

    def test_write_back_is_off_unless_asked_for(self) -> None:
        without = mcp_config(self.root / "kb.db", client="claude")
        with_feedback = mcp_config(
            self.root / "kb.db", client="claude", allow_feedback=True
        )

        self.assertNotIn(
            "--allow-feedback",
            without["config"]["mcpServers"]["email-knowledge"]["args"],
        )
        self.assertIn(
            "--allow-feedback",
            with_feedback["config"]["mcpServers"]["email-knowledge"]["args"],
        )

    def test_each_client_is_told_where_its_file_lives(self) -> None:
        self.assertEqual(
            mcp_config(self.root / "kb.db", client="claude")["install_to"]["project"],
            ".mcp.json",
        )
        self.assertEqual(
            mcp_config(self.root / "kb.db", client="cursor")["install_to"]["project"],
            ".cursor/mcp.json",
        )

    def test_unknown_client_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            mcp_config(self.root / "kb.db", client="notepad")


class ExposureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", *arguments], cwd=self.root, check=True, capture_output=True
        )

    def test_database_outside_any_repository_is_safe(self) -> None:
        result = exposure_check(self.root / "kb.db")

        self.assertFalse(result["inside_git_repository"])
        self.assertTrue(result["safe"])

    def test_unignored_database_in_a_repository_is_flagged(self) -> None:
        self.git("init")
        database = self.root / "kb.db"
        database.write_bytes(b"")

        result = exposure_check(database)

        self.assertTrue(result["inside_git_repository"])
        self.assertFalse(result["safe"])
        self.assertIn("NOT ignored", result["detail"])

    def test_ignored_database_in_a_repository_is_safe(self) -> None:
        self.git("init")
        (self.root / ".gitignore").write_text("*.db\n", encoding="utf-8")
        database = self.root / "kb.db"
        database.write_bytes(b"")

        result = exposure_check(database)

        self.assertTrue(result["inside_git_repository"])
        self.assertTrue(result["safe"])

    def test_a_database_in_a_subdirectory_finds_the_repository_root(self) -> None:
        self.git("init")
        nested = self.root / "data" / "nested"
        nested.mkdir(parents=True)
        database = nested / "kb.db"
        database.write_bytes(b"")

        result = exposure_check(database)

        self.assertEqual(result["repository"], str(self.root.resolve()))
        self.assertFalse(result["safe"])


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "kb.db"
        self.connection = connect(self.database)
        initialize(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def named(self, report: dict, name: str) -> dict:
        return next(item for item in report["checks"] if item["check"] == name)

    def test_an_empty_database_is_not_ready_and_says_what_to_do(self) -> None:
        report = doctor(self.connection, self.database)

        self.assertFalse(report["ready_for_agents"])
        self.assertIn("run 'ingest' against your export", report["next_steps"])
        self.assertTrue(self.named(report, "schema_version")["ok"])
        self.assertEqual(
            self.named(report, "schema_version")["detail"],
            f"database is at {SCHEMA_VERSION}, code expects {SCHEMA_VERSION}",
        )

    def test_analyses_without_an_index_are_reported_as_unindexed(self) -> None:
        save_thread_analysis(
            self.connection,
            thread_id="t1",
            source_fingerprint="f",
            status="verified",
            importance_score=80,
            model_reported_confidence=0.9,
            agreement_score=1.0,
            gap_reasons=[],
            segment_count=1,
            proposals=None,
            verified={"summary": "s", "claims": []},
        )

        report = doctor(self.connection, self.database)

        self.assertFalse(self.named(report, "index_current")["ok"])
        self.assertIn("run 'index'", report["next_steps"])

    def test_analyses_from_the_anchored_flow_are_reported_as_stale(self) -> None:
        save_thread_analysis(
            self.connection,
            thread_id="t1",
            source_fingerprint="f",
            status="stale",
            importance_score=None,
            model_reported_confidence=None,
            agreement_score=None,
            gap_reasons=[],
            segment_count=None,
            proposals=None,
            verified=None,
        )

        report = doctor(self.connection, self.database)

        self.assertFalse(self.named(report, "stale_analyses")["ok"])
        self.assertIn("run 'analyze' again to replace them", report["next_steps"])

    def test_exposure_is_part_of_readiness(self) -> None:
        report = doctor(self.connection, self.database)

        self.assertIn("exposure", report)
        self.assertTrue(self.named(report, "database_not_committable")["ok"])


@unittest.skipUnless(HAS_MCP, "mcp extra is not installed")
class WriteBackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "kb.db"
        connection = connect(self.database)
        initialize(connection)
        connection.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def tool_names(self, **kwargs) -> set[str]:
        import asyncio

        from email_kb.mcp_server import build_server

        server = build_server(self.database, **kwargs)
        return {tool.name for tool in asyncio.run(server.list_tools())}

    def test_reading_and_writing_are_separate_permissions(self) -> None:
        self.assertNotIn("record_usefulness", self.tool_names())
        self.assertIn("record_usefulness", self.tool_names(allow_feedback=True))

    def test_an_agent_cannot_mark_knowledge_wrong_or_outdated(self) -> None:
        import asyncio

        from email_kb.mcp_server import build_server

        server = build_server(self.database, allow_feedback=True)
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

        # The agent gets a boolean, not the verdict vocabulary, so the
        # visibility-changing verdicts are unreachable from this tool.
        properties = tools["record_usefulness"].input_schema["properties"]
        self.assertEqual(properties["was_useful"]["type"], "boolean")
        self.assertNotIn("verdict", properties)


if __name__ == "__main__":
    unittest.main()
