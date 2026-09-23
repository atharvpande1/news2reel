import http.server
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import make_url, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.core.hashing import content_hash
from app.db.base import Base
from app.db.models import Source  # noqa: F401 — register on Base.metadata
from app.db.models.article import Article
from app.db.models.city import City
from app.db.models.source import Source as SourceModel
from app.db.models.user import User
from app.main import create_app


def resolve_test_database_url(suffix: str = "") -> str:
    """The throwaway database, from FEEDCAST_TEST_DATABASE_URL. Refuses the
    app's own database outright: every test ends in a TRUNCATE."""
    url = os.environ.get("FEEDCAST_TEST_DATABASE_URL")
    if not url:
        pytest.exit("FEEDCAST_TEST_DATABASE_URL is not set — see .env.local.example")
    if make_url(url) == make_url(get_settings().database_url):
        pytest.exit("FEEDCAST_TEST_DATABASE_URL must not be the app's database")
    parsed = make_url(url)
    return parsed.set(database=f"{parsed.database}{suffix}").render_as_string(hide_password=False)


async def recreate_database(url: str) -> None:
    """Drop and create `url`'s database via the maintenance database. CREATE
    DATABASE cannot run inside a transaction, hence AUTOCOMMIT."""
    parsed = make_url(url)
    admin = create_async_engine(
        parsed.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    await admin.dispose()


@pytest.fixture(scope="session")
async def db_engine():
    """One real Postgres database for the run, schema from the models.

    NullPool: no connection outlives the session that opened it, so nothing is
    left holding a lock when the per-test TRUNCATE runs. Migrations are run for
    real in test_migrations.py; here create_all is what keeps the suite fast.
    """
    url = resolve_test_database_url()
    await recreate_database(url)
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean_tables(db_engine):
    """Autouse, so it is set up first and torn down last — after db_session has
    closed. A TRUNCATE waiting on an open transaction would hang the suite."""
    yield
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with db_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def db_session(session_factory):
    async with session_factory() as session:
        yield session


@pytest.fixture
def app(session_factory, db_session):
    # session_factory bound to the test engine: the background loops and the
    # refresh endpoint open their own sessions via app.state.session_factory
    # (lifespan has no Depends), so without this they would silently hit the
    # app's own database instead of the test's.
    # start_background=False: the lifespan would otherwise launch the polling
    # loop (hitting real feed URLs) and the classifier loop (spending real
    # money). ASGITransport does not run the lifespan anyway.
    app = create_app(session_factory=session_factory, start_background=False)

    async def _get_db_override():
        yield db_session

    app.dependency_overrides[get_db] = _get_db_override
    yield app
    app.dependency_overrides.clear()


def _http_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=True,
    )


@pytest.fixture
async def anon_client(app):
    """No session at all — for tests of the auth layer itself."""
    async with _http_client(app) as test_client:
        yield test_client


@pytest.fixture
async def client(app, db_session):
    """Logged in, by overriding current_user with a real row. Everything except
    tests/test_auth.py is about what the API does, not who may call it, and
    test_auth's route-table walk is what proves the dependency is in place."""
    user = User(email="editor@example.test", password_hash="unused")
    db_session.add(user)
    await db_session.commit()
    app.dependency_overrides[current_user] = lambda: user
    async with _http_client(app) as test_client:
        yield test_client


@pytest.fixture
def get_or_make_city(db_session):
    """Cities are get-or-created from a slug so the source and article factories
    can keep taking `city="nagpur"` as they always did. Sources and articles now
    carry a city_id, but almost every test cares about *which* city, not about
    the row — so the string stays the interface and the row is an implementation
    detail of the fixture."""

    async def _get_or_make(slug: str, **overrides) -> City:
        normalized = slug.strip().casefold()
        existing = await db_session.scalar(select(City).where(City.slug == normalized))
        if existing is not None:
            return existing
        city = City(name=overrides.pop("name", normalized.title()), slug=normalized, **overrides)
        db_session.add(city)
        await db_session.commit()
        await db_session.refresh(city)
        return city

    return _get_or_make


@pytest.fixture
def make_city(get_or_make_city):
    counter = {"n": 0}

    async def _make(**overrides):
        counter["n"] += 1
        slug = overrides.pop("slug", f"city-{counter['n']}")
        return await get_or_make_city(slug, **overrides)

    return _make


@pytest.fixture
def make_source(db_session, get_or_make_city):
    counter = {"n": 0}

    async def _make(**overrides):
        counter["n"] += 1
        city = overrides.pop("city", "nagpur")
        defaults = {
            "name": f"Source {counter['n']}",
            "feed_url": f"https://example.test/feed-{counter['n']}.xml",
            "language": "en",
            "publisher_group": f"group-{counter['n']}",
            "fetch_interval_minutes": 30,
        }
        defaults.update(overrides)
        defaults["city_id"] = defaults.pop("city_id", None) or (await get_or_make_city(city)).id
        source = SourceModel(**defaults)
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)
        # Loaded so tests can read source.city (lazy="raise_on_sql").
        await db_session.refresh(source, ["city"])
        return source

    return _make


@pytest.fixture
def make_article(db_session, get_or_make_city):
    counter = {"n": 0}

    async def _make(source: SourceModel, **overrides):
        counter["n"] += 1
        title = overrides.pop("title", f"Article {counter['n']}")
        summary = overrides.pop("summary", "")
        text = f"{title} {summary}".strip()
        # The slug still names the city; the id is resolved from it the way
        # ingest does, so `city="pune"` against a Nagpur source is non-local for
        # the same reason it is in production.
        city_slug = overrides.pop("city", "nagpur")
        defaults = {
            "id": f"article-{counter['n']}",
            "source_id": source.id,
            "url": f"https://example.test/city/nagpur/a-{counter['n']}",
            "canonical_url": f"https://example.test/city/nagpur/a-{counter['n']}",
            "title": title,
            "summary": summary,
            "language": "en",
            "feed_position": counter["n"],
            "content_hash": content_hash(text),
            "city": city_slug,
            "city_id": (await get_or_make_city(city_slug)).id if city_slug else None,
            "published_at": datetime.now(UTC),
        }
        defaults.update(overrides)
        article = Article(**defaults)
        db_session.add(article)
        await db_session.commit()
        await db_session.refresh(article)
        return article

    return _make


@contextmanager
def _run_local_server(handler_class: type) -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture
def local_http_server():
    """Usage: `with local_http_server(MyHandlerClass) as base_url: ...` —
    spins up a real local HTTP server on an ephemeral port, torn down after."""
    return _run_local_server


@pytest.fixture
def allow_loopback(monkeypatch):
    """Bypasses core.net's loopback rejection for tests that deliberately
    target a local test server on 127.0.0.1 — the rejection itself is tested
    directly in test_net.py against real, unpatched validation.

    The validator is async — fetch_url routes through the same async core as
    fetch_url_async, so there is only one to patch."""
    import app.core.net as net

    async def _allow(_url: str) -> None:
        return None

    monkeypatch.setattr(net, "_validate_public_url", _allow)
