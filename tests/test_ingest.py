"""Ingestion against a real local HTTP server (not a mocked feedparser call)
so the whole path — SSRF-safe fetch, conditional GET, feed parsing,
normalisation, dedupe, per-source failure isolation — is exercised together.

`_ingest_one` stands in for what services/scheduler.py does per source: fetch,
then hand the result to parse_and_persist. The concurrent orchestration around
it is tested in test_scheduler.py.
"""

import html
import http.server
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.core.config import Settings
from app.core.net import FetchError, fetch_url
from app.db.models.article import Article
from app.services.ingest import (
    build_conditional_headers,
    canonicalize_url,
    extract_city,
    parse_and_persist,
    parse_max_age,
    record_fetch_failure,
)

FIRE_ITEM = """  <item>
    <title>Fire breaks out at Butibori factory</title>
    <link>https://example.test/news/butibori-fire?utm_source=twitter</link>
    <description>A fire broke out at a chemical factory in Butibori.</description>
    <category>accident</category>
  </item>
"""

DRAINAGE_ITEM = """  <item>
    <title>City council approves drainage project</title>
    <link>https://example.test/news/drainage-project</link>
    <description>The civic body approved a new drainage project.</description>
    <category>civic</category>
  </item>
"""

# Times of India's real shape: the description is a CDATA block whose entire
# contents are a thumbnail anchor. There is no prose in it at all.
THUMBNAIL_ONLY_ITEM = """  <item>
    <title>Stay addicted to dreams, not drugs: Jitesh to youth</title>
    <link>https://example.test/city/nagpur/jitesh-to-youth</link>
    <description><![CDATA[<a href="https://example.test/x"><img align="left"
      border="0" src="https://example.test/photo.cms" style="margin-top: 3px;"
      /></a>]]></description>
  </item>
"""

# The same shape, but with the prose the thumbnail is wrapped around.
THUMBNAIL_PLUS_PROSE_ITEM = """  <item>
    <title>Metro crosses three lakh riders</title>
    <link>https://example.test/city/nagpur/metro-riders</link>
    <description><![CDATA[<a href="https://example.test/y"><img
      src="https://example.test/p.cms" /></a>Nagpur Metro recorded over 3 lakh
      passengers by 8pm on Friday.]]></description>
  </item>
"""

# Same link as FIRE_ITEM, different headline — a corrected title is a new row.
FIRE_ITEM_RETITLED = """  <item>
    <title>Blaze guts Butibori chemical unit</title>
    <link>https://example.test/news/butibori-fire</link>
    <description>A fire broke out at a chemical factory in Butibori.</description>
  </item>
"""


CITY_ITEM = """  <item>
    <title>Fire at Butibori unit</title>
    <link>https://example.test/city/nagpur/fire-at-unit/articleshow/9.cms</link>
    <description>A fire broke out at a chemical factory in Butibori.</description>
    <enclosure type="image/jpeg" url="https://static.toiimg.com/photo/msid-1,imgsize-2.jpg"/>
  </item>
"""


ENTITY_ITEM = """  <item>
    <title>Reduce charges for plug-&amp;amp;-play sheds: Butibori &amp;lt;industry&amp;gt;</title>
    <link>https://example.test/city/nagpur/plug-and-play/articleshow/7.cms</link>
    <description>Costs rose 40&amp;#37; said the body &amp;amp; the council.</description>
  </item>
"""


def _feed(*items: str) -> bytes:
    body = "".join(items)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Test Times</title>
{body}</channel>
</rss>
""".encode()


class _FeedHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def _send(self, body: bytes, *, etag: str | None = None, cache_control: str | None = None):
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        if etag:
            self.send_header("ETag", etag)
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/feed.xml":
            self._send(
                _feed(FIRE_ITEM, DRAINAGE_ITEM),
                etag='"abc123"',
                cache_control="public, must-revalidate, max-age=705",
            )
        elif self.path == "/thumbnails.xml":
            self._send(_feed(THUMBNAIL_ONLY_ITEM, THUMBNAIL_PLUS_PROSE_ITEM))
        elif self.path == "/syndicated.xml":
            # A second masthead carrying the identical fire story.
            self._send(_feed(FIRE_ITEM), etag='"syn"')
        elif self.path == "/entities.xml":
            self._send(_feed(ENTITY_ITEM), etag='"ent"')
        elif self.path == "/city-feed.xml":
            self._send(_feed(CITY_ITEM), etag='"city"')
        elif self.path == "/retitled.xml":
            self._send(_feed(FIRE_ITEM_RETITLED), etag='"ret"')
        elif self.path == "/not-a-feed":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><body>not a feed</body></html>")
        elif self.path == "/conditional":
            if self.headers.get("If-None-Match") == '"cached"':
                # Rotates the validator on an unchanged body — the case that
                # makes every later poll a full download if 304s don't refresh.
                self.send_response(304)
                self.send_header("ETag", '"rotated"')
                self.end_headers()
            else:
                self._send(_feed(FIRE_ITEM, DRAINAGE_ITEM), etag='"cached"')
        else:
            self.send_response(404)
            self.end_headers()


async def _ingest_one(db, source, settings=None, seen_ids=None):
    """One source's worth of a scheduler tick."""
    settings = settings or Settings()
    now = datetime.now(UTC)
    try:
        result = await fetch_url(
            source.feed_url, settings, conditional_headers=build_conditional_headers(source)
        )
    except FetchError as exc:
        outcome = record_fetch_failure(source, str(exc), now)
    else:
        outcome = await parse_and_persist(db, source, result, now, seen_ids=seen_ids)
    await db.commit()
    return outcome


