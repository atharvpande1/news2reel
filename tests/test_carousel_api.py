"""/api/v1/carousels — the selection-to-deck step, the autosave, and the zip.

Three failures here are silent by nature: slide order that ignores engagement
(the carousel reads fine and is wrong), a window filter creeping onto the lookup
(selections silently shrink between the feed and the canvas), and the selections
log going missing with the endpoint it used to live on — which would leave the
engagement score unfalsifiable again, the exact failure that got the previous
rank axes deleted. All three get a test.
"""

import base64
import io
import zipfile
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from httpx import AsyncClient
from sqlalchemy import func, select

from app.core.enums import ENGAGEMENT_DIMENSIONS
from app.db.models.selection import Selection

NOW = datetime.now(UTC)


async def post(client: AsyncClient, story_ids, **extra):
    return await client.post("/api/v1/carousels", json={"story_ids": story_ids, **extra})


async def add(client: AsyncClient, carousel_id, story_ids, **extra):
    """More stories into a deck already on the canvas."""
    return await client.post(
        f"/api/v1/carousels/{carousel_id}/stories", json={"story_ids": story_ids, **extra}
    )


async def _rated(make_article, source, level="high", **overrides):
    """An article the classifier has finished with. All seven engagement
    dimensions are set together, because rank.py treats a partial row as unrated
    — setting one would leave the story scoring the floor with no score shown,
    which is not what any of these tests mean by "rated"."""
    defaults = {
        "is_city_relevant": True,
        "relevance_confidence": 0.9,
        **dict.fromkeys(ENGAGEMENT_DIMENSIONS, level),
    }
    defaults.update(overrides)
    return await make_article(source, **defaults)


# --- deck assembly ---------------------------------------------------------


async def test_a_deck_is_intro_then_stories_then_cta(client, make_source, make_article):
    source = await make_source()
    articles = [await _rated(make_article, source, title=f"Story {n}") for n in range(3)]

    body = (await post(client, [a.id for a in articles])).json()
    kinds = [slide["kind"] for slide in body["slides"]]
    assert kinds == ["intro", "story", "story", "story", "cta"]
    assert body["missing_ids"] == []


async def test_the_intro_carries_the_city_and_the_editorial_date(client, make_source, make_article):
    source = await make_source(city="nagpur")
    article = await _rated(make_article, source, title="A story")

    intro = (await post(client, [article.id])).json()["slides"][0]
    # The story's own publication date in the newsroom's timezone, not now() —
    # at 02:00 IST that would print yesterday.
    expected = article.published_at.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %B %Y")
    assert intro["text"] == f"Top news\nNagpur\n{expected}"
    assert intro["story_id"] is None


async def test_the_cta_closes_the_deck(client, make_source, make_article):
    source = await make_source(city="pune")
    article = await _rated(make_article, source, title="A story")
    cta = (await post(client, [article.id])).json()["slides"][-1]
    assert cta["kind"] == "cta"
    assert "Pune" in cta["text"]


async def test_story_slides_are_ordered_by_engagement_not_date(client, make_source, make_article):
    """A mis-ordered carousel reads perfectly fluently while being wrong."""
    source = await make_source()
    fresh_dull = await _rated(
        make_article,
        source,
        title="fresher but dull",
        published_at=NOW,
        level="very_low",
    )
    old_strong = await _rated(
        make_article,
        source,
        title="older but stronger",
        published_at=NOW - timedelta(hours=6),
        level="very_high",
    )

    body = (await post(client, [fresh_dull.id, old_strong.id])).json()
    stories = [s for s in body["slides"] if s["kind"] == "story"]
    assert [s["story_id"] for s in stories] == [old_strong.id, fresh_dull.id]


async def test_each_story_slide_credits_its_outlet(client, make_source, make_article):
    source = await make_source(name="TOI Nagpur")
    article = await _rated(make_article, source, title="A story")
    story = (await post(client, [article.id])).json()["slides"][1]
    assert story["source_name"] == "TOI Nagpur"
    assert story["story_id"] == article.id


# --- the selection's edges -------------------------------------------------


