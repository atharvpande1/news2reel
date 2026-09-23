"""The scheduler's due-time arithmetic is pure and is where a silent bug costs
the most — a source that is never due stops being ingested without any error
surfacing anywhere. Tested directly, plus one end-to-end tick against a real
local server to cover the concurrency bound and per-source failure isolation.
"""

import asyncio
import http.server
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.models.article import Article
from app.services import scheduler
from app.services.scheduler import (
    CityNotFoundError,
    RefreshInProgressError,
    due_sources,
    effective_interval_seconds,
    is_due,
    refresh_city,
    run_tick,
)

RSS_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Test Times</title>
  <item>
    <title>Fire breaks out at Butibori factory</title>
    <link>https://example.test/news/butibori-fire</link>
    <description>A fire broke out at a chemical factory in Butibori.</description>
  </item>
</channel>
</rss>
"""


class _ConcurrencyHandler(http.server.BaseHTTPRequestHandler):
    """Records how many requests were ever in flight at once, so the semaphore
    bound is asserted against observed behaviour rather than trusted."""

    lock = threading.Lock()
    in_flight = 0
    max_in_flight = 0

    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.in_flight = 0
            cls.max_in_flight = 0

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        with _ConcurrencyHandler.lock:
            _ConcurrencyHandler.in_flight += 1
            _ConcurrencyHandler.max_in_flight = max(
                _ConcurrencyHandler.max_in_flight, _ConcurrencyHandler.in_flight
            )
        try:
            if self.path == "/missing":
                self.send_response(404)
                self.end_headers()
                return
            time.sleep(0.15)  # long enough for overlap to be observable
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()
            self.wfile.write(RSS_FEED)
        finally:
            with _ConcurrencyHandler.lock:
                _ConcurrencyHandler.in_flight -= 1


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


async def test_never_fetched_source_is_due(make_source) -> None:
    source = await make_source()
    assert source.last_fetched_at is None
    assert is_due(source, datetime.now(UTC), _settings()) is True


async def test_source_within_its_interval_is_not_due(make_source) -> None:
    now = datetime.now(UTC)
    source = await make_source(
        fetch_interval_minutes=30, last_fetched_at=now - timedelta(minutes=10)
    )
    assert is_due(source, now, _settings()) is False


async def test_source_past_its_interval_is_due(make_source) -> None:
    now = datetime.now(UTC)
    source = await make_source(
        fetch_interval_minutes=30, last_fetched_at=now - timedelta(minutes=31)
    )
    assert is_due(source, now, _settings()) is True


async def test_intervals_are_per_source(make_source) -> None:
    """The whole point of the change: a fast feed and a slow feed on the same
    tick must not be polled at the same cadence."""
    now = datetime.now(UTC)
    fast = await make_source(fetch_interval_minutes=5, last_fetched_at=now - timedelta(minutes=10))
    slow = await make_source(
        fetch_interval_minutes=360, last_fetched_at=now - timedelta(minutes=10)
    )
    assert due_sources([fast, slow], now, _settings()) == [fast]


async def test_cache_max_age_raises_the_interval_but_never_lowers_it(make_source) -> None:
    now = datetime.now(UTC)
    # Origin says content is good for an hour; our 10m interval is overridden.
    throttled = await make_source(fetch_interval_minutes=10, cache_max_age_seconds=3600)
    throttled.last_fetched_at = now - timedelta(minutes=30)
    assert is_due(throttled, now, _settings()) is False
    assert effective_interval_seconds(throttled, _settings()) == 3600

    # A short max-age must not drag a deliberately slow interval down.
    slow = await make_source(fetch_interval_minutes=120, cache_max_age_seconds=60)
    assert effective_interval_seconds(slow, _settings()) == 7200


async def test_backoff_doubles_per_consecutive_failure(make_source) -> None:
    settings = _settings()
    source = await make_source(fetch_interval_minutes=10)

    source.consecutive_failures = 0
    assert effective_interval_seconds(source, settings) == 600
    source.consecutive_failures = 1
    assert effective_interval_seconds(source, settings) == 1200
    source.consecutive_failures = 3
    assert effective_interval_seconds(source, settings) == 4800


async def test_backoff_clamps_at_the_ceiling(make_source) -> None:
    settings = _settings(scheduler_max_backoff_seconds=3600)
    source = await make_source(fetch_interval_minutes=10)
    source.consecutive_failures = 20
    assert effective_interval_seconds(source, settings) == 3600


async def test_backoff_ceiling_never_shortens_a_long_base_interval(make_source) -> None:
    """A source configured to poll weekly must not be dragged back to the
    6-hour backoff ceiling just because it failed once."""
    settings = _settings(scheduler_max_backoff_seconds=21_600)
    source = await make_source(fetch_interval_minutes=10_080)  # 7 days
    source.consecutive_failures = 5
    assert effective_interval_seconds(source, settings) == 10_080 * 60


async def test_disabled_and_archived_sources_are_never_polled(
    db_session, db_engine, make_source, local_http_server, allow_loopback
) -> None:
    _ConcurrencyHandler.reset()
    with local_http_server(_ConcurrencyHandler) as base_url:
        await make_source(feed_url=f"{base_url}/feed.xml", enabled=False)
        archived = await make_source(feed_url=f"{base_url}/feed.xml")
        archived.archived_at = datetime.now(UTC)
        await db_session.commit()

        session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
        outcomes = await run_tick(session_factory, _settings())

    assert outcomes == []
    assert (await db_session.scalar(select(func.count()).select_from(Article))) == 0


async def test_tick_respects_the_concurrency_limit(
    db_session, db_engine, make_source, local_http_server, allow_loopback
) -> None:
    _ConcurrencyHandler.reset()
    with local_http_server(_ConcurrencyHandler) as base_url:
        for n in range(6):
            await make_source(feed_url=f"{base_url}/feed-{n}.xml", publisher_group=f"g{n}")

        session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
        outcomes = await run_tick(session_factory, _settings(scheduler_max_concurrent_fetches=2))

    assert len(outcomes) == 6
    assert _ConcurrencyHandler.max_in_flight <= 2
    # ...and it really did overlap, or the bound above is vacuous.
    assert _ConcurrencyHandler.max_in_flight > 1


async def test_tick_isolates_a_failing_source(
    db_session, db_engine, make_source, local_http_server, allow_loopback
) -> None:
    _ConcurrencyHandler.reset()
    with local_http_server(_ConcurrencyHandler) as base_url:
        good = await make_source(feed_url=f"{base_url}/feed.xml", publisher_group="good")
        bad = await make_source(feed_url="http://127.0.0.1:1/feed.xml", publisher_group="bad")

        session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
        outcomes = await run_tick(session_factory, _settings())

    by_source = {o.source_id: o for o in outcomes}
    assert by_source[good.id].ok is True
    assert by_source[good.id].new_articles == 1
    assert by_source[bad.id].ok is False
    assert by_source[bad.id].error is not None

    db_session.expunge_all()
    assert (await db_session.get(type(good), good.id)).consecutive_failures == 0
    assert (await db_session.get(type(bad), bad.id)).consecutive_failures == 1


async def test_tick_stamps_last_fetched_at_so_a_source_is_not_repolled(
    db_session, db_engine, make_source, local_http_server, allow_loopback
) -> None:
    _ConcurrencyHandler.reset()
    with local_http_server(_ConcurrencyHandler) as base_url:
        await make_source(feed_url=f"{base_url}/feed.xml", fetch_interval_minutes=60)
        session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)

        first = await run_tick(session_factory, _settings())
        second = await run_tick(session_factory, _settings())

    assert len(first) == 1
    assert second == []  # not due again for another hour


class TestForcedRefresh:
    """The refresh button in the UI. Its whole job is to ignore the schedule,
    so the test that matters is the one proving it polls a source the scheduler
    would have skipped."""

    async def test_it_polls_a_source_that_is_not_due(
        self,
        db_session,
        db_engine,
        make_source,
        get_or_make_city,
        local_http_server,
        allow_loopback,
    ) -> None:
        _ConcurrencyHandler.reset()
        city = await get_or_make_city("nagpur")
        with local_http_server(_ConcurrencyHandler) as base_url:
            await make_source(
                feed_url=f"{base_url}/feed.xml", city="nagpur", fetch_interval_minutes=60
            )
            session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)

            first = await run_tick(session_factory, _settings())
            # The scheduler will not touch it again for an hour...
            assert (await run_tick(session_factory, _settings())) == []
            # ...but this is exactly what the button is for.
            refreshed = await refresh_city(session_factory, _settings(), city.id)

        assert len(first) == 1
        assert [source.name for source in refreshed] == ["Source 1"]
        assert refreshed[0].ok is True

    async def test_it_polls_only_the_named_city(
        self,
        db_session,
        db_engine,
        make_source,
        get_or_make_city,
        local_http_server,
        allow_loopback,
    ) -> None:
        _ConcurrencyHandler.reset()
        nagpur = await get_or_make_city("nagpur")
        with local_http_server(_ConcurrencyHandler) as base_url:
            await make_source(name="Nagpur Feed", feed_url=f"{base_url}/a.xml", city="nagpur")
            await make_source(name="Pune Feed", feed_url=f"{base_url}/b.xml", city="pune")
            session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
            refreshed = await refresh_city(session_factory, _settings(), nagpur.id)

        assert [source.name for source in refreshed] == ["Nagpur Feed"]

    async def test_it_skips_archived_and_disabled_sources(
        self,
        db_session,
        db_engine,
        make_source,
        get_or_make_city,
        local_http_server,
        allow_loopback,
    ) -> None:
        _ConcurrencyHandler.reset()
        city = await get_or_make_city("nagpur")
        with local_http_server(_ConcurrencyHandler) as base_url:
            await make_source(name="Live", feed_url=f"{base_url}/a.xml", city="nagpur")
            await make_source(
                name="Archived",
                feed_url=f"{base_url}/b.xml",
                city="nagpur",
                archived_at=datetime.now(UTC),
            )
            await make_source(
                name="Paused", feed_url=f"{base_url}/c.xml", city="nagpur", enabled=False
            )
            session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
            refreshed = await refresh_city(session_factory, _settings(), city.id)

        assert [source.name for source in refreshed] == ["Live"]

    async def test_one_failing_source_does_not_lose_the_others(
        self,
        db_session,
        db_engine,
        make_source,
        get_or_make_city,
        local_http_server,
        allow_loopback,
    ) -> None:
        """Same invariant as a scheduled tick: per-source failures are recorded
        and reported, never fatal."""
        _ConcurrencyHandler.reset()
        city = await get_or_make_city("nagpur")
        with local_http_server(_ConcurrencyHandler) as base_url:
            await make_source(name="Good", feed_url=f"{base_url}/feed.xml", city="nagpur")
            await make_source(name="Bad", feed_url="https://127.0.0.1:1/feed.xml", city="nagpur")
            session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
            refreshed = await refresh_city(session_factory, _settings(), city.id)

        by_name = {source.name: source for source in refreshed}
        assert by_name["Good"].ok is True
        assert by_name["Bad"].ok is False
        assert by_name["Bad"].error is not None

    async def test_a_city_with_no_live_sources_is_not_an_error(
        self, db_engine, get_or_make_city
    ) -> None:
        city = await get_or_make_city("nagpur")
        session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
        assert (await refresh_city(session_factory, _settings(), city.id)) == []

    async def test_an_unknown_city_raises(self, db_engine) -> None:
        session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
        with pytest.raises(CityNotFoundError):
            await refresh_city(session_factory, _settings(), 999)

    async def test_a_second_refresh_while_one_is_running_is_refused(
        self, db_engine, get_or_make_city, monkeypatch
    ) -> None:
        """The button is one click away from a second full round of requests at
        third-party publishers, so overlapping refreshes are refused."""
        city = await get_or_make_city("nagpur")
        session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)

        started = asyncio.Event()

        async def _slow_fetch(_targets, _settings):
            started.set()
            await asyncio.sleep(0.2)
            return []

        monkeypatch.setattr(scheduler, "fetch_targets", _slow_fetch)

        async def _one_target(_db, _city_id):
            return [object()]

        monkeypatch.setattr(scheduler, "load_city_targets", _one_target)

        async def _race():
            first = asyncio.create_task(refresh_city(session_factory, _settings(), city.id))
            await started.wait()
            with pytest.raises(RefreshInProgressError):
                await refresh_city(session_factory, _settings(), city.id)
            await first

        await _race()

        # ...and the guard is released, so the next click works.
        assert (await refresh_city(session_factory, _settings(), city.id)) is not None


async def test_loop_survives_a_failing_tick(monkeypatch) -> None:
    """A dead loop is the quiet failure that matters most: runs keep
    succeeding, they just score an ever-staler corpus."""
    calls = {"n": 0}

    async def _boom(_session_factory, _settings):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("tick exploded")
        return []

    monkeypatch.setattr(scheduler, "run_tick", _boom)

    async def _drive():
        task = asyncio.create_task(
            scheduler.scheduler_loop(None, _settings(scheduler_tick_seconds=0))
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    await _drive()
    assert calls["n"] >= 2  # kept ticking after the exception


async def test_loop_stops_cleanly_when_cancelled_mid_sleep(monkeypatch, caplog) -> None:
    """Shutdown cancels the task while it is parked in the inter-tick sleep,
    not inside a tick — so the CancelledError handler has to wrap the sleep
    too, or a clean stop goes unlogged."""

    async def _no_op(_session_factory, _settings):
        return []

    monkeypatch.setattr(scheduler, "run_tick", _no_op)

    async def _drive():
        task = asyncio.create_task(
            scheduler.scheduler_loop(None, _settings(scheduler_tick_seconds=30))
        )
        await asyncio.sleep(0.05)  # long tick interval: it is now in the sleep
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.INFO, logger="app.services.scheduler"):
        await _drive()

    assert "scheduler: stopped" in caplog.text
