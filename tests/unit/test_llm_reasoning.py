"""Reasoning-model handling, and the test-isolation guard.

`openai/gpt-oss-*` emit reasoning tokens that count against `max_tokens`. Measured on a
real call: a one-word reply burned 43 completion tokens, 35 of them reasoning. With the
old 200-token question budget a real prompt would often return empty content, and the app
would quietly fall back to a generic question — an interview that looks fine and is not.

These tests pin the three things that stop that: budgets sized for reasoning, effort
dialled down where depth is not needed, and truncation reported as truncation.
"""

from __future__ import annotations

import pytest

from app.core import llm
from app.core.config import settings
from app.core.llm import (
    LLMError,
    LLMTruncated,
    Task,
    budget_for,
    complete_text,
    reasoning_effort_for,
)


class FakeProvider:
    """Stands in for Groq. Records what it was asked for."""

    def __init__(self, text: str = "a question?", finish_reason: str = "stop") -> None:
        self.text = text
        self.finish_reason = finish_reason
        self.calls: list[dict] = []

    async def complete(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return self.text, 100, 40, self.finish_reason

    async def transcribe(self, **kwargs):  # noqa: ANN003
        return ""


@pytest.fixture
def fake_provider(monkeypatch):
    provider = FakeProvider()
    llm.set_providers_for_tests([provider])
    monkeypatch.setattr(llm, "record_usage", lambda *a, **k: None)
    yield provider
    llm.set_providers_for_tests(None)


# ── Budgets ───────────────────────────────────────────────────────────────────


def test_question_budget_leaves_room_for_reasoning():
    """200 was not enough once reasoning counted against it."""
    assert budget_for(Task.QUESTION) >= 500
    assert budget_for(Task.GRADER) >= 1_500
    assert budget_for(Task.EVALUATOR) >= 2_500


def test_budgets_come_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "LLM_TOKEN_BUDGET_QUESTION", 1_234)
    assert budget_for(Task.QUESTION) == 1_234


# ── Reasoning effort routing ──────────────────────────────────────────────────


@pytest.mark.parametrize("task", [Task.QUESTION, Task.CHEAP])
def test_fast_tasks_use_low_effort(task):
    """Writing a question does not need deep reasoning, and the tokens are billed."""
    assert reasoning_effort_for(task) == settings.LLM_REASONING_EFFORT_FAST


@pytest.mark.parametrize("task", [Task.GRADER, Task.EVALUATOR, Task.EXTRACTOR])
def test_grading_keeps_more_depth(task):
    assert reasoning_effort_for(task) == settings.LLM_REASONING_EFFORT_DEEP


def test_effort_can_be_switched_off(monkeypatch):
    """A future model that rejects the parameter must not break every call."""
    monkeypatch.setattr(settings, "LLM_SEND_REASONING_EFFORT", False)
    assert reasoning_effort_for(Task.QUESTION) is None


@pytest.mark.asyncio
async def test_effort_is_passed_to_the_provider(fake_provider):
    await complete_text(task=Task.QUESTION, system="s", user="u")
    assert fake_provider.calls[0]["reasoning_effort"] == settings.LLM_REASONING_EFFORT_FAST
    assert fake_provider.calls[0]["max_tokens"] == budget_for(Task.QUESTION)


# ── Truncation is not the same as an empty answer ─────────────────────────────


@pytest.mark.asyncio
async def test_budget_exhausted_by_reasoning_says_so(monkeypatch):
    provider = FakeProvider(text="", finish_reason="length")
    llm.set_providers_for_tests([provider])
    monkeypatch.setattr(llm, "record_usage", lambda *a, **k: None)
    try:
        with pytest.raises(LLMTruncated) as exc:
            await complete_text(task=Task.QUESTION, system="s", user="u")
        message = str(exc.value)
        # The message has to point at the actual cause and the actual fix.
        assert "reasoning" in message
        assert "budget" in message or "LLM_REASONING_EFFORT" in message
    finally:
        llm.set_providers_for_tests(None)


@pytest.mark.asyncio
async def test_an_empty_answer_that_finished_normally_is_a_different_error(monkeypatch):
    provider = FakeProvider(text="", finish_reason="stop")
    llm.set_providers_for_tests([provider])
    monkeypatch.setattr(llm, "record_usage", lambda *a, **k: None)
    try:
        with pytest.raises(LLMError) as exc:
            await complete_text(task=Task.QUESTION, system="s", user="u")
        assert not isinstance(exc.value, LLMTruncated)
        assert "empty" in str(exc.value).lower()
    finally:
        llm.set_providers_for_tests(None)


def test_truncated_is_an_llm_error_so_callers_still_catch_it():
    """Existing `except LLMError` paths must keep working."""
    assert issubclass(LLMTruncated, LLMError)


# ── Test isolation ────────────────────────────────────────────────────────────


def test_the_suite_does_not_read_the_developers_dotenv():
    """Guards a bug this suite actually had.

    Adding real credentials to `.env` made six tests fail, because they started talking
    to the live Upstash instance. A test run must not be able to reach — or pollute —
    real infrastructure, nor send real email.
    """
    import os

    assert os.environ.get("PLACEPREP_ENV_FILE") == "", (
        "conftest must disable dotenv loading before importing app.core.config"
    )
    assert not settings.UPSTASH_REDIS_REST_URL
    assert not settings.RESEND_API_KEY
    assert not settings.SUPABASE_SERVICE_ROLE_KEY
    assert settings.DATABASE_URL.startswith("sqlite")
