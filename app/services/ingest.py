"""Normalise a fetched feed into Article rows.

Deliberately split at the network boundary: `build_conditional_headers` and
`parse_and_persist` are the two halves of one ingest. services/scheduler.py
fetches concurrently and then calls `parse_and_persist`, so nothing here touches
the network. feedparser is synchronous and CPU-bound, so it runs on a worker
thread; everything else is on the loop. See CLAUDE.md's runtime constraints.
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.hashing import article_key, content_hash
from app.core.text import strip_markup
from app.db.models.article import Article
from app.db.models.city import City
from app.db.models.source import Source
from app.services.sensitivity import rule_based_flags

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
_MAX_AGE_RE = re.compile(r"\bmax-age\s*=\s*(\d+)", re.IGNORECASE)
_NO_STORE_RE = re.compile(r"\bno-(store|cache)\b", re.IGNORECASE)


def canonicalize_url(url: str) -> str:
    """Strip tracking params and normalise trailing slashes before hashing —
    two URLs differing only by a utm_source param must produce the same
    Article.id, or the same story gets ingested twice under different ids."""
    parts = urlsplit(url)
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))


def extract_city(url: str) -> str | None:
    """Pull the city out of a `/city/<name>/...` URL path, casefolded.

    Returns None when the path carries no city segment. Deliberately no
    fallback to the Source's own city: this column is the ingested fact, and
    filling it with a city we cannot actually read off the URL would destroy the
    one key that lets a city onboarded later be backfilled against articles
    already stored. See CLAUDE.md's domain rules.
    """
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    for index, segment in enumerate(segments[:-1]):
        if segment.casefold() == "city":
            return segments[index + 1].casefold()
    return None


@dataclass
class SourceIngestOutcome:
    source_id: int
    ok: bool
    new_articles: int
    error: str | None
    not_modified: bool = False


def build_conditional_headers(source: Source) -> dict[str, str]:
    headers = {}
    if source.etag:
        headers["If-None-Match"] = source.etag
    if source.last_modified:
        headers["If-Modified-Since"] = source.last_modified
    return headers


def parse_max_age(headers: httpx.Headers) -> int | None:
    """`cache-control: max-age` becomes a floor on the poll interval. A
    no-store/no-cache response declares nothing useful about when to come
    back, so it contributes no floor."""
    directive = headers.get("cache-control")
    if not directive or _NO_STORE_RE.search(directive):
        return None
    match = _MAX_AGE_RE.search(directive)
    return int(match.group(1)) if match else None


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


async def city_slug_map(db: AsyncSession) -> dict[str, int]:
    """`{slug: city_id}` for resolving an article's parsed city.

    Built once per feed rather than queried per article: a tick can carry a few
    hundred entries and there are only ever a handful of cities.
    """
    rows = (await db.execute(select(City.id, City.slug))).all()
    return {slug: city_id for city_id, slug in rows}


def _normalize_entry(
    source: Source, entry, feed_position: int, cities: dict[str, int]
) -> Article | None:
    link = entry.get("link")
    raw_title = entry.get("title")
    if not link or not raw_title:
        return None

    canonical = canonicalize_url(link)
    raw_summary = entry.get("summary")
    tags = entry.get("tags") or []
    category_raw = tags[0].get("term") if tags else None

    # Feeds routinely double-escape, so XML parsing leaves a layer behind:
    # "plug-&amp;-play" reaches us still entity-encoded and would render that
    # way. Unescape once, here at the boundary, so everything downstream — the
    # dedupe key, the sensitivity regex, the API payload — sees real text.
    # `raw` deliberately keeps what the feed actually said.
    title = html.unescape(raw_title)
    # Descriptions arrive as markup — a CDATA block holding an <a><img/></a>
    # thumbnail, sometimes with prose after it, sometimes not. Keep the text and
    # drop the tags here at the boundary, so every reader downstream gets prose:
    # the card, the sensitivity regex, the dedupe content_hash, and above all
    # the classifier's prompt, which was being handed raw <img> attributes as if
    # they were a summary. A description with no prose in it at all becomes NULL
    # rather than an empty string — "we were given no summary" is the truth, and
    # it is the same state 20% of the corpus is already in. `raw` below keeps
    # what the feed actually said.
    summary = strip_markup(html.unescape(raw_summary)) or None if raw_summary else None

    text = f"{title} {summary or ''}"
    return Article(
        id=article_key(canonical, title),
        source_id=source.id,
        url=link,
        canonical_url=canonical,
        title=title,
        summary=summary,
        published_at=_parse_time(entry.get("published_parsed")),
        language=source.language,
        feed_position=feed_position,
        images=_extract_images(entry),
        # The parsed slug is kept whether or not it resolves: it is the
        # ingested fact, and it is what lets a city onboarded later be
        # backfilled against articles already stored.
        city=(parsed_city := extract_city(link)),
        city_id=cities.get(parsed_city) if parsed_city else None,
        sensitivity_flags=[flag.value for flag in rule_based_flags(text)],
        content_hash=content_hash(text),
        raw={
            "title": raw_title,
            "link": link,
            "summary": raw_summary,
            "category_raw": category_raw,
        },
    )


def _as_row(article: Article) -> dict:
    """The columns _normalize_entry set, as a Core insert row. Unset columns are
    left out so their column defaults apply, exactly as an ORM flush would."""
    return {
        column.key: getattr(article, column.key)
        for column in Article.__table__.columns
        if column.key in vars(article)
    }


def record_fetch_failure(source: Source, error: str, now: datetime) -> SourceIngestOutcome:
    """A fetch or parse failure never raises past here — it lands on the Source
    row and the caller moves on. See CLAUDE.md: per-source failures never abort
    a scheduler tick or a run."""
    source.last_error = error
    source.last_error_at = now
    source.last_fetched_at = now
    source.consecutive_failures += 1
    return SourceIngestOutcome(source_id=source.id, ok=False, new_articles=0, error=error)


def _record_fetch_success(source: Source, result_headers: httpx.Headers, now: datetime) -> None:
    # Refresh the validators on every success, 304 included: a server that
    # rotates its ETag on an unchanged body would otherwise make every
    # subsequent poll a full download.
    source.etag = result_headers.get("etag") or source.etag
    source.last_modified = result_headers.get("last-modified") or source.last_modified
    source.cache_max_age_seconds = parse_max_age(result_headers)
    source.last_success_at = now
    source.last_fetched_at = now
    source.consecutive_failures = 0
    source.last_error = None
    source.last_error_at = None


async def parse_and_persist(
    db: AsyncSession,
    source: Source,
    result,
    now: datetime,
    seen_ids: set[str] | None = None,
) -> SourceIngestOutcome:
    """Parse a fetched feed body and insert the entries we don't already have.

    `seen_ids` lets one scheduler tick share dedupe state across sources, so
    two mastheads carrying the same story in the same tick collide before
    either insert runs.

    The insert is ON CONFLICT DO NOTHING on the PK rather than a lookup and an
    add: the PK *is* the dedupe ledger, and a scheduled tick racing a city
    refresh would otherwise both see "absent" and one of them would die on an
    IntegrityError. RETURNING says which rows were actually new.
    """
    seen_ids = seen_ids if seen_ids is not None else set()
    cities = await city_slug_map(db)

    if result.status_code == _NOT_MODIFIED:
        _record_fetch_success(source, result.headers, now)
        return SourceIngestOutcome(
            source_id=source.id, ok=True, new_articles=0, error=None, not_modified=True
        )

    parsed = await asyncio.to_thread(feedparser.parse, result.content)
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
        return record_fetch_failure(source, f"response did not parse as a feed: {reason}", now)

    rows = []
    for position, entry in enumerate(parsed.entries, start=1):
        article = _normalize_entry(source, entry, position, cities)
        if article is None or article.id in seen_ids:
            continue
        seen_ids.add(article.id)
        rows.append(_as_row(article))

    new_count = 0
    if rows:
        inserted = await db.scalars(
            insert(Article)
            .values(rows)
            .on_conflict_do_nothing(index_elements=[Article.id])
            .returning(Article.id)
        )
        new_count = len(inserted.all())

    _record_fetch_success(source, result.headers, now)
    return SourceIngestOutcome(source_id=source.id, ok=True, new_articles=new_count, error=None)
