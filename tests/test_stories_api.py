"""GET /api/v1/stories — the one read contract phase 2 and phase 3 consume.

Ordering is asserted here as well as in test_rank.py on purpose: test_rank
covers the arithmetic, this covers that the endpoint actually applies it to
real rows. The admission test lives here too — `_hidden` is the only place a
story can silently leave the feed, and every row of its truth table is a
distinct bug.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.core.enums import ENGAGEMENT_DIMENSIONS

NOW = datetime.now(UTC)


def _levels(level: str) -> dict[str, str]:
    """All seven together — rank.py treats a partial row as unrated."""
    return dict.fromkeys(ENGAGEMENT_DIMENSIONS, level)


def _titles(response) -> list[str]:
    return [story["title"] for story in response.json()]


async def test_returns_window_articles_newest_first(
    client: AsyncClient, make_source, make_article
) -> None:
    source = await make_source(city="nagpur")
    for title, age in (("oldest", 6), ("middle", 1), ("newest", 0)):
        await make_article(source, title=title, published_at=NOW - timedelta(hours=age))

    response = await client.get("/api/v1/stories")
    assert response.status_code == 200
    assert _titles(response) == ["newest", "middle", "oldest"]


async def test_a_local_outlet_shows_its_own_city_without_one_in_the_url(
    client: AsyncClient, make_source, make_article
) -> None:
    """thelivenagpur.com publishes to /2026/09/15/<slug>, so there is no city in
    the URL to read. The outlet flag is what lets the card show one at all — it
    no longer decides anything about ranking or admission."""
    outlet = await make_source(city="nagpur", is_local_outlet=True)
    await make_article(
        outlet,
        title="Worker dies at Hingna MIDC",
        url="https://thelivenagpur.com/2026/09/15/worker-dies-at-higna-midc",
        city=None,
    )

    story = (await client.get("/api/v1/stories")).json()[0]
    assert story["city"] == "nagpur"


async def test_an_article_with_no_city_in_its_url_reports_none(
    client: AsyncClient, make_source, make_article
) -> None:
    agency = await make_source(city="nagpur", is_local_outlet=False)
    await make_article(agency, title="wire copy", city=None, is_city_relevant=True)
    assert (await client.get("/api/v1/stories")).json()[0]["city"] is None


async def test_article_in_an_unonboarded_city_keeps_its_slug(
    client: AsyncClient, db_session, make_source, make_article
) -> None:
    """The slug is the ingested fact and the only key that lets a city onboarded
    later be backfilled against articles already stored."""
    source = await make_source(city="nagpur")
    article = await make_article(
        source, title="from a city we do not cover", city="nagpur", is_city_relevant=True
    )
    article.city = "kolhapur"
    article.city_id = None
    await db_session.commit()

    story = (await client.get("/api/v1/stories")).json()[0]
    assert story["city"] == "kolhapur"
    assert story["city_id"] is None


# --- the admission test ----------------------------------------------------
#
# Two branches. Unclassified defers to the deterministic derive_is_local;
# classified is fail-open, hidden iff the model said no AND was at least as
# confident as the floor. Every row is asserted because each fails differently —
# a falsy test on is_city_relevant never reaches the fallback at all, and a NOT
# over a NULL comparison drops rows with no confidence recorded.


async def test_an_unclassified_story_falls_back_to_the_url_city(
    client: AsyncClient, make_source, make_article
) -> None:
    """The state every row is in for a classify tick after ingest, and the state
    every row was in immediately after migration f4a2c07e6b91. The deterministic
    test stands in until the model answers, so a refresh never serves a whole
    tick of unfiltered feed."""
    source = await make_source(city="nagpur")
    await make_article(source, title="ours", city="nagpur", is_city_relevant=None)
    await make_article(source, title="someone else's", city="pune", is_city_relevant=None)

    assert _titles(await client.get("/api/v1/stories")) == ["ours"]


async def test_an_unclassified_story_from_a_local_outlet_is_shown(
    client: AsyncClient, make_source, make_article
) -> None:
    """The outlet flag is the other half of the fallback: a genuinely local
    masthead has no reason to put its city in its URLs, and without this every
    article it publishes would be hidden until the classifier reached it."""
    outlet = await make_source(city="nagpur", is_local_outlet=True)
    await make_article(outlet, title="no city in the url", city=None, is_city_relevant=None)

    assert _titles(await client.get("/api/v1/stories")) == ["no city in the url"]


async def test_the_model_overrides_the_fallback_in_both_directions(
    client: AsyncClient, make_source, make_article
) -> None:
    """The fallback is provisional and never gets a veto: a URL-local story the
    model rejected goes, and a URL-foreign story the model accepted stays."""
    source = await make_source(city="nagpur")
    await make_article(
        source,
        title="url says ours, model says no",
        city="nagpur",
        is_city_relevant=False,
        relevance_confidence=0.9,
    )
    await make_article(
        source,
        title="url says elsewhere, model says yes",
        city="pune",
        is_city_relevant=True,
        relevance_confidence=0.9,
    )

    assert _titles(await client.get("/api/v1/stories")) == ["url says elsewhere, model says yes"]


async def test_correcting_a_source_city_re_admits_with_no_re_ingest(
    client: AsyncClient, db_session, make_source, make_article, get_or_make_city
) -> None:
    """Why is_local is derived rather than stored: a source filed under the wrong
    city hides its whole unclassified feed, and fixing the row is the whole fix."""
    source = await make_source(city="pune")
    await make_article(source, title="nagpur story", city="nagpur", is_city_relevant=None)
    assert (await client.get("/api/v1/stories")).json() == []

    source.city_id = (await get_or_make_city("nagpur")).id
    await db_session.commit()
    assert _titles(await client.get("/api/v1/stories")) == ["nagpur story"]


async def test_a_relevant_story_is_shown(client: AsyncClient, make_source, make_article) -> None:
    source = await make_source(city="nagpur")
    await make_article(
        source, title="about nagpur", is_city_relevant=True, relevance_confidence=0.9
    )
    assert _titles(await client.get("/api/v1/stories")) == ["about nagpur"]


async def test_a_confidently_irrelevant_story_is_hidden(
    client: AsyncClient, make_source, make_article
) -> None:
    source = await make_source(city="nagpur")
    await make_article(
        source, title="national wire copy", is_city_relevant=False, relevance_confidence=0.9
    )
    assert (await client.get("/api/v1/stories")).json() == []


async def test_an_unsure_no_keeps_the_story(client: AsyncClient, make_source, make_article) -> None:
    """Confidence gates removal, never admission: an off-city story the editor
    scrolls past costs a second, a local story withheld costs the story."""
    source = await make_source(city="nagpur")
    await make_article(
        source, title="probably not ours", is_city_relevant=False, relevance_confidence=0.1
    )
    assert _titles(await client.get("/api/v1/stories")) == ["probably not ours"]


async def test_a_no_with_no_confidence_recorded_keeps_the_story(
    client: AsyncClient, make_source, make_article
) -> None:
    """Shouldn't happen — the classifier writes both together — but a missing
    confidence must read as "unsure", not raise and not silently hide."""
    source = await make_source(city="nagpur")
    await make_article(
        source, title="no confidence", is_city_relevant=False, relevance_confidence=None
    )
    assert _titles(await client.get("/api/v1/stories")) == ["no confidence"]


async def test_a_relevant_story_is_shown_however_unsure(
    client: AsyncClient, make_source, make_article
) -> None:
    source = await make_source(city="nagpur")
    await make_article(
        source, title="probably ours", is_city_relevant=True, relevance_confidence=0.01
    )
    assert _titles(await client.get("/api/v1/stories")) == ["probably ours"]


async def test_confidence_exactly_at_the_floor_hides_the_story(
    client: AsyncClient, make_source, make_article
) -> None:
    """The boundary is `>=`, asserted so a later `>` is a visible change. The
    default floor is 0.5."""
    source = await make_source(city="nagpur")
    await make_article(
        source, title="at the floor", is_city_relevant=False, relevance_confidence=0.5
    )
    await make_article(
        source, title="just under", is_city_relevant=False, relevance_confidence=0.49
    )

    assert _titles(await client.get("/api/v1/stories")) == ["just under"]


async def test_there_is_no_parameter_that_brings_hidden_stories_back(
    client: AsyncClient, make_source, make_article
) -> None:
    """The admission test is unconditional. `local_only` and `scope` are gone,
    and an unknown query param must not quietly widen the feed."""
    source = await make_source(city="nagpur")
    await make_article(source, title="hidden", is_city_relevant=False, relevance_confidence=0.9)

    for params in ({"local_only": "false"}, {"scope": "national"}):
        assert (await client.get("/api/v1/stories", params=params)).json() == []


# --- the content-type admission test ---------------------------------------
#
# The second hard filter, ANDed with locality. Rows 1, 3, 4 and 5 below are the
# four genuinely distinct bugs; the rest are cheap guards.


async def test_an_untyped_story_is_shown(client: AsyncClient, make_source, make_article) -> None:
    """Fail-open, and the loudest thing to get wrong: this is the state of every
    row for a classify tick after ingest, and of the whole window right after a
    migration that resets the backlog. Inverted, it empties the feed and reads
    exactly like a dead scheduler."""
    source = await make_source(city="nagpur")
    await make_article(source, title="just ingested", is_city_relevant=True, content_type=None)
    assert _titles(await client.get("/api/v1/stories")) == ["just ingested"]


@pytest.mark.parametrize("content_type", ["news", "oddity"])
async def test_a_discoverable_type_is_shown(
    client: AsyncClient, make_source, make_article, content_type: str
) -> None:
    source = await make_source(city="nagpur")
    await make_article(source, title="keep me", is_city_relevant=True, content_type=content_type)
    assert _titles(await client.get("/api/v1/stories")) == ["keep me"]


@pytest.mark.parametrize(
    "content_type",
    ["opinion", "advice_tips", "promotional", "celebrity_entertainment", "explainer", "other"],
)
async def test_junk_types_are_hidden(
    client: AsyncClient, make_source, make_article, content_type: str
) -> None:
    """If this passes when it should fail, the second filter is a silent no-op:
    nothing errors, and the feed looks exactly like it did yesterday."""
    source = await make_source(city="nagpur")
    await make_article(source, title="junk", is_city_relevant=True, content_type=content_type)
    assert (await client.get("/api/v1/stories")).json() == []


async def test_a_discoverable_type_does_not_override_locality(
    client: AsyncClient, make_source, make_article
) -> None:
    """The two tests are ANDed. A NEWS piece the model says is not about this
    city stays out — otherwise content_type becomes the override the feed is
    promised not to have."""
    source = await make_source(city="nagpur")
    await make_article(
        source,
        title="news, but not ours",
        content_type="news",
        is_city_relevant=False,
        relevance_confidence=0.9,
    )
    assert (await client.get("/api/v1/stories")).json() == []


async def test_a_discoverable_type_does_not_override_the_provisional_locality_test(
    client: AsyncClient, make_source, make_article
) -> None:
    """Same as above through the other code path — the one that runs on every
    freshly ingested row."""
    source = await make_source(city="nagpur")
    await make_article(
        source, title="news from elsewhere", city="pune", content_type="news", is_city_relevant=None
    )
    assert (await client.get("/api/v1/stories")).json() == []


async def test_a_junk_type_is_hidden_even_while_locality_is_unjudged(
    client: AsyncClient, make_source, make_article
) -> None:
    """The ordering test. Both locality branches return early, so a content
    check written after them never runs for an unjudged row — and this article,
    whose URL city matches its source, would sail through."""
    source = await make_source(city="nagpur")
    await make_article(
        source, title="promo", city="nagpur", content_type="promotional", is_city_relevant=None
    )
    assert (await client.get("/api/v1/stories")).json() == []


async def test_the_confidence_floor_does_not_leak_into_the_content_test(
    client: AsyncClient, make_source, make_article
) -> None:
    """A sub-floor "no" keeps a story on locality grounds, but says nothing
    about its form — the junk type still removes it."""
    source = await make_source(city="nagpur")
    await make_article(
        source,
        title="unsure no, and an opinion",
        content_type="opinion",
        is_city_relevant=False,
        relevance_confidence=0.1,
    )
    assert (await client.get("/api/v1/stories")).json() == []


async def test_an_unrecognised_content_type_is_treated_as_unjudged(
    client: AsyncClient, make_source, make_article
) -> None:
    """A value written by a newer deploy that was then rolled back. It degrades
    to "shown" rather than being silently deleted — the same way rank.py treats
    an impact tier it does not recognise."""
    source = await make_source(city="nagpur")
    await make_article(
        source, title="from the future", is_city_relevant=True, content_type="listicle"
    )
    assert _titles(await client.get("/api/v1/stories")) == ["from the future"]


async def test_window_excludes_older_articles(
    client: AsyncClient, make_source, make_article
) -> None:
    source = await make_source()
    await make_article(source, title="inside", published_at=NOW - timedelta(hours=23))
    await make_article(source, title="outside", published_at=NOW - timedelta(hours=25))

    assert _titles(await client.get("/api/v1/stories")) == ["inside"]


async def test_window_falls_back_to_fetched_at(
    client: AsyncClient, make_source, make_article
) -> None:
    """published_at is nullable and plenty of feeds omit it — filtering on it
    alone would drop those articles from every response."""
    source = await make_source()
    await make_article(
        source, title="no pubdate", published_at=None, fetched_at=NOW - timedelta(hours=2)
    )

    assert _titles(await client.get("/api/v1/stories")) == ["no pubdate"]


async def test_category_filter(client: AsyncClient, make_source, make_article) -> None:
    source = await make_source()
    await make_article(source, title="a crime", category="crime")
    await make_article(source, title="a match", category="sport")
    await make_article(source, title="unclassified", category=None)

    assert _titles(await client.get("/api/v1/stories", params={"category": "crime"})) == ["a crime"]
    assert sorted(
        _titles(
            await client.get(
                "/api/v1/stories", params=[("category", "crime"), ("category", "sport")]
            )
        )
    ) == ["a crime", "a match"]


async def test_unknown_category_is_rejected(client: AsyncClient) -> None:
    assert (
        await client.get("/api/v1/stories", params={"category": "gardening"})
    ).status_code == 422


async def test_city_filter_selects_a_citys_whole_feed(
    client: AsyncClient, make_source, make_article, get_or_make_city
) -> None:
    """Switching city filters on the *source's* city, so a national story a
    Nagpur masthead carried stays in the Nagpur feed. Filtering on the article's
    own parsed city would drop every story whose URL names no city — most of
    them on real data."""
    nagpur_source = await make_source(city="nagpur")
    pune_source = await make_source(city="pune")
    await make_article(nagpur_source, title="local story", city="nagpur")
    await make_article(
        nagpur_source,
        title="national story carried by a nagpur paper",
        city=None,
        is_city_relevant=True,
    )
    await make_article(pune_source, title="pune story", city="pune", is_city_relevant=True)

    nagpur = (await get_or_make_city("nagpur")).id
    titles = _titles(await client.get("/api/v1/stories", params={"city_id": nagpur}))
    assert sorted(titles) == ["local story", "national story carried by a nagpur paper"]

    pune = (await get_or_make_city("pune")).id
    assert _titles(await client.get("/api/v1/stories", params={"city_id": pune})) == ["pune story"]


async def test_city_filter_omitted_returns_every_city(
    client: AsyncClient, make_source, make_article
) -> None:
    await make_article(await make_source(city="nagpur"), title="a")
    await make_article(await make_source(city="pune"), title="b", is_city_relevant=True)
    assert len((await client.get("/api/v1/stories")).json()) == 2


async def test_limit_and_offset(client: AsyncClient, make_source, make_article) -> None:
    source = await make_source()
    for n in range(5):
        await make_article(source, title=f"story {n}", published_at=NOW - timedelta(minutes=n))

    first = _titles(await client.get("/api/v1/stories", params={"limit": 2}))
    assert first == ["story 0", "story 1"]
    assert _titles(await client.get("/api/v1/stories", params={"limit": 2, "offset": 2})) == [
        "story 2",
        "story 3",
    ]


async def test_limit_is_capped(client: AsyncClient, make_source, make_article) -> None:
    source = await make_source()
    await make_article(source, title="only one")
    response = await client.get("/api/v1/stories", params={"limit": 100_000})
    assert response.status_code == 200


async def test_empty_window_returns_empty_list(client: AsyncClient) -> None:
    """The scheduler dying shows up as an empty 200, never an error — which is
    exactly why it needs an alert. See CLAUDE.md's observability section."""
    response = await client.get("/api/v1/stories")
    assert response.status_code == 200
    assert response.json() == []