async def test_more_than_eight_stories_is_rejected(client, make_source, make_article):
    source = await make_source()
    ids = [(await _rated(make_article, source, title=f"Story {n}")).id for n in range(9)]
    assert (await post(client, ids)).status_code == 422


async def test_eight_stories_is_accepted(client, make_source, make_article):
    source = await make_source()
    ids = [(await _rated(make_article, source, title=f"Story {n}")).id for n in range(8)]
    body = (await post(client, ids)).json()
    assert len(body["slides"]) == 10


async def test_duplicate_ids_collapse_to_one_slide(client, make_source, make_article):
    source = await make_source()
    article = await _rated(make_article, source, title="A story")
    body = (await post(client, [article.id, article.id])).json()
    assert len([s for s in body["slides"] if s["kind"] == "story"]) == 1


async def test_an_unknown_id_is_reported_not_fatal(client, make_source, make_article):
    source = await make_source()
    article = await _rated(make_article, source, title="A story")
    body = (await post(client, [article.id, "nope"])).json()
    assert body["missing_ids"] == ["nope"]
    assert len([s for s in body["slides"] if s["kind"] == "story"]) == 1


async def test_all_ids_unknown_is_a_404(client):
    """An empty deck reads as success — exactly the failure worth erroring on."""
    assert (await post(client, ["nope"])).status_code == 404


async def test_a_story_outside_the_window_still_resolves(client, make_source, make_article):
    """The guard against someone helpfully adding the story window filter to
    this lookup. A browser selection routinely outlives the window; the articles
    table outlives it by design."""
    source = await make_source()
    old = await _rated(make_article, source, title="Aged out", published_at=NOW - timedelta(days=5))
    assert (await client.get("/api/v1/stories")).json() == []
    body = (await post(client, [old.id])).json()
    assert len([s for s in body["slides"] if s["kind"] == "story"]) == 1


# --- the selection record --------------------------------------------------


async def test_building_a_deck_records_what_was_picked(
    client, db_session, make_source, make_article
):
    """Moved here from the deleted prompt endpoint. If these rows stop being
    written the engagement score goes back to being unfalsifiable."""
    source = await make_source()
    article = await _rated(
        make_article,
        source,
        title="A story",
        level="very_high",
    )

    carousel_id = (await post(client, [article.id])).json()["id"]

    row = (await db_session.scalars(select(Selection))).one()
    assert row.article_id == article.id
    assert row.engagement == 10.0
    assert row.score == 10.0
    # And which deck it went into: a pick that ended up downloaded is stronger
    # evidence than one abandoned in a draft.
    assert row.carousel_id == carousel_id


async def test_the_sort_the_editor_picked_under_is_recorded(
    client, db_session, make_source, make_article
):
    source = await make_source()
    article = await _rated(make_article, source, title="A story")
    await post(client, [article.id], sort="engagement")
    assert (await db_session.scalars(select(Selection))).one().sort == "engagement"


async def test_a_rejected_selection_is_not_recorded(client, db_session, make_source, make_article):
    """A deck they never got is not a selection."""
    source = await make_source()
    ids = [(await _rated(make_article, source, title=f"Story {n}")).id for n in range(9)]
    assert (await post(client, ids)).status_code == 422
    assert (await post(client, ["nope"])).status_code == 404
    assert (await db_session.scalar(select(func.count()).select_from(Selection))) == 0


# --- the zip ---------------------------------------------------------------

JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0 not really a jpeg").decode()


async def a_carousel(client, make_source, make_article) -> int:
    """The cheapest real carousel — the zip endpoint hangs off one now, because
    a streamed archive is what flips the state to downloaded."""
    article = await _rated(make_article, await make_source(), title="A story")
    return (await post(client, [article.id])).json()["id"]


async def zip_post(client: AsyncClient, images, carousel_id=1):
    return await client.post(f"/api/v1/carousels/{carousel_id}/zip", json={"images": images})


async def test_slides_come_back_as_a_zip(client, make_source, make_article):
    carousel_id = await a_carousel(client, make_source, make_article)
    response = await zip_post(client, [JPEG, JPEG], carousel_id)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        # Numbered, and ours — an uploaded filename could carry path separators,
        # and the numbering is what keeps slide order once they are loose.
        assert archive.namelist() == ["slide-01.jpg", "slide-02.jpg"]


