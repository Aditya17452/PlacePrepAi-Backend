"""Question generation, live feedback and final evaluation.

Phase 1 keeps the existing interview *shape* — two warm-up questions, LLM-written
technical questions, two closing questions — but moves every model call onto
`core.llm`, so there is one retry policy, one timeout, one concurrency gate, structured
output that is validated rather than hoped for, and usage accounting.

The important behavioural change is that **nothing invents a score any more**. The old
code returned a plausible 70 when JSON parsing failed, which is indistinguishable from a
real result on the report. Now a failed evaluation is recorded as failed.

Phase 3 replaces the selection logic in this module with the deterministic engine in
PLAN.md §4; the `decide_next_question` seam below is where that plugs in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from core.config import settings
from core.llm import LLMBusy, LLMError, Task, complete_structured, complete_text
from core.logging_setup import get_logger
from services.session_service import InterviewState

log = get_logger("interview_agent")

WARMUP_QUESTIONS = [
    "Tell me about yourself — your background, skills, and what drew you to this field.",
    "Walk me through your most significant project — what did you build, what was your "
    "role, and what was the biggest challenge?",
]

CLOSING_QUESTIONS = [
    "What is one technical decision you made in a project that you would do differently "
    "now, and why?",
    "Where do you see yourself growing technically in the next two years, and what steps "
    "are you taking toward that?",
]

FILLERS = ("um", "uh", "like", "you know", "basically", "literally")

_UNINFORMATIVE = frozenset(
    {
        "no",
        "yes",
        "ok",
        "okay",
        "idk",
        "i dont know",
        "i don't know",
        "dont know",
        "not sure",
        "n/a",
        "na",
        "",
    }
)


# ── Structured outputs ────────────────────────────────────────────────────────


class LiveFeedback(BaseModel):
    tip: str = Field(max_length=300)
    tone: str = Field(pattern="^(positive|neutral|needs_work)$")


class TopicScore(BaseModel):
    name: str = Field(max_length=120)
    score: float = Field(ge=0, le=100)
    feedback: str = Field(max_length=400)


class Evaluation(BaseModel):
    technical_score: float = Field(ge=0, le=100)
    communication_score: float = Field(ge=0, le=100)
    body_language_score: float = Field(ge=0, le=100)
    overall_score: float = Field(ge=0, le=100)
    verdict: str = Field(pattern="^(Excellent|Good|Needs Work|Poor)$")
    topics: list[TopicScore] = Field(min_length=1, max_length=8)
    strengths: list[str] = Field(min_length=1, max_length=6)
    improvements: list[str] = Field(min_length=1, max_length=6)
    tips: list[str] = Field(min_length=1, max_length=6)


@dataclass
class QuestionDecision:
    question: str | None
    phase: str
    session_complete: bool


# ── Helpers ───────────────────────────────────────────────────────────────────


def normalize_question(text: str) -> str:
    lowered = re.sub(r"[^a-z0-9\s]", "", (text or "").lower().strip())
    return re.sub(r"\s+", " ", lowered).strip()


def is_uninformative(answer: str) -> bool:
    text = (answer or "").strip().lower()
    if text in _UNINFORMATIVE:
        return True
    return len(text.split()) <= 4


def is_repeat(question: str, previous: list[str]) -> bool:
    """Exact / containment check on normalised text.

    Phase 3 adds embedding similarity on top; containment alone misses paraphrases.
    """
    candidate = normalize_question(question)
    if not candidate:
        return True
    for earlier in previous:
        prior = normalize_question(earlier)
        if not prior:
            continue
        if candidate == prior or candidate in prior or prior in candidate:
            return True
    return False


def count_fillers(answer: str) -> int:
    lowered = f" {(answer or '').lower()} "
    return sum(lowered.count(f" {filler} ") for filler in FILLERS)


def fallback_question(role: str, answer: str, previous: list[str]) -> str:
    """Used when the model is unavailable or keeps repeating itself.

    Deliberately generic but still role-aware, and never scored as if the model had
    chosen it — the transcript records which questions came from here.
    """
    if is_uninformative(answer):
        candidates = [
            f"Let us shift focus. What is one core technical concept you believe is "
            f"essential for a {role} role, and how have you applied it?",
            f"Could you walk me through a challenging problem you solved recently that "
            f"is relevant to a {role} position?",
            "Imagine you are given a project with vague requirements. How would you "
            "approach clarifying and executing it?",
            f"What part of the {role} role do you find most interesting, and what "
            "relevant skills do you bring to it?",
        ]
    else:
        candidates = [
            "Can you share one concrete technical decision from that work and the "
            "trade-off you considered?",
            "What did you measure to know it was working, and why that metric?",
            "How did you structure your solution, and what edge cases did you handle?",
            "What part of that system was hardest to build, and how did you solve it?",
        ]
    for candidate in candidates:
        if not is_repeat(candidate, previous):
            return candidate
    return (
        f"Let us move on: which concept in {role} are you most confident about, and why?"
    )


# ── Question selection ────────────────────────────────────────────────────────


def decide_phase(question_number: int) -> str:
    if question_number <= len(WARMUP_QUESTIONS):
        return "warmup"
    if question_number >= settings.MAX_QUESTIONS - 1:
        return "closing"
    return "technical"


def opening_question() -> str:
    return WARMUP_QUESTIONS[0]


async def decide_next_question(
    *,
    state: InterviewState,
    jd_text: str,
    jd_context: str,
    candidate_profile: str,
    answer: str,
) -> QuestionDecision:
    """Choose and write the next question.

    Phase 3 replaces the body of this function with the deterministic engine; the
    signature is the seam, which is why the JD requirements and candidate profile are
    already passed in rather than fetched inside.
    """
    if (
        state.questions_asked >= settings.MAX_QUESTIONS
        or state.elapsed_minutes >= settings.HARD_CUTOFF_MIN
    ):
        return QuestionDecision(question=None, phase="complete", session_complete=True)

    next_number = state.questions_asked + 1
    phase = decide_phase(next_number)
    previous = state.asked_questions()

    if phase == "warmup":
        question = WARMUP_QUESTIONS[next_number - 1]
    elif phase == "closing":
        index = 0 if next_number == settings.MAX_QUESTIONS - 1 else 1
        question = CLOSING_QUESTIONS[index]
    elif is_uninformative(answer):
        question = fallback_question(state.jd_role, answer, previous)
    else:
        question = await _write_technical_question(
            state=state,
            jd_context=jd_context or jd_text[:600],
            candidate_profile=candidate_profile,
            answer=answer,
            question_number=next_number,
        )

    if is_repeat(question, previous):
        question = fallback_question(state.jd_role, answer, previous)

    return QuestionDecision(question=question, phase=phase, session_complete=False)


async def _write_technical_question(
    *,
    state: InterviewState,
    jd_context: str,
    candidate_profile: str,
    answer: str,
    question_number: int,
) -> str:
    profile_section = ""
    if candidate_profile:
        profile_section = (
            "\nCANDIDATE'S VERIFIED PROFILE (from their own links):\n"
            f"{candidate_profile[:1500]}\n"
            "Ground at least some questions in these actual repositories and projects — "
            "name them.\n"
        )

    system = f"""You are a senior engineer conducting a real placement interview for a \
{state.jd_role} role.

