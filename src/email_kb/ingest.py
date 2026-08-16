from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from .cleaning import clean_body
from .database import (
    begin_source,
    finish_source,
    sha256_file,
    source_is_current,
    upsert_messages,
)

SUPPORTED_SUFFIXES = {".csv", ".json"}


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_jsonish(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y"}


def _email_address(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        return {"name": "", "address": value.strip()}
    if not isinstance(value, Mapping):
        return {"name": "", "address": ""}
    nested = value.get("emailAddress")
    if isinstance(nested, Mapping):
        value = nested
    return {
        "name": str(value.get("name") or "").strip(),
        "address": str(value.get("address") or "").strip(),
    }


def _recipients(value: Any) -> list[dict[str, str]]:
    parsed = _parse_jsonish(value, [])
    if isinstance(parsed, Mapping):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    recipients = [_email_address(item) for item in parsed]
    return [item for item in recipients if item["name"] or item["address"]]


def _categories(value: Any) -> list[str]:
    parsed = _parse_jsonish(value, [])
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _body(record: Mapping[str, Any]) -> tuple[str, str]:
    value = record.get("body")
    if isinstance(value, Mapping):
        return (
            str(value.get("content") or ""),
            str(value.get("contentType") or value.get("content_type") or ""),
        )
    return str(value or ""), str(record.get("body_type") or "")


def _stable_id(record: Mapping[str, Any], body: str) -> str:
    explicit = record.get("id") or record.get("email_id")
    if explicit:
        return str(explicit)
    identity = "\x1f".join(
        str(
            record.get(name)
            or record.get(
                {
                    "internetMessageId": "internet_message_id",
                    "conversationId": "conversation_id",
                    "receivedDateTime": "received_at_utc",
                    "subject": "subject",
                }.get(name, name)
            )
            or ""
        )
        for name in (
            "internetMessageId",
            "conversationId",
            "receivedDateTime",
            "subject",
        )
    )
    digest = hashlib.sha256(f"{identity}\x1f{body}".encode()).hexdigest()
    return f"synthetic:{digest}"


def normalize_message(record: Mapping[str, Any]) -> dict[str, Any]:
    body, body_type = _body(record)
    sender = _email_address(
        record.get("from")
        or record.get("sender")
        or {
            "name": record.get("sender_name"),
            "address": record.get("sender_address"),
        }
    )
    email_id = _stable_id(record, body)
    conversation_id = str(
        record.get("conversationId") or record.get("conversation_id") or email_id
    )

    to_recipients = _recipients(
        record.get("toRecipients") or record.get("to_recipients_json")
    )
    cc_recipients = _recipients(
        record.get("ccRecipients") or record.get("cc_recipients_json")
    )
    bcc_recipients = _recipients(
        record.get("bccRecipients") or record.get("bcc_recipients_json")
    )
    reply_to = _recipients(record.get("replyTo") or record.get("reply_to_json"))
    categories = _categories(record.get("categories") or record.get("categories_json"))

    return {
        "email_id": email_id,
        "internet_message_id": (
            str(
                record.get("internetMessageId")
                or record.get("internet_message_id")
                or ""
            )
            or None
        ),
        "conversation_id": conversation_id,
        "parent_folder_id": (
            str(record.get("parentFolderId") or record.get("parent_folder_id") or "")
            or None
        ),
        "received_at_utc": (
            str(record.get("receivedDateTime") or record.get("received_at_utc") or "")
            or None
        ),
        "sent_at_utc": (
            str(record.get("sentDateTime") or record.get("sent_at_utc") or "") or None
        ),
        "sender_name": sender["name"] or None,
        "sender_address": sender["address"] or None,
        "to_recipients_json": _json_dump(to_recipients),
        "cc_recipients_json": _json_dump(cc_recipients),
        "bcc_recipients_json": _json_dump(bcc_recipients),
        "reply_to_json": _json_dump(reply_to),
        "subject": str(record.get("subject") or "").strip(),
        "body": body,
        "clean_body": clean_body(body, body_type),
        "body_type": body_type or None,
        "body_preview": (
            str(record.get("bodyPreview") or record.get("body_preview") or "") or None
        ),
        "importance": str(record.get("importance") or "") or None,
        "has_attachments": int(
            _as_bool(
                record.get("hasAttachments")
                if "hasAttachments" in record
                else record.get("has_attachments")
            )
        ),
        "is_read": int(
            _as_bool(
                record.get("isRead") if "isRead" in record else record.get("is_read")
            )
        ),
        "categories_json": _json_dump(categories),
        "web_link": (
            str(record.get("webLink") or record.get("web_link") or "") or None
        ),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "raw_json": _json_dump(dict(record)),
    }


def _unwrap_graph_payload(payload: Any) -> list[Mapping[str, Any]]:
    for _ in range(3):
        if isinstance(payload, str):
            payload = json.loads(payload)
            continue
        break

    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, Mapping):
        if isinstance(payload.get("value"), list):
            records = payload["value"]
        elif isinstance(payload.get("body"), Mapping) and isinstance(
            payload["body"].get("value"), list
        ):
            records = payload["body"]["value"]
        elif "id" in payload or "email_id" in payload:
            records = [payload]
        else:
            raise ValueError("JSON does not contain a Graph 'value' array")
    else:
        raise TypeError("JSON root must be an object, array, or encoded JSON string")

    if not all(isinstance(item, Mapping) for item in records):
        raise ValueError("Every email record must be a JSON object")
    return list(records)


def iter_source_messages(path: str | Path) -> Iterator[dict[str, Any]]:
    source = Path(path)
    suffix = source.suffix.casefold()
    if suffix == ".csv":
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            for record in csv.DictReader(handle):
                yield normalize_message(record)
        return
    if suffix == ".json":
        with source.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        for record in _unwrap_graph_payload(payload):
            yield normalize_message(record)
        return
    raise ValueError(f"Unsupported source type: {source.suffix}")


def discover_sources(paths: Iterable[str | Path]) -> list[Path]:
    discovered: set[Path] = set()
    for value in paths:
        path = Path(value).expanduser().resolve()
        if path.is_file() and path.suffix.casefold() in SUPPORTED_SUFFIXES:
            discovered.add(path)
        elif path.is_dir():
            for candidate in path.rglob("*"):
                if (
                    candidate.is_file()
                    and candidate.suffix.casefold() in SUPPORTED_SUFFIXES
                ):
                    discovered.add(candidate.resolve())
        else:
            raise FileNotFoundError(path)
    return sorted(discovered)


def ingest_sources(
    connection: sqlite3.Connection,
    paths: Iterable[str | Path],
    *,
    force: bool = False,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "files_total": 0,
        "files_imported": 0,
        "files_skipped": 0,
        "files_failed": 0,
        "messages_imported": 0,
        "errors": [],
    }
    for source in discover_sources(paths):
        summary["files_total"] += 1
        digest = sha256_file(source)
        if not force and source_is_current(connection, source, digest):
            summary["files_skipped"] += 1
            continue

        kind = source.suffix.casefold().lstrip(".")
        source_id = begin_source(connection, source, digest, kind)
        try:
            messages = list(iter_source_messages(source))
            if sha256_file(source) != digest:
                raise RuntimeError("Source file changed while it was being read")
            count = upsert_messages(connection, source_id, messages)
        # One malformed source must be recorded without stopping other source files.
        except Exception as error:  # noqa: BLE001
            finish_source(connection, source_id, message_count=0, error=str(error))
            summary["files_failed"] += 1
            summary["errors"].append({"path": str(source), "error": str(error)})
            continue

        finish_source(connection, source_id, message_count=count)
        summary["files_imported"] += 1
        summary["messages_imported"] += count
    return summary
