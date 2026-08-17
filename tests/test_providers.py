from __future__ import annotations

import json
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from email_kb.providers import (
    Completion,
    CursorProvider,
    OpenAICompatibleProvider,
    build_provider,
)


def chat_response(text: str, *, model: str = "local-model") -> dict:
    return {
        "id": "cmpl-1",
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}}],
    }


class _Body:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class CursorProviderTests(unittest.TestCase):
    def test_a_missing_key_says_the_desktop_app_is_not_enough(self) -> None:
        with self.assertRaises(ValueError) as raised:
            CursorProvider(api_key="", workspace=".")

        self.assertIn("authenticates separately", str(raised.exception))

    def test_tools_are_disabled_on_every_call(self) -> None:
        seen = {}

        def fake_prompt(prompt, options):
            seen["tools"] = options.tools
            return SimpleNamespace(
                status="finished",
                id="run-1",
                agent_id="agent-1",
                result="{}",
                model="m",
                duration_ms=5,
            )

        provider = CursorProvider(api_key="k", workspace=".")
        with patch("cursor_sdk.Agent.prompt", side_effect=fake_prompt):
            result = provider.complete("hi", model="m", idempotency_key="i")

        # Mail is untrusted input; a run that could act on what it reads would
        # turn the mailbox into an instruction channel.
        self.assertEqual(seen["tools"], [])
        self.assertEqual(result.run_id, "run-1")
        self.assertEqual(result.duration_ms, 5)

    def test_an_unfinished_run_is_an_error_not_an_empty_answer(self) -> None:
        provider = CursorProvider(api_key="k", workspace=".", retries=1)
        failed = SimpleNamespace(
            status="errored", id="run-9", result="boom", model="m", duration_ms=1
        )

        with (
            patch("cursor_sdk.Agent.prompt", return_value=failed),
            self.assertRaises(RuntimeError) as raised,
        ):
            provider.complete("hi", model="m", idempotency_key="i")

        self.assertIn("run-9", str(raised.exception))


class OpenAICompatibleTests(unittest.TestCase):
    def test_a_base_url_is_required(self) -> None:
        with self.assertRaises(ValueError):
            OpenAICompatibleProvider(base_url="")

    def test_a_local_endpoint_needs_no_key(self) -> None:
        provider = OpenAICompatibleProvider(base_url="http://localhost:11434/v1")
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data)
            return _Body(chat_response("answer"))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = provider.complete("hi", model="qwen", idempotency_key="i")

        self.assertEqual(result.text, "answer")
        self.assertEqual(captured["url"], "http://localhost:11434/v1/chat/completions")
        self.assertNotIn("Authorization", captured["headers"])
        self.assertEqual(captured["body"]["model"], "qwen")
        self.assertEqual(captured["body"]["temperature"], 0.0)

    def test_a_key_is_sent_in_both_common_header_shapes(self) -> None:
        provider = OpenAICompatibleProvider(
            base_url="https://example.invalid/v1", api_key="secret"
        )
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured.update(dict(request.header_items()))
            return _Body(chat_response("ok"))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            provider.complete("hi", model="m", idempotency_key="i")

        # Bearer for OpenAI-style, api-key for Azure.
        self.assertEqual(captured["Authorization"], "Bearer secret")
        self.assertEqual(captured["Api-key"], "secret")

    def test_a_transient_failure_is_retried(self) -> None:
        provider = OpenAICompatibleProvider(base_url="http://x/v1", retries=3)
        attempts = []

        def fake_urlopen(request, timeout=None):
            attempts.append(1)
            if len(attempts) < 3:
                raise urllib.error.HTTPError("u", 503, "busy", {}, None)
            return _Body(chat_response("finally"))

        with (
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
            patch("time.sleep"),
        ):
            result = provider.complete("hi", model="m", idempotency_key="i")

        self.assertEqual(result.text, "finally")
        self.assertEqual(len(attempts), 3)

    def test_a_permanent_failure_reports_what_the_endpoint_said(self) -> None:
        provider = OpenAICompatibleProvider(base_url="http://x/v1", retries=3)
        error = urllib.error.HTTPError("u", 404, "nope", {}, None)
        error.read = lambda: b'{"error":"model not found"}'  # type: ignore[method-assign]

        with (
            patch("urllib.request.urlopen", side_effect=error),
            self.assertRaises(RuntimeError) as raised,
        ):
            provider.complete("hi", model="missing", idempotency_key="i")

        self.assertIn("404", str(raised.exception))
        self.assertIn("model not found", str(raised.exception))

    def test_a_malformed_response_is_not_read_as_an_empty_answer(self) -> None:
        provider = OpenAICompatibleProvider(base_url="http://x/v1")

        with (
            patch("urllib.request.urlopen", return_value=_Body({"choices": []})),
            self.assertRaises(RuntimeError) as raised,
        ):
            provider.complete("hi", model="m", idempotency_key="i")

        self.assertIn("no completion", str(raised.exception))


class BuildProviderTests(unittest.TestCase):
    def test_cursor_is_the_default_and_reads_the_environment(self) -> None:
        with patch.dict("os.environ", {"CURSOR_API_KEY": "k"}, clear=True):
            self.assertEqual(build_provider(workspace=".").name, "cursor")

    def test_a_local_backend_is_selectable_without_any_cursor_key(self) -> None:
        with patch.dict(
            "os.environ",
            {"EMAIL_KB_BASE_URL": "http://localhost:11434/v1"},
            clear=True,
        ):
            provider = build_provider(provider="openai-compatible")

        self.assertEqual(provider.name, "openai-compatible")

    def test_an_unknown_backend_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_provider(provider="telepathy")

    def test_completion_carries_no_required_metadata_beyond_text(self) -> None:
        self.assertEqual(Completion(text="x").model, None)


if __name__ == "__main__":
    unittest.main()
