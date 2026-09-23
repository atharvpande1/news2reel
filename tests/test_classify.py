"""The classifier pass. Batch index mapping is the silent failure that matters:
a batch that maps title 3's category onto article 1 mislabels everything and
raises nothing, so the mapping is asserted end to end against real rows."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.enums import ENGAGEMENT_DIMENSIONS
from app.db.models.article import Article
from app.services.classify import load_unclassified, run_classify_pass
from app.services.llm import LlmResponse


def _settings(**overrides) -> Settings:
    return Settings(llm_retry_backoff_seconds=0, **overrides)


class ScriptedClient:
    def __init__(self, *bodies: str) -> None:
        self.bodies = list(bodies)
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, *, model, system, prompt, schema) -> LlmResponse:
        self.calls += 1
        self.prompts.append(prompt)
        body = self.bodies.pop(0) if self.bodies else '{"results": []}'
        return LlmResponse(text=body, prompt_tokens=10, completion_tokens=5)


def _result(index, category="other", relevant=True, confidence=0.9, content_type="news", **levels):
    """One classification payload. Keyword-only past the index, deliberately:
    the positional form this replaced already carried a warning that appending a
    field silently re-bound every existing call, and eleven fields is well past
    where that stops being a warning and starts being a promise.

    All seven engagement dimensions default together — rank.py treats a partial
    row as unrated, so a payload missing one does not mean "mostly rated"."""
    return {
        "index": index,
        "category": category,
        "content_type": content_type,
        "is_city_relevant": relevant,
        "confidence": confidence,
        **{name: levels.get(name, "high") for name in ENGAGEMENT_DIMENSIONS},
    }


def _batch(*results) -> str:
    return json.dumps({"results": list(results)})


def _factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)


async def test_categories_land_on_the_right_articles(
    db_session, db_engine, make_source, make_article
):
    source = await make_source()
    fire = await make_article(source, title="Fire breaks out at Butibori factory")
    drainage = await make_article(source, title="City council approves drainage project")
    cricket = await make_article(source, title="Vidarbha beat Mumbai by four wickets")

    # Deliberately out of order in the reply — the mapping must follow the
    # index, not the order the model happened to answer in.
    client = ScriptedClient(
        _batch(_result(2, "sport", False), _result(0, "accident", True), _result(1, "civic", True))
    )
    classified = await run_classify_pass(_factory(db_engine), _settings(), client)

    assert classified == 3
    db_session.expunge_all()
    assert (await db_session.get(Article, fire.id)).category == "accident"
    assert (await db_session.get(Article, fire.id)).is_city_relevant is True
    assert (await db_session.get(Article, drainage.id)).category == "civic"
    assert (await db_session.get(Article, drainage.id)).is_city_relevant is True
    assert (await db_session.get(Article, cricket.id)).category == "sport"
    assert (await db_session.get(Article, cricket.id)).is_city_relevant is False


async def test_content_type_lands_on_the_right_article(
    db_session, db_engine, make_source, make_article
):
    """The new field rides the same index map as everything else, and a mis-map
    here does not mislabel a story — it removes the wrong one from the feed."""
    source = await make_source()
    promo = await make_article(source, title="Kesh King Introduces Scalp First")
    report = await make_article(source, title="Fire breaks out at Butibori factory")

    client = ScriptedClient(
        _batch(
            _result(1, "accident", True, content_type="news"),
            _result(0, "business", True, content_type="promotional"),
        )
    )
    await run_classify_pass(_factory(db_engine), _settings(), client)

    db_session.expunge_all()
    assert (await db_session.get(Article, promo.id)).content_type == "promotional"
    assert (await db_session.get(Article, report.id)).content_type == "news"


async def test_one_call_for_the_whole_batch(db_session, db_engine, make_source, make_article):
    source = await make_source()
    for n in range(5):
        await make_article(source, title=f"Story {n}")

    client = ScriptedClient(_batch(*[_result(n, "other", True) for n in range(5)]))
    await run_classify_pass(_factory(db_engine), _settings(), client)

    assert client.calls == 1  # batching is the entire point


async def test_attempts_increment_even_when_nothing_comes_back(
    db_session, db_engine, make_source, make_article
):
    """Otherwise a title the model refuses is re-sent every pass for the whole window."""
    source = await make_source()
    article = await make_article(source, title="Unclassifiable")

    client = ScriptedClient(_batch())  # valid response, zero results
    await run_classify_pass(_factory(db_engine), _settings(), client)

    db_session.expunge_all()
    refreshed = await db_session.get(Article, article.id)
    assert refreshed.category is None
    assert refreshed.category_attempts == 1


async def test_article_at_the_attempt_ceiling_is_not_reselected(
    db_session, db_engine, make_source, make_article
):
    source = await make_source()
    article = await make_article(source, title="Exhausted")
    article.category_attempts = 3
    await db_session.commit()

    settings = _settings(classify_max_attempts=3)
    assert await load_unclassified(db_session, datetime.now(UTC), settings) == []


async def test_fully_classified_articles_are_not_reselected(
    db_session, db_engine, make_source, make_article
):
    source = await make_source()
    await make_article(
        source,
        title="Done",
        category="crime",
        content_type="news",
        is_city_relevant=True,
        **dict.fromkeys(ENGAGEMENT_DIMENSIONS, "high"),
    )
    assert await load_unclassified(db_session, datetime.now(UTC), _settings()) == []


async def test_articles_judged_before_the_engagement_axes_existed_are_reselected(
    db_session, make_source, make_article
):
    """The predicate is the newest column, emotional_salience — the same move as
    when location_scope, the shareability axes, community_impact,
    is_city_relevant and then content_type were added. A row classified on every
    older axis must still get a pass, or it stays unrated for the rest of its
    life in the window: it scores the floor, shows no rating at all, and can
    never be picked on merit, with nothing on screen saying why."""
    source = await make_source()
    await make_article(
        source,
        title="Old",
        category="crime",
        content_type="news",
        is_city_relevant=True,
        relevance_confidence=0.9,
    )
    assert len(await load_unclassified(db_session, datetime.now(UTC), _settings())) == 1


async def test_a_partially_rated_article_is_reselected(db_session, make_source, make_article):
    """Six of seven is not rated. The predicate only reads one column, so this
    passes for free today — it is here because the day someone keys the
    predicate on something else, a six-of-seven row would go unrated forever and
    rank.py would silently give it the floor."""
    source = await make_source()
    levels = dict.fromkeys(ENGAGEMENT_DIMENSIONS, "high")
    del levels["emotional_salience"]
    await make_article(source, title="Half done", category="crime", content_type="news", **levels)
    assert len(await load_unclassified(db_session, datetime.now(UTC), _settings())) == 1


async def test_load_returns_the_feed_city(db_session, make_source, make_article):
    """Relevance is judged relative to the feed's city, so it has to travel
    with the title."""
    source = await make_source(city="pune")
    await make_article(source, title="A story", summary="Something happened.")
    batch = await load_unclassified(db_session, datetime.now(UTC), _settings())
    # The display name, not the slug: the model reads it, and "Pune" is what a
    # person would write.
    assert [(row[1], row[2], row[3]) for row in batch] == [
        ("A story", "Something happened.", "Pune")
    ]


async def test_confidence_is_clamped_on_write(db_session, db_engine, make_source, make_article):
    """The request schema can't carry min/max, so the bound is enforced here."""
    source = await make_source()
    article = await make_article(source, title="Out of range")
    client = ScriptedClient(_batch(_result(0, "crime", True, 4.2)))
    await run_classify_pass(_factory(db_engine), _settings(), client)

    db_session.expunge_all()
    assert (await db_session.get(Article, article.id)).relevance_confidence == 1.0