async def test_canonicalize_url_strips_tracking_params_and_trailing_slash() -> None:
    assert canonicalize_url("https://example.test/a/b/?utm_source=x&id=1") == canonicalize_url(
        "https://example.test/a/b?id=1"
    )


async def test_parse_max_age_reads_directive_and_ignores_no_store() -> None:
    import httpx

    assert parse_max_age(httpx.Headers({"cache-control": "public, max-age=705"})) == 705
    assert parse_max_age(httpx.Headers({"cache-control": "no-store, max-age=60"})) is None
    assert parse_max_age(httpx.Headers({})) is None


async def test_ingest_normalizes_entries_and_records_success(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    with local_http_server(_FeedHandler) as base_url:
        source = await make_source(feed_url=f"{base_url}/feed.xml")
        outcome = await _ingest_one(db_session, source)

    assert outcome.ok is True
    assert outcome.new_articles == 2

    articles = (
        await db_session.scalars(
            select(Article).where(Article.source_id == source.id).order_by(Article.feed_position)
        )
    ).all()
    assert len(articles) == 2
    assert articles[0].title == "Fire breaks out at Butibori factory"
    assert "utm_source" not in articles[0].canonical_url
    # The feed's own section label survives only in `raw` — nothing reads it.
    assert articles[0].raw["category_raw"] == "accident"
    assert articles[0].feed_position == 1

    await db_session.refresh(source)
    assert source.last_success_at is not None
    assert source.last_fetched_at is not None
    assert source.last_error is None
    assert source.etag == '"abc123"'
    assert source.consecutive_failures == 0
    # The origin's own cache hint, kept as a floor on the poll interval.
    assert source.cache_max_age_seconds == 705


async def test_ingest_is_idempotent(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    with local_http_server(_FeedHandler) as base_url:
        source = await make_source(feed_url=f"{base_url}/feed.xml")
        await _ingest_one(db_session, source)
        second = await _ingest_one(db_session, source)

    assert second.new_articles == 0
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Article).where(Article.source_id == source.id)
        )
    ) == 2


