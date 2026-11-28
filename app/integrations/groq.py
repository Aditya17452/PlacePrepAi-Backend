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
    ) -> tuple[str, int, int]:
        """Return (text, prompt_tokens, completion_tokens)."""
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
    ) -> tuple[str, int, int]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "timeout": timeout,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        resp = await self._client.chat.completions.create(**kwargs)
        text = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        return (
            text,
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0),
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