async def test_an_unknown_engagement_level_fails_validation(
    db_session, db_engine, make_source, make_article
):
    """The enum is the bound here, not a clamp — pydantic rejects the whole
    batch, the attempt is counted, and every dimension stays null rather than
    six being written and one guessed at. A row rated on six of seven would
    score the floor while looking rated."""
    source = await make_source()
    article = await make_article(source, title="Bad level")
    client = ScriptedClient(*[_batch(_result(0, "crime", True, novelty="medium"))] * 3)
    await run_classify_pass(_factory(db_engine), _settings(), client)

    db_session.expunge_all()
    refreshed = await db_session.get(Article, article.id)
    assert all(getattr(refreshed, name) is None for name in ENGAGEMENT_DIMENSIONS)
    assert refreshed.category_attempts == 1


async def test_batch_size_bounds_the_candidates(db_session, make_source, make_article):
    source = await make_source()
    for n in range(10):
        await make_article(source, title=f"Story {n}")

    batch = await load_unclassified(db_session, datetime.now(UTC), _settings(classify_batch_size=4))
    assert len(batch) == 4


async def test_articles_outside_the_window_are_not_classified(
    db_session, make_source, make_article
):
    source = await make_source()
    await make_article(source, title="Ancient", fetched_at=datetime.now(UTC) - timedelta(hours=72))

    assert await load_unclassified(db_session, datetime.now(UTC), _settings()) == []


async def test_a_failing_batch_leaves_categories_null_without_raising(
    db_session, db_engine, make_source, make_article
):
    """A dead LLM must never take the pass — or the loop — down with it."""
    source = await make_source()
    article = await make_article(source, title="Fire at factory")

    client = ScriptedClient("garbage", "garbage", "garbage")
    classified = await run_classify_pass(_factory(db_engine), _settings(), client)

    assert classified == 0
    db_session.expunge_all()
    refreshed = await db_session.get(Article, article.id)
    assert refreshed.category is None
    assert refreshed.category_attempts == 1


async def test_empty_backlog_makes_no_call(db_session, db_engine):
    client = ScriptedClient()
    assert (await run_classify_pass(_factory(db_engine), _settings(), client)) == 0
    assert client.calls == 0
