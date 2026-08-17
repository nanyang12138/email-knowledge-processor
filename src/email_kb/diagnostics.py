"""
Setup checks and client configuration.

Two things go wrong when wiring this into an agent, and neither announces
itself. The first is tedious: a path typed slightly wrong in an MCP config
produces a server that silently fails to start. The second is serious: the
knowledge database is a derivative of the mailbox it was built from, so a
database sitting inside a git working tree that does not ignore it is one
`git add -A` away from publishing the mailbox.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from .migrations import SCHEMA_VERSION, current_version

SERVER_NAME = "email-knowledge"

CLIENT_LOCATIONS = {
    "claude": {
        "project": ".mcp.json",
        "user": "~/.claude.json",
        "note": (
            "Project scope is committed to the repository, so use it only for "
            "the config itself. The database path it points at must stay "
            "outside the repository."
        ),
    },
    "cursor": {
        "project": ".cursor/mcp.json",
        "user": "~/.cursor/mcp.json",
        "note": (
            "A project file overrides the global one entirely for a server of "
            "the same name; they are not merged."
        ),
    },
}


def mcp_config(
    database: str | Path,
    *,
    client: str,
    allow_feedback: bool = False,
    server_name: str = SERVER_NAME,
    python: str | None = None,
) -> dict[str, Any]:
    """
    Build a ready-to-paste MCP entry with every path already resolved.

    Relative paths are the usual reason a stdio server fails to start: the
    client launches it from a working directory you did not choose.
    """
    if client not in CLIENT_LOCATIONS:
        raise ValueError(f"client must be one of: {', '.join(CLIENT_LOCATIONS)}")
    interpreter = python or sys.executable
    args = ["-m", "email_kb.mcp_server", "--db", str(Path(database).resolve())]
    if allow_feedback:
        args.append("--allow-feedback")
    return {
        "client": client,
        "install_to": CLIENT_LOCATIONS[client],
        "config": {
            "mcpServers": {
                server_name: {
                    "type": "stdio",
                    "command": interpreter,
                    "args": args,
                }
            }
        },
    }


def _git_root(path: Path) -> Path | None:
    for candidate in [path, *path.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _git(root: Path, *arguments: str) -> tuple[int, str]:
    if shutil.which("git") is None:
        return 127, ""
    finished = subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return finished.returncode, finished.stdout.strip()


def exposure_check(database: str | Path) -> dict[str, Any]:
    """
    Report whether the database could be committed by accident.

    The database holds message bodies, extracted claims, and the raw model
    outputs for every run, so publishing it publishes the mailbox. This only
    reports; it never edits a repository on the owner's behalf.
    """
    path = Path(database).expanduser().resolve()
    root = _git_root(path.parent)
    if root is None:
        return {
            "database": str(path),
            "inside_git_repository": False,
            "safe": True,
            "detail": "The database is not inside a git working tree.",
        }

    ignored_code, _ = _git(root, "check-ignore", "-q", str(path))
    if ignored_code == 127:
        return {
            "database": str(path),
            "inside_git_repository": True,
            "repository": str(root),
            "safe": None,
            "detail": "git is not on PATH, so this could not be checked.",
        }
    ignored = ignored_code == 0
    _, remote = _git(root, "remote", "get-url", "origin")
    return {
        "database": str(path),
        "inside_git_repository": True,
        "repository": str(root),
        "remote": remote or None,
        "ignored_by_git": ignored,
        "safe": ignored,
        "detail": (
            "The database is inside a git repository but ignored, so it will "
            "not be committed."
            if ignored
            else "The database is inside a git repository and is NOT ignored. "
            "It contains message bodies and extracted knowledge. Move it "
            "outside the repository, or add it to .gitignore, before the next "
            "commit."
        ),
    }


def storage_report(
    connection: sqlite3.Connection, database: str | Path
) -> dict[str, Any]:
    """
    Where the space went, and how much is left on that volume.

    Every message is stored three times over: the original record, the body,
    and the cleaned body. That is deliberate, because derived text has to be
    rebuildable from an untouched original, but it means the database lands at
    roughly three times the size of the export and that is worth seeing rather
    than discovering when a disk fills up.
    """
    path = Path(database).expanduser().resolve()
    on_disk = sum(
        candidate.stat().st_size
        for candidate in (
            path,
            path.with_name(path.name + "-wal"),
            path.with_name(path.name + "-shm"),
        )
        if candidate.exists()
    )
    usage = shutil.disk_usage(path.parent if path.parent.exists() else Path.cwd())

    def total(query: str) -> int:
        try:
            return int(connection.execute(query).fetchone()[0] or 0)
        except sqlite3.Error:
            return 0

    return {
        "database_bytes": on_disk,
        "volume_free_bytes": usage.free,
        "volume_total_bytes": usage.total,
        "largest_contents": {
            "original_records": total("SELECT SUM(LENGTH(raw_json)) FROM messages"),
            "message_bodies": total(
                "SELECT SUM(LENGTH(body) + LENGTH(clean_body)) FROM messages"
            ),
            "model_outputs": total(
                "SELECT SUM(LENGTH(COALESCE(output_json, ''))) FROM analysis_runs"
            ),
        },
    }


def doctor(connection: sqlite3.Connection, database: str | Path) -> dict[str, Any]:
    """One report answering whether this is ready for an agent to use."""

    def count(table: str, where: str = "") -> int:
        try:
            return int(
                connection.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[
                    0
                ]
            )
        except sqlite3.Error:
            return 0

    version = current_version(connection)
    messages = count("messages")
    agent_visible = count(
        "thread_analyses", "WHERE status IN ('verified', 'verified_with_gaps')"
    )
    indexed = count("cases")
    stale = count("thread_analyses", "WHERE status = 'stale'")

    searchable = count("messages_fts")

    # Two layers, and the first one stands on its own. Searching the mail needs
    # no model, so reporting the whole system as not ready because nothing has
    # been analyzed would hide the part that already works.
    checks: list[dict[str, Any]] = [
        {
            "check": "schema_version",
            "layer": "search",
            "ok": version == SCHEMA_VERSION,
            "detail": f"database is at {version}, code expects {SCHEMA_VERSION}",
            "fix": None if version == SCHEMA_VERSION else "run 'init'",
        },
        {
            "check": "messages_imported",
            "layer": "search",
            "ok": messages > 0,
            "detail": f"{messages} messages",
            "fix": None if messages else "run 'ingest' against your export",
        },
        {
            "check": "messages_searchable",
            "layer": "search",
            "ok": searchable >= messages,
            "detail": f"{searchable} of {messages} messages indexed for search",
            "fix": None if searchable >= messages else "run 'index'",
        },
        {
            "check": "mcp_extra_installed",
            "layer": "search",
            "ok": _has_mcp(),
            "detail": "the mcp package is required to serve agents",
            "fix": None if _has_mcp() else 'install with pip install -e ".[agent]"',
        },
        {
            "check": "analyses_available",
            "layer": "experience",
            "ok": agent_visible > 0,
            "detail": f"{agent_visible} threads distilled into cases",
            "fix": None if agent_visible else "run 'analyze'",
        },
        {
            "check": "cases_indexed",
            "layer": "experience",
            "ok": indexed >= agent_visible,
            "detail": f"{indexed} cases indexed against {agent_visible} analyses",
            "fix": None if indexed >= agent_visible else "run 'index'",
        },
    ]
    if stale:
        checks.append(
            {
                "check": "stale_analyses",
                "layer": "experience",
                "ok": False,
                "detail": (
                    f"{stale} analyses came from the anchored review flow, whose "
                    "verified status was decided by a model-reported field"
                ),
                "fix": "run 'analyze' again to replace them",
            }
        )

    exposure = exposure_check(database)
    checks.append(
        {
            "check": "database_not_committable",
            "layer": "search",
            "ok": bool(exposure["safe"]),
            "detail": exposure["detail"],
            "fix": None
            if exposure["safe"]
            else "move the database outside the repository or ignore it",
        }
    )
    storage = storage_report(connection, database)
    free = storage["volume_free_bytes"]
    # Importing and analysing both grow the database, so less headroom than it
    # already occupies means the next run may not finish.
    room = free > max(storage["database_bytes"], 1_000_000_000)
    checks.append(
        {
            "check": "disk_headroom",
            "layer": "search",
            "ok": room,
            "detail": (
                f"{free // 1_000_000} MB free where the database lives, "
                f"database is {storage['database_bytes'] // 1_000_000} MB"
            ),
            "fix": None
            if room
            else "put the database on a volume with more room, with --db or "
            "EMAIL_KB_DB",
        }
    )
    search_checks = [item for item in checks if item["layer"] == "search"]
    return {
        "can_search_email": all(item["ok"] for item in search_checks),
        "can_answer_from_experience": all(item["ok"] for item in checks),
        "next_steps": [
            item["fix"] for item in checks if not item["ok"] and item["fix"]
        ],
        "checks": checks,
        "exposure": exposure,
        "storage": storage,
    }


def _has_mcp() -> bool:
    import importlib.util

    return importlib.util.find_spec("mcp.server.mcpserver") is not None
