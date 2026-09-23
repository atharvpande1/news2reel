"""Deterministic half of sensitivity_flags — the LLM pass supplies the other
half, and the stored value is the union of both. See CLAUDE.md: article text
is untrusted input to the same prompt that returns the safety field, so a
single-sourced flag means one injected instruction (or one ordinary model
miss) silently removes the editor's signal.

Patterns are intentionally narrow and lean toward matching too eagerly rather
than too rarely: a false positive here just costs the editor one glance at an
extra flag, a false negative leaves the LLM pass as the only line of defense.
"""

from __future__ import annotations

import re

from app.core.enums import SensitivityFlag

_PATTERNS: dict[SensitivityFlag, list[re.Pattern]] = {
    SensitivityFlag.MINOR_NAMED: [
        re.compile(r"\b(minor|juvenile)\b[\s\S]{0,40}\b(named|identified as)\b", re.I),
        re.compile(r"\b(named|identified as)\b[\s\S]{0,40}\b(minor|juvenile)\b", re.I),
    ],
    SensitivityFlag.SEXUAL_OFFENCE_DETAIL: [
        re.compile(r"\b(rape|sexual assault|molest(ed|ation)?)\b", re.I),
    ],
    SensitivityFlag.SUB_JUDICE: [
        re.compile(r"\bsub[- ]judice\b", re.I),
        re.compile(r"\b(matter|case) is (currently )?(pending|before the court)\b", re.I),
    ],
    SensitivityFlag.COMMUNAL_FRAMING: [
        re.compile(
            r"\b(hindu|muslim|christian|sikh|community)s?\b[\s\S]{0,60}\b(mob|clash(ed)?|riot(ed)?)\b",
            re.I,
        ),
    ],
    SensitivityFlag.UNVERIFIED_CASUALTY_COUNT: [
        re.compile(
            r"\b(unconfirmed|unverified|reportedly)\b[\s\S]{0,40}\b(dead|died|killed|casualt(y|ies))\b",
            re.I,
        ),
    ],
    SensitivityFlag.NAMED_UNCONVICTED_ACCUSED: [
        re.compile(r"\baccused\b[\s\S]{0,40}\b(named as|identified as)\b", re.I),
    ],
}


def rule_based_flags(text: str) -> set[SensitivityFlag]:
    return {flag for flag, patterns in _PATTERNS.items() if any(p.search(text) for p in patterns)}
