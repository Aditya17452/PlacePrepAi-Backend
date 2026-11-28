"""Every `settings.X` in the codebase must exist on Settings.

This exists because two did not. Rewriting `core/config.py` as pydantic-settings dropped
`TOP_K_JD_CHUNKS` and `POSTURE_WARN_COOLDOWN_SEC`, and nothing noticed: the first raised
`AttributeError` on *every interview turn* in `rag_service`, and 129 tests passed because
none of them submitted a turn.

A missing setting is invisible until the line runs, which on a hot path means it is
invisible until production. This test makes it a lint-time failure instead.
"""

from __future__ import annotations

import pathlib
import re

from app.core.config import Settings

#: `settings.NAME` where NAME is SHOUTY_CASE — the convention for config values.
REFERENCE = re.compile(r"settings\.([A-Z][A-Z0-9_]*)")

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _declared() -> set[str]:
    return set(Settings.model_fields) | {
        name for name in dir(Settings) if not name.startswith("_")
    }


def _referenced() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted((ROOT / "app").rglob("*.py")):
        for name in REFERENCE.findall(path.read_text(encoding="utf-8")):
            found.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return found


def test_every_referenced_setting_is_declared():
    declared = _declared()
    missing = {
        name: sorted(where)
        for name, where in _referenced().items()
        if name not in declared
    }
    assert not missing, "settings referenced in code but not declared: " + "; ".join(
        f"{name} (in {', '.join(where)})" for name, where in sorted(missing.items())
    )


def test_the_two_that_were_dropped_are_back():
    """Named explicitly so a future config rewrite that drops them fails loudly."""
    declared = _declared()
    assert "TOP_K_JD_CHUNKS" in declared
    assert "POSTURE_WARN_COOLDOWN_SEC" in declared


def test_the_audit_itself_works():
    """A guard test that cannot fail is worse than no guard test."""
    assert "NOT_A_REAL_SETTING_XYZ" not in _declared()
    assert REFERENCE.findall("settings.FOO_BAR and settings.baz") == ["FOO_BAR"]
