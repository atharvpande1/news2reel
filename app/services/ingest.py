"""Fetch every enabled, non-archived Source's feed and normalise entries into
Article rows. Per-source failures never abort the run — see CLAUDE.md: record
last_error on the Source row, continue, report per-source outcomes."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.net import FetchError, fetch_url
from app.db.models.article import Article
from app.db.models.source import Source
from app.services.similarity import content_hash as compute_content_hash

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "cmpid",
}
_NOT_MODIFIED = 304


def canonicalize_url(url: str) -> str:
    """Strip tracking params and normalise trailing slashes before hashing —
    two URLs differing only by a utm_source param must hash to the same
    Article.id, or the same story gets ingested twice under different ids."""
    parts = urlsplit(url)
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))


def article_id(source_id: int, canonical_url: str) -> str:
    return hashlib.sha256(f"{source_id}:{canonical_url}".encode()).hexdigest()


@dataclass
class SourceIngestOutcome:
    source_id: int
    ok: bool
    new_articles: int
    error: str | None


def _parse_time(struct_time) -> datetime | None:
    if not struct_time:
        return None
    return datetime.fromtimestamp(time.mktime(struct_time), tz=UTC)


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_images(entry) -> list[dict]:
    images = []
    for media in entry.get("media_content", []) or []:
        url = media.get("url")
        if url:
            images.append(
                {
                    "url": url,
                    "width": _to_int(media.get("width")),
                    "height": _to_int(media.get("height")),
                    "credit": None,
                    "caption": None,
                }
            )
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image/"):
            images.append(
                {
                    "url": link.get("href"),
                    "width": None,
                    "height": None,
                    "credit": None,
                    "caption": None,
                }
            )
    return images


def _normalize_entry(source: Source, entry, feed_position: int) -> Article | None:
    link = entry.get("link")
    title = entry.get("title")
    if not link or not title:
        return None

    canonical = canonicalize_url(link)
    summary = entry.get("summary")
    tags = entry.get("tags") or []
    category_raw = tags[0].get("term") if tags else None

    return Article(
        id=article_id(source.id, canonical),
        source_id=source.id,
        url=link,
        canonical_url=canonical,
        title=title,
        summary=summary,
        body_text=None,
        published_at=_parse_time(entry.get("published_parsed")),
        language=source.language,
        feed_position=feed_position,
        category_raw=category_raw,
        images=_extract_images(entry),
        content_hash=compute_content_hash(f"{title} {summary or ''}"),
        raw={"title": title, "link": link, "summary": summary, "category_raw": category_raw},
    )


def ingest_source(db: Session, source: Source, settings: Settings) -> SourceIngestOutcome:
    """Fetch one source's feed and upsert its entries. Never raises for a
    fetch or parse failure — records it on the Source row instead."""
    conditional_headers = {}
    if source.etag:
        conditional_headers["If-None-Match"] = source.etag
    if source.last_modified:
        conditional_headers["If-Modified-Since"] = source.last_modified

    now = datetime.now(UTC)
    try:
        result = fetch_url(source.feed_url, settings, conditional_headers=conditional_headers)
    except FetchError as exc:
        source.last_error = str(exc)
        source.last_error_at = now
        db.flush()
        return SourceIngestOutcome(source_id=source.id, ok=False, new_articles=0, error=str(exc))

    if result.status_code == _NOT_MODIFIED:
        source.last_success_at = now
        db.flush()
        return SourceIngestOutcome(source_id=source.id, ok=True, new_articles=0, error=None)

    parsed = feedparser.parse(result.content)
    # parsed.version == "" means feedparser couldn't identify this as any
    # known feed format at all (e.g. plain HTML) — a more reliable signal
    # than bozo, which feedparser doesn't set for non-feed-shaped input, only
    # for malformed-but-feed-shaped XML. A legitimately empty RSS feed (valid
    # format, zero items today) has a non-empty version and must not be
    # flagged as broken.
    if not getattr(parsed, "version", ""):
        # getattr, not parsed.version directly: feedparser's FeedParserDict
        # raises AttributeError rather than returning "" when the key is
        # altogether absent (e.g. a truly empty response body) — that must
        # become a recorded per-source error, not an uncaught crash.
        reason = parsed.bozo_exception if parsed.bozo else "unrecognized format"
        error = f"response did not parse as a feed: {reason}"
        source.last_error = error
        source.last_error_at = now
        db.flush()
        return SourceIngestOutcome(source_id=source.id, ok=False, new_articles=0, error=error)

    new_count = 0
    for position, entry in enumerate(parsed.entries, start=1):
        article = _normalize_entry(source, entry, position)
        if article is None:
            continue
        if db.get(Article, article.id) is None:
            db.add(article)
            new_count += 1

    source.etag = result.headers.get("etag") or source.etag
    source.last_modified = result.headers.get("last-modified") or source.last_modified
    source.last_success_at = now
    source.last_error = None
    source.last_error_at = None
    db.flush()

    return SourceIngestOutcome(source_id=source.id, ok=True, new_articles=new_count, error=None)


def ingest_all(db: Session, settings: Settings) -> list[SourceIngestOutcome]:
    sources = list(
        db.scalars(select(Source).where(Source.enabled.is_(True), Source.archived_at.is_(None)))
    )
    return [ingest_source(db, source, settings) for source in sources]
