"""Constrained selection, not a sort. See CLAUDE.md's domain rules: sorting
by composite score alone hands you eight crime stories."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass
class SelectionCandidate:
    story_id: int
    category: str
    composite_score: float
    videoability_score: float


@dataclass
class SelectionResult:
    selected_ids: list[int]  # in lineup order
    alternate_ids: list[int]  # in order


def select_lineup(
    candidates: list[SelectionCandidate],
    *,
    lineup_size: int,
    alternate_size: int,
    category_cap: int,
    videoability_floor: float,
) -> SelectionResult:
    ranked = sorted(candidates, key=lambda c: c.composite_score, reverse=True)
    eligible = [c for c in ranked if c.videoability_score >= videoability_floor]

    selected: list[SelectionCandidate] = []
    category_counts: dict[str, int] = defaultdict(int)
    deferred: list[SelectionCandidate] = []

    # Pass 1: respect per-category caps.
    for candidate in eligible:
        if len(selected) >= lineup_size:
            break
        if category_counts[candidate.category] >= category_cap:
            deferred.append(candidate)
            continue
        selected.append(candidate)
        category_counts[candidate.category] += 1

    # Pass 2: fill remaining slots from cap-deferred candidates, best first.
    # The floor is never relaxed here — only the category cap is, and only
    # once the eligible pool has been exhausted at the cap.
    for candidate in deferred:
        if len(selected) >= lineup_size:
            break
        selected.append(candidate)

    selected_ids = {c.story_id for c in selected}
    remaining = [c for c in ranked if c.story_id not in selected_ids]
    alternate_ids = [c.story_id for c in remaining[:alternate_size]]

    return SelectionResult(selected_ids=[c.story_id for c in selected], alternate_ids=alternate_ids)
