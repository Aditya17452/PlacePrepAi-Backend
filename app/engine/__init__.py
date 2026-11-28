"""The adaptive interview engine — pure, deterministic, no I/O.

Empty until Phase 3. The modules planned here (PLAN.md §4 Phase 3) are competency
mapping, ability estimation, question selection, difficulty, weighting, rubric
generation, grading, scoring, the stopping rule, similarity and the decision trace.

The seam already exists: `app.services.interview_agent.decide_next_question` takes the JD
requirements and candidate context as arguments rather than fetching them, so the engine
can replace its body without touching the HTTP layer.

Nothing in this package may import from `app.api`, `app.models` or `app.integrations` —
that is what makes it unit-testable against a scripted fake LLM.
"""

from __future__ import annotations

__all__: list[str] = []