async def test_story_shape(client: AsyncClient, make_source, make_article) -> None:
    source = await make_source(city="nagpur", name="TOI Nagpur", publisher_group="toi")
    await make_article(
        source, title="a story", city="nagpur", images=[{"url": "https://i.test/1.jpg"}]
    )

    story = (await client.get("/api/v1/stories")).json()[0]
    assert set(story) == {
        "id",
        "title",
        "summary",
        "url",
        "published_at",
        "source_name",
        "publisher_group",
        "city",
        "city_id",
        "category",
        "score",
        "engagement",
        "feed_position",
        "images",
        "sensitivity_flags",
        "carousel_state",
    }
    assert story["source_name"] == "TOI Nagpur"
    assert story["publisher_group"] == "toi"
    # Still carried for phase 2's renderer even though the feed no longer shows it.
    assert story["images"] == [{"url": "https://i.test/1.jpg"}]


# --- the sort toggle -------------------------------------------------------


async def test_default_sort_is_chronological_regardless_of_score(
    client: AsyncClient, make_source, make_article
) -> None:
    """The default must stay uncorrelated with the score. If it drifts, the
    editor's picks stop being evidence about whether the score works — see
    CLAUDE.md's note on why chronological is the default."""
    source = await make_source()
    await make_article(
        source,
        title="older but stronger",
        published_at=NOW - timedelta(hours=5),
        **_levels("very_high"),
    )
    await make_article(
        source,
        title="fresher but dull",
        published_at=NOW,
        **_levels("very_low"),
    )

    titles = [s["title"] for s in (await client.get("/api/v1/stories")).json()]
    assert titles == ["fresher but dull", "older but stronger"]