ROLE REQUIREMENTS:
{jd_context[:1200]}
{profile_section}
QUESTIONS ALREADY ASKED (never repeat these or a paraphrase):
{chr(10).join(f"- {q}" for q in state.asked_questions()) or "- (none yet)"}

CONVERSATION SO FAR:
{state.memory_as_text()[:2000]}

State: question {question_number} of {settings.MAX_QUESTIONS}, \
{state.elapsed_minutes:.0f} minutes elapsed.

RULES:
- Ask exactly ONE question, at most two sentences.
- Judge them against the role requirements above, not against their degree or branch.
- Go deeper on what they just said when it is substantive; change topic when it is not.
- No compliments, no "great answer", no preamble.
- Answers arrive via speech-to-text. Forgive homophones and mis-transcribed jargon and \
infer the intended meaning.
- If the previous answer dodged the question or was nonsense, open with \
"Let's stay on topic." and then ask your question.

Output only the question."""

    try:
        question = await complete_text(
            task=Task.QUESTION,
            system=system,
            user=f'The candidate said: "{answer[:600]}"\n\nYour next question:',
            temperature=0.8,
            session_id=state.session_id,
            student_id=state.student_id,
        )
        return question.strip().strip('"')
    except LLMBusy:
        # Surface backpressure to the caller rather than silently asking something bland.
        raise
    except LLMError as exc:
        log.warning("question_generation_failed", error=str(exc)[:200])
        return fallback_question(state.jd_role, answer, state.asked_questions())


# ── Live feedback ─────────────────────────────────────────────────────────────


async def live_feedback(state: InterviewState, answer: str) -> LiveFeedback | None:
    """One short coaching tip, every N questions. Returns None if the model is unavailable."""
    vision = state.vision_summary()
    fillers = count_fillers(answer)

    system = (
        "You are a supportive interview coach giving one quick tip. "
        "Be specific, at most two sentences. "
        'Reply as JSON: {"tip": "...", "tone": "positive|neutral|needs_work"}'
    )
    body_language = (
        f"Eye contact {vision['avg_eye_contact']}%, posture {vision['avg_posture']}%."
        if vision["samples"]
        else "No webcam data for this session."
    )
    user = (
        f'Answer: "{answer[:400]}"\n'
        f"Filler words: {fillers}. {body_language}\n"
        "Give one specific, actionable tip."
    )

    try:
        return await complete_structured(
            task=Task.CHEAP,
            system=system,
            user=user,
            schema=LiveFeedback,
            temperature=0.4,
            session_id=state.session_id,
            student_id=state.student_id,
        )
    except (LLMError, LLMBusy) as exc:
        # A missing coaching tip is cosmetic; never fail a turn over it.
        log.info("live_feedback_unavailable", error=str(exc)[:120])
        return None


# ── Final evaluation ──────────────────────────────────────────────────────────


async def evaluate_transcript(
    *,
    session_id: str,
    student_id: str,
    role_title: str,
    jd_text: str,
    transcript: list[tuple[int, str, str]],
    vision: dict[str, float],
    questions_total: int,
) -> Evaluation:
    """Grade a finished interview.

    Raises on failure instead of returning invented numbers — the caller marks the
    evaluation failed and the report says so.
    """
    if len(transcript) < 3:
        raise ValueError(
            f"Only {len(transcript)} answers recorded; too few to evaluate honestly"
        )

    transcript_text = "\n".join(
        f"Q{turn}: {question}\nA{turn}: {answer}"
        for turn, question, answer in transcript
    )
    body_language = (
        f"Eye contact {vision['avg_eye_contact']}%, posture {vision['avg_posture']}% "
        f"across {int(vision['samples'])} samples."
        if vision.get("samples")
        else "No webcam data was recorded; score body language as 0 and say so."
    )

    system = """You are a senior engineer and hiring manager grading a mock interview.

Judge: clarity of thought and structure, depth of real understanding versus buzzwords, \
problem-solving approach, and communication.

Be honest. Shallow or very short answers score 20-40, not 65. A candidate who gave \
one-word replies scores below 30. Do not award a default middling score.

Answers were captured by speech-to-text: forgive homophones and mis-transcribed technical \
terms and grade the intended meaning.

Reply as JSON only, with these keys: technical_score, communication_score, \
body_language_score, overall_score (all 0-100), verdict (exactly one of "Excellent", \
"Good", "Needs Work", "Poor"), topics (3-5 objects with name, score, feedback), \
strengths (3), improvements (3), tips (3)."""

    user = f"""Role: {role_title}

Role requirements:
{jd_text[:900]}

Transcript — {len(transcript)} of {questions_total} questions answered:
{transcript_text[:6000]}

{body_language}

Grade strictly on what was actually said."""

    evaluation = await complete_structured(
        task=Task.EVALUATOR,
        system=system,
        user=user,
        schema=Evaluation,
        temperature=0.2,
        session_id=session_id,
        student_id=student_id,
    )
    log.info(
        "evaluation_complete",
        session_id=session_id,
        overall=evaluation.overall_score,
        verdict=evaluation.verdict,
    )
    return evaluation
