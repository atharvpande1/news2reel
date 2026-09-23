"""Carousel state, the Gallery listing, and the article's "already used" badge.

Three of these are silent by nature. A state that never flips leaves every
carousel reading "draft" forever and nothing errors. An article badge derived
from the wrong table keeps saying "used" for a story the editor deliberately
dropped. And a delete that takes `selections` with it destroys the only
evidence the engagement score has, undetectably — feeds carry a recent window
only, so those picks cannot be rebuilt. See CLAUDE.md.
"""

import base64
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import func, select

from app.core.enums import ENGAGEMENT_DIMENSIONS
from app.db.models.carousel import CarouselSlide
from app.db.models.selection import Selection

JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0 not really a jpeg").decode()


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


async def build(client: AsyncClient, story_ids):
    return await client.post("/api/v1/carousels", json={"story_ids": story_ids})


async def save(client: AsyncClient, carousel_id, slides):
    return await client.put(f"/api/v1/carousels/{carousel_id}", json={"slides": slides})


async def download(client: AsyncClient, carousel_id):
    return await client.post(f"/api/v1/carousels/{carousel_id}/zip", json={"images": [JPEG]})


def as_write(slides):
    """A deck the client would send back: only what cannot be recovered from the
    article."""
    return [
        {"kind": s["kind"], "story_id": s["story_id"], "text": s["text"], "clipped": s["clipped"]}
        for s in slides
    ]


# --- state is the timestamp ------------------------------------------------


async def test_a_fresh_carousel_is_a_draft(client, make_source, make_article):
    article = await _rated(make_article, await make_source(), title="A story")
    body = (await build(client, [article.id])).json()
    assert body["state"] == "draft"
    assert body["last_downloaded_at"] is None


async def test_a_streamed_zip_is_what_makes_it_downloaded(client, make_source, make_article):
    """Only a built archive stamps it. The state has to mean a file reached
    someone, not that a button was pressed."""
    article = await _rated(make_article, await make_source(), title="A story")
    carousel_id = (await build(client, [article.id])).json()["id"]

    assert (await download(client, carousel_id)).status_code == 200

    body = (await client.get(f"/api/v1/carousels/{carousel_id}")).json()
    assert body["state"] == "downloaded"
    assert body["last_downloaded_at"] is not None


async def test_a_rejected_zip_leaves_it_a_draft(client, make_source, make_article):
    """The cap fires before the archive exists, so nothing shipped."""
    article = await _rated(make_article, await make_source(), title="A story")
    carousel_id = (await build(client, [article.id])).json()["id"]

    response = await client.post(
        f"/api/v1/carousels/{carousel_id}/zip", json={"images": [JPEG] * 11}
    )
    assert response.status_code == 422
    assert (await client.get(f"/api/v1/carousels/{carousel_id}")).json()["state"] == "draft"


async def test_editing_after_a_download_keeps_it_downloaded(client, make_source, make_article):
    """ "Downloaded at least once" is the definition. The two timestamps are what
    let the Gallery add "edited since download" on top."""
    article = await _rated(make_article, await make_source(), title="A story")
    built = (await build(client, [article.id])).json()
    await download(client, built["id"])

    edited = (await save(client, built["id"], as_write(built["slides"]))).json()
    assert edited["state"] == "downloaded"
    assert edited["last_edited_at"] > edited["last_downloaded_at"]


# --- the autosave is not a pick --------------------------------------------


async def test_saving_writes_no_selection_rows(client, db_session, make_source, make_article):
    """The one that protects the measurement. If an autosave could add rows, a
    long editing session would inflate the evidence the score is judged on."""
    article = await _rated(make_article, await make_source(), title="A story")
    built = (await build(client, [article.id])).json()
    assert (await db_session.scalar(select(func.count()).select_from(Selection))) == 1

    for _ in range(5):
        await save(client, built["id"], as_write(built["slides"]))
    assert (await db_session.scalar(select(func.count()).select_from(Selection))) == 1


