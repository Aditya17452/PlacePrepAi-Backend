"""The single place the application talks to a language model.

Previously five call sites each had their own client, retry loop, timeout, markdown
stripping and `json.loads`, and each one silently defaulted to a plausible-looking score
when parsing failed. This module centralises that and adds what was missing: a bounded
concurrency gate, a per-task token budget, schema validation, key fallback, and usage
accounting so cost per interview is measured rather than guessed.

`llama-3.3-70b-versatile` is Enterprise-only on Groq as of 2026-11, so the defaults are
the self-serve `openai/gpt-oss-*` models. Model IDs are per task (PLAN.md §10), because
grading wants the stronger model while a live coaching tip does not.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from core.config import settings
from core.logging_setup import get_logger

log = get_logger("llm")

T = TypeVar("T", bound=BaseModel)


class Task(StrEnum):
    QUESTION = "question"
    GRADER = "grader"
    EXTRACTOR = "extractor"
    EVALUATOR = "evaluator"
    CHEAP = "cheap"
    STT = "stt"


#: USD per 1M tokens (prompt, completion), and USD per audio hour for STT.
#: Kept here so a historical `llm_usage` row keeps the price it was costed at.
PRICING: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
}
STT_USD_PER_HOUR: dict[str, float] = {
    "whisper-large-v3-turbo": 0.04,
    "whisper-large-v3": 0.111,
}


def model_for(task: Task) -> str:
    return {
        Task.QUESTION: settings.LLM_MODEL_QUESTION,
        Task.GRADER: settings.LLM_MODEL_GRADER,
        Task.EXTRACTOR: settings.LLM_MODEL_EXTRACTOR,
        Task.EVALUATOR: settings.LLM_MODEL_EVALUATOR,
        Task.CHEAP: settings.LLM_MODEL_CHEAP,
        Task.STT: settings.STT_MODEL,
    }[task]


def budget_for(task: Task) -> int:
    return {
        Task.QUESTION: settings.LLM_TOKEN_BUDGET_QUESTION,
        Task.GRADER: settings.LLM_TOKEN_BUDGET_GRADER,
        Task.EVALUATOR: settings.LLM_TOKEN_BUDGET_EVALUATOR,
        Task.EXTRACTOR: settings.LLM_TOKEN_BUDGET_GRADER,
        Task.CHEAP: 160,
        Task.STT: 0,
    }[task]


class LLMError(RuntimeError):
    """The model could not be reached, or returned nothing usable."""


class LLMBusy(LLMError):
    """Upstream rate limit. Callers should surface backpressure, not a fake answer."""


@dataclass
class Usage:
    model: str
    task: Task
    prompt_tokens: int = 0
    completion_tokens: int = 0
    audio_seconds: float = 0.0
    latency_ms: int = 0
    ok: bool = True
    error: str | None = None

    @property
    def cost_usd(self) -> float:
        if self.task is Task.STT:
            rate = STT_USD_PER_HOUR.get(self.model, 0.0)
            return (self.audio_seconds / 3600.0) * rate
        prompt_rate, completion_rate = PRICING.get(self.model, (0.0, 0.0))
        return (
            self.prompt_tokens * prompt_rate + self.completion_tokens * completion_rate
        ) / 1_000_000


# ── Concurrency gate ──────────────────────────────────────────────────────────
# Groq's per-key limits are the real ceiling on concurrent interviews. This bounds how
# many in-flight calls one instance will make; the caller turns a refusal into an explicit
# "the interviewer is busy" state rather than a silently degraded question.

_semaphore: asyncio.Semaphore | None = None


def _gate() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.LLM_MAX_CONCURRENCY)
    return _semaphore


# ── Provider interface ────────────────────────────────────────────────────────


class Provider:
    """Minimal surface so another vendor can be dropped in behind the same calls."""

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


_providers: list[Provider] | None = None


def providers() -> list[Provider]:
    """Primary key first, then the fallback key if one is configured."""
    global _providers
    if _providers is None:
        built: list[Provider] = []
        for key in (settings.GROQ_API_KEY, settings.GROQ_API_KEY2):
            if key:
                built.append(GroqProvider(key))
        _providers = built
    return _providers


def set_providers_for_tests(new: list[Provider] | None) -> None:
    global _providers
    _providers = new


# ── Usage recording ───────────────────────────────────────────────────────────


def record_usage(
    usage: Usage, *, session_id: str | None = None, student_id: str | None = None
) -> None:
    """Persist one usage row. Never raises — accounting must not break a request."""
    try:
        from models.billing import LlmUsage
        from models.database import SessionLocal

        db = SessionLocal()
        try:
            db.add(
                LlmUsage(
                    session_id=session_id,
                    student_id=student_id,
                    task=usage.task.value,
                    model=usage.model,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    audio_seconds=usage.audio_seconds,
                    cost_usd=usage.cost_usd,
                    latency_ms=usage.latency_ms,
                    ok=usage.ok,
                    error=usage.error[:200] if usage.error else None,
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover - accounting is best effort
        log.warning("llm_usage_record_failed", error=str(exc))


# ── Public API ────────────────────────────────────────────────────────────────


_RETRYABLE = ("rate limit", "429", "timeout", "timed out", "502", "503", "overloaded")


def _is_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _RETRYABLE)


def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return "rate limit" in text or "429" in text


async def complete_text(
    *,
    task: Task,
    system: str,
    user: str,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    session_id: str | None = None,
    student_id: str | None = None,
) -> str:
    """Plain-text completion with retries, key fallback and usage accounting."""
    text, _ = await _complete(
        task=task,
        system=system,
        user=user,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=False,
        session_id=session_id,
        student_id=student_id,
    )
    return text


async def complete_structured(
    *,
    task: Task,
    system: str,
    user: str,
    schema: type[T],
    temperature: float = 0.2,
    max_tokens: int | None = None,
    session_id: str | None = None,
    student_id: str | None = None,
) -> T:
    """Completion validated against a Pydantic model.

    On a parse or validation failure the model is asked once more, with the error
    appended, and then the call fails. It never invents a default — a fabricated score is
    worse than a visible failure.
    """
    attempt_user = user
    last_error: str | None = None

    for attempt in range(2):
        text, usage = await _complete(
            task=task,
            system=system,
            user=attempt_user,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
            session_id=session_id,
            student_id=student_id,
        )
        try:
            return schema.model_validate(json.loads(_strip_json_fence(text)))
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = str(exc)
            log.warning(
                "llm_structured_parse_failed",
                task=task.value,
                attempt=attempt + 1,
                error=last_error[:300],
            )
            attempt_user = (
                f"{user}\n\nYour previous reply could not be parsed: {last_error[:500]}\n"
                "Reply with valid JSON matching the schema exactly, and nothing else."
            )

    raise LLMError(f"Structured output failed validation twice: {last_error}")


async def transcribe(
    *,
    audio: bytes,
    filename: str = "answer.webm",
    session_id: str | None = None,
    student_id: str | None = None,
) -> str:
    """Speech to text. Returns "" rather than raising — callers have a text fallback."""
    model = model_for(Task.STT)
    usage = Usage(model=model, task=Task.STT)
    # Groq bills by audio duration; we do not decode the container here, so estimate from
    # size at a typical Opus bitrate. Good enough for cost trending, not for invoicing.
    usage.audio_seconds = round(len(audio) / 4_000, 2)
    started = time.perf_counter()

    for provider in providers():
        try:
            async with _gate():
                text = await provider.transcribe(
                    model=model,
                    audio=audio,
                    filename=filename,
                    timeout=settings.LLM_TIMEOUT_SEC,
                )
            usage.latency_ms = int((time.perf_counter() - started) * 1000)
            record_usage(usage, session_id=session_id, student_id=student_id)
            return _filter_stt_hallucination(text)
        except Exception as exc:
            usage.ok = False
            usage.error = str(exc)
            log.warning("stt_failed", error=str(exc)[:200])

    usage.latency_ms = int((time.perf_counter() - started) * 1000)
    record_usage(usage, session_id=session_id, student_id=student_id)
    return ""


async def _complete(
    *,
    task: Task,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int | None,
    json_mode: bool,
    session_id: str | None,
    student_id: str | None,
) -> tuple[str, Usage]:
    model = model_for(task)
    budget = max_tokens or budget_for(task)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    candidates = list(providers())
    if not candidates:
        raise LLMError("No Groq API key configured")

    fallback_model = settings.LLM_MODEL_FALLBACK
    last_exc: Exception | None = None
    rate_limited = False

    for provider_index, provider in enumerate(candidates):
        for attempt in range(settings.LLM_MAX_RETRIES + 1):
            usage = Usage(model=model, task=task)
            started = time.perf_counter()
            try:
                async with _gate():
                    text, prompt_tokens, completion_tokens = await provider.complete(
                        model=model,
                        messages=messages,
                        max_tokens=budget,
                        temperature=temperature,
                        json_mode=json_mode,
                        timeout=settings.LLM_TIMEOUT_SEC,
                    )
                usage.prompt_tokens = prompt_tokens
                usage.completion_tokens = completion_tokens
                usage.latency_ms = int((time.perf_counter() - started) * 1000)
                record_usage(usage, session_id=session_id, student_id=student_id)
                if not text:
                    raise LLMError("Model returned an empty response")
                return text, usage

            except Exception as exc:
                last_exc = exc
                usage.ok = False
                usage.error = str(exc)
                usage.latency_ms = int((time.perf_counter() - started) * 1000)
                record_usage(usage, session_id=session_id, student_id=student_id)

                if _is_rate_limit(exc):
                    rate_limited = True
                if not _is_retryable(exc) or attempt == settings.LLM_MAX_RETRIES:
                    break
                # Exponential backoff with a small deterministic jitter by attempt.
                await asyncio.sleep(min(2**attempt + 0.25 * attempt, 8))

        log.warning(
            "llm_provider_failed",
            task=task.value,
            provider_index=provider_index,
            error=str(last_exc)[:200],
        )

    if fallback_model and fallback_model != model:
        log.warning("llm_trying_fallback_model", task=task.value, model=fallback_model)
        try:
            async with _gate():
                text, prompt_tokens, completion_tokens = await candidates[0].complete(
                    model=fallback_model,
                    messages=messages,
                    max_tokens=budget,
                    temperature=temperature,
                    json_mode=json_mode,
                    timeout=settings.LLM_TIMEOUT_SEC,
                )
            usage = Usage(
                model=fallback_model,
                task=task,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            record_usage(usage, session_id=session_id, student_id=student_id)
            if text:
                return text, usage
        except Exception as exc:  # pragma: no cover
            last_exc = exc

    if rate_limited:
        raise LLMBusy(
            "The interviewer is handling too many sessions right now. "
            "Please retry in a few seconds."
        ) from last_exc
    raise LLMError(f"LLM call failed: {last_exc}") from last_exc


# ── Helpers ───────────────────────────────────────────────────────────────────

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

#: Whisper's well-known outputs on silence. Short replies matching these are dropped so a
#: silent answer is not graded as if the candidate said "thank you".
_STT_HALLUCINATIONS = (
    "thank you",
    "thanks for watching",
    "subscribe",
    "amara.org",
    "by the way",
)


def _strip_json_fence(text: str) -> str:
    match = _FENCE.match(text)
    return match.group(1) if match else text.strip()


def _filter_stt_hallucination(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) < 30:
        lowered = cleaned.lower()
        if any(h in lowered for h in _STT_HALLUCINATIONS):
            log.info("stt_hallucination_filtered", text=cleaned)
            return ""
    return cleaned
