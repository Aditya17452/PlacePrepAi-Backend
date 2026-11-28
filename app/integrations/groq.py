"""Groq client: the wire protocol only.

Moved out of `app/core/llm.py`, which keeps the parts that are ours — task-to-model
routing, retries, the concurrency gate, schema validation and cost accounting. What is
left here is building a request and reading a response, so another vendor can be added by
writing a second `Provider` without touching any of that.
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings


class Provider:
    """The surface `app.core.llm` depends on. Implement it to add a vendor."""

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        json_mode: bool,
        timeout: float,
        reasoning_effort: str | None = None,
    ) -> tuple[str, int, int, str]:
        """Return (text, prompt_tokens, completion_tokens, finish_reason).

        `finish_reason` matters for reasoning models: "length" means the budget ran out,
        which is a different failure from the model declining to answer, and the caller
        needs to tell them apart.
        """
        raise NotImplementedError

    async def transcribe(
        self, *, model: str, audio: bytes, filename: str, timeout: float
    ) -> str:
        raise NotImplementedError


class GroqProvider(Provider):
    def __init__(self, api_key: str) -> None:
        from groq import AsyncGroq

        self._client = AsyncGroq(api_key=api_key)

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        json_mode: bool,
        timeout: float,
        reasoning_effort: str | None = None,
    ) -> tuple[str, int, int, str]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "timeout": timeout,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if reasoning_effort:
            # Not a named parameter in the installed SDK, so it goes through extra_body.
            kwargs["extra_body"] = {"reasoning_effort": reasoning_effort}

        resp = await self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        text = (choice.message.content or "").strip()
        usage = getattr(resp, "usage", None)
        return (
            text,
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0),
            str(getattr(choice, "finish_reason", "") or ""),
        )

    async def transcribe(
        self, *, model: str, audio: bytes, filename: str, timeout: float
    ) -> str:
        resp = await self._client.audio.transcriptions.create(
            file=(filename, audio, "audio/webm"),
            model=model,
            language="en",
            response_format="text",
            temperature=0.0,
            timeout=timeout,
        )
        return str(resp).strip()


def build_providers() -> list[Provider]:
    """Primary key first, then the fallback key if one is configured."""
    built: list[Provider] = []
    for key in (settings.GROQ_API_KEY, settings.GROQ_API_KEY2):
        if key:
            built.append(GroqProvider(key))
    return built