async def test_engagement_sort_is_the_score_alone(
    client: AsyncClient, make_source, make_article
) -> None:
    """No day bucket: the oldest story still inside the window tops this list if
    it scores highest. Deliberate — every card carries its publish time."""
    source = await make_source()
    await make_article(
        source,
        title="older but stronger",
        published_at=NOW - timedelta(hours=23),
        **_levels("very_high"),
    )
    await make_article(
        source,
        title="fresher but dull",
        published_at=NOW,
        **_levels("very_low"),
    )

    titles = [
        s["title"]
        for s in (await client.get("/api/v1/stories", params={"sort": "engagement"})).json()
    ]
    assert titles == ["older but stronger", "fresher but dull"]


async def test_a_dull_notice_outranks_a_drama(
    client: AsyncClient, make_source, make_article
) -> None:
    """What impact buys over drama: tomorrow's water cut beats a crime nobody
    has to act on, even though the crime is fresher."""
    source = await make_source()
    await make_article(
        source,
        title="water cut tomorrow",
        published_at=NOW - timedelta(hours=2),
        **_levels("very_high"),
    )
    await make_article(
        source,
        title="settled crime",
        published_at=NOW,
        **_levels("very_low"),
    )

    titles = [
        s["title"]
        for s in (await client.get("/api/v1/stories", params={"sort": "engagement"})).json()
    ]
    assert titles == ["water cut tomorrow", "settled crime"]


