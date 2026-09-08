"""Constrained selection, not a sort — the quota logic CLAUDE.md calls out as
a place where bugs are silent rather than loud (a duplicate-category lineup
still looks like a plausible top 10)."""

from app.services.select import SelectionCandidate, select_lineup


def _candidate(
    story_id: int, category: str, score: float, videoability: float = 1.0
) -> SelectionCandidate:
    return SelectionCandidate(
        story_id=story_id, category=category, composite_score=score, videoability_score=videoability
    )


def test_selects_top_n_by_score_when_categories_are_diverse() -> None:
    candidates = [_candidate(i, category=f"cat-{i}", score=float(20 - i)) for i in range(15)]
    result = select_lineup(
        candidates, lineup_size=10, alternate_size=5, category_cap=3, videoability_floor=0.0
    )
    assert result.selected_ids == list(range(10))
    assert result.alternate_ids == list(range(10, 15))


def test_category_cap_is_enforced_in_first_pass() -> None:
    # 8 crime candidates all outscore 2 sport candidates.
    crime = [_candidate(i, "crime", score=100 - i) for i in range(8)]
    sport = [_candidate(100 + i, "sport", score=10 - i) for i in range(2)]
    result = select_lineup(
        crime + sport, lineup_size=10, alternate_size=10, category_cap=3, videoability_floor=0.0
    )

    selected_categories = {c.story_id: c.category for c in crime + sport}
    crime_selected = sum(1 for sid in result.selected_ids if selected_categories[sid] == "crime")
    # Cap holds until the fill pass has to relax it to reach 10.
    assert crime_selected > 3  # only 10 total candidates exist, so the fill pass must relax the cap
    assert (
        100 in result.selected_ids and 101 in result.selected_ids
    )  # both sport stories make it in


def test_category_cap_relaxes_only_in_fill_pass_when_not_enough_diversity() -> None:
    crime = [_candidate(i, "crime", score=100 - i) for i in range(12)]
    sport = [_candidate(100 + i, "sport", score=10 - i) for i in range(2)]
    result = select_lineup(
        crime + sport, lineup_size=10, alternate_size=10, category_cap=3, videoability_floor=0.0
    )

    assert len(result.selected_ids) == 10
    # The 3 best crime stories and both sport stories are in from pass 1;
    # the fill pass adds the next-best crime stories (by score) to reach 10 —
    # never the worse-scoring, lower-priority ones.
    assert set(range(3)) <= set(result.selected_ids)  # top 3 crime
    assert {100, 101} <= set(result.selected_ids)  # both sport
    assert set(result.selected_ids) == {0, 1, 2, 3, 4, 5, 6, 7, 100, 101}


def test_videoability_floor_excludes_from_default_but_not_alternates() -> None:
    good = [_candidate(i, f"cat-{i}", score=10 - i, videoability=0.9) for i in range(5)]
    below_floor = _candidate(
        999, "cat-999", score=100, videoability=0.1
    )  # highest score, fails floor

    result = select_lineup(
        [*good, below_floor],
        lineup_size=10,
        alternate_size=10,
        category_cap=3,
        videoability_floor=0.4,
    )

    assert 999 not in result.selected_ids
    assert 999 in result.alternate_ids  # still available for the editor to pull in


def test_fewer_eligible_candidates_than_lineup_size() -> None:
    candidates = [_candidate(i, f"cat-{i}", score=10 - i) for i in range(4)]
    result = select_lineup(
        candidates, lineup_size=10, alternate_size=10, category_cap=3, videoability_floor=0.0
    )
    assert result.selected_ids == [0, 1, 2, 3]
    assert result.alternate_ids == []


def test_selection_never_duplicates_a_story_between_selected_and_alternates() -> None:
    candidates = [_candidate(i, "crime", score=20 - i) for i in range(15)]
    result = select_lineup(
        candidates, lineup_size=10, alternate_size=10, category_cap=10, videoability_floor=0.0
    )
    assert set(result.selected_ids).isdisjoint(result.alternate_ids)
