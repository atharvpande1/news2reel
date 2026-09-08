"""Ingestion against a real local HTTP server (not a mocked feedparser call)
so the whole path — SSRF-safe fetch, feed parsing, normalisation, per-source
failure isolation — is exercised together."""

import http.server

from app.core.config import Settings
from app.db.models.article import Article
from app.services.ingest import canonicalize_url, ingest_all, ingest_source

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Test Times</title>
  <item>
    <title>Fire breaks out at Butibori factory</title>
    <link>https://example.test/news/butibori-fire?utm_source=twitter</link>
    <description>A fire broke out at a chemical factory in Butibori.</description>
    <category>accident</category>
  </item>
  <item>
    <title>City council approves drainage project</title>
    <link>https://example.test/news/drainage-project</link>
    <description>The civic body approved a new drainage project.</description>
    <category>civic</category>
  </item>
</channel>
</rss>
"""


class _FeedHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/feed.xml":
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("ETag", '"abc123"')
            self.end_headers()
            self.wfile.write(RSS_FEED.encode())
        elif self.path == "/not-a-feed":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><body>not a feed</body></html>")
        elif self.path == "/conditional":
            if self.headers.get("If-None-Match") == '"cached"':
                self.send_response(304)
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("ETag", '"cached"')
                self.end_headers()
                self.wfile.write(RSS_FEED.encode())
        else:
            self.send_response(404)
            self.end_headers()


def test_canonicalize_url_strips_tracking_params_and_trailing_slash() -> None:
    assert canonicalize_url("https://example.test/a/b/?utm_source=x&id=1") == canonicalize_url(
        "https://example.test/a/b?id=1"
    )


def test_ingest_source_normalizes_entries(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    with local_http_server(_FeedHandler) as base_url:
        source = make_source(feed_url=f"{base_url}/feed.xml")
        settings = Settings()
        outcome = ingest_source(db_session, source, settings)

    assert outcome.ok is True
    assert outcome.new_articles == 2

    articles = (
        db_session.query(Article)
        .filter_by(source_id=source.id)
        .order_by(Article.feed_position)
        .all()
    )
    assert len(articles) == 2
    assert articles[0].title == "Fire breaks out at Butibori factory"
    assert "utm_source" not in articles[0].canonical_url
    assert articles[0].category_raw == "accident"
    assert articles[0].feed_position == 1

    db_session.refresh(source)
    assert source.last_success_at is not None
    assert source.last_error is None
    assert source.etag == '"abc123"'


def test_ingest_source_is_idempotent(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    with local_http_server(_FeedHandler) as base_url:
        source = make_source(feed_url=f"{base_url}/feed.xml")
        settings = Settings()
        ingest_source(db_session, source, settings)
        second = ingest_source(db_session, source, settings)

    assert second.new_articles == 0  # already-seen articles are not duplicated
    assert db_session.query(Article).filter_by(source_id=source.id).count() == 2


def test_ingest_source_handles_non_feed_response(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    with local_http_server(_FeedHandler) as base_url:
        source = make_source(feed_url=f"{base_url}/not-a-feed")
        outcome = ingest_source(db_session, source, Settings())

    assert outcome.ok is False
    assert outcome.error is not None
    db_session.refresh(source)
    assert source.last_error is not None


def test_ingest_source_respects_conditional_get(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    with local_http_server(_FeedHandler) as base_url:
        source = make_source(feed_url=f"{base_url}/conditional")
        settings = Settings()
        first = ingest_source(db_session, source, settings)
        assert first.new_articles == 2

        second = ingest_source(db_session, source, settings)  # now carries If-None-Match: "cached"
        assert second.ok is True
        assert second.new_articles == 0


def test_ingest_all_isolates_per_source_failure(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    with local_http_server(_FeedHandler) as base_url:
        good = make_source(feed_url=f"{base_url}/feed.xml", publisher_group="good")
        bad = make_source(feed_url=f"{base_url}/does-not-exist", publisher_group="bad")

        outcomes = ingest_all(db_session, Settings())

    by_source = {o.source_id: o for o in outcomes}
    assert by_source[good.id].ok is True
    assert by_source[good.id].new_articles == 2
    assert by_source[bad.id].ok is False
    assert by_source[bad.id].error is not None