async def test_saving_replaces_the_deck(client, make_source, make_article):
    source = await make_source()
    articles = [await _rated(make_article, source, title=f"Story {n}") for n in range(3)]
    built = (await build(client, [a.id for a in articles])).json()

    kept = [s for s in as_write(built["slides"]) if s["kind"] != "story"]
    body = (await save(client, built["id"], kept)).json()
    assert [s["kind"] for s in body["slides"]] == ["intro", "cta"]


async def test_an_edited_slide_keeps_its_new_text(client, make_source, make_article):
    article = await _rated(make_article, await make_source(), title="A story")
    built = (await build(client, [article.id])).json()

    deck = as_write(built["slides"])
    deck[1]["text"] = "Rewritten by the editor"
    await save(client, built["id"], deck)

    reopened = (await client.get(f"/api/v1/carousels/{built['id']}")).json()
    assert reopened["slides"][1]["text"] == "Rewritten by the editor"
    # Still joined to its article, so the inspector keeps working.
    assert reopened["slides"][1]["story_id"] == article.id
    assert reopened["slides"][1]["score"] > 0


# --- the article's badge ---------------------------------------------------


async def state_of(client, article_id):
    stories = (await client.get("/api/v1/stories")).json()
    return next(s["carousel_state"] for s in stories if s["id"] == article_id)


async def test_an_unused_story_has_no_state(client, make_source, make_article):
    article = await _rated(make_article, await make_source(), title="A story")
    assert await state_of(client, article.id) is None


async def test_a_story_in_a_draft_reads_in_draft(client, make_source, make_article):
    article = await _rated(make_article, await make_source(), title="A story")
    await build(client, [article.id])
    assert await state_of(client, article.id) == "in_draft"


async def test_downloaded_beats_in_draft(client, make_source, make_article):
    """Two decks hold the same story. Having already shipped is the stronger
    claim on it, whichever carousel was touched last."""
    article = await _rated(make_article, await make_source(), title="A story")
    shipped = (await build(client, [article.id])).json()["id"]
    await download(client, shipped)
    await build(client, [article.id])  # and again, into a draft

    assert await state_of(client, article.id) == "downloaded"


async def test_deleting_the_slide_clears_the_badge(client, make_source, make_article):
    """The badge means "a deck holds this now", which is why it reads from
    carousel_slides and not from the append-only selection log."""
    source = await make_source()
    kept = await _rated(make_article, source, title="Kept")
    dropped = await _rated(make_article, source, title="Dropped")
    built = (await build(client, [kept.id, dropped.id])).json()

    deck = [s for s in as_write(built["slides"]) if s["story_id"] != dropped.id]
    await save(client, built["id"], deck)

    assert await state_of(client, kept.id) == "in_draft"
    assert await state_of(client, dropped.id) is None


async def test_deleting_the_carousel_clears_the_badge(client, make_source, make_article):
    article = await _rated(make_article, await make_source(), title="A story")
    carousel_id = (await build(client, [article.id])).json()["id"]

    assert (await client.delete(f"/api/v1/carousels/{carousel_id}")).status_code == 204
    assert await state_of(client, article.id) is None


# --- delete must not take the evidence -------------------------------------


async def test_deleting_a_carousel_keeps_its_selections(
    client, db_session, make_source, make_article
):
    """The one that would be undetectable. Tidying the Gallery must not shrink
    the baseline the engagement score is measured against, and the window has
    long since moved on — those picks could not be rebuilt."""
    article = await _rated(make_article, await make_source(), title="A story")
    carousel_id = (await build(client, [article.id])).json()["id"]
    assert (await db_session.scalars(select(Selection))).one().carousel_id == carousel_id

    await client.delete(f"/api/v1/carousels/{carousel_id}")
    db_session.expunge_all()

    row = (await db_session.scalars(select(Selection))).one()
    assert row.article_id == article.id
    assert row.score > 0
    # The link goes, the evidence stays.
    assert row.carousel_id is None
    # The slides do go — they are working state, not evidence.
    assert (await db_session.scalar(select(func.count()).select_from(CarouselSlide))) == 0


# --- the Gallery listing ---------------------------------------------------