async def test_an_unknown_sort_is_rejected(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/stories", params={"sort": "viral"})).status_code == 422


async def test_an_unrated_story_reports_no_engagement_so_the_card_hides_its_score(
    client: AsyncClient, make_source, make_article
) -> None:
    """The card keys off `engagement`. The story is shown — the feed is
    fail-open — and scores the floor, so printing that number would read as a
    verdict rather than a gap in the backlog."""
    source = await make_source()
    await make_article(source, title="fresh")
    story = (await client.get("/api/v1/stories")).json()[0]
    assert story["engagement"] is None
    assert story["score"] == 1.0


async def test_a_partially_rated_story_reports_no_engagement_either(
    client: AsyncClient, make_source, make_article
) -> None:
    """Six of seven is unrated, not nearly rated. Scoring the gap at zero would
    print a confident low number with nothing saying it is a gap."""
    source = await make_source()
    levels = _levels("very_high")
    del levels["novelty"]
    await make_article(source, title="half judged", **levels)
    story = (await client.get("/api/v1/stories")).json()[0]
    assert story["engagement"] is None
    assert story["score"] == 1.0


async def test_a_rated_story_carries_all_seven_levels(
    client: AsyncClient, make_source, make_article
) -> None:
    source = await make_source()
    await make_article(source, title="judged", **_levels("high"))
    story = (await client.get("/api/v1/stories")).json()[0]
    assert story["engagement"] == dict.fromkeys(ENGAGEMENT_DIMENSIONS, "high")
    assert story["score"] == 7.0