async def test_the_zip_is_stored_not_deflated(client, make_source, make_article):
    """JPEG is already compressed; deflating it again spends CPU to save
    nothing."""
    carousel_id = await a_carousel(client, make_source, make_article)
    response = await zip_post(client, [JPEG], carousel_id)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.infolist()[0].compress_type == zipfile.ZIP_STORED


async def test_a_non_jpeg_data_url_is_rejected(client):
    """The prefix is the only declaration of type we get — without it we would
    be zipping whatever was sent under a .jpg name."""
    png = "data:image/png;base64," + base64.b64encode(b"nope").decode()
    assert (await zip_post(client, [png])).status_code == 422
    assert (await zip_post(client, ["just a string"])).status_code == 422


async def test_malformed_base64_is_rejected(client):
    assert (await zip_post(client, ["data:image/jpeg;base64,!!!not base64!!!"])).status_code == 422


async def test_an_empty_deck_is_rejected(client):
    assert (await zip_post(client, [])).status_code == 422


async def test_more_slides_than_a_carousel_holds_is_rejected(client):
    assert (await zip_post(client, [JPEG] * 11)).status_code == 422


# --- the story's facts reach the slide -------------------------------------


async def test_a_story_slide_carries_what_the_inspector_needs(client, make_source, make_article):
    """The canvas shows the outlet, a link to the original, and the signals that
    got the story picked. All of it exists on StoryRead; the slide is where it
    used to get dropped."""
    source = await make_source(name="TOI Nagpur")
    article = await _rated(
        make_article,
        source,
        title="A story",
        category="civic",
        level="very_high",
    )

    slide = (await post(client, [article.id])).json()["slides"][1]
    assert slide["source_name"] == "TOI Nagpur"
    assert slide["url"] == article.url
    assert slide["category"] == "civic"
    assert slide["score"] == 10.0


async def test_the_bracket_slides_carry_none_of_it(client, make_source, make_article):
    """Which is what lets the inspector drop its source block on a flag it
    already has, rather than a second one to keep in step."""
    source = await make_source()
    article = await _rated(make_article, source, title="A story")

    body = (await post(client, [article.id])).json()
    for slide in (body["slides"][0], body["slides"][-1]):
        assert slide["story_id"] is None
        assert slide["url"] is None
        assert slide["category"] is None
        assert slide["score"] is None


# --- the headline becomes the slide ----------------------------------------
#
# There is no model in this path any more, which makes it easy to assume there
# is nothing to get wrong. The failure is the same one batched hook generation
# had: text landing on the wrong slide. Slides come back in engagement order,
# not the order the ids were sent, so the mapping is still worth pinning down.


async def test_a_slide_carries_its_story_s_headline_verbatim(client, make_source, make_article):
    source = await make_source()
    title = "Water supply cut in Sitabuldi for 12 hours on Thursday"
    article = await _rated(make_article, source, title=title)

    slide = (await post(client, [article.id])).json()["slides"][1]
    assert slide["text"] == title
    assert slide["clipped"] is False


async def test_each_slide_gets_its_own_story_s_headline(client, make_source, make_article):
    """Re-ordered by engagement, so position in the request says nothing about
    position in the deck — every slide still has to carry its own text."""
    source = await make_source()
    weakest = await _rated(make_article, source, title="Weakest", level="very_low")
    strongest = await _rated(
        make_article,
        source,
        title="Strongest",
        level="very_high",
    )
    middle = await _rated(make_article, source, title="Middle", level="high")

    body = (await post(client, [weakest.id, strongest.id, middle.id])).json()
    stories = [s for s in body["slides"] if s["kind"] == "story"]
    assert [s["text"] for s in stories] == ["Strongest", "Middle", "Weakest"]
    # And each one's text belongs to the story it names.
    by_id = {weakest.id: "Weakest", strongest.id: "Strongest", middle.id: "Middle"}
    assert all(s["text"] == by_id[s["story_id"]] for s in stories)