async def test_same_story_from_two_sources_is_stored_once(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    """The dedupe key is URL+title with no source_id, so the second masthead's
    copy collides with the first and is skipped rather than inserted."""
    with local_http_server(_FeedHandler) as base_url:
        first = await make_source(feed_url=f"{base_url}/feed.xml", publisher_group="a")
        second = await make_source(feed_url=f"{base_url}/syndicated.xml", publisher_group="b")
        first_outcome = await _ingest_one(db_session, first)
        second_outcome = await _ingest_one(db_session, second)

    assert first_outcome.new_articles == 2
    assert second_outcome.new_articles == 0
    assert (await db_session.scalar(select(func.count()).select_from(Article))) == 2
    # First source to deliver it owns the row.
    fire = (
        await db_session.scalars(select(Article).where(Article.title.like("Fire breaks%")))
    ).one()
    assert fire.source_id == first.id


async def test_same_story_shared_within_one_tick_is_stored_once(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    """Two sources in the SAME tick share a seen_ids set, so they collide even
    before either row is flushed."""
    seen_ids: set[str] = set()
    with local_http_server(_FeedHandler) as base_url:
        first = await make_source(feed_url=f"{base_url}/feed.xml", publisher_group="a")
        second = await make_source(feed_url=f"{base_url}/syndicated.xml", publisher_group="b")
        await _ingest_one(db_session, first, seen_ids=seen_ids)
        second_outcome = await _ingest_one(db_session, second, seen_ids=seen_ids)

    assert second_outcome.new_articles == 0
    assert (await db_session.scalar(select(func.count()).select_from(Article))) == 2


async def test_retitled_story_at_same_url_is_a_new_row(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    with local_http_server(_FeedHandler) as base_url:
        first = await make_source(feed_url=f"{base_url}/feed.xml", publisher_group="a")
        retitled = await make_source(feed_url=f"{base_url}/retitled.xml", publisher_group="b")
        await _ingest_one(db_session, first)
        outcome = await _ingest_one(db_session, retitled)

    assert outcome.new_articles == 1
    assert (await db_session.scalar(select(func.count()).select_from(Article))) == 3


async def test_non_feed_response_is_recorded_not_raised(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    with local_http_server(_FeedHandler) as base_url:
        source = await make_source(feed_url=f"{base_url}/not-a-feed")
        outcome = await _ingest_one(db_session, source)

    assert outcome.ok is False
    assert outcome.error is not None
    await db_session.refresh(source)
    assert source.last_error is not None
    assert source.consecutive_failures == 1
    # Stamped even on failure, or the scheduler would retry every tick.
    assert source.last_fetched_at is not None


async def test_failed_fetch_stamps_last_fetched_at_and_increments_failures(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    with local_http_server(_FeedHandler) as base_url:
        source = await make_source(feed_url=f"{base_url}/does-not-exist")
        await _ingest_one(db_session, source)
        await _ingest_one(db_session, source)

    await db_session.refresh(source)
    assert source.last_fetched_at is not None
    assert source.consecutive_failures == 2


async def test_conditional_get_returns_not_modified_and_refreshes_validator(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    with local_http_server(_FeedHandler) as base_url:
        source = await make_source(feed_url=f"{base_url}/conditional")
        first = await _ingest_one(db_session, source)
        assert first.new_articles == 2
        await db_session.refresh(source)
        assert source.etag == '"cached"'

        second = await _ingest_one(db_session, source)  # now sends If-None-Match: "cached"

    assert second.ok is True
    assert second.new_articles == 0
    assert second.not_modified is True
    await db_session.refresh(source)
    # Without this the server's rotated ETag is dropped and every later poll
    # downloads the whole body again.
    assert source.etag == '"rotated"'
    assert source.consecutive_failures == 0


async def test_success_resets_consecutive_failures(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    with local_http_server(_FeedHandler) as base_url:
        source = await make_source(feed_url=f"{base_url}/does-not-exist")
        await _ingest_one(db_session, source)
        await db_session.refresh(source)
        assert source.consecutive_failures == 1

        source.feed_url = f"{base_url}/feed.xml"
        await db_session.commit()
        await _ingest_one(db_session, source)

    await db_session.refresh(source)
    assert source.consecutive_failures == 0
    assert source.last_error is None


async def test_extract_city_reads_the_city_segment() -> None:
    assert (
        extract_city(
            "https://timesofindia.indiatimes.com/city/nagpur/fire-at-unit/articleshow/1.cms"
        )
        == "nagpur"
    )


async def test_extract_city_is_casefolded() -> None:
    """The URL and the source's declared city are compared directly, so both
    sides have to be normalised or a capitalised segment never matches."""
    assert extract_city("https://x.test/city/Nagpur/headline") == "nagpur"


async def test_extract_city_returns_none_without_a_city_segment() -> None:
    # No guessing from the source's own city: an unproven city would make
    # is_local true for an article we have not actually localized.
    assert extract_city("https://x.test/business/markets/headline") is None
    assert extract_city("https://x.test/") is None
    # A trailing /city/ with nothing after it names no city.
    assert extract_city("https://x.test/news/city/") is None


async def test_extract_city_ignores_a_city_segment_elsewhere_in_the_path() -> None:
    assert extract_city("https://x.test/city/pune/the-city/articleshow/2.cms") == "pune"


async def test_ingested_article_resolves_its_city_to_an_id(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    """The slug is what the URL carries; the id is what is_local compares."""
    with local_http_server(_FeedHandler) as base_url:
        source = await make_source(feed_url=f"{base_url}/city-feed.xml", city="nagpur")
        await _ingest_one(db_session, source)

    article = (await db_session.scalars(select(Article))).one()
    assert article.city == "nagpur"
    assert article.city_id == source.city_id


async def test_an_article_in_an_unonboarded_city_keeps_the_slug_without_an_id(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    """A city we do not cover cannot be resolved, but the string is still the
    ingested fact — and it is the only thing that lets that city be backfilled
    the day it is onboarded."""
    with local_http_server(_FeedHandler) as base_url:
        # The feed's articles are under /city/nagpur/, and this source covers Pune.
        source = await make_source(feed_url=f"{base_url}/city-feed.xml", city="pune")
        await _ingest_one(db_session, source)

    article = (await db_session.scalars(select(Article))).one()
    assert article.city == "nagpur"
    assert article.city_id is None
    # Deliberately not the ingesting source's city: a fabricated one here would
    # destroy the only key a later backfill has to work from.
    assert article.city_id != source.city_id


async def test_ingested_article_records_city_and_image(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    with local_http_server(_FeedHandler) as base_url:
        source = await make_source(feed_url=f"{base_url}/city-feed.xml", city="nagpur")
        outcome = await _ingest_one(db_session, source)

    assert outcome.new_articles == 1
    article = (await db_session.scalars(select(Article))).one()
    assert article.city == "nagpur"
    # has_image is derived from this at read time — the enclosure is enough.
    assert article.images and article.images[0]["url"].endswith(".jpg")
    assert article.category is None  # the classifier has not run
    assert article.category_attempts == 0


async def test_html_entities_are_unescaped_at_ingest(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    """Feeds double-escape, so XML parsing leaves a layer behind and the title
    would otherwise reach the renderer as "plug-&amp;-play"."""
    with local_http_server(_FeedHandler) as base_url:
        source = await make_source(feed_url=f"{base_url}/entities.xml", city="nagpur")
        await _ingest_one(db_session, source)

    article = (await db_session.scalars(select(Article))).one()
    assert article.title == "Reduce charges for plug-&-play sheds: Butibori <industry>"
    assert "&amp;" not in article.title
    assert article.summary == "Costs rose 40% said the body & the council."


async def test_raw_keeps_what_the_feed_actually_said(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    """`raw` is the debugging record of the feed's own bytes — unescaping there
    too would leave nothing to compare against when a title looks wrong."""
    with local_http_server(_FeedHandler) as base_url:
        source = await make_source(feed_url=f"{base_url}/entities.xml", city="nagpur")
        await _ingest_one(db_session, source)

    article = (await db_session.scalars(select(Article))).one()
    assert "&amp;" in article.raw["title"]
    assert article.raw["title"] != article.title


async def test_unescaping_makes_differently_escaped_titles_dedupe_together(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    """Two mastheads can escape the same headline differently. Unescaping
    before hashing means the dedupe key sees one story, not two."""
    from app.core.hashing import article_key

    assert article_key("https://x.test/a", "AT&T merger") == article_key(
        "https://x.test/a", html.unescape("AT&amp;T merger")
    )


async def test_a_thumbnail_only_description_becomes_no_summary(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    """Times of India's description is often a CDATA block holding nothing but
    an <a><img/></a>. Stored raw it reaches the classifier's prompt as `<img
    align="left" border="0" ...>` in the summary slot — tokens spent on markup,
    and a story judged on its headline alone without anything saying so. NULL is
    the truthful value: we were given no summary."""
    with local_http_server(_FeedHandler) as base_url:
        source = await make_source(feed_url=f"{base_url}/thumbnails.xml")
        await _ingest_one(db_session, source)

    article = (
        await db_session.scalars(select(Article).where(Article.title.like("Stay addicted%")))
    ).one()
    assert article.summary is None
    # The description the feed actually sent is still on the row.
    assert "<img" in article.raw["summary"]


async def test_prose_survives_the_thumbnail_wrapped_around_it(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    """The other half: strip the tags, keep the sentence. Dropping the whole
    description because it opens with an anchor would throw away the only
    article text there is — no article body is fetched."""
    with local_http_server(_FeedHandler) as base_url:
        source = await make_source(feed_url=f"{base_url}/thumbnails.xml")
        await _ingest_one(db_session, source)

    article = (
        await db_session.scalars(select(Article).where(Article.title.like("Metro crosses%")))
    ).one()
    assert article.summary == "Nagpur Metro recorded over 3 lakh passengers by 8pm on Friday."


async def test_the_dedupe_hash_is_taken_over_the_stripped_text(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    """content_hash and the sensitivity regex both run on title + summary. With
    markup left in, they were scanning href and style attributes."""
    with local_http_server(_FeedHandler) as base_url:
        source = await make_source(feed_url=f"{base_url}/thumbnails.xml")
        await _ingest_one(db_session, source)

    for article in (await db_session.scalars(select(Article))).all():
        assert "<" not in (article.summary or "")
        assert "img" not in article.content_hash
