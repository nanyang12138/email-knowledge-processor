"""
Model backends.

Which model sees the mail is a data decision before it is a quality decision.
Binding the pipeline to one vendor makes that decision permanent and invisible,
so every model call goes through this interface and the backend is chosen at
the edge.

Two are shipped. Cursor is the default and needs an API key even when the
desktop app is installed, because the SDK authenticates separately from the
IDE. Anything speaking the OpenAI chat-completions API is the alternative,
which covers Azure OpenAI, OpenAI, vLLM, llama.cpp, and Ollama; pointing it at
a local runtime is what keeps message bodies on the machine that owns them.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DEFAULT_TIMEOUT = 600
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Completion:
    """One model response, with whatever the backend can say about the call."""

    text: str
    model: str | None = None
    run_id: str | None = None
    agent_id: str | None = None
    duration_ms: int | None = None


class Provider(Protocol):
    name: str

    def complete(
        self, prompt: str, *, model: str, idempotency_key: str
    ) -> Completion: ...


def _sleep(attempt: int, retry_after: float | None = None) -> None:
    delay = retry_after if retry_after is not None else 2**attempt
    time.sleep(min(float(delay), 30))


class CursorProvider:
    """
    The Cursor SDK.

    Tools are disabled on every call. Mail is untrusted input, so a run that
    could act on what it reads would turn a mailbox into an instruction
    channel.
    """

    name = "cursor"

    def __init__(
        self, *, api_key: str, workspace: str | Path, retries: int = 3
    ) -> None:
        if not api_key:
            raise ValueError(
                "CURSOR_API_KEY is not set. Create one in Cursor Dashboard > "
                "Integrations. Installing the Cursor app is not enough: the "
                "SDK authenticates separately from the editor."
            )
        self._api_key = api_key
        self._workspace = Path(workspace).expanduser().resolve()
        self._retries = retries

    def complete(self, prompt: str, *, model: str, idempotency_key: str) -> Completion:
        from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions

        last_error: Exception | None = None
        for attempt in range(self._retries):
            try:
                result = Agent.prompt(
                    prompt,
                    AgentOptions(
                        api_key=self._api_key,
                        model=model,
                        idempotency_key=idempotency_key,
                        local=LocalAgentOptions(cwd=self._workspace),
                        tools=[],
                    ),
                )
                status = str(getattr(result.status, "value", result.status)).casefold()
                if status not in {"finished", "completed", "success"}:
                    raise RuntimeError(
                        f"Cursor run {result.id} ended with status "
                        f"{result.status}: {result.result}"
                    )
                return Completion(
                    text=str(result.result),
                    model=_model_name(getattr(result, "model", None)) or model,
                    run_id=getattr(result, "id", None),
                    agent_id=getattr(result, "agent_id", None),
                    duration_ms=getattr(result, "duration_ms", None),
                )
            except CursorAgentError as error:
                last_error = error
                if not getattr(error, "is_retryable", False):
                    raise
                if attempt + 1 >= self._retries:
                    raise
                retry_after = getattr(error, "retry_after", None)
                _sleep(
                    attempt,
                    float(retry_after)
                    if isinstance(retry_after, (int, float))
                    else None,
                )
            except RuntimeError as error:
                last_error = error
                if attempt + 1 >= self._retries:
                    raise
                _sleep(attempt)
        raise RuntimeError("Cursor run failed") from last_error


class OpenAICompatibleProvider:
    """
    Any endpoint speaking the OpenAI chat-completions API.

    Uses the standard library so choosing this backend does not drag in a
    dependency. Temperature defaults to zero: reproducing a run after a prompt
    change is worth more than sampling variety, and the instability that the
    two blind passes measure should come from using two different models rather
    than from the same model rolling different dice.
    """

    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        temperature: float = 0.0,
        timeout: int = DEFAULT_TIMEOUT,
        retries: int = 3,
    ) -> None:
        if not base_url:
            raise ValueError(
                "A base URL is required, for example "
                "http://localhost:11434/v1 for Ollama"
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._temperature = temperature
        self._timeout = timeout
        self._retries = retries

    def complete(self, prompt: str, *, model: str, idempotency_key: str) -> Completion:
        payload = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self._temperature,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
            headers["api-key"] = self._api_key  # Azure OpenAI
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )

        last_error: Exception | None = None
        for attempt in range(self._retries):
            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    body = json.load(response)
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code not in RETRYABLE_STATUS or attempt + 1 >= self._retries:
                    detail = error.read().decode("utf-8", "replace")[:500]
                    raise RuntimeError(
                        f"{self._base_url} returned {error.code}: {detail}"
                    ) from error
                _sleep(attempt)
                continue
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
                if attempt + 1 >= self._retries:
                    raise RuntimeError(
                        f"Could not reach {self._base_url}: {error}"
                    ) from error
                _sleep(attempt)
                continue

            try:
                text = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as error:
                raise RuntimeError(
                    f"{self._base_url} returned no completion: {json.dumps(body)[:500]}"
                ) from error
            return Completion(
                text=str(text),
                model=str(body.get("model") or model),
                run_id=str(body.get("id")) if body.get("id") else None,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        raise RuntimeError("Model call failed") from last_error


def _model_name(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("id") or value)
    return str(getattr(value, "id", value))


def build_provider(
    *,
    provider: str = "cursor",
    workspace: str | Path = ".",
    base_url: str | None = None,
    api_key: str | None = None,
) -> Provider:
    """
    Resolve a backend from arguments, falling back to the environment.

    Keys are never read from a file or written to one: they come from the
    environment of the session that runs the analysis and go no further.
    """
    if provider == "cursor":
        return CursorProvider(
            api_key=api_key or os.environ.get("CURSOR_API_KEY", ""),
            workspace=workspace,
        )
    if provider == "openai-compatible":
        return OpenAICompatibleProvider(
            base_url=base_url or os.environ.get("EMAIL_KB_BASE_URL", ""),
            api_key=api_key or os.environ.get("EMAIL_KB_API_KEY", ""),
        )
    raise ValueError("provider must be 'cursor' or 'openai-compatible'")