async def gallery(client):
    """No parameters. The Gallery spans cities and is unfiltered — the browser
    narrows it, which is what makes the filter chips' counts free."""
    return await client.get("/api/v1/carousels")


async def test_the_most_recently_worked_on_comes_first(client, make_source, make_article):
    """Sorted on last_edited_at coalesced with created_at, not created_at — an
    old carousel picked back up belongs at the top, and the card prints the same
    value so the dates read straight down the grid."""
    source = await make_source()
    article = await _rated(make_article, source, title="A story")
    older = (await build(client, [article.id])).json()
    newer = (await build(client, [article.id])).json()["id"]

    assert [r["id"] for r in (await gallery(client)).json()] == [newer, older["id"]]

    # Touch the older one. Nothing about created_at changed.
    await save(client, older["id"], as_write(older["slides"]))
    assert [r["id"] for r in (await gallery(client)).json()] == [older["id"], newer]


async def test_every_city_comes_back_from_one_call(client, make_source, make_article):
    """The Gallery is an archive, not a feed: the work you want is as likely to
    be in the city you covered last week. The masthead picker does not reach it."""
    nagpur = await make_source(city="nagpur")
    pune = await make_source(city="pune")
    await build(client, [(await _rated(make_article, nagpur, title="Nagpur story")).id])
    pune_story = await _rated(make_article, pune, title="Pune story", city="pune")
    await build(client, [pune_story.id])

    rows = (await gallery(client)).json()
    assert {r["city_name"] for r in rows} == {"Nagpur", "Pune"}
    # The id as well as the name — the city <select> filters on it.
    assert {r["city_id"] for r in rows} == {nagpur.city_id, pune.city_id}


async def test_a_row_carries_everything_the_card_draws(client, make_source, make_article):
    """City and timestamp identify a row; the count and state fill the card.
    Both timestamps, because the card prints one and badges "edited since
    download" off the pair.

    No slide comes back: the card shows a carousel icon, which is what keeps
    this endpoint a single query."""
    source = await make_source(city="nagpur")
    articles = [await _rated(make_article, source, title=f"Story {n}") for n in range(3)]
    await build(client, [a.id for a in articles])

    row = (await gallery(client)).json()[0]
    assert row["city_name"] == "Nagpur"
    assert row["slide_count"] == 5  # intro + 3 + cta
    assert row["state"] == "draft"
    assert row["created_at"] is not None
    assert row["last_edited_at"] is not None
    assert row["last_downloaded_at"] is None
    assert "cover" not in row


async def test_both_states_come_back_together(client, make_source, make_article):
    """There is no state parameter any more. The chips count over the whole set
    and filter in the browser, so the endpoint must never narrow it."""
    source = await make_source()
    article = await _rated(make_article, source, title="A story")
    shipped = (await build(client, [article.id])).json()["id"]
    draft = (await build(client, [article.id])).json()["id"]
    await download(client, shipped)

    rows = {r["id"]: r["state"] for r in (await gallery(client)).json()}
    assert rows == {shipped: "downloaded", draft: "draft"}


async def test_a_carousel_created_late_at_night_files_under_the_local_day(
    client, db_session, make_source, make_article
):
    """20:00 UTC is 01:30 the next day in Asia/Kolkata. The Gallery groups on
    the local date, so the server sends the instant and the client files it —
    the same trap date_label exists to avoid."""
    from app.db.models.carousel import Carousel

    article = await _rated(make_article, await make_source(), title="A story")
    carousel_id = (await build(client, [article.id])).json()["id"]

    (await db_session.get(Carousel, carousel_id)).created_at = datetime(
        2026, 9, 18, 20, 0, tzinfo=UTC
    )
    await db_session.commit()

    created = (await client.get(f"/api/v1/carousels/{carousel_id}")).json()["created_at"]
    parsed = datetime.fromisoformat(created)
    # Sent as an instant, in UTC — not pre-formatted into a date the client
    # would then have no way to re-zone.
    assert parsed.astimezone(UTC) == datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
    assert (parsed.astimezone(UTC) + timedelta(hours=5, minutes=30)).day == 19