async def test_a_headline_past_the_box_is_cut_on_a_word_boundary(client, make_source, make_article):
    """3.6% of real headlines are this long. A mid-word cut is what the browser
    would do on its own; the whole point of clipping server-side is to beat it."""
    title = (
        "Banerjee, Deshmukh, Arjun Kirtane and Aryan Kirrtane emerge champions at the "
        "Vencobb Paddle Pulse Open 2026 Pickleball and Paddle Tournament"
    )
    article = await _rated(make_article, await make_source(), title=title)

    slide = (await post(client, [article.id])).json()["slides"][1]
    assert slide["clipped"] is True
    assert len(slide["text"]) <= 120
    assert title.startswith(slide["text"])
    # Whole words only: what survived is a prefix that ends where a space did.
    assert title[len(slide["text"])] == " "


async def test_the_bracket_slides_are_never_clipped(client, make_source, make_article):
    """They are built from our own templates, not from feed text."""
    article = await _rated(make_article, await make_source(), title="A story")

    body = (await post(client, [article.id])).json()
    assert body["slides"][0]["clipped"] is False
    assert body["slides"][-1]["clipped"] is False


async def test_a_hostile_headline_is_sanitised_on_the_way_to_the_slide(
    client, make_source, make_article
):
    """Slide text is third-party feed text on the way out as well as in. It
    reaches a textarea's value and a canvas fillText, neither of which parses
    markup — but core/text.py is the one policy and it applies here too."""
    article = await _rated(
        make_article, await make_source(), title="<b>Turnout</b> below <5% in ward 12"
    )

    slide = (await post(client, [article.id])).json()["slides"][1]
    assert "<" not in slide["text"]
    assert "<b>" not in slide["text"]
    assert "Turnout below" in slide["text"]
    assert "5% in ward 12" in slide["text"]


# --- adding to a deck already on the canvas --------------------------------


async def test_added_stories_land_before_the_cta(client, make_source, make_article):
    """The closing slide has to stay closing, and the brackets must not be
    regenerated on top of the ones already there."""
    source = await make_source()
    first = await _rated(make_article, source, title="First")
    later = await _rated(make_article, source, title="Later")

    carousel_id = (await post(client, [first.id])).json()["id"]
    body = (await add(client, carousel_id, [later.id])).json()
    assert [s["kind"] for s in body["slides"]] == ["intro", "story", "story", "cta"]
    assert body["slides"][-1]["text"].startswith("Follow for daily")


async def test_an_added_story_is_still_recorded_as_a_selection(
    client, db_session, make_source, make_article
):
    """The one that matters. Putting a story into the deck is committing to it
    whichever screen the click happened on, and `selections` is the only
    evidence the engagement score has."""
    source = await make_source()
    first = await _rated(make_article, source, title="First")
    later = await _rated(make_article, source, title="Later")

    carousel_id = (await post(client, [first.id])).json()["id"]
    assert (await add(client, carousel_id, [later.id])).status_code == 200

    rows = (await db_session.scalars(select(Selection).order_by(Selection.id))).all()
    assert [r.article_id for r in rows] == [first.id, later.id]
    assert all(r.carousel_id == carousel_id for r in rows)


async def test_re_adding_a_story_already_in_the_deck_is_a_no_op(
    client, db_session, make_source, make_article
):
    """A double click, or a picker that missed one, must not duplicate a slide
    or count the pick twice."""
    article = await _rated(make_article, await make_source(), title="A story")
    carousel_id = (await post(client, [article.id])).json()["id"]

    body = (await add(client, carousel_id, [article.id])).json()
    assert [s["kind"] for s in body["slides"]] == ["intro", "story", "cta"]
    assert (await db_session.scalar(select(func.count()).select_from(Selection))) == 1


async def test_adding_past_the_cap_is_rejected(client, make_source, make_article):
    """The cap is on the deck's stories, not on one request: adding four to five
    already there has to fail the way picking nine at once does."""
    source = await make_source()
    articles = [await _rated(make_article, source, title=f"Story {n}") for n in range(9)]

    carousel_id = (await post(client, [a.id for a in articles[:5]])).json()["id"]
    response = await add(client, carousel_id, [a.id for a in articles[5:]])
    assert response.status_code == 422
    assert "at most 8" in response.json()["detail"]
