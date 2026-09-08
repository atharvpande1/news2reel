import http.server
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.db.base import Base
from app.db.models import Source  # noqa: F401 — register on Base.metadata
from app.db.models.article import Article
from app.db.models.run import Run
from app.db.models.source import Source as SourceModel
from app.main import create_app
from app.services.similarity import content_hash

# Model weights are already on disk after the first run; avoid a Hub
# round-trip on every test session for speed and offline-safety.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
# This container runs under a tight memory budget shared with the IDE. Both
# suppress background worker processes that torch/tokenizers can otherwise
# spawn, which briefly doubles peak RSS (a second process re-importing torch)
# right when memory is already tightest.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")
# No GPU is used here, and this container's driver is too old for the CUDA
# build torch ships — probing it anyway spawns an extra subprocess (visible
# as a `multiprocessing.spawn` process re-importing torch, ~500MB) that can
# outlive a killed test run as an orphan. Skip the probe entirely.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")


@pytest.fixture
def db_engine():
    # StaticPool: a bare :memory: DB is per-connection, and the pool would
    # otherwise hand different callers different connections (and therefore
    # different, empty databases). Shared by db_session and, for the `client`
    # fixture, the app's own session_factory — see that fixture for why.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def db_session(db_engine):
    session_factory = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_engine, db_session):
    # session_factory bound to the same in-memory engine as db_session: the
    # app's startup reconciler opens its own session via app.state.session_
    # factory (lifespan runs outside dependency injection, see main.py), so
    # without this it would silently hit the real on-disk DB instead of the
    # test's.
    app = create_app(
        session_factory=sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    )

    def _get_db_override():
        yield db_session

    app.dependency_overrides[get_db] = _get_db_override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def make_source(db_session):
    counter = {"n": 0}

    def _make(**overrides):
        counter["n"] += 1
        defaults = {
            "name": f"Source {counter['n']}",
            "feed_url": f"https://example.test/feed-{counter['n']}.xml",
            "language": "en",
            "publisher_group": f"group-{counter['n']}",
        }
        defaults.update(overrides)
        source = SourceModel(**defaults)
        db_session.add(source)
        db_session.commit()
        db_session.refresh(source)
        return source

    return _make


@pytest.fixture
def make_run(db_session):
    def _make(**overrides):
        defaults = {"logical_date": date(2026, 1, 1)}
        defaults.update(overrides)
        run = Run(**defaults)
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)
        return run

    return _make


@pytest.fixture
def make_article(db_session):
    counter = {"n": 0}

    def _make(source: SourceModel, **overrides):
        counter["n"] += 1
        title = overrides.pop("title", f"Article {counter['n']}")
        summary = overrides.pop("summary", "")
        text = f"{title} {summary}".strip()
        defaults = {
            "id": f"article-{counter['n']}",
            "source_id": source.id,
            "url": f"https://example.test/a-{counter['n']}",
            "canonical_url": f"https://example.test/a-{counter['n']}",
            "title": title,
            "summary": summary,
            "language": "en",
            "feed_position": counter["n"],
            "content_hash": content_hash(text),
            "published_at": datetime.now(UTC),
        }
        defaults.update(overrides)
        article = Article(**defaults)
        db_session.add(article)
        db_session.commit()
        db_session.refresh(article)
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
    directly in test_net.py against real, unpatched validation."""
    import app.core.net as net

    monkeypatch.setattr(net, "_validate_public_url", lambda _url: None)
